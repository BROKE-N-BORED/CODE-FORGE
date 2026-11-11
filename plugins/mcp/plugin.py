"""MCP connector — Note 17.

Bridges locally-run MCP servers into the Forge as tools. Servers are
declared in the app data dir's connectors.json:

  {"mcp_servers": {"files": {"command": "python3", "args": ["/path/server.py"]}}}

mcp_list_tools is read-only and runs without approval; mcp_call is
approval-gated like any write-class tool, and permission rules/modes
apply to it the same as built-ins.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mcp_client import McpError, get_server  # noqa: E402


def _specs(ctx) -> dict:
    return (getattr(ctx, "config", None) or {}).get("mcp_servers", {})


def _server_or_error(args, ctx):
    name = str(args.get("server", "")).strip()
    specs = _specs(ctx)
    if name not in specs:
        known = ", ".join(sorted(specs)) or "none configured"
        return None, f"Unknown MCP server '{name}'. Configured servers: {known}."
    try:
        return get_server(name, specs[name]), None
    except Exception as exc:
        return None, f"MCP server '{name}' failed to start: {exc}"


def mcp_list_tools(args, ctx):
    server, error = _server_or_error(args, ctx)
    if error:
        return error
    try:
        tools = server.list_tools()
    except McpError as exc:
        return str(exc)
    if not tools:
        return f"MCP server '{args.get('server')}' exposes no tools."
    lines = [f"Tools on MCP server '{args.get('server')}':"]
    for t in tools:
        lines.append(f"- {t.get('name')}: {t.get('description', '')[:120]}")
    return "\n".join(lines)


def mcp_call(args, ctx):
    server, error = _server_or_error(args, ctx)
    if error:
        return error
    raw = args.get("arguments_json") or "{}"
    try:
        arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception as exc:
        return f"arguments_json is not valid JSON: {exc}"
    try:
        return server.call_tool(str(args.get("tool", "")), arguments)
    except McpError as exc:
        return str(exc)


HANDLERS = {"mcp_list_tools": mcp_list_tools, "mcp_call": mcp_call}
