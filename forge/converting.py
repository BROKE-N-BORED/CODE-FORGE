"""File converter — only the conversions Forge can really do.

Owner's order (2026-11-11): "a way to convert files to any format".
The honest version: the converter offers exactly the conversions
this tree can perform, computed per file, and unsupported pairs are
never listed. Everything is local shell machinery — no model calls:

* PDF / DOCX / ODT -> text / Markdown
  (forge.tools.extract_document_text — stdlib extraction; a
  scanned-image PDF with no text layer fails in plain words)
* Markdown -> PDF / HTML, text -> PDF / Markdown / HTML,
  HTML -> PDF / text / Markdown (Qt QTextDocument rendering,
  forge.exporting's QPdfWriter path; browser html2md for HTML->MD)
* JSON -> CSV and CSV -> JSON (stdlib; JSON->CSV needs a list of
  objects or a flat object, and says so when the shape doesn't fit)
* images PNG / JPG / BMP / WebP in any direction Qt can actually
  read AND write — probed at runtime from QImageReader/QImageWriter,
  never assumed
* audio -> text ONLY when a transcription backend is really
  available (the browser media seat's chain: FORGE_TRANSCRIBE_CMD,
  a whisper binary, or faster-whisper importable)
* audio -> audio, video -> video, video -> audio (extract the
  track), video -> GIF — via ffmpeg WHEN IT IS ON THE MACHINE
  (owner's clarification, 2026-11-11: no artificial restrictions;
  conversions stay within what the detected toolbox can really
  do). The supported containers come from probing
  `ffmpeg -formats` at runtime, never from a hardcoded promise.
  When ffmpeg is absent the families are not silently hidden:
  ffmpeg_status() gives the converter window a plain note with
  the Mint install line, and the other families keep working.
* PDF text extraction falls back to a ToUnicode-aware reader
  (below) for PDFs whose text is glyph-encoded — Qt-produced
  PDFs (including Forge's own transcript exports) encode text as
  font glyph ids with ToUnicode maps, which the literal-string
  extractor in forge.tools cannot read.

A failed conversion reports in plain words and leaves no partial
file (write to temp, move on success).
"""
import csv
import html
import io
import json
import os
import re
import shutil
import subprocess
import zlib
from pathlib import Path

from . import exporting
from .tools import extract_document_text


class ConversionError(Exception):
    """A conversion that cannot be done, in plain words."""


_DOC_EXTS = {".pdf", ".docx", ".odt"}
_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus"}
_VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov"}
_IMAGE_CANDIDATES = {".png": "png", ".jpg": "jpg", ".jpeg": "jpg",
                     ".bmp": "bmp", ".webp": "webp"}

# Container extension -> the ffmpeg muxer names that produce it
# (first one the runtime probe finds wins).
_AUDIO_MUXERS = {"wav": ("wav",), "mp3": ("mp3",), "flac": ("flac",),
                 "ogg": ("ogg",), "m4a": ("ipod", "mp4", "m4a"),
                 "opus": ("opus", "ogg")}
_VIDEO_MUXERS = {"mp4": ("mp4",), "webm": ("webm",),
                 "mkv": ("matroska",), "mov": ("mov",)}
_GIF_MUXERS = ("gif",)


# ------------------------------------------------------------------
# ffmpeg runtime probing
# ------------------------------------------------------------------

_ffmpeg_muxers_cache = None


def ffmpeg_path():
    return shutil.which("ffmpeg")


def _reset_ffmpeg_cache():
    global _ffmpeg_muxers_cache
    _ffmpeg_muxers_cache = None


def ffmpeg_muxers():
    """The set of container names the machine's ffmpeg can WRITE
    (mux), parsed from `ffmpeg -formats`. Empty set when ffmpeg is
    absent or its listing cannot be read. Cached per process."""
    global _ffmpeg_muxers_cache
    if _ffmpeg_muxers_cache is not None:
        return _ffmpeg_muxers_cache
    muxers = set()
    exe = ffmpeg_path()
    if exe:
        try:
            proc = subprocess.run(
                [exe, "-hide_banner", "-formats"],
                capture_output=True, timeout=60)
            text = proc.stdout.decode("utf-8", errors="replace")
            in_table = False
            for line in text.splitlines():
                if line.strip().startswith("--"):
                    in_table = True
                    continue
                if not in_table:
                    continue
                match = re.match(r"\s*([D ])([E ])\s+(\S+)", line)
                if match and match.group(2) == "E":
                    muxers.update(name.strip()
                                  for name in match.group(3).split(","))
        except Exception:
            muxers = set()
    _ffmpeg_muxers_cache = muxers
    return muxers


def _muxer_for(ext, table):
    """The first probed muxer that produces container `ext`."""
    available = ffmpeg_muxers()
    for name in table.get(ext, ()):
        if name in available:
            return name
    return None


def ffmpeg_status():
    """(present, plain note) for the converter window. When ffmpeg
    is missing the note says so and gives the Mint install line —
    the media families are unavailable-with-reason, never silently
    hidden."""
    exe = ffmpeg_path()
    if not exe:
        return False, (
            "Audio and video conversion needs ffmpeg, which was not "
            "found on this machine. Install it with: "
            "sudo apt install ffmpeg — document, image, and data "
            "conversions work without it.")
    return True, f"Audio/video conversions use ffmpeg ({exe})."


def transcription_available():
    """True when the browser media seat's transcription chain has a
    backend on this machine — checked without running anything."""
    if os.environ.get("FORGE_TRANSCRIBE_CMD", "").strip():
        return True
    if shutil.which("whisper") or shutil.which("whisper-cli"):
        return True
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _image_formats():
    """(readable, writable) canonical format names from the local
    Qt build itself."""
    try:
        from PySide6.QtGui import QImageReader, QImageWriter
        read = {bytes(f).decode().lower()
                for f in QImageReader.supportedImageFormats()}
        write = {bytes(f).decode().lower()
                 for f in QImageWriter.supportedImageFormats()}
    except Exception:
        return set(), set()
    canon = {"jpeg": "jpg", "jpg": "jpg", "png": "png",
             "bmp": "bmp", "webp": "webp"}
    readable = {canon[f] for f in read if f in canon}
    writable = {canon[f] for f in write if f in canon}
    return readable, writable


def _target(ext, label=None):
    return {"ext": ext, "label": label or ext.upper()}


def capabilities(path):
    """The conversions `path` can really undergo here, as a list of
    {"ext", "label"} target dicts. Empty = nothing honest to offer."""
    path = Path(path)
    suffix = path.suffix.lower()
    targets = []
    if suffix in _DOC_EXTS:
        targets = [_target("txt", "Text (.txt)"),
                   _target("md", "Markdown (.md)")]
    elif suffix in (".md", ".markdown"):
        targets = [_target("pdf", "PDF (.pdf)"),
                   _target("html", "HTML (.html)")]
    elif suffix == ".txt":
        targets = [_target("pdf", "PDF (.pdf)"),
                   _target("md", "Markdown (.md)"),
                   _target("html", "HTML (.html)")]
    elif suffix in (".html", ".htm"):
        targets = [_target("pdf", "PDF (.pdf)"),
                   _target("txt", "Text (.txt)"),
                   _target("md", "Markdown (.md)")]
    elif suffix == ".json":
        targets = [_target("csv", "CSV (.csv)")]
    elif suffix == ".csv":
        targets = [_target("json", "JSON (.json)")]
    elif suffix in _IMAGE_CANDIDATES:
        readable, writable = _image_formats()
        mine = _IMAGE_CANDIDATES[suffix]
        if mine in readable:
            for fmt in sorted(writable - {mine}):
                targets.append(_target(fmt, f"{fmt.upper()} (.{fmt})"))
    elif suffix in _AUDIO_EXTS:
        mine = suffix.lstrip(".")
        for ext in sorted(_AUDIO_MUXERS):
            if ext != mine and _muxer_for(ext, _AUDIO_MUXERS):
                targets.append(_target(ext, f"{ext.upper()} (.{ext})"))
        if transcription_available():
            targets.append(_target("txt", "Text (.txt) — transcription"))
    elif suffix in _VIDEO_EXTS:
        mine = suffix.lstrip(".")
        for ext in sorted(_VIDEO_MUXERS):
            if ext != mine and _muxer_for(ext, _VIDEO_MUXERS):
                targets.append(_target(ext, f"{ext.upper()} (.{ext})"))
        for ext in sorted(_AUDIO_MUXERS):
            if _muxer_for(ext, _AUDIO_MUXERS):
                targets.append(_target(
                    ext, f"{ext.upper()} (.{ext}) — audio track only"))
        if _muxer_for("gif", {"gif": _GIF_MUXERS}):
            targets.append(_target("gif", "GIF (.gif)"))
    return targets


# ------------------------------------------------------------------
# individual conversions (each writes the finished file to `dest`)
# ------------------------------------------------------------------

def _pdf_text_tounicode(data: bytes):
    """Text from a glyph-encoded PDF (Qt-style): content streams
    show text as <hex> glyph ids per font, and each font carries a
    ToUnicode CMap saying which Unicode char each glyph is. Parses
    the CMaps, the /Fxx -> font -> ToUnicode wiring, and decodes
    the shown glyphs in order. Returns "" when there is nothing
    decodable this way."""
    objects = {}
    for match in re.finditer(rb"(\d+)\s+0\s+obj(.*?)endobj",
                             data, re.DOTALL):
        objects[int(match.group(1))] = match.group(2)

    def stream_of(body):
        found = re.search(rb"stream\r?\n(.*?)endstream", body, re.DOTALL)
        if not found:
            return None
        raw = found.group(1)
        try:
            return zlib.decompress(raw)
        except Exception:
            return raw

    def parse_cmap(stream):
        mapping = {}
        if not stream:
            return mapping
        for block in re.finditer(rb"beginbfchar(.*?)endbfchar",
                                 stream, re.DOTALL):
            for src_hex, dst_hex in re.findall(
                    rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>",
                    block.group(1)):
                try:
                    mapping[int(src_hex, 16)] = bytes.fromhex(
                        dst_hex.decode()).decode("utf-16-be", "replace")
                except Exception:
                    continue
        for block in re.finditer(rb"beginbfrange(.*?)endbfrange",
                                 stream, re.DOTALL):
            body = block.group(1)
            for lo, hi, first in re.findall(
                    rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*"
                    rb"<([0-9A-Fa-f]+)>", body):
                lo_i, hi_i, first_i = int(lo, 16), int(hi, 16), \
                    int(first, 16)
                for code in range(lo_i, min(hi_i, lo_i + 4096) + 1):
                    mapping[code] = chr(first_i + code - lo_i)
            for lo, hi, array in re.findall(
                    rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*"
                    rb"\[(.*?)\]", body, re.DOTALL):
                lo_i = int(lo, 16)
                for offset, dst_hex in enumerate(re.findall(
                        rb"<([0-9A-Fa-f]+)>", array)):
                    try:
                        mapping[lo_i + offset] = bytes.fromhex(
                            dst_hex.decode()).decode(
                                "utf-16-be", "replace")
                    except Exception:
                        continue
        return mapping

    # font object number -> its ToUnicode map
    font_maps = {}
    for number, body in objects.items():
        ref = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", body)
        if ref and int(ref.group(1)) in objects:
            cmap = parse_cmap(stream_of(objects[int(ref.group(1))]))
            if cmap:
                font_maps[number] = cmap
    if not font_maps:
        return ""
    # resource name (/F11) -> font object number
    name_to_font = {}
    for body in objects.values():
        fonts = re.search(rb"/Font\s*<<(.*?)>>", body, re.DOTALL)
        if fonts:
            for name, ref in re.findall(
                    rb"/(\w+)\s+(\d+)\s+0\s+R", fonts.group(1)):
                name_to_font[name.decode()] = int(ref)
    lines = []
    for body in objects.values():
        stream = stream_of(body)
        if not stream or b" Tj" not in stream:
            continue
        current = None
        line = []
        for token in re.finditer(
                rb"/(\w+)\s+[\d.]+\s+Tf|<([0-9A-Fa-f]+)>\s*Tj|"
                rb"\((?:\\.|[^\\()])*\)\s*Tj|\bET\b", stream):
            if token.group(1):
                current = font_maps.get(
                    name_to_font.get(token.group(1).decode()))
            elif token.group(2):
                cmap = current or (next(iter(font_maps.values()))
                                   if len(font_maps) == 1 else None)
                if cmap is None:
                    continue
                hexed = token.group(2)
                for i in range(0, len(hexed) - 3, 4):
                    code = int(hexed[i:i + 4], 16)
                    line.append(cmap.get(code, ""))
            elif token.group(0) == b"ET":
                if line:
                    lines.append("".join(line))
                    line = []
            else:
                # literal-string Tj: the char codes ARE the text
                literal = re.search(rb"\((?:\\.|[^\\()])*\)",
                                    token.group(0))
                if literal:
                    line.append(literal.group(0)[1:-1].decode(
                        "latin-1", "replace"))
        if line:
            lines.append("".join(line))
    text = "\n".join(lines).replace("\t", " ")
    return text.strip()


def _doc_to_text(src):
    data = Path(src).read_bytes()
    text = extract_document_text(Path(src), data)
    if (text is None or not text.strip()) \
            and Path(src).suffix.lower() == ".pdf":
        # Glyph-encoded PDFs (Qt output — including Forge's own
        # exports): read the fonts' ToUnicode maps instead.
        text = _pdf_text_tounicode(data) or None
    if text is None or not text.strip():
        raise ConversionError(
            "No readable text could be extracted from this file. "
            "Scanned-image PDFs and empty documents have no text "
            "layer for the extractor to read.")
    return text


def _read_text(src):
    try:
        return Path(src).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return Path(src).read_text(encoding="utf-8", errors="replace")


def _text_document():
    from PySide6.QtGui import QTextDocument
    return QTextDocument()


def _md_to_html(src, dest):
    document = _text_document()
    document.setMarkdown(_read_text(src))
    body = document.toHtml()
    title = html.escape(Path(src).stem)
    Path(dest).write_text(
        "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
        f"<title>{title}</title></head><body>\n{body}\n</body></html>\n",
        encoding="utf-8")


def _md_to_pdf(src, dest):
    document = _text_document()
    document.setMarkdown(_read_text(src))
    exporting.write_qtextdocument_pdf(document, dest)


def _html_to_pdf(src, dest):
    from PySide6.QtCore import QUrl
    document = _text_document()
    document.setBaseUrl(QUrl.fromLocalFile(str(Path(src).parent) + "/"))
    document.setHtml(_read_text(src))
    exporting.write_qtextdocument_pdf(document, dest)


def _html_to_text(src, dest):
    document = _text_document()
    document.setHtml(_read_text(src))
    Path(dest).write_text(document.toPlainText(), encoding="utf-8")


def _html_to_md(src, dest):
    from .browser.html2md import html_to_markdown
    Path(dest).write_text(html_to_markdown(_read_text(src)),
                          encoding="utf-8")


def _txt_to_md(src, dest):
    text = _read_text(src)
    Path(dest).write_text(f"# {Path(src).stem}\n\n{text}\n",
                          encoding="utf-8")


def _txt_to_html(src, dest):
    text = _read_text(src)
    paragraphs = [p.strip() for p in text.split("\n\n")]
    body = "\n".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>"
                     for p in paragraphs if p)
    title = html.escape(Path(src).stem)
    Path(dest).write_text(
        "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
        f"<title>{title}</title></head><body>\n{body}\n</body></html>\n",
        encoding="utf-8")


def _txt_to_pdf(src, dest):
    document = _text_document()
    document.setPlainText(_read_text(src))
    exporting.write_qtextdocument_pdf(document, dest)


def _json_to_csv(src, dest):
    try:
        data = json.loads(_read_text(src))
    except ValueError as exc:
        raise ConversionError(f"This file is not valid JSON ({exc}).")
    rows = None
    if isinstance(data, list) and data and \
            all(isinstance(row, dict) for row in data):
        rows = data
    elif isinstance(data, dict) and data and \
            all(not isinstance(v, (dict, list)) for v in data.values()):
        rows = [data]
    if rows is None:
        raise ConversionError(
            "JSON to CSV needs a list of objects (or one flat "
            "object) — this file's shape has no table in it.")
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: (json.dumps(v, ensure_ascii=False)
                             if isinstance(v, (dict, list)) else v)
                         for k, v in row.items()})
    Path(dest).write_text(buffer.getvalue(), encoding="utf-8")


def _csv_to_json(src, dest):
    try:
        with open(src, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error) as exc:
        raise ConversionError(f"This CSV could not be read ({exc}).")
    if not rows:
        raise ConversionError("This CSV has no data rows to convert.")
    Path(dest).write_text(json.dumps(rows, indent=2, ensure_ascii=False),
                          encoding="utf-8")


_QT_FORMAT = {"png": "PNG", "jpg": "JPEG", "bmp": "BMP", "webp": "WEBP"}


def _image_convert(src, dest, target_ext):
    from PySide6.QtGui import QImage
    image = QImage()
    if not image.load(str(src)):
        raise ConversionError(
            "This image could not be read by the local Qt build.")
    if not image.save(str(dest), _QT_FORMAT[target_ext]):
        raise ConversionError(
            f"The local Qt build could not write a "
            f"{target_ext.upper()} file.")


def _ffmpeg_convert(src, dest, target_ext):
    """One ffmpeg conversion. The muxer is passed explicitly
    (-f) because the caller writes to a temp name whose suffix is
    not the target's. Any ffmpeg failure is a plain-words error."""
    table = None
    if target_ext in _AUDIO_MUXERS:
        table = _AUDIO_MUXERS
    elif target_ext in _VIDEO_MUXERS:
        table = _VIDEO_MUXERS
    elif target_ext == "gif":
        table = {"gif": _GIF_MUXERS}
    muxer = _muxer_for(target_ext, table) if table else None
    exe = ffmpeg_path()
    if not exe or not muxer:
        raise ConversionError(
            "ffmpeg is not available for this conversion. Install "
            "it with: sudo apt install ffmpeg")
    command = [exe, "-hide_banner", "-y", "-i", str(src)]
    if target_ext in _AUDIO_MUXERS:
        command.append("-vn")  # audio target: drop any video track
    if target_ext == "gif":
        command += ["-vf", "fps=10,scale=480:-1:flags=lanczos"]
    command += ["-f", muxer, str(dest)]
    try:
        proc = subprocess.run(command, capture_output=True,
                              timeout=3600)
    except Exception as exc:
        raise ConversionError(f"ffmpeg could not run: {exc}")
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", errors="replace").strip()
        last = tail.splitlines()[-1] if tail else "unknown error"
        raise ConversionError(
            f"ffmpeg could not convert this file ({last})")


def _audio_to_text(src, dest):
    from .browser.media import transcribe_saved_audio
    text = transcribe_saved_audio(str(src))
    if not text:
        raise ConversionError(
            "No transcription backend produced any text for this "
            "file. Forge transcribes with FORGE_TRANSCRIBE_CMD, a "
            "whisper binary, or faster-whisper when one is present.")
    Path(dest).write_text(text + "\n", encoding="utf-8")


def _convert_inner(src, target_ext, dest):
    suffix = Path(src).suffix.lower()
    if suffix in _DOC_EXTS and target_ext == "txt":
        Path(dest).write_text(_doc_to_text(src), encoding="utf-8")
    elif suffix in _DOC_EXTS and target_ext == "md":
        Path(dest).write_text(
            f"# {Path(src).stem}\n\n{_doc_to_text(src)}\n",
            encoding="utf-8")
    elif suffix in (".md", ".markdown") and target_ext == "html":
        _md_to_html(src, dest)
    elif suffix in (".md", ".markdown") and target_ext == "pdf":
        _md_to_pdf(src, dest)
    elif suffix == ".txt" and target_ext == "md":
        _txt_to_md(src, dest)
    elif suffix == ".txt" and target_ext == "html":
        _txt_to_html(src, dest)
    elif suffix == ".txt" and target_ext == "pdf":
        _txt_to_pdf(src, dest)
    elif suffix in (".html", ".htm") and target_ext == "txt":
        _html_to_text(src, dest)
    elif suffix in (".html", ".htm") and target_ext == "md":
        _html_to_md(src, dest)
    elif suffix in (".html", ".htm") and target_ext == "pdf":
        _html_to_pdf(src, dest)
    elif suffix == ".json" and target_ext == "csv":
        _json_to_csv(src, dest)
    elif suffix == ".csv" and target_ext == "json":
        _csv_to_json(src, dest)
    elif suffix in _IMAGE_CANDIDATES and target_ext in _QT_FORMAT:
        _image_convert(src, dest, target_ext)
    elif suffix in _AUDIO_EXTS and target_ext == "txt":
        _audio_to_text(src, dest)
    elif suffix in _AUDIO_EXTS and target_ext in _AUDIO_MUXERS:
        _ffmpeg_convert(src, dest, target_ext)
    elif suffix in _VIDEO_EXTS and (target_ext in _VIDEO_MUXERS
                                    or target_ext in _AUDIO_MUXERS
                                    or target_ext == "gif"):
        _ffmpeg_convert(src, dest, target_ext)
    else:
        raise ConversionError(
            f"Forge cannot convert {suffix or 'this file'} to "
            f".{target_ext} — that pair is not one it really does.")


def convert(src, target_ext, dest):
    """Convert src to target_ext at dest. Returns {path, size}.
    Raises ConversionError in plain words; a failure leaves no
    partial file at dest."""
    src, dest = Path(src), Path(dest)
    target_ext = str(target_ext).lower().lstrip(".")
    offered = {t["ext"] for t in capabilities(src)}
    if target_ext not in offered:
        raise ConversionError(
            f"Forge cannot convert {src.suffix.lower() or 'this file'} "
            f"to .{target_ext} — that pair is not one it really does.")
    tmp = dest.with_name(dest.name + f".tmp-{os.getpid()}")
    try:
        _convert_inner(src, target_ext, tmp)
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise ConversionError("The conversion produced no output.")
        os.replace(tmp, dest)
    except ConversionError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    except Exception as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ConversionError(f"Conversion failed: {exc}")
    return {"path": str(dest), "size": dest.stat().st_size}
