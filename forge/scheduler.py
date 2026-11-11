"""Scheduled runs — Note 17.

Owner-defined jobs that fire on an interval or daily at a set time.
A job is a saved prompt + workspace. When it fires, the Forge starts a
fresh conversation and sends the prompt like the owner typed it.

Approvals during a scheduled run behave exactly like F8 demands: the
dialog PAUSES with no clock and waits — a job that needs the owner
parks until he answers, whether that's in a minute or in the morning.
Jobs never fire while a turn is already running; a due job simply
waits for the bench to be free.
"""
from __future__ import annotations
import time
from datetime import datetime, timedelta


def valid_daily(text) -> bool:
    """Strict "HH:MM" check: two int fields, 0<=HH<=23, 0<=MM<=59.
    Accepts "7:30" and "07:30"; rejects "24:00", "7:60", "730", "",
    "ab:cd" and anything else that is not a real wall-clock time.
    Stored job data can be hand-edited garbage — this never raises."""
    try:
        parts = str(text).strip().split(":")
        if len(parts) != 2:
            return False
        hh, mm = int(parts[0]), int(parts[1])
    except Exception:
        return False
    return 0 <= hh <= 23 and 0 <= mm <= 59


def compute_next(job: dict, now: float | None = None) -> float:
    now = time.time() if now is None else now
    every = job.get("every_minutes")
    if every:
        try:
            every = int(every)
        except Exception:
            every = 0
        if every > 0:
            base = job.get("last_run") or now
            nxt = base + every * 60
            return nxt if nxt > now else now + every * 60
    daily = job.get("daily")
    if daily:
        if not valid_daily(daily):
            return now + 86400
        hh, mm = (int(x) for x in str(daily).split(":"))
        local = datetime.fromtimestamp(now)
        candidate = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate.timestamp() <= now:
            candidate += timedelta(days=1)
        return candidate.timestamp()
    return now + 3600


def ensure_next(job: dict, now: float | None = None) -> dict:
    if not job.get("next_run"):
        job["next_run"] = compute_next(job, now)
    return job


def due_jobs(jobs: list[dict], now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    out = []
    for job in jobs:
        if not job.get("enabled", True):
            continue
        ensure_next(job, now)
        if job["next_run"] <= now:
            out.append(job)
    return out


def mark_run(job: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    job["last_run"] = now
    job["next_run"] = compute_next(job, now)
    return job
