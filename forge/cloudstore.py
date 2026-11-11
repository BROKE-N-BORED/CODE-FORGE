"""Cloud storage connector machinery — Item #70 (ADOPT, don't build).

The owner asked to "connect my Google Drive or other third party
cloud storage service." Forge builds NO storage backend and holds
NO cloud credentials. It adopts rclone — one free, single-binary
tool that already speaks Drive, Dropbox, OneDrive, Nextcloud /
WebDAV, S3 and dozens more — and drives it with subprocess
argument lists, exactly the posture item #55 took with Browse.

Why rclone and not a Drive MCP server (the route not taken): the
known Drive MCP servers make the OWNER create a Google Cloud
project and an OAuth client first — a client-secret JSON that has
to live somewhere, i.e. a brand-new credential surface inside
Forge's world, against this item's absolute rule that the
connector stores no secrets of its own. And it would be one
server per service. rclone inverts both: the owner runs
`rclone config` once in a terminal, rclone runs the OAuth flow in
the owner's browser and keeps the tokens in ITS OWN config file
(~/.config/rclone/rclone.conf). Forge never sees a token, a
client secret, or a refresh flow — it sees remote NAMES
(`rclone listremotes`) and moves files. The only config Forge
reads is which binary to run (connectors.json "cloudstore" →
"command", default "rclone").

This module is the logic half (no Qt); plugins/cloudstore/
plugin.py is the thin handler shell over it.

THE FENCE — the project's .forge/cloudstore.json names allowed
remote prefixes: {"allowed": ["gdrive:Forge", "dropbox:shared"]}.
Every verb's remote target is normalized lexically (backslashes
become slashes, "." and ".." collapse) and must BE a prefix or sit
UNDER one ("gdrive:Forge" allows "gdrive:Forge/notes.txt", never
"gdrive:Forge2/x" and never "gdrive:secret.txt" reached via
"gdrive:Forge/../secret.txt"). The check runs BEFORE any rclone
invocation — a fence refusal means the backend was never called.
No fence file = nothing allowed: the fence starts shut, and its
refusal names the file that opens it. Local paths (an upload's
source, a download's destination) are judged by the tool layer's
own jail (forge.tools._inside), never by new code here.

DISCIPLINE — explicit transfers only. There is no sync verb, no
mirror, no background anything: nothing runs unless a tool call
asks for it. cloud_list / cloud_read are read-class; download,
upload and delete are approval-gated from day one (a download
writes a local file, so it gates like a write). cloud_read is
size-capped and refuses binaries in plain words — a file too big
for the turn is a download, not a read.
"""
from __future__ import annotations
import json
import posixpath
import shutil
import subprocess
from pathlib import Path

from . import projects as proj
from .tools import ToolError, _inside

# The model-facing surface: plugin.json declares these exact names.
VERB_TOOLS = ("cloud_list", "cloud_read", "cloud_download",
              "cloud_upload", "cloud_delete")

FENCE_NAME = "cloudstore.json"  # .forge/cloudstore.json in the project

DEFAULT_COMMAND = "rclone"

# cloud_read's cap: the bytes of ONE file fetched into the turn.
# The context window is the budget (the same 8K rule Browse's text
# pin follows) — anything bigger travels as a download instead.
READ_MAX_BYTES = 64 * 1024

LIST_MAX_ENTRIES = 200          # listing lines per call
_STDOUT_CAP = 8 * 1024 * 1024   # lsjson sanity cap
_STDERR_CAP = 500               # chars of backend stderr surfaced

_TIMEOUTS = {"listremotes": 15, "lsjson": 60,
             "cat": 60, "copyto": 300, "deletefile": 60}


# ---------- connector registration ----------

def preset_config() -> dict:
    """The connectors.json block for this connector. The single
    source for the README's setup text and the setup message, so
    the two can never drift. It names ONLY the binary — there is
    deliberately no credential field anywhere in this connector:
    rclone owns the tokens, in rclone's own config."""
    return {"cloudstore": {"command": DEFAULT_COMMAND}}


def setup_message(command: str = DEFAULT_COMMAND) -> str:
    preset = json.dumps(preset_config(), indent=2)
    return (f"Cloud storage is not set up yet: no '{command}' "
            f"command was found on PATH. Cloud storage rides "
            f"rclone — one free tool that speaks Google Drive, "
            f"Dropbox, OneDrive, Nextcloud/WebDAV, S3 and more — "
            f"and Forge never holds cloud credentials: rclone "
            f"keeps the login tokens in its OWN config. Setup, on "
            f"your PC:\n"
            f"1. Install rclone (your package manager, or the "
            f"single binary from rclone.org).\n"
            f"2. In a terminal run:  rclone config\n"
            f"   Create a remote for your service (name it e.g. "
            f"'gdrive') and sign in there — the OAuth tokens live "
            f"in rclone's config file, never in Forge.\n"
            f"3. List the remote folders the agent may touch in "
            f"the project's .forge/{FENCE_NAME}, e.g. "
            f'{{"allowed": ["gdrive:Forge"]}} — nothing is '
            f"allowed until that file names it.\n"
            f"4. Enable Cloud Storage in Customize → Connectors. "
            f"If your rclone binary has another name or path, "
            f"this connectors.json block in the app data dir says "
            f"which command to run:\n{preset}")


def no_remotes_message(command: str = DEFAULT_COMMAND) -> str:
    return (f"Cloud storage: '{command}' is installed but has no "
            f"remotes configured — `rclone listremotes` comes back "
            f"empty. Run `rclone config` in a terminal once and "
            f"create a remote for your service (name it e.g. "
            f"'gdrive'); rclone holds the login tokens in its own "
            f"config, Forge never sees them. Then this call can be "
            f"retried.")


def resolve_config(config: dict | None) -> dict:
    """Merge the connectors.json "cloudstore" key over the
    defaults. Only the binary name is configurable — there is no
    credential to configure, by design."""
    raw = (config or {}).get("cloudstore")
    if not isinstance(raw, dict):
        raw = {}
    command = str(raw.get("command") or DEFAULT_COMMAND).strip() \
        or DEFAULT_COMMAND
    return {"command": command}


def command_path(command: str) -> str | None:
    return shutil.which(command)


# ---------- the backend ----------

class _BackendMissing(Exception):
    """The rclone binary vanished between the PATH check and the
    call (or was never there)."""


def _run(command: str, args: list[str], verb: str) -> tuple[int, bytes, str]:
    """One backend call: an argument LIST, never a shell string.
    Returns (returncode, stdout bytes, stderr text capped). A
    timeout or a spawn failure comes back as rc -1 with the plain
    explanation in the stderr slot — never a traceback."""
    try:
        proc = subprocess.run(
            [command, *args], capture_output=True,
            timeout=_TIMEOUTS.get(verb, 60))
    except FileNotFoundError:
        raise _BackendMissing(command)
    except subprocess.TimeoutExpired:
        return -1, b"", (f"rclone timed out after "
                         f"{_TIMEOUTS.get(verb, 60)}s running '{verb}'")
    except OSError as exc:
        return -1, b"", f"rclone could not be run: {exc}"
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    return proc.returncode, proc.stdout[:_STDOUT_CAP], stderr[:_STDERR_CAP]


def _fail(verb_words: str, stderr: str) -> str:
    detail = stderr.strip()
    if detail:
        return f"Cloud storage: rclone could not {verb_words} — {detail}"
    return f"Cloud storage: rclone could not {verb_words} (it said nothing more)."


def list_remotes(command: str) -> list[str] | None:
    """Configured remote names ("gdrive", "dropbox", …) — NAMES
    only; rclone's config contents never pass through Forge.
    None = the backend call itself failed."""
    try:
        rc, out, _err = _run(command, ["listremotes"], "listremotes")
    except _BackendMissing:
        return None
    if rc != 0:
        return None
    names = []
    for line in out.decode("utf-8", errors="replace").splitlines():
        name = line.strip().rstrip(":").strip()
        if name:
            names.append(name)
    return names


def _remotes_guard(command: str) -> str | None:
    """None when at least one remote is configured; otherwise the
    plain words the model reads. Runs only AFTER the fence has
    passed, so a fence refusal never spends a backend call."""
    remotes = list_remotes(command)
    if remotes is None:
        return (f"Cloud storage: '{command} listremotes' failed — "
                "rclone is on PATH but is not answering. Check the "
                "install (run `rclone version` in a terminal), "
                "then retry.")
    if not remotes:
        return no_remotes_message(command)
    return None


# ---------- the remote fence ----------

def load_fence(workspace) -> list[str]:
    """The project's allowed remote prefixes:
    .forge/cloudstore.json holding {"allowed": ["gdrive:Forge"]}.
    Missing, unreadable, or malformed means NO prefixes — the
    fence starts shut, it never fails open."""
    try:
        data = json.loads(
            (proj.forge_area(workspace) / FENCE_NAME)
            .read_text(encoding="utf-8"))
    except Exception:
        return []
    entries = data.get("allowed") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    return [str(e).strip() for e in entries if str(e).strip()]


def normalize_remote(target) -> str | None:
    """The canonical "remote:path" form of a target, or None when
    the target is malformed or climbs out with "..". Judged
    lexically: backslashes become slashes, "." and ".." collapse,
    and the path part never keeps a leading slash ("gdrive:/Forge"
    and "gdrive:Forge" are the same place). The normalized form is
    the operative target for BOTH the fence and the backend call —
    one canonical shape, so what was checked is what runs."""
    text = str(target or "").strip()
    if ":" not in text:
        return None
    remote, _, path = text.partition(":")
    remote = remote.strip()
    if not remote or "/" in remote or "\\" in remote:
        return None
    path = path.replace("\\", "/")
    norm = posixpath.normpath(path.lstrip("/"))
    if norm == ".":
        norm = ""
    if norm == ".." or norm.startswith("../"):
        return None
    return f"{remote}:{norm}" if norm else f"{remote}:"


def _under(target_norm: str, prefix_norm: str) -> bool:
    if target_norm == prefix_norm:
        return True
    boundary = prefix_norm if prefix_norm.endswith(":") \
        else prefix_norm + "/"
    return target_norm.startswith(boundary)


def check_remote(target, allowed: list[str]) -> tuple:
    """(normalized target, None) when the fence allows it;
    (None, plain refusal) otherwise. Pure — no backend call has
    happened when this answers, and callers rely on that."""
    text = str(target or "").strip()
    norm = normalize_remote(text)
    if norm is None:
        if ":" in text and ".." in text.replace("\\", "/"):
            return None, (f"Cloud storage refused: '{text}' climbs "
                          "out of its folder with '..'. Remote "
                          "targets must stay inside one of the "
                          "folders this project allows "
                          f"(.forge/{FENCE_NAME}).")
        return None, (f"Cloud storage refused: '{text}' is not a "
                      "remote target. Use the form remote:path — "
                      "e.g. 'gdrive:Forge/notes.txt' — with a "
                      "remote you configured in `rclone config`.")
    prefixes = [p for p in (normalize_remote(a) for a in allowed)
                if p is not None]
    if any(_under(norm, p) for p in prefixes):
        return norm, None
    where = f".forge/{FENCE_NAME}"
    if not prefixes:
        return None, (f"Cloud storage refused: this project has "
                      f"no cloud storage fence, so no remote folder "
                      f"is allowed yet. The owner adds prefixes to "
                      f"{where} in the project folder, e.g. "
                      f'{{"allowed": ["gdrive:Forge"]}} — then the '
                      "call can be retried.")
    return None, (f"Cloud storage refused: '{norm}' is not under "
                  f"any folder this project allows ({where}). The "
                  "owner can add its prefix there; until then this "
                  "call does not happen.")


# ---------- the verbs ----------

def _fence(workspace, target) -> tuple:
    """(normalized target, None) or (None, refusal). The fence is
    pure — no backend call has happened when it answers."""
    return check_remote(target, load_fence(workspace))


def _human_size(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    if num < 1024 * 1024 * 1024:
        return f"{num / 1024 / 1024:.1f} MB"
    return f"{num / 1024 / 1024 / 1024:.1f} GB"


def do_list(command: str, workspace, target) -> str:
    norm, refusal = _fence(workspace, target)
    if refusal:
        return refusal
    refusal = _remotes_guard(command)
    if refusal:
        return refusal
    rc, out, err = _run(command, ["lsjson", norm], "lsjson")
    if rc != 0:
        return _fail(f"list {norm}", err)
    try:
        entries = json.loads(out.decode("utf-8", errors="replace"))
        if not isinstance(entries, list):
            raise ValueError("not a list")
    except Exception:
        return (f"Cloud storage: rclone's listing for {norm} could "
                "not be read (it did not come back as JSON).")
    if not entries:
        return f"{norm} is empty — nothing is stored there."
    lines = [f"Contents of {norm} ({len(entries)} item"
             f"{'s' if len(entries) != 1 else ''}):"]
    for entry in entries[:LIST_MAX_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("Name") or entry.get("Path") or "?")
        if entry.get("IsDir"):
            lines.append(f"  {name}/  (folder)")
        else:
            size = entry.get("Size")
            suffix = f"  ({_human_size(int(size))})" \
                if isinstance(size, (int, float)) else ""
            lines.append(f"  {name}{suffix}")
    if len(entries) > LIST_MAX_ENTRIES:
        lines.append(f"  … and {len(entries) - LIST_MAX_ENTRIES} more "
                     "(listing capped).")
    return "\n".join(lines)


def _stat_file(command: str, norm: str) -> tuple:
    """(size in bytes, None) when `norm` names one remote FILE;
    (None, plain refusal) otherwise. Judged from the PARENT
    folder's lsjson listing — the one listing whose entries say
    plainly whether the named thing is a file or a folder, on any
    backend (a recursive size count cannot tell a one-file folder
    from a file)."""
    remote, _, path = norm.partition(":")
    if not path:
        return None, (f"Cloud storage: {norm} is the remote's "
                      "root folder, not a file. Use cloud_list to "
                      "see what is inside.")
    parent_path, _, name = path.rpartition("/")
    parent_norm = f"{remote}:{parent_path}" if parent_path \
        else f"{remote}:"
    rc, out, err = _run(command, ["lsjson", parent_norm], "lsjson")
    if rc != 0:
        return None, _fail(f"check {norm}", err)
    try:
        entries = json.loads(out.decode("utf-8", errors="replace"))
        if not isinstance(entries, list):
            raise ValueError("not a list")
    except Exception:
        return None, (f"Cloud storage: rclone's listing for "
                      f"{parent_norm} could not be read.")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("Name") or "") != name:
            continue
        if entry.get("IsDir"):
            return None, (f"Cloud storage: {norm} is a folder, "
                          "not a file. cloud_list shows what is "
                          "inside it.")
        size = entry.get("Size")
        if not isinstance(size, (int, float)) or size < 0:
            return None, (f"Cloud storage: rclone did not report "
                          f"a size for {norm}, so it is not read "
                          "or downloaded blind.")
        return int(size), None
    return None, (f"Cloud storage: rclone could not find a file "
                  f"at {norm} — the listing of {parent_norm} has "
                  f"no '{name}'.")


def do_read(command: str, workspace, target) -> str:
    norm, refusal = _fence(workspace, target)
    if refusal:
        return refusal
    refusal = _remotes_guard(command)
    if refusal:
        return refusal
    num_bytes, refusal = _stat_file(command, norm)
    if refusal:
        return refusal
    if num_bytes > READ_MAX_BYTES:
        return (f"Cloud storage: {norm} is "
                f"{_human_size(num_bytes)} — too large to read into "
                f"this conversation (the read cap is "
                f"{_human_size(READ_MAX_BYTES)}). Download it with "
                "cloud_download instead, then read it locally.")
    rc, out, err = _run(command, ["cat", norm], "cat")
    if rc != 0:
        return _fail(f"read {norm}", err)
    if b"\x00" in out[:8192]:
        return (f"Cloud storage: {norm} looks like a binary file "
                "(it contains NUL bytes), not text — reading it "
                "into the conversation would be garbage. Download "
                "it with cloud_download if you need the file.")
    try:
        text = out.decode("utf-8")
    except UnicodeDecodeError:
        return (f"Cloud storage: {norm} is not UTF-8 text, so it "
                "is treated as binary and not read into the "
                "conversation. Download it with cloud_download if "
                "you need the file.")
    return f"--- {norm} ({_human_size(len(out))}) ---\n{text}"


def _local_inside(workspace, local) -> tuple:
    """(resolved local Path, None) or (None, refusal) — judged by
    the tool layer's own workspace jail, forge.tools._inside."""
    text = str(local or "").strip()
    if not text:
        return None, ("Cloud storage refused: no local path was "
                      "given. Local paths are project-relative — "
                      "e.g. 'downloads/notes.txt'.")
    try:
        return _inside(Path(workspace), text), None
    except ToolError as exc:
        return None, f"Cloud storage refused: {exc}"


def do_download(command: str, workspace, target, local) -> str:
    norm, refusal = _fence(workspace, target)
    if refusal:
        return refusal
    dest, refusal = _local_inside(workspace, local)
    if refusal:
        return refusal
    refusal = _remotes_guard(command)
    if refusal:
        return refusal
    _num_bytes, refusal = _stat_file(command, norm)
    if refusal:
        return refusal
    dest.parent.mkdir(parents=True, exist_ok=True)
    rc, _out, err = _run(
        command, ["copyto", norm, str(dest)], "copyto")
    if rc != 0:
        return _fail(f"download {norm}", err)
    if not dest.is_file():
        return (f"Cloud storage: rclone reported success but "
                f"{dest} is not on disk — the download did not "
                "land. Nothing was changed remotely.")
    rel = dest.relative_to(Path(workspace).resolve())
    return (f"Downloaded {norm} → {rel} "
            f"({_human_size(dest.stat().st_size)}). The file is "
            "in the project now; the remote copy is untouched.")


def do_upload(command: str, workspace, local, target) -> str:
    norm, refusal = _fence(workspace, target)
    if refusal:
        return refusal
    src, refusal = _local_inside(workspace, local)
    if refusal:
        return refusal
    if not src.is_file():
        return (f"Cloud storage refused: there is no file "
                f"'{local}' in this project to upload.")
    refusal = _remotes_guard(command)
    if refusal:
        return refusal
    rc, _out, err = _run(
        command, ["copyto", str(src), norm], "copyto")
    if rc != 0:
        return _fail(f"upload to {norm}", err)
    return (f"Uploaded {local} → {norm} "
            f"({_human_size(src.stat().st_size)}). The project "
            "copy stays where it was — this was a copy, not a move.")


def do_delete(command: str, workspace, target) -> str:
    norm, refusal = _fence(workspace, target)
    if refusal:
        return refusal
    refusal = _remotes_guard(command)
    if refusal:
        return refusal
    rc, _out, err = _run(command, ["deletefile", norm], "deletefile")
    if rc != 0:
        return _fail(f"delete {norm}", err)
    return (f"Deleted {norm} from cloud storage. Only that one "
            "file was removed — cloud_delete never touches a "
            "folder's contents.")
