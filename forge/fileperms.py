"""Private-file permission helpers (S2 permission hardening).

Forge's app dir holds the owner's secrets (the vault, connector
config), settings, conversation history and logs.  On posix those
files are born — and kept — readable only by the owner:

* the app dir itself is mode 0700,
* every app-data file is mode 0600.

All helpers are fail-soft and posix-gated: a chmod that fails (an
odd filesystem, a non-posix host) never breaks the write it guards,
and on non-posix systems they are no-ops, matching the vault's
existing posture.  Costs are one syscall per write — every caller
here writes rarely (settings saves, vault writes, log creation),
never on a hot path.
"""
from __future__ import annotations

import os
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


def chmod_private(path) -> None:
    """Best-effort chmod 0600 on one file (posix only)."""
    if os.name != "posix":
        return
    try:
        os.chmod(path, FILE_MODE)
    except OSError:
        pass


def ensure_private_dir(path) -> Path:
    """Create a directory (with parents) and make it 0700."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(path, DIR_MODE)
        except OSError:
            pass
    return path


def write_text_private(path, text: str, encoding: str = "utf-8") -> None:
    """Write text atomically with private permissions.

    The temp file lives in the same folder, is unique to this
    process (Sweep R41 — two writers never share one .tmp path), is
    created via os.open at 0600 (born private, never briefly
    world-readable), is flushed and fsynced before the replace (a
    power cut right after save cannot leave an empty file the
    atomic replace made official), and is cleaned up when the
    write fails — the pid-unique tmp + fsync + atomic replace
    shape save_settings and the connectors write already used
    (R40/R41/R48), plus the owner-only mode."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    ok = False
    try:
        data = text.encode(encoding)
        while data:
            written = os.write(fd, data)
            data = data[written:]
        os.fsync(fd)
        ok = True
    finally:
        os.close(fd)
        if not ok:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    chmod_private(tmp)
    tmp.replace(path)
    chmod_private(path)


def ensure_private_file(path) -> None:
    """chmod 0600 a file that another writer (sqlite, an appending
    log) creates and owns — called after the write completes, so the
    end state on disk is private."""
    if Path(path).exists():
        chmod_private(path)
