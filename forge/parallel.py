"""Parallel agent jobs (owner order, 2026-11-11): several agents
working at once, each isolated, supervised from Mission Control,
merging back only through the owner's gate.

The law of this module, stated once and enforced everywhere:

* A code-writing job NEVER writes in the owner's project tree. It
  works in its own git worktree of the project, on its own branch,
  and the only way its changes reach the project is the owner's
  Approve in Mission Control. Discard leaves the tree untouched.
* A project that is not a git repo cannot host code jobs — the
  refusal is plain words, and read-only jobs (research / design /
  review questions, answered with reading tools only, against the
  real tree) are the alternative.
* Concurrency is capped (owner-tunable, default 2) and the resource
  governor can hold the queue: when the machine is loaded, queued
  jobs stay queued and say why.
* Job records persist under the project's .forge/parallel/ so a
  crash or a closed app never orphans the record or the worktree.

The runner is a headless turn in the bench's shape (forge/bench.py):
same client facade, same forge.tools executor, same Containment
jail — rooted at the worktree for code jobs, at the project for
read-only jobs (whose belt has no writing tools at all). Inside a
job's worktree, tool calls run without per-call approval prompts:
the dispatch was the owner's instruction, the worktree is a
throwaway sandbox, and nothing lands without the merge gate.
That trade is deliberate and is stated on the dispatch dialog.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from forge.tools import TOOL_SCHEMAS, Containment, execute_tool

JOB_STATES = ("queued", "running", "done", "failed", "stopped",
              "interrupted", "merge_failed", "merged", "discarded")
SETTLED_STATES = ("done", "failed", "stopped", "interrupted",
                  "merge_failed", "merged", "discarded")

CODE_TOOL_NAMES = ("list_directory", "read_file", "write_file",
                   "append_file", "run_command")
READONLY_TOOL_NAMES = ("list_directory", "read_file")

JOB_SYSTEM = (
    "You are a parallel agent job inside Code Forge, working in "
    "your own isolated copy of the owner's project. Complete the "
    "task in the user's message exactly as specified — no extra "
    "features, no refactoring beyond the ask. You may use the "
    "tools provided; every path you touch stays inside this "
    "workspace. When the task is complete, reply with a plain "
    "summary of what you changed.")

MAX_CYCLES = 40
JOB_SECONDS = 1800
_DIFF_CAP = 200_000


def _schemas(names):
    wanted = set(names)
    return [s for s in TOOL_SCHEMAS
            if s["function"]["name"] in wanted]


# ------------------------------------------------------------------
# Git plumbing (plain subprocess git, the project's own repo)
# ------------------------------------------------------------------

def _git(root, *args, check=False):
    env = dict(os.environ)
    # Agent-made commits (worktree merges) carry an honest default
    # author when the owner has no git identity configured; any
    # identity the owner DID configure always wins.
    env.setdefault("GIT_AUTHOR_NAME", "Code Forge")
    env.setdefault("GIT_AUTHOR_EMAIL", "forge@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "Code Forge")
    env.setdefault("GIT_COMMITTER_EMAIL", "forge@localhost")
    proc = subprocess.run(
        ["git", "-C", str(root), *args], env=env,
        capture_output=True, text=True, timeout=120)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def is_git_repo(root) -> bool:
    try:
        return _git(root, "rev-parse", "--git-dir").returncode == 0
    except Exception:
        return False


def has_head(root) -> bool:
    try:
        return _git(root, "rev-parse", "--verify", "HEAD").returncode == 0
    except Exception:
        return False


def _exclude_worktrees(root) -> None:
    """Keep job worktrees out of the owner's git status — via the
    repo-local info/exclude, never a tracked file."""
    try:
        git_dir = _git(root, "rev-parse", "--git-dir").stdout.strip()
        base = Path(root) / git_dir if not Path(git_dir).is_absolute() \
            else Path(git_dir)
        info = base / "info"
        info.mkdir(parents=True, exist_ok=True)
        exclude = info / "exclude"
        line = ".forge/parallel/wt/"
        text = exclude.read_text(encoding="utf-8") \
            if exclude.exists() else ""
        if line not in text.splitlines():
            exclude.write_text(text.rstrip("\n") + "\n" + line + "\n",
                               encoding="utf-8")
    except Exception:
        pass  # cosmetic only; never block a job on it


# ------------------------------------------------------------------
# Job records + persistence
# ------------------------------------------------------------------

def parallel_dir(project_root) -> Path:
    return Path(project_root) / ".forge" / "parallel"


def new_job(task, project_root, model, kind="code", mode="") -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "task": str(task or "").strip(),
        "project": str(project_root),
        "model": str(model or ""),
        "kind": kind,                # "code" | "readonly"
        "mode": str(mode or ""),     # readonly filing: research/…
        "state": "queued",
        "worktree": "",
        "branch": "",
        "step": "",
        "note": "",
        "error": "",
        "result": "",
        "diff_stat": "",
        "created": time.time(),
        "started": 0.0,
        "finished": 0.0,
    }


class JobStore:
    """The per-project job ledger: .forge/parallel/jobs.json."""

    def __init__(self, project_root):
        self.root = Path(project_root)
        self.dir = parallel_dir(self.root)
        self.path = self.dir / "jobs.json"
        self._lock = threading.Lock()

    def load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def save(self, jobs: list[dict]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(jobs, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def upsert(self, job: dict) -> None:
        with self._lock:
            jobs = [j for j in self.load() if j.get("id") != job["id"]]
            jobs.append(job)
            jobs.sort(key=lambda j: j.get("created", 0))
            self.save(jobs)

    def get(self, job_id: str) -> dict | None:
        for job in self.load():
            if job.get("id") == job_id:
                return job
        return None


# ------------------------------------------------------------------
# Worktrees: create, diff, merge back, discard
# ------------------------------------------------------------------

def create_worktree(project_root, job: dict) -> Path:
    root = Path(project_root)
    if not is_git_repo(root):
        raise RuntimeError(
            "This project is not a git repository, so a code job "
            "has no isolated copy to work in. Initialize git in "
            "the project first, or run a read-only job instead.")
    if not has_head(root):
        raise RuntimeError(
            "This project's git repository has no commits yet, so "
            "there is nothing to branch a job copy from. Make a "
            "first commit, or run a read-only job instead.")
    _exclude_worktrees(root)
    wt = parallel_dir(root) / "wt" / job["id"]
    wt.parent.mkdir(parents=True, exist_ok=True)
    branch = f"forge-job-{job['id']}"
    _git(root, "worktree", "add", "-b", branch, str(wt), "HEAD",
         check=True)
    job["worktree"] = str(wt)
    job["branch"] = branch
    return wt


def capture_diff(job: dict) -> str:
    """Stage everything in the worktree, record the diff — the
    exact artifact the owner approves or discards — and COMMIT it
    onto the job's branch, so the branch actually carries the work
    the merge gate will land."""
    wt = Path(job["worktree"])
    _git(wt, "add", "-A")
    stat = _git(wt, "diff", "--cached", "--stat").stdout.strip()
    diff = _git(wt, "diff", "--cached").stdout
    job["diff_stat"] = stat
    if len(diff) > _DIFF_CAP:
        diff = diff[:_DIFF_CAP] + "\n… [diff truncated for display]\n"
    out = parallel_dir(job["project"]) / f"{job['id']}.diff"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(diff, encoding="utf-8")
    if stat:
        _git(wt, "commit", "-m",
             f"Parallel job {job['id']}: {job['task'][:60]}",
             check=True)
    return diff


def read_diff(job: dict) -> str:
    try:
        return (parallel_dir(job["project"]) /
                f"{job['id']}.diff").read_text(encoding="utf-8")
    except Exception:
        return ""


def approve_merge(job: dict) -> tuple[bool, str]:
    """Merge the job's branch into the project's current branch.
    Returns (landed, message). A conflict aborts the merge, leaves
    the tree exactly as it was, and reports the conflicted files —
    conflicts are never auto-resolved and never forced."""
    root = Path(job["project"])
    branch = job["branch"]
    proc = _git(root, "merge", "--no-ff", "-m",
                f"Merge parallel job {job['id']}: {job['task'][:60]}",
                branch)
    if proc.returncode != 0:
        conflicted = _git(
            root, "diff", "--name-only",
            "--diff-filter=U").stdout.strip()
        _git(root, "merge", "--abort")
        files = ", ".join(conflicted.splitlines()) or "see git output"
        return False, (f"Merge conflict in: {files}. Nothing was "
                       f"merged; the project tree is unchanged and "
                       f"the job's copy is kept for inspection.")
    _remove_worktree(job)
    _git(root, "branch", "-d", branch)
    return True, "Merged into the project."


def _remove_worktree(job: dict) -> None:
    root = Path(job["project"])
    wt = job.get("worktree")
    if wt:
        _git(root, "worktree", "remove", "--force", wt)


def discard_job(job: dict) -> None:
    """Remove the worktree and branch. The project tree is never
    touched — that is the point of the worktree."""
    if job.get("kind") == "code" and job.get("worktree"):
        _remove_worktree(job)
        if job.get("branch"):
            _git(Path(job["project"]), "branch", "-D", job["branch"])
    job["worktree"] = ""


# ------------------------------------------------------------------
# The headless job runner (bench-shaped; injectable for tests)
# ------------------------------------------------------------------

def run_job_turn(client, model, task_text, root, kind, cancel_event,
                 step_cb=None, max_cycles=MAX_CYCLES,
                 timeout_s=JOB_SECONDS) -> str:
    """Work one job task in `root` and return the model's final
    summary text. Tool calls execute through forge.tools under a
    Containment rooted at `root`; a read-only job's belt simply
    has no writing tools in it."""
    names = CODE_TOOL_NAMES if kind == "code" else READONLY_TOOL_NAMES
    belt = _schemas(names)
    containment = Containment(root)
    messages = [{"role": "system", "content": JOB_SYSTEM},
                {"role": "user", "content": task_text}]
    started = time.time()
    deadline = started + timeout_s
    original_timeout = getattr(client, "timeout", None)
    final_text = ""
    try:
        for cycle in range(1, max_cycles + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise _JobStopped()
            if time.time() >= deadline:
                raise RuntimeError("job time budget reached")
            if step_cb:
                step_cb(f"cycle {cycle}: asking the model")
            chunks, tool_calls = [], []
            for event in client.chat_stream(
                    model, messages, {}, belt,
                    cancel_event=cancel_event):
                msg = event.get("message") or {}
                if msg.get("content"):
                    chunks.append(str(msg["content"]))
                for call in msg.get("tool_calls") or []:
                    tool_calls.append(call)
                if event.get("done"):
                    break
            text = "".join(chunks)
            assistant = {"role": "assistant", "content": text}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            messages.append(assistant)
            if text:
                final_text = text
            if not tool_calls:
                break
            for call in tool_calls:
                if cancel_event is not None and cancel_event.is_set():
                    raise _JobStopped()
                fn = call.get("function") or {}
                name = str(fn.get("name") or "")
                args = dict(fn.get("arguments") or {})
                if step_cb:
                    step_cb(f"cycle {cycle}: {name or 'tool'}")
                if name not in names:
                    result = (f"Tool '{name}' is not available to "
                              f"this job.")
                else:
                    try:
                        result = execute_tool(
                            name, args, str(root),
                            cancel_event=cancel_event,
                            containment=containment)
                    except Exception as exc:
                        result = f"Tool error: {exc}"
                messages.append({"role": "tool", "tool_name": name,
                                 "content": str(result)})
    finally:
        if original_timeout is not None:
            try:
                client.timeout = original_timeout
            except Exception:
                pass
    return final_text


class _JobStopped(Exception):
    pass


READONLY_FOLDERS = {"research": "research", "design": "designs",
                    "review": "reviews"}


def file_readonly_result(job: dict, text: str) -> Path:
    """File a read-only job's answer where its mode reads results,
    plus the job's own results folder. Plain file writes into the
    project's .forge — artifacts, not project code."""
    base = parallel_dir(job["project"]) / "results"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{job['id']}.md"
    path.write_text(text or "", encoding="utf-8")
    folder = READONLY_FOLDERS.get(job.get("mode") or "")
    if folder:
        target_dir = Path(job["project"]) / ".forge" / folder
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            slug = "".join(c if c.isalnum() else "-"
                           for c in job["task"][:40]).strip("-")
            (target_dir / f"parallel-{slug}-{job['id']}.md"
             ).write_text(text or "", encoding="utf-8")
        except Exception:
            pass
    return path


# ------------------------------------------------------------------
# The manager: queue, cap, governor, threads, supervision state
# ------------------------------------------------------------------

def machine_loaded(snapshot: dict | None) -> bool:
    """The governor's word on whether new work may start: CPU
    saturated or under a gigabyte of RAM free means hold."""
    if not snapshot:
        return False
    return bool(snapshot.get("cpu_busy")) or \
        float(snapshot.get("avail_gb") or 99) < 1.0


class ParallelManager:
    """Owns every job across the owner's projects. UI-free: the
    Mission Control window reads rows() and calls the verbs."""

    def __init__(self, client_factory, snapshot_fn=None,
                 concurrency=2, runner=None):
        self._client_factory = client_factory
        self._snapshot_fn = snapshot_fn
        self._runner = runner or run_job_turn
        self.concurrency = max(1, int(concurrency or 2))
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._jobs: dict[str, dict] = {}
        self._snap_cache = (0.0, None)

    # -- registry / persistence -----------------------------------

    def attach_project(self, project_root) -> None:
        """Load a project's ledger. Jobs left mid-flight by a dead
        process become 'interrupted' — the record and the worktree
        survive; nothing silently resumes or vanishes."""
        store = JobStore(project_root)
        for job in store.load():
            if job.get("state") in ("running", "queued"):
                job["state"] = "interrupted"
                job["note"] = ("Forge closed while this job was "
                               "in flight. Its copy is intact — "
                               "requeue it or discard it.")
                store.upsert(job)
            with self._lock:
                self._jobs[job["id"]] = job

    def _persist(self, job: dict) -> None:
        JobStore(job["project"]).upsert(job)

    def jobs(self) -> list[dict]:
        with self._lock:
            return sorted(self._jobs.values(),
                          key=lambda j: j.get("created", 0))

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            return self._jobs.get(job_id)

    # -- dispatch ---------------------------------------------------

    def submit(self, task, project_root, model, kind="code",
               mode="") -> dict:
        job = new_job(task, project_root, model, kind=kind, mode=mode)
        if kind == "code":
            create_worktree(project_root, job)  # raises plain words
        with self._lock:
            self._jobs[job["id"]] = job
        self._persist(job)
        self.pump()
        return job

    def requeue(self, job_id: str) -> None:
        job = self.get(job_id)
        if job and job["state"] in ("interrupted", "stopped",
                                    "failed", "merge_failed"):
            if job["state"] == "merge_failed":
                return  # a merge decision, not a re-run
            job["state"] = "queued"
            job["note"] = ""
            self._persist(job)
            self.pump()

    # -- supervision ------------------------------------------------

    def _snapshot(self):
        now = time.time()
        stamp, snap = self._snap_cache
        if now - stamp < 10 and snap is not None:
            return snap
        snap = None
        if self._snapshot_fn is not None:
            try:
                snap = self._snapshot_fn()
            except Exception:
                snap = None
        self._snap_cache = (now, snap)
        return snap

    def pump(self) -> None:
        """Start queued jobs up to the cap, unless the governor
        says the machine is loaded — then the queue holds and the
        jobs say so."""
        with self._lock:
            running = sum(1 for j in self._jobs.values()
                          if j["state"] == "running")
            queued = [j for j in self._jobs.values()
                      if j["state"] == "queued"]
        if not queued or running >= self.concurrency:
            return
        loaded = machine_loaded(self._snapshot())
        for job in queued:
            if running >= self.concurrency:
                break
            if loaded:
                if job.get("note") != "Queued — the machine is " \
                        "loaded right now (resource governor).":
                    job["note"] = ("Queued — the machine is loaded "
                                   "right now (resource governor).")
                    self._persist(job)
                continue
            self._start(job)
            running += 1

    def _start(self, job: dict) -> None:
        cancel = threading.Event()
        job["state"] = "running"
        job["started"] = time.time()
        job["note"] = ""
        self._persist(job)
        thread = threading.Thread(
            target=self._run, args=(job, cancel), daemon=True,
            name=f"forge-job-{job['id']}")
        with self._lock:
            self._threads[job["id"]] = thread
            self._cancels[job["id"]] = cancel
        thread.start()

    def _run(self, job: dict, cancel: threading.Event) -> None:
        root = job["worktree"] if job["kind"] == "code" \
            else job["project"]

        def step(text):
            job["step"] = text
            self._persist(job)

        try:
            client = self._client_factory(job["model"])
            text = self._runner(client, job["model"], job["task"],
                                root, job["kind"], cancel,
                                step_cb=step)
            if cancel.is_set():
                job["state"] = "stopped"
            else:
                job["result"] = text or ""
                if job["kind"] == "code":
                    capture_diff(job)
                else:
                    file_readonly_result(job, text or "")
                job["state"] = "done"
        except _JobStopped:
            job["state"] = "stopped"
        except Exception as exc:
            if cancel.is_set():
                job["state"] = "stopped"
            else:
                job["state"] = "failed"
                job["error"] = str(exc)[:400]
        job["finished"] = time.time()
        self._persist(job)
        self.pump()

    def stop(self, job_id: str) -> None:
        """Stop one job. Its worktree and partial diff stay exactly
        where they are for inspection."""
        job = self.get(job_id)
        if job is None:
            return
        if job["state"] == "queued":
            job["state"] = "stopped"
            self._persist(job)
            return
        with self._lock:
            cancel = self._cancels.get(job_id)
        if cancel is not None:
            cancel.set()

    def stop_all(self) -> None:
        for job in self.jobs():
            if job["state"] in ("queued", "running"):
                self.stop(job["id"])

    # -- the gate ---------------------------------------------------

    def approve(self, job_id: str) -> tuple[bool, str]:
        job = self.get(job_id)
        if job is None or job["state"] not in ("done", "merge_failed"):
            return False, "Only a finished job can be merged."
        if job["kind"] != "code":
            return False, "Read-only jobs have nothing to merge."
        landed, message = approve_merge(job)
        job["state"] = "merged" if landed else "merge_failed"
        job["note"] = message
        if landed:
            job["worktree"] = ""
        self._persist(job)
        return landed, message

    def discard(self, job_id: str) -> None:
        job = self.get(job_id)
        if job is None or job["state"] in ("running", "queued",
                                           "merged", "discarded"):
            return
        discard_job(job)
        job["state"] = "discarded"
        job["note"] = "Discarded by the owner — nothing landed."
        self._persist(job)

    # -- rows for Mission Control -----------------------------------

    def rows(self) -> list[dict]:
        out = []
        for job in self.jobs():
            elapsed = ""
            if job.get("started"):
                end = job.get("finished") or time.time()
                secs = int(end - job["started"])
                elapsed = f"{secs // 60}m{secs % 60:02d}s"
            state = job["state"]
            line = (f"[{state}] {job['task'][:64]} — "
                    f"{job['model']} · {Path(job['project']).name}")
            if job["kind"] == "readonly":
                line += " · read-only"
            if elapsed:
                line += f" · {elapsed}"
            if state == "running" and job.get("step"):
                line += f" · {job['step']}"
            if job.get("note"):
                line += f" · {job['note'][:60]}"
            if job.get("error"):
                line += f" · {job['error'][:60]}"
            out.append({"id": job["id"], "line": line,
                        "state": state, "job": job})
        return out
