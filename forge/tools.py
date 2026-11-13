"""Explicitly approved local tools. File operations stay inside the selected workspace.

Forge v2 changes:
- F1: arguments are VALIDATED before the approval dialog ever opens —
  a broken path (".", empty, missing filename) is refused by the shell,
  not presented to the user as if it were a real request.
- The approval dialog gets the FULL write content (scrollable), not a
  1,800-char preview.
- Note 17 containment pack: protected credential paths are refused even
  inside the workspace; run_command runs in its own process group and a
  timeout kills the WHOLE group (a timed-out command must never outlive
  its session in the background); workspace_danger() flags home/system
  roots so the shell can warn before they're used as a workspace.
- Audit fix round, bite #1 (2026-11-06): run_command no longer dies at
  120s. A command runs to completion or until the OWNER presses Stop —
  the worker threads its cancel event into execute_tool, and Stop kills
  the whole process group mid-command. The only remaining ceiling is
  the documented runaway guard below.
- Numbered list, item 8 (Note 3): append_file — a single write_file
  call is bounded by the model's max output tokens, so big files are
  built in pieces: write_file starts one, append_file continues it
  in chunks. Same jail, same approval gate, same write class.
- Numbered list, item 11 (Note 29): list_directory / read_file errors
  for a path that does not resolve carry a correction hint when the
  basename plainly lives elsewhere in the workspace ("did you mean
  `skills/`?") — and stay byte-identical to the old bare error when
  no unambiguous candidate exists. See _correction_hint.
- Write/Research modes (spec, 2026-11-09): the file kit grows —
  edit_file (targeted find-and-replace; missing or non-unique
  matches are loud refusals, nothing written), copy_file (byte-safe,
  any file type), move_file (never overwrites), delete_file (files
  only; the WORKER lays a forced safety checkpoint before it runs).
  read_file reads any file type honestly: .pdf/.docx/.odt yield
  stdlib-extracted text or a plain no-text statement, images and
  binaries a plain type/size statement — never invented content.
"""
from __future__ import annotations
import html
import io
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import zipfile
import zlib
from pathlib import Path

from . import production as production_mod
from . import codegraph as codegraph_mod

# Runaway guard for run_command — NOT an execution limit. Owner's rule:
# commands run to completion; the human's Stop button is the control.
# 1800s exists only so a genuinely stuck or forgotten process cannot
# run for days, and it matches the Ollama request timeout raised under
# F3. History: 120s shipped here from v1 through v2.5 and silently
# killed real builds and test suites mid-flight — the exact v1 death
# F3 was supposed to have fixed and missed (audit bite #1).
COMMAND_TIMEOUT = 1800
# Audit B23: run_command output SPOOLS TO DISK under a hard cap instead
# of being buffered whole in RAM by communicate() — a command spewing
# hundreds of MB (a runaway build log, `yes`, a cat of a huge file) used
# to eat the machine's memory before the 12,000-char result slice ever
# applied. Past this combined spool size the process group is killed
# and the kill is reported as its own case.
COMMAND_OUTPUT_CAP = 32 * 1024 * 1024  # 32 MB of combined stdout+stderr
# How much of each spool is read back to build the result. The result
# only ever shows the combined string's first 12,000 chars, and 12,000
# UTF-8 chars occupy at most ~48 KB — reading this much per stream
# reproduces the old full-buffer slice byte-for-byte.
_SPOOL_HEAD_BYTES = 48_004
# Credential stores the file tools must never touch, even when a broad
# workspace (e.g. the home folder) would otherwise contain them.
# Re-audit C1 (S2 W1): item #70's cloudstore adopted rclone, whose
# own config (~/.config/rclone/rclone.conf) holds OAuth refresh
# tokens — the segment covers the whole config dir, the name below
# covers the file wherever it sits.
PROTECTED_SEGMENTS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker",
                      "linux-forge-agent-shell", "rclone"}
# Sweep R134 (residual): id_ecdsa / id_dsa are the same never-read
# private-key class as the listed id_rsa / id_ed25519 — neither the
# H2 approval carve-out nor S2's app-file names cover them, so the
# refusal list itself learns them here. S2: Forge's own app files
# (connector config, the vault, settings, the conversation store)
# are protected by name as well.
PROTECTED_NAMES = {".netrc", ".git-credentials", "id_rsa", "id_ed25519",
                   "id_ecdsa", "id_dsa", "rclone.conf",
                   "connectors.json", "vault.json", "vault.key",
                   "settings.json", "conversations.sqlite3"}
DANGEROUS_WORKSPACE_ROOTS = {Path("/"), Path("/etc"), Path("/usr"),
                             Path("/var"), Path("/boot"), Path("/home")}

TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "list_directory", "description": "List files and folders in a directory inside the active workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Relative path; use . for workspace root"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a UTF-8 text file inside the active workspace. Long files are paged: one call returns at most `limit` lines (default 2000) starting at line `offset` (default 1); when a file does not fit, a footer says how many lines it has and which offset continues it.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "description": "1-based line number to start reading at (default 1)"}, "limit": {"type": "integer", "description": "Maximum number of lines to return (default 2000)"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "code_graph", "description": "Ask the project's code graph (Python only): find where a symbol is defined, which files use it, or which files a file connects to through imports. Name matches only. Actions: find, used_by, related.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "description": "find, used_by, or related"}, "target": {"type": "string", "description": "A symbol name (find, used_by) or a file path inside the workspace (related)"}}, "required": ["action", "target"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create or replace a UTF-8 text file inside the active workspace. Requires user approval. For a file too big for one call, write the first part here and continue it with append_file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "append_file", "description": "Append content to the end of a UTF-8 text file inside the active workspace, creating the file if it does not exist. Build a file too big for one write_file call by starting it with write_file, then continuing with append_file in chunks. Requires user approval.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "Make a targeted change to an existing UTF-8 text file inside the active workspace: replace old_text with new_text. old_text must occur EXACTLY ONCE in the file — if it is missing or occurs more than once the edit is refused and nothing changes (add more surrounding context to make it unique, or pass replace_all=true to replace every occurrence). Prefer this over rewriting a whole file with write_file. Requires user approval.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string", "description": "The exact text to replace, copied from the file"}, "new_text": {"type": "string", "description": "The replacement text"}, "replace_all": {"type": "boolean", "description": "Replace every occurrence of old_text instead of requiring exactly one (default false)"}}, "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {"name": "copy_file", "description": "Copy a file inside the active workspace to another path inside it, byte for byte — any file type, binary included. The destination's folders are created if needed; an existing destination file is overwritten. Requires user approval.", "parameters": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source", "destination"]}}},
    {"type": "function", "function": {"name": "move_file", "description": "Rename or move a file inside the active workspace. Refused if the destination already exists — a move never overwrites. Requires user approval.", "parameters": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source", "destination"]}}},
    {"type": "function", "function": {"name": "delete_file", "description": "Delete a file inside the active workspace. The shell takes a safety checkpoint of the project before the delete lands, so the file stays recoverable from Checkpoints. Files only — directories are refused. Requires user approval.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "run_command", "description": "Run a shell command in the active workspace. Requires user approval. Return stdout and stderr.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    # Remainder R4: the agent-callable replay tool — the M9 export
    # seam as a tool. No arguments: it always exports THIS
    # conversation to the same deterministic file the menu export
    # writes. Transcript-only from the agent (the turn timeline
    # lives in the window's session marks, which the worker cannot
    # see); the render says so honestly. Write class, approval
    # gated, offered in Code mode only (see permissions).
    {"type": "function", "function": {"name": "export_replay", "description": "Export this conversation as a session replay — one self-contained HTML file at .forge/replays/conversation-<id>-replay.html, with the transcript as it happened. Transcript-only from the agent: the turn timeline is added by the menu export. Requires user approval.", "parameters": {"type": "object", "properties": {}}}},
]

BUILTIN_NAMES = {s["function"]["name"] for s in TOOL_SCHEMAS}
FULL_PREVIEW_CHARS = 20000

# Audit B18: read_file paging. A whole 2 MB file poured into the
# prompt can eat the model's window in one call, so reads page by
# lines: at most READ_FILE_DEFAULT_LIMIT lines per call unless the
# caller asks for a different window (clamped to
# READ_FILE_MAX_LIMIT). A file that fits the window is returned
# byte-identical to the old whole-file read — the footer naming the
# total and the next offset appears only when a window truncates
# the file or starts past line 1.
READ_FILE_DEFAULT_LIMIT = 2000
READ_FILE_MAX_LIMIT = 10000

# Write/Research modes (spec, 2026-11-09): read_file reads "any
# file type" honestly. Text files read as before. Text-bearing
# documents (.pdf / .docx / .odt) get their text EXTRACTED with
# the standard library only — a .docx/.odt is a zip of XML; a PDF
# yields whatever its embedded (FlateDecode) text streams carry.
# No new dependency is added for this: where extraction finds no
# text (a scanned-image PDF, an encrypted file, an exotic
# variant), read_file says exactly that instead of guessing.
# Images and other binaries are reported by type and size — an
# image's contents are a vision seat's job, and no vision seat
# ships in this build, so the note says so plainly.
DOCUMENT_EXTS = (".pdf", ".docx", ".odt")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
              ".ico", ".tif", ".tiff")

# S1-32 (security audit; the cap was ABSENT in the W+R build and
# is added at this merge, 2026-11-10): the decompression ceiling
# for extracted text. A document file is at most 2 MB on disk
# (read_file's own limit), but a crafted one can inflate far past
# that — extraction never materializes more than this much text
# from any one zip member or PDF stream, or in total.
DOCUMENT_EXTRACT_MAX = 10_000_000


def _xml_to_text(xml: str, paragraph_close: str) -> str:
    """A zip-XML document body as plain text: paragraph ends
    become newlines, every other tag is dropped, entities are
    unescaped. Deliberately minimal — reading, not rendering."""
    text = re.sub(rf"</{paragraph_close}\s*>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _extract_zipxml_text(data: bytes, member: str,
                         paragraph_close: str) -> str | None:
    """The text of a .docx / .odt (both are zips of XML), or None
    when the bytes are not such a document or carry no text."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # S1-32: judge the member's INFLATED size before
            # reading it — a zip bomb's directory entry names it.
            if zf.getinfo(member).file_size > DOCUMENT_EXTRACT_MAX:
                return None
            xml = zf.read(member).decode("utf-8", "replace")
    except Exception:
        return None
    text = _xml_to_text(xml, paragraph_close)
    return text if text.strip() else None


def _pdf_literal(raw: bytes) -> str:
    """One PDF string literal's bytes as text: the standard
    escapes (\\n \\r \\t \\( \\) \\\\) and octal escapes resolved,
    decoded as Latin-1 (PDFDocEncoding's ASCII range agrees)."""
    out = []
    i = 0
    while i < len(raw):
        byte = raw[i]
        if byte == 0x5C and i + 1 < len(raw):  # backslash
            nxt = raw[i + 1]
            simple = {0x6E: "\n", 0x72: "\r", 0x74: "\t",
                      0x28: "(", 0x29: ")", 0x5C: "\\"}
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
            if 0x30 <= nxt <= 0x37:  # octal, up to 3 digits
                j = i + 1
                digits = []
                while j < len(raw) and len(digits) < 3 \
                        and 0x30 <= raw[j] <= 0x37:
                    digits.append(raw[j])
                    j += 1
                out.append(chr(int(bytes(digits), 8)))
                i = j
                continue
            out.append(chr(nxt))
            i += 2
            continue
        out.append(chr(byte))
        i += 1
    return "".join(out)


_PDF_STREAM_RE = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
_PDF_TJ_RE = re.compile(rb"\((?:\\.|[^\\()])*\)\s*Tj")
_PDF_TJ_ARRAY_RE = re.compile(rb"\[(.*?)\]\s*TJ", re.DOTALL)
_PDF_STR_RE = re.compile(rb"\((?:\\.|[^\\()])*\)")


def _extract_pdf_text(data: bytes) -> str | None:
    """The text embedded in a PDF's content streams, or None.

    Minimal by design (no PDF library ships with Forge): each
    stream is FlateDecode-inflated when it inflates, and the
    text-showing operators (Tj, TJ) contribute their string
    literals in order. Encrypted PDFs, image-only scans, and
    object-stream/compressed-xref variants yield nothing — the
    caller reports that honestly rather than inventing content."""
    chunks: list[str] = []
    for match in _PDF_STREAM_RE.finditer(data):
        raw = match.group(1)
        try:
            # S1-32: inflate under a hard ceiling — decompressobj
            # with max_length never materializes more than the cap
            # (+1, to detect the overflow) for one stream; a stream
            # that overflows it is skipped, not trusted as text.
            content = zlib.decompressobj().decompress(
                raw, DOCUMENT_EXTRACT_MAX + 1)
            if len(content) > DOCUMENT_EXTRACT_MAX:
                continue
        except Exception:
            content = raw  # uncompressed stream: read it as-is
        found: list[str] = []
        for tj in _PDF_TJ_ARRAY_RE.finditer(content):
            parts = [_pdf_literal(s[1:-1])
                     for s in _PDF_STR_RE.findall(tj.group(1))]
            if parts:
                found.append("".join(parts))
        for tj in _PDF_TJ_RE.finditer(content):
            literal = _PDF_STR_RE.search(tj.group(0))
            if literal:
                found.append(_pdf_literal(literal.group(0)[1:-1]))
        if found:
            chunks.append("\n".join(found))
            if sum(len(c) for c in chunks) > DOCUMENT_EXTRACT_MAX:
                break  # S1-32: the total is capped too
    text = "\n".join(chunks)
    return text if text.strip() else None


def extract_document_text(path: Path, data: bytes) -> str | None:
    """The readable text of a .pdf / .docx / .odt, or None when
    the file carries none that stdlib extraction can reach."""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _extract_zipxml_text(data, "word/document.xml", r"w:p")
    if suffix == ".odt":
        return _extract_zipxml_text(data, "content.xml", r"text:[ph]")
    if suffix == ".pdf":
        return _extract_pdf_text(data)
    return None



def _paging_int(value) -> int | None:
    """Coerce a read_file offset/limit to an int, or None when the
    value is not an integer at all. Ints pass through, integral
    floats and numeric strings coerce; anything else (including
    bools, which are not line numbers) is None — a validation
    problem at validate time, a silent default at execute time."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


class ToolError(RuntimeError):
    pass


# Study item M4 ("keep the wrong stuff in"): the compact diagnostic
# line for an UNEXPECTED tool failure. A ToolError's message is an
# authored diagnostic — written to tell the model exactly what was
# refused and how to fix the call — so it needs no help. Any OTHER
# exception escaping a tool or a plugin handler is a surprise (a bug,
# a broken environment), and the old flattening to "Tool error: {exc}"
# threw away the two facts the model steers by: the exception TYPE
# (a bare KeyError reads as just "'config'") and WHERE it was raised.
# This line carries both, in one capped line: type + message, and the
# deepest traceback frame as file:line in function. Callers append it
# to the failure the model reads; the full traceback stays a job for
# the event log, not the 8k window.
_FAILURE_DETAIL_CAP = 320


def failure_detail(exc) -> str:
    """The one-line M4 diagnostic for an unexpected exception, or ""
    for a ToolError (authored message — already the diagnostic) and
    for anything without a usable traceback/message."""
    if isinstance(exc, ToolError):
        return ""
    try:
        import traceback as _tb
        frames = _tb.extract_tb(exc.__traceback__) if \
            exc.__traceback__ is not None else []
        where = ""
        if frames:
            last = frames[-1]
            where = (f" — raised at {Path(last.filename).name}:"
                     f"{last.lineno} in {last.name}")
        line = (f"[Failure detail: {type(exc).__name__}: "
                f"{exc}{where}]")
    except Exception:
        return ""
    if len(line) > _FAILURE_DETAIL_CAP:
        line = line[:_FAILURE_DETAIL_CAP - 1] + "…]"
    return line


def _inside(root: Path, relative: str) -> Path:
    root = root.expanduser().resolve()
    # resolve() follows symlinks, so a symlink inside the workspace that
    # points outside resolves outside and is refused here.
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ToolError("Path is outside the active workspace.")
    rel_parts = target.relative_to(root).parts if target != root else ()
    if any(part in PROTECTED_SEGMENTS for part in rel_parts) or target.name in PROTECTED_NAMES:
        raise ToolError(f"Protected path refused: {relative} (credential stores are off-limits to file tools).")
    return target


# ------------------------------------------------------------------
# Contained run (owner directive, 2026-11-09): an opt-in cage around
# the tool layer, modeled on the minimal-agent pattern Joshua saw —
# a handful of tools, all of them able to touch ONE folder only. When
# the window passes a Containment policy, file tools refuse any path
# that resolves outside the project root, and run_command runs with
# the project root as its working directory while commands that reach
# outside the root in writing are blocked. Copy law (the same words
# the Settings caption uses): this is a guardrail at the tool layer,
# NOT an OS sandbox — it catches what is written in the call and backs
# up the approval gate; it does not replace it. The optional Docker
# cage is the true sandbox (only the project folder is mounted into
# the container), but Docker is never required: absent or failed, the
# contained local run proceeds and the result says so in one line.
CONTAINED_DOCKER_DEFAULT_IMAGE = "python:3-slim"
# Device files a contained command may still name — they are not
# files in anyone's folder, and blocking them would brick ordinary
# shell idioms (2>/dev/null above all).
_CONTAINED_DEVICE_OK = ("/dev/null", "/dev/stdout", "/dev/stderr",
                        "/dev/stdin", "/dev/fd", "/dev/zero")
# Argument names that carry a path in non-built-in (plugin) tools —
# plugin schemas are not fixed, so the worker checks these names
# against the same cage at dispatch time.
CONTAINED_PATH_ARG_KEYS = ("path", "file", "filepath", "file_path",
                           "directory", "dir", "folder")


class Containment:
    """The resolved contained-run policy for tool execution: the
    project root nothing may leave, plus the optional Docker cage."""

    def __init__(self, root, docker: bool = False,
                 docker_image: str = CONTAINED_DOCKER_DEFAULT_IMAGE):
        self.root = Path(root).expanduser().resolve()
        self.docker = bool(docker)
        self.docker_image = (str(docker_image or "").strip()
                             or CONTAINED_DOCKER_DEFAULT_IMAGE)


def contained_refusal(root, raw) -> str | None:
    """Contained run: the refusal text when a path argument resolves
    outside the project root, else None. The FULL resolved path is
    what gets checked — realpath settles symlinks and .. first —
    never a truncated display target."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None  # emptiness is validate_tool_args' refusal, not this one
    root_r = Path(os.path.realpath(str(root)))
    candidate = Path(os.path.expanduser(text))
    if not candidate.is_absolute():
        candidate = root_r / candidate
    resolved = Path(os.path.realpath(candidate))
    if resolved == root_r or root_r in resolved.parents:
        return None
    return (f"Contained run is ON: '{text}' resolves to {resolved}, which is "
            f"outside the project folder ({root_r}) — refused. In contained "
            f"run the agent's tools can only touch files inside the project "
            f"folder.")


def contained_args_refusal(root, args) -> str | None:
    """The same cage for any tool whose arguments carry a path under
    a common name (plugin tools have no fixed schema to check)."""
    if not isinstance(args, dict):
        return None
    for key in CONTAINED_PATH_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            refusal = contained_refusal(root, value)
            if refusal is not None:
                return refusal
    return None


_COMMAND_TOKEN_SPLIT = re.compile(r"[\s;|&<>()'\"]+")
_CD_RE = re.compile(r"(?:^|[;|&()\n'\"]\s*)cd(?:\s+([^\s;|&]+))?")


def _contained_token_detail(root_r: Path, token: str,
                            verb: str = "references") -> str | None:
    """One command token: a short "it ..." clause when the token names
    a path outside the project root (absolute, ~, or a .. climb),
    else None. `verb` phrases the clause ("references" for a plain
    path, "changes directory to" for a cd target)."""
    token = token.strip().strip(",")
    if not token:
        return None
    if token.startswith(("http://", "https://", "ftp://", "git@")):
        return None  # a URL is not a local path
    # Attached redirects (>/tmp/out, 2>/dev/null, </etc/passwd): the
    # path rides on the operator when no space separates them.
    token = re.sub(r"^\d*[<>]+", "", token)
    if "=" in token and not token.startswith(("/", "./", "../")):
        # --flag=/abs/path and VAR=/abs/path forms: judge the value.
        token = token.split("=", 1)[1]
    if token.startswith("/"):
        if any(token == ok or token.startswith(ok + "/")
               for ok in _CONTAINED_DEVICE_OK):
            return None
        resolved = Path(os.path.realpath(token))
        if resolved == root_r or root_r in resolved.parents:
            return None
        return f"it {verb} '{token}', outside the project folder"
    if token.startswith("~"):
        resolved = Path(os.path.realpath(os.path.expanduser(token)))
        if resolved == root_r or root_r in resolved.parents:
            return None
        return f"it {verb} '{token}', outside the project folder"
    if token == ".." or token.startswith("../"):
        resolved = Path(os.path.realpath(root_r / token))
        if resolved == root_r or root_r in resolved.parents:
            return None
        reach = "climbs to" if verb == "references" else verb
        return f"it {reach} '{token}', outside the project folder"
    return None


def contained_command_refusal(root, command) -> str | None:
    """Contained run, local commands: a best-effort static check of
    the command text. Blocks cd escapes (a bare `cd` lands in HOME)
    and any written path outside the project root. It reads what is
    written — paths built from variables it cannot see, which is
    exactly why the honest copy calls this a guardrail, not a
    sandbox."""
    root_r = Path(os.path.realpath(str(root)))

    def blocked(detail: str) -> str:
        return (f"Contained run is ON: this command was blocked because "
                f"{detail} ({root_r}). Contained commands run with the "
                f"project folder as their working directory. This cage is "
                f"a guardrail at the tool layer, not an OS sandbox — it "
                f"catches paths written in the command. Rewrite the "
                f"command with paths inside the project folder, or turn "
                f"Contained run off in Settings → Behavior.")

    for match in _CD_RE.finditer(str(command or "")):
        target = match.group(1)
        if target is None:
            return blocked("it runs a bare `cd`, which leaves the "
                           "project folder for the home folder")
        target = target.strip("'\"")
        if target in ("-", "$OLDPWD"):
            return blocked("it changes directory with `cd -`, to a "
                           "previous location the cage cannot verify")
        detail = _contained_token_detail(root_r, target,
                                         verb="changes directory to")
        if detail is not None:
            return blocked(detail)
    for token in _COMMAND_TOKEN_SPLIT.split(str(command or "")):
        detail = _contained_token_detail(root_r, token)
        if detail is not None:
            return blocked(detail)
    return None


def _docker_available() -> bool:
    """Whether a docker binary is on PATH. A module-level seam so
    tests can mock detection — the suite never requires docker."""
    return shutil.which("docker") is not None


def _docker_argv(root, image, command) -> list:
    """The contained Docker invocation: ONLY the project folder is
    bind-mounted (at /work, also the workdir) — that single mount is
    the whole cage."""
    return ["docker", "run", "--rm", "-v", f"{root}:/work",
            "-w", "/work", image, "sh", "-c", command]


def _correction_hint(root: Path, target: Path, relative: str, want_dir: bool) -> str:
    """Item 11 (Note 29): the "did you mean" suffix for a failed path.

    The note's incident: the agent asked for forge/skills when the
    real folder is skills/ at the workspace root, and burned repeated
    approvals on the same wrong path. So when list_directory /
    read_file fails, look for the basename (case-insensitive) among
    the workspace root's entries, then one level into each of them —
    a CHEAP listing by construction, never a recursive walk — and
    offer the match. Rules, in order:

    - A root-level match beats nested ones (the note's case).
    - Exactly one candidate at the winning level: name it. Exactly
      two: name both. More than two, or none: stay silent — the
      driver follows hints literally, so a guessed hint is worse
      than the bare error, which stays byte-identical to before.
    - Candidates must be the kind the tool needs (a directory for
      list_directory, a file for read_file) and must resolve INSIDE
      the workspace — a symlinked entry pointing outside is never
      hinted at, same jail philosophy as _inside.
    - The requested target itself is never its own hint.
    """
    name = Path(relative).name
    if not name:
        return ""
    wanted = name.lower()

    def usable(entry: Path) -> bool:
        try:
            if entry.resolve() == target:
                return False
            resolved = entry.resolve()
            if resolved != root and root not in resolved.parents:
                return False
            return entry.is_dir() if want_dir else entry.is_file()
        except OSError:
            return False

    try:
        root_entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return ""
    hits = [e for e in root_entries if e.name.lower() == wanted and usable(e)]
    at_root = True
    if not hits:
        at_root = False
        for entry in root_entries:
            try:
                if not entry.is_dir():
                    continue
                resolved = entry.resolve()
                if resolved != root and root not in resolved.parents:
                    continue  # a symlink out of the jail: never look inside
                children = sorted(entry.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                continue
            hits.extend(e for e in children
                        if e.name.lower() == wanted and usable(e))
    if not hits or len(hits) > 2:
        return ""

    def shown(entry: Path) -> str:
        return entry.relative_to(root).as_posix() + ("/" if want_dir else "")

    if len(hits) == 1:
        where = " It exists at the workspace root." if at_root else ""
        return f" Did you mean `{shown(hits[0])}`?{where}"
    where = " Both exist at the workspace root." if at_root else ""
    return f" Did you mean `{shown(hits[0])}` or `{shown(hits[1])}`?{where}"


def workspace_danger(workspace: str) -> str | None:
    """A human-readable warning if this workspace root is dangerously
    broad (home directory, filesystem root, system trees), else None."""
    root = Path(workspace).expanduser().resolve()
    if root == Path.home().resolve():
        return ("This workspace is your entire HOME folder — the agent's file tools "
                "can reach every document you own. A project folder is safer.")
    if root in DANGEROUS_WORKSPACE_ROOTS:
        return f"This workspace is a system location ({root}) — file tools should never run here."
    return None


def validate_tool_args(name: str, args: dict) -> str | None:
    """F1: return an error string if the call is malformed, else None.
    Runs BEFORE approval — the user should never be asked to approve a
    broken request like writing to a path of '.' ."""
    if name in ("read_file", "write_file", "append_file"):
        path = str(args.get("path", "")).strip()
        if not path:
            return f"{name}: path is empty."
        if path in (".", "./") or path.endswith("/"):
            return f"{name}: '{path}' is a directory, not a file. Give a full file path."
        if name in ("write_file", "append_file") and "content" not in args:
            return f"{name}: no content given."
        if name in ("write_file", "append_file") \
                and not isinstance(args.get("content"), str):
            # Audit A13: types were never enforced, so content=None
            # sailed through and the tool wrote the literal text
            # "None" into the file.
            return f"{name}: content must be text."
        if name == "read_file":
            # Audit B18: paging args, when present, must be
            # integers. A numeric string coerces (and is written
            # back, so execution sees the clean value); anything
            # else is a malformed call, refused here before
            # approval is ever asked.
            for key in ("offset", "limit"):
                if args.get(key) is not None:
                    coerced = _paging_int(args[key])
                    if coerced is None:
                        return f"read_file: {key} must be an integer."
                    args[key] = coerced
    if name == "edit_file":
        path = str(args.get("path", "")).strip()
        if not path:
            return "edit_file: path is empty."
        if path in (".", "./") or path.endswith("/"):
            return f"edit_file: '{path}' is a directory, not a file. Give a full file path."
        if "old_text" not in args or "new_text" not in args:
            return "edit_file: both old_text and new_text are required."
        if not isinstance(args.get("old_text"), str) \
                or not isinstance(args.get("new_text"), str):
            return "edit_file: old_text and new_text must be text."
        if not args["old_text"]:
            return ("edit_file: old_text is empty — an empty match is "
                    "not an edit. Copy the exact text to replace "
                    "from the file.")
        if "replace_all" in args \
                and not isinstance(args.get("replace_all"), bool):
            return "edit_file: replace_all must be true or false."
    if name in ("copy_file", "move_file"):
        for key in ("source", "destination"):
            value = str(args.get(key, "")).strip()
            if not value:
                return f"{name}: {key} is empty."
            if value in (".", "./") or value.endswith("/"):
                return (f"{name}: '{value}' is a directory, not a "
                        f"file. Give a full file path for {key}.")
    if name == "delete_file":
        path = str(args.get("path", "")).strip()
        if not path:
            return "delete_file: path is empty."
        if path in (".", "./") or path.endswith("/"):
            return (f"delete_file: '{path}' is a directory, not a "
                    "file. delete_file removes files only.")
    if name == "list_directory" and "path" not in args:
        return "list_directory: no path given (use . for the workspace root)."
    if name == "run_command" and not str(args.get("command", "")).strip():
        return "run_command: command is empty."
    if name == "code_graph":
        # Tier 2 #2 (code graph, light edition): the action must be
        # one of the three the graph answers, the target must be
        # present, and a find/used_by target that looks like a file
        # path is refused here — files belong to the related action.
        action = str(args.get("action", "")).strip()
        if action not in codegraph_mod.ACTIONS:
            return (f"code_graph: unknown action '{action}' — use "
                    f"find, used_by, or related.")
        target = str(args.get("target", "")).strip()
        if not target:
            return "code_graph: target is empty."
        if action in ("find", "used_by") and (
                "/" in target or "\\" in target or ".." in target):
            return (f"code_graph: '{target}' looks like a file path "
                    f"— use action related for files.")
    return None


def _vault_secret(name: str):
    """Fail-soft read from the encrypted secrets vault (forge/vault.py).
    Any vault problem reads as "not stored" — a damaged vault must
    degrade to the old behaviour, never break a tool call."""
    try:
        from . import vault as vault_mod
        return vault_mod.get_secret(name)
    except Exception:
        return None


def _sudo_word_ends(command: str) -> list[int]:
    """End offsets of every ``sudo`` sitting in command position:
    the first word of the command or of a segment after a shell
    separator (&&, ||, ;, |, & or a newline), allowing leading
    parentheses and VAR=value assignments.  Quoted text is never
    scanned, so ``echo "sudo"`` — or a sudo inside any quoted string —
    does not count; neither does a sudo buried mid-segment
    (``echo sudo``) or inside a nested shell string
    (``bash -c "sudo ls"``), which keeps behaving exactly as today.
    """
    ends = []
    i, n = 0, len(command)
    in_single = in_double = False
    at_head = True
    while i < n:
        ch = command[i]
        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_double = False
            i += 1
            continue
        if ch == "'":
            in_single = True
            i += 1
            continue
        if ch == '"':
            in_double = True
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch in ";&|\n":
            at_head = True
            i += 1
            continue
        if at_head:
            j = i
            while True:
                while j < n and (command[j].isspace() or command[j] == "("):
                    j += 1
                start = j
                while j < n and not command[j].isspace() \
                        and command[j] not in ";&|()":
                    j += 1
                token = command[start:j]
                if not token:
                    break
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token,
                                re.DOTALL):
                    continue  # env assignment — still at the head
                if token == "sudo":
                    ends.append(j)
                break
            at_head = False
            i = max(j, i + 1)
            continue
        i += 1
    return ends


def command_uses_sudo(command: str) -> bool:
    """True when the command invokes sudo in a command position
    (see _sudo_word_ends for exactly what counts)."""
    return bool(_sudo_word_ends(str(command or "")))


def sudo_feed_plan(command: str) -> str:
    """The command with ``-S`` added to each command-position sudo
    that does not already read the password from stdin, so sudo takes
    the password Forge feeds it on stdin.  Byte-identical to the
    input everywhere else, and unchanged when no sudo is present."""
    command = str(command or "")
    ends = _sudo_word_ends(command)
    if not ends:
        return command
    out = command
    for end in sorted(ends, reverse=True):
        rest = out[end:].lstrip()
        if rest.startswith("-"):
            flag = rest.split(None, 1)[0]
            if "S" in flag[1:]:
                continue  # already reads the password from stdin
        out = out[:end] + " -S" + out[end:]
    return out


def sudo_approval_notice(command: str) -> str:
    """The extra approval-dialog line for a sudo run_command, or ""
    when the command does not use sudo."""
    if not command_uses_sudo(command):
        return ""
    if _vault_secret("sudo_password"):
        return ("⚠ This command runs with sudo (administrator). "
                "If approved, Forge will use your stored PC "
                "password — the password is never shown to the model.")
    return ("⚠ This command runs with sudo (administrator), and no "
            "PC password is stored in Forge's vault — sudo may "
            "fail without one.")


def describe_tool(name: str, args: dict, workspace: str) -> str:
    if name == "run_command":
        text = f"Run this command in {workspace}:\n\n{args.get('command', '')}"
        notice = sudo_approval_notice(str(args.get("command", "")))
        if notice:
            text += f"\n\n{notice}"
        return text
    if name == "write_file":
        content = str(args.get("content", ""))
        preview = content[:FULL_PREVIEW_CHARS] + (
            f"\n… [preview truncated — {len(content)} chars total]" if len(content) > FULL_PREVIEW_CHARS else "")
        return f"Write {args.get('path')} inside {workspace}?\n\n{preview}"
    if name == "append_file":
        content = str(args.get("content", ""))
        preview = content[:FULL_PREVIEW_CHARS] + (
            f"\n… [preview truncated — {len(content)} chars total]" if len(content) > FULL_PREVIEW_CHARS else "")
        return f"Append to {args.get('path')} inside {workspace}?\n\n{preview}"
    if name == "edit_file":
        old = str(args.get("old_text", ""))
        new = str(args.get("new_text", ""))
        scope = "every occurrence" if args.get("replace_all") else "the one occurrence"
        return (f"Edit {args.get('path')} inside {workspace} — replace {scope} of:\n\n"
                f"--- old ---\n{old[:FULL_PREVIEW_CHARS]}\n--- new ---\n{new[:FULL_PREVIEW_CHARS]}")
    if name == "copy_file":
        return (f"Copy {args.get('source')} to {args.get('destination')} "
                f"inside {workspace}? (byte for byte; an existing "
                "destination file is overwritten)")
    if name == "move_file":
        return (f"Move {args.get('source')} to {args.get('destination')} "
                f"inside {workspace}? (refused if the destination exists)")
    if name == "delete_file":
        return (f"Delete {args.get('path')} inside {workspace}? "
                "A safety checkpoint of the project is taken first, "
                "so the file stays recoverable from Checkpoints.")
    if name == "read_file":
        return f"Read {args.get('path')} from {workspace}?"
    if name == "list_directory":
        return f"List {args.get('path')} inside {workspace}?"
    if name == "code_graph":
        return (f"Ask the code graph: {args.get('action')} "
                f"'{args.get('target')}' in {workspace}?")
    if name == "export_replay":
        # Remainder R4: the approval dialog names the file the
        # tool writes — a fixed destination, no path argument.
        return (f"Export this conversation as a session replay — "
                f"one HTML file at .forge/replays/ inside "
                f"{workspace}? (transcript-only; the turn timeline "
                f"is added by the menu export)")
    return f"Unknown tool request: {name}"


def describe_tool_short(name: str, args: dict) -> str:
    """One-line target summary for logs and receipts."""
    if name == "run_command":
        return str(args.get("command", ""))[:90]
    if name in ("copy_file", "move_file"):
        return f"{args.get('source', '')} -> {args.get('destination', '')}"[:90]
    if name == "code_graph":
        return f"{args.get('action', '')} {args.get('target', '')}"[:90]
    if name == "export_replay":
        # Remainder R4: no path argument — the receipt names the
        # fixed destination.
        return ".forge/replays/"
    return str(args.get("path", args.get("name", "")))[:90]


def describe_tool_full(name: str, args: dict) -> str:
    """S3 (audit S1-03): the UNtruncated target a permission rule is
    created from and matched against. describe_tool_short stays the
    display string (logs, receipts, dialog targets); this is the rule
    key. A remembered rule keyed on the 90-char display prefix used
    to auto-allow ANY later call sharing that prefix — a different
    command tail past char 90 never reached the owner. The rule must
    name the whole call it approves. Two-path tools (source +
    destination) key on both ends, never the source alone."""
    if name == "run_command":
        return str(args.get("command", ""))
    source, destination = args.get("source"), args.get("destination")
    if source is not None and destination is not None:
        return f"{source} -> {destination}"
    return str(args.get("path", args.get("name", "")))


def _combined_spool_output(spool_out, spool_err) -> tuple[str, int, bool]:
    """Audit B23: build run_command's combined output string from the
    two spools. Returns (combined, total_chars, truncated) where
    combined is EXACTLY the string the old communicate() path built —
    stdout + "\\n[stderr]\\n" + stderr (when stderr is non-empty),
    stripped — truncated says whether it runs past the 12,000 chars the
    result shows, and total_chars is its true length when both spools
    were read whole, else a byte-size approximation (the caller words
    the truncation note with "about")."""
    out_size = os.fstat(spool_out.fileno()).st_size
    err_size = os.fstat(spool_err.fileno()).st_size
    spool_out.seek(0)
    head_out = spool_out.read(_SPOOL_HEAD_BYTES)
    spool_err.seek(0)
    head_err = spool_err.read(_SPOOL_HEAD_BYTES)
    stdout = head_out.decode("utf-8", "replace")
    stderr = head_err.decode("utf-8", "replace")
    combined = (stdout + ("\n[stderr]\n" + stderr if err_size else "")).strip()
    if len(head_out) == out_size and len(head_err) == err_size:
        total = len(combined)
    else:
        total = out_size + err_size + (10 if err_size else 0)
    return combined, total, len(combined) > 12000


def _atomic_write_text(target: Path, content: str) -> None:
    """Sweep R133: write_file's write path — temp file in the SAME
    directory, fsync, atomic replace, so a crash or power loss
    mid-write can never leave the owner's source file truncated
    (planning.py and update.py already write this way; the agent's
    own file tool was the odd one out). An existing file's permission
    bits ride across the replace; a fresh file keeps the temp's."""
    try:
        mode = target.stat().st_mode & 0o777
    except OSError:
        mode = None
    fd, tmp = tempfile.mkstemp(dir=str(target.parent),
                               prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def execute_tool(name: str, args: dict, workspace: str, cancel_event=None,
                 containment=None, production=None) -> str:
    """Run one built-in tool. cancel_event (a threading.Event, passed by
    the worker) lets the owner's Stop kill a running command: when it
    fires, run_command kills the whole process group and reports the
    stop honestly instead of running on to the runaway ceiling.
    containment (a tools.Containment, passed by the worker only when
    the owner has Contained run on) cages the call to the project
    root; None — the default — is exactly the old behavior.
    production (a production.ProductionMarkings, passed by the
    worker only when the project has a .forge/production.json)
    makes marked paths read-only by rule (item C2); None — the
    default, and the state of every unmarked project — is exactly
    the old behavior too."""
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ToolError(f"Workspace directory does not exist: {root}")
    if containment is not None and name in (
            "list_directory", "read_file", "write_file", "append_file",
            "edit_file", "delete_file"):
        # The cage speaks first, in its own words; the workspace jail
        # in _inside still applies underneath it.
        refusal = contained_refusal(containment.root, args.get("path", ""))
        if refusal is not None:
            raise ToolError(refusal)
    if containment is not None and name in ("copy_file", "move_file"):
        # Both ends of the move live inside the cage or neither does.
        for key in ("source", "destination"):
            refusal = contained_refusal(containment.root, args.get(key, ""))
            if refusal is not None:
                raise ToolError(refusal)
    if production is not None and name in (
            "write_file", "append_file", "edit_file", "delete_file"):
        # Item C2: production data is read-only by rule. The check
        # judges the path as written; _inside's jail and protected-
        # path refusals still apply underneath, unchanged. (The
        # C2 build's own tree had only write/append; edit_file and
        # delete_file are the Write/Research kit judged by the same
        # one check — a delete of production data is a write to it.)
        refusal = production_mod.path_refusal(
            production, root, args.get("path", ""),
            shown=args.get("path", ""))
        if refusal is not None:
            raise ToolError(refusal)
    if production is not None and name in ("copy_file", "move_file"):
        # Item C2, the Write/Research kit's two-ended tools, judged
        # the way the command fence judges cp vs mv: a copy READS
        # its source (copying FROM production is a read and passes)
        # and writes only its destination; a move writes its
        # destination AND deletes its source, so both ends are
        # judged.
        judged = ("destination",) if name == "copy_file" else (
            "source", "destination")
        for key in judged:
            refusal = production_mod.path_refusal(
                production, root, args.get(key, ""),
                shown=args.get(key, ""))
            if refusal is not None:
                raise ToolError(refusal)
    if production is not None and name == "run_command":
        refusal = production_mod.command_refusal(
            production, root, args.get("command", ""))
        if refusal is not None:
            raise ToolError(refusal)
    if name == "list_directory":
        target = _inside(root, str(args.get("path", ".")))
        if not target.is_dir():
            raise ToolError(f"Not a directory: {target}" + _correction_hint(
                root, target, str(args.get("path", ".")), want_dir=True))
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        lines = [("[DIR] " if p.is_dir() else "[FILE] ") + p.name
                 for p in entries[:500]]
        if len(entries) > 500:
            # Sweep R142: the cap used to be silent — the model
            # reported a partial listing as the directory's contents.
            lines.append(f"[... and {len(entries) - 500} more entries "
                         f"— {len(entries)} in total; list a subfolder "
                         "to see the rest]")
        return "\n".join(lines) or "(empty directory)"
    if name == "read_file":
        target = _inside(root, str(args.get("path", "")))
        if not target.is_file():
            raise ToolError(f"File does not exist: {target}" + _correction_hint(
                root, target, str(args.get("path", "")), want_dir=False))
        size = target.stat().st_size
        if size > 2_000_000:
            raise ToolError("File exceeds the 2 MB read limit.")
        rel = target.relative_to(root)
        suffix = target.suffix.lower()
        # Write/Research modes (spec, 2026-11-09): any file type
        # gets an honest read — documents yield their extracted
        # text, images and binaries yield a plain statement of
        # what the file is, and only real text reaches the paging
        # below. The notes are RESULTS, not errors: the file
        # exists and this is the truth about it.
        if suffix in IMAGE_EXTS:
            return (f"[read_file: {rel} is an image file "
                    f"({suffix.lstrip('.')}, {size} bytes). Images "
                    "are read by a vision seat, and no vision seat "
                    "is wired into this build — the image's contents "
                    "cannot be read here. Ask the owner to describe "
                    "it, or open it with a vision-capable model.]")
        if suffix in DOCUMENT_EXTS:
            text = extract_document_text(target, target.read_bytes())
            if text is None:
                kind = {".pdf": "PDF", ".docx": "Word (.docx)",
                        ".odt": "OpenDocument (.odt)"}[suffix]
                return (f"[read_file: {rel} is a {kind} document "
                        f"({size} bytes) and no readable text could "
                        "be extracted from it — it may be scanned "
                        "images, encrypted, or a variant the "
                        "built-in extractor cannot reach. Nothing "
                        "about its contents can be stated from this "
                        "file.]")
        else:
            data = target.read_bytes()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                if b"\x00" in data[:8192]:
                    return (f"[read_file: {rel} is a binary file "
                            f"({suffix.lstrip('.') or 'no extension'}, "
                            f"{size} bytes) with no readable text. "
                            "copy_file can duplicate it byte for "
                            "byte, but its contents cannot be read "
                            "as text.]")
                text = data.decode("utf-8", errors="replace")
        # Audit B18: page by lines. Defaults reproduce the old
        # whole-file read exactly for any file that fits the
        # window (raw text returned untouched — no re-joining);
        # only a truncating window or a start past line 1 pays
        # for the footer that teaches the model how to continue.
        offset = _paging_int(args.get("offset"))
        offset = 1 if offset is None else max(1, offset)
        limit = _paging_int(args.get("limit"))
        limit = READ_FILE_DEFAULT_LIMIT if limit is None else \
            min(READ_FILE_MAX_LIMIT, max(1, limit))
        lines = text.splitlines()
        total = len(lines)
        if offset == 1 and total <= limit:
            return text
        start = offset - 1
        if start >= total:
            return (f"[read_file: offset {offset} is beyond the "
                    f"file — it has {total} lines]")
        window = lines[start:start + limit]
        end = start + len(window)
        return "\n".join(window) + (
            f"\n[read_file: showing lines {start + 1}–{end} of {total} "
            f"— call read_file with offset={end + 1} to continue]")
    if name == "code_graph":
        # Tier 2 #2 (code graph, light edition): ask the queryable
        # half of the repo map — where a symbol is defined, who
        # uses it, what a file is connected to. Read-only; the
        # graph only ever reads inside the workspace root, and the
        # related target is a path, so it is jailed with _inside
        # like read_file's. No containment or production branch:
        # a read tool judges no writes.
        action = str(args.get("action", "")).strip()
        target = str(args.get("target", "")).strip()
        if action == "related":
            resolved = _inside(root, target)
            target = str(resolved.relative_to(root))
        graph = codegraph_mod.build_graph(root)
        return codegraph_mod.answer(graph, action, target)
    if name == "write_file":
        content = args.get("content", "")
        if not isinstance(content, str):
            # Audit A13: refuse rather than write str(None) == "None"
            # into the file (validate_tool_args is the first line;
            # this is the tool's own, for direct callers).
            raise ToolError(f"write_file: content must be text, "
                            f"not {type(content).__name__}.")
        target = _inside(root, str(args.get("path", "")))
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(target, content)
        return f"Wrote {target.relative_to(root)} ({target.stat().st_size} bytes)."
    if name == "append_file":
        # Item 8 (Note 3): the chunked-write continuation of write_file
        # — same jail, and the same parent behavior (write_file creates
        # missing parents, so append does too; they must never differ).
        chunk = args.get("content", "")
        if not isinstance(chunk, str):
            raise ToolError(f"append_file: content must be text, "
                            f"not {type(chunk).__name__}.")
        target = _inside(root, str(args.get("path", "")))
        existed = target.is_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(chunk)
            # Sweep R133 (append half): an append cannot truncate
            # what is already there, so it keeps its direct write —
            # but the chunk is fsynced before the tool reports
            # success, like the atomic write above.
            fh.flush()
            os.fsync(fh.fileno())
        size = target.stat().st_size
        rel = target.relative_to(root)
        if existed:
            return f"Appended {len(chunk)} chars to {rel} (file now {size} bytes)."
        return f"Created {rel} by append ({len(chunk)} chars; file now {size} bytes)."
    if name == "edit_file":
        # The surgical edit (Write/Research spec, 2026-11-09):
        # targeted find-and-replace. The match discipline is the
        # point — a missing match and a non-unique match are both
        # LOUD refusals with nothing written, so a small model can
        # never silently rewrite the wrong place. replace_all is
        # the explicit escape, never the default.
        old = args.get("old_text")
        new = args.get("new_text")
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            raise ToolError("edit_file: old_text and new_text must be "
                            "text, and old_text must not be empty.")
        target = _inside(root, str(args.get("path", "")))
        if not target.is_file():
            raise ToolError(f"edit_file: file does not exist: {target}"
                            + _correction_hint(
                                root, target, str(args.get("path", "")),
                                want_dir=False))
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ToolError(
                f"edit_file: {args.get('path', '')} is not a UTF-8 "
                "text file — edit_file edits text only.")
        count = text.count(old)
        rel = target.relative_to(root)
        if count == 0:
            raise ToolError(
                f"edit_file: old_text was not found in {rel} — no "
                "edit was made. Read the file and copy the exact "
                "text, including whitespace.")
        if count > 1 and not args.get("replace_all"):
            raise ToolError(
                f"edit_file: old_text occurs {count} times in {rel} "
                "— no edit was made, because which one to change "
                "is ambiguous. Add more surrounding context so it "
                "matches exactly once, or pass replace_all=true "
                "to change every occurrence.")
        if args.get("replace_all"):
            updated = text.replace(old, new)
            made = count
        else:
            updated = text.replace(old, new, 1)
            made = 1
        _atomic_write_text(target, updated)
        return (f"Edited {rel} ({made} replacement"
                f"{'s' if made != 1 else ''}; file now "
                f"{target.stat().st_size} bytes).")
    if name == "copy_file":
        # Byte for byte, any file type (Write/Research spec,
        # 2026-11-09): the content never passes through text, so a
        # binary copies exactly. Both ends are jailed.
        source = _inside(root, str(args.get("source", "")))
        dest = _inside(root, str(args.get("destination", "")))
        if not source.is_file():
            raise ToolError(f"copy_file: source file does not exist: {source}"
                            + _correction_hint(
                                root, source, str(args.get("source", "")),
                                want_dir=False))
        if source == dest:
            raise ToolError("copy_file: source and destination are "
                            "the same file — nothing to copy.")
        if dest.is_dir():
            raise ToolError(f"copy_file: destination is a directory: {dest}")
        overwrote = dest.exists()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        verb = "Overwrote" if overwrote else "Copied"
        return (f"{verb} {source.relative_to(root)} -> "
                f"{dest.relative_to(root)} "
                f"({dest.stat().st_size} bytes).")
    if name == "move_file":
        # A move never overwrites: landing on an existing file
        # would silently destroy it, so it is refused loudly.
        source = _inside(root, str(args.get("source", "")))
        dest = _inside(root, str(args.get("destination", "")))
        if not source.is_file():
            raise ToolError(f"move_file: source file does not exist: {source}"
                            + _correction_hint(
                                root, source, str(args.get("source", "")),
                                want_dir=False))
        if source == dest:
            raise ToolError("move_file: source and destination are "
                            "the same file — nothing to move.")
        if dest.exists():
            raise ToolError(
                f"move_file: destination already exists: "
                f"{dest.relative_to(root)} — a move never "
                "overwrites. Delete or rename the destination "
                "first, or pick another destination.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(dest))
        return (f"Moved {source.relative_to(root)} -> "
                f"{dest.relative_to(root)} "
                f"({dest.stat().st_size} bytes).")
    if name == "delete_file":
        # Files only, and never a silent permanent delete at the
        # WORKER level: the worker lays a forced safety checkpoint
        # (and the turn's pre-edit snapshot, when enabled) before
        # this runs — see AgentWorker._execute. The tool itself
        # just removes the file and says exactly what it removed.
        target = _inside(root, str(args.get("path", "")))
        if not target.exists():
            raise ToolError(f"delete_file: file does not exist: {target}"
                            + _correction_hint(
                                root, target, str(args.get("path", "")),
                                want_dir=False))
        if target.is_dir():
            raise ToolError(
                f"delete_file: {args.get('path', '')} is a "
                "directory — delete_file removes files only.")
        size = target.stat().st_size
        rel = target.relative_to(root)
        target.unlink()
        return f"Deleted {rel} ({size} bytes)."
    if name == "run_command":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ToolError("Empty command.")
        # Sweep R132: _timeout is a model-reachable argument the
        # schema never declares — an unparseable value used to crash
        # execute_tool with a raw ValueError, a negative one killed
        # every command instantly, and a huge one defeated the 1800s
        # runaway guard entirely. Parse it like the bench does and
        # clamp it into [1, COMMAND_TIMEOUT].
        try:
            timeout = int(args.get("_timeout", COMMAND_TIMEOUT))
        except (TypeError, ValueError):
            raise ToolError("run_command: _timeout must be a whole "
                            "number of seconds.") from None
        timeout = max(1, min(timeout, COMMAND_TIMEOUT))
        # Contained run: decide the cage BEFORE anything spawns. The
        # Docker cage (when requested and available) is the sandbox —
        # the container only ever sees the project folder, so the
        # command text needs no scan. Every other contained run is
        # local: the command text is checked against the root first,
        # and a Docker request that cannot be honored degrades to
        # that local run with one plain note line on the result.
        prefix_note = ""
        docker_argv = None
        if containment is not None:
            if containment.docker and _docker_available():
                docker_argv = _docker_argv(containment.root,
                                           containment.docker_image,
                                           command)
            else:
                if containment.docker:
                    prefix_note = ("Contained run: Docker was requested "
                                   "but Docker is not available on this "
                                   "PC — the command ran locally inside "
                                   "the project folder instead.\n")
                refusal = contained_command_refusal(containment.root,
                                                    command)
                if refusal is not None:
                    raise ToolError(refusal)
        # Secrets vault (GUIFX V1): when this approved command uses
        # sudo and the owner has stored his PC password, the LOCAL
        # run below feeds it to `sudo -S` on stdin behind the scenes.
        # The password is never part of the command text, the result,
        # or any message — and the Docker cage path never gets it.
        fed_password = None
        run_text = command
        if command_uses_sudo(command):
            stored_pw = _vault_secret("sudo_password")
            if stored_pw:
                run_text = sudo_feed_plan(command)
                fed_password = stored_pw
        # Audit B23: stdout/stderr spool to temporary FILES, not pipes —
        # communicate() used to buffer the entire output in RAM before
        # any slice applied. Own process group: on stop/ceiling/cap we
        # kill the group, so children the command spawned die with it
        # instead of running on in background.
        spool_out = tempfile.TemporaryFile()
        spool_err = tempfile.TemporaryFile()
        try:
            proc = None
            if docker_argv is not None:
                try:
                    proc = subprocess.Popen(
                        docker_argv, shell=False, cwd=root,
                        stdout=spool_out, stderr=spool_err,
                        start_new_session=True)
                except OSError:
                    # The cage failed to even start: fall back to the
                    # local contained run — which means the command
                    # text gets the local scan it skipped above.
                    docker_argv = None
                    prefix_note = ("Contained run: Docker failed to "
                                   "start — the command ran locally "
                                   "inside the project folder instead.\n")
                    refusal = contained_command_refusal(containment.root,
                                                        command)
                    if refusal is not None:
                        raise ToolError(refusal)
            if proc is None:
                proc = subprocess.Popen(
                    run_text, shell=True, cwd=root,
                    stdin=subprocess.PIPE if fed_password else None,
                    stdout=spool_out, stderr=spool_err,
                    start_new_session=True)
                if fed_password:
                    # One line on stdin for `sudo -S`, then EOF.  A
                    # broken pipe just means the command never read
                    # it — the run itself is unaffected.
                    try:
                        proc.stdin.write(
                            (fed_password + "\n").encode("utf-8"))
                        proc.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
            # Poll in short slices: the command runs to completion (no work
            # limit), but the owner's Stop (cancel_event), the runaway
            # ceiling, and the output cap all get a chance to end it —
            # and say so honestly.
            started = time.monotonic()
            stopped = hit_ceiling = over_cap = False
            while proc.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    stopped = True
                    break
                if time.monotonic() - started >= timeout:
                    hit_ceiling = True
                    break
                if (os.fstat(spool_out.fileno()).st_size
                        + os.fstat(spool_err.fileno()).st_size
                        > COMMAND_OUTPUT_CAP):
                    over_cap = True
                    break
                time.sleep(0.05)
            if stopped or hit_ceiling or over_cap:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            output, total, truncated = _combined_spool_output(spool_out, spool_err)
            if fed_password:
                # Belt and braces (the same scrub providers.py applies
                # to API keys): if the password somehow echoes into
                # the output, it never reaches the result the model
                # sees, the eventlog, or any pane.
                output = output.replace(fed_password, "[redacted]")
            note = (f"\n[output truncated — the command produced about {total} "
                    f"characters; the first 12,000 are shown]"
                    if truncated else "")
            if stopped or hit_ceiling or over_cap:
                elapsed = round(time.monotonic() - started)
                if stopped:
                    head = (f"Command stopped by the owner after {elapsed}s and killed "
                            f"(entire process group — nothing left running in the background).")
                elif hit_ceiling:
                    head = (f"Command hit the runaway ceiling of {timeout}s and was killed "
                            f"(entire process group — nothing left running in the background). "
                            f"The ceiling is a runaway guard, not a work limit; the command "
                            f"was still running when it hit it.")
                else:
                    head = ("Command produced more than the output cap of 32 MB "
                            "and was killed (entire process group — nothing left "
                            "running in the background).")
                return prefix_note + head + (
                    f"\nPartial output before it was killed:\n{output[:12000]}"
                    if output else "") + note
            return prefix_note + f"Exit code: {proc.returncode}\n{output[:12000]}" + note
        finally:
            spool_out.close()
            spool_err.close()
    raise ToolError(f"Unsupported tool: {name}")
