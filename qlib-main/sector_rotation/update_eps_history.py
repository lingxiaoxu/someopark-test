"""
update_eps_history.py — Incremental maintenance of sector ETF constituent EPS history.

Uses Polygon /vX/reference/financials (same endpoint as MRPTFetchEarnings.py).
Output: price_data/sector_etfs/eps_history.json

Store format:
{
  "fetched_at": "2026-04-24",           # last full run date
  "symbol_meta": {                       # per-symbol fetch tracking
    "XOM": {
      "last_fetched":    "2026-04-24",   # when we last called Polygon for this stock
      "newest_end_date": "2025-12-31"    # newest quarter end date in store
    }, ...
  },
  "symbols": {
    "XOM": [
      {"end_date": "2009-03-31", "eps": 0.43},
      ...
    ], ...
  }
}

Incremental logic:
  - On each run, compare today vs symbol_meta[sym]["last_fetched"]
  - If within REFRESH_DAYS: skip (no Polygon call)
  - Otherwise: fetch ONLY quarters after newest_end_date using
    period_of_report_date.gte filter → merge into existing store
  - This means: first run is a full fetch; daily/weekly runs are tiny incremental fetches

Usage:
    # First run or incremental update (skips symbols fetched recently):
    set -a && source .env && set +a
    conda run -n qlib_run --no-capture-output \\
        python qlib-main/sector_rotation/update_eps_history.py

    # Force full re-fetch for specific symbols:
    python qlib-main/sector_rotation/update_eps_history.py XOM CVX --force

    # Force full re-fetch for ALL symbols:
    python qlib-main/sector_rotation/update_eps_history.py --force

    # Fetch specific symbols only (incremental):
    python qlib-main/sector_rotation/update_eps_history.py AAPL MSFT NVDA

Recommended schedule: run daily during earnings season; weekly otherwise.
The script is idempotent and very cheap after first run.
"""

import json
import os
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# ── Path setup ───────────────────────────────────────────────────────────────
_THIS_DIR    = Path(__file__).parent.resolve()           # qlib-main/sector_rotation/
_PROJECT_DIR = _THIS_DIR.parent.parent.resolve()         # someopark-test/

sys.path.insert(0, str(_THIS_DIR.parent))  # sector_rotation.* imports

from sector_rotation.signals.value import SECTOR_REPRESENTATIVES

# All unique constituent stocks (55 stocks across 11 sectors)
ALL_STOCKS: list[str] = list(dict.fromkeys(
    s for stocks in SECTOR_REPRESENTATIVES.values() for s in stocks
))

POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "")
STORE_PATH = _PROJECT_DIR / "price_data" / "sector_etfs" / "eps_history.json"

# Only re-fetch if last_fetched is older than this many days
REFRESH_DAYS = 7


# ── Polygon fetch helpers ─────────────────────────────────────────────────────

def _curl_get(url: str) -> dict | None:
    result = subprocess.run(
        ["curl", "-s", "--max-time", "30", url],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _parse_results(results: list) -> list[dict]:
    """Extract {end_date, eps} from Polygon financials results, deduplicating."""
    entries = []
    seen: set[str] = set()
    for r in results:
        end_date = r.get("end_date")
        if not end_date or end_date in seen:
            continue
        inc = r.get("financials", {}).get("income_statement", {})
        eps = (
            (inc.get("diluted_earnings_per_share") or {}).get("value")
            or (inc.get("basic_earnings_per_share") or {}).get("value")
        )
        if eps is not None:
            entries.append({"end_date": end_date, "eps": float(eps)})
            seen.add(end_date)
    return sorted(entries, key=lambda x: x["end_date"])


def fetch_eps_full(symbol: str, max_pages: int = 6) -> list[dict]:
    """
    Full historical fetch: all available quarterly EPS from Polygon (paginated).
    Used on first run or when --force is specified.
    """
    url = (
        f"https://api.polygon.io/vX/reference/financials"
        f"?ticker={symbol}&timeframe=quarterly&limit=40"
        f"&sort=period_of_report_date&order=desc"
        f"&apiKey={POLYGON_API_KEY}"
    )
    all_results = []
    pages = 0
    while url and pages < max_pages:
        data = _curl_get(url)
        if not data:
            break
        all_results.extend(data.get("results", []))
        pages += 1
        next_url = data.get("next_url")
        url = (next_url + f"&apiKey={POLYGON_API_KEY}") if next_url else None
        time.sleep(0.15)
    return _parse_results(all_results)


def fetch_eps_incremental(symbol: str, since_date: str) -> list[dict]:
    """
    Incremental fetch: only quarters with end_date >= since_date.
    Uses period_of_report_date.gte to minimize API calls and data transfer.
    since_date: YYYY-MM-DD (typically the newest end_date already in store)
    """
    url = (
        f"https://api.polygon.io/vX/reference/financials"
        f"?ticker={symbol}&timeframe=quarterly&limit=10"
        f"&period_of_report_date.gte={since_date}"
        f"&sort=period_of_report_date&order=asc"
        f"&apiKey={POLYGON_API_KEY}"
    )
    data = _curl_get(url)
    if not data:
        return []
    return _parse_results(data.get("results", []))


# ── Store helpers ─────────────────────────────────────────────────────────────

def load_store() -> dict:
    if STORE_PATH.exists():
        with open(STORE_PATH) as f:
            d = json.load(f)
        # Migrate old format (no symbol_meta) → add symbol_meta
        if "symbol_meta" not in d:
            d["symbol_meta"] = {}
            for sym, quarters in d.get("symbols", {}).items():
                newest = max((e["end_date"] for e in quarters), default="")
                # Mark as never properly fetched so first run triggers incremental
                d["symbol_meta"][sym] = {
                    "last_fetched":    d.get("fetched_at", ""),
                    "newest_end_date": newest,
                }
        return d
    return {"fetched_at": "", "symbol_meta": {}, "symbols": {}}


def save_store(store: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STORE_PATH, "w") as f:
        json.dump(store, f, indent=2)


def _needs_refresh(store: dict, symbol: str, force: bool) -> bool:
    """
    Returns True if the symbol should be re-fetched from Polygon.

    Decision based on last_fetched date (not newest quarter end date).
    This avoids re-fetching when the newest quarter is old (e.g., Q4 2025)
    but we already fetched it last week.
    """
    if force:
        return True
    if symbol not in store.get("symbols", {}):
        return True                          # never fetched
    meta = store.get("symbol_meta", {}).get(symbol, {})
    last_fetched_str = meta.get("last_fetched", "")
    if not last_fetched_str:
        return True                          # no fetch record
    try:
        last_fetched = date.fromisoformat(last_fetched_str)
        return (date.today() - last_fetched).days > REFRESH_DAYS
    except ValueError:
        return True


def _merge_entries(existing: list[dict], new_entries: list[dict]) -> list[dict]:
    """
    Merge new_entries into existing list.
    New entries override existing ones at the same end_date (handles restatements).
    Returns sorted list by end_date.
    """
    by_date = {e["end_date"]: e for e in existing}
    for e in new_entries:
        by_date[e["end_date"]] = e      # override with latest value
    return sorted(by_date.values(), key=lambda x: x["end_date"])


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Incrementally update sector ETF constituent EPS history from Polygon"
    )
    parser.add_argument("symbols", nargs="*",
                        help="Symbols to update (default: all 55 constituents)")
    parser.add_argument("--force", action="store_true",
                        help="Full re-fetch even if recently updated (ignores last_fetched)")
    args = parser.parse_args()

    if not POLYGON_API_KEY:
        print("ERROR: POLYGON_API_KEY not in environment. Run: set -a && source .env && set +a")
        sys.exit(1)

    symbols = [s.upper() for s in args.symbols] if args.symbols else ALL_STOCKS
    store = load_store()
    store.setdefault("symbol_meta", {})
    store.setdefault("symbols", {})

    print(f"EPS history store : {STORE_PATH}")
    print(f"Symbols to check  : {len(symbols)}  (REFRESH_DAYS={REFRESH_DAYS}, force={args.force})")
    print()

    today_str = date.today().isoformat()
    updated = added = skipped = 0

    for i, sym in enumerate(symbols):
        if not _needs_refresh(store, sym, args.force):
            meta = store["symbol_meta"].get(sym, {})
            print(
                f"[{i+1:2d}/{len(symbols)}] {sym:8s}  SKIP "
                f"(last_fetched={meta.get('last_fetched','?')}, "
                f"newest={meta.get('newest_end_date','?')})"
            )
            skipped += 1
            continue

        existing = store["symbols"].get(sym, [])
        meta = store.get("symbol_meta", {}).get(sym, {})
        newest_in_store = meta.get("newest_end_date", "")

        if existing and newest_in_store and not args.force:
            # Incremental: only fetch quarters on or after newest known date
            # (include the boundary to catch restatements)
            print(f"[{i+1:2d}/{len(symbols)}] {sym:8s}  incremental since {newest_in_store}...", end="", flush=True)
            new_entries = fetch_eps_incremental(sym, since_date=newest_in_store)
            if new_entries:
                merged = _merge_entries(existing, new_entries)
                truly_new = len(merged) - len(existing)
                store["symbols"][sym] = merged
                newest_now = merged[-1]["end_date"]
                print(f"  +{truly_new} new quarters  (newest: {newest_now})")
            else:
                merged = existing
                newest_now = newest_in_store
                print(f"  no new quarters")
            added += len(new_entries)
        else:
            # Full fetch: first run or --force
            print(f"[{i+1:2d}/{len(symbols)}] {sym:8s}  full fetch...", end="", flush=True)
            entries = fetch_eps_full(sym)
            if entries:
                store["symbols"][sym] = entries
                oldest = entries[0]["end_date"]
                newest_now = entries[-1]["end_date"]
                print(f"  {len(entries)} quarters  ({oldest} → {newest_now})")
                updated += 1
            else:
                print(f"  WARNING: no data from Polygon")
                newest_now = newest_in_store

        # Update per-symbol metadata
        store["symbol_meta"][sym] = {
            "last_fetched":    today_str,
            "newest_end_date": newest_now or newest_in_store,
        }
        time.sleep(0.2)

    store["fetched_at"] = today_str
    save_store(store)

    total_syms     = len(store["symbols"])
    total_quarters = sum(len(v) for v in store["symbols"].values())
    print()
    print(f"Done.  Full-fetch={updated}  Incremental={added} new quarters  Skipped={skipped}")
    print(f"Store: {total_syms} symbols, {total_quarters} total quarters")
    print(f"Saved → {STORE_PATH}")


if __name__ == "__main__":
    main()
