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


# ---------- Finding acceptance rate (Tier 2 #6) ----------
#
# Acceptance-rate tracking, Forge-scale: every time
# the owner acts on a Review finding — sends it for fixing
# (accepted) or teaches from it (dismissed) — one event lands in
# the SAME event log the Turn timeline already views, and the
# timeline's head shows the rate for the current project. Local
# only, no dashboard product: one line in an existing window.
#
# The events are not turns, so they never enter turn_entries —
# they are their own kinds, read by acceptance_stats. A decision
# with no parsed severity or type still counts in the totals
# (under "unparsed"); the rate is accepted / (accepted +
# dismissed) and reads as None when no decision is recorded —
# absent, never zero-filled, the timeline's standing rule.

FINDING_ACCEPTED = "finding_accepted"
FINDING_DISMISSED = "finding_dismissed"


def record_finding_decision(eventlog, decision, project, review,
                            finding_text):
    """Record one finding decision in the event log — the ONE
    place that does it, so the Fix button, the Teach button and
    (when T2-4 lands) the autofix rounds all log the same shape.
    `decision` is "accepted" or "dismissed"; the finding line is
    parsed for severity/type/source/file (modes.py — the same
    parser the room uses). A missing or broken event log is a
    silent no-op: logging never blocks the fix or the lesson.
    Never raises."""
    try:
        if eventlog is None:
            return
        kind = (FINDING_ACCEPTED if decision == "accepted"
                else FINDING_DISMISSED)
        from . import modes as modes_mod
        parsed = modes_mod.parse_finding(finding_text or "")
        eventlog.record(
            kind,
            project=str(project or ""),
            review=str(review or ""),
            file=modes_mod.finding_file(finding_text or ""),
            severity=(parsed or {}).get("severity") or "",
            type=(parsed or {}).get("type") or "",
            source=(parsed or {}).get("source") or "",
            finding=str(finding_text or ""))
    except Exception:
        return


def acceptance_stats(records, project=None):
    """The acceptance rate over finding-decision records:
    {accepted, dismissed, rate, by_severity, by_type}. `project`
    filters to one workspace (the current project); None counts
    every decision in the log. by_severity covers the four
    severities plus "unparsed"; by_type covers the finding types
    seen, plus "unparsed". Each bucket is {accepted, dismissed,
    rate} with rate None when the bucket has no decisions.
    Never raises."""
    stats = {"accepted": 0, "dismissed": 0, "rate": None,
             "by_severity": {}, "by_type": {}}
    try:
        from . import modes as modes_mod
        severities = list(modes_mod.FINDING_SEVERITIES)
        types = list(modes_mod.FINDING_TYPES)
    except Exception:
        severities = ["Critical", "Major", "Minor", "Trivial"]
        types = ["Bug", "Security", "Performance", "Refactor",
                 "Nitpick"]
    for severity in severities + ["unparsed"]:
        stats["by_severity"][severity] = {"accepted": 0,
                                          "dismissed": 0,
                                          "rate": None}
    for ftype in types + ["unparsed"]:
        stats["by_type"][ftype] = {"accepted": 0, "dismissed": 0,
                                   "rate": None}
    try:
        for record in records or []:
            if not isinstance(record, dict):
                continue
            kind = record.get("kind")
            if kind not in (FINDING_ACCEPTED, FINDING_DISMISSED):
                continue
            if project is not None and \
                    str(record.get("project") or "") != str(project):
                continue
            accepted = kind == FINDING_ACCEPTED
            if accepted:
                stats["accepted"] += 1
            else:
                stats["dismissed"] += 1
            severity = record.get("severity") or "unparsed"
            bucket = stats["by_severity"].get(severity)
            if bucket is None:
                bucket = stats["by_severity"]["unparsed"]
            bucket["accepted" if accepted else "dismissed"] += 1
            ftype = record.get("type") or "unparsed"
            bucket = stats["by_type"].get(ftype)
            if bucket is None:
                bucket = stats["by_type"]["unparsed"]
            bucket["accepted" if accepted else "dismissed"] += 1
    except Exception:
        return stats

    def _rate(bucket):
        total = bucket["accepted"] + bucket["dismissed"]
        return bucket["accepted"] / total if total else None

    stats["rate"] = _rate(stats)
    for bucket in list(stats["by_severity"].values()) + \
            list(stats["by_type"].values()):
        bucket["rate"] = _rate(bucket)
    return stats


def acceptance_line(stats) -> str:
    """The acceptance rate as one line for the Turn timeline's
    head — "Findings: 12 accepted · 5 dismissed — 71% accepted
    (Critical 3/3 · Major 5/7 · Minor 3/4 · Trivial 1/3)" — or
    "" when no decision is recorded (nothing to show, never a
    fake zero). Only severities with decisions appear in the
    breakdown. Never raises."""
    try:
        stats = stats or {}
        accepted = int(stats.get("accepted") or 0)
        dismissed = int(stats.get("dismissed") or 0)
        total = accepted + dismissed
        if not total:
            return ""
        rate = stats.get("rate")
        percent = round(rate * 100) if rate is not None else 0
        line = (f"Findings: {accepted} accepted · {dismissed} "
                f"dismissed — {percent}% accepted")
        parts = []
        for severity, bucket in (
                stats.get("by_severity") or {}).items():
            decided = (bucket.get("accepted") or 0) + \
                (bucket.get("dismissed") or 0)
            if decided:
                parts.append(f"{severity} {bucket['accepted']}/"
                             f"{decided}")
        if parts:
            line += " (" + " · ".join(parts) + ")"
        return line
    except Exception:
        return ""


def presend_stats(records, project=None):
    """Pre-send scan counts over the log's own records: blocks,
    and the owner's decisions after them, plus sends that went
    out un-scanned (the toggle off). A project filter keeps the
    counts to one project's sends — the records carry the
    project folder name. Unknown decision values are ignored,
    never counted."""
    stats = {"blocked": 0, "send": 0, "redact": 0, "allow": 0,
             "cancel": 0, "scan_off": 0, "exempted": 0}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        if project is not None \
                and record.get("project") != project:
            continue
        kind = record.get("kind")
        if kind == "presend_blocked":
            stats["blocked"] += 1
        elif kind == "presend_decision":
            decision = record.get("decision")
            if decision in stats:
                stats[decision] += 1
        elif kind == "presend_scan_off":
            stats["scan_off"] += 1
        elif kind == "presend_allowed":
            # "Exempted" is distinct from "allowed": allowed is
            # the owner's Allow + send choice in the dialog, this
            # is a finding an existing allow-list entry covered.
            stats["exempted"] += 1
    return stats


def presend_line(stats):
    """The timeline head's pre-send line, or "" when the scan has
    nothing to say (no block, no decision, no un-scanned send).
    Decisions are historical — the line counts the whole log,
    like the rollups above it."""
    if not stats:
        return ""
    if not (stats.get("blocked") or stats.get("send")
            or stats.get("redact") or stats.get("allow")
            or stats.get("cancel") or stats.get("scan_off")
            or stats.get("exempted")):
        return ""
    return (f"Pre-send scan: {stats['blocked']} blocked · "
            f"{stats['send']} sent anyway · "
            f"{stats['redact']} redacted · "
            f"{stats['allow']} allowed · "
            f"{stats['exempted']} exempted · "
            f"{stats['cancel']} cancelled · "
            f"{stats['scan_off']} sent un-scanned")
