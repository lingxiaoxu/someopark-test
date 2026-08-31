"""
AEUS company-layer signals
==========================
Company-specific derived signals stored under ``price_data/elec_strategy/company/``.

  1. AI CapEx pulse  (capex_pulse.json)
     Equal-weight 3-month return of the four hyperscalers (MSFT/GOOGL/META/AMZN),
     z-scored over a 24-month rolling window.  Pure price computation, daily,
     fetched **independently via yfinance** (these names are NOT in the tradeable
     universe and NOT in the price store) — fully decoupled from loader.py.

  2. Utility CapEx proxy  (utility_capex_actual.json)   [AEUS analog of MU DIO]
     Aggregate REAL quarterly CapEx of the regulated mega-utilities
     (NEE / DUK / SO) from SEC XBRL ``PaymentsToAcquirePropertyPlantAndEquipment``
     — the rate-base growth engine, i.e. how fast the regulated grid is being
     built out.  YoY% of the group sum; PIT availability = latest ``filed``.
     Reuses the same YTD-decumulation engine (``_standalone_quarters``) that the
     AISS lineage built for MU COGS + hyperscaler CapEx.

  3. Water-utility CapEx proxy  (water_capex_actual.json)  [AEUS_PLAN §4.1]
     Same engine over AWK / WTRG — the cooling-water buildout confirmation for
     the water_cooling subsector.

  4. Hyperscaler ACTUAL quarterly CapEx  (hyperscaler_capex_actual.json)
     Inherited from AISS unchanged — the same four hyperscalers drive both
     chip demand and datacenter power demand.

All are read back PIT-correctly via ``load_*`` (see ``aeus_pit``).

CLI
---
    python -m electric_utilities_strategy.data.company_signals --init
    python -m electric_utilities_strategy.data.company_signals --update-capex
    python -m electric_utilities_strategy.data.company_signals --update-utility-capex
    python -m electric_utilities_strategy.data.company_signals --update-water-capex
    python -m electric_utilities_strategy.data.company_signals --update-hyperscaler-capex
    python -m electric_utilities_strategy.data.company_signals --verify
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date
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
    from electric_utilities_strategy.data import universe as U
except Exception:  # pragma: no cover
    import aeus_pit as pit          # type: ignore
    import aeus_fetch_sec_data as sec  # type: ignore
    import universe as U            # type: ignore

log = logging.getLogger("aeus.company")

CAPEX_PATH = pit.COMPANY_DIR / "capex_pulse.json"

CAPEX_TICKERS = U.CAPEX_PULSE_TICKERS          # [MSFT, GOOGL, META, AMZN]
CAPEX_LOOKBACK_DAYS = 63                        # ~3 trading months
CAPEX_ZSCORE_WINDOW = 504                       # ~24 trading months
CAPEX_HISTORY_START = "2015-01-01"             # gives z-scores from ~2017

# --- Utility / water CapEx groups (AEUS analogs; CIKs verified live against
# --- SEC company_tickers.json on 2026-08-30) -------------------------------
UTILITY_CAPEX_PATH = pit.COMPANY_DIR / "utility_capex_actual.json"
UTILITY_CAPEX = {
    "NEE": (753308,  "PaymentsToAcquirePropertyPlantAndEquipment"),
    "DUK": (1326160, "PaymentsToAcquirePropertyPlantAndEquipment"),
    "SO":  (92122,   "PaymentsToAcquirePropertyPlantAndEquipment"),
}
UTILITY_MIN_COMPANIES = 2                       # need >=2 of 3 for the aggregate

WATER_CAPEX_PATH = pit.COMPANY_DIR / "water_capex_actual.json"
WATER_CAPEX = {
    "AWK":  (1410636, "PaymentsToAcquirePropertyPlantAndEquipment"),
    "WTRG": (78128,   "PaymentsToAcquirePropertyPlantAndEquipment"),
}
# 2026-08-30 实测:WTRG 近年只按年度 tag capex(YTD 链断,无法去累计出单季),
# min=2 会把序列冻死在 2018;AWK(全美最大水务、DC 冷却水主角)单家序列完整
# 到当季 → min=1,WTRG 有干净季度时自动并入(composition 记录在 companies_mn)。
WATER_MIN_COMPANIES = 1

# --- N1: Hyperscaler ACTUAL quarterly CapEx (SEC XBRL) ---------------------
# Precise quarterly CapEx from 10-Q/10-K filings — augments the daily price-proxy
# capex_pulse with the real spend figure (PIT = SEC ``filed`` date).
# CIKs + XBRL concepts verified live 2026-05-30 (companyconcept API).  AMZN uses
# PaymentsToAcquireProductiveAssets (its PP&E tag stops in 2017).
HYPERSCALER_CAPEX_PATH = pit.COMPANY_DIR / "hyperscaler_capex_actual.json"
HYPERSCALER_CAPEX = {
    "MSFT":  (789019,  "PaymentsToAcquirePropertyPlantAndEquipment"),   # 2009+
    "GOOGL": (1652044, "PaymentsToAcquirePropertyPlantAndEquipment"),   # 2015+
    "META":  (1326801, "PaymentsToAcquirePropertyPlantAndEquipment"),   # 2012+
    "AMZN":  (1018724, "PaymentsToAcquireProductiveAssets"),            # 2018+
}
_FRAME_Q = re.compile(r"^CY\d{4}Q[1-4]$")     # SEC calendar-quarter standardized frame
HYPERSCALER_MIN_COMPANIES = 3                  # need >=3 of 4 for a meaningful aggregate
HYPERSCALER_Z_WINDOW = 12                      # quarters for YoY z-score


# ===========================================================================
# AI CapEx pulse
# ===========================================================================

def _download_capex_prices(start: str, end: Optional[str]) -> pd.DataFrame:
    import warnings as _w
    _w.filterwarnings("ignore")
    import yfinance as yf
    raw = yf.download(CAPEX_TICKERS, start=start, end=end, auto_adjust=True, progress=False)
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"]
    else:
        close = raw[["Close"]].rename(columns={"Close": CAPEX_TICKERS[0]})
    close.index = pd.to_datetime(close.index).normalize()
    keep = [t for t in CAPEX_TICKERS if t in close.columns]
    return close[keep].dropna(how="all")


def compute_capex_pulse(close: pd.DataFrame) -> pd.Series:
    """Equal-weight 3M return of the 4 hyperscalers, 24M rolling z-score."""
    ret_3m = close.pct_change(CAPEX_LOOKBACK_DAYS)
    pulse = ret_3m.mean(axis=1)  # equal-weight across available names
    mu = pulse.rolling(CAPEX_ZSCORE_WINDOW, min_periods=CAPEX_ZSCORE_WINDOW // 2).mean()
    sd = pulse.rolling(CAPEX_ZSCORE_WINDOW, min_periods=CAPEX_ZSCORE_WINDOW // 2).std()
    z = (pulse - mu) / sd.replace(0, np.nan)
    return z.dropna()


def update_capex_pulse(end_date: Optional[str] = None, start: str = CAPEX_HISTORY_START,
                       refreeze: bool = False) -> int:
    """Recompute the CapEx-pulse z-score series and persist it (append-only/frozen)."""
    pit.ensure_dirs()
    close = _download_capex_prices(start, end_date)
    if close.empty:
        log.error("CapEx pulse: no hyperscaler prices downloaded")
        return 0
    z = compute_capex_pulse(close)
    fresh = {d.date().isoformat(): float(v) for d, v in z.items()}
    # PIT FREEZE (append-only): keep already-recorded values immutable, append
    # only newer dates.  The rolling z-score would otherwise rewrite history on
    # every refresh (yfinance adj-prices drift with dividends) → non-reproducible
    # backtest.  Pass --refreeze to reseed the frozen baseline from scratch.
    existing = {} if refreeze else pit.load_json(CAPEX_PATH, default={}).get("series", {})
    series = pit.merge_frozen(existing, fresh)
    payload = {
        "meta": {
            "tickers": CAPEX_TICKERS,
            "lookback_days": CAPEX_LOOKBACK_DAYS,
            "zscore_window": CAPEX_ZSCORE_WINDOW,
            "updated_at": date.today().isoformat(),
            "frozen_append_only": True,
            "n": len(series),
            "last_date": max(series) if series else None,
            "last_value": series[max(series)] if series else None,
        },
        "series": series,
    }
    pit.save_json(CAPEX_PATH, payload)
    log.info("CapEx pulse (frozen append-only): %d points, last %s z=%.2f (%d fresh dates merged)",
             len(series), payload["meta"]["last_date"], payload["meta"]["last_value"] or 0.0,
             max(0, len(series) - len(existing)))
    return len(series)


def load_capex_pulse(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """Daily CapEx-pulse z-score Series (PIT-clean: each value is same-day prices)."""
    payload = pit.load_json(CAPEX_PATH, default={})
    series = payload.get("series", {})
    if not series:
        return pd.Series(dtype="float64", name="capex_pulse")
    s = pd.Series({pd.Timestamp(k): float(v) for k, v in series.items()}).sort_index()
    s.name = "capex_pulse"
    if start:
        s = s.loc[pd.Timestamp(start):]
    if end:
        s = s.loc[:pd.Timestamp(end)]
    return s


# ===========================================================================
# MU DIO (SEC XBRL)
# ===========================================================================

def _duration_facts(facts: dict, concept: str,
                    forms: tuple = ("10-Q", "10-K")) -> dict:
    """All duration facts for ``concept``, keyed by ``(start, end)``.

    **``(start, end)`` — not ``end`` alone.**  ``sec.concept_series`` de-dupes by
    ``end``, which is the right key for *instant* concepts (InventoryNet) but wrong
    for *duration* concepts: a 10-Q tags both the fiscal-YTD span and the standalone
    quarter with the SAME ``end`` and the SAME ``filed`` date, so an end-keyed dict
    silently keeps whichever the file happened to list first.  That is exactly how
    MU's Q2/Q3 COGS were lost (see ``_standalone_quarters``).

    Dedups within a key by EARLIEST ``filed`` — a restatement must not retroactively
    change what we believed on the original filing date (PIT).
    """
    node = facts.get("facts", {}).get("us-gaap", {}).get(concept)
    if not node:
        return {}
    out: dict = {}
    for item in node.get("units", {}).get("USD", []):
        if item.get("form") not in forms:
            continue
        s, e, val, fy = (item.get("start"), item.get("end"),
                         item.get("val"), item.get("fy"))
        if not s or not e or val is None or fy is None:
            continue
        try:
            days = (date.fromisoformat(e) - date.fromisoformat(s)).days
        except Exception:
            continue
        if days < 60 or days > 380:
            continue  # keep quarterly..annual durations; drop instants & odd ranges
        filed = item.get("filed") or ""
        key = (s, e)
        prev = out.get(key)
        if prev is None or (filed and filed < (prev["filed"] or "9999")):
            out[key] = {"start": s, "end": e, "val": float(val), "filed": filed,
                        "fy": fy, "fp": item.get("fp"), "days": days}
    return out


def _standalone_quarters(facts: dict, concept: str,
                         forms: tuple = ("10-Q", "10-K"),
                         prefer_tagged: bool = False) -> dict:
    """Standalone (single-quarter) values for a cumulative-duration concept.

    → ``{end_iso: {"val", "filed", "start", "days", "fy", "fp", "source"}}``

    Default path is **decumulation** of the fiscal-YTD chain, which every filer
    supports (META tags only YTD CapEx):
        Q1 = YTD(Q1);  Q2 = YTD(Q2) - YTD(Q1);  …;  Q4 = FY - YTD(Q3).
    Availability = the *later* YTD's ``filed`` date, which is the day the
    subtraction first becomes computable — the earlier term is already public by
    then, so this stays PIT-honest.

    ``prefer_tagged=True`` additionally lets an explicitly-tagged ~90-day fact
    override the decumulated value for the same ``end``.  It is **off by default**
    on purpose: for MU the two agree exactly (2026Q2 tagged 6,105mn vs decumulated
    12,102-5,997 = 6,105mn; 2026Q3 6,400 vs 18,502-12,102 = 6,400), but for the
    hyperscalers it is not neutral — it recovers AMZN quarters whose YTD chain is
    incomplete (e.g. CY2018Q3 goes 3→4 companies), which would silently rewrite a
    live signal's history.  Flipping it is a deliberate, separately-reviewed change.

    2026-08-27: added when MU's "quarterly" DIO turned out to be annual.  MU tags
    BOTH a 181-day YTD and a 90-day standalone fact on ``end=2026-02-26``, both
    ``filed=2026-03-19``; the old end-keyed dedup kept the YTD, and the downstream
    80-100 day filter then dropped it.  Only each year's Q1 (whose YTD *is* the
    quarter) survived, so the signal moved once a year, every December.
    """
    by_se = _duration_facts(facts, concept, forms=forms)
    if not by_se:
        return {}

    from collections import defaultdict
    by_fy: dict = defaultdict(list)
    for rec in by_se.values():
        by_fy[rec["fy"]].append(rec)

    out: dict = {}
    for fy, recs in by_fy.items():
        # fy_start = start of the true first fiscal quarter (fp=Q1, ~90d) — NOT
        # min(start), which would catch trailing-12-month facts (e.g. AMZN's TTM
        # start a year earlier).  Fall back to the shortest-duration fact's start.
        q1 = [r for r in recs if r.get("fp") == "Q1" and 80 <= r["days"] <= 100]
        if q1:
            fy_start = min(r["start"] for r in q1)
        else:
            shortq = [r for r in recs if 80 <= r["days"] <= 100]
            fy_start = min(r["start"] for r in (shortq or recs))
        # YTD chain = facts sharing fy_start, durations increasing (~3/6/9/12mo)
        ytd = sorted([r for r in recs if r["start"] == fy_start and r["days"] <= 370],
                     key=lambda r: r["end"])
        prev_val, prev_end, prev_days = 0.0, fy_start, 0
        for r in ytd:
            cand = {"val": r["val"] - prev_val, "filed": r["filed"], "start": prev_end,
                    "days": r["days"] - prev_days, "fy": fy, "fp": r.get("fp"),
                    "source": "decumulated"}
            prev_val, prev_end, prev_days = r["val"], r["end"], r["days"]
            # The same ``end`` can surface under two ``fy`` values (a 10-K restates
            # the prior year's quarters in its own fiscal context).  Keep the
            # EARLIEST filing — that is when the number first became knowable.
            prev = out.get(r["end"])
            if prev is None or (cand["filed"] and cand["filed"] < (prev["filed"] or "9999")):
                out[r["end"]] = cand

    if prefer_tagged:
        # Explicitly-tagged standalone quarters win over anything decumulated above;
        # among two tagged facts for the same end, earliest filed wins (as above).
        for r in by_se.values():
            if not (80 <= r["days"] <= 100):
                continue
            cur = out.get(r["end"])
            if (cur is None or cur.get("source") != "tagged"
                    or (r["filed"] and r["filed"] < (cur["filed"] or "9999"))):
                out[r["end"]] = {**r, "source": "tagged"}
    return out


# ===========================================================================
# Group aggregate CapEx engine (shared by utility / water / hyperscaler groups)
# — the AISS hyperscaler aggregation logic, parameterised (AEUS_PLAN §4).
# ===========================================================================

def _compute_group_capex(companies: dict, min_companies: int, label: str) -> dict:
    """Aggregate a company group's real quarterly CapEx by calendar quarter.

    For each calendar quarter we sum the companies that reported a clean single
    quarter (require >= ``min_companies``), record the *availability* date as
    the latest ``filed`` among them (conservative / PIT-safe), and compute YoY
    vs the same quarter a year earlier.  Returns {frame: record}.
    """
    per_frame: dict = {}      # frame -> {ticker: {"val","filed","end"}}
    for tk, (cik, concept) in companies.items():
        try:
            facts = sec.fetch_company_facts(int(cik))
        except Exception as e:  # noqa: BLE001
            log.warning("%s CapEx: %s (CIK %s) fetch failed: %s", label, tk, cik, e)
            continue
        for fr, rec in _raw_quarterly_capex(facts, concept).items():
            per_frame.setdefault(fr, {})[tk] = rec

    sums: dict = {}
    records: dict = {}
    for fr in sorted(per_frame):
        comp = per_frame[fr]
        if len(comp) < min_companies:
            continue
        total = sum(c["val"] for c in comp.values())
        filed = max((c["filed"] or "") for c in comp.values())
        end = max((c["end"] or "") for c in comp.values())
        sums[fr] = total
        records[fr] = {
            "period_end": end,
            "filed_date": filed,        # PIT availability = last of the group's filings
            "capex_usd_mn": round(total / 1e6, 1),
            "n_companies": len(comp),
            "companies_mn": {t: round(c["val"] / 1e6, 1) for t, c in comp.items()},
        }

    # YoY% vs same calendar quarter a year earlier
    for fr in records:
        yr, q = int(fr[2:6]), int(fr[7])
        prev = f"CY{yr - 1}Q{q}"
        if prev in sums and sums[prev] > 0:
            records[fr]["capex_yoy_pct"] = round((sums[fr] / sums[prev] - 1.0) * 100.0, 1)
        else:
            records[fr]["capex_yoy_pct"] = None
    return records


def _update_group_capex(path: Path, companies: dict, min_companies: int,
                        label: str) -> int:
    pit.ensure_dirs()
    records = _compute_group_capex(companies, min_companies, label)
    if not records:
        log.error("%s CapEx: no records computed", label)
        return 0
    payload = {
        "meta": {
            "ciks": {t: c for t, (c, _) in companies.items()},
            "min_companies": min_companies,
            "updated_at": date.today().isoformat(),
            "n": len(records),
        },
        "records": records,
    }
    pit.save_json(path, payload)
    latest = sorted(records.values(), key=lambda r: r["period_end"])[-1]
    log.info("%s CapEx: %d quarters; latest %s sum=$%.1fB YoY=%s filed=%s",
             label, len(records), latest["period_end"],
             latest["capex_usd_mn"] / 1e3, latest["capex_yoy_pct"],
             latest["filed_date"])
    return len(records)


def _load_group_capex_yoy(path: Path, name: str,
                          start: Optional[str] = None,
                          end: Optional[str] = None) -> pd.Series:
    """PIT-correct daily series of a group's CapEx YoY% (ffilled from filed)."""
    payload = pit.load_json(path, default={})
    records = {k: v for k, v in payload.get("records", {}).items()
               if v.get("capex_yoy_pct") is not None}
    if not records:
        return pd.Series(dtype="float64", name=name)
    avail = pit.pit_series(records, value_field="capex_yoy_pct", date_field="filed_date")
    daily = pit.reindex_pit_daily(avail, start=start, end=end)
    daily.name = name
    return daily


# --- N2: Utility CapEx proxy (NEE/DUK/SO — rate-base growth engine) --------

def compute_utility_capex() -> dict:
    return _compute_group_capex(UTILITY_CAPEX, UTILITY_MIN_COMPANIES, "Utility")


def update_utility_capex() -> int:
    return _update_group_capex(UTILITY_CAPEX_PATH, UTILITY_CAPEX,
                               UTILITY_MIN_COMPANIES, "Utility")


def load_utility_capex_yoy(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    return _load_group_capex_yoy(UTILITY_CAPEX_PATH, "utility_capex_yoy", start, end)


def utility_capex_value_at(as_of_date) -> Optional[dict]:
    payload = pit.load_json(UTILITY_CAPEX_PATH, default={})
    return pit.get_latest_available(payload.get("records", {}), as_of_date, "filed_date")


# --- N3: Water-utility CapEx proxy (AWK/WTRG — cooling-water buildout) -----

def compute_water_capex() -> dict:
    return _compute_group_capex(WATER_CAPEX, WATER_MIN_COMPANIES, "Water")


def update_water_capex() -> int:
    return _update_group_capex(WATER_CAPEX_PATH, WATER_CAPEX,
                               WATER_MIN_COMPANIES, "Water")


def load_water_capex_yoy(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    return _load_group_capex_yoy(WATER_CAPEX_PATH, "water_capex_yoy", start, end)


def water_capex_value_at(as_of_date) -> Optional[dict]:
    payload = pit.load_json(WATER_CAPEX_PATH, default={})
    return pit.get_latest_available(payload.get("records", {}), as_of_date, "filed_date")


# ===========================================================================
# N1: Hyperscaler ACTUAL quarterly CapEx (SEC XBRL)
# ===========================================================================

def _raw_quarterly_capex(facts: dict, concept: str) -> dict:
    """Decumulate YTD CapEx → standalone calendar-quarter values.

    CapEx is a cumulative-duration flow: most filers (e.g. META) tag only the
    fiscal-YTD figure, so a 3-month-duration filter would miss Q2/Q3/Q4.  Instead
    we group facts by fiscal year, isolate the YTD chain (facts sharing the FY
    start date), and difference consecutive YTDs:
        Q1 = YTD(Q1);  Q2 = YTD(Q2) - YTD(Q1);  Q3 = YTD(Q3) - YTD(Q2);  Q4 = FY - YTD(Q3).
    Each standalone quarter is bucketed by the CALENDAR quarter of its ``end``
    (aligning the 4 firms' differing fiscal years) with the EARLIEST filing date
    for PIT correctness.  Returns {calendar_frame: {val, filed, end}}.

    2026-08-27: the decumulation itself moved to ``_standalone_quarters`` so MU's
    COGS and the hyperscalers' CapEx share one implementation; this function is now
    just the calendar-quarter bucketing on top.
    """
    out: dict = {}
    for end, r in _standalone_quarters(facts, concept).items():
        try:
            edt = date.fromisoformat(end)
        except Exception:
            continue
        frame = f"CY{edt.year}Q{(edt.month - 1) // 3 + 1}"
        filed = r["filed"]
        prev = out.get(frame)
        if prev is None or (filed and filed < (prev.get("filed") or "9999")):
            out[frame] = {"val": float(r["val"]), "filed": filed, "end": end}
    return out


def compute_hyperscaler_capex() -> dict:
    """Aggregate the 4 hyperscalers' real quarterly CapEx by calendar quarter.

    Delegates to the shared group engine (behaviour and payload schema are
    byte-identical to the AISS-lineage implementation this was factored from).
    """
    return _compute_group_capex(HYPERSCALER_CAPEX, HYPERSCALER_MIN_COMPANIES,
                                "Hyperscaler")


def update_hyperscaler_capex() -> int:
    return _update_group_capex(HYPERSCALER_CAPEX_PATH, HYPERSCALER_CAPEX,
                               HYPERSCALER_MIN_COMPANIES, "Hyperscaler")


def load_hyperscaler_capex_yoy(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """PIT-correct daily series of aggregate hyperscaler CapEx YoY% (ffilled from filed)."""
    return _load_group_capex_yoy(HYPERSCALER_CAPEX_PATH, "hyperscaler_capex_yoy",
                                 start, end)


def hyperscaler_capex_value_at(as_of_date) -> Optional[dict]:
    payload = pit.load_json(HYPERSCALER_CAPEX_PATH, default={})
    return pit.get_latest_available(payload.get("records", {}), as_of_date, "filed_date")


# ===========================================================================
# Snapshot (industry + company merged) — used by smart_select / reports
# ===========================================================================

def get_aeus_signals_snapshot(as_of_date) -> dict:
    """Return all slow + derived AEUS signals available as of ``as_of_date``."""
    snap: dict = {}
    cap = load_capex_pulse(end=str(as_of_date))
    snap["capex_pulse_zscore"] = float(cap.iloc[-1]) if len(cap) else None
    uc = utility_capex_value_at(as_of_date)
    snap["utility_capex_usd_mn"] = (uc or {}).get("capex_usd_mn")
    snap["utility_capex_yoy_pct"] = (uc or {}).get("capex_yoy_pct")
    wc = water_capex_value_at(as_of_date)
    snap["water_capex_yoy_pct"] = (wc or {}).get("capex_yoy_pct")
    hc = hyperscaler_capex_value_at(as_of_date)
    snap["hyperscaler_capex_usd_mn"] = (hc or {}).get("capex_usd_mn")
    snap["hyperscaler_capex_yoy_pct"] = (hc or {}).get("capex_yoy_pct")
    # industry layer (imported lazily to avoid a hard cycle)
    try:
        from electric_utilities_strategy.data import industry_signals as ind
    except Exception:  # pragma: no cover
        import industry_signals as ind  # type: ignore
    gen = ind.elec_gen_value_at(as_of_date)
    snap["elec_gen_yoy_latest"] = (gen or {}).get("yoy_pct")
    bk = ind.backlog_value_at(as_of_date)
    snap["backlog_rpo_usd_bn"] = (bk or {}).get("rpo_usd_bn")
    snap["backlog_rpo_yoy_pct"] = (bk or {}).get("yoy_pct")
    gas = ind.load_gas_price_proxy(end=str(as_of_date))
    snap["gas_price_z"] = float(gas.iloc[-1]) if len(gas) else None
    return snap


def verify() -> bool:
    """存在性 + **时效性** 双检。

    2026-08-27 加时效检查:此前只查 `if len(x)`,于是 capex_pulse 冻在 2026-06-04
    整整 57 个交易日,每周 weekly 照打 `RESULT: OK`。阈值与语义见 aeus_pit.staleness()。
    """
    cap = load_capex_pulse()
    print("=" * 70)
    print("AEUS COMPANY-LAYER SIGNALS")
    print("=" * 70)
    ok = True
    if len(cap):
        # 日频(每个交易日一个 z 点),用最后一个数据点本身当新鲜度基准
        tag = pit.stale_tag(cap.index[-1].date(), "daily")
        print(f"  capex_pulse : {len(cap):5} pts {cap.index[0].date()}→{cap.index[-1].date()} "
              f"last z={cap.iloc[-1]:+.2f}{tag}")
        if tag:
            ok = False
    else:
        print("  capex_pulse : MISSING"); ok = False

    def _check_group(name: str, path: Path, fatal: bool = True) -> bool:
        nonlocal ok
        payload = pit.load_json(path, default={})
        recs = payload.get("records", {})
        if recs:
            latest = sorted(recs.values(), key=lambda r: r["period_end"])[-1]
            first = sorted(recs.values(), key=lambda r: r["period_end"])[0]
            # 季频:基准取 filed_date(真正可用那天),不取 period_end —— 后者天生晚 90 天
            tag = pit.stale_tag(latest["filed_date"], "quarterly")
            print(f"  {name:17}: {len(recs):3} quarters {first['period_end']}→{latest['period_end']}, "
                  f"latest sum=${latest['capex_usd_mn']/1e3:.1f}B YoY={latest['capex_yoy_pct']}% "
                  f"filed={latest['filed_date']}{tag}")
            if tag:
                ok = False
            return True
        print(f"  {name:17}: MISSING")
        if fatal:
            ok = False
        return False

    _check_group("utility_capex", UTILITY_CAPEX_PATH)
    _check_group("water_capex", WATER_CAPEX_PATH)
    _check_group("hyperscaler_capex", HYPERSCALER_CAPEX_PATH)
    print("=" * 70)
    print("RESULT:", "OK" if ok else "STALE/INCOMPLETE")
    return ok


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="AEUS company-layer signals")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--update-capex", action="store_true")
    ap.add_argument("--update-utility-capex", "--init-utility-capex",
                    dest="utility_capex", action="store_true")
    ap.add_argument("--update-water-capex", "--init-water-capex",
                    dest="water_capex", action="store_true")
    ap.add_argument("--update-hyperscaler-capex", "--init-hyperscaler-capex",
                    dest="hyperscaler_capex", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    did = False
    if args.init or args.update_capex:
        update_capex_pulse(end_date=args.end); did = True
    if args.init or args.utility_capex:
        update_utility_capex(); did = True
    if args.init or args.water_capex:
        update_water_capex(); did = True
    if args.init or args.hyperscaler_capex:
        update_hyperscaler_capex(); did = True
    _ok = True
    if args.verify or did:
        _ok = verify()
    if not did and not args.verify:
        print("Nothing to do. Use --init / --update-capex / --update-utility-capex / "
              "--update-water-capex / --update-hyperscaler-capex / --verify.")
    # 退出码只在**显式** --verify 时才反映体检结果。
    # 为什么不在 update 之后也退非零: update_data 是每日跑的,而 ASML(2026 起停止
    # 披露季度 bookings)之类结构性缺口会让它天天红,红久了就没人看 —— 正是本次要
    # 修的病理本身。weekly 走的是显式 `--verify`(aeus_pipeline.sh),
    # 那条路径必须炸。
    if args.verify and not _ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
