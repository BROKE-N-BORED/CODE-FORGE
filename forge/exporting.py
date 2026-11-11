"""Chat transcript export — PDF / Markdown / HTML, images included.

Owner's order (2026-11-11): export chat transcripts WITH their images
as files he can carry into other AIs or keep as references on his PC.

Where the images live (audit, 2026-11-11): a live conversation's
messages carry "images" as a list of base64 strings (the Ollama
payload shape — see ForgeWindow's attachment intake). The SQLite
conversation store intentionally strips image payloads when it saves
("Image payloads are intentionally excluded from persistent history
to keep the database small" — forge/storage.py), so a stored
conversation usually has NO images to export; the open conversation
exports everything it holds. Either way this module takes a plain
message list and renders whatever images it actually carries — an
image that cannot be decoded or found renders a plain
"[image not found]" line, never a crash and never a silent drop.

Pure shell machinery: no model calls, fully offline. PDF rendering
is Qt (QTextDocument + QPdfWriter) — real selectable text, not
screenshots.
"""
import base64
import binascii
import html
import json
import os
import time
from pathlib import Path

PLACEHOLDER_MISSING = "[image not found]"
PLACEHOLDER_OMITTED = "[image not included]"

_SPEAKERS = {"user": "You", "assistant": "Forge",
             "system": "System", "tool": "Tool"}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}


def _image_ext(data: bytes, fallback=".png"):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:2] == b"BM":
        return ".bmp"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return fallback


class ImageRef:
    """One image riding a message: base64 data or a file path.
    load_bytes() returns the image bytes, or None when the image
    cannot be decoded or found (the placeholder case)."""

    def __init__(self, data_b64=None, path=None):
        self.data_b64 = data_b64
        self.path = path

    def load_bytes(self):
        if self.data_b64:
            try:
                return base64.b64decode(self.data_b64, validate=False)
            except (binascii.Error, ValueError):
                return None
        if self.path:
            try:
                return Path(self.path).read_bytes()
            except OSError:
                return None
        return None

    def fallback_ext(self):
        if self.path:
            suffix = Path(self.path).suffix.lower()
            if suffix in _IMAGE_EXTS:
                return suffix
        return ".png"


def _message_images(message):
    """Every ImageRef riding one message, in order. Accepts the
    live base64 shape (message["images"] = [b64, ...]), dict entries
    ({"data": b64} / {"path": p}), an "image_paths" list, and
    attachment dicts of kind "image"."""
    refs = []
    raw = message.get("images")
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str):
                candidate = entry.strip()
                if not candidate:
                    continue
                # A short string naming an existing image file is a
                # path; anything else is treated as base64 payload.
                if len(candidate) < 4096 and \
                        Path(candidate).suffix.lower() in _IMAGE_EXTS \
                        and Path(candidate).is_file():
                    refs.append(ImageRef(path=candidate))
                else:
                    refs.append(ImageRef(data_b64=candidate))
            elif isinstance(entry, dict):
                if entry.get("data"):
                    refs.append(ImageRef(data_b64=str(entry["data"])))
                elif entry.get("path"):
                    refs.append(ImageRef(path=str(entry["path"])))
    paths = message.get("image_paths")
    if isinstance(paths, list):
        for entry in paths:
            if isinstance(entry, str) and entry:
                refs.append(ImageRef(path=entry))
    attachments = message.get("attachments")
    if isinstance(attachments, list):
        for entry in attachments:
            if isinstance(entry, dict) and entry.get("kind") == "image":
                if entry.get("data"):
                    refs.append(ImageRef(data_b64=str(entry["data"])))
                elif entry.get("path"):
                    refs.append(ImageRef(path=str(entry["path"])))
    return refs


def _message_text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, (list, dict)):
        # Ollama content-part lists: keep the text parts in order.
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif isinstance(part, str):
                    parts.append(part)
            if parts:
                return "\n".join(parts)
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def _message_stamp(message):
    for key in ("ts", "timestamp", "time"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and value > 0:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))
    return ""


def iter_turns(messages):
    """Normalize a stored/live message list into export turns:
    {speaker, role, stamp, text, images: [ImageRef]}."""
    turns = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role not in _SPEAKERS:
            continue
        turns.append({
            "speaker": _SPEAKERS[role],
            "role": role,
            "stamp": _message_stamp(message),
            "text": _message_text(message),
            "images": _message_images(message),
        })
    return turns


def _meta_facts(model=None, project=None, updated_at=None):
    facts = []
    if model:
        facts.append(f"Model: {model}")
    if project:
        facts.append(f"Project: {project}")
    if updated_at:
        facts.append(f"Last updated: {updated_at}")
    return facts


def render_markdown(title, turns, include_images=True, assets_dir=None,
                    model=None, project=None, updated_at=None):
    """(markdown_text, stats). With include_images and an assets_dir,
    decodable images are written there and linked; without one they
    render as a plain note (there is nowhere honest to put them)."""
    stats = {"embedded": 0, "missing": 0, "omitted": 0}
    lines = [f"# {title or 'Conversation'}"]
    facts = _meta_facts(model, project, updated_at)
    if facts:
        lines += ["", " · ".join(facts)]
    counter = 0
    for turn in turns:
        heading = f"## {turn['speaker']}"
        if turn["stamp"]:
            heading += f" — {turn['stamp']}"
        lines += ["", heading, "", turn["text"]]
        for ref in turn["images"]:
            if not include_images:
                lines += ["", PLACEHOLDER_OMITTED]
                stats["omitted"] += 1
                continue
            data = ref.load_bytes()
            if data is None or assets_dir is None:
                if data is None:
                    stats["missing"] += 1
                    lines += ["", PLACEHOLDER_MISSING]
                else:
                    # Bytes exist but no assets folder was requested
                    # (defensive: the dialog always passes one).
                    stats["missing"] += 1
                    lines += ["", PLACEHOLDER_MISSING]
                continue
            counter += 1
            name = f"image-{counter}{_image_ext(data, ref.fallback_ext())}"
            Path(assets_dir, name).write_bytes(data)
            lines += ["", f"![image {counter}]({Path(assets_dir).name}/{name})"]
            stats["embedded"] += 1
    lines.append("")
    return "\n".join(lines), stats


def _turns_html(title, turns, include_images, image_src):
    """Shared HTML body. image_src(ref, index, data) -> src string
    or None for the placeholder."""
    stats = {"embedded": 0, "missing": 0, "omitted": 0}
    parts = [f"<h1>{html.escape(title or 'Conversation')}</h1>"]
    counter = 0
    for turn in turns:
        heading = html.escape(turn["speaker"])
        if turn["stamp"]:
            heading += f" — {html.escape(turn['stamp'])}"
        parts.append(f"<h2>{heading}</h2>")
        body = html.escape(turn["text"])
        parts.append("<p>" + body.replace("\n", "<br>") + "</p>")
        for ref in turn["images"]:
            if not include_images:
                parts.append(f"<p>{PLACEHOLDER_OMITTED}</p>")
                stats["omitted"] += 1
                continue
            data = ref.load_bytes()
            src = image_src(ref, counter + 1, data) if data is not None else None
            if src is None:
                parts.append(f"<p>{PLACEHOLDER_MISSING}</p>")
                stats["missing"] += 1
            else:
                counter += 1
                parts.append(f'<p><img src="{src}"></p>')
                stats["embedded"] += 1
    return "\n".join(parts), stats


_HTML_SHELL = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: sans-serif; margin: 2em auto; max-width: 46em; padding: 0 1em; }}
h1 {{ font-size: 1.5em; }} h2 {{ font-size: 1.1em; margin-top: 1.6em; }}
img {{ max-width: 100%; }}
.meta {{ color: #555; }}
</style></head><body>
{meta}
{body}
</body></html>
"""


def render_html(title, turns, include_images=True,
                model=None, project=None, updated_at=None):
    """(html_text, stats) — standalone HTML, images as data URIs."""
    def data_src(ref, _index, data):
        mime = {".png": "image/png", ".jpg": "image/jpeg",
                ".gif": "image/gif", ".bmp": "image/bmp",
                ".webp": "image/webp"}[_image_ext(data, ".png")]
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    body, stats = _turns_html(title, turns, include_images, data_src)
    facts = _meta_facts(model, project, updated_at)
    meta = (f'<p class="meta">{html.escape(" · ".join(facts))}</p>'
            if facts else "")
    return _HTML_SHELL.format(title=html.escape(title or "Conversation"),
                              meta=meta, body=body), stats


def write_qtextdocument_pdf(document, out_path):
    """Render a prepared QTextDocument to a real-text PDF at
    out_path via QPdfWriter. Returns the byte size written."""
    from PySide6.QtCore import QMarginsF, QSizeF
    from PySide6.QtGui import QPainter, QPageSize, QPdfWriter

    writer = QPdfWriter(str(out_path))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setResolution(96)
    writer.setPageMargins(QMarginsF(15, 15, 15, 15))
    page_size = QSizeF(writer.width(), writer.height())
    document.setPageSize(page_size)
    painter = QPainter(writer)
    try:
        for page in range(document.pageCount()):
            if page:
                writer.newPage()
            painter.save()
            painter.translate(0, -page * page_size.height())
            document.drawContents(painter)
            painter.restore()
    finally:
        painter.end()
    return Path(out_path).stat().st_size


def write_transcript_pdf(out_path, title, turns, include_images=True,
                         model=None, project=None, updated_at=None):
    """Render the transcript straight to PDF. Images are added as
    QTextDocument resources (guaranteed embedding, no file deps).
    Returns stats including the byte size."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QImage, QTextDocument

    resources = {}

    def resource_src(ref, index, data):
        image = QImage()
        if not image.loadFromData(data):
            return None
        if image.width() > 520:
            image = image.scaledToWidth(520)
        name = f"img{index}"
        resources[name] = image
        return name

    body, stats = _turns_html(title, turns, include_images, resource_src)
    facts = _meta_facts(model, project, updated_at)
    meta = (f'<p class="meta">{html.escape(" · ".join(facts))}</p>'
            if facts else "")
    document = QTextDocument()
    document.setHtml(_HTML_SHELL.format(
        title=html.escape(title or "Conversation"), meta=meta, body=body))
    for name, image in resources.items():
        document.addResource(QTextDocument.ResourceType.ImageResource,
                             QUrl(name), image)
    stats["size"] = write_qtextdocument_pdf(document, out_path)
    return stats


def _atomic_write(out_path, write_fn):
    """Run write_fn(temp_path) beside the destination, then move.
    A failure leaves no partial file at the destination."""
    out_path = Path(out_path)
    tmp = out_path.with_name(out_path.name + f".tmp-{os.getpid()}")
    try:
        result = write_fn(tmp)
        os.replace(tmp, out_path)
        return result
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def export_transcript(messages, fmt, out_path, include_images=True,
                      title="Conversation", model=None, project=None,
                      updated_at=None):
    """Export one conversation. fmt is "pdf" | "md" | "html".
    Returns {path, size, embedded, missing, omitted}."""
    fmt = str(fmt).lower().lstrip(".")
    turns = iter_turns(messages)
    out_path = Path(out_path)
    if fmt == "md":
        assets_dir = None
        if include_images and any(t["images"] for t in turns):
            assets_dir = out_path.with_name(out_path.stem + "_assets")
            assets_dir.mkdir(parents=True, exist_ok=True)
        holder = {}

        def _write(tmp):
            text, stats = render_markdown(
                title, turns, include_images, assets_dir,
                model, project, updated_at)
            tmp.write_text(text, encoding="utf-8")
            holder.update(stats)

        _atomic_write(out_path, _write)
        holder.update({"path": str(out_path),
                       "size": out_path.stat().st_size})
        return holder
    if fmt == "html":
        holder = {}

        def _write(tmp):
            text, stats = render_html(title, turns, include_images,
                                      model, project, updated_at)
            tmp.write_text(text, encoding="utf-8")
            holder.update(stats)

        _atomic_write(out_path, _write)
        holder.update({"path": str(out_path),
                       "size": out_path.stat().st_size})
        return holder
    if fmt == "pdf":
        holder = {}

        def _write(tmp):
            stats = write_transcript_pdf(tmp, title, turns, include_images,
                                         model, project, updated_at)
            holder.update(stats)

        _atomic_write(out_path, _write)
        holder.update({"path": str(out_path),
                       "size": out_path.stat().st_size})
        return holder
    raise ValueError(f"Unknown export format: {fmt}")
