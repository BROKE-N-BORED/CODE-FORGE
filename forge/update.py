"""Self-update helpers — Note 19 (in-app side).

The app writes a RESUME STATE before quitting for an update; on the
next launch it reads the state back and restores the conversation,
model, mode and workspace, prefilling an interrupted prompt so the
owner resumes with one Send. The same state doubles as the crash /
freeze resume record (Note 14 #2): it is refreshed whenever a turn
starts and cleared when a turn completes cleanly.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

STATE_NAME = "resume_state.json"
RESULT_NAME = "update_result.json"

# C15 (audit S1-22/W1): the manual update gate. The dialog branch of
# apply_update used to hand any owner-picked zip straight to the
# updater with no verification at all — no hash, no signature, no
# manifest check, no contents preview — and that branch is the path
# every real Forge release takes. The gate below runs BEFORE the
# spawn: it previews the zip's contents against the updater's own
# allowlist, verifies a release checksum sidecar when one sits
# beside the zip, and otherwise requires the owner's explicit
# unverified acknowledgement. Pure functions, no Qt — UpdateDialog
# shows their results and apply_update enforces them.
SIDECAR_SUFFIX = ".sha256"

# The updater's allowlist, duplicated here ONLY as the fallback when
# forge_updater.py cannot be loaded — its own file is the authority
# (_updater_allowlist loads it); a test pins these two sets equal.
_FALLBACK_ALLOWED_TOPS = {"forge", "plugins", "skills", "commands"}
_FALLBACK_ALLOWED_ROOT_FILES = {"README.md", "install.sh", "run.sh",
                                "requirements.txt", "forge_updater.py"}

_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_PREVIEW_LIST_CAP = 40  # entries shown per section; the counts are exact
_REFUSED_NAME_CAP = 5   # unsafe entries named in a refusal reason


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


def _hash_file(path) -> str:
    """sha256 of a file, streamed (a release zip is megabytes)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_sidecar_path(zip_path) -> Path:
    """The release checksum sidecar for a zip: <zip>.sha256 — the
    suffix is APPENDED, not substituted, so CODE-FORGE-v2.5.zip
    pairs with CODE-FORGE-v2.5.zip.sha256. Convention (defined
    here; shipping the file with each release is the DEV
    release-process half): the file holds one sha256sum-format
    line — "<hex>  <zipname>" — or a bare 64-hex digest on its own.
    The filename token is ignored on purpose: the zip may have
    been renamed on download."""
    return Path(str(zip_path) + SIDECAR_SUFFIX)


def verify_release_sidecar(zip_path) -> dict:
    """Verify a manual-update zip against its release sidecar.
    Returns {"status", "sidecar", "sha256", "expected", "detail"}.
    status is one of:
      "verified"   — sidecar present, digest matches the zip.
      "absent"     — no sidecar beside the zip (the acknowledgement
                     path, not a refusal on its own).
      "mismatch"   — sidecar present, digest differs (tampered or
                     truncated after release).
      "unreadable" — sidecar present but unparseable (no 64-hex
                     digest on its first line), or the zip itself
                     cannot be hashed.
    """
    zip_path = Path(zip_path)
    sidecar = release_sidecar_path(zip_path)
    result = {"status": "unreadable", "sidecar": str(sidecar),
              "sha256": "", "expected": "", "detail": ""}
    try:
        actual = _hash_file(zip_path)
    except OSError as exc:
        result["detail"] = f"could not read the zip ({exc})"
        return result
    result["sha256"] = actual
    try:
        first = sidecar.read_text(encoding="utf-8").splitlines()[0]
    except FileNotFoundError:
        result["status"] = "absent"
        result["detail"] = "no checksum file shipped beside this zip"
        return result
    except (OSError, IndexError, UnicodeDecodeError) as exc:
        result["detail"] = f"could not read the checksum file ({exc})"
        return result
    token = first.strip().split()
    expected = token[0] if token else ""
    if not _HEX64_RE.match(expected):
        result["detail"] = ("the checksum file holds no sha256 digest "
                            "(expected '<64 hex>  <zipname>')")
        return result
    result["expected"] = expected.lower()
    if expected.lower() != actual:
        result["status"] = "mismatch"
        result["detail"] = "the zip does not match its checksum file"
        return result
    result["status"] = "verified"
    result["detail"] = "checksum matches"
    return result


def _updater_allowlist(root_dir):
    """(ALLOWED_TOPS, ALLOWED_ROOT_FILES) from the updater's own
    file — forge_updater.py at the tree root is the authority, so
    the preview can never drift from what the updater will do.
    Loaded as a module (stdlib-only; its main() runs only under
    __main__). Falls back to the duplicated sets when the file is
    missing or unloadable."""
    try:
        updater_path = Path(root_dir) / "forge_updater.py"
        spec = importlib.util.spec_from_file_location(
            "forge_updater_allowlist", updater_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tops = set(module.ALLOWED_TOPS)
        files = set(module.ALLOWED_ROOT_FILES)
        if tops and files:
            return tops, files
    except Exception:
        pass
    return set(_FALLBACK_ALLOWED_TOPS), set(_FALLBACK_ALLOWED_ROOT_FILES)


def preview_update_zip(zip_path, root_dir) -> dict:
    """Classify a manual-update zip's entries WITHOUT extracting —
    the pre-install preview. Mirrors safe_extract's rules in
    forge_updater.py exactly (same top-folder strip, same
    allowlist, same unsafe-entry refusals), so the preview shows
    what the updater would actually do:
      "install" — entries the updater would stage (shown by the
                  path they would land at, top folder stripped).
      "skipped" — entries outside the updater's allowlist (tests/,
                  templates/, LICENSE, …). The updater SKIPS these
                  by design — a real release zip carries dozens —
                  so they are NAMED here, never silently dropped,
                  but they do not refuse the update.
      "refused" — unsafe entries (path escapes, absolute paths),
                  shown by their raw entry name. The updater
                  refuses these itself; the gate refuses the whole
                  update when any appear.
    Returns {"install", "skipped", "refused", "top_folder",
    "total_entries", "install_bytes", "total_bytes", "error"}.
    "error" is set (and the lists empty) when the zip cannot be
    read at all. Byte counts come from the zip headers (display
    only — the updater's streamed count is the real guard).
    """
    root_dir = Path(root_dir)
    top_name = root_dir.name
    tops, root_files = _updater_allowlist(root_dir)
    out = {"install": [], "skipped": [], "refused": [],
           "top_folder": top_name, "total_entries": 0,
           "install_bytes": 0, "total_bytes": 0, "error": ""}
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
    except Exception as exc:
        out["error"] = f"not a readable zip file ({exc})"
        return out
    for info in infos:
        name = info.filename
        if name.endswith("/"):
            continue
        out["total_entries"] += 1
        out["total_bytes"] += info.file_size
        parts = Path(name).parts
        if parts and parts[0] == top_name:
            parts = parts[1:]
        if (not parts or parts[0] in ("..", "/")
                or ".." in parts or os.path.isabs(name)):
            out["refused"].append(name)
            continue
        if len(parts) > 1 and parts[0] not in tops:
            out["skipped"].append("/".join(parts))
            continue
        if len(parts) == 1 and parts[0] not in root_files:
            out["skipped"].append("/".join(parts))
            continue
        out["install"].append("/".join(parts))
        out["install_bytes"] += info.file_size
    return out


def gate_manual_update(zip_path, root_dir, acknowledged=False) -> dict:
    """The manual-update gate: {"allowed", "reason", "preview",
    "sidecar"}. Decided BEFORE the updater spawns; a refusal
    installs nothing. Refuses when the zip is unreadable, carries
    any unsafe entry, holds no installable Forge code, or its
    sidecar mismatches / is unreadable. A zip with NO sidecar is
    allowed only when the owner acknowledged it as unverified.
    Entries outside the updater's allowlist are named in the
    preview but never refuse — every real release zip carries
    them (tests/, templates/, LICENSE, …) and the updater skips
    them by design."""
    preview = preview_update_zip(zip_path, root_dir)
    sidecar = verify_release_sidecar(zip_path)
    result = {"allowed": False, "reason": "", "preview": preview,
              "sidecar": sidecar}
    if preview["error"]:
        result["reason"] = f"Update refused: {preview['error']}."
        return result
    if preview["refused"]:
        named = preview["refused"][:_REFUSED_NAME_CAP]
        extra = len(preview["refused"]) - len(named)
        listing = ", ".join(named)
        if extra:
            listing += f" (+{extra} more)"
        result["reason"] = (
            "Update refused: this zip contains unsafe entries the "
            f"updater would refuse: {listing}.")
        return result
    if not preview["install"]:
        result["reason"] = (
            "Update refused: this zip contains no Forge code files "
            "(wrong top folder?). Nothing would install.")
        return result
    status = sidecar["status"]
    if status == "verified":
        result["allowed"] = True
        result["reason"] = (
            "Checksum verified: this zip matches its checksum file "
            f"(sha256 {sidecar['sha256'][:12]}…).")
        return result
    if status == "mismatch":
        result["reason"] = (
            "Update refused: this zip does not match its checksum "
            "file. It may be corrupted or substituted. Refusing to "
            "install.")
        return result
    if status == "unreadable":
        result["reason"] = (
            "Update refused: " + (sidecar["detail"]
            or "the checksum file could not be read.") + ".")
        return result
    # status == "absent": no sidecar shipped — the owner's explicit
    # acknowledgement is the only thing standing between this zip
    # and the install.
    if not acknowledged:
        result["reason"] = (
            "No checksum shipped with this zip. Nothing has "
            "verified it. Tick “Install unverified. I trust this "
            "file” to install it anyway, or cancel.")
        return result
    result["allowed"] = True
    result["reason"] = (
        "No checksum shipped with this zip. Installing UNVERIFIED "
        "on your acknowledgement.")
    return result


def format_update_preview(preview, sidecar) -> str:
    """The preview text UpdateDialog shows: verification state,
    what would install, what the updater would skip (named), and
    any unsafe entries. Lists are capped for display; the counts
    are exact."""
    lines = []
    status = (sidecar or {}).get("status", "")
    if status == "verified":
        lines.append("✓ Checksum verified: this zip matches the "
                     ".sha256 file beside it.")
    elif status == "mismatch":
        lines.append("✗ Checksum MISMATCH: this zip does not match "
                     "its .sha256 file. It will not install.")
    elif status == "unreadable":
        lines.append("✗ Checksum file unreadable. This zip will "
                     "not install.")
    else:
        lines.append("⚠ No checksum file beside this zip. Nothing "
                     "has verified it.")
    if preview.get("error"):
        lines.append(preview["error"] + ".")
        return "\n".join(lines)

    def _section(title, names):
        lines.append(title)
        for name in names[:_PREVIEW_LIST_CAP]:
            lines.append(f"  {name}")
        if len(names) > _PREVIEW_LIST_CAP:
            lines.append(f"  …and {len(names) - _PREVIEW_LIST_CAP} more")
    install = preview["install"]
    _section(f"Would install {len(install)} file(s) "
             f"({preview['install_bytes']:,} bytes):", install)
    skipped = preview["skipped"]
    if skipped:
        _section(f"Not installed by the updater ({len(skipped)}):",
                 skipped)
    refused = preview["refused"]
    if refused:
        _section(f"UNSAFE entries. The update will be refused "
                 f"({len(refused)}):", refused)
    return "\n".join(lines)
