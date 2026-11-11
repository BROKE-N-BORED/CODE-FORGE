#!/usr/bin/env python3
"""Project security scanner (project-security-scan skill, item C6).

A deterministic, read-only, standard-library-only scan of a project
tree. It looks for committed-secret shapes, dangerous config files,
and a small set of risky code patterns, and prints a report to
stdout. Findings are data, not failure: the exit code is 0 whenever
the scan itself ran, 2 only when the scanner could not run at all
(bad root, unreadable tree).

Redaction is deliberate and total: a matched secret is reported as a
short prefix plus its length (e.g. AKIA...(20 chars)); the full
value never appears in the report, and .env files are reported by
name only — their contents are never even read.

This is a pattern-based pass, not a full audit. Code-pattern checks
are line-level heuristics and are labelled as such in the report.

Usage:
    python3 scan.py <project-root>           # JSON report (default)
    python3 scan.py <project-root> --text    # plain-text report

JSON shape:
    {"root": str, "files_scanned": int,
     "summary": {"HIGH": n, "MEDIUM": n, "LOW": n, "total": n},
     "findings": [{"severity", "category", "path", "line",
                   "message", "evidence"}, ...]}
Findings are sorted by severity (HIGH first), then path, then line.
"line" is 1-based, or None for file-level findings. "evidence" is
the redacted token for secret findings, the matched source line
(trimmed) for code-pattern findings, and "" where neither is safe
or useful to show.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

MAX_FILE_BYTES = 1_000_000  # files larger than this are skipped
SNIFF_BYTES = 8192          # binary sniff window

SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "env", ".env",
    "__pycache__", ".tox", ".mypy_cache", ".pytest_cache",
}

# Extensions whose contents get the code-pattern checks. Secret and
# filename checks apply to every text file regardless of extension.
CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".rb", ".php", ".sh",
    ".java", ".go", ".pl", ".lua",
}

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

# ------------------------------------------------------------------
# Secret shapes. Each pattern yields (category, message, redaction).
# ------------------------------------------------------------------

PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")

TOKEN_PATTERNS = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
     "secret", "AWS access key id in source"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
     "secret", "GitHub token in source"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
     "secret", "API key (sk-...) in source"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
     "secret", "Slack token in source"),
]

ASSIGNMENT_RE = re.compile(
    r"""(?i)\b([a-z0-9_$]*(?:password|passwd|secret|api[_-]?key|apikey|access[_-]?key|auth[_-]?token|access[_-]?token|client[_-]?secret)[a-z0-9_$]*)"""
    r"""\s*[:=]\s*["']([^"'\n]{8,})["']""")

# Values that are obviously placeholders, not real secrets.
_PLACEHOLDER_WORDS = (
    "changeme", "change-me", "example", "placeholder", "your-", "your_",
    "insert-", "insert_", "replace-", "replace_", "todo", "none", "null",
    "xxxx", "****", "....", "<", "${", "os.environ", "environ", "getenv",
    "dummy", "sample", "test-only", "not-a-real", "fake",
)


def _is_placeholder(value: str) -> bool:
    low = value.strip().lower()
    if not low:
        return True
    if any(w in low for w in _PLACEHOLDER_WORDS):
        return True
    if len(set(low)) <= 2:          # "aaaaaaaa", "--------"
        return True
    return False


def _redact(token: str, keep: int = 4) -> str:
    """The only form in which a secret may appear in the report."""
    return f"{token[:keep]}...({len(token)} chars)"


# ------------------------------------------------------------------
# Risky code patterns (line-level heuristics, labelled as such).
# ------------------------------------------------------------------

def _eval_exec(line: str):
    m = re.search(r"\b(eval|exec)\s*\(\s*(\S)", line)
    if m and m.group(2) not in "\"'":
        return f"{m.group(1)}() called on a non-literal argument (heuristic)"
    return None


def _shell_true(line: str):
    if re.search(r"\bshell\s*=\s*True\b", line):
        return "shell=True passed to a subprocess-style call (heuristic)"
    return None


def _pickle(line: str):
    if re.search(r"\bpickle\.loads?\s*\(", line):
        return "pickle.load/loads — unpickling untrusted data runs arbitrary code (heuristic)"
    return None


def _sql_format(line: str):
    if ".execute(" not in line:
        return None
    if (re.search(r"\.execute\s*\(\s*f[\"']", line)
            or re.search(r"\.execute\s*\([^)]*%", line)
            or re.search(r"\.execute\s*\([^)]*\.format\s*\(", line)
            or re.search(r"\.execute\s*\([^)]*\+", line)):
        return "SQL built by string formatting/concatenation into execute() (heuristic)"
    return None


_BIND_HINTS = ("bind", "host", "listen", ".run(", "serve", "httpserver",
              "uvicorn", "gunicorn", "addr")


def _bind_all(line: str):
    if "0.0.0.0" in line and any(h in line.lower() for h in _BIND_HINTS):
        return "hardcoded bind to 0.0.0.0 — service reachable on every interface (heuristic)"
    return None


CODE_CHECKS = [
    (_eval_exec, "MEDIUM"),
    (_shell_true, "MEDIUM"),
    (_pickle, "MEDIUM"),
    (_sql_format, "MEDIUM"),
    (_bind_all, "MEDIUM"),
]

ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")


def _finding(severity, category, path, line, message, evidence=""):
    return {"severity": severity, "category": category, "path": path,
            "line": line, "message": message, "evidence": evidence}


def _scan_lines(rel: str, lines: list[str], is_code: bool,
                findings: list[dict]) -> None:
    for lineno, line in enumerate(lines, start=1):
        if PRIVATE_KEY_RE.search(line):
            findings.append(_finding(
                "HIGH", "secret", rel, lineno,
                "private key block in source — key material must not be committed",
                "PRIVATE KEY block"))
        token_hit = False
        for pattern, category, message in TOKEN_PATTERNS:
            for m in pattern.finditer(line):
                token_hit = True
                findings.append(_finding(
                    "HIGH", category, rel, lineno, message,
                    _redact(m.group(0))))
        # One finding per secret per line: a value already caught
        # by a specific token shape is not reported again as a
        # generic assignment.
        m = None if token_hit else ASSIGNMENT_RE.search(line)
        if m and not _is_placeholder(m.group(2)):
            findings.append(_finding(
                "HIGH", "secret", rel, lineno,
                f"hardcoded value assigned to '{m.group(1).lower()}'",
                _redact(m.group(2), keep=2)))
        if is_code:
            for check, severity in CODE_CHECKS:
                message = check(line)
                if message:
                    findings.append(_finding(
                        severity, "code-pattern", rel, lineno, message,
                        line.strip()[:120]))


def _git_remote_findings(root: Path) -> list[dict]:
    """Report a .git remote by URL, credentials stripped. The .git
    internals are otherwise never scanned."""
    config = root / ".git" / "config"
    findings: list[dict] = []
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    for m in re.finditer(r"(?m)^\s*url\s*=\s*(\S+)\s*$", text):
        url = m.group(1)
        # Strip any userinfo (user or user:token@) before reporting.
        url = re.sub(r"^(https?://)[^/@\s]+@", r"\1", url)
        url = re.sub(r"^[^/@\s]+@", "", url) if "://" not in url and "@" in url else url
        findings.append(_finding(
            "LOW", "config", ".git/config", None,
            f"git repository with remote: {url} (reported, not judged)",
            url))
    return findings


def scan(root: Path) -> dict:
    findings: list[dict] = []
    files_scanned = 0
    findings.extend(_git_remote_findings(root))
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            low = name.lower()
            # --- filename / presence checks (contents never read) ---
            if low == ".env" or (low.startswith(".env.")
                                 and not low.endswith(ENV_TEMPLATE_SUFFIXES)):
                findings.append(_finding(
                    "MEDIUM", "config", rel, None,
                    ".env file present in the project — confirm it is "
                    "gitignored and never committed (contents not read)"))
                continue  # never open an env file
            if low.startswith("credentials") or low.startswith("secret"):
                findings.append(_finding(
                    "MEDIUM", "config", rel, None,
                    "credentials/secret-named file in the project — "
                    "confirm it holds no live secrets and is gitignored"))
            # --- content checks ---
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:SNIFF_BYTES]:
                continue  # binary — sniffed and skipped
            files_scanned += 1
            text = raw.decode("utf-8", errors="replace")
            _scan_lines(rel, text.splitlines(),
                        path.suffix.lower() in CODE_EXTS, findings)
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]],
                                 f["path"], f["line"] or 0))
    summary = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in findings:
        summary[f["severity"]] += 1
    summary["total"] = len(findings)
    return {"root": str(root), "files_scanned": files_scanned,
            "summary": summary, "findings": findings}


def _render_text(report: dict) -> str:
    out = [f"Security scan of {report['root']}",
           f"Files scanned: {report['files_scanned']}"]
    s = report["summary"]
    out.append(f"Findings: {s['total']} "
               f"(HIGH {s['HIGH']}, MEDIUM {s['MEDIUM']}, LOW {s['LOW']})")
    for f in report["findings"]:
        where = f["path"] + (f":{f['line']}" if f["line"] else "")
        line = f"[{f['severity']}] {where} — {f['message']}"
        if f["evidence"]:
            line += f"  [{f['evidence']}]"
        out.append(line)
    if not report["findings"]:
        out.append("Clean: no findings.")
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    text_mode = "--text" in argv
    if len(args) != 1:
        print("usage: scan.py <project-root> [--text]", file=sys.stderr)
        return 2
    root = Path(args[0]).expanduser()
    if not root.is_dir():
        print(f"scan.py: not a directory: {root}", file=sys.stderr)
        return 2
    try:
        report = scan(root.resolve())
    except Exception as exc:  # scanner error — findings are not this
        print(f"scan.py: scan failed: {exc}", file=sys.stderr)
        return 2
    if text_mode:
        sys.stdout.write(_render_text(report))
    else:
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
