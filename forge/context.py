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
from pathlib import Path

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


# --- Manus study M2: restorable folds --------------------------------
# The fold above is lossy by design — render_span clips the span for
# the summarizer and the summary then replaces the originals. M2 makes
# every fold RESTORABLE (Manus: "the file system as the ultimate
# context — drop content, keep the pointer"): before a span is
# rendered/clipped for the summarizer, the caller archives the FULL
# span, un-clipped, inside the project, and the summary that replaces
# it carries a pointer to the archive. The archive lives under the
# project root (.forge/context/), so the ordinary read_file tool —
# jailed to the workspace — can read it back on demand. A summary is
# a plain content string in the message dict, so the pointer persists
# with the stored conversation and survives a session reload; and
# when a span containing an earlier summary folds again, that summary
# (pointer line included) is archived whole into the new archive —
# the pointers chain, and the oldest bytes stay reachable by walking
# the chain back.

ARCHIVE_REL_DIR = ".forge/context"  # workspace-relative, POSIX form


def archive_span(span, workspace) -> str:
    """Write a fold span's FULL messages to the project's archive and
    return the workspace-relative path (e.g.
    ".forge/context/fold-1.json"). The write is byte-exact: the span's
    own dicts serialized as JSON with sorted keys (the M1 discipline),
    never the clipped render_span text. Sequence = the next free
    fold-<n> integer in the archive folder, which is created lazily
    on this first write. RAISES on any failure — callers must treat a
    raise as "do not fold": content is never dropped without its
    pointer having landed on disk first."""
    folder = Path(workspace) / ".forge" / "context"
    folder.mkdir(parents=True, exist_ok=True)
    seq = 1
    while (folder / f"fold-{seq}.json").exists():
        seq += 1
    target = folder / f"fold-{seq}.json"
    payload = json.dumps(list(span), ensure_ascii=False,
                         sort_keys=True, default=str)
    target.write_text(payload, encoding="utf-8")
    return f"{ARCHIVE_REL_DIR}/fold-{seq}.json"


def archive_pointer(archive_rel: str) -> str:
    """The pointer line a summary carries when its span was archived
    (Manus study M2). Plain text inside the summary content, so it
    persists, renders, and re-folds like any other summary text."""
    return (f"[Full earlier conversation archived at {archive_rel} — "
            "use read_file on it if exact details are needed.]")


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
                # Manus study M1: sort_keys — this rendering enters
                # the summarizer's request verbatim, so the same call
                # must render to the same bytes whatever order its
                # arguments dict was parsed in.
                args = raw_args if isinstance(raw_args, str) else json.dumps(
                    raw_args or {}, ensure_ascii=False, default=str,
                    sort_keys=True)
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


def apply_fold(messages, plan, summary_text: str, style="prose",
               archive_rel=None):
    """Build the folded history from a plan + the model's summary.
    Returns (new_messages, folded_count), or (None, 0) when the fold
    must not happen: empty summary, a candidate that fails the Note 26
    normalization checks (an orphan tool message, or no user message
    at all), or a "fold" that would not actually shrink the history.
    The caller's list is never mutated. style="scratchpad" stamps
    the result with SCRATCHPAD_PREFIX instead of SUMMARY_PREFIX;
    every other rule is identical for both styles. archive_rel
    (Manus study M2) is the workspace-relative path archive_span
    returned for this span: the summary gains the archive pointer
    line and a "fold_archive" marker, so the clipped originals stay
    reachable. None — every caller without a project in scope — is
    exactly the pre-M2 behaviour."""
    text = str(summary_text or "").strip()
    if not text:
        return None, 0
    prefix = SCRATCHPAD_PREFIX if style == "scratchpad" else SUMMARY_PREFIX
    content = prefix + text
    summary = {"role": "system", "summary": True,
               "folded_count": len(plan["span"]),
               "content": content}
    if archive_rel:
        summary["content"] = content + "\n" + archive_pointer(archive_rel)
        summary["fold_archive"] = archive_rel
    candidate = list(plan["head"]) + [summary] + list(plan["tail"])
    _checked, repairs = normalize_history(candidate)
    if repairs:
        return None, 0
    if estimate_messages(candidate) >= estimate_messages(messages):
        return None, 0
    return candidate, len(plan["span"])

# --- system-prompt rank + compress (CR10) -------------------------
# The fold above handles HISTORY — the oldest exchanges fold into a
# summary when the window fills. What it never touched is the
# leading system prompt: it rides whole, every turn, however big
# the project got (repo map, knowledge index, guidelines, briefs).
# CR10 ranks and compresses THAT side before the model, the way
# CodeRabbit ranks its review context before its model. The
# ranking is by tier, not by keyword — no model call, no guessing:
#
#   tier 0 — core: the base prompt, house rules, mode guidance,
#     the self-extension instruction, the maintenance charter.
#     Never dropped; truncated only when even the core alone
#     exceeds the budget, and then with a truthful marker.
#   tier 1 — the owner's words: project instructions, hints,
#     folder prompts, review learnings, approved guidelines,
#     scratchpad. Dropped whole when they do not fit — an
#     owner's instruction cut mid-sentence is worse than absent.
#   tier 2 — project state: brief, knowledge index, sources
#     index, production markings.
#   tier 3 — generated indexes: project map, skills index,
#     macros index. First to go.
#
# The budget is a share of the window (SYSTEM_PROMPT_SHARE): the
# system prompt is the static part of a request, and the rest of
# the window belongs to the history, the tool schemas, and the
# reply. When everything fits, the output is the input unchanged
# — no footer, no reordering — so a small project's prompt is
# byte-identical to before CR10. When something is dropped or
# truncated, the prompt carries a short footer naming what was
# lost, so the model sees the gap instead of silently working
# without it, and the caller logs the same line for the owner.
# Nothing here mutates its input. Never raises.

SYSTEM_PROMPT_SHARE = 0.50

#: The chars kept when a tier-0 part must be truncated and there
#: is no budget left at all — a stub, not a silent drop.
_TRUNCATED_STUB_CHARS = 400


def estimate_text(text: str) -> int:
    """Estimated tokens for a plain string, on the same
    characters-per-token rule as the message estimate."""
    return math.ceil(len(str(text or "")) / CHARS_PER_TOKEN)


def _truncate_marked(text: str, keep_chars: int) -> tuple[str, int]:
    """(truncated text, omitted chars): the head of `text` plus a
    truthful marker naming how much was cut. A part already under
    the cap returns unchanged with 0 omitted."""
    text = str(text or "")
    if keep_chars < 0:
        keep_chars = 0
    if len(text) <= keep_chars:
        return text, 0
    head = text[:keep_chars].rstrip()
    omitted = len(text) - len(head)
    return (head + f"\n[… {omitted} chars omitted to fit the "
                   f"context window …]"), omitted


def compress_system_parts(parts, budget_tokens: int):
    """Rank and compress a system prompt's parts to a token
    budget. `parts` is a list of (label, tier, text) in the
    prompt's own order. Returns (kept_texts, report): kept texts
    in ORIGINAL order (the prompt reads the same, just shorter),
    and report {dropped: [labels], truncated: [(label,
    omitted_chars)], kept_tokens, budget}. Ranking is by tier,
    then original order (stable). Tier 0 is never dropped —
    truncated with a marker when it alone exceeds the budget.
    Tiers 1–3 are dropped whole when they do not fit. When the
    whole prompt fits, kept_texts is the input texts unchanged
    and the report is empty. Never raises."""
    try:
        budget = int(budget_tokens or 0)
    except Exception:
        budget = 0
    try:
        items = [(str(label), int(tier), str(text))
                 for label, tier, text in (parts or [])]
    except Exception:
        return [], {"dropped": [], "truncated": [],
                    "kept_tokens": 0, "budget": budget}
    if not items:
        return [], {"dropped": [], "truncated": [],
                    "kept_tokens": 0, "budget": budget}
    estimates = [estimate_text(text) for _label, _tier, text in items]
    total = sum(estimates)
    if total <= budget:
        return [text for _label, _tier, text in items], {
            "dropped": [], "truncated": [],
            "kept_tokens": total, "budget": budget}
    ranked = sorted(range(len(items)),
                    key=lambda i: (items[i][1], i))
    kept: dict[int, str] = {}
    dropped: list[str] = []
    truncated: list[tuple[str, int]] = []
    remaining = budget
    for i in ranked:
        label, tier, text = items[i]
        est = estimates[i]
        if est <= remaining:
            kept[i] = text
            remaining -= est
            continue
        if tier == 0:
            keep_chars = max(
                int(remaining * CHARS_PER_TOKEN),
                _TRUNCATED_STUB_CHARS if remaining <= 0 else 0)
            cut, omitted = _truncate_marked(text, keep_chars)
            kept[i] = cut
            truncated.append((label, omitted))
            remaining = 0
            continue
        dropped.append(label)
    kept_tokens = sum(estimate_text(kept[i]) for i in kept)
    return [kept[i] for i in sorted(kept)], {
        "dropped": dropped, "truncated": truncated,
        "kept_tokens": kept_tokens, "budget": budget}


def compression_note(report: dict) -> str:
    """The footer a compressed prompt carries, naming what was
    lost. Empty report reads as "" — the caller appends nothing
    when nothing was compressed."""
    try:
        dropped = report.get("dropped") or []
        truncated = report.get("truncated") or []
        if not dropped and not truncated:
            return ""
        bits = [f"est. {report.get('kept_tokens', 0)} of "
                f"{report.get('budget', 0)} tokens kept"]
        if dropped:
            bits.append("dropped: " + ", ".join(dropped))
        if truncated:
            bits.append("truncated: " + ", ".join(
                label for label, _omitted in truncated))
        return ("Context note — the system prompt was ranked and "
                "compressed to fit the window (" + "; ".join(bits)
                + ").")
    except Exception:
        return ""


# --- Manus study M8: schema-based trajectory summary -----------------
# The backstop for when M2's restorable compression SATURATES: the
# fold cannot shrink the history any further (apply_fold refuses a
# non-shrinking fold, the summary comes back empty, or the mid-turn
# fold cap is spent) and the turn would otherwise wrap up over a
# stale, over-full history. The backstop is a trajectory summary
# built PROGRAMMATICALLY from the recorded history against FIXED
# fields only — goal, plan status, files changed, decisions,
# errors, open items, record pointer — never freeform prose, never
# a model call. A field with nothing on record renders as
# "(not recorded)": the summary never invents a value. The summary
# is both a message (marker {"trajectory": True} — deliberately NOT
# "summary", so a refused fold's tests and the fold machinery never
# mistake it for a fold summary) carrying the structured data on a
# "trajectory_data" key, and a file (.forge/context/trajectory.json
# beside the M2 archives) so a later resume can find it. The
# message persists with the stored conversation like any other
# message, so a resumed worker re-arms its live plan state from
# the marker (see worker._restore_trajectory_plan) and the turn
# refreshes the message at its end (worker._refresh_trajectory)
# so the snapshot never goes stale while the plan evolves.

TRAJECTORY_PREFIX = ("Trajectory summary — the run so far, in fixed "
                     "fields (program-built from the recorded "
                     "history; a field with nothing on record "
                     "says so):\n")
TRAJECTORY_REL = ".forge/context/trajectory.json"
NOT_RECORDED = "(not recorded)"
TRAJECTORY_FIELDS = ("goal", "plan_status", "files_changed",
                     "decisions", "errors", "open_items",
                     "record_pointer")
_WRITE_TOOLS = frozenset({"write_file", "append_file", "edit_file"})
_TRAJ_FILES_CAP = 20
_TRAJ_ERRORS_CAP = 10
_TRAJ_OPEN_CAP = 20
_TRAJ_STEP_CLIP = 120
_TRAJ_GOAL_CLIP = 200


def _traj_clip(text: str, cap: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= cap else text[:cap].rstrip() + "…"


def _traj_tool_args(call) -> dict:
    """A tool call's arguments as a dict (they arrive parsed or as
    a JSON string, exactly as render_span already handles)."""
    fn = (call.get("function") or {}) if isinstance(call, dict) else {}
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _traj_files_changed(messages) -> list[str]:
    """Paths the run actually wrote: write/append/edit calls whose
    paired tool result did NOT come back a tool error. Pairing is
    positional — the tool messages directly after the assistant
    message, in call order — the same pairing the transcript
    itself carries. Order of first write, deduplicated, capped."""
    files: list[str] = []
    msgs = list(messages or [])
    i = 0
    while i < len(msgs):
        m = msgs[i]
        calls = (m.get("tool_calls") if m.get("role") == "assistant"
                 else None)
        if not calls:
            i += 1
            continue
        results: list[dict] = []
        j = i + 1
        while (j < len(msgs) and msgs[j].get("role") == "tool"
               and len(results) < len(calls)):
            results.append(msgs[j])
            j += 1
        for call, res in zip(calls, results):
            fn = (call.get("function") or {}) \
                if isinstance(call, dict) else {}
            if fn.get("name") not in _WRITE_TOOLS:
                continue
            if str((res or {}).get("content") or "") \
                    .startswith("Tool error:"):
                continue
            path = _traj_tool_args(call).get("path")
            if path and str(path) not in files:
                files.append(str(path))
        i = j
    return files[:_TRAJ_FILES_CAP]


def _traj_errors(messages) -> list[str]:
    """Distinct failed tool results, first line each, capped.
    The M4 failure_detail rides on the message copy, not the
    result, so the result's own first line is the stable shape."""
    out: list[str] = []
    for m in messages or []:
        if m.get("role") != "tool":
            continue
        content = str(m.get("content") or "")
        if not content.startswith("Tool error:"):
            continue
        first = _traj_clip(content.splitlines()[0], _TRAJ_STEP_CLIP)
        if first and first not in out:
            out.append(first)
    return out[:_TRAJ_ERRORS_CAP]


def _traj_archives(messages) -> list[str]:
    """Fold-archive pointers already on record in this history
    (the M2 fold_archive markers), oldest first, deduplicated."""
    out: list[str] = []
    for m in messages or []:
        rel = m.get("fold_archive")
        if rel and rel not in out:
            out.append(str(rel))
    return out


def build_trajectory(messages, plan_steps=None, plan_done=None,
                     title="", handoffs=None, decisions=None,
                     archive_rels=None) -> dict:
    """The fixed-field trajectory of a run, built only from what
    is on record. plan_steps/plan_done are the worker's live
    checklist (None = no plan on record); handoffs are M7 entries
    ({"parsed": {"open": [...]}}); decisions is a caller-supplied
    list (the plan document has no Decisions section on record —
    see the audit — so the worker path passes none and the field
    honestly says so); archive_rels overrides the scan of
    fold_archive markers when the caller already collected them.
    Keys are the fixed TRAJECTORY_FIELDS, in that order; any field
    with nothing on record is None (plan_status) or [] and renders
    as "(not recorded)" / "(none recorded)". Pure — no model, no
    disk, no mutation of the input."""
    steps = list(plan_steps) if plan_steps is not None else None
    done = set(plan_done or set()) if steps is not None else set()
    goal = _traj_clip(title, _TRAJ_GOAL_CLIP)
    if not goal:
        for m in messages or []:
            if m.get("role") == "user":
                first = str(m.get("content") or "").strip() \
                    .splitlines()
                if first:
                    goal = _traj_clip(first[0], _TRAJ_GOAL_CLIP)
                    break
    if not goal:
        goal = NOT_RECORDED
    if steps is None:
        plan_status = None
    else:
        done_idx = sorted(i + 1 for i in done if 0 <= i < len(steps))
        # Manus fix F4: the plan's standing. A fresh build is for
        # the LIVE plan, so it is always "active". The other values
        # are written only by an explicit owner act, never inferred:
        # "superseded" when the owner approves a newer plan (the
        # window marks it — see supersede_trajectory), "abandoned"
        # for a future explicit-abandon path. The worker's restore
        # never re-arms a superseded or abandoned plan; a record
        # written before F4 has no "status" key and reads as
        # active, so old trajectories resume exactly as before.
        plan_status = {
            "title": _traj_clip(title, _TRAJ_GOAL_CLIP),
            "total": len(steps),
            "done": done_idx,
            "steps": [_traj_clip(s, _TRAJ_STEP_CLIP) for s in steps],
            "open": [_traj_clip(steps[i], _TRAJ_STEP_CLIP)
                     for i in range(len(steps)) if i not in done],
            "status": "active",
        }
    open_items: list[str] = []
    if plan_status is not None:
        open_items.extend(plan_status["open"])
    for entry in handoffs or []:
        parsed = (entry or {}).get("parsed") or {}
        for item in parsed.get("open") or []:
            text = _traj_clip(item, _TRAJ_STEP_CLIP)
            if text and text not in open_items:
                open_items.append(text)
    open_items = open_items[:_TRAJ_OPEN_CAP]
    rels = list(archive_rels) if archive_rels is not None \
        else _traj_archives(messages)
    return {"goal": goal,
            "plan_status": plan_status,
            "files_changed": _traj_files_changed(messages),
            "decisions": [_traj_clip(d, _TRAJ_STEP_CLIP)
                          for d in (decisions or [])],
            "errors": _traj_errors(messages),
            "open_items": open_items,
            "record_pointer": rels}


def render_trajectory(data: dict) -> str:
    """The fixed-field summary as compact text, keys always in
    TRAJECTORY_FIELDS order. Nothing here is freeform: every line
    is a label plus the recorded values, or the honest marker."""
    lines = [f"Goal: {data.get('goal') or NOT_RECORDED}"]
    status = data.get("plan_status")
    if status:
        done = status.get("done") or []
        total = status.get("total") or len(status.get("steps") or [])
        open_idx = [i + 1 for i in range(total)
                    if (i + 1) not in set(done)]
        # Manus fix F4: a dead plan says so in its own text, so a
        # model reading the record never mistakes it for live work.
        standing = status.get("status") or "active"
        marker = ""
        if standing == "superseded":
            marker = (" — SUPERSEDED: the owner approved a newer "
                      "plan; do not resume this one.")
        elif standing == "abandoned":
            marker = (" — ABANDONED: the owner ended this plan; "
                      "do not resume it.")
        lines.append(
            f"Plan: {status.get('title') or 'plan'} — "
            f"{len(done)} of {total} steps done; "
            f"open steps: "
            f"{', '.join(str(i) for i in open_idx) or 'none'}"
            f"{marker}")
        if status.get("open"):
            lines.append("Open steps: " + "; ".join(status["open"]))
    else:
        lines.append(f"Plan: {NOT_RECORDED}")
    files = data.get("files_changed") or []
    lines.append("Files changed: " + ("; ".join(files)
                                      if files else "(none recorded)"))
    decisions = data.get("decisions") or []
    lines.append("Decisions: " + ("; ".join(decisions) if decisions
                                  else NOT_RECORDED))
    errors = data.get("errors") or []
    lines.append("Errors: " + ("; ".join(errors) if errors
                               else "(none recorded)"))
    open_items = data.get("open_items") or []
    lines.append("Open items: " + ("; ".join(open_items) if open_items
                                   else "(none recorded)"))
    rels = data.get("record_pointer") or []
    lines.append("Full record: " + (", ".join(rels) if rels
                                    else "(no fold archive on record)"))
    return "\n".join(lines)


def trajectory_message(data: dict) -> dict:
    """The trajectory as a system message. Marker is
    {"trajectory": True} — deliberately NOT "summary": a refused
    fold's pinned tests assert no summary message lands, and the
    fold machinery must never mistake this for one. The structured
    data rides on "trajectory_data" so a resumed worker can re-arm
    its plan state from the record without parsing prose."""
    return {"role": "system", "trajectory": True,
            "trajectory_data": data,
            "content": TRAJECTORY_PREFIX + render_trajectory(data)}


def is_trajectory(message) -> bool:
    return bool((message or {}).get("trajectory"))


def latest_trajectory(messages):
    """The newest trajectory message in a history, or None. The
    trajectory is a snapshot: only the newest one is the record."""
    for m in reversed(list(messages or [])):
        if is_trajectory(m):
            return m
    return None


def supersede_trajectory(messages):
    """Manus fix F4: mark the newest trajectory's plan SUPERSEDED —
    the owner approved a newer plan, so the old one is dead and a
    later resume must never re-arm it. Only a LIVE plan is marked:
    no trajectory, no plan on record, an already-finished plan
    (re-arm is already a no-op for it), or an already-dead plan
    all return None. The message is mutated in place (the window
    owns its messages list) and its content re-rendered, so the
    stored text says superseded too; the caller persists the
    conversation and the trajectory.json copy. Returns the updated
    data dict, or None when nothing was marked."""
    latest = latest_trajectory(messages)
    if latest is None:
        return None
    data = latest.get("trajectory_data") or {}
    status = data.get("plan_status") or {}
    if not status:
        return None
    if (status.get("status") or "active") in ("superseded",
                                              "abandoned"):
        return None
    done = status.get("done") or []
    total = status.get("total") or len(status.get("steps") or [])
    if total and len(done) >= total:
        return None
    status["status"] = "superseded"
    latest["content"] = TRAJECTORY_PREFIX + render_trajectory(data)
    return data


def store_trajectory(workspace, data: dict) -> str:
    """Write the trajectory dict beside the M2 archives and return
    its workspace-relative path. RAISES on any failure — the
    in-memory message is the record; the file is the durable copy,
    so a failed store never blocks the wrap-up, it only logs."""
    folder = Path(workspace) / ".forge" / "context"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "trajectory.json"
    payload = json.dumps(data, ensure_ascii=False,
                         sort_keys=True, default=str)
    target.write_text(payload, encoding="utf-8")
    return TRAJECTORY_REL


def trajectory_replacement(messages, plan, data: dict):
    """head + trajectory + tail for a fold plan's split, or None
    when the replacement must not happen: the candidate fails the
    Note 26 normalization checks, or it would not actually shrink
    the history (the same two refusals apply_fold enforces). The
    caller's list is never mutated."""
    candidate = (list(plan["head"]) + [trajectory_message(data)]
                 + list(plan["tail"]))
    _checked, repairs = normalize_history(candidate)
    if repairs:
        return None
    if estimate_messages(candidate) >= estimate_messages(messages):
        return None
    return candidate


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
