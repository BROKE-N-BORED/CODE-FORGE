"""Agent-driven browser verification — Item #65.

Item #55's Browse connector spec named this its "first job": the
agent looks at its OWN work in the browser and iterates, instead
of declaring a page done from the source alone. verify_in_browser
is a Forge-side composite over the three observation verbs (open /
read / screenshot): it adds no server surface of its own and rides
the same fenced dispatch (plugins/browse/plugin.py's _call) as
every verb, so the wall hold, the tool map, and read's text pin
apply unchanged. This module is the orchestration half (no Qt);
the plugin file holds the thin handler shell, mirroring the
browse.py / plugin.py split.

TARGETS — two shapes, two fence postures:

* A project-relative path ("index.html", "site/page.html") is
  served from a THROWAWAY preview: a stdlib HTTP server bound to
  127.0.0.1 only, rooted at the workspace, alive only for the
  length of this call (shut down on every exit path). Its origin
  is the ONE navigation exempt from the project's site allowlist —
  safe by construction: the server exists only during the call,
  serves only this project's files, and listens only on loopback.
  A path that resolves outside the workspace is refused
  (traversal), as is a path that does not exist.
* An http(s) URL gets NO exception: the project's site allowlist
  (.forge/browse.json) decides, exactly as for browse_open, and
  the refusal lands before the Browse server is ever called.

RESULTS — text, always. The result carries browse_read's text
snapshot and a note naming the screenshot saved under the
project's .forge/artifacts/. The screenshot itself is NEVER
attached as image data, for any model: plugin handlers return
text by contract (forge/plugins.py), the worker appends tool
results as text messages, and every provider converter reads
images only off USER messages (providers' Groq/Gemini converters
render tool results as bare strings; ollama_client passes message
dicts through, but chat templates render images from user turns).
There is no tool-result image channel to attach through — and
inventing one by parking an "images" key on a stored tool message
would re-create the #71 poison: a stored image re-sends with the
history on every later turn. So the verify loop's eyes are its
text snapshot; the screenshot is for the OWNER — saved to disk
and named in the result, whatever the driving model can see.
Every run, however it ends, also files a verification RECORDING
artifact — the step log + the screenshots taken, bundled as one
entry (forge/recordings.py; browser merge, 2026-11-11).
"""
from __future__ import annotations
import base64
import binascii
import functools
import http.server
import re
import threading
import urllib.parse
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import browse as browse_mod
from . import projects as proj
from . import recordings as recordings_mod

_IMAGE_MAGICS = ((b"\x89PNG\r\n\x1a\n", "png"),
                 (b"\xff\xd8\xff", "jpg"),
                 (b"GIF87a", "gif"), (b"GIF89a", "gif"))


# ---------- targets ----------

def classify_target(target, workspace) -> tuple:
    """Sort a verify target into ("local", Path) / ("url", str) /
    ("error", refusal). Local paths are judged on their RESOLVED
    path, so a ".." detour out of the project is a traversal
    refusal, however it is spelled."""
    text = str(target or "").strip()
    if not text:
        return ("error",
                "verify_in_browser needs a target: a project-relative "
                "path to a page in this project (e.g. 'index.html'), "
                "or an http(s) URL on the project's Browse allowlist.")
    scheme = urllib.parse.urlparse(text).scheme
    if scheme:
        if scheme in ("http", "https"):
            return ("url", text)
        return ("error",
                f"Verify refused: '{text}' is not an http/https URL. "
                "Only http/https pages and files inside this project "
                "can be verified.")
    root = Path(workspace).resolve()
    candidate = (root / text).resolve()
    if not candidate.is_relative_to(root):
        return ("error",
                f"Verify refused: '{text}' points outside this "
                "project. The preview serves only files inside the "
                "project folder.")
    if not candidate.exists():
        return ("error",
                f"Verify refused: there is no '{text}' in this "
                "project. Give a project-relative path to a page "
                "that exists, or an http(s) URL on the project's "
                "Browse allowlist.")
    return ("local", candidate)


# ---------- the throwaway preview ----------

class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # the preview never logs
        pass


@contextmanager
def preview_server(workspace):
    """Serve the workspace on 127.0.0.1 for the length of one
    verify call; yields the base URL. Loopback only, this project
    only, and shut down on EVERY exit — the allowlist exception in
    the module docstring is exactly as wide as this block."""
    root = str(Path(workspace).resolve())
    handler = functools.partial(_QuietHandler, directory=root)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------- the screenshot ----------

def extract_image_bytes(text) -> tuple | None:
    """(bytes, extension) when a screenshot reply IS image data —
    a data URI, or bare base64 decoding to bytes with a known
    image magic — else None. The magic check is the judge: text
    that merely looks like base64 does not become a file."""
    body = str(text or "").strip()
    candidates = []
    if body.lower().startswith("data:image/") and ";base64," in body:
        candidates.append(body.split(",", 1)[1])
    candidates.append(body)
    for cand in candidates:
        cleaned = re.sub(r"\s+", "", cand)
        if len(cleaned) < 16:
            continue
        try:
            raw = base64.b64decode(cleaned, validate=True)
        except (binascii.Error, ValueError):
            continue
        for magic, ext in _IMAGE_MAGICS:
            if raw.startswith(magic):
                return raw, ext
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return raw, "webp"
    return None


def save_screenshot(workspace, shot_text) -> tuple | None:
    """File one screenshot reply under .forge/artifacts/ and
    return (file name, "image" | "text") — or None when there is
    nothing to save (an empty reply, or an MCP error the caller
    reports instead). A reply that is not image data is saved AS
    RETURNED, as text: the file says what the server said, never
    a pretense of pixels."""
    body = str(shot_text or "")
    if not body.strip() or body.startswith("MCP tool error"):
        return None
    folder = proj.forge_area(workspace) / "artifacts"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    found = extract_image_bytes(body)
    if found:
        raw, ext = found
        path = folder / f"verify-{stamp}.{ext}"
        counter = 1
        while path.exists():
            counter += 1
            path = folder / f"verify-{stamp}-{counter}.{ext}"
        path.write_bytes(raw)
        return path.name, "image"
    path = folder / f"verify-{stamp}.txt"
    counter = 1
    while path.exists():
        counter += 1
        path = folder / f"verify-{stamp}-{counter}.txt"
    path.write_text(body, encoding="utf-8")
    return path.name, "text"


# ---------- the loop ----------

def run_verify(target, workspace, call) -> str:
    """Drive one verify loop and compose its text result. `call`
    is the Browse plugin's fenced verb dispatch,
    call(verb_key, args) -> str — hold check, tool map, and wall
    scan on every call — so verify composes the verbs, it never
    replaces their fences.

    Browser merge (2026-11-11): every run — verified, failed,
    held, or refused before it started — also files a RECORDING
    artifact (forge/recordings.py): the step log of what the loop
    did plus the screenshots it saved, bundled as one artifact
    entry. The composed result text is unchanged by the recording;
    filing never breaks the loop."""
    steps = [("Verify requested", f"target: {target}")]
    shots: list[str] = []
    engine = "Browse connector (#65 verify loop)"
    kind, value = classify_target(target, workspace)
    if kind == "error":
        steps.append(("Target check", "refused before any browsing"))
        recordings_mod.file_recording(
            workspace, str(target), steps, shots, "refused",
            engine_label=engine)
        return value
    if kind == "url":
        # NO exception for external pages: the owner's allowlist
        # decides before the server is touched, as for browse_open.
        refusal = browse_mod.check_open(
            value, browse_mod.load_allowlist(workspace))
        if refusal:
            steps.append(("Site allowlist",
                          "refused — the host is not on the "
                          "project's Browse allowlist"))
            recordings_mod.file_recording(
                workspace, str(target), steps, shots, "refused",
                engine_label=engine)
            return refusal
        result, outcome = _verify_at(value, value, workspace, call,
                                     steps, shots)
        recordings_mod.file_recording(
            workspace, str(target), steps, shots, outcome,
            engine_label=engine)
        return result
    root = Path(workspace).resolve()
    rel = value.relative_to(root).as_posix()
    with preview_server(workspace) as base:
        url = base + "/" + urllib.parse.quote(rel)
        result, outcome = _verify_at(
            url, f"{rel} (local preview)", workspace, call,
            steps, shots)
    recordings_mod.file_recording(
        workspace, str(target), steps, shots, outcome,
        engine_label=engine)
    return result


def _verify_at(url, label, workspace, call, steps, shots) -> tuple:
    """The open / read / screenshot loop. Returns (result text,
    outcome) and appends the session's steps + screenshot names
    to the caller's lists, for the recording run_verify files."""
    opened = call("open", {"url": url})
    if opened.startswith("MCP tool error"):
        steps.append(("Open the page",
                      f"failed — {opened.splitlines()[0][:200]}"))
        return (f"Verify failed — the browser could not open "
                f"{label}: {opened}"), "failed"
    if browse_mod.get_hold(workspace) is not None:
        # The open hit a wall: the dispatch already set the hold
        # and appended the forced-handoff notice to its result.
        # That text IS the verify result — the loop stops here.
        steps.append(("Open the page",
                      "a wall held the session — the tab is with "
                      "the owner"))
        return opened, "held for the owner"
    steps.append(("Open the page", f"opened {label}"))
    text = call("read", {})
    if text.startswith("MCP tool error"):
        steps.append(("Read the page",
                      f"failed — {text.splitlines()[0][:200]}"))
        return (f"Verify failed — the page opened ({label}) but "
                f"its text could not be read: {text}"), "failed"
    if browse_mod.get_hold(workspace) is not None:
        steps.append(("Read the page",
                      "a wall held the session — the tab is with "
                      "the owner"))
        return text, "held for the owner"
    steps.append(("Read the page", "text snapshot taken"))
    shot = call("screenshot", {})
    lines = [f"Verified in the browser: {label}",
             "",
             "Page text (browse_read's text snapshot):",
             text,
             ""]
    saved = save_screenshot(workspace, shot)
    if saved is not None:
        name, kind = saved
        shots.append(name)
        if kind == "image":
            steps.append(("Screenshot", f"saved as {name}"))
            lines.append(
                f"Screenshot saved to .forge/artifacts/{name} — "
                "the owner can open it from the project folder. "
                "(Verify results are text: the screenshot is "
                "never attached to this conversation as image "
                "data, whatever model is driving.)")
        else:
            steps.append(("Screenshot",
                          "the server's reply was not image data; "
                          f"saved as returned ({name})"))
            lines.append(
                "The Browse server's screenshot reply was not "
                f"image data; it was saved as returned to "
                f".forge/artifacts/{name}.")
    elif shot.startswith("MCP tool error"):
        steps.append(("Screenshot",
                      f"failed — {shot.splitlines()[0][:200]}"))
        lines.append(f"Screenshot failed: {shot.splitlines()[0]}")
    else:
        steps.append(("Screenshot",
                      "the server returned nothing to save"))
        lines.append("The Browse server returned no screenshot "
                     "to save.")
    return "\n".join(lines), "verified"
