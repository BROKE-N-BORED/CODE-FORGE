#!/usr/bin/env python3
"""CODE FORGE standalone updater — Note 19 (staged install, audit A7).

Runs OUTSIDE the app (the app launches this, then quits). A running
program cannot safely overwrite its own code or its own virtualenv;
this script waits for the app to exit, then:

  1. extracts the overlay zip into a STAGING folder (code paths only,
     path-escape entries refused) — the live app folder is untouched,
  2. refuses the update if nothing landed or the staged code does not
     byte-compile — a bad zip can no longer leave a mixed tree behind,
  3. backs up the current code, then swaps the staged code in as a
     SYNC (files removed from the new version actually disappear),
  4. installs dependencies into the app's .venv (requirements.txt plus
     any extra pip packages the manifest declares) — any failure after
     the swap begins restores the backup and relaunches the old code,
  5. writes a result file the relaunched app reports to the owner,
  6. relaunches the app, which restores the resume state.

Stdlib only. Usage: forge_updater.py --manifest <manifest.json>
Manifest: {app_dir, data_dir, zip|null, wait_pid|null, extra_pip: []}
"""
from __future__ import annotations
import argparse
import json
import os
import py_compile
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ALLOWED_TOPS = {"forge", "plugins", "skills", "commands"}
ALLOWED_ROOT_FILES = {"README.md", "install.sh", "run.sh", "requirements.txt",
                      "forge_updater.py"}

# Sweep R160: extraction ceilings. Nothing ever bounded what an
# update zip could expand to — a zip bomb (or a lying header) could
# fill the disk from inside a candidate. A real Forge build is a
# few MB; these ceilings are far above it and far below a disk.
_MAX_ENTRY_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024


def wait_for_pid(pid: int | None, timeout: float = 90.0) -> None:
    if not pid:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            # Sweep R162: the process EXISTS but may not be
            # signalled (owned by another user) — the old code
            # treated that as "exited" and swapped code under a
            # running app. Keep waiting until the deadline instead.
            pass
        time.sleep(0.5)


def write_result(data_dir: Path, result: dict) -> None:
    """Write update_result.json atomically (temp file + replace), so a
    crash mid-write can never leave the app a torn result to read."""
    path = Path(data_dir) / "update_result.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
    tmp.replace(path)


def safe_extract(zip_path: Path, staging_dir: Path, steps: list[str],
                 top_name: str) -> int:
    """Extract the zip's code entries into staging_dir; returns the
    number of files actually written. Entry rules are unchanged from
    the old overlay extract: only ALLOWED_TOPS folders and
    ALLOWED_ROOT_FILES at the root land, escapes are refused."""
    staging_dir = staging_dir.resolve()
    landed = 0
    total_bytes = 0
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            if name.endswith("/"):
                continue
            parts = Path(name).parts
            if parts and parts[0] == top_name:
                parts = parts[1:]
            if not parts or parts[0] in ("..", "/") or os.path.isabs(name):
                steps.append(f"REFUSED unsafe entry: {name}")
                continue
            if len(parts) > 1 and parts[0] not in ALLOWED_TOPS:
                steps.append(f"SKIPPED non-code entry: {name}")
                continue
            if len(parts) == 1 and parts[0] not in ALLOWED_ROOT_FILES:
                steps.append(f"SKIPPED non-code entry: {name}")
                continue
            dest = staging_dir.joinpath(*parts)
            if staging_dir not in dest.resolve().parents and dest.resolve() != staging_dir:
                steps.append(f"REFUSED escape entry: {name}")
                continue
            if info.file_size > _MAX_ENTRY_BYTES:
                # Sweep R160: declared-size refusal (the streamed
                # count below still guards a header that lies).
                steps.append(f"REFUSED oversized entry: {name}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            try:
                with z.open(info) as src, dest.open("wb") as out:
                    while True:
                        chunk = src.read(262144)
                        if not chunk:
                            break
                        written += len(chunk)
                        total_bytes += len(chunk)
                        if (written > _MAX_ENTRY_BYTES
                                or total_bytes > _MAX_TOTAL_BYTES):
                            raise RuntimeError(
                                "update zip expands past the size "
                                f"ceiling at entry {name} — refused")
                        out.write(chunk)
            except BaseException:
                dest.unlink(missing_ok=True)
                raise
            landed += 1
    steps.append(f"staged {landed} code file(s) from the update zip")
    return landed


def verify_staged(staging_dir: Path, steps: list[str]) -> None:
    """Byte-compile every staged .py. Broken code fails the update
    here, while the live app folder is still untouched."""
    checked = 0
    for py in sorted(staging_dir.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        try:
            py_compile.compile(str(py), doraise=True)
        except py_compile.PyCompileError as exc:
            raise RuntimeError(
                f"staged code does not compile: "
                f"{py.relative_to(staging_dir)} — {str(exc)[:300]}")
        checked += 1
    steps.append(f"staged code verified ({checked} python file(s) compile)")


def staged_entries(staging_dir: Path) -> tuple[list[str], list[str]]:
    """(top dirs, root files) the swap may touch — filtered to the
    allowed set, so nothing outside it can ever be swapped in."""
    tops = sorted(p.name for p in staging_dir.iterdir()
                  if p.is_dir() and p.name in ALLOWED_TOPS)
    files = sorted(p.name for p in staging_dir.iterdir()
                   if p.is_file() and p.name in ALLOWED_ROOT_FILES)
    return tops, files


def backup_current(app_dir: Path, backup_dir: Path, tops: list[str],
                   files: list[str], steps: list[str]) -> None:
    """Copy the app's current versions of everything the swap will
    replace into backup_dir (fresh each update)."""
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    for top in tops:
        src = app_dir / top
        if src.is_dir():
            shutil.copytree(src, backup_dir / top)
    for name in files:
        src = app_dir / name
        if src.is_file():
            shutil.copy2(src, backup_dir / name)
    steps.append("previous code backed up")


def swap_in(app_dir: Path, staging_dir: Path, tops: list[str],
            files: list[str], steps: list[str]) -> None:
    """Replace the app's code with the staged code as a sync: each
    staged top folder replaces the app's wholesale (so files deleted
    in the new version disappear), root files are replaced one by one.
    .venv and everything outside the allowed set is never touched.

    Sweep R161: a top folder is swapped by RENAME, not by deleting
    the live folder and copying over it — the old way left a window
    where the app's code folder simply did not exist, and a crash or
    power loss inside it bricked the install (the backup restore
    only runs when the updater itself survives to run it). The old
    folder moves aside, the new one copies in, and any failure puts
    the old one back; a leftover aside whose live folder is missing
    (an earlier swap died mid-flight) is renamed home first."""
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for top in sorted(ALLOWED_TOPS):
        aside = app_dir / f".swap-old-{top}"
        dest = app_dir / top
        if aside.exists() and not dest.exists():
            os.rename(aside, dest)  # an earlier swap died mid-flight
    for top in tops:
        dest = app_dir / top
        aside = app_dir / f".swap-old-{top}"
        if aside.exists():
            shutil.rmtree(aside)  # stale copy beside a live folder
        try:
            if dest.exists():
                os.rename(dest, aside)
            shutil.copytree(staging_dir / top, dest, ignore=ignore)
        except BaseException:
            if dest.is_symlink() or dest.is_file():
                dest.unlink(missing_ok=True)
            elif dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            if aside.exists() and not dest.exists():
                os.rename(aside, dest)
            raise
        if aside.exists():
            shutil.rmtree(aside, ignore_errors=True)
    for name in files:
        shutil.copy2(staging_dir / name, app_dir / name)
    steps.append("staged code installed")


def restore_backup(app_dir: Path, backup_dir: Path, tops: list[str],
                   files: list[str], steps: list[str]) -> None:
    """Put the backed-up code back exactly: backed-up tops/files are
    restored; tops/files the failed update newly added are removed."""
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for top in tops:
        dest = app_dir / top
        if dest.exists():
            shutil.rmtree(dest)
        saved = backup_dir / top
        if saved.is_dir():
            shutil.copytree(saved, dest, ignore=ignore)
    for name in files:
        saved = backup_dir / name
        dest = app_dir / name
        if saved.is_file():
            shutil.copy2(saved, dest)
        elif dest.exists():
            dest.unlink()
    steps.append("previous code restored from backup")


def install_deps(app_dir: Path, extra_pip: list[str], steps: list[str]) -> None:
    venv = app_dir / ".venv"
    python = venv / "bin" / "python"
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True,
                       capture_output=True, text=True, timeout=600)
        steps.append("virtualenv created")
    req = app_dir / "requirements.txt"
    cmds = []
    if req.is_file():
        cmds.append([str(python), "-m", "pip", "install", "-r", str(req)])
    if extra_pip:
        cmds.append([str(python), "-m", "pip", "install", *extra_pip])
    for cmd in cmds:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        tail = (done.stdout + done.stderr).strip().splitlines()[-3:]
        steps.append(f"pip ({' '.join(cmd[3:])[:60]}): exit {done.returncode} — " + " | ".join(tail)[:300])
        if done.returncode != 0:
            raise RuntimeError(f"pip step failed: {' '.join(cmd)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    manifest_path = Path(args.manifest)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        app_dir = Path(manifest["app_dir"])
        data_dir = Path(manifest["data_dir"])
    except Exception as exc:
        # The app has already quit for this update. app_dir is unknown,
        # so no relaunch is possible — but the manifest lives in
        # data_dir, so a result written beside it is the one place the
        # app will look when the owner starts it again.
        result = {"ok": False, "steps": [],
                  "error": f"could not read update manifest: {exc}"[:500]}
        try:
            write_result(manifest_path.parent, result)
        except Exception:
            pass
        return 1
    data_dir.mkdir(parents=True, exist_ok=True)
    result = {"ok": False, "steps": [], "error": ""}
    steps = result["steps"]
    staging_dir = data_dir / "update_staging"
    backup_dir = data_dir / "update_backup"
    swap_begun = False
    staged_tops: list[str] = []
    staged_files: list[str] = []
    try:
        wait_for_pid(manifest.get("wait_pid"))
        if manifest.get("zip"):
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            staging_dir.mkdir(parents=True, exist_ok=True)
            landed = safe_extract(Path(manifest["zip"]), staging_dir,
                                  steps, app_dir.name)
            if landed == 0:
                raise RuntimeError(
                    "update zip installed no code files (wrong top "
                    "folder?) — app folder left untouched")
            verify_staged(staging_dir, steps)
            staged_tops, staged_files = staged_entries(staging_dir)
            backup_current(app_dir, backup_dir, staged_tops, staged_files,
                           steps)
            swap_begun = True
            swap_in(app_dir, staging_dir, staged_tops, staged_files, steps)
        install_deps(app_dir, list(manifest.get("extra_pip", [])), steps)
        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)[:500]
        if swap_begun:
            try:
                restore_backup(app_dir, backup_dir, staged_tops,
                               staged_files, steps)
                steps.append("update failed after the install began — "
                             "rolled back to the previous code")
            except Exception as rb_exc:
                steps.append(f"ROLLBACK FAILED: {rb_exc}")
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
    write_result(data_dir, result)
    python = app_dir / ".venv" / "bin" / "python"
    relaunch = [str(python), "-m", "forge.main"] if python.exists() else ["bash", str(app_dir / "run.sh")]
    try:
        subprocess.Popen(relaunch, cwd=app_dir, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        result["ok"] = False
        result["error"] = f"relaunch failed: {exc}"
        write_result(data_dir, result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
