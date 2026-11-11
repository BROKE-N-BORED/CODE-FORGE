# Profiles — what changes per project type

The pipeline never changes. A profile sets three things only:
the Breakdown headings, the prototype slice, and verification.
(Canonical data: forge/reference_build.py PROFILES — this file
is the agent-readable copy; if they ever differ, the .py wins.)

- **generic** (any project) — Breakdown: Parts, Dependencies,
  Order, Unknowns. Slice: the smallest end-to-end usable piece.
  Verify: use it the way its user will; report pass/fail plainly.
- **app-ui** (app / website / UI) — Breakdown: Screens,
  Components, Flows, Data, States (empty/loading/error/success),
  Unknowns. Slice: one screen or flow working end to end.
  Verify: open it on the target device, click through the flow.
- **tool** (tool / utility / CLI) — Breakdown: Inputs, Outputs,
  Commands/actions, Edge cases, Unknowns. Slice: one command
  end to end on a real example. Verify: run it on a real
  example; compare output to the reference output.
- **data** (dashboard / data) — Breakdown: Sources, Fields,
  Views, Filters, Refresh/update, Unknowns. Slice: one view
  with real data. Verify: shown numbers vs source, value by value.
- **writing** (writing / documents) — Breakdown: Sections,
  Tone & voice, Sources, Format rules, Unknowns. Slice: one
  section drafted in full. Verify: read it as its reader will;
  SKRIBE-grade it when grading is on.
- **music** (music project) — Breakdown: Sections/arrangement,
  Instruments/sounds, Structure, Sound targets, Unknowns.
  Slice: one loop or one section. Verify: listen, start to finish.
- **game-3d** (game / 3D) — Breakdown: Scenes/levels, Assets,
  Mechanics, Collision & traversal, Spawn & exit, Unknowns.
  Slice: one playable scene. Verify: play it on the target device.
  (One profile among seven — never treat the workflow itself
  as a game workflow.)
