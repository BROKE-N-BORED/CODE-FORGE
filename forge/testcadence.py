"""Auto-test cadence (Replit-study item C3, 2026-11-09).

The tester seat exists and works — but until now it ran only when
the driver chose to delegate (run_tester) or the owner asked. C3
makes it CHANGE-DRIVEN, woven into the machinery that already
tracks change: the worker tallies every executed call's file
changes beside the SKRIBE queue feed (the gate's own proof
standards decide what counts as a write), the window accumulates
those tallies per workspace as an untested-changes TAB, and at a
turn's settle the tab is judged by the rule below. Nothing here
runs a test — firing means calling the existing run_tester plugin
handler; this module is the bookkeeping and the rule, kept pure
so both are testable without a window or a model.

The rule, in one sentence: the tester seat runs when the changes
since it last ran reach FILES_THRESHOLD files or LINES_THRESHOLD
changed lines — or, at a plan-step boundary, the smaller
BOUNDARY floors — and a run that does not PASS leaves the whole
tab standing (only the since-last-run delta resets), so failed
work stays visibly owed until a passing run clears it.
"""
from __future__ import annotations

# The thresholds. The two main values are owner settings (Settings
# → Seats & limits: test_cadence_files / test_cadence_lines);
# these constants are the defaults those settings start from. The
# boundary floors are fixed: a plan step landing is the owner's
# own "a piece is done" signal, so the tab is judged sooner there.
FILES_THRESHOLD = 5
LINES_THRESHOLD = 300
BOUNDARY_FILES_THRESHOLD = 2
BOUNDARY_LINES_THRESHOLD = 60


class ChangeAccumulator:
    """One workspace's untested-changes tab.

    Two maps, both path -> changed lines (the latest tally for a
    path stands — a rewritten file's untested content is its new
    content, not the sum of everything ever written to it):

    - _debt: everything written since the last PASSING tester
      run. Only a pass (or a fresh accumulator) clears it.
    - _since_run: everything written since the last tester run
      of ANY outcome. This is what the firing rule judges, so a
      failing run is not re-fired at the very next settle — the
      debt stays on the tab, and the tester runs again only when
      another threshold of NEW change lands on top of it.
    """

    def __init__(self):
        self._debt: dict[str, int] = {}
        self._since_run: dict[str, int] = {}

    def record(self, changes) -> None:
        """Merge one turn's tally ({path: changed lines})."""
        for path, lines in (changes or {}).items():
            path = str(path or "")
            if not path:
                continue
            try:
                lines = max(0, int(lines))
            except (TypeError, ValueError):
                lines = 0
            self._debt[path] = lines
            self._since_run[path] = lines

    @staticmethod
    def _counts(tab) -> tuple[int, int]:
        return len(tab), sum(tab.values())

    def debt_counts(self) -> tuple[int, int]:
        """(files, lines) still untested — the tab itself."""
        return self._counts(self._debt)

    def since_run_counts(self) -> tuple[int, int]:
        """(files, lines) changed since the last tester run."""
        return self._counts(self._since_run)

    def since_run_paths(self) -> list[str]:
        return sorted(self._since_run)

    def has_debt(self) -> bool:
        return bool(self._debt)

    def due(self, files_threshold: int, lines_threshold: int,
            boundary: bool = False) -> bool:
        """The firing rule, judged on the since-last-run delta:
        either count reaching its threshold fires. At a boundary
        the fixed smaller floors replace the owner's thresholds."""
        files, lines = self.since_run_counts()
        if boundary:
            return (files >= BOUNDARY_FILES_THRESHOLD
                    or lines >= BOUNDARY_LINES_THRESHOLD)
        return files >= files_threshold or lines >= lines_threshold

    def note_run(self) -> None:
        """A tester run just started over everything outstanding:
        the since-run delta resets. The debt does NOT — it clears
        only on a pass (note_pass)."""
        self._since_run = {}

    def note_pass(self) -> None:
        """A passing run clears the whole tab."""
        self._debt = {}
        self._since_run = {}


def verdict_of(report) -> str:
    """Classify a run_tester report: "pass" / "fail" / "none".

    The tester seat's contract (plugins/tester) is that a finished
    report carries a line "VERDICT: PASS" or "VERDICT: FAIL".
    Anything else — "Tester error: …", "Tester stopped: …", an
    INCOMPLETE report that never reached a verdict — is "none":
    no evidence the changes were verified, so the tab stands."""
    text = str(report or "")
    if "VERDICT: PASS" in text:
        return "pass"
    if "VERDICT: FAIL" in text:
        return "fail"
    return "none"
