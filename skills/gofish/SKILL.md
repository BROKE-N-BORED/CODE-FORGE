name: gofish
description: Fresh-context review of a draft plan before the owner's approval gate — use when a plan draft needs a second pair of eyes that hold no conversation context.

# GOFISH — the fresh-eyes plan review (Item #56)

Plan mode drafts the plan (first response, no tools, nothing executed).
GoFish reviews the draft BEFORE the owner approves it — a fresh
reviewer-seat read, trimmed for small local seats.

## The firewall rule — the whole trick

A gofish review is a FRESH reviewer-seat context holding ONLY:

- the plan text, and
- the current contents of the files the plan names.

No chat history. No conversation. No memory of how the plan was drafted.
A review done in the drafting context is not a gofish review and does
not count — the value is precisely that the reviewer cannot know what
the drafter "meant", only what the plan says.

## The three stages

1. **Zoom Out** — the reviewer judges the plan as a whole: does it reach
   the goal, in a working order, with nothing obviously missing,
   duplicated, or too vague to act on?
2. **Comprehension** — the reviewer paraphrases the plan back in its own
   words. The paraphrase is shown to the owner verbatim. Where the
   paraphrase differs from what the owner meant, the plan is ambiguous —
   that mismatch is the cheapest, highest-value signal a small model can
   produce. Nobody auto-judges the paraphrase.
3. **Implementation Readiness** — MECHANICAL, no model opinion: every
   file and backticked function the plan names is checked against the
   project on disk. A file the plan treats as existing but that is not
   there is a Major; a named function defined in none of the named files
   is a Major; an unmarked missing path is a Nit (creation or wrong
   path — the owner decides).

## Findings

Ranked **Blocker / Major / Nit**, delivered into the existing plan
approval gate (desktop Plan window; the phone deck reads the same plan
state). Findings are ADVISORY — the owner stays the approver; the
gofish has no veto, holds no governing seat, and never blocks the
gate. The reviewer seat is the ordinary council reviewer seat, resolved
through forge/seats.py like every governing seat.

## Bounds

- One revision round max: Blocker/Major findings earn the driver ONE
  revision and ONE re-review, then the plan goes to the owner either way.
- The pass is a sub-agent and lives under the #53 sub-agent caps:
  sequential, a small generation budget, capped output. Each stage is a
  full extra local generation — minutes on a 2B CPU machine. That cost
  is disclosed on the Customize toggle (Customize → Skills), which is
  also where the pass is switched off; switching the gofish skill
  itself off disables the pass too, exactly like SKRIBE grading.
