"""Save a finished run as a skill (Manus study M10).

At the end of a successful task — an approved plan whose every
step was announced done — Forge may offer to draft a skill from
the run, via the existing Skill Creator shape (forge/create_ext.py
`create_skill`: `name:` / `description:` / body in SKILL.md, one
folder under the global skills directory).

The draft is PROGRAMMATIC, not a model generation: it is built
only from the run's own record — M8's trajectory fields (goal,
plan status, files changed, errors, open items) plus the M3-armed
success criteria. Nothing is invented; a field with nothing on
record is left out or says so. The draft lands DISABLED, exactly
like every new skill under the S6 rule (forge/main.py
reload_extensions: a name with no known_skills history starts
disabled until the owner enables it in Customize) — it is never
loaded, never run, and never auto-enabled.

The offer itself is OFF by default (settings
`offer_save_run_as_skill`, Customize → Skills). Off means the
worker never builds a draft, never asks, and never changes a
prompt — byte-identical behaviour to before M10.
"""
from __future__ import annotations

import re

# Pseudo tool name for the approval-bridge ask — the same bridge
# as the S6 long-turn prompt (worker.approval_needed), so the
# owner answers in the dialog / phone-deck pending card, untimed
# (F8), and Stop releases the wait as a decline.
SAVE_SKILL_TOOL = "SAVE_RUN_AS_SKILL"

_SLUG_BAD = re.compile(r"[^a-z0-9]+")
_NAME_CLIP = 50
_DESC_CLIP = 140
_LINE_CLIP = 200


def slugify_goal(title: str) -> str:
    """A goal title as a skill slug (create_ext.validate_slug's
    rules: lowercase letters, digits, hyphens). Falls back to
    "saved-run" when the title has nothing slug-shaped in it."""
    slug = _SLUG_BAD.sub("-", str(title or "").strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")[:_NAME_CLIP].strip("-")
    return slug or "saved-run"


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit].rstrip() if len(text) > limit else text


def build_run_skill_draft(trajectory: dict, success: str = "") -> dict:
    """Draft a skill from a run's trajectory (M8's
    context.build_trajectory dict). Returns {"name",
    "description", "body"} — the three arguments create_skill
    takes. Pure composition: no model, no disk, no mutation."""
    traj = trajectory or {}
    plan = (traj.get("plan_status") or {}) if isinstance(traj, dict) else {}
    steps = [str(s) for s in (plan.get("steps") or [])]
    goal = _clip(traj.get("goal") or "", _DESC_CLIP)
    if goal in ("", "(not recorded)"):
        goal = _clip(plan.get("title") or "", _DESC_CLIP) or "Saved run"
    name = slugify_goal(plan.get("title") or goal)
    description = _clip(f"Repeat how this run did it: {goal}", _DESC_CLIP)
    lines = [f"# {goal}",
             "",
             "Drafted by Forge from a finished run — review and edit "
             "before enabling. This skill starts disabled.",
             "",
             "## Purpose",
             goal,
             ""]
    criteria = _clip(success, _LINE_CLIP)
    lines.append("## When to use")
    if criteria:
        lines.append(f"When the job matches this run's goal and its "
                     f"success criteria: {criteria}")
    else:
        lines.append(f"When the job matches this run's goal: {goal}")
    lines.append("")
    lines.append("## Procedure")
    for i, step in enumerate(steps, 1):
        lines.append(f"{i}. {_clip(step, _LINE_CLIP)}")
    lines.append("")
    files = [str(f) for f in (traj.get("files_changed") or [])]
    lines.append("## Files this run touched")
    if files:
        lines.extend(f"- {f}" for f in files)
    else:
        lines.append("(none recorded)")
    lines.append("")
    errors = [str(e) for e in (traj.get("errors") or [])]
    if errors:
        lines.append("## What went wrong on the way")
        lines.append("Pitfalls this run hit — check for them first:")
        lines.extend(f"- {e}" for e in errors)
        lines.append("")
    open_items = [str(o) for o in (traj.get("open_items") or [])]
    if open_items:
        lines.append("## Open items left from the run")
        lines.extend(f"- {_clip(o, _LINE_CLIP)}" for o in open_items)
        lines.append("")
    lines.append("## Checks")
    if files:
        lines.append("Verify the files above changed as expected before "
                     "calling the job done.")
    else:
        lines.append("Verify the outcome against the success criteria "
                     "above before calling the job done.")
    body = "\n".join(lines).strip() + "\n"
    return {"name": name, "description": description, "body": body}


def draft_skill(skills_dir, draft: dict, create_skill) -> tuple[bool, str, str]:
    """Write the draft through create_skill. A taken name never
    overwrites: the slug gains a -2, -3, … suffix until one lands
    (bounded at 99 tries). Returns (ok, name_used, message) — the
    message is create_skill's own, so the owner reads the same
    wording the Skill Creator path gives."""
    base = str((draft or {}).get("name") or "saved-run")
    for i in range(1, 100):
        name = base if i == 1 else f"{base}-{i}"
        ok, message = create_skill(skills_dir, name,
                                   draft.get("description", ""),
                                   draft.get("body", ""))
        if ok:
            return True, name, message
        if "already exists" not in str(message):
            return False, name, message
    return False, base, ("create_skill refused: no free name after "
                         "99 tries.")
