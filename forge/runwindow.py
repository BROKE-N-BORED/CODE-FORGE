"""Owner-driven program runs for the Note 13A run window.

This is deliberately separate from the agent's ``run_command`` tool:
the owner types a command here and presses Start himself, so there is
no approval step. The process discipline is the same as audit bite 1,
though — a run gets its own process group and Stop kills that whole
group, so a program's children cannot outlive the run in the
background.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading

from PySide6.QtCore import QThread, Signal

# The output pane is a ring, not an archive: on an 8 GB machine a
# chatty program must not be able to grow the pane without bound.
MAX_OUTPUT_LINES = 2000


def kill_process_group(proc) -> None:
    """Kill *proc*'s whole process group, falling back to the process
    itself if the group is already gone."""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


class RunThread(QThread):
    """Runs one shell command, streaming merged stdout/stderr lines.

    Signals:
        line(str)                 — one output line, as it arrives
        run_finished(int, bool)   — (exit code, stopped by the owner)
        failed(str)               — the process could not be started
    """

    line = Signal(str)
    run_finished = Signal(int, bool)
    failed = Signal(str)

    def __init__(self, command: str, cwd: str, parent=None):
        super().__init__(parent)
        self.command = str(command)
        self.cwd = str(cwd)
        self.proc = None
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Owner's Stop: kill the entire process group, children too."""
        self._stop_event.set()
        kill_process_group(self.proc)

    def run(self):
        try:
            proc = subprocess.Popen(
                self.command, shell=True, cwd=self.cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                start_new_session=True)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.proc = proc
        if self._stop_event.is_set():
            kill_process_group(proc)
        try:
            if proc.stdout is not None:
                # Sweep R128: readline iteration, not `for raw in
                # proc.stdout` — the iterator's read-ahead can
                # withhold lines (a prompt with no newline looks
                # hung until EOF); readline delivers each line as
                # it arrives, including a final unterminated one.
                for raw in iter(proc.stdout.readline, ""):
                    self.line.emit(raw.rstrip("\r\n"))
            code = proc.wait()
        except Exception as exc:
            kill_process_group(proc)
            self.failed.emit(str(exc))
            return
        finally:
            if proc.stdout is not None:
                # Sweep R129: a failing close() must not swallow the
                # run_finished emit below — it is the only path that
                # resets the run controls.
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            self.proc = None
        self.run_finished.emit(code, self._stop_event.is_set())
