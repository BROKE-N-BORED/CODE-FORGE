"""Live voice input — Note 17.

Dictation-first: the microphone records on the owner's machine, the
existing local transcription path (faster-whisper, fully offline) turns
it into text, and the text lands in the INPUT BOX for the owner to read
and edit before sending. Nothing is auto-sent.

QtMultimedia is optional: if it (or a microphone) is missing, the
feature disables itself and says why, instead of breaking the app.
Raw PCM is wrapped in a WAV header with the stdlib `wave` module.
"""
from __future__ import annotations
import io
import threading
import wave

try:
    from PySide6.QtCore import QBuffer, QByteArray
    from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
    _AVAILABLE = True
    _IMPORT_ERROR = ""
except Exception as exc:  # QtMultimedia missing / no audio stack
    _AVAILABLE = False
    _IMPORT_ERROR = str(exc)

SAMPLE_RATE = 16000


def available() -> tuple[bool, str]:
    if not _AVAILABLE:
        return False, f"QtMultimedia unavailable ({_IMPORT_ERROR})"
    try:
        if not QMediaDevices.audioInputs():
            return False, "no microphone found"
    except Exception as exc:
        return False, str(exc)
    return True, ""


def write_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE, channels: int = 1,
              sampwidth: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


# --- Audit C31 (item #24): the whisper model is RESIDENT, not rebuilt
# per clip. Constructing a WhisperModel costs seconds every time, and
# the first-ever construction quietly downloads ~40 MB from
# HuggingFace. The holder below builds the model once per process
# (when keep=True) and reuses it; the local-first construction means
# an already-downloaded model never touches the network, and the
# on_download callback lets the shell SAY a one-time download is
# starting instead of hanging a thread in silence.
_MODEL_LOCK = threading.Lock()
_model_cache = None


def _new_whisper_model(local_files_only: bool = False):
    from faster_whisper import WhisperModel
    kwargs = {}
    if local_files_only:
        kwargs["local_files_only"] = True
    return WhisperModel("tiny", device="cpu", compute_type="int8", **kwargs)


def get_whisper_model(keep: bool = True, on_download=None):
    """The shared faster-whisper model ("tiny", CPU int8).

    keep=True caches the instance process-wide behind a lock, so a
    second clip transcribes immediately instead of paying the model
    load again; keep=False constructs a fresh instance per call and
    retains nothing. Construction is local-first: when the model is
    not in the local cache yet, on_download (if given) fires once and
    the normal (downloading) construction runs. An ImportError from
    importing faster_whisper itself propagates unchanged — callers
    route it to the missing-dependency flow."""
    global _model_cache
    if keep and _model_cache is not None:
        return _model_cache
    with _MODEL_LOCK:
        if keep and _model_cache is not None:
            return _model_cache
        # Import FIRST, outside the fallback below: a missing package
        # must surface as ImportError, never be mistaken for "model
        # not downloaded yet".
        import faster_whisper  # noqa: F401
        try:
            model = _new_whisper_model(local_files_only=True)
        except Exception:
            # Sweep R155: ANY local-construction failure used to fall
            # into the download path — so a machine that HAS the
            # model but cannot load it (a corrupt cache file, a
            # broken native library) silently attempted a network
            # download, which on an offline machine failed later with
            # a misleading network error. Probe the cache first: the
            # model files provably present means the local error is
            # the real one and it surfaces unchanged; only a genuine
            # absence takes the download path.
            present = False
            try:
                from huggingface_hub import snapshot_download
                snapshot_download("Systran/faster-whisper-tiny",
                                  local_files_only=True)
                present = True
            except Exception:
                present = False
            if present:
                raise
            if on_download is not None:
                on_download()
            model = _new_whisper_model()
        if keep:
            _model_cache = model
        return model


def reset_whisper_model() -> None:
    """Drop the cached model (tests; the keep-model toggle)."""
    global _model_cache
    with _MODEL_LOCK:
        _model_cache = None


def audio_input_descriptions() -> list[str]:
    """Descriptions of the microphones this PC reports right now
    (best effort — empty when QtMultimedia or the mics are absent).
    The Settings picker and the Recorder both read this list."""
    if not _AVAILABLE:
        return []
    try:
        return [str(d.description()) for d in QMediaDevices.audioInputs()]
    except Exception:
        return []


def pick_input_device(description: str = ""):
    """The audio input the owner picked by description (audit C31),
    else the system default. An empty name, an unplugged mic, or any
    enumeration failure falls back to the default input — a stale
    setting must never make Dictate unusable."""
    if description:
        try:
            for device in QMediaDevices.audioInputs():
                if str(device.description()) == description:
                    return device
        except Exception:
            pass
    return QMediaDevices.defaultAudioInput()


def ideal_format():
    """The capture format dictation wants: 16 kHz mono Int16 — what
    the transcription path was tuned on."""
    fmt = QAudioFormat()
    fmt.setSampleRate(SAMPLE_RATE)
    fmt.setChannelCount(1)
    fmt.setSampleFormat(QAudioFormat.Int16)
    return fmt


def choose_format(device):
    """The format a recording will ACTUALLY use (audit C31): the
    ideal format when the device supports it, else the device's own
    preferred format. Yesterday's Recorder never asked — it assumed
    the ideal everywhere and wrote the WAV header from the assumption;
    a device that can't do 16 kHz mono Int16 produced a header that
    lied about the bytes under it."""
    fmt = ideal_format()
    try:
        supported = device.isFormatSupported(fmt)
    except Exception:
        return fmt  # cannot ask — keep the historical behavior
    if supported:
        return fmt
    return device.preferredFormat()


def format_sample_width(fmt) -> int:
    """Bytes per sample of a QAudioFormat, for the WAV header."""
    try:
        width = int(fmt.bytesPerSample())
    except Exception:
        width = 0
    if width > 0:
        return width
    sample_format = fmt.sampleFormat()
    if sample_format == QAudioFormat.UInt8:
        return 1
    if sample_format == QAudioFormat.Int16:
        return 2
    return 4  # Int32 / Float


class Recorder:
    """One-shot microphone capture to WAV bytes."""

    def __init__(self):
        self._buffer: QBuffer | None = None
        self._data: QByteArray | None = None
        self._source: QAudioSource | None = None
        self._format = None

    @property
    def recording(self) -> bool:
        return self._source is not None

    def start(self, device_description: str = "") -> None:
        device = pick_input_device(device_description)
        fmt = choose_format(device)
        self._format = fmt
        self._data = QByteArray()
        self._buffer = QBuffer(self._data)
        self._buffer.open(QBuffer.WriteOnly)
        self._source = QAudioSource(device, fmt)
        self._source.start(self._buffer)

    def stop(self) -> bytes:
        if self._source:
            self._source.stop()
            self._source = None
        pcm = bytes(self._data) if self._data is not None else b""
        if self._buffer:
            self._buffer.close()
            self._buffer = None
        fmt = self._format
        if fmt is None:
            return write_wav(pcm)
        # The header describes the format actually captured in —
        # identical to the old constants whenever the ideal format
        # was used (16 kHz / mono / Int16 → width 2).
        return write_wav(pcm, sample_rate=int(fmt.sampleRate()),
                         channels=int(fmt.channelCount()),
                         sampwidth=format_sample_width(fmt))
