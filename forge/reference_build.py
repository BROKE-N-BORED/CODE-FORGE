"""Reference-Driven Build — the universal reference-first workflow
(pure stdlib, no Qt, like modes.py / planning.py / gofish.py).

Owner directive (2026-11-09): take the workflow from Shikhar's
Sumiro thread — reference first, break it down, build a slice,
expand module by module, then a use / report / fix loop — and
implement it in Forge for ANY project, not just games. Game/3D is
one profile of the pipeline, never the pipeline itself.

The pipeline, identical for every project type:

  1. REFERENCE LOCK — pin what the reference actually says:
     purpose, user, must-haves, must-nevers, what "done" is.
  2. BREAKDOWN — a strict parts list from the reference, using the
     profile's headings. Anything not visible in the reference is
     UNKNOWN, never invented.
  3. SPEC — one part at a time turned into a buildable spec.
  4. PROTOTYPE SLICE — the smallest usable piece, built and
     verified the profile's way before anything expands.
  5. EXPANSION GRID — the full build as a grid of modules (up to
     8 per grid), built ONE at a time, in queue order.
  6. FIX LOOP — the owner uses it and reports problems in plain
     words; only the named module is patched and re-verified.

Integration (owner directive 2026-11-09: INTEGRATED, not added on
top): this module owns only the workflow's text and data — the
profiles, the six prompts, the plan-document sections, and the
project opt-in record. Everything else is existing Forge
machinery: the skill directory (skills/reference-driven-build/),
the Plan-mode document draft (forge/worker.py asks plan_sections()
below), the approval gate, the seats, the Tester Seat, and the
project's .forge/ area (forge/projects.py creates .forge/
reference/; a project opts in by holding a profile.txt there —
written by the owner or by the agent under the usual approval
gate — and opts out by removing it).
"""
from __future__ import annotations
from pathlib import Path

# The six stages, in order. Stage names are the vocabulary the
# skill, the plan document, and the fix log all share.
STAGES = ("Reference lock", "Breakdown", "Spec", "Prototype slice",
          "Expansion grid", "Fix loop")

# The plan-document sections a reference-driven Plan draft carries:
# the Item #58 Plan skeleton with the workflow's two extra sections
# laid in after "Context found" — the lock and the breakdown are
# context the builder must be able to read in the document itself,
# not facts that live only in an earlier chat turn. Item C7's
# Success criteria / Out of scope sections ride at the end, exactly
# as in the plain skeleton (forge/modes.py).
PLAN_SECTIONS = ["Goal", "Context found", "Reference lock",
                 "Breakdown", "Contents", "Steps", "Risks",
                 "Assumptions", "Success criteria", "Out of scope"]

# Project types. A profile changes ONLY three things: the headings
# the Breakdown lists parts under, what the prototype slice is, and
# how a module is verified. The pipeline never changes.
PROFILES = {
    "generic": {
        "label": "Any project",
        "breakdown": ["Parts", "Dependencies", "Order", "Unknowns"],
        "prototype": "the smallest end-to-end usable piece",
        "verify": "use it the way its user will and report pass or fail plainly",
    },
    "app-ui": {
        "label": "App / website / UI",
        "breakdown": ["Screens", "Components", "Flows", "Data",
                      "States (empty/loading/error/success)", "Unknowns"],
        "prototype": "one screen or one flow, working end to end",
        "verify": "open it on the target device and click through the flow",
    },
    "tool": {
        "label": "Tool / utility / CLI",
        "breakdown": ["Inputs", "Outputs", "Commands/actions",
                      "Edge cases", "Unknowns"],
        "prototype": "one command or action, end to end on a real example",
        "verify": "run it on a real example and compare the output to the reference output",
    },
    "data": {
        "label": "Dashboard / data",
        "breakdown": ["Sources", "Fields", "Views", "Filters",
                      "Refresh/update", "Unknowns"],
        "prototype": "one view showing real data",
        "verify": "check the shown numbers against the source, value by value",
    },
    "writing": {
        "label": "Writing / document system",
        "breakdown": ["Sections", "Tone & voice", "Sources",
                      "Format rules", "Unknowns"],
        "prototype": "one section, drafted in full",
        "verify": "read it as its reader will; grade it when SKRIBE grading is on",
    },
    "music": {
        "label": "Music project",
        "breakdown": ["Sections/arrangement", "Instruments/sounds",
                      "Structure", "Sound targets", "Unknowns"],
        "prototype": "one loop or one section",
        "verify": "listen to it, start to finish",
    },
    "game-3d": {
        "label": "Game / 3D",
        "breakdown": ["Scenes/levels", "Assets", "Mechanics",
                      "Collision & traversal", "Spawn & exit", "Unknowns"],
        "prototype": "one playable scene or level",
        "verify": "play it on the target device",
    },
}

DEFAULT_PROFILE = "generic"


def profile_key(name: str | None) -> str:
    """A profile name resolved to a key: unknown names fall back
    to the generic profile rather than failing — a mistyped
    profile must never block a build."""
    key = str(name or "").strip().lower().replace(" ", "-").replace("_", "-")
    return key if key in PROFILES else DEFAULT_PROFILE


def breakdown_headings(name: str | None) -> list[str]:
    return list(PROFILES[profile_key(name)]["breakdown"])


def verify_method(name: str | None) -> str:
    return PROFILES[profile_key(name)]["verify"]


def plan_sections(active: bool) -> list[str]:
    """The Plan skeleton for a draft: the workflow's sections when
    the project is reference-driven, the plain Item #58 skeleton
    (forge/modes.py) otherwise."""
    if active:
        return list(PLAN_SECTIONS)
    from . import modes as modes_mod
    return list(modes_mod.SKELETONS["plan"])


# --- the project opt-in record ------------------------------------
# .forge/reference/profile.txt — first non-empty line is the
# profile key. The file's presence is the opt-in; there is no
# setting, no registry, and no second place to look.

def reference_area(root: str | Path) -> Path:
    return Path(root) / ".forge" / "reference"


def active_profile(root: str | Path | None) -> str | None:
    """The project's reference-driven profile key, or None when
    the project has not opted in (no profile.txt, or an empty
    one). Never raises: a broken opt-in record reads as not
    opted in, and the plain workflow proceeds."""
    if root is None:
        return None
    try:
        text = (reference_area(root) / "profile.txt").read_text(
            encoding="utf-8", errors="replace")
    except Exception:
        return None
    for line in text.splitlines():
        if line.strip():
            return profile_key(line)
    return None


def is_active(root: str | Path | None) -> bool:
    return active_profile(root) is not None


# --- the six prompts ----------------------------------------------
# Short, headed, fill-in — written for small local models. These
# are the canonical texts; the skill (skills/reference-driven-
# build/SKILL.md) carries the same six for the agent to follow,
# and a test pins the two together by stage name.

def prompt_for(stage: int, profile: str | None = None) -> str:
    """The prompt text for one stage (1–6) under a profile."""
    key = profile_key(profile)
    prof = PROFILES[key]
    headings = ", ".join(prof["breakdown"])
    if stage == 1:
        return (
            "REFERENCE LOCK. Project type: " + prof["label"] + ". "
            "From the reference the owner gave (file, image, video, "
            "example, or description), extract ONLY: purpose; who "
            "or what it is for; look-or-feel / behaviour; must-haves; "
            "must-nevers; success — what 'done' looks like. Write it "
            "to .forge/reference/lock.md under those headings. "
            "Invent nothing that is not in the reference; anything "
            "unclear goes under 'Open questions', not into the lock.")
    if stage == 2:
        return (
            "BREAKDOWN. Read the project first (homework), then "
            "break the locked reference into parts under these "
            "headings: " + headings + ". For each part: what it is, "
            "its size/priority relative to the whole, what it "
            "depends on, and how it will be verified (" +
            prof["verify"] + "). Table or one-line-per-part list "
            "only. A part you cannot see clearly in the reference "
            "is listed as UNKNOWN — never filled in from imagination. "
            "Write it to .forge/reference/breakdown.md.")
    if stage == 3:
        return (
            "SPEC. Take ONE part of the breakdown at a time and turn "
            "it into a build spec: exact behaviour, the files it "
            "touches, inputs and outputs, edge cases, the "
            "verification step, and an explicit out-of-scope list. "
            "No code yet. One part fully specced beats five parts "
            "sketched.")
    if stage == 4:
        return (
            "PROTOTYPE SLICE. Build only " + prof["prototype"] +
            ", from the spec, using only the files the plan names. "
            "Then verify it: " + prof["verify"] + ". Report the "
            "result plainly — pass or fail, and what was actually "
            "checked. Nothing expands until the slice passes or the "
            "owner accepts it as-is at the gate.")
    if stage == 5:
        return (
            "EXPANSION CARD. Lay the remaining breakdown out as a "
            "grid of modules, at most 8 per grid, in dependency "
            "order. Build ONE module at a time: for the module — "
            "its purpose, spec summary, what it depends on, its "
            "verification (" + prof["verify"] + "). Change nothing "
            "already working outside the module. Announce each "
            "finished module before starting the next; the owner "
            "can stop the queue at any module.")
    if stage == 6:
        return (
            "FIX LOOP. The owner used it and reported, in their own "
            "words: [their report]. Module: [name or number]. Find "
            "the cause, patch only what this problem needs, "
            "re-verify that one module (" + prof["verify"] + "), "
            "and append the report, the cause, and the fix to "
            ".forge/reference/fix-log.md. Do not rebuild, restyle, "
            "or 'improve' any other module while fixing this one.")
    raise ValueError(f"No stage {stage} — the workflow has six stages (1–6).")
