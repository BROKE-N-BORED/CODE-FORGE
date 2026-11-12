"""Review Learnings (CR1 — the CodeRabbit Learnings loop, 2026-11-11;
perfection pass 2026-11-11).

CodeRabbit's main anti-noise mechanism, at Forge scale: when the
owner dismisses a review finding in plain language ("not a finding —
handled higher up the stack"), that correction PERSISTS and is
applied to future reviews, so the reviewer stops re-raising what the
owner has already settled. Forge had no equivalent: a dismissed
finding could come back verbatim on the next review.

Storage, deliberately plain like the knowledge store:

  <project>/.forge/learnings.md   — PROJECT learnings
  <app data>/learnings.md         — GLOBAL learnings (the app-data
                                    folder forge/main.py's APP_DIR
                                    names; settings.json lives there)

Both files share one owner-readable shape:

  # Review learnings

  - <the owner's words> · Filed: <YYYY-MM-DD HH:MM> · From: <review>

The "· From:" part is present only when the source review is known.
Lines are APPEND-ONLY here: add_learning never rewrites or deletes
an existing line — the owner edits the files by hand when a learning
stops being true, exactly like hints.md. Appends are ATOMIC (temp
file in the same folder, fsync, replace — the tree's write_file
pattern from forge/tools.py), so a crash mid-add can never truncate
learnings already filed, and the owner's own text survives
byte-for-byte apart from the one line added.

Hand-edits are expected, and reading is tolerant of them (pinned by
tests): bullets may use -, *, + or • markers (or a numbered list),
blank lines and prose are skipped, the heading may be edited or
gone entirely (an append still lands), a reworded bullet reads as
its new words, and a stamp is only treated as a stamp when it is
timestamp-shaped and sits at the end of the line — a learning whose
own text contains "· Filed:" keeps its words whole.

One learning is one line of the owner's words, flattened, at most
MAX_TEXT_CHARS characters: a longer text is refused with a plain
ValueError rather than silently truncated, so what is stored is
always exactly what the owner said.

Context contract (pinned by tests): learnings ride in REVIEW turns
(build_system_prompt adds learnings_block() when the mode is
"review" and the block is non-empty, and in no other bench mode)
and in the Council reviewer's pass (forge/council.py adds the same
block when a workspace is threaded through). With no learnings the
block is "" and every prompt is byte-identical to what it was
before this module existed (the M1 stable-prefix rule). No model
writes learnings: the text is the owner's, captured verbatim from
the Review room's teach action.
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from .projects import forge_area

PROJECT_FILENAME = "learnings.md"
HEADING = "# Review learnings"
MAX_LEARNINGS = 100
MAX_TEXT_CHARS = 500
DEFAULT_CAP_CHARS = 1500

FRAMING = ("Owner's review learnings — apply these; do not re-raise "
           "findings the owner has already dismissed for these "
           "reasons:")

# A bullet in the owner's file: a markdown marker (-, *, +, •) or a
# numbered item, then the learning's words.
_BULLET_RE = re.compile(r"^(?:[-*+•]\s+|\d+[.)]\s+)(.*\S)\s*$")
# The stamp add_learning appends — recognised ONLY in this exact
# shape, at the end of the line, so the same words inside a
# learning's own text are never mistaken for it.
_STAMP_RE = re.compile(
    r" · Filed: \d{4}-\d{2}-\d{2} \d{2}:\d{2}(?: · From: .*)?$")


# ---------- layout ----------

def project_path(root: str | Path) -> Path:
    return forge_area(root) / PROJECT_FILENAME


def default_global_path() -> Path:
    """The global learnings file, in the app-data folder the app
    already uses for its global settings (forge/main.py APP_DIR,
    where settings.json / seats.json / recipes live). learnings.py
    cannot import main (main imports this module), so the path is
    rebuilt from the same pattern — and a test pins it equal to
    main's real APP_DIR constant."""
    return (Path.home() / ".local" / "share" / "linux-forge-agent-shell"
            / PROJECT_FILENAME)


def _path_for(root: str | Path, scope: str,
              global_path: str | Path | None) -> Path:
    if scope == "global":
        return (Path(global_path) if global_path is not None
                else default_global_path())
    if scope == "project":
        return project_path(root)
    raise ValueError(
        f"Unknown learnings scope: {scope!r} (use 'project' or 'global').")


# ---------- text shape ----------

def _flatten(text) -> str:
    """One line, single-spaced — the stored shape of a learning."""
    return " ".join(str(text or "").split())


def _key(text: str) -> str:
    """The dedupe key: flattened and case-folded, so whitespace and
    case variants of the same learning are the same learning."""
    return _flatten(text).lower()


# ---------- reading (never raises) ----------

def _parse_file(path: Path) -> list[str]:
    """The learning texts in one file, oldest first. A bullet's text
    is everything before its stamp (see _STAMP_RE — shape-checked,
    so a learning mentioning "· Filed:" in its own words is kept
    whole); a bullet the owner wrote by hand, with no stamp, counts
    whole. Missing, unreadable or malformed files read as empty —
    a broken learnings file must never break a review turn."""
    try:
        with open(path, "r", encoding="utf-8-sig",
                  errors="replace") as fh:
            lines = fh.read().splitlines()
    except Exception:
        return []
    out = []
    for raw in lines:
        s = raw.strip()
        m = _BULLET_RE.match(s)
        if not m:
            continue
        inner = m.group(1)
        stamp = _STAMP_RE.search(inner)
        if stamp:
            inner = inner[:stamp.start()]
        text = _flatten(inner)
        if text:
            out.append(text)
    return out


def list_learnings(root: str | Path, scope: str = "project",
                   global_path: str | Path | None = None) -> list[str]:
    """One scope's learning texts, oldest first. [] when there are
    none — the normal state for most projects. An unknown scope is
    a plain ValueError."""
    return _parse_file(_path_for(root, scope, global_path))


# ---------- writing (atomic) ----------

def _atomic_write(path: Path, content: str) -> None:
    """Write `content` over `path` atomically — the tree's pattern
    (forge/tools.py _atomic_write_text): temp file in the SAME
    folder, fsync, os.replace, so a crash mid-write leaves the old
    file standing whole. Permission bits ride across the replace."""
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


def add_learning(root: str | Path, text: str, scope: str = "project",
                 source: str = "",
                 global_path: str | Path | None = None) -> Path:
    """File one learning and return its file's path.

    The text is the owner's, stored verbatim (flattened to one
    line, at most MAX_TEXT_CHARS — longer is refused plainly,
    never truncated). A duplicate — compared flattened and
    case-insensitively, so whitespace/case variants count as the
    same learning — files nothing and simply returns the path.
    Beyond MAX_LEARNINGS in a scope the add is refused with a
    plain ValueError: the cap keeps the file (and the prompt block
    built from it) honest, and the oldest learnings are KEPT —
    making room is the owner's edit, never a silent deletion here.
    The append lands even in a hand-edited file (heading gone, no
    trailing newline, CRLF endings): existing content is preserved
    and the new line is added after it, atomically."""
    text = _flatten(text)
    if not text:
        raise ValueError("A learning needs some text — say why it is "
                         "not a finding.")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(
            f"A learning is at most {MAX_TEXT_CHARS} characters — "
            f"shorten it (yours runs {len(text)}).")
    path = _path_for(root, scope, global_path)
    existing = _parse_file(path)
    if any(_key(item) == _key(text) for item in existing):
        return path
    if len(existing) >= MAX_LEARNINGS:
        raise ValueError(
            f"Learnings are full ({MAX_LEARNINGS} in {path}) — remove "
            "one from that file before adding another.")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"- {text} · Filed: {stamp}"
    source = _flatten(source)
    if source:
        line += f" · From: {source}"
    raw = ""
    if path.exists():
        with open(path, "r", encoding="utf-8", errors="replace",
                  newline="") as fh:
            raw = fh.read()
    if not raw.strip():
        content = f"{HEADING}\n\n{line}\n"
    else:
        content = raw if raw.endswith(("\n", "\r")) else raw + "\n"
        content += line + "\n"
    _atomic_write(path, content)
    return path


# ---------- the prompt block ----------

def learnings_block(root: str | Path,
                    global_path: str | Path | None = None,
                    cap_chars: int = DEFAULT_CAP_CHARS) -> str:
    """The learnings as one compact prompt block: the framing line,
    then GLOBAL learnings first (they hold everywhere), then the
    project's, one bullet each. The cap is a budget on BULLET text:
    whole bullets are shown while they fit (a bullet is never cut),
    and what does not fit is counted in a whole tail line that
    names the file(s) the dropped learnings actually live in —
    the project's, the global file, or both — rather than being
    silently dropped. So the bullet text never exceeds cap_chars,
    and the block exceeds it only by the framing and tail lines.
    "" when there are no learnings at all — callers add the block
    only when it is non-empty, so an untaught project prompts
    byte-identically to before."""
    entries = ([(text, "global")
                for text in list_learnings(root, "global", global_path)]
               + [(text, "project")
                  for text in list_learnings(root, "project",
                                             global_path)])
    if not entries:
        return ""
    lines = [FRAMING]
    used = 0
    shown = 0
    for text, _scope in entries:
        bullet = f"- {text}"
        if used + len(bullet) > cap_chars:
            break
        lines.append(bullet)
        used += len(bullet)
        shown += 1
    dropped = entries[shown:]
    if dropped:
        scopes = {scope for _text, scope in dropped}
        if scopes == {"global"}:
            where = "the global learnings file"
        elif scopes == {"project"}:
            where = ".forge/learnings.md"
        else:
            where = "the global learnings file and .forge/learnings.md"
        lines.append(f"+{len(dropped)} more in {where}")
    return "\n".join(lines)
