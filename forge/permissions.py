"""Permission rules + modes — Note 17.

Two deterministic layers, both owner-authored, neither involves a model
judging anything (the governing-seat rule forbids model-judged
auto-approval; these are pattern matches in the shell):

RULES — persistent allow/ask/deny patterns, created by "Approve &
remember" in the approval dialog or edited in Customize. A rule:
  {"scope": "global" | <project path>, "tool": <name or "*">,
   "pattern": <fnmatch glob matched against the call's target text>,
   "action": "allow" | "ask" | "deny"}
Deny always wins; then a project-scoped allow beats a global one.

H2 (trust completion) narrows what a remembered rule may silently
approve, in this same module, at the one consultation point
(evaluate_rules — the worker's only pre-prompt check, so the dialog,
stored rules, and the phone deck answering the same dialog all flow
through it):
  * SENSITIVE CARVE-OUT — a call whose target is a credential-shaped
    file (.env, *.pem, .ssh/, a credentials file, connectors.json…)
    is NEVER auto-approved; it always asks. Deny still wins over the
    carve-out; only the silent approval is retired.
  * SCOPED REMEMBERING — a remembered run_command allow counts only
    in its new shape: "exact": True (the pattern is the escaped
    literal command) and scoped to the project it was remembered in.
    Old-shape command rules keep loading, stay listed/removable in
    Customize → Permissions, and keep denying — they just no longer
    approve silently. File-tool rules already store the literal
    path as the pattern (exact in practice) and are unchanged.

MODES — named tool-group restrictions for a turn:
  code      — everything, as usual.
  architect — read everything; write_file only for .md files;
              no commands, no write-class plugin tools.
  ask       — read-only. Write tools are not even offered to the model
              (which also makes requests shorter and faster).
  write     — the document workshop (Write/Research spec, 2026-11-09):
              read everything; the full file kit (write / append /
              edit / copy / move / delete) anywhere inside the
              project; no commands.
  research  — read everything plus the browse_open / browse_read
              verbs (admitted by name — browse_open's plugin class
              is "write"); content writes fenced to
              .forge/research/; no commands. The worker's plan
              gate withholds the browse verbs until the owner
              approves a research plan.
Tool classes: built-ins are fixed; a plugin tool counts as "read" when
its manifest does not require approval (v2's read-only connectors and
lookups), and "write" when it does (e.g. the tester seat).

Item #58: MODE_GUIDANCE also carries each mode's workflow discipline
(homework pass, clarification/assumptions, required skeleton) — the
machinery those words drive lives in forge/modes.py (pure text) and
forge/worker.py (the phases themselves). The tool restrictions in
this module are unchanged by it.
"""
from __future__ import annotations
import fnmatch
import posixpath
import re

from . import modes as modes_mod

BUILTIN_CLASSES = {"list_directory": "read", "read_file": "read",
                   "write_file": "write", "append_file": "write",
                   "copy_file": "write", "edit_file": "write",
                   "move_file": "write", "delete_file": "write",
                   "run_command": "write"}
MODES = ("code", "plan", "design", "architect", "review", "ask",
         "write", "research")

# Write/Research modes (spec, 2026-11-09): the file-tool groups
# the two new modes fence by name. CONTENT tools write a file's
# contents; ADMIN tools move whole files around (copy / rename /
# delete — byte-level, content never passes through the model).
FILE_CONTENT_TOOLS = ("write_file", "append_file", "edit_file")
FILE_ADMIN_TOOLS = ("copy_file", "move_file", "delete_file")
# The Browse verbs Research runs on: open navigates, read reads.
# (The acting verbs — click / type / hand_back — are not research
# tools and are never offered in Research mode; browse_open is
# approval-gated at the plugin, which makes its tool CLASS
# "write", so Research admits these two BY NAME, ahead of the
# class check, in mode_allows / mode_visible_tools below.)
RESEARCH_BROWSE_TOOLS = ("browse_open", "browse_read")
# Where Research may write: its own corner of .forge, fenced the
# way Plan's and Review's corners are fenced.
RESEARCH_DIR_MARK = "/.forge/research/"


# Per-mode behavior, appended to the system prompt so the model works
# the seat it is sitting in. The tool restrictions below are enforced
# by the shell regardless of what the model does with these words.
#
# Item #58: every mode's guidance now carries the same three-part
# discipline — a HOMEWORK pass first (the shell offers read tools
# only until the mode ends the pass), the design-changing specifics
# PINNED or else written as explicit ASSUMPTIONS, and a required
# output SKELETON (forge/modes.py SKELETONS). Plan's skeleton is
# enforced by construction (the shell drafts the document section
# by section); the other modes carry theirs here.
_PLAN_SKELETON = " · ".join(f"## {s}" for s in modes_mod.SKELETONS["plan"])
_DESIGN_SKELETON = " · ".join(f"## {s}" for s in modes_mod.SKELETONS["design"])
_ARCH_SKELETON = " · ".join(f"## {s}" for s in modes_mod.SKELETONS["architect"])
_WRITE_SKELETON = " · ".join(f"## {s}" for s in modes_mod.SKELETONS["write"])
_RESEARCH_SKELETON = " · ".join(f"## {s}" for s in modes_mod.SKELETONS["research"])
MODE_GUIDANCE = {
    "code": ("MODE: CODE (Item #58 discipline). Build from gathered context, "
             "not from the raw one-line request: read the files your change "
             "touches (and check the repo map) BEFORE your first edit, and "
             "state in your reply what you read before the first edit lands. "
             "When an approved plan document is in the conversation it is "
             "your working instruction — build from the document, not from "
             "memory of the request. A specific the owner has not pinned "
             "down that would change the build is named in your reply as an "
             "explicit assumption — never a silent invention."),
    "plan": ("MODE: PLAN (Note 12 workflow, reworked Item #58). The shell "
             "drives the turn in phases. (1) HOMEWORK: you are first offered "
             "READ tools only — read the project (its files, the repo map) "
             "until you know what is actually there, then end the pass with "
             "a reply whose first line starts with READY and a one-line "
             "note of what you found. If the project folder is empty, your "
             "draft must say so — never invent contents. If the request "
             "needs no plan, answer it plainly in prose instead — a plain "
             "answer skips the gate. (2) CLARIFY: if a design-changing "
             "specific is still open (who/what it is for, the device or "
             "surface, what the user does, what done looks like), the "
             "shell may hand you a choice point: reply with ONLY the "
             "questions, starting with the line "
             f"'{modes_mod.QUESTIONS_MARKER}', and draft nothing yet. "
             "Never invent answers to your own questions. (3) DRAFT: you "
             "then write the plan DOCUMENT section by section, as the "
             "shell asks for each one, against this skeleton: "
             + _PLAN_SKELETON +
             ". The document IS the plan — write it so a builder could "
             "build from it alone without inventing anything. Each step "
             "is a numbered line carrying its detail, with indented "
             "continuation lines for the how and a 'Verify:' line per "
             "step. Anything the owner did not pin down goes under "
             "## Assumptions as an explicit 'Assumed:' line — a silent "
             "invention anywhere in the document is a defect. The "
             "finished document goes to the owner at a gate — approve, "
             "edit, or kill — and nothing is executed before that "
             "decision. On approval you will be told to work the steps "
             "in order (full tools return for the work) and to announce "
             "STEP <n> DONE as each step completes. During the drafting "
             "generations you are offered no tools and must not write "
             "tool-call markup or XML. REFERENCE-DRIVEN PROJECTS: when "
             "the project has opted into the reference-driven-build "
             "skill (.forge/reference/profile.txt exists), the shell "
             "adds ## Reference lock and ## Breakdown sections to the "
             "skeleton and asks for them in turn — draft those from "
             "the reference exactly as asked, inventing nothing the "
             "reference does not show. This workflow overrides the "
             "skills index and the self-extension note until a plan is "
             "approved: no extension work happens inside a plan draft."),
    "design": ("MODE: DESIGN — the universal design discipline (owner directive, "
               "2026-11-08). Design is everything about how a thing is shaped and how it "
               "behaves: layout and composition, UI and components, wiring (how parts "
               "connect, signal, and respond), mechanics (the rules and systems "
               "underneath), flow (how a user moves through it, screen to screen and "
               "state to state), and the overall aesthetic that makes it feel like one "
               "thing. This holds for ANY project — apps, software, websites, games, "
               "tools, skills, and this Forge itself — and for any stack or language: "
               "design in principles first, never hard-wired to one project, framework, "
               "or tongue.\n"
               "THE KINDS OF DESIGN: layout · ui (components) · flow · mechanics · "
               "aesthetic · systems. Name the kind(s) every artifact belongs to, and "
               "file design artifacts under .forge/design/<kind>/ so the Design window "
               "keeps each kind in its own tab.\n"
               "PROCESS: (1) read the project as it is — what exists, its constraints, "
               "its hardware and performance reality; (2) state the design problem and "
               "which kind(s) it needs BEFORE producing anything; (3) produce the "
               "artifact — mockups and prototypes as HTML/CSS/SVG, mechanics/flow/"
               "systems documents as Markdown, component inventories, wiring described "
               "precisely enough to build from; (4) self-check against the quality bar "
               "before presenting.\n"
               "THE QUALITY BAR (universal): clear hierarchy; consistent spacing and "
               "components — a system, not one-offs; every state designed (empty, "
               "loading, error, success), not just the happy path; flow with no dead "
               "ends; mechanics understandable and tunable; accessibility basics "
               "(contrast, target sizes, readable type); and performance designed-in "
               "from the first sketch — budgets sized for limited hardware (8 GB RAM, "
               "CPU-only), heavy effects justifying their cost. Quality and performance "
               "are design requirements, not cleanup.\n"
               "Do not modify program logic and do not run commands — both are "
               "disabled. Present each artifact so the owner can react to how it "
               "looks, feels, and flows; end by stating what you would hand to Code "
               "mode next, file by file."
               "\nITEM #58 DISCIPLINE: your turn opens with a HOMEWORK pass — "
               "the shell offers READ tools only until you end the pass with "
               "a reply whose first line starts with READY; design from what "
               "the pass actually found (on an empty project the artifact "
               "says the project is empty — never invent contents). Draft "
               "the artifact SECTION BY SECTION against this skeleton: "
               + _DESIGN_SKELETON +
               ". Drafted in sections, in that order — not one free-written "
               "response. Specifics the owner has not pinned down (who it "
               "is for, the surface, what the user does, what done looks "
               "like) are either asked first — end your reply with ONLY "
               "the questions, starting with the line "
               f"'{modes_mod.QUESTIONS_MARKER}' — or written into the "
               "artifact as explicit ASSUMPTIONS. Never silently invent "
               "them."),
    "review": ("MODE: REVIEW. Read and critique — do not fix anything. Report findings by "
               "severity (BLOCKER / SHOULD-FIX / NIT), each with the file and place, the "
               "concrete failure it can cause, and the fix you would suggest. You may write "
               "the report to a review file (*review*.md or .forge/reviews/). Fixes happen "
               "in Code mode, at the owner's word."
               "\nITEM #58 DISCIPLINE: your turn opens with a HOMEWORK pass — "
               "the shell offers READ tools only until you end the pass with "
               "a reply whose first line starts with READY; review what the "
               "pass actually read, never your memory of the code. Every "
               "report ends with a COVERAGE line in exactly this shape: "
               "'COVERAGE — read: <the files you actually read>; NOT read: "
               "<what you did not read, and why>'. A review that cannot say "
               "what it did not read has not said what it is worth."),
    "architect": ("MODE: ARCHITECT. Read everything; write Markdown documentation only "
                  "(architecture notes, decisions, specs). No code edits, no commands."
                  "\nITEM #58 DISCIPLINE: your turn opens with a HOMEWORK pass — "
                  "the shell offers READ tools only until you end the pass "
                  "with a reply whose first line starts with READY; document "
                  "the system the pass actually found (on an empty project "
                  "the document says the project is empty — never invent "
                  "components). Draft the document SECTION BY SECTION "
                  "against this skeleton: "
                  + _ARCH_SKELETON +
                  ". Drafted in sections, in that order — not one "
                  "free-written response. Specifics the owner has not "
                  "pinned down are either asked first — end your reply "
                  "with ONLY the questions, starting with the line "
                  f"'{modes_mod.QUESTIONS_MARKER}' — or listed under "
                  "## Decisions / ## Open questions as explicit open "
                  "questions. Never silently invent them."),
    "ask": ("MODE: ASK. Read-only. Answer the owner's questions from the code itself; "
            "cite files. Do not write or run anything."
            "\nITEM #58 DISCIPLINE: your turn opens with a HOMEWORK pass — "
            "the shell offers READ tools only until you end the pass with "
            "a reply whose first line starts with READY; then answer from "
            "the files you actually read in this turn, never from your "
            "memory of them, and cite those files. Anything the files do "
            "not settle, name as an explicit assumption in the answer "
            "rather than inventing it."),
    "write": ("MODE: WRITE — the document workshop. You write and edit "
              "DOCUMENTS and files of any kind: letters, notes, specs, "
              "READMEs, essays, data files, and code files too. Your "
              "tools: read_file / list_directory to read, write_file / "
              "append_file to create, edit_file for a targeted change "
              "to an existing file (prefer it over rewriting a whole "
              "file — it fails loudly when its match is not found or "
              "not unique, so give old_text enough context to match "
              "exactly once), copy_file to duplicate any file "
              "byte-for-byte, move_file to rename or move, delete_file "
              "to remove (the shell takes a safety checkpoint first). "
              "Commands do not run in this mode — that is Code's seat."
              "\nITEM #58 DISCIPLINE (read-target-first): NEVER edit a "
              "file you have not read in this turn. Read the target "
              "first — and whatever it must agree with (the file it "
              "answers, the data it describes) — then edit from what "
              "you actually read, never from memory of it. Specifics "
              "that would change the document (who it is for, what "
              "kind of document, what done looks like) are either "
              "asked first — end your reply with ONLY the questions, "
              "starting with the line "
              f"'{modes_mod.QUESTIONS_MARKER}' — or stated as explicit "
              "Assumptions in your closing account. The document "
              "itself lives in its FILE, complete — never paste the "
              "whole document into the chat. Close with this skeleton: "
              + _WRITE_SKELETON +
              ". Under Document: the file(s) you wrote or changed and "
              "a one-line account of what each holds. Under "
              "Assumptions: every specific you assumed, or one line "
              "saying the request pinned everything down."),
    "research": ("MODE: RESEARCH — deep research, run like Gemini's and "
                 "kept honest. The shell drives the turn in phases. "
                 "(1) HOMEWORK: you are first offered READ tools only — "
                 "read the project until you know what it already "
                 "holds about the question, then end the pass with a "
                 "reply whose first line starts with READY. Never "
                 "re-derive from the web what the project already "
                 "knows. If the request is answerable from the project "
                 "alone, answer it plainly in prose instead — a plain "
                 "answer skips the gate and no browsing happens. "
                 "(2) PLAN: you then draft the research PLAN document "
                 "section by section, as the shell asks for each one: "
                 "the Question, its Sub-questions (numbered), the "
                 "Sources to search, the Depth (Standard or Deep), "
                 "and Assumptions. The plan goes to the owner at a "
                 "gate — approve, edit, or kill — and NOTHING browses "
                 "before that decision: the browse tools do not exist "
                 "for you until the plan is approved. (3) RUN: on "
                 "approval you work the plan with browse_open / "
                 "browse_read, inside the budget the approval sets "
                 "(page operations and one hour, enforced by the "
                 "shell), numbering sources in the order you first "
                 "open them, and you finish by writing the report to "
                 "the .forge/research/ path the approval names, "
                 "against this skeleton: "
                 + _RESEARCH_SKELETON +
                 " · ## Sources cited. Every claim in Findings "
                 "carries its source number in brackets — a claim "
                 "with no number is a defect. What the research could "
                 "not answer goes under Open questions, plainly. "
                 "Writes in this mode land only in .forge/research/; "
                 "commands do not run. Searches are the only thing "
                 "that leaves this machine — say so when it matters. "
                 "During the drafting generations you are offered no "
                 "tools and must not write tool-call markup or XML."),
}


def tool_class(name: str, classes: dict | None = None) -> str:
    if classes and name in classes:
        return classes[name]
    return BUILTIN_CLASSES.get(name, "write")


DESIGN_EXTS = (".md", ".html", ".htm", ".css", ".svg")


def _write_path_ok(mode: str, path: str) -> bool:
    # Normalize lexically BEFORE any test (audit item A1): judged raw,
    # ".forge/reviews/../../src/evil.py" contains the "/.forge/reviews/"
    # substring and passed — the prefix smuggled the write back out of
    # the special directory. normpath collapses it to "src/evil.py",
    # and every check below judges that true form. Legitimate paths
    # normalize to themselves, so their decisions are unchanged.
    low = posixpath.normpath(path.lower().replace("\\", "/"))
    stem = low.rsplit("/", 1)[-1]
    if mode == "architect":
        return low.endswith(".md")
    if mode == "plan":
        return (low.endswith(".md") and "plan" in stem) or "/.forge/plans/" in ("/" + low)
    if mode == "design":
        return low.endswith(DESIGN_EXTS)
    if mode == "review":
        return (low.endswith(".md") and "review" in stem) or "/.forge/reviews/" in ("/" + low)
    if mode == "write":
        # The document workshop is deliberately NOT fenced to a
        # corner: any path inside the project may be written (the
        # tool layer's jail is what keeps it inside the project).
        return True
    if mode == "research":
        return RESEARCH_DIR_MARK in ("/" + low)
    return False


def mode_allows(mode: str, name: str, args: dict, classes: dict | None = None) -> tuple[bool, str]:
    """(allowed, explanation-if-not)."""
    if mode == "code":
        return True, ""
    if mode == "research" and name in RESEARCH_BROWSE_TOOLS:
        # Research's web verbs, admitted by name ahead of the class
        # check: browse_open is approval-gated at the plugin (a
        # "write" class), but opening and reading pages IS this
        # mode's read work. Whether they are OFFERED yet is the
        # plan gate's business — the worker withholds them until
        # the owner approves a research plan.
        return True, ""
    cls = tool_class(name, classes)
    if cls == "read":
        return True, ""
    if mode == "ask":
        return False, (f"Mode is ASK (read-only): {name} is a write-class tool and is "
                       "disabled in this mode. The owner can switch modes in the header.")
    if mode == "write" and name in FILE_CONTENT_TOOLS + FILE_ADMIN_TOOLS:
        # The workshop's full file kit: content writes are judged
        # per path below; the admin tools (copy / move / delete)
        # are admitted as tools — every path they touch is judged
        # by the tool layer's jail when the call executes.
        if name in FILE_ADMIN_TOOLS:
            return True, ""
    if name in FILE_CONTENT_TOOLS:
        # Item 8's rule, generalized: append_file and edit_file are
        # writes in every sense write_file is — wherever a mode lets
        # write_file touch a path, they may touch the same path;
        # wherever it may not, none of them may.
        path = str(args.get("path", ""))
        if _write_path_ok(mode, path):
            return True, ""
        label = {"plan": "plan files (PLAN.md, *plan*.md, .forge/plans/)",
                 "design": "design artifacts (.md, .html, .css, .svg)",
                 "architect": "Markdown (.md) files",
                 "review": "review reports (*review*.md, .forge/reviews/)",
                 "research": "research files (.forge/research/)"}.get(mode, "files")
        return False, (f"Mode is {mode.upper()}: in this mode {name} may only create {label} "
                       f"(asked for {path}). The owner can switch modes in the header.")
    return False, (f"Mode is {mode.upper()}: {name} is disabled in this mode "
                   "(read + restricted writes only). The owner can switch modes in the header.")


def mode_visible_tools(mode: str, names: list[str], classes: dict | None = None) -> list[str]:
    """Which tools a mode even OFFERS the model (schema-level filter)."""
    if mode == "code":
        return names
    if mode == "ask":
        return [n for n in names if tool_class(n, classes) == "read"]
    if mode == "write":
        # The workshop: reads + the whole file kit. run_command is
        # not offered — commands are Code's seat.
        return [n for n in names if tool_class(n, classes) == "read"
                or n in FILE_CONTENT_TOOLS + FILE_ADMIN_TOOLS]
    if mode == "research":
        # Reads + the content tools (fenced to .forge/research/
        # per call) + the two browse verbs BY NAME (browse_open's
        # plugin class is "write"; see RESEARCH_BROWSE_TOOLS).
        # The worker additionally withholds the browse verbs until
        # a research plan is approved — visibility here is the
        # mode's ceiling, the gate is the turn's state.
        return [n for n in names if tool_class(n, classes) == "read"
                or n in FILE_CONTENT_TOOLS
                or n in RESEARCH_BROWSE_TOOLS]
    # plan / design / architect / review: read tools + write_file /
    # append_file (the paths they may write to are checked per-call
    # in mode_allows — append is offered exactly where write is).
    return [n for n in names if tool_class(n, classes) == "read"
            or n in ("write_file", "append_file")]


# ------------------------------------------------------------------
# H2 — trust completion. Pure functions of the same three values the
# worker already hands evaluate_rules (tool, target text, project),
# so the whole layer lives at that one consultation point — no new
# plumbing through the worker, the dialog, or the deck contract.
# ------------------------------------------------------------------

def fnmatch_escape(text: str) -> str:
    """Escape *text* so fnmatch matches it literally and only
    literally. Remembered command rules store their pattern escaped:
    the raw target-as-pattern let glob characters in a command
    ('*', '?', '[...]') widen what a rule would silently approve."""
    out = []
    for ch in str(text or ""):
        out.append(f"[{ch}]" if ch in "*?[" else ch)
    return "".join(out)


_SENSITIVE_SEGMENTS = {".ssh", ".aws", ".gnupg"}
_SENSITIVE_NAMES = {"connectors.json", "vault.json", "vault.key",
                    ".netrc", ".git-credentials"}
_ENV_TEMPLATE_NAMES = {".env.example", ".env.sample", ".env.template"}
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def is_sensitive_path(path) -> bool:
    """True when a file target names a credential-shaped file:
    .env and .env.* (but NOT the shareable templates .env.example /
    .env.sample / .env.template), key material (*.pem, *.key,
    *.p12, *.pfx, id_rsa*, id_ed25519*), credentials* files, any
    filename containing 'secret', anything under a .ssh/ .aws/ or
    .gnupg/ path component, and the Forge's own secret stores
    (connectors.json — which can hold raw provider keys — and the
    vault files vault.json / vault.key). Matching is on path
    segments and the basename, case-insensitively: erring toward
    asking is the safe direction."""
    text = str(path or "").strip().strip("\"'").replace("\\", "/")
    if not text:
        return False
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        return False
    base = parts[-1].lower()
    if base in _ENV_TEMPLATE_NAMES:
        return False
    if any(part.lower() in _SENSITIVE_SEGMENTS for part in parts):
        return True
    if base in _SENSITIVE_NAMES:
        return True
    if base == ".env" or base.startswith(".env."):
        return True
    if base.startswith(("id_rsa", "id_ed25519")):
        return True
    if base.startswith("credentials"):
        return True
    if "secret" in base:
        return True
    return base.endswith(_SENSITIVE_SUFFIXES)


_COMMAND_SPLIT = re.compile(r"[\s\"']+")
_COMMAND_GLUE = "(),;|&<>"


def command_references_sensitive(command) -> bool:
    """Deterministic token scan of a command string: split on
    whitespace and quotes, look through '=' (so --env-file=.env
    counts), trim shell glue from token edges, and test each token
    as a path. Deliberately simple — it catches the realistic
    shapes (cat/cp/source/ssh -i/openssl against a sensitive file)
    rather than parsing shell grammar."""
    for token in _COMMAND_SPLIT.split(str(command or "")):
        if not token:
            continue
        for candidate in (token, token.rsplit("=", 1)[-1]):
            candidate = candidate.strip(_COMMAND_GLUE)
            if candidate and is_sensitive_path(candidate):
                return True
    return False


def is_sensitive_call(tool: str, target) -> bool:
    """The carve-out test for one call, on its gate-visible target
    text (for file tools the path, for run_command the command —
    exactly the text describe_tool_short shows as the target)."""
    if tool == "run_command":
        return command_references_sensitive(target)
    return is_sensitive_path(target)


def _inside_project(target: str, workspace: str) -> bool:
    """Lexical inside/outside judgement: relative targets resolve
    against the project root (the tool layer's own semantics);
    '..' escapes and foreign absolute paths are outside."""
    text = str(target or "").replace("\\", "/").strip()
    if not text:
        return True
    root = (posixpath.normpath(str(workspace).replace("\\", "/"))
            if str(workspace or "").strip() else "")
    if text.startswith("/"):
        joined = posixpath.normpath(text)
        if not root:
            return False
        return joined == root or joined.startswith(root + "/")
    if not root:
        return True
    joined = posixpath.normpath(root + "/" + text)
    return joined == root or joined.startswith(root + "/")


def classify_risk(tool: str, target, workspace: str = "") -> tuple[str, str]:
    """(label, kind) for one pending call; kind is "caution" or
    "calm" and only steers the label's colour. Pure — the desktop
    approval dialog and the phone deck's pending card share this
    helper, so both surfaces say the same words."""
    target = str(target or "")
    if tool == "run_command":
        if command_references_sensitive(target):
            return ("Runs a command on this PC · touches a sensitive file",
                    "caution")
        return "Runs a command on this PC", "caution"
    if is_sensitive_path(target):
        return "Sensitive file · always asks", "caution"
    if tool_class(tool) == "read":
        return "Read-only · makes no changes", "calm"
    lowered = tool.lower()
    inside = _inside_project(target, workspace)
    if "delete" in lowered or "remove" in lowered:
        # Checkpoints cover deletes: the shadow repo stages
        # deletions (git add -A) and a restore swaps the whole
        # tree back, deleted files included — for files the
        # shadow tracks (its excludes excepted).
        if inside:
            return "Reversible · deletes a file in this project", "calm"
        return "Hard to undo · deletes a file outside this project", "caution"
    if inside:
        return "Reversible · inside this project", "calm"
    return "Hard to undo · outside this project", "caution"


def _command_allow_ok(rule: dict, project: str) -> bool:
    """H2 scoped remembering: a remembered COMMAND allow counts
    only in its new shape — created by the approval dialog with
    "exact": True (its pattern is the escaped literal command) and
    scoped to the project it was remembered in. Old-shape command
    rules (global scope, raw glob pattern — everything the dialog
    saved before H2, and anything hand-written in that shape) no
    longer auto-approve: they keep loading, stay listed and
    removable in Customize → Permissions, and any deny in them
    keeps denying — only the silent approval retired."""
    return (bool(project) and bool(rule.get("exact"))
            and rule.get("scope", "global") == project)


def evaluate_rules(rules: list[dict], tool: str, target: str, project: str = "") -> str:
    """Return 'allow' | 'ask' | 'deny' for one call."""
    matched = []
    for rule in rules or []:
        # Sweep R138: rules are persisted, hand-editable settings
        # data — a malformed entry (a non-dict, a None/non-string
        # pattern) used to raise TypeError out of fnmatch and break
        # the approval path for EVERY call. A rule that cannot be
        # stated cannot match: it is skipped, and the call degrades
        # to 'ask', never to 'allow'.
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("pattern", "*")
        if not isinstance(pattern, str) or not pattern:
            continue
        scope = rule.get("scope", "global")
        if scope != "global" and scope != project:
            continue
        if rule.get("tool") not in (tool, "*"):
            continue
        if fnmatch.fnmatch(target or "", pattern):
            matched.append(rule)
        elif rule.get("action") == "deny" and fnmatch.fnmatchcase(
                (target or "").casefold(), pattern.casefold()):
            # Sweep R147: fnmatch is case-sensitive on Linux, so a
            # deny for *.py silently missed EVIL.PY — the deny layer
            # could be bypassed by casing alone. Deny matching is
            # therefore case-insensitive. Allow matching deliberately
            # keeps the exact semantics: a fix here must never widen
            # a silent approval.
            matched.append(rule)
    if any(r.get("action") == "deny" for r in matched):
        return "deny"
    # H2 carve-out: a sensitive target is never auto-approved — the
    # call asks, whatever any remembered allow says. (Deny above
    # still wins: an owner's deny is not an approval path.)
    if is_sensitive_call(tool, target or ""):
        return "ask"
    for scope in (project, "global"):
        for rule in matched:
            if rule.get("action") != "allow" or rule.get("scope", "global") != scope:
                continue
            if tool == "run_command" and not _command_allow_ok(rule, project):
                continue
            return "allow"
    return "ask"
