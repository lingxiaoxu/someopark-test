"""
MRPTFetchEarnings.py — Fetch and cache quarterly earnings dates for all tracked symbols

Uses Polygon vX/reference/financials endpoint.
Output: price_data/earnings_cache.json

Format:
{
  "fetched_at": "2026-03-04",
  "symbols": {
    "LYFT": [
      {"fiscal_period": "Q3 2025", "end_date": "2025-09-30",
       "acceptance_datetime": "2025-11-05T22:20:44Z",
       "filing_date": "2025-11-05",
       "earnings_date": "2025-11-05",        # date market reacts (next trading day if after 4pm)
       "earnings_release": "AMC"},            # BMO = before market open, AMC = after market close
    ],
    ...
  }
}

Usage:
    python MRPTFetchEarnings.py          # fetch all symbols
    python MRPTFetchEarnings.py LYFT GS  # fetch specific symbols only
"""

import sys
import os
import json
import time
import subprocess
from datetime import datetime, timedelta, date

POLYGON_API_KEY = os.environ['POLYGON_API_KEY']
CACHE_PATH = 'price_data/earnings_cache.json'

from pair_universe import all_symbols
ALL_SYMBOLS = all_symbols()


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
    After 16:00 ET = AMC → market reacts next day
    Before 09:30 ET = BMO → market reacts same day
    Returns: ('AMC', next_trading_date) or ('BMO', same_date)
    """
    if not acceptance_dt_str:
        return 'UNKNOWN', None
    # acceptance_datetime is UTC, ET = UTC-5 (EST) or UTC-4 (EDT)
    # Approximate: if UTC hour >= 20 → after 4pm ET; if UTC hour <= 13 → before 9:30am ET
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
            continue  # not yet filed

        timing, reaction_date = _is_after_market(acceptance)

        if reaction_date is None and filing:
            reaction_date = datetime.strptime(filing, '%Y-%m-%d').date() + timedelta(days=1)

        if reaction_date is None:
            continue

        key = reaction_date.isoformat()
        if key in seen_dates:
            continue  # deduplicate (annual + TTM often same date)
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
    Quarterly covers Q1-Q3; annual covers Q4/FY (companies file annual instead of Q4).
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
        time.sleep(0.1)

    if not all_results:
        print(f"  [{symbol}] No data")
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
    with open(CACHE_PATH, 'w') as f:
        json.dump(cache, f, indent=2)


def print_summary(symbol, entries):
    print(f"  [{symbol}] {len(entries)} quarters:")
    for e in entries[-6:]:  # show last 6
        print(f"    {e['fiscal_period']:8s}  end={e['end_date']}  "
              f"reaction={e['earnings_date']}  {e['release_timing']}")


def main():
    symbols = sys.argv[1:] if len(sys.argv) > 1 else ALL_SYMBOLS

    cache = load_cache()
    if 'symbols' not in cache:
        cache['symbols'] = {}

    print(f"Fetching earnings dates for {len(symbols)} symbols...")
    print()

    for i, sym in enumerate(symbols):
        print(f"[{i+1}/{len(symbols)}] {sym}")
        entries = fetch_earnings_for_symbol(sym)
        if entries:
            cache['symbols'][sym] = entries
            print_summary(sym, entries)
        else:
            print(f"  [{sym}] WARNING: no data fetched")
        time.sleep(0.3)

    cache['fetched_at'] = date.today().isoformat()
    save_cache(cache)
    print()
    print(f"Saved to {CACHE_PATH}")
    print(f"Total symbols cached: {len(cache['symbols'])}")


if __name__ == '__main__':
    main()
