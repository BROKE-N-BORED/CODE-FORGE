"""Design canvas (Replit-study item C5, honest v1): a code-linked
spatial design board — the model layer. NO Qt in this module.

What the board IS: a per-project arrangement of FRAMES on a
bounded virtual board. Three frame kinds:

- ``artifact`` — a frame LINKED to a project file (a mockup, a
  page, a design document). The frame holds the path, never a
  copy of the file: the file on disk is the one truth, and the
  frame is a reference the owner can stand in front of.
- ``note`` — a note card: the owner's own text, on the board.
- ``variant`` — a frame carrying LINEAGE (``derived_from``, the
  frame it was duplicated from) plus a viewport preset label and
  an interaction-state label. The labels are metadata only — v1
  does not rewrite source to match them; they say what the
  variant is FOR, and the bench turn they can seed says the same.

What the board is NOT (v1, deliberately): an infinite canvas
(the board is BOARD_W x BOARD_H and geometry clamps to it), a
live HTML renderer (frame previews are title / path / excerpt),
an editor of the linked files (frames reference; the ordinary
tools edit), or a second filing system (the generated summary
files through the ordinary artifact path like any design text).

Persistence is one sanitized JSON document per project at
``.forge/design/canvas.json`` plus a generated
``canvas-summary.md`` beside it. Sanitization is the load path's
whole job: whatever is on disk — hand-edited, agent-written
through the ordinary file tools, or corrupt — comes back as a
board this module can stand behind, or an empty board. Junk
geometry clamps or drops; a link to a frame that does not exist
drops; a file reference that escapes the project drops (the
frame stays, unlinked). Loading never raises.

The agent reaches the board the same way it reaches everything
else: canvas.json is a project file, so the ordinary gated file
tools (read_file / write_file / edit_file) read and revise it —
there is no canvas tool and no ungated path. A board the agent
rewrites is re-sanitized on the window's next load, exactly like
a board the owner hand-edits.
"""
from __future__ import annotations

import json
from pathlib import Path

# The bounded board, in board units (pixels at zoom 1.0). Big
# enough to lay a project's surfaces side by side, small enough
# that "where did that frame go" always has an answer.
BOARD_W = 4800
BOARD_H = 3200

KINDS = ("artifact", "note", "variant")

# Variant labels: presets, never free text, so a summary and a
# bench prompt can say exactly what a variant means. The state
# label names an interaction state the variant is FOR — v1
# records the label; it does not edit source into that state.
VIEWPORTS = ("desktop", "tablet", "phone")
STATES = ("default", "hover", "active", "focus", "disabled")

MIN_W, MAX_W = 160, 2400
MIN_H, MAX_H = 110, 1600
DEFAULT_W, DEFAULT_H = 360, 260

TITLE_CAP = 120
NOTE_CAP = 4000
FILE_CAP = 400
MAX_FRAMES = 400
MAX_LINKS = 32

EXCERPT_CAP = 600


def empty_board() -> dict:
    return {"version": 1, "seq": 0, "frames": []}


def canvas_dir(workspace) -> Path:
    return Path(workspace) / ".forge" / "design"


def canvas_path(workspace) -> Path:
    return canvas_dir(workspace) / "canvas.json"


def summary_path(workspace) -> Path:
    return canvas_dir(workspace) / "canvas-summary.md"


# ---------- sanitization ----------

def _number(value, fallback):
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        return value
    return fallback


def _clamp(value, low, high):
    return max(low, min(high, value))


def _text(value, cap):
    text = str(value or "")
    return text[:cap]


def sanitize_file_ref(value):
    """A frame's linked file: a project-relative path, or "".
    Absolute paths and any ".." escape drop the reference (the
    frame itself survives, unlinked) — a canvas file is data the
    agent can write, so a reference is never trusted to stay
    inside the project on its own say-so."""
    text = str(value or "").strip().replace("\\", "/")
    if not text or len(text) > FILE_CAP:
        return ""
    if text.startswith("/") or ":" in text.split("/")[0]:
        return ""
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return ""
    return "/".join(parts)


def _sanitize_frame(raw, seen_ids):
    if not isinstance(raw, dict):
        return None
    fid = str(raw.get("id") or "").strip()
    if not fid or fid in seen_ids:
        return None
    kind = str(raw.get("kind") or "")
    if kind not in KINDS:
        kind = "note"
    w = _clamp(_number(raw.get("w"), DEFAULT_W), MIN_W, MAX_W)
    h = _clamp(_number(raw.get("h"), DEFAULT_H), MIN_H, MAX_H)
    x = _clamp(_number(raw.get("x"), 0), 0, BOARD_W - MIN_W)
    y = _clamp(_number(raw.get("y"), 0), 0, BOARD_H - MIN_H)
    viewport = str(raw.get("viewport") or "")
    if viewport not in VIEWPORTS:
        viewport = ""
    state = str(raw.get("state") or "")
    if state not in STATES:
        state = ""
    frame = {
        "id": fid,
        "kind": kind,
        "title": _text(raw.get("title"), TITLE_CAP),
        "file": sanitize_file_ref(raw.get("file")),
        "note": _text(raw.get("note"), NOTE_CAP),
        "x": int(round(x)), "y": int(round(y)),
        "w": int(round(w)), "h": int(round(h)),
        "z": _number(raw.get("z"), 0),
        "derived_from": str(raw.get("derived_from") or "").strip(),
        "viewport": viewport,
        "state": state,
        "links": raw.get("links"),
    }
    return frame


def sanitize_board(raw) -> dict:
    """Any value -> a board this module stands behind. Frames
    that aren't objects, lack an id, or duplicate an id drop;
    geometry clamps into the board; links and lineage pointing
    at frames that don't exist (or at the frame itself) drop."""
    board = empty_board()
    if not isinstance(raw, dict):
        return board
    seq = _number(raw.get("seq"), 0)
    board["seq"] = max(0, int(seq))
    raw_frames = raw.get("frames")
    if not isinstance(raw_frames, list):
        return board
    seen = set()
    frames = []
    for item in raw_frames[:MAX_FRAMES]:
        frame = _sanitize_frame(item, seen)
        if frame is None:
            continue
        seen.add(frame["id"])
        frames.append(frame)
    ids = {f["id"] for f in frames}
    for frame in frames:
        links = frame.get("links")
        clean = []
        if isinstance(links, list):
            for other in links:
                other = str(other or "").strip()
                if other in ids and other != frame["id"] \
                        and other not in clean:
                    clean.append(other)
                if len(clean) >= MAX_LINKS:
                    break
        frame["links"] = clean
        if frame["derived_from"] not in ids \
                or frame["derived_from"] == frame["id"]:
            frame["derived_from"] = ""
    # z becomes a total order: stable by the stored z, then by
    # the order the frames arrived in.
    frames.sort(key=lambda f: f["z"])
    for i, frame in enumerate(frames):
        frame["z"] = i
    board["frames"] = frames
    return board


# ---------- persistence ----------

def load_board(workspace) -> dict:
    """The project's board, sanitized. A missing or unreadable
    canvas.json is an empty board — the window opens either way."""
    try:
        text = canvas_path(workspace).read_text(encoding="utf-8")
        raw = json.loads(text)
    except Exception:
        return empty_board()
    return sanitize_board(raw)


def save_board(workspace, board) -> dict:
    """Sanitize, then write canvas.json. Returns the sanitized
    board — what was written is what the caller now holds."""
    clean = sanitize_board(board)
    folder = canvas_dir(workspace)
    folder.mkdir(parents=True, exist_ok=True)
    canvas_path(workspace).write_text(
        json.dumps(clean, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return clean


# ---------- frame operations (pure; each returns the frame) ----------

def get_frame(board, frame_id):
    for frame in board.get("frames", []):
        if frame.get("id") == frame_id:
            return frame
    return None


def _next_id(board):
    seq = int(board.get("seq") or 0)
    ids = {f["id"] for f in board.get("frames", [])}
    while True:
        seq += 1
        candidate = f"frame-{seq}"
        if candidate not in ids:
            board["seq"] = seq
            return candidate


def add_frame(board, kind, title="", file="", note="", viewport="",
              state="", derived_from="", x=None, y=None,
              w=None, h=None) -> dict:
    """Append a frame on top of the board. Position defaults to
    a cascade off the current frame count so new frames land
    visible instead of stacked exactly on the last one."""
    frames = board.setdefault("frames", [])
    count = len(frames)
    frame = {
        "id": _next_id(board),
        "kind": kind if kind in KINDS else "note",
        "title": _text(title, TITLE_CAP),
        "file": sanitize_file_ref(file),
        "note": _text(note, NOTE_CAP),
        "x": 0, "y": 0,
        "w": int(_clamp(_number(w, DEFAULT_W), MIN_W, MAX_W)),
        "h": int(_clamp(_number(h, DEFAULT_H), MIN_H, MAX_H)),
        "z": count,
        "derived_from": str(derived_from or ""),
        "viewport": viewport if viewport in VIEWPORTS else "",
        "state": state if state in STATES else "",
        "links": [],
    }
    frame["x"] = int(_clamp(
        _number(x, 40 + (count % 8) * 36), 0, BOARD_W - MIN_W))
    frame["y"] = int(_clamp(
        _number(y, 40 + (count % 8) * 36), 0, BOARD_H - MIN_H))
    frames.append(frame)
    return frame


def move_frame(board, frame_id, x, y):
    frame = get_frame(board, frame_id)
    if frame is None:
        return None
    frame["x"] = int(_clamp(_number(x, frame["x"]), 0, BOARD_W - MIN_W))
    frame["y"] = int(_clamp(_number(y, frame["y"]), 0, BOARD_H - MIN_H))
    return frame


def resize_frame(board, frame_id, w, h):
    frame = get_frame(board, frame_id)
    if frame is None:
        return None
    frame["w"] = int(_clamp(_number(w, frame["w"]), MIN_W, MAX_W))
    frame["h"] = int(_clamp(_number(h, frame["h"]), MIN_H, MAX_H))
    return frame


def rename_frame(board, frame_id, title):
    frame = get_frame(board, frame_id)
    if frame is None:
        return None
    frame["title"] = _text(title, TITLE_CAP)
    return frame


def set_note(board, frame_id, text):
    frame = get_frame(board, frame_id)
    if frame is None:
        return None
    frame["note"] = _text(text, NOTE_CAP)
    return frame


def set_file(board, frame_id, file):
    frame = get_frame(board, frame_id)
    if frame is None:
        return None
    frame["file"] = sanitize_file_ref(file)
    return frame


def set_labels(board, frame_id, viewport=None, state=None):
    """A variant's viewport / state labels — either left alone
    (None), set to a preset, or cleared ("" or any junk value:
    labels are a closed vocabulary)."""
    frame = get_frame(board, frame_id)
    if frame is None:
        return None
    if viewport is not None:
        frame["viewport"] = viewport if viewport in VIEWPORTS else ""
    if state is not None:
        frame["state"] = state if state in STATES else ""
    return frame


def set_links(board, frame_id, links):
    frame = get_frame(board, frame_id)
    if frame is None:
        return None
    ids = {f["id"] for f in board.get("frames", [])}
    clean = []
    for other in links or []:
        other = str(other or "").strip()
        if other in ids and other != frame_id and other not in clean:
            clean.append(other)
        if len(clean) >= MAX_LINKS:
            break
    frame["links"] = clean
    return frame


def delete_frame(board, frame_id):
    """Remove a frame. References to it do not dangle: links to
    it drop from every frame, and a variant whose source is gone
    keeps its labels but loses the lineage pointer."""
    frames = board.get("frames", [])
    if get_frame(board, frame_id) is None:
        return False
    board["frames"] = [f for f in frames if f["id"] != frame_id]
    for frame in board["frames"]:
        if frame_id in frame.get("links", []):
            frame["links"] = [o for o in frame["links"] if o != frame_id]
        if frame.get("derived_from") == frame_id:
            frame["derived_from"] = ""
    for i, frame in enumerate(
            sorted(board["frames"], key=lambda f: f["z"])):
        frame["z"] = i
    return True


def duplicate_as_variant(board, frame_id):
    """The variant operation: a NEW frame of kind "variant",
    linked to the same file, carrying the source's labels (or
    the defaults when the source had none), offset from the
    source, with derived_from naming the source. The source is
    never modified. A variant of a variant names its immediate
    source — the lineage is a chain the summary walks link by
    link."""
    source = get_frame(board, frame_id)
    if source is None:
        return None
    title = source.get("title") or source.get("file") or "Untitled"
    frame = add_frame(
        board, "variant",
        title=_text(f"{title} — variant", TITLE_CAP),
        file=source.get("file", ""),
        note=source.get("note", "") if source["kind"] == "note" else "",
        viewport=source.get("viewport") or "desktop",
        state=source.get("state") or "default",
        derived_from=source["id"],
        x=source["x"] + 32, y=source["y"] + 32,
        w=source["w"], h=source["h"])
    return frame


def bring_to_front(board, frame_id):
    frame = get_frame(board, frame_id)
    if frame is None:
        return None
    ordered = sorted(board["frames"], key=lambda f: f["z"])
    ordered.remove(frame)
    ordered.append(frame)
    for i, item in enumerate(ordered):
        item["z"] = i
    return frame


def send_to_back(board, frame_id):
    frame = get_frame(board, frame_id)
    if frame is None:
        return None
    ordered = sorted(board["frames"], key=lambda f: f["z"])
    ordered.remove(frame)
    ordered.insert(0, frame)
    for i, item in enumerate(ordered):
        item["z"] = i
    return frame


# ---------- reading linked files / summary / bench prompt ----------

def frame_excerpt(workspace, frame, max_chars=EXCERPT_CAP):
    """The frame's preview text: a note card previews its own
    text; a linked frame previews the head of its file. A
    missing or unreadable file previews as "" — the frame still
    names the path it points at, and the window says the file is
    missing rather than inventing content."""
    if frame.get("kind") == "note":
        return str(frame.get("note") or "")[:max_chars]
    rel = sanitize_file_ref(frame.get("file"))
    if not rel:
        return ""
    try:
        text = (Path(workspace) / rel).read_text(
            encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return text[:max_chars]


def frame_label(board, frame):
    """The short human name other surfaces use for a frame: its
    title, else its file, else its kind + id."""
    title = str(frame.get("title") or "").strip()
    if title:
        return title
    if frame.get("file"):
        return frame["file"]
    return f"{frame.get('kind', 'frame')} {frame.get('id', '')}".strip()


def summary_text(board, project_name=""):
    """The board as a markdown document: what is on it, where
    each frame points, and how the variants descend. This text
    is what canvas-summary.md holds and what files as a design
    artifact — a reading of the board, never a second board."""
    frames = sorted(board.get("frames", []), key=lambda f: f["z"])
    counts = {kind: 0 for kind in KINDS}
    for frame in frames:
        counts[frame.get("kind", "note")] = \
            counts.get(frame.get("kind", "note"), 0) + 1
    lines = [f"# Design canvas — {project_name or 'project'}", ""]
    lines.append(
        f"{len(frames)} frame(s): {counts['artifact']} artifact, "
        f"{counts['note']} note, {counts['variant']} variant. "
        f"Board {BOARD_W}x{BOARD_H}; frames reference project "
        "files — they never copy them.")
    if not frames:
        lines.append("")
        lines.append("The board is empty.")
        return "\n".join(lines) + "\n"
    by_id = {f["id"]: f for f in frames}
    for frame in frames:
        lines.append("")
        lines.append(f"## {frame_label(board, frame)}")
        meta = [f"kind: {frame['kind']}",
                f"at ({frame['x']}, {frame['y']}), "
                f"{frame['w']}x{frame['h']}, z {frame['z']}"]
        if frame.get("file"):
            meta.append(f"file: {frame['file']}")
        if frame.get("viewport"):
            meta.append(f"viewport: {frame['viewport']}")
        if frame.get("state"):
            meta.append(f"state: {frame['state']}")
        source = by_id.get(frame.get("derived_from") or "")
        if source is not None:
            meta.append(f"variant of: {frame_label(board, source)}")
        lines.append(" · ".join(meta))
        linked = [by_id[o] for o in frame.get("links", []) if o in by_id]
        if linked:
            lines.append("links: " + ", ".join(
                frame_label(board, other) for other in linked))
        if frame.get("note"):
            lines.append("")
            lines.append(frame["note"])
    return "\n".join(lines) + "\n"


def save_summary(workspace, board, project_name=""):
    """Write canvas-summary.md beside canvas.json and return
    its path. The summary is generated, never hand-kept: every
    save of the board can regenerate it, and the artifact filed
    from it is a snapshot of this text."""
    folder = canvas_dir(workspace)
    folder.mkdir(parents=True, exist_ok=True)
    path = summary_path(workspace)
    path.write_text(summary_text(board, project_name),
                    encoding="utf-8")
    return path


def compose_bench_prompt(board, frame, excerpt=""):
    """The turn "send frame to bench" composes: the frame's
    metadata, its lineage, and an excerpt of its linked source —
    a normal user turn in every respect after that. The prompt
    points at the file for the full text rather than inlining
    it: an excerpt orients, the tools fetch."""
    kind = frame.get("kind", "note")
    label = frame_label(board, frame)
    lines = [
        "[DESIGN CANVAS] The owner sent a frame from the design "
        "canvas to the bench.",
        "",
        f"Frame: {kind} — \"{label}\"",
    ]
    if frame.get("file"):
        lines.append(
            f"Linked file: {frame['file']} — read it in full "
            "before answering; the excerpt below is only its head.")
    if kind == "variant":
        source = get_frame(board, frame.get("derived_from") or "")
        if source is not None:
            lines.append(
                f"Lineage: a variant of \"{frame_label(board, source)}\""
                f" ({source.get('kind', '')})")
        labels = []
        if frame.get("viewport"):
            labels.append(f"viewport: {frame['viewport']}")
        if frame.get("state"):
            labels.append(f"state: {frame['state']}")
        if labels:
            lines.append(
                "Variant labels (the variant is FOR these; the "
                "source is not yet edited into them): "
                + ", ".join(labels))
    linked = [get_frame(board, o) for o in frame.get("links", [])]
    names = [frame_label(board, o) for o in linked if o is not None]
    if names:
        lines.append("Linked frames on the board: " + ", ".join(names))
    if frame.get("note"):
        lines.append("")
        lines.append("Frame note:")
        lines.append(frame["note"])
    if excerpt:
        lines.append("")
        lines.append("Excerpt:")
        lines.append(excerpt)
    lines.append("")
    lines.append(
        "Work with the owner on this frame: explain what it "
        "shows, and make the changes they ask for through the "
        "ordinary tools. The board itself is "
        ".forge/design/canvas.json — a project file you can read "
        "or revise with the file tools when asked.")
    return "\n".join(lines)
