"""Secret shapes, shared (pre-send scan, 2026-11-12).

The ONE source of truth for what "looks like a secret" in Forge.
The patterns moved here verbatim from the project-security-scan
skill's bundled scanner (skills/project-security-scan/scan.py,
item C6) so the on-demand skill and the pre-send check can never
drift apart: scan.py imports this module, and the Provider send
path scans outbound text through it before anything leaves the
machine.

Standard library only, no Qt: scan.py runs as a standalone
script and the Provider layer must stay importable without a
display. forge/__init__.py is version-only, so importing this
module can never drag in Qt.

What this module covers — SECRET SHAPES ONLY:

- private key blocks (PEM BEGIN lines)
- token shapes: AWS access key ids, GitHub tokens, sk- API
  keys, Slack tokens
- hardcoded values assigned to password/secret/api-key-style
  names (obvious placeholders skipped)

What it deliberately does NOT cover:

- .env presence: that is a FILE-level check (a file named
  .env exists in the project). It has no meaning over bare
  text and stays in scan.py.
- Code-pattern heuristics (eval/exec, shell=True, pickle,
  SQL formatting, 0.0.0.0 binds): those are line-level
  judgments about code, not secret shapes. They stay in
  scan.py; the pre-send scan never runs them.

Redaction is total: a matched secret is reported as a short
prefix plus its length (e.g. AKIA...(20 chars)) and the full
value NEVER appears in a finding, a log line, a dialog, or a
redacted text. The one exception to the prefix form is the
private-key finding, whose evidence is the literal string
"PRIVATE KEY block" — no part of the key is shown at all.

Findings carry character spans (start, end) into the scanned
text so redact_text() can remove exactly what matched. The
private-key span covers the WHOLE PEM block (BEGIN line
through the matching END line; to the end of the text when
no END line is found) — redacting only the BEGIN line would
send the key material. An assignment finding's span covers
only the VALUE, never the variable name.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ------------------------------------------------------------------
# Secret shapes — moved verbatim from scan.py (item C6). Do not
# edit a pattern here without re-running ProjectSecurityScanTests:
# the C6 fixture pins the exact per-line behavior.
# ------------------------------------------------------------------

PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")

_PRIVATE_KEY_END_RE = re.compile(
    r"-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")

# (pattern, finding name, message) — order is the report order.
TOKEN_PATTERNS = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
     "aws-key", "AWS access key id in source"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
     "github-token", "GitHub token in source"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
     "sk-key", "API key (sk-...) in source"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
     "slack-token", "Slack token in source"),
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


def is_placeholder(value: str) -> bool:
    low = value.strip().lower()
    if not low:
        return True
    if any(w in low for w in _PLACEHOLDER_WORDS):
        return True
    if len(set(low)) <= 2:          # "aaaaaaaa", "--------"
        return True
    return False


def redact(token: str, keep: int = 4) -> str:
    """The only form in which a secret may appear anywhere."""
    return f"{token[:keep]}...({len(token)} chars)"


def _private_key_end(text: str, begin_end: int) -> int:
    """End offset of the PEM block whose BEGIN line ends at
    begin_end: the end of the matching END line, or the end of
    the text when the block is truncated (fail closed — an
    unterminated block redacts to the end rather than leaking
    its body)."""
    match = _PRIVATE_KEY_END_RE.search(text, begin_end)
    if match is None:
        return len(text)
    newline = text.find("\n", match.end())
    return len(text) if newline < 0 else newline


def scan_text(text: str) -> list[dict]:
    """Secret-shape findings in TEXT, in scan order (line order;
    within a line: private key, then tokens in TOKEN_PATTERNS
    order, then the assignment — exactly scan.py's per-line
    rules, so the C6 report is unchanged).

    Each finding: {"pattern", "message", "evidence", "start",
    "end"}. "evidence" is redacted (or the literal "PRIVATE KEY
    block"); "start"/"end" are character offsets into TEXT for
    redact_text(). One finding per secret per line: a value
    already caught by a token shape is not reported again as a
    generic assignment.
    """
    text = str(text or "")
    findings: list[dict] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        key = PRIVATE_KEY_RE.search(line)
        if key is not None:
            findings.append({
                "pattern": "private-key",
                "message": "private key block in source — key "
                           "material must not be committed",
                "evidence": "PRIVATE KEY block",
                "start": line_start + key.start(),
                "end": _private_key_end(
                    text, line_start + key.end()),
            })
        token_hit = False
        for pattern, name, message in TOKEN_PATTERNS:
            for match in pattern.finditer(line):
                token_hit = True
                findings.append({
                    "pattern": name,
                    "message": message,
                    "evidence": redact(match.group(0)),
                    "start": line_start + match.start(),
                    "end": line_start + match.end(),
                })
        # One finding per secret per line (see the docstring).
        match = None if token_hit else ASSIGNMENT_RE.search(line)
        if match is not None and not is_placeholder(match.group(2)):
            findings.append({
                "pattern": "hardcoded-assignment",
                "message": "hardcoded value assigned to "
                           f"'{match.group(1).lower()}'",
                "evidence": redact(match.group(2), keep=2),
                "start": line_start + match.start(2),
                "end": line_start + match.end(2),
            })
    return findings


def finding_line(text: str, finding: dict,
                 findings: list[dict] | None = None) -> str:
    """The line of TEXT containing the finding, with EVERY secret
    span on that line replaced by "[redacted]" — the anchor for
    the allow-list and the "where" shown in the owner dialog.
    Never contains a secret value. FINDINGS is the full finding
    set for the scanned text (the caller that has it passes it):
    a line holding two secrets must not leak the sibling through
    either finding's anchor (security review, 2026-11-12). With
    FINDINGS omitted only the finding's own span redacts — the
    original behavior, kept for callers with one finding.
    A private-key finding's line is its BEGIN line verbatim: the
    header names only the key type (RSA, EC, ...) — it holds no
    key material — and it keeps one key type's anchor distinct
    from another's. But a line strictly INSIDE a PEM block is key
    material, so it redacts whole: a token-shaped string inside a
    block must not leave the rest of the body line in the anchor.
    The END header line stays verbatim like the BEGIN line."""
    text = str(text or "")
    start = finding["start"]
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    if line_end < 0:
        line_end = len(text)
    line = text[line_start:line_end]
    pool = list(findings) if findings is not None else [finding]
    # A body line inside a PEM block is key material — redact the
    # whole line. The BEGIN/END header lines hold no material and
    # stay verbatim (the END line is recognized by its own header,
    # so a truncated block with no END line still fails closed).
    if _PRIVATE_KEY_END_RE.search(line) is None:
        for other in pool:
            if other.get("pattern") != "private-key":
                continue
            if (other["start"] < line_start
                    and line_end <= other["end"]):
                return "[redacted]"
    # Every secret span on this line redacts — the finding's own
    # and any sibling's. A private-key finding's own span is
    # skipped here: its line is the BEGIN header (see above), and
    # a body line was already handled by the whole-line rule.
    spans = []
    for other in pool:
        if other.get("pattern") == "private-key":
            continue
        span_start = max(other["start"], line_start) - line_start
        span_end = min(other["end"], line_end) - line_start
        if span_start < span_end:
            spans.append((span_start, span_end))
    if not spans:
        return line
    spans.sort()
    merged = [spans[0]]
    for span_start, span_end in spans[1:]:
        if span_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0],
                          max(merged[-1][1], span_end))
        else:
            merged.append((span_start, span_end))
    for span_start, span_end in reversed(merged):
        line = line[:span_start] + "[redacted]" + line[span_end:]
    return line


def _disjoint(findings: list[dict]) -> list[dict]:
    """Findings with overlaps resolved: a finding fully inside
    another finding's span is dropped (the outer span already
    covers it — e.g. a token-shaped string inside a PEM block).
    Kept in scan order."""
    kept: list[dict] = []
    for finding in sorted(findings,
                          key=lambda f: (f["start"],
                                         -(f["end"] - f["start"]))):
        start, end = finding["start"], finding["end"]
        if any(start < kept_end and end > kept_start
               for kept_start, kept_end in
               ((k["start"], k["end"]) for k in kept)):
            continue
        kept.append(finding)
    kept.sort(key=lambda f: f["start"])
    return kept


def redact_text(text: str, findings: list[dict]) -> str:
    """TEXT with every finding's span replaced by "[redacted]".
    Everything outside the spans is byte-identical. Overlapping
    spans resolve to the outermost one (see _disjoint)."""
    text = str(text or "")
    if not findings:
        return text
    out = text
    for finding in sorted(_disjoint(findings),
                          key=lambda f: f["start"], reverse=True):
        start = max(0, finding["start"])
        end = min(len(out), finding["end"])
        if start >= end:
            continue
        out = out[:start] + "[redacted]" + out[end:]
    return out


# ---------- Per-project allow-list (item 5, 2026-11-12) ----------
#
# One file per project, in OWNER SETTINGS — never in the project
# (security review, 2026-11-12): project content is untrusted
# under S3's model, and a planted .forge/presend-allowlist.json
# in a cloned repo would silently exempt a secret. The list
# lives at <app_dir>/presend-allowlist/<key>.json where <key>
# is 16 hex chars of the sha256 of the resolved workspace path
# (the checkpoints.py keying pattern), so it survives the
# project being moved only when the path is unchanged — a moved
# project starts with an empty list, which fails closed.
# An entry is {pattern, anchor, reason, added} where the anchor
# is the finding's full LINE with every secret span on it
# replaced by "[redacted]" — specific enough to tell two
# same-shaped secrets apart, and it never stores a secret
# value. Matching is pattern + anchor. No expiry in v1: an
# entry stands until the owner removes it (Settings → Secret
# allow-list…), and removal re-blocks immediately — the scan
# re-reads the file on every send. Every exemption is recorded
# as a presend_allowed event (see the worker's drain) — a
# silent exemption is not acceptable for a security control.

# Overridable for tests only: production always uses the
# standard app dir (the same path forge/main.py's APP_DIR and
# forge/vault.py compute).
APP_DIR_OVERRIDE = None


def app_dir() -> Path:
    """Forge's per-user app dir (or the test override)."""
    if APP_DIR_OVERRIDE is not None:
        return Path(APP_DIR_OVERRIDE)
    return (Path.home() / ".local" / "share"
            / "linux-forge-agent-shell")


def allowlist_path(workspace) -> Path | None:
    """The allow-list file for WORKSPACE, or None without one."""
    if not workspace:
        return None
    try:
        resolved = str(Path(workspace).expanduser().resolve())
    except (OSError, RuntimeError):
        resolved = str(workspace)
    key = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return app_dir() / "presend-allowlist" / f"{key}.json"


def load_allowlist(workspace) -> list[dict]:
    """The project's allow-list entries ([] when the file is
    missing, unreadable, or corrupt — a broken list fails closed:
    the scan still runs, the entries just aren't there)."""
    path = allowlist_path(workspace)
    if path is None:
        return []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
    except (OSError, ValueError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pattern = entry.get("pattern")
        anchor = entry.get("anchor")
        if not isinstance(pattern, str) or not pattern:
            continue
        if not isinstance(anchor, str) or not anchor:
            continue
        out.append({"pattern": pattern, "anchor": anchor,
                    "reason": str(entry.get("reason") or ""),
                    "added": str(entry.get("added") or "")})
    return out


def _atomic_write(path: Path, content: str) -> None:
    """Write CONTENT over PATH atomically — the tree's pattern
    (forge/learnings.py): temp file in the SAME folder, fsync,
    os.replace, so a crash mid-write leaves the old file
    standing whole. Permission bits ride across the replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = None
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_allowlist(workspace, entries) -> None:
    """Write ENTRIES as the project's allow-list (atomic). A
    missing workspace is a no-op: there is nowhere to keep it.
    The file also records its workspace path (not a secret) so
    a human can tell which hashed file belongs to which project."""
    path = allowlist_path(workspace)
    if path is None:
        return
    content = json.dumps({"workspace": str(workspace),
                          "entries": list(entries or [])},
                         indent=2, sort_keys=True) + "\n"
    _atomic_write(path, content)


def is_allowed(finding, entries) -> bool:
    """Whether FINDING matches an allow-list entry — pattern +
    anchor (the finding's redacted line)."""
    pattern = (finding or {}).get("pattern")
    anchor = (finding or {}).get("line")
    return any(entry.get("pattern") == pattern
               and entry.get("anchor") == anchor
               for entry in entries or [])


def partition_allowed(findings, workspace) -> tuple[list, list]:
    """FINDINGS split into (remaining, allowed): the allowed ones
    are the entries the project's allow-list covers. Without a
    workspace there is no list to consult, so nothing is
    filtered — fail closed. The caller logs the allowed half as
    presend_allowed events; an exemption must never be silent
    (security review, 2026-11-12)."""
    if not findings:
        return [], []
    entries = load_allowlist(workspace)
    if not entries:
        return list(findings), []
    remaining, allowed = [], []
    for finding in findings:
        (allowed if is_allowed(finding, entries) else remaining
         ).append(finding)
    return remaining, allowed


def filter_allowed(findings, workspace) -> list[dict]:
    """FINDINGS minus the ones the project's allow-list covers.
    Without a workspace there is no list to consult, so nothing
    is filtered — fail closed. Prefer partition_allowed when the
    caller must log the exemptions it makes."""
    remaining, _allowed = partition_allowed(findings, workspace)
    return remaining


def add_allowlist_entry(workspace, pattern, anchor, reason=""):
    """Record one allow-list entry (a no-op when the same
    pattern + anchor is already listed). Returns the entries."""
    entries = load_allowlist(workspace)
    if not any(entry.get("pattern") == pattern
               and entry.get("anchor") == anchor
               for entry in entries):
        entries.append({
            "pattern": str(pattern or ""),
            "anchor": str(anchor or ""),
            "reason": str(reason or ""),
            "added": datetime.now(timezone.utc).isoformat()})
        save_allowlist(workspace, entries)
    return entries


def remove_allowlist_entry(workspace, pattern, anchor):
    """Drop the entry for PATTERN + ANCHOR. Returns the entries."""
    entries = [entry for entry in load_allowlist(workspace)
               if not (entry.get("pattern") == pattern
                       and entry.get("anchor") == anchor)]
    save_allowlist(workspace, entries)
    return entries
