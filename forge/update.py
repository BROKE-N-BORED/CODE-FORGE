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
import http.client
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
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


# ------------------------------------------------------------------
# Auto-update (Joshua's standing order, 2026-11-17): on launch the
# app silently checks its GitHub repo for a newer Release; up to
# date means nothing happens; an update found is a popup that asks,
# and only the owner's tap pulls it; the pull verifies against the
# release's own checksum sidecar before anything installs; after a
# verified pull the owner is asked "restart now, yes or no" — No
# records the verified zip as pending and the NEXT launch applies
# it before the window opens, re-verified against the recorded
# hash. Everything in this section is Qt-free: the window drives
# it through two small threads and does the asking; the checking,
# downloading, verifying and pending-record keeping live here
# where tests can reach them without a display. The check is a
# plain unauthenticated GET to GitHub's public API — no token,
# nothing identifying is sent.

LATEST_RELEASE_URL = ("https://api.github.com/repos/BROKE-N-BORED/"
                      "CODE-FORGE/releases/latest")
RELEASES_PAGE_URL = "https://github.com/BROKE-N-BORED/CODE-FORGE/releases"
CHECK_TIMEOUT_S = 8
DOWNLOAD_TIMEOUT_S = 30
PENDING_NAME = "pending_update.json"
UPDATES_DIR_NAME = "updates"

_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)$")


class UpdateError(Exception):
    """A plain-words update failure: the check could not run, the
    download failed, or a downloaded file did not verify. Whoever
    catches it shows str(exc) as it stands."""


class DownloadCancelled(UpdateError):
    """The owner cancelled the download. The partial file stays on
    disk so the next attempt resumes where this one stopped."""


def parse_version(text):
    """A version string as a tuple of ints — "2.6" becomes (2, 6),
    "v2.6" the same — or None when it is not a version at all.
    Anything but an optional "v" followed by numbers and dots is
    refused, so a tag like "v2.5-f11f13" reads as no usable
    version rather than a wrong one."""
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.match(text.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(latest, current):
    """True when the latest version is newer than the running one.
    Either side unparsable means no — an unknown version is never
    reported as an update. The compare is numeric, not
    lexicographic: 2.10 is newer than 2.9. Shorter tuples are
    padded with zeros, so 2.6 and 2.6.0 compare equal."""
    latest_parsed = parse_version(latest)
    current_parsed = parse_version(current)
    if latest_parsed is None or current_parsed is None:
        return False
    width = max(len(latest_parsed), len(current_parsed))
    latest_padded = latest_parsed + (0,) * (width - len(latest_parsed))
    current_padded = current_parsed + (0,) * (width - len(current_parsed))
    return latest_padded > current_padded


def _asset_entry(raw):
    """One release asset normalized to {"name", "url", "size"},
    or None when the payload's entry is unusable. The name is
    reduced to its final path part — it becomes a file name on
    disk here, so nothing path-like may ride in on it."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    url = raw.get("browser_download_url")
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(url, str) or not url:
        return None
    size = raw.get("size")
    return {"name": Path(name).name, "url": url,
            "size": size if isinstance(size, int) else None}


def pick_release_assets(assets):
    """The downloadable pair from a release's asset list: the
    update zip and its checksum sidecar. The zip is the .zip
    asset that HAS a "<name>.sha256" partner when one exists (a
    release can carry other zips); otherwise the first .zip.
    Returns {"zip": entry | None, "sidecar": entry | None}."""
    entries = [entry for entry in
               (_asset_entry(raw) for raw in assets or [])
               if entry is not None]
    by_name = {entry["name"]: entry for entry in entries}
    zips = [entry for entry in entries
            if entry["name"].lower().endswith(".zip")]
    chosen = None
    for entry in zips:
        if entry["name"] + SIDECAR_SUFFIX in by_name:
            chosen = entry
            break
    if chosen is None and zips:
        chosen = zips[0]
    sidecar = None
    if chosen is not None:
        sidecar = by_name.get(chosen["name"] + SIDECAR_SUFFIX)
    return {"zip": chosen, "sidecar": sidecar}


def fetch_latest_release(urlopen=None):
    """The latest Code Forge release from GitHub's public
    endpoint: {"version", "url", "notes", "zip", "sidecar"}.
    Raises UpdateError with a plain reason when the network
    fails, the answer is not JSON, or it names no usable
    version. urlopen is a test seam — pass a stub and no network
    is touched."""
    opener = urlopen or urllib.request.urlopen
    try:
        with opener(LATEST_RELEASE_URL,
                    timeout=CHECK_TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError):
            raise UpdateError("the update check timed out")
        raise UpdateError(f"could not reach GitHub: {reason}")
    except TimeoutError:
        raise UpdateError("the update check timed out")
    except (ValueError, UnicodeDecodeError, http.client.HTTPException):
        raise UpdateError(
            "the releases page did not answer in a form "
            "Code Forge understands")
    if not isinstance(payload, dict):
        raise UpdateError(
            "the releases page did not answer in a form "
            "Code Forge understands")
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise UpdateError("the releases page did not name a version")
    version = parse_version(tag)
    if version is None:
        raise UpdateError(
            "the latest release tag did not look like a version: "
            f"{tag}")
    picked = pick_release_assets(payload.get("assets"))
    return {"version": ".".join(str(part) for part in version),
            "url": payload.get("html_url") or RELEASES_PAGE_URL,
            "notes": payload.get("body") or "",
            "zip": picked["zip"], "sidecar": picked["sidecar"]}


def check_for_update(current_version, urlopen=None):
    """One call, three outcomes, never an exception for a network
    or parsing problem:

    {"state": "newer", ...}   — a newer release is out; "zip" and
    "sidecar" carry its downloadable pair (either may be None
    when the release carries no such asset).
    {"state": "current", ...} — the running version is the newest
    (or newer than the latest release: a dev tree).
    {"state": "unknown", "reason": plain words, ...} — the check
    could not run; the caller stays silent unless the owner asked
    for the check by hand.
    """
    try:
        release = fetch_latest_release(urlopen=urlopen)
    except UpdateError as exc:
        return {"state": "unknown", "latest": None, "url": None,
                "reason": str(exc), "notes": "",
                "zip": None, "sidecar": None}
    state = ("newer" if is_newer(release["version"], current_version)
             else "current")
    return {"state": state, "latest": release["version"],
            "url": release["url"], "reason": "",
            "notes": release["notes"], "zip": release["zip"],
            "sidecar": release["sidecar"]}


def _response_total(response, status, resumed_from):
    """The full size of the body being read, from the headers:
    the Content-Range total on a 206, Content-Length otherwise;
    None when the server never said."""
    headers = getattr(response, "headers", None)

    def _get(name):
        try:
            return headers.get(name) if headers is not None else None
        except Exception:
            return None

    if status == 206:
        content_range = _get("Content-Range") or ""
        tail = content_range.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
        length = _get("Content-Length")
        if length is not None and str(length).isdigit():
            return resumed_from + int(length)
        return None
    length = _get("Content-Length")
    if length is not None and str(length).isdigit():
        return int(length)
    return None


def download_file(url, dest, progress=None, cancel_event=None,
                  urlopen=None):
    """Download url to dest, resuming a partial file when one is
    already there (U24): the request carries a Range header for
    the bytes on disk, and a server that ignores it (a plain 200)
    simply restarts the file. progress(done, total) is called as
    bytes land; total is None when the server never said.
    cancel_event is anything with is_set() — a threading.Event in
    the app. Returns the final size in bytes. Raises
    DownloadCancelled when cancelled (the partial stays for the
    next attempt) and UpdateError with plain words for anything
    else."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    opener = urlopen or urllib.request.urlopen
    for attempt in range(2):
        existing = dest.stat().st_size if dest.is_file() else 0
        headers = {}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        request = urllib.request.Request(url, headers=headers)
        try:
            response = opener(request, timeout=DOWNLOAD_TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and existing and attempt == 0:
                # The resume point is past the end of the file
                # the server has: the partial cannot be trusted.
                # Clear it and start the file over, once.
                try:
                    dest.unlink()
                except OSError:
                    pass
                continue
            raise UpdateError(
                f"the server answered {exc.code} for the download")
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise UpdateError("the download timed out")
            raise UpdateError(f"could not reach the server: {reason}")
        except TimeoutError:
            raise UpdateError("the download timed out")
        with response:
            status = getattr(response, "status", None)
            if status is None:
                try:
                    status = response.getcode()
                except Exception:
                    status = 200
            if status == 416 and existing and attempt == 0:
                # Same refusal, delivered as a response instead of
                # an exception (a stub or a proxy can do either).
                try:
                    dest.unlink()
                except OSError:
                    pass
                continue
            if status not in (200, 206):
                raise UpdateError(
                    f"the server answered {status} for the download")
            resumed = existing if status == 206 else 0
            total = _response_total(response, status, resumed)
            done = resumed
            with open(dest, "ab" if resumed else "wb") as fh:
                while True:
                    if (cancel_event is not None
                            and cancel_event.is_set()):
                        raise DownloadCancelled(
                            "the download was cancelled; the part "
                            "already here is kept, and the next "
                            "try resumes from it")
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
            if total is not None and done != total:
                raise UpdateError(
                    f"the download ended early ({done:,} of "
                    f"{total:,} bytes); run it again and it "
                    "resumes from what is already here")
            if progress is not None:
                progress(done, total)
            return done
    raise UpdateError("the download could not be completed")


def make_pending_record(zip_path, sha256, version):
    """The pending-update record's shape, built in one place: the
    zip path, the sha256 it verified against at pull time, and
    the version it will install."""
    return {"zip": str(zip_path), "sha256": sha256,
            "version": version,
            "recorded_at": datetime.now(timezone.utc).isoformat()}


def write_pending_update(data_dir, record) -> None:
    """Record a downloaded, verified update the owner chose to
    install at the next launch (the "No" answer to the restart
    question). Written atomically, like every state file here."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(data_dir / PENDING_NAME, record)


def read_pending_update(data_dir) -> dict | None:
    try:
        record = json.loads(
            (Path(data_dir) / PENDING_NAME).read_text(encoding="utf-8"))
    except Exception:
        return None
    return record if isinstance(record, dict) else None


def clear_pending_update(data_dir) -> None:
    try:
        (Path(data_dir) / PENDING_NAME).unlink()
    except OSError:
        pass


def apply_pending_update(root_dir, data_dir, current_version) -> dict:
    """The next-launch half of the deferred install, run before
    the main window opens. A pending record's zip is re-verified
    against the hash recorded when it was pulled — before
    anything installs it — so a zip swapped or damaged while it
    waited is still caught. Returns {"status", "reason",
    "record"}:

      "none"    — no record; startup proceeds untouched.
      "applied" — verified and handed to the updater; the caller
                  exits without opening the window, and the
                  updater installs and relaunches as usual.
      "dropped" — the record was incomplete, no longer newer
                  (stale), pointed at a missing or unreadable
                  file, or the zip no longer matches the recorded
                  hash: the record is cleared and NOTHING is
                  installed.
      "error"   — verified, but the updater could not be
                  spawned; the record is kept for the next launch.
    """
    record = read_pending_update(data_dir)
    if record is None:
        return {"status": "none", "reason": "", "record": None}

    def dropped(reason):
        clear_pending_update(data_dir)
        return {"status": "dropped", "reason": reason,
                "record": record}

    zip_path = record.get("zip")
    sha256 = record.get("sha256")
    version = record.get("version")
    if (not isinstance(zip_path, str) or not zip_path
            or not isinstance(sha256, str)
            or not _HEX64_RE.match(sha256)):
        return dropped(
            "the pending update record was incomplete, so it "
            "was cleared without installing anything")
    if not is_newer(version, current_version):
        return dropped(
            "the pending update is no longer newer than the "
            "running version, so it was cleared without "
            "installing anything")
    zip_file = Path(zip_path)
    if not zip_file.is_file():
        return dropped(
            "the downloaded update file is gone, so the pending "
            "update was cleared")
    try:
        actual = _hash_file(zip_file)
    except OSError:
        return dropped(
            "the downloaded update file could not be read, so "
            "the pending update was cleared")
    if actual.lower() != sha256.lower():
        return dropped(
            "the downloaded update no longer matches the "
            "checksum it verified with, so it was NOT installed "
            "and the pending record was cleared")
    try:
        spawn_updater(root_dir, data_dir, zip_path)
    except Exception as exc:
        return {"status": "error",
                "reason": "the updater could not be started "
                          f"({exc}); the verified update stays "
                          "pending for the next launch",
                "record": record}
    clear_pending_update(data_dir)
    return {"status": "applied",
            "reason": "installing the verified update "
                      f"(Code Forge {version}) downloaded last "
                      "session",
            "record": record}
