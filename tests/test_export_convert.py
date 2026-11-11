"""Export & Convert (2026-11-11): transcript export with images
(PDF / Markdown / HTML) and the capability-probed file converter.

The PDF loop closes with the converter's ToUnicode-aware reader:
Qt glyph-encodes PDF text, so the literal-string extractor cannot
read Forge's own PDFs back — _pdf_text_tounicode can, and the
export test asserts the turn text through it.
"""
import base64
import io
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import unittest
import wave
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge import converting, exporting  # noqa: E402


def _png_bytes(width=400, height=300):
    """A real, non-trivial PNG (random noise compresses poorly, so
    an embedded copy measurably grows the PDF)."""
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage
    image = QImage(width, height, QImage.Format.Format_RGB32)
    rng = random.Random(42)
    for x in range(0, width, 4):
        for y in range(0, height, 4):
            image.setPixelColor(
                x, y, QColor(rng.randrange(256), rng.randrange(256),
                             rng.randrange(256)))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


class _QtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = Path(self.id().replace(".", "_") + "-tmp")
        # tests run from the app dir; keep fixtures under /tmp
        import tempfile
        self.dir = Path(tempfile.mkdtemp(prefix="forge-export-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)


class ExportTests(_QtCase):
    def _messages(self, with_image=True):
        messages = [
            {"role": "user", "content": "Look at this picture please"},
            {"role": "assistant", "content": "I see a noisy square."},
        ]
        if with_image:
            messages[0]["images"] = [
                base64.b64encode(_png_bytes()).decode("ascii")]
        return messages

    def test_pdf_embeds_image_and_text_is_extractable(self):
        out = self.dir / "chat.pdf"
        result = exporting.export_transcript(
            self._messages(), "pdf", out, title="Test chat",
            model="some-model")
        self.assertTrue(out.exists())
        self.assertEqual(result["embedded"], 1)
        data = out.read_bytes()
        self.assertIn(b"/Image", data)
        control = self.dir / "control.pdf"
        exporting.export_transcript(
            self._messages(with_image=False), "pdf", control,
            title="Test chat", model="some-model")
        self.assertGreater(len(data), control.stat().st_size)
        text = converting._pdf_text_tounicode(data)
        self.assertIn("Look at this picture please", text)
        self.assertIn("I see a noisy square.", text)

    def test_missing_image_placeholder(self):
        messages = [
            {"role": "user", "content": "here",
             "images": ["!!!not-base64!!!"]},
            {"role": "assistant", "content": "ok"},
        ]
        out = self.dir / "chat.md"
        result = exporting.export_transcript(messages, "md", out)
        self.assertEqual(result["missing"], 1)
        self.assertIn("[image not found]", out.read_text())
        pdf = self.dir / "chat.pdf"
        result = exporting.export_transcript(messages, "pdf", pdf)
        self.assertEqual(result["missing"], 1)

    def test_markdown_assets_folder(self):
        out = self.dir / "chat.md"
        result = exporting.export_transcript(
            self._messages(), "md", out, title="Test chat")
        self.assertEqual(result["embedded"], 1)
        assets = self.dir / "chat_assets"
        self.assertTrue(assets.is_dir())
        images = list(assets.iterdir())
        self.assertEqual(len(images), 1)
        text = out.read_text()
        self.assertIn("## You", text)
        self.assertIn("## Forge", text)
        self.assertIn(f"chat_assets/{images[0].name}", text)

    def test_html_data_uri_and_toggle_off(self):
        out = self.dir / "chat.html"
        result = exporting.export_transcript(self._messages(), "html", out)
        self.assertEqual(result["embedded"], 1)
        self.assertIn("data:image/png;base64", out.read_text())
        result = exporting.export_transcript(
            self._messages(), "html", out, include_images=False)
        self.assertEqual(result["omitted"], 1)
        self.assertIn("[image not included]", out.read_text())

    def test_export_dialog_construct(self):
        from PySide6.QtWidgets import QWidget
        from forge.main import ExportChatDialog
        owner = QWidget()
        owner.log = lambda *_a: None
        dialog = ExportChatDialog(owner, "A chat", self._messages(),
                                  "model-x", None)
        labels = [dialog.format_combo.itemText(i) for i in range(3)]
        self.assertEqual(labels, ["PDF (.pdf)", "Markdown (.md)",
                                  "HTML (.html)"])
        self.assertTrue(dialog.images_check.isChecked())
        self.assertTrue(dialog.path_edit.text().endswith(".pdf"))
        dialog.format_combo.setCurrentIndex(1)
        self.assertTrue(dialog.path_edit.text().endswith(".md"))


class ConverterMatrixTests(_QtCase):
    def _caps(self, name, content=b"x"):
        path = self.dir / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
        return {t["ext"] for t in converting.capabilities(path)}

    def test_document_and_data_matrix(self):
        self.assertEqual(self._caps("a.pdf", b"%PDF-1.4 x"), {"txt", "md"})
        self.assertEqual(self._caps("a.docx", b"PK x"), {"txt", "md"})
        self.assertEqual(self._caps("a.odt", b"PK x"), {"txt", "md"})
        self.assertEqual(self._caps("a.md", "# hi"), {"pdf", "html"})
        self.assertEqual(self._caps("a.txt", "hi"), {"pdf", "md", "html"})
        self.assertEqual(self._caps("a.html", "<p>hi</p>"),
                         {"pdf", "txt", "md"})
        self.assertEqual(self._caps("a.json", "{}"), {"csv"})
        self.assertEqual(self._caps("a.csv", "a,b\n1,2"), {"json"})
        self.assertEqual(self._caps("a.xyz", "hi"), set())

    def test_image_matrix_from_qt_probe(self):
        caps = self._caps("a.png", _png_bytes(20, 10))
        self.assertEqual(caps, {"bmp", "jpg", "webp"})

    def test_unsupported_pair_refused_and_no_partial(self):
        src = self.dir / "a.json"
        src.write_text('{"outer": {"nested": "no table here"}}')
        dest = self.dir / "a.csv"
        with self.assertRaises(converting.ConversionError):
            converting.convert(src, "csv", dest)
        self.assertFalse(dest.exists())
        src2 = self.dir / "b.txt"
        src2.write_text("hello")
        with self.assertRaises(converting.ConversionError):
            converting.convert(src2, "csv", self.dir / "b.csv")
        self.assertFalse((self.dir / "b.csv").exists())


class ConverterRunTests(_QtCase):
    def test_md_to_pdf(self):
        src = self.dir / "note.md"
        src.write_text("# Heading\n\nSome **bold** words here.")
        result = converting.convert(src, "pdf", self.dir / "note.pdf")
        self.assertGreater(result["size"], 1000)
        text = converting._pdf_text_tounicode(
            (self.dir / "note.pdf").read_bytes())
        self.assertIn("Heading", text)

    def test_png_to_jpg_round_trip(self):
        src = self.dir / "img.png"
        src.write_bytes(_png_bytes(120, 80))
        converting.convert(src, "jpg", self.dir / "img.jpg")
        from PySide6.QtGui import QImage
        image = QImage()
        self.assertTrue(image.load(str(self.dir / "img.jpg")))
        self.assertEqual((image.width(), image.height()), (120, 80))

    def test_json_csv_round_trip(self):
        src = self.dir / "data.json"
        src.write_text('[{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]')
        converting.convert(src, "csv", self.dir / "data.csv")
        self.assertIn("a,b", (self.dir / "data.csv").read_text())
        converting.convert(self.dir / "data.csv", "json",
                           self.dir / "back.json")
        rows = json.loads((self.dir / "back.json").read_text())
        self.assertEqual(rows[1], {"a": "2", "b": "y"})

    def test_pdf_to_text_uses_tounicode_fallback(self):
        messages = [{"role": "user", "content": "a very unique phrase zqx"}]
        pdf = self.dir / "chat.pdf"
        exporting.export_transcript(messages, "pdf", pdf, title="T")
        converting.convert(pdf, "txt", self.dir / "chat.txt")
        self.assertIn("a very unique phrase zqx",
                      (self.dir / "chat.txt").read_text())

    def test_docx_to_text(self):
        src = self.dir / "doc.docx"
        xml = ('<?xml version="1.0"?><w:document xmlns:w='
               '"http://schemas.openxmlformats.org/wordprocessingml/'
               '2006/main"><w:body><w:p><w:r><w:t>Hello docx '
               'world</w:t></w:r></w:p></w:body></w:document>')
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("word/document.xml", xml)
        converting.convert(src, "txt", self.dir / "doc.txt")
        self.assertIn("Hello docx world",
                      (self.dir / "doc.txt").read_text())


_FAKE_FORMATS = """File formats:
 D. = Demuxing supported
 .E = Muxing supported
 --
 D  wav             WAVE format
  E wav             WAVE format
  E flac            FLAC format
"""


class FfmpegProbeTests(_QtCase):
    def _wav(self):
        path = self.dir / "tone.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 800)
        return path

    def tearDown(self):
        converting._reset_ffmpeg_cache()

    def test_ffmpeg_absent_families_unavailable_with_reason(self):
        with mock.patch.object(converting, "ffmpeg_path", lambda: None):
            converting._reset_ffmpeg_cache()
            present, note = converting.ffmpeg_status()
            self.assertFalse(present)
            self.assertIn("sudo apt install ffmpeg", note)
            self.assertEqual(converting.capabilities(self._wav()), [])
            clip = self.dir / "clip.mp4"
            clip.write_bytes(b"not a real mp4")
            self.assertEqual(converting.capabilities(clip), [])

    def test_ffmpeg_stub_probe_and_copy_conversion(self):
        def fake_run(command, **_kwargs):
            if "-formats" in command:
                return subprocess.CompletedProcess(
                    command, 0, stdout=_FAKE_FORMATS.encode())
            src = command[command.index("-i") + 1]
            Path(command[-1]).write_bytes(Path(src).read_bytes())
            return subprocess.CompletedProcess(command, 0, stderr=b"")

        with mock.patch.object(converting, "ffmpeg_path",
                               lambda: "/fake/ffmpeg"), \
                mock.patch.object(converting.subprocess, "run", fake_run), \
                mock.patch.object(converting, "transcription_available",
                                  lambda: False):
            converting._reset_ffmpeg_cache()
            wav = self._wav()
            self.assertEqual(
                {t["ext"] for t in converting.capabilities(wav)},
                {"flac"})
            result = converting.convert(wav, "flac", self.dir / "tone.flac")
            self.assertEqual(result["size"], wav.stat().st_size)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_ffmpeg_real_wav_to_mp3_and_mp4_extract(self):
        converting._reset_ffmpeg_cache()
        wav = self._wav()
        caps = {t["ext"] for t in converting.capabilities(wav)}
        self.assertIn("mp3", caps)
        converting.convert(wav, "mp3", self.dir / "tone.mp3")
        self.assertGreater((self.dir / "tone.mp3").stat().st_size, 0)
        clip = self.dir / "clip.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=red:32x32:d=0.2",
             "-f", "lavfi", "-i", "sine=440:0.2", "-shortest", str(clip)],
            capture_output=True, check=True)
        caps = {t["ext"] for t in converting.capabilities(clip)}
        self.assertTrue({"mp3", "gif"} <= caps)
        converting.convert(clip, "mp3", self.dir / "clip.mp3")
        converting.convert(clip, "gif", self.dir / "clip.gif")
        self.assertGreater((self.dir / "clip.gif").stat().st_size, 0)


class ConverterWindowProbeTests(_QtCase):
    def test_window_inventory(self):
        from forge.main import ConverterWindow
        from PySide6.QtWidgets import QWidget
        owner = QWidget()
        owner.log = lambda *_a: None
        window = ConverterWindow(owner)
        # ffmpeg present here: the note stays silent (no line shown)
        self.assertEqual(window.ffmpeg_label.text(), "")
        self.assertFalse(window.convert_btn.isEnabled())
        # ffmpeg absent: exactly one passive line, with the install
        with mock.patch.object(converting, "ffmpeg_path", lambda: None):
            window._refresh_ffmpeg_note()
            self.assertIn("sudo apt install ffmpeg",
                          window.ffmpeg_label.text())
            self.assertTrue(window.ffmpeg_label.isVisibleTo(window)
                            or window.ffmpeg_label.text())
        window._refresh_ffmpeg_note()
        src = self.dir / "note.md"
        src.write_text("# Hi")
        window._path = str(src)
        window.path_label.setText(str(src))
        window.targets_list.clear()
        for target in converting.capabilities(src):
            from PySide6.QtWidgets import QListWidgetItem
            from PySide6.QtCore import Qt
            item = QListWidgetItem(target["label"])
            item.setData(Qt.UserRole, target["ext"])
            window.targets_list.addItem(item)
        self.assertEqual(window.targets_list.count(), 2)


if __name__ == "__main__":
    unittest.main()
