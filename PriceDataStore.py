"""
PriceDataStore — Parquet-based weekly partitioned price data cache.

Stores split-adjusted OHLCV from Polygon.io in weekly Parquet files.
Adj Close is computed at read time from dividends, so weekly files are
immutable once a week is complete.

Folder layout:
    price_data/
        index.json
        week_2024-12-02/
            <hash>.parquet
        week_2024-12-09/
            <hash>.parquet
        ...
"""

import os
import json
import hashlib
import logging
import requests
import time as time_module
from datetime import date, datetime, timedelta
import pandas as pd

log = logging.getLogger(__name__)

STORED_FIELDS = ['Open', 'High', 'Low', 'Close', 'Volume']


class PriceDataStore:

    def __init__(self, base_dir, polygon_api_key, api_delay=0.2):
        self._base_dir = base_dir
        self._data_dir = os.path.join(base_dir, 'price_data')
        self._api_key = polygon_api_key
        self._api_delay = api_delay
        self._index_path = os.path.join(self._data_dir, 'index.json')
        self._div_cache_path = os.path.join(self._data_dir, 'dividends_cache.json')
        self._index = None
        self._div_cache = None  # loaded lazily
        os.makedirs(self._data_dir, exist_ok=True)

    # ---------------------------------------------------------------- public

    def load(self, symbols, start_date, end_date):
        """Load OHLCV + Adj Close for symbols over [start_date, end_date].

        Returns a MultiIndex DataFrame with columns (Price, Ticker) and
        DatetimeIndex named 'Date', identical to load_historical_data_polygon().
        """
        symbols = sorted(set(symbols))
        start_dt = self._parse_date(start_date)
        end_dt = self._parse_date(end_date)

        self._load_index()

        weeks = self._get_weeks(start_dt, end_dt)
        log.info(f"PriceDataStore: loading {len(symbols)} symbols across {len(weeks)} weeks")

        week_frames = []
        for week_start in weeks:
            df = self._get_or_fetch_week(week_start, symbols)
            if df is not None and not df.empty:
                week_frames.append(df)

        if not week_frames:
            log.error("PriceDataStore: no data retrieved for any week")
            return pd.DataFrame()

        # Concatenate and trim to exact range
        combined = pd.concat(week_frames, axis=0)
        combined = combined.sort_index()
        combined = combined.loc[pd.Timestamp(start_date):pd.Timestamp(end_date)]

        # Apply dividend adjustment to produce Adj Close
        combined = self._apply_dividend_adjustment(combined, symbols,
                                                    start_date, end_date)

        log.info(f"PriceDataStore: assembled {len(combined)} rows, "
                 f"{len(combined.columns)} columns")
        return combined

    # -------------------------------------------------------------- date math

    @staticmethod
    def _parse_date(d):
        if isinstance(d, str):
            return date.fromisoformat(d)
        if isinstance(d, datetime):
            return d.date()
        return d

    @staticmethod
    def _get_week_start(d):
        """Monday of the ISO week containing d."""
        if isinstance(d, str):
            d = date.fromisoformat(d)
        if isinstance(d, datetime):
            d = d.date()
        return d - timedelta(days=d.weekday())

    @staticmethod
    def _get_weeks(start_dt, end_dt):
        """All Monday dates for weeks overlapping [start_dt, end_dt]."""
        first_monday = start_dt - timedelta(days=start_dt.weekday())
        last_monday = end_dt - timedelta(days=end_dt.weekday())
        weeks = []
        current = first_monday
        while current <= last_monday:
            weeks.append(current)
            current += timedelta(days=7)
        return weeks

    @staticmethod
    def _is_current_week(week_start):
        today = date.today()
        current_monday = today - timedelta(days=today.weekday())
        return week_start == current_monday

    # ---------------------------------------------------------- cache logic

    def _get_or_fetch_week(self, week_start, symbols):
        """Get data for one week — from cache or API."""
        week_start_str = week_start.isoformat()
        week_end = week_start + timedelta(days=6)
        week_end_str = week_end.isoformat()
        is_current = self._is_current_week(week_start)

        # Check which symbols are cached
        cached_symbols = set()
        if not is_current:
            for sym in symbols:
                sym_weeks = self._index.get('symbol_index', {}).get(sym, {})
                if week_start_str in sym_weeks:
                    cached_symbols.add(sym)

        all_cached = (cached_symbols == set(symbols))

        if all_cached and not is_current:
            # All symbols cached for a past week — read from Parquet
            df = self._read_parquet(week_start_str, symbols)
            if df is not None:
                log.info(f"  week {week_start_str}: cache hit ({len(symbols)} symbols)")
                return df
            # If read failed, fall through to re-fetch

        # Determine what needs fetching
        if is_current:
            # Always re-fetch the entire current week
            missing = symbols
            cached_frames = []
        else:
            missing = [s for s in symbols if s not in cached_symbols]
            # Read cached symbols
            cached_frames = []
            if cached_symbols:
                cached_df = self._read_parquet(week_start_str,
                                               [s for s in symbols if s in cached_symbols])
                if cached_df is not None:
                    cached_frames.append(cached_df)

        if missing:
            log.info(f"  week {week_start_str}: fetching {len(missing)} symbols "
                     f"from API{' (current week)' if is_current else ''}")
            raw = self._fetch_from_api(missing, week_start_str, week_end_str)
            if raw:
                new_df = self._assemble_week_df(raw, missing)
                self._write_parquet(week_start_str, missing, new_df)
                cached_frames.append(new_df)

        if not cached_frames:
            return None

        result = pd.concat(cached_frames, axis=1) if len(cached_frames) > 1 else cached_frames[0]
        return result

    # ---------------------------------------------------------- parquet I/O

    def _read_parquet(self, week_start_str, symbols):
        """Read Parquet file(s) for the given symbols and week. Returns DataFrame or None."""
        # Group symbols by their hash (they may be in different files)
        hash_to_syms = {}
        for sym in symbols:
            hash_id = self._index.get('symbol_index', {}).get(sym, {}).get(week_start_str)
            if hash_id is None:
                return None  # Symbol not found in index for this week
            hash_to_syms.setdefault(hash_id, []).append(sym)

        frames = []
        for hash_id, syms in hash_to_syms.items():
            file_entry = self._index.get('files', {}).get(hash_id)
            if not file_entry:
                return None
            filepath = os.path.join(self._data_dir, file_entry['filename'])
            if not os.path.exists(filepath):
                return None
            df = pd.read_parquet(filepath)
            # Filter to requested symbols only
            cols = [c for c in df.columns if c[1] in syms]
            frames.append(df[cols])

        if not frames:
            return None
        return pd.concat(frames, axis=1) if len(frames) > 1 else frames[0]

    def _write_parquet(self, week_start_str, symbols, df):
        """Write DataFrame to Parquet and update index."""
        sorted_syms = sorted(symbols)
        file_hash = self._compute_hash(sorted_syms, week_start_str)
        week_dir = os.path.join(self._data_dir, f'week_{week_start_str}')
        os.makedirs(week_dir, exist_ok=True)

        rel_path = f'week_{week_start_str}/{file_hash}.parquet'
        filepath = os.path.join(self._data_dir, rel_path)
        df.to_parquet(filepath, engine='pyarrow')

        self._update_index(file_hash, sorted_syms, week_start_str,
                           rel_path, len(df))

    def _assemble_week_df(self, raw, symbols):
        """Build MultiIndex DataFrame from raw per-symbol data dict."""
        tuples = []
        arrays = {}
        for field in STORED_FIELDS:
            for symbol in sorted(symbols):
                if symbol not in raw:
                    continue
                col_key = (field, symbol)
                tuples.append(col_key)
                arrays[col_key] = raw[symbol][field]

        if not tuples:
            return pd.DataFrame()

        multi_index = pd.MultiIndex.from_tuples(tuples, names=['Price', 'Ticker'])
        df = pd.DataFrame(arrays, columns=multi_index)
        df.index.name = 'Date'
        return df

    # ---------------------------------------------------------- Polygon API

    def _fetch_from_api(self, symbols, start_date, end_date):
        """Fetch split-adjusted OHLCV from Polygon (no dividend adjustment).
        Returns dict[symbol] -> dict with keys Open, High, Low, Close, Volume."""
        result = {}
        for i, symbol in enumerate(symbols):
            url = (
                f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
                f"{start_date}/{end_date}?adjusted=true&sort=asc&limit=50000"
                f"&apiKey={self._api_key}"
            )
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                body = resp.json()

                if body.get('status') not in ('OK', 'DELAYED'):
                    if body.get('resultsCount', 0) == 0:
                        # No data for this week (e.g., holiday week) — skip
                        continue
                    raise ValueError(f"Polygon API error for {symbol}: "
                                     f"status={body.get('status')}")

                results = body.get('results')
                if not results:
                    continue

                df = pd.DataFrame(results)
                df['date'] = pd.to_datetime(df['t'], unit='ms')
                df = df.set_index('date')

                result[symbol] = {
                    'Open': df['o'],
                    'High': df['h'],
                    'Low': df['l'],
                    'Close': df['c'],
                    'Volume': df['v'],
                }
            except Exception as e:
                log.error(f"  API fetch failed for {symbol}: {e}")
                raise

            if i < len(symbols) - 1:
                time_module.sleep(self._api_delay)

        return result

    def _load_div_cache(self):
        """Load the dividend cache from disk (once per session)."""
        if self._div_cache is not None:
            return
        if os.path.exists(self._div_cache_path):
            with open(self._div_cache_path, 'r') as f:
                self._div_cache = json.load(f)
        else:
            self._div_cache = {}

    def _save_div_cache(self):
        """Persist the dividend cache to disk."""
        with open(self._div_cache_path, 'w') as f:
            json.dump(self._div_cache, f)

    def _fetch_dividends(self, symbol, start_date, end_date):
        """Fetch dividend records, using local cache where possible.

        Cache key: symbol. We store all known dividends per symbol and only
        re-fetch if the requested end_date is newer than the cache's coverage.
        """
        self._load_div_cache()

        today_str = date.today().isoformat()
        end_str = end_date if isinstance(end_date, str) else end_date.isoformat()
        start_str = start_date if isinstance(start_date, str) else start_date.isoformat()

        entry = self._div_cache.get(symbol, {})
        cached_end = entry.get('fetched_through', '')
        cached_divs = entry.get('dividends', [])

        # Re-fetch if: no cache, or cache doesn't cover requested end_date,
        # or the requested end_date is today/future (current data may have changed).
        needs_fetch = (
            not cached_divs
            or cached_end < end_str
            or end_str >= today_str
        )

        if needs_fetch:
            all_divs = []
            url = (
                f"https://api.polygon.io/v3/reference/dividends"
                f"?ticker={symbol}"
                f"&order=asc&limit=1000"
                f"&apiKey={self._api_key}"
            )
            while url:
                resp = requests.get(url, timeout=60)
                resp.raise_for_status()
                body = resp.json()
                all_divs.extend(body.get('results', []))
                next_url = body.get('next_url')
                url = f"{next_url}&apiKey={self._api_key}" if next_url else None
            # Store all fetched dividends in cache
            self._div_cache[symbol] = {
                'fetched_through': end_str,
                'dividends': all_divs,
            }
            self._save_div_cache()
            time_module.sleep(self._api_delay)
        else:
            all_divs = cached_divs

        # Filter to requested date range
        return [
            d for d in all_divs
            if start_str <= d.get('ex_dividend_date', '') <= end_str
        ]

    # ---------------------------------------------------------- adjustment

    def _apply_dividend_adjustment(self, df, symbols, start_date, end_date):
        """Compute Adj Close using CRSP cumulative dividend formula."""
        for symbol in symbols:
            if ('Close', symbol) not in df.columns:
                continue
            dividends = self._fetch_dividends(symbol, start_date, end_date)
            close = df[('Close', symbol)]
            adj = close.copy()

            for div in reversed(dividends):
                ex_date = pd.Timestamp(div['ex_dividend_date'])
                amount = div['cash_amount']
                mask = adj.index < ex_date
                if not mask.any():
                    continue
                pre_ex_close = close.loc[close.index[mask][-1]]
                if pre_ex_close == 0:
                    continue
                factor = 1.0 - (amount / pre_ex_close)
                adj.loc[mask] *= factor

            df[('Adj Close', symbol)] = adj

        return df

    # ---------------------------------------------------------- hash / index

    @staticmethod
    def _compute_hash(sorted_symbols, week_start_str):
        """SHA256[:12] of 'SYM1,SYM2,...|week_start'."""
        key = ','.join(sorted_symbols) + '|' + week_start_str
        return hashlib.sha256(key.encode()).hexdigest()[:12]

    def _load_index(self):
        if self._index is not None:
            return
        if os.path.exists(self._index_path):
            with open(self._index_path, 'r') as f:
                self._index = json.load(f)
        else:
            self._index = {'version': 1, 'files': {}, 'symbol_index': {}}

    def _save_index(self):
        with open(self._index_path, 'w') as f:
            json.dump(self._index, f, indent=2)

    def _update_index(self, file_hash, symbols, week_start_str, filename, row_count):
        self._index.setdefault('files', {})[file_hash] = {
            'symbols': symbols,
            'week_start': week_start_str,
            'filename': filename,
            'row_count': row_count,
        }
        sym_idx = self._index.setdefault('symbol_index', {})
        for sym in symbols:
            sym_idx.setdefault(sym, {})[week_start_str] = file_hash
        self._save_index()
