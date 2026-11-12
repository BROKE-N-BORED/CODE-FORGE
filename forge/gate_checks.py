"""Natural-language pre-merge checks for the plan gate (CR8 —
the review study, Tier 1 #8).

Forge gates a merge on conditions written in plain words.
Forge's gate is the plan gate (the Plan window: Approve / Edit /
Kill — forge/main.py PlanWindow + ForgeWindow._plan_render), and
CR8 rides in it: the owner writes the checks in plain words, one
per line, in <project>/.forge/gate-checks.md, and the gate shows
each one as PASS, FAIL, or CAN'T CHECK before the owner approves.

The checks Forge can actually evaluate are the ones its own
record can answer — findings in the latest review (the CR2
severity parser reads them), whether the review is current (the
CR6 review-state fingerprint), and whether the review summary
names the latest review. A line that maps to no evaluator is
never silently passed: it shows as CAN'T CHECK with the reason,
so the owner reads it with their own eyes. Nothing here blocks
anything — Approve stays the owner's call, exactly as the
GoFish view beside it is advisory only. Reads never raise;
a missing gate-checks.md simply means no checks written."""

from __future__ import annotations

import re
from pathlib import Path

from forge import modes as modes_mod
from forge import review_state as review_state_mod

#: The owner's check list, one plain-words check per line.
CHECKS_FILENAME = "gate-checks.md"

#: Review files the checks read — summary.md is the running
#: summary, PROMPT.md is the folder prompt, state.json is the
#: CR6 fingerprint; none of them is a review.
_REVIEW_SKIP = {"summary.md", "state.json", "PROMPT.md"}

#: One bullet/number prefix per line, stripped before reading.
_BULLET_RE = re.compile(r"^(?:[-*+•]\s+|\d+[.)]\s+)")

#: "no / zero / none" — a check only counts as a check when the
#: owner phrases it as an absence to enforce, not a passing
#: mention ("critical findings exist" is not a check).
_NEGATION_RE = re.compile(r"\b(no|zero|none|no more)\b")


def load_checks(root) -> list[str]:
    """The owner's check lines from .forge/gate-checks.md, in
    order — bullets/numbers stripped, headings and blank lines
    skipped. A missing or unreadable file reads as no checks,
    never an error."""
    try:
        path = Path(root) / ".forge" / CHECKS_FILENAME
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    out: list[str] = []
    for raw in text.splitlines():
        line = _BULLET_RE.sub("", raw.strip()).strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def latest_review(root):
    """The newest review file in .forge/reviews/ as (path, text),
    or (None, "") when there is none. summary.md is the running
    summary, not a review, and state.json is the CR6 fingerprint,
    so both are skipped. Never raises."""
    try:
        folder = Path(root) / ".forge" / "reviews"
        candidates = [p for p in folder.iterdir()
                      if p.is_file() and p.suffix == ".md"
                      and p.name not in _REVIEW_SKIP]
        if not candidates:
            return None, ""
        newest = max(candidates,
                     key=lambda p: (p.stat().st_mtime, p.name))
        return newest, newest.read_text(encoding="utf-8",
                                        errors="replace")
    except Exception:
        return None, ""


def _latest_counts(root):
    """(path, {severity: count}) for the latest review, or
    (None, None) when there is no review on file. Never raises."""
    path, text = latest_review(root)
    if path is None:
        return None, None
    try:
        body = modes_mod.section_body(text, "Findings") or ""
        parsed = modes_mod.parse_findings_body(body)
        counts = modes_mod.finding_severity_counts(parsed)
        counts["total"] = len(parsed)
        return path, counts
    except Exception:
        return path, None


def _check_no_critical(root, _text):
    path, counts = _latest_counts(root)
    if counts is None:
        return ("uncheckable",
                "no review on file yet — nothing to count")
    if counts["Critical"]:
        return ("fail",
                f"{counts['Critical']} Critical finding(s) open in "
                f"{path.name}")
    return ("pass", f"no Critical findings in {path.name}")


def _check_no_major(root, _text):
    path, counts = _latest_counts(root)
    if counts is None:
        return ("uncheckable",
                "no review on file yet — nothing to count")
    if counts["Major"]:
        return ("fail",
                f"{counts['Major']} Major finding(s) open in "
                f"{path.name}")
    return ("pass", f"no Major findings in {path.name}")


def _check_no_critical_or_major(root, _text):
    path, counts = _latest_counts(root)
    if counts is None:
        return ("uncheckable",
                "no review on file yet — nothing to count")
    open_ = counts["Critical"] + counts["Major"]
    if open_:
        return ("fail",
                f"{open_} Critical/Major finding(s) open in "
                f"{path.name}")
    return ("pass", f"no Critical or Major findings in {path.name}")


def _check_no_findings(root, _text):
    path, counts = _latest_counts(root)
    if counts is None:
        return ("uncheckable",
                "no review on file yet — nothing to count")
    if counts["total"]:
        return ("fail",
                f"{counts['total']} finding(s) open in {path.name}")
    return ("pass", f"no findings in {path.name}")


def _check_review_current(root, _text):
    try:
        state = review_state_mod.changed_since(root)
    except Exception:
        return ("uncheckable", "review state unreadable")
    if not state["ever"]:
        return ("uncheckable",
                "no review baseline yet — Mark reviewed first")
    if state["changed_total"] or state["removed"]:
        return ("fail",
                f"{state['changed_total']} file(s) changed and "
                f"{len(state['removed'])} removed since the last "
                f"marked review ({state['last_review'] or 'unknown'})")
    return ("pass",
            "nothing changed since the last marked review "
            f"({state['last_review'] or 'unknown'})")


def _check_summary_current(root, _text):
    path, _text2 = latest_review(root)
    if path is None:
        return ("uncheckable", "no review on file yet")
    try:
        summary = (Path(root) / ".forge" / "reviews"
                   / "summary.md").read_text(encoding="utf-8",
                                             errors="replace")
    except Exception:
        return ("fail", "no review summary on file")
    if path.name in summary:
        return ("pass", f"summary names {path.name}")
    return ("fail", f"summary does not mention {path.name}")


#: The evaluators, in the order a check line is matched against
#: them — the most specific first, so "no Critical findings"
#: reads as the Critical check, not the any-findings check.
_EVALUATORS = (
    _check_no_critical_or_major,
    _check_no_critical,
    _check_no_major,
    _check_no_findings,
    _check_review_current,
    _check_summary_current,
)


def match_check(text: str):
    """The evaluator for one plain-words check line, or None when
    the line names nothing Forge can evaluate. The line must
    phrase the check as an absence ("no / zero / none") for the
    findings checks — "critical findings exist" is a statement,
    not a check. Matching is keyword-based and documented, never
    a model call: the owner sees exactly which rule fired, and
    an unmatched line is reported, never guessed. Never raises."""
    try:
        folded = " ".join((text or "").lower().split())
    except Exception:
        return None
    has_negation = bool(_NEGATION_RE.search(folded))
    if has_negation and "critical" in folded and "major" in folded:
        return _check_no_critical_or_major
    if has_negation and "critical" in folded:
        return _check_no_critical
    if has_negation and "major" in folded:
        return _check_no_major
    if has_negation and ("finding" in folded or "findings" in folded):
        return _check_no_findings
    if ("all findings" in folded or "findings fixed" in folded
            or "findings closed" in folded
            or "findings resolved" in folded):
        return _check_no_findings
    # "Summary is up to date" names the summary — it must match
    # here, before "up to date" below would read it as the
    # review being current.
    if "summary" in folded:
        return _check_summary_current
    if ("review" in folded and "current" in folded) \
            or "up to date" in folded \
            or "nothing changed" in folded \
            or "changes reviewed" in folded:
        return _check_review_current
    return None


def run_checks(root) -> list[dict]:
    """Evaluate every check in .forge/gate-checks.md against the
    project, in file order: {text, status, detail}. status is
    "pass", "fail", or "uncheckable" — an unmapped line is
    uncheckable with the reason ("Forge can't evaluate this —
    check it yourself"), never passed by default. [] when no
    checks are written. Never raises."""
    out: list[dict] = []
    for text in load_checks(root):
        evaluator = match_check(text)
        if evaluator is None:
            out.append({"text": text, "status": "uncheckable",
                        "detail": "Forge can't evaluate this — "
                                  "check it yourself"})
            continue
        try:
            status, detail = evaluator(root, text)
        except Exception as exc:
            status, detail = "uncheckable", f"check error: {exc}"
        out.append({"text": text, "status": status,
                    "detail": detail})
    return out


def format_results(results) -> str:
    """The gate's display text for run_checks output: one line
    per check, ✓ PASS / ✗ FAIL / ? CAN'T CHECK, each with its
    detail. No checks written reads as the pointer to the file,
    so the owner learns where the checks live."""
    if not results:
        return ("No gate checks written — add them to "
                ".forge/gate-checks.md, one per line (e.g. "
                "'No Critical findings open').")
    marks = {"pass": "✓ PASS", "fail": "✗ FAIL",
             "uncheckable": "? CAN'T CHECK"}
    lines = []
    for result in results or []:
        mark = marks.get(result.get("status"), "? CAN'T CHECK")
        lines.append(f"{mark} — {result.get('text', '')} "
                     f"({result.get('detail', '')})")
    return "\n".join(lines)


def render_text(root) -> str:
    """The gate-checks display text for a project: the evaluated
    checks, or the pointer when none are written. Never raises."""
    try:
        return format_results(run_checks(root))
    except Exception:
        return ("Gate checks unreadable — check "
                ".forge/gate-checks.md by hand.")
