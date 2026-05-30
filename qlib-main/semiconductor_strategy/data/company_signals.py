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


def update_capex_pulse(end_date: Optional[str] = None, start: str = CAPEX_HISTORY_START) -> int:
    """Recompute the full CapEx-pulse z-score series and persist it."""
    pit.ensure_dirs()
    close = _download_capex_prices(start, end_date)
    if close.empty:
        log.error("CapEx pulse: no hyperscaler prices downloaded")
        return 0
    z = compute_capex_pulse(close)
    payload = {
        "meta": {
            "tickers": CAPEX_TICKERS,
            "lookback_days": CAPEX_LOOKBACK_DAYS,
            "zscore_window": CAPEX_ZSCORE_WINDOW,
            "updated_at": date.today().isoformat(),
            "n": int(len(z)),
            "last_date": z.index[-1].date().isoformat() if len(z) else None,
            "last_value": float(z.iloc[-1]) if len(z) else None,
        },
        "series": {d.date().isoformat(): float(v) for d, v in z.items()},
    }
    pit.save_json(CAPEX_PATH, payload)
    log.info("CapEx pulse: %d points %s→%s (last z=%.2f)", len(z),
             z.index[0].date(), z.index[-1].date(), z.iloc[-1])
    return len(z)


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

def _quarterly_duration_points(points: list) -> list:
    """Keep only single-quarter (~80-100 day) duration facts (drop YTD/annual)."""
    out = []
    for p in points:
        s, e = p.get("start"), p.get("end")
        if not s or not e:
            continue
        try:
            days = (date.fromisoformat(e) - date.fromisoformat(s)).days
        except Exception:
            continue
        if 80 <= days <= 100:
            p = dict(p)
            p["_days"] = days
            out.append(p)
    return out


def compute_mu_dio() -> dict:
    """Fetch MU XBRL, compute quarterly DIO + signal with PIT filed dates."""
    facts = sec.fetch_company_facts(MU_CIK)
    inv = sec.concept_series(facts, "InventoryNet", forms=["10-Q", "10-K"])
    inv_by_end = {p["end"]: p for p in inv}

    cogs = []
    for concept in ("CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"):
        cogs = sec.concept_series(facts, concept, forms=["10-Q", "10-K"])
        cogs = _quarterly_duration_points(cogs)
        if cogs:
            log.info("MU DIO using COGS concept: %s (%d quarterly pts)", concept, len(cogs))
            break

    records: dict = {}
    for c in cogs:
        end = c["end"]
        inv_pt = inv_by_end.get(end)
        if inv_pt is None:
            continue
        inv_val = inv_pt.get("val")
        cogs_val = c.get("val")
        days = c.get("_days", 91)
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
    # industry layer (imported lazily to avoid a hard cycle)
    try:
        from semiconductor_strategy.data import industry_signals as ind
    except Exception:  # pragma: no cover
        import industry_signals as ind  # type: ignore
    tsmc = ind.tsmc_value_at(as_of_date)
    snap["tsmc_yoy_latest"] = (tsmc or {}).get("yoy_pct")
    asml = ind.asml_value_at(as_of_date)
    snap["asml_orders_latest"] = (asml or {}).get("net_bookings_eur_bn")
    dram = ind.load_dram_proxy(end=str(as_of_date))
    snap["dram_signal"] = float(dram.iloc[-1]) if len(dram) else None
    return snap


def verify() -> bool:
    cap = load_capex_pulse()
    mu = load_mu_dio()
    print("=" * 70)
    print("AISS COMPANY-LAYER SIGNALS")
    print("=" * 70)
    ok = True
    if len(cap):
        print(f"  capex_pulse : {len(cap):5} pts {cap.index[0].date()}→{cap.index[-1].date()} "
              f"last z={cap.iloc[-1]:+.2f}")
    else:
        print("  capex_pulse : MISSING"); ok = False
    payload = pit.load_json(MU_DIO_PATH, default={})
    recs = payload.get("records", {})
    if recs:
        latest = sorted(recs.values(), key=lambda r: r["period_end"])[-1]
        print(f"  mu_dio      : {len(recs):5} quarters, latest {latest['period_end']} "
              f"DIO={latest['dio_days']} sig={latest['signal']:+d} filed={latest['filed_date']}")
    else:
        print("  mu_dio      : MISSING"); ok = False
    print("=" * 70)
    print("RESULT:", "OK" if ok else "INCOMPLETE")
    return ok


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="AISS company-layer signals")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--update-capex", action="store_true")
    ap.add_argument("--check-mu-dio", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    did = False
    if args.init or args.update_capex:
        update_capex_pulse(end_date=args.end); did = True
    if args.init or args.check_mu_dio:
        update_mu_dio_proxy(); did = True
    if args.verify or did:
        verify()
    if not did and not args.verify:
        print("Nothing to do. Use --init / --update-capex / --check-mu-dio / --verify.")


if __name__ == "__main__":
    main()
