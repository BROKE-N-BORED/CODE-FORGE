"""Project Guidelines (CR4 — CodeRabbit path instructions, 2026-11-11).

CodeRabbit reads a repo's guideline files and enforces them in
review. Forge's version is smaller and gated, because the naive
shape is a hole: guideline files arrive with the project, and a
project can be cloned from anywhere — an AGENTS.md dropped into a
repo by someone else must never reach the agent's system prompt on
its own. That is the prompt-injection shape, and Forge already has
the answer to it: the knowledge store's owner gate
(forge/knowledge.py — drafts wait, only approved entries reach a
turn). Guidelines follow the same rule.

Detection (read-only, never raises): in the PROJECT workspace
root, and nested up to 2 levels down so folder-scoped rules stay
scoped:

  AGENTS.md, CLAUDE.md, .cursorrules,
  .github/copilot-instructions.md

Priority order is root files first, in the order above, then
nested files by path; a root file of a name wins over a nested
file of the same name in block order. The scan skips .git,
node_modules, __pycache__ and Forge's own .forge/ folder, and is
capped at MAX_DETECTED files.

Approval (the gate): a detected file applies ONLY once the owner
approves it — in the Artifacts window's "Guideline files" list,
or by hand-editing the record file
<project>/.forge/guidelines-approved.txt (one relative path per
line, owner-editable like hints.md). Approval is a record, not a
copy: the project's own file stays where it is and is read fresh
each turn, so an approved file the owner later edits is applied
as edited, and an approved file later deleted simply stops
applying. Approving a path that is not a detected guideline file
is refused with a plain ValueError.

Context contract (pinned by tests): guidelines_block() returns ""
unless at least one APPROVED file has readable, non-empty text —
so a project with no approved guidelines prompts byte-identically
to before this module existed. The block rides in REVIEW and PLAN
turns only (build_system_prompt, same seam as CR1's learnings),
per-file cap MAX_FILE_CHARS and total cap DEFAULT_CAP_CHARS,
whole files only (a file that does not fit whole is left out and
counted in the tail line, never cut mid-rule). The block's framing
names the files, so a CR2 Source line may cite them, e.g.
Source: AGENTS.md.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .projects import forge_area

# Basenames that count as guideline files, in priority order.
# The copilot file is a PATH (.github/copilot-instructions.md),
# not a bare basename — it is matched separately below.
GUIDELINE_BASENAMES = ("AGENTS.md", "CLAUDE.md", ".cursorrules")
COPILOT_REL = ".github/copilot-instructions.md"
APPROVED_FILENAME = "guidelines-approved.txt"

MAX_DETECTED = 12
MAX_DEPTH = 2
MAX_FILE_CHARS = 4000
DEFAULT_CAP_CHARS = 8000

# Directories the nested scan never descends into.
_SKIP_DIRS = {".git", ".forge", "node_modules", "__pycache__",
              ".hg", ".svn", ".venv", "venv"}


def _approved_path(root: str | Path) -> Path:
    return forge_area(root) / APPROVED_FILENAME


def _rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _priority_key(rel: str) -> tuple:
    """Root files first, in GUIDELINE_BASENAMES order, then nested
    files by path. A root file of a name wins over a nested file of
    the same name."""
    parts = rel.split("/")
    if len(parts) == 1:
        name = parts[0]
        try:
            rank = GUIDELINE_BASENAMES.index(name)
        except ValueError:
            rank = len(GUIDELINE_BASENAMES)
        return (0, rank, rel)
    if rel == COPILOT_REL:
        return (0, len(GUIDELINE_BASENAMES), rel)
    return (1, 0, rel)


# ---------- detection (never raises) ----------

def detect_guidelines(root: str | Path) -> list[str]:
    """Relative POSIX paths of the project's guideline files, in
    priority order (see _priority_key). [] when there are none —
    the normal state for most projects. Never raises: an
    unreadable workspace reads as no guidelines."""
    try:
        base = Path(root)
    except Exception:
        return []
    found: list[str] = []

    def _add(path: Path) -> None:
        try:
            if path.is_file():
                found.append(_rel_posix(path, base))
        except Exception:
            pass

    try:
        for name in GUIDELINE_BASENAMES:
            _add(base / name)
        _add(base / COPILOT_REL)
        # Nested scan, depth <= MAX_DEPTH: the same basenames in
        # subfolders (docs/AGENTS.md, src/CLAUDE.md), plus a
        # copilot file under any .github/ folder at depth <= 2.
        stack = [(base, 0)]
        while stack:
            folder, depth = stack.pop()
            try:
                children = sorted(folder.iterdir())
            except OSError:
                continue
            for child in children:
                try:
                    if child.is_dir():
                        if child.name in _SKIP_DIRS:
                            continue
                        if child.name.startswith(".") \
                                and child.name != ".github":
                            continue
                        # A folder at depth MAX_DEPTH is listed
                        # (its files sit at depth MAX_DEPTH) but
                        # never descended into.
                        if depth + 1 <= MAX_DEPTH:
                            stack.append((child, depth + 1))
                    elif child.is_file():
                        if child.name in GUIDELINE_BASENAMES:
                            _add(child)
                        elif (child.name == "copilot-instructions.md"
                                and child.parent.name == ".github"):
                            _add(child)
                except OSError:
                    continue
    except Exception:
        return []
    # Dedupe (a root file and a nested walk can meet), then order.
    seen: set[str] = set()
    ordered: list[str] = []
    for rel in found:
        if rel not in seen:
            seen.add(rel)
            ordered.append(rel)
    ordered.sort(key=_priority_key)
    return ordered[:MAX_DETECTED]


# ---------- the approval record (atomic, owner-editable) ----------

def _read_approved(root: str | Path) -> list[str]:
    try:
        text = _approved_path(root).read_text(
            encoding="utf-8-sig", errors="replace")
    except Exception:
        return []
    out: list[str] = []
    for line in text.splitlines():
        rel = line.strip()
        if rel and rel not in out:
            out.append(rel)
    return out


def _atomic_write(path: Path, content: str) -> None:
    """Temp file in the same folder, fsync, os.replace — the tree's
    pattern (forge/tools.py _atomic_write_text, mirrored in
    learnings.py): a crash mid-write leaves the old record whole."""
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


def list_approved(root: str | Path) -> list[str]:
    """The approved relative paths, in approval order. [] when
    none are approved."""
    return _read_approved(root)


def pending_guidelines(root: str | Path) -> list[str]:
    """Detected files the owner has NOT approved yet — the gate's
    waiting list. Never raises."""
    approved = set(_read_approved(root))
    return [rel for rel in detect_guidelines(root)
            if rel not in approved]


def approve_guideline(root: str | Path, rel: str) -> Path:
    """Approve one detected guideline file and return the record
    file's path. Approving a path that is not a detected guideline
    file is refused with a plain ValueError — approval can only
    bless a file Forge actually found in the project. Approving
    an already-approved file files nothing and returns the path."""
    rel = str(rel or "").strip().replace(os.sep, "/")
    if rel not in detect_guidelines(root):
        raise ValueError(
            f"Not a guideline file in this project: {rel!r} — "
            "only a detected AGENTS.md, CLAUDE.md, .cursorrules "
            "or copilot-instructions.md can be approved.")
    approved = _read_approved(root)
    if rel in approved:
        return _approved_path(root)
    approved.append(rel)
    _atomic_write(_approved_path(root),
                  "".join(line + "\n" for line in approved))
    return _approved_path(root)


def revoke_guideline(root: str | Path, rel: str) -> bool:
    """Take one file's approval back. True when a line was
    removed, False when it was not approved."""
    rel = str(rel or "").strip().replace(os.sep, "/")
    approved = _read_approved(root)
    if rel not in approved:
        return False
    approved.remove(rel)
    _atomic_write(_approved_path(root),
                  "".join(line + "\n" for line in approved))
    return True


# ---------- reading (never raises) ----------

def _read_file(root: str | Path, rel: str) -> str:
    """One approved file's text, stripped. "" when it is missing,
    unreadable, empty, or looks binary — a broken guideline file
    must never break a review or plan turn."""
    try:
        raw = (Path(root) / rel).read_bytes()
    except Exception:
        return ""
    if b"\x00" in raw[:4096]:
        return ""
    try:
        text = raw.decode("utf-8-sig", errors="replace").strip()
    except Exception:
        return ""
    return text


# ---------- the prompt block ----------

FRAMING = ("Project guidelines — rules the OWNER approved from "
           "these files (apply them; they outrank generic habits "
           "but not the owner's current message):")


def guidelines_block(root: str | Path,
                     cap_chars: int = DEFAULT_CAP_CHARS) -> str:
    """The approved guidelines as one compact prompt block: the
    framing line, then each approved file with its name and its
    rules. Whole files only — a file longer than MAX_FILE_CHARS
    is shown truncated with a truthful marker; a file that does
    not fit the remaining total cap is left out whole and counted
    in the tail line. "" when no approved file has usable text —
    callers add the block only when it is non-empty, so a project
    with no approved guidelines prompts byte-identically to
    before."""
    approved = list_approved(root)
    if not approved:
        return ""
    lines = [FRAMING]
    used = 0
    shown = 0
    dropped: list[str] = []
    for rel in approved:
        text = _read_file(root, rel)
        if not text:
            continue
        if len(text) > MAX_FILE_CHARS:
            text = (text[:MAX_FILE_CHARS]
                    + f"\n… [truncated at {MAX_FILE_CHARS} chars]")
        chunk = f"--- {rel} ---\n{text}"
        if used + len(chunk) > cap_chars:
            dropped.append(rel)
            continue
        lines.append(chunk)
        used += len(chunk)
        shown += 1
    if shown == 0:
        return ""
    if dropped:
        lines.append(
            f"+{len(dropped)} approved guideline file(s) not shown "
            f"(cap: {', '.join(dropped)})")
    return "\n".join(lines)
