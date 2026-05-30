"""
AISS point-in-time (PIT) + JSON storage helpers
===============================================
Shared utilities for the AISS data layer.  The single most dangerous source of
look-ahead bias in this strategy is slow (quarterly/monthly) external data, so
every slow signal is stored with an explicit *availability date* (release /
filing / acceptance date) and read back PIT-correctly:

    a data point is usable on ``as_of_date`` only if availability_date <= as_of_date

Storage convention (all under price_data/semi_strategy/{industry,company}/)
--------------------------------------------------------------------------
JSON keyed by period (e.g. "2026-04" or "2026Q1") whose value dict always
contains a PIT date field (``release_date`` / ``filing_date`` / ``filed_date``).
``pit_series`` converts such a JSON into a pandas Series indexed by the
availability date, which the caller forward-fills onto its trading calendar.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

# Project root: data -> semiconductor_strategy -> qlib-main -> someopark-test
_PROJECT_DIR = Path(__file__).resolve().parents[3]
SEMI_DATA_DIR = _PROJECT_DIR / "price_data" / "semi_strategy"
INDUSTRY_DIR = SEMI_DATA_DIR / "industry"
COMPANY_DIR = SEMI_DATA_DIR / "company"
PRICES_DIR = SEMI_DATA_DIR / "prices"
CACHE_DIR = SEMI_DATA_DIR / "cache"


def ensure_dirs() -> None:
    for d in (INDUSTRY_DIR, COMPANY_DIR, PRICES_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# JSON IO (atomic write so a crashed run never leaves a half-written file)
# ---------------------------------------------------------------------------

def load_json(path: Path, default=None) -> dict:
    if Path(path).exists():
        try:
            return json.loads(Path(path).read_text())
        except Exception:
            return default if default is not None else {}
    return default if default is not None else {}


def save_json(path: Path, obj: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, sort_keys=True, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------------------
# Taiwan ROC date conversion (TWSE returns ROC-year dates)
# ---------------------------------------------------------------------------

def roc_yyyymmdd_to_iso(s: str) -> Optional[str]:
    """'1150517' -> '2026-05-17'  (ROC year = Gregorian - 1911)."""
    if not s:
        return None
    s = str(s).strip()
    # ROC date is 6 or 7 chars: YYY?MMDD
    if len(s) not in (6, 7):
        return None
    mm = s[-4:-2]
    dd = s[-2:]
    yyy = s[:-4]
    try:
        greg = int(yyy) + 1911
        return f"{greg:04d}-{int(mm):02d}-{int(dd):02d}"
    except Exception:
        return None


def roc_yyyymm_to_iso_month(s: str) -> Optional[str]:
    """'11504' -> '2026-04'  (ROC year+month)."""
    if not s:
        return None
    s = str(s).strip()
    if len(s) not in (5, 6):
        return None
    mm = s[-2:]
    yyy = s[:-2]
    try:
        greg = int(yyy) + 1911
        return f"{greg:04d}-{int(mm):02d}"
    except Exception:
        return None


# ---------------------------------------------------------------------------
# PIT lookups
# ---------------------------------------------------------------------------

def get_latest_available(
    data: Dict[str, dict], as_of_date, date_field: str,
) -> Optional[dict]:
    """Return the most recent record whose ``date_field`` <= ``as_of_date``.

    Never uses period-end dates for availability — only the explicit PIT field.
    """
    as_of = pd.Timestamp(as_of_date).date() if not isinstance(as_of_date, date) else as_of_date
    available = {}
    for k, v in data.items():
        df = v.get(date_field)
        if not df:
            continue
        try:
            d = date.fromisoformat(str(df)[:10])
        except Exception:
            continue
        if d <= as_of:
            available[k] = (d, v)
    if not available:
        return None
    best = max(available.values(), key=lambda t: t[0])
    return best[1]


def pit_series(
    data: Dict[str, dict],
    value_field: str,
    date_field: str,
) -> pd.Series:
    """Build a Series indexed by *availability date* -> value.

    Forward-fill this onto a trading calendar to obtain a PIT-correct daily
    series:  ``pit_series(...).reindex(trading_days, method='ffill')`` (or
    ``.reindex(union).ffill().reindex(trading_days)``).  A value first becomes
    visible on its availability date and persists until superseded.
    """
    rows: List[tuple] = []
    for v in data.values():
        d = v.get(date_field)
        val = v.get(value_field)
        if d is None or val is None:
            continue
        try:
            ts = pd.Timestamp(str(d)[:10])
        except Exception:
            continue
        rows.append((ts, float(val)))
    if not rows:
        return pd.Series(dtype="float64")
    s = pd.Series({ts: val for ts, val in rows}).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return s


def reindex_pit_daily(
    avail_series: pd.Series,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> pd.Series:
    """Forward-fill an availability-indexed series onto a daily calendar."""
    if avail_series.empty:
        return avail_series
    start_ts = pd.Timestamp(start) if start else avail_series.index[0]
    end_ts = pd.Timestamp(end) if end else pd.Timestamp(date.today())
    idx = pd.date_range(start=min(start_ts, avail_series.index[0]), end=end_ts, freq="D")
    daily = avail_series.reindex(idx).ffill()
    if start:
        daily = daily.loc[pd.Timestamp(start):]
    return daily
