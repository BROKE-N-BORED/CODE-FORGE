"""Light Machine profile (2026-11-11): one Settings toggle
(`light_machine`, default OFF) enforcing one local model at a time.

Covered, with fake clients/transports throughout (no real Ollama):

- the Settings → Behavior round-trip (toggle + caption present);
- seat generations carry keep_alive 0 while ON vs the pinned
  5m / session values while OFF (council reviewer, SKRIBE grading);
- single residency: ps() shows model A resident, seat B runs →
  A is unloaded BEFORE B's call; A == B → no unload; OFF → ps()
  is never even called;
- provider driver/seat generations are untouched (no ps/unload);
- the Knowledge distiller defers while the governor reads busy,
  runs when idle, and drops + logs past the bound;
- parallel effective concurrency is 1 while ON, the configured
  value while OFF (the configured value is never rewritten);
- local context budgets cap at 4096 while ON; a Groq model's
  TPM-capped budget is identical both ways;
- the tester seat, the bench, the Watch seat thread and the
  browser vision seat all route through the same rules.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from forge import lightmachine as light_mod
from forge import providers as providers_mod
from forge import knowledge as knowledge_mod
from forge import parallel as parallel_mod


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class _FakeEventLog:
    def __init__(self):
        self.events = []

    def record(self, kind, **fields):
        self.events.append((kind, fields))

    def kinds(self):
        return [k for k, _f in self.events]


class _SeatClient:
    """Routes by model like the council test doubles, recording
    (kind, model, keep_alive) for every call in order — ps(),
    unload_model() and chat_stream() share one tape so residency
    ORDER (unload before the seat's call) is assertable."""

    def __init__(self, driver_script, reviewer="rev-model",
                 resident=()):
        self.script = list(driver_script)
        self.reviewer = reviewer
        self.tape = []
        self.resident = list(resident)

    def tags(self):
        return [{"name": "fake-driver"}, {"name": self.reviewer}]

    def ps(self):
        self.tape.append(("ps", None, None))
        return [{"name": n} for n in self.resident]

    def unload_model(self, model):
        self.tape.append(("unload", model, None))
        return True

    def chat_stream(self, model, messages, options, schemas,
                    keep_alive=None, cancel_event=None):
        self.tape.append(("chat", model, keep_alive))
        if model == self.reviewer:
            yield {"message": {"content": "WRONG: nothing. "
                                          "CHANGE: add a test.",
                               "tool_calls": []}}
            return
        item = self.script.pop(0) if self.script else {"content": "end"}
        yield {"message": {"content": item["content"], "tool_calls": []}}

    def chats(self):
        return [(m, k) for kind, m, k in self.tape if kind == "chat"]

    def ps_calls(self):
        return sum(1 for kind, _m, _k in self.tape if kind == "ps")

    def unloads(self):
        return [m for kind, m, _k in self.tape if kind == "unload"]


class SettingsRoundTripTests(unittest.TestCase):
    def test_toggle_round_trips_and_caption_is_plain(self):
        _app()
        from forge.main import SettingsDialog
        dialog = SettingsDialog(None, {})
        self.assertFalse(dialog.light_machine.isChecked())
        self.assertFalse(dialog.values()["light_machine"])
        # The caption names the trade in plain words.
        texts = []
        from PySide6.QtWidgets import QLabel
        for label in dialog.findChildren(QLabel):
            texts.append(label.text())
        joined = " ".join(texts)
        self.assertIn("one model in memory at a time", joined)
        dialog.light_machine.setChecked(True)
        self.assertTrue(dialog.values()["light_machine"])
        dialog2 = SettingsDialog(None, dialog.values())
        self.assertTrue(dialog2.light_machine.isChecked())


class WorkerSeatTests(unittest.TestCase):
    def _worker(self, client, model="fake-driver", options=None,
                **kw):
        from forge.worker import AgentWorker
        return AgentWorker(
            client, model, [{"role": "user", "content": "build it"}],
            options or {}, tempfile.mkdtemp(),
            eventlog=_FakeEventLog(), skribe_writes=False, **kw)

    def test_council_reviewer_keep_alive_zero_when_on(self):
        client = _SeatClient([{"content": "first draft"},
                              {"content": "revised draft"}])
        worker = self._worker(
            client, options={"light_machine": True},
            council=True, reviewer_model="rev-model",
            governing_roster="fake-driver, rev-model")
        worker.run()
        self.assertEqual(client.chats(), [("fake-driver", "30m"),
                                          ("rev-model", 0),
                                          ("fake-driver", "30m")])

    def test_council_reviewer_keep_alive_pinned_when_off(self):
        client = _SeatClient([{"content": "first draft"},
                              {"content": "revised draft"}])
        worker = self._worker(
            client, council=True, reviewer_model="rev-model",
            governing_roster="fake-driver, rev-model")
        worker.run()
        self.assertEqual(client.chats(), [("fake-driver", "30m"),
                                          ("rev-model", "5m"),
                                          ("fake-driver", "30m")])
        self.assertEqual(client.ps_calls(), 0)
        self.assertEqual(client.unloads(), [])

    def test_residency_swap_unloads_before_the_seat_call(self):
        client = _SeatClient([{"content": "first draft"},
                              {"content": "revised draft"}],
                             resident=["fake-driver"])
        worker = self._worker(
            client, options={"light_machine": True},
            council=True, reviewer_model="rev-model",
            governing_roster="fake-driver, rev-model")
        worker.run()
        tape = client.tape
        unload_at = next(i for i, t in enumerate(tape)
                         if t[0] == "unload")
        seat_at = next(i for i, t in enumerate(tape)
                       if t[0] == "chat" and t[1] == "rev-model")
        self.assertLess(unload_at, seat_at)
        self.assertEqual(tape[unload_at][1], "fake-driver")
        # Logged once per swap, in plain words.
        notes = [f.get("note", "") for k, f in
                 worker.eventlog.events if k == "light_machine_unload"]
        self.assertTrue(any("unloaded fake-driver so rev-model "
                            "could run" in n for n in notes))

    def test_no_unload_when_the_resident_model_is_the_seat(self):
        client = _SeatClient([{"content": "first draft"},
                              {"content": "revised draft"}],
                             resident=["rev-model"])
        worker = self._worker(
            client, options={"light_machine": True},
            council=True, reviewer_model="rev-model",
            governing_roster="fake-driver, rev-model")
        worker.run()
        # The DRIVER generation unloads rev-model (a different
        # model than the driver — correct single residency); the
        # reviewer generation's own preparation, with rev-model
        # already the resident target, unloads nothing.
        seat_at = next(i for i, t in enumerate(client.tape)
                       if t[0] == "chat" and t[1] == "rev-model")
        driver_at = max(i for i, t in enumerate(client.tape[:seat_at])
                        if t[0] == "chat")
        seat_prep = client.tape[driver_at + 1:seat_at]
        self.assertIn(("ps", None, None), seat_prep)
        self.assertFalse([t for t in seat_prep if t[0] == "unload"])

    def test_provider_driver_untouched_when_on(self):
        client = _SeatClient([{"content": "done"}])
        worker = self._worker(
            client, model="groq:openai/gpt-oss-20b",
            options={"light_machine": True, "keep_alive": "30m"})
        worker.run()
        self.assertEqual(client.ps_calls(), 0)
        self.assertEqual(client.unloads(), [])
        self.assertEqual(client.chats(),
                         [("groq:openai/gpt-oss-20b", "30m")])

    def test_provider_reviewer_untouched_when_on(self):
        reviewer = "groq:openai/gpt-oss-20b"
        client = _SeatClient([{"content": "first draft"},
                              {"content": "revised draft"}],
                             reviewer=reviewer,
                             resident=["fake-driver"])
        worker = self._worker(
            client, options={"light_machine": True},
            council=True, reviewer_model=reviewer,
            governing_roster="fake-driver, " + reviewer)
        worker.run()
        reviewer_chats = [(m, k) for m, k in client.chats()
                          if m == reviewer]
        self.assertEqual(reviewer_chats, [(reviewer, "5m")])

    def test_skribe_grading_generation_is_a_seat_when_on(self):
        from forge.worker import AgentWorker
        # A worker whose queue holds one written file: the first
        # generation is main, the grading generation must carry 0.
        client = _SeatClient([{"content": "wrote it"},
                              {"content": "x.py — finalize — FZI 90"},
                              {"content": "final"}])
        worker = AgentWorker(
            client, "fake-driver",
            [{"role": "user", "content": "write x.py"}],
            {"light_machine": True}, tempfile.mkdtemp(),
            eventlog=_FakeEventLog(), skribe_writes=True,
            governing_roster="fake-driver")
        worker._skribe_queue.add("x.py")
        # Drive just the grading seam: _skribe_step queues the
        # grading instruction, then one _generate IS the grade.
        self.assertTrue(worker._skribe_step(""))
        text, _calls = worker._generate([], phase="grading")
        self.assertEqual(client.chats()[-1], ("fake-driver", 0))


class ResidencyHelperTests(unittest.TestCase):
    class _Client:
        def __init__(self, resident):
            self.resident = resident
            self.calls = []

        def ps(self):
            self.calls.append("ps")
            return [{"name": n} for n in self.resident]

        def unload_model(self, model):
            self.calls.append(("unload", model))
            return True

    def test_swap_order_and_log_line(self):
        client = self._Client(["model-a"])
        logged = []
        out = light_mod.ensure_single_residency(
            client, "model-b", True, log=logged.append)
        self.assertEqual(out, ["model-a"])
        self.assertEqual(client.calls, ["ps", ("unload", "model-a")])
        self.assertEqual(logged, ["Light machine: unloaded model-a "
                                  "so model-b could run"])

    def test_same_model_and_off_and_provider_are_noops(self):
        client = self._Client(["model-b"])
        self.assertEqual(light_mod.ensure_single_residency(
            client, "model-b", True), [])
        self.assertEqual(client.calls, ["ps"])  # ps read, no unload
        client2 = self._Client(["model-a"])
        self.assertEqual(light_mod.ensure_single_residency(
            client2, "model-b", False), [])
        self.assertEqual(client2.calls, [])  # OFF: ps never called
        client3 = self._Client(["model-a"])
        self.assertEqual(light_mod.ensure_single_residency(
            client3, "gemini:gemini-3.8-flash", True), [])
        self.assertEqual(client3.calls, [])  # provider: never called

    def test_unload_via_generate_payload(self):
        from forge.ollama_client import OllamaClient
        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["payload"] = json.loads(req.data.decode())
            return _Resp()

        client = OllamaClient("http://127.0.0.1:11434")
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertTrue(client.unload_model("m:2b"))
        self.assertTrue(captured["url"].endswith("/api/generate"))
        self.assertEqual(captured["payload"],
                         {"model": "m:2b", "keep_alive": 0})


class ContextBudgetTests(unittest.TestCase):
    class _LocalClient:
        def context_length(self, model):
            return 32768

    def test_local_budget_capped_only_when_on(self):
        client = self._LocalClient()
        off = providers_mod.resolve_context_budget(
            "local-model:7b", config=None, client=client,
            fallback=16384)
        on = providers_mod.resolve_context_budget(
            "local-model:7b", config=None, client=client,
            fallback=16384, light_machine=True)
        self.assertEqual(off, 16384)
        self.assertEqual(on, 4096)

    def test_groq_tpm_budget_identical_both_ways(self):
        config = {"model_registry": [{
            "id": "groq:openai/gpt-oss-20b", "enabled": True,
            "scope": "both", "roles": ["driver"],
            "context_window": 131072}]}
        off = providers_mod.resolve_context_budget(
            "groq:openai/gpt-oss-20b", config=config, client=None,
            fallback=8192)
        on = providers_mod.resolve_context_budget(
            "groq:openai/gpt-oss-20b", config=config, client=None,
            fallback=8192, light_machine=True)
        self.assertEqual(off, on)
        self.assertEqual(off, 8000 - 2048 - 500)  # the TPM cap math


class DistillerTests(unittest.TestCase):
    class _Client:
        def __init__(self):
            self.calls = []

        def chat_stream(self, model, messages, options, tools,
                        **kw):
            self.calls.append(kw)
            yield {"message": {"content":
                               "TITLE: T\nSUMMARY: S\nbody"},
                   "done": True}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        from forge import projects as proj
        proj.ensure_project(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_distill_keep_alive_zero_when_on_absent_when_off(self):
        client = self._Client()
        out = knowledge_mod.distill(
            client, "fast-seat", self.root, "q", "a long reply",
            light_machine=True)
        self.assertIsNotNone(out)
        self.assertEqual(client.calls[0].get("keep_alive"), 0)
        client2 = self._Client()
        knowledge_mod.distill(
            client2, "fast-seat", self.root, "q", "a long reply")
        self.assertNotIn("keep_alive", client2.calls[0])

    def test_deferral_busy_then_idle_then_drop(self):
        busy = {"cpu_busy": True, "avail_gb": 8.0}
        idle = {"cpu_busy": False, "avail_gb": 8.0}
        snaps = [busy, busy, idle]
        clock = {"t": 0.0}
        client = self._Client()
        out = knowledge_mod.distill_when_idle(
            client, "fast-seat", self.root, "q", "a long reply",
            snapshot_fn=lambda: snaps.pop(0) if snaps else idle,
            sleep_fn=lambda s: clock.__setitem__(
                "t", clock["t"] + s),
            now_fn=lambda: clock["t"], max_wait=600, poll=30)
        self.assertIsNotNone(out)
        self.assertEqual(len(client.calls), 1)
        # Always busy → the bound drops it: no seat call, one log
        # line, no exception, no draft.
        client2 = self._Client()
        logged = []
        clock2 = {"t": 0.0}
        out2 = knowledge_mod.distill_when_idle(
            client2, "fast-seat", self.root, "q", "a long reply",
            log=logged.append,
            snapshot_fn=lambda: busy,
            sleep_fn=lambda s: clock2.__setitem__(
                "t", clock2["t"] + s),
            now_fn=lambda: clock2["t"], max_wait=90, poll=30)
        self.assertIsNone(out2)
        self.assertEqual(client2.calls, [])
        self.assertEqual(len(logged), 1)
        self.assertIn("stayed busy", logged[0])


class ParallelCapTests(unittest.TestCase):
    def test_effective_concurrency_values(self):
        self.assertEqual(light_mod.effective_concurrency(4, True), 1)
        self.assertEqual(light_mod.effective_concurrency(4, False), 4)
        self.assertEqual(light_mod.effective_concurrency(1, True), 1)

    def test_manager_cap_read_at_use_time(self):
        state = {"on": False}
        mgr = parallel_mod.ParallelManager(
            client_factory=lambda model: None,
            concurrency=3,
            light_machine_fn=lambda: state["on"])
        self.assertEqual(mgr.effective_concurrency, 3)
        state["on"] = True
        self.assertEqual(mgr.effective_concurrency, 1)
        # The configured value itself is never rewritten.
        self.assertEqual(mgr.concurrency, 3)


class TesterSeatTests(unittest.TestCase):
    def _run_tester(self, light):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "tester_plugin_lm",
            Path(__file__).parent.parent / "plugins" / "tester"
            / "plugin.py")
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        tape = []

        class _FakeOllama:
            def __init__(self, base_url, timeout=None):
                tape.append(("init", base_url))

            def tags(self):
                return [{"name": "eriktonon/bonsai-2-uncensored:2b"}]

            def ps(self):
                tape.append(("ps", None))
                return [{"name": "other-model:2b"}]

            def unload_model(self, model):
                tape.append(("unload", model))
                return True

            def chat_stream(self, model, messages, options, tools,
                            keep_alive=None):
                tape.append(("chat", keep_alive))
                yield {"message": {"content": "VERDICT: PASS",
                                   "tool_calls": []}}

        ctx = type("Ctx", (), {})()
        ctx.tester_model = "eriktonon/bonsai-2-uncensored:2b"
        ctx.governing_roster = ""
        ctx.tester_max_cycles = 0
        ctx.base_url = "http://127.0.0.1:11434"
        ctx.workspace = tempfile.mkdtemp()
        ctx.log = lambda msg: tape.append(("log", msg))
        ctx.cancel_event = None
        ctx.light_machine = light
        with mock.patch.object(plugin, "OllamaClient", _FakeOllama):
            report = plugin.run_tester({"task": "check it"}, ctx)
        self.assertIn("VERDICT: PASS", report)
        return tape

    def test_tester_seat_when_on(self):
        tape = self._run_tester(True)
        self.assertIn(("chat", 0), tape)
        kinds = [t[0] for t in tape]
        self.assertLess(kinds.index("unload"), kinds.index("chat"))

    def test_tester_seat_when_off_is_the_shipped_call(self):
        tape = self._run_tester(False)
        self.assertIn(("chat", None), tape)
        self.assertNotIn("ps", [t[0] for t in tape])
        self.assertNotIn("unload", [t[0] for t in tape])


class WatchSeatThreadTests(unittest.TestCase):
    class _Client:
        def __init__(self):
            self.tape = []

        def ps(self):
            self.tape.append("ps")
            return [{"name": "driver-model"}]

        def unload_model(self, model):
            self.tape.append(("unload", model))
            return True

        def chat_stream(self, model, messages, options, tools,
                        keep_alive=None, cancel_event=None):
            self.tape.append(("chat", keep_alive))
            yield {"message": {"content": "a fix", "tool_calls": []}}

    def test_watch_seat_thread_when_on(self):
        _app()
        from forge.main import SummarizeThread
        client = self._Client()
        thread = SummarizeThread(
            client, "watch-model",
            [{"role": "user", "content": "fix it"}], {},
            light_machine=True, seat=True)
        got = []
        thread.done.connect(got.append)
        thread.run()
        self.assertEqual(got, ["a fix"])
        self.assertEqual(client.tape[0], "ps")
        self.assertIn(("unload", "driver-model"), client.tape)
        self.assertIn(("chat", 0), client.tape)

    def test_fold_use_of_the_thread_is_untouched(self):
        _app()
        from forge.main import SummarizeThread
        client = self._Client()
        thread = SummarizeThread(
            client, "driver-model",
            [{"role": "user", "content": "summarize"}], {},
            light_machine=True, seat=False)
        thread.run()
        self.assertEqual(client.tape, [("chat", None)])


class BenchSeatTests(unittest.TestCase):
    class _Client:
        def __init__(self):
            self.tape = []
            self.timeout = None

        def ps(self):
            self.tape.append("ps")
            return [{"name": "other:2b"}]

        def unload_model(self, model):
            self.tape.append(("unload", model))
            return True

        def chat_stream(self, model, messages, options, tools,
                        keep_alive=None):
            self.tape.append(("chat", keep_alive))
            yield {"message": {"content": "done", "tool_calls": []},
                   "done": True}

    def test_bench_task_when_on_and_off(self):
        from forge import bench as bench_mod
        task = bench_mod.TASKS[0]
        client = self._Client()
        with tempfile.TemporaryDirectory() as tmp:
            bench_mod._run_one_task(task, "bench-model", client,
                                    None, tmp, light_machine=True)
        self.assertIn(("chat", 0), client.tape)
        self.assertIn(("unload", "other:2b"), client.tape)
        client2 = self._Client()
        with tempfile.TemporaryDirectory() as tmp:
            bench_mod._run_one_task(task, "bench-model", client2,
                                    None, tmp)
        self.assertEqual(client2.tape, [("chat", None)])


class VisionSeatTests(unittest.TestCase):
    def test_vision_payload_and_residency(self):
        from forge.browser import media
        calls = []

        def fake_json(path, payload=None, timeout=120):
            calls.append((path, payload))
            if path == "/api/ps":
                return {"models": [{"name": "driver:2b"}]}
            if payload and payload.get("prompt") is None:
                return {}
            return {"response": "a described image"}

        env = dict(os.environ)
        try:
            os.environ["FORGE_LIGHT_MACHINE"] = "1"
            with mock.patch.object(media, "pick_vision_model",
                                   lambda: "vision:2b"), \
                    mock.patch.object(media, "_ollama_json", fake_json):
                out = media.describe_image_bytes(b"pngbytes")
            self.assertEqual(out, "a described image")
            gen = [p for path, p in calls if path == "/api/generate"]
            self.assertTrue(any(p.get("keep_alive") == 0
                                and p.get("model") == "driver:2b"
                                for p in gen))
            describe = [p for p in gen if p.get("prompt")]
            self.assertEqual(describe[0].get("keep_alive"), 0)
            calls.clear()
            os.environ.pop("FORGE_LIGHT_MACHINE", None)
            with mock.patch.object(media, "pick_vision_model",
                                   lambda: "vision:2b"), \
                    mock.patch.object(media, "_ollama_json", fake_json):
                media.describe_image_bytes(b"pngbytes")
            gen = [p for path, p in calls if path == "/api/generate"]
            self.assertEqual(len(gen), 1)
            self.assertNotIn("keep_alive", gen[0])
        finally:
            os.environ.clear()
            os.environ.update(env)


class _UnloadAllClient:
    """Fake client for the manual reset: a scripted resident set
    (or a ps() that raises), recording every unload request."""

    def __init__(self, resident=(), ps_error=None, unload_ok=True):
        self._resident = [{"name": n} for n in resident]
        self._ps_error = ps_error
        self._unload_ok = unload_ok
        self.unloaded = []

    def ps(self):
        if self._ps_error is not None:
            raise self._ps_error
        return list(self._resident)

    def unload_model(self, model):
        self.unloaded.append(model)
        return self._unload_ok


class UnloadAllModelsTests(unittest.TestCase):
    """The manual "Unload all models" action (owner's tuning
    list): toggle-independent, locals only, one plain receipt
    line, fails soft."""

    def test_helper_unloads_every_resident_model(self):
        client = _UnloadAllClient(resident=("a:2b", "b:2b"))
        outcome, names = light_mod.unload_all_models(client)
        self.assertEqual(outcome, "unloaded")
        self.assertEqual(names, ["a:2b", "b:2b"])
        self.assertEqual(client.unloaded, ["a:2b", "b:2b"])

    def test_helper_empty_ps_is_none(self):
        client = _UnloadAllClient(resident=())
        outcome, names = light_mod.unload_all_models(client)
        self.assertEqual((outcome, names), ("none", []))
        self.assertEqual(client.unloaded, [])

    def test_helper_raising_ps_is_unreachable_not_an_exception(self):
        client = _UnloadAllClient(ps_error=RuntimeError("down"))
        outcome, names = light_mod.unload_all_models(client)
        self.assertEqual((outcome, names), ("unreachable", []))

    def test_helper_refused_unloads_report_failed(self):
        client = _UnloadAllClient(resident=("a:2b",), unload_ok=False)
        outcome, names = light_mod.unload_all_models(client)
        self.assertEqual(outcome, "failed")
        self.assertEqual(names, ["a:2b"])

    def _verb(self, client):
        from forge.main import ForgeWindow
        lines = []
        owner = type("Stub", (), {
            "client": client,
            "log": lambda self, line: lines.append(line)})()
        line = ForgeWindow.unload_all_models(owner)
        return line, lines

    def test_window_verb_receipts(self):
        line, lines = self._verb(
            _UnloadAllClient(resident=("a:2b", "b:2b")))
        self.assertEqual(line, "Unloaded 2 models — RAM freed.")
        self.assertEqual(lines, [line])
        line, _lines = self._verb(_UnloadAllClient(resident=()))
        self.assertEqual(line, "No models were loaded.")
        line, _lines = self._verb(
            _UnloadAllClient(ps_error=RuntimeError("down")))
        self.assertIn("couldn't reach Ollama", line)

    def test_models_window_has_the_button(self):
        _app()
        from PySide6.QtWidgets import QWidget
        from forge.main import ModelsWindow
        owner = QWidget()
        owner._models_data = []
        owner.settings = {}
        win = ModelsWindow(owner)
        try:
            self.assertEqual(win.unload_all_btn.text(),
                             "Unload all models")
            self.assertTrue(hasattr(win, "unload_note"))
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
