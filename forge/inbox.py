"""Inbox mode — one consolidated view of what needs attention,
what's running, and what's ready to merge (VS Code 1.146's Inbox
mode, adapted to Forge's real machinery; owner order 2026-11-11).

Pure stdlib, no Qt — the window in forge/main.py renders what
group_items() returns, and ForgeWindow owns the triage verbs.

Groups, in the owner's order (Working first — VS Code itself puts
Needs attention first; he can flip it with one word):
  working   — live turns (settled is None) with no gate, and
              parallel jobs queued/running.
  attention — live turns parked at a gate (stream["gate"] set, the
              _ws_gate queue) and the plan gate: plan_state with
              status "draft". The GoFish review rides the plan
              draft (plan_state["gofish"]), so a plan gate with
              findings says so in its detail — there is no separate
              GoFish queue in the real machinery.
  ready     — finished turns whose project has shadow checkpoints
              (list_checkpoints, via the owner's cached checker),
              and finished code jobs waiting on the gated Approve
              merge-back (parallel.py).
  held      — items the owner put on hold from the Inbox.
  done      — dismissed-from-Inbox items (still retrievable, not
              archived) plus finished sessions (settled turns,
              finished jobs) that carry no triage.

Triage (dismiss / hold / snooze / mark-done) is keyed by item and
persists in APP_DIR/inbox.json, app-level (the Inbox spans all
projects), so it survives a restart. A record snapshots the
item's title/project/kind and the activity stamp it was triaged
at; when the live item's activity stamp changes — a new turn in
the same conversation, a job requeued — the triage clears and the
item revives in its live group. Mark-as-Done never revives: the
owner explicitly closed it.

Snooze hides the item until the chosen time passes (1 hour, or
tomorrow 09:00 local) or new activity arrives, whichever comes
first. "Until next launch" hides it for the rest of this session
only: the record is dropped when the store next loads, so the
item is back after a restart.

Dismiss never stops a running turn and never archives anything:
there is no conversation-archive operation in Forge (storage has
delete, which is permanent, and no archived flag), so Dismiss and
Mark as Done are both Inbox-level states — Dismiss revives on
new activity, Mark as Done does not — and the underlying
conversation is untouched either way.

No wall-clock dependence in the grouping itself: every time read
goes through an injected now (store.now_fn / the now argument),
so tests drive it with fake time.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import fileperms as fileperms_mod

GROUPS = ("working", "attention", "ready", "held", "done")

GROUP_TITLES = {
    "working": "WORKING",
    "attention": "NEEDS ATTENTION",
    "ready": "READY TO MERGE",
    "held": "ON HOLD",
    "done": "DONE",
}

# Triage states a record can hold.
STATE_DISMISSED = "dismissed"
STATE_HELD = "held"
STATE_SNOOZED = "snoozed"
STATE_DONE = "done"

# Snooze choices the window offers.
SNOOZE_ONE_HOUR = "1h"
SNOOZE_TOMORROW = "tomorrow"
SNOOZE_NEXT_LAUNCH = "next_launch"
SNOOZE_CHOICES = (SNOOZE_ONE_HOUR, SNOOZE_TOMORROW, SNOOZE_NEXT_LAUNCH)


def snooze_window(choice, now):
    """(until_epoch | None, until_next_launch) for a snooze choice.

    "tomorrow" is tomorrow 09:00 local (today 09:00 when it is
    still ahead). "next_launch" carries no time — the record is
    dropped when the store next loads instead.
    """
    if choice == SNOOZE_NEXT_LAUNCH:
        return None, True
    if choice == SNOOZE_TOMORROW:
        day = datetime.fromtimestamp(now)
        target = day.replace(hour=9, minute=0, second=0, microsecond=0)
        if target.timestamp() <= now:
            target = target + timedelta(days=1)
        return target.timestamp(), False
    return now + 3600, False


class InboxStore:
    """The persisted triage state: one JSON file, app-level.

    Records are keyed by item key ("turn:<cid>", "plan:<cid>",
    "job:<id>") and hold {kind, state, title, project,
    snooze_until, until_next_launch, activity, updated}. A
    corrupt or missing file reads as empty — triage is never
    allowed to break the window. Saves are best-effort and never
    raise (the atomic private writer from fileperms, same shape
    as settings.json).
    """

    def __init__(self, path, now_fn=None):
        self.path = Path(path)
        self.now_fn = now_fn or time.time
        self._records = {}
        self.load()

    def load(self):
        try:
            raw = json.loads(self.path.read_text())
        except Exception:
            self._records = {}
            return
        if not isinstance(raw, dict):
            self._records = {}
            return
        self._records = {str(k): v for k, v in raw.items()
                         if isinstance(v, dict)}
        # "Until next launch" snoozes end at the launch: a record
        # left over from a previous session is dropped here, so
        # its item is back in its live group after a restart.
        dropped = [k for k, v in self._records.items()
                   if v.get("state") == STATE_SNOOZED
                   and v.get("until_next_launch")]
        for key in dropped:
            self._records.pop(key, None)
        if dropped:
            self.save()

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fileperms_mod.write_text_private(
                self.path, json.dumps(self._records, indent=2))
        except Exception:
            pass

    def records(self):
        return dict(self._records)

    def peek(self, key):
        """The raw record for `key`, or None — no expiry applied."""
        return self._records.get(key)

    def get(self, key, now=None):
        """The live record for `key`, or None. An expired timed
        snooze is deleted (and saved) here, so the item returns
        to its live group on the next grouping."""
        rec = self._records.get(key)
        if rec is None:
            return None
        if rec.get("state") == STATE_SNOOZED \
                and not rec.get("until_next_launch"):
            until = rec.get("snooze_until")
            if isinstance(until, (int, float)) \
                    and (now if now is not None else self.now_fn()) >= until:
                self._records.pop(key, None)
                self.save()
                return None
        return rec

    def set(self, key, record):
        self._records[key] = dict(record)
        self.save()

    def clear(self, key):
        if key in self._records:
            self._records.pop(key, None)
            self.save()


def _turn_key(cid, stream_id=None):
    if cid is not None:
        return f"turn:{cid}"
    return f"stream:{stream_id}"


def _is_live_stream(stream):
    # Mission Control's rows present a stream as running until it
    # settles; the Inbox groups the same way (a worker that died
    # without a settle signal is a stale record, and it reads as
    # stopped only once its stream settles).
    return stream.get("settled") is None


def _state_text(settled):
    if settled is None:
        return "Running"
    return {"done": "Finished", "failed": "Failed",
            "stopped": "Stopped"}.get(settled[0], "Finished")


def group_items(streams, plan_gate, job_rows, triage, now,
                has_checkpoints):
    """Group every inbox item. `streams` are the raw _streams
    records, `plan_gate` is None or {conversation_id, title,
    request, steps, gofish, started_at}, `job_rows` are
    parallel_job_rows() dicts, `triage` is an InboxStore,
    `has_checkpoints` maps a workspace path to bool.

    Returns {group: [item, ...]} in GROUPS order. Each item is
    {key, kind, title, project, model, detail, live,
    conversation_id, job_id, workspace, promote, activity,
    triage_state, triage_note, order}. Side effects, both
    documented: an expired snooze is cleared through
    triage.get(), and a dismissed item whose activity stamp
    changed is revived (its record cleared) here.
    """
    items = []
    plan_cid = (plan_gate or {}).get("conversation_id")

    for s in streams:
        cid = s.get("conversation_id")
        key = _turn_key(cid, s.get("id"))
        workspace = s.get("workspace") or ""
        project = s.get("project") or ""
        title = s.get("title") or "Conversation"
        model = s.get("model") or ""
        if _is_live_stream(s):
            # The plan turn's stream IS the plan-gate item (below);
            # listing it here too would double it.
            if s.get("planning") and plan_gate is not None \
                    and cid is not None and cid == plan_cid:
                continue
            gate = s.get("gate") or None
            if gate:
                detail = (f"{'Gate queued' if gate.get('queued') else 'Parked at gate'}"
                          f" — {gate.get('tool', '')}")
                group = "attention"
            else:
                status = (s.get("status_text") or "").strip()
                detail = f"Running — {status}" if status else "Running"
                group = "working"
            items.append({
                "key": key, "kind": "turn", "title": title,
                "project": project, "model": model, "detail": detail,
                "live": True, "conversation_id": cid, "job_id": None,
                "workspace": workspace, "promote": None,
                "activity": ["turn", s.get("started_at") or 0.0],
                "triage_state": None, "triage_note": "",
                "order": s.get("started_at") or 0.0,
                "_group": group})
        else:
            settled = s.get("settled")
            detail = _state_text(settled)
            promote = None
            group = "done"
            if settled is not None and settled[0] == "done" \
                    and workspace:
                try:
                    has = bool(has_checkpoints(workspace))
                except Exception:
                    has = False
                if has:
                    group = "ready"
                    promote = "checkpoints"
                    detail = ("Finished — checkpoints ready to "
                              "review")
            items.append({
                "key": key, "kind": "turn", "title": title,
                "project": project, "model": model, "detail": detail,
                "live": False, "conversation_id": cid, "job_id": None,
                "workspace": workspace, "promote": promote,
                "activity": ["turn", s.get("started_at") or 0.0],
                "triage_state": None, "triage_note": "",
                "order": s.get("started_at") or 0.0,
                "_group": group})

    if plan_gate is not None:
        detail = "Plan awaiting your call"
        if plan_gate.get("gofish"):
            detail += " · GoFish findings to read"
        items.append({
            "key": f"plan:{plan_cid}", "kind": "plan",
            "title": plan_gate.get("title") or "Plan",
            "project": plan_gate.get("project") or "",
            "model": plan_gate.get("model") or "",
            "detail": detail, "live": True,
            "conversation_id": plan_cid, "job_id": None,
            "workspace": plan_gate.get("workspace") or "",
            "promote": None,
            "activity": ["plan", plan_gate.get("request") or "",
                         plan_gate.get("steps") or 0],
            "triage_state": None, "triage_note": "",
            "order": plan_gate.get("started_at") or float("inf"),
            "_group": "attention"})

    for row in job_rows or []:
        job = row.get("job") or {}
        jid = job.get("id") or row.get("id")
        if not jid:
            continue
        state = job.get("state") or row.get("state") or ""
        kind = job.get("kind") or ""
        step = (job.get("step") or "").strip()
        detail = f"Parallel job — {state}"
        if state == "running" and step:
            detail += f" · {step}"
        if state in ("queued", "running"):
            group, promote = "working", None
        elif state in ("done", "merge_failed") and kind == "code":
            group, promote = "ready", "merge"
            detail = ("Parallel job — finished, merge waiting on "
                      "you" if state == "done" else
                      "Parallel job — merge failed, review the diff")
        else:
            group, promote = "done", None
        items.append({
            "key": f"job:{jid}", "kind": "job",
            "title": (job.get("task") or "")[:100] or "Parallel job",
            "project": Path(job.get("project") or "").name
                       or job.get("project") or "",
            "model": job.get("model") or "",
            "detail": detail, "live": state in ("queued", "running"),
            "conversation_id": None, "job_id": jid,
            "workspace": job.get("project") or "",
            "promote": promote,
            "activity": ["job", state, job.get("started") or 0.0,
                         job.get("finished") or 0.0],
            "triage_state": None, "triage_note": "",
            "order": job.get("started") or job.get("created") or 0.0,
            "job": job,
            "_group": group})

    grouped = {g: [] for g in GROUPS}
    live_keys = set()
    for item in items:
        live_keys.add(item["key"])
        rec = triage.get(item["key"], now=now)
        if rec is None:
            grouped[item["_group"]].append(item)
            continue
        state = rec.get("state")
        item["triage_state"] = state
        # Held/done sort newest-triage-first, so a triaged item
        # orders by when it was triaged, not when it started.
        if state in (STATE_HELD, STATE_DISMISSED, STATE_DONE):
            item["order"] = rec.get("updated") or item["order"]
        if state == STATE_HELD:
            item["triage_note"] = "On hold"
            grouped["held"].append(item)
        elif state == STATE_SNOOZED:
            # Still hidden (get() already cleared expired ones).
            continue
        elif state == STATE_DISMISSED:
            if list(rec.get("activity") or []) != list(item["activity"]):
                # New activity since the dismiss — the item
                # revives in its live group, triage cleared.
                triage.clear(item["key"])
                item["triage_state"] = None
                grouped[item["_group"]].append(item)
            else:
                item["triage_note"] = ("Dismissed — returns on new "
                                       "activity")
                grouped["done"].append(item)
        elif state == STATE_DONE:
            item["triage_note"] = "Marked done"
            grouped["done"].append(item)
        else:
            grouped[item["_group"]].append(item)

    # Triage records with no live item behind them stay visible
    # from their snapshot — a dismissed turn is retrievable after
    # its stream record is popped, a held job after it merges.
    for key, rec in triage.records().items():
        if key in live_keys:
            continue
        state = rec.get("state")
        if state == STATE_SNOOZED:
            # Expired timed snoozes are dropped by get() above
            # only for keys that were looked up; drop them here
            # too so an orphan never hides forever.
            until = rec.get("snooze_until")
            if not rec.get("until_next_launch") \
                    and isinstance(until, (int, float)) \
                    and now >= until:
                triage.clear(key)
            continue
        cid = None
        job_id = None
        if key.startswith("turn:"):
            cid = key[len("turn:"):]
        elif key.startswith("plan:"):
            cid = key[len("plan:"):]
        elif key.startswith("job:"):
            job_id = key[len("job:"):]
        kind = rec.get("kind") or ("job" if job_id else "turn")
        item = {
            "key": key, "kind": kind,
            "title": rec.get("title") or "Conversation",
            "project": rec.get("project") or "",
            "model": "", "detail": rec.get("detail") or "",
            "live": False, "conversation_id": cid,
            "job_id": job_id,
            "workspace": rec.get("workspace") or "",
            "promote": None,
            "activity": list(rec.get("activity") or []),
            "triage_state": state, "triage_note": "",
            "order": rec.get("updated") or 0.0}
        if state == STATE_HELD:
            item["triage_note"] = "On hold"
            grouped["held"].append(item)
        elif state in (STATE_DISMISSED, STATE_DONE):
            item["triage_note"] = ("Dismissed — returns on new "
                                   "activity" if state == STATE_DISMISSED
                                   else "Marked done")
            grouped["done"].append(item)

    for group in ("working", "attention", "ready"):
        grouped[group].sort(key=lambda i: i["order"])
    for group in ("held", "done"):
        grouped[group].sort(key=lambda i: -i["order"])
    for item in items:
        item.pop("_group", None)
    return grouped
