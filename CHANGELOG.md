# Changelog

## 2026-11-12 — Chat streaming glitch fix
- Fixed: the chat window could show duplicated or overlapping text when scrolled to the bottom during a streaming turn. The streaming anchor had been dead since the Reply links landed — it read the second-to-last block as the bubble header, but the tail bubble is three blocks now (header, body, Reply links), so the anchor never established and every tick rebuilt the whole chat. It now checks the third-to-last block first, with the two-block shape as fallback, and a parked turn at an approval gate no longer repaints at all.

## 2026-11-12 — CodeRabbit review improvements, Tier 2 #1 (linter layer)
- New "Run linters" button in the Review window: runs the project's own installed linters (Ruff, ShellCheck, ESLint) on demand and loads their findings into the composer, in the review format with the tool as the Source. Nothing is installed, nothing auto-runs, nothing auto-fixes. When no linter is installed it says so plainly.

## 2026-11-12 — Widget styling token pass (GUIFX T3)
- Tooltips, tab bars, checkboxes, radio buttons, and list items are now styled from the bench tokens, matching the rest of the window.

## 2026-11-12 — Manus proper fixes F1–F7
- The "[Plan progress]" reminder is retired; plan abandonment is now a real signal (a plan's trajectory records active, superseded, or abandoned, and approving a new plan supersedes the old one).
- Fan-out and Proposals get their own windows (palette entries plus deck items), so the Manus features are reachable from the bench instead of existing only in code.
- New file-change watcher (forge/project_watcher.py): proposes when project files change on disk. Off by default, propose-only, and watches every file — no cap.
- The Knowledge distiller gets its own client instead of sharing the driver's, and the stable-prefix test now proves determinism by time travel instead of pinning a date.

## 2026-11-11 — LOADED MODEL label fix
- The bench header stamp that read LOADED MODEL now reads IN RAM (LOCAL). It names the local models sitting in this PC's RAM (from Ollama), not the model running the turn — hovering it says so plainly, and names the dropdown as the model doing the work. Same data, same refresh, same MODELS row.

## 2026-11-11 — EXAONE rate-limit message fix
- The rate-limit dialog no longer claims Forge shrank its requests for every provider: that shrink exists only for Groq, whose requests are pre-sized to its recorded 8,000 tokens-per-minute cap. For EXAONE and Gemini the dialog now says plainly that Forge does not know the provider's limit and cannot size its requests to fit it.
- The dialog names a time window (per-minute / per-day) only when the provider's own error text names it, or when a recorded cap says so. The provider's full error is still kept in the session log. No number is invented for EXAONE — LG publishes none.

## 2026-11-11 — Gemini thought signature fix
- Gemini 3 models failed with HTTP 400 on the second tool call in a turn ("Function call is missing a thought_signature"). Forge now captures the thoughtSignature Gemini attaches to each functionCall part and sends it back verbatim on the same part in later requests, as Google requires. Parallel calls (only the first carries one) and multi-step calls (each carries its own) are both handled.
- The signature never leaks into other providers: Groq and EXAONE payloads are rebuilt from name and arguments only, and the local Ollama client strips the key before sending.

## 2026-11-11 — Inbox mode
- New Inbox window (menu + palette): one view of what needs attention, what's running, and what's ready to merge, across all projects. Groups: Working, Needs attention, Ready to merge, On hold, Done.
- Triage actions: Dismiss from Inbox (a dismissed item comes back on new activity), Put on Hold, Snooze (1 hour, until tomorrow 09:00, or until next launch), Mark as Done, Undo. Dismiss never stops a running turn and never archives anything. Ready items offer the real next step: approve a parallel job's merge, view its diff, or open a turn's checkpoints.

## 2026-11-11 — Code turns can use tools again (budget-stop fix)
- Fixed: the mid-turn context-budget stop was firing on the first generation of every Code turn (the tool surface had grown past the 90% threshold), so turns wrapped up without ever calling a tool. The stop now applies from the second generation onward; the first generation offers tools whenever the request fits the window at all. Mid-turn wrap-up behavior is unchanged.

## 2026-11-11 — Manus study items M1–M13
- Deterministic system prompts (stable prefix), filesystem context in prompts, programmatic plan recitation, errors kept in the conversation, a stable tool surface (homework and research gates enforced at execution), anti-rut detection, handoff keeper (ASKED/DONE/FILES/EVIDENCE/OPEN, one repair round), trajectory summaries, session replay, save-a-run-as-a-skill (off by default), authoritative sources for Research, fan-out batches (owner-seeded, up to 4), and gated local event triggers (proposals, 24-hour expiry).
- The handoff keeper's learning store is the same one the review room uses — one helper, not two.

## 2026-11-11 — Scrollbar styling (GUIFX T2)
- Scrollbars are styled to match the bench (warm-charcoal track and thumb, no blue).

## 2026-11-11 — CodeRabbit review improvements, Tier 1
- Review turns and Council now carry owner-taught learnings ("Not a finding — teach" in the Review room); findings carry severity labels (Critical/Major/Minor/Trivial) plus type and source; reports get a Walkthrough section and a counted findings heading.
- Owner-approved project guidelines (AGENTS.md and similar) are detected and applied only after approval, never silently. Review profiles: Quiet, Chill, Assertive.
- Incremental review: "Mark reviewed" and "Review what's new" track what's been seen (.forge/reviews/). Per-finding "Prompt for the Driver", "Fix with Driver" (recorded under Fix requests), and owner pre-merge checks at the plan gate (.forge/gate-checks.md).
- The system prompt now ranks its sections and compresses to half the window when it would overflow; unchanged when everything fits.

## 2026-11-11 — Security stack S2–S7
- Secrets are scrubbed from stored conversations and exports; provider keys live in an encrypted vault (not plain text in settings); app files and directories are created with private permissions.
- Project hooks and MCP servers run only after the owner's trust decision; untrusted hooks never execute. Update and extension diffs are shown for approval before anything is applied.

## 2026-11-11 — Button states (GUIFX T1)
- Buttons now show pressed and disabled states across the bench (primary, navigation, accordion, top-bar). No new colors — existing tokens only.

## 2026-11-11 — Design Shell bridge (Phase B1)
- New tool "design_shell": Forge can now call the standalone Design Shell program (installed separately) and drive it from chat — draft a design plan from words, search the shell's library, build approved jobs, generate batches of 3 reference images, file and import finished work.
- Approval stays the owner's: a plan comes back as a plain-English summary and stops; the model cannot approve its own plan. Approving happens from the menu ("Approve Design Shell job") and spends a one-time gate token.
- Built results are COPIED into the project (.forge/design/<job>/) with the description file and build report as lineage; the shell keeps its originals. Reference-image picking uses the shell's picker page (a link is given in the result).
- Heavy work is guarded: a build or image batch refuses in plain words while a local model is resident in RAM, so the shell and Ollama don't thrash the machine. Set the shell's folder under Settings -> Behavior ("Design Shell folder"); keys stay in the environment only, never stored.

## 2026-11-11 — Header loaded-models readout
- The bench header's gauge box gains a third row, MODELS, under RAM and CPU: it shows how many local models are resident in memory and their total weight (e.g. "2 · 3.0 GB"), or "none loaded", or a dash while Ollama isn't answering. Hovering lists each resident model with its size.
- The data comes from the governor's existing Ollama report on the existing 3-second gauge refresh — no new timer, no extra calls. Provider models never appear (they don't run on this machine).

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
