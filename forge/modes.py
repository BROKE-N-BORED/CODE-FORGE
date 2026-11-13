"""Mode workflow machinery — Item #58 (pure stdlib, no Qt, like
planning.py and gofish.py).

The fault this module answers (verified 2026-11-08): every mode's
entire instruction was one MODE_GUIDANCE paragraph — no homework
phase, no required output skeleton, no clarification step in ANY
mode — so a Plan/Design/Architect draft was written blind, in
whatever shape the model fancied, with the specifics silently
invented. The rework, whose text halves live here:

  HOMEWORK — every non-Code mode's turn opens with a read pass
  (the worker offers read tools only) so drafting is built from
  what is actually in the project. The shell records what was
  read and whether the project is EMPTY (homework_note); on an
  empty project the draft must say so, never invent contents.

  CLARIFY — before drafting, the design-changing specifics are
  pinned: the model may answer with ONLY the questions (a reply
  starting with QUESTIONS_MARKER) and the turn ends there; the
  owner's next message is the answer round. Questions asked in an
  earlier turn are found again in the history (asked_questions);
  whatever the owner did not answer becomes an explicit
  ASSUMPTION in the document (assumptions_fallback is the shell's
  safety net when the Assumptions section comes back empty).

  SKELETONS — each mode drafts against a required section list
  (SKELETONS). Plan's document is assembled BY THE SHELL section
  by section, so its skeleton is guaranteed by construction; the
  other modes carry their skeleton in their guidance.

  THE DOCUMENT IS THE PLAN — the full text lives in the plan .md
  (forge/planning.py extracts the checklist from its Steps
  section; the gate machinery consumes the checklist exactly as
  before).
"""
from __future__ import annotations
import re

# The modes whose turns open with the homework read pass. Code is
# the exception: its loop already offers reads from the first
# cycle, and its discipline (read before the first edit, state
# what was read) rides in its guidance instead. Research joins
# the pass (its phase runs it inside _research_phase, the Plan
# pattern); Write is the second exception, for Code's reason —
# its loop offers reads from the first cycle too, and its
# read-target-first rule rides in its guidance.
HOMEWORK_MODES = ("plan", "design", "architect", "review", "ask",
                  "research")

# A drafting reply that is ONLY questions starts with this line.
QUESTIONS_MARKER = "QUESTIONS BEFORE DRAFTING:"

# The word a homework pass ends with when the model has read
# enough and is ready to draft (Plan's flow keys on it: a plain
# prose homework ending that does NOT start with READY is the
# model's plain answer, and the turn settles on it — the escape
# hatch Plan mode has always promised).
READY_WORD = "READY"

# A clarify reply that means "nothing is open — draft now".
PROCEED_WORD = "PROCEED"

# Required output sections per mode. Plan's are the document
# skeleton the shell assembles; Design's and Architect's are the
# skeleton their artifacts are drafted against; Review's trio is
# its report shape (CR3: the Walkthrough orientation first, then
# the findings, then the coverage line); Ask's is its answer
# shape (the answer, then its sources); Code's is the closing
# account its reply states.
SKELETONS = {
    # Item C7: a plan draft also carries Success criteria ("done
    # means X is true" — the target the work is judged against)
    # and Out of scope (what the plan deliberately does NOT do).
    # They draft LAST, with the whole document in view. Documents
    # drafted before C7 simply lack the sections — every consumer
    # treats a missing section as absent, never an error.
    "plan": ["Goal", "Context found", "Contents", "Steps",
             "Risks", "Assumptions", "Success criteria",
             "Out of scope"],
    "design": ["What it is", "How it plays/works", "Look & feel",
               "Numbers/tuning", "Assets", "Hand-off notes"],
    "architect": ["Components", "Data", "Interfaces", "Decisions",
                  "Open questions"],
    "review": ["Walkthrough", "Findings", "Coverage"],
    "ask": ["Answer", "Sources"],
    "code": ["Context read", "Changes", "Verification"],
    # Write mode (Write/Research spec, 2026-11-09): the document
    # itself is free-form — its sections are the owner's, decided
    # with the clarify round — so the skeleton shapes the mode's
    # ACCOUNT of the work instead: what document was written and
    # where, then the assumptions it was written under. The full
    # document never rides the chat (it lives in its file).
    "write": ["Document", "Assumptions"],
    # Research mode: the REPORT's skeleton. The report document
    # additionally carries "Sources cited" (the model's own list)
    # and "Sources consulted" (the shell's, appended and kept
    # honest from the pages the run actually opened) after these.
    "research": ["Question", "Findings", "Evidence",
                 "Open questions"],
}

# Research mode's PLAN document sections (the gate document the
# owner approves before anything browses — distinct from the
# report skeleton above: this is the plan OF the research, the
# report is its product). The shell drafts the document section
# by section against this list, exactly as Plan's is drafted.
RESEARCH_PLAN_SECTIONS = ["Question", "Sub-questions",
                          "Sources to search", "Depth",
                          "Assumptions"]

# The few questions per mode whose answers change the design —
# the clarification step asks these (or the subset still open).
# Ask has none: its job is answering from the files, and an
# ambiguity it cannot resolve from the code is named as an
# assumption in the answer, per its guidance.
CLARIFY_QUESTIONS = {
    "plan": ["Who or what is this for?",
             "What device or surface will it run on or be used from?",
             "What does the user actually do with it, step by step?",
             "What does 'done' look like — how will we know it works?"],
    "design": ["Who or what is this for?",
               "What device or surface will it be used on?",
               "What does the user actually do with it, step by step?",
               "Are there existing looks, brands, or references it "
               "must match?",
               "What does 'done' look like — how will we know it works?"],
    "architect": ["What must this integrate with or replace?",
                  "What data does it own, and where does that data live?",
                  "What scale and hardware must it run on?",
                  "What does 'done' look like — how will we know "
                  "it works?"],
    "review": ["What should the review focus on — correctness, "
               "safety, performance, or all three?",
               "How deep should it go — only the named files, or "
               "everything they touch?"],
    "ask": [],
    "code": ["What does 'done' look like — how will we know it works?",
             "Is anything off-limits to change?"],
    "write": ["Who is this document for?",
              "What kind of document is it — a letter, notes, a "
              "spec, a README, something else?",
              "What does 'done' look like — how will we know the "
              "document is finished?"],
    "research": ["What exactly should the research answer?",
                 "How deep should it go — Standard (a focused "
                 "pass) or Deep (a wide sweep)?",
                 "Where should it look — the web, this project, "
                 "files the owner supplies, or a mix?"],
}


def skeleton_text(mode: str) -> str:
    """The mode's skeleton as a prompt block: one '## Heading'
    line per required section, in order."""
    return "\n".join(f"## {s}" for s in SKELETONS.get(mode, []))


def is_questions_response(text: str) -> bool:
    """True when a reply is a clarification round: its first
    non-empty line starts with the questions marker."""
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line.upper().startswith(QUESTIONS_MARKER)
    return False


def extract_questions(text: str) -> list[str]:
    """The questions out of a marker reply: the non-empty lines
    after the marker line, numbering/bullets stripped. Capped —
    a clarification round is a few questions, not an interview."""
    out = []
    seen_marker = False
    for line in str(text or "").splitlines():
        line = line.strip()
        if not seen_marker:
            if line.upper().startswith(QUESTIONS_MARKER):
                seen_marker = True
                rest = line[len(QUESTIONS_MARKER):].strip()
                if rest:
                    out.append(rest)
            continue
        if not line:
            continue
        line = re.sub(r"^(?:\d+[.)]|[-*•])\s+", "", line).strip()
        if line:
            out.append(line)
        if len(out) >= 8:
            break
    return out


def asked_questions(messages: list[dict]) -> list[str]:
    """The questions the assistant most recently asked in this
    conversation (a marker reply), or [] when it never has. The
    drafting turn reads them back so the round is not repeated
    and the unanswered ones land in Assumptions."""
    found: list[str] = []
    for m in messages or []:
        if m.get("role") == "assistant" and \
                is_questions_response(m.get("content", "")):
            found = extract_questions(m.get("content", ""))
    return found


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _marker_reply(text: str, word: str) -> bool:
    """True when the reply's FIRST line is the bare marker word,
    alone or followed by a punctuated note ('READY — read the
    demo.'). Prose that merely starts with the word ('Ready or
    not, here I come') is NOT a marker."""
    first = _first_line(text)
    if not first:
        return False
    head, _, rest = first.partition(" ")
    if head.strip("—–-:.,").upper() != word:
        return False
    return not rest or rest.lstrip()[:1] in "—–-:.,"


def is_ready(text: str) -> bool:
    """True when a homework ending declares the read pass done:
    its first line is the READY marker (see _marker_reply)."""
    return _marker_reply(text, READY_WORD)


def is_proceed(text: str) -> bool:
    """True when a clarify reply says 'draft now': its first line
    is the PROCEED marker (see _marker_reply)."""
    return _marker_reply(text, PROCEED_WORD)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def document_sections(text: str) -> list[tuple[str, str]]:
    """A Markdown document split at its ## headings: ordered
    (heading, body) pairs. Text before the first heading rides
    under the empty heading."""
    sections: list[tuple[str, str]] = []
    heading, body_lines = "", []
    for line in str(text or "").splitlines():
        m = _HEADING_RE.match(line)
        if m and len(m.group(1)) <= 2:
            sections.append((heading, "\n".join(body_lines)))
            heading, body_lines = m.group(2).strip(), []
        else:
            body_lines.append(line)
    sections.append((heading, "\n".join(body_lines)))
    return sections


def section_body(text: str, name: str) -> str | None:
    """One section's body by heading name (case-insensitive,
    a trailing colon tolerated), or None when the document has
    no such section."""
    for heading, body in document_sections(text):
        if heading.rstrip(":").lower() == name.rstrip(":").lower():
            return body
    return None


# ---------- Review findings (CR2 — severity + type + Source) ----------
#
# One documented shape for a Review finding (the review study,
# Tier 1 #2): every finding in a review's ## Findings section is
# one line —
#
#   - [SEVERITY · TYPE · Source: SOURCE] FILE:PLACE — PROBLEM — Fix: FIX
#
# SEVERITY is Critical | Major | Minor | Trivial; TYPE is Bug |
# Security | Performance | Refactor | Nitpick; SOURCE names what
# actually triggered the finding (a guideline, an owner learning,
# a mechanical check, a rubric item, or "Reviewer judgment").
# Reviews written before this format used BLOCKER / SHOULD-FIX /
# NIT; those lines still parse through LEGACY_SEVERITY_MAP below,
# with type/source unspecified. parse_finding is the ONE parser —
# every consumer of finding lines (the Review room's list, the
# Fix/Teach flows) goes through it, never a private copy.

FINDING_SEVERITIES = ("Critical", "Major", "Minor", "Trivial")
FINDING_TYPES = ("Bug", "Security", "Performance", "Refactor",
                 "Nitpick")

#: Pre-CR2 Review severity words -> the CR2 scale.
LEGACY_SEVERITY_MAP = {"BLOCKER": "Critical",
                       "SHOULD-FIX": "Major",
                       "NIT": "Trivial"}

_FINDING_BRACKET_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)$")
_FINDING_LEGACY_RE = re.compile(
    r"^(BLOCKER|SHOULD-FIX|NIT)\b\s*[:\-—]?\s*(.*)$", re.IGNORECASE)
_FINDING_BULLET_RE = re.compile(r"^(?:[-*+•]\s+|\d+[.)]\s+)")
_FINDING_SEP = "·"


def _finding_canonical(value: str, options: tuple) -> str | None:
    """The canonical-case member of `options` matching `value`
    (case-insensitive), or None."""
    folded = (value or "").strip().casefold()
    for option in options:
        if option.casefold() == folded:
            return option
    return None


def parse_finding(line: str) -> dict | None:
    """Parse ONE finding line (as listed from a review's ##
    Findings section) into {severity, type, source, text, legacy}.

    severity/type/source are None when the line does not carry
    them (legacy lines, plain lines, malformed bracket headers) —
    the caller reports them as unspecified, never guesses. text
    is the cleaned full line (bullet/number prefix stripped) —
    the exact string the Fix/Teach flows act on. legacy is True
    when no bracket header was parsed (pre-format lines, incl.
    the old severity words); a bracket header, even a malformed
    one, marks an attempted new-format line (legacy False).
    Returns None for a blank line — the one non-finding input,
    matching the Review room's long-standing rule that every
    non-empty Findings line is listed. Never raises."""
    text = _FINDING_BULLET_RE.sub("", str(line or "").strip()).strip()
    if not text:
        return None
    out = {"severity": None, "type": None, "source": None,
           "text": text, "legacy": True}
    bracket = _FINDING_BRACKET_RE.match(text)
    if bracket:
        out["legacy"] = False
        parts = [p.strip() for p in bracket.group(1).split(_FINDING_SEP)]
        for part in parts:
            if not part:
                continue
            severity = _finding_canonical(part, FINDING_SEVERITIES)
            if severity is not None and out["severity"] is None:
                out["severity"] = severity
                continue
            ftype = _finding_canonical(part, FINDING_TYPES)
            if ftype is not None and out["type"] is None:
                out["type"] = ftype
                continue
            if part.lower().startswith("source:") \
                    and out["source"] is None:
                source = part.split(":", 1)[1].strip()
                if source:
                    out["source"] = source
                continue
        return out
    legacy = _FINDING_LEGACY_RE.match(text)
    if legacy:
        out["severity"] = LEGACY_SEVERITY_MAP[legacy.group(1).upper()]
    return out


def finding_display(parsed: dict) -> str:
    """The one-line list display for a parse_finding result: the
    parsed severity + type lead, then the finding's own words (the
    bracket header / legacy severity word is not repeated). Lines
    with nothing parsed display as themselves."""
    text = (parsed or {}).get("text", "")
    if not parsed:
        return text
    head = " · ".join(x for x in (parsed.get("severity"),
                                  parsed.get("type")) if x)
    if text.startswith("[") and "]" in text:
        rest = text.split("]", 1)[1].strip()
        if head and rest:
            return f"{head} — {rest}"
        return rest or text
    if parsed.get("legacy") and parsed.get("severity"):
        legacy = _FINDING_LEGACY_RE.match(text)
        if legacy and legacy.group(2).strip():
            return f"{parsed['severity']} — {legacy.group(2).strip()}"
    return text


# ---------- "Prompt for the Driver" (CR7 — one finding, one fix) -----
#
# A review finding says what is wrong; the driver (Code mode) still
# has to work out what to DO. CR7 closes that gap in the report
# itself: under each finding line, one indented line starting
# "Driver:" — a self-contained instruction for Code mode to fix
# exactly this finding (file, exact change, how to verify). The
# Review prompt instructs the shape (forge/permissions.py); the
# helpers below are the program half. A Driver line is a
# CONTINUATION, never a finding of its own: parse_findings_body
# attaches it to the finding above it, and the Review room's Fix
# flow sends it (with the finding line as context) instead of the
# bare finding. Reports written before CR7 have no Driver lines
# and fix exactly as before — the driver is None, never guessed.

_DRIVER_RE = re.compile(r"^Driver:\s*(.*)$", re.IGNORECASE)


def parse_driver_line(line: str) -> str | None:
    """The driver prompt on a "Driver:" continuation line, or None.
    Bullet/number prefixes are stripped first, the match is
    case-insensitive, and an empty prompt ("Driver:" alone) reads
    as no driver line at all. Never raises."""
    text = _FINDING_BULLET_RE.sub("", str(line or "").strip()).strip()
    if not text:
        return None
    match = _DRIVER_RE.match(text)
    if not match:
        return None
    prompt = match.group(1).strip()
    return prompt or None


def parse_findings_body(body: str) -> list[dict]:
    """Parse a ## Findings section into parse_finding dicts, each
    carrying a "driver" key (the Driver line under it, or None).
    A Driver line attaches to the finding directly above it — a
    second Driver line under the same finding replaces the first,
    so a re-filed report cannot stack stale prompts. An orphan
    Driver line (no finding above it) is ignored, never listed.
    Blank lines are skipped. parse_finding's own shape is
    untouched; this only adds the "driver" key. Never raises."""
    out: list[dict] = []
    try:
        lines = (body or "").splitlines()
    except Exception:
        return out
    for raw in lines:
        driver = parse_driver_line(raw)
        if driver is not None:
            if out:
                out[-1]["driver"] = driver
            continue
        try:
            parsed = parse_finding(raw)
        except Exception:
            parsed = None
        if parsed is None:
            continue
        parsed["driver"] = None
        out.append(parsed)
    return out


# ---------- Fix requests (CR9 — one finding, one recorded fix) -----
#
# The Review room's "Fix with Driver" action sends one finding to
# Code mode (CR7's driver prompt when the report has one). CR9
# records the request in the review file itself, under a ##
# Fix requests section at the end — the review carries its own
# fix history, so the owner can see which findings already have
# a fix on the way and which are still open. The section is a
# separate section, so the Findings parser never sees it, and
# the entries are plain text the owner can read or edit by hand.
# A fix request is a RECORD of asking, not proof the fix landed
# — the Code turn runs on the bench, and its result lands in
# the walkthroughs, not here.

_FIX_REQUEST_RE = re.compile(r"^-\s+(\S+(?:\s+\S+)?)\s+—\s+(.*)$")
_FIX_SENT_RE = re.compile(r"^\s*Sent:\s*(.*)$")


def _flatten_one_line(text: str) -> str:
    """One line of text, newlines folded to spaces — a fix
    request entry is one finding line plus one Sent line, never
    a pasted multi-line turn."""
    return " ".join((text or "").split())


def record_fix_request(review_text: str, when: str,
                       finding_text: str, sent: str) -> str:
    """The review text with one fix-request entry recorded under
    ## Fix requests at the end (the section is created when the
    review has none). The entry is "- <when> — <finding line>"
    plus an indented "Sent: <the prompt that went to Code mode>".
    The original text is otherwise untouched — a review with no
    Fix requests section keeps every byte outside the appended
    section. Never raises."""
    try:
        text = review_text or ""
        when = (when or "").strip() or "unknown time"
        entry = (f"- {when} — {_flatten_one_line(finding_text)}\n"
                 f"  Sent: {_flatten_one_line(sent)}\n")
        body = section_body(text, "Fix requests")
        if body is None:
            if text and not text.endswith("\n"):
                text += "\n"
            return text + f"\n## Fix requests\n\n{entry}"
        # The section exists: append the entry after its last
        # line. section_body cannot say where the section ends,
        # so the append point is found by re-scanning the lines.
        lines = text.splitlines(keepends=True)
        in_section = False
        insert_at = len(lines)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#"):
                if in_section:
                    insert_at = i
                    break
                if stripped == "## Fix requests":
                    in_section = True
                continue
        if insert_at == len(lines) and lines \
                and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.insert(insert_at, entry)
        return "".join(lines)
    except Exception:
        return review_text or ""


def fix_requests(review_text: str) -> list[dict]:
    """The recorded fix requests from a review's ## Fix requests
    section, in file order: {when, finding, sent}. A Sent line
    belongs to the entry above it; an entry without one records
    sent as "". No section reads as []. Never raises."""
    out: list[dict] = []
    try:
        body = section_body(review_text or "", "Fix requests")
    except Exception:
        return out
    if not body:
        return out
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _FIX_REQUEST_RE.match(line)
        if match:
            out.append({"when": match.group(1).strip(),
                        "finding": match.group(2).strip(),
                        "sent": ""})
            continue
        sent = _FIX_SENT_RE.match(line)
        if sent and out:
            out[-1]["sent"] = sent.group(1).strip()
    return out


def fix_requested_for(review_text: str, finding_text: str):
    """The latest "when" a fix was requested for this exact
    finding line, or None when none was recorded. Never raises."""
    try:
        wanted = _flatten_one_line(finding_text)
        latest = None
        for request in fix_requests(review_text):
            if request.get("finding") == wanted:
                latest = request.get("when")
        return latest
    except Exception:
        return None


# ---------- Autofix as a bounded loop (Tier 2 #4) ----------
#
# Continuous fix-until-clean, Forge-scale: a review's
# findings are fixed a few at a time, one Code turn per round, with
# a re-review after each round — and the loop is BOUNDED. Three
# rounds per review chain, five findings per round, and the owner
# clicks every round: nothing runs on its own, and every write the
# fix turn makes is approved by the owner at the existing tool
# gate (worker.py approval_needed with a diff). Never an
# autonomous commit mode.
#
# State lives in the review file's ## Autofix rounds section, the
# way fix requests live under ## Fix requests. The chain bound
# rides the re-review filename (autofix-<stem>-r<N>.md) — the
# model files its own reports, so the prompt instructs that name
# and the room reads the round back from it. A model that names
# its report differently restarts the count for that file; the
# hard backstops are the owner's click per round and the per-file
# cap, so a disobedient filename can never make the loop run away.

AUTOFIX_MAX_ROUNDS = 3
AUTOFIX_MAX_FINDINGS_PER_ROUND = 5

_AUTOFIX_ROUND_RE = re.compile(
    r"^-\s+Round\s+(\d+)\s+—\s+(\S+(?:\s+\S+)?)\s+—\s+"
    r"(\d+)\s+findings?\s+sent$")
_AUTOFIX_FINDING_RE = re.compile(r"^\s*Finding:\s+(.*)$")
_AUTOFIX_REREVIEW_RE = re.compile(
    r"^-\s+Round\s+(\d+)\s+re-review:\s+(.*)$")
_AUTOFIX_NAME_RE = re.compile(r"^autofix-(.+)-r(\d+)\.md$")


def autofix_round_from_name(filename: str) -> int:
    """The chain round a re-review filename carries — 1 for
    autofix-fix-review-r1.md — or 0 when the name carries none
    (an original review, or a model that named its report
    differently). Never raises."""
    try:
        match = _AUTOFIX_NAME_RE.match(
            (filename or "").strip())
        return int(match.group(2)) if match else 0
    except Exception:
        return 0


def autofix_chain_stem(filename: str) -> str:
    """The chain's base stem: the review filename without a
    leading "autofix-" and a trailing "-r<N>", and without the
    .md — so the next re-review in the chain is named
    autofix-<stem>-r<N>.md instead of stacking prefixes.
    "fix-review.md" reads as "fix-review". Never raises."""
    try:
        stem = (filename or "").strip()
        if stem.endswith(".md"):
            stem = stem[:-3]
        match = _AUTOFIX_NAME_RE.match((filename or "").strip())
        if match:
            return match.group(1)
        if stem.startswith("autofix-"):
            stem = stem[len("autofix-"):]
        return stem or "review"
    except Exception:
        return "review"


def autofix_next_findings(parsed, sent_lines, fix_requested_lines,
                          cap=AUTOFIX_MAX_FINDINGS_PER_ROUND):
    """The next findings for an autofix round, in severity order
    (Critical first, file order within a severity, unparsed
    severity last), capped at `cap`. Findings already sent in an
    earlier round, or with an individual fix request (CR9), are
    skipped — the round covers only what is still open. A
    finding naming no file is skipped too: a fix turn has
    nothing to open for it (a Findings section that just says
    "Nothing to report." is one such line, and must never become
    a round). `parsed` is parse_findings_body output (dicts with
    text/severity/driver); the returned dicts are the same
    dicts, in round order. Never raises."""
    try:
        limit = max(1, int(cap))
    except Exception:
        limit = AUTOFIX_MAX_FINDINGS_PER_ROUND
    out = []
    try:
        sent = {_flatten_one_line(s) for s in (sent_lines or [])}
        requested = {_flatten_one_line(s)
                     for s in (fix_requested_lines or [])}
        order = {name: i for i, name in enumerate(
            FINDING_SEVERITIES)}
        pool = []
        for found in parsed or []:
            text = _flatten_one_line((found or {}).get("text", ""))
            if not text or text in sent or text in requested:
                continue
            if not finding_file(text):
                continue
            severity = (found or {}).get("severity")
            pool.append((order.get(severity, len(order)), found))
        # Python's sort is stable: file order holds within a
        # severity.
        pool.sort(key=lambda pair: pair[0])
        out = [found for _rank, found in pool[:limit]]
    except Exception:
        return out
    return out


def _autofix_append(text: str, entry: str) -> str:
    """`text` with `entry` appended under ## Autofix rounds at
    the end (the section is created when the review has none),
    before any following section. The original text is otherwise
    untouched. Never raises."""
    try:
        body = section_body(text, "Autofix rounds")
        if body is None:
            if text and not text.endswith("\n"):
                text += "\n"
            return text + f"\n## Autofix rounds\n\n{entry}"
        lines = text.splitlines(keepends=True)
        in_section = False
        insert_at = len(lines)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#"):
                if in_section:
                    insert_at = i
                    break
                if stripped == "## Autofix rounds":
                    in_section = True
                continue
        if insert_at == len(lines) and lines \
                and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.insert(insert_at, entry)
        return "".join(lines)
    except Exception:
        return text or ""


def record_autofix_round(review_text: str, when: str, round_n: int,
                         findings, sent: str) -> str:
    """The review text with one autofix round recorded under
    ## Autofix rounds: "- Round N — <when> — K findings sent",
    one indented "Finding: <line>" per finding sent, and an
    indented "Sent: <the prompt that went to Code mode>". Never
    raises."""
    try:
        text = review_text or ""
        when = (when or "").strip() or "unknown time"
        try:
            number = max(1, int(round_n))
        except Exception:
            number = 1
        lines = list(findings or [])
        entry = (f"- Round {number} — {when} — {len(lines)} "
                 f"finding{'s' if len(lines) != 1 else ''} sent\n")
        for line in lines:
            entry += f"  Finding: {_flatten_one_line(line)}\n"
        entry += f"  Sent: {_flatten_one_line(sent)}\n"
        return _autofix_append(text, entry)
    except Exception:
        return review_text or ""


def record_autofix_rereview(review_text: str, round_n: int,
                            filename: str) -> str:
    """The review text with one re-review recorded under
    ## Autofix rounds: "- Round N re-review: <filename>".
    Never raises."""
    try:
        text = review_text or ""
        try:
            number = max(1, int(round_n))
        except Exception:
            number = 1
        entry = (f"- Round {number} re-review: "
                 f"{(filename or '').strip() or '(unnamed)'}\n")
        return _autofix_append(text, entry)
    except Exception:
        return review_text or ""


def autofix_rounds(review_text: str) -> list[dict]:
    """The recorded autofix rounds from a review's
    ## Autofix rounds section, in file order: {round, when,
    findings, sent, rereview}. A Finding or Sent line belongs to
    the round above it; a re-review line fills that round's
    rereview. No section reads as []. Never raises."""
    out: list[dict] = []
    try:
        body = section_body(review_text or "", "Autofix rounds")
    except Exception:
        return out
    if not body:
        return out
    by_round: dict[int, dict] = {}
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _AUTOFIX_ROUND_RE.match(line)
        if match:
            number = int(match.group(1))
            entry = {"round": number,
                     "when": match.group(2).strip(),
                     "findings": [], "sent": "",
                     "rereview": ""}
            out.append(entry)
            by_round[number] = entry
            continue
        rereview = _AUTOFIX_REREVIEW_RE.match(line)
        if rereview:
            entry = by_round.get(int(rereview.group(1)))
            if entry is not None:
                entry["rereview"] = rereview.group(2).strip()
            continue
        finding = _AUTOFIX_FINDING_RE.match(line)
        if finding and out:
            out[-1]["findings"].append(finding.group(1).strip())
            continue
        sent = _FIX_SENT_RE.match(line)
        if sent and out:
            out[-1]["sent"] = sent.group(1).strip()
    return out


def autofix_round_for(review_text: str, finding_text: str):
    """The round number that sent this exact finding line, or
    None when no round sent it. Never raises."""
    try:
        wanted = _flatten_one_line(finding_text)
        for entry in autofix_rounds(review_text):
            if wanted in [_flatten_one_line(f)
                          for f in entry.get("findings", [])]:
                return entry.get("round")
        return None
    except Exception:
        return None


def autofix_next_round(review_text: str, filename: str) -> int:
    """The next autofix round for this review: one past the
    furthest round the chain has reached — the chain round its
    filename carries, or the highest round recorded in this
    file, whichever is further. Recorded round numbers are
    chain-absolute (round 3 recorded in an r2 file already
    includes the base), so the two are compared, never added:
    adding them double-counts the base. An original review with
    no rounds reads as 1; autofix-x-r1.md with no rounds reads
    as 2. Never raises."""
    try:
        base = autofix_round_from_name(filename)
        done = [entry.get("round", 0)
                for entry in autofix_rounds(review_text)]
        furthest = max([base] + done)
        return furthest + 1
    except Exception:
        return 1


# ---------- Finishing Touches (Tier 2 #3 — docstrings + unit tests) ----------
#
# The finishing moves, offered as checkboxes at the end of a
# Review artifact: docstrings and unit tests for the files the review
# names. Both run as Code turns (the Tester seat has no write tools —
# it verifies the new tests on its normal run, it cannot write them).
# The file a finding names is read from the finding line itself, and
# every run is recorded in the review under ## Finishing touches, the
# way fix requests are recorded under ## Fix requests.

_FINISHING_TOUCH_RE = re.compile(
    r"^-\s+(\S+(?:\s+\S+)?)\s+—\s+(Docstrings|Unit tests) for\s+(.*)$")


def _finding_body(line: str) -> str:
    """A finding line with its bullet, bracket header and legacy
    severity word stripped — the "FILE:PLACE — PROBLEM — Fix:
    FIX" body, or the plain line as written. The strip is shared
    by finding_file, finding_line and finding_problem_fix so the
    three never disagree about where the body starts. Never
    raises."""
    try:
        text = _FINDING_BULLET_RE.sub(
            "", str(line or "").strip()).strip()
        if not text:
            return ""
        bracket = _FINDING_BRACKET_RE.match(text)
        if bracket:
            return bracket.group(2).strip()
        # Legacy lines lead with the old severity word
        # ("BLOCKER: store.py:3 — ...") — the same strip
        # finding_display applies, so the word never reads as
        # part of the file.
        legacy = _FINDING_LEGACY_RE.match(text)
        if legacy:
            return legacy.group(2).strip()
        return text
    except Exception:
        return ""


def finding_file(line: str) -> str:
    """The file a finding line names, or "" when it names none.

    The CR2 format puts FILE:PLACE right after the bracket header,
    before the " — " that introduces the problem; legacy lines carry
    the same head without the bracket. A trailing :line or :line:col
    reference is stripped. A line without the " — " separator names
    no file — the file is never guessed from the prose. Never
    raises."""
    try:
        text = _finding_body(line)
        if " — " not in text:
            return ""
        head = text.split(" — ", 1)[0].strip()
        head = re.sub(r"(?::\d+)+$", "", head).strip()
        return head
    except Exception:
        return ""


def finding_line(line: str):
    """The line number a finding line's FILE:PLACE names, or None
    when it names none. "store.py:412" reads as 412; a ":line:col"
    tail reads as the line (412, not the column); a file with no
    place reads as None. Never raises."""
    try:
        text = _finding_body(line)
        if " — " not in text:
            return None
        head = text.split(" — ", 1)[0].strip()
        match = re.search(r":(\d+)(?::\d+)?$", head)
        return int(match.group(1)) if match else None
    except Exception:
        return None


def finding_problem_fix(line: str) -> tuple[str, str | None]:
    """A finding line's (problem, fix): the problem is the text
    between the file head and the "Fix:" tail, the fix is the
    text after "Fix:", or None when the line carries no fix. A
    line with no " — " separator is all problem — a plain line
    like "Nothing to report." states its own problem and has no
    fix to give. Never raises."""
    try:
        text = _finding_body(line)
        if not text:
            return "", None
        parts = text.split(" — ")
        if len(parts) < 2:
            return text, None
        if parts[-1].startswith("Fix:"):
            fix = parts[-1][len("Fix:"):].strip() or None
            problem = " — ".join(parts[1:-1]).strip()
            return problem, fix
        return " — ".join(parts[1:]).strip(), None
    except Exception:
        return "", None


def review_files(lines, cap=10) -> list[str]:
    """The distinct files a review's finding lines name, in first
    appearance order, capped at `cap` (10 by default — a finishing
    touch covers the review's files, not the whole tree). Lines
    naming no file are skipped. Never raises."""
    out: list[str] = []
    try:
        limit = max(1, int(cap))
    except Exception:
        limit = 10
    try:
        for line in lines or []:
            found = finding_file(line)
            if found and found not in out:
                out.append(found)
                if len(out) >= limit:
                    break
    except Exception:
        return out
    return out


def record_finishing_touch(review_text: str, when: str, kind: str,
                           files, sent: str) -> str:
    """The review text with one finishing-touch entry recorded
    under ## Finishing touches at the end (the section is created
    when the review has none). The entry is "- <when> — <Kind> for
    <file, file>" plus an indented "Sent: <the prompt that went to
    Code mode>". The original text is otherwise untouched. Never
    raises."""
    try:
        text = review_text or ""
        when = (when or "").strip() or "unknown time"
        kind = (kind or "").strip() or "Finishing touch"
        file_list = ", ".join(str(f) for f in (files or [])) or "(no files)"
        entry = (f"- {when} — {kind} for "
                 f"{_flatten_one_line(file_list)}\n"
                 f"  Sent: {_flatten_one_line(sent)}\n")
        body = section_body(text, "Finishing touches")
        if body is None:
            if text and not text.endswith("\n"):
                text += "\n"
            return text + f"\n## Finishing touches\n\n{entry}"
        # The section exists: append the entry after its last
        # line. section_body cannot say where the section ends,
        # so the append point is found by re-scanning the lines.
        lines = text.splitlines(keepends=True)
        in_section = False
        insert_at = len(lines)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#"):
                if in_section:
                    insert_at = i
                    break
                if stripped == "## Finishing touches":
                    in_section = True
                continue
        if insert_at == len(lines) and lines \
                and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.insert(insert_at, entry)
        return "".join(lines)
    except Exception:
        return review_text or ""


def finishing_touches(review_text: str) -> list[dict]:
    """The recorded finishing touches from a review's
    ## Finishing touches section, in file order: {when, kind,
    files, sent}. A Sent line belongs to the entry above it; an
    entry without one records sent as "". No section reads as [].
    Never raises."""
    out: list[dict] = []
    try:
        body = section_body(review_text or "", "Finishing touches")
    except Exception:
        return out
    if not body:
        return out
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _FINISHING_TOUCH_RE.match(line)
        if match:
            files = [f.strip() for f in match.group(3).split(",")
                     if f.strip()]
            out.append({"when": match.group(1).strip(),
                        "kind": match.group(2).strip(),
                        "files": files, "sent": ""})
            continue
        sent = _FIX_SENT_RE.match(line)
        if sent and out:
            out[-1]["sent"] = sent.group(1).strip()
    return out


def finishing_touched_for(review_text: str, kind: str):
    """The latest "when" a finishing touch of this kind was
    requested, or None when none was recorded. Never raises."""
    try:
        wanted = (kind or "").strip()
        latest = None
        for touch in finishing_touches(review_text):
            if touch.get("kind") == wanted:
                latest = touch.get("when")
        return latest
    except Exception:
        return None


# ---------- Review walkthrough (CR3 — orientation first) ----------
#
# A Review report opens with a ## Walkthrough section (the review
# study, Tier 1 #3): what changed, the changes in dependency order
# grouped by layer, an Effort line and a Risk line — the 30-second
# orientation before the findings. The shape is instructed by the
# Review prompt (forge/permissions.py) and declared by the review
# skeleton above; reports written before CR3 have no Walkthrough
# section, and every consumer treats a missing section as absent
# (section_body returns None), never an error. The helpers below
# are the counts half of CR3: the Review room's Findings heading
# summarises its parsed findings by severity, legacy lines counted
# under their mapped severities (parse_finding already mapped
# them, so no second mapping lives here).

def finding_severity_counts(parsed_findings) -> dict:
    """{severity: count} over parse_finding results, in the
    canonical FINDING_SEVERITIES order, zero counts included.
    Findings with no parsed severity are not counted."""
    counts = {severity: 0 for severity in FINDING_SEVERITIES}
    for parsed in parsed_findings or []:
        severity = (parsed or {}).get("severity")
        if severity in counts:
            counts[severity] += 1
    return counts


def finding_counts_text(parsed_findings) -> str:
    """The counts as one line — '1 Critical · 2 Major · 0 Minor ·
    0 Trivial' — or "" when no finding carried a severity (a
    pre-format report of plain lines has no counts to show)."""
    counts = finding_severity_counts(parsed_findings)
    if not any(counts.values()):
        return ""
    return " · ".join(f"{counts[s]} {s}" for s in FINDING_SEVERITIES)


_WALKTHROUGH_PART_RE = re.compile(
    r"^\s*\*{0,2}(What changed|Changes, in dependency order"
    r"|Effort|Risk):\*{0,2}\s*(.*)$")


def parse_walkthrough(review_text: str):
    """A review's ## Walkthrough section as a dict — {what_changed,
    changes, effort, risk} — or None when the report has no
    Walkthrough section (pre-CR3 reports). The four parts follow
    the Review prompt's shape: What changed is 1–3 sentences (it
    may run over several lines, until the next part); Changes, in
    dependency order is a list of {area, what, why} rows, each
    "FILE/AREA — what changed — why it matters" (a row with
    fewer separators keeps what it has, the rest reads as None);
    Effort and Risk are one line each. A part the report skips
    reads as None (changes as []), never an error — the parser
    reports what the report says and invents nothing. The bold
    markers are optional: a model that drops them still parses.
    Never raises."""
    try:
        body = section_body(review_text or "", "Walkthrough")
    except Exception:
        return None
    if body is None:
        return None
    out = {"what_changed": None, "changes": [],
           "effort": None, "risk": None}
    try:
        parts: dict[str, list[str]] = {}
        current = None
        for raw in body.splitlines():
            match = _WALKTHROUGH_PART_RE.match(raw)
            if match:
                current = match.group(1)
                parts.setdefault(current, [])
                if match.group(2).strip():
                    parts[current].append(match.group(2).strip())
                continue
            if current is not None and raw.strip():
                parts[current].append(raw.strip())
        changed = parts.get("What changed", [])
        if changed:
            out["what_changed"] = " ".join(changed).strip() or None
        for row in parts.get("Changes, in dependency order", []):
            text = _FINDING_BULLET_RE.sub("", row).strip()
            if not text:
                continue
            bits = [bit.strip() for bit in text.split(" — ")]
            if len(bits) >= 3:
                out["changes"].append(
                    {"area": bits[0] or None, "what": bits[1],
                     "why": " — ".join(bits[2:])})
            elif len(bits) == 2:
                out["changes"].append(
                    {"area": bits[0] or None, "what": bits[1],
                     "why": None})
            else:
                out["changes"].append(
                    {"area": None, "what": bits[0], "why": None})
        effort = parts.get("Effort", [])
        if effort:
            out["effort"] = " ".join(effort).strip() or None
        risk = parts.get("Risk", [])
        if risk:
            out["risk"] = " ".join(risk).strip() or None
    except Exception:
        return out
    return out


def homework_note(mode: str, reads: list[str], project_files: list[str]) -> str:
    """The shell's deterministic record of a homework pass, added
    to the conversation as a system note: what was actually read,
    and — decisively — whether the project is EMPTY, in which
    case the draft must say so instead of inventing contents."""
    read_text = "; ".join(reads) if reads else "nothing"
    if not project_files:
        return (f"[Homework — {mode} read pass complete. Read: {read_text}. "
                "The project folder is EMPTY — no files exist yet. State "
                "that plainly in the draft's context instead of inventing "
                "contents that are not there.]")
    names = ", ".join(project_files[:30])
    if len(project_files) > 30:
        names += f", … ({len(project_files)} files in all)"
    return (f"[Homework — {mode} read pass complete. Read: {read_text}. "
            f"The project holds {len(project_files)} file(s): {names}.]")


def clarify_instruction(mode: str) -> str:
    """The user-role instruction for Plan's clarify choice point.
    The reply is one of three shapes, all machine-checkable: the
    questions (marker line), PROCEED, or a plain answer."""
    questions = CLARIFY_QUESTIONS.get(mode, [])
    listed = "\n".join(f"- {q}" for q in questions)
    return (
        "Before any drafting, pin the specifics. Consider these "
        "questions:\n" + listed + "\n\n"
        "The owner's request and the homework may already answer some "
        "of them. Your reply must be exactly one of:\n"
        f"1. If any question whose answer would CHANGE the plan is "
        f"still open, reply with ONLY the open questions, starting "
        f"with the line '{QUESTIONS_MARKER}' and one question per "
        f"line. Ask nothing else and draft nothing yet.\n"
        "2. If everything that matters is pinned down, reply with "
        f"the single word {PROCEED_WORD}.\n"
        "3. If the request needs no plan at all, reply with the "
        "plain answer in prose — it will stand as the answer and "
        "no plan will be drafted.")


def section_instruction(mode: str, section: str, document_so_far: str,
                        asked: list[str] | None = None) -> str:
    """The user-role instruction for ONE section of a Plan draft.
    The shell assembles the document, so the reply is the section
    BODY only — no heading, no other sections."""
    parts = [
        f"You are drafting a plan document one section at a time. "
        f"Write ONLY the body of the '## {section}' section now — "
        "no heading, no other sections, no preamble."]
    if document_so_far.strip():
        parts.append("The document so far:\n" + document_so_far.strip())
    if section == "Goal":
        parts.append(
            "State what will be built and for whom, in a few concrete "
            "sentences — the outcome, not the steps.")
    elif section == "Context found":
        parts.append(
            "State what the homework actually found in the project: "
            "the files that exist and what they hold, as read — never "
            "invented. If the project folder is empty, say exactly "
            "that: the project is empty and this plan starts from "
            "nothing.")
    elif section == "Reference lock":
        parts.append(
            "This project is reference-driven (forge/reference_build.py): "
            "state the lock the reference produced — purpose, who or what "
            "it is for, look-or-feel / behaviour, must-haves, must-nevers, "
            "and what 'done' looks like — as read from "
            ".forge/reference/lock.md or the owner's reference in this "
            "conversation. Invent nothing that is not in the reference; "
            "anything unclear is named here as an open question, not "
            "filled in.")
    elif section == "Breakdown":
        parts.append(
            "This project is reference-driven (forge/reference_build.py): "
            "list the parts the locked reference breaks into under the "
            "project profile's breakdown headings, one part per line with "
            "its size/priority, what it depends on, and how it will be "
            "verified. A part that cannot be seen clearly in the "
            "reference is listed as UNKNOWN — never filled in from "
            "imagination.")
    elif section == "Contents":
        parts.append(
            "List the files this plan will create or change, one per "
            "line, each with what happens to it. Name real paths.")
    elif section == "Steps":
        parts.append(
            "Write the steps as a numbered list (1. 2. 3. …). Each "
            "step is ONE numbered line carrying the step's title and "
            "its substance, followed by indented continuation lines "
            "with the detail — what exactly happens and how — and a "
            "'Verify:' line saying how that step will be checked. "
            "Detail has room to live here: a builder reading only "
            "this section must know what to do at every step.")
    elif section == "Risks":
        parts.append(
            "Name the real risks and unknowns — what could go wrong, "
            "what could turn out different than assumed — one per line.")
    elif section == "Assumptions":
        if asked:
            listed = "\n".join(f"- {q}" for q in asked)
            parts.append(
                "Earlier you asked the owner these questions:\n"
                + listed + "\n"
                "The owner's reply answered some of them. For every "
                "question the reply did NOT answer, write the "
                "assumption you are drafting with, one per line, "
                "starting 'Assumed:'. Never silently invent an answer "
                "to an open question — an unanswered question with no "
                "assumption line here is a defect in this plan.")
        else:
            parts.append(
                "List every specific you are assuming because the "
                "owner did not pin it down, one per line, starting "
                "'Assumed:'. If the request and homework genuinely "
                "pinned everything down, say so in one line — never "
                "invent assumptions to fill the section, and never "
                "silently invent answers anywhere else either.")
    elif section == "Success criteria":
        parts.append(
            "State how everyone will know this worked — concrete, "
            "checkable statements, one per line, each phrased so a "
            "reviewer can verify it true or false ('Done means X "
            "is true'). Cover the whole plan, not single steps: "
            "the steps' Verify lines check the parts; these "
            "criteria are the standard the finished work as a "
            "whole is judged against. Outcomes only — never effort, "
            "intentions, or 'the code is written'.")
    elif section == "Out of scope":
        parts.append(
            "State what this plan deliberately does NOT do — the "
            "tempting extras, the neighbouring work, the follow-ups "
            "that are excluded — one per line, each starting "
            "'Not doing:'. Anything the steps do not cover that a "
            "reader might expect belongs here if it is excluded. "
            "If genuinely nothing is excluded, say so in one line — "
            "never invent exclusions to fill the section.")
    return "\n\n".join(parts)


_PLACEHOLDER_BODIES = ("", "(not stated)", "none", "none.", "n/a", "-",
                       "no assumptions", "no assumptions.")


def assumptions_fallback(document: str, questions: list[str]) -> str:
    """The shell's safety net for the clarification discipline:
    questions were asked in an earlier turn, the owner answered
    what they answered, and the drafted document's Assumptions
    section came back effectively empty — the unanswered round
    must not vanish silently. One line per asked question is
    appended, honestly framed as open questions carried as
    assumptions. A document whose Assumptions section carries
    real content is returned untouched."""
    if not questions:
        return document
    body = section_body(document, "Assumptions")
    if body is None:
        return document
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    if lines and not all(l.lower() in _PLACEHOLDER_BODIES for l in lines):
        return document
    addition = "\n".join(
        f"Assumed (open question carried forward): {q} — drafted "
        "with a reasonable default; correct it at the gate if wrong."
        for q in questions)
    sections = document_sections(document)
    out = []
    for heading, sec_body in sections:
        if heading.rstrip(":").lower() == "assumptions":
            kept = "\n".join(l for l in sec_body.splitlines()
                             if l.strip() and
                             l.strip().lower() not in _PLACEHOLDER_BODIES)
            sec_body = (kept + "\n" if kept else "") + addition
        out.append((heading, sec_body))
    rebuilt = []
    for heading, sec_body in out:
        if heading:
            rebuilt.append(f"## {heading}\n{sec_body}".rstrip())
        elif sec_body.strip():
            rebuilt.append(sec_body.strip())
    return "\n\n".join(rebuilt) + "\n"


# --- Write/Research modes (spec, 2026-11-09) ----------------------
# Research's plan gate: the worker drafts a research PLAN document
# section by section (RESEARCH_PLAN_SECTIONS), the owner approves
# it at the same gate Plan uses, and only then do the browse tools
# exist for the turn. The helpers below are the text half of that
# flow, mirroring section_instruction's contract: the reply is the
# section BODY only, the shell assembles the document.

def research_section_instruction(section: str, document_so_far: str,
                                 asked: list[str] | None = None) -> str:
    """The user-role instruction for ONE section of a research
    plan draft. Mirrors section_instruction: body only, no
    heading, no other sections."""
    parts = [
        f"You are drafting a research plan document one section at "
        f"a time. Write ONLY the body of the '## {section}' section "
        "now — no heading, no other sections, no preamble."]
    if document_so_far.strip():
        parts.append("The document so far:\n" + document_so_far.strip())
    if section == "Question":
        parts.append(
            "State the exact question this research must answer, in "
            "one or two concrete sentences — the question itself, "
            "not the method.")
    elif section == "Sub-questions":
        parts.append(
            "Break the question into the sub-questions the research "
            "must answer to settle it, as a numbered list (1. 2. "
            "3. …), one per line. Each sub-question is answerable "
            "from sources — a sub-question that no source could "
            "answer does not belong here.")
    elif section == "Sources to search":
        parts.append(
            "Name where the research will look, one source type per "
            "line: the web (which kinds of sites), this project's "
            "files, or files the owner supplied. Name real places — "
            "'the web' alone is not a source list. Name the "
            "authoritative source for each kind of question FIRST — "
            "the official docs, the official API reference, the "
            "project's own repository — before any copy, mirror, or "
            "summary, with its address when you know it.")
    elif section == "Depth":
        parts.append(
            "Write exactly one word — Standard or Deep — followed "
            "by one line saying why that depth fits this question. "
            "Standard is a focused pass over the best sources; "
            "Deep is a wide sweep that chases gaps through many "
            "more pages.")
    elif section == "Assumptions":
        if asked:
            listed = "\n".join(f"- {q}" for q in asked)
            parts.append(
                "Earlier you asked the owner these questions:\n"
                + listed + "\n"
                "The owner's reply answered some of them. For every "
                "question the reply did NOT answer, write the "
                "assumption you are drafting with, one per line, "
                "starting 'Assumed:'. Never silently invent an "
                "answer to an open question.")
        else:
            parts.append(
                "List every specific you are assuming because the "
                "owner did not pin it down, one per line, starting "
                "'Assumed:'. If the request and homework genuinely "
                "pinned everything down, say so in one line.")
    return "\n\n".join(parts)


_ITEM_LINE_RE = re.compile(r"^\s*(?:\d+[.)]|[-*]\s*(?:\[[ xX]\]\s*)?)\s+(\S.*)$")


def research_plan_steps(document: str) -> list[str]:
    """The gate checklist for a research plan: the items of its
    Sub-questions section, one string each (numbering/bullets
    stripped). [] when the document has no such section or it
    holds fewer than one item — the caller falls back to a
    single run-the-research step."""
    body = section_body(document, "Sub-questions")
    if body is None:
        return []
    steps = []
    for line in body.splitlines():
        m = _ITEM_LINE_RE.match(line)
        if m:
            steps.append(m.group(1).strip())
    return steps


# Study item M11 — authoritative sources: the one mechanical
# rule in this item. "Authoritative" has no clean general
# definition in code (the shell cannot know the docs domain of
# an arbitrary subject), so the definition is narrow and
# owner-grounded: a URL the owner-approved plan itself named in
# its "Sources to search" section. The run instruction lists
# those URLs first (below); everything else about authority
# stays instruction, in MODE_GUIDANCE and the run instruction.
_URL_RE = re.compile(r"https?://[^\s)>\]\"']+")


def authoritative_urls(document: str) -> list[str]:
    """The http(s) URLs the research plan names in its "Sources to
    search" section, in plan order, deduped — the plan's own
    authoritative sources. [] when the section names none (the
    instruction still applies; nothing is invented). Trailing
    sentence punctuation is stripped; a URL named twice counts
    once."""
    body = section_body(document, "Sources to search")
    if not body:
        return []
    seen: list[str] = []
    for match in _URL_RE.finditer(body):
        url = match.group(0).rstrip(".,;:!?")
        if url and url not in seen:
            seen.append(url)
    return seen


def research_depth(document: str) -> str:
    """The depth a research plan declares: "deep" when its Depth
    section leads with the word Deep, else "standard" — an
    undeclared depth is a Standard run, never a silent Deep one."""
    body = section_body(document, "Depth")
    if body:
        for line in body.splitlines():
            line = line.strip()
            if line:
                return "deep" if line.lower().startswith("deep") \
                    else "standard"
    return "standard"


def research_slug(request: str) -> str:
    """The file slug for a research run, derived from the owner's
    request: lowercase words joined with dashes, capped — the
    plan lands at .forge/research/<slug>-plan.md and the report
    at .forge/research/<slug>.md. An empty derivation falls back
    to "research"."""
    words = re.findall(r"[a-z0-9]+", str(request or "").lower())
    slug = "-".join(words[:8])[:48].strip("-")
    return slug or "research"


def research_work_instruction(document: str, steps: list[str],
                              report_path: str, depth: str,
                              page_cap: int) -> str:
    """The user-role message that turns an approved research plan
    into the turn's working instruction: run the plan with the
    browse tools, cite as you go, and land the report document
    at the named path against the report skeleton."""
    lines = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    skeleton = "\n".join(f"## {s}" for s in SKELETONS["research"])
    # Study item M11: the plan's own named sources, opened first.
    named = authoritative_urls(document)
    named_line = ""
    if named:
        named_line = (
            "The approved plan names these authoritative sources — "
            "open them first: " + ", ".join(named) + ". ")
    return (
        "The owner approved this research plan — it is now your "
        "working instruction. Run the research it describes:\n\n"
        + str(document).strip()
        + "\n\nWork the sub-questions in order:\n" + lines + "\n\n"
        "HOW TO RUN IT: use browse_open to open sources (a search "
        "results page first when you need to find them) and "
        "browse_read to read each page as text. Number your "
        "sources in the order you FIRST open them — source 1 is "
        "the first page you open, source 2 the next new one, and "
        f"so on. This run's budget is {page_cap} page operations "
        f"({depth} depth) and one hour; the shell stops browsing "
        "at the budget, so spend it on the best sources first. "
        "AUTHORITATIVE SOURCES FIRST: open the "
        "official docs, the official API reference, or the "
        "project's own repository before any copy, mirror, or "
        "summary. A scraped copy is second-class — cite it as a "
        "copy, and when sources disagree the authoritative one "
        "wins and the report says so. " + named_line +
        "When a sub-question stays unanswered after a round, note "
        "the gap and search again with different terms before "
        "giving up on it.\n\n"
        f"THE REPORT: when the research is done, write the report "
        f"to {report_path} with write_file, against this skeleton:\n"
        + skeleton + "\n## Sources cited\n\n"
        "In Findings, EVERY claim carries its source number in "
        "brackets, like [1] or [2] — a claim with no number is a "
        "defect. Evidence states what each source actually said, "
        "per source. Sources cited lists the sources your "
        "findings rely on, numbered to match. The shell appends "
        "the Sources consulted list itself, from the pages this "
        "run actually opened — do not write that section. If a "
        "sub-question could not be answered, it goes under Open "
        "questions, stated plainly — never papered over with a "
        "guess.")
