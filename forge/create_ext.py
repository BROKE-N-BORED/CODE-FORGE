"""Global skill / plugin creation — Note 22.

The self-extension instruction (Note 18) tells the agent to create the
skills and tools it needs, but the file tools are jailed to the active
workspace — so an agent working in a Desktop project saved its new
skills INTO that project folder, where no other project could ever see
them (owner's bug report, 2026-11-06). Skills and plugins are GLOBAL:
they live in the app's own skills/ and plugins/ folders, beside the
shell, and are available in every project.

This module is the shell-side machinery, kept free of Qt so it is unit
testable in isolation (same pattern as forge/skribe.py):

- create_skill / create_plugin — the tools' backing functions. They
  write ONLY under the injected app skills/plugins dirs (resolved-path
  paranoia check included), validate names as slugs, refuse name
  collisions, and refuse plugin code that does not compile or does not
  declare HANDLERS. Every refusal is a plain result string, never an
  exception — a 2B model must be able to read why and fix its call.
- update_skill / update_plugin — the update half. The owner's
  instruction said the agent may update existing skills/plugins too,
  but create_* refuses collisions, so updating had no tool at all.
  update_* refuses when the item does NOT exist (creating is
  create_*'s job), runs the same validations BEFORE writing a byte —
  a refused plugin update leaves the existing plugin byte-identical —
  and update_plugin edits plugin.json rather than regenerating it,
  so fields it does not own (kind, config_keys, hand-set per-tool
  descriptions and approval flags) survive the update.
- find_project_local / house_project_local — rescan also looks for
  skills/plugins saved inside the workspace (where an older session, or
  a hand save, put them). They are detected and reported as
  project-only, never silently loaded as globals; the owner can house
  them into the app folders from Customize.
- project_local_hint — a write_file that lands at a skill/plugin-shaped
  path inside the workspace gets a hint appended to its result, so the
  model learns the global tools exist. Hint only; the write stands.
"""
from __future__ import annotations
import ast
import json
import re
import shutil
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Where a model plausibly saves an extension inside a project folder.
LOCAL_SUBDIRS = ("skills", "plugins")

CREATE_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_skill",
        "description": ("Create a NEW skill in the Forge's GLOBAL skill directory (the app's own "
                        "skills/ folder — available in every project, NOT inside the current "
                        "project folder). Use this instead of write_file whenever you create a "
                        "skill. The new skill starts DISABLED — the owner enables it in "
                        "Customize before load_skill will load it. Requires user approval."),
        "parameters": {"type": "object",
                       "properties": {
                           "name": {"type": "string",
                                    "description": "Skill slug: lowercase letters, digits and hyphens (e.g. 'pdf-tables')"},
                           "description": {"type": "string",
                                           "description": "One line: what the skill does AND when to use it — this is the only part shown at rest in the skill index"},
                           "body": {"type": "string",
                                    "description": "The skill instructions (Markdown): the procedure, in order, terse and operational"}},
                       "required": ["name", "description", "body"]},
    },
}

CREATE_PLUGIN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_plugin",
        "description": ("Create a NEW plugin in the Forge's GLOBAL plugins directory (the app's "
                        "own plugins/ folder — available in every project, NOT inside the "
                        "current project folder). Use this instead of write_file whenever you "
                        "create a plugin or tool. The code must be a complete plugin.py: it "
                        "must define HANDLERS = {'tool_name': handler_function, ...} where each "
                        "handler takes (args, ctx) and returns a string. You may also declare "
                        "TOOL_SCHEMAS = {'tool_name': {JSON-schema parameters object}, ...} at "
                        "the top level of the code to give a tool arguments; without it a tool "
                        "takes no arguments. Tools declared by the "
                        "plugin require approval. The new plugin starts DISABLED — the owner "
                        "enables it in Customize. Requires user approval."),
        "parameters": {"type": "object",
                       "properties": {
                           "name": {"type": "string",
                                    "description": "Plugin slug: lowercase letters, digits and hyphens (e.g. 'weather-fetch')"},
                           "description": {"type": "string",
                                           "description": "One line: what the plugin does and when to use it"},
                           "code": {"type": "string",
                                    "description": "Complete Python source of plugin.py, defining HANDLERS"}},
                       "required": ["name", "description", "code"]},
    },
}

UPDATE_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_skill",
        "description": ("Update an EXISTING skill in the Forge's GLOBAL skill directory (the "
                        "app's own skills/ folder). Use this when the skill name already "
                        "exists — create_skill refuses names that are taken and points "
                        "here. Replaces the skill's description and instructions "
                        "(SKILL.md) wholesale; other files in the skill's folder are left "
                        "alone. Refuses if no skill with that name exists. Whether the "
                        "skill is enabled stays the owner's Customize choice — an update "
                        "never switches a skill on or off. Requires user approval."),
        "parameters": {"type": "object",
                       "properties": {
                           "name": {"type": "string",
                                    "description": "The existing skill's slug: lowercase letters, digits and hyphens (e.g. 'pdf-tables')"},
                           "description": {"type": "string",
                                           "description": "The new one line: what the skill does AND when to use it — this replaces the old index line"},
                           "body": {"type": "string",
                                    "description": "The new skill instructions (Markdown), complete: this replaces the old body, it is not appended"}},
                       "required": ["name", "description", "body"]},
    },
}

UPDATE_PLUGIN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_plugin",
        "description": ("Update an EXISTING plugin in the Forge's GLOBAL plugins directory "
                        "(the app's own plugins/ folder). Use this when the plugin name "
                        "already exists — create_plugin refuses names that are taken and "
                        "points here. The new code passes the SAME validations as "
                        "create_plugin (it must compile, declare HANDLERS with string keys, "
                        "and any TOOL_SCHEMAS must be readable) BEFORE anything is "
                        "written, so a refused update leaves the current plugin untouched. "
                        "Its plugin.json settings (kind, config keys, per-tool approval "
                        "flags) are preserved — only the code, the description and the tool "
                        "list change. One state change is deliberate: if the plugin is "
                        "currently ENABLED, a successful update switches it OFF — the "
                        "owner re-enables it in Customize after reviewing the new code. "
                        "Refuses if no "
                        "plugin with that name exists. Requires user approval."),
        "parameters": {"type": "object",
                       "properties": {
                           "name": {"type": "string",
                                    "description": "The existing plugin's slug: lowercase letters, digits and hyphens (e.g. 'weather-fetch')"},
                           "description": {"type": "string",
                                           "description": "The new one line: what the plugin does and when to use it"},
                           "code": {"type": "string",
                                    "description": "The complete new Python source of plugin.py, defining HANDLERS — it replaces the old code wholesale"}},
                       "required": ["name", "description", "code"]},
    },
}

PROJECT_LOCAL_HINT = (
    "Hint from the shell: this path looks like a skill or plugin, but it was saved "
    "INSIDE the current project folder, so it is project-local only — it will not "
    "be available in other projects. Skills and plugins are global: to create one "
    "for every project, use create_skill / create_plugin instead of write_file.")


def validate_slug(name: str) -> str | None:
    """An error string if `name` is not a usable slug, else None."""
    name = (name or "").strip()
    if not name:
        return "name is empty."
    if "/" in name or "\\" in name or name in (".", ".."):
        return f"'{name}' contains a path separator — give a plain slug, not a path."
    if not SLUG_RE.match(name):
        return (f"'{name}' is not a valid slug. Use lowercase letters, digits and "
                "hyphens only, e.g. 'pdf-tables'.")
    return None


def _inside(base: Path, target: Path) -> bool:
    base, target = base.resolve(), target.resolve()
    return target == base or base in target.parents


def skill_file_text(name: str, description: str, body: str) -> str:
    """The exact SKILL.md content a create_skill writes: `name:` line,
    `description:` line (collapsed to one line, always), blank line,
    the stripped body, one trailing newline. Pure composition — the
    approval diff preview (forge/diffview.py) builds from this same
    function so what the owner approves is what lands, byte for byte."""
    description = " ".join(str(description or "").split())  # one line, always
    return f"name: {name}\ndescription: {description}\n\n{str(body).strip()}\n"


def plugin_file_text(code: str) -> str:
    """The exact plugin.py content a create_plugin writes: the code
    as given, with a trailing newline added when missing. Pure
    composition — see skill_file_text for why it exists."""
    code = str(code or "")
    return code if code.endswith("\n") else code + "\n"


def create_skill(skills_dir, name: str, description: str, body: str) -> tuple[bool, str]:
    """Write <skills_dir>/<name>/SKILL.md in the directory's own format
    (line 1 `name:`, line 2 `description:`, blank line, body — the exact
    shape forge/skills.py parses). Returns (ok, result-for-the-model)."""
    name = (name or "").strip()
    if problem := validate_slug(name):
        return False, f"create_skill refused: {problem}"
    description = " ".join(str(description or "").split())  # one line, always
    if not description:
        return False, "create_skill refused: description is empty — it is the skill's index line."
    if not str(body or "").strip():
        return False, "create_skill refused: body is empty — a skill needs instructions."
    base = Path(skills_dir)
    folder = base / name
    if not _inside(base, folder):
        return False, "create_skill refused: target would land outside the app skill directory."
    if folder.exists():
        return False, (f"create_skill refused: a skill named '{name}' already exists at {folder}. "
                       "Pick another name, or update the existing skill instead.")
    # Sweep R156: the module's contract is (ok, message) tuples —
    # a mkdir race or a failed write used to escape as a raw
    # exception, and a write that failed after the mkdir left a
    # torn folder behind that blocked the name forever ("already
    # exists" on every retry).
    try:
        folder.mkdir(parents=True)
    except FileExistsError:
        return False, (f"create_skill refused: a skill named '{name}' already exists at {folder}. "
                       "Pick another name, or update the existing skill instead.")
    target = folder / "SKILL.md"
    try:
        target.write_text(skill_file_text(name, description, body), encoding="utf-8")
    except OSError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        return False, f"create_skill refused: could not write {target} ({exc})."
    return True, (f"Created GLOBAL skill '{name}' at {target} — it lives in the Forge's own "
                  "skill directory, so it is available in EVERY project, not just this one. "
                  "It starts DISABLED: the owner enables it in Customize before it can be "
                  "loaded with load_skill.")


def update_skill(skills_dir, name: str, description: str, body: str) -> tuple[bool, str]:
    """Rewrite an EXISTING global skill's SKILL.md — the update half
    of Note 22, mirroring create_skill's checks with the existence
    rule inverted (create refuses a taken name; update refuses a
    missing one). Only SKILL.md is rewritten: knowledge files beside
    it in the skill's folder are the skill's own and stay untouched.
    Returns (ok, result-for-the-model)."""
    name = (name or "").strip()
    if problem := validate_slug(name):
        return False, f"update_skill refused: {problem}"
    description = " ".join(str(description or "").split())  # one line, always
    if not description:
        return False, "update_skill refused: description is empty — it is the skill's index line."
    if not str(body or "").strip():
        return False, "update_skill refused: body is empty — a skill needs instructions."
    base = Path(skills_dir)
    folder = base / name
    if not _inside(base, folder):
        return False, "update_skill refused: target would land outside the app skill directory."
    target = folder / "SKILL.md"
    if not target.is_file():
        return False, (f"update_skill refused: no skill named '{name}' exists at {folder}. "
                       "Use create_skill to make a new one.")
    target.write_text(skill_file_text(name, description, body), encoding="utf-8")
    return True, (f"Updated GLOBAL skill '{name}' at {target} — the new description and "
                  "instructions replace the old ones, and the skill is available in EVERY "
                  "project, not just this one. Whether it is enabled is the owner's "
                  "Customize choice and is unchanged.")


def declared_tools(code: str) -> list[str] | None:
    """Tool names from a top-level HANDLERS = {...} dict with string keys,
    read by AST so plugin code is never executed by the shell at
    creation time. None when no such declaration exists."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "HANDLERS" for t in node.targets):
            if isinstance(node.value, ast.Dict):
                names = [k.value for k in node.value.keys
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                return names or None
            return None
    return None


def declared_schemas(code: str) -> tuple[dict | None, str | None]:
    """Tool parameter schemas from a top-level TOOL_SCHEMAS = {...}
    declaration, read by AST so plugin code is never executed by the
    shell at creation time (the declared_tools pattern). Returns
    (schemas, problem):

    - (None, None) — no TOOL_SCHEMAS assignment exists; the caller
      keeps the historic empty-parameter behaviour.
    - (dict, None) — read cleanly: {tool_name: parameters-dict}.
    - (None, reason) — an assignment EXISTS but cannot be read as a
      plain dict literal mapping tool names (strings) to dicts; the
      caller refuses the create rather than silently dropping the
      schemas the model tried to declare.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None, None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "TOOL_SCHEMAS" for t in node.targets):
            if not isinstance(node.value, ast.Dict):
                return None, "TOOL_SCHEMAS is not a dict literal"
            try:
                value = ast.literal_eval(node.value)
            except Exception as exc:
                return None, f"TOOL_SCHEMAS is not a plain literal ({exc})"
            if not isinstance(value, dict):
                return None, "TOOL_SCHEMAS is not a dict"
            for key, val in value.items():
                if not isinstance(key, str):
                    return None, f"a TOOL_SCHEMAS key ({key!r}) is not a tool name string"
                if not isinstance(val, dict):
                    return None, f"the TOOL_SCHEMAS entry for '{key}' is not an object"
            return value, None
    return None, None


def _plugin_payload(verb: str, name: str, description: str, code: str):
    """The plugin-code validation gate, shared VERBATIM by create and
    update so an update can never slip past a weaker copy of the
    checks: the code must compile, declare HANDLERS with string keys,
    and any TOOL_SCHEMAS declaration must be readable (audit C30 — the
    declaration is the model's only channel for giving its tools
    arguments; an unreadable one refuses rather than silently
    dropping the schemas). Pure checks only, nothing is written here,
    so a refusal leaves any existing plugin byte-identical. Returns
    (refusal, tools, schemas): refusal is the full '<verb> refused: …'
    string or None."""
    try:
        compile(code, f"{name}/plugin.py", "exec")
    except SyntaxError as exc:
        return (f"{verb} refused: the code does not compile — "
                f"{exc.msg} (line {exc.lineno}). Nothing was written; fix the code and retry."), None, None
    tools = declared_tools(code)
    if not tools:
        return (f"{verb} refused: the code does not declare HANDLERS = "
                "{'tool_name': handler, ...} with string keys at the top level, so the "
                "shell could not register any tool from it. Nothing was written."), None, None
    schemas, schema_problem = declared_schemas(code)
    if schema_problem:
        return (f"{verb} refused: TOOL_SCHEMAS could not be read — "
                f"{schema_problem}. Nothing was written; fix it or drop TOOL_SCHEMAS."), None, None
    return None, tools, schemas or {}


def _created_plugin_meta(name: str, description: str, tools: list, schemas: dict) -> dict:
    """The plugin.json a create writes, in the exact shape
    forge/plugins.py loads (id / name / kind / description / access /
    tools[])."""
    return {
        "id": name,
        "name": name.replace("-", " ").title(),
        "kind": "plugin",
        "description": description,
        "access": ("Agent-created plugin (Note 22). Every tool it registers requires "
                   "approval. It starts DISABLED until the owner enables it in Customize."),
        "tools": [{"name": tool,
                   "description": f"Tool from agent-created plugin '{name}': {description}",
                   "parameters": schemas.get(tool, {"type": "object", "properties": {}}),
                   "requires_approval": True} for tool in tools],
    }


def create_plugin(plugins_dir, name: str, description: str, code: str) -> tuple[bool, str]:
    """Scaffold <plugins_dir>/<name>/ with plugin.py (the supplied code)
    + plugin.json, following the exact shape forge/plugins.py loads
    (id / name / kind / description / access / tools[]). The plugin is
    NOT enabled by this function — the caller folds it into the same
    new-plugin-starts-DISABLED machinery rescan uses (Note 18)."""
    name = (name or "").strip()
    if problem := validate_slug(name):
        return False, f"create_plugin refused: {problem}"
    description = " ".join(str(description or "").split())
    if not description:
        return False, "create_plugin refused: description is empty."
    code = str(code or "")
    if not code.strip():
        return False, "create_plugin refused: no code given."
    refusal, tools, schemas = _plugin_payload("create_plugin", name, description, code)
    if refusal:
        return False, refusal
    base = Path(plugins_dir)
    folder = base / name
    if not _inside(base, folder):
        return False, "create_plugin refused: target would land outside the app plugins directory."
    if folder.exists():
        return False, (f"create_plugin refused: a plugin named '{name}' already exists at {folder}. "
                       "Pick another name, or update the existing plugin instead.")
    meta = _created_plugin_meta(name, description, tools, schemas)
    # Sweep R156: same tuple contract as create_skill — a mkdir
    # race refuses in words, and a failed write rolls the folder
    # back instead of leaving a torn plugin behind.
    try:
        folder.mkdir(parents=True)
    except FileExistsError:
        return False, (f"create_plugin refused: a plugin named '{name}' already exists at {folder}. "
                       "Pick another name, or update the existing plugin instead.")
    try:
        (folder / "plugin.py").write_text(plugin_file_text(code), encoding="utf-8")
        (folder / "plugin.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    except OSError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        return False, f"create_plugin refused: could not write the plugin files ({exc})."
    tool_list = ", ".join(tools)
    return True, (f"Created GLOBAL plugin '{name}' at {folder} (tools: {tool_list}) — it lives "
                  "in the Forge's own plugins directory, so it is available in EVERY project, "
                  "not just this one. It starts DISABLED: the owner enables it in Customize "
                  "before its tools can be used.")


def update_plugin(plugins_dir, name: str, description: str, code: str) -> tuple[bool, str]:
    """Replace an EXISTING global plugin's code and description —
    the update half of Note 22, mirroring create_plugin with the
    existence rule inverted. The payload passes the exact gate
    create_plugin uses (_plugin_payload) BEFORE anything is written,
    so a refused update leaves the existing plugin byte-identical.
    plugin.json is EDITED, not regenerated: fields the update does
    not own (id, name, kind, access, config_keys, hand-set per-tool
    descriptions and approval flags) survive, while the tools list is
    rebuilt from the new code so the registry loads what the code
    actually declares. Enabled/disabled state lives in the owner's
    settings, not in these files, and is untouched."""
    name = (name or "").strip()
    if problem := validate_slug(name):
        return False, f"update_plugin refused: {problem}"
    description = " ".join(str(description or "").split())
    if not description:
        return False, "update_plugin refused: description is empty."
    code = str(code or "")
    if not code.strip():
        return False, "update_plugin refused: no code given."
    base = Path(plugins_dir)
    folder = base / name
    if not _inside(base, folder):
        return False, "update_plugin refused: target would land outside the app plugins directory."
    meta_file, code_file = folder / "plugin.json", folder / "plugin.py"
    if not (meta_file.is_file() and code_file.is_file()):
        return False, (f"update_plugin refused: no plugin named '{name}' exists at {folder}. "
                       "Use create_plugin to make a new one.")
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            raise ValueError("plugin.json is not an object")
    except Exception as exc:
        return False, (f"update_plugin refused: the existing plugin.json for '{name}' could "
                       f"not be read ({exc}), so an update might lose its settings. "
                       "Nothing was written.")
    refusal, tools, schemas = _plugin_payload("update_plugin", name, description, code)
    if refusal:
        return False, refusal
    raw_tools = meta.get("tools")
    old_tools = ({t.get("name"): t for t in raw_tools if isinstance(t, dict)}
                 if isinstance(raw_tools, list) else {})
    old_description = str(meta.get("description", ""))
    new_tools = []
    for tool in tools:
        old = old_tools.get(tool)
        if old is None:
            # A tool the new code adds: the create shape — approval on.
            new_tools.append({"name": tool,
                              "description": f"Tool from agent-created plugin '{name}': {description}",
                              "parameters": schemas.get(tool, {"type": "object", "properties": {}}),
                              "requires_approval": True})
            continue
        entry = dict(old)
        entry["name"] = tool
        if str(old.get("description", "")) == \
                f"Tool from agent-created plugin '{name}': {old_description}":
            # The old line was create boilerplate naming the old
            # description — refresh it; a hand-written one stays.
            entry["description"] = f"Tool from agent-created plugin '{name}': {description}"
        if tool in schemas:
            entry["parameters"] = schemas[tool]
        entry.setdefault("parameters", {"type": "object", "properties": {}})
        entry.setdefault("requires_approval", True)
        new_tools.append(entry)
    meta["description"] = description
    meta["tools"] = new_tools
    code_file.write_text(plugin_file_text(code), encoding="utf-8")
    meta_file.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    tool_list = ", ".join(tools)
    return True, (f"Updated GLOBAL plugin '{name}' at {folder} (tools: {tool_list}) — the new "
                  "code and description replace the old ones, and the plugin is available "
                  "in EVERY project, not just this one, as soon as the shell rescans "
                  "(automatic). Whether it is enabled is the owner's Customize choice.")


def _scan_local_root(root: Path, kind: str) -> list[dict]:
    items = []
    if not root.is_dir():
        return items
    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        if kind == "skill" and (folder / "SKILL.md").is_file():
            items.append({"kind": "skill", "name": folder.name, "folder": folder})
        elif kind == "plugin" and (folder / "plugin.json").is_file() and (folder / "plugin.py").is_file():
            items.append({"kind": "plugin", "name": folder.name, "folder": folder})
    return items


def find_project_local(workspace, app_skills_dir=None, app_plugins_dir=None) -> list[dict]:
    """Skills/plugins saved INSIDE the workspace (project-local). A root
    that IS the corresponding app dir (the app folder itself opened as
    the project) is not 'local' — those are the globals themselves."""
    ws = Path(workspace)
    found: list[dict] = []
    for sub in ("skills", ".forge/skills"):
        root = ws / sub
        if app_skills_dir is not None and root.resolve() == Path(app_skills_dir).resolve():
            continue
        found += _scan_local_root(root, "skill")
    for sub in ("plugins", ".forge/plugins"):
        root = ws / sub
        if app_plugins_dir is not None and root.resolve() == Path(app_plugins_dir).resolve():
            continue
        found += _scan_local_root(root, "plugin")
    return found


def house_project_local(workspace, skills_dir, plugins_dir) -> tuple[list[str], list[str]]:
    """Move every project-local item into the app folders. Returns
    (moved labels, skipped labels-with-reasons). A name collision skips
    that item (the global copy wins; nothing is overwritten)."""
    moved: list[str] = []
    skipped: list[str] = []
    for item in find_project_local(workspace, skills_dir, plugins_dir):
        base = Path(skills_dir) if item["kind"] == "skill" else Path(plugins_dir)
        target = base / item["name"]
        if target.exists():
            skipped.append(f"{item['kind']} '{item['name']}' — a global one with that name already exists")
            continue
        if not _inside(base, target):
            skipped.append(f"{item['kind']} '{item['name']}' — target would land outside the app directory")
            continue
        try:
            base.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item["folder"]), str(target))
            moved.append(f"{item['kind']} '{item['name']}'")
        except Exception as exc:
            skipped.append(f"{item['kind']} '{item['name']}' — move failed: {exc}")
    return moved, skipped


def project_local_hint(workspace, path_arg) -> str | None:
    """The write_file hint: the PROJECT_LOCAL_HINT text when a written
    path inside the workspace looks like a skill/plugin, else None."""
    root = Path(workspace).expanduser().resolve()
    try:
        target = (root / str(path_arg or "")).resolve()
    except Exception:
        return None
    if target == root or root not in target.parents:
        return None
    rel = target.relative_to(root)
    if "skills" in rel.parts or "plugins" in rel.parts or rel.name == "SKILL.md":
        return PROJECT_LOCAL_HINT
    return None
