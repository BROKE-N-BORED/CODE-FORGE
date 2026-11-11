"""Item #57 — the performance envelope's regression gate.

The budget lives in forge/perf_budget.py. These tests enforce the
parts that can be enforced HERE, deterministically:

- the install footprint of the real tree (bytes + files, packaging
  exclusions) stays under its ceilings;
- the generation-diet / payload caps in the live code still equal
  the values the budget mirrors — a cap that quietly grows fails;
- every budget line carries a derivation and an honest enforcement
  label, and the RAM / cold-start lines stay labelled "pc" (targets
  proven by BENCH.md on the owner's machine), never "suite" — this
  VM's shared-CPU timings would be fiction as a gate.

No timing assertions, no process-RSS assertions: nothing here can
flake under load.
"""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path

from forge import perf_budget

ROOT = Path(__file__).resolve().parents[1]


class BudgetWellFormedTests(unittest.TestCase):
    def test_every_budget_line_is_traceable(self):
        for name, line in perf_budget.BUDGET.items():
            with self.subTest(line=name):
                self.assertIn("value", line)
                self.assertTrue(line["unit"])
                self.assertIn(line["enforcement"], ("suite", "pc"))
                self.assertGreaterEqual(len(line["derivation"]), 40)

    def test_min_spec_is_the_8gb_cpu_only_box(self):
        self.assertEqual(perf_budget.MIN_SPEC["ram_gb"], 8)
        self.assertIn("CPU-only", perf_budget.MIN_SPEC["cpu"])
        self.assertEqual(perf_budget.MIN_SPEC["storage"], "HDD")

    def test_unmeasurable_lines_stay_pc_targets(self):
        # RAM residency and cold start can only be proven on the
        # owner's PC (BENCH.md). If a future edit relabels one as
        # suite-enforced, it is claiming a measurement this
        # environment cannot make — fail loudly here instead.
        for name in ("ram_combined_ceiling_gb",
                     "ram_app_idle_ceiling_mb",
                     "cold_start_ceiling_s"):
            self.assertEqual(perf_budget.BUDGET[name]["enforcement"], "pc",
                             name)


class CapMirrorTests(unittest.TestCase):
    def test_caps_match_live_code(self):
        for name, (module_name, attr, expected) in perf_budget.CAPS.items():
            with self.subTest(cap=name):
                module = importlib.import_module(module_name)
                self.assertTrue(hasattr(module, attr),
                                f"{module_name}.{attr} is gone — the cap "
                                f"the budget mirrors no longer exists")
                self.assertEqual(getattr(module, attr), expected,
                                 f"{module_name}.{attr} drifted from the "
                                 f"budget; growing a cap is a budget "
                                 f"decision — update forge/perf_budget.py "
                                 f"in the same change, with the reason")

    def test_default_num_ctx_matches_bench_pin(self):
        from forge import bench
        self.assertEqual(bench._OPTIONS["num_ctx"],
                         perf_budget.BUDGET["default_num_ctx"]["value"])


class FootprintTests(unittest.TestCase):
    def test_exclusions_match_packaging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "real.py").write_bytes(b"x" * 100)
            (root / "junk.pyc").write_bytes(b"x" * 100)
            for skipped in (".venv", "__pycache__", ".git", ".forge"):
                folder = root / skipped
                folder.mkdir()
                (folder / "big.bin").write_bytes(b"x" * 10_000)
            total, count = perf_budget.tree_footprint(root)
            self.assertEqual((total, count), (100, 1))

    def test_report_flags_a_bloated_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fat = root / "fat.bin"
            with open(fat, "wb") as fh:
                fh.truncate(perf_budget.BUDGET[
                    "footprint_bytes_ceiling"]["value"] + 1)
            report = perf_budget.footprint_report(root)
            self.assertFalse(report["ok"])
            self.assertGreater(report["bytes"], report["bytes_ceiling"])

    def test_real_tree_is_inside_the_envelope(self):
        report = perf_budget.footprint_report(ROOT)
        self.assertGreater(report["bytes"], 0)
        self.assertLessEqual(report["bytes"], report["bytes_ceiling"],
                             f"tree is {report['bytes']} bytes — over the "
                             f"{report['bytes_ceiling']}-byte budget; "
                             f"something heavy moved in")
        self.assertLessEqual(report["files"], report["files_ceiling"],
                             f"tree has {report['files']} files — over "
                             f"the {report['files_ceiling']}-file budget")


class DocGuardTests(unittest.TestCase):
    def test_readme_states_the_budget_as_targets(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for needle in ("Performance envelope", "≤ 6 GB", "≤ 30 s",
                       "TARGET", "BENCH.md", "forge/perf_budget.py"):
            self.assertIn(needle, readme)

    def test_bench_sheet_has_the_required_fields(self):
        sheet = (ROOT / "BENCH.md").read_text(encoding="utf-8")
        for needle in ("Ollama version", "llama.cpp", "tok/s",
                       "Time to first token", "ollama ps",
                       "Cold start", "Verdict"):
            self.assertIn(needle, sheet)


if __name__ == "__main__":
    unittest.main()
