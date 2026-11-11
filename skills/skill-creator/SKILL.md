name: skill-creator
description: Create a new Forge skill — interview for purpose and steps, generate the skill folder, register it in the directory, and verify it loads. Use when the owner wants a new skill or wants an existing routine turned into one.

# Skill Creator

You are building a skill for the Forge skill directory. A skill is a folder
under `skills/` containing a `SKILL.md` (the instructions an agent follows)
plus any knowledge files it needs. Follow this procedure exactly.

## 1. Interview (PCA — lock purpose before building)
Ask, one or two at a time, and do not build until these are answered:
- Purpose: what does this skill make the agent able to do?
- Trigger: in what situation should an agent load it? (This becomes the
  one-line description — it is the ONLY part in context at rest, so it must
  say what the skill does AND when to use it.)
- Inputs: what does the agent need in hand before starting?
- Steps: the procedure, in order, in the owner's words where possible.
- Checks: how does the agent know it did the job right?

## 2. Generate the folder
Pick a short lowercase slug (words joined with hyphens). Create:
- `skills/<slug>/SKILL.md`:
  - line 1: `name: <slug>`
  - line 2: `description: <one line — what it does and when to use it>`
  - blank line, then the procedure: terse, operational, numbered steps,
    tables over prose. Write for a small local model: no filler, no theory
    the agent cannot act on.
- Any knowledge the procedure needs goes in separate `.md` files in the same
  folder (they load together with SKILL.md). Keep the whole folder small —
  it must leave room in an 8192-token window to actually work.

## 3. Register
The directory is scanned from the folders themselves — there is no separate
registry to edit. Registration = the folder existing with a valid header.

## 4. Verify it loads
- Re-read the created SKILL.md and confirm lines 1–2 parse as the name and
  description you intended.
- State the final directory entry exactly as it will appear in the index:
  `- <name>: <description>`
- Report the folder path and a rough size. Done — the owner approves skills
  by reading them, like everything else in the Forge.
