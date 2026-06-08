"""
RefreshEventRiskData.py — daily data-maintenance for the event-risk overlay.

Single owner of the shared event-risk data (run in qlib_run, BEFORE the AISS /
MRPT-MTFS daily strategy runs).  Both detectors then just READ the refreshed data
(MTFS reads the parquet store directly; no cross-env fetch).

Refreshes, idempotently:
  1. Prices  — incremental update of the event_risk parquet store
               (aiss_fetch_prices.update_all; fetch_start = last-7d, dedup keep-last).
  2. NFP     — re-pull FRED release 50 (Employment Situation) ONLY if the current
               calendar does not already extend >= 60 days into the future.
  3. Bellwether — NVDA/AVGO forward earnings calendar (FetchBellwetherEarnings).
  4. Dead-ticker sentinel — flag any ticker lagging the store's latest date by
               > STALE_DAYS calendar days (e.g. a delisting like JNPR) and ALERT.

Env: qlib_run.  Keys: POLYGON_API_KEY (prices), FRED_API_KEY (NFP) from .env.

Usage:
    python RefreshEventRiskData.py            # full daily refresh
    python RefreshEventRiskData.py --prices-only
"""

import os
import sys
import json
import glob
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_ROOT, "qlib-main"))

UNIVERSE_PATH = os.path.join(_ROOT, "price_data", "semiconductor_universe.json")
EVENT_STORE = os.path.join(_ROOT, "price_data", "event_risk", "prices")
NFP_DIR = os.path.join(_ROOT, "price_data", "macro", "nfp")
BENCHMARKS = ["SMH", "SPY", "SOXX"]
STALE_DAYS = 5          # ticker lagging store-latest by more than this -> sentinel alert
NFP_HORIZON_DAYS = 60   # ensure NFP calendar extends at least this far ahead


def _alert(msg: str) -> None:
    banner = "!" * 70
    for stream in (sys.stderr, sys.stdout):
        print(f"\n{banner}\n[EVENT_RISK_DATA ALERT] {msg}\n{banner}", file=stream)


def load_universe() -> list:
    d = json.load(open(UNIVERSE_PATH))
    tks = sorted(set(d.get("tier1", []) + d.get("tier2", []) + BENCHMARKS))
    return tks


# ── 1. Prices (incremental) + 4. dead-ticker sentinel ──────────────────────────
def refresh_prices(tickers: list) -> dict:
    from pathlib import Path
    import pandas as pd
    from semiconductor_strategy.data import aiss_fetch_prices as fp

    today = date.today().isoformat()
    res = fp.update_all(tickers=tickers, end=today, prices_dir=Path(EVENT_STORE))
    ok = [t for t, v in res.items() if v]
    bad = [t for t, v in res.items() if not v]

    # sentinel: compare each ticker's last date to the store's latest
    last_dates = {}
    for f in glob.glob(os.path.join(EVENT_STORE, "*.parquet")):
        t = os.path.basename(f).replace("_prices.parquet", "")
        try:
            idx = pd.to_datetime(pd.read_parquet(f, columns=[]).index)
            last_dates[t] = idx.max().date()
        except Exception:
            continue
    store_latest = max(last_dates.values()) if last_dates else None
    stale = []
    if store_latest:
        for t, ld in last_dates.items():
            if (store_latest - ld).days > STALE_DAYS:
                stale.append((t, ld.isoformat()))
    if bad:
        _alert(f"price update failed for {len(bad)}: {bad}")
    if stale:
        _alert("STALE/DEAD tickers (lagging store-latest > "
               f"{STALE_DAYS}d — likely delisted/M&A, prune from universe): {stale}")
    print(f"  prices: ok={len(ok)} failed={len(bad)} stale={len(stale)} "
          f"store_latest={store_latest}")
    return {"ok": len(ok), "failed": bad, "stale": stale, "store_latest": str(store_latest)}


# ── 2. NFP calendar (conditional FRED re-pull) ─────────────────────────────────
def _nfp_max_date() -> str:
    mx = ""
    for f in glob.glob(os.path.join(NFP_DIR, "nfp_*.json")):
        for d in json.load(open(f)):
            mx = max(mx, d)
    return mx


def refresh_nfp() -> dict:
    cur_max = _nfp_max_date()
    horizon = (date.today() + timedelta(days=NFP_HORIZON_DAYS)).isoformat()
    if cur_max >= horizon:
        print(f"  nfp: skip (covered to {cur_max} >= {horizon})")
        return {"status": "skip", "max": cur_max}
    key = os.environ.get("FRED_API_KEY")
    if not key:
        _alert("NFP refresh needed but FRED_API_KEY missing — calendar may go stale.")
        return {"status": "no_key", "max": cur_max}
    try:
        end = (date.today() + timedelta(days=540)).isoformat()
        u = (f"https://api.stlouisfed.org/fred/release/dates?release_id=50"
             f"&realtime_start=2022-01-01&realtime_end={end}"
             f"&include_release_dates_with_no_data=true&sort_order=asc"
             f"&api_key={key}&file_type=json")
        dates = [x["date"] for x in json.load(urllib.request.urlopen(u))["release_dates"]]
        byyr = defaultdict(list)
        for d in sorted(set(dates)):
            byyr[d[:4]].append(d)
        os.makedirs(NFP_DIR, exist_ok=True)
        for y, ds in byyr.items():
            json.dump(sorted(ds), open(os.path.join(NFP_DIR, f"nfp_{y}.json"), "w"), indent=2)
        print(f"  nfp: refreshed FRED release 50 -> max {max(dates)}")
        return {"status": "ok", "max": max(dates)}
    except Exception as e:  # noqa: BLE001
        _alert(f"NFP FRED re-pull failed: {e!r} — kept existing calendar.")
        return {"status": "error", "max": cur_max}


# ── 3. Bellwether forward calendar ─────────────────────────────────────────────
def refresh_bellwether() -> dict:
    try:
        from FetchBellwetherEarnings import update_bellwether_calendar
        return update_bellwether_calendar(quiet=True)
    except Exception as e:  # noqa: BLE001
        _alert(f"bellwether refresh failed: {e!r}")
        return {"status": "error"}


def main(prices_only: bool = False) -> dict:
    print(f"=== RefreshEventRiskData @ {datetime.now().isoformat(timespec='seconds')} ===")
    tickers = load_universe()
    print(f"  universe: {len(tickers)} tickers (incl benchmarks)")
    out = {"prices": refresh_prices(tickers)}
    if not prices_only:
        out["nfp"] = refresh_nfp()
        out["bellwether"] = refresh_bellwether()
    print("=== done ===")
    return out


if __name__ == "__main__":
    main(prices_only="--prices-only" in sys.argv)
