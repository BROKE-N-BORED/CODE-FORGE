# SKRIBE rubric — runtime card

Scores 0–100 per metric. Anchors read as 0 / 50 / 100.

## Critical failures — check FIRST
Any one → FZI = 0, rejected, stop. A Critical-severity pitfall voids all other scores.
- False or unverifiable claim where verification is required
- CODE: does not parse, does not run, or crashes on its own happy path
- Content fails its locked purpose entirely

## Scale A — FCI (general content)
- **Peak Performance (30)** min 85 · tgt 98 — 0: purpose missed · 50: purpose partly met · 100: objectives met/exceeded, measurable value
- **Quality (25)** min 75 · tgt 95 — 0: imprecise, broken structure · 50: clear in parts, flow breaks · 100: precise wording, intact structure, cohesive flow
- **Accuracy (25)** min 80 · tgt 98 — 0: false/unverifiable claims · 50: mostly verifiable, gaps · 100: every claim verifiable, terms precise, complete, consistent
- **Efficiency (10)** min 70 · tgt 90 — 0: bloated, redundant · 50: some redundancy · 100: concise, dense, machine-parsable
- **Detail (10)** min 75 · tgt 95 — 0: core detail missing · 50: main points only · 100: comprehensive, granular, cross-referenced

Composite A: `FZI = .30·PP + .25·Q + .25·A + .10·E + .10·D`

## Scale B — Controller (knowledge files)
- **Structure & Organization (20)** — 0: no metadata, no sections · 50: sections present, hierarchy flaws · 100: metadata complete, modular layout, strict heading hierarchy
- **Accuracy & Factual Integrity (20)** — 0: false/speculative claims · 50: minor unverified points · 100: no false claims, definitions correct, internally consistent
- **Usefulness & Utility (20)** — 0: unusable without clarification · 50: works with workarounds · 100: complete, executable as-is, deterministic output
- **Clarity & Readability (15)** — 0: ambiguous throughout · 50: readable, unclear spots · 100: unambiguous, audience-level terms, consistent format
- **Depth & Specificity (15)** — 0: surface only · 50: main cases, thin edge coverage · 100: edge cases + fallback logic, all detail operational
- **Domain Alignment (10)** — 0: off-purpose, drift · 50: mostly on-domain · 100: strictly on objective, domain terms consistent

Composite B: `FZI = .20·S + .20·AdjA + .20·U + .15·C + .15·D + .10·Dom` — cap 100.00

## Code Accuracy anchor (overrides judgment for code)
100: parses + runs + all tests pass · 50: runs but some tests fail, or key paths untested · 0: does not parse or run → critical failure.
Deterministic results outrank judgment. No runnable checks → Accuracy ≤ 50.

## Bands
95–100 Zenith — ship now · 90–94 Superior — ship-ready · 80–89 Strong — minor fixes · 70–79 Acceptable — revisions required · <70 Rejected

## EVS — web validation of Accuracy (when a web tool is available)
Extract key facts (names, dates, statistics, terms) → search each. Prefer sources ≤12 months old; official (.gov/.edu) or peer-reviewed; consensus = >3 independent sources.
EVS = 1.0 verified match (≥3 sources) · 0.8 no valid sources found · 0.5 verified contradiction.
`Adjusted_Accuracy = Accuracy × EVS`. Log citations in the report.
