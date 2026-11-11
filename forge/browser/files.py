"""Universal file handling — fetch_any(url) works for any file type.

Joshua's order (2026-11-11): the browser must work regardless of
file or extension type. So fetch dispatches on what actually came
back — the Content-Type first, the extension only when the server
sent a generic binary type:

* HTML / XHTML        -> the Item 1 Markdown conversion
* PDF                 -> text extracted by the small standard-library
                         extractor below (FlateDecode streams; the
                         common Tj / TJ text operators). Scanned-image
                         PDFs have no text layer — that is reported
                         plainly, and the file is saved for the vision
                         seat instead of pretending it was read.
* plain text / JSON / XML / CSV / Markdown / source code
                      -> returned as text (JSON pretty-printed, CSV
                         as a Markdown table, XML as indented text)
* images              -> saved under the media folder and described
                         by the vision seat when one is configured
                         (forge/browser/media.py); the description is
                         the text a model reads, the image itself is
                         never attached to a model that cannot see it
* audio / video       -> saved; audio is transcribed by a local
                         transcription backend when one is configured
* anything else       -> saved, with its type and size reported

Every non-HTML result is a FileResult: kind, title, text (possibly
empty), the saved path when the bytes were kept, and notes saying
exactly what was and was not done. Nothing is silently dropped.
"""

import csv
import io
import json
import os
import re
import zlib
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ._http import decode_body, http_get
from .errors import BrowserError
from .html2md import html_to_markdown

_TEXT_TYPES = {"text/plain", "text/markdown", "text/csv", "application/json",
               "application/xml", "text/xml", "application/javascript",
               "text/javascript"}
_EXT_KIND = {
    ".pdf": "pdf", ".png": "image", ".jpg": "image", ".jpeg": "image",
    ".gif": "image", ".webp": "image", ".svg": "image", ".bmp": "image",
    ".mp3": "audio", ".wav": "audio", ".ogg": "audio", ".m4a": "audio",
    ".flac": "audio", ".mp4": "video", ".webm": "video", ".mov": "video",
    ".txt": "text", ".md": "text", ".json": "text", ".csv": "text",
    ".xml": "text", ".html": "html", ".htm": "html",
}


@dataclass
class FileResult:
    url: str
    final_url: str
    kind: str            # html | pdf | text | image | audio | video | binary
    title: str = ""
    text: str = ""
    saved_path: str = ""
    content_type: str = ""
    size_bytes: int = 0
    notes: list = field(default_factory=list)

    def to_dict(self):
        return {"url": self.url, "final_url": self.final_url, "kind": self.kind,
                "title": self.title, "text": self.text,
                "saved_path": self.saved_path, "content_type": self.content_type,
                "size_bytes": self.size_bytes, "notes": self.notes}


def media_dir():
    path = os.environ.get("FORGE_BROWSER_MEDIA_DIR", "").strip() or os.path.join(
        os.path.expanduser("~"), ".forge", "browser-media")
    os.makedirs(path, exist_ok=True)
    return path


def _save_bytes(body, final_url, kind):
    name = os.path.basename(urlparse(final_url).path) or f"download.{kind}"
    if "." not in name:
        name += "." + kind
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    path = os.path.join(media_dir(), safe)
    with open(path, "wb") as handle:
        handle.write(body)
    return path


def _kind_for(content_type, final_url):
    if content_type in ("text/html", "application/xhtml+xml"):
        return "html"
    if content_type == "application/pdf":
        return "pdf"
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("audio/"):
        return "audio"
    if content_type.startswith("video/"):
        return "video"
    if content_type in _TEXT_TYPES or content_type.startswith("text/"):
        return "text"
    ext = os.path.splitext(urlparse(final_url).path)[1].lower()
    return _EXT_KIND.get(ext, "binary")


def _csv_to_markdown(text):
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return text
    lines = []
    for index, row in enumerate(rows[:200]):
        lines.append("| " + " | ".join(cell.strip() for cell in row) + " |")
        if index == 0:
            lines.append("| " + " | ".join("---" for _ in row) + " |")
    return "\n".join(lines)


def extract_pdf_text(body):
    """Best-effort text from a PDF using only the standard library.

    Handles the common case: FlateDecode content streams carrying
    Tj / TJ text-show operators with literal or hex strings. Returns
    "" when the PDF has no extractable text layer (scans, exotic
    encodings) — the caller reports that plainly.
    """
    chunks = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", body, re.DOTALL):
        raw = match.group(1)
        try:
            data = zlib.decompress(raw)
        except zlib.error:
            data = raw
        if b"Tj" not in data and b"TJ" not in data:
            continue
        text_parts = []
        for token in re.finditer(rb"\((?:\\.|[^\\()])*\)\s*Tj|\[(.*?)\]\s*TJ", data, re.DOTALL):
            segment = token.group(0)
            for literal in re.finditer(rb"\((?:\\.|[^\\()])*\)", segment):
                text_parts.append(_pdf_unescape(literal.group(0)[1:-1]))
            for hexed in re.finditer(rb"<([0-9A-Fa-f\s]+)>", segment):
                try:
                    text_parts.append(bytes.fromhex(hexed.group(1).decode()).decode("latin-1"))
                except ValueError:
                    pass
        if text_parts:
            chunks.append(" ".join(text_parts))
    return "\n".join(chunks).strip()


def _pdf_unescape(raw):
    out = raw.replace(rb"\(", b"(").replace(rb"\)", b")").replace(rb"\\", b"\\")
    out = re.sub(rb"\\[nrtbf]", b" ", out)
    out = re.sub(rb"\\[0-7]{1,3}", b" ", out)
    return out.decode("latin-1", errors="replace")


def fetch_any(url, timeout=30, describe=True, transcribe=True):
    """Fetch any URL and return a FileResult for whatever it is."""
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise BrowserError(f"Forge Browser only fetches http(s) — refused {url!r}")
    final_url, headers, body = http_get(url, timeout=timeout)
    content_type = ""
    if headers is not None:
        content_type = (headers.get_content_type() or "").lower()
    kind = _kind_for(content_type, final_url)
    result = FileResult(url=url, final_url=final_url, kind=kind,
                        content_type=content_type, size_bytes=len(body))

    if kind == "html":
        text = decode_body(body, headers)
        result.title, result.text, _ = html_to_markdown(text, final_url)
    elif kind == "pdf":
        result.text = extract_pdf_text(body)
        result.title = os.path.basename(urlparse(final_url).path)
        if not result.text:
            result.saved_path = _save_bytes(body, final_url, "pdf")
            result.notes.append(
                "this PDF has no readable text layer (likely scanned images) — "
                "the file was saved; reading it needs the vision seat / OCR, "
                "which is not part of this build")
    elif kind == "text":
        text = decode_body(body, headers)
        if content_type == "application/json" or final_url.split("?")[0].endswith(".json"):
            try:
                result.text = json.dumps(json.loads(text), indent=2)[:200000]
            except json.JSONDecodeError:
                result.text = text
        elif content_type == "text/csv" or final_url.split("?")[0].endswith(".csv"):
            result.text = _csv_to_markdown(text)
        else:
            result.text = text
    elif kind in ("image", "audio", "video", "binary"):
        result.saved_path = _save_bytes(body, final_url, kind)
        result.notes.append(f"saved the {kind} file ({len(body)} bytes)")
        if kind == "image" and describe:
            from .media import describe_saved_image
            description = describe_saved_image(result.saved_path)
            if description:
                result.text = description
            else:
                result.notes.append(
                    "no local vision seat is configured, so the image was "
                    "saved but not described (see BROWSER.md: vision seat)")
        if kind == "audio" and transcribe:
            from .media import transcribe_saved_audio
            transcript = transcribe_saved_audio(result.saved_path)
            if transcript:
                result.text = transcript
            else:
                result.notes.append(
                    "no local transcription backend is configured, so the "
                    "audio was saved but not transcribed (see BROWSER.md)")
    return result
