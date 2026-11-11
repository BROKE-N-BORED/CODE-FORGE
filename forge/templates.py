"""Templates shelf (Replit-study item C4).

A template is a starter project that ships INSIDE the app tree as
packaged data — a folder under `templates/` (beside `skills/` and
`plugins/`, the same packaged-data idiom), holding:

  template.json — the manifest, mirroring plugin.json's shape:
                  {"id", "name", "description", "kind", "tags",
                  "version"} — id and name are the load-bearing
                  fields; the rest is shelf display.
  everything else — the template's content files, copied verbatim
                  into a new project when the owner picks it.

Discovery is a scan: each subfolder with a parseable manifest is a
template. A malformed one (manifest missing, unreadable, not JSON,
not an object, no usable id/name) is SKIPPED with a line through the
caller's log hook — never an exception, never a half-listed entry.

Picking a template runs the ordinary project path: the content
files are copied into a fresh folder (copy only — template sources
are never moved, written, or deleted), then forge.projects'
ensure_project lays the .forge area exactly as it does for any
project, and the window adopts the folder through the same
bookkeeping add_project / set_project use. A destination that
already exists is refused, mirroring create_dev_clone.

EMBER BREAK: the owner's first-project build (a single-file
Breakout game he creates himself, driving Forge) is the shelf's #1
template. Its content is NOT authored here — EMBER_BREAK_ID below
is the registered slot: when a folder named "ember-break" with a
manifest lands in templates/, discovery surfaces it with no code
change, and while it is absent the shelf simply shows the
templates that exist.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import projects as proj

MANIFEST_NAME = "template.json"

# The registered EMBER BREAK slot (see the module docstring): the
# expected template id of the owner's first-project build. The
# shelf needs no code change when its folder arrives.
EMBER_BREAK_ID = "ember-break"

# The packaged shelf: the app tree's own templates/ folder.
DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _manifest_entry(folder: Path, raw) -> dict | None:
    """One template entry from a parsed manifest, or None when the
    manifest cannot name a template (not an object, or neither an
    id nor a name to register/display it by)."""
    if not isinstance(raw, dict):
        return None
    tid = raw.get("id")
    if not isinstance(tid, str) or not tid.strip():
        tid = folder.name
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        name = tid
    if not tid or not name:
        return None
    tags = raw.get("tags")
    if not isinstance(tags, list):
        tags = []
    return {
        "id": tid,
        "name": name,
        "description": raw.get("description")
        if isinstance(raw.get("description"), str) else "",
        "kind": raw.get("kind") if isinstance(raw.get("kind"), str) else "",
        "tags": [t for t in tags if isinstance(t, str)],
        "version": raw.get("version")
        if isinstance(raw.get("version"), str) else "",
        "dir": folder,
    }


def discover_templates(templates_dir=None, on_skip=None) -> list[dict]:
    """Scan a templates folder and return its template entries,
    sorted by folder name. Malformed template folders are skipped —
    `on_skip(message)` (when given) hears one plain line per skip,
    so the caller can log it; discovery itself never raises over a
    bad template. A missing templates folder is an empty shelf."""
    base = Path(templates_dir) if templates_dir is not None \
        else DEFAULT_TEMPLATES_DIR
    entries: list[dict] = []
    if not base.is_dir():
        return entries
    for folder in sorted(base.iterdir()):
        if not folder.is_dir():
            continue
        manifest = folder / MANIFEST_NAME
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception as exc:
            if on_skip is not None:
                on_skip(f"Template skipped ({folder.name}): "
                        f"manifest unreadable — {exc}")
            continue
        entry = _manifest_entry(folder, raw)
        if entry is None:
            if on_skip is not None:
                on_skip(f"Template skipped ({folder.name}): "
                        f"manifest names no template.")
            continue
        entries.append(entry)
    return entries


def content_files(entry: dict) -> list[Path]:
    """The template's content files (recursive, sorted): every file
    in the template folder except the manifest itself."""
    base = Path(entry["dir"])
    return [p for p in sorted(base.rglob("*"))
            if p.is_file() and p.name != MANIFEST_NAME
            and p != base / MANIFEST_NAME]


def instantiate_template(entry: dict, dest) -> Path:
    """Create a new project at `dest` from a template entry: copy
    the content files in byte-identical (copy2), then run the
    ordinary project setup (ensure_project's .forge area). Template
    sources are only ever read. Refuses — FileExistsError, before
    anything is written — when `dest` already exists, the same
    refusal shape as the dev-clone path."""
    dest = Path(dest)
    if dest.exists():
        raise FileExistsError(f"A folder already exists: {dest}")
    base = Path(entry["dir"])
    dest.mkdir(parents=True)
    for src in content_files(entry):
        target = dest / src.relative_to(base)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    proj.ensure_project(dest)
    return dest
