"""Governing-seat enforcement (audit fix round, bite #3).

The owner's standing law: no un-abliterated model may hold a GOVERNING
seat — judge, reviewer-with-veto, merger/final-say, GRADER, or a router
with content discretion. Until this module existed the law was a
poster: seats were free-text settings, run_tester took its model from
the driver's own tool-call arguments, and an unsaved tester setting
silently fell back to the driver judging itself.

This module is the lock, and the single choke point every seat
resolution passes through:

- ONE roster (BASE_ROSTER below), deny-by-default. A model is
  governing-eligible only if it is on the roster. A name that merely
  CONTAINS "uncensored" proves nothing and earns nothing — no pattern
  guessing, ever.
- The owner may EXTEND the roster via the `governing_roster` agent
  setting (comma- or newline-separated Ollama tags), for models he
  vouches for himself. Extensions add to the base roster; they never
  replace it and never subtract from it.
- Enforcement lives at RESOLUTION time (resolve_seat_model): seat
  settings stay editable text in the dialog — the owner's machine,
  the owner's call — but a non-roster name never actually takes a
  seat. The seat falls back to its roster default and the refusal is
  returned to the caller so it can be logged loudly.

Matching is tag-tolerant: a roster entry without a tag names the whole
repo (any tag of it is eligible); an entry with a tag pins that tag,
and a bare candidate name counts as ":latest" (which is what Ollama
itself would resolve it to).
"""
from __future__ import annotations
import json
import re
from pathlib import Path

from . import permissions as perm_mod

# The owner's verified abliterated roster (2026-11-06), aligned with
# the seat metadata in forge/governor.py's MODELS table.
#
# Every entry is PINNED to the exact tag verified installed on the
# owner's machine (2026-11-07, audit A11). An untagged entry would
# make ANY tag of that repo governing-eligible, so a future bigger
# or different tag could take a governing seat without the owner
# ever vouching for it. A different tag of a listed repo is NOT
# eligible until the owner adds it himself (the settings extension
# below is his way to do that).
#
# NOTE: stock "bonsai-2:4b" is DELIBERATELY absent, and must stay
# absent. It is the un-abliterated control/benchmark model; the whole
# point of this roster is that it may never hold a governing seat.
# Do not add it here, and do not add it via the settings extension
# unless the owner himself orders it with his eyes open.
BASE_ROSTER = (
    "maternion/minicpm5-heretic:2b",       # the driver
    "eriktonon/bonsai-2-uncensored:2b",    # the tester seat (fast seat)
    "dolphinlabs/qwen3.5-uncensored:latest",   # the reviewer seat
)

# Seat default for the tester: Erik, carrying the tag actually
# installed on the owner's machine (the settings-dialog default and
# governor's roster agree on ":2b"). Used when the tester setting is
# empty or refused — NEVER the driver/current model.
DEFAULT_TESTER_MODEL = "eriktonon/bonsai-2-uncensored:2b"


def _split(tag: str) -> tuple[str, str | None]:
    """'repo/name:tag' -> ('repo/name', 'tag'); no tag -> (name, None)."""
    t = str(tag or "").strip().lower()
    if ":" in t:
        repo, _, tg = t.rpartition(":")
        if repo and "/" not in tg:
            return repo, tg
    return t, None


def _matches(candidate: str, entry: str) -> bool:
    c_repo, c_tag = _split(candidate)
    e_repo, e_tag = _split(entry)
    if not c_repo or c_repo != e_repo:
        return False
    if e_tag is None:
        return True  # the entry names the repo; any tag of it serves
    return (c_tag or "latest") == e_tag


def parse_roster_setting(setting: str | None) -> list[str]:
    """The owner's roster extension: comma- or newline-separated tags."""
    if not setting:
        return []
    return [p.strip() for p in re.split(r"[,\n]", str(setting)) if p.strip()]


def governing_allowed(model: str | None, roster_setting: str | None = "") -> bool:
    """THE guard. True only when `model` is on the governing roster
    (base roster plus the owner's settings extension). Deny-by-default:
    empty, unknown, and merely-plausible names are all refused."""
    m = str(model or "").strip()
    if not m:
        return False
    entries = list(BASE_ROSTER) + parse_roster_setting(roster_setting)
    return any(_matches(m, entry) for entry in entries)


def resolve_seat_model(setting: str | None, default: str,
                       roster_setting: str | None = "") -> tuple[str, str | None]:
    """Resolve one governing seat. Returns (model, refused):

    - setting empty      -> (default, None): the seat's roster occupant.
    - setting eligible   -> (setting, None): the owner's pick stands.
    - setting ineligible -> (default, setting): REFUSED. The caller is
      expected to log the refusal loudly; the refused model never runs.
    """
    s = str(setting or "").strip()
    if not s:
        return default, None
    if governing_allowed(s, roster_setting):
        return s, None
    return default, s


def same_model(a: str, b: str) -> bool:
    """Tag-tolerant identity for installed-model checks: same repo, and
    tags that do not conflict (a missing tag matches any tag)."""
    a_repo, a_tag = _split(a)
    b_repo, b_tag = _split(b)
    if not a_repo or a_repo != b_repo:
        return False
    if a_tag is None or b_tag is None:
        return True
    return a_tag == b_tag


def grader_refusal_reason(model: str) -> str:
    """The exact reason SKRIBE reports up when the session model may
    not hold the grader seat (rides the NOT VERIFIED receipts)."""
    return (f"grader seat refused: session model '{model}' is not on "
            f"the governing roster")


# --- Item #41 (audit D42): seats as files + mode→seat bindings -----
#
# Roo-style per-mode seat assignment. An OPTIONAL file — seats.json
# in the app data dir — may (a) override a seat's model and
# (b) bind a permission MODE to a seat, so e.g. Plan mode is driven
# by the reviewer seat's model instead of the combo's. Absence is
# the normal state: no file, no bindings, and the settings rule
# exactly as before. The file never weakens the guard: a file value
# for a governing seat passes through resolve_seat_model exactly
# like a settings value, refusals included. Mode names are
# validated against forge/permissions.MODES — permissions is a
# leaf module (stdlib imports only), so importing it here cannot
# cycle.

SEATS = ("driver", "tester", "reviewer")

# The reviewer seat's roster default, for resolutions the shell
# performs in this module. It MUST agree with forge/council.py's
# DEFAULT_REVIEWER_MODEL (council cannot be imported here — it
# imports this module); a test pins the parity.
DEFAULT_REVIEWER_MODEL = "dolphinlabs/qwen3.5-uncensored"

SEAT_DEFAULTS = {"tester": DEFAULT_TESTER_MODEL,
                 "reviewer": DEFAULT_REVIEWER_MODEL}


def load_seat_file(app_dir) -> tuple[dict, str | None]:
    """Read + sanitize app_dir/seats.json. Returns (config, problem).

    Missing file -> ({}, None): absence is the normal no-bindings
    state. Invalid JSON or a non-object top level -> ({}, a problem
    string naming the file). A valid file is SANITIZED before it is
    returned: only known seats carrying a {"model": "<non-empty
    tag>"} entry and only known modes (forge/permissions.MODES)
    bound to known seats survive — unknown seats, unknown modes
    and malformed entries are dropped individually, never fatal.
    The returned config has the shape {"seats": {...}, "modes":
    {...}} (either may be empty).
    """
    path = Path(app_dir) / "seats.json"
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, f"{path}: could not be read as JSON ({exc})"
    if not isinstance(data, dict):
        return {}, (f"{path}: expected a JSON object with "
                    f"'seats' and 'modes'")
    seats: dict = {}
    raw_seats = data.get("seats")
    if isinstance(raw_seats, dict):
        for name, entry in raw_seats.items():
            key = str(name).strip().lower()
            if key not in SEATS or not isinstance(entry, dict):
                continue
            model = str(entry.get("model") or "").strip()
            if model:
                seats[key] = {"model": model}
    modes: dict = {}
    raw_modes = data.get("modes")
    if isinstance(raw_modes, dict):
        for mode, seat in raw_modes.items():
            mode_key = str(mode).strip().lower()
            seat_key = str(seat).strip().lower() \
                if isinstance(seat, str) else ""
            if mode_key in perm_mod.MODES and seat_key in SEATS:
                modes[mode_key] = seat_key
    return {"seats": seats, "modes": modes}, None


def file_seat_model(seat: str, file_config: dict | None) -> str | None:
    """The model the seats file declares for *seat*, or None."""
    seats = (file_config or {}).get("seats") or {}
    entry = seats.get(seat) or {}
    model = str(entry.get("model") or "").strip()
    return model or None


def mode_bindings(file_config: dict | None) -> dict:
    """The sanitized {mode: seat} bindings (for display/logging)."""
    return dict((file_config or {}).get("modes") or {})


def mode_seat(mode: str, file_config: dict | None) -> str | None:
    """The seat name bound to *mode* by the seats file, or None."""
    seat = mode_bindings(file_config).get(str(mode or "").strip().lower())
    return seat if seat in SEATS else None


def effective_seat_model(seat: str, settings_value: str | None,
                         file_config: dict | None,
                         roster_setting: str | None = ""
                         ) -> tuple[str, str | None]:
    """Resolve one seat with the seats file in play. Returns
    (model, refused) under resolve_seat_model's contract.

    Precedence: the file's seats[seat].model, then the settings
    value, then the seat's roster default. Governing seats
    (tester, reviewer) pass through resolve_seat_model, so a file
    value is guarded exactly like a settings value — a file naming
    a stock model is refused to the default and the refusal rides
    the return value for the caller to log. The DRIVER seat has no
    roster and no guard (the owner's model choice is sovereign):
    its candidate resolves plainly and refused is always None.
    """
    candidate = file_seat_model(seat, file_config)
    if candidate is None:
        candidate = str(settings_value or "").strip()
    if seat == "driver":
        return candidate, None
    default = SEAT_DEFAULTS.get(seat)
    if default is None:
        return candidate, None
    return resolve_seat_model(candidate, default, roster_setting)
