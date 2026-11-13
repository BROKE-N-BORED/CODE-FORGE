"""Command palette — Note 14 item 5 (pure stdlib, no Qt, like
planning.py).

Note 14 #5 in the owner's words: "one key combo, type the action
('run tests', 'switch model'), done. Keyboard speed over clicking."
This module owns everything about the palette that is not Qt: the
action catalog as data and the filter that ranks it.

The catalog is a LAUNCHER for what the window already has — it adds
no capabilities. Every entry is a dict:

    {"id": str,        # stable key the window binds to a callable
     "title": str,     # what the owner reads in the list
     "hint": str,      # short: the action's shortcut, or a category
     "group": str,     # one of GROUPS — the dialog's section for it
     "keywords": [str],# extra match words (synonyms, parts)
     "enabled": bool}  # False = shown greyed, runs nothing

G1 (GUIFX Research Brief #1, 2026-11-09): the dialog renders the
catalog under group headers (GROUPS, in that order) instead of one
flat list, shows a keycap badge only for hints that are real
shortcuts (is_shortcut_hint), and — on an empty query only — puts
the owner's recent picks first under a "Recent" pseudo-group
(push_recent / display_groups). Recents never appear twice: a
recent lives in Recent instead of its own group until the owner
starts typing, and a typed query ranks exactly as before.

The fixed actions come from base_catalog() in a defined order (the
empty-query order). Project, mode, and model entries are generated
from the window's live state at open time by project_entries()/
mode_entries()/model_entries() — nothing about the owner's projects,
mode list, or installed models is hardcoded here beyond their entry
shape.

Filter ranking (filter_entries), best first, catalog order breaks
ties inside a tier:
  0  the title starts with the query
  1  a word of the title, or a keyword, starts with the query
     (word-initial match)
  2  the query is a substring of the title
  3  every word of the query appears somewhere in title + keywords
     (covers keyword substrings and multi-word queries)
No tier matches -> the entry is not returned. Disabled entries are
returned like any other (greying is the dialog's job); the query
never hides an action the owner might be looking for.

Q3 (GUIFX task list, 2026-11-12): frecency — a pick that actually
runs is counted (record_use, persisted by the window under the
settings key "palette_use_counts"), and filter_entries orders
inside a tier by use-count first: most-used, then most-recent,
then catalog order. A tier is still match quality — a count never
promotes a worse match above a better one. With no counts and no
recents the ranking is exactly the pre-Q3 one. The empty-query
Recent section keeps recency membership (the same RECENT_CAP most
recent picks) but orders by the same frecency key, so a pick the
owner runs ten times surfaces above one run once an hour ago.

Q5 (GUIFX task list, 2026-11-12): highlight_spans finds every
query term's first occurrence in a title and returns the merged
(start, end) spans, so the dialog can render the matched
characters in amber bold (plain-text segment labels — rich-text
HTML in a QListWidget row clips: QLabel's rich-text layout needs
~3 px more than its sizeHint, and the list's item padding already
squeezes the row; see the Q5 note on PaletteDialog._row_widget).
Terms that appear only in keywords earn no span.
"""
from __future__ import annotations

import re
from pathlib import Path

# The dialog's sections, in display order (G1). Every catalog entry
# carries exactly one of these in its "group" field.
GROUPS = ["Windows", "Run", "Actions", "Go to", "Models"]

# The pseudo-group display_groups puts recent picks under on an
# empty query. It is a rendering section, never an entry's group.
RECENT_GROUP = "Recent"

# How many recent picks the palette remembers and shows.
RECENT_CAP = 5


def _entry(id, title, hint, keywords, group="Actions"):
    return {"id": id, "title": title, "hint": hint, "group": group,
            "keywords": list(keywords), "enabled": True}


def base_catalog() -> list:
    """The window's fixed actions, in the order the palette shows
    them on an empty query. Fresh dicts on every call — the window
    annotates "enabled" on the deck entries per open."""
    return [
        _entry("new_chat", "New conversation", "chat",
               ["new", "chat", "conversation", "fresh"], "Actions"),
        _entry("dock_activity", "Activity panel", "Ctrl+Shift+A",
               ["activity", "log", "panel", "dock"], "Windows"),
        _entry("dock_receipts", "Receipts panel", "Ctrl+Shift+R",
               ["receipts", "panel", "dock"], "Windows"),
        _entry("plan_window", "Plan window", "Ctrl+Shift+P",
               ["plan", "checklist", "window", "panel", "dock"], "Windows"),
        # Remainder R2: abandon a running plan — the owner ends
        # it outright and no resume ever re-arms it.
        _entry("abandon_plan", "Abandon plan…", "",
               ["abandon", "plan", "stop", "end", "cancel",
                "give up", "quit"], "Actions"),
        _entry("run_window", "Run window", "Ctrl+Shift+B",
               ["run", "output", "window", "program", "panel"], "Run"),
        _entry("test_now", "Test now — tester seat", "test",
               ["test", "tester", "run", "tests", "verify", "cadence",
                "untested"], "Run"),
        # Fix F5: the M12 fan-out entry point — the owner
        # supplies the batch (one item per line) and asserts the
        # items are independent; the dialog fans them out.
        _entry("fan_out", "Fan out a batch…", "",
               ["fan", "fan out", "fan-out", "batch", "parallel",
                "independent", "items", "run"], "Run"),
        _entry("testing_window", "Testing window", "window",
               ["testing", "tests", "test", "verify", "watch", "live"], "Windows"),
        _entry("debug_window", "Debug window", "window",
               ["debug", "debugger", "breakpoints", "step", "inspect",
                "pdb", "window"], "Windows"),
        _entry("artifacts_window", "Artifacts window", "",
               ["artifacts", "plan", "design", "review", "walkthrough", "deliverables", "window"], "Windows"),
        _entry("converter_window", "Converter window", "window",
               ["convert", "converter", "pdf", "ffmpeg", "audio",
                "video", "gif", "csv", "json", "image", "window"], "Windows"),
        _entry("export_chat", "Export chat…", "chat",
               ["export", "chat", "transcript", "pdf", "save",
                "share", "reference"], "Actions"),
        _entry("sessions_window", "Sessions window", "",
               ["sessions", "chats", "conversations", "open", "tabs",
                "state", "window"], "Windows"),
        _entry("privacy_window", "Privacy window", "",
               ["privacy", "local", "cloud", "data", "delete",
                "telemetry", "window"], "Windows"),
        _entry("session_notes", "Session notes window", "",
               ["session", "notes", "memory", "fold", "summary",
                "window"], "Windows"),
        _entry("what_ai_sees", "What the AI sees window", "",
               ["context", "sees", "sent", "messages", "summary",
                "notes", "window"], "Windows"),
        _entry("models_window", "Models window", "",
               ["models", "fit", "ram", "size", "publisher",
                "provenance", "window"], "Windows"),
        # DEV-line entries the G1 base predates (#64 mission
        # control, #69 maintenance) — carried into the grouped
        # catalog at the G1 merge; both are windows.
        _entry("mission_control", "Mission control", "window",
               ["mission", "control", "parallel", "agents", "streams",
                "running", "background", "turns"], "Windows"),
        # Fix F6: the M13 proposals room.
        _entry("proposals", "Proposals…", "",
               ["proposals", "proposal", "events", "tester",
                "failed", "run", "fix", "decide", "window"],
               "Windows"),
        _entry("inbox", "Inbox", "window",
               ["inbox", "attention", "triage", "dismiss", "snooze",
                "hold", "merge", "ready", "sessions", "needs",
                "waiting"], "Windows"),
        _entry("templates", "Templates shelf", "window",
               ["templates", "shelf", "starter", "new", "project",
                "ember", "break", "scaffold"], "Windows"),
        # Provider chat (owner ask, 2026-11-09; PC merge):
        # the hosted-model side conversation — a window.
        _entry("provider_chat", "Provider chat", "window",
               ["provider", "chat", "groq", "gemini", "exaone", "cloud",
                "hosted", "talk", "alongside", "side"], "Windows"),
        _entry("turn_timeline", "Turn timeline", "window",
               ["timeline", "turns", "history", "cost", "tokens",
                "budget", "usage", "time"], "Windows"),
        _entry("design_canvas", "Design canvas", "window",
               ["design", "canvas", "board", "frames", "mockup",
                "variants", "lineage", "layout", "spatial"], "Windows"),
        _entry("customize", "Customize", "dialog",
               ["customize", "skills", "plugins", "connectors", "extensions"], "Actions"),
        _entry("settings", "Agent settings", "dialog",
               ["agent", "settings", "options", "model", "preferences"], "Actions"),
        _entry("snapshots", "Snapshots", "Ctrl+Shift+S",
               ["snapshots", "git", "rollback", "restore", "backup"], "Actions"),
        _entry("ui_builder", "UI builder", "Ctrl+Shift+U",
               ["ui", "builder", "layout", "window", "design"], "Windows"),
        _entry("schedule", "Scheduled runs", "dialog",
               ["scheduled", "runs", "schedule", "jobs", "timer"], "Run"),
        _entry("update", "Update & restart", "app",
               ["update", "restart", "upgrade"], "Actions"),
        _entry("maintenance", "Forge maintenance", "app",
               ["maintenance", "self", "repair", "engineer", "groq",
                "gemini", "exaone", "cloud", "update"], "Windows"),
        _entry("rescan", "Rescan skills & plugins", "extensions",
               ["rescan", "reload", "skills", "plugins"], "Actions"),
        _entry("deck_start", "Start phone deck", "phone",
               ["phone", "deck", "start", "serve"], "Actions"),
        _entry("deck_stop", "Stop phone deck", "phone",
               ["phone", "deck", "stop"], "Actions"),
        # G1 catalog completeness (UI review U25 + the EXTFIX
        # editor windows): doors that used to exist only in the
        # menu / inside Customize. All three dialogs and both
        # editor windows are windows, so they group under Windows.
        _entry("bench", "Forge Bench", "dialog",
               ["bench", "benchmark", "tasks", "score", "test", "models"], "Windows"),
        _entry("checkpoints", "Checkpoints", "dialog",
               ["checkpoints", "shadow", "restore", "points", "history"], "Windows"),
        _entry("recipes", "Recipes", "dialog",
               ["recipes", "prompts", "parameterized", "templates"], "Windows"),
        _entry("skill_new", "New skill…", "window",
               ["new", "skill", "create", "editor"], "Windows"),
        _entry("plugin_new", "New plugin…", "window",
               ["new", "plugin", "create", "editor"], "Windows"),
    ]


def project_entries(projects, active_path) -> list:
    """One entry per known project: "Switch to project: <name>".
    The ACTIVE project is disabled — it is already where the owner
    is, and the window's set_project restarts the conversation, so
    "switching" to it would be a destructive no-op, not a switch."""
    entries = []
    for project in projects or []:
        path = project.get("path", "")
        if not path:
            continue
        name = project.get("name") or Path(path).name
        entry = _entry(f"project:{path}", f"Switch to project: {name}",
                       "project", ["switch", "project", name.lower()],
                       "Go to")
        entry["enabled"] = path != active_path
        entries.append(entry)
    return entries


def mode_entries(modes, current) -> list:
    """One entry per permission mode: "Switch mode: <name>". The
    current mode is disabled — selecting it would change nothing."""
    entries = []
    for mode in modes or []:
        entry = _entry(f"mode:{mode.lower()}", f"Switch mode: {mode}",
                       "mode", ["switch", "mode", "permissions", mode.lower()],
                       "Go to")
        entry["enabled"] = mode.lower() != (current or "").lower()
        entries.append(entry)
    return entries


def model_entries(models, current) -> list:
    """One entry per installed model: "Switch model: <name>". The
    current model is disabled — selecting it would change nothing.
    Blank names are skipped: the combo can hold an empty slot while
    the model list is still loading, and an unnamed entry would be
    a switch to nothing."""
    entries = []
    for name in models or []:
        if not name or not str(name).strip():
            continue
        entry = _entry(f"model:{name}", f"Switch model: {name}",
                       "model", ["switch", "model", str(name).lower()],
                       "Models")
        entry["enabled"] = name != (current or "")
        entries.append(entry)
    return entries


def _tier(entry, q, terms):
    title = entry.get("title", "").lower()
    keywords = [str(k).lower() for k in entry.get("keywords", [])]
    words = title.split() + keywords
    hay = title + " " + " ".join(keywords)
    if title.startswith(q):
        return 0
    if any(w.startswith(q) for w in words):
        return 1
    if q in title:
        return 2
    if all(t in hay for t in terms):
        return 3
    return None


def _use_count(counts, entry_id) -> int:
    """How many times `entry_id` has run, per the counts dict.
    Missing, blank, or non-numeric entries count as zero — the
    dict is owner settings and may be hand-edited."""
    if not isinstance(counts, dict):
        return 0
    try:
        return max(0, int(counts.get(entry_id, 0)))
    except (TypeError, ValueError):
        return 0


def record_use(counts, entry_id) -> dict:
    """The use-counts dict after `entry_id` runs: its count
    incremented by one. Pure — the window persists the returned
    dict (settings key "palette_use_counts"), like push_recent.
    A blank id changes nothing (beyond the copy); a non-dict
    input starts a fresh dict."""
    out = dict(counts) if isinstance(counts, dict) else {}
    if entry_id:
        out[entry_id] = _use_count(out, entry_id) + 1
    return out


def filter_entries(entries, query, counts=None, recents=None) -> list:
    """The entries matching `query`, best match first. An empty (or
    whitespace) query returns the full catalog in its defined
    order.

    Q3 frecency: inside a match tier, entries order by use-count
    (most-used first), then by recency (earliest in `recents`
    first; entries absent from `recents` all tie), then by
    catalog order — so the ranking is deterministic and, with no
    counts and no recents, exactly the pre-Q3 tier + catalog
    order. A count never crosses a tier boundary: match quality
    outranks frequency."""
    q = (query or "").strip().lower()
    if not q:
        return list(entries)
    terms = q.split()
    recent_pos = {}
    for pos, rid in enumerate(recents or []):
        recent_pos.setdefault(rid, pos)
    no_recent = len(recents or [])
    ranked = []
    for index, entry in enumerate(entries):
        tier = _tier(entry, q, terms)
        if tier is not None:
            ranked.append((tier, -_use_count(counts, entry.get("id")),
                           recent_pos.get(entry.get("id"), no_recent),
                           index, entry))
    ranked.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    return [entry for *_, entry in ranked]


def highlight_spans(title, query) -> list:
    """[(start, end)] of every query term's first occurrence in
    `title`, merged where terms overlap and sorted by start.
    Matching is case-insensitive; the spans index the original
    title, so the dialog can slice it into plain-text labels.
    Terms that appear only in keywords (not in the title) earn
    no span, and an empty query or no title match returns []."""
    text = str(title or "")
    terms = (query or "").strip().lower().split()
    if not terms:
        return []
    lowered = text.lower()
    spans = []
    for term in terms:
        start = lowered.find(term)
        if start >= 0:
            spans.append((start, start + len(term)))
    if not spans:
        return []
    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


# ---------- G1: groups, shortcut badges, recents ----------

_MODIFIERS = ("ctrl", "alt", "shift", "meta", "cmd", "command")
_KEY_NAMES = {"enter", "return", "esc", "escape", "tab", "space",
              "delete", "del", "backspace", "home", "end",
              "pageup", "pagedown", "up", "down", "left", "right"}
_FKEY = re.compile(r"f\d{1,2}$")


def is_shortcut_hint(hint) -> bool:
    """True when a catalog hint is an actual keyboard shortcut and
    should render as the row's keycap badge ("Ctrl+Shift+A", "F5",
    "Enter"). Category words the catalog also parks in the hint
    field ("chat", "dialog", "phone", "project", "mode", "model",
    "app", "extensions", "window") are not shortcuts and earn no
    badge — the G1 spec: a badge means a key the owner can press."""
    if not hint:
        return False
    text = str(hint).strip().lower()
    if not text:
        return False
    if any(mod in text for mod in _MODIFIERS):
        return True
    return text in _KEY_NAMES or bool(_FKEY.match(text))


def group_entries(entries) -> list:
    """[(group, [entries])] in GROUPS order, preserving the input
    order inside each group; empty groups are omitted. An entry
    with no group (or one outside GROUPS) falls under "Actions" so
    nothing the catalog offers can fall out of the rendering."""
    buckets = {group: [] for group in GROUPS}
    for entry in entries:
        group = entry.get("group") or "Actions"
        if group not in buckets:
            group = "Actions"
        buckets[group].append(entry)
    return [(group, buckets[group]) for group in GROUPS if buckets[group]]


def push_recent(recents, entry_id, cap=RECENT_CAP) -> list:
    """The recents list after `entry_id` runs: most-recent-first,
    deduped, truncated to `cap`. Pure — the window persists the
    returned list. A blank id changes nothing (beyond the cap)."""
    out = [r for r in (recents or []) if r and r != entry_id]
    if entry_id:
        out.insert(0, entry_id)
    return out[:cap]


def display_groups(entries, recents=None, query="", counts=None) -> list:
    """What the dialog renders: [(group, [entries])] for `query`.

    A typed query ranks through filter_entries exactly as before
    and groups the ranked result — recents get no special
    placement, but the frecency key (counts, then recency)
    orders inside each tier. An empty query shows the full
    catalog grouped, except that entries named in `recents`
    (most recent first, ids that no longer exist skipped) move
    up into a leading "Recent" pseudo-group and are NOT repeated
    in their own group — and Q3 orders that section by use-count
    first, MRU position breaking count ties, so the owner's
    most-run recent pick sits at the top."""
    q = (query or "").strip()
    filtered = filter_entries(entries, q, counts=counts, recents=recents)
    if q:
        return group_entries(filtered)
    by_id = {}
    for entry in filtered:
        by_id.setdefault(entry.get("id"), entry)
    recent_entries = []
    recent_pos = {}
    seen = set()
    for pos, rid in enumerate(recents or []):
        if rid in by_id and rid not in seen:
            seen.add(rid)
            recent_pos[rid] = pos
            recent_entries.append(by_id[rid])
    # Q3: membership is recency (the RECENT_CAP most recent
    # picks), order is frecency — most-used first, MRU position
    # breaking count ties.
    recent_entries.sort(key=lambda e: (-_use_count(counts, e.get("id")),
                                      recent_pos[e.get("id")]))
    rest = [e for e in filtered if e.get("id") not in seen]
    groups = group_entries(rest)
    if recent_entries:
        groups = [(RECENT_GROUP, recent_entries)] + groups
    return groups
