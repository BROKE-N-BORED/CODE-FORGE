"""Maintenance commands + the optional-dependency registry — Note 25.

THE RULE (owner's words): "the forge should automatically have access
to its own terminal and ask me permission to run the command itself
instead of passing it back to me." The Forge never hands the owner a
command to type. When the Forge ITSELF needs something — a dependency,
a repair, a setup step — it proposes the exact command through the
approval flow, and on approval runs it in its own terminal path,
streams the output to the activity log, and lets the caller retry
whatever needed it.

This module is the pure-logic half (registry, argv building, display
formatting, fallback copy): no Qt, so it is unit-testable in isolation.
The dialog + runner live in forge/main.py, reusing the existing
approval machinery (and its phone-deck bridge).
"""
from __future__ import annotations
import importlib.util
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OptionalDep:
    import_name: str   # what the code imports
    pip_name: str      # what pip installs
    feature: str       # the owner-facing feature label
    reason: str        # shown in the approval: why the Forge needs it


# The registry: import name -> entry. New optional deps plug in here;
# callers ask for a proposal instead of printing install instructions.
OPTIONAL_DEPS: dict[str, OptionalDep] = {
    "faster_whisper": OptionalDep(
        import_name="faster_whisper",
        pip_name="faster-whisper",
        feature="Voice transcription",
        reason=("Voice transcription turns your dictation into text locally, on this PC. "
                "It needs the faster-whisper package. Approving runs the install into the "
                "Forge's own virtualenv — the Forge does the work, not you. The first "
                "transcription afterwards also downloads the small speech model (one time)."),
    ),
}

# Fallback copy for the voice path. These are what the owner reads when
# an install is declined, fails, or doesn't take — plain statements of
# what happened, NEVER instructions to go run something himself.
VOICE_DECLINED = "Install declined — nothing was transcribed. The audio is saved and kept."
VOICE_INSTALL_FAILED = "The install didn't complete — nothing was transcribed. The audio is saved and kept."
VOICE_STILL_MISSING = "The transcription package is still unavailable — nothing was transcribed. The audio is saved and kept."


def get_dep(import_name: str) -> OptionalDep | None:
    return OPTIONAL_DEPS.get(import_name)


def is_missing(import_name: str) -> bool:
    """True when the module cannot be found. A module that exists but is
    broken raises on real import — callers treat that path separately."""
    try:
        return importlib.util.find_spec(import_name) is None
    except (ImportError, ValueError):
        return True


def venv_python(root_dir: Path) -> Path:
    """The app virtualenv's interpreter (where installs belong). Falls
    back to the running interpreter when no venv exists (dev runs)."""
    candidate = Path(root_dir) / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else Path(sys.executable)


def install_command(python_path, import_name: str) -> list[str] | None:
    """Argv for installing a registered dep, or None if unregistered."""
    entry = get_dep(import_name)
    if entry is None:
        return None
    return [str(python_path), "-m", "pip", "install", entry.pip_name]


def format_command(argv) -> str:
    """THE display string for an argv list — built from the argv itself
    (shlex-quoted), so what the owner approves is word-for-word what
    runs. Never hand-write a parallel display string."""
    return shlex.join([str(a) for a in argv])
