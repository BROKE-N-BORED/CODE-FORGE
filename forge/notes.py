"""Session notes — H5 (GUIFX Research Brief #2, 2026-11-09).

THE PROBLEM THIS ANSWERS. Every agent tool folds (summarizes) old
context when the window fills, and the fold silently loses decisions
and done-steps — nothing on screen admits it happened, and the lost
detail is gone from what the model sees. Forge folds too
(forge/context.py), so Forge keeps a second, durable memory the fold
cannot eat: one plain-markdown notes file per conversation, on disk,
owned by the user.

    APP_DIR/notes/<conversation_id>.md

WHAT GOES IN. Only deterministic, app-generated entries for events
the app itself already observes — a plan approved, a plan step
finished, a context fold landing — each one timestamped line. No
model inference is involved in writing them, ever. The user can also
write their own lines in the same file (the Session Notes window is
an editor over exactly this file).

WHY IT WORKS. When a fold lands, the window re-includes the file's
current content in the folded history (forge/context.py's
with_session_notes), so what the notes remember rides past the fold
and the model still sees it. The file — not the in-context copy —
is the source of truth: edit it, and the next fold carries the
edited version.

Pure stdlib, no Qt: the store is three small functions over one
file per conversation. Appends open the file in append mode, so an
entry lands as one write at the end of the file and never rewrites
what is already there.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

NOTES_DIRNAME = "notes"


def _stamp() -> str:
    """The codebase's timestamp shape (planning._now's convention):
    local time, ISO format, second precision."""
    return datetime.now().isoformat(timespec="seconds")


def notes_path(app_dir, conversation_id):
    """The notes file for one conversation, or None when there is
    no conversation to key it to (an id that was never persisted)."""
    if conversation_id is None:
        return None
    try:
        cid = int(conversation_id)
    except (TypeError, ValueError):
        return None
    return Path(app_dir) / NOTES_DIRNAME / f"{cid}.md"


def append_entry(app_dir, conversation_id, text) -> bool:
    """Append one timestamped entry line. Creates the notes folder
    and the file on first use. Returns False (a silent no-op) when
    there is no conversation id — callers on event paths must never
    fail because a note could not be keyed. Entry text is flattened
    to a single line: the file stays one-entry-per-line, which is
    what makes the window's append-merge save rule reliable."""
    path = notes_path(app_dir, conversation_id)
    if path is None:
        return False
    line = " ".join(str(text or "").split())
    if not line:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"- [{_stamp()}] {line}\n")
    except OSError:
        return False
    return True


def read_notes(app_dir, conversation_id) -> str:
    """The file's full content, or "" when there is no file (or no
    conversation id). The returned text is exactly what the fold
    re-inclusion and the windows show — no reformatting."""
    path = notes_path(app_dir, conversation_id)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_notes(app_dir, conversation_id, text) -> bool:
    """Replace the file's content (the Notes window's save).
    Returns False when there is no conversation id to key it to."""
    path = notes_path(app_dir, conversation_id)
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(text or ""), encoding="utf-8")
    except OSError:
        return False
    return True


def delete_notes(app_dir, conversation_id) -> None:
    """Remove a conversation's notes file (conversation deletion).
    A missing file is not an error."""
    path = notes_path(app_dir, conversation_id)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
