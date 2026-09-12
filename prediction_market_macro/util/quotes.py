"""Freshness policy shared by live marks and exits (timestamps are UTC instants)."""
from __future__ import annotations

from datetime import datetime

from prediction_market_macro.strategy.edge import two_sided

# Fifteen-minute position maintenance plus five minutes of scheduling/network margin.
MAX_QUOTE_AGE_SECONDS = 20 * 60


def quote_age_seconds(row, now: datetime) -> float | None:
    if row is None:
        return None
    try:
        ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        return (now - ts).total_seconds()
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def quote_is_fresh(row, now: datetime, max_age_seconds=MAX_QUOTE_AGE_SECONDS) -> bool:
    age = quote_age_seconds(row, now)
    return age is not None and 0 <= age <= max_age_seconds


def quote_status(row, now: datetime, max_age_seconds=MAX_QUOTE_AGE_SECONDS) -> str:
    if row is None:
        return "missing"
    if not quote_is_fresh(row, now, max_age_seconds):
        return "stale"
    return "marked" if two_sided(row["yes_bid"], row["yes_ask"]) else "illiquid"
