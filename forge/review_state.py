"""Review state (CR6 — Review Tier 1 #6, 2026-11-11).

Forge reviews only what changed since the last review and
keeps a running summary — file-based and owner-gated,
because only the owner can say a review is done:

  <project>/.forge/reviews/state.json   — what was reviewed, when
  <project>/.forge/reviews/summary.md   — the running summary

The state is a fingerprint: the sha1 of every project file (up to
MAX_FILES files, skipping Forge's own .forge/, .git, caches and
files over MAX_FILE_BYTES — a fingerprint is a review baseline,
not a backup). NOTHING is marked reviewed on its own: the owner
clicks "Mark reviewed" in the Review room after a review, and
record_review() snapshots the project as it stands. "Review
what's new" then lists only files whose fingerprint changed (or
that are new) plus files that vanished since — the incremental
review's scope, computed by the program, never by the model.

The summary is the summary-of-summaries: one block per review,
newest first — date, the review file's name, the Walkthrough's
What-changed line, and the severity counts — so the whole review
history reads at a glance. The Review guidance tells the model to
maintain it when it files a report (the model is already the one
that files review reports); format_summary_entry() is the exact
block shape it writes, so tests can pin it and the room can show
it. summary.md lives in .forge/reviews/, so it appears in the
Review room's Reviews tab like any other review document.

Context contract (pinned by tests): every function here never
raises — a missing, corrupt or unreadable state reads as "never
reviewed", and a broken fingerprint run reads as "nothing
changed". A project with no state behaves exactly as before CR6:
full reviews, no incremental scope, no summary pressure.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STATE_FILENAME = "state.json"
SUMMARY_FILENAME = "summary.md"

MAX_CHANGED_LISTED = 50
MAX_FILES = 2000
MAX_FILE_BYTES = 2 * 1024 * 1024

# Directories the fingerprint walk never descends into.
_SKIP_DIRS = {".git", ".forge", "node_modules", "__pycache__",
              ".hg", ".svn", ".venv", "venv", ".mypy_cache",
              ".pytest_cache", "dist", "build", ".tox"}


def _reviews_dir(root: str | Path) -> Path:
    return Path(root) / ".forge" / "reviews"


def state_path(root: str | Path) -> Path:
    return _reviews_dir(root) / STATE_FILENAME


def summary_path(root: str | Path) -> Path:
    return _reviews_dir(root) / SUMMARY_FILENAME


# ---------- the fingerprint ----------

def _fingerprint(root: str | Path) -> dict:
    """{relative POSIX path: sha1 hex} for the project's files.
    Never raises; an unreadable workspace fingerprints as {}."""
    try:
        base = Path(root)
    except Exception:
        return {}
    out: dict = {}
    try:
        stack = [base]
        while stack and len(out) < MAX_FILES:
            folder = stack.pop()
            try:
                children = sorted(folder.iterdir())
            except OSError:
                continue
            for child in children:
                try:
                    if child.is_dir():
                        if child.name in _SKIP_DIRS:
                            continue
                        if child.name.startswith("."):
                            continue
                        stack.append(child)
                    elif child.is_file():
                        try:
                            if child.stat().st_size > MAX_FILE_BYTES:
                                continue
                            digest = hashlib.sha1()
                            with child.open("rb") as fh:
                                while True:
                                    piece = fh.read(65536)
                                    if not piece:
                                        break
                                    digest.update(piece)
                            out[child.relative_to(base).as_posix()] = \
                                digest.hexdigest()
                        except OSError:
                            continue
                        if len(out) >= MAX_FILES:
                            break
                except OSError:
                    continue
    except Exception:
        return {}
    return out


# ---------- the state file (atomic, never raises on read) ----------

def load_state(root: str | Path) -> dict:
    """The recorded review state: {"files": {...}, "last_review":
    iso timestamp or None, "review_count": int}. Missing, corrupt
    or unreadable state reads as never reviewed — never raises."""
    try:
        raw = state_path(root).read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
        files = data.get("files")
        if not isinstance(files, dict):
            files = {}
        last = data.get("last_review")
        count = data.get("review_count")
        return {"files": {str(k): str(v) for k, v in files.items()},
                "last_review": last if isinstance(last, str) else None,
                "review_count": count if isinstance(count, int) else 0}
    except Exception:
        return {"files": {}, "last_review": None, "review_count": 0}


def _atomic_write(path: Path, content: str) -> None:
    """Temp file in the same folder, fsync, os.replace — the tree's
    pattern (forge/tools.py _atomic_write_text, mirrored in
    learnings.py and guidelines.py)."""
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


def record_review(root: str | Path) -> dict:
    """Mark the project reviewed as it stands: fingerprint it now,
    stamp the time, bump the review count, and write the state
    atomically. Returns the state dict. Never raises — a failed
    write returns the state it tried to file."""
    state = load_state(root)
    state["files"] = _fingerprint(root)
    state["last_review"] = datetime.now(timezone.utc).isoformat()
    state["review_count"] = int(state.get("review_count") or 0) + 1
    try:
        _atomic_write(state_path(root),
                      json.dumps(state, indent=2, sort_keys=True) + "\n")
    except Exception:
        pass
    return state


def changed_since(root: str | Path) -> dict:
    """What changed since the last recorded review:
      {"changed": [listed relative paths, sorted],
       "changed_total": int (listed + unlisted),
       "removed": [recorded paths now gone, sorted],
       "last_review": iso timestamp or None,
       "ever": bool — False when no review was ever recorded}
    Never raises. With no recorded review, "ever" is False and
    the changed/removed lists are empty — the caller says so
    plainly instead of pretending the whole project is new."""
    state = load_state(root)
    if not state["files"] and state["last_review"] is None:
        return {"changed": [], "changed_total": 0, "removed": [],
                "last_review": None, "ever": False}
    now = _fingerprint(root)
    old = state["files"]
    changed = sorted(
        rel for rel, digest in now.items()
        if old.get(rel) != digest)
    removed = sorted(rel for rel in old if rel not in now)
    return {"changed": changed[:MAX_CHANGED_LISTED],
            "changed_total": len(changed),
            "removed": removed,
            "last_review": state["last_review"],
            "ever": True}


# ---------- the running summary ----------

def format_summary_entry(when: str, review_file: str,
                         what_changed: str, counts: str) -> str:
    """One summary block, newest-first shape: date, review file,
    the Walkthrough's What-changed line, and the severity counts.
    The Review guidance tells the model to prepend a block in
    exactly this shape to summary.md when it files a report."""
    return (f"## {when} — {review_file}\n"
            f"What changed: {what_changed}\n"
            f"Findings: {counts}\n")


def read_summary(root: str | Path) -> str:
    """The running summary's text, or "" when there is none.
    Never raises."""
    try:
        return summary_path(root).read_text(encoding="utf-8")
    except Exception:
        return ""
