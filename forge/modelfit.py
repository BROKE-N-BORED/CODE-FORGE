"""Model fit + provenance — pure math over (size, context, RAM).

H4 (GUIFX Research Brief #2, 2026-11-09): the LM Studio lesson —
a model list that does not say whether the model fits THIS PC is
only half a list. This module answers three questions for any
local model, deterministically and with no system reads inside
the math:

  * estimate_need_bytes(size, ctx)  — what loading it would take
  * classify_fit(size, ctx, ram)    — "fits" / "tight" / "too_large"
  * comfortable_max_size_bytes(ram, ctx) — the biggest model that
    still classifies "fits" (the models UI's header line)

plus the provenance derivations (publisher_for, short_digest)
and the one system read the UI needs, total_ram_bytes().

Deliberately UI-free: no Qt imports, no window state. A future
surface (e.g. a model scout weighing a candidate it found) calls
the same functions the models window's badges call:
classify_fit() for the verdict and estimate_need_bytes() /
format_gb() for the number it would show the owner.

RAM is always PASSED IN to the math. total_ram_bytes() is the
single patch point for tests and the single caller of the app's
existing /proc/meminfo reader (forge.governor.mem_total_gb —
wrapped, not duplicated).
"""
from __future__ import annotations

from . import governor as governor_mod

# --- documented constants -------------------------------------------
# Weights on disk are memory-mapped; the runtime still needs slack
# for the tokenizer, compute buffers and fragmentation. 5%, done in
# integer math (size // 20) so estimates are exact and testable.
OVERHEAD_DIVISOR = 20            # need += size // 20  (5%)

# KV cache allowance: ~80 MiB of fp16 KV per 1K context tokens for
# this class of small local models (2-4B, grouped-query attention).
# At Forge's standard 8192 context that is 640 MiB — deliberately a
# touch more conservative than the governor's rough 0.5 GB figure.
KV_BYTES_PER_1K_CTX = 80 * 1024 * 1024

# Reserves that define the classes. OS_RESERVE is the governor's
# own number (forge/governor.py: "always leave this much RAM for
# the OS itself") — one reserve, two readers. APP_RESERVE is the
# headroom for Forge's own UI, the Ollama daemon and the desktop.
OS_RESERVE_BYTES = int(1.5 * 1024 ** 3)
APP_RESERVE_BYTES = int(1.0 * 1024 ** 3)
RESERVE_BYTES = OS_RESERVE_BYTES + APP_RESERVE_BYTES

FITS = "fits"
TIGHT = "tight"
TOO_LARGE = "too_large"
UNKNOWN = "unknown"

FIT_LABELS = {FITS: "Fits", TIGHT: "Tight", TOO_LARGE: "Too large"}


def estimate_need_bytes(size_bytes, ctx_tokens) -> int:
    """Bytes loading this model at this context would take:
    the weights, +5% runtime overhead, +the KV cache allowance.
    Pure integer math — same inputs, same answer, always."""
    size = max(0, int(size_bytes or 0))
    ctx = max(0, int(ctx_tokens or 0))
    kv = (ctx * KV_BYTES_PER_1K_CTX) // 1024
    return size + size // OVERHEAD_DIVISOR + kv


def classify_fit(size_bytes, ctx_tokens, total_ram_bytes):
    """(klass, need_bytes). The class rules, in spirit and letter:

      fits      — the need leaves OS_RESERVE + APP_RESERVE free;
      tight     — it runs, but eats into that reserve;
      too_large — the need exceeds the machine's total RAM.

    Unknown inputs (no size, or RAM unreadable) classify UNKNOWN
    with the need still computed when a size exists — a badge that
    guesses would be worse than no badge."""
    need = estimate_need_bytes(size_bytes, ctx_tokens)
    total = int(total_ram_bytes or 0)
    if not size_bytes or total <= 0:
        return UNKNOWN, need
    if need > total:
        return TOO_LARGE, need
    if need <= total - RESERVE_BYTES:
        return FITS, need
    return TIGHT, need


def comfortable_max_size_bytes(total_ram_bytes, ctx_tokens) -> int:
    """The largest on-disk model size that still classifies FITS
    at this context on this machine — the header line's "models up
    to ≈X GB fit comfortably". Exact under the integer overhead:
    solve, then step to the true edge (the //20 makes the need a
    staircase, so the closed-form answer can sit one step off)."""
    total = int(total_ram_bytes or 0)
    if total <= 0:
        return 0
    budget = total - RESERVE_BYTES - (max(0, int(ctx_tokens or 0))
                                      * KV_BYTES_PER_1K_CTX) // 1024
    if budget <= 0:
        return 0
    size = (budget * OVERHEAD_DIVISOR) // (OVERHEAD_DIVISOR + 1)
    while estimate_need_bytes(size + 1, ctx_tokens) <= total - RESERVE_BYTES:
        size += 1
    while size > 0 and estimate_need_bytes(size, ctx_tokens) > total - RESERVE_BYTES:
        size -= 1
    return size


def format_gb(num_bytes) -> str:
    """One-decimal GB for MODEL sizes — the app's existing display
    convention (the resident-in-RAM log line divides by 1e9)."""
    return f"{(int(num_bytes or 0)) / 1_000_000_000:.1f}"


def format_ram_gb(total_ram_bytes) -> str:
    """One-decimal GB for the machine's RAM — the governor's
    convention (MemTotal KiB / 1024 / 1024, i.e. GiB labelled GB,
    the way the header gauges already speak)."""
    return f"{(int(total_ram_bytes or 0)) / 1024 ** 3:.1f}"


def publisher_for(tag) -> str:
    """Provenance the tag itself carries: a namespaced tag
    ("maternion/minicpm5-heretic:2b") names its publisher before
    the slash; a plain name ("llama3.2:3b") is the Ollama library."""
    name = str(tag or "").strip()
    if "/" in name:
        publisher = name.split("/", 1)[0].strip()
        if publisher:
            return publisher
    return "Ollama library"


def short_digest(digest) -> str:
    """The first 12 characters of an Ollama digest, ellipsised —
    enough to tell two builds of the same tag apart at a glance.
    "" when the data source carried no digest."""
    text = str(digest or "").strip()
    if not text:
        return ""
    return text[:12] + "…" if len(text) > 12 else text


def total_ram_bytes() -> int:
    """This machine's total RAM in bytes, via the app's existing
    /proc/meminfo reader (governor.mem_total_gb). 0 when unreadable.
    The UI calls this at open/refresh only — never on a timer."""
    try:
        return int(governor_mod.mem_total_gb() * 1024 ** 3)
    except Exception:
        return 0
