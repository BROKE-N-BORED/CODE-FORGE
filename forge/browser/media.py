"""Multimodal seats — the browser hears and sees through LOCAL models.

Joshua's order (2026-11-11): the browser must be multimodal — hear
and transcribe, describe what it sees and finds. It follows the
vision-seat design he set for Forge on 2026-11-09: a background local
model turns media into TEXT, and that text is what any driver model
reads. Media itself is never attached to a model that cannot parse
it, and when no local seat is configured the functions return ""
and the caller says so in plain words — a description is never
invented.

VISION — Ollama at 127.0.0.1:11434 (local, no key). Seat choice:
FORGE_VISION_MODEL when set; otherwise the first installed model
from Joshua's seat list (eriktonon/bonsai-2-uncensored:2b — vision
projector included — then dolphinlabs/qwen3.5-uncensored), then any
installed model whose Ollama capabilities report "vision", then
common vision model names. The prompt asks for a description plus
any text visible in the image (OCR), per the seat design.

TRANSCRIPTION — local backends only, first that exists wins:
FORGE_TRANSCRIBE_CMD (a command template with {file}), the `whisper`
CLI, whisper.cpp (`whisper-cli` / `main`), or an installed
faster-whisper Python package. Model size defaults to "base"
(FORGE_WHISPER_MODEL) — small enough for a CPU-only machine.
"""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin

OLLAMA_URL = os.environ.get("FORGE_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")

_SEAT_PREFERENCE = ["eriktonon/bonsai-2-uncensored", "bonsai-2-uncensored",
                    "dolphinlabs/qwen3.5-uncensored", "qwen3.5-uncensored"]
_KNOWN_VISION = ["moondream", "llava", "qwen2.5vl", "qwen3-vl", "llama3.2-vision",
                 "minicpm-v", "bakllava", "gemma3"]

_DESCRIBE_PROMPT = (
    "Describe this image thoroughly for someone who cannot see it: "
    "what it shows, layout, objects, people (appearance only), colours, "
    "and context. Transcribe ALL text visible in the image exactly. "
    "If it is a screenshot, describe the interface state: what page or "
    "app, what controls are visible, and any message or error shown."
)


def _ollama_json(path, payload=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        OLLAMA_URL + path, data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def installed_models():
    """Names of models the local Ollama reports, [] when unreachable."""
    try:
        data = _ollama_json("/api/tags", timeout=5)
        return [model.get("name", "") for model in data.get("models", [])]
    except Exception:
        return []


def pick_vision_model():
    """The configured/installed local vision seat's model name, or None."""
    override = os.environ.get("FORGE_VISION_MODEL", "").strip()
    if override:
        return override
    names = installed_models()
    if not names:
        return None
    lowered = {name.lower(): name for name in names}
    for wanted in _SEAT_PREFERENCE + _KNOWN_VISION:
        for lowered_name, original in lowered.items():
            if wanted in lowered_name:
                return original
    for name in names:  # last resort: ask Ollama what each model can do
        try:
            info = _ollama_json("/api/show", {"model": name}, timeout=5)
            if "vision" in (info.get("capabilities") or []):
                return name
        except Exception:
            continue
    return None


def _light_machine_on():
    """The Light Machine profile's bridge into this standalone
    package: the app syncs the owner's `light_machine` setting to
    FORGE_LIGHT_MACHINE (the FORGE_VISION_MODEL / FORGE_OLLAMA_URL
    env pattern this module already uses), read fresh per call so
    a toggle flip reaches the next description."""
    return os.environ.get("FORGE_LIGHT_MACHINE", "").strip().lower() \
        in ("1", "true", "yes", "on")


def describe_image_bytes(image_bytes, prompt=_DESCRIBE_PROMPT):
    """Describe image bytes via the local vision seat; "" if none."""
    model = pick_vision_model()
    if not model:
        return ""
    try:
        payload = {
            "model": model,
            "prompt": prompt,
            "images": [base64.b64encode(image_bytes).decode()],
            "stream": False,
        }
        if _light_machine_on():
            # Light Machine: the vision seat is a seat — unload any
            # different resident model first, and carry keep_alive 0
            # so this model unloads the moment it has described.
            try:
                resident = _ollama_json("/api/ps", timeout=5)
                for entry in (resident or {}).get("models", []):
                    name = entry.get("name", "") \
                        if isinstance(entry, dict) else ""
                    if name and name != model:
                        _ollama_json("/api/generate", {
                            "model": name, "keep_alive": 0},
                            timeout=8)
            except Exception:
                pass
            payload["keep_alive"] = 0
        data = _ollama_json("/api/generate", payload, timeout=180)
        return (data.get("response") or "").strip()
    except Exception:
        return ""


def describe_saved_image(path, prompt=_DESCRIBE_PROMPT):
    with open(path, "rb") as handle:
        return describe_image_bytes(handle.read(), prompt=prompt)


def transcribe_saved_audio(path):
    """Transcribe an audio file with a local backend; "" if none."""
    template = os.environ.get("FORGE_TRANSCRIBE_CMD", "").strip()
    if template:
        try:
            out_dir = tempfile.mkdtemp(prefix="forge-transcribe-")
            command = template.replace("{file}", path).replace("{out_dir}", out_dir)
            subprocess.run(command, shell=True, capture_output=True, timeout=1800,
                           check=True)
            for name in sorted(os.listdir(out_dir)):
                if name.endswith((".txt", ".srt", ".vtt")):
                    with open(os.path.join(out_dir, name)) as handle:
                        return handle.read().strip()
        except Exception:
            return ""
    whisper = shutil.which("whisper")
    if whisper:
        try:
            out_dir = tempfile.mkdtemp(prefix="forge-transcribe-")
            subprocess.run([whisper, path, "--output_format", "txt",
                            "--output_dir", out_dir,
                            "--model", os.environ.get("FORGE_WHISPER_MODEL", "base"),
                            "--fp16", "False"],
                           capture_output=True, timeout=1800, check=True)
            base = os.path.splitext(os.path.basename(path))[0] + ".txt"
            with open(os.path.join(out_dir, base)) as handle:
                return handle.read().strip()
        except Exception:
            return ""
    for binary in ("whisper-cli", "main"):
        exe = shutil.which(binary)
        if exe and "whisper" in (exe + binary):
            try:
                out_base = os.path.join(tempfile.mkdtemp(prefix="forge-transcribe-"), "out")
                subprocess.run([exe, "-f", path, "-otxt", "-of", out_base],
                               capture_output=True, timeout=1800, check=True)
                with open(out_base + ".txt") as handle:
                    return handle.read().strip()
            except Exception:
                continue
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(os.environ.get("FORGE_WHISPER_MODEL", "base"),
                             device="cpu", compute_type="int8")
        segments, _info = model.transcribe(path)
        return " ".join(segment.text.strip() for segment in segments).strip()
    except Exception:
        return ""


class _MediaParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.media = {"images": [], "audio": [], "video": []}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        src = attrs.get("src") or attrs.get("data-src") or ""
        if tag == "img" and src:
            self.media["images"].append(urljoin(self.base_url, src))
        elif tag == "audio" and src:
            self.media["audio"].append(urljoin(self.base_url, src))
        elif tag == "video" and src:
            self.media["video"].append(urljoin(self.base_url, src))
        elif tag == "source" and src:
            kind = "audio" if (attrs.get("type") or "").startswith("audio") else "video"
            self.media[kind].append(urljoin(self.base_url, src))


def extract_media(html, base_url):
    """All image/audio/video URLs a page references, by kind."""
    parser = _MediaParser(base_url)
    parser.feed(html)
    parser.close()
    return parser.media
