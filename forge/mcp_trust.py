"""MCP / Browse spawn trust — S5 (audit S1-08 + S1-20).

An MCP server entry in connectors.json is a PROGRAM: the first
tool call that needs it spawns it as the owner. Until S5 the spawn
was invisible to the approval gate — mcp_list_tools is a read-class
tool (no approval), yet listing tools starts the process, and a
poisoned connectors.json edit was all an attacker needed.

The rule now: no MCP or Browse server process spawns unless the
owner has trusted its exact configuration. Trust is keyed by a
fingerprint of the WHOLE server spec (sha256 over its canonical
JSON — the identical formula plugins/mcp/mcp_client.py uses for
restart-on-change, so the two can never disagree about what "the
same server" means) and lives in the owner's settings
("trusted_mcp_servers": {name: {"fingerprint": ..., "resolved":
...}}) — settings live in the app data dir, so neither a project
file nor connectors.json itself can write trust; editing the spec
changes the fingerprint and the owner is asked again.

This module is the pure-logic half (no Qt, no subprocess): it maps
a tool call to the server it would spawn, computes fingerprints
and resolved command paths, renders the spawn-gate description,
and reads/writes the trust records in a plain dict the window
hands the worker (the settings dict itself, plus a save callback).
The worker's approval branch is the gate seam — see
AgentWorker._execute_call_batch.

S5-fix (re-audit W1/W2/W15, 2026-11-12): the gate also covers
verify_in_browser — the verify loop drives the Browse server
through the plugin's _call, so it gates like a browse verb — and
the cloudstore command, a connectors.json-named program like any
MCP server. And the gate takes the engine into account: under the
Forge Browser package engine the browse verbs and verify never
touch the MCP server, so no spawn prompt fires for a spawn that
will not happen.
"""
from __future__ import annotations
import hashlib
import json
import os
import shlex
import shutil

from . import browse as browse_mod
from . import cloudstore as cloudstore_mod

MCP_TOOLS = ("mcp_list_tools", "mcp_call")

# The trust-store key for the cloudstore command (W2). It rides in
# the same trusted_mcp_servers dict as the MCP servers — one store,
# one save path — under its own name, and its spec carries a kind
# marker, so its fingerprint can never collide with an MCP
# server's record even if a server were named "cloudstore".
CLOUDSTORE_SERVER = "cloudstore"

TRUST_KEY = "trusted_mcp_servers"


def spec_fingerprint(spec: dict) -> str:
    """sha256 over the spec's canonical JSON. Identical formula to
    plugins/mcp/mcp_client._spec_fingerprint — keep them in step."""
    canonical = json.dumps(spec or {}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_command(command: str) -> str | None:
    """The absolute path a spec's command resolves to on PATH, or
    None when it cannot be resolved. Mirrors mcp_client's spawn
    resolution, so what the gate shows is what runs."""
    command = str(command or "")
    if not command:
        return None
    if os.path.isabs(command):
        return command if os.path.exists(command) else None
    return shutil.which(command)


def spawn_target(name: str, args: dict, config: dict | None,
                 package_engine: bool = False):
    """(server_name, spec) when executing this tool call would spawn
    an MCP/Browse server process — else None (built-ins, other
    plugins, unconfigured servers, and calls whose handler answers
    without ever reaching get_server).

    package_engine (S5-fix, audit W15) is True when the owner's
    Research web engine is the Forge Browser package: its verbs
    and verify are served without touching the MCP server, so no
    spawn prompt fires for them — a prompt for a spawn that will
    not happen trains the owner to approve without reading.
    browse_hand_back is the exception: its handler never routes
    by engine, so a mapped hand-back tool still spawns under
    either engine. An unset engine reads as the connector, which
    is today's behavior exactly."""
    config = config or {}
    specs = config.get("mcp_servers")
    if not isinstance(specs, dict):
        specs = {}
    if name in MCP_TOOLS:
        server = str((args or {}).get("server", "") or "").strip()
        spec = specs.get(server)
        # An unknown server name is answered by the handler with an
        # error — get_server is never reached, nothing spawns.
        return (server, spec) if isinstance(spec, dict) else None
    if name == "verify_in_browser" or name.startswith("browse_"):
        if package_engine and name != "browse_hand_back":
            # The package engine serves this call without touching
            # the MCP server (open/read/screenshot via the built-in
            # fetch, click/type refused without a spawn, verify on
            # the package's own engine) — no spawn, no gate.
            return None
        resolved = browse_mod.resolve_config(config)
        spec = browse_mod.server_spec(config, resolved)
        if spec is None:
            return None  # not configured: setup message, no spawn
        if name == "verify_in_browser":
            # S5-fix (audit W1): the verify loop drives
            # open/read/screenshot through the plugin's _call, and
            # the first verb mapped to a server tool spawns it —
            # the same spawn as a browse verb, gated the same way.
            # All three unmapped means the loop answers without
            # ever reaching get_server.
            for verb_key in ("open", "read", "screenshot"):
                tool, _pinned = browse_mod.server_args(
                    resolved, verb_key, {})
                if tool:
                    return (resolved["server"], spec)
            return None
        verb_key = name[len("browse_"):]
        tool, _pinned = browse_mod.server_args(resolved, verb_key, {})
        if not tool:
            return None  # verb mapped to no server tool (hand_back's
            # default) — the handler never touches the server
        return (resolved["server"], spec)
    if name.startswith("cloud_"):
        # S5-fix (audit W2): the cloudstore command is a program
        # like any MCP server — connectors.json names it, the
        # verbs run it. The gate's spec is the command itself
        # (plus the kind marker, so the fingerprint can never
        # collide with an MCP server's). An unresolvable command
        # means the handler answers with the setup words and
        # nothing spawns — no gate.
        resolved = cloudstore_mod.resolve_config(config)
        command = resolved["command"]
        if cloudstore_mod.command_path(command) is None:
            return None
        return (CLOUDSTORE_SERVER,
                {"command": command, "kind": "cloudstore"})
    return None


def trusted_fingerprint(trust: dict | None, server: str) -> str | None:
    """The trusted fingerprint recorded for a server, accepting both
    the current dict record and a bare-string record."""
    if not isinstance(trust, dict):
        return None
    record = trust.get(server)
    if isinstance(record, dict):
        value = record.get("fingerprint")
        return str(value) if value else None
    if isinstance(record, str) and record:
        return record
    return None


def is_trusted(trust: dict | None, server: str, spec: dict) -> bool:
    return trusted_fingerprint(trust, server) == spec_fingerprint(spec)


def record_trust(trust: dict, server: str, spec: dict) -> dict:
    """Write the trust record for a server into the settings dict
    the window handed over; returns the record written. The
    resolved command path rides alongside, so what was approved
    stays auditable after the fact."""
    record = {"fingerprint": spec_fingerprint(spec),
              "resolved": resolve_command(spec.get("command", ""))
              or str(spec.get("command", ""))}
    trust[server] = record
    return record


def command_line(spec: dict) -> str:
    """The full command line for the gate text: resolved absolute
    path first, then the spec's arguments, shell-quoted."""
    resolved = resolve_command(spec.get("command", "")) \
        or str(spec.get("command", ""))
    parts = [resolved] + [str(a) for a in spec.get("args", []) or []]
    return shlex.join(parts)


def spawn_description(name: str, server: str, spec: dict,
                      call_description: str) -> str:
    """The approval text for an untrusted spawn: what program, at
    what path, and the standing warning — followed by the call's
    own description, so one prompt covers spawn + call. The
    cloudstore command gets its own wording: it is a cloud
    storage program, not an MCP server, and the owner is told
    what it reaches."""
    if str(name or "").startswith("cloud_"):
        return (
            f"This call will RUN the cloud storage program "
            f"'{spec.get('command', '')}' — a program on this PC — "
            "with this command:\n\n"
            f"  {command_line(spec)}\n\n"
            "Running a cloud storage program means trusting it to "
            "run on this PC and reach your cloud accounts. "
            "Approving runs it now and trusts this exact command: "
            "if the command changes, you will be asked again.\n\n"
            f"{call_description}")
    return (
        f"This call will START the MCP server '{server}' — a "
        "program on this PC — with this command:\n\n"
        f"  {command_line(spec)}\n\n"
        "Adding an MCP server means trusting a program to run on "
        "this PC. Approving starts it now and trusts this exact "
        "server configuration: if its command or arguments change, "
        "you will be asked again.\n\n"
        f"{call_description}")
