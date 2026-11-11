"""Turn timeline (Replit-study item C9): a VIEW over the event
log's own turn records — nothing in this module records anything.

The worker already opens every turn with a `request` record (the
prompt text; the model rides every worker record) and closes it
with a `turn_complete` record carrying seconds, generations and
the prompt/eval token counts — the worker's ESTIMATES, and every
surface built on this module says so. A turn that raises closes
with an `error` record instead and has no totals: the timeline
shows what the record holds and nothing it doesn't.

What the log does NOT carry is which conversation a turn belonged
to, its mode, or whether the owner stopped it (a stopped turn
still logs turn_complete). Those three facts exist only as signals
on the window that ran the turn, so the window keeps per-
conversation MARKS — prompt, model, mode, outcome, observed at its
existing settle dispatchers — and conversation_entries() pairs
them with the log's entries in order. A turn with no mark, and a
mark with no log entry, simply does not appear: absent, never
zero-filled.
"""
from __future__ import annotations

import json
from pathlib import Path

# The per-conversation SOFT token budget default (settings key
# `soft_token_budget`, Settings → Seats & limits). The total is
# cumulative across a conversation's turns, so it sits far above
# the 8,192-token context window: it is the local answer to
# runaway-spend complaints — a flag the owner sees once per
# crossing, never a block, never a stop.
DEFAULT_SOFT_TOKEN_BUDGET = 200_000


def read_records(log_dir):
    """Every record in the log directory, oldest first: the day
    files in name order, lines in file order, streamed line by
    line (full-capture logs grow by design). Malformed lines and
    non-object JSON are skipped, as the session-summary reader
    already does."""
    records = []
    try:
        files = sorted(Path(log_dir).glob("forge-*.jsonl"))
    except Exception:
        return records
    for path in files:
        try:
            stream = open(path, encoding="utf-8")
        except Exception:
            continue
        with stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if isinstance(entry, dict):
                    records.append(entry)
    return records


def _number(value):
    """A record field as a number, or None when it isn't one —
    a partial record shows what it holds, never a fabricated
    zero in its place."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def turn_entries(records):
    """Pair the record stream into completed turns, oldest first.

    A `request` record opens a turn; the next `turn_complete`
    closes it as finished, the next `error` closes it as failed
    (no token totals — the turn never reached its own counting).
    A `turn_complete` with no open request still counts (its
    request scrolled past the read window); an opened turn that
    is never closed — superseded by the next request, or the app
    closed mid-turn — is dropped: no closing record, no entry.
    """
    entries = []
    pending = None
    for record in records:
        kind = record.get("kind")
        if kind == "request":
            pending = {"started": record.get("ts"),
                       "model": record.get("model"),
                       "prompt": str(record.get("prompt", ""))}
        elif kind == "turn_complete":
            entry = {"started": None, "model": record.get("model"),
                     "prompt": "", "outcome": "finished"}
            if pending is not None:
                entry.update(pending)
                pending = None
            entry["ended"] = record.get("ts")
            entry["seconds"] = _number(record.get("seconds"))
            entry["generations"] = _number(record.get("generations"))
            entry["prompt_tokens"] = _number(record.get("prompt_tokens"))
            entry["eval_tokens"] = _number(record.get("eval_tokens"))
            entries.append(entry)
        elif kind == "error" and pending is not None:
            entry = dict(pending)
            entry["ended"] = record.get("ts")
            entry["outcome"] = "failed"
            entry["seconds"] = None
            entry["generations"] = None
            entry["prompt_tokens"] = None
            entry["eval_tokens"] = None
            pending = None
            entries.append(entry)
    return entries


def conversation_entries(records, marks):
    """One conversation's turns: the window's marks, in settle
    order, consume the log's entries in record order — a mark
    takes the next unconsumed entry whose prompt AND model match
    it, and lends that entry its mode and its outcome (the settle
    signal knows "stopped"; the log alone cannot tell a stopped
    turn from a finished one). A mark with no matching entry
    contributes nothing."""
    entries = turn_entries(records)
    picked = []
    cursor = 0
    for mark in marks:
        for i in range(cursor, len(entries)):
            entry = entries[i]
            if entry.get("prompt") == mark.get("prompt") and \
                    entry.get("model") == mark.get("model"):
                merged = dict(entry)
                merged["mode"] = mark.get("mode") or ""
                merged["outcome"] = mark.get("outcome") or entry["outcome"]
                picked.append(merged)
                cursor = i + 1
                break
    return picked


def rollups(entries):
    """Conversation totals over exactly the entries the timeline
    lists; a sum skips any field a record didn't carry."""
    def total(key):
        return sum(e[key] for e in entries if e.get(key) is not None)

    prompt = total("prompt_tokens")
    evaled = total("eval_tokens")
    return {"turns": len(entries),
            "prompt_tokens": prompt,
            "eval_tokens": evaled,
            "total_tokens": prompt + evaled,
            "seconds": round(total("seconds"), 2),
            "generations": total("generations")}
