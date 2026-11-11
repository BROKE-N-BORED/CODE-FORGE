"""Design Shell bridge (Phase B1, 2026-11-11) — Forge drives the
standalone Design Shell as a subprocess, as a tool.

The shell stays its own program, installed separately (the owner's
order: "build it into code forge and wire it up so that it can use
it as a tool" — a bridge, never a fork: no shell code is copied
into this tree). This module is the whole bridge:

- locate the shell install (Settings `design_shell_path`, else the
  FORGE_DESIGN_SHELL environment variable, else ~/design-shell);
- run its CLI (`python3 -m designshell ...`) with the owner's
  environment inherited untouched — provider keys the seats use
  live in the environment only; the bridge never reads, stores, or
  logs them;
- keep each project's shell workspace inside the project
  (<project>/.forge/design-shell/), initialized on first use;
- guard the HEAVY commands (build, images) with a resident-model
  check: a Blender build or an image batch started while a local
  model sits in RAM would thrash this class of machine, so the
  bridge refuses in plain words. The check FAILS OPEN — if Ollama
  cannot be reached, nothing can be proven busy, and the command
  runs. Light commands are never guarded.
- the gate rule: this module NEVER approves a job on the model's
  say-so. `plan` drafts and stops. Approval reaches the shell only
  through ForgeWindow's owner gate (forge/main.py), which consumes
  a one-shot token that only the UI can mint — the tool handler
  refuses action "approve" outright, in words the model can relay.
- import: after a build, the job's deliverables are COPIED into
  the project (.forge/design/<job>/) with the description file and
  build report riding as lineage — copies, never moves or links;
  the shell workspace keeps its originals (the art-department
  plan's copy rule, Options B+C).

Pure subprocess + filesystem: no Qt imports, so the module is
testable headlessly and the worker tool handler in forge/main.py
stays a thin adapter.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

# ------------------------------------------------------------------
# Words the owner and the model see. Plain, no tracebacks, ever.
# ------------------------------------------------------------------
NOT_INSTALLED = (
    "Design Shell isn't installed/found — set its folder in "
    "Settings → Behavior (the 'Design Shell folder' field), or "
    "install the shell first. Nothing was run.")

BUSY_REFUSAL = (
    "A local model is loaded right now ({names}) — a {what} on top "
    "would thrash this machine. Unload models (Models window → "
    "Unload all) or finish the turn first. Nothing was run.")

APPROVE_REFUSAL = (
    "Refused: the design_shell tool cannot approve a Design Shell "
    "job. Approval is the owner's call, made at Forge's plan gate — "
    "present the plan summary above to the owner and STOP. When the "
    "owner approves there, Forge itself runs the shell's approve; "
    "you never call approve.")

PICKER_URL = "http://127.0.0.1:8762"
PICKER_PORT = 8762

ACTIONS = ("plan", "show", "status", "search", "approve", "build",
           "images", "pick", "pick_status", "decide", "reroll",
           "file", "import")
HEAVY_ACTIONS = ("build", "images")

# The tool schema the model sees. Registered in
# ForgeWindow._extra_tools as a write-class (approval-gated) tool —
# it runs a subprocess and writes into the project, so Forge's
# mode fences treat it exactly like the other write tools: offered
# in Code mode, refused in the read-only modes.
DESIGN_SHELL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "design_shell",
        "description": (
            "Drive the Design Shell (the standalone design & asset "
            "workshop installed on this PC) for this project: draft "
            "asset plans from plain words, build approved assets "
            "(3D scenes, UI art, images) with Blender and import the "
            "results into the project, generate candidate reference "
            "images, and search the shell's library. Actions: plan "
            "(words -> a draft job + its plain-English summary; it "
            "STOPS there — the owner approves the plan at Forge's "
            "gate, never you), show/status/search (read the shell), "
            "build (an approved job; imports the outputs into the "
            "project), images (prompt -> a batch of 3 candidates + "
            "the picker page to open), pick_status (what the owner "
            "kept/rejected), decide (keep/reject one candidate), "
            "reroll (another round), file (file a reviewed job into "
            "the shell library), import (copy a job's outputs — or "
            "a filed library asset's files — into the project). "
            "There is deliberately NO approve action for you: "
            "approval is the owner's, at the gate."),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": list(ACTIONS),
                           "description": "What to do."},
                "words": {"type": "string",
                          "description": "For plan: the plain-words "
                                         "description of the asset."},
                "kind": {"type": "string",
                         "description": "For plan: the asset kind "
                                        "(default 'scene')."},
                "job": {"type": "string",
                        "description": "A job name, for show / build "
                                       "/ images / file / import."},
                "query": {"type": "string",
                          "description": "For search: what to look "
                                         "for in the library."},
                "prompt": {"type": "string",
                           "description": "For images: the image "
                                         "prompt."},
                "batch": {"type": "string",
                          "description": "A batch folder (the full "
                                         "path the images action "
                                         "returned), for pick / "
                                         "pick_status / decide / "
                                         "reroll."},
                "file": {"type": "string",
                         "description": "For decide: the candidate "
                                        "file name (e.g. cand-01.png)."},
                "decision": {"type": "string",
                             "enum": ["keep", "reject"],
                             "description": "For decide: keep or "
                                            "reject the candidate."},
                "asset": {"type": "string",
                          "description": "For import: a filed "
                                         "library asset's folder "
                                         "name to copy instead of "
                                         "a job."},
            },
            "required": ["action"],
        },
    },
}


# ------------------------------------------------------------------
# Locating the shell
# ------------------------------------------------------------------
def locate_shell(settings: dict | None = None,
                 env: dict | None = ...,
                 home: Path | None = None) -> Path | None:
    """The shell's install folder, or None when no candidate is a
    real install. Resolution order: the Settings value, the
    FORGE_DESIGN_SHELL environment variable, ~/design-shell. A
    candidate counts only when it holds the shell package
    (designshell/__main__.py) or its run.sh launcher. env: a
    mapping to read INSTEAD of the process environment (tests);
    the default reads os.environ."""
    env = os.environ if env is ... else env
    candidates = []
    configured = str((settings or {}).get("design_shell_path", "") or "").strip()
    if configured:
        candidates.append(configured)
    if str(env.get("FORGE_DESIGN_SHELL", "") or "").strip():
        candidates.append(str(env["FORGE_DESIGN_SHELL"]).strip())
    base = Path(home) if home is not None else Path.home()
    candidates.append(str(base / "design-shell"))
    for raw in candidates:
        path = Path(raw).expanduser()
        if (path / "designshell" / "__main__.py").is_file() \
                or (path / "run.sh").is_file():
            return path
    return None


def project_workspace(project_root) -> Path:
    """This project's shell workspace: <project>/.forge/design-shell/.

    Inside .forge (Forge's own per-project tree) but OUTSIDE
    .forge/design/: the design folder is the mode windows' document
    area with its own scaffold subfolders, while the shell workspace
    is a working directory (jobs/, library/, images/, its own
    settings.json) that the shell manages — mixing the two would
    scatter shell state through the document folders. Imports land
    in .forge/design/<job>/, the human-facing side."""
    return Path(project_root) / ".forge" / "design-shell"


def _project_design_dir(project_root) -> Path:
    return Path(project_root) / ".forge" / "design"


# ------------------------------------------------------------------
# The busy guard (heavy commands only; fails open)
# ------------------------------------------------------------------
def resident_models(base_url: str = "http://127.0.0.1:11434",
                    timeout: float = 2.0) -> list[str] | None:
    """Resident local model names from Ollama's /api/ps, or None
    when Ollama cannot be reached. The same urllib idiom as
    forge/governor._ps_models — duplicated in miniature because the
    guard must distinguish 'unreachable' (None: fail open) from
    'nothing resident' ([], proceed)."""
    try:
        with urllib.request.urlopen(
                base_url.rstrip("/") + "/api/ps", timeout=timeout) as r:
            data = json.load(r)
    except Exception:
        return None
    return [str(m.get("name", "?")) for m in data.get("models", [])
            if isinstance(m, dict)]


# ------------------------------------------------------------------
# Running the shell CLI
# ------------------------------------------------------------------
def _tail(text: str, lines: int = 12) -> str:
    rows = [r for r in (text or "").strip().splitlines() if r.strip()]
    return "\n".join(rows[-lines:])


class Bridge:
    """One bridge for one project. `runner`, `ps_check`, and
    `launcher` are injectable seams for tests (the real shell is
    never invoked there); production leaves them None and the real
    subprocess / /api/ps / Popen paths run."""

    def __init__(self, project_root, settings: dict | None = None,
                 runner=None, ps_check=None, launcher=None,
                 ollama_url: str = "http://127.0.0.1:11434",
                 env: dict | None = ..., home: Path | None = None):
        self.project_root = Path(project_root)
        self.settings = settings or {}
        self._runner = runner
        self._ps_check = ps_check
        self._launcher = launcher
        self.ollama_url = ollama_url
        self._env = env
        self._home = home
        self.picker_processes: list = []

    # -- plumbing --------------------------------------------------
    def shell_dir(self) -> Path | None:
        return locate_shell(self.settings, env=self._env, home=self._home)

    def workspace(self) -> Path:
        return project_workspace(self.project_root)

    def _argv(self, shell_dir: Path, args: list[str]) -> list[str]:
        return [sys.executable, "-m", "designshell", *args]

    def run_shell(self, args: list[str], timeout: int = 1800):
        """Run one shell CLI command; (ok, combined_output). The
        owner's environment is inherited whole (keys pass through
        to the seats; the bridge never reads them). A non-zero exit
        is a plain failure carrying the shell's own last lines —
        never a traceback."""
        shell_dir = self.shell_dir()
        if shell_dir is None:
            return False, NOT_INSTALLED
        ws = self.workspace()
        ws.mkdir(parents=True, exist_ok=True)
        argv = self._argv(shell_dir, args)
        env = dict(os.environ if self._env is ... else self._env)
        prev = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(shell_dir) + (
            os.pathsep + prev if prev else "")
        if self._runner is not None:
            code, out, err = self._runner(argv, timeout, env)
        else:
            try:
                proc = subprocess.run(
                    argv, cwd=str(shell_dir), env=env,
                    capture_output=True, text=True, timeout=timeout)
                code, out, err = proc.returncode, proc.stdout, proc.stderr
            except subprocess.TimeoutExpired:
                return False, (f"The Design Shell command ran past "
                               f"{timeout}s and was stopped: "
                               f"{' '.join(args[:2])}. Nothing was "
                               f"filed by the bridge.")
            except OSError as exc:
                return False, (f"Could not start the Design Shell "
                               f"({exc}). Nothing was run.")
        combined = ((out or "") + ("\n" + err if err else "")).strip()
        if code != 0:
            return False, ("The Design Shell reported a problem:\n"
                           + (_tail(combined) or
                              f"(exit code {code}, no output)"))
        return True, combined

    def ensure_workspace(self) -> str | None:
        """`init` the project workspace on first use; returns an
        error string when even that fails, else None."""
        ws = self.workspace()
        if (ws / "jobs").is_dir() and (ws / "library").is_dir():
            return None
        ok, out = self.run_shell(["init", str(ws)], timeout=60)
        return None if ok else out

    def busy_refusal(self, what: str) -> str | None:
        """The heavy-command guard: a refusal string when a local
        model is resident, else None. Fails open — an unreachable
        Ollama proves nothing, so the command runs."""
        if self._ps_check is not None:
            resident = self._ps_check()
        else:
            resident = resident_models(self.ollama_url)
        if resident:
            return BUSY_REFUSAL.format(
                names=", ".join(resident), what=what)
        return None

    def _job_dir(self, job: str) -> Path:
        return self.workspace() / "jobs" / job

    # -- actions ----------------------------------------------------
    def act(self, args: dict) -> str:
        action = str(args.get("action", "") or "").strip().lower()
        handler = getattr(self, f"_act_{action}", None) \
            if action in ACTIONS else None
        if handler is None:
            return (f"Unknown design_shell action {action!r}. "
                    f"Actions: {', '.join(ACTIONS)}.")
        return handler(args)

    def _act_plan(self, args: dict) -> str:
        ready = self.ensure_workspace()
        if ready is not None:
            return ready
        words = str(args.get("words", "") or "").strip()
        if not words:
            return "design_shell plan needs 'words' — the plain-words description of the asset."
        kind = str(args.get("kind", "") or "scene").strip() or "scene"
        argv = ["plan", str(self.workspace()), *words.split(),
                "--kind", kind]
        ok, out = self.run_shell(argv, timeout=900)
        if not ok:
            return out
        job = _job_name_from_plan_output(out)
        if job:
            _ok2, shown = self.run_shell(
                ["show", str(self.workspace()), job], timeout=60)
            summary = shown if _ok2 else out
            return (f"Design Shell job drafted: {job}\n\n{summary}\n\n"
                    "STOP — this plan is a draft. Present the summary "
                    "to the owner; the job builds only after the "
                    "owner approves it at Forge's plan gate (Forge "
                    "runs the approval itself). Do not call build "
                    "yet.")
        return out

    def _act_show(self, args: dict) -> str:
        job = str(args.get("job", "") or "").strip()
        if not job:
            return "design_shell show needs a 'job' name."
        ready = self.ensure_workspace()
        if ready is not None:
            return ready
        _ok, out = self.run_shell(
            ["show", str(self.workspace()), job], timeout=60)
        return out

    def _act_status(self, args: dict) -> str:
        return self._act_show(args)

    def _act_search(self, args: dict) -> str:
        query = str(args.get("query", "") or "").strip()
        ready = self.ensure_workspace()
        if ready is not None:
            return ready
        argv = ["search", str(self.workspace()), query]
        kind = str(args.get("kind", "") or "").strip()
        if kind:
            argv += ["--kind", kind]
        _ok, out = self.run_shell(argv, timeout=60)
        return out

    def _act_approve(self, args: dict) -> str:
        # The inviolable B1 rule: never on the model's say-so. The
        # owner's gate in forge/main.py calls approve_from_gate(),
        # which is not reachable from any tool argument.
        return APPROVE_REFUSAL

    def _act_build(self, args: dict) -> str:
        job = str(args.get("job", "") or "").strip()
        if not job:
            return "design_shell build needs a 'job' name."
        refusal = self.busy_refusal("Blender build")
        if refusal:
            return refusal
        ready = self.ensure_workspace()
        if ready is not None:
            return ready
        ok, out = self.run_shell(
            ["build", str(self.workspace()), job], timeout=3600)
        if not ok:
            return out
        landed = import_job(self.project_root, self.workspace(), job)
        lines = [f"Design Shell build finished for job '{job}'.",
                 _tail(out, 6)]
        if landed:
            lines.append("Imported into the project (copies — the "
                         "shell keeps its originals):")
            lines += [f"  {p}" for p in landed]
        else:
            lines.append("The build produced no importable files "
                         "in the job's output folder.")
        return "\n".join(lines)

    def _act_images(self, args: dict) -> str:
        prompt = str(args.get("prompt", "") or "").strip()
        if not prompt:
            return "design_shell images needs a 'prompt'."
        refusal = self.busy_refusal("image batch")
        if refusal:
            return refusal
        ready = self.ensure_workspace()
        if ready is not None:
            return ready
        argv = ["images", str(self.workspace()), *prompt.split(),
                "--count", "3", "--size", "512"]
        job = str(args.get("job", "") or "").strip()
        if job:
            argv += ["--job", job]
        ok, out = self.run_shell(argv, timeout=1800)
        if not ok:
            return out
        batch_dir = _batch_dir_from_images_output(out)
        lines = [out]
        if batch_dir:
            pick = self.launch_picker(batch_dir)
            lines.append("")
            lines.append(pick)
            lines.append("Tell the owner to open the picker page "
                         "and keep or reject each candidate. Then "
                         "call design_shell with action pick_status "
                         f"and batch \"{batch_dir}\" to see what "
                         "landed.")
        return "\n".join(lines)

    def _act_pick(self, args: dict) -> str:
        batch_dir = str(args.get("batch", "") or "").strip()
        if not batch_dir:
            return "design_shell pick needs the 'batch' folder the images action returned."
        return self.launch_picker(batch_dir)

    def _act_pick_status(self, args: dict) -> str:
        batch_dir = Path(str(args.get("batch", "") or "").strip())
        record = batch_dir / "batch.json"
        if not record.is_file():
            return f"No image batch found at {batch_dir} (no batch.json)."
        try:
            batch = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return f"The batch record at {record} could not be read: {exc}"
        lines = [f"Image batch: {batch_dir}",
                 f"Prompt: {batch.get('prompt', '')}"]
        for cand in batch.get("candidates", []):
            lines.append(f"  {cand.get('file')}  seed {cand.get('seed')}  "
                         f"{cand.get('status')}")
        ref = batch.get("reference_file")
        job = batch.get("job")
        if ref and job:
            landed = self.workspace() / "jobs" / job / "reference.png"
            lines.append(
                f"Keeper: {ref} — already promoted by the shell to "
                f"the job's reference image: {landed} (the keeper's "
                "prompt, seed and model are recorded in the job's "
                "description.json).")
        elif ref:
            lines.append(f"Keeper so far: {ref} (this batch is not "
                         "linked to a job, so it is the batch's "
                         "reference_file only).")
        else:
            lines.append("No keeper chosen yet — every candidate is "
                         "still waiting, or all were rejected "
                         "(rejected files are deleted by the shell; "
                         "action reroll makes a fresh round).")
        return "\n".join(lines)

    def _act_decide(self, args: dict) -> str:
        batch_dir = str(args.get("batch", "") or "").strip()
        fname = str(args.get("file", "") or "").strip()
        decision = str(args.get("decision", "") or "").strip().lower()
        if not batch_dir or not fname or decision not in ("keep", "reject"):
            return ("design_shell decide needs 'batch', 'file', and "
                    "'decision' (keep or reject).")
        _ok, out = self.run_shell(
            ["decide", batch_dir, fname, decision], timeout=60)
        return out

    def _act_reroll(self, args: dict) -> str:
        batch_dir = str(args.get("batch", "") or "").strip()
        if not batch_dir:
            return "design_shell reroll needs the 'batch' folder."
        _ok, out = self.run_shell(["reroll", batch_dir], timeout=1800)
        return out

    def _act_file(self, args: dict) -> str:
        job = str(args.get("job", "") or "").strip()
        if not job:
            return "design_shell file needs a 'job' name."
        ready = self.ensure_workspace()
        if ready is not None:
            return ready
        _ok, out = self.run_shell(
            ["file", str(self.workspace()), job], timeout=300)
        return out

    def _act_import(self, args: dict) -> str:
        ready = self.ensure_workspace()
        if ready is not None:
            return ready
        asset = str(args.get("asset", "") or "").strip()
        job = str(args.get("job", "") or "").strip()
        if asset:
            landed = import_library_asset(
                self.project_root, self.workspace(), asset)
            if landed is None:
                return (f"No library asset named '{asset}' in this "
                        f"project's shell library "
                        f"({self.workspace() / 'library'}).")
            return ("Imported library asset into the project "
                    "(copies — the library keeps its originals):\n"
                    + "\n".join(f"  {p}" for p in landed))
        if job:
            landed = import_job(self.project_root, self.workspace(), job)
            if not landed:
                return (f"Job '{job}' has nothing importable yet — "
                        "it imports after a successful build (its "
                        "output files, description, and report).")
            return ("Imported into the project (copies — the shell "
                    "keeps its originals):\n"
                    + "\n".join(f"  {p}" for p in landed))
        return "design_shell import needs a 'job' name or a library 'asset' name."

    # -- the picker --------------------------------------------------
    def launch_picker(self, batch_dir: str) -> str:
        """Start the shell's own picker page for a batch, detached,
        and return the words for the owner: the URL to open. B1
        builds no native picker — the shell's page IS the surface,
        on the owner's phone browser (same machine or LAN)."""
        shell_dir = self.shell_dir()
        if shell_dir is None:
            return NOT_INSTALLED
        argv = self._argv(shell_dir, ["pick", str(batch_dir),
                                      "--port", str(PICKER_PORT)])
        if self._launcher is not None:
            self._launcher(argv, str(shell_dir))
        else:
            try:
                proc = subprocess.Popen(
                    argv, cwd=str(shell_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True)
                self.picker_processes.append(proc)
            except OSError as exc:
                return (f"The image batch is ready at {batch_dir}, "
                        f"but the picker page could not be started "
                        f"({exc}). It can be opened by hand: "
                        f"python3 -m designshell pick {batch_dir}")
        return (f"Image picker is open for the batch: {PICKER_URL} — "
                "open it on the phone browser (same machine; on the "
                "LAN use this PC's address with port "
                f"{PICKER_PORT}). Candidates live in {batch_dir}; "
                "keeping the first candidate of a job-linked batch "
                "makes it that job's reference image automatically.")


# ------------------------------------------------------------------
# Output parsing + the owner-gate approval entry point
# ------------------------------------------------------------------
def _job_name_from_plan_output(out: str) -> str | None:
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("job created:"):
            return line.split(":", 1)[1].split("(")[0].strip() or None
    return None


def _batch_dir_from_images_output(out: str) -> str | None:
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("batch:"):
            return line.split(":", 1)[1].strip() or None
    return None


def approve_from_gate(project_root, settings: dict, job: str,
                      runner=None) -> str:
    """Approve a job — THE OWNER-GATE PATH ONLY. Called by
    ForgeWindow's Design Shell gate button handler after the owner
    approves there; the one-shot token is consumed by the window
    before this runs. No tool argument can reach this function."""
    bridge = Bridge(project_root, settings=settings, runner=runner)
    if bridge.shell_dir() is None:
        return NOT_INSTALLED
    ready = bridge.ensure_workspace()
    if ready is not None:
        return ready
    _ok, out = bridge.run_shell(
        ["approve", str(bridge.workspace()), job], timeout=60)
    return out


# ------------------------------------------------------------------
# Import — the copy rule (copies, never moves/links; lineage rides)
# ------------------------------------------------------------------
def import_job(project_root, workspace, job: str) -> list[str]:
    """Copy a built job's deliverables into
    <project>/.forge/design/<job>/: everything in the job's output
    folder (renders, .glb/.blend, every exported variant), the
    chosen reference image when the job has one, and the lineage —
    description.json + build-report.json (+ session.jsonl and
    job.json when present). Returns the project-relative paths
    that landed (empty when the job has no outputs yet)."""
    ws = Path(workspace)
    job_dir = ws / "jobs" / job
    output_dir = job_dir / "output"
    if not output_dir.is_dir():
        return []
    outputs = [p for p in sorted(output_dir.iterdir()) if p.is_file()]
    if not outputs:
        return []
    dest = _project_design_dir(project_root) / job
    dest.mkdir(parents=True, exist_ok=True)
    landed: list[str] = []
    for src in outputs:
        shutil.copy2(src, dest / src.name)
        landed.append(f".forge/design/{job}/{src.name}")
    reference = job_dir / "reference.png"
    if reference.is_file():
        shutil.copy2(reference, dest / "reference.png")
        landed.append(f".forge/design/{job}/reference.png")
    for extra in ("description.json", "build-report.json",
                  "session.jsonl", "job.json"):
        src = job_dir / extra
        if src.is_file():
            shutil.copy2(src, dest / extra)
            landed.append(f".forge/design/{job}/{extra}")
    return landed


def import_library_asset(project_root, workspace, asset: str):
    """Copy a FILED library asset into the project, following the
    same copy rule: the asset's files per its manifest.json, plus
    the manifest itself and the lineage files that ride the
    library folder (description.json, build-report.json,
    session.jsonl). Lands in .forge/design/<asset>/. Returns the
    project-relative paths, or None when no such asset exists."""
    lib_dir = Path(workspace) / "library" / asset
    manifest_path = lib_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    dest = _project_design_dir(project_root) / asset
    dest.mkdir(parents=True, exist_ok=True)
    landed: list[str] = []
    names = list(manifest.get("files", [])) + [
        "manifest.json", "description.json", "build-report.json",
        "session.jsonl"]
    for name in dict.fromkeys(names):
        src = lib_dir / name
        if src.is_file():
            shutil.copy2(src, dest / name)
            landed.append(f".forge/design/{asset}/{name}")
    return landed
