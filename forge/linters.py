"""Linter layer, light edition (Tier 2 #1 — the review study).

A hosted reviewer runs ~60 linter/SAST tools in a cloud microVM.
Forge's machine cannot, and must not install anything for this: the layer
runs ONLY the linters already installed on the project (Ruff,
ShellCheck, ESLint when present), on demand, and hands their
findings to the Reviewer as CR2-format lines with the tool named
as the Source — the Reviewer writes them up as findings instead
of re-deriving them. It extends the C6 security scan skill
(skills/project-security-scan/ — a deterministic read-only scan),
it does not replace it: C6 hunts secrets and risky patterns, this
runs the project's own linters.

Rules that keep it Forge-scale:
  * detection is shutil.which — a missing tool is a plain note
    ("Ruff not installed"), never an error and never an install;
  * each tool runs with a timeout and a file cap, on the project
    as it stands; a tool that times out or returns unreadable
    output is reported as such and the other tools still run;
  * findings are capped (MAX_FINDINGS) with a truthful "+N more"
    tail — the full list lives in the tool's own output, run it
    directly for all of it;
  * severity maps onto the CR2 scale (Critical/Major/Minor/
    Trivial) by fixed documented rules per tool — no model call,
    no guessing;
  * nothing here writes, installs, or auto-fixes. On demand
    only: the Review room's "Run linters" button (forge/main.py
    ReviewWindow) loads the findings into the composer for the
    next Review turn. Nothing runs on its own.

Pure stdlib. Every function is safe to call with a missing root,
a missing tool, or garbage output — it returns a note, never
raises."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

#: Seconds a single tool gets before it is reported as timed out.
TOOL_TIMEOUT = 60

#: Most files one tool is handed (ShellCheck/ESLint take file
#: lists; Ruff takes the root itself).
MAX_FILES_PER_TOOL = 50

#: Most findings shown; the rest are counted in the tail line.
MAX_FINDINGS = 50

#: Directories a file walk never descends into.
SKIP_DIRS = {".git", ".forge", "node_modules", "__pycache__",
             ".venv", "venv", ".tox", ".mypy_cache",
             ".pytest_cache"}

#: Ruff codes that read as style nits rather than bugs.
RUFF_STYLE_CODES = {"E501", "W291", "W292", "W293", "W391", "W605"}

#: CR2 severity order, for sorting Major-first.
_SEVERITY_ORDER = {"Critical": 0, "Major": 1, "Minor": 2,
                   "Trivial": 3}


def detect_tools() -> dict:
    """{tool name: executable path or None} for the three tools
    this layer knows. A missing tool is None, never an error."""
    return {name: shutil.which(name)
            for name in ("ruff", "shellcheck", "eslint")}


def _collect_files(root: Path, suffixes, cap=MAX_FILES_PER_TOOL):
    """Project files with one of `suffixes`, in path order,
    skipping SKIP_DIRS, capped at `cap`. Never raises."""
    try:
        out: list[Path] = []
        for path in sorted(root.rglob("*")):
            if len(out) >= cap:
                break
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in suffixes:
                out.append(path)
        return out
    except Exception:
        return []


def _run(cmd, timeout=TOOL_TIMEOUT):
    """Run one linter command. Returns (stdout, stderr, returncode)
    — or (None, note, None) when it could not run at all (missing
    binary, timeout). A linter exit code of 1 means "findings",
    not failure, so the caller treats 0 and 1 alike. Never raises."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
        return proc.stdout or "", proc.stderr or "", proc.returncode
    except FileNotFoundError:
        return None, "not installed", None
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s", None
    except Exception as exc:
        return None, f"could not run: {exc}", None


def _finding(tool, severity, ftype, rel, line, message, fix):
    return {"tool": tool, "severity": severity, "type": ftype,
            "file": rel, "line": line or None,
            "message": (message or "").strip(),
            "fix": (fix or "").strip()}


def parse_ruff_output(text: str, root) -> list[dict]:
    """Ruff --output-format=json into findings. F-codes and E9x
    (syntax/undefined-name class errors) read as Major; style
    codes (E501, trailing whitespace, ...) as Nitpick; the rest
    as Minor. Never raises."""
    out: list[dict] = []
    try:
        items = json.loads(text or "[]")
    except Exception:
        return out
    if not isinstance(items, list):
        return out
    root = Path(root)
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")
        location = item.get("location") or {}
        filename = str(item.get("filename") or "")
        try:
            rel = str(Path(filename).relative_to(root))
        except Exception:
            rel = filename
        message = str(item.get("message") or "")
        fix_obj = item.get("fix") or {}
        fix = ""
        if isinstance(fix_obj, dict):
            fix = str(fix_obj.get("message") or "")
        if not fix:
            fix = f"see {code} (Ruff)" if code else "see Ruff"
        if code.startswith("F") or code.startswith("E9"):
            severity, ftype = "Major", "Bug"
        elif code in RUFF_STYLE_CODES:
            severity, ftype = "Minor", "Nitpick"
        else:
            severity, ftype = "Minor", "Bug"
        out.append(_finding("Ruff", severity, ftype, rel,
                            location.get("row"), message, fix))
    return out


def parse_shellcheck_output(text: str, root) -> list[dict]:
    """ShellCheck -f json into findings. level error -> Major,
    warning -> Minor, info/style -> Trivial; style reads as
    Nitpick, the rest as Bug. Never raises."""
    out: list[dict] = []
    try:
        data = json.loads(text or "{}")
    except Exception:
        return out
    comments = data.get("comments") if isinstance(data, dict) else None
    if not isinstance(comments, list):
        return out
    root = Path(root)
    for item in comments:
        if not isinstance(item, dict):
            continue
        level = str(item.get("level") or "")
        code = str(item.get("code") or "")
        filename = str(item.get("file") or "")
        try:
            rel = str(Path(filename).relative_to(root))
        except Exception:
            rel = filename
        message = str(item.get("message") or "")
        fix = item.get("fix")
        if isinstance(fix, dict):
            fix = str(fix.get("message") or "")
        if not isinstance(fix, str) or not fix:
            fix = f"see SC{code} (ShellCheck)" if code \
                else "see ShellCheck"
        if level == "error":
            severity, ftype = "Major", "Bug"
        elif level == "warning":
            severity, ftype = "Minor", "Bug"
        elif level == "style":
            severity, ftype = "Trivial", "Nitpick"
        else:
            severity, ftype = "Trivial", "Bug"
        out.append(_finding("ShellCheck", severity, ftype, rel,
                            item.get("line"), message, fix))
    return out


def parse_eslint_output(text: str, root) -> list[dict]:
    """ESLint --format json into findings. severity 2 (error) ->
    Major, 1 (warning) -> Minor; all read as Bug. A missing rule
    id reads as "see ESLint". Never raises."""
    out: list[dict] = []
    try:
        items = json.loads(text or "[]")
    except Exception:
        return out
    if not isinstance(items, list):
        return out
    root = Path(root)
    for result in items:
        if not isinstance(result, dict):
            continue
        filename = str(result.get("filePath") or "")
        try:
            rel = str(Path(filename).relative_to(root))
        except Exception:
            rel = filename
        messages = result.get("messages")
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            rule = str(msg.get("ruleId") or "")
            severity = "Major" if msg.get("severity") == 2 \
                else "Minor"
            fix = str((msg.get("fix") or {}).get("text") or "")
            if not fix:
                fix = f"see {rule} (ESLint)" if rule \
                    else "see ESLint"
            out.append(_finding(
                "ESLint", severity, "Bug", rel, msg.get("line"),
                str(msg.get("message") or ""), fix))
    return out


def format_findings(findings) -> str:
    """Findings as CR2-format lines, Major-first, then file, then
    line — the exact shape the Reviewer writes up, with the tool
    as the Source. Capped at MAX_FINDINGS with a truthful tail.
    Empty reads as "No linter findings.". Never raises."""
    try:
        items = sorted(findings or [],
                       key=lambda f: (
                           _SEVERITY_ORDER.get(f.get("severity"), 9),
                           str(f.get("file") or ""),
                           f.get("line") or 0))
    except Exception:
        return "No linter findings."
    if not items:
        return "No linter findings."
    lines = []
    for f in items[:MAX_FINDINGS]:
        place = str(f.get("file") or "")
        if f.get("line"):
            place += f":{f['line']}"
        lines.append(
            f"- [{f.get('severity')} · {f.get('type')} · "
            f"Source: {f.get('tool')}] {place} — "
            f"{f.get('message')} — Fix: {f.get('fix')}")
    if len(items) > MAX_FINDINGS:
        lines.append(
            f"+{len(items) - MAX_FINDINGS} more linter finding(s) "
            f"not shown — run the tool directly for the full list.")
    return "\n".join(lines)


def run_linters(root, timeout=TOOL_TIMEOUT) -> dict:
    """Run the installed linters on a project, on demand.
    Returns {tools: {name: status}, findings: [finding dicts],
    notes: [str]}. tools maps each known tool to "ran (N
    finding(s))", "not installed", "no files", "timed out", or
    "unreadable output". A project with no linters installed
    returns no findings and the plain note — never an error.
    Never raises."""
    result: dict = {"tools": {}, "findings": [], "notes": []}
    try:
        root_path = Path(root)
    except Exception:
        result["notes"].append("No project open — open one first.")
        return result
    if not root_path.is_dir():
        result["notes"].append("No project open — open one first.")
        return result
    detected = detect_tools()
    all_findings: list[dict] = []

    if detected.get("ruff"):
        out, _err, code = _run(
            [detected["ruff"], "check", str(root_path),
             "--output-format=json"], timeout)
        if out is None:
            result["tools"]["Ruff"] = _err
        elif code not in (0, 1):
            result["tools"]["Ruff"] = "unreadable output"
        else:
            found = parse_ruff_output(out, root_path)
            all_findings.extend(found)
            result["tools"]["Ruff"] = f"ran ({len(found)} finding(s))"
    else:
        result["tools"]["Ruff"] = "not installed"

    if detected.get("shellcheck"):
        files = _collect_files(root_path, {".sh"})
        if not files:
            result["tools"]["ShellCheck"] = "no files"
        else:
            out, _err, code = _run(
                [detected["shellcheck"], "-f", "json"]
                + [str(f) for f in files], timeout)
            if out is None:
                result["tools"]["ShellCheck"] = _err
            elif code not in (0, 1):
                result["tools"]["ShellCheck"] = "unreadable output"
            else:
                found = parse_shellcheck_output(out, root_path)
                all_findings.extend(found)
                result["tools"]["ShellCheck"] = (
                    f"ran ({len(found)} finding(s))")
    else:
        result["tools"]["ShellCheck"] = "not installed"

    if detected.get("eslint"):
        files = _collect_files(
            root_path, {".js", ".jsx", ".ts", ".tsx"})
        if not files:
            result["tools"]["ESLint"] = "no files"
        else:
            out, _err, code = _run(
                [detected["eslint"], "--format", "json"]
                + [str(f) for f in files], timeout)
            if out is None:
                result["tools"]["ESLint"] = _err
            elif code not in (0, 1):
                result["tools"]["ESLint"] = "unreadable output"
            else:
                found = parse_eslint_output(out, root_path)
                all_findings.extend(found)
                result["tools"]["ESLint"] = (
                    f"ran ({len(found)} finding(s))")
    else:
        result["tools"]["ESLint"] = "not installed"

    result["findings"] = all_findings
    if not any(status.startswith("ran")
               for status in result["tools"].values()):
        result["notes"].append(
            "No linters installed — Ruff, ShellCheck and ESLint "
            "were all missing; install one to use this.")
    return result
