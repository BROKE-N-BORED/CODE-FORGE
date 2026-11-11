"""Artifacts (owner directive, 2026-11-08, from the Antigravity
comparison): the reviewable deliverables of agent work — plans,
designs, architecture passes, reviews, and a walkthrough of each
finished Code turn — kept as first-class records in the project,
not buried in chat scrollback or the event log.

Storage is deliberately plain: one text file per artifact under the
project's .forge/artifacts/, plus an index.json naming them. No
database, nothing to migrate; delete the folder and they're gone.
Recording never raises INTO A TURN: record() itself may raise
(OSError from the filesystem), and both of its callers wrap it, so a
failed capture is logged by the caller and the work itself always
stands.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

KINDS = ("plan", "design", "architect", "review", "walkthrough", "note")
MAX_TEXT = 200_000


def _folder(workspace) -> Path:
    return Path(workspace) / ".forge" / "artifacts"


def _index_path(workspace) -> Path:
    return _folder(workspace) / "index.json"


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug[:48] or "artifact"


def _read_index(workspace) -> list:
    try:
        data = json.loads(_index_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and e.get("file")]


# Sweep R116: the index read-modify-write is serialised in-process
# and written atomically (pid-unique temp + replace), so a crash or
# two overlapping record() calls can no longer truncate the index
# into silently orphaning every prior artifact.
_INDEX_LOCK = threading.Lock()


def _write_index(workspace, entries) -> None:
    path = _index_path(workspace)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    tmp.replace(path)


def _entry_path(folder: Path, entry) -> Path | None:
    """The on-disk path for an index entry, CONFINED to the artifacts
    folder — or None. Sweep R115: the index is just a file inside the
    project (a cloned repo can ship a poisoned one), so an entry whose
    `file` names a path outside the folder (../.. or absolute) must
    never be listed or read."""
    try:
        target = (folder / str(entry.get("file", ""))).resolve()
    except (OSError, RuntimeError):
        return None
    base = folder.resolve()
    if target != base and base not in target.parents:
        return None
    return target


def record(workspace, kind, title, text, conversation_id=None,
           comment=None, revises=None) -> dict:
    """File one artifact and index it. Returns the index entry.
    Kind is validated against KINDS (unknown kinds land as "note").
    Text is capped at MAX_TEXT with an honest truncation line.

    Item #66: a revision filed from an owner comment carries the
    comment and the original's file name (`revises`) on its index
    entry — the lineage lives in the index, the texts in their own
    files, and neither file is ever rewritten by the filing."""
    if kind not in KINDS:
        kind = "note"
    folder = _folder(workspace)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    title = (title or kind.capitalize()).strip() or kind.capitalize()
    body = text or ""
    if len(body) > MAX_TEXT:
        body = body[:MAX_TEXT] + "\n\n[artifact truncated — the full text lives in the conversation]"
    # Sweep R118: claim the name with an exclusive create instead of
    # exists()-then-write (two same-second records could land on one
    # file), and the first duplicate is -1, not -2.
    counter = 0
    while True:
        suffix = "" if counter == 0 else f"-{counter}"
        path = folder / f"{stamp}-{kind}-{_slug(title)}{suffix}.md"
        try:
            with open(path, "x", encoding="utf-8") as fh:
                fh.write(body)
            break
        except FileExistsError:
            counter += 1
    entry = {"kind": kind, "title": title, "file": path.name,
             "created_at": datetime.now().isoformat(timespec="seconds"),
             "conversation_id": conversation_id}
    if comment:
        entry["comment"] = str(comment)
    if revises:
        entry["revises"] = str(revises)
    with _INDEX_LOCK:
        entries = _read_index(workspace)
        entries.append(entry)
        _write_index(workspace, entries)
    return entry


def list_artifacts(workspace) -> list:
    """Index entries, newest first. Entries whose file has been
    deleted out from under the index drop out of the list — and so do
    entries whose `file` points outside the artifacts folder
    (R115: a crafted index must not pull arbitrary files into the
    Artifacts window)."""
    folder = _folder(workspace)
    entries = []
    for e in _read_index(workspace):
        path = _entry_path(folder, e)
        if path is not None and path.is_file():
            entries.append(e)
    entries.reverse()
    return entries


def read_text(workspace, entry) -> str:
    path = _entry_path(_folder(workspace), entry)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
