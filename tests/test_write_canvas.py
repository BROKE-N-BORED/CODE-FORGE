"""Canvas build tests for the Write window (owner's directive,
2026-11-11): the Write room was a ModeWindow frame with an Editor
tab — the same generic room as every other mode. The spec he
approved 2026-11-09 (Write/Canvas half) is a Canvas: conversation
beside ONE persistent document, three edit paths (type / prompt /
select-and-instruct), suggest-edits cards applied one by one,
length/tone steppers, and version stepping over the document's
own snapshots.

These tests pin the Canvas mechanics with a fake bench (the
ModeRoomToolsTests pattern): the split is the window's face, an
unapplied card never touches the document, Apply lands exactly
one passage with the prior version snapshotted, Dismiss changes
nothing, selection-scoped turns carry only the passage, the
stepper instruction is shell-built per step, and auto-save writes
the real file while feeding the version history.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class WriteCanvasTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _bare_owner(self, ws):
        from PySide6.QtWidgets import QWidget
        owner = QWidget()
        owner.workspace = str(ws)
        return owner

    def _bench_owner(self, ws):
        from PySide6.QtWidgets import QComboBox, QPlainTextEdit, QWidget
        owner = QWidget()
        owner.workspace = str(ws)
        owner.mode_combo = QComboBox()
        owner.mode_combo.addItems(
            ["Code", "Plan", "Design", "Architect", "Review",
             "Ask", "Write", "Research"])
        owner.input = QPlainTextEdit()
        owner.send_calls = []
        owner.send_message = lambda: owner.send_calls.append(
            owner.input.toPlainText())
        return owner

    def _window_with_doc(self, owner=None):
        from forge.main import WriteWindow
        ws = Path(tempfile.mkdtemp())
        (ws / "notes").mkdir()
        target = ws / "notes" / "letter.md"
        target.write_text("Alpha one. Beta two. Gamma three.\n",
                          encoding="utf-8")
        win = WriteWindow(owner or self._bare_owner(ws), "write")
        win.set_project(str(ws))
        win.doc_path_edit.setText("notes/letter.md")
        win.open_document()
        return win, ws, target

    # -- the split is the window's face --------------------------
    def test_window_opens_on_canvas_split(self):
        from PySide6.QtWidgets import QPlainTextEdit
        from forge.main import WriteWindow
        ws = Path(tempfile.mkdtemp())
        win = WriteWindow(self._bare_owner(ws), "write")
        # One tab, and it is the canvas split: conversation,
        # document, suggestions side by side.
        self.assertEqual(
            win.tabs.tabText(win.tabs.currentIndex()), "Editor")
        self.assertEqual(win.canvas_splitter.count(), 3)
        self.assertIsInstance(win.doc_editor, QPlainTextEdit)
        self.assertIsInstance(win.composer, QPlainTextEdit)
        self.assertEqual(win.suggest_btn.text(), "Suggest edits")
        self.assertEqual(win.length_combo.currentText(), "Same")
        self.assertEqual(win.tone_combo.currentText(), "Same")
        win.close()

    # -- suggest-edits cards --------------------------------------
    def test_unapplied_cards_never_touch_the_document(self):
        win, ws, target = self._window_with_doc()
        before = target.read_text(encoding="utf-8")
        win._add_suggestion_cards([("Beta two.", "Beta TWO.")])
        self.assertEqual(len(win._suggestion_cards), 1)
        self.assertEqual(target.read_text(encoding="utf-8"), before)
        self.assertEqual(win.doc_editor.toPlainText(), before)
        win.close()

    def test_dismiss_changes_nothing(self):
        win, ws, target = self._window_with_doc()
        before = target.read_text(encoding="utf-8")
        win._add_suggestion_cards([("Beta two.", "Beta TWO.")])
        win.dismiss_suggestion(win._suggestion_cards[0])
        self.assertEqual(win._suggestion_cards, [])
        self.assertEqual(target.read_text(encoding="utf-8"), before)
        self.assertEqual(win.doc_editor.toPlainText(), before)
        win.close()

    def test_apply_changes_only_that_passage_and_snapshots(self):
        win, ws, target = self._window_with_doc()
        win._add_suggestion_cards([("Beta two.", "Beta TWO.")])
        win.apply_suggestion(win._suggestion_cards[0])
        self.assertEqual(win._suggestion_cards, [])
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "Alpha one. Beta TWO. Gamma three.\n")
        self.assertEqual(win.doc_editor.toPlainText(),
                         "Alpha one. Beta TWO. Gamma three.\n")
        # The version before the applied edit is in the history.
        history = ws / ".forge" / "write-history" / "notes__letter.md"
        bodies = [p.read_text(encoding="utf-8")
                  for p in history.glob("*.txt")]
        self.assertIn("Alpha one. Beta two. Gamma three.\n", bodies)
        win.close()

    def test_apply_refuses_a_stale_card(self):
        win, ws, target = self._window_with_doc()
        win._add_suggestion_cards([("Not in the doc.", "Nope.")])
        win.apply_suggestion(win._suggestion_cards[0])
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "Alpha one. Beta two. Gamma three.\n")
        self.assertIn("exactly once", win.editor_status.text())
        win.close()

    def test_parse_suggestion_blocks(self):
        from forge.main import WriteWindow
        reply = ("Here you go:\n"
                 "--- OLD ---\nBeta two.\n--- NEW ---\nBeta TWO.\n"
                 "--- OLD ---\nGamma three.\n--- NEW ---\n"
                 "Gamma three.\n"  # no-op: skipped
                 "--- OLD ---\nMissing new marker\n")
        self.assertEqual(
            WriteWindow.parse_suggestion_blocks(reply),
            [("Beta two.", "Beta TWO.")])

    def test_suggest_request_and_harvest_into_cards(self):
        ws_owner = self._bench_owner(Path(tempfile.mkdtemp()))
        win, ws, target = self._window_with_doc(owner=ws_owner)
        win.suggest_edits()
        self.assertEqual(len(ws_owner.send_calls), 1)
        self.assertIn("--- OLD ---", ws_owner.send_calls[0])
        self.assertIn("Do NOT change the file",
                      ws_owner.send_calls[0])
        self.assertIsNotNone(win._suggest_pending)
        win.add_exchange(
            "suggest", "--- OLD ---\nBeta two.\n--- NEW ---\n"
            "Beta TWO.\n")
        self.assertEqual(len(win._suggestion_cards), 1)
        # Harvesting proposed; the document is still untouched.
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "Alpha one. Beta two. Gamma three.\n")
        win.close()

    # -- selection-scoped edit path -------------------------------
    def _select(self, win, text):
        cursor = win.doc_editor.document().find(text)
        self.assertFalse(cursor.isNull())
        win.doc_editor.setTextCursor(cursor)

    def test_selection_rewrite_sends_only_the_passage(self):
        owner = self._bench_owner(Path(tempfile.mkdtemp()))
        win, ws, target = self._window_with_doc(owner=owner)
        self._select(win, "Beta two.")
        win.selection_instr_edit.setText("shout it")
        win.rewrite_selection()
        self.assertEqual(len(owner.send_calls), 1)
        sent = owner.send_calls[0]
        self.assertIn("Beta two.", sent)
        self.assertIn("ONLY the passage", sent)
        self.assertIn("edit_file", sent)
        self.assertEqual(owner.mode_combo.currentText(), "Write")
        win.close()

    # -- steppers ---------------------------------------------------
    def test_stepper_instruction_construction(self):
        from forge.main import WriteWindow
        step = WriteWindow.stepper_instruction
        self.assertIsNone(step("Same", "Same"))
        self.assertEqual(step("Shorter", "Same"),
                         "make it somewhat shorter")
        self.assertEqual(step("Very short", "Same"),
                         "make it much shorter (about half as "
                         "long or less)")
        self.assertEqual(step("Same", "Very formal"),
                         "make the tone much more formal")
        self.assertEqual(step("Longer", "Casual"),
                         "make it somewhat longer and make the "
                         "tone more casual")

    def test_steppers_both_same_sends_nothing(self):
        owner = self._bench_owner(Path(tempfile.mkdtemp()))
        win, ws, target = self._window_with_doc(owner=owner)
        win.apply_steppers()
        self.assertEqual(owner.send_calls, [])
        self.assertIn("on Same", win.editor_status.text())
        win.close()

    def test_steppers_scope_to_selection(self):
        owner = self._bench_owner(Path(tempfile.mkdtemp()))
        win, ws, target = self._window_with_doc(owner=owner)
        self._select(win, "Beta two.")
        win.length_combo.setCurrentText("Shorter")
        win.apply_steppers()
        self.assertEqual(len(owner.send_calls), 1)
        sent = owner.send_calls[0]
        self.assertIn("make it somewhat shorter", sent)
        self.assertIn("Beta two.", sent)
        self.assertIn("edit_file", sent)
        win.close()

    def test_steppers_whole_document(self):
        owner = self._bench_owner(Path(tempfile.mkdtemp()))
        win, ws, target = self._window_with_doc(owner=owner)
        win.tone_combo.setCurrentText("Formal")
        win.apply_steppers()
        self.assertEqual(len(owner.send_calls), 1)
        sent = owner.send_calls[0]
        self.assertIn("make the tone more formal", sent)
        self.assertIn("whole file", sent)
        self.assertIn("write_file", sent)
        win.close()

    # -- auto-save ----------------------------------------------------
    def test_autosave_writes_and_feeds_version_history(self):
        win, ws, target = self._window_with_doc()
        win.doc_editor.setPlainText("Alpha one. Beta two. V2.\n")
        win._autosave_tick()
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "Alpha one. Beta two. V2.\n")
        win.doc_editor.setPlainText("Alpha one. Beta two. V3.\n")
        win._autosave_tick()
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "Alpha one. Beta two. V3.\n")
        history = ws / ".forge" / "write-history" / "notes__letter.md"
        bodies = [p.read_text(encoding="utf-8")
                  for p in history.glob("*.txt")]
        self.assertIn("Alpha one. Beta two. Gamma three.\n", bodies)
        self.assertIn("Alpha one. Beta two. V2.\n", bodies)
        win.close()

    def test_autosave_never_fires_on_a_viewed_version(self):
        win, ws, target = self._window_with_doc()
        win.doc_editor.setPlainText("Alpha one. Beta two. V2.\n")
        win._autosave_tick()
        win.version_prev_btn.click()  # viewing the V1 snapshot
        self.assertIsNotNone(win._viewing_stamp)
        win._autosave_tick()  # must NOT write the view over V2
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "Alpha one. Beta two. V2.\n")
        win.close()


if __name__ == "__main__":
    unittest.main()
