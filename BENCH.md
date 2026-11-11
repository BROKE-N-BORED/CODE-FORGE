# BENCH.md — performance measurements (item #57)

The budget lives in `forge/perf_budget.py` and is summarized in the
README's "Performance envelope" section. Budgets are promises; THIS
sheet is where they get proven, on the owner's actual PC. Nothing on
this page is a result until a run fills it in.

**Rules of the sheet**

- No number counts without the engine versions recorded in the run
  block below. An engine change changes the number; an unversioned
  number cannot be compared with anything.
- One sheet per run. Copy this file (or this section) per measurement
  session; never overwrite a previous run's numbers.
- Every field names its source. If a number came from somewhere else,
  write where — a number without a source is a rumor.

---

## Run block (fill first)

| Field | Value |
|---|---|
| Date | |
| Forge build (zip name / version) | |
| Machine | HP All-in-One 22-df0xxx — Ryzen 3 3250U, 8 GB DDR4, 1 TB HDD *(correct if different)* |
| OS | |
| **Ollama version** (`ollama --version`) | |
| **llama.cpp build** (first lines of the `ollama serve` console / `~/.ollama/logs/server.log`) | |
| GPU | none — CPU only *(correct if different)* |

## Installed models (`ollama list`, copy the SIZE column exactly)

| Seat | Model tag | Size on disk |
|---|---|---|
| Driver | `maternion/minicpm5-heretic:2b` *(correct to what's installed)* | |
| Reviewer | `dolphinlabs/qwen3.5-uncensored:latest` *(correct to what's installed)* | |
| Tester / fast seat | `eriktonon/bonsai-2-uncensored:2b` *(correct to what's installed)* | |
| Other (dictation etc.) | | |

## Generation speed, per seat

Use the same prompt for every seat, in a fresh conversation:
*"Write a Python function that returns the nth Fibonacci number, with
a docstring and two example calls."*

- **tok/s** — source: the turn's own telemetry. Every generation is
  logged with Ollama's exact `eval_count` / `eval_duration` (see the
  event log's `generation` records, `tok_per_s` field). Use the
  driver/reviewer/tester generation that answered the prompt.
- **Time to first token** — the app does not surface this yet, so
  measure it at the engine. Run, per seat model:
  `curl -s http://localhost:11434/api/generate -d '{"model":"TAG","prompt":"Write a Python function that returns the nth Fibonacci number, with a docstring and two example calls.","stream":false}'`
  and compute `(load_duration + prompt_eval_duration) / 1e9` seconds
  from the JSON reply. Record the COLD run (first call after the
  model was unloaded — includes model load) and the WARM run (same
  call again immediately) separately; they answer different
  questions.

| Seat | tok/s | TTFT cold (s) | TTFT warm (s) | Eval tokens |
|---|---|---|---|---|
| Driver | | | | |
| Reviewer | | | | |
| Tester / fast seat | | | | |

## RAM (the budget's combined ceiling is 6 GB — see verdict table)

Sources: Forge process RSS from `ps -o rss= -C python3` (match the
Forge process) or System Monitor; resident models from `ollama ps`
(NAME + SIZE columns); machine total from `free -m` ("used").

| State | Forge RSS (MB) | Resident models (`ollama ps`) | Machine used (MB) |
|---|---|---|---|
| Desktop idle, Forge closed | — | | |
| Forge open, no turn yet | | | |
| Mid driver turn | | | |
| During council review | | | |
| 6+ min after a turn (seat keep-alive expired) | | | |

## Cold start (budget target: ≤ 30 s)

Stopwatch, three launches: start at the `run.sh` / launcher click,
stop when the window is up and the prompt accepts typing. Note for
each launch whether it was the first after a reboot (cold disk
cache) or a repeat.

| Launch | Seconds | First-after-reboot? |
|---|---|---|
| 1 | | |
| 2 | | |
| 3 | | |
| Median | | |

## Forge bench cross-check

Run the built-in bench (forge/bench.py) on the driver model and
record the report's footer numbers. The bench report carries wall
seconds and eval tokens per task — the raw material, kept honest by
the planted checks.

| Field | Value |
|---|---|
| Bench score (passed / total) | |
| Bench total seconds | |
| Bench model tag | |

## Verdict (budget vs measured)

| Budget line | Budget | Measured | Pass? |
|---|---|---|---|
| Forge + resident models, worst state above | ≤ 6 GB | | |
| Forge idle RSS | ≤ 1 GB | | |
| Cold start (median) | ≤ 30 s | | |
| Install footprint | ≤ 6 MiB / 200 files | *(suite-enforced — `tests/test_perf_budget.py` prints the real tree's numbers)* | |

Any line that fails is a finding, not an embarrassment: record it,
and either the code gets lighter or the budget line in
`forge/perf_budget.py` gets revised — with the reason written next
to it, in the same change.
