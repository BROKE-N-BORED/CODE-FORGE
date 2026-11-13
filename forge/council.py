"""Council — the Reviewer Seat (audit fix round, bite #2).

The multi-model strategy's council, on demand only: the DRIVER works a
turn as usual; when the turn would finish (SKRIBE gate settled first),
ONE review generation runs on the REVIEWER model over the finished
work; the critique goes back to the driver, who gets exactly ONE
revision pass; the turn then ends. No debate loop — full back-and-
forth was rejected on the owner's hardware, and each extra generation
is minutes on a 2B CPU model.

This is NOT SKRIBE (Note 20): SKRIBE grades the driver's own writes,
on the session model, with parsed verdicts. The council is a different
model reading the whole finished turn and critiquing it in prose. The
reviewer is ADVISORY: it cannot veto, block, or edit anything itself.
The driver holds final say; the owner above all.

Seat integrity: the reviewer model comes from a SETTING, resolved in
exactly one place — resolve_reviewer_model() below. It is never a
tool-call argument and never the driver's choice (the tester seat's
model-from-args flaw, audit bite #3, is deliberately not repeated
here). Governing-seat enforcement (bite #3) guards this function: a
reviewer setting that is not on the forge/seats.py roster is REFUSED
and the seat falls back to the roster default — the refused model
never runs, and the refusal is reported through the on_refused hook
so the shell can log it loudly.
"""
from __future__ import annotations

from . import learnings as learnings_mod
from . import seats as seats_mod

# The reviewer seat's standing occupant: the abliterated Qwen (the
# roster's "reviewer" per forge/governor.py). A settings value — set by
# the owner in Agent settings — overrides it at resolve time.
DEFAULT_REVIEWER_MODEL = "dolphinlabs/qwen3.5-uncensored"

REVIEWER_SYSTEM = """You are the REVIEWER seat of a local coding agent — a second model \
reading the driver's finished turn with fresh eyes. Critique the work, do not redo it.

Judge against the owner's original request:
- WRONG: anything incorrect, broken, or contradicted by the evidence in the turn.
- MISSING: anything the request asked for that the turn did not deliver.
- CHANGE: what you would do differently, concretely — file, place, and why.

Be specific and brief; a working review, not an essay. Do not praise without cause.
If the work is sound, say so plainly and name the strongest point and the one
residual risk. Never claim you ran tools or checked anything yourself — you
only read the turn you were handed.

Close your review with these lines, one per line, after your prose:
ASKED: <the request as you understood it>
DONE: <what the turn did>
FILES: <files the turn touched, one per line, or (none)>
EVIDENCE: <what the receipts and answer actually show>
OPEN: <anything unfinished, risks found, or (none)>
DECISIONS: <what was decided, what was rejected and why, or (none)> — optional; include it only when the turn made a real decision"""

# Caps that keep one review inside the shared 8K context alongside the
# request: the critique is about the finished answer and the receipts,
# not a re-read of the whole conversation.
ANSWER_CAP = 6000
RECEIPT_CAP = 2000
REQUEST_CAP = 2000


def resolve_reviewer_model(setting: str | None, roster_setting: str | None = "",
                           on_refused=None) -> str:
    """THE seat resolution: the owner's reviewer_model setting if set
    AND governing-eligible (forge/seats.py), else the roster default.
    Single choke point, guarded (bite #3): a non-roster setting is
    refused — the seat resolves to DEFAULT_REVIEWER_MODEL and
    on_refused(refused_setting) fires so the caller can log the
    refusal (the worker logs a council_seat_refused event)."""
    model, refused = seats_mod.resolve_seat_model(
        setting, DEFAULT_REVIEWER_MODEL, roster_setting)
    if refused is not None and on_refused is not None:
        on_refused(refused)
    return model


def model_installed(model: str, installed: list[str]) -> bool:
    """Installed-model check via forge/seats.py's same_model: same
    repo, and tags that do not conflict — a missing tag on either
    side matches any tag. So a seat resolved as the bare repo name
    still reads installed when Ollama lists it under a real tag
    (e.g. ':2b'), while ':latest' vs ':2b' remain the different
    artifacts they are."""
    return any(seats_mod.same_model(model, str(name)) for name in installed)


def build_review_messages(request: str, answer: str, receipts: list[str],
                          reviewer_model: str,
                          workspace: str | None = None) -> list[dict]:
    """The reviewer's whole world: standing orders + the owner's
    request + what the turn did (receipts) + the driver's final answer.

    CR1 (perfection pass): when `workspace` — the turn's project —
    is threaded through and that project (or the owner globally)
    has review learnings, the same learnings block the Review mode
    prompt carries (forge/learnings.py, framing included) rides in
    the user message too, so the Council reviewer also stops
    re-raising what the owner already dismissed. workspace=None
    (every caller from before CR1) or a project with no learnings
    leaves the messages byte-identical to before."""
    done = "\n".join(receipts)[:RECEIPT_CAP] if receipts else "(no tools were used)"
    block = ""
    if workspace is not None:
        try:
            block = learnings_mod.learnings_block(workspace)
        except Exception:
            # A learnings read must never eat a review.
            block = ""
    user = (
        f"The owner asked:\n{str(request)[:REQUEST_CAP]}\n\n"
        f"What the driver's turn did (shell receipts):\n{done}\n\n"
        f"The driver's final answer:\n{str(answer)[:ANSWER_CAP]}"
        + (f"\n\n{block}" if block else "")
        + "\n\nWrite your review now: WRONG / MISSING / CHANGE, as instructed.")
    return [{"role": "system", "content": REVIEWER_SYSTEM},
            {"role": "user", "content": user}]


def revision_instruction(reviewer_model: str, critique: str) -> str:
    """The user-role continuation handed back to the driver. Framed so
    the driver answers the critique point by point — and so it knows
    this is the single revision pass, after which the turn ends."""
    return (
        f"Your reviewer ({reviewer_model}) reviewed the finished turn and said:\n\n"
        f"{critique}\n\n"
        "Revise your work and your answer accordingly — use your tools if the "
        "critique requires changes — or state briefly why a point does not apply. "
        "This is your one revision pass; when you stop, the turn ends.")
