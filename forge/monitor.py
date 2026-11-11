"""C1 — the Watch seat: monitoring for a running local project,
integrated into the machinery that already exists (Replit-study
item C1; the owner's integration directive: INTEGRATED, never
bolted on).

Replit's monitoring design is three switches — alerts, auto-fix,
auto-republish. Forge takes exactly one of them as-is (alerts) and
adapts the second to its own approval law: a fix is PROPOSED as an
ordinary gated write_file diff through the normal approval path —
never auto-applied, never auto-fixed in the background, and never
auto-republished (that switch is refused outright, the way the
study recommends). Nothing here is a standalone watcher app:

- DETECTION rides the Run machinery's own output: ForgeWindow feeds
  every line the RunThread streams (and the run-finish event) into
  observe_line / observe_finish. This module never spawns, scrapes
  or polls anything.
- THE SEAT: monitoring is a Watch seat beside driver / tester /
  reviewer. seats.py is the roster choke point and is NOT edited:
  the seat's name lives here, its model setting is the plain
  `watch_model` setting exactly like `tester_model` /
  `reviewer_model`, the seats.json file is READ through
  seats.load_seat_file from an in-memory copy the window builds
  (the file's sanitizer drops the unknown seat name — the same
  copy-paste discipline council.py uses for its reviewer default),
  and every resolution for a governing question runs through
  seats.resolve_seat_model / governing_allowed themselves. A cloud
  (provider) model may hold the seat only under the same #69
  registry role check as the tester — the window performs it, since
  it owns the registry config. Governing-seat honesty: the Watch
  seat proposes; it never judges, vetoes, grades, or applies. A
  stock model holding it can only SUGGEST text — the owner still
  judges every proposal at the gate.
- ALERTS surface where alerts already live: the window pushes them
  into the bench activity log (ForgeWindow.log) and the phone
  deck's state ("monitor" field, rendered by the Bench screen's
  ACTIVITY card), and mirrors them into the event log as
  `monitor_alert` records.
- FIX PROPOSALS (owner-triggered ONLY — the alert's button, never
  automatic): the seated model is asked for ONE file's complete
  corrected content; the reply is parsed defensively (anything that
  is not one whole-file fix is refused in words), diffed with
  forge/diffview against the file on disk, and presented through
  ForgeWindow.request_approval — the same gate, dialog, deck
  pending card and remembered-rule machinery as any agent write.
  On approval the window applies it with the very execute_tool
  call the worker uses for write_file. Deny (or a dialog that
  never ran) writes nothing, ever.

Pure stdlib + the in-package seats/diffview imports; no Qt, no
threads, no clock of its own (the stall check is driven by the
window's existing 5-second watch tick, so a silent run is caught
without a second timer).
"""
from __future__ import annotations

import re
import time

from . import seats as seats_mod

# The seat's name. seats.SEATS stays ("driver", "tester",
# "reviewer") — the sanitizer in load_seat_file drops unknown seat
# names, so monitor_seat_config() below reads the file through a
# sanitized COPY extended with this name (see the module docstring).
WATCH_SEAT = "watch"

# The roster default when the seat has no setting / file entry, or
# its candidate is refused: the reviewer seat's default — the slow,
# careful reader is the right default shape for post-mortem work.
DEFAULT_WATCH_MODEL = seats_mod.DEFAULT_REVIEWER_MODEL

# --- detection thresholds (module constants, owner-tunable later) --
REPEAT_THRESHOLD = 3        # identical normalized error lines -> alert
STALL_SECONDS = 60.0        # no output this long, mid-run -> alert
MAX_ALERTS = 50             # the session list is a ring, like the log
MAX_EVIDENCE = 300          # chars of the raw line an alert carries
MAX_PROPOSAL_FILES = 40     # project files listed in a fix prompt

# HTTP status in a log line: "GET /x HTTP/1.1" 500, a bare
# status=500, or werkzeug-style '" 500 -'. Deliberately conservative:
# a 3-digit run that merely looks like a number is not an HTTP error.
_HTTP_STATUS_RE = re.compile(
    r"(?:HTTP/\d(?:\.\d)?[\"']?\s+|status[=:\s]+|\bcode[=:\s]+|\s\")"
    r"([45]\d\d)\b")
_TRACEBACK_MARK = "Traceback (most recent call last)"
_CRASH_RES = tuple(re.compile(p) for p in (
    r"(?:^|\s)[\w.]+Error\b",                   # ValueError:, KeyError…
    r"(?:^|\s)[\w.]+Exception\b",
    r"\bpanic:",
    r"Segmentation fault",
    r"\bfatal error\b",
    r"command not found",
    r"No such file or directory",
))
_ERROR_WORD_RE = re.compile(r"\b(error|failed|failure|exception)\b", re.I)


def _normalize(line: str) -> str:
    """The repeat-detector's key: digits collapsed, whitespace
    squeezed, case folded — 'timeout after 30s' and 'timeout after
    45s' are the same repeating failure."""
    text = re.sub(r"\d+", "#", str(line or ""))
    return re.sub(r"\s+", " ", text).strip().lower()[:200]


def classify_line(line: str) -> tuple[str, str] | None:
    """One output line -> (kind, detail) or None. Kinds: 'crash'
    (traceback or a hard error line), 'http' (an HTTP 4xx/5xx
    status). 'repeat' and 'stall' are stateful and live in
    RunMonitor, not here. Clean output -> None."""
    text = str(line or "")
    if not text.strip():
        return None
    if _TRACEBACK_MARK in text:
        return "crash", "python traceback"
    match = _HTTP_STATUS_RE.search(text)
    if match:
        return "http", f"HTTP {match.group(1)}"
    for pattern in _CRASH_RES:
        if pattern.search(text):
            return "crash", "error line"
    return None


def looks_like_error(line: str) -> bool:
    """The softer test the repeat detector counts: a line carrying
    an error word, even when it isn't a crash or an HTTP status."""
    return bool(_ERROR_WORD_RE.search(str(line or "")))


class RunMonitor:
    """One run session's watch state. The window owns exactly one,
    reset at every run start; lines and events arrive from the Run
    machinery, the stall check from the window's existing tick.

    An alert is a plain dict: {id, kind, detail, evidence, count,
    when}. `evidence` is the raw line (capped) that raised it —
    an alert without its evidence is a rumor. Repeat alerts update
    their count in place instead of stacking duplicates."""

    def __init__(self, now=time.monotonic):
        self._now = now
        self.alerts: list[dict] = []
        self._seq = 0
        self._repeat: dict[str, int] = {}
        self._repeat_alerted: set[str] = set()
        self._last_output = self._now()
        self._running = False
        self._stall_alerted = False
        self.command = ""

    # -- lifecycle (called by the window's run hooks) ---------------
    def begin(self, command: str = ""):
        self.alerts = []
        self._repeat = {}
        self._repeat_alerted = set()
        self._last_output = self._now()
        self._running = True
        self._stall_alerted = False
        self.command = str(command or "")

    def end(self):
        self._running = False

    # -- detection ---------------------------------------------------
    def observe_line(self, line: str) -> dict | None:
        """Feed one streamed output line. Returns the alert it
        raised (or the repeat alert it bumped), else None."""
        self._last_output = self._now()
        # Any line at all ends the current silent stretch: the
        # stall flag re-arms here, at the moment output resumes
        # (check_stall's own re-arm only sees sub-threshold ticks).
        self._stall_alerted = False
        text = str(line or "")
        hit = classify_line(text)
        if hit is not None:
            return self._raise(hit[0], hit[1], text)
        if looks_like_error(text):
            key = _normalize(text)
            if key:
                count = self._repeat.get(key, 0) + 1
                self._repeat[key] = count
                if count >= REPEAT_THRESHOLD:
                    if key in self._repeat_alerted:
                        alert = self._find("repeat", key)
                        if alert is not None:
                            alert["count"] = count
                        return alert
                    self._repeat_alerted.add(key)
                    return self._raise(
                        "repeat", f"same error x{count}", text,
                        key=key, count=count)
        return None

    def observe_finish(self, code, stopped: bool) -> dict | None:
        """The run ended. A non-zero exit the output never explained
        is itself the crash alert; a clean or owner-stopped run
        raises nothing."""
        self.end()
        if stopped or not code:
            return None
        if any(a["kind"] == "crash" for a in self.alerts):
            return None
        return self._raise("crash", f"exited {code}",
                           f"— exited {code}")

    def check_stall(self) -> dict | None:
        """Called on the window's existing 5 s tick while a run is
        live. One alert per silent stretch; fresh output re-arms."""
        if not self._running:
            return None
        silent = self._now() - self._last_output
        if silent >= STALL_SECONDS:
            if not self._stall_alerted:
                self._stall_alerted = True
                return self._raise(
                    "stall", f"no output for {int(silent)}s",
                    f"(no output for {int(silent)} seconds)")
            return None
        # Below the threshold the stretch is over: re-arm so the
        # NEXT silent stretch alerts again. (Fresh output also
        # re-arms via _last_output moving — this branch covers a
        # tick that lands between threshold crossings.)
        self._stall_alerted = False
        return None

    # -- alert bookkeeping -------------------------------------------
    def _find(self, kind: str, key: str):
        for alert in self.alerts:
            if alert["kind"] == kind and alert.get("_key") == key:
                return alert
        return None

    def _raise(self, kind: str, detail: str, evidence: str,
               key: str = "", count: int = 1) -> dict:
        self._seq += 1
        alert = {"id": self._seq, "kind": kind, "detail": detail,
                 "evidence": str(evidence or "")[:MAX_EVIDENCE],
                 "count": count, "when": time.strftime("%H:%M:%S")}
        if key:
            alert["_key"] = key
        self.alerts.append(alert)
        del self.alerts[:-MAX_ALERTS]
        return alert

    def active(self) -> list[dict]:
        """The deck/log view: public fields only, newest last."""
        return [{"id": a["id"], "kind": a["kind"],
                 "detail": a["detail"], "evidence": a["evidence"],
                 "count": a["count"], "when": a["when"]}
                for a in self.alerts]

    def summary_line(self) -> str:
        """The one line the bench log and the deck header share."""
        if not self.alerts:
            return ""
        latest = self.alerts[-1]
        return (f"👁 Watch: {len(self.alerts)} alert(s) this run — "
                f"latest: {latest['kind']} · {latest['detail']}")


# --- the seat ---------------------------------------------------------

def monitor_seat_config(file_config: dict | None) -> dict:
    """The seats-file entry for the Watch seat, sanitized to the
    same shape load_seat_file guarantees for the built-in seats:
    {'model': '<non-empty tag>'} or {}. The window passes the RAW
    parsed seats.json dict here (load_seat_file's sanitizer drops
    the unknown seat before this module ever sees it — extending
    seats.SEATS is a seats.py change and out of bounds), so this
    reader accepts only the exact documented shape."""
    raw = (file_config or {}).get("seats")
    if not isinstance(raw, dict):
        return {}
    entry = raw.get(WATCH_SEAT)
    if not isinstance(entry, dict):
        return {}
    model = str(entry.get("model") or "").strip()
    return {"model": model} if model else {}


def resolve_watch_model(setting: str | None, file_config: dict | None,
                        roster_setting: str | None = ""
                        ) -> tuple[str, str | None]:
    """(model, refused) under resolve_seat_model's exact contract,
    with effective_seat_model's precedence: the seats file's entry,
    then the plain setting, then the roster default — every
    candidate judged by seats.resolve_seat_model itself, so the
    guard that protects the tester and reviewer protects the Watch
    seat identically. `file_config` is the RAW parsed seats.json
    (see monitor_seat_config)."""
    entry = monitor_seat_config(file_config)
    candidate = entry.get("model") or str(setting or "").strip()
    return seats_mod.resolve_seat_model(
        candidate, DEFAULT_WATCH_MODEL, roster_setting)


# --- fix proposals ------------------------------------------------------

FIX_SYSTEM = (
    "You are the Watch seat of a local coding agent, looking at a "
    "program that misbehaved while its owner ran it. You are given "
    "the alert and the project files. Propose ONE fix: choose the "
    "single file most likely at fault and answer with its COMPLETE "
    "corrected content, in exactly this shape and nothing else:\n"
    "FIX-FILE: <project-relative path>\n"
    "```\n"
    "<the file's full corrected content>\n"
    "```\n"
    "If the evidence does not point at one file you can correct "
    "whole, answer with the single word: NONE")

_FIX_FILE_RE = re.compile(r"^FIX-FILE:\s*(\S+)\s*$", re.M)
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def build_fix_messages(alert: dict, file_listing: list[str],
                       read_file) -> list[dict]:
    """The proposal request: the alert with its evidence, the
    project listing, and the current content of the file the
    evidence names (when it names one inside the project) —
    `read_file(rel) -> str | None` is the caller's jailed reader,
    so this module never touches the filesystem itself."""
    lines = [f"Alert: {alert.get('kind', '')} — {alert.get('detail', '')}",
             f"Evidence line: {alert.get('evidence', '')}",
             "",
             "Project files:"]
    lines.extend(f"- {rel}" for rel in file_listing[:MAX_PROPOSAL_FILES])
    evidence = str(alert.get("evidence", ""))
    named = re.findall(r"[\w./\\-]+\.[A-Za-z]{1,5}\b", evidence)
    for rel in named:
        rel = rel.replace("\\", "/").lstrip("./")
        if rel in file_listing:
            content = read_file(rel)
            if content:
                lines.extend(["", f"Current content of {rel}:", "```",
                              content[:6000], "```"])
            break
    return [{"role": "system", "content": FIX_SYSTEM},
            {"role": "user", "content": "\n".join(lines)}]


def parse_fix_reply(text: str, file_listing: list[str]) -> tuple[str, str] | None:
    """(rel_path, new_content) from the seat's reply, or None.
    Deliberately strict: the reply must name ONE file from the
    project listing and carry ONE fenced block — a reply that
    rambles, names a file outside the project, or answers NONE
    earns no proposal. A bad proposal must fail here, in words,
    never become a vague diff at the gate."""
    text = str(text or "")
    if not text.strip() or text.strip().upper() == "NONE":
        return None
    head = _FIX_FILE_RE.search(text)
    fence = _FENCE_RE.search(text)
    if head is None or fence is None:
        return None
    rel = head.group(1).replace("\\", "/").lstrip("./")
    if rel not in file_listing:
        return None
    content = fence.group(1)
    if not content.strip():
        return None
    if not content.endswith("\n"):
        content += "\n"
    return rel, content
