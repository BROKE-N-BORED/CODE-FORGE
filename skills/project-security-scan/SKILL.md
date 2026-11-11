name: project-security-scan
description: Scan the owner's project for committed secrets, dangerous config, and risky code patterns — run when the owner asks for a security check, a secrets audit, or a pre-commit / pre-publish review of their project.

# Project Security Scan — deterministic first, judgment second

A security pass over the OWNER'S PROJECT (never over Forge
itself). The scan is a bundled script that finds things by
pattern; your job is to run it, read what it actually found, and
walk the owner through it in plain words. The script's findings
are the facts — do not add concerns it did not raise, and do not
soften the ones it did.

## 1. Run the scanner FIRST

Before reading any project file yourself, run the bundled
scanner through the normal run_command tool (it goes through
the approval gate like any command):

    python3 "<this skill's folder>/scan.py" "<project root>"

scan.py sits beside this SKILL.md in the skill's folder — use
that folder's real path. It is read-only: it writes nothing,
changes nothing, and needs nothing installed (standard library
only). Its report is JSON on stdout:

- `summary` — counts per severity and a total.
- `findings` — each with severity (HIGH / MEDIUM / LOW),
  category, file path, line number (null for file-level
  findings), a plain message, and evidence.
- Exit code 0 means the scan ran — findings are data, not a
  failure. A non-zero exit means the scanner itself failed;
  report that plainly instead of improvising a scan by eye.

What it looks for: private key blocks; AWS / GitHub / sk- /
Slack token shapes; hardcoded values assigned to
password/secret/api-key-style names (obvious placeholders are
skipped); .env files present (named only — contents are never
read); credentials*/secret* filenames; a git remote URL
(credentials stripped, reported not judged); and, in the
project's own source, line-level heuristics for eval/exec on
non-literal input, subprocess shell=True, pickle.loads, SQL
built by string formatting into execute(), and hardcoded
0.0.0.0 binds. It skips .git internals, venvs, node_modules,
binary files, and files over 1 MB.

## 2. Present the findings

- Group by severity, HIGH first. For each finding give the
  file and line and say in one plain sentence what it is and
  why it matters. Evidence for a secret is already redacted by
  the scanner (a short prefix + length) — quote only that
  redacted form. Never try to recover, guess, or display a
  full secret value, and never read a flagged .env file to
  "check" it.
- Heuristic findings are labelled "(heuristic)" by the
  scanner — present them as things to look at, not verdicts.
  If you read the flagged line and it is plainly safe (a
  literal, a test fixture), say so and move on.
- A clean scan is reported as clean: "the scan found nothing"
  is a complete result. Do not invent residual concern to
  sound thorough.

## 3. Fixes — one at a time, never automatic

- Never auto-fix a finding. Offer fixes one finding at a
  time, highest severity first, and let the owner pick.
- Each fix goes through the ordinary approval flow like any
  other change (write_file / run_command gates apply as
  usual). Typical fixes: move a secret into an environment
  variable or the project vault and replace the literal with
  a lookup; add the file to .gitignore; parameterize a SQL
  query. A committed secret also needs ROTATING at its
  provider — removing it from the file does not un-leak it;
  say that once, plainly, for the first secret finding.
- After fixes the owner approves, offer to re-run the scanner
  so the improvement is measured, not asserted.

## Honest limits (say these if the owner asks what this covers)

- Pattern-based, not a full audit: it catches known shapes
  and a few risky idioms. It cannot judge business logic,
  runtime configuration, dependency vulnerabilities, or
  secrets in shapes it does not know.
- Redaction is deliberate: the report never contains a full
  secret, so it is safe to show and file — and it also means
  the report alone cannot prove which credential a redacted
  finding is; the owner identifies it by file and line.
- A clean result means "none of these patterns were found",
  not "this project is secure".
