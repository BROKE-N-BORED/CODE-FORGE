"""Guarded self-maintenance workflow for Code Forge (item #69).

The cloud maintenance engineer never edits the live application.  It
works in a candidate copy kept under the app data directory:

    <app_dir>/self-maintenance/worktree/   candidate source copy
    <app_dir>/self-maintenance/packages/   candidate update zips
    <app_dir>/self-maintenance/backups/    replaced work copies

This module is the file-system half of that workflow: preparing the
work copy, diffing it against the live root by content hash, flagging
protected paths, and building a candidate update package in exactly
the shape forge_updater.py already installs (a zip whose top folder is
"Linux Forge Agent/").  Installing a package stays an owner act in the
shell; nothing here promotes, swaps, or restarts anything.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path


TOP_FOLDER = "Linux Forge Agent"
MANIFEST_NAME = "SELF_MAINTENANCE_MANIFEST.json"

_IGNORE_DIRS = {".venv", "venv", "__pycache__", ".git"}
_IGNORE_PATTERNS = (".venv", "__pycache__", "*.pyc", ".git")

# Files whose behavior guards the owner, the keys, the update path, or
# the evidence that a change works.  A candidate that touches one is
# still buildable, but the shell requires the owner's separate
# acknowledgement before it may be installed.
PROTECTED_PATHS = frozenset({
    "forge/seats.py",
    "forge/permissions.py",
    "forge/providers.py",
    "forge/update.py",
    "forge_updater.py",
})

MAINTENANCE_CHARTER = """Forge maintenance session — the owner's standing charter for this room:
- You are Code Forge's dedicated maintenance engineer. You work ONLY in the maintenance work copy named below; the live application folder is read-only reference, never an edit target.
- Repairs, features, skills, tools and plugins are staged inside the candidate work copy. Nothing you write is live until the owner reviews the diff, the verification suite passes, and the owner installs the candidate update himself.
- Never claim a test, check, or command passed unless it actually ran in this session and you saw its result. Report failures plainly, with the output that proves them.
- You author changes; the owner approves installation. You do not approve, promote, publish, or install your own work, and you never weaken an approval gate to make a change pass.
- Protected files (the seat roster/guard, permissions, the provider seam and its keys, the updater, and the test suite) need the owner's separate acknowledgement before a candidate touching them may be installed. Say plainly when your changes touch one.
- Keep changes surgical: fix what the owner asked for, do not remove working behavior, and leave a readable summary of what changed and how it was verified."""


def maintenance_root(app_dir) -> Path:
    return Path(app_dir) / "self-maintenance"


def worktree_dir(app_dir) -> Path:
    return maintenance_root(app_dir) / "worktree"


def packages_dir(app_dir) -> Path:
    return maintenance_root(app_dir) / "packages"


def backups_dir(app_dir) -> Path:
    return maintenance_root(app_dir) / "backups"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _ignore(_directory, names):
    ignored = []
    for name in names:
        if name in _IGNORE_DIRS or name.endswith(".pyc"):
            ignored.append(name)
    return ignored


def prepare_work_copy(root_dir, app_dir, reset=False) -> Path:
    """Create (or reuse) the candidate work copy of the app root.

    The copy ignores virtualenvs, bytecode caches, and .git — the same
    set the app's own clone machinery ignores.  An existing copy is
    reused untouched (it may hold the engineer's work in progress);
    ``reset=True`` first moves it into the backups folder under a
    timestamped name, then copies fresh.
    """
    root_dir = Path(root_dir)
    work = worktree_dir(app_dir)
    backups = backups_dir(app_dir)
    work.parent.mkdir(parents=True, exist_ok=True)
    backups.mkdir(parents=True, exist_ok=True)
    packages_dir(app_dir).mkdir(parents=True, exist_ok=True)
    if work.exists() and reset:
        target = backups / f"worktree-{_stamp()}"
        suffix = 1
        while target.exists():
            suffix += 1
            target = backups / f"worktree-{_stamp()}-{suffix}"
        shutil.move(str(work), str(target))
    if not work.exists():
        shutil.copytree(root_dir, work, ignore=_ignore)
    return work


def _file_map(base) -> dict[str, Path]:
    base = Path(base)
    out: dict[str, Path] = {}
    if not base.is_dir():
        return out
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        if any(part in _IGNORE_DIRS for part in rel.parts):
            continue
        if path.suffix == ".pyc":
            continue
        out[rel.as_posix()] = path
    return out


def _hash_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MaintenanceError(Exception):
    """A candidate package refused at install time (S6, audit
    S1-22): the zip on disk does not match the checksum its sidecar
    recorded, or no checksum was recorded at all. Raised BEFORE any
    extraction or updater spawn — a refused candidate installs
    nothing."""


def candidate_sidecar_path(zip_path) -> Path:
    """The sidecar JSON build_candidate_package writes beside a
    candidate zip (same stem, .json suffix)."""
    return Path(zip_path).with_suffix(".json")


def verify_candidate_package(zip_path) -> dict:
    """Verify a candidate zip against the sha256 its sidecar
    recorded when the package was built. Returns the sidecar
    manifest on a match; raises MaintenanceError otherwise —
    sidecar missing/unreadable, no hash recorded, or the zip's
    actual hash differs (tampered or truncated after the build).
    One hash over one file, install-time only."""
    zip_path = Path(zip_path)
    sidecar = candidate_sidecar_path(zip_path)
    try:
        manifest = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("sidecar is not a JSON object")
    except Exception:
        raise MaintenanceError(
            "candidate package has no readable checksum record "
            f"({sidecar.name}) — refusing to install") from None
    recorded = str(manifest.get("sha256") or "").strip()
    if not recorded:
        raise MaintenanceError(
            "candidate package has no recorded checksum — "
            "refusing to install")
    if _hash_file(zip_path) != recorded:
        raise MaintenanceError(
            "candidate package does not match its recorded "
            "checksum — refusing to install")
    return manifest


def tree_fingerprint(base) -> str:
    """One hash for a whole tree: sorted relative paths + file hashes."""
    files = _file_map(base)
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_hash_file(files[rel]).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def changed_files(root_dir, worktree) -> list[dict]:
    """Content-hash diff of the live root against the work copy.

    Returns [{"path": rel, "status": added|modified|removed}] sorted
    by path.  "added" means present in the candidate but not the live
    root; "removed" means the candidate dropped a live file.
    """
    live = _file_map(root_dir)
    candidate = _file_map(worktree)
    changes: list[dict] = []
    for rel in sorted(set(live) | set(candidate)):
        if rel not in live:
            changes.append({"path": rel, "status": "added"})
        elif rel not in candidate:
            changes.append({"path": rel, "status": "removed"})
        elif _hash_file(live[rel]) != _hash_file(candidate[rel]):
            changes.append({"path": rel, "status": "modified"})
    return changes


def is_protected(path) -> bool:
    rel = str(path or "").replace("\\", "/").lstrip("/")
    return rel in PROTECTED_PATHS or rel.startswith("tests/")


def protected_changes(changes) -> list[dict]:
    """The subset of a changed_files() list that touches protected
    paths — the seat guard, permissions, the provider seam, the
    updater, and anything under tests/."""
    return [c for c in (changes or []) if is_protected(c.get("path"))]


def build_candidate_package(root_dir, worktree, app_dir, metadata=None) -> tuple[Path, dict]:
    """Zip the work copy as a candidate update + a sidecar manifest.

    The zip carries the top folder "Linux Forge Agent/" (the shape the
    external updater installs) and a SELF_MAINTENANCE_MANIFEST.json at
    the app root inside it.  The sidecar JSON beside the zip carries
    the same record plus the zip's own sha256 — a hash that cannot
    live inside the file it describes.  Returns (zip_path, manifest).
    """
    root_dir = Path(root_dir)
    worktree = Path(worktree)
    packages = packages_dir(app_dir)
    packages.mkdir(parents=True, exist_ok=True)
    metadata = dict(metadata or {})
    changes = changed_files(root_dir, worktree)
    protected = protected_changes(changes)
    created_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "kind": "code-forge-self-maintenance-candidate",
        "created_at": created_at,
        "base_fingerprint": tree_fingerprint(root_dir),
        "candidate_fingerprint": tree_fingerprint(worktree),
        "changed_files": changes,
        "protected_files": protected,
        "models_used": [str(m) for m in (metadata.get("models_used") or [])
                        if str(m).strip()],
        "tests_passed": bool(metadata.get("tests_passed", False)),
    }
    for key in ("base_version", "summary"):
        if metadata.get(key):
            manifest[key] = str(metadata[key])
    stamp = _stamp()
    zip_path = packages / f"forge-self-update-{stamp}.zip"
    suffix = 1
    while zip_path.exists():
        suffix += 1
        zip_path = packages / f"forge-self-update-{stamp}-{suffix}.zip"
    files = _file_map(worktree)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(files):
            if rel == MANIFEST_NAME:
                continue  # replaced by the generated manifest below
            zf.write(files[rel], f"{TOP_FOLDER}/{rel}")
        zf.writestr(f"{TOP_FOLDER}/{MANIFEST_NAME}",
                    json.dumps(manifest, indent=2))
    zip_hash = _hash_file(zip_path)
    manifest["sha256"] = zip_hash
    sidecar = zip_path.with_suffix(".json")
    sidecar.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return zip_path, manifest
