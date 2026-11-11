"""Layout-state persistence for the collapsible shell (Note 24).

Pure stdlib on purpose — same pattern as skribe.py / create_ext.py — so
the sanitize/round-trip rules are unit-testable without Qt. The window
keeps three flat keys in settings.json:

    layout_sidebar_collapsed : bool
    layout_accordions        : {section key: open bool}
    layout_docks             : [open dock names]
    layout_panel_sizes       : [sidebar px, inspector px]

read_layout() never raises and never trusts the file: junk types are
coerced or dropped, unknown dock names are filtered out, and anything
missing falls back to the pre-v2.6 bench (sidebar open, sections as
constructed, both docks open, panels at their original fixed widths).
"""
from __future__ import annotations

# Item 16 retired "run" from the dock system (it is a standalone
# window now), and the 2026-11-08 mode-windows pass retired "plan"
# the same way (the plan gate is a window, not a panel). Saved
# layouts from older builds may still name either —
# read_layout/write_layout filter unknown names, so they are dropped
# silently and the layout loads clean. That IS the migration.
KNOWN_DOCKS = ("activity", "receipts")
DEFAULT_DOCKS = ["activity", "receipts"]

# Resizable panels (owner's order, 2026-11-10): the sidebar and the
# inspector ride a QSplitter now. Their widths persist here, as
# [sidebar, inspector] pixel pairs. The defaults are the widths the
# panels shipped at when they were fixed (244 / 268), so a settings
# file without the key reopens pixel-identical to the old bench.
# Bounds keep a hand-edited or corrupt pair from crushing a panel
# shut or starving the center column: the floors are the panels'
# own minimum widths, the ceilings leave the center its minimum
# even at the window's minimum size.
PANEL_DEFAULTS = (244, 268)
PANEL_BOUNDS = ((180, 900), (200, 900))


def _sanitize_panel_sizes(raw, fallback=None):
    """Coerce a stored panel-size pair into [sidebar, inspector]
    ints inside PANEL_BOUNDS. Anything unusable falls back to
    `fallback` (itself sanitized against the defaults)."""
    def clean(pair):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return None
        out = []
        for value, (lo, hi) in zip(pair, PANEL_BOUNDS):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            out.append(max(lo, min(hi, int(value))))
        return out
    cleaned = clean(raw)
    if cleaned is not None:
        return cleaned
    cleaned = clean(fallback)
    if cleaned is not None:
        return cleaned
    return list(PANEL_DEFAULTS)


def read_layout(settings) -> dict:
    """Return sanitized layout state from a settings dict."""
    if not isinstance(settings, dict):
        settings = {}
    collapsed = settings.get("layout_sidebar_collapsed", False)
    raw_acc = settings.get("layout_accordions")
    accordions = {}
    if isinstance(raw_acc, dict):
        for key, value in raw_acc.items():
            if isinstance(key, str) and key:
                accordions[key] = bool(value)
    raw_docks = settings.get("layout_docks")
    if isinstance(raw_docks, (list, tuple, set)):
        docks = []
        for name in raw_docks:
            if name in KNOWN_DOCKS and name not in docks:
                docks.append(name)
    else:
        docks = list(DEFAULT_DOCKS)
    panel_sizes = _sanitize_panel_sizes(settings.get("layout_panel_sizes"))
    return {"sidebar_collapsed": bool(collapsed),
            "accordions": accordions,
            "docks": docks,
            "panel_sizes": panel_sizes}


def write_layout(settings: dict, state: dict) -> dict:
    """Store layout state into a settings dict (mutates and returns it).

    Only the four layout keys are touched — every other setting the
    owner has is left exactly as it was. A state dict that carries
    no usable panel sizes (an older caller) keeps the pair already
    stored rather than resetting the owner's panels to defaults."""
    if not isinstance(settings, dict):
        settings = {}
    settings["layout_sidebar_collapsed"] = bool(state.get("sidebar_collapsed", False))
    raw_acc = state.get("accordions") or {}
    settings["layout_accordions"] = {str(k): bool(v) for k, v in raw_acc.items()}
    docks = []
    for name in state.get("docks") or []:
        if name in KNOWN_DOCKS and name not in docks:
            docks.append(name)
    settings["layout_docks"] = docks
    settings["layout_panel_sizes"] = _sanitize_panel_sizes(
        state.get("panel_sizes"), fallback=settings.get("layout_panel_sizes"))
    return settings
