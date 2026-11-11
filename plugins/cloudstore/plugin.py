"""Cloud Storage connector — Item #70. ADOPT, don't build.

Forge holds NO cloud credentials and builds NO storage backend.
The backend is rclone — one free, single-binary tool the owner
installs and configures: the owner runs `rclone config` once in a
terminal, rclone runs the OAuth sign-in and keeps the tokens in
ITS OWN config file, and Forge only ever learns remote NAMES and
moves files with subprocess argument lists. All the logic — the
backend calls, the setup words, and the fence (the project's
.forge/cloudstore.json names the allowed remote prefixes;
default refuse-all, checked before the backend is touched) —
lives in forge/cloudstore.py; this file is the thin handler shell
over it, mirroring the browse.py / plugins/browse split.

The verbs are explicit transfers only: list, read (size-capped,
text only), download, upload, delete. No sync, no mirror, no
background anything. Download / upload / delete are declared
requires_approval — they pass the owner's ordinary approval gate
(desktop dialog or phone deck) before these handlers ever run;
list / read are read-class.
"""
from __future__ import annotations

from forge import cloudstore as cs  # noqa: E402


def _command_or_setup(ctx):
    """(command, None) when the rclone binary is on PATH;
    (None, the plain-words setup message) otherwise. Every verb
    answers with the setup words when the backend is absent —
    there is nothing to fence-check against a missing tool."""
    resolved = cs.resolve_config(getattr(ctx, "config", None) or {})
    command = resolved["command"]
    if cs.command_path(command) is None:
        return None, cs.setup_message(command)
    return command, None


def cloud_list(args, ctx):
    command, error = _command_or_setup(ctx)
    if error:
        return error
    return cs.do_list(command, ctx.workspace,
                      str(args.get("remote", "")))


def cloud_read(args, ctx):
    command, error = _command_or_setup(ctx)
    if error:
        return error
    return cs.do_read(command, ctx.workspace,
                      str(args.get("remote", "")))


def cloud_download(args, ctx):
    command, error = _command_or_setup(ctx)
    if error:
        return error
    return cs.do_download(command, ctx.workspace,
                          str(args.get("remote", "")),
                          str(args.get("local", "")))


def cloud_upload(args, ctx):
    command, error = _command_or_setup(ctx)
    if error:
        return error
    return cs.do_upload(command, ctx.workspace,
                        str(args.get("local", "")),
                        str(args.get("remote", "")))


def cloud_delete(args, ctx):
    command, error = _command_or_setup(ctx)
    if error:
        return error
    return cs.do_delete(command, ctx.workspace,
                        str(args.get("remote", "")))


HANDLERS = {"cloud_list": cloud_list, "cloud_read": cloud_read,
            "cloud_download": cloud_download,
            "cloud_upload": cloud_upload, "cloud_delete": cloud_delete}
