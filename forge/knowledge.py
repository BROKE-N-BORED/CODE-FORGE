"""Project Knowledge (owner-approved build, 2026-11-11).

Work distills itself into knowledge the project keeps — but nothing
the agent distills is believed until the owner approves it.

Storage, deliberately plain like the artifact store:

  .forge/knowledge/          — APPROVED entries, one .md file each
  .forge/knowledge/drafts/   — distilled or hand-added drafts awaiting
                               the owner's gate; a draft is invisible
                               to the model (see below)
  .forge/brief.md            — the project brief; approving an entry
                               appends its one-line summary under the
                               brief's "## Knowledge" section, so the
                               brief compounds as knowledge is approved.
                               Appends are insertion-only — owner text
                               already in the brief is never rewritten.

An entry file:

  # <title>
  Summary: <one line>
  Filed: <stamp> · Source: distilled | owner

  <body>

Context contract (pinned by tests): ONLY approved entries reach a
turn, and only as an INDEX — name + one-line summary, exactly like
the sources library (forge/projects.py). Full text loads on demand
via the read_knowledge tool, in slices. Drafts contribute NOTHING —
not their index lines, not their text — until approved.

Distillation itself lives here too: distill() asks the fast seat to
compress a finished turn (what was asked, what was answered) into a
draft entry. It writes DRAFTS ONLY, and it never raises — a failed
distillation is a log line, never a broken turn.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .projects import forge_area, _read_window

KNOWLEDGE_DIRNAME = "knowledge"
DRAFTS_DIRNAME = "drafts"
BRIEF_FILENAME = "brief.md"
MAX_BRIEF_CHARS = 4000
MAX_ENTRY_TEXT = 200_000

STARTER_BRIEF = """# Project brief
What this project is, where it stands, and what matters about it.
The owner writes and edits this freely; the agent reads it first when
a project reopens. Approving a knowledge entry appends its summary
under "## Knowledge" below — nothing else in this file is ever
rewritten by the agent.
"""

_DISTILL_SYSTEM = (
    "You are the fast seat of a coding agent, filing project knowledge. "
    "You are given one finished turn: what the owner asked and what the "
    "agent answered/did. Distill ONLY what a future session in THIS "
    "project would need: decisions made and why, dead ends already "
    "tried, and the current state of the work. No praise, no filler, "
    "no restating the whole answer. If the turn holds nothing worth "
    "keeping, reply with exactly: NOTHING.\n"
    "Otherwise reply in exactly this shape:\n"
    "TITLE: <short title>\n"
    "SUMMARY: <one line, at most 110 characters>\n"
    "<the entry body, a few short lines>"
)


# ---------- layout ----------

def knowledge_area(root: str | Path) -> Path:
    return forge_area(root) / KNOWLEDGE_DIRNAME


def drafts_area(root: str | Path) -> Path:
    return knowledge_area(root) / DRAFTS_DIRNAME


def brief_path(root: str | Path) -> Path:
    return forge_area(root) / BRIEF_FILENAME


def ensure_knowledge(root: str | Path) -> Path:
    """Create the knowledge + drafts folders if missing. Creates
    nothing else and overwrites nothing."""
    area = knowledge_area(root)
    (area / DRAFTS_DIRNAME).mkdir(parents=True, exist_ok=True)
    return area


def ensure_brief(root: str | Path) -> Path:
    """Create .forge/brief.md from the starter if missing. An
    existing brief — above all one the owner has edited — is never
    touched."""
    path = brief_path(root)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STARTER_BRIEF, encoding="utf-8")
    return path


# ---------- entry files ----------

def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug[:48] or "knowledge"


def _entry_text(title: str, summary: str, body: str, source: str) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (f"# {title}\n"
            f"Summary: {summary}\n"
            f"Filed: {stamp} · Source: {source}\n"
            f"\n{body.strip()}\n")


def _write_unique(folder: Path, title: str, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    counter = 0
    while True:
        suffix = "" if counter == 0 else f"-{counter}"
        path = folder / f"{stamp}-{_slug(title)}{suffix}.md"
        try:
            with open(path, "x", encoding="utf-8") as fh:
                fh.write(text)
            return path
        except FileExistsError:
            counter += 1


def _parse_entry(path: Path) -> dict:
    """{name, title, summary} for one entry file. The title is the
    '# ' heading; the summary is the 'Summary:' line, falling back
    to the first plain line — the same first-line discipline the
    sources index uses."""
    title, summary = "", ""
    try:
        for line in path.read_text(encoding="utf-8",
                                   errors="replace").splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("# ") and not title:
                title = s[2:].strip()
                continue
            if s.lower().startswith("summary:"):
                summary = s.split(":", 1)[1].strip()[:110]
                break
            if not summary and not s.startswith("#"):
                summary = s.lstrip("# ").strip()[:110]
                break
    except Exception:
        pass
    return {"name": path.name,
            "title": title or path.stem,
            "summary": summary or "(no summary line)"}


def _list_folder(folder: Path) -> list[dict]:
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.glob("*.md")):
        if p.is_file():
            entry = _parse_entry(p)
            try:
                entry["bytes"] = p.stat().st_size
            except OSError:
                entry["bytes"] = 0
            out.append(entry)
    return out


def list_knowledge(root: str | Path) -> list[dict]:
    """The APPROVED entries' index (drafts are a different folder
    and a different function — they never appear here)."""
    return _list_folder(knowledge_area(root))


def list_drafts(root: str | Path) -> list[dict]:
    """Drafts awaiting the owner's gate, oldest first."""
    return _list_folder(drafts_area(root))


def _inside(folder: Path, name: str) -> Path:
    """Resolve `name` confined to `folder` — the sources discipline
    (a cloned project must not aim a read or a delete outside)."""
    base = folder.resolve()
    target = (base / str(name)).resolve()
    if target != base and base not in target.parents:
        raise ValueError("Knowledge path is outside the knowledge folder.")
    return target


# ---------- writing: drafts, hand-adds, the gate ----------

def write_draft(root: str | Path, title: str, summary: str,
                body: str, source: str = "distilled") -> Path:
    """File one DRAFT entry. Drafts reach no context, ever — the
    owner's approval is what promotes an entry (approve_draft)."""
    title = (title or "Knowledge").strip() or "Knowledge"
    summary = (summary or "").strip()[:110] or "(no summary line)"
    body = (body or "").strip()
    if len(body) > MAX_ENTRY_TEXT:
        body = body[:MAX_ENTRY_TEXT]
    return _write_unique(drafts_area(root), title,
                         _entry_text(title, summary, body, source))


def add_entry(root: str | Path, title: str, body: str,
              summary: str = "", approved: bool = False) -> Path:
    """The owner's own hand-add (the gate UI's Add action). Lands as
    a draft by default — behind the same gate as distilled entries —
    or straight into approved when the owner chooses, in which case
    its summary also compounds onto the brief, exactly like an
    approval."""
    title = (title or "Knowledge").strip() or "Knowledge"
    body = (body or "").strip()
    if not summary:
        for line in body.splitlines():
            if line.strip():
                summary = line.strip().lstrip("# ").strip()[:110]
                break
    summary = (summary or "").strip()[:110] or "(no summary line)"
    if approved:
        text = _entry_text(title, summary, body, "owner")
        path = _write_unique(knowledge_area(root), title, text)
        append_knowledge_to_brief(root, title, summary)
        return path
    return write_draft(root, title, summary, body, source="owner")


def approve_draft(root: str | Path, name: str) -> Path:
    """The gate's Approve: move a draft into the approved store and
    compound its summary onto the brief. Returns the entry's new
    path. The draft file itself is moved, never copied-and-left —
    one entry, one state."""
    src = _inside(drafts_area(root), name)
    if not src.is_file():
        raise ValueError(f"No such knowledge draft: {name}")
    entry = _parse_entry(src)
    dest_folder = knowledge_area(root)
    dest_folder.mkdir(parents=True, exist_ok=True)
    dest = dest_folder / src.name
    counter = 1
    while dest.exists():
        dest = dest_folder / f"{src.stem}-{counter}.md"
        counter += 1
    src.replace(dest)
    append_knowledge_to_brief(root, entry["title"], entry["summary"])
    return dest


def discard_draft(root: str | Path, name: str) -> bool:
    """The gate's Discard: the draft is deleted — it never reached
    context and now never will. True when a file was removed."""
    target = _inside(drafts_area(root), name)
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# ---------- the brief ----------

def append_knowledge_to_brief(root: str | Path, title: str,
                              summary: str) -> Path:
    """Append one approved entry's summary line under the brief's
    '## Knowledge' section (creating the brief, and the section, if
    missing). INSERTION-ONLY: the text is split at the insertion
    point and rejoined — every existing line, above all the owner's
    own edits, survives byte-for-byte."""
    path = ensure_brief(root)
    text = path.read_text(encoding="utf-8")
    line = f"- {title} — {summary}"
    lines = text.split("\n")
    section_at = None
    insert_at = None
    for i, raw in enumerate(lines):
        if raw.strip() == "## Knowledge":
            section_at = i
            insert_at = i + 1
            continue
        if section_at is not None and raw.startswith("## "):
            break
        if section_at is not None:
            insert_at = i + 1
    if section_at is None:
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n## Knowledge\n\n{line}\n"
    else:
        # Land the line after the section's existing content (past
        # any blank line right under the heading).
        while insert_at < len(lines) and not lines[insert_at].strip():
            insert_at += 1
        while (insert_at < len(lines)
               and lines[insert_at].startswith("- ")):
            insert_at += 1
        lines.insert(insert_at, line)
        text = "\n".join(lines)
    path.write_text(text, encoding="utf-8")
    return path


def brief_text(root: str | Path) -> str:
    """The brief for context assembly: content, stripped and capped
    like hints — a reopened project loads its brief first. '' when
    there is no brief or it is blank."""
    path = brief_path(root)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    if len(text) > MAX_BRIEF_CHARS:
        text = (text[:MAX_BRIEF_CHARS]
                + f"\n… [brief truncated at {MAX_BRIEF_CHARS} chars]")
    return text


# ---------- the approved index in context (sources pattern) ----------

# The same stat-only fingerprint cache discipline as the sources
# index (projects.py, audit B16): build_system_prompt asks on EVERY
# send, so a hit reads no file content; any added / removed /
# changed APPROVED entry misses and rebuilds. Drafts are not
# fingerprinted or read at all — they are not in this folder.
_index_text_cache: dict[str, tuple[tuple, str]] = {}


def _index_fingerprint(base: Path) -> tuple:
    if not base.is_dir():
        return ()
    fp = []
    for p in sorted(base.glob("*.md")):
        if p.is_file():
            try:
                st = p.stat()
                fp.append((p.name, st.st_mtime_ns, st.st_size))
            except OSError:
                fp.append((p.name, -1, -1))
    return tuple(fp)


def knowledge_index_text(root: str | Path) -> str:
    """Index lines for APPROVED entries only — name + one-line
    summary, the sources pattern. Drafts contribute nothing: this
    function never opens the drafts folder."""
    base = knowledge_area(root)
    fingerprint = _index_fingerprint(base)
    cache_key = str(Path(root))
    cached = _index_text_cache.get(cache_key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    index = list_knowledge(root)
    if not index:
        text = ""
    else:
        lines = ["Project knowledge — lessons distilled from finished "
                 "work in THIS project and approved by the owner. Call "
                 "read_knowledge with an entry's name to load it (or "
                 "the slice you need) before relying on it:"]
        for entry in index:
            lines.append(f"- {entry['name']}: {entry['summary']}")
        text = "\n".join(lines)
    if len(_index_text_cache) >= 64:  # bounded, as the sources cache
        _index_text_cache.clear()
    _index_text_cache[cache_key] = (fingerprint, text)
    return text


def read_knowledge(root: str | Path, name: str, max_chars: int = 7000,
                   offset: int = 0) -> str:
    """Load an APPROVED entry in slices — read_source's discipline,
    confined to the approved folder (a draft name reads as
    not-found: drafts are not knowledge yet)."""
    target = _inside(knowledge_area(root), name)
    if not target.is_file():
        return f"Knowledge entry not found: {name}"
    chunk, total = _read_window(target, max(0, offset), max_chars)
    offset = max(0, min(offset, total))
    if total and offset >= total:
        return (f"Knowledge entry {name} has {total} chars — "
                f"offset {offset} is past the end.")
    end = min(offset + max_chars, total)
    if offset == 0 and end == total:
        return chunk
    if end < total:
        return (chunk + f"\n… [showing chars {offset}–{end} of {total}"
                        f" — call read_knowledge with offset={end} "
                        f"to continue]")
    return (chunk + f"\n… [showing chars {offset}–{end} of {total}"
                    f" — the end of the entry]")


READ_KNOWLEDGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_knowledge",
        "description": ("Load an owner-approved project knowledge entry "
                        "by its index name. Entries load in 7,000-char "
                        "slices — when a slice ends with a footer "
                        "naming an offset, pass that offset to continue."),
        "parameters": {"type": "object",
                       "properties": {"name": {"type": "string"},
                                      "offset": {"type": "integer",
                                                 "description":
                                                 "Char offset to start "
                                                 "from (default 0)."}},
                       "required": ["name"]},
    },
}


# ---------- distillation ----------

def _collect_reply(client, model, messages) -> str:
    chunks = []
    for event in client.chat_stream(model, messages, {}, None):
        if not isinstance(event, dict):
            continue
        content = (event.get("message") or {}).get("content", "")
        if content:
            chunks.append(str(content))
    return "".join(chunks)


def _parse_distillation(text: str):
    """(title, summary, body) from the seat's reply, or None when
    the seat answered NOTHING (a turn with nothing worth keeping
    files no draft). Lenient on shape: missing TITLE/SUMMARY lines
    fall back to derived values rather than losing the entry."""
    stripped = (text or "").strip()
    if not stripped or stripped.upper() == "NOTHING":
        return None
    title = summary = ""
    body_lines = []
    for line in stripped.splitlines():
        s = line.strip()
        if s.lower().startswith("title:") and not title:
            title = s.split(":", 1)[1].strip()
        elif s.lower().startswith("summary:") and not summary:
            summary = s.split(":", 1)[1].strip()
        else:
            body_lines.append(line)
    body = "\n".join(body_lines).strip()
    if not body:
        body = stripped
    if not summary:
        for line in body.splitlines():
            if line.strip():
                summary = line.strip().lstrip("# ").strip()
                break
    return title, summary[:110], body


def distill(client, model, root: str | Path, prompt: str, reply: str,
            log=None):
    """Draft one knowledge entry from a finished turn, via the fast
    seat. Returns the draft's path, or None (nothing worth keeping,
    or anything at all went wrong).

    NEVER raises and never touches the approved store: distillation
    runs after a turn has settled, and its failure is a log line —
    the owner's finished work is never held hostage by a filing
    step."""
    try:
        if not (reply or "").strip():
            return None
        messages = [
            {"role": "system", "content": _DISTILL_SYSTEM},
            {"role": "user", "content":
             "THE OWNER ASKED:\n" + (prompt or "")[:2000]
             + "\n\nTHE AGENT ANSWERED / DID:\n" + (reply or "")[:6000]},
        ]
        text = _collect_reply(client, model, messages)
        parsed = _parse_distillation(text)
        if parsed is None:
            return None
        title, summary, body = parsed
        if not title:
            title = ((prompt or "Turn").strip().splitlines()
                     or ["Turn"])[0][:60]
        return write_draft(root, title, summary, body,
                           source="distilled")
    except Exception as exc:
        if log is not None:
            try:
                log(f"Knowledge distillation failed: {exc}")
            except Exception:
                pass
        return None
