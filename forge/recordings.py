"""Verification recordings (browser merge, 2026-11-11).

When the agent verifies its own work in the browser — the #65
loop over the Browse connector, or Forge Browser's own verify —
the session files a RECORDING artifact: the step log of what the
session actually did, in order, plus the screenshots it took,
bundled as one entry in the project's artifact index
(.forge/artifacts/index.json) that the owner can open from the
Artifacts window.

Honest naming, on the artifact's face: a recording here is a
step log + still screenshots. It is NOT a video — no engine in
Forge records motion — and the document says so, so nobody
mistakes it for one.

Filing never raises into a turn (the artifacts convention):
file_recording swallows its own failures and returns None, so a
recording that could not be written costs the verify nothing —
the verification result stands on its own either way. A FAILED
or REFUSED verification files its attempt too, marked as such:
proof of a failed attempt is still proof, and a verify loop that
only records its successes would be lying by omission.
"""
from __future__ import annotations

from datetime import datetime

from . import artifacts as artifacts_mod


def build_text(target, steps, screenshots, outcome, engine_label="") -> str:
    """The recording document itself: what was verified, how it
    ended, the steps in order, and the screenshot files (they live
    in the same .forge/artifacts/ folder as this document)."""
    lines = [f"# Browser verification recording — {target}", "",
             f"Target: {target}",
             f"Outcome: {outcome}",
             f"Recorded: {datetime.now().isoformat(timespec='seconds')}"]
    if engine_label:
        lines.append(f"Engine: {engine_label}")
    lines += ["",
              "This recording is a step log plus the screenshots "
              "listed below — it is not a video. Each step is one "
              "thing the verification session did, in the order it "
              "did it.",
              "", "## Steps", ""]
    if steps:
        for index, (label, detail) in enumerate(steps, 1):
            lines.append(f"{index}. **{label}** — {detail}"
                         if detail else f"{index}. **{label}**")
    else:
        lines.append("(No steps were recorded.)")
    lines += ["", "## Screenshots", ""]
    if screenshots:
        for name in screenshots:
            lines.append(f"- .forge/artifacts/{name}")
    else:
        lines.append("None — no screenshot was saved this session.")
    lines.append("")
    return "\n".join(lines)


def file_recording(workspace, target, steps, screenshots, outcome,
                   engine_label="") -> dict | None:
    """File the recording as one artifact and return its index
    entry — or None when filing failed, quietly: the caller is a
    verify loop mid-turn, and a recording must never break it."""
    try:
        text = build_text(target, steps, screenshots, outcome,
                          engine_label)
        return artifacts_mod.record(
            workspace, "recording",
            f"Browser verification — {target}", text)
    except Exception:
        return None
