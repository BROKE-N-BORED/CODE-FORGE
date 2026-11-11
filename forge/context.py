"""Context gauge + auto-summary — Note 14 #3 (pure stdlib, no Qt).

Two halves, both honest about being approximations:

THE ESTIMATE. There is no tokenizer on the box and no new dependency
is welcome, so token load is estimated from characters: roughly
CHARS_PER_TOKEN characters per token (English prose runs ~4, code and
JSON run denser; 3.5 splits the difference for a coding agent), plus a
small fixed PER_MESSAGE_OVERHEAD for the role/template wrapping every
message carries. Every user-facing surface that shows these numbers
labels them as estimates ("~"). They exist to tell the owner "the
window is filling" in time to act — never to state an exact count.

THE FOLD. When a send would start with the estimated load at or above
FOLD_THRESHOLD of the window, the OLDEST exchanges fold into ONE
summary message before the worker starts (the window runs the
summarizer generation; this module owns the arithmetic and the
shaping). The fold is conservative:

  * the leading system prompt is never folded;
  * the last KEEP_EXCHANGES exchanges stay verbatim, and a trailing
    lone user message — the request being sent right now — is kept
    on top of those, so the current request can never fold;
  * a fold unit is a WHOLE exchange (a user message and everything
    that follows it up to the next user message), so an assistant
    tool_call and its tool results fold together as text or stay
    together verbatim — a tool message can never be orphaned;
  * an earlier summary is a system-role message marked
    {"summary": True}; on a later fold it joins the span and is
    re-folded into the new summary;
  * the candidate history must come back clean from the Note 26
    normalization checks and must actually be smaller than what it
    replaces — otherwise there is no fold, and the caller proceeds
    with the history untouched.

Nothing here mutates its input: folds build new lists (the message
dicts themselves are shared, exactly as normalize_history treats
them).
"""
from __future__ import annotations
import json
import math

from .ollama_client import normalize_history

# --- the estimate ---------------------------------------------------
CHARS_PER_TOKEN = 3.5
PER_MESSAGE_OVERHEAD = 6  # tokens of role/template wrapping per message

# --- the fold --------------------------------------------------------
FOLD_THRESHOLD = 0.80     # fold when a send would start at/above this fill
WARN_FRACTION = 0.75      # gauge turns amber
CRIT_FRACTION = 0.90      # gauge turns red
KEEP_EXCHANGES = 4        # newest exchanges that always stay verbatim

SUMMARY_PREFIX = ("Summary of the earlier conversation "
                  "(auto-summarized to fit the context window):\n")
SUMMARIZER_SYSTEM = (
    "You are summarizing the earlier part of a coding conversation so "
    "it can be replaced by your summary and the work can continue. "
    "Write a compact summary in plain text: what the user asked for, "
    "what was done, which files were created or changed and their "
    "state, decisions made, problems hit, and anything still open. "
    "Keep names, paths, and numbers exact. Do not add commentary, do "
    "not answer the conversation — only summarize it.")
SUMMARIZER_REQUEST = (
    "Summarize this earlier part of the conversation:\n\n")

SCRATCHPAD_PREFIX = ("Working scratchpad — the earlier conversation "
                     "condensed into state (auto-generated to fit "
                     "the context window):\n")
SCRATCHPAD_SYSTEM = (
    "You are condensing the earlier part of a coding conversation "
    "into a structured working scratchpad so it can replace that "
    "part and the work can continue. Output ONLY the scratchpad, "
    "with exactly these section headings, in this order:\n"
    "## Goal\n"
    "## Done\n"
    "## Files\n"
    "## Decisions\n"
    "## Open\n"
    "## Next\n"
    "Under each heading write terse bullets: what the goal is, what "
    "was done, which files were created or changed and their state, "
    "decisions made, anything still open, and what comes next. "
    "Keep names, paths, and numbers exact. If a section has nothing, "
    "its only bullet is \"- none\". Do not add commentary, do not "
    "answer the conversation — only condense it into state.")
SCRATCHPAD_REQUEST = (
    "Condense this earlier part of the conversation into "
    "the scratchpad:\n\n")

_TOOL_RESULT_CAP = 800    # chars per tool result inside the span text
_CONTENT_CAP = 4000       # chars per message inside the span text
_TOOL_CALL_CAP = 300      # chars per rendered tool call


def _payload_chars(value) -> int:
    """Characters a payload contributes: raw length for text, JSON
    length for anything structured (tool calls, schemas)."""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return len(str(value))


def estimate_message(message: dict) -> int:
    """Estimated tokens for ONE message: its content, its tool-call
    payloads (an assistant turn that mostly calls tools still fills
    the window), and a tool message's name, plus the fixed wrapping
    overhead."""
    chars = _payload_chars(message.get("content"))
    if message.get("tool_calls"):
        chars += _payload_chars(message["tool_calls"])
    if message.get("role") == "tool" and message.get("name"):
        chars += len(str(message["name"]))
    return math.ceil(chars / CHARS_PER_TOKEN) + PER_MESSAGE_OVERHEAD


def estimate_messages(messages) -> int:
    return sum(estimate_message(m) for m in messages or [])


def estimate_schemas(schemas) -> int:
    """Estimated tokens for the tool schemas sent with every request.
    Schemas carry no per-message wrapping, so no overhead is added."""
    return sum(math.ceil(_payload_chars(s) / CHARS_PER_TOKEN)
               for s in schemas or [])


def total_estimate(messages, schemas=None) -> int:
    """The load a request actually carries: the messages (the system
    prompt rides inside them) plus the tool schemas."""
    return estimate_messages(messages) + estimate_schemas(schemas)


def fill_fraction(messages, num_ctx, schemas=None) -> float:
    if not num_ctx or num_ctx <= 0:
        return 0.0
    return total_estimate(messages, schemas) / num_ctx


def breakdown(messages, schemas=None) -> dict:
    """What eats the window (the Note 15 design input on this item):
    system prompt vs conversation history vs tool schemas."""
    system = sum(estimate_message(m) for m in messages or []
                 if m.get("role") == "system")
    return {"system": system,
            "history": estimate_messages(messages) - system,
            "tools": estimate_schemas(schemas)}


def is_summary(message) -> bool:
    return bool(message.get("summary"))


def _units(body: list[dict]) -> list[list[dict]]:
    """Group a message body into fold units. A unit starts at each
    user message and runs to the next user message; anything before
    the first user message (a previous summary, a stray assistant
    message) forms a leading preamble unit. Folding and keeping only
    ever happen on whole units, so a tool message always travels with
    the assistant tool_call that produced it."""
    units: list[list[dict]] = []
    current: list[dict] = []
    for m in body:
        if m.get("role") == "user" and current:
            units.append(current)
            current = []
        current.append(m)
    if current:
        units.append(current)
    return units


def split_fold(messages):
    """Split a history into (head, span, tail) for a fold, or None
    when there is nothing foldable. head = the leading system prompt
    (never folded; a summary message is NOT head — it re-folds). tail
    = the last KEEP_EXCHANGES exchanges, verbatim, plus a trailing
    lone user message when one is present. span = everything older,
    and it only qualifies when it holds at least two messages or
    contains an earlier summary (a lone small message is not worth
    a generation)."""
    if not messages:
        return None
    head: list[dict] = []
    body = list(messages)
    if body[0].get("role") == "system" and not is_summary(body[0]):
        head = [body[0]]
        body = body[1:]
    units = _units(body)
    # A trailing lone user message is the request being sent (or the
    # newest turn of a restored history): it is kept ON TOP OF the
    # KEEP_EXCHANGES verbatim exchanges, never as one of them.
    trailing: list[dict] = []
    if units and len(units[-1]) == 1 and units[-1][0].get("role") == "user":
        trailing = units.pop()
    exchange_starts = [i for i, u in enumerate(units)
                       if u and u[0].get("role") == "user"]
    if len(exchange_starts) > KEEP_EXCHANGES:
        cut = exchange_starts[-KEEP_EXCHANGES]
    else:
        cut = exchange_starts[0] if exchange_starts else len(units)
    span = [m for u in units[:cut] for m in u]
    tail = [m for u in units[cut:] for m in u] + trailing
    if not span or not tail:
        return None
    if len(span) < 2 and not any(is_summary(m) for m in span):
        return None
    return head, span, tail


def plan_fold(messages, num_ctx, schemas=None, force=False):
    """Decide a fold for a history about to be sent. Returns a plan
    dict {head, span, tail, estimate_before} or None: below the
    threshold (unless force), or nothing foldable. Pure — the plan
    only borrows the caller's message dicts."""
    if not num_ctx or num_ctx <= 0:
        return None
    total = total_estimate(messages, schemas)
    if not force and total < FOLD_THRESHOLD * num_ctx:
        return None
    split = split_fold(messages)
    if split is None:
        return None
    head, span, tail = split
    return {"head": head, "span": span, "tail": tail,
            "estimate_before": total}


def _clip(text: str, cap: int) -> str:
    text = str(text or "")
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n[… {len(text) - cap} chars clipped for the summary request …]"


def render_span(span) -> str:
    """The fold span as plain text for the summarizer. Tool results
    are clipped hard (they are the bulkiest thing in any history) and
    tool calls render as one compact line — the summary needs to know
    what was done, not re-read every byte of it."""
    lines: list[str] = []
    for m in span:
        role = m.get("role")
        content = str(m.get("content") or "")
        if is_summary(m):
            lines.append("Earlier summary: " + _clip(content, _CONTENT_CAP))
        elif role == "user":
            lines.append("User: " + _clip(content, _CONTENT_CAP))
        elif role == "assistant":
            if content.strip():
                lines.append("Assistant: " + _clip(content, _CONTENT_CAP))
            for call in m.get("tool_calls") or []:
                fn = (call.get("function") or {}) if isinstance(call, dict) else {}
                name = fn.get("name", "tool")
                raw_args = fn.get("arguments")
                args = raw_args if isinstance(raw_args, str) else json.dumps(
                    raw_args or {}, ensure_ascii=False, default=str)
                lines.append(f"[called {name}({_clip(args, _TOOL_CALL_CAP)})]")
        elif role == "tool":
            lines.append("Tool result: " + _clip(content, _TOOL_RESULT_CAP))
        elif content.strip():
            lines.append(f"{role or 'message'}: " + _clip(content, _CONTENT_CAP))
    return "\n".join(lines)


def summary_messages(span, num_ctx, style="prose") -> list[dict]:
    """The summarizer request: a plain instruction on the SESSION
    model with tools off (the caller passes tools=[]). The span text
    is capped so the summary request itself fits the window it is
    making room in. style="scratchpad" swaps in the structured
    scratchpad instruction; any other value behaves as prose."""
    if style == "scratchpad":
        system, request = SCRATCHPAD_SYSTEM, SCRATCHPAD_REQUEST
    else:
        system, request = SUMMARIZER_SYSTEM, SUMMARIZER_REQUEST
    text = render_span(span)
    cap = int(num_ctx * CHARS_PER_TOKEN * 0.9)
    if len(text) > cap:
        text = text[:cap] + "\n[… span clipped to fit the summary request …]"
    return [{"role": "system", "content": system},
            {"role": "user", "content": request + text}]


def apply_fold(messages, plan, summary_text: str, style="prose"):
    """Build the folded history from a plan + the model's summary.
    Returns (new_messages, folded_count), or (None, 0) when the fold
    must not happen: empty summary, a candidate that fails the Note 26
    normalization checks (an orphan tool message, or no user message
    at all), or a "fold" that would not actually shrink the history.
    The caller's list is never mutated. style="scratchpad" stamps
    the result with SCRATCHPAD_PREFIX instead of SUMMARY_PREFIX;
    every other rule is identical for both styles."""
    text = str(summary_text or "").strip()
    if not text:
        return None, 0
    prefix = SCRATCHPAD_PREFIX if style == "scratchpad" else SUMMARY_PREFIX
    summary = {"role": "system", "summary": True,
               "folded_count": len(plan["span"]),
               "content": prefix + text}
    candidate = list(plan["head"]) + [summary] + list(plan["tail"])
    _checked, repairs = normalize_history(candidate)
    if repairs:
        return None, 0
    if estimate_messages(candidate) >= estimate_messages(messages):
        return None, 0
    return candidate, len(plan["span"])

# --- session notes (H5) ----------------------------------------------
# The fold above is lossy by design: the summary keeps the shape of
# the work, not every decision. The session notes file (forge/
# notes.py) is the durable counterweight — deterministic entries the
# app writes as events happen, plus anything the user adds. When a
# fold lands, the caller passes the file's current content here and
# it rides into the folded history as its own system message, right
# behind the summary, so the model keeps seeing it after the fold.
# The message is marked {"notes": True} the way a summary is marked
# {"summary": True}; on a LATER fold it is ordinary span text (its
# content re-folds like everything else) and the caller re-inserts a
# fresh copy from the file, which is the source of truth.

NOTES_PREFIX = "Session notes (kept on disk, user-editable):\n"


def is_notes(message) -> bool:
    return bool(message.get("notes"))


def with_session_notes(messages, notes_text):
    """Return a copy of a (folded) history carrying the session
    notes as one system message directly after the fold summary —
    or directly after the leading system prompt when there is no
    summary. Empty notes text returns the input list unchanged (the
    same object: no notes, no block, no cost). The input is never
    mutated."""
    text = str(notes_text or "").strip()
    if not text:
        return messages
    block = {"role": "system", "notes": True,
             "content": NOTES_PREFIX + text}
    out = list(messages)
    for i, m in enumerate(out):
        if is_summary(m):
            out.insert(i + 1, block)
            return out
    idx = 1 if out and out[0].get("role") == "system" else 0
    out.insert(idx, block)
    return out
