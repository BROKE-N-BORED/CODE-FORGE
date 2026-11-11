"""Structured backend event log (Forge v2, Note 2).

Everything the shell does lands here as one JSON object per line:
prompts sent, tokens streamed, tool calls with arguments and results,
approval decisions WITH how long the dialog was open, generation
durations, errors. Machine-readable on purpose — this log is how the
next version gets designed from evidence instead of memory.

Item 9 (Note 2 remainder): prompt/response capture is FULL — request
records carry the whole prompt text and generation records the whole
generated text (the char counts stay as metadata). There is no
rotation and no size cap in this writer, by design: the files grow
with use because the record is the point (Note 2 — his disk, his
data). Old files written before full capture still parse identically;
the new fields are additive.
"""
from __future__ import annotations
import json
import threading
from datetime import datetime
from pathlib import Path

from . import fileperms, scrub as scrub_mod


class EventLog:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # S2: an existing log file from before the hardening is
        # made private too — logs can carry prompts and tool text.
        fileperms.ensure_private_file(self.path)

    @property
    def path(self) -> Path:
        """Today's log file, resolved from the CURRENT date at each
        use — a session that crosses midnight rolls into the new
        day's file instead of writing on into yesterday's (and the
        summary reads the day it is actually asked about)."""
        return self.directory / f"forge-{datetime.now().strftime('%Y%m%d')}.jsonl"

    def record(self, kind: str, **fields):
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind}
        # S2 central scrub (audit S1-05): a secret that enters a
        # prompt, tool argument or tool result must not persist in
        # the log.  Fail-soft inside the scrubber — with no secrets
        # configured and no key=value pattern present, the entry is
        # byte-identical to what it has always been.
        entry.update(scrub_mod.scrub_obj(fields))
        try:
            line = json.dumps(entry, ensure_ascii=False, default=str)
        except Exception:
            return
        with self._lock:
            try:
                existed = self.path.exists()
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                if not existed:
                    # S2: log files are born private (0600).
                    fileperms.chmod_private(self.path)
            except Exception:
                pass  # logging must never break the agent

    def session_summary(self) -> str:
        """A plain-language digest of today's log, for the export button."""
        counts: dict[str, int] = {}
        approvals = {"approved": 0, "denied": 0}
        errors: list[str] = []
        tool_calls: dict[str, int] = {}
        # Sweep R154 (memory half): stream the day's file line by
        # line — the old read_text().splitlines() held the whole day
        # (full prompts and responses, by design) in RAM to count it.
        # The on-disk growth itself stays: no rotation is this log's
        # documented design (Note 2 — the record is the point).
        try:
            stream = open(self.path, encoding="utf-8")
        except Exception:
            return "No events logged yet today."
        with stream:
            for line in stream:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                counts[e.get("kind", "?")] = counts.get(e.get("kind", "?"), 0) + 1
                if e.get("kind") == "approval":
                    approvals["approved" if e.get("approved") else "denied"] += 1
                if e.get("kind") == "tool_call":
                    tool_calls[e.get("tool", "?")] = tool_calls.get(e.get("tool", "?"), 0) + 1
                if e.get("kind") == "error":
                    errors.append(str(e.get("error", ""))[:120])
        out = [f"Forge session summary — {self.path.name}", ""]
        out.append("Events: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        out.append(f"Approvals: {approvals['approved']} approved, {approvals['denied']} denied")
        if tool_calls:
            out.append("Tool calls: " + ", ".join(f"{k}×{v}" for k, v in sorted(tool_calls.items())))
        if errors:
            out.append("Errors:")
            out.extend("  - " + e for e in errors[-10:])
        return "\n".join(out)
