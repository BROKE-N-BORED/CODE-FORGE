"""Sub-agent handoffs + the knowledge keeper (Study item M7).

The shape, at Forge scale: a seat's return is not freeform
prose the caller has to mine — it closes in a fixed five-field
schema, and at task end ONE keeper pass decides what of it is
worth keeping in the project's .forge/learnings.md and discards
the rest. A finished task leaves a curated entry, never a raw
dump.

THE SCHEMA — plain marker lines, not JSON (small models and JSON
do not mix well; the marker convention follows the project's own
VERDICT: / FINDING: lines):

    ASKED: <the task as the seat understood it>
    DONE: <what it did>
    FILES: <files it read, ran or changed, one per line, or (none)>
    EVIDENCE: <commands run and what they returned / what it saw>
    OPEN: <anything unfinished, risks found, or (none)>
    DECISIONS: <what was decided, what was rejected and why,
                or (none)> — OPTIONAL (Remainder R8)

parse_handoff reads those lines wherever they sit in the return
(the seat's prose above them is untouched) and reports which
fields are missing. DECISIONS is never missing: the validator
requires only the five fields above, and a return without it
parses byte-identically to before — its decisions are simply [].
A return with at least one schema line is a seat that ATTEMPTED
the format; a return with none is the legacy verdict-only shape,
which the callers leave byte-identical and the keeper ignores —
it is still usable through its VERDICT line, and flagging every
old-style report would be noise, not signal.

REPAIR: a caller that gets an attempted-but-incomplete handoff
sends ONE repair prompt naming only the missing sections, then
accepts whatever comes back. A handoff still incomplete after
repair reaches its caller FLAGGED (a trailing [HANDOFF: ...]
line) with the raw text intact — content is never dropped to
gain structure (the M2/M4 principle).

THE KEEPER (keeper_pass) is programmatic — no model call. It
runs once at task end over the turn's validated handoffs and
keeps, at most MAX_KEEPER_BULLETS_PER_TASK bullets:
  - every OPEN item (unfinished work is the pointer worth
    persisting), as "Open (<seat>): <item>";
  - every DECISIONS item (Remainder R8 — what was decided is
    the record worth keeping), as "Decision (<seat>): <item>";
  - a tester FAIL verdict, as one pointer to the failure;
  - GoFish Blocker/Major findings, as pointers to the plan
    gate's own findings.
Decisions the cap drops are counted, not silently lost: the
pass returns decisions_dropped (the R7 truncation convention —
the count is on the record, in the return and the log).
A clean task — no open items, no failures, no findings — writes
nothing and creates no file. Bullets land in
<project>/.forge/learnings.md in CR1's bullet shape
("- <text> · Filed: <YYYY-MM-DD HH:MM> · From: keeper"),
deduped by CR1's rule (flattened, case-insensitive) and written
atomically — the same append helper CR1's learnings.py has.
CR1 COMPATIBILITY (merged 2026-11-11): the filing itself lives
exactly once, in forge/learnings.py — keeper_add below is a
no-raise adapter over learnings.add_learning (same file, bullet
shape, stamp, dedupe key and atomic write), translating its
contract into the keeper's: add_learning raises on refusal and
returns the path even for a duplicate that files nothing, so the
adapter returns None unless the file's bullet count grew.
"""
from __future__ import annotations

import re
from pathlib import Path

# ---------- the schema ----------

FIELDS = ("asked", "done", "files", "evidence", "open")

# Remainder R8: the OPTIONAL sixth section. It is NOT in FIELDS —
# the validator never requires it, the repair never asks for it,
# and a handoff without it parses exactly as before.
_HEADERS = {"ASKED": "asked", "DONE": "done", "FILES": "files",
            "EVIDENCE": "evidence", "OPEN": "open",
            "DECISIONS": "decisions"}

_HEADER_RE = re.compile(
    r"^\s*(ASKED|DONE|FILES|EVIDENCE|OPEN|DECISIONS)\s*:\s*(.*)$")

# A field value that means "nothing here", not a value.
_EMPTY_VALUES = {"", "none", "(none)", "-", "n/a", "nil"}


def _is_empty(value: str) -> bool:
    return str(value or "").strip().lower() in _EMPTY_VALUES


def _split_list(value: str) -> list[str]:
    """A FILES:/OPEN: value into items — one per line, commas also
    split, "- " bullets stripped, "(none)" dropped."""
    out: list[str] = []
    for chunk in str(value or "").replace(",", "\n").splitlines():
        item = chunk.strip().lstrip("-*• ").strip()
        if item and not _is_empty(item):
            out.append(item)
    return out


def _split_lines(value: str) -> list[str]:
    """A DECISIONS: value into items — one per line, "- " bullets
    stripped, "(none)" dropped. Commas do NOT split: a decision
    is a sentence, and "chose SQLite, rejected Postgres because
    it needs a server" is ONE decision, not two."""
    out: list[str] = []
    for chunk in str(value or "").splitlines():
        item = chunk.strip().lstrip("-*• ").strip()
        if item and not _is_empty(item):
            out.append(item)
    return out


def parse_handoff(text: str) -> tuple[dict, list[str], bool]:
    """Parse a seat return into (handoff, missing, attempted).

    handoff: {"asked": str, "done": str, "files": [str],
    "evidence": str, "open": [str], "decisions": [str]}.
    missing: the REQUIRED fields absent or empty, in schema
    order — DECISIONS is optional and never appears here.
    attempted: at least one schema header line was present at
    all."""
    values: dict[str, list[str]] = {}
    current: str | None = None
    for line in str(text or "").splitlines():
        m = _HEADER_RE.match(line)
        if m:
            current = _HEADERS[m.group(1)]
            values.setdefault(current, [])
            if m.group(2).strip():
                values[current].append(m.group(2).strip())
            continue
        if current is not None:
            values[current].append(line)
    handoff = {
        "asked": "\n".join(values.get("asked", [])).strip(),
        "done": "\n".join(values.get("done", [])).strip(),
        "files": _split_list("\n".join(values.get("files", []))),
        "evidence": "\n".join(values.get("evidence", [])).strip(),
        "open": _split_list("\n".join(values.get("open", []))),
        "decisions": _split_lines(
            "\n".join(values.get("decisions", []))),
    }
    missing = [f for f in FIELDS
               if (not handoff[f] if f in ("files", "open")
                   else _is_empty(handoff[f]))]
    return handoff, missing, bool(values)


def repair_instruction(missing: list[str]) -> str:
    """The one repair prompt: name only the missing sections, in
    the schema's own line format, and ask for nothing else."""
    lines = ["Your return is missing these sections: "
             + ", ".join(m.upper() for m in missing) + ".",
             "Reply with ONLY those sections, one per line, in "
             "exactly this format:"]
    for field in missing:
        lines.append(f"{field.upper()}: ...")
    return "\n".join(lines)


# ---------- the keeper's append (CR1's learnings.py does the filing) ----------

MAX_KEEPER_BULLETS_PER_TASK = 3


def _flatten(text) -> str:
    return " ".join(str(text or "").split())


def keeper_add(root: str | Path, text: str,
               source: str = "keeper") -> Path | None:
    """File one keeper bullet via CR1's learnings.add_learning and
    return its path, or None when nothing was filed (empty text,
    over-long text, a duplicate, or a full file — the keeper never
    raises, and never deletes to make room).

    The filing itself — bullet shape, stamp, dedupe key, atomic
    write — lives exactly once, in forge/learnings.py; this is a
    no-raise adapter translating add_learning's contract into the
    keeper's. add_learning raises ValueError on refusal and returns
    the path even for a duplicate that files nothing, so the
    adapter returns the path only when the file's bullet count
    actually grew."""
    from . import learnings as learnings_mod
    text = _flatten(text)
    if not text or len(text) > learnings_mod.MAX_TEXT_CHARS:
        return None
    before = len(learnings_mod.list_learnings(root, "project"))
    try:
        path = learnings_mod.add_learning(root, text, scope="project",
                                          source=source)
    except ValueError:
        return None
    after = len(learnings_mod.list_learnings(root, "project"))
    return path if after > before else None


# ---------- the keeper pass ----------

def _candidates(handoffs: list[dict]) -> list[tuple[str, str]]:
    """The keepable bullets for one task's handoffs, in order,
    as (kind, text) pairs — kind is "open", "decision",
    "failure" or "finding". OPEN items first per handoff, then
    decisions (Remainder R8), then failure pointers — a verdict
    or finding is evidence worth re-checking, a passing handoff
    with nothing open keeps nothing."""
    out: list[tuple[str, str]] = []
    for entry in handoffs or []:
        seat = str(entry.get("seat") or "seat")
        if entry.get("unparsed"):
            continue  # raw text lives in the history/log already
        parsed = entry.get("parsed") or {}
        for item in parsed.get("open") or []:
            out.append(("open",
                        f"Open ({seat}): {_flatten(item)[:200]}"))
        for item in parsed.get("decisions") or []:
            out.append(("decision",
                        f"Decision ({seat}): {_flatten(item)[:200]}"))
        evidence = str(parsed.get("evidence") or "")
        if seat == "tester" and "VERDICT: FAIL" in evidence.upper():
            first = next((ln.strip() for ln in evidence.splitlines()
                          if ln.strip()), "")
            asked = _flatten(parsed.get("asked") or "")[:120]
            out.append(("failure",
                        f"Tester reported FAIL on '{asked}': "
                        f"{first[:160]}"))
        for finding in entry.get("findings") or []:
            if str(finding.get("severity", "")).capitalize() in (
                    "Blocker", "Major"):
                out.append(("finding",
                            f"GoFish {finding['severity']}: "
                            f"{_flatten(finding.get('text', ''))[:200]}"))
    return out


def _keeper_log(log, event: str, **fields) -> None:
    """The keeper's log call in the caller's shape. The worker's
    _log takes (event, **fields); the window's log takes one
    plain string — and the fan-out path passes the window's, so
    the event-style call used to raise TypeError inside the
    pass, silently resetting `discarded` to 0 (fixed in place,
    Remainder R8). Try the event shape; a one-arg log earns the
    plain line instead. A log failure never eats the pass."""
    if log is None:
        return
    try:
        log(event, **fields)
    except TypeError:
        try:
            detail = ", ".join(f"{k} {v}"
                               for k, v in fields.items())
            log(f"{event}: {detail}" if detail else event)
        except Exception:
            pass
    except Exception:
        pass


def keeper_pass(root: str | Path, handoffs: list[dict],
                log=None) -> dict:
    """One programmatic pass over a finished task's handoffs.
    Writes at most MAX_KEEPER_BULLETS_PER_TASK curated bullets
    and logs what it kept and discarded. Never raises — a keeper
    failure must never eat the turn it closes. Returns
    {"kept": n, "discarded": n, "path": str|None,
    "decisions_dropped": n} — decisions_dropped counts the
    decision bullets the per-task cap dropped (the R7 truncation
    convention: the count is on the record, not silent)."""
    kept = 0
    path = None
    decisions_dropped = 0
    try:
        candidates = _candidates(handoffs)
        discarded = 0
        for kind, text in candidates:
            if kept >= MAX_KEEPER_BULLETS_PER_TASK:
                discarded += 1
                if kind == "decision":
                    decisions_dropped += 1
                continue
            filed = keeper_add(root, text)
            if filed is None:
                discarded += 1
            else:
                kept += 1
                path = str(filed)
        if log is not None and (kept or discarded):
            _keeper_log(log, "keeper_pass", kept=kept,
                        discarded=discarded, path=path,
                        decisions_dropped=decisions_dropped)
        return {"kept": kept, "discarded": discarded, "path": path,
                "decisions_dropped": decisions_dropped}
    except Exception as exc:  # the keeper never eats the turn
        if log is not None:
            _keeper_log(log, "keeper_failed", error=str(exc)[:160])
        return {"kept": kept, "discarded": 0, "path": path,
                "decisions_dropped": decisions_dropped}
