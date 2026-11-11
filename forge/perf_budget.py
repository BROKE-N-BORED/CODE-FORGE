"""Item #57 — the performance envelope, as machine-readable data.

Code Forge's product promise is that it runs COMFORTABLY on a small
box: 8 GB of RAM, CPU only, a spinning disk. This module states that
promise as a budget so it can be enforced instead of just believed:

- Lines marked ``suite`` are enforced by tests/test_perf_budget.py in
  this environment: the install footprint of the tree itself, and the
  generation-diet / payload caps staying wired at their budgeted
  values in the live code (a cap that quietly grows fails the suite).
- Lines marked ``pc`` are TARGETS for the min-spec machine. Nothing
  in this repo can measure them honestly (no Ollama, no models, and
  a shared-CPU VM whose timings would be fiction), so they are proven
  — or the budget gets revised — by filling in BENCH.md on the
  owner's actual PC. A ``pc`` line is a promise to measure, never a
  measured result.

Pure stdlib, no Qt, no forge imports: anything (tests, packaging, a
future bench) can read the budget without pulling the app in.
"""
from __future__ import annotations

import os
from pathlib import Path

# The min-spec machine the whole product is sized for (owner's PC
# class; the README's Requirements section names the same box).
MIN_SPEC = {
    "ram_gb": 8,
    "cpu": "Ryzen 3 3250U class (CPU-only, no GPU inference)",
    "storage": "HDD",
}

# ------------------------------------------------------------------
# The budget. Every line: value, unit, enforcement, derivation.
# "derivation" is mandatory — a number nobody can trace is marketing.
# ------------------------------------------------------------------
BUDGET = {
    # --- RAM (targets; proven on the PC via BENCH.md) ---
    "ram_combined_ceiling_gb": {
        "value": 6,
        "unit": "GB RSS",
        "enforcement": "pc",
        "derivation": (
            "Min-spec RAM is 8 GB (hardware fact). The envelope keeps "
            "2 GB for the OS and desktop and budgets the remaining "
            "6 GB for the Forge process plus every resident Ollama "
            "model together. Seat models are sized to fit inside it "
            "by design: the council path in forge/worker.py runs the "
            "reviewer only after the driver has stopped, so the seats "
            "are never resident together during a turn."),
    },
    "ram_app_idle_ceiling_mb": {
        "value": 1024,
        "unit": "MB RSS",
        "enforcement": "pc",
        "derivation": (
            "TARGET: the Qt shell with no model loaded and no turn "
            "running must stay at or under 1 GB RSS — a small, fixed "
            "fraction of the 8 GB box, so the RAM story is decided by "
            "the models, not the shell. Measured on the PC (BENCH.md); "
            "nothing here weighs the live process."),
    },
    # --- Install footprint (enforced HERE, deterministically) ---
    "footprint_bytes_ceiling": {
        "value": 4 * 1024 * 1024,
        "unit": "bytes",
        "enforcement": "suite",
        "derivation": (
            "The tree measured 1,923,259 bytes across 68 files on "
            "2026-11-09 (packaging exclusions: no .venv, __pycache__, "
            ".git, .forge, *.pyc). The ceiling is ~2.2x that — room "
            "to grow features, while a single vendored dependency, "
            "venv, or stray binary (tens of MB) trips it by an order "
            "of magnitude. Enforced by tests/test_perf_budget.py."),
    },
    "footprint_files_ceiling": {
        "value": 150,
        "unit": "files",
        "enforcement": "suite",
        "derivation": (
            "68 files measured 2026-11-09 (same exclusions as the "
            "byte ceiling); ~2.2x headroom. A vendored dependency "
            "tree trips this by the hundreds even if its bytes were "
            "somehow small. Enforced by tests/test_perf_budget.py."),
    },
    # --- Cold start (target; stopwatch on the PC) ---
    "cold_start_ceiling_s": {
        "value": 30,
        "unit": "seconds",
        "enforcement": "pc",
        "derivation": (
            "TARGET: launch to an interactive window in 30 s or "
            "under on the min-spec HDD. Chosen as the envelope's "
            "promise, not a measurement — proven or revised by the "
            "stopwatch protocol in BENCH.md. Startup timing in this "
            "VM would be shared-CPU fiction, so the suite does not "
            "gate on it."),
    },
    # --- Generation window (enforced HERE via the cap mirrors) ---
    "default_num_ctx": {
        "value": 8192,
        "unit": "tokens",
        "enforcement": "suite",
        "derivation": (
            "The default context window every seat runs with "
            "(settings default throughout forge/main.py; the bench "
            "pins the same 8192 in forge/bench.py _OPTIONS). The KV "
            "cache a window costs scales with this number, so its "
            "growth is a budget event, not a settings tweak. The "
            "suite asserts the bench's pinned copy still equals it."),
    },
}

# ------------------------------------------------------------------
# The generation-diet / payload caps, mirrored. Each entry names the
# live constant it mirrors; the suite imports the live module and
# fails if the values drift apart. Growing any of these is a budget
# decision — update this table AND the live code in the same change,
# with the reason recorded.
# ------------------------------------------------------------------
CAPS = {
    # forge/context.py — how history folds into the window.
    "fold_threshold": ("forge.context", "FOLD_THRESHOLD", 0.80),
    "chars_per_token": ("forge.context", "CHARS_PER_TOKEN", 3.5),
    "summary_tool_result_cap": ("forge.context", "_TOOL_RESULT_CAP", 800),
    "summary_content_cap": ("forge.context", "_CONTENT_CAP", 4000),
    "summary_tool_call_cap": ("forge.context", "_TOOL_CALL_CAP", 300),
    # forge/worker.py — the turn's own budget discipline.
    "budget_stop_fraction": ("forge.worker", "BUDGET_STOP_FRACTION", 0.90),
    "tool_result_log_cap": ("forge.worker", "_TOOL_RESULT_LOG_CAP", 200_000),
    "tool_call_args_log_cap": ("forge.worker", "_TOOL_CALL_ARGS_LOG_CAP", 20_000),
    # forge/tools.py — what one tool call may drag into the turn.
    "command_output_cap": ("forge.tools", "COMMAND_OUTPUT_CAP", 32 * 1024 * 1024),
    "read_file_max_limit": ("forge.tools", "READ_FILE_MAX_LIMIT", 10_000),
}

# Directories / files the footprint count ignores — the same set the
# release packaging excludes, so the gate weighs what actually ships.
_FOOTPRINT_SKIP_DIRS = frozenset({".venv", "__pycache__", ".git", ".forge"})
_FOOTPRINT_SKIP_SUFFIXES = frozenset({".pyc"})


def tree_footprint(root) -> tuple[int, int]:
    """(total_bytes, file_count) for the tree at root, packaging
    exclusions applied. Deterministic: file sizes only, no timing,
    no process state — safe to gate a test suite on."""
    total = 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _FOOTPRINT_SKIP_DIRS]
        for name in filenames:
            if Path(name).suffix in _FOOTPRINT_SKIP_SUFFIXES:
                continue
            total += os.path.getsize(os.path.join(dirpath, name))
            count += 1
    return total, count


def footprint_report(root) -> dict:
    """The footprint lines of the budget, checked against root."""
    total, count = tree_footprint(root)
    return {
        "bytes": total,
        "files": count,
        "bytes_ceiling": BUDGET["footprint_bytes_ceiling"]["value"],
        "files_ceiling": BUDGET["footprint_files_ceiling"]["value"],
        "ok": (total <= BUDGET["footprint_bytes_ceiling"]["value"]
               and count <= BUDGET["footprint_files_ceiling"]["value"]),
    }
