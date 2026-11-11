"""Plugins and connectors (Forge v2, Notes 4 + 6).

A plugin is a folder under plugins/ with:
  plugin.json — declares id, name, kind ("plugin" or "connector"), a
                one-line description, the access it needs, and its tools
                (name, description, parameters, requires_approval).
  plugin.py   — defines HANDLERS = {tool_name: fn(args, ctx) -> str}.

Skills are read; plugins are RUN — they register real tools with the
shell. Every plugin tool passes the same approval gate as built-in
tools when its declaration says requires_approval. Connector auth lives
in the app config (connectors.json in the app data dir), never in
project files or prompts.

Loading is metadata-first: every folder's plugin.json is read so the
shell can list the plugin, but plugin.py only EXECUTES for a plugin
that is enabled and known — a disabled or brand-new plugin's code
never runs (Note 22: new plugins start DISABLED; S6/audit S1-25a:
with no load history the SHELL seeds the known set from
BUNDLED_PLUGIN_IDS, so an unseeded folder's code does not execute
on first load either). Handlers see a
per-plugin view of the context whose `config` holds only the
connectors.json keys the manifest lists in "config_keys"; a plugin
that declares none sees an empty config.
"""
from __future__ import annotations
import contextlib
import importlib.util
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path


# S6 (audit S1-25a): the plugins that SHIP with Forge — the manifest
# ids of the folders under plugins/ at build time. On an install with
# no known-plugins history, the shell seeds its history from this
# list: a seeded plugin keeps the historic first-run behaviour,
# while any OTHER plugin folder that appears (an update zip's
# payload, a hand copy, anything) starts DISABLED instead of being
# grandfathered into execution on first load. Keep this list exactly
# the shipped folders' ids — a bundled plugin missing from it would
# start disabled on fresh installs. (Assembly port note: the S6
# build's own tree shipped six folders; this tree ships seven —
# "cloudstore" landed with DEV #70 after the security stack
# branched — so the seed names all seven shipped ids, which is what
# S6's own seed test computes from this tree's plugins/ folder.)
BUNDLED_PLUGIN_IDS = frozenset({
    "browse",
    "cloudstore",
    "github",
    "huggingface",
    "mcp",
    "resource-governor",
    "tester",
})


@dataclass
class PluginContext:
    workspace: str
    base_url: str = "http://127.0.0.1:11434"
    current_model: str = ""
    tester_model: str = ""
    # Item #53: the owner's tester cycle cap (settings
    # `tester_max_cycles`), refreshed by the shell each turn the
    # same way tester_model is; 0 = unset — the tester plugin then
    # keeps its own built-in cap.
    tester_max_cycles: int = 0
    # Browser merge (2026-11-11): the owner's Research web engine
    # choice — settings `research_web_engine`: "" / "browse" = the
    # #55 Browse connector (today's behavior), "forge-browser" =
    # the built-in forge/browser package — and the package's CDP
    # engine choice (settings `forge_browser_engine`: "auto" /
    # "chromium" / "lightpanda"). Both are refreshed by the shell
    # each turn the same way tester_max_cycles is; the Browse
    # plugin reads them to route behind the browse seam.
    research_web_engine: str = ""
    forge_browser_engine: str = "auto"
    governing_roster: str = ""  # owner's roster extension (bite #3)
    config: dict = field(default_factory=dict)
    log: object = None  # callable(str) or None
    cancel_event: object = None  # threading.Event of the live turn (Stop)


# Item #64 (parallel turns): the per-thread PluginContext override.
# A worker dispatching a plugin tool wraps the call in scoped_ctx()
# with its own turn's context snapshot; _PluginContextView then
# delegates to that snapshot for the duration of the call. Threads
# that never enter scoped_ctx (the GUI thread, every pre-#64 call
# path) see no override at all.
_ctx_local = threading.local()


@contextlib.contextmanager
def scoped_ctx(ctx):
    """Make `ctx` the plugin context this thread's views delegate
    to, for the duration of the block. Nesting restores the outer
    value on exit."""
    previous = getattr(_ctx_local, "ctx", None)
    _ctx_local.ctx = ctx
    try:
        yield
    finally:
        _ctx_local.ctx = previous


@dataclass
class Plugin:
    pid: str
    name: str
    kind: str
    description: str
    folder: Path
    tools: list[dict]
    handlers: dict


class _PluginContextView:
    """A per-plugin view over the LIVE PluginContext.

    Attribute reads delegate to the live context, so shell-side updates
    (workspace, current model, log…) flow through as they happen; only
    `config` is filtered: at call time it returns exactly the
    connectors.json keys named in the plugin's "config_keys" manifest
    list. A plugin that declares no config_keys sees an empty config.
    Writes delegate to the live context too (except the view's own
    internals)."""

    def __init__(self, base, config_keys):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_config_keys", tuple(config_keys or ()))

    def _effective_base(self):
        # Item #64: while a worker thread dispatches a plugin tool
        # under scoped_ctx(), the view delegates to THAT turn's own
        # PluginContext snapshot instead of the window's live one —
        # parallel turns each read their own workspace / tester
        # seat / cancel event. No override is ever set outside a
        # worker's dispatch, so every other caller sees the live
        # context exactly as before.
        override = getattr(_ctx_local, "ctx", None)
        return override if override is not None else self._base

    def __getattr__(self, name):
        base = self._effective_base()
        if name == "config":
            live = getattr(base, "config", None) or {}
            out = {k: live[k] for k in self._config_keys if k in live}
            # S2 (audit S1-13): secret keys a plugin DECLARES in its
            # config_keys now live in the encrypted vault, not in
            # connectors.json — resolve those from the vault (the
            # vault value wins over any stale file value).  Only
            # declared keys are ever looked up: a plugin that does
            # not declare a secret never receives it, and non-secret
            # keys keep coming from the file exactly as before.  The
            # lookup is fail-soft — a vault problem leaves the file
            # values in place.
            try:
                from . import vault as vault_mod
                for k in self._config_keys:
                    if k in vault_mod.SECRET_CONFIG_KEYS:
                        stored = vault_mod.get_secret(k)
                        if stored:
                            out[k] = stored
            except Exception:
                pass
            return out
        return getattr(base, name)

    def __setattr__(self, name, value):
        if name in ("_base", "_config_keys"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._effective_base(), name, value)


def load_plugins(plugins_dir: Path, ctx: PluginContext,
                 disabled=frozenset(), known=None) -> tuple[list[Plugin], dict]:
    """Returns (plugins, tool_registry) where tool_registry maps
    tool_name -> (schema, handler, requires_approval, plugin_name).

    Two passes. The METADATA pass reads every folder's plugin.json so
    the shell can list all plugins (Customize, known/disabled
    bookkeeping); a manifest that is unreadable or not a JSON object
    skips the folder, is logged, and never raises. The EXEC pass runs
    plugin.py ONLY for a plugin whose pid is not in `disabled` and
    that is known — `known` None means no history yet (everything
    present counts, the first-run grandfathering — the shell itself
    no longer relies on this since S6: with no stored history it
    passes the BUNDLED_PLUGIN_IDS seed as the history, so only
    bundled plugins grandfather); otherwise the pid must be listed
    in `known`. Non-executed plugins are returned with
    their declared tools and handlers={}. The registry is built from
    executed plugins only; on a tool-name collision the FIRST plugin
    (sorted folder order) keeps the name and the collision is logged.
    """
    plugins: list[Plugin] = []
    registry: dict = {}
    if not Path(plugins_dir).is_dir():
        return plugins, registry
    disabled = set(disabled or ())
    known_set = None if known is None else set(known)
    for folder in sorted(Path(plugins_dir).iterdir()):
        meta_file, code_file = folder / "plugin.json", folder / "plugin.py"
        if not (folder.is_dir() and meta_file.is_file() and code_file.is_file()):
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                raise ValueError(
                    f"plugin.json is a {type(meta).__name__}, not an object")
        except Exception as exc:
            if ctx.log:
                ctx.log(f"Plugin {folder.name} skipped: bad plugin.json ({exc})")
            continue
        declared = meta.get("tools", [])
        if not isinstance(declared, list):
            declared = []
        plugin = Plugin(pid=meta.get("id", folder.name), name=meta.get("name", folder.name),
                        kind=meta.get("kind", "plugin"),
                        description=meta.get("description", ""), folder=folder,
                        tools=[t for t in declared if isinstance(t, dict)],
                        handlers={})
        plugins.append(plugin)
        if plugin.pid in disabled or (known_set is not None
                                      and plugin.pid not in known_set):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"forge_plugin_{folder.name}", code_file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            handlers = getattr(module, "HANDLERS", {})
            if not isinstance(handlers, dict):
                handlers = {}
        except Exception as exc:
            if ctx.log:
                ctx.log(f"Plugin {folder.name} failed to load: {exc}")
            continue
        plugin.handlers = handlers
        config_keys = meta.get("config_keys", [])
        if not isinstance(config_keys, list):
            config_keys = []
        view = _PluginContextView(ctx, config_keys)
        for tool in plugin.tools:
            name = tool.get("name", "")
            handler = handlers.get(name)
            if not name or not callable(handler):
                continue
            if name in registry:
                if ctx.log:
                    ctx.log(f"Plugin tool '{name}' from {plugin.name} ignored — "
                            f"already registered by {registry[name][3]}.")
                continue
            schema = {"type": "function", "function": {
                "name": name,
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {"type": "object", "properties": {}})}}
            registry[name] = (schema,
                              (lambda h, v: lambda args: h(args, v))(handler, view),
                              bool(tool.get("requires_approval", True)),
                              plugin.name)
    return plugins, registry


def load_connector_config(app_dir: Path) -> dict:
    try:
        return json.loads((Path(app_dir) / "connectors.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
