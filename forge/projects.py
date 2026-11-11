"""Projects (Forge v2, Notes 1 + 8).

A project is a folder the user works in. Inside it, a `.forge/` area the
shell maintains:
  instructions.md  — how the agent should behave in THIS project
  hints.md         — the owner's micro-knowledge for this project:
                     conventions, commands, gotchas (rides in the
                     system prompt, capped — see hints_text)
  sources/         — the project's library: specs, references, knowledge
  uploads/         — files the user handed the agent (with receipts)
  reference/       — the Reference-Driven Build area (forge/
                     reference_build.py): profile.txt is the opt-in
                     record; lock.md / breakdown.md / fix-log.md
                     live beside it when the workflow runs
  scratchpad.md    — the agent's persistent notes for this project

Only the sources INDEX (name + first line of each file) rides in the
model's context; full files load on demand via read_source — the same
slice-loading as the skill directory.
"""
from __future__ import annotations
import shutil
from pathlib import Path

FORGE_DIR = ".forge"
STARTER_INSTRUCTIONS = """# Project instructions
# How should the agent behave in this project? Rules, style, do's and don'ts.
# (Written by the project owner — the agent reads this, it does not edit it.)
"""
STARTER_SCRATCHPAD = """# Agent scratchpad
Persistent notes the agent keeps for this project: what was built, decisions
made, what is still open. The agent may append here; the owner can edit freely.
"""


def forge_area(root: str | Path) -> Path:
    return Path(root) / FORGE_DIR


def ensure_project(root: str | Path) -> Path:
    """Create the .forge area if missing. Never overwrites existing files."""
    area = forge_area(root)
    (area / "sources").mkdir(parents=True, exist_ok=True)
    (area / "uploads").mkdir(parents=True, exist_ok=True)
    (area / "reference").mkdir(parents=True, exist_ok=True)
    instructions = area / "instructions.md"
    if not instructions.exists():
        instructions.write_text(STARTER_INSTRUCTIONS, encoding="utf-8")
    scratchpad = area / "scratchpad.md"
    if not scratchpad.exists():
        scratchpad.write_text(STARTER_SCRATCHPAD, encoding="utf-8")
    return area


def project_instructions(root: str | Path) -> str:
    try:
        text = (forge_area(root) / "instructions.md").read_text(encoding="utf-8")
    except Exception:
        return ""
    body = "\n".join(l for l in text.splitlines() if not l.startswith("#")).strip()
    return body


def _inside_sources(root: Path, name: str) -> Path:
    base = (forge_area(root) / "sources").resolve()
    target = (base / name).resolve()
    if target != base and base not in target.parents:
        raise ValueError("Source path is outside the project sources.")
    return target


def sources_index(root: str | Path) -> list[dict]:
    base = forge_area(root) / "sources"
    out: list[dict] = []
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*")):
        if p.is_file():
            desc = ""
            try:
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.strip():
                        desc = line.strip().lstrip("# ").strip()[:110]
                        break
            except Exception:
                desc = ""
            out.append({"name": str(p.relative_to(base)),
                        "description": desc or "(no summary line)",
                        "bytes": p.stat().st_size})
    return out


# Audit B16: the rendered index text is cached per project behind a
# stat-only fingerprint of exactly the source files sources_index
# reads — build_system_prompt asks for it on EVERY send, and re-reading
# every source's first line each time was pure wiring waste. The
# fingerprint reads no file content (relpath, mtime_ns, size); any
# added / removed / changed source misses the cache and the text is
# rebuilt, byte-identical to the uncached result.
_sources_text_cache: dict[str, tuple[tuple, str]] = {}


def _sources_fingerprint(base: Path) -> tuple:
    if not base.is_dir():
        return ()
    fp = []
    for p in sorted(base.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(base))
            try:
                st = p.stat()
                fp.append((rel, st.st_mtime_ns, st.st_size))
            except OSError:
                fp.append((rel, -1, -1))
    return tuple(fp)


def sources_index_text(root: str | Path) -> str:
    base = forge_area(root) / "sources"
    fingerprint = _sources_fingerprint(base)
    cache_key = str(Path(root))
    cached = _sources_text_cache.get(cache_key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    index = sources_index(root)
    if not index:
        text = ""
    else:
        lines = ["Project source files — the project's library. Call read_source with a "
                 "file's name to load it (or the slice you need) before relying on it:"]
        for entry in index:
            lines.append(f"- {entry['name']}: {entry['description']}")
        text = "\n".join(lines)
    # Sweep R153: the module caches are bounded — a long session
    # visiting many projects must not grow them for the life of the
    # process. A full cache simply clears (every entry is a pure
    # performance copy, rebuilt on demand).
    if len(_sources_text_cache) >= 64:
        _sources_text_cache.clear()
    _sources_text_cache[cache_key] = (fingerprint, text)
    return text


# Audit D44: the micro-knowledge file (.forge/hints.md) — the owner's
# small standing instructions for one project (conventions, commands,
# gotchas). It rides in the system prompt, so build_system_prompt asks
# for it on EVERY send, and it gets the same stat-fingerprint cache
# treatment as the sources index (B16): the fingerprint stats only the
# hints file itself (mtime_ns, size); a hit reads no file content, and
# any edit misses the cache and the text is rebuilt, byte-identical to
# the uncached result.
MAX_HINTS_CHARS = 4000
_hints_text_cache: dict[str, tuple[tuple, str]] = {}


def _hints_fingerprint(path: Path) -> tuple:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (-1, -1)


def _load_hints(path: Path) -> str:
    """The uncached read: hints file content, stripped + capped."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    if len(text) > MAX_HINTS_CHARS:
        text = (text[:MAX_HINTS_CHARS]
                + f"\n… [hints truncated at {MAX_HINTS_CHARS} chars]")
    return text


def hints_text(root: str | Path) -> str:
    """The project's .forge/hints.md, stripped and capped at
    MAX_HINTS_CHARS. "" when the file is missing, unreadable, or
    blank — no hints is the normal state for most projects."""
    path = forge_area(root) / "hints.md"
    fingerprint = _hints_fingerprint(path)
    cache_key = str(Path(root))
    cached = _hints_text_cache.get(cache_key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    text = _load_hints(path)
    if len(_hints_text_cache) >= 64:  # Sweep R153: bounded, as above
        _hints_text_cache.clear()
    _hints_text_cache[cache_key] = (fingerprint, text)
    return text


def _read_window(target: Path, offset: int, max_chars: int):
    """(window_text, total_chars) for a char window of a text file,
    read in bounded chunks — Sweep R141: read_source used to load
    the ENTIRE file into RAM to slice 7,000 chars out of it, so one
    huge file in .forge/sources/ could OOM the app. Only the
    requested window is retained; the rest of the stream is counted
    and dropped. Text mode throughout, so newline translation and
    the errors="replace" decoding match the old whole read exactly."""
    total = 0
    parts: list[str] = []
    end_wanted = offset + max_chars
    with target.open("r", encoding="utf-8", errors="replace") as fh:
        while True:
            piece = fh.read(65536)
            if not piece:
                break
            start = total
            total += len(piece)
            lo = max(offset, start)
            hi = min(end_wanted, total)
            if lo < hi:
                parts.append(piece[lo - start:hi - start])
    return "".join(parts), total


def read_source(root: str | Path, name: str, max_chars: int = 7000,
                offset: int = 0) -> str:
    target = _inside_sources(Path(root), name)
    if not target.is_file():
        return f"Source not found: {name}"
    chunk, total = _read_window(target, max(0, offset), max_chars)
    # Audit C29: char-slice paging — a source longer than one slice
    # used to come back truncated with the rest UNREACHABLE (the old
    # "[truncated at …]" line named no way forward). The footer now
    # teaches the next offset, the read_file treatment (fix #16) in
    # chars. offset clamps into the file.
    offset = max(0, min(offset, total))
    if total and offset >= total:
        return f"Source {name} has {total} chars — offset {offset} is past the end."
    end = min(offset + max_chars, total)
    if offset == 0 and end == total:
        return chunk
    if end < total:
        return (chunk + f"\n… [showing chars {offset}–{end} of {total}"
                        f" — call read_source with offset={end} to continue]")
    return (chunk + f"\n… [showing chars {offset}–{end} of {total}"
                    f" — the end of the file]")


READ_SOURCE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_source",
        "description": ("Load a project source file (the project's library) by its index name. "
                        "Sources load in 7,000-char slices — when a slice ends with a footer "
                        "naming an offset, pass that offset to continue."),
        "parameters": {"type": "object",
                       "properties": {"name": {"type": "string"},
                                      "offset": {"type": "integer",
                                                 "description": "Char offset to start from (default 0)."}},
                       "required": ["name"]},
    },
}


def add_upload(root: str | Path, src: str | Path) -> Path:
    """Copy a user-handed file into the project's uploads/. Returns the copy's path."""
    area = ensure_project(root)
    src = Path(src)
    dest = area / "uploads" / src.name
    n = 2
    while dest.exists():
        dest = area / "uploads" / f"{src.stem}-{n}{src.suffix}"
        n += 1
    shutil.copy2(src, dest)
    return dest
