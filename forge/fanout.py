"""Fan-out for similar independent items (Study item M12).

Fanning a task out to many agents at once is the wide-research
shape. Forge's hardware (Ryzen 3 / 8 GB, CPU-only) cannot do that, and
the #53 cap (max_parallel_turns, 1-4, default 1) is the hard
ceiling on how many turns may ever be live. What M12 adds, at
Forge scale, is a fan-out path for a batch of SIMILAR
INDEPENDENT items: each item runs as its own fresh-context
parallel turn under the existing cap, and each turn's return is
collected through the M7 handoff schema (forge/handoff.py) —
parsed, one repair round for an attempted-but-incomplete
return, flagged with its raw text intact when it still does
not parse.

THE SEEDING RULE (owner-grounded, never inferred): fan-out
happens ONLY when the batch itself asserts the items are
independent. A FanOutItem carries `independent` — False by
default, so an item is never fanned unless the owner (or an
approved plan record the owner accepted) said so. The model
never decides independence from the task text: that would be a
guess, and a wrong guess fans dependent steps. There is no
automatic independence detection anywhere in this module.

THE REFUSALS (plain, nothing runs):
  - the batch is empty, or an item has no task text;
  - ANY item is not independent — the WHOLE batch is refused,
    naming the dependent items. A mixed batch is never
    partially fanned: partial fanning would silently change
    what the owner asked to run, and the refusal names the
    dependent items so the owner can re-seed them as a
    separate sequential run;
  - the cap is 1 (parallel turns off) — fan-out is refused and
    the window behaves exactly as it does today: no turn
    starts, nothing is queued, byte-identical.

THE CAP: waves of at most `cap` live turns, in batch order;
excess items QUEUE in order and start as slots free — never
more than `cap` live, never a refusal for a queued item. A
pending repair turn reserves its slot (it is a turn too), so a
repair can never push the live count past the cap. On this
machine a fanned turn is a fresh context plus a model seat,
so the cap is also the memory guard: cap 4 is four resident
contexts, and the owner sets it knowing that.

THE RETURNS: each fanned turn's prompt ends with the handoff
close-out instruction (fanout_instruction), so the turn's final
text is expected to close in the M7 schema. Collection mirrors
the council path exactly (worker._council_step): parse; an
attempted-but-incomplete return gets ONE repair round (the
missing sections only); the repair reply is appended to the
original text and re-parsed; a return still incomplete after
repair lands FLAGGED with its raw text intact — content is
never dropped to gain structure. A turn that fails (or is
stopped) files a failed entry and never a handoff. A return
that never attempted the schema is flagged the same way, not
rewritten.

This module is Qt-free and model-free: the window owns
spawning (fresh conversation + send, the #64 detach dance)
and the repair turn; FanOutRun owns validation, waves, the
live/queued bookkeeping, and collection. The window drives it
from the stream settle hooks (_ws_finished / _ws_failed).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import handoff as handoff_mod


# ---------- the batch ----------

@dataclass(frozen=True)
class FanOutItem:
    """One fanned item. `independent` is the owner's assertion —
    False by default, so nothing is ever fanned on a guess. A
    plan record the owner approved may assert it instead; the
    model never does."""
    task: str
    independent: bool = False
    title: str = ""

    def label(self) -> str:
        return self.title or (self.task.strip().splitlines() or ["item"])[0][:60]


def validate_batch(items) -> str | None:
    """The batch's refusal, or None when it may fan. The whole
    batch is refused when any item is not independent — see the
    module docstring for why partial fanning is not offered."""
    items = list(items or [])
    if not items:
        return ("Nothing to fan out — the batch is empty.")
    for position, item in enumerate(items, 1):
        if not str(getattr(item, "task", "") or "").strip():
            return (f"Fan-out refused — item #{position} has no task "
                    "text, so nothing ran.")
    dependent = [item.label() for item in items
                 if not getattr(item, "independent", False)]
    if dependent:
        names = ", ".join(f"'{name}'" for name in dependent)
        return ("Fan-out refused — these items are marked dependent "
                f"or ordered: {names}. Dependent steps are never "
                "fanned; run them in order instead, or re-seed the "
                "independent items as their own batch.")
    return None


def cap_refusal(cap: int) -> str | None:
    """The cap's refusal, or None when fan-out may run. Cap 1 is
    the classic one-turn Forge: fan-out is off, and refusing
    keeps behaviour byte-identical to today."""
    try:
        value = int(cap)
    except (TypeError, ValueError):
        value = 1
    if value < 2:
        return ("Fan-out refused — parallel turns are off (cap 1). "
                "Raise Parallel turns in Settings → Seats & limits "
                "to fan items out.")
    return None


def plan_waves(items, cap: int) -> list[list[FanOutItem]]:
    """The batch cut into waves of at most `cap`, in batch order.
    Pure scheduling — validation is the caller's job."""
    items = list(items)
    waves: list[list[FanOutItem]] = []
    for start in range(0, len(items), max(1, int(cap))):
        waves.append(items[start:start + max(1, int(cap))])
    return waves


# ---------- the per-turn instruction ----------

def fanout_instruction(item: FanOutItem) -> str:
    """The item's prompt: its task, plus the handoff close-out.
    The close-out rides in the USER prompt (visible, per item),
    never in the system prompt — the M1 stable prefix is
    untouched, and a fanned turn's context is fresh either way."""
    task = str(item.task or "").strip()
    return (
        f"{task}\n\n"
        "When your work is done, close with a handoff as your "
        "final message, in exactly this format, one section per "
        "line:\n"
        "ASKED: the task as you understood it\n"
        "DONE: what you did\n"
        "FILES: the files you read, ran or changed, one per line, "
        "or (none)\n"
        "EVIDENCE: the commands you ran and what they returned, "
        "or what you saw\n"
        "OPEN: anything unfinished or risky, or (none)")


# ---------- collection (mirrors the M7 council path) ----------

def collect_handoff(raw_text, seat: str, repair=None,
                    log=None) -> dict:
    """Parse one fanned turn's return into the keeper's entry
    shape ({"seat", "parsed", "missing", "unparsed", "raw"}).
    `repair`, when given, is called once with the missing
    fields and returns the repair reply text (or nothing); the
    reply is appended to the original and re-parsed, exactly as
    the council does. A return still incomplete after repair —
    or one that never attempted the schema — lands flagged with
    its raw text intact."""
    raw = str(raw_text or "")
    parsed, missing, attempted = handoff_mod.parse_handoff(raw)
    if missing and attempted and repair is not None:
        try:
            reply = repair(missing)
        except Exception:
            reply = None
        if reply:
            raw = raw + "\n" + str(reply)
            parsed, missing, _attempted = handoff_mod.parse_handoff(raw)
            if log is not None:
                log("fanout_handoff_repaired", seat=seat, missing=missing)
    if missing and log is not None:
        log("fanout_handoff_unparsed", seat=seat, missing=missing)
    return {"seat": seat, "parsed": parsed, "missing": missing,
            "unparsed": bool(missing), "raw": raw,
            "attempted": attempted}


# ---------- the run (Qt-free driver state) ----------

class FanOutRun:
    """One fan-out's live state. The window supplies `spawn`
    (item -> handle, or None when the turn could not start) and
    drives settlements through on_settled; this class owns
    validation, waves, the live/queued bookkeeping, and
    collection. `log`, when given, takes (event, **fields).

    Handles are opaque (the window uses stream ids). A repair
    turn is spawned by the WINDOW — it owns the conversation —
    so on_settled returns a repair action for it, and the window
    reports the repair turn's handle back through note_repair.
    A repair reserves its slot from the moment the action is
    issued, so live + reserved never exceeds the cap; if the
    window cannot spawn the repair, repair_unavailable finalises
    the entry flagged, per M7."""

    def __init__(self, items, cap, spawn, log=None):
        refusal = validate_batch(items)
        if refusal is not None:
            raise ValueError(refusal)
        refusal = cap_refusal(cap)
        if refusal is not None:
            raise ValueError(refusal)
        self.items = list(items)
        self.cap = max(2, min(4, int(cap)))
        self._spawn = spawn
        self._log = log or (lambda *args, **kwargs: None)
        self._queue: list[int] = list(range(len(self.items)))
        self._live: dict[object, int] = {}       # handle -> index
        self._repairs: dict[object, int] = {}    # handle -> index
        self._raw: dict[int, str] = {}           # index -> raw awaiting repair
        self._reserved_repairs = 0
        self.results: list[dict] = []
        self.done = False

    # -- starting ------------------------------------------------
    def start(self) -> None:
        """Spawn the first wave. A spawn the window refuses
        leaves its item queued; the next settle retries it —
        unless nothing is live at all, in which case the queue
        is stalled (no settle will ever come) and _check_done
        finalises it."""
        self._fill_slots()
        self._check_done()
        self._log("fanout_started", items=len(self.items), cap=self.cap)

    def _live_count(self) -> int:
        return len(self._live) + len(self._repairs) \
            + self._reserved_repairs

    def _fill_slots(self) -> None:
        """Spawn queued items while a slot is free, in order. A
        refused spawn stops the fill — the item stays queued and
        the next settle retries it, so a refusal never loses an
        item and never spins."""
        while self._queue and self._live_count() < self.cap:
            index = self._queue[0]
            handle = self._spawn(self.items[index])
            if handle is None:
                self._log("fanout_spawn_refused",
                          item=self.items[index].label())
                break
            self._queue.pop(0)
            self._live[handle] = index

    # -- settling ------------------------------------------------
    def on_settled(self, handle, text=None, error=None,
                   is_repair: bool = False) -> dict:
        """A fanned (or repair) turn settled. Returns {"repair":
        None or {"item", "index", "instruction"}, "done": bool}.
        The repair action, when present, is the window's job to
        spawn; queued items are spawned here, through the
        injected spawn callable."""
        if is_repair:
            index = self._repairs.pop(handle, None)
            if index is None:
                return {"repair": None, "done": self.done}
            raw = self._raw.pop(index, "") + "\n" + str(text or "")
            parsed, missing, _attempted = handoff_mod.parse_handoff(raw)
            self.results.append({
                "seat": self._seat(index), "parsed": parsed,
                "missing": missing, "unparsed": bool(missing),
                "raw": raw, "attempted": True})
            self._log("fanout_item_done",
                      item=self.items[index].label(),
                      missing=missing)
        elif handle in self._live:
            index = self._live.pop(handle)
            if error is not None:
                self.results.append({
                    "seat": self._seat(index), "parsed": {},
                    "missing": list(handoff_mod.FIELDS),
                    "unparsed": True, "raw": "",
                    "status": "failed", "error": str(error)[:200]})
                self._log("fanout_item_failed",
                          item=self.items[index].label(),
                          error=str(error)[:160])
            else:
                entry = collect_handoff(text, self._seat(index),
                                        log=self._log)
                if entry["missing"] and entry["attempted"]:
                    # Attempted but incomplete: ONE repair round.
                    # The repair reserves its slot now, so the
                    # fill below cannot overrun the cap.
                    self._raw[index] = entry["raw"]
                    self._reserved_repairs += 1
                    self._fill_slots()
                    return {"repair": {
                        "item": self.items[index], "index": index,
                        "instruction": handoff_mod.repair_instruction(
                            entry["missing"])},
                        "done": False}
                self.results.append(entry)
                self._log("fanout_item_done",
                          item=self.items[index].label(),
                          missing=entry["missing"])
        else:
            return {"repair": None, "done": self.done}
        self._fill_slots()
        self._check_done()
        return {"repair": None, "done": self.done}

    def note_repair(self, handle, index: int) -> None:
        """The window spawned the repair turn for `index`; its
        handle joins the repair map and the reservation becomes
        a live turn."""
        self._reserved_repairs = max(0, self._reserved_repairs - 1)
        self._repairs[handle] = index

    def repair_unavailable(self, index: int) -> dict:
        """The repair turn could not be spawned. The handoff
        lands flagged with its raw text intact (M7's fallback),
        the reservation is released, and the queue fills on."""
        raw = self._raw.pop(index, "")
        parsed, missing, _attempted = handoff_mod.parse_handoff(raw)
        self._reserved_repairs = max(0, self._reserved_repairs - 1)
        self.results.append({
            "seat": self._seat(index), "parsed": parsed,
            "missing": missing, "unparsed": True, "raw": raw})
        self._log("fanout_repair_unavailable",
                  item=self.items[index].label(), missing=missing)
        self._fill_slots()
        self._check_done()
        return {"repair": None, "done": self.done}

    def _seat(self, index: int) -> str:
        return f"fanout:{self.items[index].label()}"

    def _check_done(self) -> None:
        if self._queue and not self._live and not self._repairs \
                and not self._reserved_repairs:
            # Stalled: items still queued, nothing in flight, and
            # no settle will ever arrive to retry them (a stage
            # turn that cannot detach, e.g. Plan). Finalise the
            # queued items as stalled — plainly, never silently
            # dropped — so the run completes instead of hanging.
            for index in self._queue:
                self.results.append({
                    "seat": self._seat(index), "parsed": {},
                    "missing": list(handoff_mod.FIELDS),
                    "unparsed": True, "raw": "",
                    "status": "stalled",
                    "error": "no fanned turn could start — the "
                             "stage turn cannot detach right now"})
                self._log("fanout_item_stalled",
                          item=self.items[index].label())
            self._queue = []
        if not self._queue and not self._live and not self._repairs \
                and not self._reserved_repairs:
            if not self.done:
                self.done = True
                self._log("fanout_done", items=len(self.results),
                          flagged=sum(1 for r in self.results
                                      if r.get("unparsed")),
                          failed=sum(1 for r in self.results
                                     if r.get("status") == "failed"))
