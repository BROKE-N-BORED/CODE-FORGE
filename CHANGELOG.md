# Changelog

One current build lives on the main branch. This file records what changed in each update.

## 2026-11-11

- **Rate-limit fix.** Provider requests to Groq are now sized to Groq's free per-minute token cap (8,000 tokens per minute, prompt and reply counted together) instead of the model's full catalog window, which caused instant "rate limit reached" errors on the first turn. A provider rate-limit error now shows a plain-language message naming the limit that was hit; the raw provider text is kept in the session log. Gemini, EXAONE, and local models are unchanged.
- **README** rewritten to plain facts.
- **Public release.** First public version of Code Forge (the v2.5 build): eight modes with working mode rooms, plan gate and diff approvals, SKRIBE grading, phone deck, optional Groq / Gemini / EXAONE providers with your own keys, local Ollama models by default.
