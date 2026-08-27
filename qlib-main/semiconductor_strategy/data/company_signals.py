"""
AISS company-layer signals
==========================
Company-specific derived signals stored under ``price_data/semi_strategy/company/``.

  1. AI CapEx pulse  (capex_pulse.json)
     Equal-weight 3-month return of the four hyperscalers (MSFT/GOOGL/META/AMZN),
     z-scored over a 24-month rolling window.  Pure price computation, daily,
     fetched **independently via yfinance** (these names are NOT in the tradeable
     universe and NOT in the price store) — fully decoupled from loader.py.

  2. MU DIO proxy  (mu_dio_proxy.json)
     Micron Days-Inventory-Outstanding from SEC XBRL (CIK 723125):
         DIO = InventoryNet / (quarterly_COGS / days_in_quarter)
     Signal: DIO < 100 -> +1 (tight inventory, memory upcycle), > 150 -> -1
     (glut, downcycle), else 0.  PIT availability = 10-Q/K ``filed`` date.

Both are read back PIT-correctly via ``load_*`` (see ``aiss_pit``).

CLI
---
    python -m semiconductor_strategy.data.company_signals --init
    python -m semiconductor_strategy.data.company_signals --update-capex
    python -m semiconductor_strategy.data.company_signals --check-mu-dio
    python -m semiconductor_strategy.data.company_signals --verify
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
    from semiconductor_strategy.data import aiss_pit as pit
    from semiconductor_strategy.data import aiss_fetch_sec_data as sec
    from semiconductor_strategy.data import universe as U
except Exception:  # pragma: no cover
    import aiss_pit as pit          # type: ignore
    import aiss_fetch_sec_data as sec  # type: ignore
    import universe as U            # type: ignore

log = logging.getLogger("aiss.company")

CAPEX_PATH = pit.COMPANY_DIR / "capex_pulse.json"
MU_DIO_PATH = pit.COMPANY_DIR / "mu_dio_proxy.json"

CAPEX_TICKERS = U.CAPEX_PULSE_TICKERS          # [MSFT, GOOGL, META, AMZN]
CAPEX_LOOKBACK_DAYS = 63                        # ~3 trading months
CAPEX_ZSCORE_WINDOW = 504                       # ~24 trading months
CAPEX_HISTORY_START = "2015-01-01"             # gives z-scores from ~2017

MU_CIK = 723125
DIO_TIGHT = 100.0   # < -> +1 bullish memory
DIO_GLUT = 150.0    # > -> -1 bearish memory

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


def compute_mu_dio() -> dict:
    """Fetch MU XBRL, compute quarterly DIO + signal with PIT filed dates."""
    facts = sec.fetch_company_facts(MU_CIK)
    inv = sec.concept_series(facts, "InventoryNet", forms=["10-Q", "10-K"])
    inv_by_end = {p["end"]: p for p in inv}

    # COGS is a cumulative-duration flow — must NOT go through ``concept_series``
    # (end-keyed dedup silently drops the standalone quarter; see _standalone_quarters).
    # prefer_tagged=True: MU publishes explicit 90-day COGS facts and they agree
    # with decumulation to the euro (2026Q2 6,105mn both ways), but the tagged path
    # also repairs quarters the fiscal-year grouping drops — FY2010's ``fy_start``
    # resolves to a prior-year comparative Q1, which excludes end=2009-12-03 from
    # its own YTD chain.  Using the filer's own figure sidesteps that entirely.
    cogs_q: dict = {}
    for concept in ("CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"):
        cogs_q = _standalone_quarters(facts, concept, prefer_tagged=True)
        if cogs_q:
            n_tag = sum(1 for c in cogs_q.values() if c["source"] == "tagged")
            log.info("MU DIO using COGS concept: %s (%d quarters: %d tagged, %d decumulated)",
                     concept, len(cogs_q), n_tag, len(cogs_q) - n_tag)
            break

    records: dict = {}
    for end, c in cogs_q.items():
        inv_pt = inv_by_end.get(end)
        if inv_pt is None:
            continue
        inv_val = inv_pt.get("val")
        cogs_val = c.get("val")
        days = c.get("days") or 91
        if not (60 <= days <= 110):
            continue  # a decumulation artefact (gap/overlap in the YTD chain)
        if not inv_val or not cogs_val or cogs_val <= 0:
            continue
        dio = float(inv_val) / (float(cogs_val) / days)
        sig = 1 if dio < DIO_TIGHT else (-1 if dio > DIO_GLUT else 0)
        # PIT availability = later of the two filings
        filed = max((inv_pt.get("filed") or ""), (c.get("filed") or ""))
        fy, fp = c.get("fy"), c.get("fp")
        qkey = f"{fy}{fp}" if fy and fp else end
        records[qkey] = {
            "period_end": end,
            "filed_date": filed,
            "inventory_net_mn": round(float(inv_val) / 1e6, 1),
            "cogs_quarterly_mn": round(float(cogs_val) / 1e6, 1),
            "dio_days": round(dio, 1),
            "signal": sig,
            "fy": fy,
            "fp": fp,
        }
    return records


def update_mu_dio_proxy() -> int:
    pit.ensure_dirs()
    records = compute_mu_dio()
    if not records:
        log.error("MU DIO: no records computed")
        return 0
    payload = {
        "meta": {"cik": MU_CIK, "tight": DIO_TIGHT, "glut": DIO_GLUT,
                 "updated_at": date.today().isoformat(), "n": len(records)},
        "records": records,
    }
    pit.save_json(MU_DIO_PATH, payload)
    latest = sorted(records.values(), key=lambda r: r["period_end"])[-1]
    log.info("MU DIO: %d quarters; latest %s DIO=%.1f signal=%+d filed=%s",
             len(records), latest["period_end"], latest["dio_days"],
             latest["signal"], latest["filed_date"])
    return len(records)


def load_mu_dio(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """PIT-correct daily MU-DIO signal Series (+1/0/-1), ffilled from filed_date."""
    payload = pit.load_json(MU_DIO_PATH, default={})
    records = payload.get("records", {})
    if not records:
        return pd.Series(dtype="float64", name="mu_dio")
    avail = pit.pit_series(records, value_field="signal", date_field="filed_date")
    daily = pit.reindex_pit_daily(avail, start=start, end=end)
    daily.name = "mu_dio"
    return daily


def mu_dio_value_at(as_of_date) -> Optional[dict]:
    payload = pit.load_json(MU_DIO_PATH, default={})
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

    For each calendar quarter we sum the companies that reported a clean single
    quarter (require >=HYPERSCALER_MIN_COMPANIES), record the *availability* date
    as the latest ``filed`` among them (conservative / PIT-safe), and compute YoY
    vs the same quarter a year earlier.  Returns {frame: record}.
    """
    per_frame: dict = {}      # frame -> {ticker: {"val","filed","end"}}
    for tk, (cik, concept) in HYPERSCALER_CAPEX.items():
        try:
            facts = sec.fetch_company_facts(int(cik))
        except Exception as e:  # noqa: BLE001
            log.warning("Hyperscaler CapEx: %s (CIK %s) fetch failed: %s", tk, cik, e)
            continue
        for fr, rec in _raw_quarterly_capex(facts, concept).items():
            per_frame.setdefault(fr, {})[tk] = rec

    sums: dict = {}
    records: dict = {}
    for fr in sorted(per_frame):
        comp = per_frame[fr]
        if len(comp) < HYPERSCALER_MIN_COMPANIES:
            continue
        total = sum(c["val"] for c in comp.values())
        filed = max((c["filed"] or "") for c in comp.values())
        end = max((c["end"] or "") for c in comp.values())
        sums[fr] = total
        records[fr] = {
            "period_end": end,
            "filed_date": filed,            # PIT availability = last of the 4 filings
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


def update_hyperscaler_capex() -> int:
    pit.ensure_dirs()
    records = compute_hyperscaler_capex()
    if not records:
        log.error("Hyperscaler CapEx: no records computed")
        return 0
    payload = {
        "meta": {
            "ciks": {t: c for t, (c, _) in HYPERSCALER_CAPEX.items()},
            "min_companies": HYPERSCALER_MIN_COMPANIES,
            "updated_at": date.today().isoformat(),
            "n": len(records),
        },
        "records": records,
    }
    pit.save_json(HYPERSCALER_CAPEX_PATH, payload)
    latest = sorted(records.values(), key=lambda r: r["period_end"])[-1]
    log.info("Hyperscaler CapEx: %d quarters; latest %s sum=$%.1fB YoY=%s filed=%s",
             len(records), latest["period_end"], latest["capex_usd_mn"] / 1e3,
             latest["capex_yoy_pct"], latest["filed_date"])
    return len(records)


def load_hyperscaler_capex_yoy(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """PIT-correct daily series of aggregate hyperscaler CapEx YoY% (ffilled from filed)."""
    payload = pit.load_json(HYPERSCALER_CAPEX_PATH, default={})
    records = {k: v for k, v in payload.get("records", {}).items()
               if v.get("capex_yoy_pct") is not None}
    if not records:
        return pd.Series(dtype="float64", name="hyperscaler_capex_yoy")
    avail = pit.pit_series(records, value_field="capex_yoy_pct", date_field="filed_date")
    daily = pit.reindex_pit_daily(avail, start=start, end=end)
    daily.name = "hyperscaler_capex_yoy"
    return daily


def hyperscaler_capex_value_at(as_of_date) -> Optional[dict]:
    payload = pit.load_json(HYPERSCALER_CAPEX_PATH, default={})
    return pit.get_latest_available(payload.get("records", {}), as_of_date, "filed_date")


# ===========================================================================
# Snapshot (industry + company merged) — used by smart_select / reports
# ===========================================================================

def get_aiss_signals_snapshot(as_of_date) -> dict:
    """Return all slow + derived AISS signals available as of ``as_of_date``."""
    snap: dict = {}
    cap = load_capex_pulse(end=str(as_of_date))
    snap["capex_pulse_zscore"] = float(cap.iloc[-1]) if len(cap) else None
    mu = mu_dio_value_at(as_of_date)
    snap["mu_dio_signal"] = (mu or {}).get("signal")
    snap["mu_dio_days"] = (mu or {}).get("dio_days")
    hc = hyperscaler_capex_value_at(as_of_date)
    snap["hyperscaler_capex_usd_mn"] = (hc or {}).get("capex_usd_mn")
    snap["hyperscaler_capex_yoy_pct"] = (hc or {}).get("capex_yoy_pct")
    # industry layer (imported lazily to avoid a hard cycle)
    try:
        from semiconductor_strategy.data import industry_signals as ind
    except Exception:  # pragma: no cover
        import industry_signals as ind  # type: ignore
    tsmc = ind.tsmc_value_at(as_of_date)
    snap["tsmc_yoy_latest"] = (tsmc or {}).get("yoy_pct")
    asml = ind.asml_value_at(as_of_date)
    snap["asml_orders_latest"] = (asml or {}).get("net_bookings_eur_bn")
    # bookings 已停更(冻在 2026Q1);接续的前瞻量是下季度净销售指引中值
    guid = ind.asml_guidance_value_at(as_of_date)
    snap["asml_guidance_eur_bn"] = (guid or {}).get("mid_eur_bn")
    snap["asml_guidance_quarter"] = (guid or {}).get("quarter")
    dram = ind.load_dram_proxy(end=str(as_of_date))
    snap["dram_signal"] = float(dram.iloc[-1]) if len(dram) else None
    return snap


def verify() -> bool:
    """存在性 + **时效性** 双检。

    2026-08-27 加时效检查:此前只查 `if len(x)`,于是 capex_pulse 冻在 2026-06-04
    整整 57 个交易日,每周 weekly 照打 `RESULT: OK`。阈值与语义见 aiss_pit.staleness()。
    """
    cap = load_capex_pulse()
    mu = load_mu_dio()
    print("=" * 70)
    print("AISS COMPANY-LAYER SIGNALS")
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
    payload = pit.load_json(MU_DIO_PATH, default={})
    recs = payload.get("records", {})
    if recs:
        latest = sorted(recs.values(), key=lambda r: r["period_end"])[-1]
        # 季频:基准取 filed_date(真正可用那天),不取 period_end —— 后者天生晚 90 天
        tag = pit.stale_tag(latest["filed_date"], "quarterly")
        print(f"  mu_dio      : {len(recs):5} quarters, latest {latest['period_end']} "
              f"DIO={latest['dio_days']} sig={latest['signal']:+d} filed={latest['filed_date']}{tag}")
        if tag:
            ok = False
    else:
        print("  mu_dio      : MISSING"); ok = False
    hpayload = pit.load_json(HYPERSCALER_CAPEX_PATH, default={})
    hrecs = hpayload.get("records", {})
    if hrecs:
        latest = sorted(hrecs.values(), key=lambda r: r["period_end"])[-1]
        first = sorted(hrecs.values(), key=lambda r: r["period_end"])[0]
        tag = pit.stale_tag(latest["filed_date"], "quarterly")
        print(f"  hyperscaler_capex: {len(hrecs):3} quarters {first['period_end']}→{latest['period_end']}, "
              f"latest sum=${latest['capex_usd_mn']/1e3:.1f}B YoY={latest['capex_yoy_pct']}% "
              f"filed={latest['filed_date']}{tag}")
        if tag:
            ok = False
    else:
        print("  hyperscaler_capex: MISSING (run --update-hyperscaler-capex)")  # not fatal for V1
    print("=" * 70)
    print("RESULT:", "OK" if ok else "STALE/INCOMPLETE")
    return ok


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="AISS company-layer signals")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--update-capex", action="store_true")
    ap.add_argument("--check-mu-dio", action="store_true")
    ap.add_argument("--update-hyperscaler-capex", "--init-hyperscaler-capex",
                    dest="hyperscaler_capex", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    did = False
    if args.init or args.update_capex:
        update_capex_pulse(end_date=args.end); did = True
    if args.init or args.check_mu_dio:
        update_mu_dio_proxy(); did = True
    if args.init or args.hyperscaler_capex:
        update_hyperscaler_capex(); did = True
    _ok = True
    if args.verify or did:
        _ok = verify()
    if not did and not args.verify:
        print("Nothing to do. Use --init / --update-capex / --check-mu-dio / "
              "--update-hyperscaler-capex / --verify.")
    # 退出码只在**显式** --verify 时才反映体检结果。
    # 为什么不在 update 之后也退非零: update_data 是每日跑的,而 ASML(2026 起停止
    # 披露季度 bookings)之类结构性缺口会让它天天红,红久了就没人看 —— 正是本次要
    # 修的病理本身。weekly 走的是显式 `--verify`(semiconductor_pipeline.sh),
    # 那条路径必须炸。
    if args.verify and not _ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
