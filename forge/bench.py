"""Forge-bench (audit D37) — a fixed, test-scored benchmark.

Fifteen small tasks, the same fifteen for every model, each scored by
a deterministic planted check (file contents, or a check script's exit
code) — never by another model's opinion. Run a seat candidate through
the bench and the seat race stops being vibes: the report carries the
score, the wall time, the cycle count, and the eval tokens per task.

HONEST CONTRACT: bench tasks run the model's commands in a THROWAWAY
folder without per-command approvals. The owner launches the bench
knowing this (the dialog says so in plain words). The model's
file/command tools run through forge.tools.execute_tool under a
Containment policy rooted at the task folder (S3, audit S1-04):
file tools refuse any path outside it, and a command whose text
names an outside path is refused before it spawns — the contained
run's guardrail rules, exactly as in a contained turn (a tool-layer
guardrail, not an OS sandbox). The planted verify commands are the
bench's own, not the model's: they run with cwd= the task folder,
outside that policy. Each task's folder is KEPT after scoring (its
path rides in the report) so the owner can inspect exactly what a
model did, pass or fail.

The loop shape follows the tester seat's proven pattern (streamed
events, caps checked between events and before every tool execution)
but stands alone — this module never imports the tester plugin.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from forge.tools import TOOL_SCHEMAS, Containment, execute_tool

MAX_CYCLES = 8
TASK_SECONDS = 240        # per-task wall cap; patchable in tests
VERIFY_TIMEOUT = 30       # seconds for one planted verify command

# The bench tool belt: the built-ins a seat needs to work a small
# task. (There is no edit_file built-in in this codebase — surgical
# edits ride write_file, exactly as they do in a real turn.)
BENCH_TOOLS = [s for s in TOOL_SCHEMAS
               if s["function"]["name"] in ("list_directory", "read_file",
                                            "write_file", "append_file",
                                            "run_command")]

BENCH_SYSTEM = """You are being benchmarked by Code Forge (forge-bench). You work ONLY in the current folder, using the tools provided. Complete the task in the user's message exactly as specified — no extra features, no refactoring beyond the ask. You may run the task's check command to verify your work. When the task is complete and verified, reply with a plain answer whose first word is DONE."""

# ------------------------------------------------------------------
# The fifteen tasks. Check scripts are planted by setup and are the
# score: a task NEVER starts already passing (the suite proves it).
# ------------------------------------------------------------------

_CHECK_GREET = '''from greet import greet

assert greet("Forge") == "Hello, Forge!"
assert greet("Kai") == "Hello, Kai!"
print("ok")
'''

_CHECK_SETTINGS = '''import json

data = json.loads(open("settings.json", encoding="utf-8").read())
assert data == {"app": "forge-bench", "retries": 3, "verbose": True}, data
print("ok")
'''

_CHECK_ADD = '''from calc import add

assert add(2, 3) == 5
assert add(-1, 1) == 0
assert add(0, 7) == 7
print("ok")
'''

_CHECK_REVERSE = '''from textutil import reverse_text

assert reverse_text("forge") == "egrof"
assert reverse_text("ab") == "ba"
assert reverse_text("") == ""
print("ok")
'''

_CHECK_SUM_TO = '''from ranges import sum_to

assert sum_to(1) == 1
assert sum_to(4) == 10
assert sum_to(10) == 55
print("ok")
'''

_CHECK_SHOUT = '''from names import shout

assert shout("forge") == "FORGE"
assert shout("Forge") == "FORGE"
print("ok")
'''

_CHECK_AREA = '''import math

from util import area

assert abs(area(1) - math.pi) < 0.0001
assert abs(area(2) - 4 * math.pi) < 0.0001
print("ok")
'''

_CHECK_PACKAGE = '''from calc.ops import add, sub

assert add(2, 3) == 5
assert sub(9, 4) == 5
print("ok")
'''

_CHECK_DESCRIBE = '''import app

assert app.describe() == "timeout=30 retries=3"
print("ok")
'''

_CHECK_PRICING = '''from pricing import total

assert total(1) == 100
assert total(3) == 300
print("ok")
'''

_VERSION_APP_BEFORE = '''APP_NAME = "forge-demo"
VERSION = "1.0.0"
AUTHOR = "bench"


def banner():
    return f"{APP_NAME} v{VERSION}"
'''

_VERSION_APP_AFTER = '''APP_NAME = "forge-demo"
VERSION = "1.1.0"
AUTHOR = "bench"


def banner():
    return f"{APP_NAME} v{VERSION}"
'''

_CONFIG_BEFORE = '''usrname = "kai"
greeting = "hello " + usrname
'''

_CONFIG_AFTER = '''username = "kai"
greeting = "hello " + username
'''

_FROZEN_PY = '''def base_price():
    return 100
'''

_PRICING_BEFORE = '''from frozen import base_price


def total(qty):
    return base_price() + qty
'''

_APP_MONOLITH = '''TIMEOUT = 30
RETRIES = 3


def describe():
    return f"timeout={TIMEOUT} retries={RETRIES}"
'''

_DATA_TXT = "alpha\nbravo\ncharlie\ndelta\necho\n"


def _check_cmd(script_name="check.py"):
    return {"kind": "command_exit", "command": f"python3 {script_name}",
            "expect": 0}


TASKS = [
    # ---- write from spec (3) ----
    {"id": "write-greet-module",
     "title": "Write a greet module from spec",
     "prompt": ("Create greet.py defining greet(name) that returns the string "
                "'Hello, <name>!' — for example greet('Forge') returns "
                "'Hello, Forge!'. The planted check.py verifies it: run "
                "`python3 check.py` and make it pass."),
     "setup": {"check.py": _CHECK_GREET},
     "verify": {"kind": "all", "checks": [
         _check_cmd(),
         {"kind": "file_contains", "path": "greet.py", "needle": "def greet"}]}},
    {"id": "write-readme-from-spec",
     "title": "Write a README from a spec",
     "prompt": ("Write README.md for a tool called pocketlint, a tiny linter "
                "for shopping lists. The README must have: a first-level "
                "heading line '# pocketlint', a sentence saying it lints "
                "shopping lists, and a '## Usage' section containing the "
                "example command 'pocketlint list.txt'. Nothing else is "
                "required — keep it short."),
     "setup": {},
     "verify": {"kind": "all", "checks": [
         {"kind": "file_contains", "path": "README.md", "needle": "# pocketlint"},
         {"kind": "file_contains", "path": "README.md", "needle": "shopping lists"},
         {"kind": "file_contains", "path": "README.md", "needle": "## Usage"},
         {"kind": "file_contains", "path": "README.md",
          "needle": "pocketlint list.txt"}]}},
    {"id": "write-settings-json",
     "title": "Write an exact settings JSON",
     "prompt": ("Create settings.json containing a JSON object with exactly "
                "these keys and values: \"app\" is \"forge-bench\", "
                "\"retries\" is 3, \"verbose\" is true. Key order does not "
                "matter. The planted check.py loads the JSON and compares "
                "the values — run `python3 check.py` to confirm."),
     "setup": {"check.py": _CHECK_SETTINGS},
     "verify": _check_cmd()},
    # ---- fix the planted bug (4) ----
    {"id": "fix-addition",
     "title": "Fix the addition bug",
     "prompt": ("calc.py's add(a, b) returns the wrong result. Read it, find "
                "the bug, and fix it so the planted check.py passes: run "
                "`python3 check.py` to confirm."),
     "setup": {"calc.py": "def add(a, b):\n    return a - b\n",
               "check.py": _CHECK_ADD},
     "verify": _check_cmd()},
    {"id": "fix-reverse",
     "title": "Fix the string reverse bug",
     "prompt": ("textutil.py's reverse_text(s) drops a character instead of "
                "reversing the whole string. Fix it so the planted check.py "
                "passes: run `python3 check.py` to confirm."),
     "setup": {"textutil.py": "def reverse_text(s):\n    return s[::-1][:-1]\n",
               "check.py": _CHECK_REVERSE},
     "verify": _check_cmd()},
    {"id": "fix-sum-range",
     "title": "Fix the off-by-one sum",
     "prompt": ("ranges.py's sum_to(n) should return 1 + 2 + ... + n, but it "
                "comes up short. Fix it so the planted check.py passes: run "
                "`python3 check.py` to confirm."),
     "setup": {"ranges.py": ("def sum_to(n):\n    total = 0\n"
                             "    for i in range(1, n):\n"
                             "        total += i\n    return total\n"),
               "check.py": _CHECK_SUM_TO},
     "verify": _check_cmd()},
    {"id": "fix-shout-case",
     "title": "Fix the shout case bug",
     "prompt": ("names.py's shout(name) should return the name in UPPERCASE, "
                "but it returns the opposite case. Fix it so the planted "
                "check.py passes: run `python3 check.py` to confirm."),
     "setup": {"names.py": "def shout(name):\n    return name.lower()\n",
               "check.py": _CHECK_SHOUT},
     "verify": _check_cmd()},
    # ---- surgical edit (3) ----
    {"id": "edit-version-bump",
     "title": "Surgical edit: bump only the version",
     "prompt": ("In app.py, change ONLY the VERSION value from \"1.0.0\" to "
                "\"1.1.0\". Every other byte of the file must stay exactly "
                "as it is."),
     "setup": {"app.py": _VERSION_APP_BEFORE},
     "verify": {"kind": "file_equals", "path": "app.py",
                "content": _VERSION_APP_AFTER}},
    {"id": "edit-add-missing-import",
     "title": "Surgical edit: add the missing import",
     "prompt": ("util.py crashes with a NameError when its area() function "
                "runs. Fix it with the smallest possible change — do not "
                "rewrite the function body. The planted check.py must pass: "
                "run `python3 check.py` to confirm."),
     "setup": {"util.py": "def area(radius):\n    return math.pi * radius * radius\n",
               "check.py": _CHECK_AREA},
     "verify": {"kind": "all", "checks": [
         _check_cmd(),
         {"kind": "file_contains", "path": "util.py", "needle": "import math"},
         {"kind": "file_contains", "path": "util.py",
          "needle": "return math.pi * radius * radius"}]}},
    {"id": "edit-rename-variable",
     "title": "Surgical edit: rename a variable",
     "prompt": ("In config.py, rename the variable usrname to username "
                "everywhere it appears. Change nothing else in the file."),
     "setup": {"config.py": _CONFIG_BEFORE},
     "verify": {"kind": "file_equals", "path": "config.py",
                "content": _CONFIG_AFTER}},
    # ---- read and report into a file (2) ----
    {"id": "report-data-summary",
     "title": "Read data.txt and write a summary report",
     "prompt": ("Read data.txt in this folder. Create report.txt containing "
                "exactly two lines: the first line is 'lines: N' where N is "
                "the number of lines in data.txt, and the second line is "
                "'first: X' where X is the first line of data.txt."),
     "setup": {"data.txt": _DATA_TXT},
     "verify": {"kind": "all", "checks": [
         {"kind": "file_contains", "path": "report.txt", "needle": "lines: 5"},
         {"kind": "file_contains", "path": "report.txt",
          "needle": "first: alpha"}]}},
    {"id": "report-largest-file",
     "title": "Find the largest file and report it",
     "prompt": ("Three files sit in this folder: small.txt, medium.txt and "
                "large.txt. Create report.txt containing the NAME of the "
                "largest of the three by byte size."),
     "setup": {"small.txt": "s" * 10, "medium.txt": "m" * 40,
               "large.txt": "l" * 120},
     "verify": {"kind": "file_contains", "path": "report.txt",
                "needle": "large.txt"}},
    # ---- multi-file change (2) ----
    {"id": "multi-calc-package",
     "title": "Create a two-file calc package",
     "prompt": ("Create a Python package named calc: calc/__init__.py (it "
                "may be empty) and calc/ops.py defining add(a, b) and "
                "sub(a, b). The planted check.py imports from calc.ops and "
                "must pass: run `python3 check.py` to confirm."),
     "setup": {"check.py": _CHECK_PACKAGE},
     "verify": _check_cmd()},
    {"id": "multi-extract-constants",
     "title": "Extract constants into their own module",
     "prompt": ("app.py holds the constants TIMEOUT and RETRIES next to its "
                "logic. Move both constants into a new module constants.py, "
                "and change app.py to import them with exactly this line: "
                "from constants import TIMEOUT, RETRIES — describe() must "
                "keep returning 'timeout=30 retries=3'. The planted "
                "check.py must pass: run `python3 check.py` to confirm."),
     "setup": {"app.py": _APP_MONOLITH, "check.py": _CHECK_DESCRIBE},
     "verify": {"kind": "all", "checks": [
         _check_cmd(),
         {"kind": "file_contains", "path": "constants.py",
          "needle": "TIMEOUT = 30"},
         {"kind": "file_contains", "path": "constants.py",
          "needle": "RETRIES = 3"},
         {"kind": "file_contains", "path": "app.py",
          "needle": "from constants import TIMEOUT, RETRIES"}]}},
    # ---- follow a hard constraint (1) ----
    {"id": "constraint-frozen-base",
     "title": "Fix pricing without touching frozen.py",
     "prompt": ("pricing.py's total(qty) is wrong: it must return "
                "base_price() times qty. frozen.py is FROZEN — do not "
                "modify it in any way, not one byte. Fix the bug in "
                "pricing.py only. The planted check.py must pass: run "
                "`python3 check.py` to confirm."),
     "setup": {"frozen.py": _FROZEN_PY, "pricing.py": _PRICING_BEFORE,
               "check.py": _CHECK_PRICING},
     "verify": {"kind": "all", "checks": [
         _check_cmd(),
         {"kind": "file_equals", "path": "frozen.py", "content": _FROZEN_PY}]}},
]

TASKS_BY_ID = {t["id"]: t for t in TASKS}

# ------------------------------------------------------------------
# Setup + verify
# ------------------------------------------------------------------


def write_setup(task: dict, root: Path) -> None:
    """Plant the task's starting files under root (created as needed)."""
    root = Path(root)
    for rel, content in task.get("setup", {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _norm_text(text: str) -> str:
    """file_equals comparison form: line endings unified, trailing
    newlines ignored — the score judges content, not whether the
    model ended the file with exactly one newline."""
    return text.replace("\r\n", "\n").rstrip("\n")


def _run_check(verify: dict, root: Path) -> tuple[bool, str]:
    """Run one verify dict against root. Returns (passed, note) —
    the note is empty on a pass and names the failing check on a
    failure, in one short line."""
    kind = verify.get("kind")
    if kind == "all":
        for check in verify.get("checks", []):
            ok, note = _run_check(check, root)
            if not ok:
                return False, note
        return True, ""
    if kind in ("file_contains", "file_equals"):
        rel = str(verify.get("path", ""))
        target = (root / rel).resolve()
        if target != root.resolve() and root.resolve() not in target.parents:
            return False, f"verify path escapes the task folder: {rel}"
        if not target.is_file():
            return False, f"{rel} was not created"
        text = target.read_text(encoding="utf-8", errors="replace")
        if kind == "file_contains":
            needle = str(verify.get("needle", ""))
            if needle not in text:
                return False, f"{rel} does not contain {needle!r}"
            return True, ""
        if _norm_text(text) != _norm_text(str(verify.get("content", ""))):
            return False, f"{rel} does not match the expected content"
        return True, ""
    if kind == "command_exit":
        command = str(verify.get("command", ""))
        expect = verify.get("expect", 0)
        try:
            proc = subprocess.run(command, shell=True, cwd=str(root),
                                  capture_output=True, text=True,
                                  timeout=VERIFY_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, f"check `{command}` timed out after {VERIFY_TIMEOUT}s"
        except Exception as exc:
            return False, f"check `{command}` could not run: {exc}"
        if proc.returncode != expect:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            detail = f" — {tail[-1][:120]}" if tail else ""
            return False, (f"check `{command}` exited {proc.returncode} "
                           f"(expected {expect}){detail}")
        return True, ""
    return False, f"unknown verify kind: {kind!r}"


def check_task(task: dict, root) -> tuple[bool, str]:
    """Score a task folder: run the task's verify against it."""
    return _run_check(task["verify"], Path(root))


# ------------------------------------------------------------------
# The runner
# ------------------------------------------------------------------

_OPTIONS = {"temperature": 0.2, "num_predict": 1024, "num_ctx": 8192}


def _stop_reason(cancel_event, deadline) -> str | None:
    if cancel_event is not None and cancel_event.is_set():
        return "cancelled"
    if time.time() >= deadline:
        return "time"
    return None


def _run_one_task(task: dict, model: str, client, cancel_event,
                  workspace_root, disabled=frozenset(),
                  max_cycles=MAX_CYCLES) -> tuple[dict, bool]:
    """Work one task in a fresh temp folder and score it. Returns
    (record, cancelled) — cancelled is True only when the owner's
    Stop ended the task, which ends the whole run."""
    # Item #52: the owner's kill switch reaches the bench too — a
    # disabled built-in leaves the belt the model is offered, and a
    # call naming one anyway is refused instead of executed, so a
    # task that needs it fails on its planted check, honestly.
    belt = [s for s in BENCH_TOOLS
            if s["function"]["name"] not in disabled]
    started = time.time()
    deadline = started + TASK_SECONDS
    record = {"id": task["id"], "title": task["title"], "passed": False,
              "seconds": 0.0, "cycles": 0, "eval_tokens": 0,
              "prompt_tokens": 0, "note": "", "workspace": ""}
    task_dir = Path(tempfile.mkdtemp(prefix=f"forge-bench-{task['id']}-",
                                     dir=workspace_root))
    record["workspace"] = str(task_dir)
    write_setup(task, task_dir)
    # S3 (audit S1-04): the model's tools run contained to this task
    # folder — the same Containment a contained turn passes — so a
    # bench model's command cannot reach outside it. The planted
    # verify commands below are the bench's own and stay outside it.
    containment = Containment(task_dir)
    messages = [{"role": "system", "content": BENCH_SYSTEM},
                {"role": "user", "content": task["prompt"]}]
    stop = None  # None (model finished) | "cancelled" | "time" | "cycles"
    # Sweep R136: the loop below rewrites client.timeout per request
    # (the per-task budget wall). The client is the window's LIVE
    # chat client, shared with the bench — its original timeout is
    # captured here and restored in the finally, or every bench run
    # left the main chat with a leftover timeout as low as 1 second.
    original_timeout = getattr(client, "timeout", None)
    try:
        while True:
            reason = _stop_reason(cancel_event, deadline)
            if reason:
                stop = reason
                break
            if record["cycles"] >= max_cycles:
                stop = "cycles"
                break
            record["cycles"] += 1
            if hasattr(client, "timeout"):
                # One request may not outlive the task's remaining
                # budget (the tester seat's hard-wall rule).
                client.timeout = max(1, int(deadline - time.time()))
            chunks, tool_calls = [], []
            for event in client.chat_stream(model, messages, _OPTIONS,
                                            belt):
                message = event.get("message", {})
                if message.get("content"):
                    chunks.append(message["content"])
                if message.get("tool_calls"):
                    tool_calls.extend(message["tool_calls"])
                if event.get("done"):
                    record["eval_tokens"] += int(event.get("eval_count") or 0)
                    record["prompt_tokens"] += int(
                        event.get("prompt_eval_count") or 0)
                reason = _stop_reason(cancel_event, deadline)
                if reason:
                    break
            if reason:
                stop = reason
                break
            assistant = {"role": "assistant", "content": "".join(chunks)}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            messages.append(assistant)
            if not tool_calls:
                break  # the model finished on its own
            for call in tool_calls:
                reason = _stop_reason(cancel_event, deadline)
                if reason:
                    stop = reason
                    break
                fn = call.get("function", {})
                name = fn.get("name", "")
                targs = dict(fn.get("arguments", {}) or {})
                if name in disabled:
                    messages.append(
                        {"role": "tool", "tool_name": name,
                         "content": (f"Tool '{name}' is disabled by the "
                                     "owner (Customize → Tools).")})
                    continue
                if name == "run_command":
                    # An inner command may not outlive the task either.
                    try:
                        requested = int(targs.get("_timeout")
                                        or targs.get("timeout") or 300)
                    except (TypeError, ValueError):
                        requested = 300
                    targs["_timeout"] = max(
                        1, min(requested, int(deadline - time.time())))
                try:
                    result = execute_tool(name, targs, str(task_dir),
                                          cancel_event=cancel_event,
                                          containment=containment)
                except Exception as exc:
                    result = f"Tool error: {exc}"
                messages.append({"role": "tool", "tool_name": name,
                                 "content": result})
            if stop:
                break
    except Exception as exc:
        # A dead server / broken client fails THIS task honestly
        # instead of killing the whole bench.
        record["note"] = f"model request failed: {exc}"[:200]
        record["seconds"] = round(time.time() - started, 1)
        return record, False
    finally:
        if original_timeout is not None:
            try:
                client.timeout = original_timeout
            except Exception:
                pass
    record["seconds"] = round(time.time() - started, 1)
    if stop == "cancelled":
        record["passed"] = None
        record["note"] = "stopped by the owner during this task"
        return record, True
    ok, vnote = check_task(task, task_dir)
    if ok:
        record["passed"] = True
        record["note"] = ""
    else:
        record["passed"] = False
        if stop == "cycles":
            record["note"] = (f"used all {max_cycles} cycles and was still "
                              f"calling tools; {vnote}")
        elif stop == "time":
            record["note"] = f"hit the {TASK_SECONDS}s wall cap; {vnote}"
        else:
            record["note"] = vnote
    return record, False


_BENCH_KEEP_FOLDERS = 150  # ten runs of fifteen tasks


def _prune_bench_workspaces(workspace_root) -> None:
    """Sweep R145: task folders are kept for inspection, but nothing
    ever reaped them — every run left ~15 more behind forever. Prune
    the bench root's forge-bench-* folders to the newest
    _BENCH_KEEP_FOLDERS by mtime. Best effort throughout: a folder
    that will not go is simply left."""
    try:
        root = (Path(workspace_root) if workspace_root
                else Path(tempfile.gettempdir()))
        folders = [p for p in root.glob("forge-bench-*") if p.is_dir()]
        if len(folders) <= _BENCH_KEEP_FOLDERS:
            return
        folders.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for old in folders[_BENCH_KEEP_FOLDERS:]:
            shutil.rmtree(old, ignore_errors=True)
    except Exception:
        pass


def run_bench(model: str, client, task_ids=None, cancel_event=None,
              on_task=None, workspace_root=None, disabled_tools=None,
              max_cycles=None) -> dict:
    """Run the bench (all fifteen tasks, or the task_ids subset)
    against model via client (an OllamaClient-shaped object) and
    return the report dict. on_task(record), when given, fires as
    each task is scored; a set cancel_event stops the run — the
    in-flight task is recorded as stopped and every remaining task
    as skipped. disabled_tools (item #52) is the owner's kill-switch
    set: those built-ins leave the bench belt and calls to them are
    refused, exactly as in a real turn. max_cycles (item #53) is
    the owner's per-task cycle cap (settings `bench_max_cycles`):
    a positive int wins; None / unset / unparseable keeps the
    MAX_CYCLES default."""
    disabled = frozenset(disabled_tools or ())
    _prune_bench_workspaces(workspace_root)
    try:
        cycles_cap = int(max_cycles)
    except (TypeError, ValueError):
        cycles_cap = 0
    if cycles_cap <= 0:
        cycles_cap = MAX_CYCLES
    run_started = time.time()
    wanted = set(task_ids) if task_ids is not None else None
    tasks = [t for t in TASKS if wanted is None or t["id"] in wanted]
    records: list[dict] = []
    stopped = cancel_event is not None and cancel_event.is_set()
    for task in tasks:
        if stopped:
            record = {"id": task["id"], "title": task["title"],
                      "passed": None, "seconds": 0.0, "cycles": 0,
                      "eval_tokens": 0, "prompt_tokens": 0,
                      "note": "skipped — the bench was stopped",
                      "workspace": ""}
        else:
            try:
                record, stopped = _run_one_task(task, model, client,
                                                cancel_event, workspace_root,
                                                disabled,
                                                max_cycles=cycles_cap)
            except Exception as exc:
                record = {"id": task["id"], "title": task["title"],
                          "passed": False, "seconds": 0.0, "cycles": 0,
                          "eval_tokens": 0, "prompt_tokens": 0,
                          "note": f"bench error: {exc}"[:200], "workspace": ""}
        records.append(record)
        if on_task is not None:
            on_task(record)
    return {"model": model,
            "started": datetime.now(timezone.utc).isoformat(),
            "tasks": records,
            "passed": sum(1 for r in records if r["passed"] is True),
            "total": len(records),
            "seconds": round(time.time() - run_started, 1)}


def format_report(report: dict) -> str:
    """The plain-text scorecard: one line per task, score footer."""
    lines = [f"Forge bench — {report.get('model', '?')}",
             f"Started {report.get('started', '')}"]
    for record in report.get("tasks", []):
        mark = ("✓" if record["passed"] is True
                else "✗" if record["passed"] is False else "–")
        line = (f"{mark} {record['id']} — {record['title']} "
                f"({record.get('seconds', 0):.1f}s, "
                f"{record.get('cycles', 0)} cycles, "
                f"{record.get('eval_tokens', 0)} eval tok)")
        if record["passed"] is not True and record.get("note"):
            line += f" — {record['note']}"
        lines.append(line)
    lines.append(f"Score: {report.get('passed', 0)}/{report.get('total', 0)} "
                 f"passed · {report.get('seconds', 0):.1f}s total")
    return "\n".join(lines)
