"""Gated local event triggers (Manus study M13, Tier 2 #5).

A local event the shell already observes — a Tester run that came
back VERDICT: FAIL, a run that crashed — may PROPOSE an action to
the owner. A proposal is a small fixed-schema record in
<project>/.forge/proposals.json; it surfaces in the bench log, the
event log, and the phone deck's proposals field, and it waits.
Accepting runs the action as an ordinary owner-approved step
through the existing gates; declining — or no action at all —
kills it. Nothing here runs a tool, starts a turn, watches a file,
or spawns a thread: proposals are raised only by code paths that
already observed the event (the worker's tester-handoff
collection, the window's monitor-alert noting), and there is no
background observation of any kind. A proposal that is not
decided within PROPOSAL_TTL_SECONDS dies on its own — it never
queues forever and can never execute late.

What is NOT covered, on purpose:
- File-changed events (Manus fix F7, the M11 remainder):
  forge/project_watcher.py watches the project tree when the
  owner enables it (Settings → File watcher, default OFF) and
  raises a file_changed proposal through the existing path —
  propose-only, never auto-applied. The watcher is the only
  observation this module trusts; there is still no polling
  loop and no background daemon.
- Build-finished (a run that exited 0): observed, but it
  proposes nothing — a success leaves the owner nothing to
  decide.
- Stall / repeat run alerts: they stay alerts only. A stall
  (silence) has no failing code to fix, and a repeat restates
  an error the crash alert already covers.

A proposal can never approve itself. It is not a tool call, so
the worker's rule-allow branch never sees it; the gate a
proposal's action passes through is presented with Remember
hidden (the maintenance-command precedent), so no remembered
allow-rule can ever be minted from a proposal; and a decided
or expired proposal refuses any further decision — an accept
cannot be replayed.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

# A proposal the owner never answers dies after a day — long
# enough to be seen across a restart, short enough that nothing
# executes on stale news.
PROPOSAL_TTL_SECONDS = 24 * 60 * 60
MAX_PROPOSALS = 50
STORE_NAME = "proposals.json"

# Manus fix F7: file_changed joins the two M13 kinds — a file
# the owner (or another tool) changed outside the Forge, raised
# by the owner-enabled watcher only.
PROPOSAL_KINDS = ("tester_failed", "run_failed", "file_changed")

# Fixed keys in fixed order — the record is a file the owner can
# read, and a later version must still parse an old one.
PROPOSAL_FIELDS = ("id", "kind", "event", "action", "touches",
                   "detail", "raised_at", "status", "decided_at")
STATUSES = ("pending", "accepted", "declined", "expired")


def store_path(root: str | Path) -> Path:
    return Path(root) / ".forge" / STORE_NAME


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _atomic_write(path: Path, content: str) -> None:
    """Temp file in the SAME folder, fsync, replace — the tree's
    write pattern, so a crash mid-write never truncates the
    store (handoff.keeper_add's shape)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def new_proposal(kind: str, event: str, action: str, touches: str,
                 detail: dict | None = None, now: float | None = None,
                 seq: int = 0) -> dict:
    """One proposal record. `detail` carries the event's own
    evidence (the monitor alert dict, the tester report's
    sections) so the accept path can act without re-deriving it."""
    if kind not in PROPOSAL_KINDS:
        raise ValueError(f"unknown proposal kind: {kind!r}")
    stamp = now if now is not None else time.time()
    return {"id": f"prop-{time.strftime('%Y%m%d%H%M%S', time.localtime(stamp))}-{seq:02d}",
            "kind": kind,
            "event": str(event or ""),
            "action": str(action or ""),
            "touches": str(touches or ""),
            "detail": dict(detail or {}),
            "raised_at": _iso(stamp),
            "status": "pending",
            "decided_at": None}


def public(proposal: dict) -> dict:
    """The fields the log, the deck, and the owner see — the
    record as stored, in field order, nothing internal."""
    return {key: proposal.get(key) for key in PROPOSAL_FIELDS}


def fix_turn_task(proposal: dict) -> str:
    """The task text an accepted tester_failed proposal starts
    as an ordinary Code turn. Built from the record only —
    nothing invented, the failing evidence quoted back."""
    detail = proposal.get("detail") or {}
    evidence = str(detail.get("evidence") or "").strip()
    files = str(detail.get("files") or "").strip()
    lines = ["Fix the failing test(s) the Tester just reported."]
    if files and files != "(none)":
        lines.append(f"Files involved:\n{files}")
    if evidence:
        lines.append(f"Failing evidence:\n{evidence[:1000]}")
    lines.append("Run the tests again when the fix is in, and say "
                 "plainly what changed.")
    return "\n".join(lines)


def review_turn_task(proposal: dict) -> str:
    """The task text an accepted file_changed proposal starts
    as an ordinary Code turn (Manus fix F7). Built from the
    record only — the file the watcher saw change. The turn
    looks and reports; it changes nothing unless the owner
    asks, because the change came from outside the Forge and
    the owner may have meant exactly what they did."""
    touches = str(proposal.get("touches") or "").strip()
    lines = ["A file changed outside the Forge — look at what "
             "changed and say whether anything needs doing."]
    if touches:
        lines.append(f"File: {touches}")
    lines.append("Change nothing unless I ask — this turn only "
                 "looks and reports.")
    return "\n".join(lines)


class ProposalStore:
    """The per-project proposal store: a plain JSON list under
    .forge/, read fresh on every operation (the window is the
    only writer, and the list is tiny)."""

    def __init__(self, root: str | Path, now=time.time):
        self.root = Path(root)
        self._now = now

    @property
    def path(self) -> Path:
        return store_path(self.root)

    def _load(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        kept = []
        for entry in raw:
            if isinstance(entry, dict) and entry.get("kind") in PROPOSAL_KINDS:
                kept.append({key: entry.get(key) for key in PROPOSAL_FIELDS
                             if key in entry})
                kept[-1].setdefault("status", "pending")
                kept[-1].setdefault("decided_at", None)
                kept[-1].setdefault("detail", {})
        return kept

    def _save(self, records: list[dict]) -> None:
        _atomic_write(self.path, json.dumps(records, indent=2,
                                            sort_keys=False) + "\n")

    def _is_expired(self, record: dict, now: float) -> bool:
        if record.get("status") != "pending":
            return False
        try:
            raised = time.mktime(time.strptime(
                str(record.get("raised_at") or ""),
                "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError, OverflowError):
            return False
        return (now - raised) > PROPOSAL_TTL_SECONDS

    def prune_expired(self, now: float | None = None) -> list[dict]:
        """Mark every over-TTL pending proposal expired and persist
        it. Returns the records that expired in this call."""
        stamp = now if now is not None else self._now()
        records = self._load()
        expired = []
        changed = False
        for record in records:
            if self._is_expired(record, stamp):
                record["status"] = "expired"
                record["decided_at"] = _iso(stamp)
                expired.append(record)
                changed = True
        if changed:
            self._save(records[-MAX_PROPOSALS:])
        return expired

    def raise_proposal(self, kind: str, event: str, action: str,
                       touches: str, detail: dict | None = None,
                       now: float | None = None) -> tuple[dict, bool]:
        """Raise a proposal. A pending proposal for the same kind
        and the same event already exists -> it is returned
        untouched (created=False): the same failure must not
        stack proposals while the owner has not answered the
        first. Returns (proposal, created)."""
        stamp = now if now is not None else self._now()
        self.prune_expired(stamp)
        records = self._load()
        for record in records:
            if record.get("status") == "pending" \
                    and record.get("kind") == kind \
                    and record.get("event") == event:
                return record, False
        seq = sum(1 for r in records
                  if r.get("id", "").startswith("prop-"))
        proposal = new_proposal(kind, event, action, touches,
                                detail=detail, now=stamp, seq=seq)
        records.append(proposal)
        self._save(records[-MAX_PROPOSALS:])
        return proposal, True

    def pending(self, now: float | None = None) -> list[dict]:
        """Pending proposals, oldest first, after expiring the
        over-TTL ones. What this returns is everything the owner
        can still decide."""
        stamp = now if now is not None else self._now()
        self.prune_expired(stamp)
        return [public(r) for r in self._load()
                if r.get("status") == "pending"]

    def decide(self, proposal_id: str, accept: bool,
               now: float | None = None) -> dict | None:
        """Decide a proposal. Only a pending, unexpired proposal
        can be decided — a decided or expired one refuses, so an
        accept can never be replayed. Returns the decided record
        (public view) or None when nothing was decided."""
        stamp = now if now is not None else self._now()
        self.prune_expired(stamp)
        records = self._load()
        for record in records:
            if record.get("id") != proposal_id:
                continue
            if record.get("status") != "pending":
                return None
            record["status"] = "accepted" if accept else "declined"
            record["decided_at"] = _iso(stamp)
            self._save(records[-MAX_PROPOSALS:])
            return public(record)
        return None
