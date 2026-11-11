"""Diff-first approvals — Note 14 #4 (pure stdlib, no Qt, like planning.py).

Note 14 #4, in the owner's words as recorded: approvals show the
CHANGE, not the whole file — faster to judge, and a bad edit can't
hide inside a big file. So when a pending approval is a file write,
the window computes the diff at decision time (from the file's
CURRENT on-disk content, read through the same workspace jail the
tools themselves use — forge/tools._inside, never a second jail) and
the ApprovalDialog leads with it; the phone deck's pending card
carries the compact form in the same description field it already
had, so the bridge payload is unchanged.

What counts as a file write here: a tool call whose arguments carry
a workspace-relative `path` and a computable full new content. In
the shipped registry that is the built-in `write_file` (path +
content) and, since item 8, `append_file` — whose full new content
is the current content with the chunk appended. `edit_file` — the plugin-registered edit tool the
worker and the SKRIBE gate already anticipate by name, though none
ships today — is handled defensively: a full `content` argument
diffs like a write_file, and an `old_text`/`new_text` replacement
pair diffs by applying the replacement to the on-disk content. Any
other shape yields None and the approval keeps its classic dialog,
byte for byte. create_skill / create_plugin also write files, but
into the app's global folders at slug-derived paths under a
different jail (forge/create_ext.py), not a workspace path argument
— since C26 they are diffed too, from the call arguments alone: the
created file's content is fully determined by the args (composed by
the same pure helpers create_ext writes with), and a create always
lands as a NEW file because the create itself refuses a collision,
so the preview is an all-new-file diff built without touching the
filesystem. Any arg shape the create would refuse earns no diff.
update_skill / update_plugin (S1-25b) are diffed the other way:
the proposed content is still composed from the args alone by the
same helpers, but the "before" is the file the update will replace,
read from the app's global folders through create_ext's own jail —
the update's refusal shapes (no such skill/plugin, blank fields,
bad slug) earn no diff, and a target that exists but cannot be read
shows the proposed content under the plain unavailable note, like
an unreadable workspace file. The two app folders reach this module
as optional arguments: the window resolves them (settings-derived),
hands them to the worker at turn start, and the worker passes them
through; absent folders (older constructions, test doubles) simply
earn the classic dialog for update calls.

Rendering law (owner's v2.1 verdict): NO GREEN, anywhere. Added
lines carry a "+" prefix on an amber/steel tint, removed lines a
"−" prefix on a red tint — the prefix plus the background does the
work; the dialog applies the tints, this module only produces text.

Size policy: the rendered diff is capped (MAX_DIFF_LINES) with an
explicit truncation marker carrying the true totals; the compact
form (COMPACT_DIFF_LINES) is header + counts + the first lines, for
the phone. The current file is read under the same 2 MB ceiling as
the read_file tool; anything unreadable (permissions, a directory
sitting at the path, too large, a jail refusal) produces a diff of
the proposed content against nothing, labeled plainly "current
content unavailable" — building a diff must never fail an approval.
"""
from __future__ import annotations
import difflib
import json
from dataclasses import dataclass
from pathlib import Path

from . import create_ext as create_mod
from . import tools as tools_mod

MAX_DIFF_LINES = 400        # rendered-diff cap (desktop dialog)
COMPACT_DIFF_LINES = 40     # compact-form cap (phone deck card)
MAX_CURRENT_BYTES = 2_000_000  # mirrors the read_file tool's ceiling

# Tools whose calls can carry a diffable workspace write. write_file
# and append_file (item 8) ship; edit_file is anticipated by name in
# worker.py / skribe.py. An append's proposed content is the current
# content plus the chunk, so its diff is the added tail.
WRITE_TOOLS = ("write_file", "edit_file", "append_file")

UNAVAILABLE_NOTE = "Current content unavailable — showing the proposed content as all-new."


@dataclass
class DiffResult:
    path: str
    added: int
    removed: int
    body: list[str]      # the first MAX_DIFF_LINES unified-diff lines
    total_lines: int     # the full unified-diff line count
    is_new: bool = False
    no_change: bool = False
    note: str | None = None

    @property
    def truncated(self) -> bool:
        return self.total_lines > len(self.body)

    @property
    def header(self) -> str:
        if self.no_change:
            return f"{self.path} — no changes"
        counts = f"+{self.added} −{self.removed}"
        if self.is_new:
            return f"{self.path} — new file, {counts}"
        return f"{self.path} — {counts}"

    def _head_lines(self) -> list[str]:
        lines = [self.header]
        if self.note:
            lines.append(self.note)
        if self.no_change:
            lines.append("No changes — the proposed content is identical "
                         "to the file on disk.")
        return lines

    def full_text(self) -> str:
        """Header + the diff, capped at MAX_DIFF_LINES body lines
        with an explicit marker (and the true totals) when capped."""
        lines = self._head_lines()
        if not self.no_change:
            lines.extend(self.body)
            if self.truncated:
                lines.append(
                    f"… diff truncated — showing {len(self.body)} of "
                    f"{self.total_lines} lines "
                    f"(+{self.added} −{self.removed} total)")
        return "\n".join(lines)

    def compact_text(self) -> str:
        """Header + counts + the first COMPACT_DIFF_LINES diff lines
        — the small-surface form (the phone deck's pending card)."""
        lines = self._head_lines()
        if not self.no_change:
            shown = self.body[:COMPACT_DIFF_LINES]
            lines.extend(shown)
            if self.total_lines > len(shown):
                lines.append(
                    f"… compact view — showing {len(shown)} of "
                    f"{self.total_lines} diff lines "
                    f"(+{self.added} −{self.removed} total); "
                    f"the full diff is on the PC.")
        return "\n".join(lines)


def build_diff(path: str, old_text: str | None, new_text: str, *,
               is_new: bool = False, note: str | None = None) -> DiffResult:
    """A unified diff of old_text -> new_text for `path`. old_text of
    None means "no current content" (a new file, or — with `note`
    set — content that exists but could not be read). Identical
    content is a first-class 'no changes' result, not an empty diff."""
    new_text = str(new_text or "")
    if old_text is not None and old_text == new_text:
        return DiffResult(path=path, added=0, removed=0, body=[],
                          total_lines=0, no_change=True, note=note)
    old_lines = [] if old_text is None else old_text.splitlines()
    new_lines = new_text.splitlines()
    body_all: list[str] = []
    for line in difflib.unified_diff(old_lines, new_lines, lineterm="", n=3):
        # Our own header replaces difflib's ---/+++ file lines.
        if not body_all and (line.startswith("---") or line.startswith("+++")):
            continue
        body_all.append(line)
    added = sum(1 for line in body_all if line.startswith("+"))
    removed = sum(1 for line in body_all if line.startswith("-"))
    return DiffResult(path=path, added=added, removed=removed,
                      body=body_all[:MAX_DIFF_LINES],
                      total_lines=len(body_all),
                      is_new=is_new, note=note)


def _proposed_content(name: str, args: dict, current: str | None) -> str | None:
    """The full new content a call proposes, or None when the call's
    shape doesn't determine one (then no diff is possible and the
    classic approval stands)."""
    if name == "write_file":
        return str(args["content"]) if "content" in args else None
    if name == "append_file":
        if "content" not in args:
            return None
        # The change an append proposes is the chunk landing at the
        # end of whatever is on disk now (nothing, for a new file).
        # When the current content is unavailable the caller passes
        # None and the chunk alone is shown under the plain note.
        return (current or "") + str(args["content"])
    if name == "edit_file":
        if "content" in args:
            return str(args["content"])
        old = args.get("old_text", args.get("old"))
        new = args.get("new_text", args.get("new"))
        if old is not None and new is not None and current is not None \
                and str(old) in current:
            return current.replace(str(old), str(new), 1)
    return None


def _read_current(workspace: str, rel_path: str) -> tuple[str | None, bool]:
    """(current_text, unavailable) for a workspace-relative path,
    read through the tools module's own jail. Missing file ->
    (None, False): a new file. Anything that exists but can't be
    read -> (None, True): the caller labels the diff accordingly
    instead of failing the approval."""
    try:
        target = tools_mod._inside(Path(workspace), rel_path)
    except Exception:
        return None, True
    if not target.exists():
        return None, False
    if not target.is_file():
        return None, True
    try:
        if target.stat().st_size > MAX_CURRENT_BYTES:
            return None, True
        return target.read_bytes().decode("utf-8", errors="replace"), False
    except OSError:
        return None, True


def _create_skill_diff(args: dict) -> DiffResult | None:
    """create_skill's approval preview (C26): the SKILL.md the call
    will write, composed from the args alone by the same helper the
    create writes with. Any arg shape the create itself would refuse
    (bad slug, blank description or body) earns no diff — the classic
    dialog and the create's own refusal text stand."""
    name, description, body = (args.get("name"), args.get("description"),
                               args.get("body"))
    if not all(isinstance(v, str) and v.strip()
               for v in (name, description, body)):
        return None
    slug = name.strip()
    if create_mod.validate_slug(slug) is not None:
        return None
    return build_diff(f"skills/{slug}/SKILL.md", None,
                      create_mod.skill_file_text(slug, description, body),
                      is_new=True)


def _create_plugin_diff(args: dict) -> DiffResult | None:
    """create_plugin's approval preview (C26): the plugin.py the call
    will write, composed from the args alone. plugin.json is shell
    boilerplate the dialog's description text already covers — the
    code is the substance being approved. Refusal shapes earn no
    diff, exactly as in _create_skill_diff."""
    name, code = args.get("name"), args.get("code")
    if not (isinstance(name, str) and name.strip()):
        return None
    if not (isinstance(code, str) and code.strip()):
        return None
    slug = name.strip()
    if create_mod.validate_slug(slug) is not None:
        return None
    return build_diff(f"plugins/{slug}/plugin.py", None,
                      create_mod.plugin_file_text(code), is_new=True)


def _read_app_current(base_dir, rel_path: str) -> tuple[str | None, bool]:
    """(current_text, unavailable) for a path inside one of the
    app's global folders (skills / plugins), contained by
    create_ext's own jail — the same containment the create/update
    tools themselves enforce. Missing file -> (None, False);
    anything that exists but can't be read -> (None, True)."""
    try:
        target = (Path(base_dir) / rel_path).resolve()
        if not create_mod._inside(Path(base_dir), target):
            return None, True
    except Exception:
        return None, True
    if not target.exists():
        return None, False
    if not target.is_file():
        return None, True
    try:
        if target.stat().st_size > MAX_CURRENT_BYTES:
            return None, True
        return target.read_bytes().decode("utf-8", errors="replace"), False
    except OSError:
        return None, True


def _update_skill_diff(args: dict, skills_dir) -> DiffResult | None:
    """update_skill's approval preview (S1-25b): the SKILL.md the
    call will write, diffed against the one on disk — an update
    approved blind was the finding; the owner now sees old vs new.
    The update's own refusal shapes (bad slug, blank description or
    body, no such skill) earn no diff: the classic dialog and the
    update's refusal text stand."""
    if skills_dir is None:
        return None
    name, description, body = (args.get("name"), args.get("description"),
                               args.get("body"))
    if not all(isinstance(v, str) and v.strip()
               for v in (name, description, body)):
        return None
    slug = name.strip()
    if create_mod.validate_slug(slug) is not None:
        return None
    proposed = create_mod.skill_file_text(slug, description, body)
    label = f"skills/{slug}/SKILL.md"
    current, unavailable = _read_app_current(skills_dir, f"{slug}/SKILL.md")
    if unavailable:
        return build_diff(label, None, proposed, note=UNAVAILABLE_NOTE)
    if current is None:
        return None
    return build_diff(label, current, proposed)


def _current_plugin_description(plugins_dir, slug: str) -> str | None:
    """The description the plugin's plugin.json currently records,
    read through _read_app_current (create_ext's own jail, so a
    symlinked folder escaping the root yields nothing), or None
    when it cannot be read — then no description change can be
    claimed in the preview."""
    current, unavailable = _read_app_current(plugins_dir,
                                             f"{slug}/plugin.json")
    if unavailable or current is None:
        return None
    try:
        meta = json.loads(current)
    except Exception:
        return None
    if isinstance(meta, dict):
        return str(meta.get("description", ""))
    return None


def _description_change_note(old: str | None, new: str) -> str | None:
    """The preview's plain naming of an update_plugin description
    change (S7 refinement) — update_plugin rewrites plugin.json's
    description along with the code, so a changed one is part of
    what the owner is approving and must not pass silently. None
    when unchanged, or when the old one could not be read. Both
    sides are compared in the collapsed one-line form the update
    stores."""
    if old is None:
        return None
    old, new = " ".join(str(old).split()), " ".join(str(new).split())
    if old == new:
        return None

    def _short(text: str) -> str:
        return text if len(text) <= 120 else text[:117] + "…"

    return (f"Description changes too — from “{_short(old)}” "
            f"to “{_short(new)}” (plugin.json).")


def _update_plugin_diff(args: dict, plugins_dir) -> DiffResult | None:
    """update_plugin's approval preview (S1-25b): the plugin.py the
    call will write, diffed against the one on disk — the code is
    the substance being approved, exactly as in _create_plugin_diff.
    update_plugin also rewrites plugin.json's description, so a
    changed description is named in the diff's note line (S7
    refinement); the rebuilt tool list follows from the code diff
    itself (the HANDLERS declaration). Refusal shapes earn no diff,
    as above."""
    if plugins_dir is None:
        return None
    name, code = args.get("name"), args.get("code")
    if not (isinstance(name, str) and name.strip()):
        return None
    if not (isinstance(code, str) and code.strip()):
        return None
    slug = name.strip()
    if create_mod.validate_slug(slug) is not None:
        return None
    proposed = create_mod.plugin_file_text(code)
    label = f"plugins/{slug}/plugin.py"
    description = args.get("description")
    desc_note = None
    if isinstance(description, str) and description.strip():
        desc_note = _description_change_note(
            _current_plugin_description(plugins_dir, slug), description)
    current, unavailable = _read_app_current(plugins_dir, f"{slug}/plugin.py")
    if unavailable:
        result = build_diff(label, None, proposed, note=UNAVAILABLE_NOTE)
    elif current is None:
        return None
    else:
        result = build_diff(label, current, proposed)
    if desc_note is None:
        return result
    if result.no_change:
        # Code identical, description changed: NOT "no changes" —
        # the preview says what the update still does, as a +0 −0
        # result carrying the note.
        return DiffResult(path=label, added=0, removed=0, body=[],
                          total_lines=0,
                          note=(desc_note
                                + " The plugin code itself is unchanged."))
    result.note = (desc_note if result.note is None
                   else result.note + "\n" + desc_note)
    return result


def diff_for_approval(name: str, args: dict, workspace: str,
                      skills_dir=None, plugins_dir=None) -> DiffResult | None:
    """The diff for a pending approval, or None when the call is not
    a diffable file write (the caller then shows the classic dialog
    and the classic deck description, unchanged). Computed in the
    worker at ask time (audit fix #18) and carried to the window's
    approval gate — a call a remembered rule allows never reaches
    the gate, so it never builds a diff."""
    if not isinstance(args, dict):
        return None
    if name == "create_skill":
        return _create_skill_diff(args)
    if name == "create_plugin":
        return _create_plugin_diff(args)
    if name == "update_skill":
        return _update_skill_diff(args, skills_dir)
    if name == "update_plugin":
        return _update_plugin_diff(args, plugins_dir)
    if name not in WRITE_TOOLS:
        return None
    path = str(args.get("path", "")).strip()
    if not path:
        return None
    current, unavailable = _read_current(workspace, path)
    if unavailable:
        proposed = _proposed_content(name, args, None)
        if proposed is None:
            return None
        return build_diff(path, None, proposed, note=UNAVAILABLE_NOTE)
    proposed = _proposed_content(name, args, current)
    if proposed is None:
        return None
    if current is None:
        return build_diff(path, None, proposed, is_new=True)
    return build_diff(path, current, proposed)
