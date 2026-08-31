"""
AEUS alternative-data layer
===========================
Backfillable alt-data signals (FRED + EIA + STEO) + a forward-only GPU-pricing
pipeline.  Stored under ``price_data/elec_strategy/altdata/`` — purely additive,
never touches existing data.  Every series here is consumed by ``supply_chain``
as a *confirmation tilt* or by the engine as the exposure amplifier (missing →
graceful 0 / 1.0), per AEUS_PLAN §4.1/§4.2.

Signal map (AEUS_PLAN §4.1; endpoints/IDs verified live 2026-08-30):

  A1  power demand, daily by region   EIA v2 ``electricity/rto/daily-region-data``
        respondents US48 / TEX / MIDA / CAL, type=D; history 2015-07+; PIT +3d.
        → structural (weather-adjusted) demand YoY z = power_demand_proxy node.
  A3② DC-state retail price premium   EIA ``electricity/retail-sales``
        mean(VA GA TX AZ OH price) − US price, monthly, EPM lag; z36
        → regional_utility confirmation tilt.
  A4  installed capacity (EIA-860M)   EIA ``electricity/operating-generator-capacity``
        plant rows aggregated to monthly US totals by energy source; PIT +60d.
        → capacity YoY; solar+wind adds → renewables_storage confirmation;
        shortage score = z(demand YoY − capacity YoY) → exposure amplifier.
  A5  degree days (CDD/HDD)           EIA ``steo`` seriesId ZWCDPUS / ZWHDPUS
        (monthly, includes forecasts) → weather-adjustment for A1.
  A8  transformer PPI                 FRED ``PCU335311335311`` (1967+) — the
        transformer-bottleneck price thermometer → grid_equipment tilt.
      CPI electricity                 FRED ``CUSR0000SEHF01`` → power-price pulse
        second source (with EIA retail price).
      construction hiring             FRED ``IHLIDXUSTPCONS`` (Indeed, daily,
        2020-02+) → grid_epc labor pulse.
  N8  GPU cloud pricing               ComputePrices.com — FORWARD-ONLY snapshot
        (AI demand proxy, kept from AISS; not a backtest factor).

PIT: FRED observation dates are shifted by conservative publication lags
(monthly +45d, weekly/daily +7d) before daily ffill; EIA monthly uses the EPM
lag (+56d), 860M +60d, daily demand +3d.  All backfillable stores are
append-only frozen (``merge_frozen``) so backtests stay reproducible.

CLI
---
    python -m electric_utilities_strategy.data.altdata_signals --init-fred
    python -m electric_utilities_strategy.data.altdata_signals --update-fred
    python -m electric_utilities_strategy.data.altdata_signals --update-demand
    python -m electric_utilities_strategy.data.altdata_signals --update-dd
    python -m electric_utilities_strategy.data.altdata_signals --update-capacity
    python -m electric_utilities_strategy.data.altdata_signals --update-state-price
    python -m electric_utilities_strategy.data.altdata_signals --snapshot-gpu
    python -m electric_utilities_strategy.data.altdata_signals --verify
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_QLIB_DIR = _THIS_DIR.parents[1]
_PROJECT_DIR = _THIS_DIR.parents[2]
for _p in (str(_QLIB_DIR), str(_PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from electric_utilities_strategy.data import aeus_pit as pit
except Exception:  # pragma: no cover
    import aeus_pit as pit  # type: ignore

log = logging.getLogger("aeus.altdata")

ALTDATA_DIR = pit.SEMI_DATA_DIR / "altdata"
FRED_ALTDATA_PATH = ALTDATA_DIR / "fred_altdata.json"
DEMAND_PATH = ALTDATA_DIR / "eia_demand_daily.json"
DD_PATH = ALTDATA_DIR / "steo_degree_days.json"
CAPACITY_PATH = ALTDATA_DIR / "eia_capacity_monthly.json"
STATE_PRICE_PATH = ALTDATA_DIR / "eia_retail_state_price.json"
GPU_HISTORY_PATH = ALTDATA_DIR / "gpu_pricing_history.parquet"

# --- FRED series (IDs verified live 2026-08-30) ----------------------------
# name -> (series_id, frequency)  freq in {"M","W"} drives the PIT lag
FRED_HIRING: Dict[str, tuple] = {
    "hiring_construction": ("IHLIDXUSTPCONS", "W"),   # grid_epc labor pulse (daily series, weekly lag class)
}
FRED_ELEC: Dict[str, tuple] = {
    "transformer_ppi":  ("PCU335311335311", "M"),  # power/distribution transformer mfg PPI (1967+)
    "cpi_electricity":  ("CUSR0000SEHF01",  "M"),  # CPI: electricity (1952+)
}
ALL_FRED = {**FRED_HIRING, **FRED_ELEC}

# Conservative publication lag (days) by frequency → PIT availability shift
LAG_DAYS = {"M": 45, "W": 7}
FRED_HISTORY_START = "2006-01-01"
_TS_Z_WINDOW = 36                   # months for z-scores
_TS_Z_MIN = 12

# --- EIA v2 ----------------------------------------------------------------
EIA_BASE = "https://api.eia.gov/v2"
DEMAND_RESPONDENTS = ["US48", "TEX", "MIDA", "CAL"]   # lower-48 + DC hotspots
DEMAND_START = "2015-07-01"                            # series inception
DEMAND_LAG_DAYS = 3
DD_SERIES = {"cdd": "ZWCDPUS", "hdd": "ZWHDPUS"}       # STEO population-weighted
CAPACITY_START = "2019-01"
CAPACITY_LAG_DAYS = 60
DC_STATES = ["VA", "GA", "TX", "AZ", "OH"]             # datacenter-heavy states
EPM_LAG_DAYS = 56

# --- GPU pricing (forward-only) --------------------------------------------
GPU_API = "https://computeprices.com/api/v1/gpu-prices"
GPU_MODELS = ("H100", "H200", "B200", "B100")


# ===========================================================================
# Shared fetch helpers
# ===========================================================================

def _fetch_fred(series_id: str, start: str) -> pd.Series:
    """Observation-date-indexed FRED series (reuses fredapi like loader.py)."""
    from fredapi import Fred
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise ValueError("FRED_API_KEY not set")
    s = Fred(api_key=key).get_series(series_id, observation_start=start)
    s.index = pd.to_datetime(s.index).normalize()
    return s.dropna()


def _eia_get(route: str, params: dict) -> list:
    """Paged GET against EIA v2 (pattern from prediction_market_macro/ingest/eia.py)."""
    key = os.environ.get("EIA_API_KEY")
    if not key:
        log.warning("EIA_API_KEY not set; %s cannot be updated", route)
        return []
    rows: list = []
    offset = 0
    while True:
        q = dict(params)
        q.update({"api_key": key, "length": 5000, "offset": offset})
        url = f"{EIA_BASE}/{route}/data/?" + urllib.parse.urlencode(q, doseq=True)
        req = urllib.request.Request(url, headers={"User-Agent": "someopark-aeus/1.0"})
        with urllib.request.urlopen(req, timeout=90) as r:
            payload = json.loads(r.read())
        page = payload.get("response", {}).get("data", [])
        rows.extend(page)
        if len(page) < 5000:
            return rows
        offset += 5000


# ===========================================================================
# FRED fetch / persist  (machinery inherited from the AISS lineage)
# ===========================================================================

def update_fred_altdata(start: str = FRED_HISTORY_START) -> int:
    """Fetch all FRED alt-data series and persist raw observations + meta."""
    ALTDATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"meta": {"updated_at": date.today().isoformat(),
                        "lag_days": LAG_DAYS, "z_window": _TS_Z_WINDOW}, "series": {}}
    import time
    n_ok = 0
    for name, (sid, freq) in ALL_FRED.items():
        s = None
        for attempt in range(4):                 # retry w/ backoff on FRED 429
            try:
                s = _fetch_fred(sid, start)
                break
            except Exception as e:  # noqa: BLE001
                if "Too Many Requests" in str(e) or "Rate Limit" in str(e):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                log.warning("FRED %s (%s) failed: %s", sid, name, e)
                break
        if s is None:
            log.warning("FRED %s (%s) failed after retries", sid, name)
            continue
        time.sleep(0.7)
        payload["series"][name] = {
            "series_id": sid, "freq": freq, "n": int(len(s)),
            "first": s.index[0].date().isoformat() if len(s) else None,
            "last": s.index[-1].date().isoformat() if len(s) else None,
            "obs": {d.date().isoformat(): float(v) for d, v in s.items()},
        }
        n_ok += 1
        log.info("FRED %-20s %-18s %4d obs %s→%s", name, sid, len(s),
                 s.index[0].date() if len(s) else "-", s.index[-1].date() if len(s) else "-")
    pit.save_json(FRED_ALTDATA_PATH, payload)
    log.info("FRED alt-data: %d/%d series saved", n_ok, len(ALL_FRED))
    return n_ok


def _raw_fred(name: str) -> pd.Series:
    payload = pit.load_json(FRED_ALTDATA_PATH, default={})
    node = payload.get("series", {}).get(name)
    if not node or not node.get("obs"):
        return pd.Series(dtype="float64", name=name)
    s = pd.Series({pd.Timestamp(k): float(v) for k, v in node["obs"].items()}).sort_index()
    s.name = name
    return s


def _pit_daily(name: str, start: Optional[str], end: Optional[str]) -> pd.Series:
    """Observation series shifted forward by the publication lag, ffilled daily."""
    s = _raw_fred(name)
    if s.empty:
        return s
    _, freq = ALL_FRED.get(name, (None, "M"))
    lag = LAG_DAYS.get(freq, 45)
    s = s.copy()
    s.index = s.index + pd.Timedelta(days=lag)          # availability = obs + lag
    s = s[~s.index.duplicated(keep="last")]
    end_ts = pd.Timestamp(end) if end else pd.Timestamp(date.today())
    idx = pd.date_range(start=s.index[0], end=end_ts, freq="D")
    daily = s.reindex(idx).ffill()
    if start:
        daily = daily.loc[pd.Timestamp(start):]
    daily.name = name
    return daily


def _ts_z(s: pd.Series, window_months: int = _TS_Z_WINDOW) -> pd.Series:
    """Z-score a daily series on a ~monthly cadence (window in months → ~21d each)."""
    if s.empty:
        return s
    w = window_months * 21
    mu = s.rolling(w, min_periods=_TS_Z_MIN * 21).mean()
    sd = s.rolling(w, min_periods=_TS_Z_MIN * 21).std().replace(0, np.nan)
    return ((s - mu) / sd).dropna()


def _yoy_z(name: str, start: Optional[str], end: Optional[str], periods: int = 252) -> pd.Series:
    s = _pit_daily(name, None, end)
    if s.empty:
        return s
    yoy = s.pct_change(periods)
    z = _ts_z(yoy.dropna())
    z.name = name
    if start:
        z = z.loc[pd.Timestamp(start):]
    return z


# ── FRED public loaders ─────────────────────────────────────────────────────

def load_transformer_ppi_yoy(start=None, end=None) -> pd.Series:
    """Transformer PPI YoY z — the bottleneck price thermometer (grid_equipment)."""
    return _yoy_z("transformer_ppi", start, end)


def load_cpi_electricity_yoy(start=None, end=None) -> pd.Series:
    return _yoy_z("cpi_electricity", start, end)


def load_construction_hiring(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """Construction hiring pulse: 6m change of Indeed postings, z (grid_epc labor)."""
    s = _pit_daily("hiring_construction", None, end)
    if s.empty:
        return pd.Series(dtype="float64", name="construction_hiring")
    chg = s - s.shift(126)             # ~6-month change
    z = _ts_z(chg.dropna())
    z.name = "construction_hiring"
    if start:
        z = z.loc[pd.Timestamp(start):]
    return z


# ===========================================================================
# A1: Daily power demand by region (EIA rto/daily-region-data)
# ===========================================================================

def update_demand(refreeze: bool = False) -> int:
    """US48 + DC-hotspot daily demand (MWh), frozen append-only, PIT +3d."""
    pit.ensure_dirs(); ALTDATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = {} if refreeze else pit.load_json(DEMAND_PATH, default={}).get("records", {})
    # incremental: refetch from 30d before the last stored date
    start = DEMAND_START
    if existing:
        start = (pd.Timestamp(max(existing)) - pd.Timedelta(days=30)).date().isoformat()
    rows = _eia_get("electricity/rto/daily-region-data", {
        "frequency": "daily", "data[0]": "value",
        **{"facets[respondent][]": DEMAND_RESPONDENTS},
        "facets[type][]": "D", "facets[timezone][]": "Eastern",
        "start": start,
        "sort[0][column]": "period", "sort[0][direction]": "asc",
    })
    if not rows:
        log.error("demand: EIA returned no rows")
        return 0
    fresh: dict = {}
    for r in rows:
        per, resp, val = r.get("period"), r.get("respondent"), r.get("value")
        if not per or resp not in DEMAND_RESPONDENTS or val is None:
            continue
        fresh.setdefault(per, {"period": per})[f"demand_{resp.lower()}_mwh"] = float(val)
    records = pit.merge_frozen(existing, fresh)
    payload = {"meta": {"source": "EIA v2 rto/daily-region-data type=D",
                        "respondents": DEMAND_RESPONDENTS, "lag_days": DEMAND_LAG_DAYS,
                        "updated_at": date.today().isoformat(),
                        "frozen_append_only": True, "n": len(records)},
               "records": records}
    pit.save_json(DEMAND_PATH, payload)
    last = max(records)
    log.info("demand: %d days, last %s US48=%.0f MWh", len(records), last,
             records[last].get("demand_us48_mwh", 0))
    return len(records)


def _demand_daily(respondent: str = "us48") -> pd.Series:
    records = pit.load_json(DEMAND_PATH, default={}).get("records", {})
    field = f"demand_{respondent}_mwh"
    s = pd.Series({pd.Timestamp(k): v[field] for k, v in records.items()
                   if v.get(field) is not None}).sort_index()
    s.name = field
    return s


# ===========================================================================
# A5: STEO degree days (CDD/HDD, monthly, includes forecasts)
# ===========================================================================

def update_degree_days(refreeze: bool = False) -> int:
    pit.ensure_dirs(); ALTDATA_DIR.mkdir(parents=True, exist_ok=True)
    fresh: dict = {}
    for name, sid in DD_SERIES.items():
        rows = _eia_get("steo", {
            "frequency": "monthly", "data[0]": "value",
            "facets[seriesId][]": sid, "start": "2005-01",
            "sort[0][column]": "period", "sort[0][direction]": "asc",
        })
        for r in rows:
            per, val = r.get("period"), r.get("value")
            if per and val is not None:
                fresh.setdefault(per, {"period": per})[name] = float(val)
    if not fresh:
        log.error("degree days: EIA STEO returned no rows")
        return 0
    # STEO 当月发布含未来月预测;历史月为实测。冻结历史,未来月随 vintage 覆写:
    # merge_frozen 只追加"更新的日期",这里按 period 冻结即可(过去月不再变)。
    today_per = date.today().strftime("%Y-%m")
    existing = {} if refreeze else pit.load_json(DD_PATH, default={}).get("records", {})
    frozen_hist = {k: v for k, v in existing.items() if k < today_per}
    records = dict(sorted({**fresh, **frozen_hist}.items()))
    payload = {"meta": {"source": "EIA STEO ZWCDPUS/ZWHDPUS (incl. forecasts)",
                        "updated_at": date.today().isoformat(), "n": len(records)},
               "records": records}
    pit.save_json(DD_PATH, payload)
    log.info("degree days: %d months (through %s, incl. forecasts)",
             len(records), max(records))
    return len(records)


def _dd_monthly() -> pd.DataFrame:
    records = pit.load_json(DD_PATH, default={}).get("records", {})
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame.from_dict(records, orient="index")
    df.index = pd.PeriodIndex(df.index, freq="M")
    return df.sort_index()[["cdd", "hdd"]].astype(float)


def load_power_demand_structural(start: Optional[str] = None,
                                 end: Optional[str] = None,
                                 respondent: str = "us48") -> pd.Series:
    """Weather-adjusted structural demand YoY z (AEUS_PLAN §4.2 A1×A5).

    结构性需求 = 月度需求 YoY − f(ΔCDD, ΔHDD);f 用截至 as-of 的滚动 60 个月
    OLS(扩张窗,PIT 干净)。残差再 z36 —— 把天气解释掉的用电涨剥掉,剩下的
    (数据中心负荷)才是信号本体。度日或需求缺失 → 退化为原始 YoY z(响亮标注)。
    """
    d = _demand_daily(respondent)
    if d.empty:
        return pd.Series(dtype="float64", name="power_demand_structural")
    m = d.resample("ME").sum(min_count=20)
    # 当月未走完 → 月和天然偏低 → YoY 假暴跌(实测 -6.6z)。只保留已收官的月份。
    if len(m) and m.index[-1] >= pd.Timestamp(date.today()).normalize():
        m = m.iloc[:-1]
    n_days = d.resample("ME").count()
    m = m[n_days.reindex(m.index) >= 25]      # 缺日过多的月份一并剔除
    yoy = (m / m.shift(12) - 1.0) * 100.0
    dd = _dd_monthly()
    resid = yoy.copy()
    if not dd.empty:
        dd.index = dd.index.to_timestamp("M")
        cdd_d = dd["cdd"] - dd["cdd"].shift(12)
        hdd_d = dd["hdd"] - dd["hdd"].shift(12)
        X = pd.concat([cdd_d, hdd_d], axis=1).reindex(yoy.index)
        # expanding-window OLS, min 36 obs, refit each month on data ≤ t
        fitted = pd.Series(index=yoy.index, dtype=float)
        ys, xs = yoy.dropna(), X.dropna()
        xs.columns = ["cdd_d", "hdd_d"]
        common = ys.index.intersection(xs.index)
        for i, t in enumerate(common):
            hist = common[max(0, i - 60):i]
            if len(hist) < 36:
                continue
            A = np.column_stack([np.ones(len(hist)), xs.loc[hist].values])
            coef, *_ = np.linalg.lstsq(A, ys.loc[hist].values, rcond=None)
            if not xs.loc[[t]].isna().any().any():
                fitted.loc[t] = float(coef[0] + coef[1] * xs.loc[t, "cdd_d"]
                                      + coef[2] * xs.loc[t, "hdd_d"])
        resid = (yoy - fitted).where(fitted.notna(), yoy)
    else:
        log.warning("power_demand_structural: degree days missing — raw YoY (no weather adj)")
    # PIT: month value available at month-end + DEMAND_LAG_DAYS (demand is near-real-time)
    resid.index = resid.index + pd.Timedelta(days=DEMAND_LAG_DAYS)
    daily = resid.dropna().resample("D").ffill()
    z = _ts_z(daily)
    z.name = "power_demand_structural"
    if start:
        z = z.loc[pd.Timestamp(start):]
    if end:
        z = z.loc[:pd.Timestamp(end)]
    return z


# ===========================================================================
# A4: Installed capacity (EIA-860M plant rows → monthly US aggregates)
# ===========================================================================

def update_capacity(start: str = CAPACITY_START, refreeze: bool = False) -> int:
    """Aggregate operating nameplate capacity by month × energy source.

    Plant-level rows (~24k/month) are aggregated client-side; init from 2019 is
    a few hundred pages (run once, then incremental = latest 2 months).
    """
    pit.ensure_dirs(); ALTDATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = {} if refreeze else pit.load_json(CAPACITY_PATH, default={}).get("records", {})
    if existing:
        start = max(existing)          # refetch last stored month + newer
    rows = _eia_get("electricity/operating-generator-capacity", {
        "frequency": "monthly", "data[0]": "nameplate-capacity-mw",
        "facets[status][]": "OP",
        "start": start,
        "sort[0][column]": "period", "sort[0][direction]": "asc",
    })
    if not rows:
        log.error("capacity: EIA returned no rows")
        return 0
    agg: dict = {}
    for r in rows:
        per, es, mw = r.get("period"), r.get("energy-source-code"), r.get("nameplate-capacity-mw")
        if not per or mw is None:
            continue
        rec = agg.setdefault(per, {"period": per, "total_mw": 0.0, "by_source": {}})
        mw = float(mw)
        rec["total_mw"] += mw
        if es:
            rec["by_source"][es] = rec["by_source"].get(es, 0.0) + mw
    for per, rec in agg.items():
        rec["total_mw"] = round(rec["total_mw"], 1)
        rec["by_source"] = {k: round(v, 1) for k, v in
                            sorted(rec["by_source"].items(), key=lambda kv: -kv[1])[:12]}
        p = pd.Period(per, freq="M")
        rec["release_date"] = (p.end_time.date() + timedelta(days=CAPACITY_LAG_DAYS)).isoformat()
    records = pit.merge_frozen(existing, agg)
    payload = {"meta": {"source": "EIA v2 operating-generator-capacity status=OP",
                        "lag_days": CAPACITY_LAG_DAYS,
                        "updated_at": date.today().isoformat(),
                        "frozen_append_only": True, "n": len(records)},
               "records": records}
    pit.save_json(CAPACITY_PATH, payload)
    last = max(records)
    log.info("capacity: %d months, last %s total=%.0f GW",
             len(records), last, records[last]["total_mw"] / 1e3)
    return len(records)


def load_capacity_yoy(start=None, end=None) -> pd.Series:
    """PIT daily capacity YoY% (total operating MW)."""
    records = pit.load_json(CAPACITY_PATH, default={}).get("records", {})
    if not records:
        return pd.Series(dtype="float64", name="capacity_yoy")
    m = pd.Series({k: v["total_mw"] for k, v in records.items()}).sort_index()
    yoy = (m / m.shift(12) - 1.0) * 100.0
    recs = {k: {"yoy": float(v), "release_date": records[k]["release_date"]}
            for k, v in yoy.dropna().items()}
    if not recs:
        return pd.Series(dtype="float64", name="capacity_yoy")
    avail = pit.pit_series(recs, value_field="yoy", date_field="release_date")
    daily = pit.reindex_pit_daily(avail, start=start, end=end)
    daily.name = "capacity_yoy"
    return daily


def load_renewables_adds_yoy(start=None, end=None) -> pd.Series:
    """Solar+wind (+battery when coded) operating capacity YoY% — NXT/FLNC
    order-conversion evidence (renewables_storage confirmation)."""
    records = pit.load_json(CAPACITY_PATH, default={}).get("records", {})
    if not records:
        return pd.Series(dtype="float64", name="renewables_adds_yoy")
    codes = ("SUN", "WND", "MWH", "BA", "ES")     # battery code varies by vintage
    m = pd.Series({k: sum(v.get("by_source", {}).get(c, 0.0) for c in codes)
                   for k, v in records.items()}).sort_index()
    m = m[m > 0]
    yoy = (m / m.shift(12) - 1.0) * 100.0
    recs = {k: {"yoy": float(v), "release_date": records[k]["release_date"]}
            for k, v in yoy.dropna().items()}
    if not recs:
        return pd.Series(dtype="float64", name="renewables_adds_yoy")
    avail = pit.pit_series(recs, value_field="yoy", date_field="release_date")
    daily = pit.reindex_pit_daily(avail, start=start, end=end)
    daily.name = "renewables_adds_yoy"
    return daily


# ===========================================================================
# A3②: DC-state retail price premium (EIA retail-sales by state)
# ===========================================================================

def update_state_price(refreeze: bool = False) -> int:
    pit.ensure_dirs(); ALTDATA_DIR.mkdir(parents=True, exist_ok=True)
    rows = _eia_get("electricity/retail-sales", {
        "frequency": "monthly", "data[0]": "price",
        **{"facets[stateid][]": DC_STATES + ["US"]},
        "facets[sectorid][]": "ALL", "start": "2010-01",
        "sort[0][column]": "period", "sort[0][direction]": "asc",
    })
    if not rows:
        log.error("state price: EIA returned no rows")
        return 0
    fresh: dict = {}
    for r in rows:
        per, st, price = r.get("period"), r.get("stateid"), r.get("price")
        if not per or price is None:
            continue
        fresh.setdefault(per, {"period": per})[f"price_{st.lower()}"] = float(price)
    for per, rec in fresh.items():
        us = rec.get("price_us")
        states = [rec.get(f"price_{s.lower()}") for s in DC_STATES]
        states = [x for x in states if x is not None]
        if us and states:
            rec["dc_premium_c_kwh"] = round(sum(states) / len(states) - us, 3)
        p = pd.Period(per, freq="M")
        rec["release_date"] = (p.end_time.date() + timedelta(days=EPM_LAG_DAYS)).isoformat()
    existing = {} if refreeze else pit.load_json(STATE_PRICE_PATH, default={}).get("records", {})
    records = pit.merge_frozen(existing, fresh)
    payload = {"meta": {"source": "EIA retail-sales price by state",
                        "dc_states": DC_STATES, "lag_days": EPM_LAG_DAYS,
                        "updated_at": date.today().isoformat(),
                        "frozen_append_only": True, "n": len(records)},
               "records": records}
    pit.save_json(STATE_PRICE_PATH, payload)
    last = max(records)
    log.info("state price: %d months, last %s DC premium=%.2f c/kWh",
             len(records), last, records[last].get("dc_premium_c_kwh", 0))
    return len(records)


def load_dc_price_premium(start=None, end=None) -> pd.Series:
    """PIT daily z of the DC-state price premium (差分序列天然去全国季节因子)."""
    records = pit.load_json(STATE_PRICE_PATH, default={}).get("records", {})
    records = {k: v for k, v in records.items() if v.get("dc_premium_c_kwh") is not None}
    if not records:
        return pd.Series(dtype="float64", name="dc_price_premium")
    avail = pit.pit_series(records, value_field="dc_premium_c_kwh",
                           date_field="release_date")
    daily = pit.reindex_pit_daily(avail, start=None, end=end)
    z = _ts_z(daily)
    z.name = "dc_price_premium"
    if start:
        z = z.loc[pd.Timestamp(start):]
    return z


# ===========================================================================
# Shortage score + AI demand cycle (exposure amplifier, engine Path A)
# ===========================================================================

def load_shortage_score(start=None, end=None) -> pd.Series:
    """缺电度 = z36(结构性需求 YoY − 装机 YoY) — AEUS_PLAN §4.2 A4通路③.

    需求跑赢装机 = 全链稀缺 → 高分;装机追上 = 降温.  Either leg missing → empty
    (engine leaves exposure at 1.0)."""
    d = load_power_demand_structural(end=end)          # already z — use raw resid instead
    c = load_capacity_yoy(end=end)
    if d.empty or c.empty:
        return pd.Series(dtype="float64", name="shortage_score")
    df = pd.concat([d, c], axis=1).ffill().dropna()
    if df.empty:
        return pd.Series(dtype="float64", name="shortage_score")
    gap = df.iloc[:, 0] - _ts_z(df.iloc[:, 1])          # both in z-space
    z = _ts_z(gap.dropna())
    z.name = "shortage_score"
    if start:
        z = z.loc[pd.Timestamp(start):]
    return z


def load_ai_demand_cycle(start: Optional[str] = None, end: Optional[str] = None,
                         sources: Optional[list] = None) -> pd.Series:
    """PIT daily z-score of AI-power demand strength (exposure amplifier).

    AEUS blend (each leg z-scored, averaged over what's available, re-z'd):
        hyperscaler_capex_yoy   — the prime AI-capex driver (shared with AISS)
        power_demand_structural — weather-adjusted US demand (A1×A5)
        shortage_score          — demand vs capacity gap (A4)
    Missing data → empty → engine leaves exposure at 1.0 (AISS convention).
    """
    try:
        from electric_utilities_strategy.data import company_signals as comp
    except Exception:  # pragma: no cover
        import company_signals as comp  # type: ignore
    src = sources or ["hyperscaler_capex_yoy", "power_demand_structural", "shortage_score"]
    parts = []
    if "hyperscaler_capex_yoy" in src:
        h = comp.load_hyperscaler_capex_yoy(end=end)
        if not h.empty:
            parts.append(_ts_z(h.dropna()))
    if "power_demand_structural" in src:
        parts.append(load_power_demand_structural(end=end))
    if "shortage_score" in src:
        parts.append(load_shortage_score(end=end))
    parts = [p for p in parts if p is not None and not p.empty]
    if not parts:
        return pd.Series(dtype="float64", name="ai_demand_cycle")
    df = pd.concat(parts, axis=1).sort_index().ffill().dropna(how="all")
    blend = df.mean(axis=1)
    z = _ts_z(blend.dropna())
    z.name = "ai_demand_cycle"
    if start:
        z = z.loc[pd.Timestamp(start):]
    return z


# ===========================================================================
# N8: GPU cloud pricing (FORWARD-ONLY — inherited from AISS unchanged)
# ===========================================================================

def _fetch_gpu_snapshot() -> pd.DataFrame:
    req = urllib.request.Request(GPU_API, headers={"User-Agent": "aeus-research/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    rows = data if isinstance(data, list) else data.get("data", [])
    return pd.DataFrame(rows)


def snapshot_gpu(as_of: Optional[str] = None) -> int:
    """Append today's GPU price snapshot to the history parquet (forward-only)."""
    ALTDATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        df = _fetch_gpu_snapshot()
    except Exception as e:  # noqa: BLE001
        log.warning("GPU snapshot failed: %s", e)
        return 0
    if df.empty:
        return 0
    df["snapshot_date"] = as_of or date.today().isoformat()
    if GPU_HISTORY_PATH.exists():
        hist = pd.read_parquet(GPU_HISTORY_PATH)
        snap_dates = set(hist["snapshot_date"].unique())
        if df["snapshot_date"].iloc[0] in snap_dates:
            log.info("GPU snapshot for %s already recorded", df["snapshot_date"].iloc[0])
            return 0
        df = pd.concat([hist, df], ignore_index=True)
    df.to_parquet(GPU_HISTORY_PATH, index=False)
    log.info("GPU pricing: history now %d rows (%s snapshots)",
             len(df), df["snapshot_date"].nunique())
    return len(df)


def load_gpu_price_history() -> pd.DataFrame:
    if not GPU_HISTORY_PATH.exists():
        return pd.DataFrame()
    return pd.read_parquet(GPU_HISTORY_PATH)


def init_gpu() -> None:
    snapshot_gpu()


# ===========================================================================
# Verify / CLI
# ===========================================================================

def verify() -> bool:
    print("=" * 70)
    print("AEUS ALT-DATA SIGNALS")
    print("=" * 70)
    ok = True

    def _series_check(name, s, cadence):
        nonlocal ok
        if len(s):
            tag = pit.stale_tag(s.index[-1].date(), cadence)
            print(f"  {name:24}: {len(s):5} pts →{s.index[-1].date()} last={s.iloc[-1]:+.2f}{tag}")
            if tag:
                ok = False
        else:
            print(f"  {name:24}: EMPTY")
            ok = False

    _series_check("transformer_ppi_yoy", load_transformer_ppi_yoy(), "monthly")
    _series_check("cpi_electricity_yoy", load_cpi_electricity_yoy(), "monthly")
    _series_check("construction_hiring", load_construction_hiring(), "monthly")
    _series_check("power_demand_structural", load_power_demand_structural(), "monthly")
    _series_check("capacity_yoy", load_capacity_yoy(), "monthly")
    _series_check("dc_price_premium", load_dc_price_premium(), "monthly")
    sh = load_shortage_score()
    print(f"  shortage_score          : {len(sh):5} pts"
          + (f" →{sh.index[-1].date()} last={sh.iloc[-1]:+.2f}" if len(sh) else " (needs demand+capacity)"))
    cyc = load_ai_demand_cycle()
    print(f"  ai_demand_cycle         : {len(cyc):5} pts"
          + (f" →{cyc.index[-1].date()} last={cyc.iloc[-1]:+.2f}" if len(cyc) else ""))
    gpu = load_gpu_price_history()
    print(f"  gpu_pricing (fwd-only)  : {len(gpu):5} rows, "
          f"{gpu['snapshot_date'].nunique() if len(gpu) else 0} snapshots")
    print("=" * 70)
    print("RESULT:", "OK" if ok else "INCOMPLETE")
    return ok


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="AEUS alt-data signals")
    ap.add_argument("--init-fred", "--update-fred", dest="fred", action="store_true")
    ap.add_argument("--update-demand", dest="demand", action="store_true")
    ap.add_argument("--update-dd", dest="dd", action="store_true")
    ap.add_argument("--update-capacity", dest="capacity", action="store_true")
    ap.add_argument("--update-state-price", dest="state_price", action="store_true")
    ap.add_argument("--init", action="store_true", help="all of the above")
    ap.add_argument("--snapshot-gpu", dest="gpu", action="store_true")
    ap.add_argument("--init-gpu", dest="gpu_init", action="store_true")
    ap.add_argument("--refreeze", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    did = False
    if args.init or args.fred:
        update_fred_altdata(); did = True
    if args.init or args.demand:
        update_demand(refreeze=args.refreeze); did = True
    if args.init or args.dd:
        update_degree_days(refreeze=args.refreeze); did = True
    if args.init or args.capacity:
        update_capacity(refreeze=args.refreeze); did = True
    if args.init or args.state_price:
        update_state_price(refreeze=args.refreeze); did = True
    if args.gpu or args.gpu_init:
        snapshot_gpu(); did = True
    _ok = True
    if args.verify or did:
        _ok = verify()
    if not did and not args.verify:
        print("Nothing to do. See --help.")
    if args.verify and not _ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
