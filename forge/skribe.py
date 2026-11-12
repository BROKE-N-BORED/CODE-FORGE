"""SKRIBE-gated writes (Note 20) — the shell side of the grading gate.

Joshua's rule: writing is not a bare tool call anymore. When a turn
writes files, the writes are QUEUED here; before the turn counts as
finished the shell issues ONE batched grading pass (worker.py drives
it) instructing the session model to load the SKRIBE skill and grade
every queued file. Grading rides the session's own model only — the
governing-seat rule: no other model, stock or otherwise, may grade.
One exception: the Decisions fast lane (worker.py, settings
`skribe_decisions`, default off) scores files through the Gemini
Decisions API — but that endpoint only measures a score; the shell's
deterministic rule makes the finalize/revise call, so no model holds
the grader seat there either.

The model reports verdicts as prose; the shell trusts only the exact
machine line

    SKRIBE-VERDICT file=<path> fzi=<0-100> decision=<finalize|revise>

parsed by this module — never the surrounding prose (F4: a claim is
not a receipt). Everything here is pure stdlib so it is unit-testable
without Qt.
"""
from __future__ import annotations
import re

VERDICT_RE = re.compile(
    r"SKRIBE-VERDICT\s+file=(.+?)\s+fzi=(\d{1,3})\s+decision=(finalize|revise)",
    re.IGNORECASE)

CLASS_SOURCE = "source file"
CLASS_INSTRUCTIONS = "instruction set"
CLASS_LIST = "source file list"

# A finalize whose own FZI is absolute zero contradicts itself — a
# score of 0 cannot mean "passes SKRIBE as-is". The worker honors a
# finalize only at or above this floor; below it the file follows
# the failing path with its real verdict on record. This floor is
# the WHOLE rule — no higher passing bar is implied by it.
FINALIZE_MIN_FZI = 1

_WRITE_ERROR_PREFIXES = ("Tool error", "Tool notice", "Denied", "Blocked")


def normalize_path(path: str) -> str:
    """Loose-canonical form for matching a verdict line to a queued
    write: quotes off, backslashes to slashes, leading ./ and / off."""
    p = str(path or "").strip().strip("\"'").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def classify_file(path: str) -> str:
    """Receipt label only — every write gets graded regardless.
    Instruction sets win over file lists (a manifest inside skills/
    is still an instruction-set file)."""
    p = normalize_path(path).lower()
    parts = p.split("/")
    name = parts[-1]
    stem = name.rsplit(".", 1)[0]
    if "skills" in parts or "commands" in parts or name == "skill.md" \
            or "instruction" in name:
        return CLASS_INSTRUCTIONS
    if ("manifest" in stem or "repomap" in stem or "repo-map" in stem
            or "repo_map" in stem or "inventory" in stem
            or "filelist" in stem or "file-list" in stem
            or "file_list" in stem or stem.endswith("_list")
            or stem.endswith("-list")):
        return CLASS_LIST
    return CLASS_SOURCE


def parse_verdicts(text: str) -> dict[str, dict]:
    """Extract verdict lines from a grading reply. Malformed lines
    (bad shape, FZI outside 0-100, unknown decision) are ignored —
    a file whose line does not parse simply has NO verdict.
    Keys are normalized lowercase paths; a repeated line for the
    same file overrides the earlier one."""
    found: dict[str, dict] = {}
    for m in VERDICT_RE.finditer(text or ""):
        fzi = int(m.group(2))
        if not 0 <= fzi <= 100:
            continue
        found[normalize_path(m.group(1)).lower()] = {
            "fzi": fzi, "decision": m.group(3).lower()}
    return found


def match_verdicts(queue_paths: list[str],
                   verdicts: dict[str, dict]) -> dict[str, dict]:
    """Pair queued paths with parsed verdicts — ONE verdict pairs
    with AT MOST ONE queued path. A full exact-match pass runs
    first; then a suffix pass (the model may answer with an absolute
    path, or drop a leading folder) over what remains, consuming
    each verdict as it pairs. Ambiguity resolves in queue order:
    the earlier queued path wins, and a queued path left without a
    verdict simply has NO verdict. (Before this, one verdict for
    "a.py" could verify both "a.py" and "src/a.py".)"""
    out: dict[str, dict] = {}
    remaining = dict(verdicts)
    normalized = [(queued, normalize_path(queued).lower())
                  for queued in queue_paths]
    for queued, nq in normalized:
        hit = remaining.pop(nq, None)
        if hit is not None:
            out[queued] = hit
    for queued, nq in normalized:
        if queued in out:
            continue
        for np_ in list(remaining):
            if np_.endswith("/" + nq) or nq.endswith("/" + np_):
                out[queued] = remaining.pop(np_)
                break
    return out


class WriteQueue:
    """This turn's written-but-not-yet-verified files, in write order,
    each carrying its classification label."""

    def __init__(self):
        self._items: dict[str, str] = {}

    def add(self, path: str):
        path = str(path or "").strip()
        if path and path not in self._items:
            self._items[path] = classify_file(path)

    def discard(self, path: str):
        self._items.pop(path, None)

    def paths(self) -> list[str]:
        return list(self._items)

    def items(self) -> list[tuple[str, str]]:
        return list(self._items.items())

    def class_of(self, path: str) -> str:
        return self._items.get(path) or classify_file(path)

    def __len__(self):
        return len(self._items)

    def __contains__(self, path):
        return path in self._items


def successful_write(name: str, args, result, executed: bool) -> str | None:
    """Return the written path when a tool call verifiably wrote a
    file, else None. Built-in write_file proves itself with its
    'Wrote …' result (the same signal the post-edit hooks trust);
    built-in append_file (item 8) proves itself with its 'Appended …'
    or 'Created … by append' result — the queue carries paths, not
    calls, so a file written in chunks still queues exactly once and
    is graded on its final assembled content. An edit_file counts
    when it executed against a path and its result is not an
    error/denial (a built-in since the Write/Research modes,
    2026-11-09; plugin-registered edit tools count the same way)."""
    if not executed or not isinstance(args, dict):
        return None
    path = str(args.get("path", "")).strip()
    if not path:
        return None
    text = str(result or "")
    if name == "write_file":
        return path if text.startswith("Wrote") else None
    if name == "append_file":
        return path if text.startswith(("Appended", "Created")) else None
    if name == "edit_file":
        if text and not text.startswith(_WRITE_ERROR_PREFIXES):
            return path
    return None


def grading_instruction(items: list[tuple[str, str]]) -> str:
    """The shell's one batched grading request for a turn's writes."""
    listing = "\n".join(f"- {path} ({cls})" for path, cls in items)
    return (
        "[SHELL — SKRIBE GATE] This turn wrote files, and writing is "
        "SKRIBE-gated in this Forge: the turn cannot finish until the "
        "writes are graded.\n"
        "1. Call load_skill with name \"skribe\" and follow it exactly "
        "(PCA lock first, deterministic checks before judgment, FZI score).\n"
        "2. Grade each file below on its final on-disk content — read_file "
        "it if you need to. Do NOT edit any file in this pass.\n"
        "3. End your reply with exactly one verdict line per file, in "
        "this exact format and no other:\n"
        "SKRIBE-VERDICT file=<path> fzi=<0-100> decision=<finalize|revise>\n"
        "Use the paths exactly as listed. decision=finalize means the "
        "file passes SKRIBE as-is; decision=revise sends it back for "
        "one revision round.\n"
        f"Files to grade:\n{listing}")


def revise_instruction(failing: list[tuple[str, dict | None]]) -> str:
    """Bounce after a grading pass: revise the failed files, then the
    shell re-grades. Files that verified are not listed and are done."""
    lines = []
    for path, verdict in failing:
        if verdict is None:
            lines.append(f"- {path} — no verdict line was returned for it")
        else:
            lines.append(f"- {path} — SKRIBE verdict: revise, FZI {verdict['fzi']}")
    return (
        "[SHELL — SKRIBE GATE] Grading did not pass these files:\n"
        + "\n".join(lines)
        + "\nRevise them now, using your own grading notes from the "
          "SKRIBE pass above: improve the files and rewrite them with "
          "write_file, then finish your reply. The shell will re-grade "
          "the revised files. Files that already verified are done — "
          "do not touch them.")


def verified_receipt(path: str, cls: str, fzi: int) -> str:
    return f"SKRIBE ▸ {path} · {cls} · FZI {fzi} · VERIFIED"


def revision_receipt(path: str, cls: str, fzi: int) -> str:
    return f"SKRIBE ▸ {path} · {cls} · FZI {fzi} · REVISION REQUIRED"


def unverified_receipt(path: str, cls: str, reason: str) -> str:
    return f"SKRIBE ▸ {path} · {cls} · NOT VERIFIED ({reason})"
