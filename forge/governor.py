"""Resource governor — the getcompute logic as an importable module.

Claims nothing by itself; it MEASURES and reports, and that is all it
does — the owner's law: the governor measures, it never governs. It
never gates, blocks, or refuses a send; the send path does not consult
it. The shell uses its numbers for the header gauges, and the
resource-governor plugin exposes it to the agent as the
check_resources tool. One source of truth for what fits on this
machine (Ryzen 3 3250U, 8 GB RAM, CPU-only).
"""
from __future__ import annotations
import json
import os
import urllib.request

OS_RESERVE_GB = 1.5   # always leave this much RAM for the OS itself
CTX_GB = 0.5          # rough KV-cache headroom at 4K-8K context

MODELS = {
    "minicpm5": {"ollama": "maternion/minicpm5-heretic:2b", "gb": 2.7,
                 "note": "The driver. Best coder of the roster; Q8_0."},
    "qwen-dolphin": {"ollama": "dolphinlabs/qwen3.5-uncensored:latest", "gb": 2.1,
                     "note": "The reviewer. Full-weight Qwen3.5-2B, abliterated; Q8_0."},
    "bonsai2b": {"ollama": "eriktonon/bonsai-2-uncensored:2b", "gb": 1.0,
                 "note": "The fast seat. Bonsai 2 2B itself, abliterated; lightest."},
    "bonsai4b": {"ollama": "bonsai-2:4b", "gb": 1.5,
                 "note": "Original stock default (estimate). Governor/control runs only."},
}


def model_key_for(tag: str) -> str | None:
    """Map an Ollama tag to a roster key, or None if it is not in the roster.

    Sweep R150: matching used to compare only the part before the
    first colon, so any same-repo variant inherited the roster
    entry's size — "bonsai-2:2b" mapped to the 4B entry and a
    "dolphinlabs/qwen3.5-uncensored:7b" would have been judged by
    the 2B reviewer's 2.1 GB. A tag now maps only when it IS the
    roster tag (a missing variant means :latest, Ollama's own
    rule); a different size variant of a roster repo is simply not
    in the roster, and verdict_for treats it as unknown."""
    tag = str(tag or "")
    for key, info in MODELS.items():
        if tag == info["ollama"]:
            return key
    if ":" not in tag:
        for key, info in MODELS.items():
            repo, _, variant = info["ollama"].rpartition(":")
            if tag == repo and variant == "latest":
                return key
    return None


def mem_available_gb() -> float:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
    except Exception:
        pass
    return 0.0


def mem_total_gb() -> float:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024 / 1024
    except Exception:
        pass
    return 0.0


def load_1min() -> float:
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def _ps_models(base_url: str, timeout: float):
    """(reachable, [model dicts]) from Ollama's own /api/ps report —
    the raw rows (name / size bytes / expires_at), unfiltered."""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/ps", timeout=timeout) as r:
            data = json.load(r)
        return True, [m for m in data.get("models", []) if isinstance(m, dict)]
    except Exception:
        return False, []


def ollama_loaded(base_url: str = "http://127.0.0.1:11434", timeout: float = 2.0) -> tuple[bool, list[str]]:
    """(reachable, [loaded model tags]) from Ollama's own /api/ps report."""
    reachable, models = _ps_models(base_url, timeout)
    return reachable, [m.get("name", "?") for m in models]


def snapshot(base_url: str = "http://127.0.0.1:11434", timeout: float = 2.0) -> dict:
    """One measurement of the whole bench: RAM, CPU, Ollama slot."""
    threads = os.cpu_count() or 4
    load = load_1min()
    reachable, loaded_models = _ps_models(base_url, timeout=timeout)
    loaded = [m.get("name", "?") for m in loaded_models]
    return {
        "avail_gb": round(mem_available_gb(), 2),
        "total_gb": round(mem_total_gb(), 2),
        "load_1min": load,
        "threads": threads,
        "cpu_busy": load > threads,
        "ollama_running": reachable,
        "ollama_loaded": loaded,
        # Header loaded-models readout (2026-11-11): each resident
        # model's weight in bytes, from the same /api/ps rows. The
        # name list above stays the contract older readers use;
        # sizes ride alongside. Ollama-only by construction — a
        # provider model never appears here (it isn't in this RAM).
        "ollama_loaded_sizes": {
            m.get("name", "?"): (m.get("size") or 0) for m in loaded_models},
    }


def verdict_for(model_tag: str, snap: dict | None = None,
                base_url: str = "http://127.0.0.1:11434", timeout: float = 2.0) -> dict:
    """Would loading this model right now be safe? Pure function of the snapshot
    plus the roster size table — this is the part the tests pin down."""
    snap = snap or snapshot(base_url, timeout=timeout)
    key = model_key_for(model_tag)
    size = MODELS[key]["gb"] if key else 2.0  # unknown model: assume mid-size
    need = round(size + CTX_GB, 2)
    already = model_tag in snap.get("ollama_loaded", [])
    fits_ram = already or (snap["avail_gb"] - OS_RESERVE_GB) >= need
    fits = fits_ram and not snap["cpu_busy"]
    return {
        "model": model_tag,
        "roster_key": key,
        "size_gb": size,
        "need_gb": need,
        "already_loaded": already,
        "avail_gb": snap["avail_gb"],
        "total_gb": snap["total_gb"],
        "load_1min": snap["load_1min"],
        "threads": snap["threads"],
        "ollama_loaded": snap.get("ollama_loaded", []),
        "fits": fits,
        "fits_ram": fits_ram,
        "cpu_busy": snap["cpu_busy"],
    }


def verdict_text(v: dict) -> str:
    lines = [
        f"RAM: {v['avail_gb']} GB free of {v['total_gb']} GB | need {v['need_gb']} GB "
        f"for {v['model']} + {OS_RESERVE_GB} GB left for the OS",
        f"CPU: load {v['load_1min']} on {v['threads']} threads"
        f"{' — BUSY, wait' if v['cpu_busy'] else ' — OK'}",
        "Ollama loaded: " + (", ".join(v["ollama_loaded"]) or "none"),
    ]
    if v["already_loaded"]:
        lines.append("VERDICT: already loaded — no swap needed, safe to run")
    elif v["fits"]:
        lines.append("VERDICT: fits — safe to run")
    else:
        lines.append("VERDICT: does NOT fit right now — close browser tabs / "
                     "wait for the other run to finish")
    return "\n".join(lines)
