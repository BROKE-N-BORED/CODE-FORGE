"""Modular PDF knowledge system (modpdf, 2026-11-21): reference
material the model needs lives in PDF files on disk and loads
into a turn only when it is relevant, inside a fixed slice of
the context window.

The contract pinned here:
- the manifest round-trips, a corrupt manifest is reported in
  plain words and quarantined by ensure_store, never fatal;
- bundled starters seed into the store once, and the owner's
  on/off toggles survive re-seeding;
- extraction reuses the app's own PDF text path and is cached
  by content hash, so changed bytes always re-extract;
- selection: always modules first, then standing self modules
  in their modes, then relevance (mode match beats keyword-only
  matches); whole modules only, inside the budget slice, with
  every skip carrying a plain-words reason;
- a disabled module never loads; a missing or unreadable PDF
  is named, never fatal;
- selection reads only the newest real user message, so a
  folded history (summary in place of the old turns) selects
  exactly like the unfolded one;
- the self-store views (Addition 1): a store write refreshes
  its generated module in the same operation, the extraction
  cache key changes with the bytes, an emptied store's view is
  removed, and a self module loads by relevance and standing
  like any other;
- angle brackets in a store's text survive the markdown to
  PDF render (the renderer would otherwise eat them as HTML);
- context.breakdown reports the modules' share of the system
  prompt;
- the Customize Modules tab lists the manifest, and toggling
  a row writes the manifest at once.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from forge import context as context_mod
from forge import converting
from forge import modpdf

APP_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_DIR = APP_ROOT / "bundled_modules"


def _ensure_app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pdf_file(folder: Path, markdown: str, name: str) -> Path:
    """A real PDF on disk, made with the app's own path."""
    src = folder / (name + ".md")
    src.write_text(markdown, encoding="utf-8")
    dest = folder / (name + ".pdf")
    converting._md_to_pdf(src, dest)
    return dest


class StoreCase(unittest.TestCase):
    """A temp app dir plus a scratch dir for source PDFs."""

    def setUp(self):
        _ensure_app()
        self.temp = tempfile.TemporaryDirectory()
        self.app_dir = Path(self.temp.name) / "app"
        self.app_dir.mkdir(parents=True)
        self.scratch = Path(self.temp.name) / "scratch"
        self.scratch.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def add_module(self, name, markdown, **kwargs):
        pdf = _pdf_file(self.scratch, markdown, name)
        kwargs.setdefault("about", "")
        return modpdf.add_user_module(
            self.app_dir, pdf, **kwargs)


class ManifestTests(StoreCase):

    def test_manifest_round_trip_and_defaults(self):
        entry = self.add_module(
            "wrench", "# Wrench notes\n\nUse the wrench gently.",
            title="Wrench notes", keywords=["wrench"],
            modes=["code"])
        entries, problems = modpdf.load_manifest(self.app_dir)
        self.assertEqual(problems, [])
        self.assertEqual(len(entries), 1)
        got = entries[0]
        self.assertEqual(got["id"], entry["id"])
        self.assertEqual(got["source"], "user")
        self.assertEqual(got["kind"], "knowledge")
        self.assertTrue(got["enabled"])
        self.assertFalse(got["always"])
        # The PDF was copied into the store, not referenced.
        self.assertTrue(
            (modpdf.store_dir(self.app_dir) / got["file"]).exists())

    def test_corrupt_manifest_reported_and_quarantined(self):
        folder = modpdf.store_dir(self.app_dir)
        folder.mkdir(parents=True)
        (folder / modpdf.MANIFEST_NAME).write_text(
            "{not json", encoding="utf-8")
        entries, problems = modpdf.load_manifest(self.app_dir)
        self.assertEqual(entries, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("could not be read", problems[0])
        entries, problems = modpdf.ensure_store(
            self.app_dir, BUNDLED_DIR)
        self.assertTrue(
            (folder / "manifest.corrupt.json").exists())
        self.assertEqual(len(entries), 3)  # the bundled seeds

    def test_bundled_seed_and_owner_toggle_persists(self):
        entries, _problems = modpdf.ensure_store(
            self.app_dir, BUNDLED_DIR)
        self.assertEqual(len(entries), 3)
        for entry in entries:
            self.assertEqual(entry["source"], "bundled")
            self.assertTrue(
                (modpdf.store_dir(self.app_dir)
                 / entry["file"]).exists())
        modpdf.set_enabled(self.app_dir, entries[0]["id"], False)
        again, _problems = modpdf.ensure_store(
            self.app_dir, BUNDLED_DIR)
        by_id = {e["id"]: e for e in again}
        self.assertFalse(by_id[entries[0]["id"]]["enabled"])


class ExtractionCacheTests(StoreCase):

    def test_cache_hit_and_changed_bytes_reextract(self):
        entry = self.add_module(
            "alpha", "# Alpha\n\nThe alpha marker is zebrafish.",
            title="Alpha", keywords=["alpha"], modes="all")
        first = modpdf.extract_module(self.app_dir, entry)
        self.assertEqual(first["problem"], "")
        self.assertIn("zebrafish", first["text"])
        cache_files = list(modpdf.cache_dir(self.app_dir).glob(
            "*.txt"))
        self.assertEqual(len(cache_files), 1)
        second = modpdf.extract_module(self.app_dir, entry)
        self.assertEqual(second["text"], first["text"])
        self.assertEqual(
            len(list(modpdf.cache_dir(self.app_dir).glob("*.txt"))),
            1)  # cache hit: no new entry
        # New bytes at the same path: a new cache entry, new text.
        target = modpdf.store_dir(self.app_dir) / entry["file"]
        _pdf_file(self.scratch, "# Alpha\n\nThe alpha marker "
                                "is now octopus.", "alpha2")
        target.write_bytes(
            (self.scratch / "alpha2.pdf").read_bytes())
        third = modpdf.extract_module(self.app_dir, entry)
        self.assertEqual(third["problem"], "")
        self.assertIn("octopus", third["text"])
        self.assertNotIn("zebrafish", third["text"])
        self.assertEqual(
            len(list(modpdf.cache_dir(self.app_dir).glob("*.txt"))),
            2)


class SelectionTests(StoreCase):

    def test_mode_match_beats_keyword_only(self):
        self.add_module(
            "modefirst", "# Mode first\n\nZEBRAONE marker text.",
            title="Mode first", keywords=["zebra"],
            modes=["code"])
        self.add_module(
            "wordonly", "# Word only\n\nZEBRATWO marker text.",
            title="Word only",
            keywords=["zebra", "stripe", "hoof"], modes=["review"])
        pack = modpdf.assemble_pack(
            self.app_dir, "code",
            [{"role": "user",
              "content": "zebra stripe hoof question"}], 8192)
        titles = pack["loaded"]
        self.assertEqual(titles, ["Mode first", "Word only"])
        self.assertLess(pack["text"].index("ZEBRAONE"),
                        pack["text"].index("ZEBRATWO"))

    def test_budget_cut_skips_whole_module_with_reason(self):
        big_text = "\n\n".join(
            f"Paragraph {i} about calibration drift and torque "
            f"curves in the old workshop manual." for i in range(60))
        self.add_module(
            "big", "# Big\n\n" + big_text, title="Big module",
            keywords=["calibration"], modes=["code"])
        self.add_module(
            "small", "# Small\n\nCalibration is quick.",
            title="Small module", keywords=["calibration"],
            modes=["code"])
        pack = modpdf.assemble_pack(
            self.app_dir, "code",
            [{"role": "user", "content": "calibration help"}],
            2000)  # slice is 400 tokens; the big module cannot fit
        titles = pack["loaded"]
        self.assertEqual(titles, ["Small module"])
        skipped = dict(pack["skipped"])
        self.assertIn("Big module", skipped)
        self.assertTrue(
            skipped["Big module"].startswith("needs about"))

    def test_disabled_module_never_loads(self):
        entry = self.add_module(
            "off", "# Off\n\nThe offmodule marker.",
            title="Off module", keywords=["offmodule"],
            modes="all")
        modpdf.set_enabled(self.app_dir, entry["id"], False)
        pack = modpdf.assemble_pack(
            self.app_dir, "code",
            [{"role": "user", "content": "offmodule please"}],
            8192)
        self.assertEqual(pack["loaded"], [])
        self.assertEqual(dict(pack["skipped"])["Off module"],
                         "turned off in Customize")

    def test_always_module_loads_in_every_mode(self):
        pdf = _pdf_file(self.scratch, "# Always\n\nThe alwayson "
                                      "marker.", "always")
        entry = modpdf.add_user_module(
            self.app_dir, pdf, title="Always module", about="",
            keywords=[], modes=["review"])
        entries, _p = modpdf.load_manifest(self.app_dir)
        entries[0]["always"] = True
        modpdf.save_manifest(self.app_dir, entries)
        for mode in ("code", "plan", "review"):
            pack = modpdf.assemble_pack(
                self.app_dir, mode, [], 8192)
            self.assertIn("Always module", pack["loaded"])

    def test_missing_pdf_is_named_not_fatal(self):
        entry = self.add_module(
            "gone", "# Gone\n\nThe gonemarker text.",
            title="Gone module", keywords=["gonemarker"],
            modes="all")
        (modpdf.store_dir(self.app_dir) / entry["file"]).unlink()
        pack = modpdf.assemble_pack(
            self.app_dir, "code",
            [{"role": "user", "content": "gonemarker"}], 8192)
        self.assertEqual(pack["loaded"], [])
        self.assertEqual(dict(pack["skipped"])["Gone module"],
                         "its PDF could not be read")
        self.assertTrue(any("Gone module" in p
                            for p in pack["problems"]))

    def test_irrelevant_module_stays_on_disk(self):
        self.add_module(
            "quiet", "# Quiet\n\nThe quietmodule marker.",
            title="Quiet module", keywords=["quietmodule"],
            modes=["review"])
        pack = modpdf.assemble_pack(
            self.app_dir, "code",
            [{"role": "user", "content": "unrelated words here"}],
            8192)
        self.assertEqual(pack["loaded"], [])
        self.assertEqual(dict(pack["skipped"])["Quiet module"],
                         "not relevant to this turn")


class FoldSafetyTests(StoreCase):

    def test_selection_reads_the_real_message_behind_a_fold(self):
        self.add_module(
            "dbg", "# Debug\n\nThe crashfix marker.",
            title="Debug module", keywords=["crashfix"],
            modes="all")
        folded = [
            {"role": "system", "content": "prompt"},
            {"role": "user",
             "content": context_mod.SUMMARY_PREFIX
             + " Older talk about crashfix and other things."},
            {"role": "assistant", "content": "Noted."},
            {"role": "user",
             "content": "the crashfix is back, look again"},
        ]
        self.assertEqual(
            modpdf.latest_user_text(folded),
            "the crashfix is back, look again")
        pack = modpdf.assemble_pack(
            self.app_dir, "code", folded, 8192)
        self.assertIn("Debug module", pack["loaded"])
        # A history whose only user text is the summary selects
        # nothing: the summary is not the owner's words.
        summary_only = folded[:2]
        self.assertEqual(modpdf.latest_user_text(summary_only), "")
        pack = modpdf.assemble_pack(
            self.app_dir, "code", summary_only, 8192)
        self.assertEqual(pack["loaded"], [])


class BreakdownTests(StoreCase):

    def test_breakdown_reports_module_share(self):
        self.add_module(
            "share", "# Share\n\nThe sharemarker text rides.",
            title="Share module", keywords=["sharemarker"],
            modes="all")
        pack = modpdf.assemble_pack(
            self.app_dir, "code",
            [{"role": "user", "content": "sharemarker"}], 8192)
        self.assertTrue(pack["text"])
        messages = [{"role": "system", "content": pack["text"]},
                    {"role": "user", "content": "hi"}]
        parts = context_mod.breakdown(messages, [])
        self.assertGreater(parts["modules"], 0)
        self.assertLessEqual(parts["modules"], parts["system"])
        plain = context_mod.breakdown(
            [{"role": "system", "content": "no modules here"}], [])
        self.assertEqual(plain["modules"], 0)


def _memories_source(markdown):
    return {"memories": {
        "title": "Code Forge Memories",
        "about": "Brief and knowledge index.",
        "modes": "all",
        "keywords": ["brief", "knowledge"],
        "standing": False,
        "markdown": markdown,
        "workspace": "/tmp/project"}}


class SelfStoreTests(StoreCase):

    def test_write_refreshes_view_and_cache_key(self):
        problems = modpdf.refresh_self_modules(
            self.app_dir,
            _memories_source("# Brief\n\nVersion one zebrafish."))
        self.assertEqual(problems, [])
        entries, _p = modpdf.load_manifest(self.app_dir)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["source"], "self")
        self.assertEqual(entry["title"], "Code Forge Memories")
        first = modpdf.extract_module(self.app_dir, entry)
        self.assertIn("zebrafish", first["text"])
        keys_before = {f.name for f in
                       modpdf.cache_dir(self.app_dir).glob("*.txt")}
        problems = modpdf.refresh_self_modules(
            self.app_dir,
            _memories_source("# Brief\n\nVersion two octopus."))
        self.assertEqual(problems, [])
        entries, _p = modpdf.load_manifest(self.app_dir)
        second = modpdf.extract_module(self.app_dir, entries[0])
        keys_after = {f.name for f in
                      modpdf.cache_dir(self.app_dir).glob("*.txt")}
        self.assertTrue(keys_after - keys_before,
                        "the changed bytes must re-extract under "
                        "a new cache key")
        self.assertIn("octopus", second["text"])
        self.assertNotIn("zebrafish", second["text"])

    def test_emptied_store_removes_its_view(self):
        modpdf.refresh_self_modules(
            self.app_dir, _memories_source("# Brief\n\nText."))
        entries, _p = modpdf.load_manifest(self.app_dir)
        self.assertEqual(len(entries), 1)
        modpdf.refresh_self_modules(self.app_dir, {})
        entries, _p = modpdf.load_manifest(self.app_dir)
        self.assertEqual(entries, [])

    def test_self_module_loads_by_relevance(self):
        modpdf.refresh_self_modules(
            self.app_dir,
            _memories_source("# Brief\n\nThe briefmarker text."))
        pack = modpdf.assemble_pack(
            self.app_dir, "code",
            [{"role": "user",
              "content": "what does the brief say"}], 8192)
        self.assertIn("Code Forge Memories", pack["loaded"])
        self.assertIn("briefmarker", pack["text"])

    def test_standing_rules_load_in_their_modes_only(self):
        sources = {"rules": {
            "title": "Code Forge Rules",
            "about": "Approved guidelines.",
            "modes": ["review", "plan"],
            "keywords": ["rule"],
            "standing": True,
            "markdown": "# Rules\n\nThe rulemarker line.",
            "workspace": "/tmp/project"}}
        modpdf.refresh_self_modules(self.app_dir, sources)
        for mode, expect in (("review", True), ("plan", True),
                             ("code", False)):
            pack = modpdf.assemble_pack(
                self.app_dir, mode,
                [{"role": "user", "content": "unrelated words"}],
                8192, self_sources=sources)
            loaded = pack["loaded"]
            self.assertEqual("Code Forge Rules" in loaded, expect,
                             f"mode {mode}")

    def test_angle_brackets_survive_the_render(self):
        modpdf.refresh_self_modules(
            self.app_dir, _memories_source(
                "# Brief\n\nAnnounce STEP <n> DONE as each "
                "step finishes."))
        entries, _p = modpdf.load_manifest(self.app_dir)
        got = modpdf.extract_module(self.app_dir, entries[0])
        self.assertEqual(got["problem"], "")
        self.assertIn("STEP <n> DONE", got["text"])


class ModulesTabTests(StoreCase):

    def test_tab_lists_store_and_toggle_writes_manifest(self):
        from PySide6.QtWidgets import QCheckBox, QTabWidget
        from forge import main as main_mod
        sources = _memories_source("# Brief\n\nTab test text.")
        dialog = main_mod.CustomizeDialog(
            None, [], [], set(), set(), {},
            modules_app_dir=self.app_dir,
            modules_bundled_dir=BUNDLED_DIR,
            modules_sources=lambda: sources)
        try:
            tabs = dialog.findChild(QTabWidget)
            titles = [tabs.tabText(i)
                      for i in range(tabs.count())]
            self.assertIn("Modules", titles)
            boxes = dialog.findChildren(QCheckBox)
            texts = [b.text() for b in boxes]
            self.assertIn("Code Forge Memories", texts)
            self.assertIn("Debugging protocol", texts)
            target = next(b for b in boxes
                          if b.text() == "Debugging protocol")
            self.assertTrue(target.isChecked())
            target.setChecked(False)
            entries, _p = modpdf.load_manifest(self.app_dir)
            by_title = {e["title"]: e for e in entries}
            self.assertFalse(
                by_title["Debugging protocol"]["enabled"])
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
