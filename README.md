# Code Forge

**A fully local AI coding agent for Linux.**

[![Donate via Cash App](https://img.shields.io/badge/Donate-Cash_App-00D632?style=for-the-badge&logo=cashapp&logoColor=white)](https://cash.app/$XPRNC8)

Code Forge is a desktop coding agent that runs entirely on your own hardware, using local models served by [Ollama](https://ollama.com). No cloud, no subscription, no account, no kill switch. It plans before it builds, asks your permission before it changes anything, writes and tests real code in a real project folder, and grades its own work before it calls anything done.

It is built for the machines people actually have: **8 GB of RAM and a CPU are enough.** No GPU required.

---

## Why Code Forge

Most coding agents upload your code to someone else's computer, charge you every month, and start changing files the second you hit enter. Code Forge takes the opposite position on all three:

- **Local.** Models run on your machine through Ollama. Your repositories, documents, and ideas stay on your disk.
- **No subscription.** No seats, no usage meter. Download it and it is yours.
- **Permission before action.** Code Forge shows you a plan and waits. Nothing is written, run, or changed until you approve it — and you can cage it to a single project folder.
- **Abliterated models.** Code Forge works with abliterated (uncensored) local models out of the box, and its judging seats — the reviewer, the tester, the grader — are reserved for them. A stock cloud model will never silently grade your work here.
- **Low-spec hardware.** The agent is designed for low-spec Linux machines: 8 GB RAM, CPU-only, spinning disks included.

## Features

### Eight modes, one agent

| Mode | What it does |
| --- | --- |
| **Plan** | Drafts a step-by-step plan and parks it at an approval gate. You read, edit, approve, or kill it. Nothing builds from an unapproved plan. |
| **Design** | Produces design documents — layout, interface, flow, mechanics, look and feel — without drifting into code. |
| **Architect** | Systems thinking: components, data flow, trade-offs, on paper. |
| **Code** | The builder. Writes, edits, and runs code in your project with diff-first approvals. |
| **Review** | Reads code and returns graded findings. Changes nothing. |
| **Ask** | Questions answered. No tools, no file changes. |
| **Write** | A document workshop: read, write, copy, edit, and manage files of any type in a dedicated editor window. Documents are delivered as complete files. |
| **Research** | Deep research, recreated local-first: you approve the research plan, it works in the background across the web and your files, and delivers a cited report as a real document. |

### The approval system

- **Plan gate.** Plans park until you approve them. A parked approval has no timer — you are never rushed into a decision.
- **Diff-first approvals.** File changes are shown as diffs or full content before they are written.
- **Goldfish review.** An optional fresh-eyes pass reads every plan with a clean context and flags blockers before you ever see the gate.
- **SKRIBE grading.** Finished work is graded against a rubric before it is presented as done.

### Seats, not just a model

Code Forge separates the jobs a single model usually fakes:

- **Driver** — the model doing the work. Your pick, always.
- **Reviewer** — a second model that checks the driver's output, with veto power.
- **Tester** — a seat that runs and exercises what was built.
- **Council** — an optional reviewer pass with one revision round.

Governing seats (reviewer, tester, grader) are reserved for abliterated models from the roster you control. The driver seat is unguarded: whoever you pick, drives.

### It extends itself — with your permission

- **Create and update skills.** Code Forge can author new skills for itself and revise existing ones — every creation and update passes your approval gate.
- **Create and update plugins.** New tools arrive as plugins. A plugin whose code fails validation is refused and the existing plugin stays byte-identical. New plugins start **disabled** until you enable them.
- **Forge maintenance.** The agent can work on its own codebase — on a *work copy*, never the live app. You review the diff, run its test suite, and install the update yourself, with rollback.
- **Self-update.** Staged updates verify, back up, swap, and relaunch. A failed verification aborts without touching your working install.

### Work the way you work

- **Mission Control.** Run several projects and turns in parallel, each with its own status and stop control.
- **Phone deck.** A companion web app for your phone: five screens (Chat, Plan, Bench, Docs, Project). Approve plans from the couch, watch a build run, stop a turn, and download finished documents to your phone.
- **Contained mode.** Confine the agent to the project folder — every file tool and command is checked against the cage. Optionally run commands in Docker with only the project folder mounted. (Honest note: without Docker this is a tool-layer guardrail, not an OS sandbox. Code Forge says so in its own settings.)
- **Snapshots and checkpoints.** Git snapshots before edits, one-click restore, and crash resume — kill the app mid-turn and it picks up where you left off.
- **Artifacts.** Plans, designs, reviews, and walkthroughs are captured as documents you keep.
- **Voice dictation.** Local transcription, on your machine.
- **Scheduler, recipes, command palette, UI builder.** Timed runs that park approvals safely, saved prompt recipes, a Ctrl+K palette, and a drag-and-drop window designer that exports runnable PySide6.

### Optional cloud providers

Local is the default and the point. If *you* want cloud speed for a specific job, Code Forge can also talk to **Groq**, **Gemini**, and **EXAONE** through its Providers tab — your keys, stored in an encrypted vault on your machine, assigned per-model roles and scopes through the model registry. Picking a cloud model is the consent; nothing phones home behind your back.

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
4. Read the plan at the gate. Edit it if you like. Hit **Approve**.
5. Watch it build. Every file change, test run, and grade lands where you can see it.

## The phone deck

Enable **Phone deck** from the menu, scan the QR code with your phone, and the deck opens in your phone's browser — no app install, no account. The deck is served by your own Forge on your own network, behind a per-run token shown only in the Forge window.

## Project layout

Each project carries a `.forge/` folder: instructions, plans, design documents, reviews, artifacts, sources, and per-project settings. Your global settings, conversations, and API keys live in the app's own data directory; provider keys are kept in the encrypted vault, not in plain text.

## Honest limits

- Local models are small models. Code Forge is built around that truth — planning, review seats, grading, and repair loops exist because a 2B model with a good harness out-works a 2B model left alone. It will still sometimes be wrong; the approval gates are where you catch it.
- Contained mode without Docker is a guardrail at the tool layer, not an operating-system sandbox.
- Performance numbers in this README are deliberately absent: Forge publishes measured results from its built-in Bench, on real hardware, rather than marketing figures.

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
