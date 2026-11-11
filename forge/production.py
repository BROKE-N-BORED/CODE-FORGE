"""Production-data hard separation — Replit-study item C2.

Born of Replit's July 2025 SaaStr incident (its agent deleted a
production database during a code freeze): the platform rule that
followed is the one this module implements for Forge — the agent
is READ-ONLY on production data, by rule, not by preference.

THE MARKING — a project declares its production data in
.forge/production.json (the same .forge/*.json pattern as
browse.json / cloudstore.json), written by the OWNER:

    {"paths": ["data/prod", "backups"],
     "files": ["db/live.sqlite3"]}

"paths" entries are workspace-relative prefixes: an entry covers
the path itself and everything under it (a file named there is
therefore covered exactly). "files" entries are exact paths only —
naming a folder there marks the folder path itself, never its
contents. "." as a "paths" entry marks the whole project. No file
at all = no production data = the project behaves exactly as it
always has.

A marking file that exists but cannot be parsed, or that names a
path escaping the workspace, is BROKEN, not empty: while it is
broken every write is refused, with the refusal naming the file
and the problem. A fence that failed open on a typo would
silently unprotect the very data it exists to protect.

ENFORCEMENT sits at the tool layer — the one choke point every
write flows through — in the same shape as the contained-run
cage: forge/tools.py's execute_tool checks the built-in file
tools and run_command, and forge/worker.py's _dispatch checks
approval-gated plugin tools' path arguments, both through the
pure functions here. A marked write is REFUSED outright, not
approval-gated: there is no "approve anyway" for production
data. Reads are untouched everywhere.

run_command is checked as command TEXT. The postures refused:
output redirection into a marked path; rm / shred / mv touching
a marked path or an ancestor of one (rm -r of a parent folder
destroys the marking inside it); cp / install / rsync / ln with
a marked DESTINATION (copying FROM production is a read and
passes); tee, truncate, touch, mkdir naming a marked path;
sed -i on a marked file; dd of= a marked path; find -delete over
a marked path; and the python one-liner shapes a pattern can
deterministically see — open(path, 'w'/'a'/'x'/'r+'),
Path(path).write_text/write_bytes, os.remove / os.unlink /
shutil.rmtree on a marked literal, shutil.move touching one,
shutil.copy* with a marked destination literal.

THE HONEST LIMIT — this is a fence on identifiable references in
the call text, NOT a shell parser and NOT a sandbox. A command
that builds a production path indirectly — through variables,
$(...), a glob whose fixed part never reaches the marking, or a
script file an interpreter then runs — is beyond what the text
shows, and the fence does not claim otherwise. The approval gate
remains the backstop for commands, exactly as the contained-run
copy says of its own cage. What the fence guarantees is narrower
and real: the direct, written-down ways an agent reaches for a
file — the file tools themselves and plainly-written commands —
cannot write to a marked path, ever, approval or not.

SURFACING — build_system_prompt (forge/main.py) appends
context_text() to the turn's system prompt, in the same place
project instructions and hints ride, so the model knows the
boundary before it ever reaches for the data.
"""
from __future__ import annotations
import json
import re
import shlex
from pathlib import Path

from . import projects as proj

MARKING_NAME = "production.json"  # .forge/production.json in the project
MARKING_DISPLAY = ".forge/production.json"

# Argument names that carry a path in plugin tools (their schemas
# are not fixed) — the same list the contained-run cage checks
# (forge/tools.py's CONTAINED_PATH_ARG_KEYS), one idiom for
# path-carrying arguments everywhere.
PATH_ARG_KEYS = ("path", "file", "filepath", "file_path",
                 "directory", "dir", "folder")


class ProductionMarkings:
    """One project's resolved production markings: normalized
    workspace-relative POSIX prefixes (`paths`), exact files
    (`files`), whether the marking file exists at all (`present`),
    and `error` — set when the file exists but is broken, the
    state in which every write is refused (module docstring)."""

    def __init__(self, paths=(), files=(), present=False, error=None):
        self.paths = tuple(paths)
        self.files = tuple(files)
        self.present = bool(present)
        self.error = error

    @property
    def active(self) -> bool:
        """True when at least one path or file is actually marked."""
        return bool(self.paths or self.files)


# ---------- loading ----------

_DRIVE_RE = re.compile(r"^[A-Za-z]:/")


def _normalize_entry(raw) -> str | None:
    """One marking entry → its normalized workspace-relative POSIX
    form, or None when the entry is unusable: not text, empty, or
    escaping the workspace (absolute, or a .. climb past the
    root). "." normalizes to "" — the root prefix, which covers
    the whole project; the caller decides whether "" is legal
    for the list the entry came from."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().replace("\\", "/")
    if not text:
        return None
    if text.startswith("/") or _DRIVE_RE.match(text):
        return None
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def load_markings(workspace) -> ProductionMarkings:
    """The project's production markings. A missing file is the
    unmarked project (present=False — the state every caller
    treats as exactly today's behavior). An existing file that is
    unreadable, not a JSON object, has a non-list "paths"/"files",
    or carries an entry that is not a workspace-relative path
    inside the project comes back present with `error` set."""
    path = proj.forge_area(workspace) / MARKING_NAME
    if not path.exists():
        return ProductionMarkings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ProductionMarkings(
            present=True,
            error=f"it is not readable JSON ({exc})")
    if not isinstance(data, dict):
        return ProductionMarkings(
            present=True,
            error='it must be a JSON object like '
                  '{"paths": [...], "files": [...]}')
    loaded: dict[str, list[str]] = {}
    for key, allow_root in (("paths", True), ("files", False)):
        raw = data.get(key, [])
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            return ProductionMarkings(
                present=True,
                error=f'"{key}" must be a list of workspace-relative '
                      f'paths')
        entries: list[str] = []
        for entry in raw:
            norm = _normalize_entry(entry)
            if norm is None or (norm == "" and not allow_root):
                return ProductionMarkings(
                    present=True,
                    error=f"the entry {entry!r} in \"{key}\" is not a "
                          f"path inside the project — entries are "
                          f"workspace-relative, like \"data/prod\"")
            if norm not in entries:
                entries.append(norm)
        loaded[key] = entries
    return ProductionMarkings(paths=loaded["paths"],
                              files=loaded["files"], present=True)


# ---------- matching ----------

def _relative(root, target) -> str | None:
    """target as a workspace-relative POSIX string, or None when
    it does not resolve inside root. Both sides are resolved
    first, so symlinks and .. settle before the comparison — the
    same jail philosophy as tools._inside."""
    root_r = Path(root).expanduser().resolve()
    candidate = Path(str(target)).expanduser()
    if not candidate.is_absolute():
        candidate = root_r / candidate
    resolved = candidate.resolve()
    if resolved == root_r:
        return ""
    if root_r in resolved.parents:
        return resolved.relative_to(root_r).as_posix()
    return None


def covering_entry(markings: ProductionMarkings, root, target) -> str | None:
    """The marking entry covering target, or None. A "paths" entry
    matches the path itself and everything under it; a "files"
    entry matches exactly. The return is the entry as written in
    the file ("" — the root prefix — is reported as ".")."""
    rel = _relative(root, target)
    if rel is None:
        return None
    for entry in markings.paths:
        if entry == "" or rel == entry or rel.startswith(entry + "/"):
            return entry or "."
    if rel in markings.files:
        return rel
    return None


def _is_ancestor_of_marking(markings: ProductionMarkings, root,
                            target) -> bool:
    """True when target is a folder that CONTAINS a marking —
    the rm -r case: destroying the parent destroys the marked
    data inside it, so destructive verbs refuse it too."""
    rel = _relative(root, target)
    if rel is None:
        return False
    if rel == "":
        return markings.active
    return any(entry == rel or entry.startswith(rel + "/")
               for entry in (*markings.paths, *markings.files))


# ---------- refusals (plain words, naming path + marking file) ----------

def broken_refusal(markings: ProductionMarkings) -> str:
    return (f"Refused: this project has production-data protection, but "
            f"its marking file ({MARKING_DISPLAY}) is broken: "
            f"{markings.error}. Every write is refused until the owner "
            f"fixes {MARKING_DISPLAY} — a fence that failed open on a "
            f"typo could silently unprotect production data. Reads "
            f"still work.")


def path_refusal(markings: ProductionMarkings, root, target,
                 shown=None) -> str | None:
    """The refusal for a file-tool-style path argument, or None
    when the call may proceed. `shown` is the path as the caller
    wrote it (what the refusal names); target is what gets judged."""
    if markings.error:
        return broken_refusal(markings)
    entry = covering_entry(markings, root, target)
    if entry is None:
        return None
    named = shown if shown is not None else target
    return (f"Refused: '{named}' is production data — it is covered by "
            f"the production marking '{entry}' in {MARKING_DISPLAY}, "
            f"and production data is read-only for the agent: writes, "
            f"appends, moves and deletes are refused outright, not "
            f"approved. Reading it is fine. If this path should not "
            f"be production, the owner edits {MARKING_DISPLAY} and "
            f"removes the marking.")


def args_refusal(markings: ProductionMarkings, root, args) -> str | None:
    """The path-argument scan for plugin tools, whose schemas are
    not fixed: any argument under a common path name that lands on
    production data refuses the call. Callers consult this only
    for approval-gated (write-class) plugin tools — read-class
    tools keep full read access to production data."""
    if markings.error:
        return broken_refusal(markings)
    if not isinstance(args, dict):
        return None
    for key in PATH_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            refusal = path_refusal(markings, root, value)
            if refusal is not None:
                return refusal
    return None


# ---------- run_command: the posture scan ----------

_SEGMENT_SPLIT = re.compile(r"[;|&\n]")
_REDIRECT_TOKEN = re.compile(r"^(?:\d+|&)?(>>?)$")
_REDIRECT_ATTACHED = re.compile(r"^(?:\d+|&)?(>>?)(.+)$")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_GLOB_CHARS = "*?["

# Verbs where ANY marked file argument is a write/delete posture.
_ANY_ARG_WRITE = {"rm", "shred", "tee", "truncate", "touch", "mkdir"}
# Verbs where the LAST positional argument is the write target;
# the rest are reads (copying FROM production stays allowed).
_DEST_WRITE = {"cp", "install", "rsync", "ln"}
# Verbs whose target may be an ANCESTOR of a marking (rm -r of a
# parent destroys the marking inside it).
_DESTRUCTIVE = {"rm", "shred", "mv"}

_PY_OPEN_WRITE = re.compile(
    r"""open\(\s*(['"])(?P<path>[^'"]+)\1\s*,\s*(['"])(?P<mode>[^'"]+)\3""")
_PY_PATH_WRITE = re.compile(
    r"""\(\s*(['"])(?P<path>[^'"]+)\1\s*\)\s*\.\s*write_(?:text|bytes)\s*\(""")
_PY_REMOVE = re.compile(
    r"""(?:os\.(?:remove|unlink)|shutil\.rmtree)\s*\(\s*(['"])(?P<path>[^'"]+)\1""")
_PY_MOVE = re.compile(
    r"""shutil\.move\s*\(\s*(['"])(?P<a>[^'"]+)\1\s*,\s*(['"])(?P<b>[^'"]+)\3""")
_PY_COPY = re.compile(
    r"""shutil\.copy(?:2|file)?\s*\(\s*(['"])(?P<a>[^'"]+)\1\s*,\s*(['"])(?P<b>[^'"]+)\3""")


def _command_refusal_text(detail: str) -> str:
    return (f"Refused: this command would write to production data — "
            f"{detail}. Production data is read-only for the agent "
            f"(markings in {MARKING_DISPLAY}); the refusal is by rule, "
            f"not by approval, and reading production data is fine. If "
            f"the marking is wrong, the owner edits {MARKING_DISPLAY}.")


def _token_entry(markings: ProductionMarkings, root, token) -> str | None:
    """The marking entry a command token references, or None. A
    token references production when it resolves to a marked path
    or under one. A glob token is judged by its fixed part
    (data/prod/*.csv reaches data/prod; data/* does not reach
    data/prod — the ancestor check in the verb rules is what
    covers destructive globs). $PWD is the workspace (run_command
    runs there); other variables are not resolved — the honest
    limit in the module docstring."""
    text = str(token).strip().strip("'\"")
    if not text or text.startswith("-"):
        return None
    if text.startswith(("http://", "https://", "ftp://", "git@")):
        return None
    if text == "$PWD":
        text = "."
    elif text.startswith("$PWD/"):
        text = text[len("$PWD/"):]
    elif "$" in text:
        return None  # a path built from a variable: not identifiable
    glob_at = min((text.find(c) for c in _GLOB_CHARS if c in text),
                  default=-1)
    if glob_at >= 0:
        text = text[:glob_at].rstrip("/")
        if not text:
            return None
    return covering_entry(markings, root, text)


def _token_ancestor(markings: ProductionMarkings, root, token) -> bool:
    text = str(token).strip().strip("'\"")
    glob_at = min((text.find(c) for c in _GLOB_CHARS if c in text),
                  default=-1)
    if glob_at >= 0:
        text = text[:glob_at].rstrip("/")
    if not text or "$" in text:
        return False
    return _is_ancestor_of_marking(markings, root, text)


def _segment_tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _python_literal_refusal(markings: ProductionMarkings, root,
                            command: str) -> str | None:
    """The interpreter one-liner shapes a pattern can see (module
    docstring). Runs over the whole command text: the shapes are
    distinctive enough that a match IS the posture, whichever
    verb carried them."""
    for match in _PY_OPEN_WRITE.finditer(command):
        if any(c in match.group("mode") for c in "wax+"):
            entry = _token_entry(markings, root, match.group("path"))
            if entry is not None:
                return _command_refusal_text(
                    f"it opens '{match.group('path')}' for writing "
                    f"(production marking '{entry}')")
    for match in _PY_PATH_WRITE.finditer(command):
        entry = _token_entry(markings, root, match.group("path"))
        if entry is not None:
            return _command_refusal_text(
                f"it writes '{match.group('path')}' directly "
                f"(production marking '{entry}')")
    for match in _PY_REMOVE.finditer(command):
        entry = _token_entry(markings, root, match.group("path"))
        if entry is not None:
            return _command_refusal_text(
                f"it deletes '{match.group('path')}' "
                f"(production marking '{entry}')")
    for match in _PY_MOVE.finditer(command):
        for group in ("a", "b"):
            entry = _token_entry(markings, root, match.group(group))
            if entry is not None:
                return _command_refusal_text(
                    f"it moves '{match.group(group)}' "
                    f"(production marking '{entry}')")
    for match in _PY_COPY.finditer(command):
        entry = _token_entry(markings, root, match.group("b"))
        if entry is not None:
            return _command_refusal_text(
                f"it copies onto '{match.group('b')}' "
                f"(production marking '{entry}')")
    return None


def command_refusal(markings: ProductionMarkings, root,
                    command) -> str | None:
    """run_command's fence: the refusal text when the command
    text writes to production data in an identifiable posture,
    else None. Deterministic and text-only by design — see the
    module docstring for exactly what is and is not claimed."""
    if markings.error:
        return broken_refusal(markings)
    if not markings.active:
        return None
    text = str(command or "")
    refusal = _python_literal_refusal(markings, root, text)
    if refusal is not None:
        return refusal
    for segment in _SEGMENT_SPLIT.split(text):
        tokens = _segment_tokens(segment)
        if not tokens:
            continue
        # Redirection into a marked path writes it, whatever the
        # verb is — check first, for every segment.
        for i, token in enumerate(tokens):
            attached = _REDIRECT_ATTACHED.match(token)
            if _REDIRECT_TOKEN.match(token):
                if i + 1 < len(tokens):
                    entry = _token_entry(markings, root, tokens[i + 1])
                    if entry is not None:
                        return _command_refusal_text(
                            f"it redirects output to '{tokens[i + 1]}' "
                            f"(production marking '{entry}')")
            elif attached:
                entry = _token_entry(markings, root, attached.group(2))
                if entry is not None:
                    return _command_refusal_text(
                        f"it redirects output to '{attached.group(2)}' "
                        f"(production marking '{entry}')")
        # The verb: first token after any VAR=value prefixes and a
        # leading sudo.
        pos = 0
        while pos < len(tokens) and _ENV_ASSIGN.match(tokens[pos]):
            pos += 1
        if pos < len(tokens) and tokens[pos] == "sudo":
            pos += 1
        if pos >= len(tokens):
            continue
        verb = tokens[pos].rsplit("/", 1)[-1]
        args = [t for t in tokens[pos + 1:] if not t.startswith("-")]
        if verb in _ANY_ARG_WRITE or verb == "mv":
            for arg in args:
                entry = _token_entry(markings, root, arg)
                if entry is not None:
                    return _command_refusal_text(
                        f"'{verb}' would write or delete '{arg}' "
                        f"(production marking '{entry}')")
                if verb in _DESTRUCTIVE and _token_ancestor(
                        markings, root, arg):
                    return _command_refusal_text(
                        f"'{verb}' targets '{arg}', which contains "
                        f"production data marked in {MARKING_DISPLAY}")
        elif verb in _DEST_WRITE:
            if args:
                entry = _token_entry(markings, root, args[-1])
                if entry is not None:
                    return _command_refusal_text(
                        f"'{verb}' would write to '{args[-1]}' "
                        f"(production marking '{entry}')")
        elif verb == "sed":
            flags = [t for t in tokens[pos + 1:] if t.startswith("-")]
            in_place = any(t == "--in-place"
                           or t.startswith("--in-place=")
                           or (t.startswith("-i")
                               and not t.startswith("--"))
                           for t in flags)
            if in_place:
                for arg in args:
                    entry = _token_entry(markings, root, arg)
                    if entry is not None:
                        return _command_refusal_text(
                            f"'sed -i' would edit '{arg}' in place "
                            f"(production marking '{entry}')")
        elif verb == "dd":
            for token in tokens[pos + 1:]:
                if token.startswith("of="):
                    entry = _token_entry(markings, root, token[3:])
                    if entry is not None:
                        return _command_refusal_text(
                            f"'dd' would write to '{token[3:]}' "
                            f"(production marking '{entry}')")
        elif verb == "find":
            if "-delete" in tokens:
                for arg in args:
                    entry = _token_entry(markings, root, arg)
                    if entry is not None or _token_ancestor(
                            markings, root, arg):
                        return _command_refusal_text(
                            f"'find -delete' would delete under "
                            f"'{arg}', which reaches production data "
                            f"marked in {MARKING_DISPLAY}")
    return None


# ---------- the agent-facing surface ----------

def context_text(markings: ProductionMarkings) -> str:
    """The system-prompt block naming the boundary (\"\" when the
    project is unmarked, or marks nothing — an empty marking file
    is no boundary and earns no prompt noise)."""
    if not markings.present:
        return ""
    if markings.error:
        return (f"Production data ({MARKING_DISPLAY}): THE MARKING FILE "
                f"IS BROKEN — {markings.error}. Every write in this "
                f"project is being REFUSED until the owner fixes "
                f"{MARKING_DISPLAY}; reads still work. If this blocks "
                f"the task, tell the owner plainly which file to fix "
                f"instead of working around it.")
    if not markings.active:
        return ""
    lines = [f"Production data — READ-ONLY (marked by the owner in "
             f"{MARKING_DISPLAY}; you may read all of it freely, and "
             f"the tools REFUSE any write to it by rule — no approval "
             f"can override this):"]
    for entry in markings.paths:
        lines.append(f"- {entry or '.'} — this path and everything "
                     f"under it is production")
    for entry in markings.files:
        lines.append(f"- {entry} — this file is production")
    lines.append("Never write, append, move, delete, or run a command "
                 "that writes to production data. If a task needs "
                 "changes, work on a copy outside the marked paths.")
    return "\n".join(lines)
