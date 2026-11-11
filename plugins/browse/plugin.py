"""Browse connector — Item #55. ADOPT, don't build.

OpenCode Browse (anomalyco/browse, MIT, free) is an external MCP
server + Chromium extension: the extension lives in the Chromium
the owner already runs — no second browser process — and the
server speaks MCP over stdio. This plugin REGISTERS it: the six
model-facing verbs below ride the same MCP client machinery as
the generic MCP connector (plugins/mcp), and the fences live in
forge/browse.py:

  * models see ONLY these verbs, mapped onto the server's tools
    by the connectors.json "browse" tool_map (defaults in
    forge/browse.py) — read pins a TEXT snapshot, screenshots are
    a separate on-demand verb;
  * browse_open is fenced by the project's site allowlist
    (.forge/browse.json) before the server is ever called;
  * a server-reported wall (login / 2FA / payment / captcha)
    HOLDS every verb until the owner continues: browse_hand_back
    is approval-gated, so the owner's approval at the existing
    gate — desktop dialog or phone deck — is the Continue, and
    this file's hand-back handler is what releases the hold.

Setup is the owner's, on their PC (Forge never installs Browse):
install Browse with its own installer, add the preset block from
forge.browse.preset_config() to connectors.json, enable this
connector in Customize → Connectors, and list the project's
sites in .forge/browse.json.

Item #65 adds a seventh tool, verify_in_browser — the verify
loop the #55 spec named as Browse's first job. It is a Forge-side
composite, not a server verb: it drives open / read / screenshot
through this file's own fenced _call and composes their results
in forge/verify.py, so every fence above applies to it unchanged.

Browser merge (2026-11-11): a second engine now sits behind the
same seam. When the owner's Settings → Behavior → "Research web
engine" names Forge Browser, open / read / screenshot are served
by the built-in keyless forge/browser package
(forge/webengine.py) and verify composes on the package's own
engine — behind the SAME fences: the hold refuses first, the
allowlist decides before either engine is touched, and acting
verbs (click / type) keep riding this connector under either
choice. The default is unchanged: everything above.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp"))
from mcp_client import McpError, get_server  # noqa: E402

from forge import browse as browse_mod  # noqa: E402
from forge import verify as verify_mod  # noqa: E402
from forge import webengine as webengine_mod  # noqa: E402


def _resolved(ctx) -> dict:
    return browse_mod.resolve_config(getattr(ctx, "config", None) or {})


def _server_or_error(resolved, ctx):
    spec = browse_mod.server_spec(getattr(ctx, "config", None) or {}, resolved)
    if spec is None:
        return None, browse_mod.setup_message(resolved["server"])
    try:
        return get_server(resolved["server"], spec), None
    except Exception as exc:
        return None, f"Browse MCP server '{resolved['server']}' failed to start: {exc}"


def _call(verb_key, verb_args, ctx):
    """One fenced verb call: hold check, map, server, wall scan."""
    hold = browse_mod.get_hold(ctx.workspace)
    if hold is not None:
        return browse_mod.hold_refusal("browse_" + verb_key, hold)
    resolved = _resolved(ctx)
    tool, args = browse_mod.server_args(resolved, verb_key, verb_args)
    if not tool:
        return (f"Browse setup: no server tool is mapped for the "
                f"'{verb_key}' verb. Set one in connectors.json under "
                '"browse" → "tool_map".')
    server, error = _server_or_error(resolved, ctx)
    if error:
        return error
    try:
        result = server.call_tool(str(tool), args)
    except McpError as exc:
        return str(exc)
    kind = browse_mod.detect_wall(result, resolved["wall_markers"])
    if kind:
        browse_mod.set_hold(ctx.workspace, kind, str(result)[:200])
        if ctx.log:
            ctx.log(f"Browse: {kind} wall reported by the server — "
                    "session HELD until the owner continues.")
        return str(result) + "\n\n" + browse_mod.handoff_notice(kind)
    return result


def browse_open(args, ctx):
    hold = browse_mod.get_hold(ctx.workspace)
    if hold is not None:
        return browse_mod.hold_refusal("browse_open", hold)
    url = str(args.get("url", "")).strip()
    refusal = browse_mod.check_open(
        url, browse_mod.load_allowlist(ctx.workspace))
    if refusal:
        return refusal
    # The fence above is engine-independent: whichever engine the
    # owner picked, the project's allowlist has already decided.
    if webengine_mod.engine_for(ctx) == webengine_mod.ENGINE_PACKAGE:
        return webengine_mod.open_page(ctx.workspace, url, ctx)
    return _call("open", {"url": url}, ctx)


def browse_read(args, ctx):
    if webengine_mod.engine_for(ctx) == webengine_mod.ENGINE_PACKAGE:
        return webengine_mod.read_page(ctx.workspace)
    return _call("read", {}, ctx)


def browse_click(args, ctx):
    if webengine_mod.engine_for(ctx) == webengine_mod.ENGINE_PACKAGE:
        return webengine_mod.acting_refusal("click")
    return _call("click", {"selector": str(args.get("selector", ""))}, ctx)


def browse_type(args, ctx):
    if webengine_mod.engine_for(ctx) == webengine_mod.ENGINE_PACKAGE:
        return webengine_mod.acting_refusal("type")
    return _call("type", {"selector": str(args.get("selector", "")),
                          "text": str(args.get("text", ""))}, ctx)


def browse_screenshot(args, ctx):
    if webengine_mod.engine_for(ctx) == webengine_mod.ENGINE_PACKAGE:
        return webengine_mod.screenshot_page(ctx.workspace, ctx)
    return _call("screenshot", {}, ctx)


def browse_hand_back(args, ctx):
    """The release. This handler runs ONLY after the owner approved
    the call at the shell's approval gate (the tool is declared
    requires_approval) — that approval is the owner's Continue, on
    the desktop or the phone. A denied call never reaches here, so
    a hold the owner has not released simply stands."""
    hold = browse_mod.clear_hold(ctx.workspace)
    resolved = _resolved(ctx)
    tool, sargs = browse_mod.server_args(
        resolved, "hand_back", {"reason": str(args.get("reason", ""))})
    note = ""
    if tool:
        server, error = _server_or_error(resolved, ctx)
        if error:
            note = f" (Note: {error})"
        else:
            try:
                note = " Server: " + str(server.call_tool(str(tool), sargs))
            except McpError as exc:
                note = f" (Note: the server's hand-back call failed: {exc})"
    if hold is not None:
        return (f"Hand-back complete: the owner took the tab for the "
                f"{hold['kind']} step and has now continued — the "
                f"Browse hold is released and the verbs are live "
                f"again.{note}")
    return ("Hand-back complete: the owner approved at the gate and "
            f"has the tab. Continue when they are done.{note}")


def verify_in_browser(args, ctx):
    """Item #65: the verify loop. Same doors as every verb — the
    hold refuses it, and an unconfigured connector answers with
    the setup words — then forge/verify.py drives the three
    observation verbs through _call and composes the result."""
    hold = browse_mod.get_hold(ctx.workspace)
    if hold is not None:
        return browse_mod.hold_refusal("verify_in_browser", hold)
    if webengine_mod.engine_for(ctx) == webengine_mod.ENGINE_PACKAGE:
        # Forge Browser's own verify: same classification and
        # fences inside the package, no MCP server needed at all.
        target = str(args.get("target", "")).strip()
        if not target:
            return ("verify_in_browser needs a target: a project-"
                    "relative path to a page in this project (e.g. "
                    "'index.html'), or an http(s) URL on the "
                    "project's Browse allowlist.")
        return webengine_mod.verify_page(
            ctx.workspace, target, webengine_mod.cdp_engine_for(ctx))
    resolved = _resolved(ctx)
    if browse_mod.server_spec(
            getattr(ctx, "config", None) or {}, resolved) is None:
        return browse_mod.setup_message(resolved["server"])
    target = str(args.get("target", "")).strip()
    if not target:
        return ("verify_in_browser needs a target: a project-"
                "relative path to a page in this project (e.g. "
                "'index.html'), or an http(s) URL on the "
                "project's Browse allowlist.")
    return verify_mod.run_verify(
        target, ctx.workspace, lambda key, a: _call(key, a, ctx))


HANDLERS = {"browse_open": browse_open, "browse_read": browse_read,
            "browse_click": browse_click, "browse_type": browse_type,
            "browse_screenshot": browse_screenshot,
            "browse_hand_back": browse_hand_back,
            "verify_in_browser": verify_in_browser}
