"""Shadow checkpoints — Audit D38 (pure stdlib, no Qt, like
planning.py / snapshots.py).

Per-step restore points for ANY project, built on a SHADOW git
repo: a repo that lives OUTSIDE the project, under the app data
dir (``<app_dir>/checkpoints/<hash of the project path>``), with
its work-tree pointed at the project folder. The project's own
.git — if it has one — is never read, written, or committed to by
anything here; the shadow is a second, private history the Forge
keeps alongside the project.

How it differs from forge/snapshots.py (Note 14): snapshots are
the OWNER's restore points on the project's own git — tracking
starts at his explicit click, in the Snapshots dialog. Checkpoints
are the AGENT-TURN safety net at finer granularity: with the
owner's `shadow_checkpoints` setting on, the worker lays one down
per write step and/or per turn, for tracked and untracked projects
alike. Default is off — an owner who never enables it pays no disk
and sees no behavior change.

Mechanism: every git call runs with GIT_DIR=<shadow> and
GIT_WORK_TREE=<project> in the subprocess environment (plus ``-C
<project>``), argument lists only, no shell. Because the shadow's
index tracks the project folder, `git add -A` + commit captures a
step, and restoring is the snapshots-proven swap: a forced safety
checkpoint of the current state first, then `read-tree --reset -u`
(the tree is swapped, never the history — HEAD never moves, so the
checkpoint list stays complete and every restore is itself
undoable from the same list), then `git clean -fd` removes files
the target checkpoint never heard of — that removal is the point
of a per-step undo. Ignored files are touched by neither half.

The project's own `.git` is protected twice over: git itself
refuses `.git` path components in an index, and the shadow's
info/exclude seeds ".git/" (on top of snapshots' junk defaults),
so the project's repo is ignored content as far as the shadow is
concerned — never staged, never cleaned.

Commits never depend on the owner's git configuration: when the
shadow repo has no usable user.name/user.email, a Forge-local
identity is passed per-command via ``git -c``. Signing is off for
these commits, exactly as in snapshots.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from .snapshots import EXCLUDE_DEFAULTS

GIT_NAME = "Code Forge"
GIT_EMAIL = "forge@localhost"
SAFETY_LABEL = "Safety — before checkpoint restore"
_GIT_TIMEOUT = 120  # seconds; a hung git must fail, not freeze.

# The shadow's exclude seed: snapshots' junk defaults (so a .venv
# never enters checkpoint history) plus the load-bearing ".git/" —
# see the module docstring. The header matches snapshots' so the
# two files read as one convention.
_EXCLUDE_HEADER = "# Code Forge snapshot defaults"
_SHADOW_EXCLUDES = [*EXCLUDE_DEFAULTS, ".git/"]


class CheckpointError(Exception):
    """A git failure in plain language — the UI shows it verbatim."""


def git_available(git: str = "git") -> bool:
    """True when a git executable answers. *git* is injectable so the
    dialog (and tests) can point at a missing binary honestly."""
    if shutil.which(git) is None and not Path(git).is_file():
        return False
    try:
        proc = subprocess.run([git, "--version"], capture_output=True,
                              text=True, timeout=10)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def default_label() -> str:
    """The label a checkpoint gets when the caller doesn't name one."""
    return f"Checkpoint — {_now_stamp()}"


def _clean_label(label: str) -> str:
    """One line, collapsed whitespace: labels live in commit subjects,
    and a newline would corrupt the log parsing in list_checkpoints."""
    return " ".join(str(label or "").split())


def shadow_dir(app_dir, workspace) -> Path:
    """The shadow repo's folder for *workspace*: keyed by the first
    16 hex chars of the sha256 of the resolved project path, so two
    projects never share a shadow and a moved project gets a fresh
    one (its old checkpoints stay behind, orphaned but harmless)."""
    key = hashlib.sha256(
        str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:16]
    return Path(app_dir) / "checkpoints" / key


def _env(shadow: Path, workspace: Path) -> dict:
    env = dict(os.environ)
    env["GIT_DIR"] = str(shadow)
    env["GIT_WORK_TREE"] = str(workspace)
    return env


def _run_git(workspace: Path, shadow: Path, git: str, args: list[str],
             what: str) -> str:
    """Run one git command against the shadow (GIT_DIR/GIT_WORK_TREE
    in the environment) and return stdout, or raise CheckpointError
    with git's own words trimmed into a plain sentence."""
    try:
        proc = subprocess.run(
            [git, "-C", str(workspace), *args],
            env=_env(shadow, workspace),
            capture_output=True, text=True, timeout=_GIT_TIMEOUT)
    except FileNotFoundError:
        raise CheckpointError("Git isn't installed on this PC, so checkpoints can't run.")
    except subprocess.TimeoutExpired:
        raise CheckpointError(f"{what}: git took too long and was stopped.")
    except OSError as exc:
        raise CheckpointError(f"{what}: {exc}")
    if proc.returncode != 0:
        said = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = said[-1].strip() if said else f"git exited {proc.returncode}"
        raise CheckpointError(f"{what}: {detail}")
    return proc.stdout


def _seed_shadow_excludes(shadow: Path) -> None:
    """Best-effort: seed the shadow's info/exclude with
    _SHADOW_EXCLUDES. Existing lines are never removed or
    duplicated. Failure is swallowed on purpose (the same reasoning
    as snapshots' seeding): the excludes are quiet protection, and
    an odd shadow layout must never stop a checkpoint."""
    try:
        exclude = shadow / "info" / "exclude"
        existing = (exclude.read_text(encoding="utf-8")
                    if exclude.is_file() else "")
        present = {line.strip() for line in existing.splitlines()}
        missing = [p for p in _SHADOW_EXCLUDES if p not in present]
        if not missing:
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        text = "" if not existing or existing.endswith("\n") else "\n"
        text += _EXCLUDE_HEADER + "\n" + "\n".join(missing) + "\n"
        with exclude.open("a", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass


def ensure_shadow(app_dir, workspace, git: str = "git") -> Path:
    """Create *workspace*'s shadow repo if it doesn't exist yet and
    return its folder. Idempotent: an existing shadow is only
    re-seeded (append-missing) and returned. The repo's
    core.worktree points at the project folder; every operation
    here also passes GIT_WORK_TREE explicitly, so the two can never
    disagree."""
    ws = Path(workspace)
    if not ws.is_dir():
        raise CheckpointError(f"That project folder doesn't exist: {ws}")
    if not git_available(git):
        raise CheckpointError("Git isn't installed on this PC, so checkpoints can't run.")
    shadow = shadow_dir(app_dir, ws)
    if not (shadow / "HEAD").is_file():
        shadow.mkdir(parents=True, exist_ok=True)
        # The shadow folder IS the git dir (HEAD, objects/, config
        # live directly inside it), so init runs with GIT_DIR set —
        # plain `git init <dir>` would nest a .git inside it instead.
        env = dict(os.environ)
        env["GIT_DIR"] = str(shadow)
        try:
            proc = subprocess.run(
                [git, "init"], env=env,
                capture_output=True, text=True, timeout=_GIT_TIMEOUT)
        except FileNotFoundError:
            raise CheckpointError("Git isn't installed on this PC, so checkpoints can't run.")
        except subprocess.TimeoutExpired:
            raise CheckpointError("Could not create the checkpoint store: git took too long and was stopped.")
        except OSError as exc:
            raise CheckpointError(f"Could not create the checkpoint store: {exc}")
        if proc.returncode != 0:
            said = (proc.stderr or proc.stdout or "").strip().splitlines()
            detail = said[-1].strip() if said else f"git exited {proc.returncode}"
            raise CheckpointError(f"Could not create the checkpoint store: {detail}")
        try:
            proc = subprocess.run(
                [git, "config", "core.worktree", str(ws.resolve())],
                env=env, capture_output=True, text=True, timeout=10)
        except OSError as exc:
            raise CheckpointError(f"Could not point the checkpoint store at this project: {exc}")
        if proc.returncode != 0:
            raise CheckpointError("Could not point the checkpoint store at this project.")
    _seed_shadow_excludes(shadow)
    return shadow


def _has_head(workspace: Path, shadow: Path, git: str) -> bool:
    try:
        proc = subprocess.run(
            [git, "-C", str(workspace), "rev-parse", "--verify", "HEAD"],
            env=_env(shadow, workspace),
            capture_output=True, text=True, timeout=10)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _commit(workspace: Path, shadow: Path, git: str, label: str,
            allow_empty: bool = False) -> str:
    """Commit whatever is staged in the shadow and return the new
    commit id. Mirrors snapshots' identity discipline: the Forge
    identity rides per-command ONLY when the repo has no usable
    identity of its own, and signing is off (a fresh PC with
    gpgsign on would hang on a prompt this subprocess cannot
    answer)."""
    flags: list[str] = []
    name = email = ""
    try:
        name = _run_git(workspace, shadow, git, ["config", "user.name"], "Reading git identity").strip()
        email = _run_git(workspace, shadow, git, ["config", "user.email"], "Reading git identity").strip()
    except CheckpointError:
        name = email = ""
    if not name or not email:
        flags += ["-c", f"user.name={GIT_NAME}", "-c", f"user.email={GIT_EMAIL}"]
    flags += ["-c", "commit.gpgsign=false"]
    args = [*flags, "commit", "-m", label]
    if allow_empty:
        args.append("--allow-empty")
    out = _run_git(workspace, shadow, git, args, "Could not save the checkpoint")
    head = _run_git(workspace, shadow, git, ["rev-parse", "HEAD"],
                    "Reading the checkpoint id")
    return head.strip() or out.strip()


def checkpoint(app_dir, workspace, label, git: str = "git",
               force: bool = False) -> dict | None:
    """Stage everything (as the shadow's excludes allow) and commit
    it as a checkpoint. Returns {"id", "label", "created": True}
    when a commit was made, or None when nothing has changed since
    the last checkpoint — per-step mode would otherwise fill the
    list with empty noise. With `force=True` (the restore safety
    net) a labeled commit is made anyway, so the safety point
    always exists in the list under its own label."""
    ws = Path(workspace)
    shadow = ensure_shadow(app_dir, ws, git)
    label = _clean_label(label) or default_label()
    _run_git(ws, shadow, git, ["add", "-A"],
             "Could not gather the project's files")
    staged = subprocess.run(
        [git, "-C", str(ws), "diff", "--cached", "--quiet"],
        env=_env(shadow, ws), capture_output=True, timeout=_GIT_TIMEOUT)
    nothing_staged = staged.returncode == 0
    if nothing_staged and _has_head(ws, shadow, git) and not force:
        return None
    commit_id = _commit(ws, shadow, git, label, allow_empty=nothing_staged)
    return {"id": commit_id, "label": label, "created": True}


def list_checkpoints(app_dir, workspace, git: str = "git") -> list[dict]:
    """Checkpoints newest-first: {"id", "label", "timestamp"}.
    A project with no shadow yet (or no checkpoints in it) lists
    empty, quietly — listing never creates the shadow."""
    ws = Path(workspace)
    if not ws.is_dir() or not git_available(git):
        return []
    shadow = shadow_dir(app_dir, ws)
    if not (shadow / "HEAD").is_file() or not _has_head(ws, shadow, git):
        return []
    out = _run_git(ws, shadow, git,
                   ["log", "--format=%H%x1f%ct%x1f%s"],
                   "Could not read the checkpoint list")
    entries = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        full, epoch, subject = parts
        try:
            when = datetime.fromtimestamp(int(epoch)).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            when = ""
        entries.append({"id": full, "label": subject, "timestamp": when})
    return entries


def restore_checkpoint(app_dir, workspace, commit_id, git: str = "git") -> dict:
    """Put the project's files back to *commit_id*'s checkpoint.

    First a safety checkpoint of the current state is taken (forced,
    item 4's law) — if that fails, this raises and NOTHING is
    restored. Then the index and working tree are swapped to the
    checkpoint's tree (read-tree never moves HEAD) and `git clean
    -fd` removes files created after the checkpoint — that removal
    is the point of a per-step undo. Ignored files (the seeded
    excludes, including the project's own .git) are touched by
    neither half. Returns {"restored": <full id>, "safety": <the
    safety checkpoint's id>}. A commit id that isn't in this
    project's list raises CheckpointError."""
    ws = Path(workspace)
    if not ws.is_dir():
        raise CheckpointError(f"That project folder doesn't exist: {ws}")
    shadow = ensure_shadow(app_dir, ws, git)
    # Sweep R139: the id must name ONE checkpoint unambiguously.
    # The old loop took the FIRST entry whose id starts with the
    # given text — and startswith("") is True, so an empty id (or a
    # short prefix shared by several) silently restored whichever
    # checkpoint the list happened to put first, then ran the
    # destructive read-tree + clean against it. An exact id wins;
    # a prefix must match exactly one entry.
    wanted = str(commit_id or "")
    if not wanted:
        raise CheckpointError("No checkpoint id given — pick one from the list.")
    entries = list_checkpoints(app_dir, ws, git)
    exact = [e["id"] for e in entries if e["id"] == wanted]
    prefixed = [e["id"] for e in entries if e["id"].startswith(wanted)]
    if exact:
        target = exact[0]
    elif len(prefixed) == 1:
        target = prefixed[0]
    elif not prefixed:
        raise CheckpointError("That checkpoint isn't in this project's list any more.")
    else:
        raise CheckpointError(
            f"'{wanted}' matches {len(prefixed)} checkpoints — "
            "give more of the id to pick one.")
    safety = checkpoint(app_dir, ws, SAFETY_LABEL, git, force=True)
    _run_git(ws, shadow, git, ["read-tree", "--reset", "-u", target],
             "Could not restore the checkpoint")
    _run_git(ws, shadow, git, ["clean", "-fd"],
             "Could not clear files created after the checkpoint")
    return {"restored": target, "safety": safety["id"] if safety else ""}
