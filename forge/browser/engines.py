"""Engine status — what browser power this machine actually has.

One plain report used by the CLI and the setup script's checks:
which engines exist, which search backends are configured, whether
the vision seat and a transcription backend are available, and
where the vault lives. Read-only; reports absence plainly instead
of guessing.
"""

import os
import shutil

from .cdp import find_chromium
from .vault import default_vault_path


def engine_status():
    from . import media
    lightpanda = shutil.which("lightpanda")
    vision_model = None
    try:
        vision_model = media.pick_vision_model()
    except Exception:
        vision_model = None
    return {
        "chromium": find_chromium(),
        "lightpanda_binary": lightpanda,
        "lightpanda_serving": os.environ.get("FORGE_LIGHTPANDA_URL", "") or None,
        "cdp_url": os.environ.get("FORGE_CDP_URL", "") or None,
        "searxng_url": os.environ.get("FORGE_SEARXNG_URL", "") or None,
        "search_default": "searxng" if os.environ.get("FORGE_SEARXNG_URL")
                          else "keyless chain (DuckDuckGo Lite/HTML, Bing)",
        "vision_seat_model": vision_model,
        "transcription": ("FORGE_TRANSCRIBE_CMD" if os.environ.get("FORGE_TRANSCRIBE_CMD")
                          else ("whisper CLI" if shutil.which("whisper") else None)),
        "vault_path": default_vault_path(),
        "vault_exists": os.path.exists(default_vault_path()),
    }


def format_status(status):
    def show(value):
        return value if value else "not found / not configured"
    return "\n".join([
        f"Chromium engine: {show(status['chromium'])}",
        f"Lightpanda binary: {show(status['lightpanda_binary'])}",
        f"Lightpanda serving: {show(status['lightpanda_serving'])}",
        f"External CDP endpoint: {show(status['cdp_url'])}",
        f"Search: {status['search_default']}"
        + (f" ({status['searxng_url']})" if status['searxng_url'] else ""),
        f"Vision seat (local Ollama): {show(status['vision_seat_model'])}",
        f"Transcription: {show(status['transcription'])}",
        f"Vault: {status['vault_path']} ({'exists' if status['vault_exists'] else 'not created yet'})",
    ])
