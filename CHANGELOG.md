# Changelog

## 2026-11-11 — Parallel agents
- Mission Control has a Parallel Jobs section: start a job with a task, a project, and a model, and watch its state, elapsed time, and current step
- Code jobs run in their own git worktree of the project — isolated from your files and from each other; read-only jobs (research, design, review) run against the real project and file results in that mode's folder
- Nothing merges back on its own: a finished job shows its diff, and you Approve the merge or Discard it; a merge conflict is reported and leaves your files untouched
- Two jobs at a time by default (adjustable in the jobs section); when the machine is loaded, new jobs wait in the queue and the row says so
- Stop leaves a job's partial work in place for inspection; jobs are recorded in the project (.forge/parallel), so a crash shows them as interrupted instead of losing them
- An approved plan's steps can be dispatched as parallel jobs by ticking them — the tick and Start are your approval; the model does not split work on its own
- A project that is not a git repo gets a plain refusal for code jobs, with read-only jobs offered instead

## 2026-11-11 — Forge Browser inside Code Forge
- The forge/browser package is part of Code Forge now (search, fetch, render, vault, media seats — no API keys)
- Settings → Behavior: Research can use Forge Browser as its web engine instead of the Browse connector; the project's browse allowlist and wall rules apply the same either way
- Browser engine choice (auto / Chromium / Lightpanda) in the same place; installing an engine stays a manual one-time step (browser-setup.sh), nothing downloads from the settings pane
- Every browser verification now files a recording artifact in the project: the step log plus the screenshots taken — it states on its face that it is a log and stills, not a video
- Under the Forge Browser engine, clicking and typing answer in plain words; those actions still ride the Browse connector

## 2026-11-11 — Write canvas
- The Write window is a canvas now: conversation, document, and suggestions side by side in one window
- Type in the document and it auto-saves; every save keeps a steppable version
- Suggest edits: the model proposes changes as cards; Apply lands that one edit, Dismiss drops it — nothing is applied without you
- Length and tone steppers (five steps each) apply to a selection or the whole document
- Selection edits show exactly which passage is targeted

## 2026-11-11 — Project system
- New projects are created prebuilt: the full .forge folder tree and starter files are laid down at creation, never a bare folder
- Every folder carries a short PROMPT.md instruction file, and the agent loads the prompt for the folder a mode works in
- Each project has its own settings file (.forge/settings.json), its own instructions, sources, and uploads
- Creating a project asks its type (generic, app, tool, data, writing, music, game); a non-generic type sets the reference-build profile for that project
- A new project starts as a git repo with a first commit of the scaffold, when git is available; creation succeeds either way and the receipt says which happened
- Every project gets a brief.md (status, decisions, next) created at birth

## 2026-11-11 — Project knowledge
- Finished work distills itself into draft knowledge entries for the project (decisions, dead ends, current state)
- Drafts wait in the Artifacts window: Approve files the entry and adds its summary to the project brief; Discard deletes it; nothing unapproved reaches the agent's context
- Approved entries are indexed like sources (name + one-line summary at rest, full text read on demand with the new read_knowledge tool)
- The project brief and the approved knowledge index are part of what the agent reads when a project opens
- You can add a knowledge entry by hand from the same window

## 2026-11-11 — Research room
- Research window rebuilt: pick sources (Web / project files / supplied files) and depth (Standard / Deep) in the room
- The research plan is drafted and approved in the Research room; nothing browses before approval
- Live run view: current step, pages used of the budget, sites browsed, tokens, elapsed time, Stop button
- Reports open in the room with Sources cited and Sources consulted listed separately; edits save as new versions you can step through
- Web access can be switched off for a run; with it off, web tools are not offered to the model and are refused if requested
- Follow-up questions answer from the open report, or draft a small top-up plan through the same approval gate

One current build lives on the main branch. This file records what changed in each update.

## 2026-11-11

- **Rate-limit fix.** Provider requests to Groq are now sized to Groq's free per-minute token cap (8,000 tokens per minute, prompt and reply counted together) instead of the model's full catalog window, which caused instant "rate limit reached" errors on the first turn. A provider rate-limit error now shows a plain-language message naming the limit that was hit; the raw provider text is kept in the session log. Gemini, EXAONE, and local models are unchanged.
- **README** rewritten to plain facts.
- **Public release.** First public version of Code Forge (the v2.5 build): eight modes with working mode rooms, plan gate and diff approvals, SKRIBE grading, phone deck, optional Groq / Gemini / EXAONE providers with your own keys, local Ollama models by default.
