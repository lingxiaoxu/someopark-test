"""
MRPTFetchEarnings.py — Fetch and cache quarterly earnings dates for tracked symbols

Uses Polygon vX/reference/financials endpoint.
Output: price_data/earnings_cache.json

Modes:
  --full          Fetch ALL S&P 500 + SECTOR_MAP tickers (~621 symbols, ~3 min)
  --incremental   Only fetch symbols whose latest cached earnings_date is within
                  30 days of today (likely to have new filings soon)
  (no flag)       Fetch only active MRPT + MTFS pair universe tickers (~48 symbols)
  LYFT GS ...     Fetch specific symbols only

Format:
{
  "fetched_at": "2026-05-23",
  "symbols": {
    "LYFT": [
      {"fiscal_period": "Q3 2025", "end_date": "2025-09-30",
       "acceptance_datetime": "2025-11-05T22:20:44Z",
       "filing_date": "2025-11-05",
       "earnings_date": "2025-11-05",        # date market reacts
       "release_timing": "AMC"},             # BMO/AMC/INTRADAY/UNKNOWN
    ],
    ...
  }
}

Usage:
    python MRPTFetchEarnings.py                # active pairs only
    python MRPTFetchEarnings.py --full         # full S&P 500 (~621 tickers)
    python MRPTFetchEarnings.py --incremental  # only symbols with upcoming earnings
    python MRPTFetchEarnings.py LYFT GS        # specific symbols
"""

import sys
import os
import json
import time
import random
import logging
import subprocess
from datetime import datetime, timedelta, date

log = logging.getLogger(__name__)

POLYGON_API_KEY = os.environ['POLYGON_API_KEY']
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'price_data', 'earnings_cache.json')


def _curl_get(url):
    """Fetch URL via curl (avoids Python SSL issues with Polygon)."""
    result = subprocess.run(
        ['curl', '-s', '--max-time', '30', url],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _is_after_market(acceptance_dt_str):
    """
    Determine if earnings were released after market close (AMC) or before open (BMO).
    Returns: ('AMC'|'BMO'|'INTRADAY'|'UNKNOWN', reaction_date or None)
    """
    if not acceptance_dt_str:
        return 'UNKNOWN', None
    try:
        dt = datetime.fromisoformat(acceptance_dt_str.replace('Z', '+00:00'))
        utc_hour = dt.hour
        filing_date = dt.date()
        if utc_hour >= 20:  # after 3pm UTC → after market close ET
            return 'AMC', filing_date + timedelta(days=1)
        elif utc_hour <= 13:  # before 8am UTC → before market open ET
            return 'BMO', filing_date
        else:
            return 'INTRADAY', filing_date
    except Exception:
        return 'UNKNOWN', None


def _parse_filings(results):
    """Parse raw Polygon financials results into earnings entry dicts."""
    entries = []
    seen_dates = set()
    for r in results:
        acceptance = r.get('acceptance_datetime', '')
        filing     = r.get('filing_date', '')
        fiscal_p   = r.get('fiscal_period', '')
        fiscal_y   = r.get('fiscal_year', '')
        end_date   = r.get('end_date', '')

        if not acceptance and not filing:
            continue

        timing, reaction_date = _is_after_market(acceptance)

        if reaction_date is None and filing:
            reaction_date = datetime.strptime(filing, '%Y-%m-%d').date() + timedelta(days=1)

        if reaction_date is None:
            continue

        key = reaction_date.isoformat()
        if key in seen_dates:
            continue
        seen_dates.add(key)

        entries.append({
            'fiscal_period':        f"{fiscal_p} {fiscal_y}",
            'end_date':             end_date,
            'acceptance_datetime':  acceptance,
            'filing_date':          filing,
            'release_timing':       timing,
            'earnings_date':        key,
        })
    return entries


def fetch_earnings_for_symbol(symbol, limit=20):
    """
    Fetch quarterly + annual earnings dates for one symbol from Polygon.
    Returns list of dicts sorted by earnings_date ascending, deduplicated.
    """
    all_results = []
    for timeframe in ('quarterly', 'annual'):
        url = (f"https://api.polygon.io/vX/reference/financials"
               f"?ticker={symbol}&timeframe={timeframe}&limit={limit}"
               f"&apiKey={POLYGON_API_KEY}")
        data = _curl_get(url)
        if data and 'results' in data:
            all_results.extend(data['results'])
        time.sleep(random.uniform(0.3, 0.8))

    if not all_results:
        return []

    entries = _parse_filings(all_results)
    entries.sort(key=lambda x: x['earnings_date'])
    return entries


def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {'fetched_at': '', 'symbols': {}}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, 'w') as f:
        json.dump(cache, f, indent=2)


def _get_incremental_symbols(cache, refresh_interval_days=3):
    """Find symbols that need an earnings data refresh.

    Two-stage check:
      Stage 1: If cache was updated less than refresh_interval_days ago → skip entirely.
               (Full fetch was recent enough, no point re-querying Polygon.)
      Stage 2: If cache is older, find symbols where:
        a. No cached data at all (new to universe)
        b. Latest cached earnings_date is >75 days ago AND older than the cache's
           fetched_at date (meaning Polygon likely has a newer filing we haven't seen).
           75 days ≈ one quarter minus 2 weeks, so next quarter's 10-Q should be filed.

    Returns list of symbols to fetch.
    """
    from pair_universe import all_sp500_symbols
    all_syms = all_sp500_symbols()
    today = date.today()

    # Stage 1: skip if cache is fresh
    fetched_at_str = cache.get('fetched_at', '')
    if fetched_at_str:
        try:
            fetched_at = date.fromisoformat(fetched_at_str)
            if (today - fetched_at).days < refresh_interval_days:
                return []  # cache is fresh, nothing to do
        except ValueError:
            pass  # invalid date → proceed with stage 2

    # Stage 2: find symbols needing update
    need_update = []
    for sym in all_syms:
        entries = cache.get('symbols', {}).get(sym, [])
        if not entries:
            need_update.append(sym)
            continue
        latest = max(e.get('earnings_date', '1900-01-01') for e in entries)
        try:
            latest_date = date.fromisoformat(latest)
        except ValueError:
            need_update.append(sym)
            continue

        # Only re-fetch if latest earnings was >75 days ago
        # (one quarter minus buffer — next filing likely available on Polygon)
        if (today - latest_date).days > 75:
            need_update.append(sym)

    return need_update


def run_fetch(symbols, cache=None, quiet=False):
    """Fetch earnings for a list of symbols and update cache.
    Returns (n_fetched, n_updated, n_failed).
    """
    if cache is None:
        cache = load_cache()

    n_fetched = 0
    n_updated = 0
    n_failed = 0

    for i, sym in enumerate(symbols):
        if not quiet:
            print(f"[{i+1}/{len(symbols)}] {sym}", end=" ")
        entries = fetch_earnings_for_symbol(sym)
        n_fetched += 1

        if entries:
            old_count = len(cache.get('symbols', {}).get(sym, []))
            cache.setdefault('symbols', {})[sym] = entries
            new_count = len(entries)
            if new_count > old_count:
                n_updated += 1
                if not quiet:
                    print(f"✓ {new_count} quarters ({new_count - old_count} new)")
                    # Show latest entry
                    latest = entries[-1]
                    print(f"    latest: {latest['fiscal_period']}  "
                          f"reaction={latest['earnings_date']}  {latest['release_timing']}")
            else:
                if not quiet:
                    print(f"— {new_count} quarters (no change)")
        else:
            n_failed += 1
            if not quiet:
                print(f"✗ no data")

        time.sleep(random.uniform(0.5, 2.0))  # random delay to avoid rate limit patterns

    cache['fetched_at'] = date.today().isoformat()
    save_cache(cache)
    return n_fetched, n_updated, n_failed


def update_earnings_incremental(quiet=True):
    """Incremental update for embedding in DailySignal.
    Only fetches symbols likely to have new filings.
    Returns summary dict for logging.
    """
    cache = load_cache()
    symbols = _get_incremental_symbols(cache)

    if not symbols:
        return {'status': 'skip', 'reason': 'no symbols need update',
                'total_cached': len(cache.get('symbols', {}))}

    n_fetched, n_updated, n_failed = run_fetch(symbols, cache, quiet=quiet)

    return {
        'status': 'ok',
        'symbols_checked': n_fetched,
        'symbols_updated': n_updated,
        'symbols_failed': n_failed,
        'total_cached': len(cache.get('symbols', {})),
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Fetch and cache S&P 500 earnings dates from Polygon")
    parser.add_argument('symbols', nargs='*', help='Specific symbols to fetch')
    parser.add_argument('--full', action='store_true',
                        help='Fetch ALL S&P 500 + SECTOR_MAP tickers (~621)')
    parser.add_argument('--incremental', action='store_true',
                        help='Only fetch symbols with upcoming earnings')
    args = parser.parse_args()

    if args.symbols:
        symbols = args.symbols
        mode = 'specific'
    elif args.full:
        from pair_universe import all_sp500_symbols
        symbols = all_sp500_symbols()
        mode = 'full'
    elif args.incremental:
        cache = load_cache()
        symbols = _get_incremental_symbols(cache)
        mode = 'incremental'
    else:
        from pair_universe import all_symbols
        symbols = all_symbols()
        mode = 'active-pairs'

    print(f"Mode: {mode}")
    print(f"Fetching earnings for {len(symbols)} symbols...")
    est_minutes = len(symbols) * 0.4 / 60
    print(f"Estimated time: ~{est_minutes:.1f} minutes")
    print()

    cache = load_cache()
    n_fetched, n_updated, n_failed = run_fetch(symbols, cache, quiet=False)

    print()
    print(f"Done. Fetched={n_fetched}, Updated={n_updated}, Failed={n_failed}")
    print(f"Total symbols in cache: {len(cache.get('symbols', {}))}")
    print(f"Saved to {CACHE_PATH}")


if __name__ == '__main__':
    main()
