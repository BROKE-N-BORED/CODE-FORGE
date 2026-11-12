"""Header loaded-models readout (owner's order, 2026-11-11):
which LOCAL models sit in RAM right now and what they weigh,
shown beside the RAM/CPU readouts. The data is the gauge
snapshot's Ollama /api/ps rows — provider models never appear,
because they don't live in this PC's RAM. The readout rides the
existing 3 s gauge cadence; it owns no timer."""
import io
import json
import unittest
from unittest import mock

from forge import governor
from forge.main import models_readout


def _snap(loaded=(), sizes=None, running=True):
    return {
        "avail_gb": 3.0, "total_gb": 8.0, "load_1min": 1.0,
        "threads": 4, "cpu_busy": False,
        "ollama_running": running,
        "ollama_loaded": list(loaded),
        "ollama_loaded_sizes": dict(sizes or {}),
    }


class ModelsReadoutFormatterTests(unittest.TestCase):
    def test_none_loaded(self):
        text, tip = models_readout(_snap())
        self.assertEqual(text, "none loaded")
        self.assertIn("No local models", tip)

    def test_one_model(self):
        text, tip = models_readout(_snap(
            ["maternion/minicpm5-heretic:2b"],
            {"maternion/minicpm5-heretic:2b": 1_200_000_000}))
        self.assertEqual(text, "1 · 1.2 GB")
        self.assertEqual(tip, "maternion/minicpm5-heretic:2b (1.2 GB)")

    def test_two_models_sum_and_tooltip_lines(self):
        text, tip = models_readout(_snap(
            ["a/one:2b", "b/two:2b"], {"a/one:2b": 2_200_000_000,
                                       "b/two:2b": 1_200_000_000}))
        self.assertEqual(text, "2 · 3.4 GB")
        self.assertEqual(tip.splitlines(),
                         ["a/one:2b (2.2 GB)", "b/two:2b (1.2 GB)"])

    def test_unreachable_is_a_dash(self):
        text, tip = models_readout(_snap(running=False))
        self.assertEqual(text, "—")
        self.assertIn("recovers", tip)

    def test_missing_sizes_fall_back_to_names(self):
        text, tip = models_readout(_snap(["a/one:2b"], {}))
        self.assertEqual(text, "1 loaded")
        self.assertEqual(tip, "a/one:2b")

    def test_missing_keys_are_tolerated(self):
        # A snapshot dict from before the sizes key existed.
        text, _tip = models_readout({"ollama_running": True,
                                     "ollama_loaded": ["a/one:2b"]})
        self.assertEqual(text, "1 loaded")


class GovernorSnapshotSizesTests(unittest.TestCase):
    def test_snapshot_carries_sizes_and_keeps_the_tag_contract(self):
        payload = json.dumps({"models": [
            {"name": "a/one:2b", "size": 2_200_000_000},
            {"name": "b/two:2b", "size": 1_200_000_000},
        ]}).encode()
        with mock.patch.object(governor.urllib.request, "urlopen",
                               return_value=io.BytesIO(payload)):
            snap = governor.snapshot("http://127.0.0.1:11434", timeout=0.5)
        self.assertTrue(snap["ollama_running"])
        self.assertEqual(snap["ollama_loaded"], ["a/one:2b", "b/two:2b"])
        self.assertEqual(snap["ollama_loaded_sizes"],
                         {"a/one:2b": 2_200_000_000, "b/two:2b": 1_200_000_000})

    def test_unreachable_gives_empty_sizes_not_an_error(self):
        with mock.patch.object(governor.urllib.request, "urlopen",
                               side_effect=OSError("down")):
            snap = governor.snapshot("http://127.0.0.1:11434", timeout=0.5)
            reachable, tags = governor.ollama_loaded(
                "http://127.0.0.1:11434", timeout=0.5)
        self.assertFalse(snap["ollama_running"])
        self.assertEqual(snap["ollama_loaded"], [])
        self.assertEqual(snap["ollama_loaded_sizes"], {})
        self.assertEqual((reachable, tags), (False, []))


class HeaderModelsWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        from forge.main import ForgeWindow
        cls.win = ForgeWindow()
        cls.initial_models_text = cls.win.models_text.text()

    @classmethod
    def tearDownClass(cls):
        cls.win.close()

    def test_readout_starts_as_a_dash(self):
        # Captured in setUpClass, before any test drove a gauge
        # pass through the shared window.
        self.assertEqual(self.initial_models_text, "—")
        self.assertEqual(self.win.models_text.objectName(), "muted")

    def test_apply_gauges_drives_the_readout(self):
        win = self.win
        win._apply_gauges(_snap(
            ["a/one:2b", "b/two:2b"], {"a/one:2b": 2_200_000_000,
                                       "b/two:2b": 1_200_000_000}))
        self.assertEqual(win.models_text.text(), "2 · 3.4 GB")
        self.assertIn("a/one:2b (2.2 GB)", win.models_text.toolTip())
        self.assertIn("b/two:2b (1.2 GB)", win.models_text.toolTip())
        win._apply_gauges(_snap())
        self.assertEqual(win.models_text.text(), "none loaded")

    def test_failure_shows_dash_then_recovers(self):
        win = self.win
        win._apply_gauges(_snap(["a/one:2b"], {"a/one:2b": 1_000_000_000}))
        self.assertEqual(win.models_text.text(), "1 · 1.0 GB")
        win._apply_gauges(_snap(running=False))
        self.assertEqual(win.models_text.text(), "—")
        win._apply_gauges(_snap(["a/one:2b"], {"a/one:2b": 1_000_000_000}))
        self.assertEqual(win.models_text.text(), "1 · 1.0 GB")

    def test_refresh_gauges_full_path_updates_the_readout(self):
        import forge.main as main_mod
        win = self.win
        old_server = win.deck._server
        real_set_state = win.deck.set_state

        class InlineThread:
            def __init__(self, target=None, daemon=None):
                self._target = target

            def start(self):
                self._target()

        try:
            win.deck._server = object()
            win.deck.set_state = lambda **kw: None
            win._gauge_busy = False
            with mock.patch.object(main_mod.threading, "Thread", InlineThread), \
                    mock.patch.object(main_mod.governor, "snapshot",
                                      return_value=_snap(
                                          ["a/one:2b"],
                                          {"a/one:2b": 1_500_000_000})):
                win.refresh_gauges()
            self.assertEqual(win.models_text.text(), "1 · 1.5 GB")
        finally:
            win.deck.set_state = real_set_state
            win.deck._server = old_server

    def test_no_new_timer_for_the_readout(self):
        from PySide6.QtCore import QTimer
        win = self.win
        # The cadence it rides: the one existing gauge timer.
        self.assertEqual(win._gauge_timer.interval(), 3000)
        self.assertTrue(win._gauge_timer.isActive())
        before = len(win.findChildren(QTimer))
        win._apply_gauges(_snap(["a/one:2b"], {"a/one:2b": 1_000_000_000}))
        win._apply_gauges(_snap())
        self.assertEqual(len(win.findChildren(QTimer)), before)

    def test_readout_geometry_never_overlaps_its_neighbours(self):
        from PySide6.QtCore import QRect
        win = self.win
        win.resize(1220, 820)
        win.show()
        self.app.processEvents()

        def global_rect(w):
            tl = w.mapToGlobal(w.rect().topLeft())
            return QRect(tl, w.size())

        mine = global_rect(win.models_text)
        self.assertGreater(mine.width(), 0)
        for other in (win.ram_text, win.cpu_text, win.ram_bar,
                      win.cpu_bar, win.loaded_label, win.model_combo):
            self.assertFalse(
                mine.intersects(global_rect(other)),
                f"models readout overlaps {other.objectName() or other!r}")
        # And it stays inside the 74 px center header it was added to.
        frame = win.models_text.parentWidget()
        while frame is not None and frame.objectName() != "header":
            frame = frame.parentWidget()
        self.assertIsNotNone(frame)
        self.assertLessEqual(mine.bottom(), global_rect(frame).bottom())
        win.hide()


class LoadedLabelWindowTests(unittest.TestCase):
    """LOADED MODEL rename (owner's report, 2026-11-11): the old
    stamp read as "the model running your turn" while the dropdown
    held a provider model — it is the local model(s) sitting in
    RAM, from Ollama /api/ps. The stamp now says where the models
    are; the tooltip says which model runs the turn. Data and
    refresh are unchanged — only the words moved."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        from forge.main import ForgeWindow
        cls.win = ForgeWindow()

    @classmethod
    def tearDownClass(cls):
        cls.win.close()

    def test_stamp_says_in_ram_local(self):
        self.assertEqual(self.win.loaded_stamp.text(), "IN RAM (LOCAL)")
        self.assertEqual(self.win.loaded_stamp.objectName(), "section")

    def test_value_still_shows_the_resident_local_model(self):
        win = self.win
        # The live report's own case: Bonsai resident in RAM while
        # the dropdown holds a provider model. The value is the
        # resident local model, short-named — that data is unchanged.
        win._apply_gauges(_snap(
            ["eriktonon/bonsai-2-uncensored:2b"],
            {"eriktonon/bonsai-2-uncensored:2b": 1_000_000_000}))
        self.assertEqual(win.loaded_label.text(), "bonsai-2-uncensored:2b")
        # The MODELS row rides the same pass, untouched by the rename.
        self.assertEqual(win.models_text.text(), "1 · 1.0 GB")
        win._apply_gauges(_snap())
        self.assertEqual(win.loaded_label.text(), "none")

    def test_tooltip_explains_the_distinction(self):
        tip = ("Local models sitting in this PC's RAM right now. "
               "The model running your turn is the one in the dropdown.")
        self.assertEqual(self.win.loaded_stamp.toolTip(), tip)
        self.assertEqual(self.win.loaded_label.toolTip(), tip)

    def test_no_old_stamp_text_remains(self):
        from PySide6.QtWidgets import QLabel
        for label in self.win.findChildren(QLabel):
            self.assertNotEqual(label.text(), "LOADED MODEL")

    def test_new_stamp_geometry_never_overlaps_its_neighbours(self):
        from PySide6.QtCore import QRect
        win = self.win

        def global_rect(w):
            tl = w.mapToGlobal(w.rect().topLeft())
            return QRect(tl, w.size())

        def header_frame(w):
            frame = w.parentWidget()
            while frame is not None and frame.objectName() != "header":
                frame = frame.parentWidget()
            return frame

        win._apply_gauges(_snap(
            ["eriktonon/bonsai-2-uncensored:2b"],
            {"eriktonon/bonsai-2-uncensored:2b": 1_000_000_000}))
        try:
            for width in (1220, 1600):
                win.resize(width, 820)
                win.show()
                self.app.processEvents()
                frame = header_frame(win.loaded_label)
                self.assertIsNotNone(frame, "header frame not found")
                frame_rect = global_rect(frame)
                for mine in (win.loaded_stamp, win.loaded_label):
                    mine_rect = global_rect(mine)
                    self.assertGreater(mine_rect.width(), 0)
                    for other in (win.ram_text, win.cpu_text, win.ram_bar,
                                  win.cpu_bar, win.models_text,
                                  win.model_combo, win.privacy_badge):
                        self.assertFalse(
                            mine_rect.intersects(global_rect(other)),
                            f"{mine.text()} overlaps "
                            f"{other.objectName() or other!r} at {width}px")
                    self.assertLessEqual(mine_rect.bottom(), frame_rect.bottom())
                    self.assertGreaterEqual(mine_rect.top(), frame_rect.top())
        finally:
            win.hide()


if __name__ == "__main__":
    unittest.main()
