"""Project Knowledge (owner-approved build, 2026-11-11): finished
work distills itself into knowledge a project keeps — but only
through the owner's gate.

The contract pinned here:
- the store layout (.forge/knowledge/ approved, drafts/ awaiting);
- the draft lifecycle (write -> approve moves + compounds the
  brief; discard deletes);
- the gate itself: a DRAFT contributes nothing to context — not its
  index line, not its text (read_knowledge reads it as not-found) —
  while an APPROVED entry appears as name + one-line summary in the
  index text and in build_system_prompt, with full text slice-load
  on demand, exactly the sources pattern;
- the brief compounds by insertion only: owner edits already in
  brief.md survive approvals byte-for-byte;
- the owner's hand-add lands as a draft by default, or approved
  (with the brief line) when they choose;
- the distiller files DRAFTS ONLY from a scripted fast seat, files
  nothing on a NOTHING reply, and NEVER raises — a transport that
  blows up leaves the turn's world exactly as it was: no draft, no
  approved entry, one log line.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forge import knowledge as knowledge_mod
from forge import projects as proj


class KnowledgeStoreTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        proj.ensure_project(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_layout_created_on_demand(self):
        path = knowledge_mod.write_draft(
            self.root, "Cache the index", "Index text is cached per project",
            "The sources index is fingerprinted; do not rebuild per send.")
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent,
                         knowledge_mod.drafts_area(self.root))
        self.assertEqual(knowledge_mod.list_knowledge(self.root), [])
        drafts = knowledge_mod.list_drafts(self.root)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["title"], "Cache the index")
        self.assertEqual(drafts[0]["summary"],
                         "Index text is cached per project")

    def test_approve_moves_entry_and_draft_is_gone(self):
        path = knowledge_mod.write_draft(
            self.root, "Gate first", "Nothing browses before approval",
            "Research runs wait for the plan gate.")
        dest = knowledge_mod.approve_draft(self.root, path.name)
        self.assertTrue(dest.is_file())
        self.assertFalse(path.exists())
        self.assertEqual(knowledge_mod.list_drafts(self.root), [])
        approved = knowledge_mod.list_knowledge(self.root)
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["summary"],
                         "Nothing browses before approval")

    def test_discard_deletes_and_never_promotes(self):
        path = knowledge_mod.write_draft(
            self.root, "Wrong turn", "The deck port is not 8766",
            "A dead end: the port guess was wrong.")
        self.assertTrue(
            knowledge_mod.discard_draft(self.root, path.name))
        self.assertFalse(path.exists())
        self.assertEqual(knowledge_mod.list_knowledge(self.root), [])
        self.assertFalse(
            knowledge_mod.discard_draft(self.root, path.name))

    def test_paths_are_confined(self):
        with self.assertRaises(ValueError):
            knowledge_mod.approve_draft(self.root, "../instructions.md")
        with self.assertRaises(ValueError):
            knowledge_mod.read_knowledge(self.root, "../instructions.md")


class KnowledgeGateContextTests(unittest.TestCase):
    """The gate at the context boundary — module level. The window
    half (build_system_prompt) is KnowledgePromptTests below."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        proj.ensure_project(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_draft_contributes_nothing(self):
        knowledge_mod.write_draft(
            self.root, "Secret draft", "Drafts must stay invisible",
            "Body of the invisible draft.")
        self.assertEqual(knowledge_mod.knowledge_index_text(self.root), "")
        self.assertEqual(
            knowledge_mod.read_knowledge(self.root, "anything.md"),
            "Knowledge entry not found: anything.md")

    def test_approved_index_and_slice_load(self):
        path = knowledge_mod.write_draft(
            self.root, "Big entry", "A long approved entry",
            "x" * 9000)
        knowledge_mod.approve_draft(self.root, path.name)
        text = knowledge_mod.knowledge_index_text(self.root)
        self.assertIn("Project knowledge", text)
        self.assertIn("A long approved entry", text)
        self.assertNotIn("x" * 100, text)  # index only, never the body
        first = knowledge_mod.read_knowledge(self.root, path.name)
        self.assertIn("offset=7000", first)
        rest = knowledge_mod.read_knowledge(self.root, path.name,
                                            offset=7000)
        self.assertIn("the end of the entry", rest)

    def test_draft_name_reads_as_not_found_even_when_file_exists(self):
        path = knowledge_mod.write_draft(
            self.root, "Pending", "Still a draft", "Unapproved body.")
        self.assertEqual(
            knowledge_mod.read_knowledge(self.root, path.name),
            f"Knowledge entry not found: {path.name}")


class KnowledgeBriefTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        proj.ensure_project(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_brief_created_by_first_approval_and_compounds(self):
        one = knowledge_mod.write_draft(
            self.root, "First", "Summary one", "Body one.")
        knowledge_mod.approve_draft(self.root, one.name)
        brief = knowledge_mod.brief_path(self.root).read_text(
            encoding="utf-8")
        self.assertIn("## Knowledge", brief)
        self.assertIn("- First — Summary one", brief)
        two = knowledge_mod.write_draft(
            self.root, "Second", "Summary two", "Body two.")
        knowledge_mod.approve_draft(self.root, two.name)
        brief = knowledge_mod.brief_path(self.root).read_text(
            encoding="utf-8")
        self.assertLess(brief.index("Summary one"),
                        brief.index("Summary two"))

    def test_owner_edits_survive_approvals(self):
        knowledge_mod.ensure_brief(self.root)
        path = knowledge_mod.brief_path(self.root)
        owner_text = ("# My project\nThe owner wrote this line and "
                      "it is NOT negotiable.\n")
        path.write_text(owner_text, encoding="utf-8")
        entry = knowledge_mod.write_draft(
            self.root, "Kept", "A kept summary", "Body.")
        knowledge_mod.approve_draft(self.root, entry.name)
        brief = path.read_text(encoding="utf-8")
        self.assertTrue(brief.startswith(owner_text))
        self.assertIn("- Kept — A kept summary", brief)

    def test_brief_text_empty_without_brief(self):
        # A project with no brief at all is a RAW directory here,
        # not ensure_project: project creation now seeds brief.md
        # as part of the prebuilt scaffold (owner's order, Project
        # System build), so only a folder the scaffold never
        # touched is brief-less. The assertion on brief_text
        # itself is unchanged.
        raw = Path(self.temp.name) / "raw-project"
        raw.mkdir()
        self.assertEqual(knowledge_mod.brief_text(raw), "")


class KnowledgeHandAddTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        proj.ensure_project(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_hand_add_defaults_to_draft(self):
        knowledge_mod.add_entry(self.root, "Owner note",
                                "Remember the deploy order.")
        self.assertEqual(len(knowledge_mod.list_drafts(self.root)), 1)
        self.assertEqual(knowledge_mod.list_knowledge(self.root), [])

    def test_hand_add_approved_lands_and_compounds(self):
        knowledge_mod.add_entry(self.root, "Owner fact",
                                "The release zip is emailed, never "
                                "attached in chat.", approved=True)
        approved = knowledge_mod.list_knowledge(self.root)
        self.assertEqual(len(approved), 1)
        brief = knowledge_mod.brief_path(self.root).read_text(
            encoding="utf-8")
        self.assertIn("Owner fact", brief)


class _ScriptedClient:
    """A fast seat on a script: yields the given reply, or raises."""

    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def chat_stream(self, model, messages, options, tools, **kw):
        self.calls.append((model, messages))
        if self.error is not None:
            raise self.error
        yield {"message": {"content": self.reply or ""}, "done": True}


class KnowledgeDistillTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        proj.ensure_project(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_distill_files_a_draft_only(self):
        client = _ScriptedClient(
            reply="TITLE: TPM cap\n"
                  "SUMMARY: Groq free caps at 8000 tokens per minute\n"
                  "Forge sizes Groq requests to the envelope now.")
        out = knowledge_mod.distill(
            client, "fast-seat", self.root,
            "why does groq rate-limit", "Because requests were oversized.")
        self.assertIsNotNone(out)
        self.assertEqual(knowledge_mod.list_knowledge(self.root), [])
        drafts = knowledge_mod.list_drafts(self.root)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["title"], "TPM cap")
        self.assertEqual(drafts[0]["summary"],
                         "Groq free caps at 8000 tokens per minute")

    def test_distill_nothing_reply_files_nothing(self):
        client = _ScriptedClient(reply="NOTHING")
        out = knowledge_mod.distill(
            client, "fast-seat", self.root, "hi", "hello")
        self.assertIsNone(out)
        self.assertEqual(knowledge_mod.list_drafts(self.root), [])

    def test_distill_failure_is_silent_and_files_nothing(self):
        logged = []
        client = _ScriptedClient(error=RuntimeError("seat exploded"))
        out = knowledge_mod.distill(
            client, "fast-seat", self.root, "do a thing", "did it",
            log=logged.append)
        self.assertIsNone(out)
        self.assertEqual(knowledge_mod.list_drafts(self.root), [])
        self.assertEqual(knowledge_mod.list_knowledge(self.root), [])
        self.assertEqual(len(logged), 1)
        self.assertIn("seat exploded", logged[0])

    def test_distill_empty_reply_files_nothing(self):
        client = _ScriptedClient(reply="did it")
        out = knowledge_mod.distill(
            client, "fast-seat", self.root, "do a thing", "   ")
        self.assertIsNone(out)
        self.assertEqual(client.calls, [])


class KnowledgePromptTests(unittest.TestCase):
    """The window half: build_system_prompt is the context assembly
    a reopened project loads first. Drafts stay out of it; approved
    index lines and the brief ride in it. (The HintsPromptTests /
    ProductionSurfacingTests pattern: one shared window, the
    workspace swapped per test.)"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        from forge.main import ForgeWindow
        cls.win = ForgeWindow()

    @classmethod
    def tearDownClass(cls):
        cls.win.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        proj.ensure_project(self.root)
        self._old_workspace = self.win.workspace
        self.win.workspace = str(self.root)

    def tearDown(self):
        self.win.workspace = self._old_workspace
        self.temp.cleanup()

    def test_prompt_excludes_drafts(self):
        knowledge_mod.write_draft(
            self.root, "Invisible", "Draft summary must not ride",
            "Draft body must not ride either.")
        prompt = self.win.build_system_prompt()
        self.assertNotIn("Draft summary must not ride", prompt)
        self.assertNotIn("Project knowledge", prompt)

    def test_prompt_carries_approved_index_and_brief(self):
        path = knowledge_mod.write_draft(
            self.root, "Riding", "Approved summary rides the prompt",
            "Approved body stays out of the prompt.")
        knowledge_mod.approve_draft(self.root, path.name)
        prompt = self.win.build_system_prompt()
        self.assertIn("Project knowledge", prompt)
        self.assertIn("Approved summary rides the prompt", prompt)
        self.assertNotIn("Approved body stays out", prompt)
        self.assertIn("Project brief", prompt)
        self.assertIn("Approved summary rides the prompt",
                      self.win.build_system_prompt())

    def test_read_knowledge_tool_registered(self):
        tools = self.win._extra_tools()
        self.assertIn("read_knowledge", tools)
        schema, handler, _write, _label = tools["read_knowledge"]
        self.assertEqual(schema["function"]["name"], "read_knowledge")
        path = knowledge_mod.write_draft(
            self.root, "Tool read", "Readable through the tool", "Body.")
        knowledge_mod.approve_draft(self.root, path.name)
        out = handler({"name": path.name})
        self.assertIn("Readable through the tool", out)


if __name__ == "__main__":
    unittest.main()
