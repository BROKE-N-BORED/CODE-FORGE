"""Design Shell bridge (Phase B1, 2026-11-11) tests.

Everything runs against a FAKE shell: a temp directory holding a
stub `designshell` package whose CLI mimics the real one's
contract (argv shape, printed lines, job/batch folders on disk)
and records every argv it receives. The real shell is never
invoked. The stub's decide even performs the shell's real keeper
promotion (first keep in a job-linked batch -> jobs/<job>/
reference.png), because pick_status's words depend on that
on-disk fact.

Pins, in order: shell location (Settings > env > ~/design-shell,
invalid -> the plain not-installed words); argv construction per
action; the busy guard (resident model refuses heavy, light
passes, unreachable Ollama fails open); the gate rule — a
model-issued approve is refused and the owner's gate approves
exactly once on its one-shot token; build -> import copies with
lineage, originals untouched; images -> the picker launches
detached and the tool call returns; a non-zero shell exit is a
plain failure with the shell's own lines; the Settings field
round-trips.
"""
import json
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge import designshell_bridge as B

STUB_MAIN = r'''
import json, os, sys
from pathlib import Path

def log(argv):
    target = os.environ.get("DSBRIDGE_STUB_LOG")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\n")

def ws_of(argv):
    return Path(argv[1])

def main():
    argv = sys.argv[1:]
    log(argv)
    if os.environ.get("DSBRIDGE_STUB_FAIL"):
        print("stub exploded: the seat said no", file=sys.stderr)
        return 3
    cmd = argv[0]
    if cmd == "init":
        ws = ws_of(argv)
        (ws / "jobs").mkdir(parents=True, exist_ok=True)
        (ws / "library").mkdir(parents=True, exist_ok=True)
        print(f"workspace ready: {ws}")
        return 0
    if cmd == "plan":
        ws = ws_of(argv)
        job = ws / "jobs" / "stub-job"
        (job / "output").mkdir(parents=True, exist_ok=True)
        (job / "description.json").write_text(json.dumps(
            {"name": "Stub Job", "kind": "scene"}))
        (job / "job.json").write_text(json.dumps(
            {"name": "Stub Job", "state": "awaiting_approval"}))
        print("job created: stub-job (state: awaiting_approval)")
        print("Stub Job: a plain-English summary to approve.")
        return 0
    if cmd == "show":
        print("Stub Job: a plain-English summary to approve.")
        print("\nstate: awaiting_approval")
        return 0
    if cmd == "approve":
        print(f"approved: {argv[2]}")
        return 0
    if cmd == "build":
        ws, name = ws_of(argv), argv[2]
        job = ws / "jobs" / name
        out = job / "output"
        out.mkdir(parents=True, exist_ok=True)
        (out / "render.png").write_bytes(b"\x89PNG-stub")
        (out / "model.glb").write_bytes(b"glTF-stub")
        (job / "reference.png").write_bytes(b"\x89PNG-ref")
        report = {"ok": True, "outputs": ["model.glb", "render.png"]}
        (job / "build-report.json").write_text(json.dumps(report))
        print(json.dumps(report))
        return 0
    if cmd == "file":
        print(f"filed: {ws_of(argv) / 'library' / argv[2]}")
        return 0
    if cmd == "search":
        print("stub-job  [scene]  Stub Job  files: model.glb, render.png")
        return 0
    if cmd == "images":
        ws = ws_of(argv)
        job = None
        if "--job" in argv:
            job = argv[argv.index("--job") + 1]
        bdir = ws / "images" / "stub-batch-120000"
        bdir.mkdir(parents=True, exist_ok=True)
        cands = []
        for i in (1, 2, 3):
            name = f"cand-{i:02d}.png"
            (bdir / name).write_bytes(b"\x89PNG-cand")
            cands.append({"file": name, "seed": 100 + i,
                          "status": "pending"})
        (bdir / "batch.json").write_text(json.dumps(
            {"prompt": "a stub prompt", "job": job,
             "candidates": cands, "reference_file": None,
             "rejected_seeds": []}))
        print(f"batch: {bdir}")
        for c in cands:
            print(f"  {c['file']}  seed {c['seed']}  pending")
        return 0
    if cmd == "decide":
        bdir, fname, decision = Path(argv[1]), argv[2], argv[3]
        batch = json.loads((bdir / "batch.json").read_text())
        rec = next(c for c in batch["candidates"] if c["file"] == fname)
        if decision == "reject":
            rec["status"] = "rejected"
            batch["rejected_seeds"].append(rec["seed"])
            (bdir / fname).unlink(missing_ok=True)
            if batch.get("reference_file") == fname:
                batch["reference_file"] = None
        else:
            rec["status"] = "kept"
            if batch.get("job") and not batch.get("reference_file"):
                ws = bdir.parent.parent
                job_dir = ws / "jobs" / batch["job"]
                job_dir.mkdir(parents=True, exist_ok=True)
                shutil_dest = job_dir / "reference.png"
                shutil_dest.write_bytes((bdir / fname).read_bytes())
                batch["reference_file"] = fname
            elif not batch.get("reference_file"):
                batch["reference_file"] = fname
        (bdir / "batch.json").write_text(json.dumps(batch))
        print(f"{fname}: {rec['status']}")
        if batch.get("reference_file"):
            print(f"job reference: {batch['reference_file']}")
        return 0
    if cmd == "reroll":
        print("  cand-04.png  seed 999  pending")
        return 0
    print(f"stub: unhandled {cmd}", file=sys.stderr)
    return 2

raise SystemExit(main())
'''


def make_shell(root: Path) -> Path:
    shell = root / "design-shell"
    pkg = shell / "designshell"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(STUB_MAIN)
    return shell


class BridgeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsbridge-"))
        self.shell = make_shell(self.tmp)
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.log_path = self.tmp / "stub-calls.jsonl"
        self._env = mock.patch.dict(os.environ, {
            "DSBRIDGE_STUB_LOG": str(self.log_path)})
        self._env.start()
        self.settings = {"design_shell_path": str(self.shell)}
        # The failure test flips the FAIL switch mid-test; keep it
        # OFF by default so a leaked value can never poison a
        # sibling subprocess (children inherit the test process env
        # through the bridge's inherited-environment rule).
        self._unfail = mock.patch.dict(os.environ,
                                       {"DSBRIDGE_STUB_FAIL": ""})
        self._unfail.start()

    def tearDown(self):
        self._env.stop()
        self._unfail.stop()

    def calls(self):
        if not self.log_path.exists():
            return []
        return [json.loads(line)
                for line in self.log_path.read_text().splitlines()]

    def bridge(self, **kw):
        # env defaults to the bridge's own default (inherit the
        # process env) — exactly the surface the stub's log path
        # and failure switch ride, and what production does with
        # the owner's env.
        kw.setdefault("home", self.tmp / "no-home")
        return B.Bridge(self.project, settings=self.settings, **kw)


class LocateShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dslocate-"))
        self.shell = make_shell(self.tmp)
        self.other = make_shell(self.tmp / "elsewhere")

    def test_settings_beats_env_beats_default(self):
        env = {"FORGE_DESIGN_SHELL": str(self.other)}
        got = B.locate_shell({"design_shell_path": str(self.shell)},
                             env=env, home=self.tmp / "no-home")
        self.assertEqual(got, self.shell)
        got = B.locate_shell({}, env=env, home=self.tmp / "no-home")
        self.assertEqual(got, self.other)
        # An INVALID Settings value falls through to the env
        # candidate — first VALID install wins, in order.
        got = B.locate_shell(
            {"design_shell_path": str(self.tmp / "missing")},
            env=env, home=self.tmp / "no-home")
        self.assertEqual(got, self.other)
        home = self.tmp / "home"
        default = make_shell(home)
        got = B.locate_shell({}, env={}, home=home)
        self.assertEqual(got, default)

    def test_run_sh_only_shape_counts(self):
        bare = self.tmp / "runsh-only"
        bare.mkdir()
        (bare / "run.sh").write_text("#!/bin/sh\n")
        got = B.locate_shell({"design_shell_path": str(bare)},
                             env={}, home=self.tmp / "no-home")
        self.assertEqual(got, bare)

    def test_invalid_candidates_fall_through_to_none(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        got = B.locate_shell({"design_shell_path": str(empty)},
                             env={"FORGE_DESIGN_SHELL": str(empty)},
                             home=self.tmp / "no-home")
        self.assertIsNone(got)


class PlanAndGateToolTests(BridgeCase):
    def test_plan_argv_job_and_stop_words(self):
        out = self.bridge().act(
            {"action": "plan", "words": "a wooden stool", "kind": "prop"})
        self.assertIn("Design Shell job drafted: stub-job", out)
        self.assertIn("plain-English summary", out)
        self.assertIn("STOP", out)
        calls = self.calls()
        self.assertEqual(calls[0][0], "init")
        self.assertEqual(calls[1][:2], ["plan", str(self.bridge().workspace())])
        self.assertEqual(calls[1][2:5], ["a", "wooden", "stool"])
        self.assertEqual(calls[1][5:], ["--kind", "prop"])
        self.assertEqual(calls[2][:2],
                         ["show", str(self.bridge().workspace())])
        self.assertEqual(calls[2][2], "stub-job")

    def test_model_issued_approve_is_refused_and_never_runs(self):
        out = self.bridge().act({"action": "approve", "job": "stub-job"})
        self.assertTrue(out.startswith("Refused"))
        self.assertIn("owner", out)
        self.assertEqual(self.calls(), [])

    def test_owner_gate_approve_runs_exactly_once(self):
        runner_calls = []

        def runner(argv, timeout, env):
            runner_calls.append(argv)
            return 0, "approved: stub-job", ""
        out = B.approve_from_gate(self.project, self.settings,
                                  "stub-job", runner=runner)
        self.assertIn("approved: stub-job", out)
        approves = [a for a in runner_calls if "approve" in a]
        self.assertEqual(len(approves), 1)
        self.assertEqual(approves[0][approves[0].index("approve") + 2],
                         "stub-job")

    def test_unknown_action_and_missing_words(self):
        self.assertIn("Unknown design_shell action",
                      self.bridge().act({"action": "explode"}))
        self.assertIn("needs 'words'",
                      self.bridge().act({"action": "plan"}))


class BusyGuardTests(BridgeCase):
    def _writing_runner(self, argv, timeout, env):
        if "build" in argv:
            job = Path(argv[argv.index("build") + 1]) / "jobs" / argv[-1]
            (job / "output").mkdir(parents=True, exist_ok=True)
            (job / "output" / "render.png").write_bytes(b"x")
            (job / "description.json").write_text("{}")
        return 0, "ok", ""

    def test_resident_model_refuses_build_but_not_search(self):
        ran = []

        def runner(argv, timeout, env):
            ran.append(argv)
            return self._writing_runner(argv, timeout, env)
        bridge = self.bridge(runner=runner,
                             ps_check=lambda: ["some/model:2b"])
        out = bridge.act({"action": "build", "job": "stub-job"})
        self.assertIn("Unload all", out)
        self.assertEqual(ran, [])
        out = bridge.act({"action": "search", "query": "stool"})
        self.assertTrue(any("search" in a for a in ran))

    def test_unreachable_ollama_fails_open(self):
        ran = []

        def runner(argv, timeout, env):
            ran.append(argv)
            return self._writing_runner(argv, timeout, env)
        bridge = self.bridge(runner=runner, ps_check=lambda: None)
        out = bridge.act({"action": "build", "job": "stub-job"})
        self.assertTrue(any("build" in a for a in ran))
        self.assertIn(".forge/design/stub-job/render.png", out)

    def test_resident_models_helper_fails_open(self):
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=OSError("down")):
            self.assertIsNone(B.resident_models(timeout=0.1))


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsimport-"))
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.ws = B.project_workspace(self.project)
        job = self.ws / "jobs" / "stool"
        (job / "output").mkdir(parents=True)
        (job / "output" / "render.png").write_bytes(b"png")
        (job / "output" / "model.glb").write_bytes(b"glb")
        (job / "description.json").write_text('{"name": "Stool"}')
        (job / "build-report.json").write_text('{"ok": true}')
        (job / "reference.png").write_bytes(b"ref")

    def test_workspace_lives_under_forge(self):
        self.assertEqual(self.ws,
                         self.project / ".forge" / "design-shell")

    def test_import_job_copies_with_lineage_originals_untouched(self):
        landed = B.import_job(self.project, self.ws, "stool")
        dest = self.project / ".forge" / "design" / "stool"
        for name in ("render.png", "model.glb", "reference.png",
                     "description.json", "build-report.json"):
            self.assertTrue((dest / name).is_file(), name)
            self.assertIn(f".forge/design/stool/{name}", landed)
        self.assertTrue(
            (self.ws / "jobs" / "stool" / "output" / "render.png").is_file())

    def test_import_job_without_outputs_is_empty(self):
        job = self.ws / "jobs" / "empty"
        (job / "output").mkdir(parents=True)
        self.assertEqual(B.import_job(self.project, self.ws, "empty"), [])

    def test_import_library_asset_and_missing_asset(self):
        lib = self.ws / "library" / "panel"
        lib.mkdir(parents=True)
        (lib / "panel.svg").write_text("<svg/>")
        (lib / "description.json").write_text("{}")
        (lib / "build-report.json").write_text("{}")
        (lib / "manifest.json").write_text(json.dumps(
            {"name": "Panel", "kind": "ui", "files": ["panel.svg"]}))
        landed = B.import_library_asset(self.project, self.ws, "panel")
        dest = self.project / ".forge" / "design" / "panel"
        self.assertTrue((dest / "panel.svg").is_file())
        self.assertTrue((dest / "manifest.json").is_file())
        self.assertIn(".forge/design/panel/panel.svg", landed)
        self.assertIsNone(
            B.import_library_asset(self.project, self.ws, "nope"))


class ImagesFlowTests(BridgeCase):
    def test_images_launches_picker_and_pick_status_tracks_decide(self):
        launched = []
        bridge = self.bridge(
            launcher=lambda argv, cwd: launched.append((argv, cwd)),
            ps_check=lambda: [])
        out = bridge.act({"action": "images", "prompt": "a red stool",
                          "job": "stub-job"})
        ws = bridge.workspace()
        bdir = ws / "images" / "stub-batch-120000"
        self.assertIn(f"batch: {bdir}", out)
        self.assertIn(B.PICKER_URL, out)
        self.assertEqual(len(launched), 1)
        argv, _cwd = launched[0]
        self.assertIn("pick", argv)
        self.assertIn(str(bdir), argv)
        self.assertIn("8762", argv)
        status = bridge.act({"action": "pick_status",
                             "batch": str(bdir)})
        self.assertIn("No keeper chosen yet", status)
        decided = bridge.act({"action": "decide", "batch": str(bdir),
                              "file": "cand-01.png", "decision": "keep"})
        self.assertIn("cand-01.png: kept", decided)
        status = bridge.act({"action": "pick_status",
                             "batch": str(bdir)})
        self.assertIn("Keeper: cand-01.png", status)
        self.assertIn("reference", status)
        self.assertTrue((ws / "jobs" / "stub-job" / "reference.png").is_file())


class FailureAndInstallTests(BridgeCase):
    def test_nonzero_exit_is_plain_words_with_shell_lines(self):
        with mock.patch.dict(os.environ, {"DSBRIDGE_STUB_FAIL": "1"}):
            out = self.bridge().act({"action": "search", "query": "x"})
        self.assertIn("The Design Shell reported a problem", out)
        self.assertIn("stub exploded", out)
        self.assertNotIn("Traceback", out)

    def test_not_installed_words(self):
        # env WITHOUT the stub log var but, critically, also
        # without any FORGE_DESIGN_SHELL: the settings path is the
        # only candidate and it names no install.
        bridge = B.Bridge(self.project,
                          settings={"design_shell_path": str(self.tmp / "nope")},
                          env={"PATH": os.environ.get("PATH", "")},
                          home=self.tmp / "no-home")
        out = bridge.act({"action": "plan", "words": "a stool"})
        self.assertIn("isn't installed/found", out)
        self.assertIn("Settings → Behavior", out)


class DesignGateWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        from forge.main import ForgeWindow
        cls.win = ForgeWindow()

    @classmethod
    def tearDownClass(cls):
        cls.win.close()

    def test_tool_registered_write_class(self):
        entry = self.win._extra_tools()["design_shell"]
        self.assertIs(entry[2], True)
        self.assertEqual(entry[3], "design shell")
        self.assertEqual(
            entry[0]["function"]["name"], "design_shell")

    def test_handler_refuses_model_approve(self):
        out = self.win._design_shell_tool(
            {"action": "approve", "job": "stub-job"}, self.win.workspace)
        self.assertTrue(out.startswith("Refused"))

    def test_gate_token_spent_exactly_once(self):
        from forge import main as main_mod
        win = self.win
        win._design_gate_from_result(
            "Design Shell job drafted: stub-job\n\nsummary")
        pending = win._design_gate_pending
        self.assertIsNotNone(pending)
        self.assertEqual(pending[0], "stub-job")
        self.assertTrue(win.act_design_shell_approve.isEnabled())
        calls = []

        def fake_approve(project_root, settings, job, runner=None):
            calls.append(job)
            return "approved: stub-job"
        with mock.patch.object(main_mod.dsbridge_mod,
                               "approve_from_gate", fake_approve):
            self.assertFalse(win._design_gate_approve("wrong-token"))
            self.assertEqual(calls, [])
            self.assertTrue(win._design_gate_approve(pending[1]))
            self.assertEqual(calls, ["stub-job"])
            self.assertFalse(win._design_gate_approve(pending[1]))
            self.assertEqual(calls, ["stub-job"])
        self.assertIsNone(win._design_gate_pending)
        self.assertFalse(win.act_design_shell_approve.isEnabled())


class SettingsDesignShellPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_field_round_trip(self):
        from forge.main import SettingsDialog
        dlg = SettingsDialog(None, {"design_shell_path": "/opt/shell"})
        self.assertEqual(dlg.design_shell_path.text(), "/opt/shell")
        dlg.design_shell_path.setText("/home/x/design-shell")
        self.assertEqual(dlg.values()["design_shell_path"],
                         "/home/x/design-shell")
        dlg2 = SettingsDialog(None, {})
        self.assertEqual(dlg2.design_shell_path.text(), "")
        self.assertEqual(dlg2.values()["design_shell_path"], "")
