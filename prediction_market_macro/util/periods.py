"""util/periods.py — Kalshi event period token <-> internal period key.

Kalshi event tickers: KXJOBLESSCLAIMS-26JUL30 (release date), KXCPI-26JUL (ref month),
KXFEDDECISION-26SEP (meeting month). Internal keys: ISO "2026-07-30" / "2026-07" / "2026-09".
"""
from __future__ import annotations

import re

_MON = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def kalshi_period_to_key(token: str) -> str | None:
    """'26JUL30' -> '2026-07-30'; '26JUL' -> '2026-07'; unparseable -> None."""
    m = re.fullmatch(r"(\d{2})([A-Z]{3})(\d{2})?(\d{2})?", token.strip().upper())
    if not m or m.group(2) not in _MON:
        return None
    yy, mon, dd, _hh = m.groups()
    y, mo = 2000 + int(yy), _MON[mon]
    return f"{y:04d}-{mo:02d}-{int(dd):02d}" if dd else f"{y:04d}-{mo:02d}"


def key_matches_calendar(series_calendar_period: str, key: str) -> bool:
    return series_calendar_period == key
