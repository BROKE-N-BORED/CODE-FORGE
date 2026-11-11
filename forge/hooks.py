"""Lifecycle hooks — Note 17.

Owner-written shell commands the SHELL fires at fixed points in the
agent loop. Prompts are requests; hooks are enforcement — the model
cannot skip them, talk its way past them, or forget them.

Events:
  pre_tool   - before a tool executes. Exit != 0 BLOCKS the call; the
               hook's output becomes the tool result the model sees.
  post_edit  - after a successful write_file. Output is appended to the
               tool result fed back to the model (this is how lint/test
               failures reach it in the same turn).
  stop       - when the model tries to end its turn. Exit != 0 means
               "not done": the output is handed back and the turn
               continues (bounded — see worker's stop-bounce guard).
  session_start - when a conversation begins. Output is logged only.

Config: a JSON list, global (settings) + per-project (.forge/hooks.json):
  [{"event": "post_edit", "command": "python3 -m py_compile {path}",
    "match": "write_file", "timeout": 60}]
{path} and {tool} in a command are replaced with the tool's target.
Hooks run in the workspace, in their own process group, and a timeout
kills the whole group — the containment rules apply to hooks too.
"""
from __future__ import annotations
import json
import os
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path

EVENTS = ("pre_tool", "post_edit", "stop", "session_start")
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 3600  # Sweep R146: a hook's longest legitimate leash
# Audit B23: hook output spools to a temporary file under a hard cap
# instead of being buffered whole in RAM by communicate() — a hook that
# spews output could otherwise eat the machine's memory before the
# 4,000-char trim ever applied. Past the cap the hook's process group
# is killed and the run reports the cap as its own outcome (125).
HOOK_OUTPUT_CAP = 4 * 1024 * 1024  # 4 MB of combined output


def load_hooks(*sources: str | Path | None) -> tuple[list[dict], list[str]]:
    """Merge hook lists from JSON files (later sources win by order).

    Returns (hooks, problems): problems holds one plain string per
    problem found — an unreadable/invalid file, a top level that is
    not a list, entries dropped by the object-with-a-command filter —
    so the caller can surface a broken config instead of it silently
    yielding no hooks forever (audit C30). A missing file is NOT a
    problem: it contributes nothing, exactly as before."""
    hooks: list[dict] = []
    problems: list[str] = []
    for source in sources:
        if not source:
            continue
        path = Path(source)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except Exception as exc:
            problems.append(f"{path}: could not be read as JSON ({exc})")
            continue
        try:
            data = json.loads(raw)
        except Exception as exc:
            problems.append(f"{path}: could not be read as JSON ({exc})")
            continue
        if not isinstance(data, list):
            problems.append(f"{path}: expected a JSON list of hooks")
            continue
        kept = [h for h in data if isinstance(h, dict) and h.get("command")]
        skipped = len(data) - len(kept)
        if skipped:
            problems.append(f"{path}: {skipped} hook entries skipped "
                            "(each hook must be an object with a 'command')")
        hooks.extend(kept)
    return hooks, problems


def matching(hooks: list[dict], event: str, tool: str | None = None) -> list[dict]:
    out = []
    for h in hooks:
        if h.get("event") != event:
            continue
        match = h.get("match")
        if match and tool is not None and match != tool:
            continue
        out.append(h)
    return out


def run_hook(hook: dict, workspace: str, tool: str = "", path: str = "",
             extra_env: dict | None = None) -> tuple[int, str]:
    """Run one hook; return (exit_code, combined output trimmed)."""
    # {path} and {tool} are substituted QUOTED: hook commands run
    # through a shell, and a path with spaces or $()/backticks in it
    # is data, not code. {tool} gets the same treatment (Sweep R137):
    # the tool name reaching run_event is the model-supplied tool-call
    # name, so an unquoted substitution was a shell-injection sink for
    # a crafted name like `x;id;#`.
    command = str(hook.get("command", "")).replace("{path}", shlex.quote(path)).replace("{tool}", shlex.quote(tool))
    try:
        timeout = int(hook.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    # Sweep R146: a zero/negative timeout killed every hook instantly
    # under a lying "timed out after -5s" message, and a huge one gave
    # a hung hook an unbounded life — out-of-range values fall back
    # to the default, like non-numeric ones.
    if not 1 <= timeout <= MAX_TIMEOUT:
        timeout = DEFAULT_TIMEOUT
    env = dict(os.environ)
    env.update({"FORGE_EVENT": str(hook.get("event", "")), "FORGE_TOOL": tool,
                "FORGE_PATH": path})
    if extra_env:
        env.update(extra_env)
    # Audit B23: output spools to a temporary file (stderr already folds
    # into stdout), polled for the timeout and the output cap — the old
    # communicate() buffered the hook's entire output in RAM.
    spool = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(command, shell=True, cwd=workspace,
                                stdout=spool, stderr=subprocess.STDOUT,
                                start_new_session=True, env=env)
        started = time.monotonic()
        timed_out = over_cap = False
        while proc.poll() is None:
            if os.fstat(spool.fileno()).st_size > HOOK_OUTPUT_CAP:
                over_cap = True
                break
            if time.monotonic() - started >= timeout:
                timed_out = True
                break
            time.sleep(0.05)
        if timed_out or over_cap:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            if over_cap:
                return 125, "hook output exceeded the 4 MB cap and was killed"
            return 124, f"hook timed out after {timeout}s and was killed"
        if os.fstat(spool.fileno()).st_size > HOOK_OUTPUT_CAP:
            # A fast producer can outrun one poll slice and exit on its
            # own past the cap — the cap outcome still applies, and the
            # spool is never read whole into RAM.
            return 125, "hook output exceeded the 4 MB cap and was killed"
        spool.seek(0)
        out = spool.read().decode("utf-8", "replace")
        return proc.returncode, out.strip()[:4000]
    finally:
        spool.close()


def run_event(hooks: list[dict], event: str, workspace: str,
              tool: str = "", path: str = "") -> list[tuple[dict, int, str]]:
    """Run all hooks for an event; return [(hook, exit_code, output)]."""
    results = []
    for hook in matching(hooks, event, tool):
        code, out = run_hook(hook, workspace, tool=tool, path=path)
        results.append((hook, code, out))
    return results
