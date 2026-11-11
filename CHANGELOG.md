# Changelog

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
