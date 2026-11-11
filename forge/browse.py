"""Browse connector machinery — Item #55 (ADOPT, don't build).

OpenCode Browse is an external MCP server + Chromium extension
(anomalyco/browse, MIT, free): the extension lives in the Chromium
the owner already runs, and the server speaks MCP over stdio like
every other local server in connectors.json. Forge does NOT drive
a browser and vendors none of that — it REGISTERS Browse as a
connector and puts its own fences in front of the server's tools.
This module is the pure-logic half (no Qt, no subprocess);
plugins/browse/plugin.py is the thin handler shell over it.

Three fences live here:

VERBS — models never see the server's raw tool surface, only six
Forge verbs (open / read / click / type / screenshot / hand-back),
mapped onto server tools by a per-connector map in connectors.json
(the "browse" key), so a server-side rename is a config edit, not
a code change. read pins format=text into the server call: page
observation is a TEXT snapshot by default — the 8K-context budget
rule, not a preference — and the model's browse_read schema has no
format knob at all. Pixels exist only behind the explicit
screenshot verb.

ALLOWLIST — navigation (browse_open) is checked against the
PROJECT's site allowlist (.forge/browse.json, the same .forge/*.json
pattern as hooks.json / verify.json) BEFORE the call reaches the
server. No allowlist configured = nothing is allowed: Forge's
posture is owner-gated everywhere else, so this fence starts shut
and its refusal names the file that opens it. Matching is exact
host, or "*.example.com" for subdomains of a host; only http/https
URLs are navigable at all, http only to loopback, and an
allowlisted host must not be — or resolve to — a private/local
address unless that exact address is itself listed (S5, audit
S1-18/S1-19; the narrow exceptions live in check_open). (Browse ALSO keeps per-session domain
allowlists server-side — mirror the same hosts there; it enforces
them at the document/IP level, this fence enforces at the call.)

HOLD — when the server reports a wall (login / 2FA / payment /
captcha — Browse detects these server-side and hands the tab back;
the signal formats are below), the session is HELD as pipeline
state in this module: every Browse verb refuses until the owner
continues. The release is the existing approval gate, not a model
decision: browse_hand_back is an approval-gated tool, so its
handler only runs after the owner approves it at the desktop
dialog or the phone deck (one gate, two doors — the same gate as
every tool approval), and the handler is what clears the hold. A
denied hand-back changes nothing; the hold stands.
"""
from __future__ import annotations
import ipaddress
import json
import re
import socket
import urllib.parse
from pathlib import Path

from . import projects as proj

# The model-facing surface: plugin.json declares these exact names.
VERB_TOOLS = ("browse_open", "browse_read", "browse_click",
              "browse_type", "browse_screenshot", "browse_hand_back")
# The mapping keys, one per verb (the tool name minus "browse_").
VERB_KEYS = ("open", "read", "click", "type", "screenshot", "hand_back")

DEFAULT_SERVER_NAME = "browse"

# Verb -> server tool. The defaults name the granular tool shape;
# Browse's own surface centers on Playwright-driven tools, and an
# install whose names differ is a connectors.json edit away
# ("browse": {"tool_map": {...}}) — no code change. A verb mapped
# to tool None never calls the server: hand_back is Forge-side by
# default (the gate IS the handoff), and an owner whose server
# exposes its own handoff tool can map one in. "pin" entries are
# merged OVER the verb's arguments and always win — that is how
# read's text default is enforced rather than requested.
DEFAULT_TOOL_MAP = {
    "open": {"tool": "navigate", "pin": {}},
    "read": {"tool": "snapshot", "pin": {"format": "text"}},
    "click": {"tool": "click", "pin": {}},
    "type": {"tool": "type", "pin": {}},
    "screenshot": {"tool": "screenshot", "pin": {}},
    "hand_back": {"tool": None, "pin": {}},
}

# The server's wall signal, as detected in a tool result's text:
#   * a JSON object with a truthy "handoff" field — the value names
#     the kind ("payment", "login", "2fa", "captcha");
#   * the marker "[browse-handoff]" or "[browse-handoff: <kind>]"
#     anywhere in the text;
#   * any extra substring the owner lists in the connector config
#     ("browse": {"wall_markers": [...]}) for their server build.
DEFAULT_WALL_MARKERS = ("[browse-handoff]",)
_HANDOFF_MARKER_RE = re.compile(
    r"\[browse-handoff(?::\s*([a-z0-9_-]+))?\]", re.IGNORECASE)

ALLOWLIST_NAME = "browse.json"  # .forge/browse.json in the project


# ---------- connector registration ----------

def preset_config() -> dict:
    """The connectors.json block that registers Browse. The single
    source for the README's setup text and the not-configured
    refusal the handlers return, so the three can never drift.

    The command is the `browse` binary the Browse installer places
    on PATH (its MCP server speaks stdio, like every mcp_servers
    entry). If an install reports a different server command, the
    owner edits command/args here — Forge never installs Browse.
    """
    return {"mcp_servers": {DEFAULT_SERVER_NAME: {"command": "browse", "args": []}},
            "browse": {"server": DEFAULT_SERVER_NAME}}


def setup_message(server_name: str) -> str:
    preset = json.dumps(preset_config(), indent=2)
    return (f"Browse is not configured yet: connectors.json has no "
            f"mcp_servers entry named '{server_name}'. Browse is an "
            f"external MCP server + Chromium extension (OpenCode "
            f"Browse) — install it with its own installer first, "
            f"then add this block to the app data dir's "
            f"connectors.json and enable the Browse connector in "
            f"Customize → Connectors:\n{preset}")


def resolve_config(config: dict | None) -> dict:
    """Merge the connectors.json "browse" key over the defaults."""
    raw = (config or {}).get("browse")
    if not isinstance(raw, dict):
        raw = {}
    server = str(raw.get("server") or DEFAULT_SERVER_NAME).strip() \
        or DEFAULT_SERVER_NAME
    tool_map = {k: dict(v) for k, v in DEFAULT_TOOL_MAP.items()}
    raw_map = raw.get("tool_map")
    if isinstance(raw_map, dict):
        for key, entry in raw_map.items():
            if key not in tool_map:
                continue
            if isinstance(entry, str):
                tool_map[key] = {"tool": entry, "pin": {}}
            elif isinstance(entry, dict):
                merged = dict(tool_map[key])
                if "tool" in entry:
                    merged["tool"] = entry["tool"]
                pin = entry.get("pin")
                if isinstance(pin, dict):
                    merged["pin"] = {**merged.get("pin", {}), **pin}
                tool_map[key] = merged
    markers = list(DEFAULT_WALL_MARKERS)
    raw_markers = raw.get("wall_markers")
    if isinstance(raw_markers, list):
        markers += [str(m) for m in raw_markers if str(m)]
    return {"server": server, "tool_map": tool_map,
            "wall_markers": markers}


def server_spec(config: dict | None, resolved: dict) -> dict | None:
    specs = (config or {}).get("mcp_servers")
    if not isinstance(specs, dict):
        return None
    spec = specs.get(resolved["server"])
    return spec if isinstance(spec, dict) else None


def server_args(resolved: dict, verb_key: str, verb_args: dict) -> tuple:
    """(server tool name, arguments) for one verb call — the map's
    pinned arguments merged over the verb's own, so a pin (read's
    format=text) cannot be overridden by the caller."""
    entry = resolved["tool_map"].get(verb_key) or {}
    args = dict(verb_args or {})
    args.update(entry.get("pin") or {})
    return entry.get("tool"), args


# ---------- the site allowlist fence ----------

def load_allowlist(workspace) -> list[str]:
    """The project's Browse allowlist: .forge/browse.json holding
    {"allowlist": ["example.com", "*.docs.example.org"]}. Missing,
    unreadable, or malformed means NO sites — the fence starts
    shut, it never fails open."""
    try:
        data = json.loads(
            (proj.forge_area(workspace) / ALLOWLIST_NAME)
            .read_text(encoding="utf-8"))
    except Exception:
        return []
    entries = data.get("allowlist") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    return [str(e).strip().lower() for e in entries if str(e).strip()]


def url_host(url: str) -> str | None:
    """The URL's host, lowercased — or None when the URL is not a
    navigable http/https address at all."""
    try:
        parsed = urllib.parse.urlparse(str(url or "").strip())
    except Exception:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    return host or None


def host_matches(host: str, pattern: str) -> bool:
    pattern = pattern.strip().lower()
    if pattern.startswith("*."):
        return host.endswith("." + pattern[2:])
    return host == pattern


# ---------- S5 (audit S1-18/S1-19): scheme + address fences ----------
# Two more checks ride on top of the allowlist match in check_open:
#
# HTTPS-ONLY — cleartext http is refused for anything but the
# owner's own machine (loopback): a signed-in browser page fetched
# over http on shared WiFi can be modified in transit. Loopback
# http is not a network exposure, so it stays allowed.
#
# PRIVATE ADDRESSES — an allowlisted host must not secretly BE (or
# resolve TO) a private / loopback / link-local / reserved /
# multicast address: that is the SSRF shape (the project's own
# Ollama, the phone deck, a router admin page). The owner's escape
# hatch is deliberate and narrow: an EXACT allowlist entry naming
# that very IP keeps access (an owner who listed 127.0.0.1 or a LAN
# IP meant it), and a loopback NAME (localhost / *.localhost) is
# the owner's machine by definition. A wildcard entry never earns
# the exception, and a public name that resolves to an internal
# address is refused even when the name itself is allowlisted
# (DNS rebinding). Resolution happens ONCE per browse_open; a name
# that cannot be resolved is refused (fail-closed). Redirects and
# later verbs cannot be re-fenced here — the server owns those;
# that residual is disclosed, not hidden.

def _ip_literal(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_internal_ip(ip) -> bool:
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified)


def _is_loopback_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    ip = _ip_literal(host)
    return bool(ip is not None and ip.is_loopback)


def _resolve_ips(host: str) -> list | None:
    """The addresses a hostname resolves to (ipaddress objects),
    or None when resolution fails — the caller fails closed."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return None
    ips = []
    for info in infos or []:
        try:
            ips.append(ipaddress.ip_address(info[4][0]))
        except Exception:
            continue
    return ips or None


def check_open(url: str, allowlist: list[str]) -> str | None:
    """None when the navigation is allowed; otherwise the plain
    refusal the model reads. Runs before the server is touched."""
    host = url_host(url)
    if host is None:
        return (f"Browse refused: '{url}' is not an http/https URL "
                "the agent may navigate to.")
    # S5 (S1-19): https-only beyond the owner's own machine.
    try:
        scheme = urllib.parse.urlparse(
            str(url or "").strip()).scheme.lower()
    except Exception:
        scheme = ""
    if scheme == "http" and not _is_loopback_host(host):
        return (f"Browse refused: browsing to non-local sites "
                f"requires https — '{host}' was given over http. "
                "Open the https address instead; http stays "
                "available for this PC itself (localhost).")
    matched = [p for p in allowlist if host_matches(host, p)]
    if not matched:
        where = f".forge/{ALLOWLIST_NAME}"
        if not allowlist:
            return (f"Browse refused: this project has no Browse site "
                    f"allowlist, so no site is allowed yet. The owner "
                    f"adds hosts to {where} in the project folder, e.g. "
                    f'{{"allowlist": ["{host}"]}} — then the navigation '
                    "can be retried.")
        return (f"Browse refused: {host} is not on this project's "
                f"Browse site allowlist ({where}). The owner can add "
                "it there; until then this navigation does not happen.")
    # S5 (S1-18): the allowlisted destination must not be — or
    # resolve to — an internal address, save the narrow exceptions
    # documented above.
    ip = _ip_literal(host)
    if ip is not None:
        if _is_internal_ip(ip) and not any(
                p.strip().lower() == host for p in matched):
            return (f"Browse refused: {host} is a private/local "
                    "address, and this project's Browse allowlist "
                    "does not list that exact address. The owner can "
                    f"add \"{host}\" itself to .forge/{ALLOWLIST_NAME} "
                    "if browsing to it is intended.")
        return None
    if _is_loopback_host(host):
        return None
    ips = _resolve_ips(host)
    if ips is None:
        return (f"Browse refused: {host} could not be resolved, "
                "so the navigation cannot be checked and does not "
                "happen. Check the address and try again.")
    for resolved_ip in ips:
        if _is_internal_ip(resolved_ip):
            return (f"Browse refused: {host} resolves to "
                    f"{resolved_ip}, a private/local address — "
                    "allowlisted sites must not point inside this "
                    "PC or its network. If the owner intends to "
                    "browse that address, they can list the address "
                    f"itself in .forge/{ALLOWLIST_NAME}.")
    return None


# ---------- wall detection + the hold ----------

def detect_wall(text, markers=None) -> str | None:
    """The wall kind a tool result reports ("payment", "login", …),
    or None. Detection is the server's job (Browse flags the wall);
    this only READS its signal, in the formats documented above."""
    body = str(text or "")
    stripped = body.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("handoff"):
            value = data["handoff"]
            return str(value).strip().lower() if isinstance(value, str) \
                else "handoff"
    match = _HANDOFF_MARKER_RE.search(body)
    if match:
        return (match.group(1) or "handoff").lower()
    for marker in markers or DEFAULT_WALL_MARKERS:
        if marker and marker in body:
            return "handoff"
    return None


_holds: dict[str, dict] = {}


def set_hold(workspace, kind: str, detail: str = "") -> dict:
    hold = {"kind": str(kind or "handoff"), "detail": str(detail or "")}
    _holds[str(workspace)] = hold
    return hold


def get_hold(workspace) -> dict | None:
    return _holds.get(str(workspace))


def clear_hold(workspace) -> dict | None:
    return _holds.pop(str(workspace), None)


def hold_refusal(tool: str, hold: dict) -> str:
    kind = hold.get("kind", "handoff")
    return (f"Browse is HELD: the browser hit a {kind} wall and the "
            "tab is with the owner. Every Browse verb is refused "
            "until the owner continues — call browse_hand_back so "
            "the owner can take over at the approval gate; only "
            "their approval releases this hold.")


def handoff_notice(kind: str) -> str:
    return (f"[FORCED HANDOFF — {kind} wall] The browser reports a "
            f"{kind} step that is the owner's, never yours. Browse "
            "is now HELD: all Browse verbs refuse until the owner "
            "continues. Call browse_hand_back now — it waits at "
            "the owner's approval gate (desktop or phone), and "
            "their approval is the Continue that releases the hold.")
