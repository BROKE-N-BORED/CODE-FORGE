"""Planning workflow — Note 12 (pure stdlib, no Qt, like skribe.py).

The workflow, in the owner's shape: in Plan mode the worker's first
response is captured with NO tools offered, parsed as a plan, and held
at a gate the owner resolves — approve / edit / kill — under the F8
rule (no clock on the decision). On approval the plan becomes the
turn's working instruction and a live checklist ticks as the driver
announces "STEP <n> DONE"; the checklist's final line is the turn's
SKRIBE outcome. This module owns everything about that which is not
Qt: the plan-text parser, the checklist state dict, and the state's
persistence.

Persistence home: a sidecar JSON per conversation in
APP_DIR/plans/<conversation_id>.json — the same pattern as the
self-update resume state (forge/update.py writes a JSON sidecar into
APP_DIR). The conversation store's schema is fixed columns for
messages/model/project; a plan is living UI state (step states tick
per announcement, the file is rewritten on each), so it does not
belong inside the stored message list, where it would be rendered as
chat and rewritten by every persist_conversation call.

Item #58: the draft the worker captures is now a plan DOCUMENT
(forge/modes.py owns the workflow text; the shell drafts it section
by section against a required skeleton). The document's full text
rides the sidecar as `document`; the checklist below is EXTRACTED
from the document's Steps section (extract_steps) and the gate,
STEP DONE ticks, and progress re-injection consume it exactly as
before. Bare-list drafts — pre-#58 sidecars, a model that answers
with a plain list, a Goldfish revision — keep working through
parse_plan untouched.

Item C7: the document skeleton also carries Success criteria and
Out of scope sections (forge/modes.py SKELETONS). They are just
document sections — the sidecar format does not change, the gate
does not change — extracted on demand by success_criteria() /
out_of_scope() below and projected onto the phone deck by
deck_plan(). A pre-C7 document has neither section: the extractors
return "" and every surface renders exactly as before.
"""
from __future__ import annotations
import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import goldfish as goldfish_mod

STEP_STATES = ("pending", "active", "done", "interrupted")
PLAN_STATUSES = ("draft", "running", "done", "killed", "interrupted")

_NUM_RE = re.compile(r"^\s*(\d+)[.)]\s+(\S.*)$")
_BOX_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s+(\S.*)$")
_BULLET_RE = re.compile(r"^\s*(?:\d+[.)]|[-*]\s*(?:\[[ xX]\]\s*)?)\s*")
_STEP_DONE_RE = re.compile(r"\bSTEP\s+(\d+)\s+DONE\b", re.IGNORECASE)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_plan(text: str) -> list[str] | None:
    """Parse a model response into plan steps, or None when it is not
    a plan. Two formats qualify:

      numbered   "1. Do this" / "2) Do that" — the longest run of
                 consecutive numbering must hold >= 2 steps;
      checkbox   "- [ ] Do this" / "- [x] Do that" — >= 2 items.

    Anything else (prose, a lone numbered line, broken numbering) is
    NOT a plan: the caller treats the response as a normal answer, so
    a model that ignores the format never traps the owner in a gate
    with nothing behind it. When both formats qualify, the longer
    list wins; ties go to the one whose first item appears first."""
    numbered: list[tuple[int, int, str]] = []  # (line no, number, text)
    boxed: list[tuple[int, str]] = []          # (line no, text)
    for lineno, line in enumerate(str(text or "").splitlines()):
        m = _NUM_RE.match(line)
        if m:
            numbered.append((lineno, int(m.group(1)), m.group(2).strip()))
            continue
        m = _BOX_RE.match(line)
        if m:
            boxed.append((lineno, m.group(1).strip()))
    num_steps = _longest_numbered_run(numbered)
    box_steps = [t for _ln, t in boxed] if len(boxed) >= 2 else None
    if num_steps and box_steps:
        if len(box_steps) > len(num_steps):
            return box_steps
        if len(num_steps) > len(box_steps):
            return num_steps
        return num_steps if numbered[0][0] <= boxed[0][0] else box_steps
    return num_steps or box_steps


def _longest_numbered_run(numbered: list[tuple[int, int, str]]) -> list[str] | None:
    best: list[str] = []
    run: list[str] = []
    prev = None
    for _ln, n, text in numbered:
        if prev is not None and n == prev + 1:
            run.append(text)
        else:
            run = [text]
        prev = n
        if len(run) > len(best):
            best = run
    return best if len(best) >= 2 else None


# --- Item #58: the document is the plan ---------------------------
# A plan draft is now a Markdown DOCUMENT with a required skeleton
# (forge/modes.py); the checklist the gate consumes is EXTRACTED
# from its Steps section. A step there is a numbered (or checkbox)
# line plus its continuation lines — the detail and the "Verify:"
# line — joined into the step's single string, so detail has
# somewhere to live instead of being cut off at the checklist.

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def _section_span(text: str, name: str) -> tuple[int, int, int] | None:
    """The span of one ## section in a document: (body_start,
    body_end, heading_line_start) as character offsets, or None.
    The body runs until the next heading of level <= 2 or EOF."""
    lines = str(text or "").split("\n")
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    start = None
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        if start is None:
            if level <= 2 and m.group(2).rstrip(":").lower() == name.lower():
                start = i
        elif level <= 2:
            return (offsets[start] + len(lines[start]) + 1,
                    offsets[i], offsets[start])
    if start is None:
        return None
    return (offsets[start] + len(lines[start]) + 1,
            len(str(text or "")), offsets[start])


def steps_section(text: str) -> str | None:
    """The body of the document's Steps section, or None when the
    text is not a sectioned document."""
    span = _section_span(text, "Steps")
    if span is None:
        return None
    return str(text or "")[span[0]:span[1]]


# --- Item C7: the plan-card fields ---------------------------------
# Success criteria ("done means X is true") and Out of scope are
# sections of the plan DOCUMENT, drafted with the rest of the
# skeleton (forge/modes.py). Nothing new is stored: the sidecar
# already carries the document whole, and these extractors read
# the sections back out of it. A document without the sections —
# every pre-C7 draft — yields "" (absent), never an error.

SUCCESS_CRITERIA_HEADING = "Success criteria"
OUT_OF_SCOPE_HEADING = "Out of scope"


def document_section(text: str, name: str) -> str | None:
    """One ## section's body (stripped), or None when the document
    has no such section."""
    span = _section_span(text, name)
    if span is None:
        return None
    return str(text or "")[span[0]:span[1]].strip()


def success_criteria(document: str) -> str:
    """The document's Success criteria body, or "" when absent."""
    return document_section(document, SUCCESS_CRITERIA_HEADING) or ""


def out_of_scope(document: str) -> str:
    """The document's Out of scope body, or "" when absent."""
    return document_section(document, OUT_OF_SCOPE_HEADING) or ""


def _steps_from_section(body: str) -> list[str] | None:
    """Steps out of a Steps section: each numbered/checkbox line
    opens a step; following non-item lines are its detail and are
    joined onto the step's string (single spaces), so the full
    detail — including the Verify line — rides the checklist."""
    steps: list[str] = []
    current: list[str] | None = None
    for line in str(body or "").splitlines():
        m = _NUM_RE.match(line) or _BOX_RE.match(line)
        if m:
            if current is not None:
                steps.append(" ".join(current))
            current = [m.group(m.lastindex).strip()]
            continue
        stripped = line.strip()
        if current is not None and stripped:
            current.append(stripped)
    if current is not None:
        steps.append(" ".join(current))
    return steps if len(steps) >= 2 else None


def extract_steps(text: str) -> list[str] | None:
    """Checklist extraction for a plan draft of either era: a
    sectioned document yields the steps of its Steps section
    (detail joined in); anything else falls back to parse_plan,
    so a bare numbered list — an older draft, a Goldfish
    revision — parses exactly as before."""
    section = steps_section(text)
    if section is not None:
        steps = _steps_from_section(section)
        if steps:
            return steps
    return parse_plan(text)


def replace_steps_section(document: str, steps: list[str]) -> str:
    """Swap a document's Steps section body for the given steps
    (the Goldfish revision path: the reviewer-driven revision
    comes back as a bare list, and the document keeps its other
    sections with the revised steps laid in). A document with no
    Steps section is returned unchanged."""
    span = _section_span(document, "Steps")
    if span is None:
        return document
    body = "\n" + format_steps(steps) + "\n"
    return str(document)[:span[0]] + body + str(document)[span[1]:]


def format_steps(steps: list[str]) -> str:
    """The editor form: one numbered step per line."""
    return "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))


def steps_from_editor(text: str) -> list[str] | None:
    """Re-parse the owner's edited plan. The strict parser runs first;
    if it declines (the owner rewrote the steps as plain lines), each
    non-empty line becomes a step with any leading number/bullet
    stripped — the owner's explicit text is never discarded for a
    formatting technicality. None only when nothing remains."""
    parsed = parse_plan(text)
    if parsed:
        return parsed
    steps = []
    for line in str(text or "").splitlines():
        line = _BULLET_RE.sub("", line).strip()
        if line:
            steps.append(line)
    return steps or None


def find_step_done(text: str, already: set[int], total: int) -> list[int]:
    """Zero-based indices newly announced as "STEP <n> DONE" in a
    generation's text (case-insensitive), ascending, ignoring repeats
    and out-of-range numbers."""
    found = set()
    for m in _STEP_DONE_RE.finditer(str(text or "")):
        n = int(m.group(1))
        if 1 <= n <= total and (n - 1) not in already:
            found.add(n - 1)
    return sorted(found)


def work_instruction(steps: list[str], document: str = "") -> str:
    """The user-role message that turns an approved plan into the
    turn's working instruction. Item #58: when the draft was a
    plan DOCUMENT, the document itself is the instruction — the
    builder works from the full text, with the extracted
    checklist appended to pin the numbering the STEP DONE
    announcements refer to. Without a document the historical
    form is produced byte-identically."""
    lines = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    if document:
        return ("The owner approved this plan document — it is now your "
                "working instruction. Build from the document, not from "
                "the original one-line request. Work the steps in order. "
                "When you finish a step, announce it on its own line "
                "exactly as: STEP <n> DONE (for example: STEP 1 DONE). Do "
                "not announce a step you have not finished.\n\n"
                + str(document).strip()
                + "\n\nThe checklist (announce against these numbers):\n"
                + lines)
    return ("The owner approved this plan — it is now your working instruction. "
            "Work the steps in order. When you finish a step, announce it on its "
            "own line exactly as: STEP <n> DONE (for example: STEP 1 DONE). Do "
            "not announce a step you have not finished.\n\n" + lines)


def plan_progress_text(steps: list[str], done: set[int]) -> str:
    """The focus-chain reminder (audit D39): a compact, deterministic
    restatement of where an approved plan stands, re-injected into the
    conversation every few execution cycles so a long run cannot bury
    the plan — far up the history, or inside a fold summary — and
    drift off it. Pure: same steps + done set, same text. `done`
    holds zero-based indices, matching find_step_done's world; the
    rendered indices are 1-based, matching the plan the model saw."""
    done = set(done or ())
    next_idx = next((i for i in range(len(steps)) if i not in done), None)
    lines = [f"[Plan progress — {len(done)} of {len(steps)} steps done.]"]
    for i, step in enumerate(steps):
        # Item #58: the step's FULL text rides — detail included.
        # The old 80-char clip belonged to the one-line-plan era;
        # a step now carries its substance, and clipping it out of
        # the re-injection would starve exactly the detail the
        # document exists to hold.
        text = str(step)
        mark = "✓" if i in done else ("→" if i == next_idx else "·")
        lines.append(f"{mark} {i + 1}. {text}")
    lines.append('Announce "STEP n DONE" as each step finishes.')
    return "\n".join(lines)


# --- Manus study M3: programmatic recitation ------------------------
# Manus keeps its plan in recent attention by constantly rewriting a
# todo.md — and later found its executor spent ~1/3 of all actions on
# that rewriting, so planning moved off the executor. Forge's version:
# the PROGRAM recites. While an approved plan executes, the worker
# appends this block to the OUTGOING request copy on every cycle —
# built fresh from the live checklist state, at request time only,
# never written into the stored conversation (so it cannot
# accumulate, cannot be folded, and leaves M1's stable prefix at the
# head of the request untouched). Zero model actions are spent on it.
# The D39 reminder above is the older, coarser cousin: it lands IN
# the history every few cycles; this block is every cycle and
# history-free. The header wording differs on purpose so the two can
# never be confused in a transcript or a test.
RECITATION_STEP_CAP = 120     # chars per step line in the block
RECITATION_CRITERIA_CAP = 200  # chars for the Success criteria line


def _recitation_clip(text: str, cap: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) > cap:
        return text[:cap - 1] + "…"
    return text


def plan_recitation_text(steps: list[str], done: set[int],
                         title: str = "", success: str = "") -> str:
    """The compact plan-state block for one outgoing request
    (Manus study M3). Pure: same state in, same text out. `done`
    holds zero-based indices, matching find_step_done's world; the
    rendered indices are 1-based. Marks match the live checklist
    and the D39 reminder: ✓ done, → current (the first undone
    step), · pending. Step text is clipped — unlike the D39
    reminder, this block rides EVERY cycle, so it stays compact;
    the plan's own words are otherwise unaltered. The Success
    criteria line appears only when the plan document has that
    section (Item C7) — absent section, no line, never an empty
    one."""
    steps = [str(s) for s in (steps or [])]
    if not steps:
        return ""
    done = set(done or ())
    next_idx = next((i for i in range(len(steps)) if i not in done),
                    len(steps) - 1)
    head = "[Current plan"
    if str(title or "").strip():
        head += f" — {_recitation_clip(title, 80)}"
    head += f" — step {next_idx + 1} of {len(steps)}]"
    lines = [head]
    for i, step in enumerate(steps):
        mark = "✓" if i in done else ("→" if i == next_idx else "·")
        lines.append(f"{mark} {i + 1}. "
                     f"{_recitation_clip(step, RECITATION_STEP_CAP)}")
    criteria = _recitation_clip(success, RECITATION_CRITERIA_CAP)
    if criteria:
        lines.append(f"Success criteria: {criteria}")
    return "\n".join(lines)


def new_plan(steps: list[str], request: str = "", document: str = "") -> dict:
    return {"version": 1,
            "status": "draft",
            "request": str(request or "")[:200],
            "steps": [{"text": str(s), "state": "pending", "resource": ""}
                      for s in steps],
            "skribe": "",
            # Item #58: the plan DOCUMENT's full text (the gate
            # shows it; the approval instruction is built from it).
            # "" for a bare-list draft and for every pre-#58
            # sidecar — the checklist alone is the plan there.
            "document": str(document or ""),
            # Item #56: the Goldfish review result dict (forge/
            # goldfish.py) or None — no review on record. Advisory
            # only: it rides the state so the gate can show it; it
            # never influences status transitions.
            "goldfish": None,
            # Item #66: the owner's gate comments, oldest first —
            # {"text", "at"} dicts appended by record_comment. The
            # sidecar is the record of what was asked; each comment
            # settled its gate as a revision (the worker rewrote
            # the draft against it and returned to the gate).
            "comments": [],
            "created": _now(),
            "updated": _now()}


def sanitize_plan(data) -> dict | None:
    """Defensive load: anything malformed becomes None (no plan),
    never an exception in the load path."""
    if not isinstance(data, dict):
        return None
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None
    steps = []
    for entry in raw_steps:
        if not isinstance(entry, dict) or not str(entry.get("text", "")).strip():
            return None
        state = entry.get("state")
        steps.append({"text": str(entry["text"]),
                      "state": state if state in STEP_STATES else "pending",
                      "resource": str(entry.get("resource", ""))})
    status = data.get("status")
    # Item #66: the owner's gate comments ride the round trip;
    # a pre-#66 sidecar (or junk entries) loads with only the
    # well-formed comments kept.
    comments = []
    for entry in data.get("comments") or []:
        if isinstance(entry, dict) and str(entry.get("text", "")).strip():
            comments.append({"text": str(entry["text"])[:4000],
                             "at": str(entry.get("at", ""))})
    return {"version": 1,
            "status": status if status in PLAN_STATUSES else "done",
            "request": str(data.get("request", ""))[:200],
            "steps": steps,
            "skribe": str(data.get("skribe", "")),
            # Item #58: absent in pre-#58 sidecars — reads as "".
            "document": str(data.get("document", ""))[:200_000],
            "goldfish": goldfish_mod.sanitize_result(data.get("goldfish")),
            "comments": comments,
            "created": str(data.get("created", "")),
            "updated": str(data.get("updated", ""))}


def record_comment(plan: dict, text: str) -> None:
    """Item #66: append one owner gate comment to the plan's
    record. The sidecar is the history of what was asked — each
    entry settled its gate as a revision. The text is capped so a
    pasted document cannot bloat a record the gate re-saves on
    every step tick."""
    plan.setdefault("comments", []).append(
        {"text": str(text or "").strip()[:4000], "at": _now()})


def owner_revision_instruction(comment: str) -> str:
    """The user-role message handed to the DRIVER (in its own main
    context — the forge/worker.py _goldfish_revision pattern) when
    the owner comments at the gate (Item #66): revise the draft
    once, against the owner's own words. The draft itself already
    rides the conversation, so the instruction carries the comment
    verbatim — the shell never paraphrases the owner. The owner
    still approves whatever comes out; a comment buys one better
    draft, never an execution."""
    return (
        "[SHELL — OWNER COMMENT] The owner read your draft plan "
        "and commented:\n"
        + str(comment or "").strip()
        + "\n\nRevise the plan ONCE, addressing the comment. "
          "Reply with ONLY the revised plan as a numbered list "
          "(1. 2. 3. …) in the same Plan format — ordered steps, "
          "the files each step touches, how each step will be "
          "verified. Do not explain the changes; the revised plan "
          "goes back to the owner's gate either way.")


def deck_plan(plan: dict | None) -> dict | None:
    """The read-only shape that rides the phone deck's snapshot."""
    if not plan:
        return None
    goldfish = plan.get("goldfish")
    document = plan.get("document", "")
    return {"status": plan.get("status", ""),
            "steps": [{"text": s.get("text", ""), "state": s.get("state", "pending")}
                      for s in plan.get("steps", [])],
            "skribe": plan.get("skribe", ""),
            # Item C7: the plan-card fields, read out of the plan
            # document the sidecar already carries — "" for a
            # pre-C7 draft, and the deck card renders exactly as
            # before when they are empty.
            "success_criteria": success_criteria(document),
            "out_of_scope": out_of_scope(document),
            # Item #56: the Goldfish review rides the same plumbing —
            # a one-line summary plus the full gate rendering, so the
            # phone snapshot carries the findings even though the
            # deck page itself is unchanged.
            "goldfish": goldfish_mod.summary_line(goldfish),
            "goldfish_text": goldfish_mod.format_result(goldfish)}


def _plan_path(app_dir, conversation_id) -> Path:
    return Path(app_dir) / "plans" / f"{int(conversation_id)}.json"


def save_plan(app_dir, conversation_id, plan: dict) -> None:
    if conversation_id is None or not plan:
        return
    path = _plan_path(app_dir, conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    plan = dict(plan)
    plan["updated"] = _now()
    # Sweep R163: pid-unique temp — two processes autosaving plans
    # for the same conversation used to share one fixed .tmp name
    # and could replace each other's half-written file.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(plan, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_plan(app_dir, conversation_id) -> dict | None:
    if conversation_id is None:
        return None
    try:
        data = json.loads(_plan_path(app_dir, conversation_id).read_text(encoding="utf-8"))
    except Exception:
        return None
    return sanitize_plan(data)
