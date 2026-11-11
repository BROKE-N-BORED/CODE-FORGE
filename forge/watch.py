"""AI-comment watch mode — Note 17 (Aider's watch-files idea).

The owner works in his own editor. When he saves a file containing a
comment ending in `AI!` (do this) or `AI?` (answer this), the Forge
picks it up and runs it as a turn. Pure polling scan — no dependencies.

  # validate this input AI!
  // how does the cache work AI?

Markers are consumed by content hash, so a saved marker fires once;
editing the line re-arms it.

Audit B17 (incremental watch): the scan polls every few seconds, and
re-reading up to MAX_FILES file bodies per tick churned the owner's
HDD even when nothing had changed. Each file's parsed markers are now
cached against its (mtime_ns, size) stamp: a candidate whose stamp
still matches is never re-read, only new or changed files are parsed,
and entries for files that leave the candidate set are pruned. The
`seen` filter is applied when assembling results, exactly as before,
so scan output is unchanged — it just costs stats instead of reads.
"""
from __future__ import annotations
import hashlib
from pathlib import Path

EXTS = {".py", ".js", ".ts", ".md", ".txt", ".html", ".css", ".sh"}
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".forge"}
MAX_FILES = 400

# path (str) -> (mtime_ns, size, markers). Markers are the file's
# COMPLETE parsed list, before the caller's `seen` filter — the
# filter belongs to the scan, not to the file.
_file_cache: dict[str, tuple[int, int, list[dict]]] = {}


def _clean(line: str) -> str:
    text = line.strip()
    for prefix in ("#", "//", "<!--", "*", "--"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text.rstrip("-").rstrip(">").strip()


def _parse_markers(path: Path, root: Path) -> list[dict] | None:
    """Every marker in one file, unfiltered. None when the file
    cannot be read (the caller then skips it, as the scan always
    has, and nothing is cached for it)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    markers = []
    for i, line in enumerate(lines, 1):
        kind = "task" if "AI!" in line else ("question" if "AI?" in line else None)
        if not kind:
            continue
        text = _clean(line).replace("AI!", "").replace("AI?", "").strip()
        if not text:
            continue
        key = hashlib.sha256(f"{path}:{i}:{text}".encode()).hexdigest()[:16]
        markers.append({"file": str(path.relative_to(root)), "line": i,
                        "kind": kind, "text": text, "key": key})
    return markers


def scan(workspace: str, seen: set[str]) -> list[dict]:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        return []
    files = []
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix in EXTS:
            files.append(path)
        if len(files) >= MAX_FILES:
            break
    found: list[dict] = []
    candidates: set[str] = set()
    for path in files:
        cache_key = str(path)
        candidates.add(cache_key)
        markers = None
        try:
            stamp = path.stat()
        except OSError:
            stamp = None
        if stamp is not None:
            entry = _file_cache.get(cache_key)
            if (entry is not None and entry[0] == stamp.st_mtime_ns
                    and entry[1] == stamp.st_size):
                markers = entry[2]
        if markers is None:
            parsed = _parse_markers(path, root)
            if parsed is None:
                _file_cache.pop(cache_key, None)
                continue
            markers = parsed
            if stamp is not None:
                _file_cache[cache_key] = (stamp.st_mtime_ns,
                                          stamp.st_size, markers)
        for marker in markers:
            if marker["key"] not in seen:
                found.append(dict(marker))
    # Prune files that left the candidate set — deleted, or pushed
    # past MAX_FILES — so their markers can never resurface.
    for stale in [key for key in _file_cache if key not in candidates]:
        del _file_cache[stale]
    return found
