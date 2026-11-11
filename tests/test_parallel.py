"""Parallel agent jobs (2026-11-11): isolation, the merge gate,
stop/governor semantics, and persistence — all with scripted
runners and fake clients. No real model is ever called."""

import os
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from forge import parallel as par


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, timeout=60)


def wait_for(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


class ParallelBase(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t"})
        self._env.start()
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="forge-parallel-"))
        self.project = self.tmp / "proj"
        self.project.mkdir()

    def tearDown(self):
        self._env.stop()

    def make_repo(self):
        git(self.project, "init", "-b", "main")
        (self.project / "app.py").write_text("x = 1\n")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-m", "initial")
        return self.project

    def manager(self, runner=None, concurrency=2, snapshot_fn=None):
        return par.ParallelManager(
            client_factory=lambda model: object(),
            snapshot_fn=snapshot_fn or (lambda: None),
            concurrency=concurrency,
            runner=runner or self._writing_runner)

    @staticmethod
    def _writing_runner(client, model, task, root, kind, cancel,
                        step_cb=None):
        name = task.split()[-1]  # task text ends with the filename
        (Path(root) / name).write_text(f"made by {task}\n")
        if step_cb:
            step_cb("wrote " + name)
        return f"done: {name}"


class RefusalTests(ParallelBase):
    def test_code_job_refused_without_git(self):
        mgr = self.manager()
        with self.assertRaises(RuntimeError) as ctx:
            mgr.submit("write a.py", str(self.project), "m")
        self.assertIn("not a git repository", str(ctx.exception))

    def test_code_job_refused_without_commits(self):
        git(self.project, "init", "-b", "main")
        mgr = self.manager()
        with self.assertRaises(RuntimeError) as ctx:
            mgr.submit("write a.py", str(self.project), "m")
        self.assertIn("no commits", str(ctx.exception))


class IsolationAndGateTests(ParallelBase):
    def test_two_jobs_isolated_discard_and_approve(self):
        self.make_repo()
        mgr = self.manager()
        a = mgr.submit("job a alpha.py", str(self.project), "m")
        b = mgr.submit("job b beta.py", str(self.project), "m")
        self.assertTrue(wait_for(
            lambda: mgr.get(a["id"])["state"] == "done"
            and mgr.get(b["id"])["state"] == "done"))
        # Isolation: neither file is in the owner's tree, and the
        # two worktrees are different folders.
        self.assertFalse((self.project / "alpha.py").exists())
        self.assertFalse((self.project / "beta.py").exists())
        self.assertNotEqual(a["worktree"], b["worktree"])
        self.assertIn("alpha.py",
                      par.read_diff(mgr.get(a["id"])))
        # Discard lands nothing and removes the copy.
        a_wt = a["worktree"]
        mgr.discard(a["id"])
        self.assertEqual(mgr.get(a["id"])["state"], "discarded")
        self.assertFalse((self.project / "alpha.py").exists())
        self.assertFalse(Path(a_wt).exists())
        # Approve lands exactly the job's diff.
        landed, _msg = mgr.approve(b["id"])
        self.assertTrue(landed)
        self.assertEqual(mgr.get(b["id"])["state"], "merged")
        self.assertEqual((self.project / "beta.py").read_text(),
                         "made by job b beta.py\n")

    def test_merge_conflict_surfaces_and_lands_nothing(self):
        self.make_repo()

        def editor(client, model, task, root, kind, cancel,
                   step_cb=None):
            (Path(root) / "app.py").write_text("x = 99\n")
            return "edited"

        mgr = self.manager(runner=editor)
        job = mgr.submit("edit app", str(self.project), "m")
        self.assertTrue(wait_for(
            lambda: mgr.get(job["id"])["state"] == "done"))
        # The owner changes the same line in the real tree.
        (self.project / "app.py").write_text("x = 42\n")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-m", "owner edit")
        landed, message = mgr.approve(job["id"])
        self.assertFalse(landed)
        self.assertEqual(mgr.get(job["id"])["state"], "merge_failed")
        self.assertIn("app.py", message)
        # The tree is untouched by the failed merge and not left
        # mid-merge; the job's copy survives for inspection.
        self.assertEqual((self.project / "app.py").read_text(),
                         "x = 42\n")
        self.assertFalse((self.project / ".git" / "MERGE_HEAD")
                         .exists())
        self.assertTrue(Path(job["worktree"]).exists())

    def test_unapproved_diff_cannot_land_via_discard_path(self):
        self.make_repo()
        mgr = self.manager()
        job = mgr.submit("job c gamma.py", str(self.project), "m")
        self.assertTrue(wait_for(
            lambda: mgr.get(job["id"])["state"] == "done"))
        before = git(self.project, "status", "--porcelain").stdout
        mgr.discard(job["id"])
        self.assertFalse((self.project / "gamma.py").exists())
        after = git(self.project, "status", "--porcelain").stdout
        self.assertEqual(before, after)


class RunnerJailTests(ParallelBase):
    class FakeClient:
        def __init__(self, calls):
            self._calls = calls
            self._n = 0
            self.timeout = None

        def chat_stream(self, model, messages, options, tools,
                        cancel_event=None):
            self._n += 1
            if self._n <= len(self._calls):
                yield {"message": {"content": "",
                                   "tool_calls": self._calls[
                                       self._n - 1]},
                       "done": True}
            else:
                yield {"message": {"content": "finished",
                                   "tool_calls": []}, "done": True}

    def test_escape_write_never_lands_outside_the_worktree(self):
        self.make_repo()
        mgr = self.manager()
        job = mgr.submit("escape test", str(self.project), "m")
        wt = Path(job["worktree"])
        escape_call = [{"function": {
            "name": "write_file",
            "arguments": {"path": "../escape.txt",
                          "content": "escaped"}}}]
        client = self.FakeClient([escape_call])
        text = par.run_job_turn(client, "m", "escape test", wt,
                                "code", threading.Event())
        self.assertEqual(text, "finished")
        self.assertFalse((self.project / "escape.txt").exists())
        self.assertFalse((self.tmp / "escape.txt").exists())
        mgr.discard(job["id"])

    def test_readonly_belt_has_no_write_tools(self):
        names = [s["function"]["name"]
                 for s in par._schemas(par.READONLY_TOOL_NAMES)]
        self.assertNotIn("write_file", names)
        self.assertNotIn("run_command", names)


class StopAndQueueTests(ParallelBase):
    def _blocking_runner(self, started):
        def run(client, model, task, root, kind, cancel,
                step_cb=None):
            (Path(root) / "partial.txt").write_text("partial\n")
            started.set()
            while not cancel.is_set():
                time.sleep(0.02)
            raise par._JobStopped()
        return run

    def test_stop_leaves_partial_work_for_inspection(self):
        self.make_repo()
        started = threading.Event()
        mgr = self.manager(runner=self._blocking_runner(started))
        job = mgr.submit("slow job", str(self.project), "m")
        self.assertTrue(started.wait(10))
        mgr.stop(job["id"])
        self.assertTrue(wait_for(
            lambda: mgr.get(job["id"])["state"] == "stopped"))
        wt = Path(job["worktree"])
        self.assertTrue((wt / "partial.txt").exists())

    def test_governor_holds_the_queue(self):
        self.make_repo()
        snap = {"cpu_busy": True, "avail_gb": 8.0}
        mgr = self.manager(snapshot_fn=lambda: snap)
        job = mgr.submit("held job", str(self.project), "m")
        time.sleep(0.3)
        held = mgr.get(job["id"])
        self.assertEqual(held["state"], "queued")
        self.assertIn("governor", held["note"])
        snap["cpu_busy"] = False
        mgr._snap_cache = (0.0, None)
        mgr.pump()
        self.assertTrue(wait_for(
            lambda: mgr.get(job["id"])["state"] == "done"))

    def test_concurrency_cap_queues_the_second_job(self):
        self.make_repo()
        started = threading.Event()
        mgr = self.manager(runner=self._blocking_runner(started),
                           concurrency=1)
        first = mgr.submit("first job", str(self.project), "m")
        self.assertTrue(started.wait(10))
        second = mgr.submit("second job", str(self.project), "m")
        time.sleep(0.3)
        self.assertEqual(mgr.get(second["id"])["state"], "queued")
        mgr.stop_all()
        self.assertTrue(wait_for(
            lambda: mgr.get(first["id"])["state"] == "stopped"))


class PersistenceTests(ParallelBase):
    def test_interrupted_jobs_survive_a_restart(self):
        self.make_repo()
        mgr = self.manager()
        job = mgr.submit("job d delta.py", str(self.project), "m")
        self.assertTrue(wait_for(
            lambda: mgr.get(job["id"])["state"] == "done"))
        # Simulate a crash mid-flight: rewrite the record as
        # running, then attach with a fresh manager.
        store = par.JobStore(self.project)
        record = store.get(job["id"])
        record["state"] = "running"
        store.upsert(record)
        mgr2 = self.manager()
        mgr2.attach_project(str(self.project))
        loaded = mgr2.get(job["id"])
        self.assertEqual(loaded["state"], "interrupted")
        self.assertTrue(Path(job["worktree"]).exists())
        mgr2.discard(job["id"])
        self.assertEqual(mgr2.get(job["id"])["state"], "discarded")

    def test_readonly_job_files_its_result(self):
        self.make_repo()
        mgr = self.manager(
            runner=lambda *a, **k: "the answer is 42")
        job = mgr.submit("what is the answer", str(self.project),
                         "m", kind="readonly", mode="research")
        self.assertTrue(wait_for(
            lambda: mgr.get(job["id"])["state"] == "done"))
        self.assertEqual(job["worktree"], "")
        filed = Path(self.project) / ".forge" / "parallel" \
            / "results" / f"{job['id']}.md"
        self.assertEqual(filed.read_text(), "the answer is 42")
        research = list((Path(self.project) / ".forge" / "research")
                        .glob("parallel-*.md"))
        self.assertEqual(len(research), 1)


class MissionControlJobsTests(unittest.TestCase):
    """The window gains a jobs section; a stub owner keeps this
    about the wiring, not the whole app."""

    def test_jobs_section_exists_and_renders_rows(self):
        try:
            from PySide6.QtWidgets import QApplication
        except Exception:
            self.skipTest("PySide6 unavailable")
        import sys
        app = QApplication.instance() or QApplication(sys.argv)
        from forge.main import MissionControlWindow
        from PySide6.QtWidgets import QWidget

        class StubOwner(QWidget):
            settings = {}
            projects = []

            def mission_rows(self):
                return []

            def parallel_job_rows(self):
                return [{"id": "j1", "state": "done",
                         "line": "[done] task — model · proj",
                         "job": {}}]

        parent = QWidget()
        stub = StubOwner(parent)
        win = MissionControlWindow(stub)
        self.assertTrue(hasattr(win, "jobs_list"))
        self.assertTrue(hasattr(win, "new_job_btn"))
        self.assertTrue(hasattr(win, "approve_job_btn"))
        win._refresh_jobs()
        self.assertEqual(win.jobs_list.count(), 1)
        self.assertIn("task", win.jobs_list.item(0).text())
        win.close()
        parent.close()


if __name__ == "__main__":
    unittest.main()
