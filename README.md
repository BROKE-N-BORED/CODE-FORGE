# Code Forge

**A fully local AI coding agent for Linux.**

[![Donate via Cash App](https://img.shields.io/badge/Donate-Cash_App-00D632?style=for-the-badge&logo=cashapp&logoColor=white)](https://cash.app/$XPRNC8)

Code Forge is a desktop coding agent for Linux. It runs local models served by [Ollama](https://ollama.com) on your own machine. It drafts a plan and waits for your approval before it writes or changes files, and it runs tests on what it builds.

Local use requires no account and no subscription. Optional cloud providers are off unless you add your own API keys.

## Features

### Eight modes

| Mode | What it does |
| --- | --- |
| **Plan** | Drafts a step-by-step plan and parks it at an approval gate. You read, edit, approve, or kill it. Nothing builds from an unapproved plan. |
| **Design** | Produces design documents — layout, interface, flow, mechanics, look and feel — without drifting into code. |
| **Architect** | Systems thinking: components, data flow, trade-offs, on paper. |
| **Code** | The builder. Writes, edits, and runs code in your project with diff-first approvals. |
| **Review** | Reads code and returns graded findings. Changes nothing. |
| **Ask** | Questions answered. No tools, no file changes. |
| **Write** | A document workshop: read, write, copy, edit, and manage files of any type in a dedicated editor window. Documents are delivered as complete files. |
| **Research** | Deep research, local-first: you approve the research plan, it works in the background across the web and your files, and delivers a cited report as a document. |

### The approval system

- **Plan gate.** Plans park until you approve them. A parked approval has no timer.
- **Diff-first approvals.** File changes are shown as diffs or full content before they are written.
- **Goldfish review.** An optional fresh-eyes pass reads every plan with a clean context and flags blockers before the gate.
- **SKRIBE grading.** Finished work is graded against a rubric before it is presented as done.

### Seats

Code Forge separates the jobs a single model usually fakes:

- **Driver** — the model doing the work. You pick it.
- **Reviewer** — a second model that checks the driver's output, with veto power.
- **Tester** — a seat that runs and exercises what was built.
- **Council** — an optional reviewer pass with one revision round.

Governing seats (reviewer, tester, grader) use abliterated models from the roster you control. The driver seat is your pick.

### Self-extension

- **Skills.** Code Forge can create and update its own skills. Every creation and update passes your approval gate.
- **Plugins.** New tools arrive as plugins. A plugin whose code fails validation is refused and the existing plugin is left unchanged. New plugins start **disabled** until you enable them.
- **Forge maintenance.** The agent can work on its own codebase — on a *work copy*, never the live app. You review the diff, run its test suite, and install the update yourself, with rollback.
- **Self-update.** Staged updates verify, back up, swap, and relaunch. A failed verification aborts without changing your working install.

### Other features

- **Mission Control.** Run several projects and turns in parallel, each with its own status and stop control.
- **Phone deck.** A companion web app for your phone: five screens (Chat, Plan, Bench, Docs, Project). Approve plans, watch a build run, stop a turn, and download finished documents to your phone.
- **Contained mode.** Confines the agent to the project folder — every file tool and command is checked against the project path. Commands can optionally run in Docker with only the project folder mounted. Without Docker this is a tool-layer guardrail, not an OS sandbox.
- **Snapshots and checkpoints.** Git snapshots before edits, one-click restore, and crash resume — kill the app mid-turn and it picks up where it left off.
- **Artifacts.** Plans, designs, reviews, and walkthroughs are saved as documents you keep.
- **Voice dictation.** Local transcription, on your machine.
- **Scheduler, recipes, command palette, UI builder.** Timed runs that park approvals safely, saved prompt recipes, a Ctrl+K palette, and a drag-and-drop window designer that exports runnable PySide6.

### Optional cloud providers

Local models are the default. Code Forge can also talk to **Groq**, **Gemini**, and **EXAONE** through its Providers tab, using API keys you enter. Keys are stored in an encrypted vault on your machine.

## Requirements

- **Linux** (developed and tested on Linux Mint 22 / Ubuntu 24.04)
- **Python 3.11+**
- **[Ollama](https://ollama.com)** installed and running
- **8 GB RAM** minimum class machine; CPU-only is fine, no GPU required
- Optional: **Docker** (only for the Docker-backed contained runs)

## Install

```bash
git clone https://github.com/BROKE-N-BORED/code-forge.git
cd code-forge
chmod +x install.sh run.sh
./install.sh
```

`install.sh` installs what is missing **and lists what is outdated** (system packages, Python packages, Ollama version). Re-running it upgrades in place. Run `./install.sh --list-only` to see the report without changing anything.

Then pull the starter roster of local models:

```bash
ollama pull maternion/minicpm5-heretic:2b
ollama pull eriktonon/bonsai-2-uncensored:2b
ollama pull dolphinlabs/qwen3.5-uncensored:latest
```

Launch it:

```bash
./run.sh
```

## Quick start

1. Pick a **project folder**.
2. Pick a **driver model** from the dropdown — Forge reads your pulled models live from Ollama.
3. Start in **Plan mode**: describe what you want in plain words.
4. Read the plan at the gate, edit it if you want, and approve it.
5. File changes, test runs, and grades are shown in the app as it builds.

## The phone deck

Enable **Phone deck** from the menu, scan the QR code with your phone, and the deck opens in your phone's browser — no app install, no account. The deck is served by your own Forge on your own network, behind a per-run token shown only in the Forge window.

## Project layout

Each project carries a `.forge/` folder: instructions, plans, design documents, reviews, artifacts, sources, and per-project settings. Your global settings, conversations, and API keys live in the app's own data directory; provider keys are kept in the encrypted vault, not in plain text.

## Honest limits

- Local models are small models, and small models make mistakes. The approval gates exist so you can catch them; planning, review seats, and grading are part of the app for the same reason.
- Contained mode without Docker is a guardrail at the tool layer, not an operating-system sandbox.
- This README lists no performance numbers. Forge has a built-in Bench that measures results on your own hardware.

## Support Code Forge

Code Forge is free — no subscription, no account. If it helps you, you can support its development with a one-time donation via Cash App:

**Donate: https://cash.app/$XPRNC8** ($XPRNC8)

## License

See the `LICENSE` file in this repository.

## Contact

**Broke n' Bored Studio**
Email: broke.n.bored.studio@gmail.com
TikTok: [@code.forge41](https://www.tiktok.com/@code.forge41)

*Dream it. Forge it.*
