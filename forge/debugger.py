"""The Debug capability's engine: a Python target run under the
standard library's own debugger (``python -m pdb``) in a subprocess,
driven over stdin/stdout pipes.

Why pdb and why a subprocess: Forge's runs already live in child
processes (forge/runwindow), and pdb ships with every Python — this
module adds ZERO dependencies (no debugpy requirement) and stays
Qt-free, so the engine is unit-testable without a window. The
protocol is the one pdb itself speaks: output arrives, then a
``(Pdb) `` prompt with no trailing newline when the debugger is
waiting for a command. A daemon reader thread drains the pipe into
a buffer; each command waits until the prompt (or EOF, or a
timeout) settles what the command produced.

One session drives one target. Every public method returns a plain
state dict:

    {"status": "stopped" | "finished" | "running" | "error",
     "file": str | None, "line": int | None, "function": str | None,
     "output": str,          # what this call produced, prompt-free
     "exit_code": int | None}

"stopped"  — the debugger sits at a location (file/line/function).
"finished" — the program ran to its end; the session is closed and
             the subprocess is gone (pdb restarts a finished program
             at a fresh prompt, so on the finish marker the engine
             quits pdb itself rather than leaving a zombie session
             sitting on line 1 of a program that already ended).
"running"  — a command's wait timed out with the program still
             executing; step/inspect refuse until it stops, but
             stop() always works.
"error"    — the call could not be made (no live session, target
             outside the workspace, ...). Refusals are data, not
             exceptions, at the state level; start() raises
             ValueError for a target that fails the workspace jail
             BEFORE anything is spawned, because there is no session
             to hang the error on yet.

The workspace jail mirrors forge/tools._inside's rule (resolve,
then require containment) implemented locally so this module keeps
no import beyond the stdlib. Process discipline mirrors
forge/runwindow: the child gets its own process group and stop()
kills the whole group — a stopped session never leaks a process.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

PROMPT = "(Pdb) "
# The transcript is a ring, not an archive — same reasoning as
# runwindow.MAX_OUTPUT_LINES on an 8 GB machine.
MAX_TRANSCRIPT_LINES = 2000
DEFAULT_TIMEOUT = 30.0

_LOCATION_RE = re.compile(r"^> (.+)\((\d+)\)([^\s()]+)\(\)", re.M)
_FINISHED_MARK = "The program finished and will be restarted"
_PROMPT_TAIL = b"(Pdb)"


def _kill_group(proc) -> None:
    """Kill *proc*'s whole process group, falling back to the
    process itself. A local copy of runwindow.kill_process_group's
    discipline so this module stays Qt-free."""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


def _strip_prompt(text: str) -> str:
    """Remove a trailing (Pdb) prompt from a settled chunk, leaving
    the content pdb printed before it (newline-terminated)."""
    stripped = text.rstrip()
    if stripped.endswith(PROMPT.strip()):
        stripped = stripped[: -len(PROMPT.strip())].rstrip("\n")
        return stripped + ("\n" if stripped else "")
    return text


def _finish_chunk(chunk: str) -> str:
    """A finished run's chunk, cut at the finish marker: everything
    pdb prints after it is restart chatter for a program this
    session has already closed."""
    idx = chunk.find(_FINISHED_MARK)
    if idx < 0:
        return chunk
    end = chunk.find("\n", idx)
    return chunk[: end + 1] if end >= 0 else chunk


def _state(status, output="", file=None, line=None, function=None,
           exit_code=None):
    return {"status": status, "file": file, "line": line,
            "function": function, "output": output,
            "exit_code": exit_code}


class DebugSession:
    """One live pdb session over one target script. Thread-safe:
    every command serializes on one lock, so the owner window (GUI
    thread, via its control thread) and the agent tools (worker
    thread) drive the SAME session without interleaving."""

    def __init__(self, workspace, timeout: float = DEFAULT_TIMEOUT):
        self.workspace = Path(workspace)
        self.timeout = float(timeout)
        self.target: Path | None = None
        self.breakpoints: list[tuple[Path, int]] = []
        self.proc = None
        self.transcript: list[str] = []
        self._buffer = bytearray()
        self._consumed = 0
        self._eof = False
        self._cond = threading.Condition()
        self._lock = threading.RLock()
        self._reader: threading.Thread | None = None
        self._at_prompt = False
        self._landed_prefix = ""
        self._last = _state("idle")

    # ---------- lifecycle ----------

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def state(self) -> dict:
        """The last known state (a copy — callers may annotate)."""
        return dict(self._last)

    def _resolve_target(self, target) -> Path:
        """The workspace jail: *target* (absolute, or relative to
        the workspace) must resolve INSIDE the workspace and be an
        existing Python file. Raises ValueError in plain words."""
        root = self.workspace.resolve()
        raw = Path(str(target or ""))
        candidate = raw if raw.is_absolute() else root / raw
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise ValueError(f"debug target cannot be resolved: {exc}")
        if resolved != root and root not in resolved.parents:
            raise ValueError(
                f"debug target is outside the workspace: {target}")
        if not resolved.is_file():
            raise ValueError(f"debug target is not a file: {target}")
        if resolved.suffix != ".py":
            raise ValueError(
                f"debug target is not a Python file: {target}")
        return resolved

    def start(self, target, breakpoints=()) -> dict:
        """Spawn the target under pdb, register *breakpoints* (an
        iterable of (path, line); paths jail-checked like the
        target, relative paths resolve against the workspace), and
        return the entry state — pdb stops before the first line.
        Raises ValueError for a jailed-out or missing target BEFORE
        anything spawns."""
        with self._lock:
            if self.alive:
                return _state(
                    "error",
                    "A debug session is already live for "
                    f"{self.target} — stop it before starting another.")
            resolved = self._resolve_target(target)
            checked = []
            for path, line in breakpoints or ():
                bp_file = self._resolve_target(path) \
                    if str(path) != str(resolved) else resolved
                try:
                    bp_line = int(line)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"breakpoint line is not a number: {line!r}")
                if bp_line < 1:
                    raise ValueError(
                        f"breakpoint line must be 1 or more: {line!r}")
                checked.append((bp_file, bp_line))
            self.target = resolved
            self.breakpoints = checked
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "pdb", str(resolved)],
                    cwd=str(self.workspace),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True)
            except Exception as exc:
                return _state("error", f"could not start pdb: {exc}")
            self.proc = proc
            self._buffer = bytearray()
            self._consumed = 0
            self._eof = False
            self._reader = threading.Thread(
                target=self._pump, name="forge-pdb-reader", daemon=True)
            self._reader.start()
            chunk, _timed_out = self._wait_for_prompt(self.timeout)
            if not self._at_prompt:
                # EOF (pdb died on a target it cannot run — a syntax
                # error prints and exits) or a first prompt that
                # never came: no session is left half-born.
                code = proc.poll()
                self._finish_close()
                return _state(
                    "error",
                    "pdb exited before the first prompt"
                    + (f" (exit {code})" if code is not None else "")
                    + (f": {chunk.strip()}" if chunk.strip() else "."),
                    exit_code=code)
            notes = []
            for bp_file, bp_line in checked:
                bp_chunk, _ = self._command(f"b {bp_file}:{bp_line}")
                notes.append(bp_chunk)
            output = chunk + "".join(notes)
            self._last = self._stopped_state(output)
            return dict(self._last)

    def stop(self) -> dict:
        """End the session: quit pdb if it is listening, then kill
        the whole process group regardless — no leak, ever.
        Idempotent; safe with no session live."""
        with self._lock:
            proc = self.proc
            if proc is None:
                return _state("idle", "No debug session is live.")
            if proc.poll() is None and self._at_prompt:
                try:
                    proc.stdin.write(b"q\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                try:
                    proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    pass
            _kill_group(proc)
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
            code = proc.poll()
            self.proc = None
            self._at_prompt = False
            self._last = _state("finished", "Debug session stopped.",
                                exit_code=code)
            return dict(self._last)

    # ---------- driving ----------

    def step_over(self) -> dict:
        return self._drive("n", "step over")

    def step_into(self) -> dict:
        return self._drive("s", "step into")

    def continue_(self) -> dict:
        return self._drive("c", "continue")

    def _drive(self, command: str, label: str) -> dict:
        with self._lock:
            guard = self._guard(label)
            if guard is not None:
                return guard
            prefix = self._take_landed()
            chunk, timed_out = self._command(command)
            chunk = prefix + chunk
            if _FINISHED_MARK in chunk:
                # pdb restarts a finished program, so the only exit
                # code available from here is pdb's own — the
                # program's is not recoverable through this channel,
                # and reporting pdb's 0 as the program's would be a
                # lie. exit_code stays None on this path.
                self._finish_close()
                self._last = _state("finished", _finish_chunk(chunk),
                                    exit_code=None)
                return dict(self._last)
            if timed_out and not self._at_prompt:
                self._last = _state(
                    "running",
                    chunk + ("\n" if chunk and not chunk.endswith("\n")
                             else "")
                    + f"(still running after {self.timeout:g}s — "
                      "the program has not stopped; Stop ends it.)")
                return dict(self._last)
            if self._eof or not self.alive:
                code = self.proc.poll() if self.proc else None
                self._finish_close()
                self._last = _state("finished", chunk, exit_code=code)
                return dict(self._last)
            self._last = self._stopped_state(chunk)
            return dict(self._last)

    def inspect(self, expression: str) -> str:
        """Evaluate *expression* in the current frame (pdb's ``p``)
        and return pdb's answer text, or a plain-words refusal. The
        expression is a SINGLE pdb command by construction: newlines
        (which would smuggle further commands into the channel) and
        a leading ``!`` (pdb's execute-statement escape) are refused
        — inspection reads, it does not execute statements."""
        expr = str(expression or "").strip()
        if not expr:
            return "Nothing to inspect — give an expression."
        if "\n" in expr or "\r" in expr:
            return ("Refused: an inspect expression is one line — "
                    "newlines would smuggle extra debugger commands.")
        if expr.startswith("!"):
            return ("Refused: '!' executes a statement in the frame; "
                    "inspection only evaluates expressions.")
        with self._lock:
            guard = self._guard("inspect")
            if guard is not None:
                return guard["output"]
            prefix = self._take_landed()
            chunk, _ = self._command(f"p {expr}")
            return (prefix + chunk).strip()

    def locals_text(self) -> str:
        """The current frame's locals as one bounded dict repr —
        what the window paints after a stop and what a caller reads
        to decide what to inspect next."""
        with self._lock:
            guard = self._guard("inspect")
            if guard is not None:
                return guard["output"]
            prefix = self._take_landed()
            chunk, _ = self._command(
                "p {k: repr(v) for k, v in locals().items()}")
            text = (prefix + chunk).strip()
            if len(text) > 4000:
                text = text[:4000] + "…[locals truncated]"
            return text

    # ---------- internals ----------

    def _guard(self, label: str) -> dict | None:
        """The shared precondition for driving/inspecting: a live
        session, stopped at a prompt. A stop that landed AFTER a
        caller stopped waiting (a continue that timed out, then the
        program hit a breakpoint) is absorbed here first — its text
        is stashed for the caller to prepend (see _take_landed), so
        a timed-out continue does not wedge the session. Returns an
        error/running/finished state dict when the call cannot be
        made, else None."""
        self._landed_prefix = ""
        if not self.alive:
            return _state("error",
                          f"Cannot {label}: no debug session is live.")
        if not self._at_prompt:
            landed = self._poll_prompt()
            if landed is None:
                return _state(
                    "running",
                    f"Cannot {label}: the program is still running — "
                    "it has not stopped at a breakpoint. Stop ends it.")
            if _FINISHED_MARK in landed:
                self._finish_close()
                self._last = _state("finished", _finish_chunk(landed),
                                    exit_code=None)
                return dict(self._last)
            self._last = self._stopped_state(landed)
            self._landed_prefix = landed
        return None

    def _take_landed(self) -> str:
        prefix = getattr(self, "_landed_prefix", "")
        self._landed_prefix = ""
        return prefix

    def _poll_prompt(self) -> str | None:
        """Non-blocking settle check: if the unconsumed buffer
        already ends at a (Pdb) prompt, consume and return its
        text; else None."""
        with self._cond:
            new = bytes(self._buffer[self._consumed:])
            if not new or not new.rstrip().endswith(_PROMPT_TAIL) \
                    or self._eof:
                return None
            self._consumed = len(self._buffer)
            self._at_prompt = True
            text = _strip_prompt(new.decode("utf-8", "replace"))
            self._note_transcript(text)
            return text

    def _pump(self):
        """Reader thread: drain the pipe into the buffer until EOF.
        os.read on the raw fd, not line iteration — the (Pdb)
        prompt has no trailing newline and must still arrive."""
        fd = self.proc.stdout.fileno()
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            with self._cond:
                if data:
                    self._buffer += data
                else:
                    self._eof = True
                self._cond.notify_all()
            if not data:
                return

    def _wait_for_prompt(self, timeout: float):
        """Wait until the unconsumed buffer ends at a (Pdb) prompt,
        the stream hits EOF, or *timeout* passes. Returns
        (new_text, timed_out) and consumes what it returns."""
        import time
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                new = bytes(self._buffer[self._consumed:])
                if new.rstrip().endswith(_PROMPT_TAIL) or self._eof:
                    self._consumed = len(self._buffer)
                    self._at_prompt = not self._eof and \
                        new.rstrip().endswith(_PROMPT_TAIL)
                    text = _strip_prompt(new.decode("utf-8", "replace"))
                    self._note_transcript(text)
                    return text, False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._consumed = len(self._buffer)
                    self._at_prompt = False
                    text = new.decode("utf-8", "replace")
                    self._note_transcript(text)
                    return text, True
                self._cond.wait(timeout=min(remaining, 0.25))

    def _command(self, command: str):
        """Send one pdb command and collect what it produces."""
        try:
            self.proc.stdin.write((command + "\n").encode())
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            with self._cond:
                self._eof = True
            return "", False
        self._at_prompt = False
        return self._wait_for_prompt(self.timeout)

    def _note_transcript(self, text: str):
        if not text:
            return
        self.transcript.extend(text.splitlines())
        overflow = len(self.transcript) - MAX_TRANSCRIPT_LINES
        if overflow > 0:
            del self.transcript[:overflow]

    def _stopped_state(self, chunk: str) -> dict:
        matches = _LOCATION_RE.findall(chunk)
        if matches:
            file, line, function = matches[-1]
            return _state("stopped", chunk, file=file,
                          line=int(line), function=function)
        return _state("stopped", chunk)

    def _finish_close(self):
        """The program ended (or pdb died): close the session out
        so no process or reader outlives it. Returns the child's
        exit code when one is known."""
        proc = self.proc
        if proc is None:
            return None
        if proc.poll() is None and self._at_prompt:
            try:
                proc.stdin.write(b"q\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is None:
            _kill_group(proc)
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        code = proc.poll()
        self.proc = None
        self._at_prompt = False
        return code


def parse_breakpoints(text: str, default_file) -> list:
    """Parse the surfaces' breakpoint text: one entry per line or
    comma-separated — ``12`` or ``util.py:9``. Bare numbers belong
    to *default_file*. Returns [(path_str, line_int), ...]; raises
    ValueError naming the offending entry."""
    out = []
    for piece in re.split(r"[,\n]", str(text or "")):
        piece = piece.strip()
        if not piece:
            continue
        if ":" in piece:
            name, _, line = piece.rpartition(":")
            if not name:
                raise ValueError(f"breakpoint has no file: {piece!r}")
        else:
            name, line = str(default_file), piece
        try:
            number = int(line)
        except ValueError:
            raise ValueError(
                f"breakpoint line is not a number: {piece!r}")
        if number < 1:
            raise ValueError(f"breakpoint line must be 1 or more: {piece!r}")
        out.append((name, number))
    return out


# ---------- agent tool schemas (registered by forge/main.py) ----------

DEBUG_START_SCHEMA = {
    "type": "function", "function": {
        "name": "debug_start",
        "description": ("Start a debug session: run a Python file in the "
                        "workspace under the debugger with breakpoints, "
                        "stopping at the first breakpoint hit (the "
                        "program runs to its end if none is hit). One "
                        "session at a time; requires user approval. "
                        "Follow with debug_inspect / debug_step / "
                        "debug_continue / debug_stop."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "Python file in the workspace, e.g. app.py"},
            "breakpoints": {"type": "string",
                            "description": "Optional, e.g. '12, 30' or "
                                           "'util.py:9, 12' — bare lines "
                                           "belong to path"}},
            "required": ["path"]}}}

DEBUG_STEP_SCHEMA = {
    "type": "function", "function": {
        "name": "debug_step",
        "description": ("Advance the live debug session by one step: "
                        "over the current line, or INTO a call on it "
                        "when 'into' is true. Requires user approval."),
        "parameters": {"type": "object", "properties": {
            "into": {"type": "boolean",
                     "description": "Step into a call instead of over it"}},
            "required": []}}}

DEBUG_CONTINUE_SCHEMA = {
    "type": "function", "function": {
        "name": "debug_continue",
        "description": ("Let the live debug session run to the next "
                        "breakpoint (or to the end of the program). "
                        "Requires user approval."),
        "parameters": {"type": "object", "properties": {}, "required": []}}}

DEBUG_INSPECT_SCHEMA = {
    "type": "function", "function": {
        "name": "debug_inspect",
        "description": ("Evaluate an expression in the debug session's "
                        "current frame — a variable's value, len(x), "
                        "a field. Read-only; with no expression, "
                        "returns the frame's locals."),
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string"}},
            "required": []}}}

DEBUG_STOP_SCHEMA = {
    "type": "function", "function": {
        "name": "debug_stop",
        "description": ("Stop the live debug session and end its "
                        "process. Always allowed — stopping is safe."),
        "parameters": {"type": "object", "properties": {}, "required": []}}}
