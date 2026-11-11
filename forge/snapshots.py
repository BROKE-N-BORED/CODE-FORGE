"""Git snapshots + rollback — Note 14 item 1 (pure stdlib, no Qt,
like planning.py / skribe.py).

Owner-facing restore points for the CURRENT project, built on plain
git. A snapshot is a commit of everything in the project folder (as
.gitignore allows — ignored files are never staged, snapshotted, or
touched by a restore); restoring one puts the folder back the way it
was. Everything here is local: no remotes are ever read, added, or
contacted, and no command ever reaches outside the project folder
(``git -C <folder>`` on every call, argument lists only, no shell).

Safety rules, enforced here rather than trusted to the caller:

* A folder counts as a snapshot repo only when it IS the repo root.
  A project nested inside some other repo (e.g. a home-directory
  repo) is reported as not-a-repo, because staging/committing from
  the parent would reach files outside the project folder.
* Every restore first takes a safety snapshot of the current state
  (label prefix ``SAFETY_PREFIX``). If that fails, the restore does
  not proceed — there is no path where a restore loses work that a
  later click cannot bring back.
* A restore is itself recorded as a new snapshot on top of history
  (the tree is swapped, never the history), so the snapshot list
  stays complete and every restore is undoable from the same list.
* Commits never depend on the owner's git configuration: when the
  repo has no usable user.name/user.email, a Forge-local identity is
  passed per-command via ``git -c``. Nothing is ever written to the
  owner's global (or the repo's) config.
* A repo the Forge itself starts gets junk-folder excludes seeded
  into .git/info/exclude (init_repo), and preflight() reports what
  the FIRST snapshot would swallow — large files, likely secrets,
  a huge file count — so the dialog can warn before one click puts
  them in git history for good.
"""
from __future__ import annotations

import fnmatch
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

GIT_NAME = "Code Forge"
GIT_EMAIL = "forge@localhost"
SAFETY_PREFIX = "Safety snapshot — before restore"
RESTORED_PREFIX = "Restored: "
_GIT_TIMEOUT = 120  # seconds; a small project's git ops are fast,
# and a hung git (e.g. a credential prompt) must fail, not freeze.

# A repo the Forge itself starts tracking gets local git excludes
# for the usual junk folders, so they never enter snapshot history.
# They live in .git/info/exclude — the owner's own .gitignore stays
# entirely theirs.
EXCLUDE_DEFAULTS = [".venv/", "venv/", "node_modules/", "__pycache__/",
                    "*.pyc", ".DS_Store", "dist/", "build/"]
_EXCLUDE_HEADER = "# Code Forge snapshot defaults"

# First-snapshot preflight thresholds — see preflight().
_LARGE_FILE_BYTES = 10 * 1024 * 1024
_MANY_FILES = 2000
_SECRET_PATTERNS = (".env", ".env.*", "credentials*", "*.pem", "*.key",
                    "*.p12", "id_rsa*")


class SnapshotError(Exception):
    """A git failure in plain language — the UI shows it verbatim."""


def _now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def default_label() -> str:
    """The label a snapshot gets when the owner doesn't type one."""
    return f"Snapshot — {_now_stamp()}"


def _clean_label(label: str) -> str:
    """One line, collapsed whitespace: labels live in commit subjects,
    and a newline would corrupt the log parsing in list_snapshots."""
    return " ".join(str(label or "").split())


def _run_git(folder, git: str, args: list[str], what: str) -> str:
    """Run one git command in *folder* and return stdout, or raise
    SnapshotError with git's own words trimmed into a plain sentence."""
    try:
        proc = subprocess.run(
            [git, "-C", str(folder), *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT)
    except FileNotFoundError:
        raise SnapshotError("Git isn't installed on this PC, so snapshots can't run.")
    except subprocess.TimeoutExpired:
        raise SnapshotError(f"{what}: git took too long and was stopped.")
    except OSError as exc:
        raise SnapshotError(f"{what}: {exc}")
    if proc.returncode != 0:
        said = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = said[-1].strip() if said else f"git exited {proc.returncode}"
        raise SnapshotError(f"{what}: {detail}")
    return proc.stdout


def _folder(folder) -> Path:
    path = Path(folder)
    if not path.is_dir():
        raise SnapshotError(f"That project folder doesn't exist: {path}")
    return path


_GIT_AVAILABLE_TTL = 30.0
_git_available_cache: dict = {}


def git_available(git: str = "git") -> bool:
    """True when a git executable answers. *git* is injectable so the
    dialog (and tests) can point at a missing binary honestly.

    Sweep R123: the answer is cached for 30 seconds per binary path
    — a panel scan used to spawn `git --version` for every one of
    its ~7 probes (repo_state + per-file calls each re-asked). The
    short TTL still recovers on its own if git is installed
    mid-session."""
    now = time.monotonic()
    hit = _git_available_cache.get(git)
    if hit is not None and now - hit[0] < _GIT_AVAILABLE_TTL:
        return hit[1]
    answer = _git_available_probe(git)
    _git_available_cache[git] = (now, answer)
    return answer


def _git_available_probe(git: str = "git") -> bool:
    if shutil.which(git) is None and not Path(git).is_file():
        return False
    try:
        proc = subprocess.run([git, "--version"], capture_output=True,
                              text=True, timeout=10)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def repo_root(folder, git: str = "git") -> Path | None:
    """The repo's top-level folder if *folder* is inside one, else
    None. Quiet by design: probing repo state is not an error."""
    path = Path(folder)
    if not path.is_dir() or not git_available(git):
        return None
    try:
        proc = subprocess.run(
            [git, "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    top = proc.stdout.strip()
    return Path(top) if top else None


def is_repo(folder, git: str = "git") -> bool:
    """True only when *folder* itself is the root of its repo — see
    the module docstring for why nested folders don't count."""
    root = repo_root(folder, git)
    return root is not None and root.resolve() == Path(folder).resolve()


def repo_state(folder, git: str = "git") -> dict:
    """Everything the dialog needs to pick a face:
    {"git": bool, "repo": bool, "folder": str}."""
    return {"git": git_available(git), "repo": is_repo(folder, git),
            "folder": str(folder)}


def init_repo(folder, git: str = "git") -> None:
    """Start tracking *folder* with git. The owner's click is the only
    way here — nothing in the app calls this automatically."""
    path = _folder(folder)
    if not git_available(git):
        raise SnapshotError("Git isn't installed on this PC, so snapshots can't run.")
    _run_git(path, git, ["init"], "Could not start tracking this project")
    _seed_exclude_defaults(path)


def _seed_exclude_defaults(path: Path) -> None:
    """Best-effort: seed .git/info/exclude with EXCLUDE_DEFAULTS in a
    repo the Forge just started. Existing lines (git's template
    comments, anything the owner added) are never removed or
    duplicated, and repos the Forge did not init are never touched —
    this runs only from init_repo.

    Failure is swallowed on purpose: the excludes are quiet
    protection, and a locked or odd .git layout must never stop the
    owner from tracking their project. preflight() is the loud layer
    — it warns before anything big or sensitive is snapshotted."""
    try:
        exclude = path / ".git" / "info" / "exclude"
        existing = (exclude.read_text(encoding="utf-8")
                    if exclude.is_file() else "")
        present = {line.strip() for line in existing.splitlines()}
        missing = [p for p in EXCLUDE_DEFAULTS if p not in present]
        if not missing:
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        text = "" if not existing or existing.endswith("\n") else "\n"
        text += _EXCLUDE_HEADER + "\n" + "\n".join(missing) + "\n"
        with exclude.open("a", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass


def _human_size(num_bytes: int) -> str:
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / 1024 ** 3:.1f} GB"
    return f"{num_bytes / 1024 ** 2:.0f} MB"


def _looks_secret(basename: str) -> bool:
    name = basename.lower()
    return any(fnmatch.fnmatchcase(name, pat) for pat in _SECRET_PATTERNS)


def preflight(folder, git: str = "git") -> list[str]:
    """Warnings about what the FIRST snapshot of *folder* would
    swallow, in plain language — pure data, no UI. Empty when git is
    unavailable, the folder isn't a snapshot repo, the repo already
    has a snapshot (preflight guards only the first one), the check
    itself fails, or nothing is flagged.

    The would-add list comes from `git add -A --dry-run`, so ignore
    rules (including the seeded .git/info/exclude defaults) apply —
    excluded junk never appears here and never triggers a warning."""
    path = Path(folder)
    if not git_available(git) or not is_repo(path, git) \
            or _has_head(path, git):
        return []
    try:
        out = _run_git(path, git, ["add", "-A", "--dry-run"],
                       "Could not check what a snapshot would gather")
    except SnapshotError:
        return []
    would_add = []
    for line in out.splitlines():
        if line.startswith("add '") and line.endswith("'") \
                and len(line) > len("add ''"):
            would_add.append(line[len("add '"):-1])
    if not would_add:
        return []
    large: list[tuple[str, int]] = []
    secrets: list[str] = []
    for rel in would_add:
        base = Path(rel).name
        if _looks_secret(base) and base not in secrets:
            secrets.append(base)
        try:
            size = (path / rel).stat().st_size
        except OSError:
            continue
        if size > _LARGE_FILE_BYTES:
            large.append((rel, size))
    warnings = []
    if large:
        named = ", ".join(f"{rel} ({_human_size(size)})"
                          for rel, size in large[:5])
        if len(large) > 5:
            named += f", + {len(large) - 5} more"
        warnings.append(
            f"Large files would enter snapshot history: {named}")
    if secrets:
        named = ", ".join(secrets[:5])
        if len(secrets) > 5:
            named += f", + {len(secrets) - 5} more"
        warnings.append(
            f"Files that look like secrets would be snapshotted: {named}"
            " — git history keeps them even if deleted later.")
    if len(would_add) > _MANY_FILES:
        warnings.append(
            f"This snapshot would track {len(would_add)} files — an "
            "un-ignored dependency folder (.venv, node_modules) is the "
            "usual cause.")
    return warnings


def _has_head(path: Path, git: str) -> bool:
    try:
        proc = subprocess.run(
            [git, "-C", str(path), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=10)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _commit(path: Path, git: str, label: str, allow_empty: bool = False) -> str:
    """Commit whatever is staged and return the new commit id. The
    Forge identity rides per-command ONLY when the repo has no usable
    identity of its own — an owner with their own git identity keeps
    it, and the owner's config files are never written either way.
    Signing is off for these commits: a fresh PC with gpgsign on
    would hang on a prompt this subprocess cannot answer."""
    flags: list[str] = []
    name = email = ""
    try:
        name = _run_git(path, git, ["config", "user.name"], "Reading git identity").strip()
        email = _run_git(path, git, ["config", "user.email"], "Reading git identity").strip()
    except SnapshotError:
        name = email = ""
    if not name or not email:
        flags += ["-c", f"user.name={GIT_NAME}", "-c", f"user.email={GIT_EMAIL}"]
    flags += ["-c", "commit.gpgsign=false"]
    args = [*flags, "commit", "-m", label]
    if allow_empty:
        args.append("--allow-empty")
    out = _run_git(path, git, args, "Could not save the snapshot")
    head = _run_git(path, git, ["rev-parse", "HEAD"], "Reading the snapshot id")
    return head.strip() or out.strip()


def take_snapshot(folder, label: str = "", git: str = "git",
                  force: bool = False) -> dict:
    """Stage everything (as .gitignore allows) and commit it as a
    snapshot. Returns {"id", "label", "created"}: when nothing has
    changed since the last snapshot, `created` is False and `id` is
    that snapshot — the current state is already captured. With
    `force=True` (the restore safety net) a labeled commit is made
    anyway, so the safety point always exists in the list under its
    own label even when its tree equals the previous snapshot's."""
    path = _folder(folder)
    if not is_repo(path, git):
        raise SnapshotError("This project isn't being tracked yet — start tracking it first.")
    label = _clean_label(label) or default_label()
    _run_git(path, git, ["add", "-A"], "Could not gather the project's files")
    staged = subprocess.run(
        [git, "-C", str(path), "diff", "--cached", "--quiet"],
        capture_output=True, timeout=_GIT_TIMEOUT)
    nothing_staged = staged.returncode == 0
    if nothing_staged and _has_head(path, git) and not force:
        head = _run_git(path, git, ["rev-parse", "HEAD"],
                        "Reading the snapshot id").strip()
        return {"id": head, "label": label, "created": False}
    commit_id = _commit(path, git, label, allow_empty=nothing_staged)
    return {"id": commit_id, "label": label, "created": True}


def list_snapshots(folder, git: str = "git") -> list[dict]:
    """Snapshots newest-first: {"id", "short", "label", "timestamp"}.
    A folder that isn't a snapshot repo lists empty, quietly — the
    dialog asks repo_state() which face to show."""
    path = Path(folder)
    if not is_repo(path, git) or not _has_head(path, git):
        return []
    out = _run_git(path, git,
                   ["log", "--format=%H%x1f%ct%x1f%s"],
                   "Could not read the snapshot list")
    snaps = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        full, epoch, subject = parts
        try:
            when = datetime.fromtimestamp(int(epoch)).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            when = ""
        snaps.append({"id": full, "short": full[:7],
                      "label": subject, "timestamp": when})
    return snaps


def restore_snapshot(folder, commit_id: str, git: str = "git") -> dict:
    """Put the project's files back to *commit_id*'s snapshot.

    First a safety snapshot of the current state is taken — if that
    fails, this raises and NOTHING is restored. Then the index and
    working tree are swapped to the snapshot's tree (read-tree never
    moves HEAD and never touches ignored files) and the result is
    committed as a new "Restored: …" snapshot, so history stays
    whole and the restore can itself be undone from the list.
    Returns {"id", "label", "safety_id", "restored"}; `restored` is
    False when the project was already at that snapshot."""
    path = _folder(folder)
    if not is_repo(path, git):
        raise SnapshotError("This project isn't being tracked yet — start tracking it first.")
    target_label = ""
    for snap in list_snapshots(path, git):
        if snap["id"] == commit_id or snap["short"] == commit_id:
            target_label = snap["label"]
            commit_id = snap["id"]
            break
    if not target_label:
        raise SnapshotError("That snapshot isn't in this project's list any more.")
    safety = take_snapshot(path, f"{SAFETY_PREFIX} ({_now_stamp()})", git,
                           force=True)
    _run_git(path, git, ["read-tree", "--reset", "-u", commit_id],
             "Could not restore the snapshot")
    staged = subprocess.run(
        [git, "-C", str(path), "diff", "--cached", "--quiet"],
        capture_output=True, timeout=_GIT_TIMEOUT)
    if staged.returncode == 0:
        return {"id": commit_id, "label": target_label,
                "safety_id": safety["id"], "restored": False}
    label = f"{RESTORED_PREFIX}{target_label}"
    new_id = _commit(path, git, label)
    return {"id": new_id, "label": label,
            "safety_id": safety["id"], "restored": True}
