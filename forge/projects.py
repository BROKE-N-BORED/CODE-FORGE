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

Project System (2026-11-11, owner's orders): creating a project lays
down the WHOLE prebuilt scaffold up front — every .forge folder the
app files into anywhere in the codebase (derived from the code's own
path constructions, listed in SCAFFOLD_FOLDERS), a PROMPT.md in every
folder saying what belongs there (the folder prompts are LIVE: the
mapped mode's folder prompt rides in the system prompt — see
folder_prompt_text), a brief.md (the project's own memory), and the
project's own settings.json. ensure_project stays idempotent: it
seeds what is missing and never overwrites what exists.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
from datetime import date
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


# ------------------------------------------------------------------
# Project System (2026-11-11): prebuilt scaffold, folder prompts,
# project types, per-project settings, git start.
# ------------------------------------------------------------------

#: The project types offered at creation — the owner's reference-build
#: profiles (forge/reference_build.py). "generic" is the default and
#: the only one that does NOT write reference/profile.txt: a non-empty
#: profile.txt is the reference-driven workflow's opt-in record, so a
#: generic project must not acquire one by accident.
PROJECT_TYPES = ("generic", "app", "tool", "data", "writing", "music",
                 "game")

#: Every .forge folder the app files into, derived from the code's own
#: path constructions (not invented): sources/uploads/reference from
#: this module; artifacts from forge/artifacts.py; design + its six
#: kind folders and architecture/reviews/research from the mode
#: windows' DOC_TABS in forge/main.py (design canvas files at the
#: design root too); mode-sessions from ModeWindow's session store;
#: write-history from WriteWindow's snapshot store.
SCAFFOLD_FOLDERS = (
    "sources",
    "uploads",
    "reference",
    "artifacts",
    "design",
    "design/layout",
    "design/ui",
    "design/flow",
    "design/mechanics",
    "design/aesthetic",
    "design/systems",
    "architecture",
    "reviews",
    "research",
    "mode-sessions",
    "write-history",
)

ROOT_PROMPT = """# This project's .forge folder
Everything Code Forge keeps for this project lives here. Each folder
has its own PROMPT.md saying what belongs in it and what the agent
does with it. The files at this level:
- instructions.md — your rules for the agent in this project.
- brief.md — the project brief: what this project is, where it
  stands, what was decided, what happens next.
- scratchpad.md — the agent's persistent working notes.
- hints.md — your short standing notes (conventions, commands,
  gotchas); they ride in every prompt, so keep them short.
- settings.json — this project's own settings.
You can edit any of these files. Your edits win — the scaffold only
ever creates what is missing, it never overwrites.
"""

FOLDER_PROMPTS = {
    "sources": """# Sources — the project's library
Specs, references, knowledge files, and existing material the agent
should know for this project. Only the index — each file's name and
first line — rides in the agent's context; full files load when the
agent calls read_source. Keep first lines descriptive: they ARE the
index. Standing knowledge belongs here; one-off hand-overs start in
uploads/ and move here when they turn out to be keepers.
""",
    "uploads": """# Uploads — the inbox
Files the owner hands the agent land here first, with receipts.
Uploads are raw hand-overs, not the library: when a file turns out
to be standing knowledge for the project, move it into sources/.
""",
    "reference": """# Reference — the reference-driven build area
profile.txt (first line) names this project's build profile and
turns the reference-driven workflow on for this project: reference
lock, breakdown, spec, prototype slice, expansion grid, fix loop.
lock.md, breakdown.md and fix-log.md live here while that workflow
runs. No profile.txt = the workflow stays off for this project.
""",
    "artifacts": """# Artifacts — filed work products
Plans, designs, architecture notes, reviews and walkthroughs the app
files for this project land here, indexed in index.json. These are
records of finished work — read them for history. Live documents a
mode is still drafting sit in that mode's own folder.
""",
    "design": """# Design
Everything Design mode produces files under here, in kind folders:
layout, ui, flow, mechanics, aesthetic, systems. Design documents
describe what will be built; they are not the code that builds it.
""",
    "design/layout": """# Design — layout
Layout documents: page and screen structure, what sits where, and
how the space is divided.
""",
    "design/ui": """# Design — UI
Interface documents: elements, controls, states, and how each piece
of the interface behaves.
""",
    "design/flow": """# Design — flow
Flow documents: user journeys, screen-to-screen movement, and the
order things happen in.
""",
    "design/mechanics": """# Design — mechanics
Mechanics documents: the rules of how it works — interactions,
systems behavior, cause and effect.
""",
    "design/aesthetic": """# Design — aesthetic
Aesthetic documents: look and feel, palette, typography, imagery,
and the mood the work should carry.
""",
    "design/systems": """# Design — systems
Systems documents: the systems underneath the design and how they
connect to each other.
""",
    "architecture": """# Architecture
Architecture notes, decisions and specs from Architect mode.
Decisions recorded here are the project's memory for why it is built
the way it is — when a decision changes, add the new one instead of
erasing the old.
""",
    "reviews": """# Reviews
Review reports from Review mode: each report opens with a
Walkthrough — what changed, the changes in dependency order
grouped by layer, an Effort line and a Risk line — before the
findings by severity. Every finding is one line carrying
its severity (Critical / Major / Minor / Trivial), its type (Bug /
Security / Performance / Refactor / Nitpick) and its Source — what
triggered it — plus the fix it suggests. Under each finding, one
indented line starting 'Driver:' carries the Prompt for the
Driver — a self-contained instruction for Code mode to fix
exactly that finding (file, exact change, how to verify). When
the owner sends a finding for fixing, the request is recorded
in the review under a '## Fix requests' section at the end
(date, finding, and the prompt that was sent), so the review
carries its own fix history. Reviews change nothing by
themselves — they are the record the owner acts on.
Incremental reviews cover only the files changed since the last
review the owner marked reviewed; after filing a report, update
summary.md at the top with one block per review (date, review
file, What-changed line, severity counts), newest first, so it
reads as the whole review history at a glance.
""",
    "research": """# Research
Research plans and reports from Research mode. Plans are named
*-plan.md and were approved before anything browsed; reports carry
their sources cited and their sources consulted, listed separately.
Nothing browses for a report that has no approved plan.
""",
    "mode-sessions": """# Mode sessions
One file per mode room: that room's session-work transcript for this
project, so a room remembers what happened in it between visits.
The app appends here; these files are records, not instructions.
""",
    "write-history": """# Write history
Version snapshots of documents worked on in the Write room: one
folder per document, one snapshot per save point, so Previous/Next
version stepping always has somewhere real to step to. Snapshots are
safety copies — the live document is the file itself, wherever it
lives in the project.
""",
}

#: The brief's Status guidance, per project type — the only place the
#: brief skeleton differs by type, because it is the only line whose
#: honest wording depends on what kind of project this is.
BRIEF_STATUS_LINES = {
    "generic": "What state is the project in right now — what "
               "exists, what works, what doesn't?",
    "app": "What state is the app in — what runs today, what's "
           "broken, what's missing?",
    "tool": "What state is the tool in — what it does today, what "
            "it can't yet?",
    "data": "What state is the data in — what's collected, what's "
            "clean, what's missing?",
    "writing": "What's drafted, what's outlined, what's still "
               "missing?",
    "music": "What's written or recorded, what's in progress, "
             "what's next to make?",
    "game": "What can you play today, what's stubbed, what's "
            "missing?",
}


def _clean_project_type(value) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in PROJECT_TYPES else None


def _seed_file(path: Path, text: str) -> None:
    """Write a starter file only when it is missing — the scaffold
    never overwrites anything the owner (or the app) already made."""
    if not path.exists():
        path.write_text(text, encoding="utf-8")


def _starter_brief(name: str, project_type: str) -> str:
    return (f"# Project brief — {name}\n"
            f"Type: {project_type} · Created: {date.today().isoformat()}\n"
            f"\n"
            f"The project's own memory: what it is, where it stands, "
            f"what was decided, what happens next. The agent reads "
            f"this when the project opens; keep it current as work "
            f"lands.\n"
            f"\n"
            f"## Status\n"
            f"{BRIEF_STATUS_LINES[project_type]}\n"
            f"\n"
            f"## Decisions\n"
            f"\n"
            f"## Next\n")


def project_settings(root: str | Path) -> dict:
    """The project's own settings (.forge/settings.json). {} when the
    file is missing or unreadable — no settings is a normal state."""
    try:
        data = json.loads(
            (forge_area(root) / "settings.json").read_text(
                encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def set_project_setting(root: str | Path, key: str, value) -> dict:
    """Set one key in the project's own settings and return the whole
    dict. Writes only inside this project's .forge — global settings
    and other projects are never touched."""
    area = forge_area(root)
    area.mkdir(parents=True, exist_ok=True)
    data = project_settings(root)
    data[key] = value
    path = area / "settings.json"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return data


def _recorded_project_type(area: Path) -> str | None:
    try:
        data = json.loads((area / "settings.json").read_text(
            encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict):
        return _clean_project_type(data.get("project_type"))
    return None


def ensure_project(root: str | Path, project_type: str | None = None) -> Path:
    """Lay down the project's complete prebuilt scaffold.

    Creates the full .forge tree (SCAFFOLD_FOLDERS), a PROMPT.md in
    every folder, and the starter files (instructions, scratchpad,
    hints, brief, settings, and — for a non-generic type — the
    reference profile record). Idempotent and non-destructive: only
    what is missing is created, so re-running it on a lived-in
    project changes nothing. project_type, when given and valid,
    is recorded in settings (the project's type of record); when
    absent, a previously recorded type is kept, else "generic".
    """
    area = forge_area(root)
    for rel in SCAFFOLD_FOLDERS:
        (area / rel).mkdir(parents=True, exist_ok=True)
    ptype = (_clean_project_type(project_type)
             or _recorded_project_type(area)
             or "generic")
    _seed_file(area / "instructions.md", STARTER_INSTRUCTIONS)
    _seed_file(area / "scratchpad.md", STARTER_SCRATCHPAD)
    _seed_file(area / "hints.md", "")
    _seed_file(area / "brief.md",
               _starter_brief(Path(root).name, ptype))
    settings_path = area / "settings.json"
    if not settings_path.exists():
        settings_path.write_text(json.dumps(
            {"version": 1, "project_type": ptype,
             "created": date.today().isoformat()},
            indent=2) + "\n", encoding="utf-8")
    elif _clean_project_type(project_type) is not None:
        set_project_setting(root, "project_type", ptype)
    # profile.txt is the reference-driven workflow's opt-in record:
    # only a non-generic type writes one (see PROJECT_TYPES note).
    if ptype != "generic":
        _seed_file(area / "reference" / "profile.txt", ptype + "\n")
    _seed_file(area / "PROMPT.md", ROOT_PROMPT)
    for rel, text in FOLDER_PROMPTS.items():
        _seed_file(area / rel / "PROMPT.md", text)
    return area


# --- Live folder prompts -------------------------------------------
# A mode's folder prompt rides in the system prompt for turns in that
# mode (build_system_prompt in forge/main.py), so the folder's rules
# are working instructions, not decoration. The map names the folder
# each mode FILES into, per the code: Plan and Code file their
# documents as artifacts (forge/artifacts.py KINDS), Design files
# under design/, Architect under architecture/, Review under
# reviews/, Research under research/. Write's documents are the
# project's own files (its DOC_TABS point at the project root), so
# Write carries the .forge root prompt. Ask files nothing and gets
# none. Reads get the same stat-fingerprint cache as hints/sources.

#: mode -> .forge-relative folder whose PROMPT.md rides for that mode
#: ("" = the .forge root itself).
MODE_PROMPT_FOLDERS = {
    "plan": "artifacts",
    "code": "artifacts",
    "design": "design",
    "architect": "architecture",
    "review": "reviews",
    "research": "research",
    "write": "",
}

MAX_FOLDER_PROMPT_CHARS = 2000
_folder_prompt_cache: dict[str, tuple[tuple, str]] = {}


def _load_folder_prompt(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    if len(text) > MAX_FOLDER_PROMPT_CHARS:
        text = (text[:MAX_FOLDER_PROMPT_CHARS]
                + f"\n… [folder prompt truncated at "
                  f"{MAX_FOLDER_PROMPT_CHARS} chars]")
    return text


def _folder_prompt(root: str | Path, rel: str) -> str:
    """One folder's PROMPT.md text (cached by stat fingerprint)."""
    path = forge_area(root) / rel / "PROMPT.md" if rel \
        else forge_area(root) / "PROMPT.md"
    fingerprint = _hints_fingerprint(path)
    cache_key = str(path)
    cached = _folder_prompt_cache.get(cache_key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    text = _load_folder_prompt(path)
    if len(_folder_prompt_cache) >= 64:  # bounded, as the other caches
        _folder_prompt_cache.clear()
    _folder_prompt_cache[cache_key] = (fingerprint, text)
    return text


def folder_prompt_text(root: str | Path, mode: str) -> str:
    """The PROMPT.md of the folder *mode* files into, or "" when the
    mode files nowhere (ask) or no prompt exists."""
    rel = MODE_PROMPT_FOLDERS.get(str(mode or "").strip().lower())
    if rel is None:
        return ""
    return _folder_prompt(root, rel)


def sources_prompt_text(root: str | Path) -> str:
    """The sources folder's PROMPT.md (rides when the sources index
    rides — the library's own rules next to its index)."""
    return _folder_prompt(root, "sources")


# --- Git start -------------------------------------------------------

def git_start(root: str | Path, git: str = "git") -> dict:
    """Initialize version history for a brand-new project and take
    the first snapshot (the scaffold as created). Never raises and
    never blocks creation — the result dict says what happened:
      {"status": "existing"}            — already a git repo
      {"status": "no-git"}              — git is not installed
      {"status": "started", "commit": sha}
      {"status": "failed", "error": words}
    Identity discipline mirrors forge/checkpoints.py: the Forge
    identity rides per-command only when the repo has no usable
    identity of its own, and signing is off so a fresh PC can never
    hang on a prompt this subprocess cannot answer.
    """
    root = Path(root)
    if (root / ".git").exists():
        return {"status": "existing"}
    if shutil.which(git) is None:
        return {"status": "no-git"}

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([git, "-C", str(root), *args],
                              capture_output=True, text=True, timeout=30)

    try:
        proc = run("init", "-q")
        if proc.returncode != 0:
            return {"status": "failed",
                    "error": (proc.stderr or "git init failed").strip()[:200]}
        run("add", "-A")
        name = run("config", "user.name").stdout.strip()
        email = run("config", "user.email").stdout.strip()
        flags: list[str] = []
        if not name or not email:
            flags += ["-c", "user.name=Code Forge",
                      "-c", "user.email=forge@localhost"]
        flags += ["-c", "commit.gpgsign=false"]
        proc = run(*flags, "commit", "-q", "-m",
                   "Project created — prebuilt scaffold (Code Forge)")
        if proc.returncode != 0:
            return {"status": "failed",
                    "error": (proc.stderr or "git commit failed").strip()[:200]}
        sha = run("rev-parse", "--short", "HEAD").stdout.strip()
        return {"status": "started", "commit": sha}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "error": str(exc)[:200]}


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
