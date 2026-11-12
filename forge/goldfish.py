"""Goldfish spec review — Item #56 (pure stdlib, no Qt, like
planning.py / skribe.py).

The Elephant already exists: Plan mode drafts a plan and writes no
code. This module is the Goldfish pass that runs BETWEEN the draft
and the owner's approval gate (the method is Google's
Elephant–Goldfish Model, trimmed for small local seats — the method
is adopted, not the plugin).

FIREWALL RULE (the whole trick): a goldfish review runs in a FRESH
reviewer-seat context holding ONLY the plan text plus the contents
of the files the plan names — never the conversation, never the
chat history. build_stage_messages() below is the only door into a
review, and it takes no history argument at all, so a same-context
review cannot be built by accident. A same-context review is not a
goldfish review and does not count.

Three stages, trimmed for 2B-class seats:
  1. Zoom Out — whole-plan sanity at a high level (reviewer model).
  2. Comprehension — the reviewer paraphrases the plan back in its
     own words. The paraphrase is SURFACED to the owner as-is; a
     wrong paraphrase is the ambiguity signal. The shell never
     auto-judges whether a paraphrase is "right".
  3. Implementation Readiness — checked MECHANICALLY against the
     filesystem (do the files / functions the plan names actually
     exist?), never by model opinion.

Findings are ranked Blocker / Major / Nit and are ADVISORY: they
ride into the existing plan-approval gate for the owner to read.
They never veto, block, or auto-revise beyond the single bounded
revision round the worker runs (one revision, one re-review, then
the plan goes to the owner either way). The reviewer seat itself is
resolved by forge/council.py + forge/seats.py exactly as the council
resolves it — this module creates no authority of its own.

Bounds (#53): the pass is a sub-agent, so it lives under the same
caps as the other sub-agents — sequential (concurrency one, inside
the worker thread, like the council), a generation budget resolved
with the bench/tester fallback pattern (a positive int wins, else
MAX_GENERATIONS), per-generation output capped like the council's,
and the named-file content it carries is bounded by the worker's
#53 per-result digest cap.
"""
from __future__ import annotations
import ast
import re
from pathlib import Path

SEVERITIES = ("Blocker", "Major", "Nit")
STAGES = ("zoom", "comprehension")

# Item #53 fallback pattern (bench MAX_CYCLES / tester MAX_CYCLES):
# a positive int setting wins; anything else keeps this default.
# Structurally the pass can never exceed 2 stages x 2 rounds = 4
# reviewer generations anyway — the budget can only trim it.
MAX_GENERATIONS = 4

MAX_NAMED_FILES = 8
PER_FILE_CHARS = 4000

ZOOM_SYSTEM = """You are the REVIEWER seat of a local coding agent, reading a \
draft plan with completely fresh eyes. You have no other context: the plan \
below, and the files it names, are your whole world. Do not claim you ran \
tools or checked anything yourself — you only read what you were handed.

Stage 1 of 3 — ZOOM OUT: judge the plan as a whole, at a high level.
- Is the goal clear, and do the steps actually reach it, in an order that works?
- Is anything obviously missing, duplicated, or out of scope?
- Is any step too vague to act on, or dependent on something the plan never sets up?

Report problems ONLY as finding lines, one per line, in this exact format:
BLOCKER: <the problem, and the step it belongs to>
MAJOR: <the problem, and the step it belongs to>
NIT: <the small problem>
Blocker = the plan fails or does damage if run as written. Major = a step
is likely to go wrong or a needed piece is missing. Nit = polish.
If you find no problem at a level, write no line for it. If the plan is
sound, reply with the single line: NO FINDINGS."""

COMPREHENSION_SYSTEM = """You are the REVIEWER seat of a local coding agent, \
reading a draft plan with completely fresh eyes. You have no other context: \
the plan below, and the files it names, are your whole world.

Stage 2 of 3 — COMPREHENSION: prove you understood the plan by restating it.
Reply in this exact shape:
PARAPHRASE: <the whole plan restated in your own words — what will be done,
in what order, to which files, and what "done" looks like. Stay faithful:
add nothing the plan does not say, drop nothing it does say.>
Then, ONLY if some part of the plan is ambiguous, unclear, or could be read
two ways, add finding lines in this exact format:
BLOCKER: <the ambiguity>
MAJOR: <the ambiguity>
NIT: <the ambiguity>
The owner reads your paraphrase against the plan: where your reading
differs from what they meant, the plan itself is ambiguous — that is the
signal this stage exists for."""

_FINDING_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(BLOCKER|MAJOR|NIT)\s*[:\-—]\s*(\S.*)$", re.IGNORECASE)
_PARAPHRASE_RE = re.compile(r"^\s*PARAPHRASE\s*[:\-—]\s*(.*)$", re.IGNORECASE)

# Path-shaped tokens: a token with a directory part and a known-ish
# extension, or a bare filename with a code/document extension.
_PATH_TOKEN_RE = re.compile(
    r"[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+\.[A-Za-z0-9]{1,8}"
    r"|[A-Za-z0-9_.\-]+\.(?:py|md|json|toml|txt|html|htm|css|js|ts|sh|"
    r"yaml|yml|cfg|ini)")
# Backticked identifiers / `name()` mentions: candidate function names.
_SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)(?:\(\))?`")
_CREATE_VERBS = ("creat", "new", "add", "write", "generat", "scaffold",
                 "introduc", "draft")
_EXISTING_VERBS = ("edit", "modify", "fix", "update", "patch", "read",
                   "open", "change", "refactor", "extend", "remov",
                   "delet", "renam", "move", "replac", "rework")


def resolve_budget(value) -> int:
    """The #53 cap resolution, goldfish edition: a positive int wins;
    None / unset / zero / junk keeps MAX_GENERATIONS."""
    try:
        budget = int(value)
    except (TypeError, ValueError):
        budget = 0
    return budget if budget > 0 else MAX_GENERATIONS


_URL_RE = re.compile(r"https?://\S+")


def named_paths(plan_text: str) -> list[str]:
    """The file paths a plan names, in first-mention order, de-duped
    and capped. Prose that merely looks path-adjacent (URLs, version
    strings like 2.5) is not a path and never comes back."""
    text = _URL_RE.sub("", str(plan_text or ""))
    out: list[str] = []
    for m in _PATH_TOKEN_RE.finditer(text):
        token = m.group(0).strip("`\"'()[]{}<>,;:")
        if "://" in token or token.startswith("."):
            continue
        if token not in out:
            out.append(token)
        if len(out) >= MAX_NAMED_FILES:
            break
    return out


def _line_intent(line: str) -> str:
    """'create' when the step speaks of bringing the file into being,
    'existing' when it speaks of working on a file that is already
    there, '' when the step says neither."""
    low = line.lower()
    if any(v in low for v in _CREATE_VERBS):
        return "create"
    if any(v in low for v in _EXISTING_VERBS):
        return "existing"
    return ""


def _defined_symbols(path: Path) -> set[str]:
    """Top-level def/class names of one Python file (stdlib ast —
    the same trick forge/repomap.py uses). Unparseable = empty."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return set()
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            names.add(node.name)
    return names


def check_readiness(workspace: str, plan_text: str) -> tuple[list[dict], list[dict]]:
    """Stage 3, MECHANICAL: (findings, checks). Every named path is
    stat'ed on disk; every backticked symbol is looked up in the
    named Python files that exist. No model opinion is consulted.

    A missing path is a finding only when the plan treats the file
    as already there: an 'existing'-intent step earns a Major, an
    unmarked step earns a Nit (creation or wrong path — the owner
    decides which), and a 'create'-intent step earns nothing: a
    file a plan creates is supposed to be missing. A backticked
    symbol that no named existing .py file defines earns a Major —
    the step leans on a function that is not where the plan says.
    """
    root = Path(workspace).expanduser() if workspace else None
    findings: list[dict] = []
    checks: list[dict] = []
    lines = str(plan_text or "").splitlines()
    paths = named_paths(plan_text)
    defined: set[str] = set()
    py_files: list[str] = []
    for rel in paths:
        target = (root / rel) if root is not None else None
        exists = bool(target is not None and target.is_file())
        checks.append({"path": rel, "exists": exists})
        if exists and rel.endswith(".py"):
            py_files.append(rel)
            defined |= _defined_symbols(target)
    for rel in paths:
        check = next(c for c in checks if c["path"] == rel)
        if check["exists"]:
            continue
        intent = ""
        for line in lines:
            if rel in line:
                intent = _line_intent(line)
                break
        if intent == "create":
            check["note"] = "to be created by the plan"
        elif intent == "existing":
            check["note"] = "named as existing but not found"
            findings.append({
                "severity": "Major", "stage": "readiness",
                "text": f"Plan names {rel} as a file to work on, but it "
                        f"does not exist in the project."})
        else:
            check["note"] = "not found — creation or wrong path?"
            findings.append({
                "severity": "Nit", "stage": "readiness",
                "text": f"Plan names {rel}, which does not exist yet — "
                        f"if the plan means to create it, that is fine; "
                        f"otherwise the path may be wrong."})
    if py_files:
        seen: set[str] = set()
        for m in _SYMBOL_RE.finditer(str(plan_text or "")):
            name = m.group(1)
            if name in seen or name in defined:
                continue
            seen.add(name)
            # A symbol mentioned on a line that names one of the
            # existing .py files is a claim about that code.
            for line in lines:
                if f"`{name}" in line and any(p in line for p in py_files):
                    findings.append({
                        "severity": "Major", "stage": "readiness",
                        "text": f"Plan names `{name}`, but no function or "
                                f"class by that name is defined in "
                                f"{', '.join(py_files)}."})
                    break
    return findings, checks


def read_named_files(workspace: str, plan_text: str,
                     total_budget: int = 24000) -> dict[str, str]:
    """The named files that exist, read for the reviewer's context,
    bounded like a #53 tool result: per-file cap, total budget,
    first-mention order. Missing files are simply absent — stage 3
    reports them; the reviewer is never handed invented content."""
    root = Path(workspace).expanduser() if workspace else None
    if root is None:
        return {}
    out: dict[str, str] = {}
    spent = 0
    for rel in named_paths(plan_text):
        if spent >= total_budget:
            break
        target = root / rel
        try:
            if not target.is_file():
                continue
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = text[:PER_FILE_CHARS]
        text = text[:max(0, total_budget - spent)]
        if text:
            out[rel] = text
            spent += len(text)
    return out


# Manus study M6 (anti-rut): the stages of a review are repeated
# similar generations off one template — the shape that invites a
# model to fall into the rhythm of its own last answer. Each stage
# therefore carries ONE framing line the program rotates by stage
# index: deterministic (same stage, same line, every run — no
# randomness, no timestamps, so M1's stable-prefix discipline is
# untouched), and confined to this one line: the stage's task,
# the plan text, and the findings format are byte-identical to
# what they were without it.
STAGE_FRAMINGS = (
    "Reviewer emphasis for this stage: weigh the plan's riskiest "
    "assumption first, then work outward from it.",
    "Reviewer emphasis for this stage: start from the plan's final "
    "step and check that the earlier steps earn it.",
    "Reviewer emphasis for this stage: give the step you understand "
    "least your closest attention.",
    "Reviewer emphasis for this stage: check the seams between "
    "steps — hand-offs are where plans tear.",
)


def stage_framing(stage: str) -> str:
    """The framing line for a stage, by its index in STAGES
    (an unknown stage keeps the first line)."""
    index = STAGES.index(stage) if stage in STAGES else 0
    return STAGE_FRAMINGS[index % len(STAGE_FRAMINGS)]


def build_stage_messages(stage: str, plan_text: str,
                         file_contents: dict | None = None) -> list[dict]:
    """THE FIREWALL, as a function shape: a goldfish stage's whole
    world is (standing orders, the plan, the named files' contents).
    There is no history parameter — nothing else can get in."""
    system = ZOOM_SYSTEM if stage == "zoom" else COMPREHENSION_SYSTEM
    parts = ["THE PLAN UNDER REVIEW:\n" + str(plan_text or ""),
             stage_framing(stage)]
    contents = file_contents or {}
    if contents:
        blocks = []
        for rel, text in contents.items():
            blocks.append(f"----- {rel} -----\n{text}")
        parts.append("THE FILES THE PLAN NAMES (current on-disk "
                     "contents):\n" + "\n\n".join(blocks))
    else:
        parts.append("(The plan names no files that could be read, "
                     "or none of them exist yet.)")
    parts.append("Write your review now, in the exact shape your "
                 "instructions require.")
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(parts)}]


def parse_findings(text: str, stage: str) -> list[dict]:
    """Extract BLOCKER:/MAJOR:/NIT: lines from a stage reply.
    Anything else — prose, praise, a 'NO FINDINGS' line — is not a
    finding and is ignored: like SKRIBE verdicts, only the machine
    lines count, never the surrounding prose."""
    out: list[dict] = []
    for line in str(text or "").splitlines():
        m = _FINDING_RE.match(line)
        if m:
            out.append({"severity": m.group(1).capitalize(),
                        "stage": str(stage),
                        "text": m.group(2).strip()})
    return out


def parse_paraphrase(text: str) -> str:
    """The comprehension stage's PARAPHRASE: block — surfaced to the
    owner verbatim, never judged by the shell. Runs from the
    PARAPHRASE: line until the first finding line or the reply's
    end; '' when the stage produced none."""
    lines = str(text or "").splitlines()
    for i, line in enumerate(lines):
        m = _PARAPHRASE_RE.match(line)
        if m:
            block = [m.group(1)]
            for rest in lines[i + 1:]:
                if _FINDING_RE.match(rest):
                    break
                block.append(rest)
            return "\n".join(block).strip()
    return ""


def needs_revision(findings: list[dict]) -> bool:
    """The one-revision trigger: a Blocker or a Major is worth the
    driver's single revision round. Nits ride to the owner as-is —
    they are not worth minutes of generation on the target PC."""
    return any(f.get("severity") in ("Blocker", "Major")
               for f in findings or [])


def revision_instruction(result: dict) -> str:
    """The user-role message handed to the DRIVER (in its own main
    context — the firewall binds the reviewer, not the driver) when
    the goldfish found a Blocker or a Major: revise the draft once,
    as a fresh numbered plan, addressing the findings. The owner
    still approves whatever comes out — this buys one better draft,
    never an auto-approval."""
    lines = []
    for f in result.get("findings") or []:
        lines.append(f"- {f.get('severity', 'Nit')}: {f.get('text', '')}")
    paraphrase = str(result.get("paraphrase") or "").strip()
    if paraphrase:
        lines.append(f"\nHow a fresh reader understood your plan:\n{paraphrase}")
    return (
        "[SHELL — GOLDFISH REVIEW] A fresh reviewer read your draft plan "
        "with no other context and reported:\n"
        + "\n".join(lines)
        + "\n\nRevise the plan ONCE, addressing the Blocker/Major points. "
          "Reply with ONLY the revised plan as a numbered list (1. 2. 3. …) "
          "in the same Plan format — ordered steps, the files each step "
          "touches, how each step will be verified. Do not explain the "
          "changes; the revised plan goes to the owner next either way.")


def new_result(reviewer: str = "") -> dict:
    """A fresh review result — the shape that rides the plan state,
    the sidecar, and the deck projection."""
    return {"reviewer": str(reviewer or ""), "rounds": 0,
            "revised": False, "paraphrase": "", "findings": [],
            "readiness": [], "skipped": ""}


def sanitize_result(data) -> dict | None:
    """Defensive load for the persisted shape: anything malformed
    becomes None (no review on record), never an exception."""
    if not isinstance(data, dict):
        return None
    findings = []
    raw = data.get("findings")
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            severity = str(entry.get("severity", "")).capitalize()
            if severity not in SEVERITIES:
                continue
            findings.append({"severity": severity,
                             "stage": str(entry.get("stage", "")),
                             "text": str(entry.get("text", ""))})
    readiness = []
    raw = data.get("readiness")
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and entry.get("path"):
                readiness.append({"path": str(entry["path"]),
                                    "exists": bool(entry.get("exists")),
                                    "note": str(entry.get("note", ""))})
    try:
        rounds = int(data.get("rounds", 0))
    except (TypeError, ValueError):
        rounds = 0
    return {"reviewer": str(data.get("reviewer", "")),
            "rounds": max(0, rounds),
            "revised": bool(data.get("revised")),
            "paraphrase": str(data.get("paraphrase", "")),
            "findings": findings, "readiness": readiness,
            "skipped": str(data.get("skipped", ""))}


_SEVERITY_MARK = {"Blocker": "⛔", "Major": "⚠", "Nit": "·"}
_STAGE_LABEL = {"zoom": "Zoom Out", "comprehension": "Comprehension",
                "readiness": "Readiness (mechanical)"}


def format_result(result: dict | None) -> str:
    """The gate rendering: findings ranked Blocker → Major → Nit
    (stable within a rank), the paraphrase verbatim, and the
    mechanical readiness tally. Advisory framing is part of the
    text — the owner approves or kills exactly as before."""
    if not result:
        return ""
    if result.get("skipped"):
        return (f"Goldfish review — SKIPPED ({result['skipped']}). "
                f"The plan stands as drafted; the call is yours.")
    findings = list(result.get("findings") or [])
    findings.sort(key=lambda f: SEVERITIES.index(f["severity"])
                  if f.get("severity") in SEVERITIES else len(SEVERITIES))
    lines = []
    rounds = int(result.get("rounds") or 0)
    head = "Goldfish review — fresh reviewer-seat pass"
    if result.get("reviewer"):
        head += f" ({result['reviewer']})"
    head += f", {rounds} round(s)"
    if result.get("revised"):
        head += ", plan revised once"
    head += ". Findings are advisory — you still approve or kill."
    lines.append(head)
    if findings:
        for f in findings:
            stage = _STAGE_LABEL.get(f.get("stage", ""), f.get("stage", ""))
            lines.append(f"{_SEVERITY_MARK.get(f['severity'], '·')} "
                         f"{f['severity']} [{stage}] — {f['text']}")
    else:
        lines.append("No findings — the fresh reader found nothing "
                     "to flag.")
    readiness = result.get("readiness") or []
    if readiness:
        ok = sum(1 for c in readiness if c.get("exists"))
        lines.append(f"Readiness (mechanical): {ok} of {len(readiness)} "
                     f"named file(s) exist.")
        for c in readiness:
            if c.get("note"):
                lines.append(f"  · {c['path']} — {c['note']}")
    paraphrase = str(result.get("paraphrase") or "").strip()
    if paraphrase:
        lines.append("")
        lines.append("How the fresh reader understood the plan "
                     "(a mismatch here flags an ambiguous plan):")
        lines.append(paraphrase)
    return "\n".join(lines)


def summary_line(result: dict | None) -> str:
    """One compact line for logs and the deck projection."""
    if not result:
        return ""
    if result.get("skipped"):
        return f"Goldfish review skipped ({result['skipped']})"
    counts = {s: 0 for s in SEVERITIES}
    for f in result.get("findings") or []:
        if f.get("severity") in counts:
            counts[f["severity"]] += 1
    return (f"Goldfish: {counts['Blocker']} blocker(s), "
            f"{counts['Major']} major(s), {counts['Nit']} nit(s)"
            + (" · revised once" if result.get("revised") else ""))
