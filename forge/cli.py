"""Code Forge's command line — the agent surface.

Tier 2 #5 (2026-11-12): a `forge review
--agent` style contract so Joshua's other bots and agents can
consume Forge reviews. The CLI reads a review artifact the GUI
already filed and emits it as structured JSON with an exit-code
gate — it never runs a review (no model, no Ollama, no display),
so it is deterministic and instant.

Usage (from the Forge tree; the launcher lives in bin/ because
the tree root already has a forge/ package directory):
    ./bin/forge review --agent [PATH] [--fail-on SEVERITY] [--out FILE]
(or: .venv/bin/python -m forge.cli review --agent [PATH] ...)

PATH is a review file, a project directory (the latest review in
its .forge/reviews/), or omitted (the current directory as the
project). --agent is required: without it the command refuses —
this surface is for agents; the GUI is the human surface.

Exit codes:
    0 — pass: no finding at or above the --fail-on threshold
    1 — fail: at least one finding at or above the threshold
    2 — error: no review found, unreadable file, bad arguments

--fail-on defaults to "major" (any Critical or Major fails);
"none" never fails. JSON goes to stdout only — nothing else
ever writes there, so a bot can parse it blind. Errors go to
stderr as one plain line.

The JSON schema is "code-forge-review/1":
    {"schema": "code-forge-review/1", "review": "<filename>",
     "walkthrough": {"what_changed": ...|null,
                     "changes": [{"area", "what", "why"}],
                     "effort": ...|null, "risk": ...|null} | null,
     "counts": {"Critical": n, "Major": n, "Minor": n,
                "Trivial": n, "unparsed": n, "total": n},
     "findings": [{"severity": ...|null, "type": ...|null,
                   "source": ...|null, "file": ...|null,
                   "line": <int>|null, "problem": "...",
                   "fix": ...|null, "driver": ...|null,
                   "text": "<the full finding line>"}],
     "fail_on": "major", "verdict": "pass"|"fail"}

A finding with no parsed severity counts as unparsed and never
trips the gate — there is nothing to compare it against. A
review with no Walkthrough section (pre-CR3) reads walkthrough
as null, not an error. The gate fails CLOSED: if a review cannot
be parsed at all (the parsers never raise by design, so this is
nearly unreachable), the document still parses as JSON but
carries "error" with the reason and verdict "fail" — a bot must
never read a broken review as clean.

This module imports stdlib + forge.modes only — never
forge.main — so it starts in milliseconds with no display and
no Qt. Keep it that way.
"""

import argparse
import json
import sys
from pathlib import Path

from forge import modes as modes_mod

SCHEMA = "code-forge-review/1"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2

FAIL_ON_CHOICES = ("critical", "major", "minor", "trivial", "none")


class ReviewCliError(Exception):
    """A plain, one-line failure for the CLI to report on stderr
    with exit code 2 — no traceback, no JSON on stdout."""


def latest_review_path(project_root) -> Path | None:
    """The newest review in a project's .forge/reviews/ by
    modification time, or None when the project has none.
    summary.md is the running summary, not a review — it never
    counts. Never raises."""
    try:
        folder = Path(project_root) / ".forge" / "reviews"
        if not folder.is_dir():
            return None
        candidates = [path for path in folder.glob("*.md")
                      if path.name != "summary.md"
                      and path.is_file()]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)
    except Exception:
        return None


def resolve_review_path(target) -> Path:
    """The review file PATH names: a file reads as itself, a
    directory reads as its latest review, None reads as the
    current directory's latest review. Raises ReviewCliError
    with a plain message when there is no review to read."""
    if target is None:
        candidate = latest_review_path(Path.cwd())
        if candidate is None:
            raise ReviewCliError(
                "no review found in .forge/reviews/ under the "
                "current directory")
        return candidate
    path = Path(target)
    if path.is_file():
        return path
    if path.is_dir():
        candidate = latest_review_path(path)
        if candidate is None:
            raise ReviewCliError(
                f"no review found in .forge/reviews/ under {path}")
        return candidate
    raise ReviewCliError(f"no such file or directory: {target}")


def _severity_at_or_above(severity, threshold) -> bool:
    """True when `severity` is at least as bad as `threshold`
    (Critical is worst). "none" never matches; an unparsed
    severity never matches either. Never raises."""
    try:
        if threshold == "none" or not severity:
            return False
        order = {name: i for i, name in enumerate(
            modes_mod.FINDING_SEVERITIES)}
        wanted = order.get(threshold.capitalize())
        got = order.get(severity)
        return wanted is not None and got is not None \
            and got <= wanted
    except Exception:
        return False


def review_to_report(review_text: str, review_name: str,
                     fail_on: str = "major") -> dict:
    """A review's text as the code-forge-review/1 document:
    walkthrough, severity counts, findings with file/line/
    problem/fix/driver, and the pass/fail verdict against the
    --fail-on threshold. Pure — no file or network access.
    Never raises (a bad threshold reads as "major")."""
    try:
        threshold = str(fail_on or "major").lower()
    except Exception:
        threshold = "major"
    if threshold not in FAIL_ON_CHOICES:
        threshold = "major"
    try:
        body = modes_mod.section_body(review_text or "", "Findings")
        parsed = modes_mod.parse_findings_body(body) if body else []
        findings = []
        counts = {severity: 0
                  for severity in modes_mod.FINDING_SEVERITIES}
        counts["unparsed"] = 0
        for found in parsed:
            severity = found.get("severity")
            if severity in counts:
                counts[severity] += 1
            else:
                counts["unparsed"] += 1
            text = found.get("text", "")
            problem, fix = modes_mod.finding_problem_fix(text)
            findings.append({
                "severity": severity,
                "type": found.get("type"),
                "source": found.get("source"),
                "file": modes_mod.finding_file(text) or None,
                "line": modes_mod.finding_line(text),
                "problem": problem,
                "fix": fix,
                "driver": found.get("driver"),
                "text": text,
            })
        counts["total"] = len(findings)
        verdict = "fail" if any(
            _severity_at_or_above(f["severity"], threshold)
            for f in findings) else "pass"
        return {
            "schema": SCHEMA,
            "review": review_name,
            "walkthrough": modes_mod.parse_walkthrough(
                review_text or ""),
            "counts": counts,
            "findings": findings,
            "fail_on": threshold,
            "verdict": verdict,
        }
    except Exception as exc:
        # A review that cannot be parsed at all still yields a
        # valid document rather than a crash mid-pipeline — but
        # the gate fails CLOSED: the document carries the reason
        # in "error" and verdict "fail", so a bot never reads a
        # broken review as clean.
        return {
            "schema": SCHEMA,
            "review": review_name,
            "walkthrough": None,
            "counts": {severity: 0 for severity in
                       modes_mod.FINDING_SEVERITIES
                       } | {"unparsed": 0, "total": 0},
            "findings": [],
            "fail_on": threshold,
            "verdict": "fail",
            "error": f"review could not be parsed: {exc}",
        }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Code Forge's command line — the agent "
                    "surface. Reviews are read as JSON; the GUI "
                    "is where reviews are run.")
    sub = parser.add_subparsers(dest="command")
    review = sub.add_parser(
        "review",
        help="emit a filed review as structured JSON "
             "(--agent) with an exit-code gate")
    review.add_argument(
        "path", nargs="?",
        help="a review file, a project directory (latest review "
             "in .forge/reviews/), or omitted for the current "
             "directory")
    review.add_argument(
        "--agent", action="store_true",
        help="required: emit the review as JSON for another "
             "agent to consume")
    review.add_argument(
        "--fail-on", default="major", choices=FAIL_ON_CHOICES,
        help="the lowest severity that fails the gate "
             "(default: major; none never fails)")
    review.add_argument(
        "--out",
        help="write the JSON to this file instead of stdout")
    return parser


def main(argv=None) -> int:
    """The CLI entry point. Returns the exit code (0 pass,
    1 fail, 2 error) — sys.exit(main()) in __main__."""
    args = _build_parser().parse_args(argv)
    if args.command != "review":
        print("forge: pass a command — 'forge review --agent "
              "[PATH]' reads a review as JSON.",
              file=sys.stderr)
        return EXIT_ERROR
    if not args.agent:
        print("forge review only speaks JSON — pass --agent "
              "(the GUI is where reviews are run and read).",
              file=sys.stderr)
        return EXIT_ERROR
    try:
        path = resolve_review_path(args.path)
    except ReviewCliError as exc:
        print(f"forge review: {exc}", file=sys.stderr)
        return EXIT_ERROR
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"forge review: cannot read {path}: {exc}",
              file=sys.stderr)
        return EXIT_ERROR
    report = review_to_report(text, path.name, args.fail_on)
    payload = json.dumps(report, indent=2) + "\n"
    if args.out:
        try:
            Path(args.out).write_text(payload, encoding="utf-8")
        except OSError as exc:
            print(f"forge review: cannot write {args.out}: {exc}",
                  file=sys.stderr)
            return EXIT_ERROR
    else:
        sys.stdout.write(payload)
    return EXIT_FAIL if report["verdict"] == "fail" else EXIT_PASS


if __name__ == "__main__":
    sys.exit(main())
