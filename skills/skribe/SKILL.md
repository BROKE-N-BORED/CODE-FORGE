name: skribe
description: Grade and recursively improve a code or knowledge file with the FZI rubric — use when asked to score, grade, polish, or verify a file's quality before it ships.

# SKRIBE — grade → enhance → verify → trace

You score only. This procedure makes every continue/finalize call — modules never decide flow (FlowControl rule).

## 1. PCA lock — before anything
Set in 1–2 lines; do not proceed until set:
- **Purpose:** what the file is FOR (one sentence)
- **Audience:** who/what consumes it + level
- **Scope:** in-scope / out-of-scope for this grading

Ambiguous → ask ONE question, on purpose, audience, or scope only.

## 2. Deterministic checks FIRST (code)
Run the facts before any judgment: does it **parse**? does it **run**? do its **tests pass**?
These results anchor Accuracy — a test outcome is a fact, not an opinion.
No checks runnable → say so; Accuracy becomes judgment-only, capped at 50.

## 3. Score
Load `rubric-runtime.md`. Check critical failures first — any one ends scoring.
Score each metric 0–100 against the card's anchors. Model judgment covers soft criteria only: clarity, structure, depth, domain fit.
Return per metric: score + one-line reason. Then: composite, band, categories triggered.

## 4. Recursion — max 2 cycles on this hardware
Enhance → re-score, while ALL hold: cycles used < 2 · last gain > +1 (first cycle exempt) · no degradation predicted · no critical failure.
- Enhance = add what's missing for completeness, expand thin parts, clarify, restructure — aligned to the locked purpose only.
- Per FlowControl: score drops, or gain ≤ +1 → STOP. Finalize the best-scoring version.
(Source loop condition — keep enhancing while FZI < 110, the overshoot target — is overridden here by the cycle cap and gain gate.)

## 5. Output verification gate — before delivery
- File complete: no missing sections, fragments, or truncation
- Structure: sections scoped, unique, non-overlapping. Knowledge files follow the scaffold: metadata / context / core content / implementation / tests & verification / links & references / revision history
- Size ≤ 512 MB

Fail any check → fix or report. Never deliver unverified.

## 6. Trace — always leave one
Append a trace entry: target file · purpose · checks run + results · score before/after each cycle · delta · cycles used · decision per cycle (continue/stop + why) · verification result · final verdict (band + completion reason: maxed / degraded / capped).
