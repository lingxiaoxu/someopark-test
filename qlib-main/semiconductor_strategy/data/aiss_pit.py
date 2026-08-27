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


def merge_frozen(existing: dict, fresh: dict) -> dict:
    """Append-only PIT merge for {iso_date: value} signal stores.

    Keeps every existing (already-recorded) value immutable and appends ONLY
    fresh dates strictly newer than the latest existing date.  This freezes the
    history so re-fetching / recomputing a price-derived or revised signal
    (capex pulse, DRAM proxy, FRED macro) never alters past values — making the
    backtest reproducible across data refreshes.  Pass an empty ``existing`` on
    the first run to seed the frozen baseline with the full fresh series.
    """
    if not existing:
        return dict(fresh)
    last = max(existing.keys())          # ISO dates sort lexicographically
    merged = dict(existing)
    for d, v in fresh.items():
        if d > last:
            merged[d] = v
    return merged


# ---------------------------------------------------------------------------
# Staleness (freshness) check
# ---------------------------------------------------------------------------
# 2026-08-27: 加这一节的原因 —— `verify()` 原本只查**有没有**(`if len(x)`),不查
# **新不新**,于是 capex_pulse / dram_proxy 从 2026-06-04 冻到 08-27(57 个交易日、
# composite 0.70 权重的输入)期间,每周六的 weekly 体检照常打印
#     capex_pulse : 2558 pts 2016-04-04→2026-06-04 last z=+0.43
#     RESULT: OK
# 一次都没报警。根因是没人调 `semiconductor_pipeline.sh update_data`,但**真正致命
# 的是检测器跑着却永远说正常** —— 与 VolumePrediction 2026-08 的 RNN 冻结事故同一
# 病理。阈值按各序列的真实节奏给,不是一刀切:太松=白装,太紧=天天喊狼没人再看。

# 日频序列的容忍度按**交易日**算(周末+假期本就没有新点,按自然日会误报)。
STALE_TRADING_DAYS = 5      # 日频: >5 个交易日没新点
STALE_MONTHLY_DAYS = 45     # 月频: 月度数据发布滞后通常 15-20 天,45 天 = 漏了一整期
STALE_QUARTERLY_DAYS = 120  # 季频: 季度 + 申报滞后约 90+30


def _trading_days_between(start, end) -> int:
    """NYSE 交易日数(不含 start 当天)。日历不可用时退化为 5/7 折算的自然日估计。"""
    try:
        import pandas_market_calendars as mcal
        sched = mcal.get_calendar("NYSE").schedule(
            start_date=pd.Timestamp(start) + pd.Timedelta(days=1), end_date=pd.Timestamp(end))
        return int(len(sched))
    except Exception:  # noqa: BLE001 — 体检项绝不因日历缺失而失效
        return int((pd.Timestamp(end) - pd.Timestamp(start)).days * 5 / 7)


def staleness(last_date, cadence: str, as_of=None) -> tuple:
    """→ (age, unit, is_stale, limit)。``cadence`` ∈ {daily, monthly, quarterly}。

    ``last_date`` 传**数据本身的最新日期**(日频=最后一个数据点;月频/季频=最新一期
    的 release/filed 日,即真正可用的那天 —— 不是 period_end,否则季度数据天生就"晚"
    90 天,阈值全废)。``as_of`` 只为测试注入,生产恒为今天。
    """
    as_of = pd.Timestamp(as_of) if as_of else pd.Timestamp(date.today())
    last = pd.Timestamp(str(last_date)[:10])
    if cadence == "daily":
        # 只查一次日历: 两次调用不只是浪费(每次都要建 NYSE schedule),还让"报告出来
        # 的 age"与"用来判定的 age"来自两次独立计算 —— 没理由留这种可以不一致的缝。
        age = _trading_days_between(last, as_of)
        return age, "交易日", age > STALE_TRADING_DAYS, STALE_TRADING_DAYS
    if cadence == "monthly":
        limit = STALE_MONTHLY_DAYS
    elif cadence == "quarterly":
        limit = STALE_QUARTERLY_DAYS
    else:
        # 故意**不**兜底成某个默认阈值: 未知 cadence 只可能是调用方写错了字符串,
        # 而静默退化成最松的季频阈值 = 这条序列从此永不报警 —— 正是本模块要根治
        # 的失效模式(检测器跑着却永远说正常)。宁可让 --verify 当场炸。
        raise ValueError(f"未知 cadence: {cadence!r} (只接受 daily/monthly/quarterly)")
    age = int((as_of - last).days)
    return age, "天", age > limit, limit


def stale_tag(last_date, cadence: str, as_of=None) -> str:
    """给 verify 输出用的后缀: 新鲜返回 '',过期返回 '  ← STALE (…)'。"""
    age, unit, is_stale, limit = staleness(last_date, cadence, as_of)
    return "" if not is_stale else f"  ← STALE ({age}{unit} > {limit}{unit})"


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
