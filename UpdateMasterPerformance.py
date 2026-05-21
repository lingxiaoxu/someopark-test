#!/usr/bin/env python3
"""
Generate master_portfolio_performance.json combining three 1/3 components:
  1. MRPT + MTFS — raw equity from strategy_performance.json (no normalization)
  2. Sector Rotation (SSRS) — normalized to match MRPT+MTFS combined start
  3. AI/Chips (SOXX ETF) — buy-and-hold, normalized to match MRPT+MTFS combined start

Master = MRPT + MTFS + SR + SOXX. Each of the 3 components starts at same equity.
MRPT and MTFS are shown separately (not merged as "pairs").

Usage:
    python UpdateMasterPerformance.py
    python UpdateMasterPerformance.py --dry-run
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PERF_JSON = os.path.join(BASE_DIR, 'someo-park-investment-management', 'public', 'data',
                         'strategy_performance.json')
MASTER_JSON = os.path.join(BASE_DIR, 'someo-park-investment-management', 'public', 'data',
                           'master_portfolio_performance.json')
SR_EQUITY_DIR = os.path.join(BASE_DIR, 'qlib-main', 'sector_rotation', 'backtest_results')
SR_INVENTORY_DIR = os.path.join(BASE_DIR, 'qlib-main', 'sector_rotation', 'inventory_history')

# Date when SR live trading starts (use live inventory instead of backtest)
SR_LIVE_START = '2026-05-08'
SR_PARAM = 'sensitive_dd'


def load_pairs_equity() -> tuple[pd.Series, pd.Series, float]:
    """Load MRPT and MTFS equity from strategy_performance.json (raw, no normalization).
    Returns (mrpt_series, mtfs_series, combined_start_equity).
    """
    with open(PERF_JSON) as f:
        data = json.load(f)
    dates = pd.DatetimeIndex([r['date'] for r in data])
    mrpt = pd.Series([r['mrpt_equity'] for r in data], index=dates, name='mrpt')
    mtfs = pd.Series([r['mtfs_equity'] for r in data], index=dates, name='mtfs')
    combined_start = float(mrpt.iloc[0] + mtfs.iloc[0])
    return mrpt, mtfs, combined_start


def load_sr_equity_backtest() -> pd.Series:
    """Load SR equity from backtest CSV (sensitive_dd column)."""
    files = sorted(glob.glob(os.path.join(SR_EQUITY_DIR, 'sr_batch_equity_*.csv')))
    if not files:
        sys.exit('[ERROR] No sr_batch_equity CSV found')
    df = pd.read_csv(files[-1], index_col=0, parse_dates=True)
    if SR_PARAM not in df.columns:
        sys.exit(f'[ERROR] Column {SR_PARAM} not found in {files[-1]}')
    return df[SR_PARAM].dropna()


def load_sr_equity_live(backtest_normalized: pd.Series,
                        trading_days: pd.DatetimeIndex) -> pd.Series:
    """Load SR equity from live inventory snapshots + real prices.
    Chains daily returns from backtest last day to ensure price continuity.
    """
    live_start = pd.Timestamp(SR_LIVE_START)
    live_days = trading_days[trading_days >= live_start]
    if len(live_days) == 0:
        return pd.Series(dtype=float)

    inv_files = sorted(glob.glob(os.path.join(SR_INVENTORY_DIR, 'inventory_sector_rotation_*.json')))
    snap_map = {}
    all_tickers = set()
    for fpath in inv_files:
        with open(fpath) as f:
            inv = json.load(f)
        as_of = inv.get('as_of', inv.get('date', ''))
        if as_of:
            snap_map[as_of] = fpath
        for ticker in inv.get('holdings', {}):
            all_tickers.add(ticker)

    if not all_tickers:
        print('  [WARN] No SR holdings found in inventory_history')
        return pd.Series(dtype=float)

    price_start = (live_start - pd.Timedelta(days=10)).strftime('%Y-%m-%d')
    price_end = (live_days[-1] + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
    try:
        prices_raw = yf.download(sorted(all_tickers), start=price_start, end=price_end,
                                 auto_adjust=True, progress=False)
        prices = prices_raw['Close'] if len(all_tickers) > 1 else prices_raw[['Close']]
        if len(all_tickers) == 1:
            prices.columns = list(all_tickers)
    except Exception as e:
        print(f'  [WARN] Failed to download SR ticker prices: {e}')
        return pd.Series(dtype=float)

    # Compute raw MTM for each live day
    raw_mtm = {}
    for td in live_days:
        td_str = td.strftime('%Y-%m-%d')
        candidates = [d for d in sorted(snap_map.keys()) if d <= td_str]
        if not candidates:
            continue
        with open(snap_map[candidates[-1]]) as f:
            inv = json.load(f)
        total = 0.0
        for ticker, holding in inv.get('holdings', {}).items():
            shares = holding.get('shares', 0)
            if shares == 0 or ticker not in prices.columns:
                continue
            try:
                ps = prices[ticker].loc[:td].dropna()
                if len(ps) > 0:
                    total += shares * float(ps.iloc[-1])
            except Exception:
                continue
        if total > 0:
            raw_mtm[td_str] = total

    if not raw_mtm:
        return pd.Series(dtype=float)

    # Chain daily returns from backtest last day
    bt_last_equity = float(backtest_normalized.iloc[-1])
    live_dates = sorted(raw_mtm.keys())
    results = {}
    prev_raw = None
    current_equity = bt_last_equity

    for d in live_dates:
        if prev_raw is not None:
            daily_ret = (raw_mtm[d] - prev_raw) / prev_raw
            current_equity = current_equity * (1 + daily_ret)
        # First live day: keep bt_last_equity (return = 0 for stitching day)
        prev_raw = raw_mtm[d]
        results[d] = round(current_equity, 2)

    return pd.Series(results, name='sr')


BDC_JSON = os.path.join(BASE_DIR, 'someo-park-investment-management', 'public', 'data',
                        'private_credit_bdc_performance.json')


def load_bdc_equity() -> pd.Series:
    """Load BDC equity from private_credit_bdc_performance.json (generated by UpdateBDCPerformance.py)."""
    if not os.path.exists(BDC_JSON):
        sys.exit(f'[ERROR] {BDC_JSON} not found. Run UpdateBDCPerformance.py first.')
    with open(BDC_JSON) as f:
        data = json.load(f)
    dates = pd.DatetimeIndex([r['date'] for r in data])
    equity = pd.Series([r['bdc_equity'] for r in data], index=dates, name='bdc')
    return equity


def load_soxx_equity(inception_date: str, target_start: float) -> pd.Series:
    """Load SOXX buy-and-hold equity, normalized to target_start at inception."""
    end_date = (datetime.now() + pd.Timedelta(days=2)).strftime('%Y-%m-%d')
    soxx_raw = yf.download('SOXX', start=inception_date, end=end_date,
                           auto_adjust=True, progress=False)
    soxx = soxx_raw['Close'].squeeze().dropna()
    if len(soxx) == 0:
        sys.exit('[ERROR] No SOXX data downloaded')

    # Buy shares at inception to get target_start equity
    inception_price = float(soxx.iloc[0])
    shares = target_start / inception_price
    equity = (soxx * shares).round(2)
    equity.index = pd.DatetimeIndex([d.strftime('%Y-%m-%d') for d in equity.index])
    equity.name = 'soxx'
    return equity


def main():
    parser = argparse.ArgumentParser(description='Generate master_portfolio_performance.json')
    parser.add_argument('--dry-run', action='store_true', help='Print results without writing')
    args = parser.parse_args()

    print('Loading components...')

    # 1. MRPT + MTFS (raw from strategy_performance.json)
    mrpt, mtfs, combined_start = load_pairs_equity()
    inception_date = str(mrpt.index[0].date())
    print(f'  MRPT: {len(mrpt)} days, start=${mrpt.iloc[0]:,.0f} end=${mrpt.iloc[-1]:,.0f}')
    print(f'  MTFS: {len(mtfs)} days, start=${mtfs.iloc[0]:,.0f} end=${mtfs.iloc[-1]:,.0f}')
    print(f'  Combined start: ${combined_start:,.0f} (SR and SOXX each normalize to this)')

    # 2. SR (backtest + live), normalized to combined_start
    sr_bt = load_sr_equity_backtest()
    inception_ts = pd.Timestamp(inception_date)
    sr_bt_from_inception = sr_bt[sr_bt.index >= inception_ts]
    sr_scale = combined_start / float(sr_bt_from_inception.iloc[0])

    sr_bt_portion = sr_bt_from_inception[sr_bt_from_inception.index < pd.Timestamp(SR_LIVE_START)]
    sr_bt_normalized = (sr_bt_portion * sr_scale).round(2)
    sr_bt_normalized.name = 'sr'
    print(f'  SR backtest: {len(sr_bt_normalized)} days, scale={sr_scale:.6f}, '
          f'start=${sr_bt_normalized.iloc[0]:,.0f} end=${sr_bt_normalized.iloc[-1]:,.0f}')

    sr_live = load_sr_equity_live(sr_bt_normalized, mrpt.index)
    if len(sr_live) > 0:
        print(f'  SR live: {len(sr_live)} days, '
              f'start=${sr_live.iloc[0]:,.0f} end=${sr_live.iloc[-1]:,.0f}')
        sr = pd.concat([sr_bt_normalized, sr_live])
    else:
        sr = sr_bt_normalized
        print('  SR live: no live data yet')

    sr.index = pd.DatetimeIndex(sr.index)
    print(f'  SR total: {len(sr)} days')

    # 3. SOXX, normalized to combined_start
    soxx = load_soxx_equity(inception_date, combined_start)
    soxx.index = pd.DatetimeIndex(soxx.index)
    print(f'  SOXX: {len(soxx)} days, start=${soxx.iloc[0]:,.0f} end=${soxx.iloc[-1]:,.0f}')

    # 4. BDC Private Credit (from private_credit_bdc_performance.json)
    bdc = load_bdc_equity()
    print(f'  BDC: {len(bdc)} days, start=${bdc.iloc[0]:,.0f} end=${bdc.iloc[-1]:,.0f}')

    # 5. Merge on common dates
    df = pd.DataFrame({'mrpt': mrpt, 'mtfs': mtfs, 'sr': sr, 'soxx': soxx, 'bdc': bdc})
    df['sr'] = df['sr'].ffill()
    df['soxx'] = df['soxx'].ffill()
    df['bdc'] = df['bdc'].ffill()
    df = df.dropna()

    print(f'\n  Merged: {len(df)} trading days ({df.index[0].date()} -> {df.index[-1].date()})')

    # 6. Compute combined (3 AI strategies) and master (all incl SOXX + BDC)
    df['combined'] = df['mrpt'] + df['mtfs'] + df['sr']
    df['master'] = df['combined'] + df['soxx'] + df['bdc']

    components = ['mrpt', 'mtfs', 'sr', 'soxx', 'bdc', 'combined', 'master']
    records = []
    peaks = {c: float(df[c].iloc[0]) for c in components}

    for i, (date_idx, row) in enumerate(df.iterrows()):
        rec = {'date': str(date_idx.date())}
        for comp in components:
            eq = float(row[comp])
            rec[f'{comp}_equity'] = round(eq, 2)
            rec[f'{comp}_pnl'] = round(eq - float(df.iloc[i - 1][comp]), 2) if i > 0 else 0.0
            if eq > peaks[comp]:
                peaks[comp] = eq
            rec[f'{comp}_dd'] = round((eq - peaks[comp]) / peaks[comp] * 100, 2) if peaks[comp] > 0 else 0.0
        records.append(rec)

    # Print summary
    last = records[-1]
    first_rec = records[0]
    print(f'\n  Final equities:')
    for comp in components:
        eq = last[f'{comp}_equity']
        start = first_rec[f'{comp}_equity']
        ret = (eq / start - 1) * 100 if start > 0 else 0
        max_dd = min(r[f'{comp}_dd'] for r in records)
        label = 'SSRS' if comp == 'sr' else comp.upper()
        print(f'    {label:<10s} ${eq:>12,.2f}  ret={ret:>+7.2f}%  maxDD={max_dd:>+6.2f}%')

    if args.dry_run:
        print('\n  [DRY RUN] Not writing file')
        return

    with open(MASTER_JSON, 'w') as f:
        json.dump(records, f, indent=2)
    print(f'\n  Written: {MASTER_JSON} ({len(records)} records)')


if __name__ == '__main__':
    main()
