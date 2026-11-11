# Changelog

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
