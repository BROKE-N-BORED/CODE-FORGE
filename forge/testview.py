"""Live testing view support — item C8 (pure stdlib, no Qt, like
palette.py).

C8 in the owner's terms: testing used to happen out of sight — the
tester seat works in the transcript and verify commands run inside
the worker — and the owner was told the result instead of shown
the proof. This module is the non-Qt half of the Testing window:
it watches the SAME run stream the Run window shows (runwindow's
RunThread lines, tapped at ForgeWindow.start_run — the view never
forks the run machinery), tallies what the streamed output itself
states, and keeps the session's run history.

The tally's honesty rule: it counts only what a runner's output
states in a form this module recognizes — unittest's verbose
per-test verdicts and its `Ran N` / `OK` / `FAILED (...)` summary
lines, pytest's `[ NN%]` progress fields, per-test verdicts and
its closing counts line. Unrecognized output simply streams: no
tally, and the finished run's verdict is the exit code the shell
reported, never a pass/fail invented from prose. When a runner
states a summary, the summary REPLACES the running tally — the
runner's own final numbers outrank a count accumulated mid-stream.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

# --- unittest signals -------------------------------------------------
# Verbose mode prints one line per test: "test_x (mod.Class) ... ok".
_UNITTEST_VERBOSE = re.compile(r"\.\.\.\s+(ok|FAIL|ERROR|skipped)\b")
# The closing lines: "Ran 14 tests in 0.032s", then "OK" (possibly
# "OK (skipped=2)") or "FAILED (failures=1, errors=2, skipped=1)".
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in [\d.]+s")
_UNITTEST_OK = re.compile(r"^OK\b")
_UNITTEST_FAILED = re.compile(r"^FAILED\s*\((.*)\)\s*$")
_COUNT_PART = re.compile(r"(\w+)=(\d+)")

# --- pytest signals ---------------------------------------------------
# Progress lines carry the test FILE, a field of result characters
# and the "[ NN%]" marker: "tests/test_x.py ....F.. [ 50%]". The
# .py file plus the marker are what make the line pytest's — a
# bare row of dots from some other tool's progress bar is not
# claimed (a fixture in the tests pins exactly that). Verbose mode
# prints one verdict per test instead:
# "tests/test_x.py::test_a PASSED [ 10%]".
_PYTEST_PROGRESS = re.compile(r"^(?:\S+\.py(?:::\S+)?)\s+([.FEsxX]+)\s+"
                              r"\[\s*\d+%\]\s*$")
_PYTEST_VERBOSE = re.compile(r"::\S+\s+(PASSED|FAILED|ERROR|SKIPPED)\s*"
                             r"(\[\s*\d+%\])?\s*$")
# The closing counts line: "===== 2 passed, 1 failed in 0.31s ====="
# (the banner rules vary; the "in N.NNs" tail plus at least one
# stated count is the claim).
_PYTEST_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped|"
                           r"xfailed|xpassed|deselected)")
_PYTEST_TIME_TAIL = re.compile(r"\bin [\d.]+s\s*$")


class TestTally:
    """Running counts for one streamed run, fed line by line.

    `recognized` stays False until a line matches a runner signal —
    the view shows no tally at all before that, so a program that
    is not a test runner never grows test numbers. Once a runner
    states its summary, `_summarized` locks the counts to the
    summary's own numbers and later lines no longer move them."""

    def __init__(self):
        self.recognized = False
        self.run = 0
        self.failures = 0
        self.errors = 0
        self.skipped = 0
        self.summary_line = ""
        self._summarized = False

    def feed(self, line: str) -> None:
        text = str(line).rstrip("\n")
        stripped = text.strip()
        if not stripped:
            return
        ran = _UNITTEST_RAN.match(stripped)
        if ran:
            self.recognized = True
            self._summarized = True
            self.run = int(ran.group(1))
            return
        failed = _UNITTEST_FAILED.match(stripped)
        if failed:
            self.recognized = True
            self._summarized = True
            self.summary_line = stripped
            for name, num in _COUNT_PART.findall(failed.group(1)):
                if name == "failures":
                    self.failures = int(num)
                elif name == "errors":
                    self.errors = int(num)
                elif name == "skipped":
                    self.skipped = int(num)
            return
        if _UNITTEST_OK.match(stripped):
            self.recognized = True
            self._summarized = True
            self.summary_line = stripped
            part = dict(_COUNT_PART.findall(stripped))
            if "skipped" in part:
                self.skipped = int(part["skipped"])
            return
        if self._pytest_summary(stripped):
            return
        if self._summarized:
            return
        verbose = _UNITTEST_VERBOSE.search(text)
        if verbose:
            self.recognized = True
            self.run += 1
            word = verbose.group(1)
            if word == "FAIL":
                self.failures += 1
            elif word == "ERROR":
                self.errors += 1
            elif word == "skipped":
                self.skipped += 1
            return
        progress = _PYTEST_PROGRESS.match(stripped)
        if progress:
            self.recognized = True
            chars = progress.group(1)
            self.run += len(chars)
            self.failures += chars.count("F")
            self.errors += chars.count("E")
            self.skipped += chars.count("s")
            return
        verdict = _PYTEST_VERBOSE.search(text)
        if verdict:
            self.recognized = True
            self.run += 1
            word = verdict.group(1)
            if word == "FAILED":
                self.failures += 1
            elif word == "ERROR":
                self.errors += 1
            elif word == "SKIPPED":
                self.skipped += 1

    def _pytest_summary(self, stripped: str) -> bool:
        body = stripped.strip("= ").strip()
        if not _PYTEST_TIME_TAIL.search(body):
            return False
        counts = {name.rstrip("s"): int(num)
                  for num, name in _PYTEST_COUNT.findall(body)}
        if not counts and "no tests ran" not in body:
            return False
        self.recognized = True
        self._summarized = True
        self.summary_line = stripped
        passed = counts.get("passed", 0)
        self.failures = counts.get("failed", 0)
        self.errors = counts.get("error", 0)
        self.skipped = counts.get("skipped", 0)
        self.run = (passed + self.failures + self.errors + self.skipped
                    + counts.get("xfailed", 0) + counts.get("xpassed", 0))
        return True

    def snapshot(self) -> dict:
        return {"recognized": self.recognized, "run": self.run,
                "failures": self.failures, "errors": self.errors,
                "skipped": self.skipped,
                "summary_line": self.summary_line}

    def tally_text(self) -> str:
        """The live tally as the view shows it — empty until the
        output itself has stated test results."""
        if not self.recognized:
            return ""
        parts = [f"{self.run} run"]
        if self.failures:
            parts.append(f"{self.failures} failed")
        if self.errors:
            parts.append(f"{self.errors} error" + ("s" if self.errors != 1 else ""))
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        return " · ".join(parts)


def run_verdict(record: dict) -> str:
    """The finished run's verdict, from the record alone: a start
    failure and an owner stop say so in words; a runner's own
    summary line outranks everything else; otherwise the exit code
    the shell reported — the only verdict left that is stated, not
    inferred."""
    if record.get("error"):
        return f"could not start: {record['error']}"
    if record.get("stopped"):
        return "stopped"
    summary = (record.get("tally") or {}).get("summary_line", "")
    if summary:
        return summary
    return f"exit {record.get('exit_code')}"


def history_line(record: dict) -> str:
    """One session-history line for a finished run."""
    parts = [record["wall"].strftime("%H:%M:%S"), record["command"]]
    parts.append(run_verdict(record))
    duration = record.get("duration_s")
    if duration is not None:
        parts.append(f"{duration:.1f}s")
    tally = record.get("tally") or {}
    if tally.get("recognized"):
        text = _tally_text(tally)
        if text:
            parts.append(text)
    return " · ".join(parts)


def _tally_text(snapshot: dict) -> str:
    """The tally text for a finished run's snapshot — built through
    the live tally's own formatter, so a finished run reads exactly
    as it did while it ran."""
    tally = TestTally()
    tally.recognized = snapshot.get("recognized", False)
    tally.run = snapshot.get("run", 0)
    tally.failures = snapshot.get("failures", 0)
    tally.errors = snapshot.get("errors", 0)
    tally.skipped = snapshot.get("skipped", 0)
    tally.summary_line = snapshot.get("summary_line", "")
    return tally.tally_text()


class TestRunLog:
    """The Testing view's session model: the one live run (Forge
    runs at most one RunThread at a time) plus the finished runs'
    history for this session. Pure state — the window renders it."""

    def __init__(self):
        self.history: list[dict] = []
        self.current: dict | None = None

    def note_started(self, command: str, cwd: str) -> None:
        self.current = {"command": str(command), "cwd": str(cwd),
                        "wall": datetime.now(),
                        "mono": time.monotonic(),
                        "tally": TestTally()}

    def note_line(self, text: str) -> None:
        if self.current is not None:
            self.current["tally"].feed(text)

    def note_finished(self, exit_code: int, stopped: bool) -> dict | None:
        return self._settle(exit_code=exit_code, stopped=stopped,
                            error="")

    def note_failed(self, message: str) -> dict | None:
        return self._settle(exit_code=None, stopped=False,
                            error=str(message))

    def _settle(self, exit_code, stopped, error) -> dict | None:
        if self.current is None:
            return None
        record = {"command": self.current["command"],
                  "cwd": self.current["cwd"],
                  "wall": self.current["wall"],
                  "duration_s": time.monotonic() - self.current["mono"],
                  "exit_code": exit_code, "stopped": bool(stopped),
                  "error": error,
                  "tally": self.current["tally"].snapshot()}
        self.history.append(record)
        self.current = None
        return record

    def live_tally_text(self) -> str:
        if self.current is None:
            return ""
        return self.current["tally"].tally_text()


def resolve_test_command(workspace: str, settings: dict | None):
    """The project's configured test command and where it came
    from: (command, source) with source "project", "settings" or
    None (nothing configured anywhere).

    Mirrors ForgeWindow._turn_config's verify resolution — the
    SAME configuration the worker verifies with, no second store:
    the project's .forge/verify.json wins. Its "test" key is the
    test command outright; otherwise the project's checks (lint,
    then commands) run in order, joined with " && " so the one run
    thread runs them all in one shell, each gating the next. With
    no project config, the settings verify_commands list is the
    default, joined the same way."""
    verify: list[str] = []
    source = None
    data = None
    try:
        data = json.loads(
            (Path(workspace) / ".forge" / "verify.json").read_text(
                encoding="utf-8"))
    except Exception:
        data = None
    if isinstance(data, dict):
        if data.get("test"):
            return str(data["test"]), "project"
        verify = [str(data[k]) for k in ("lint",) if data.get(k)]
        if isinstance(data.get("commands"), list):
            verify += [str(c) for c in data["commands"]]
        if verify:
            source = "project"
    if not verify:
        verify = [str(c) for c in
                  ((settings or {}).get("verify_commands") or [])]
        if verify:
            source = "settings"
    if not verify:
        return "", None
    return " && ".join(verify), source
