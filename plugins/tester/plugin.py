"""Tester Seat plugin (Note 9) — the AI gets an AI.

The driver delegates a concrete test task; a second model session (the
fast seat by default) works it with file/command tools inside the active
workspace and must return evidence, not vibes. The run_tester call itself
is the approved act — the tester's inner commands run inside that
approval, bounded by cycle and wall-clock caps. The tester's inner
tools run under a Containment rooted at the workspace (S3, audit
S1-04) — file tools refuse paths outside it and a command naming an
outside path is refused before it spawns. The wall clock is hard
(audit A6): it is checked between streamed events and before every
inner command, each request and command is capped by the budget
remaining, and the shell's Stop reaches the tester through the
context's cancel event. A run that exhausts its cycles with work still
in hand reports INCOMPLETE — never a raw tool result as its "report".
Everything is logged by the shell like any other tool.

Governing seat (audit bite #3): the tester model comes from the Agent
settings ONLY, resolved through forge/seats.py. A "model" key in the
tool arguments is ignored (and the ignore is logged) — the driver does
not choose who tests it. An empty setting, or one naming a model that
is not on the governing roster, resolves to the roster's tester
default (Erik) — never to the driver/current model.
"""
import inspect
import time

from forge.ollama_client import OllamaClient
from forge.tools import TOOL_SCHEMAS, Containment, execute_tool
from forge import seats as seats_mod


def _execute_contained(name, targs, workspace, cancel_event, containment):
    """S3 (audit S1-04): the tester's inner tools run under a
    Containment rooted at the workspace — the same policy a contained
    turn passes — so a tester model's command cannot reach outside
    the workspace it is verifying. execute_tool is called with the
    containment argument wherever the loaded execute_tool accepts it
    (the real one does); a stand-in without that parameter (a test
    double — it executes nothing real) is called the classic way."""
    if "containment" in inspect.signature(execute_tool).parameters:
        return execute_tool(name, targs, workspace,
                            cancel_event=cancel_event,
                            containment=containment)
    return execute_tool(name, targs, workspace,
                        cancel_event=cancel_event)

TESTER_SYSTEM = """You are the TESTER seat of a local coding agent. Your only job is verification.
Work the test task you are given: run the commands, read the files, try the edge cases.
Do not fix anything. Do not trust claims — gather evidence with your tools.
Finish with a report whose FIRST line is exactly "VERDICT: PASS" or "VERDICT: FAIL",
then the evidence (commands run and what they returned), then any failures or risks found."""

MAX_CYCLES = 4
MAX_SECONDS = 600
TESTER_TOOLS = [s for s in TOOL_SCHEMAS
                if s["function"]["name"] in ("list_directory", "read_file", "run_command")]


def run_tester(args, ctx):
    task = str(args.get("task", "")).strip()
    if not task:
        return "Tester error: no test task given."
    # Bite #3: the driver's "model" argument carries NO authority —
    # the seat is filled from the tester SETTING, guarded by the
    # governing roster. The ignore is logged, not silent.
    ignored = str(args.get("model") or "").strip()
    if ignored and ctx.log:
        ctx.log(f"run_tester: the 'model' argument ('{ignored}') was ignored — "
                "the tester seat takes its model from Agent settings only "
                "(governing-seat rule).")
    model, refused = seats_mod.resolve_seat_model(
        ctx.tester_model, seats_mod.DEFAULT_TESTER_MODEL,
        getattr(ctx, "governing_roster", ""))
    if refused and ctx.log:
        ctx.log(f"run_tester: tester setting '{refused}' is not on the governing "
                f"roster — seat refused; {model} holds the tester seat instead.")
    # Item #53: the owner's cycle cap (Settings → Sub-agent limits,
    # settings `tester_max_cycles`) reaches the run through the
    # context, the same door as the tester model above: the shell
    # refreshes ctx.tester_max_cycles from settings on every turn.
    # A positive int wins; anything else (unset context, 0, junk)
    # keeps this seat's built-in MAX_CYCLES.
    try:
        cycles_cap = int(getattr(ctx, "tester_max_cycles", 0) or 0)
    except (TypeError, ValueError):
        cycles_cap = 0
    if cycles_cap <= 0:
        cycles_cap = MAX_CYCLES
    client = OllamaClient(ctx.base_url, timeout=1800)
    try:
        installed = [m.get("name", "") for m in client.tags()]
    except Exception:
        installed = []
    if installed and not any(seats_mod.same_model(model, name) for name in installed):
        return f"Tester error: model '{model}' is not installed. Installed: {', '.join(installed)}"
    messages = [{"role": "system", "content": TESTER_SYSTEM},
                {"role": "user", "content": task}]
    options = {"temperature": 0.2, "num_predict": 1024, "num_ctx": 8192}
    # A6: the shell's Stop reaches the tester through the context's
    # cancel event, and MAX_SECONDS is a HARD wall clock — checked at
    # cycle tops, between streamed events, and before every inner tool
    # call, not just once per cycle. A single generation or command can
    # no longer overrun the run's total budget.
    cancel_event = getattr(ctx, "cancel_event", None)
    # S3 (audit S1-04): one Containment for the whole run, rooted at
    # the workspace the tester was handed.
    containment = Containment(ctx.workspace)
    started = time.time()
    transcript = []

    def cancelled():
        return cancel_event is not None and cancel_event.is_set()

    def remaining():
        return MAX_SECONDS - (time.time() - started)

    def stopped():
        """'cancelled' / 'time' when the run must end now, else None."""
        if cancelled():
            return "cancelled"
        if remaining() <= 0:
            return "time"
        return None

    def stop_result(reason):
        if reason == "cancelled":
            head = "Tester stopped: cancelled by the owner."
        else:
            head = "Tester stopped: time budget exhausted."
        return head + "\n" + "\n".join(transcript[-6:])

    for _cycle in range(cycles_cap):
        reason = stopped()
        if reason:
            return stop_result(reason)
        # The request itself may not outlive the run's remaining budget.
        client.timeout = max(1, int(remaining()))
        chunks, tool_calls = [], []
        for event in client.chat_stream(model, messages, options, TESTER_TOOLS):
            reason = stopped()
            if reason:
                break
            message = event.get("message", {})
            if message.get("content"):
                chunks.append(message["content"])
            if message.get("tool_calls"):
                tool_calls.extend(message["tool_calls"])
        if reason:
            return stop_result(reason)
        assistant = {"role": "assistant", "content": "".join(chunks)}
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        messages.append(assistant)
        transcript.append(assistant["content"])
        if not tool_calls:
            report = assistant["content"].strip()
            return f"TESTER REPORT (model: {model})\n{report or '(tester produced no report)'}"
        for call in tool_calls:
            reason = stopped()
            if reason:
                return stop_result(reason)
            fn = call.get("function", {})
            name, targs = fn.get("name", ""), dict(fn.get("arguments", {}) or {})
            if name == "run_command":
                # One inner command may not outlive the run either: its
                # ceiling is its own request, capped by what's left.
                try:
                    requested = int(targs.get("_timeout")
                                    or targs.get("timeout") or 300)
                except (TypeError, ValueError):
                    requested = 300
                targs["_timeout"] = max(1, min(requested, int(remaining())))
            try:
                result = _execute_contained(name, targs, ctx.workspace,
                                            cancel_event, containment)
            except Exception as exc:
                result = f"Tool error: {exc}"
            transcript.append(f"[{name}] {result[:400]}")
            messages.append({"role": "tool", "tool_name": name, "content": result})
    # Cycles ran out while the tester still had tools in hand — say so,
    # honestly, instead of dressing the last tool result up as a report.
    return (f"TESTER REPORT — INCOMPLETE (model: {model}): the tester used "
            f"all {cycles_cap} cycles and was still working; no verdict was "
            f"reached.\n" + "\n".join(transcript[-6:]))


HANDLERS = {"run_tester": run_tester}
