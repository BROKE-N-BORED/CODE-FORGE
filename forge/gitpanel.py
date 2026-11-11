"""Git panel engine — item #51 (pure stdlib, no Qt, like
snapshots.py).

The data half of the changes panel inside the run surface: what
changed in the CURRENT project, the diff behind any one change,
and the stage / commit / push verbs the panel buttons call. Plain
git underneath, argued the same way snapshots.py argues it —
argument lists only, no shell, ``git -C <folder>`` on every call,
and the folder counts only when it IS its repo's root (a project
nested inside some other repo is not a repo here, because staging
from the parent would reach files outside the project).

Unlike snapshots (which raises SnapshotError for its dialog to
show), these helpers return data: a failed git call comes back as
(ok=False, git's own output), and the panel shows that output
verbatim — a refused commit or a push with no upstream is a normal
outcome to display, never an exception to hide.

Commits carry the same identity rule as snapshots: when the repo
has no usable user.name/user.email of its own, the Forge identity
rides per-command via ``git -c`` and signing is off — the owner's
config files are never written, and a commit never hangs on a
prompt this subprocess cannot answer.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import snapshots as snap_mod

_LOCAL_TIMEOUT = 10    # seconds — status/diff/stage are local and fast
_PUSH_TIMEOUT = 60     # a push can legitimately spend time on a network
DIFF_CAP = 60_000      # chars of diff text the panel will carry


def _run(root, args: list[str], timeout: int = _LOCAL_TIMEOUT):
    """One git call in *root*: (returncode, stdout+stderr). Never
    raises — a missing binary, a timeout, or any OS error becomes a
    nonzero code with the reason as the output text.

    Sweep R121: stdin is DEVNULL and GIT_TERMINAL_PROMPT=0 — a push
    to an HTTPS remote needing credentials (or an SSH key with a
    passphrase, or a commit hook reading stdin) used to prompt on a
    terminal this GUI subprocess does not have and burn the whole
    timeout reporting "git took too long"; now git fails at once
    with its own words. Sweep R125: output decodes as UTF-8 with
    surrogateescape, so a filename whose bytes are not UTF-8 survives
    the round trip back into a later stage/diff call instead of
    being mangled into a path that does not exist."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, encoding="utf-8",
            errors="surrogateescape", stdin=subprocess.DEVNULL,
            env=env, timeout=timeout, cwd=str(root))
    except FileNotFoundError:
        return 127, "git is not installed."
    except subprocess.TimeoutExpired:
        return 124, "git took too long and was stopped."
    except OSError as exc:
        return 126, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# Sweep R122: the most diff output _run_capped will pull through
# RAM (DIFF_CAP bounds the DISPLAY; this bounds the capture itself —
# the old path materialised a whole multi-hundred-MB diff to show
# 60,000 chars of it). ~4 bytes per displayed char covers UTF-8.
_DIFF_READ_CAP = DIFF_CAP * 4 + 4096


def _run_capped(root, args: list[str], timeout: int = _LOCAL_TIMEOUT):
    """_run with the capture bounded: at most _DIFF_READ_CAP bytes
    are read; past that the process is killed and the text is cut
    there (_cap_diff still applies its display cap and says so).
    The read runs on a daemon thread so the timeout binds even when
    git produces a little output and then hangs."""
    import threading
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.Popen(
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env, cwd=str(root))
    except FileNotFoundError:
        return 127, "git is not installed."
    except OSError as exc:
        return 126, str(exc)
    chunks: list[bytes] = []
    state = {"total": 0, "over": False}

    def _reader() -> None:
        try:
            while True:
                data = proc.stdout.read(65536)
                if not data:
                    return
                chunks.append(data)
                state["total"] += len(data)
                if state["total"] > _DIFF_READ_CAP:
                    state["over"] = True
                    proc.kill()
                    return
        except Exception:
            return

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        reader.join(1)
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass
        return 124, "git took too long and was stopped."
    reader.join(2)
    try:
        if proc.stdout is not None:
            proc.stdout.close()
    except Exception:
        pass
    if state["over"]:
        # Killed for SIZE, not failure: the truncated text is the
        # result, and _cap_diff notes the cut at display time.
        code = 0
    data = b"".join(chunks)[:_DIFF_READ_CAP]
    return code, data.decode("utf-8", "surrogateescape")


def _git(root, args: list[str], timeout: int = _LOCAL_TIMEOUT):
    """(ok, output) for one git call — ok is simply exit code 0."""
    code, out = _run(root, args, timeout)
    return code == 0, out


def repo_state(root) -> str:
    """The panel's three faces: "ok" (a repo the panel can work),
    "not_repo", or "no_git". Mirrors snapshots' rule that only the
    repo's own root counts."""
    if not snap_mod.git_available():
        return "no_git"
    if not snap_mod.is_repo(root):
        return "not_repo"
    return "ok"


def changes(root) -> list[dict]:
    """The project's uncommitted changes, in git's own order:
    {"path", "xy", "staged", "unstaged", "untracked"}. Empty for a
    clean tree — and for a non-repo or a missing git, where
    repo_state() carries the reason instead.

    Parsed from `status --porcelain=v1 -z`: NUL-terminated entries
    keep paths with spaces (or worse) verbatim, and a rename/copy
    entry is followed by a second field holding the source path,
    which is consumed and dropped — the change is reported under
    the path the file lives at now."""
    if repo_state(root) != "ok":
        return []
    ok, out = _git(root, ["status", "--porcelain=v1", "-z"])
    if not ok:
        return []
    fields = out.split("\0")
    found: list[dict] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:            # "XY <path>" at minimum
            continue
        x, y = entry[0], entry[1]
        if x in "RC" or y in "RC":
            i += 1                    # the rename/copy source path
        untracked = x == "?" and y == "?"
        found.append({
            "path": entry[3:],
            "xy": entry[:2],
            "staged": not untracked and x not in " ",
            "unstaged": not untracked and y not in " ",
            "untracked": untracked,
            # Sweep R125 (conflict half): an unmerged path used to
            # be indistinguishable from an ordinary staged one —
            # X='U' made "staged" true, so a conflicted file wore
            # the wrong section AND the wrong buttons, and nothing
            # anywhere said "conflicted".
            "conflict": x == "U" or y == "U" or (x, y) in (
                ("A", "A"), ("D", "D")),
        })
    return found


def _cap_diff(text: str) -> str:
    if len(text) > DIFF_CAP:
        return (text[:DIFF_CAP] +
                f"\n[diff truncated — showing the first {DIFF_CAP} "
                "characters]\n")
    return text


def diff_for(root, change: dict) -> str:
    """The diff text behind one change dict from changes().

    Untracked files have no git diff, so they are diffed against
    /dev/null (git exits 1 there to mean "differs" — that output IS
    the diff, not a failure). A change staged AND unstaged shows
    both halves, worktree first, each under its own label; a
    staged-only change shows the cached diff and an unstaged-only
    one the worktree diff. A genuinely failed call returns git's
    own output text — the panel shows it rather than hiding it."""
    path = change["path"]
    if change.get("untracked"):
        _code, out = _run_capped(root, ["diff", "--no-index", "--",
                                        "/dev/null", path])
        return _cap_diff(out)
    if change.get("staged") and change.get("unstaged"):
        _c1, worktree = _run_capped(root, ["diff", "--", path])
        _c2, staged = _run_capped(root, ["diff", "--cached", "--", path])
        return _cap_diff("=== Working tree ===\n" + worktree +
                         "\n=== Staged ===\n" + staged)
    if change.get("staged"):
        _code, out = _run_capped(root, ["diff", "--cached", "--", path])
        return _cap_diff(out)
    _code, out = _run_capped(root, ["diff", "--", path])
    return _cap_diff(out)


def stage(root, path: str):
    """(ok, output): stage one path — new files included; git add
    is the whole verb."""
    return _git(root, ["add", "--", path])


def unstage(root, path: str):
    """(ok, output): pull one path back out of the index, keeping
    the working-tree edits. `git restore --staged` is the modern
    verb; a git too old to know it falls back to `git reset HEAD`,
    which says the same thing."""
    ok, out = _git(root, ["restore", "--staged", "--", path])
    if ok:
        return ok, out
    if "is not a git command" not in out:
        # Sweep R124: the fallback exists for git < 2.23, which does
        # not know `restore` and says so. The old code fell back on
        # ANY failure and returned the fallback's result, masking
        # the real error (a pathspec mistake, a corrupt index) behind
        # a second mutating command. Other failures now surface
        # git's own words from the restore attempt.
        return ok, out
    return _git(root, ["reset", "-q", "HEAD", "--", path])


def _identity_flags(root) -> list[str]:
    """Per-command commit identity, snapshots' rule: the Forge
    identity rides ONLY when the repo has no usable one of its own.
    Signing is always off for panel commits (a gpgsign prompt would
    hang a subprocess that cannot answer it)."""
    flags: list[str] = []
    _c1, name = _run(root, ["config", "user.name"])
    _c2, email = _run(root, ["config", "user.email"])
    if not name.strip() or not email.strip():
        flags += ["-c", f"user.name={snap_mod.GIT_NAME}",
                  "-c", f"user.email={snap_mod.GIT_EMAIL}"]
    flags.append("-c")
    flags.append("commit.gpgsign=false")
    return flags


def commit(root, message: str):
    """(ok, output): commit whatever is staged — git's own rule,
    nothing here stages on the sly. An empty message is refused
    before git is ever called; git's refusals (nothing staged, a
    hook's veto) come back as (False, git's words)."""
    if not str(message or "").strip():
        return False, "Write a commit message first."
    return _git(root, [*_identity_flags(root), "commit", "-m", message])


def push(root):
    """(ok, output): plain `git push` under the long timeout. The
    no-upstream refusal and every other outcome come back verbatim
    for the panel to show."""
    return _git(root, ["push"], timeout=_PUSH_TIMEOUT)
