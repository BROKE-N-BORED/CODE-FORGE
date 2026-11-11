"""Project System tests (2026-11-11): the prebuilt scaffold, live
folder prompts, project types, per-project settings, git start.

Owner's orders: creating a project lays down the WHOLE prebuilt
directory (never a bare folder); every folder carries its own
PROMPT.md; folder prompts are LIVE (the mapped mode's folder prompt
rides in the system prompt); every project has its own settings;
a type choice at creation; git init + first snapshot at creation.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from forge import projects as proj


def _tree_snapshot(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


class ScaffoldCompletenessTests(unittest.TestCase):
    def test_full_tree_and_starter_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root)
            area = root / ".forge"
            for rel in proj.SCAFFOLD_FOLDERS:
                self.assertTrue((area / rel).is_dir(), rel)
            for name in ("instructions.md", "scratchpad.md", "hints.md",
                         "brief.md", "settings.json", "PROMPT.md"):
                self.assertTrue((area / name).is_file(), name)

    def test_every_folder_has_a_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root)
            area = root / ".forge"
            for rel in proj.SCAFFOLD_FOLDERS:
                text = (area / rel / "PROMPT.md").read_text(
                    encoding="utf-8")
                self.assertTrue(text.strip(), rel)

    def test_settings_seeded_with_type_and_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root)
            data = proj.project_settings(root)
            self.assertEqual(data.get("project_type"), "generic")
            self.assertTrue(data.get("created"))

    def test_brief_carries_name_type_and_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "my-app"
            root.mkdir()
            proj.ensure_project(root, project_type="app")
            brief = (root / ".forge" / "brief.md").read_text(
                encoding="utf-8")
            self.assertIn("my-app", brief)
            self.assertIn("Type: app", brief)
            for section in ("## Status", "## Decisions", "## Next"):
                self.assertIn(section, brief)
            self.assertIn(proj.BRIEF_STATUS_LINES["app"], brief)


class ScaffoldIdempotencyTests(unittest.TestCase):
    def test_second_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root, project_type="music")
            before = _tree_snapshot(root)
            proj.ensure_project(root, project_type="music")
            proj.ensure_project(root)
            self.assertEqual(before, _tree_snapshot(root))

    def test_owner_edits_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root)
            area = root / ".forge"
            (area / "instructions.md").write_text("MY RULES\n")
            (area / "sources" / "PROMPT.md").write_text("MY PROMPT\n")
            (area / "brief.md").write_text("MY BRIEF\n")
            proj.set_project_setting(root, "custom", 7)
            proj.ensure_project(root)
            self.assertEqual(
                (area / "instructions.md").read_text(), "MY RULES\n")
            self.assertEqual(
                (area / "sources" / "PROMPT.md").read_text(),
                "MY PROMPT\n")
            self.assertEqual((area / "brief.md").read_text(), "MY BRIEF\n")
            self.assertEqual(proj.project_settings(root).get("custom"), 7)


class FolderPromptContentTests(unittest.TestCase):
    """Each folder's prompt says what belongs there, in plain words —
    pinned on the load-bearing phrases, not the whole text."""

    CASES = {
        "sources": "read_source",
        "uploads": "inbox",
        "reference": "profile.txt",
        "artifacts": "index.json",
        "design": "layout, ui, flow, mechanics, aesthetic, systems",
        "architecture": "Decisions",
        "reviews": "findings by severity",
        "research": "approved before anything browsed",
        "mode-sessions": "transcript",
        "write-history": "snapshots",
    }

    def test_prompt_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root)
            for rel, needle in self.CASES.items():
                text = (root / ".forge" / rel / "PROMPT.md").read_text(
                    encoding="utf-8")
                self.assertIn(needle, text, rel)


class FolderPromptLiveTests(unittest.TestCase):
    """The wiring data: a mode's folder prompt resolves to the folder
    that mode files into; ask (files nothing) resolves to none."""

    def test_mode_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root)
            self.assertIn("Design",
                          proj.folder_prompt_text(root, "design"))
            self.assertIn("Architecture",
                          proj.folder_prompt_text(root, "architect"))
            self.assertIn("findings by severity",
                          proj.folder_prompt_text(root, "review"))
            self.assertIn("approved before anything browsed",
                          proj.folder_prompt_text(root, "research"))
            self.assertIn("filed work products",
                          proj.folder_prompt_text(root, "plan"))
            self.assertIn("filed work products",
                          proj.folder_prompt_text(root, "code"))
            self.assertIn(".forge folder",
                          proj.folder_prompt_text(root, "write"))
            self.assertEqual(proj.folder_prompt_text(root, "ask"), "")
            self.assertEqual(proj.folder_prompt_text(root, "bogus"), "")

    def test_sources_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root)
            self.assertIn("read_source", proj.sources_prompt_text(root))

    def test_prompt_edit_is_picked_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root)
            first = proj.folder_prompt_text(root, "design")
            self.assertNotIn("OWNER EDIT MARKER", first)
            (root / ".forge" / "design" / "PROMPT.md").write_text(
                "# Design\nOWNER EDIT MARKER, longer text\n")
            self.assertIn("OWNER EDIT MARKER",
                          proj.folder_prompt_text(root, "design"))

    def test_missing_prompt_is_empty_not_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".forge").mkdir()
            self.assertEqual(proj.folder_prompt_text(root, "design"), "")


class ProjectTypeTests(unittest.TestCase):
    def test_non_generic_type_writes_profile_and_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root, project_type="music")
            self.assertEqual(
                proj.project_settings(root).get("project_type"),
                "music")
            profile = (root / ".forge" / "reference" / "profile.txt")
            self.assertEqual(profile.read_text().strip(), "music")
            brief = (root / ".forge" / "brief.md").read_text()
            self.assertIn(proj.BRIEF_STATUS_LINES["music"], brief)

    def test_generic_type_writes_no_profile(self):
        # profile.txt is the reference-driven opt-in record; a
        # generic project must not acquire one by accident.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root)
            self.assertFalse(
                (root / ".forge" / "reference" / "profile.txt").exists())
            proj.ensure_project(root, project_type="generic")
            self.assertFalse(
                (root / ".forge" / "reference" / "profile.txt").exists())

    def test_invalid_type_falls_back_to_generic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root, project_type="nonsense")
            self.assertEqual(
                proj.project_settings(root).get("project_type"),
                "generic")

    def test_recorded_type_survives_plain_ensure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root, project_type="game")
            proj.ensure_project(root)
            self.assertEqual(
                proj.project_settings(root).get("project_type"), "game")


class ProjectSettingsScopeTests(unittest.TestCase):
    def test_settings_are_per_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            proj.ensure_project(a)
            proj.ensure_project(b)
            proj.set_project_setting(a, "depth", "deep")
            self.assertEqual(
                proj.project_settings(a).get("depth"), "deep")
            self.assertNotIn("depth", proj.project_settings(b))
            self.assertNotIn("depth", proj.project_settings(a / "nope"))

    def test_unreadable_settings_read_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root)
            (root / ".forge" / "settings.json").write_text("{broken")
            self.assertEqual(proj.project_settings(root), {})


class GitStartTests(unittest.TestCase):
    def test_starts_repo_with_scaffold_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root)
            result = proj.git_start(root)
            self.assertEqual(result.get("status"), "started", result)
            self.assertTrue((root / ".git").is_dir())
            log = subprocess.run(
                ["git", "-C", str(root), "log", "--format=%s"],
                capture_output=True, text=True)
            self.assertIn("prebuilt scaffold", log.stdout)
            count = subprocess.run(
                ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
                capture_output=True, text=True)
            self.assertEqual(count.stdout.strip(), "1")
            self.assertTrue(result.get("commit"))

    def test_second_start_reports_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root)
            self.assertEqual(
                proj.git_start(root).get("status"), "started")
            self.assertEqual(
                proj.git_start(root).get("status"), "existing")

    def test_pre_existing_repo_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)],
                           capture_output=True)
            self.assertEqual(
                proj.git_start(root).get("status"), "existing")

    def test_missing_git_is_a_status_not_an_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj.ensure_project(root)
            result = proj.git_start(root, git="git-that-does-not-exist-xyz")
            self.assertEqual(result.get("status"), "no-git")
            self.assertFalse((root / ".git").exists())


class FolderPromptWindowTests(unittest.TestCase):
    """The live half, on the real window (HintsPromptTests pattern):
    the mapped mode's folder prompt text appears in the assembled
    system prompt; an unmapped mode's does not; the sources prompt
    rides only when the sources index does."""

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
        self._old_mode = self.win.settings.get("mode")
        self.win.workspace = str(self.root)

    def tearDown(self):
        self.win.workspace = self._old_workspace
        if self._old_mode is None:
            self.win.settings.pop("mode", None)
        else:
            self.win.settings["mode"] = self._old_mode
        self.temp.cleanup()

    def test_design_mode_carries_design_folder_prompt(self):
        self.win.settings["mode"] = "design"
        prompt = self.win.build_system_prompt()
        self.assertIn("Folder instructions", prompt)
        self.assertIn("Everything Design mode produces", prompt)

    def test_ask_mode_carries_no_folder_prompt(self):
        self.win.settings["mode"] = "ask"
        prompt = self.win.build_system_prompt()
        self.assertNotIn("Folder instructions", prompt)

    def test_sources_prompt_rides_with_the_index(self):
        (self.root / ".forge" / "sources" / "spec.md").write_text(
            "# Build spec\nThe real spec line.\n")
        self.win.settings["mode"] = "code"
        prompt = self.win.build_system_prompt()
        self.assertIn("Sources folder instructions", prompt)
        self.assertIn("read_source", prompt)


if __name__ == "__main__":
    unittest.main()
