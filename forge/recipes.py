"""Recipes — Audit D41 (Goose-style parameterized task files).

A recipe is a small JSON file the owner authors once — a named,
parameterized prompt — and runs any time with fresh values, from
the Recipes dialog. File-authored like seats.json / hooks.json:
one JSON object per file in the app data dir's `recipes/` folder:

  {"name": "Release notes",
   "description": "Draft release notes for a version.",
   "prompt": "Write release notes for version {{version}} "
             "covering {{scope}}.",
   "params": [{"name": "version", "required": true},
              {"name": "scope", "required": false,
               "default": "everything since the last release"}],
   "mode": "code"}

`name` and `prompt` are required; `params` and `mode` are
optional, and `mode` must name a real permission mode
(forge.permissions.MODES) when present. Running a recipe renders
its prompt and hands the text to the window's normal send path —
macros, @mentions, the context fold and every approval gate apply
exactly as if the owner had typed it. Recipes are owner-launched
only: no agent tool and no deck surface runs one.

Loading follows the hooks/seats pattern (audit C30): problems are
DATA — each broken file is skipped with one problem string naming
the file and the reason, and the caller decides how to surface
them. Rendering is strict in the other direction: a required
param with no value and no default, or a placeholder naming no
declared param, raises RecipeError — authoring bugs are loud,
never silently sent.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .permissions import MODES

# {{name}} with optional whitespace inside the braces. Param names
# are identifier-shaped; anything else between {{ }} is not a
# placeholder and rides through literally.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class RecipeError(Exception):
    """A recipe could not be rendered with the values given."""


def _normalize_recipe(data, slug: str) -> dict:
    """Validate one parsed recipe object and return its normalized
    dict. Raises ValueError with the reason on the first defect —
    load_recipes turns that into the file's one problem string."""
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("missing a 'name'")
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("missing a 'prompt'")
    description = data.get("description")
    if not isinstance(description, str):
        description = ""
    raw_params = data.get("params")
    if raw_params is None:
        raw_params = []
    if not isinstance(raw_params, list):
        raise ValueError("'params' is not a list")
    params = []
    seen = set()
    for entry in raw_params:
        if not isinstance(entry, dict):
            raise ValueError("a param is not a JSON object")
        pname = entry.get("name")
        if not isinstance(pname, str) or not pname.strip():
            raise ValueError("a param has no 'name'")
        pname = pname.strip()
        if pname in seen:
            raise ValueError(f"duplicate param '{pname}'")
        seen.add(pname)
        default = entry.get("default")
        params.append({
            "name": pname,
            "required": bool(entry.get("required", False)),
            "default": None if default is None else str(default),
        })
    mode = data.get("mode")
    if mode is not None:
        if not isinstance(mode, str) or mode.strip().lower() not in MODES:
            raise ValueError(
                f"unknown mode '{mode}' (modes: {', '.join(MODES)})")
        mode = mode.strip().lower()
    return {"slug": slug, "name": name.strip(), "description": description,
            "prompt": prompt, "params": params, "mode": mode}


def load_recipes(app_dir) -> tuple[list[dict], list[str]]:
    """Read every recipe in app_dir/"recipes", sorted by filename.

    Returns (recipes, problems): each valid file becomes a recipe
    dict {slug, name, description, prompt, params, mode}; each
    invalid file is skipped with ONE problem string naming the
    file and the reason. A missing recipes folder is the normal
    no-recipes state: ([], []).
    """
    folder = Path(app_dir) / "recipes"
    recipes: list[dict] = []
    problems: list[str] = []
    if not folder.is_dir():
        return recipes, problems
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            problems.append(f"{path}: could not be read as JSON ({exc})")
            continue
        try:
            recipes.append(_normalize_recipe(data, path.stem))
        except ValueError as exc:
            problems.append(f"{path}: {exc}")
    return recipes, problems


def _is_missing(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def render(recipe: dict, values: dict | None) -> str:
    """Render a recipe's prompt with the given param values.

    Each {{param}} placeholder is replaced by its value; an
    omitted param falls back to its declared default, and an
    optional param with neither substitutes "". Raises
    RecipeError naming the param when a required param has no
    value and no default, and naming the placeholder when the
    prompt uses one the recipe never declared.
    """
    values = values or {}
    params = {p["name"]: p for p in recipe.get("params", [])}
    resolved: dict[str, str] = {}
    for pname, param in params.items():
        value = values.get(pname)
        if _is_missing(value):
            value = param.get("default")
        if _is_missing(value):
            if param.get("required"):
                raise RecipeError(
                    f"'{recipe.get('name', '?')}' needs a value for "
                    f"'{pname}' — it is required and has no default.")
            value = ""
        resolved[pname] = str(value)

    def _substitute(match: re.Match) -> str:
        pname = match.group(1)
        if pname not in resolved:
            raise RecipeError(
                f"the prompt uses '{{{{{pname}}}}}' but "
                f"'{recipe.get('name', '?')}' declares no param "
                f"named '{pname}'.")
        return resolved[pname]

    return PLACEHOLDER_RE.sub(_substitute, str(recipe.get("prompt", "")))
