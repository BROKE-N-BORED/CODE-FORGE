"""Code graph, light edition (Tier 2 #2 — CodeRabbit study).

CodeRabbit builds a full code graph of the repo (definitions,
usages, cross-file references) and lets its reviewer investigate
with it. Forge already owns the static half of that idea:
forge/repomap.py renders a PROJECT MAP — classes, functions,
signatures, ranked by how much the rest of the project imports
them — into every system prompt. What Forge did NOT have is the
queryable half: "where is this symbol defined?", "who uses it?",
"what is this file connected to?" This module is that half, and
nothing more. No graph database, no hosted UI, no new dependency
— the standard library's `ast` module, the same parser repomap
already uses.

What it is for: context ranking (Tier 1 #10). The model queries
the graph during its HOMEWORK pass through the `code_graph` tool
(a read tool, forge/tools.py) instead of guessing which files to
read — find the definition, check the users, read the right
files. The repo map stays the static orientation; the graph is
the lookup.

Honest limits, stated plainly:
  * Python files only. Other languages appear in the repo map as
    bare lines and are invisible to the graph — ast-grep / ctags
    would extend it, and are deliberately out of this edition.
  * Names, not resolved references. `used_by` matches the NAME —
    a same-named symbol in another file can appear in the
    results. The tool description says this; the graph never
    claims a match is the same symbol.
  * One hop only. `related` is direct import edges (imports and
    importers), not a transitive blast radius.
  * A file's own uses of its own symbols are not "users" —
    used_by lists OTHER files.

Caps keep it Forge-scale: MAX_FILES files, MAX_SYMBOLS symbols
per file, files over MAX_FILE_BYTES skipped, MAX_RESULTS answers
per query with a truthful tail. The built graph is cached behind
a stat-only fingerprint of exactly the .py files it was built
from (repomap's B16 pattern) — build_system_prompt's callers ask
often, and re-parsing an unchanged tree each time is pure waste.
Every function is safe on a missing root, an unparseable file, or
garbage input — it returns an empty answer, never raises."""

from __future__ import annotations

import ast
from pathlib import Path

#: Directories a walk never descends into (repomap's set).
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
             ".forge", ".mypy_cache", ".pytest_cache", "dist", "build"}

#: Most .py files indexed (repomap's MAX_FILES).
MAX_FILES = 300

#: Most symbols kept per file — one fat file must not eat the graph.
MAX_SYMBOLS_PER_FILE = 100

#: Files larger than this are skipped, not parsed. 2 MB matches
#: read_file's limit — and it matters: Forge's own main.py is
#: ~1.1 MB, and at C6's 1 MB ceiling the hub of the codebase was
#: invisible to the graph (its callers and importers never
#: appeared). A 2 MB file parses in well under a second.
MAX_FILE_BYTES = 2_000_000

#: Most answers one query returns; the rest are counted in a tail.
MAX_RESULTS = 20

#: Actions the code_graph tool accepts.
ACTIONS = ("find", "used_by", "related")

# The assembled-graph cache: {root: (fingerprint, graph)}.
# Bounded (Sweep R153 pattern) — a long session touching many
# projects must not grow it for the life of the process.
_graph_cache: dict[str, tuple] = {}
_CACHE_MAX = 32


def _py_files(root: Path) -> list[Path]:
    """The project's .py files, in path order, capped. Never raises."""
    try:
        files: list[Path] = []
        for path in sorted(root.rglob("*.py")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if len(files) >= MAX_FILES:
                break
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            if path.is_file():
                files.append(path)
        return files
    except Exception:
        return []


def _fingerprint(root: Path, files: list[Path]) -> tuple:
    """Stat-only fingerprint (repomap's B16 pattern): relpath,
    mtime_ns, size per file. Reads no file content."""
    fp = []
    for path in files:
        rel = str(path.relative_to(root))
        try:
            stat = path.stat()
            fp.append((rel, stat.st_mtime_ns, stat.st_size))
        except OSError:
            fp.append((rel, -1, -1))
    return tuple(sorted(fp))


def _parse_file(path: Path):
    """(defines, imports, used_names, edge_modules) for one .py
    file, or None when it cannot be parsed. defines are
    (qualified name, kind, line); imports are (module, [names]);
    used_names are the bare names the file's code references;
    edge_modules are the dotted module names this file imports
    from — the import edges. The edge rule matters: for
    `from . import repomap` the module IS the name (relative
    import, no module part), while for `from .worker import
    run_turn` the module is `worker` and the names are symbols —
    so names count as modules only for a relative import with no
    module part. Never raises."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8",
                                        errors="replace"))
    except Exception:
        return None
    defines: list[tuple] = []
    imports: list[tuple] = []
    used: set[str] = set()
    edge_modules: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            defines.append((node.name, "class", node.lineno))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    defines.append((f"{node.name}.{item.name}",
                                    "method", item.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defines.append((node.name, "function", node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, []))
                edge_modules.append(alias.name)
                used.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            names = [a.name for a in node.names]
            imports.append((node.module or "", names))
            if node.module:
                edge_modules.append(node.module)
            elif node.level > 0:
                # `from . import x` — the names ARE the modules.
                edge_modules.extend(names)
            used.update(a.asname or a.name for a in node.names)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    return (defines[:MAX_SYMBOLS_PER_FILE], imports, used,
            edge_modules)


def build_graph(root) -> dict:
    """The project's code graph, cached behind a stat fingerprint.
    Returns {files: {rel: {defines, imports}}, definitions:
    {name: [(rel, line, kind, qualified)]}, uses: {name: [rel]},
    related: {rel: {imports: [rel], imported_by: [rel]}},
    files_scanned: int, skipped: int}. Definitions are indexed
    under both the qualified name and the short name. `uses`
    excludes a file's own definitions (a file is not its own
    user). Never raises."""
    try:
        root_path = Path(root).expanduser().resolve()
    except Exception:
        return _empty_graph()
    if not root_path.is_dir():
        return _empty_graph()
    files = _py_files(root_path)
    fingerprint = _fingerprint(root_path, files)
    cache_key = str(root_path)
    cached = _graph_cache.get(cache_key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    graph = _build_fresh(root_path, files)
    if len(_graph_cache) >= _CACHE_MAX:
        _graph_cache.clear()
    _graph_cache[cache_key] = (fingerprint, graph)
    return graph


def _empty_graph() -> dict:
    return {"files": {}, "definitions": {}, "uses": {},
            "related": {}, "files_scanned": 0, "skipped": 0}


def _build_fresh(root_path: Path, files: list[Path]) -> dict:
    graph = _empty_graph()
    # Stem -> files, for import resolution (repomap ranks by stem
    # too; a stem can name several files and all of them count).
    by_stem: dict[str, list[str]] = {}
    for path in files:
        rel = str(path.relative_to(root_path))
        by_stem.setdefault(path.stem, []).append(rel)
    for path in files:
        rel = str(path.relative_to(root_path))
        parsed = _parse_file(path)
        if parsed is None:
            graph["skipped"] += 1
            continue
        defines, imports, used, edge_modules = parsed
        graph["files_scanned"] += 1
        graph["files"][rel] = {"defines": [
            {"name": name, "kind": kind, "line": line}
            for name, kind, line in defines],
            "imports": [{"module": mod, "names": names}
                        for mod, names in imports]}
        own_names = {name.split(".")[-1] for name, _, _ in defines}
        for name, kind, line in defines:
            for key in {name, name.split(".")[-1]}:
                graph["definitions"].setdefault(key, []).append(
                    (rel, line, kind, name))
        for name in used - own_names:
            graph["uses"].setdefault(name, []).append(rel)
        # Import edges, resolved by stem and by dotted path.
        imported: set[str] = set()
        for mod in edge_modules:
            if not mod:
                continue
            stem = mod.split(".")[-1]
            imported.update(by_stem.get(stem, []))
            dotted = root_path.joinpath(*mod.split("."))
            for candidate in (dotted.with_suffix(".py"),
                              dotted / "__init__.py"):
                if candidate.is_file():
                    try:
                        imported.add(str(candidate.relative_to(
                            root_path)))
                    except ValueError:
                        pass
        imported.discard(rel)
        graph["related"].setdefault(
            rel, {"imports": [], "imported_by": []})
        graph["related"][rel]["imports"] = sorted(imported)
        for other in imported:
            entry = graph["related"].setdefault(
                other, {"imports": [], "imported_by": []})
            if rel not in entry["imported_by"]:
                entry["imported_by"].append(rel)
    for entry in graph["related"].values():
        entry["imported_by"] = sorted(entry["imported_by"])
    for hits in graph["definitions"].values():
        hits.sort(key=lambda h: (h[0], h[1]))
    for name in graph["uses"]:
        graph["uses"][name] = sorted(set(graph["uses"][name]))
    return graph


def find_symbol(graph: dict, name: str) -> list[tuple]:
    """Definitions of `name` (qualified or short), sorted by file
    then line, capped at MAX_RESULTS. Never raises."""
    try:
        return list((graph or {}).get("definitions", {}).get(
            (name or "").strip(), []))[:MAX_RESULTS]
    except Exception:
        return []


def used_by(graph: dict, name: str) -> list[str]:
    """Other files referencing `name`, sorted, capped. Never raises."""
    try:
        return list((graph or {}).get("uses", {}).get(
            (name or "").strip(), []))[:MAX_RESULTS]
    except Exception:
        return []


def related_files(graph: dict, rel: str) -> dict:
    """One-hop neighbours of a file: {imports: [rel],
    imported_by: [rel]}. Unknown files read as no neighbours.
    Never raises."""
    try:
        entry = (graph or {}).get("related", {}).get(
            (rel or "").strip())
        if not entry:
            return {"imports": [], "imported_by": []}
        return {"imports": list(entry["imports"])[:MAX_RESULTS],
                "imported_by": list(entry["imported_by"])[:MAX_RESULTS]}
    except Exception:
        return {"imports": [], "imported_by": []}


def _skipped_note(graph: dict) -> str:
    skipped = (graph or {}).get("skipped", 0)
    if skipped:
        return (f"\n({skipped} file(s) could not be parsed and "
                f"were skipped.)")
    return ""


def answer(graph: dict, action: str, target: str) -> str:
    """The text the code_graph tool returns for one query. Unknown
    actions and empty targets read as plain errors, never raise."""
    action = (action or "").strip()
    target = (target or "").strip()
    if action not in ACTIONS:
        return (f"code_graph: unknown action '{action}' — use "
                f"find, used_by, or related.")
    if not target:
        return "code_graph: no target given."
    if action == "find":
        hits = find_symbol(graph, target)
        total = len((graph or {}).get("definitions", {}).get(
            target, []))
        if not hits:
            return (f"No definition of '{target}' found in the "
                    f"project's Python files." + _skipped_note(graph))
        lines = [f"'{target}' is defined in:"]
        lines.extend(f"- {rel}:{line} ({kind})"
                     for rel, line, kind, _q in hits)
        if total > len(hits):
            lines.append(f"+{total - len(hits)} more definition(s) "
                         f"not shown.")
        return "\n".join(lines) + _skipped_note(graph)
    if action == "used_by":
        users = used_by(graph, target)
        total = len((graph or {}).get("uses", {}).get(target, []))
        if not users:
            return (f"No other file uses '{target}'." +
                    _skipped_note(graph))
        lines = [f"'{target}' is used in:"]
        lines.extend(f"- {rel}" for rel in users)
        if total > len(users):
            lines.append(f"+{total - len(users)} more file(s) "
                         f"not shown.")
        return "\n".join(lines) + _skipped_note(graph)
    neighbours = related_files(graph, target)
    if not neighbours["imports"] and not neighbours["imported_by"]:
        return (f"No related files found for '{target}' — it may "
                f"not be a Python file in the project, or nothing "
                f"imports it and it imports nothing here." +
                _skipped_note(graph))
    lines = [f"Files related to '{target}':"]
    for rel in neighbours["imports"]:
        lines.append(f"- {rel} (imported by it)")
    for rel in neighbours["imported_by"]:
        lines.append(f"- {rel} (imports it)")
    return "\n".join(lines) + _skipped_note(graph)
