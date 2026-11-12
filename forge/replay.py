"""Session replay export (Study item M9, Tier 2 #1): a finished
session rendered as one self-contained HTML file — the C9 turn
timeline plus the chronological transcript, as it happened.

This is a RENDER of records that already exist, not new recording
machinery: messages come from the stored conversation
(ConversationStore.load) and turns from the event log's own
turn_complete records paired with the window's per-conversation
marks (forge/timeline.py). Nothing here records anything.

Honest limits, stated in the export itself:
- The timeline only covers turns finished in THIS app session:
  the window's marks live in session state, so an older session
  (or one from another app run) exports transcript-only, with a
  line saying so — never a fabricated timeline.
- Token counts are the worker's ESTIMATES, as the timeline says.
- A turn the log never closed (app closed mid-turn) has no
  closing record and does not appear.

Security: every byte of message content is html-escaped before
it lands in the file. A session can carry arbitrary markup from
files, web pages, or a hostile repo — the replay must never
execute content as HTML/JS. There are no inline scripts, no
external assets, no network references of any kind; the CSS is
inline in the file. Deterministic: the same messages + timeline
in render byte-identical output — no wall-clock timestamps inside
the render beyond what the records themselves carry.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

# Study item M9: the render caps follow the fold render's own
# conventions (context.render_span) — tool results are the
# bulkiest thing in any history, tool calls render as one compact
# line. A clipped body says so and points at the stored record.
_TOOL_RESULT_CAP = 800
_TOOL_CALL_CAP = 300

# The trajectory summary's fixed fields, in the order
# context.build_trajectory defines them (M8).
_TRAJECTORY_FIELDS = ("goal", "plan_status", "files_changed",
                      "decisions", "errors", "open_items",
                      "record_pointer")

_ROLE_LABELS = {"user": "You", "assistant": "Forge",
                "system": "System", "tool": "Tool"}


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _clip(text: str, cap: int):
    """(shown, clipped_chars): the body as rendered, and how many
    characters were cut — 0 when nothing was."""
    text = str(text or "")
    if len(text) <= cap:
        return text, 0
    return text[:cap], len(text) - cap


def _body_text(content) -> str:
    """A message's content as plain text — strings as-is, anything
    else as JSON (the same fallback storage.export_text uses)."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        return json.dumps(content, sort_keys=True)
    except Exception:
        return str(content)


def _tool_call_line(call: dict) -> str:
    """One compact line per tool call: name + argument summary."""
    fn = call.get("function") or {}
    name = fn.get("name") or call.get("name") or "?"
    try:
        args = json.dumps(fn.get("arguments") or call.get("arguments")
                          or {}, sort_keys=True)
    except Exception:
        args = str(fn.get("arguments") or "")
    args, _ = _clip(args, _TOOL_CALL_CAP)
    return f"{name}({args})"


def _field_lines(data: dict) -> str:
    """A fixed-field dict (trajectory_data) as plain lines, keys in
    the M8 order, values as compact JSON — never freeform prose."""
    lines = []
    for key in _TRAJECTORY_FIELDS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, str):
            shown = value
        else:
            try:
                shown = json.dumps(value, sort_keys=True)
            except Exception:
                shown = str(value)
        lines.append(f"{key}: {shown}")
    # Any future field renders too, after the known ones, so the
    # render never silently drops a field it doesn't know.
    for key in sorted(data):
        if key in _TRAJECTORY_FIELDS:
            continue
        lines.append(f"{key}: {data[key]}")
    return "\n".join(lines)


def _message_block(message: dict, index: int) -> str:
    """One stored message as an HTML block. Summary and trajectory
    messages render as what they are, not as ordinary chat."""
    role = str(message.get("role") or "")
    label = _ROLE_LABELS.get(role, role.title() or "Message")
    cls = f"msg {role}" if role in _ROLE_LABELS else "msg"
    parts = [f'<section class="{cls}">',
             f"<h3>{_esc(label)} <span class='n'>#{index + 1}</span></h3>"]
    if message.get("summary"):
        parts.append("<p class='kind'>Fold summary — the span it "
                     "replaced lives in the archive below, if one "
                     "was recorded.</p>")
        body, _ = _clip(_body_text(message.get("content")), _TOOL_RESULT_CAP)
        parts.append(f"<pre>{_esc(body)}</pre>")
        archive = message.get("fold_archive")
        if archive:
            parts.append("<p class='ptr'>Full earlier conversation "
                         f"archived at <code>{_esc(archive)}</code> — "
                         "read it with read_file for the exact details."
                         "</p>")
    elif message.get("trajectory"):
        parts.append("<p class='kind'>Trajectory summary — fixed "
                     "fields only, from the recorded run (M8).</p>")
        data = message.get("trajectory_data")
        if isinstance(data, dict):
            parts.append(f"<pre>{_esc(_field_lines(data))}</pre>")
        else:
            body, _ = _clip(_body_text(message.get("content")),
                            _TOOL_RESULT_CAP)
            parts.append(f"<pre>{_esc(body)}</pre>")
    else:
        calls = message.get("tool_calls") or []
        if calls:
            lines = "\n".join(_tool_call_line(c) for c in calls
                              if isinstance(c, dict))
            parts.append("<p class='kind'>Tool calls:</p>")
            parts.append(f"<pre>{_esc(lines)}</pre>")
        body = _body_text(message.get("content"))
        shown, clipped = _clip(body, _TOOL_RESULT_CAP)
        parts.append(f"<pre>{_esc(shown)}</pre>")
        if clipped:
            parts.append("<p class='ptr'>[… "
                         f"{clipped} chars clipped — the full record "
                         "is in the stored conversation.]</p>")
    parts.append("</section>")
    return "\n".join(parts)


def _timeline_rows(entries) -> str:
    rows = []
    for i, entry in enumerate(entries):
        when = str(entry.get("ended") or entry.get("started") or "")
        when = when[11:19] if len(when) >= 19 else when
        tokens = []
        if entry.get("prompt_tokens") is not None:
            tokens.append(f"≈{entry['prompt_tokens']:,} in")
        if entry.get("eval_tokens") is not None:
            tokens.append(f"≈{entry['eval_tokens']:,} out")
        seconds = (f"{entry['seconds']:,.1f}s"
                   if entry.get("seconds") is not None else "—")
        generations = (str(entry["generations"])
                       if entry.get("generations") is not None else "—")
        prompt = " ".join((entry.get("prompt") or "").split())[:60]
        rows.append(
            "<tr>"
            f"<td>{i + 1}</td>"
            f"<td>{_esc(when)}</td>"
            f"<td>{_esc(entry.get('mode') or '—')}</td>"
            f"<td>{_esc(entry.get('outcome') or 'finished')}</td>"
            f"<td>{_esc(seconds)}</td>"
            f"<td>{_esc(generations)}</td>"
            f"<td>{_esc(' / '.join(tokens) + ' (estimated)' if tokens else '—')}</td>"
            f"<td>{_esc(entry.get('model') or '—')}</td>"
            f"<td>{_esc(prompt)}</td>"
            "</tr>")
    return "\n".join(rows)


def render_replay(title, messages, entries, model=None, project=None):
    """The replay file's bytes (a str) for one conversation.

    `entries` is the C9 timeline's paired turn entries for this
    conversation (forge/timeline.py) — [] for a session with no
    timeline on record, which renders transcript-only with the
    honest line. Pure and deterministic: same inputs in,
    byte-identical output out."""
    from . import timeline as timeline_mod

    totals = timeline_mod.rollups(entries)
    date_range = ""
    stamps = [str(e.get("started") or e.get("ended") or "")
              for e in entries if e.get("started") or e.get("ended")]
    if stamps:
        date_range = f"{stamps[0][:10]} – {stamps[-1][:10]}"
    head_bits = []
    if model:
        head_bits.append(f"Model: {_esc(model)}")
    if project:
        head_bits.append(f"Project: {_esc(project)}")
    head_bits.append(f"Messages: {len(messages)}")
    if date_range:
        head_bits.append(f"Dates: {_esc(date_range)}")
    if entries:
        head_bits.append(
            f"Turns: {totals['turns']} · "
            f"≈{totals['total_tokens']:,} tokens (estimated) · "
            f"{totals['seconds']:,.1f}s · "
            f"{totals['generations']} generation(s)")
    if entries:
        timeline_html = (
            "<h2>Turn timeline</h2>"
            "<p class='note'>Token counts are the worker's estimates, "
            "as the turn timeline reports them.</p>"
            "<table><thead><tr><th>#</th><th>Ended</th><th>Mode</th>"
            "<th>Outcome</th><th>Time</th><th>Generations</th>"
            "<th>Tokens</th><th>Model</th><th>Prompt</th></tr></thead>"
            "<tbody>\n" + _timeline_rows(entries) + "\n</tbody></table>")
    else:
        timeline_html = (
            "<h2>Turn timeline</h2>"
            "<p class='note'>No timeline recorded for this session — "
            "the turn timeline only covers turns finished in this "
            "app session, so an older session replays its transcript "
            "only. A turn the log never closed does not appear.</p>")
    transcript = "\n".join(_message_block(m, i)
                           for i, m in enumerate(messages))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Session replay — {_esc(title)}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:900px;margin:2em auto;padding:0 1em;color:#1a1a1a;background:#fdfdf8}}
h1{{font-size:1.4em}}h2{{font-size:1.1em;margin-top:2em}}h3{{font-size:.95em;margin:.2em 0}}
.note{{color:#555}}.kind{{color:#555;margin:.3em 0 0}}.ptr{{color:#555;font-size:.9em}}
section.msg{{border:1px solid #ddd;border-radius:6px;padding:.6em .9em;margin:.7em 0;background:#fff}}
section.msg.user{{border-left:4px solid #888}}section.msg.assistant{{border-left:4px solid #b48fdd}}
section.msg.tool{{border-left:4px solid #4a7}}section.msg.system{{border-left:4px solid #c84}}
pre{{white-space:pre-wrap;word-wrap:break-word;margin:.4em 0}}
table{{border-collapse:collapse;width:100%;font-size:.85em}}th,td{{border:1px solid #ddd;padding:.3em .5em;text-align:left;vertical-align:top}}
.n{{color:#888;font-weight:normal}}
</style></head>
<body>
<h1>Session replay — {_esc(title)}</h1>
<p class="note">{' · '.join(head_bits)}</p>
{timeline_html}
<h2>Transcript</h2>
{transcript}
</body></html>
"""


def export_replay(workspace, conversation_id, title, messages, entries,
                  model=None, project=None):
    """Write the replay file for one conversation and return its
    path: <workspace>/.forge/replays/conversation-<id>-replay.html.
    The folder is created lazily; the filename is deterministic
    from the session id (no timestamps in the name — M1
    discipline). Raises on failure — the caller logs it."""
    folder = Path(workspace) / ".forge" / "replays"
    folder.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(ch for ch in str(conversation_id)
                      if ch.isalnum() or ch in "-_")
    path = folder / f"conversation-{safe_id or 'unknown'}-replay.html"
    path.write_text(render_replay(title, messages, entries,
                                  model=model, project=project),
                    encoding="utf-8")
    return path
