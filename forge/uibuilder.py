"""Visual UI builder — Note 13B (pure stdlib, no Qt, like planning.py).

The layout half of the note: a design canvas where the owner places
basic widgets, a live preview of the result as a real window, and an
export that writes the design as a standalone, runnable PySide6 file.
What you lay out is literally what runs — same types, same names,
same text, same geometry, in the preview and in the exported file.

This module owns everything about that which is not Qt: the design
dict, the placement/move/edit operations, the JSON round-trip (the
owner saves a design into his project as a .json he owns), the
defensive load, and the code generation. The canvas, the dialog and
the preview window — the Qt side — live in forge/main.py, where the
tree keeps its dialogs.

The design dict:
    {"version": 1, "title": str, "width": int, "height": int,
     "widgets": [{"type", "name", "text", "x", "y", "w", "h",
                  "style": {...}}, ...]}

An entry of type "svg" also carries "svg": the imported file's text,
so the design stays one portable artifact and the export one file.

Every entry carries a "style" dict — the per-widget properties the
inspector edits: foreground/background colors, font (family, size,
bold, italic), border (width, style, color) and padding. An empty
string / zero means "unset" (the widget keeps its stock look), which
is also what designs saved before styling existed get on load: the
defensive load fills the defaults in, so styling never breaks an
old design.

Widget types are the builder's basic parts plus "svg". Names are Python
identifiers, unique within a design — the exported file uses them as
attribute names verbatim, so the sanitize path enforces both rules.
"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

# type key -> (Qt class, default text, default width, default height)
# Text semantics per type live in _text_lines()/the preview builder:
# most types show it via setText; a group box uses it as its title, a
# combo box reads it as a comma-separated item list, and the numeric
# types (spin box, slider, progress bar) read it as their value.
WIDGET_TYPES = {
    "label": ("QLabel", "Label", 120, 24),
    "button": ("QPushButton", "Button", 110, 32),
    "line_edit": ("QLineEdit", "", 180, 30),
    "text_edit": ("QTextEdit", "", 220, 90),
    "checkbox": ("QCheckBox", "Check me", 130, 26),
    "radio_button": ("QRadioButton", "Option", 130, 26),
    "combo_box": ("QComboBox", "One,Two,Three", 170, 30),
    "spin_box": ("QSpinBox", "", 100, 30),
    "slider": ("QSlider", "", 170, 24),
    "progress_bar": ("QProgressBar", "50", 190, 24),
    "group_box": ("QGroupBox", "Group", 240, 150),
    "h_line": ("QFrame", "", 200, 10),
    "v_line": ("QFrame", "", 10, 140),
    "svg": ("QSvgWidget", "", 120, 120),
}
DEFAULT_WIDTH = 480
DEFAULT_HEIGHT = 360
MIN_WINDOW = 80
MAX_WINDOW = 4000
MAX_SVG_CHARS = 200_000   # an imported SVG's text rides inside the design

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# --- per-widget styling -------------------------------------------------
# A style dict: colors are "" (unset) or a #rgb/#rrggbb hex string;
# font_size 0, border_width 0 and padding 0 all mean unset. The same
# dict drives the canvas painting, the live preview and the exported
# file, so the three can never disagree about how a design looks.
BORDER_STYLES = ("none", "solid", "dashed", "dotted")
STYLE_KEYS = ("fg", "bg", "font_family", "font_size", "bold", "italic",
              "border_width", "border_style", "border_color", "padding")
MAX_FONT_SIZE = 72
MAX_BORDER_WIDTH = 24
MAX_PADDING = 64
# Border shorthand/CSS needs a color; a width with no color picked
# renders in this neutral tone on the canvas, in the preview and in
# the export alike.
BORDER_FALLBACK_COLOR = "#000000"


def default_style() -> dict:
    return {"fg": "", "bg": "", "font_family": "", "font_size": 0,
            "bold": False, "italic": False, "border_width": 0,
            "border_style": "solid", "border_color": "", "padding": 0}


def valid_color(value) -> bool:
    """'' (unset) or a #rgb/#rrggbb hex string."""
    return value == "" or bool(
        isinstance(value, str) and _COLOR_RE.match(value))


def _clean_color(value) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value.lower() if _COLOR_RE.match(value) else ""


def _clean_int(value, ceiling: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(ceiling, value))


def _clean_family(value) -> str:
    # A family name rides into CSS and generated code; strip the
    # characters that could break out of either.
    if not isinstance(value, str):
        return ""
    return re.sub(r"""[{};"']""", "", value).strip()[:80]


def _clean_style(raw) -> dict:
    """A full, valid style dict from anything: missing keys, garbage
    values and designs saved before styling existed all land on the
    defaults. Idempotent — cleaning a clean style returns it equal."""
    style = default_style()
    if not isinstance(raw, dict):
        return style
    style["fg"] = _clean_color(raw.get("fg"))
    style["bg"] = _clean_color(raw.get("bg"))
    style["border_color"] = _clean_color(raw.get("border_color"))
    style["font_family"] = _clean_family(raw.get("font_family"))
    style["font_size"] = _clean_int(raw.get("font_size"), MAX_FONT_SIZE)
    style["border_width"] = _clean_int(raw.get("border_width"),
                                        MAX_BORDER_WIDTH)
    style["padding"] = _clean_int(raw.get("padding"), MAX_PADDING)
    style["bold"] = bool(raw.get("bold"))
    style["italic"] = bool(raw.get("italic"))
    border_style = str(raw.get("border_style", "solid"))
    style["border_style"] = (border_style if border_style in BORDER_STYLES
                             else "solid")
    return style


def widget_style(entry: dict) -> dict:
    """The entry's style as a full dict (defaults merged in) — the
    one read path for the canvas, the preview and the exporter."""
    return _clean_style(entry.get("style") if isinstance(entry, dict)
                        else None)


def _valid_style_change(key, value) -> bool:
    if key in ("fg", "bg", "border_color"):
        return valid_color(value)
    if key == "font_family":
        return isinstance(value, str) and len(value) <= 80
    if key == "font_size":
        return (isinstance(value, int) and not isinstance(value, bool)
                and 0 <= value <= MAX_FONT_SIZE)
    if key == "border_width":
        return (isinstance(value, int) and not isinstance(value, bool)
                and 0 <= value <= MAX_BORDER_WIDTH)
    if key == "padding":
        return (isinstance(value, int) and not isinstance(value, bool)
                and 0 <= value <= MAX_PADDING)
    if key == "border_style":
        return value in BORDER_STYLES
    if key in ("bold", "italic"):
        return isinstance(value, bool)
    return False


def set_widget_style(design: dict, name: str, **changes) -> dict | None:
    """Apply inspector edits to a widget's style. Every change is
    validated first; one bad key or value refuses the whole edit
    (returns None, the entry untouched), like rename's all-or-nothing
    rule. On success the entry's style is the cleaned merge."""
    entry = find_widget(design, name)
    if entry is None:
        return None
    for key, value in changes.items():
        if key not in STYLE_KEYS or not _valid_style_change(key, value):
            return None
    merged = widget_style(entry)
    merged.update(changes)
    entry["style"] = _clean_style(merged)
    return entry


def widget_stylesheet(entry: dict) -> str:
    """The entry's colors/border/padding as a Qt stylesheet string
    ('' when nothing is set). Shared by the live preview and the
    generated code so both style identically."""
    style = widget_style(entry)
    parts = []
    if style["fg"]:
        parts.append(f"color: {style['fg']}")
    if style["bg"]:
        parts.append(f"background-color: {style['bg']}")
    if style["border_width"] > 0 and style["border_style"] != "none":
        color = style["border_color"] or BORDER_FALLBACK_COLOR
        parts.append(f"border: {style['border_width']}px "
                     f"{style['border_style']} {color}")
    if style["padding"] > 0:
        parts.append(f"padding: {style['padding']}px")
    return "; ".join(parts) + (";" if parts else "")


def widget_font(entry: dict) -> dict | None:
    """The entry's font properties as a plain spec dict, or None
    when no font property is set (the widget keeps its stock font).
    Qt-free: the preview builds a QFont from it, the exporter emits
    the equivalent code."""
    style = widget_style(entry)
    if not (style["font_family"] or style["font_size"]
            or style["bold"] or style["italic"]):
        return None
    return {"family": style["font_family"], "size": style["font_size"],
            "bold": style["bold"], "italic": style["italic"]}


# --- resize handles -------------------------------------------------------
# The 8-handle selection model: four corners + four edge midpoints.
# The canvas hit-tests and drags through these pure functions, so
# the geometry rules are testable without Qt.
HANDLES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")
MIN_WIDGET = 16


def handle_rects(entry: dict, size: int = 10) -> dict:
    """handle name -> (x, y, size, size): the handle squares of the
    entry's rect, drawn inside it (corners at the corners, edges
    centred on their midpoints)."""
    x, y, w, h = entry["x"], entry["y"], entry["w"], entry["h"]
    mid_x = x + w // 2 - size // 2
    mid_y = y + h // 2 - size // 2
    return {
        "nw": (x, y, size, size),
        "n": (mid_x, y, size, size),
        "ne": (x + w - size, y, size, size),
        "e": (x + w - size, mid_y, size, size),
        "se": (x + w - size, y + h - size, size, size),
        "s": (mid_x, y + h - size, size, size),
        "sw": (x, y + h - size, size, size),
        "w": (x, mid_y, size, size),
    }


def handle_at(entry: dict, px: int, py: int, size: int = 10) -> str | None:
    """The handle under the point, or None. Corners win over edges
    where their squares overlap on small widgets."""
    rects = handle_rects(entry, size)
    for handle in ("nw", "ne", "sw", "se", "n", "s", "w", "e"):
        hx, hy, hw, hh = rects[handle]
        if hx <= px < hx + hw and hy <= py < hy + hh:
            return handle
    return None


def drag_rect(x: int, y: int, w: int, h: int, handle: str,
              dx: int, dy: int) -> tuple:
    """The rect after dragging *handle* by (dx, dy) from the start
    rect (x, y, w, h): only the dragged edge(s) move — an edge drag
    changes exactly one axis, a corner exactly two — and the
    opposite edge stays pinned when the MIN_WIDGET floor is hit."""
    if handle in ("e", "ne", "se"):
        w = w + dx
    if handle in ("w", "nw", "sw"):
        x, w = x + dx, w - dx
    if handle in ("s", "se", "sw"):
        h = h + dy
    if handle in ("n", "ne", "nw"):
        y, h = y + dy, h - dy
    if w < MIN_WIDGET:
        if handle in ("w", "nw", "sw"):
            x -= MIN_WIDGET - w
        w = MIN_WIDGET
    if h < MIN_WIDGET:
        if handle in ("n", "ne", "nw"):
            y -= MIN_WIDGET - h
        h = MIN_WIDGET
    return x, y, w, h


def set_geometry(design: dict, name: str, x: int, y: int,
                 w: int, h: int) -> dict | None:
    """Place and size a widget in one step (handle drags move both
    at once), clamped inside the window like every geometry edit."""
    entry = find_widget(design, name)
    if entry is None:
        return None
    entry["x"], entry["y"] = int(x), int(y)
    entry["w"], entry["h"] = int(w), int(h)
    _clamp_geometry(entry, design)
    return entry


def new_design(title: str = "My Window", width: int = DEFAULT_WIDTH,
               height: int = DEFAULT_HEIGHT) -> dict:
    return {"version": 1, "title": str(title or "My Window"),
            "width": _clamp_window(width), "height": _clamp_window(height),
            "widgets": []}


def _clamp_window(value) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return DEFAULT_WIDTH
    return max(MIN_WINDOW, min(MAX_WINDOW, value))


def _clamp_geometry(widget: dict, design: dict) -> None:
    widget["w"] = max(16, min(int(widget["w"]), design["width"]))
    widget["h"] = max(16, min(int(widget["h"]), design["height"]))
    widget["x"] = max(0, min(int(widget["x"]), design["width"] - 8))
    widget["y"] = max(0, min(int(widget["y"]), design["height"] - 8))


def unique_name(design: dict, base: str) -> str:
    """A free identifier for *base* within the design: base, base2, …"""
    taken = {w["name"] for w in design["widgets"]}
    if base not in taken:
        return base
    n = 2
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"


def valid_name(name) -> bool:
    return bool(_NAME_RE.match(str(name or "")))


def add_widget(design: dict, wtype: str, x: int, y: int) -> dict | None:
    """Place a new widget of *wtype* with its top-left at (x, y),
    clamped inside the window. Returns the created entry, or None for
    an unknown type (the caller keeps its current state)."""
    spec = WIDGET_TYPES.get(str(wtype))
    if spec is None:
        return None
    _qt, text, w, h = spec
    entry = {"type": str(wtype), "name": unique_name(design, str(wtype)),
             "text": text, "x": int(x), "y": int(y), "w": w, "h": h,
             "style": default_style()}
    _clamp_geometry(entry, design)
    design["widgets"].append(entry)
    return entry


def find_widget(design: dict, name: str) -> dict | None:
    for entry in design["widgets"]:
        if entry["name"] == name:
            return entry
    return None


def move_widget(design: dict, name: str, x: int, y: int) -> dict | None:
    entry = find_widget(design, name)
    if entry is None:
        return None
    entry["x"], entry["y"] = int(x), int(y)
    _clamp_geometry(entry, design)
    return entry


def resize_widget(design: dict, name: str, w: int, h: int) -> dict | None:
    entry = find_widget(design, name)
    if entry is None:
        return None
    entry["w"], entry["h"] = int(w), int(h)
    _clamp_geometry(entry, design)
    return entry


def rename_widget(design: dict, name: str, new_name: str) -> dict | None:
    """Rename, keeping the exported file valid: the new name must be
    a Python identifier and free within the design. Returns the entry,
    or None when the rename is refused (the caller keeps the old
    name). Renaming to the widget's own name is a no-op success."""
    entry = find_widget(design, name)
    if entry is None:
        return None
    new_name = str(new_name or "").strip()
    if new_name == name:
        return entry
    if not valid_name(new_name) or find_widget(design, new_name) is not None:
        return None
    entry["name"] = new_name
    return entry


def set_widget_text(design: dict, name: str, text: str) -> dict | None:
    entry = find_widget(design, name)
    if entry is None:
        return None
    entry["text"] = str(text or "")
    return entry


def remove_widget(design: dict, name: str) -> bool:
    before = len(design["widgets"])
    design["widgets"] = [w for w in design["widgets"] if w["name"] != name]
    return len(design["widgets"]) != before


def widget_at(design: dict, x: int, y: int) -> dict | None:
    """The topmost widget containing the point (later entries draw on
    top of earlier ones), or None."""
    for entry in reversed(design["widgets"]):
        if (entry["x"] <= x < entry["x"] + entry["w"]
                and entry["y"] <= y < entry["y"] + entry["h"]):
            return entry
    return None


def sanitize_design(data) -> dict | None:
    """Defensive load: anything malformed becomes None (no design),
    never an exception in the load path. Salvageable entries are
    repaired instead: unknown widget types are dropped, geometry is
    coerced and clamped, and names that are not identifiers or that
    collide are regenerated — the design the owner saved still opens,
    and the exported code from it is always valid Python."""
    if not isinstance(data, dict):
        return None
    design = new_design(data.get("title", "My Window"),
                        data.get("width", DEFAULT_WIDTH),
                        data.get("height", DEFAULT_HEIGHT))
    raw = data.get("widgets")
    if raw is None:
        return design
    if not isinstance(raw, list):
        return None
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        wtype = str(entry.get("type", ""))
        if wtype not in WIDGET_TYPES:
            continue
        try:
            clean = {"type": wtype, "text": str(entry.get("text", "")),
                     "x": int(entry.get("x", 0)), "y": int(entry.get("y", 0)),
                     "w": int(entry.get("w", WIDGET_TYPES[wtype][2])),
                     "h": int(entry.get("h", WIDGET_TYPES[wtype][3])),
                     # Designs saved before styling existed have no
                     # "style" key at all — they get the defaults,
                     # which is exactly their old look.
                     "style": _clean_style(entry.get("style"))}
        except (TypeError, ValueError):
            return None
        if wtype == "svg":
            # The payload IS the widget: without a usable one the
            # entry is dropped, like an unknown type above.
            payload = entry.get("svg")
            if not isinstance(payload, str) or len(payload) > MAX_SVG_CHARS:
                continue
            clean["svg"] = payload
        name = str(entry.get("name", ""))
        if not valid_name(name) or find_widget(design, name) is not None:
            name = unique_name(design, wtype)
        clean["name"] = name
        _clamp_geometry(clean, design)
        design["widgets"].append(clean)
    return design


def design_to_json(design: dict) -> str:
    return json.dumps(design, indent=1)


def design_from_json(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except Exception:
        return None
    return sanitize_design(data)


def save_design(path, design: dict) -> None:
    # Sweep R157: the write is atomic (pid-unique temp + replace,
    # the tree's standing pattern) — a crash mid-save used to leave
    # the owner's saved design truncated, and load_design's
    # parse-failure path then silently returned None: design gone.
    target = Path(path)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.write_text(design_to_json(design), encoding="utf-8")
    os.replace(tmp, target)


def load_design(path) -> dict | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return None
    return design_from_json(text)


def _int_value(text) -> int | None:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def combo_items(text) -> list:
    """A combo box's text is its item list, comma-separated."""
    return [part.strip() for part in str(text or "").split(",")
            if part.strip()]


def _text_lines(entry: dict, name: str) -> list:
    """The generated lines giving a widget its text/value, per the
    type's semantics. The preview builder applies the same rules."""
    wtype, text = entry["type"], entry["text"]
    if wtype == "text_edit":
        return [f"        self.{name}.setPlainText({text!r})"] if text else []
    if wtype == "group_box":
        return [f"        self.{name}.setTitle({text!r})"] if text else []
    if wtype == "combo_box":
        items = combo_items(text)
        return [f"        self.{name}.addItems({items!r})"] if items else []
    if wtype in ("spin_box", "slider", "progress_bar"):
        value = _int_value(text)
        return ([f"        self.{name}.setValue({value})"]
                if value is not None else [])
    if wtype in ("h_line", "v_line"):
        shape = "HLine" if wtype == "h_line" else "VLine"
        return [f"        self.{name}.setFrameShape(QFrame.Shape.{shape})",
                f"        self.{name}.setFrameShadow(QFrame.Shadow.Sunken)"]
    if text:
        return [f"        self.{name}.setText({text!r})"]
    return []


def _style_lines(entry: dict, name: str) -> list:
    """The generated lines carrying the widget's styling: the shared
    stylesheet for colors/border/padding, a QFont for the font
    properties. Nothing set -> no lines, so an unstyled design's
    export is byte-for-byte the pre-styling one."""
    lines = []
    css = widget_stylesheet(entry)
    if css:
        lines.append(f"        self.{name}.setStyleSheet({css!r})")
    font = widget_font(entry)
    if font is not None:
        lines.append("        _font = QFont()")
        if font["family"]:
            lines.append(f"        _font.setFamily({font['family']!r})")
        if font["size"]:
            lines.append(f"        _font.setPointSize({font['size']})")
        if font["bold"]:
            lines.append("        _font.setBold(True)")
        if font["italic"]:
            lines.append("        _font.setItalic(True)")
        lines.append(f"        self.{name}.setFont(_font)")
    return lines


def generate_code(design: dict) -> str:
    """The design as a standalone, runnable PySide6 file: real widget
    construction mirroring the layout, attribute names = the design's
    widget names, and a main() that shows the window. A layout only —
    no logic is wired, and the file says so."""
    design = sanitize_design(design) or new_design()
    used = []
    svg_entries = []
    for entry in design["widgets"]:
        if entry["type"] == "svg":
            # QSvgWidget does NOT live in QtWidgets — it gets its own
            # import line below, so it stays out of `used`.
            svg_entries.append(entry)
            continue
        qt = WIDGET_TYPES[entry["type"]][0]
        if qt not in used:
            used.append(qt)
    imports = ", ".join(["QApplication", "QWidget"] + used)
    needs_font = any(widget_font(entry) is not None
                     for entry in design["widgets"])
    lines = [
        '"""Built with Code Forge\'s UI builder (Note 13B).',
        "",
        "A layout only: the widgets are real and the window runs, but no",
        "logic is wired — connect the buttons and fields yourself.",
        '"""',
        "import sys",
        "",
        f"from PySide6.QtWidgets import {imports}",
    ]
    if needs_font:
        lines.append("from PySide6.QtGui import QFont")
    if svg_entries:
        lines.append("from PySide6.QtCore import QByteArray")
        lines.append("from PySide6.QtSvgWidgets import QSvgWidget")
    lines += ["", ""]
    for entry in svg_entries:
        # The payload rides in the file as a constant, so the export
        # stays one standalone runnable file.
        lines.append(f"_SVG_{entry['name']} = {entry['svg']!r}")
    if svg_entries:
        lines += ["", ""]
    lines += [
        "class BuiltWindow(QWidget):",
        "    def __init__(self):",
        "        super().__init__()",
        f"        self.setWindowTitle({design['title']!r})",
        f"        self.resize({design['width']}, {design['height']})",
    ]
    for entry in design["widgets"]:
        qt = WIDGET_TYPES[entry["type"]][0]
        name = entry["name"]
        lines.append(f"        self.{name} = {qt}(self)")
        if entry["type"] == "svg":
            lines.append(
                f"        self.{name}.load(QByteArray(_SVG_{name}"
                f".encode(\"utf-8\")))")
        else:
            lines.extend(_text_lines(entry, name))
        lines.append(
            f"        self.{name}.setGeometry({entry['x']}, {entry['y']}, "
            f"{entry['w']}, {entry['h']})")
        lines.extend(_style_lines(entry, name))
    lines += [
        "",
        "",
        "def main():",
        "    app = QApplication(sys.argv)",
        "    win = BuiltWindow()",
        "    win.show()",
        "    return app.exec()",
        "",
        "",
        'if __name__ == "__main__":',
        "    raise SystemExit(main())",
        "",
    ]
    return "\n".join(lines)
