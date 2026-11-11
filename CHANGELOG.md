# Changelog

## 2026-11-11 — Performance envelope: footprint ceiling raised
- The install footprint ceiling is raised from 4 MiB / 150 files to 6 MiB / 200 files (owner's call). The tree measured 4,455,870 bytes across 140 files after today's builds landed (browser package, Research/Write rooms, parallel agents, project knowledge, export/convert, Light Machine). The guard still trips on a vendored dependency or stray binary; BENCH.md and the README table state the same numbers.

## 2026-11-11 — Ollama start-timeout fix
- When Ollama fails to start a local model in time ("timed out waiting for llama-server to start", seen on slow hard drives while models load or swap), Forge now waits 3 seconds and retries the generation once on its own, and logs the retry in the session log.
- If the retry also fails, the message is plain language — what happened, that nothing was lost, send the message again — with the raw Ollama text kept in the session log. No other errors are retried.

## 2026-11-11 — Light Machine profile
- New Settings -> Behavior toggle "Light machine: one model in memory at a time" (default off; read at use time; off = previous behavior).
- When on: local helper generations (reviewer, tester, SKRIBE grading, vision seat, knowledge distiller) unload their model when they finish; before a local model runs, a different resident local model is unloaded first, with a log line naming the swap; the knowledge distiller waits for the machine to be idle (up to about 10 minutes, then skips quietly); parallel jobs are capped to 1; local context budgets are capped at 4096. Provider models are never unloaded or capped; the Groq per-minute budget math is unchanged.
- New "Unload all models" button in the Models window: one click unloads every resident local model and shows a receipt line. Works with the toggle on or off.
- Verified with fake clients (no real Ollama): 233 tests on the merged tree, plus 61 for the unload-all addendum; the Settings toggle was probe-checked headlessly.

## 2026-11-11 — Export & Convert
- Export chat: the open conversation (hamburger menu) or a saved one (right-click in history) exports to PDF, Markdown, or HTML, with images embedded at the turn they belong to; the open chat exports with its images — saved chats are stored text-only by design today, and the export dialog says so when a chat has no images
- Markdown exports copy images into a matching _assets folder and link them
- New Converter window: choose a file, pick from the formats that file can become, Convert — documents (PDF/DOCX/ODT to text or Markdown; Markdown/text/HTML to PDF), data (JSON to CSV, CSV to JSON), images (PNG/JPG/BMP/WebP), and, when ffmpeg is installed, audio between formats, video between formats, video to its audio track, and video to GIF
- Audio-to-text is offered when a transcription backend is set up
- If ffmpeg is not installed, one line in the converter window says so with the install command; everything else works

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
