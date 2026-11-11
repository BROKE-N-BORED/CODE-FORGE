"""Light Machine profile (owner's order, 2026-11-11).

The owner's PC (Ryzen 3 3250U, 8 GB RAM, CPU-only, HDD) struggles
when Forge's seats each load a model and Ollama keeps them all
resident — the driver at the session keep_alive (default "30m"),
the council reviewer at "5m" (audit B20) — until the machine swaps.

The cure is ONE reversible Settings toggle (`light_machine`,
Settings → Behavior, default OFF) enforcing Forge's own established
pattern for this hardware: one local model in memory at a time,
working in sequence. When the toggle is ON:

1. Every non-driver seat generation (reviewer/council, Goldfish,
   tester, bench, Watch, SKRIBE grading, vision, the Knowledge
   distiller) runs with keep_alive 0, so its model unloads the
   moment it finishes instead of lingering.
2. Before a local generation starts, any DIFFERENT local model
   Ollama currently holds resident is unloaded first (a
   keep_alive-0 request — Ollama's own unload idiom), so at most
   one local model is ever resident. The driver's next turn paying
   a cold reload after a seat ran is the accepted trade, and each
   swap is logged once, in plain words.
3. The Knowledge distiller defers while the governor reads the
   machine busy (forge/knowledge.py's distill_when_idle).
4. Parallel jobs' effective concurrency is capped at 1 (applied at
   read time in forge/parallel.py; the configured value stays).
5. Local context budgets resolve to at most LOCAL_CTX_CAP (an
   extra min() on the locals path of
   forge/providers.resolve_context_budget only — provider budgets,
   Groq's TPM cap included, are untouched).

Provider generations (Groq / Gemini / EXAONE) are NOT Ollama
models: residency rules never touch them — no ps() call, no unload,
no keep_alive change, no budget cap.

Everything here is read at use time and fails soft: a missing ps(),
a client without an unload surface, or a failed unload must never
break a generation. The profile OFF path never calls into this
module's enforcement at all — callers check `enabled` first — so
OFF is the shipped behavior byte-for-byte.
"""
from __future__ import annotations

import inspect

LOCAL_CTX_CAP = 4096
DISTILL_MAX_WAIT_SECONDS = 600.0   # ~10 minutes, then drop silently
DISTILL_POLL_SECONDS = 30.0

_PROVIDER_PREFIXES = ("groq:", "gemini:", "exaone:")


def is_provider_model(model) -> bool:
    """True for a hosted-provider model id (``groq:…`` etc.) — the
    same prefix rule forge/providers.split_model_id applies,
    restated here so this module stays dependency-free (providers
    imports this module's constants, never the reverse)."""
    text = str(model or "").strip().lower()
    return any(text.startswith(p) for p in _PROVIDER_PREFIXES)


def setting_enabled(settings) -> bool:
    """The toggle's value from a settings dict (or a raw value, or
    a callable returning either). Anything unrecognizable is OFF —
    the profile is opt-in, never ambient."""
    value = settings
    if callable(value):
        try:
            value = value()
        except Exception:
            return False
    if isinstance(value, dict):
        value = value.get("light_machine", False)
    return value is True or (
        isinstance(value, str)
        and value.strip().lower() in ("1", "true", "yes", "on")) or (
        isinstance(value, int) and not isinstance(value, bool)
        and value == 1)


def seat_keep_alive(model, default, enabled) -> object:
    """The keep_alive a seat generation should carry: 0 (unload on
    finish) when the profile is ON and the seat's model is local;
    the caller's pinned default otherwise — provider seats and the
    OFF profile keep their value exactly."""
    if enabled and not is_provider_model(model):
        return 0
    return default


def _same_model(a, b) -> bool:
    """Tag-tolerant identity (forge/seats.same_model's rule: same
    repo, tags that do not conflict — a missing tag matches any)."""
    def split(tag):
        t = str(tag or "").strip().lower()
        if ":" in t:
            repo, _, tg = t.rpartition(":")
            if repo and "/" not in tg:
                return repo, tg
        return t, None
    a_repo, a_tag = split(a)
    b_repo, b_tag = split(b)
    if not a_repo or a_repo != b_repo:
        return False
    if a_tag is None or b_tag is None:
        return True
    return a_tag == b_tag


def unload_model(client, model) -> bool:
    """Unload one resident model via Ollama's keep_alive-0 idiom.
    Prefers the client's own unload surface (OllamaClient /
    ProviderClient expose `unload_model`); falls back to a
    keep_alive-0 chat_stream request for clients that only carry
    the stream seam. Never raises; returns True when an unload was
    actually requested."""
    if client is None or not str(model or "").strip():
        return False
    fn = getattr(client, "unload_model", None)
    if callable(fn):
        try:
            return bool(fn(model))
        except Exception:
            return False
    stream = getattr(client, "chat_stream", None)
    if callable(stream):
        try:
            params = inspect.signature(stream).parameters
            accepts = "keep_alive" in params or any(
                p.kind is inspect.Parameter.VAR_KEYWORD
                for p in params.values())
        except (TypeError, ValueError, AttributeError):
            accepts = False
        if accepts:
            try:
                for _event in stream(model, [], {}, [], keep_alive=0):
                    pass
                return True
            except Exception:
                return False
    return False


def ensure_single_residency(client, target_model, enabled,
                            log=None) -> list:
    """Rule 2: before `target_model` runs, unload every DIFFERENT
    local model Ollama currently holds resident. Returns the list
    of models an unload was requested for. A provider target, the
    OFF profile, or a client with no ps() is a strict no-op — in
    particular ps() is never even called for a provider model.
    `log`, when given, is called once per swap with the plain-words
    line. Fail-soft throughout: residency is a courtesy to the
    machine, never a gate on the generation."""
    if not enabled or client is None:
        return []
    if is_provider_model(target_model):
        return []
    ps = getattr(client, "ps", None)
    if not callable(ps):
        return []
    try:
        resident = ps() or []
    except Exception:
        return []
    unloaded = []
    for entry in resident:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not name or _same_model(name, target_model):
            continue
        if unload_model(client, name):
            unloaded.append(name)
            if log is not None:
                try:
                    log(f"Light machine: unloaded {name} so "
                        f"{target_model} could run")
                except Exception:
                    pass
    return unloaded


def unload_all_models(client):
    """The manual residency reset (owner's tuning list,
    2026-11-11): unload EVERY local model Ollama currently holds
    resident, whether the Light Machine profile is on or off —
    ps() names the resident set (local models only; provider
    models are never in it and never touched), and each is
    unloaded through the keep_alive-0 idiom above.

    Returns (outcome, names): "unloaded" with the models actually
    released; "none" when ps() reports an empty set; "failed" when
    models were resident but none could be released; "unreachable"
    when there is no client, no ps(), or ps() itself raised.
    Never raises — the caller turns the outcome into one plain
    receipt line."""
    if client is None:
        return ("unreachable", [])
    ps = getattr(client, "ps", None)
    if not callable(ps):
        return ("unreachable", [])
    try:
        resident = ps() or []
    except Exception:
        return ("unreachable", [])
    names = [entry.get("name") for entry in resident
             if isinstance(entry, dict) and entry.get("name")]
    if not names:
        return ("none", [])
    done = [name for name in names if unload_model(client, name)]
    if done:
        return ("unloaded", done)
    return ("failed", names)


def machine_busy(snapshot) -> bool:
    """The governor's busy readings, mirroring
    forge/parallel.machine_loaded exactly: CPU saturated
    (cpu_busy) or under a gigabyte of RAM free means busy. No
    snapshot reads as not busy — the governor measures, and an
    absent measurement is not a hold."""
    if not snapshot:
        return False
    try:
        avail = float(snapshot.get("avail_gb") or 99)
    except (TypeError, ValueError):
        avail = 99.0
    return bool(snapshot.get("cpu_busy")) or avail < 1.0


def effective_concurrency(configured, enabled) -> int:
    """Rule 4: the parallel-jobs cap as read at use time — the
    configured value, capped to 1 while the profile is ON. The
    configured value itself is never rewritten."""
    try:
        value = int(configured or 2)
    except (TypeError, ValueError):
        value = 2
    value = max(1, value)
    if enabled:
        return min(value, 1)
    return value


def cap_local_budget(model, resolved, enabled) -> int:
    """Rule 5: a local model's resolved context budget capped at
    LOCAL_CTX_CAP while the profile is ON. Provider models return
    their resolved budget untouched (Groq's TPM cap math included)."""
    try:
        value = int(resolved)
    except (TypeError, ValueError):
        return resolved
    if enabled and not is_provider_model(model):
        return min(value, LOCAL_CTX_CAP)
    return value
