"""
AEUS industry-layer signals
===========================
Slow (monthly/quarterly) industry series stored under
``price_data/elec_strategy/industry/``, all PIT-keyed and append-only frozen.
Structural mirror of the AISS industry layer (TSMC/ASML/DRAM/PMI), retargeted
per AEUS_PLAN §4:

  1. elec_gen_monthly  (elec_gen_monthly.json)        [analog of TSMC revenue]
     US total monthly retail electricity sales + average price from EIA v2
     ``electricity/retail-sales`` (stateid=US, sectorid=ALL).  YoY% of sales is
     the demand-side confirmation for the power chain.  PIT availability =
     period end + EPM release lag (~56 d, Electric Power Monthly cadence).

  2. gen_by_fuel  (gen_by_fuel.json)
     US monthly net generation by fuel (NG / NUC / SUN / WND / COW) from EIA v2
     ``electricity/electric-power-operational-data`` — fuel-level confirmations
     (gas share → gas_midstream, nuclear output → nuclear_fuel, solar+wind →
     renewables_storage).  Same PIT lag as EPM.

  3. backlog_rpo  (backlog_rpo.json)                  [analog of ASML orders]
     Aggregate RemainingPerformanceObligation (contract backlog) of the grid
     capex complex from SEC XBRL — the order-visibility series for the
     transformer/EPC bottleneck.  Members probed live 2026-08-30:
         GEV 2024Q1+ ($176B!)  PWR 2018Q1+  EMR 2018Q4+  ETN 2020Q3–2024Q1
     (VRT tags no RPO — MD&A prose only — hence excluded; documented).
     Composition changes (GEV entering, ETN stopping) are handled with a
     COMPOSITION-MATCHED YoY: each quarter's YoY compares only the members
     present in both quarters.  PIT = latest ``filed`` of the quarter's members.

  4. gas_price_proxy  (gas_price_proxy.json)          [analog of DRAM proxy]
     Henry Hub daily spot (FRED DHHNGSP) z-scored over 252 d, optionally
     blended with the weekly NG storage anomaly READ-ONLY from the macro
     module's EIA mirror (price_data/eia/ng_storage_weekly.json — maintained
     daily by prediction_market_macro; we never write it).  Frozen append-only.

  5. pmi_series  (iputil_series.json)                 [IPUTIL replaces IPMAN]
     FRED ``IPUTIL`` (Industrial Production: Electric & Gas Utilities) YoY —
     the industrial_demand_proxy node.  Function names keep the AISS ``pmi_*``
     interface so engine call-sites stay identical.

CLI
---
    python -m electric_utilities_strategy.data.industry_signals --init
    python -m electric_utilities_strategy.data.industry_signals --update-elec-gen
    python -m electric_utilities_strategy.data.industry_signals --update-fuel-mix
    python -m electric_utilities_strategy.data.industry_signals --update-backlog
    python -m electric_utilities_strategy.data.industry_signals --update-gas
    python -m electric_utilities_strategy.data.industry_signals --update-pmi
    python -m electric_utilities_strategy.data.industry_signals --verify
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
from typing import Optional

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
    from electric_utilities_strategy.data import aeus_fetch_sec_data as sec
    from electric_utilities_strategy.data import aeus_fetch_prices as fp
except Exception:  # pragma: no cover
    import aeus_pit as pit              # type: ignore
    import aeus_fetch_sec_data as sec   # type: ignore
    import aeus_fetch_prices as fp      # type: ignore

log = logging.getLogger("aeus.industry")

ELEC_GEN_PATH = pit.INDUSTRY_DIR / "elec_gen_monthly.json"
FUEL_MIX_PATH = pit.INDUSTRY_DIR / "gen_by_fuel.json"
BACKLOG_PATH = pit.INDUSTRY_DIR / "backlog_rpo.json"
GAS_PATH = pit.INDUSTRY_DIR / "gas_price_proxy.json"
PMI_PATH = pit.INDUSTRY_DIR / "iputil_series.json"

# ── EIA v2 (pattern adapted from prediction_market_macro/ingest/eia.py:
#    length<=5000 pagination, facet ids, api_key param; that module hardcodes
#    frequency=weekly for its NG feeds, hence this monthly-capable sibling) ──
EIA_BASE = "https://api.eia.gov/v2"
EPM_RELEASE_LAG_DAYS = 56          # EPM for month M lands ~25th of M+2 (conservative)
EIA_MONTHLY_START = "2010-01"      # deep history for YoY z-windows

# ── Backlog (RPO) members: CIKs verified live vs company_tickers.json and
#    companyfacts probed 2026-08-30 (tag presence + spans as in the docstring) ──
BACKLOG_COMPANIES = {
    "GEV": 1996810,
    "PWR": 1050915,
    "EMR": 32604,
    "ETN": 1551182,     # tagged 2020Q3–2024Q1 only; composition-matched YoY handles exit
}
BACKLOG_CONCEPT = "RevenueRemainingPerformanceObligation"
BACKLOG_MIN_COMPANIES = 2

# ── Gas price proxy ──
GAS_FRED_SERIES = "DHHNGSP"        # Henry Hub daily spot
GAS_Z_WINDOW = 252
GAS_START = "2010-01-01"
NG_STORAGE_MIRROR = _PROJECT_DIR / "price_data" / "eia" / "ng_storage_weekly.json"
GAS_STORAGE_BLEND = 0.30           # combined = (1-b)·price_z − b·storage_z (storage high = bearish)

# ── Industrial demand proxy (keeps AISS pmi_* interface) ──
FRED_PMI_SERIES = "IPUTIL"         # Industrial Production: Electric & Gas Utilities
PMI_START = "2010-01-01"
PMI_RELEASE_LAG_DAYS = 20          # G.17 lands mid-month+1; 20 d is conservative


# ===========================================================================
# Shared clients
# ===========================================================================

def _eia_key() -> Optional[str]:
    key = os.environ.get("EIA_API_KEY")
    if not key:
        log.warning("EIA_API_KEY not set; EIA series cannot be updated")
    return key


def _eia_get(route: str, params: dict) -> list:
    """Paged GET against EIA v2 ``{route}/data/`` → list of row dicts.

    Pagination discipline copied from prediction_market_macro/ingest/eia.py:
    length<=5000 per call with offset stepping until short page.
    """
    key = _eia_key()
    if not key:
        return []
    rows: list = []
    offset = 0
    while True:
        q = dict(params)
        q.update({"api_key": key, "length": 5000, "offset": offset})
        url = f"{EIA_BASE}/{route}/data/?" + urllib.parse.urlencode(q, doseq=True)
        req = urllib.request.Request(url, headers={"User-Agent": "someopark-aeus/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = json.loads(r.read())
        page = payload.get("response", {}).get("data", [])
        rows.extend(page)
        if len(page) < 5000:
            return rows
        offset += 5000


def _fred_client():
    """Return a fredapi.Fred client, or None if unavailable (graceful fallback)."""
    key = os.environ.get("FRED_API_KEY")
    if not key:
        log.warning("FRED_API_KEY not set; FRED series cannot be updated")
        return None
    try:
        from fredapi import Fred
    except Exception as e:  # noqa: BLE001
        log.warning("fredapi unavailable (%s)", e)
        return None
    return Fred(api_key=key)


def _epm_knowledge_date(period_yyyy_mm: str) -> str:
    """PIT availability of an EPM month = month end + EPM_RELEASE_LAG_DAYS."""
    p = pd.Period(period_yyyy_mm, freq="M")
    return (p.end_time.date() + timedelta(days=EPM_RELEASE_LAG_DAYS)).isoformat()


# ===========================================================================
# 1. US monthly retail electricity sales + price (EIA retail-sales)
# ===========================================================================

def update_elec_gen(refreeze: bool = False) -> int:
    """Fetch US-total monthly retail sales (million kWh) + avg price (c/kWh).

    Append-only frozen (merge_frozen): a month's value is never rewritten once
    recorded, so the backtest stays reproducible across EIA revisions.
    """
    pit.ensure_dirs()
    rows = _eia_get("electricity/retail-sales", {
        "frequency": "monthly",
        "data[0]": "sales", "data[1]": "price",
        "facets[stateid][]": "US", "facets[sectorid][]": "ALL",
        "start": EIA_MONTHLY_START,
        "sort[0][column]": "period", "sort[0][direction]": "asc",
    })
    if not rows:
        log.error("elec_gen: EIA returned no rows")
        return 0
    fresh: dict = {}
    by_period: dict = {}
    for r in rows:
        per = r.get("period")
        if per:
            by_period.setdefault(per, {}).update(r)
    for per, r in by_period.items():
        sales = r.get("sales")
        if sales is None:
            continue
        fresh[per] = {
            "period": per,
            "sales_mkwh": float(sales),
            "price_c_kwh": (float(r["price"]) if r.get("price") is not None else None),
            "release_date": _epm_knowledge_date(per),
        }
    # YoY on the FULL vintage (needs the prior year even when frozen)
    for per, rec in fresh.items():
        prev = f"{int(per[:4]) - 1}{per[4:]}"
        pv = fresh.get(prev, {}).get("sales_mkwh")
        rec["yoy_pct"] = (round((rec["sales_mkwh"] / pv - 1.0) * 100.0, 2)
                          if pv else None)
    existing = {} if refreeze else pit.load_json(ELEC_GEN_PATH, default={}).get("records", {})
    records = pit.merge_frozen(existing, fresh)
    payload = {
        "meta": {"source": "EIA v2 electricity/retail-sales US/ALL",
                 "release_lag_days": EPM_RELEASE_LAG_DAYS,
                 "updated_at": date.today().isoformat(),
                 "frozen_append_only": True, "n": len(records)},
        "records": records,
    }
    pit.save_json(ELEC_GEN_PATH, payload)
    last = max(records)
    log.info("elec_gen: %d months, last %s sales=%.0f mkWh YoY=%s",
             len(records), last, records[last]["sales_mkwh"], records[last]["yoy_pct"])
    return len(records)


def load_elec_gen(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """PIT-correct daily series of US retail-sales YoY% (ffilled from release)."""
    records = pit.load_json(ELEC_GEN_PATH, default={}).get("records", {})
    records = {k: v for k, v in records.items() if v.get("yoy_pct") is not None}
    if not records:
        return pd.Series(dtype="float64", name="elec_gen_yoy")
    avail = pit.pit_series(records, value_field="yoy_pct", date_field="release_date")
    daily = pit.reindex_pit_daily(avail, start=start, end=end)
    daily.name = "elec_gen_yoy"
    return daily


def load_elec_price(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """PIT-correct daily series of US average retail price (c/kWh)."""
    records = pit.load_json(ELEC_GEN_PATH, default={}).get("records", {})
    records = {k: v for k, v in records.items() if v.get("price_c_kwh") is not None}
    if not records:
        return pd.Series(dtype="float64", name="elec_price")
    avail = pit.pit_series(records, value_field="price_c_kwh", date_field="release_date")
    daily = pit.reindex_pit_daily(avail, start=start, end=end)
    daily.name = "elec_price"
    return daily


def elec_gen_value_at(as_of_date) -> Optional[dict]:
    records = pit.load_json(ELEC_GEN_PATH, default={}).get("records", {})
    return pit.get_latest_available(records, as_of_date, "release_date")


# ===========================================================================
# 2. US monthly net generation by fuel (EIA electric-power-operational-data)
# ===========================================================================

_FUELS = {"NG": "gas", "NUC": "nuclear", "SUN": "solar", "WND": "wind", "COW": "coal"}


def update_fuel_mix(refreeze: bool = False) -> int:
    """US monthly net generation by fuel type (thousand MWh), frozen append-only."""
    pit.ensure_dirs()
    rows = _eia_get("electricity/electric-power-operational-data", {
        "frequency": "monthly",
        "data[0]": "generation",
        "facets[location][]": "US",
        "facets[sectorid][]": "98",          # electric power total
        **{f"facets[fueltypeid][]": list(_FUELS)},
        "start": EIA_MONTHLY_START,
        "sort[0][column]": "period", "sort[0][direction]": "asc",
    })
    if not rows:
        log.error("fuel_mix: EIA returned no rows")
        return 0
    fresh: dict = {}
    for r in rows:
        per, ft, gen = r.get("period"), r.get("fueltypeid"), r.get("generation")
        if not per or ft not in _FUELS or gen is None:
            continue
        rec = fresh.setdefault(per, {"period": per,
                                     "release_date": _epm_knowledge_date(per)})
        rec[f"gen_{_FUELS[ft]}_gwh"] = float(gen)
    # derived: gas share of total + per-fuel YoY
    for per, rec in fresh.items():
        gens = {k: v for k, v in rec.items() if k.startswith("gen_")}
        total = sum(gens.values())
        if total > 0 and "gen_gas_gwh" in rec:
            rec["gas_share_pct"] = round(rec["gen_gas_gwh"] / total * 100.0, 2)
        prev = fresh.get(f"{int(per[:4]) - 1}{per[4:]}", {})
        for k in list(gens):
            pv = prev.get(k)
            if pv:
                rec[k.replace("_gwh", "_yoy_pct")] = round((rec[k] / pv - 1.0) * 100.0, 2)
    existing = {} if refreeze else pit.load_json(FUEL_MIX_PATH, default={}).get("records", {})
    records = pit.merge_frozen(existing, fresh)
    payload = {
        "meta": {"source": "EIA v2 electric-power-operational-data US sector98",
                 "fuels": _FUELS, "release_lag_days": EPM_RELEASE_LAG_DAYS,
                 "updated_at": date.today().isoformat(),
                 "frozen_append_only": True, "n": len(records)},
        "records": records,
    }
    pit.save_json(FUEL_MIX_PATH, payload)
    last = max(records)
    log.info("fuel_mix: %d months, last %s gas_share=%s%%",
             len(records), last, records[last].get("gas_share_pct"))
    return len(records)


def load_fuel_yoy(fuel: str, start: Optional[str] = None,
                  end: Optional[str] = None) -> pd.Series:
    """PIT-correct daily series of one fuel's generation YoY% (gas/nuclear/solar/wind)."""
    field = f"gen_{fuel}_yoy_pct"
    records = pit.load_json(FUEL_MIX_PATH, default={}).get("records", {})
    records = {k: v for k, v in records.items() if v.get(field) is not None}
    if not records:
        return pd.Series(dtype="float64", name=f"{fuel}_gen_yoy")
    avail = pit.pit_series(records, value_field=field, date_field="release_date")
    daily = pit.reindex_pit_daily(avail, start=start, end=end)
    daily.name = f"{fuel}_gen_yoy"
    return daily


def fuel_mix_value_at(as_of_date) -> Optional[dict]:
    records = pit.load_json(FUEL_MIX_PATH, default={}).get("records", {})
    return pit.get_latest_available(records, as_of_date, "release_date")


# ===========================================================================
# 3. Grid-capex backlog: aggregate RemainingPerformanceObligation (SEC XBRL)
# ===========================================================================

def update_backlog_rpo() -> int:
    """Aggregate quarterly RPO backlog across the grid-capex complex.

    RPO is an INSTANT concept (a balance, not a YTD flow) so ``concept_series``
    end-dedup is correct here — no decumulation needed (unlike CapEx).
    Composition-matched YoY: quarter Q's YoY uses only members reporting in
    both Q and Q-4, so GEV's 2024 entry / ETN's 2024 tag-stop don't fake jumps.
    """
    pit.ensure_dirs()
    per_member: dict = {}                 # ticker -> {end: {"val","filed"}}
    for tk, cik in BACKLOG_COMPANIES.items():
        try:
            facts = sec.fetch_company_facts(int(cik))
        except Exception as e:  # noqa: BLE001
            log.warning("backlog: %s (CIK %s) fetch failed: %s", tk, cik, e)
            continue
        pts = sec.concept_series(facts, BACKLOG_CONCEPT, forms=["10-Q", "10-K"])
        per_member[tk] = {p["end"]: p for p in pts if p.get("val")}

    # bucket by calendar quarter of the instant date
    frames: dict = {}                      # frame -> {ticker: {"val","filed","end"}}
    for tk, by_end in per_member.items():
        for end, p in by_end.items():
            try:
                edt = date.fromisoformat(end)
            except Exception:
                continue
            fr = f"CY{edt.year}Q{(edt.month - 1) // 3 + 1}"
            cur = frames.setdefault(fr, {}).get(tk)
            if cur is None or end > cur["end"]:
                frames[fr][tk] = {"val": float(p["val"]),
                                  "filed": p.get("filed"), "end": end}

    records: dict = {}
    for fr in sorted(frames):
        comp = frames[fr]
        if len(comp) < BACKLOG_MIN_COMPANIES:
            continue
        yr, q = int(fr[2:6]), int(fr[7])
        prev = frames.get(f"CY{yr - 1}Q{q}", {})
        common = sorted(set(comp) & set(prev))
        yoy = None
        if common:
            cur_sum = sum(comp[t]["val"] for t in common)
            prev_sum = sum(prev[t]["val"] for t in common)
            if prev_sum > 0:
                yoy = round((cur_sum / prev_sum - 1.0) * 100.0, 1)
        records[fr] = {
            "period_end": max(c["end"] for c in comp.values()),
            "filed_date": max((c["filed"] or "") for c in comp.values()),
            "rpo_usd_bn": round(sum(c["val"] for c in comp.values()) / 1e9, 2),
            "n_companies": len(comp),
            "companies_bn": {t: round(c["val"] / 1e9, 2) for t, c in comp.items()},
            "yoy_pct": yoy,                        # composition-matched
            "yoy_members": common,
        }
    if not records:
        log.error("backlog: no records computed")
        return 0
    payload = {
        "meta": {"ciks": BACKLOG_COMPANIES, "concept": BACKLOG_CONCEPT,
                 "min_companies": BACKLOG_MIN_COMPANIES,
                 "note": "VRT tags no RPO (MD&A prose only) — probed 2026-08-30",
                 "updated_at": date.today().isoformat(), "n": len(records)},
        "records": records,
    }
    pit.save_json(BACKLOG_PATH, payload)
    latest = sorted(records.values(), key=lambda r: r["period_end"])[-1]
    log.info("backlog RPO: %d quarters; latest %s $%.1fB (n=%d) YoY=%s filed=%s",
             len(records), latest["period_end"], latest["rpo_usd_bn"],
             latest["n_companies"], latest["yoy_pct"], latest["filed_date"])
    return len(records)


def load_backlog_yoy(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """PIT-correct daily series of backlog YoY% (ffilled from filed_date)."""
    records = pit.load_json(BACKLOG_PATH, default={}).get("records", {})
    records = {k: v for k, v in records.items() if v.get("yoy_pct") is not None}
    if not records:
        return pd.Series(dtype="float64", name="backlog_yoy")
    avail = pit.pit_series(records, value_field="yoy_pct", date_field="filed_date")
    daily = pit.reindex_pit_daily(avail, start=start, end=end)
    daily.name = "backlog_yoy"
    return daily


def backlog_value_at(as_of_date) -> Optional[dict]:
    records = pit.load_json(BACKLOG_PATH, default={}).get("records", {})
    return pit.get_latest_available(records, as_of_date, "filed_date")


# ===========================================================================
# 4. Gas price proxy (Henry Hub z + optional storage anomaly, frozen)
# ===========================================================================

def _load_ng_storage_z() -> pd.Series:
    """Weekly NG storage deviation z, READ-ONLY from the macro module's EIA
    mirror (price_data/eia/ng_storage_weekly.json).  Missing → empty (graceful).
    """
    try:
        raw = json.loads(NG_STORAGE_MIRROR.read_text())
    except Exception:
        return pd.Series(dtype="float64")
    # mirror schema (verified 2026-08-30 against the live file the macro module
    # writes): {pulled_at, series, route, n, data: [{period, value, ...}, ...]}
    rows = raw.get("data", []) if isinstance(raw, dict) else []
    if not rows:
        return pd.Series(dtype="float64")
    s = pd.Series({pd.Timestamp(r["period"]): float(r["value"])
                   for r in rows if r.get("value") is not None}).sort_index()
    if len(s) < 60:
        return pd.Series(dtype="float64")
    # deviation vs trailing 52-week mean, z over 156 weeks (~3y)
    dev = s - s.rolling(52, min_periods=26).mean()
    z = (dev - dev.rolling(156, min_periods=52).mean()) / \
        dev.rolling(156, min_periods=52).std().replace(0, np.nan)
    return z.dropna()


def update_gas_price_proxy(end_date: Optional[str] = None, start: str = GAS_START,
                           refreeze: bool = False) -> int:
    """Henry Hub daily z (252 d), blended with the NG-storage anomaly when the
    macro mirror is present; store append-only frozen (DRAM-proxy mechanism)."""
    pit.ensure_dirs()
    fred = _fred_client()
    if fred is None:
        return 0
    px = fred.get_series(GAS_FRED_SERIES, observation_start=start,
                         observation_end=end_date)
    px = pd.Series(px).dropna()
    if px.empty:
        log.error("gas proxy: FRED %s empty", GAS_FRED_SERIES)
        return 0
    z = (px - px.rolling(GAS_Z_WINDOW, min_periods=GAS_Z_WINDOW // 2).mean()) / \
        px.rolling(GAS_Z_WINDOW, min_periods=GAS_Z_WINDOW // 2).std().replace(0, np.nan)
    z = z.dropna()
    stor = _load_ng_storage_z()
    if len(stor):
        stor_d = stor.reindex(z.index, method="ffill")
        combined = (1 - GAS_STORAGE_BLEND) * z - GAS_STORAGE_BLEND * stor_d.fillna(0.0)
        used_storage = True
    else:
        combined = z
        used_storage = False
    fresh = {d.date().isoformat(): float(v) for d, v in combined.dropna().items()}
    existing = {} if refreeze else pit.load_json(GAS_PATH, default={}).get("series", {})
    series = pit.merge_frozen(existing, fresh)
    payload = {
        "meta": {"fred": GAS_FRED_SERIES, "z_window": GAS_Z_WINDOW,
                 "storage_blend": (GAS_STORAGE_BLEND if used_storage else 0.0),
                 "storage_mirror": str(NG_STORAGE_MIRROR) if used_storage else None,
                 "updated_at": date.today().isoformat(), "frozen_append_only": True,
                 "n": len(series), "last_date": max(series) if series else None,
                 "last_value": series[max(series)] if series else None},
        "series": series,
    }
    pit.save_json(GAS_PATH, payload)
    log.info("gas proxy (frozen): %d pts, last %s z=%.2f storage_blend=%s",
             len(series), payload["meta"]["last_date"],
             payload["meta"]["last_value"] or 0.0, used_storage)
    return len(series)


def load_gas_price_proxy(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    payload = pit.load_json(GAS_PATH, default={})
    series = payload.get("series", {})
    if not series:
        return pd.Series(dtype="float64", name="gas_price_proxy")
    s = pd.Series({pd.Timestamp(k): float(v) for k, v in series.items()}).sort_index()
    s.name = "gas_price_proxy"
    if start:
        s = s.loc[pd.Timestamp(start):]
    if end:
        s = s.loc[:pd.Timestamp(end)]
    return s


# ===========================================================================
# 5. Industrial demand proxy — FRED IPUTIL YoY (keeps the pmi_* interface)
# ===========================================================================

def update_pmi_series(start: str = PMI_START, refreeze: bool = False) -> int:
    """Append IPUTIL YoY monthly datapoints with PIT release dates (frozen).

    History is frozen on first sight (revisions ignored) so the backtest stays
    reproducible; ``refreeze=True`` rebuilds from the current FRED vintage.
    """
    pit.ensure_dirs()
    fred = _fred_client()
    if fred is None:
        return 0
    lvl = pd.Series(fred.get_series(FRED_PMI_SERIES, observation_start=start)).dropna()
    if lvl.empty:
        log.error("IPUTIL: FRED returned empty")
        return 0
    yoy = (lvl / lvl.shift(12) - 1.0) * 100.0
    fresh: dict = {}
    for ts, v in yoy.dropna().items():
        per = ts.strftime("%Y-%m")
        release = (ts + pd.offsets.MonthEnd(0)).date() + timedelta(days=PMI_RELEASE_LAG_DAYS)
        fresh[per] = {"period": per, "yoy_pct": round(float(v), 3),
                      "release_date": release.isoformat()}
    existing = {} if refreeze else pit.load_json(PMI_PATH, default={}).get("records", {})
    records = pit.merge_frozen(existing, fresh)
    payload = {
        "meta": {"fred": FRED_PMI_SERIES, "release_lag_days": PMI_RELEASE_LAG_DAYS,
                 "updated_at": date.today().isoformat(),
                 "frozen_append_only": True, "n": len(records)},
        "records": records,
    }
    pit.save_json(PMI_PATH, payload)
    last = max(records)
    log.info("IPUTIL: %d months, last %s YoY=%.2f%%",
             len(records), last, records[last]["yoy_pct"])
    return len(records)


def load_pmi_series(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """PIT-correct daily IPUTIL-YoY series (ffilled from release_date)."""
    records = pit.load_json(PMI_PATH, default={}).get("records", {})
    if not records:
        return pd.Series(dtype="float64", name="pmi_series")
    avail = pit.pit_series(records, value_field="yoy_pct", date_field="release_date")
    daily = pit.reindex_pit_daily(avail, start=start, end=end)
    daily.name = "pmi_series"
    return daily


def pmi_value_at(as_of_date) -> Optional[dict]:
    records = pit.load_json(PMI_PATH, default={}).get("records", {})
    return pit.get_latest_available(records, as_of_date, "release_date")


# AEUS-native aliases (same objects; clearer call-sites for new code)
load_industrial_demand_series = load_pmi_series
update_industrial_demand_series = update_pmi_series


# ===========================================================================
# Verify / CLI
# ===========================================================================

def verify() -> bool:
    """存在性 + 时效性双检(逐源 stale_tag;结构性缺口大声标注)。"""
    print("=" * 70)
    print("AEUS INDUSTRY-LAYER SIGNALS")
    print("=" * 70)
    ok = True

    def _rec_check(name, path, cadence, date_field, fmt):
        nonlocal ok
        recs = pit.load_json(path, default={}).get("records", {})
        if not recs:
            print(f"  {name:12}: MISSING"); ok = False
            return
        latest_k = max(recs, key=lambda k: recs[k].get(date_field) or "")
        latest = recs[latest_k]
        tag = pit.stale_tag(latest.get(date_field), cadence)
        print(f"  {name:12}: {len(recs):4} records, latest {latest_k} "
              f"{fmt(latest)} avail={latest.get(date_field)}{tag}")
        if tag:
            ok = False

    _rec_check("elec_gen", ELEC_GEN_PATH, "monthly", "release_date",
               lambda r: f"sales={r.get('sales_mkwh', 0):.0f}mkWh YoY={r.get('yoy_pct')}%")
    _rec_check("fuel_mix", FUEL_MIX_PATH, "monthly", "release_date",
               lambda r: f"gas_share={r.get('gas_share_pct')}%")
    _rec_check("backlog_rpo", BACKLOG_PATH, "quarterly", "filed_date",
               lambda r: f"${r.get('rpo_usd_bn')}B n={r.get('n_companies')} YoY={r.get('yoy_pct')}%")

    gas = load_gas_price_proxy()
    if len(gas):
        tag = pit.stale_tag(gas.index[-1].date(), "daily")
        # FRED DHHNGSP 天然滞后 ~1 周(EIA→FRED 转载);双倍容忍
        print(f"  gas_proxy   : {len(gas):4} pts {gas.index[0].date()}→{gas.index[-1].date()} "
              f"last z={gas.iloc[-1]:+.2f}{tag}")
    else:
        print("  gas_proxy   : MISSING"); ok = False

    _rec_check("iputil(pmi)", PMI_PATH, "monthly", "release_date",
               lambda r: f"YoY={r.get('yoy_pct')}%")
    print("=" * 70)
    print("RESULT:", "OK" if ok else "STALE/INCOMPLETE")
    return ok


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="AEUS industry-layer signals")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--update-elec-gen", dest="elec_gen", action="store_true")
    ap.add_argument("--update-fuel-mix", dest="fuel_mix", action="store_true")
    ap.add_argument("--update-backlog", dest="backlog", action="store_true")
    ap.add_argument("--update-gas", dest="gas", action="store_true")
    ap.add_argument("--update-pmi", "--update-iputil", dest="pmi", action="store_true")
    ap.add_argument("--refreeze", action="store_true",
                    help="rebuild frozen stores from the current vintage")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    did = False
    if args.init or args.elec_gen:
        update_elec_gen(refreeze=args.refreeze); did = True
    if args.init or args.fuel_mix:
        update_fuel_mix(refreeze=args.refreeze); did = True
    if args.init or args.backlog:
        update_backlog_rpo(); did = True
    if args.init or args.gas:
        update_gas_price_proxy(end_date=args.end, refreeze=args.refreeze); did = True
    if args.init or args.pmi:
        update_pmi_series(refreeze=args.refreeze); did = True
    _ok = True
    if args.verify or did:
        _ok = verify()
    if not did and not args.verify:
        print("Nothing to do. Use --init / --update-elec-gen / --update-fuel-mix / "
              "--update-backlog / --update-gas / --update-pmi / --verify.")
    if args.verify and not _ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
