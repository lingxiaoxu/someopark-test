#!/usr/bin/env python3
"""
SyncPrivateCreditRates.py — D3: unify the private-credit module's rate feed onto the
project's single FRED access point (MacroStateStore).

Replaces portfolio_of_private_credit_deals/download_fred_data.py (now DEPRECATED): the
8 rate/spread series the module needs are maintained ONCE in MacroStateStore
(price_data/macro/state/fred/) alongside every other strategy's FRED data, with one
FRED_API_KEY. This adapter reads them from the store and rewrites the module's
fred_rates.csv in its EXACT legacy format (same index, columns, decimal scaling) — so
ForwardRateLookup / Nelson-Siegel / stressed-rate logic keep consuming it unchanged.

Idempotent + incremental: ensures each series is fresh (fetches only the gap), then
rewrites the CSV. Run daily as the first step of the BDC pipeline.

Env: someopark_run, FRED_API_KEY from .env. Use --sandbox to point at a throwaway store.

Usage:
    python SyncPrivateCreditRates.py                    # production store -> fred_rates.csv
    python SyncPrivateCreditRates.py --sandbox /tmp/x   # sandbox store + sandbox csv
    python SyncPrivateCreditRates.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

import pandas as pd

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
from MacroStateStore import MacroStateStore, FRED_SERIES  # noqa: E402

MODULE_DIR = os.path.join(_ROOT, "portfolio_of_private_credit_deals")
FRED_RATES_CSV = os.path.join(MODULE_DIR, "fred_rates.csv")
BACKFILL_START = date(2015, 1, 1)        # SOFR starts 2018; DGS deeper — 2015 is ample

# store series name -> fred_rates.csv column header (EXACT legacy header + order)
RATE_MAP = [
    ("sofr",      "SOFR"),
    ("effr",      "EFFR"),
    ("dgs2",      "DGS2"),
    ("dgs10",     "DGS10"),
    ("dgs5",      "DGS5"),
    ("dgs30",     "DGS30"),
    ("ig_spread", "BAMLC0A0CM"),
    ("hy_spread", "BAMLH0A0HYM2"),
]


def _alert(msg: str) -> None:
    banner = "!" * 70
    for stream in (sys.stderr, sys.stdout):
        print(f"\n{banner}\n[PC_RATES ALERT] {msg}\n{banner}", file=stream)


def ensure_fresh(store: MacroStateStore, names, today: date) -> None:
    """Fetch only the missing tail for each series (incremental, idempotent)."""
    for name in names:
        series_id = FRED_SERIES[name][0]
        existing = store._load_fred_series(name, BACKFILL_START.year, today.year)
        if existing.empty:
            fetch_start = BACKFILL_START
        else:
            fetch_start = existing.index.max().date() + pd.Timedelta(days=1)
        if fetch_start > today:
            continue
        try:
            s = store._fetch_fred(series_id, fetch_start, today)
        except Exception as e:  # noqa: BLE001
            _alert(f"FRED fetch {name}({series_id}) failed: {e!r} — keeping stored history")
            continue
        if not s.empty:
            store._append_fred(name, s)
            print(f"  {name}: +{len(s)} rows (to {s.index.max().date()})")
        else:
            print(f"  {name}: up-to-date")


def build_rates_frame(store: MacroStateStore, today: date) -> pd.DataFrame:
    """Read the 8 series from the store, scale FRED percent -> decimal, exact columns."""
    cols = {}
    for name, header in RATE_MAP:
        s = store._load_fred_series(name, BACKFILL_START.year, today.year)
        cols[header] = (s / 100.0) if not s.empty else pd.Series(dtype=float)
    df = pd.DataFrame(cols)
    df = df[[h for _, h in RATE_MAP]]                 # enforce legacy column order
    df.index = pd.to_datetime(df.index)
    return df.sort_index().round(6)


def sync(base_dir=None, out_csv: str = FRED_RATES_CSV, write: bool = True) -> pd.DataFrame:
    store = MacroStateStore(base_dir) if base_dir else MacroStateStore()
    today = date.today()
    names = [n for n, _ in RATE_MAP]
    print(f"[PC_RATES] ensuring {len(names)} series fresh in store={store.base_dir}")
    ensure_fresh(store, names, today)
    df = build_rates_frame(store, today)
    latest = {h: (df[h].dropna().index.max().date().isoformat() if df[h].notna().any() else None)
              for h in df.columns}
    print(f"[PC_RATES] rows={len(df)}  span={df.index.min().date()}→{df.index.max().date()}")
    print(f"[PC_RATES] per-column latest available: {latest}")
    if write:
        os.makedirs(os.path.dirname(out_csv), exist_ok=True)
        df.to_csv(out_csv)
        print(f"[PC_RATES] wrote {out_csv}")
    else:
        print("[PC_RATES] dry-run: nothing written")
    return df


def main():
    ap = argparse.ArgumentParser(description="Sync private-credit fred_rates.csv from MacroStateStore")
    ap.add_argument("--sandbox", metavar="DIR", help="use a sandbox store + write csv there")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.sandbox:
        sync(base_dir=os.path.join(a.sandbox, "macro_state"),
             out_csv=os.path.join(a.sandbox, "fred_rates.csv"), write=not a.dry_run)
    else:
        sync(write=not a.dry_run)


if __name__ == "__main__":
    main()
