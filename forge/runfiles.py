"""Item #50: what in a project folder can be RUN — the scan behind
the per-project run surface.

The owner's design, in his words: working within a project folder
should automatically LOAD what's built, so he can run it, switch
between the files that carry the executable or whatnot, and test
different windows — load, not auto-run. This module is the loading:
a pure filesystem scan with no Qt and no opinions beyond the rules
below, so it is unit-testable without a window.

Rules: the project root's direct files, plus files directly inside
the conventional output dirs (dist/, build/, out/, target/release/)
when present — no deeper recursion. Dotfiles, the .forge area and
other dot-dirs, __pycache__ and venvs never contribute (they are
directories, and directories never qualify anyway). A file qualifies
when it carries a known runnable suffix (.py, .sh, .html/.htm, .jar,
or a name ending in .AppImage) or any executable bit. Known entry
points at the root (run.sh, start.sh, main.py, app.py, index.html)
pin to the front; everything else sorts by relative path. The list
is capped at 100 entries.
"""
from __future__ import annotations

import shlex
from pathlib import Path

MAX_RUNNABLES = 100

# Conventional build-output dirs whose DIRECT files also load.
OUTPUT_DIRS = ("dist", "build", "out", "target/release")

# Root entry points, in pin order — the file a project most likely
# means by "run this" floats to the top of the surface.
ENTRY_POINTS = ("run.sh", "start.sh", "main.py", "app.py", "index.html")

_SUFFIX_KINDS = {
    ".py": "Python",
    ".sh": "Shell",
    ".html": "Web page",
    ".htm": "Web page",
    ".jar": "Java",
}


def _command_for(rel: str, kind: str) -> str:
    """The shell string the RunThread launches for *rel*, run from
    the project root. The path is shlex-quoted; the "./" prefix for
    direct execution is outside the quote so a spaced name still
    resolves (./'my app')."""
    quoted = shlex.quote(rel)
    if kind == "Python":
        return f"python3 {quoted}"
    if kind == "Shell":
        return f"bash {quoted}"
    if kind == "Web page":
        return f"xdg-open {quoted}"
    if kind == "Java":
        return f"java -jar {quoted}"
    # "AppImage" and "Program" run directly.
    return f"./{quoted}"


def _classify(path: Path, rel: str) -> dict | None:
    """One file -> its runnable entry, or None when it does not
    qualify. Suffix kinds win over the executable bit, so an
    executable .py still runs through python3 and appears once."""
    # Sweep R126: EVERY branch stats the entry — the suffix branches
    # used to qualify a path sight-unseen, so a dangling symlink with
    # a runnable suffix was listed but could never run. is_file()
    # follows links: a broken target does not qualify.
    try:
        if not path.is_file():
            return None
    except OSError:
        return None
    name = path.name
    lower = name.lower()
    if lower.endswith(".appimage"):
        kind = "AppImage"
    elif path.suffix.lower() in _SUFFIX_KINDS:
        kind = _SUFFIX_KINDS[path.suffix.lower()]
    else:
        try:
            executable = bool(path.stat().st_mode & 0o111)
        except OSError:
            return None
        if not executable:
            return None
        kind = "Program"
    return {"rel": rel, "name": name, "kind": kind,
            "command": _command_for(rel, kind)}


def _direct_files(folder: Path):
    """The non-hidden files directly inside *folder* (never dirs),
    in name order. A missing/unreadable folder yields nothing."""
    try:
        children = sorted(folder.iterdir(), key=lambda p: p.name)
    except OSError:
        return
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            if child.is_dir():
                continue
        except OSError:
            continue
        yield child


def scan_runnables(root) -> list[dict]:
    """The runnable files of the project at *root*: a list of
    {"rel", "name", "kind", "command"} dicts, entry points first,
    capped at MAX_RUNNABLES. A missing root scans as empty."""
    root = Path(root)
    found: list[dict] = []
    for child in _direct_files(root):
        entry = _classify(child, child.name)
        if entry is not None:
            found.append(entry)
    for out_rel in OUTPUT_DIRS:
        out_dir = root / out_rel
        if not out_dir.is_dir():
            continue
        for child in _direct_files(out_dir):
            entry = _classify(child, f"{out_rel}/{child.name}")
            if entry is not None:
                found.append(entry)

    def sort_key(entry):
        # Sweep R130: pinning and ordering are case-insensitive —
        # Main.py IS the entry point main.py is, and case variants
        # sort beside their lowercase twins instead of away from them.
        rel = entry["rel"]
        low = rel.lower()
        if "/" not in rel and low in ENTRY_POINTS:
            return (0, ENTRY_POINTS.index(low), low)
        return (1, 0, low)

    found.sort(key=sort_key)
    return found[:MAX_RUNNABLES]
