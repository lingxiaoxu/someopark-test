"""24/7 clock utilities (Plan 00). No exchange calendar, no holidays.

Funding cycle (probe-verified 2026-07-07): every 8h at 04:00 / 12:00 / 20:00 UTC.
All timestamps tz-aware UTC. Annualization elsewhere uses 365 days.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc
FUNDING_HOURS_UTC = (4, 12, 20)
FUNDING_INTERVAL = timedelta(hours=8)


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_utc(ts: float | int, *, ms: bool = False) -> datetime:
    """Epoch seconds (or ms — margin WS uses `_ms` fields) → aware UTC datetime."""
    return datetime.fromtimestamp(ts / 1000.0 if ms else ts, UTC)


def next_funding_time(now: datetime | None = None) -> datetime:
    """Next funding settlement strictly after ``now``."""
    now = now or utcnow()
    if now.tzinfo is None:
        raise ValueError("naive datetime — all crypto_trading timestamps are tz-aware UTC")
    now = now.astimezone(UTC)
    base = now.replace(minute=0, second=0, microsecond=0)
    for add_days in (0, 1):
        for h in FUNDING_HOURS_UTC:
            candidate = (base + timedelta(days=add_days)).replace(hour=h)
            if candidate > now:
                return candidate
    raise AssertionError("unreachable")


def prev_funding_time(now: datetime | None = None) -> datetime:
    """Most recent funding settlement at or before ``now``."""
    return next_funding_time(now or utcnow()) - FUNDING_INTERVAL


def utc_day(now: datetime | None = None) -> str:
    """YYYY-MM-DD label of the current UTC day (daily file rotation key)."""
    return (now or utcnow()).astimezone(UTC).strftime("%Y-%m-%d")
