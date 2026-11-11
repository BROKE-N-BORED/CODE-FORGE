"""Repository map — Note 17 (Aider's signature idea, sized for this bench).

A compact structural map of the project — files, classes, functions,
signatures — injected into the system prompt so the model always knows
what exists and where, without burning its 8k window on file dumps.
Python files are parsed with the stdlib `ast` module (no dependencies);
other files appear as single lines. Files are ranked by how much the
rest of the project references them, and the map is cut to a character
budget (roughly 4 chars ≈ 1 token). The map is READ-ONLY orientation,
never a file the model should try to edit.
"""
from __future__ import annotations
import ast
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
             ".forge", ".mypy_cache", ".pytest_cache", "dist", "build"}
MAX_FILES = 300
DEFAULT_BUDGET_CHARS = 4000
_cache: dict[str, tuple[float, int, list[str], set[str]]] = {}


def _py_symbols(path: Path) -> tuple[list[str], set[str]]:
    """(symbol lines, referenced module names) for one Python file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return [], set()
    lines: list[str] = []
    refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import,)):
            for a in node.names:
                refs.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            refs.add(node.module.split(".")[0])
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            lines.append(f"  class {node.name}" + (f": {', '.join(methods[:10])}" if methods else ""))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ", ".join(a.arg for a in node.args.args[:6])
            lines.append(f"  def {node.name}({args})")
    return lines, refs


def _file_entry(path: Path) -> tuple[list[str], set[str]]:
    key = str(path)
    try:
        stat = path.stat()
    except OSError:
        return [], set()
    cached = _cache.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2], cached[3]
    if path.suffix == ".py":
        lines, refs = _py_symbols(path)
    else:
        lines, refs = [], set()
    # Sweep R153: both repomap caches are bounded — a long session
    # touching many files/projects must not grow them for the life
    # of the process. A full cache clears; entries are pure
    # performance copies, rebuilt on demand.
    if len(_cache) >= 512:
        _cache.clear()
    _cache[key] = (stat.st_mtime, stat.st_size, lines, refs)
    return lines, refs


# Audit B16: the assembled map is cached per (root, budget) behind a
# stat-only fingerprint of exactly the files the map is built from —
# build_system_prompt asks for the map on EVERY send, and re-walking,
# re-parsing, and re-ranking an unchanged tree each time was pure
# wiring waste. The fingerprint reads no file content (relpath,
# mtime_ns, size); any added / removed / changed file misses the
# cache and the map is rebuilt, byte-identical to the uncached result.
_map_cache: dict[tuple, tuple[tuple, str]] = {}


def _map_fingerprint(root: Path, files: list[Path]) -> tuple:
    fp = []
    for path in files:
        rel = str(path.relative_to(root))
        try:
            stat = path.stat()
            fp.append((rel, stat.st_mtime_ns, stat.st_size))
        except OSError:
            fp.append((rel, -1, -1))
    return tuple(sorted(fp))


def workspace_files(workspace: str, limit: int = 500) -> list[str]:
    """The workspace's files as sorted relative paths, under the
    same SKIP_DIRS rules and .pyc exclusion as build_map — the
    listing behind the input box's @mention completer (audit D43)."""
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        return []
    out: list[str] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.is_file() and path.suffix not in (".pyc",):
            out.append(rel.as_posix())
        if len(out) >= limit:
            break
    return out


def build_map(workspace: str, budget_chars: int = DEFAULT_BUDGET_CHARS) -> str:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        return ""
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix not in (".pyc",):
            files.append(path)
        if len(files) >= MAX_FILES:
            break
    fingerprint = _map_fingerprint(root, files)
    cache_key = (str(root), budget_chars)
    cached = _map_cache.get(cache_key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    entries = []
    refcount: dict[str, int] = {}
    parsed = {}
    for path in files:
        lines, refs = _file_entry(path)
        parsed[path] = lines
        for r in refs:
            refcount[r] = refcount.get(r, 0) + 1
    for path in files:
        rel = str(path.relative_to(root))
        # References are the signal (Aider's insight): a file the rest of
        # the project imports matters more than a fat file nothing touches.
        score = refcount.get(path.stem, 0) * 10 + min(len(parsed[path]), 14)
        entries.append((score, rel, parsed[path]))
    entries.sort(key=lambda e: (-e[0], e[1]))
    out = ["PROJECT MAP (auto-generated orientation — read-only, not a file; "
           "use read_file for full text):"]
    used = len(out[0])
    shown = 0
    for _score, rel, lines in entries:
        if len(lines) > 14:  # one fat file must not eat the whole map
            lines = lines[:14] + [f"  … +{len(lines) - 14} more symbols"]
        block = [rel] + lines
        block_len = sum(len(l) + 1 for l in block)
        if used + block_len > budget_chars and shown > 0:
            out.append(f"  … ({len(entries) - shown} more file(s) not shown — ask or read directly)")
            break
        out.extend(block)
        used += block_len
        shown += 1
    result = "\n".join(out) if shown else ""
    if len(_map_cache) >= 64:  # Sweep R153: bounded, as above
        _map_cache.clear()
    _map_cache[cache_key] = (fingerprint, result)
    return result
