"""Self-update helpers — Note 19 (in-app side).

The app writes a RESUME STATE before quitting for an update; on the
next launch it reads the state back and restores the conversation,
model, mode and workspace, prefilling an interrupted prompt so the
owner resumes with one Send. The same state doubles as the crash /
freeze resume record (Note 14 #2): it is refreshed whenever a turn
starts and cleared when a turn completes cleanly.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path

STATE_NAME = "resume_state.json"
RESULT_NAME = "update_result.json"


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON via a temp file in the same directory + replace, so
    a crash mid-write can never leave a torn file behind. Sweep R163:
    the temp name carries the pid — two processes writing the same
    state file used to share one fixed .tmp and could replace each
    other's half-written file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_resume_state(data_dir: Path, state: dict) -> None:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(data_dir / STATE_NAME, state)


def read_resume_state(data_dir: Path) -> dict | None:
    try:
        return json.loads((Path(data_dir) / STATE_NAME).read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_resume_state(data_dir: Path) -> None:
    try:
        (Path(data_dir) / STATE_NAME).unlink()
    except OSError:
        pass


def read_update_result(data_dir: Path) -> dict | None:
    try:
        return json.loads((Path(data_dir) / RESULT_NAME).read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_update_result(data_dir: Path) -> None:
    try:
        (Path(data_dir) / RESULT_NAME).unlink()
    except OSError:
        pass


def spawn_updater(root_dir: Path, data_dir: Path, zip_path: str | None,
                  extra_pip: list[str] | None = None) -> Path:
    """Launch forge_updater.py detached; returns the manifest path."""
    root_dir, data_dir = Path(root_dir), Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"app_dir": str(root_dir), "data_dir": str(data_dir),
                "zip": zip_path, "wait_pid": os.getpid(),
                "extra_pip": extra_pip or []}
    manifest_path = data_dir / "update_manifest.json"
    _atomic_write_json(manifest_path, manifest)
    subprocess.Popen([sys.executable, str(root_dir / "forge_updater.py"),
                      "--manifest", str(manifest_path)],
                     cwd=root_dir, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return manifest_path
