"""Minimal MCP (Model Context Protocol) client over stdio.

Speaks line-delimited JSON-RPC to a locally-run MCP server process:
initialize handshake, tools/list, tools/call. One persistent process
per server, spawned lazily. Deliberately small — the Forge's own
approval gate and permission rules sit in front of every tools/call;
this module only moves messages.
"""
from __future__ import annotations
import hashlib
import json
import os
import queue
import shutil
import subprocess
import threading

PROTOCOL_VERSION = "2024-11-05"

# Startup must not hang on a dead server: the initialize handshake gets
# a short leash, while ordinary requests keep the long default in
# _request (a tools/call may legitimately run for a while).
CONNECT_TIMEOUT = 10
# tools/list pagination ceiling — a server that never stops handing
# back a nextCursor is broken, not big.
MAX_LIST_PAGES = 50


class McpError(RuntimeError):
    pass


def resolve_command(command: str) -> str | None:
    """S5 (audit S1-08): the absolute path a spec's command resolves
    to on PATH — the very path the spawn-gate approval shows the
    owner — or None when it cannot be resolved. An already-absolute
    command resolves to itself when it exists."""
    command = str(command or "")
    if not command:
        return None
    if os.path.isabs(command):
        return command if os.path.exists(command) else None
    return shutil.which(command)


class McpServer:
    def __init__(self, name: str, command: str, args: list | None = None):
        self.name = name
        self._next_id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        # S5: spawn the RESOLVED path (what the owner approved at
        # the spawn gate is what runs), and fail with a clear,
        # command-naming message when it cannot be resolved at all.
        resolved = resolve_command(command)
        if resolved is None:
            raise McpError(
                f"MCP server '{name}': command '{command}' was not "
                "found — check the command in connectors.json.")
        self.command = resolved
        self.proc = subprocess.Popen(
            [resolved, *(args or [])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        try:
            self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "code-forge", "version": "2.2"}},
                timeout=CONNECT_TIMEOUT)
            self._notify("notifications/initialized", {})
        except Exception:
            # A half-started server process must not outlive its failed
            # handshake — otherwise every retry spawns another orphan.
            try:
                self.proc.terminate()
            except Exception:
                pass
            raise

    def _read_loop(self):
        try:
            for line in self.proc.stdout:  # type: ignore[union-attr]
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                mid = msg.get("id")
                if mid in self._pending:
                    self._pending[mid].put(msg)
        except Exception:
            pass

    def _send(self, payload: dict):
        self.proc.stdin.write(json.dumps(payload) + "\n")  # type: ignore[union-attr]
        self.proc.stdin.flush()  # type: ignore[union-attr]

    def _notify(self, method: str, params: dict):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict, timeout: float = 60) -> dict:
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            box: queue.Queue = queue.Queue()
            self._pending[mid] = box
        try:
            self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
            msg = box.get(timeout=timeout)
        except queue.Empty:
            raise McpError(f"MCP server '{self.name}' did not answer {method} within {timeout}s")
        finally:
            self._pending.pop(mid, None)
        if "error" in msg:
            raise McpError(f"MCP {method} error: {msg['error']}")
        return msg.get("result", {})

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        params: dict = {}
        for _ in range(MAX_LIST_PAGES):
            result = self._request("tools/list", params)
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
            params = {"cursor": cursor}
        raise McpError(
            f"MCP server '{self.name}' did not finish listing tools "
            f"within {MAX_LIST_PAGES} pages")

    def call_tool(self, tool: str, arguments: dict) -> str:
        result = self._request("tools/call", {"name": tool, "arguments": arguments})
        parts = []
        for item in result.get("content", []):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(json.dumps(item)[:500])
        text = "\n".join(parts) or json.dumps(result)[:2000]
        if result.get("isError"):
            return f"MCP tool error: {text}"
        return text


_servers: dict[str, McpServer] = {}
_server_fingerprints: dict[str, str] = {}


def _spec_fingerprint(spec: dict) -> str:
    """S5 (audit S1-20): fingerprint of the WHOLE server spec —
    sha256 over its canonical JSON (sorted keys). It drives both
    restart-on-change here and the owner's spawn trust in
    forge/mcp_trust.py, which computes the identical value; the two
    must never drift (same formula, both places)."""
    canonical = json.dumps(spec or {}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stop(server: McpServer) -> None:
    try:
        server.proc.terminate()
    except Exception:
        pass


def get_server(name: str, spec: dict) -> McpServer:
    fingerprint = _spec_fingerprint(spec)
    server = _servers.get(name)
    if server is not None:
        if server.proc.poll() is None and \
                _server_fingerprints.get(name) == fingerprint:
            return server
        # Dead, or the config was edited under the running server:
        # drop it and spawn fresh under the current spec instead of
        # ignoring the edit (or the corpse) until the app restarts.
        _stop(server)
        _servers.pop(name, None)
        _server_fingerprints.pop(name, None)
    server = McpServer(name, str(spec.get("command", "")), list(spec.get("args", [])))
    _servers[name] = server
    _server_fingerprints[name] = fingerprint
    return server
