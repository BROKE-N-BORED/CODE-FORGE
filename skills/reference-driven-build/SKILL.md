name: reference-driven-build
description: Build any project reference-first — lock a reference, break it down, spec one part, build a prototype slice, expand module by module, then fix from the owner's reports. Use when the owner gives an example, screenshot, video, mockup, or existing thing and wants something built from it, in any project type.

# Reference-Driven Build — any project, reference first

The pipeline is always the same six stages, in order, one stage
at a time. It works for apps, websites, tools, dashboards,
documents, music, and games alike — the project type (its
PROFILE, see profiles.md) changes only the breakdown headings,
what the prototype slice is, and how work is verified. Never
skip a stage, never run two stages in one pass, and stop at
each approval gate like every other Forge workflow.

## Opt-in

A project is reference-driven when `.forge/reference/profile.txt`
exists — its first line is the profile key (generic, app-ui,
tool, data, writing, music, game-3d). If the owner asks for this
workflow and no profile.txt exists, propose the profile and
write the file (it is a normal gated write). No file = the
plain workflow runs instead; nothing about this skill applies.

## The six stages

1. **Reference lock** — from the reference the owner gave,
   extract ONLY: purpose, who/what it is for, look-or-feel /
   behaviour, must-haves, must-nevers, success (what "done"
   looks like). Write `.forge/reference/lock.md` under those
   headings. Invent nothing not in the reference; the unclear
   goes under Open questions.
2. **Breakdown** — homework first (read the project), then break
   the locked reference into parts under the profile's headings
   (profiles.md). Per part: what it is, size/priority, depends
   on, how it will be verified. Parts not clearly visible in
   the reference are UNKNOWN — never imagined. Write
   `.forge/reference/breakdown.md`.
3. **Spec** — ONE part at a time into a build spec: exact
   behaviour, files touched, inputs/outputs, edge cases,
   verification step, out-of-scope list. No code yet.
4. **Prototype slice** — build only the profile's smallest
   usable piece, only the files the plan names. Verify it the
   profile's way and report pass or fail plainly, with what
   was actually checked. Nothing expands until the slice
   passes or the owner accepts it at the gate.
5. **Expansion grid** — the rest as a grid of modules, at most
   8 per grid, dependency order, built ONE at a time. Change
   nothing already working outside the module being built.
   Announce each finished module; the owner can stop the
   queue at any module.
6. **Fix loop** — the owner uses it and reports a problem in
   their own words. Patch only that module, re-verify that
   module, and append report / cause / fix to
   `.forge/reference/fix-log.md`. No rebuilding or "improving"
   other modules inside a fix.

## How it rides the existing Forge

- In Plan mode, an opted-in project's plan document carries
  two extra sections — ## Reference lock and ## Breakdown —
  drafted from the stage 1–2 files (forge/reference_build.py
  owns the skeleton; the gate, Goldfish, and checklist work
  exactly as for any plan).
- Seats are the ordinary ones: the vision seat reads image /
  video references, Design drafts visual specs, Architect owns
  Breakdown → Spec, Code builds, the Tester Seat verifies.
- External generators (image, video, or 3D services) are
  OPTIONAL stage-1/3 helpers a spec may call for — never
  required, and never presented as free until verified free.
