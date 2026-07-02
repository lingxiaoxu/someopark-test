#!/usr/bin/env python3
"""
Generate master_portfolio_performance.json combining three 1/3 components:
  1. MRPT + MTFS — raw equity from strategy_performance.json (no normalization)
  2. Sector Rotation (SSRS) — normalized to match MRPT+MTFS combined start
  3. AI Infra & Semiconductor Strategy (AISS) — normalized to match MRPT+MTFS combined start

Master = MRPT + MTFS + SR + AISS. Each of the 3 components starts at same equity.
MRPT and MTFS are shown separately (not merged as "pairs").
BDC is an additional overlay.

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

# ── SR (Sector Rotation) ───────────────────────────────────────────────────
SR_EQUITY_DIR = os.path.join(BASE_DIR, 'qlib-main', 'sector_rotation', 'backtest_results')
SR_INVENTORY_DIR = os.path.join(BASE_DIR, 'qlib-main', 'sector_rotation', 'inventory_history')
SR_LIVE_START = '2026-05-08'
# backtest param is auto-selected (best total return); live segment follows the
# actual traded param via inventory MTM — see _best_backtest_column()

# ── AISS (AI Infra & Semiconductor Strategy) ───────────────────────────────
AISS_EQUITY_DIR = os.path.join(BASE_DIR, 'qlib-main', 'semiconductor_strategy', 'backtest_results')
AISS_INVENTORY_DIR = os.path.join(BASE_DIR, 'qlib-main', 'semiconductor_strategy', 'inventory_history')
AISS_LIVE_START = '2026-06-01'
# backtest param auto-selected (best total return); live segment (>= AISS_LIVE_START)
# follows the actual traded param via inventory_aiss stock_holdings MTM


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


def _best_backtest_column(df: pd.DataFrame, label: str) -> str:
    """Best-looking backtest param column = highest total return (final/first over
    its valid history). The pre-live segment is backtest-only, so we display the
    strongest backtest curve; the live segment then follows the ACTUAL traded param
    via inventory MTM. Nothing is hardcoded — adapts automatically as params change."""
    best_col, best_ret = None, -1.0
    for c in df.columns:
        s = pd.to_numeric(df[c], errors='coerce').dropna()
        if len(s) < 30 or s.iloc[0] <= 0:
            continue
        r = float(s.iloc[-1] / s.iloc[0])
        if r > best_ret:
            best_ret, best_col = r, c
    if best_col is None:
        sys.exit(f'[ERROR] No valid {label} backtest column found')
    print(f'  {label} backtest param (best total return, auto-selected): {best_col} ({best_ret:.2f}x)')
    return best_col


def load_sr_equity_backtest() -> pd.Series:
    """Load SR equity from backtest CSV — best-looking param column (dynamic, not hardcoded)."""
    files = sorted(glob.glob(os.path.join(SR_EQUITY_DIR, 'sr_batch_equity_*.csv')))
    if not files:
        sys.exit('[ERROR] No sr_batch_equity CSV found')
    df = pd.read_csv(files[-1], index_col=0, parse_dates=True)
    return df[_best_backtest_column(df, 'SR')].dropna()


def _load_live_equity_from_inventory(
    inv_dir: str, inv_prefix: str, holdings_key: str,
    live_start: str, backtest_normalized: pd.Series,
    trading_days: pd.DatetimeIndex, label: str,
    price_loader=None,
) -> pd.Series:
    """Generic live equity loader from inventory snapshots + real prices.
    Works for both SR (holdings = ETFs) and AISS (stock_holdings = individual stocks).
    ``price_loader(tickers, start, end) -> wide DataFrame`` overrides the default
    yfinance download (AISS passes a Polygon-store loader; yfinance is the fallback).
    Chains daily returns from backtest last day to ensure price continuity.
    """
    live_start_ts = pd.Timestamp(live_start)
    live_days = trading_days[trading_days >= live_start_ts]
    if len(live_days) == 0:
        return pd.Series(dtype=float)

    inv_files = sorted(glob.glob(os.path.join(inv_dir, f'{inv_prefix}_*.json')))
    snap_map = {}
    all_tickers = set()
    for fpath in inv_files:
        with open(fpath) as f:
            inv = json.load(f)
        as_of = inv.get('as_of', inv.get('date', ''))
        if as_of:
            snap_map[as_of] = fpath
        for ticker in inv.get(holdings_key, {}):
            all_tickers.add(ticker)

    if not all_tickers:
        print(f'  [WARN] No {label} holdings found in {inv_dir}')
        return pd.Series(dtype=float)

    price_start = (live_start_ts - pd.Timedelta(days=10)).strftime('%Y-%m-%d')
    price_end = (live_days[-1] + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
    # Preferred source = price_loader (AISS → Polygon store, more stable than
    # yfinance); fall back to yfinance if it fails or returns nothing.
    prices = None
    if price_loader is not None:
        try:
            prices = price_loader(sorted(all_tickers), price_start, price_end)
            if prices is None or prices.empty:
                _polygon_fallback_alert(label, 'returned no data')
                prices = None
            else:
                print(f'  {label}: live prices via Polygon store ({len(prices.columns)} tickers)')
        except Exception as e:
            _polygon_fallback_alert(label, repr(e))
            prices = None
    if prices is None:
        try:
            prices_raw = yf.download(sorted(all_tickers), start=price_start, end=price_end,
                                     auto_adjust=True, progress=False)
            if len(all_tickers) > 1:
                prices = prices_raw['Close']
            else:
                prices = prices_raw[['Close']]
                prices.columns = list(all_tickers)
        except Exception as e:
            print(f'  [WARN] Failed to download {label} ticker prices: {e}')
            return pd.Series(dtype=float)

    def _mark_snapshot(snap_path: str, price_day: str) -> tuple:
        """MTM one snapshot's holdings at price_day close. Returns (total, cash_weight).

        Corporate actions: historical snapshots store entry-era shares; the price
        series (prices[ticker]) is split-adjusted (current caliber). Without the
        adjust, a split (KLAC 1:10) makes a snapshot's old-caliber shares ×
        new-caliber price spike the MTM (AISS live jumped +40% on 6/12).
        ref_price = source price at the snapshot's OWN as_of date — guard 4:
        holdings rewritten by a rebalance (marker lost, entry_date pre-split,
        shares already current-caliber) must NOT be re-adjusted (KLAC 465→4650
        faked +32% on 7/1)."""
        with open(snap_path) as f:
            inv = json.load(f)
        snap_as_of = inv.get('as_of', inv.get('date', price_day)) or price_day
        total = 0.0
        for ticker, holding in inv.get(holdings_key, {}).items():
            try:
                from CorporateActions import adjust_stock_holding_view
                _ref = None
                try:
                    _ps0 = prices[ticker].loc[:snap_as_of].dropna()
                    _ref = float(_ps0.iloc[-1]) if len(_ps0) else None
                except Exception:
                    pass
                holding = adjust_stock_holding_view(holding, ticker, ref_price=_ref)
            except Exception:
                pass
            shares = holding.get('shares', 0)
            if shares == 0 or ticker not in prices.columns:
                continue
            try:
                ps = prices[ticker].loc[:price_day].dropna()
                if len(ps) > 0:
                    total += shares * float(ps.iloc[-1])
            except Exception:
                continue
        cw = inv.get('cash_weight', 0.0) or 0.0
        return total, float(cw)

    # Chain daily returns from backtest last day — FLOW-NEUTRAL: each day-pair
    # (d_prev, d) is marked with the SAME snapshot (latest as_of <= d_prev), so
    # rebalance buys/sells never leak into the return (7/1 AISS moved to 58.75%
    # cash; the old per-day-snapshot chaining booked the flow as a -64% "return").
    # Cash earns 0: portfolio return = invested return × (1 − cash_weight).
    bt_last_equity = float(backtest_normalized.iloc[-1])
    snap_dates = sorted(snap_map.keys())
    results = {}
    current_equity = bt_last_equity
    prev_td_str = None

    for td in live_days:
        td_str = td.strftime('%Y-%m-%d')
        if not any(d <= td_str for d in snap_dates):
            continue   # no snapshot yet → live segment hasn't started
        if prev_td_str is not None:
            prev_candidates = [d for d in snap_dates if d <= prev_td_str]
            snap_path = snap_map[prev_candidates[-1]]
            mtm_prev, cw = _mark_snapshot(snap_path, prev_td_str)
            mtm_cur, _ = _mark_snapshot(snap_path, td_str)
            if mtm_prev > 0 and mtm_cur > 0:
                invested_ret = mtm_cur / mtm_prev - 1.0
                port_ret = invested_ret * max(0.0, 1.0 - cw)
                current_equity = current_equity * (1.0 + port_ret)
            # mtm_prev == 0 → fully in cash on prev day → return 0, equity unchanged
        results[td_str] = round(current_equity, 2)
        prev_td_str = td_str

    if not results:
        return pd.Series(dtype=float)

    return pd.Series(results, name=label)


def load_sr_equity_live(backtest_normalized: pd.Series,
                        trading_days: pd.DatetimeIndex) -> pd.Series:
    """SR live equity：优先 portfolio_ledger 账户（Phase 5，账本自 4/27 起、
    此处从 SR_LIVE_START 拼接段取收益率）；无账本时回退旧 inventory-MTM 合成。"""
    acct = _load_account_equity('ssrs')
    if len(acct):
        s = _chain_account_live(acct, SR_LIVE_START, backtest_normalized, 'sr')
        if len(s):
            print(f'  sr: live via LEDGER account equity ({len(s)} days, '
                  f'book ${acct.iloc[-1]:,.0f})')
            return s
    print('  ⚠️  sr: NO LEDGER ACCOUNT — falling back to inventory-MTM chaining')
    return _load_live_equity_from_inventory(
        inv_dir=SR_INVENTORY_DIR,
        inv_prefix='inventory_sector_rotation',
        holdings_key='holdings',
        live_start=SR_LIVE_START,
        backtest_normalized=backtest_normalized,
        trading_days=trading_days,
        label='sr',
        price_loader=lambda tk, s, e: _polygon_price_loader(tk, s, e, prices_dir=SR_POLYGON_STORE),
    )


def load_aiss_equity_backtest() -> pd.Series:
    """Load AISS equity from backtest CSV — best-looking param column (dynamic, not hardcoded).
    Live segment (>= AISS_LIVE_START) follows the actual traded param via inventory MTM."""
    files = sorted(glob.glob(os.path.join(AISS_EQUITY_DIR, 'aiss_batch_equity_*.csv')))
    if not files:
        sys.exit('[ERROR] No aiss_batch_equity CSV found')
    df = pd.read_csv(files[-1], index_col=0, parse_dates=True)
    return df[_best_backtest_column(df, 'AISS')].dropna()


# Separate Polygon parquet store for SSRS sector ETFs (kept OUT of the AISS store
# so the two universes don't mix). AISS stocks + benchmarks use the default store.
SR_POLYGON_STORE = os.path.join(BASE_DIR, 'price_data', 'sector_etfs', 'polygon')


def _polygon_price_loader(tickers, start, end, prices_dir=None):
    """Shared Polygon-store price loader for the live segments + benchmarks (more
    stable than yfinance). Uses aiss_fetch_prices.load_prices_wide, which reads the
    isolated Polygon parquet store and incrementally refreshes any ticker stale for
    ``end`` (auto-fetches tickers not yet in the store). ``prices_dir`` selects which
    store: default = AISS semi_strategy (stocks + SMH/SPY); SR passes its own ETF
    store so SSRS ETFs are not mixed into the AISS store. Both still go through the
    same loader, so SR and AISS live are sourced identically."""
    qlib_dir = os.path.join(BASE_DIR, 'qlib-main')
    if qlib_dir not in sys.path:
        sys.path.insert(0, qlib_dir)
    from semiconductor_strategy.data import aiss_fetch_prices as _fp
    return _fp.load_prices_wide(list(tickers), start=start, end=end, field='AdjClose',
                                prices_dir=prices_dir)


def _polygon_fallback_alert(label, reason):
    """Loud alert when the Polygon source fails and we fall back to yfinance — so a
    silent degradation never hides a Polygon outage. Goes to stderr + stdout."""
    msg = (f'⚠️  [ALERT] {label}: POLYGON PRICE SOURCE FAILED ({reason}) — '
           f'FELL BACK TO YFINANCE. Investigate the Polygon store / API.')
    print(msg, file=sys.stderr)
    print('  ' + msg)


def _load_account_equity(strategy: str) -> pd.Series:
    """portfolio_ledger 账户每日 equity 序列（真实 cash/分红/费用全含）。"""
    dirs = {'aiss': 'semiconductor_strategy', 'ssrs': 'sector_rotation'}
    hd = os.path.join(BASE_DIR, 'qlib-main', dirs[strategy], 'account_history')
    out = {}
    for fp in sorted(glob.glob(os.path.join(hd, f'account_{strategy}_*.json'))):
        try:
            with open(fp) as f:
                d = json.load(f)
            if d.get('equity') is not None:
                out[pd.Timestamp(d['as_of'])] = float(d['equity'])
        except Exception:
            continue
    return pd.Series(out).sort_index()


def _chain_account_live(account_eq: pd.Series, live_start: str,
                        backtest_normalized: pd.Series, label: str) -> pd.Series:
    """账本 equity 日收益率链到回测拼接点（ledger Phase 5）。
    与旧的 inventory-MTM 合成相比：收益率来自真实账本（含分红/费用/复利），
    无需价格加载、无 flow 问题。live_start 前若有账本值，首日即有真实收益。"""
    live = account_eq[account_eq.index >= pd.Timestamp(live_start)]
    if live.empty:
        return pd.Series(dtype=float)
    prev_vals = account_eq[account_eq.index < pd.Timestamp(live_start)]
    prev = float(prev_vals.iloc[-1]) if len(prev_vals) else None
    cur = float(backtest_normalized.iloc[-1])
    results = {}
    for ts, v in live.items():
        if prev is not None and prev > 0:
            cur = cur * (float(v) / prev)
        results[str(ts.date())] = round(cur, 2)
        prev = float(v)
    return pd.Series(results, name=label)


def load_aiss_equity_live(backtest_normalized: pd.Series,
                          trading_days: pd.DatetimeIndex) -> pd.Series:
    """AISS live equity：优先 portfolio_ledger 账户（Phase 5）；无账本时回退
    旧的 inventory-MTM 合成（响亮告警）。"""
    acct = _load_account_equity('aiss')
    if len(acct):
        s = _chain_account_live(acct, AISS_LIVE_START, backtest_normalized, 'aiss')
        if len(s):
            print(f'  aiss: live via LEDGER account equity ({len(s)} days, '
                  f'book ${acct.iloc[-1]:,.0f})')
            return s
    print('  ⚠️  aiss: NO LEDGER ACCOUNT — falling back to inventory-MTM chaining')
    return _load_live_equity_from_inventory(
        inv_dir=AISS_INVENTORY_DIR,
        inv_prefix='inventory_aiss',
        holdings_key='stock_holdings',
        price_loader=_polygon_price_loader,
        live_start=AISS_LIVE_START,
        backtest_normalized=backtest_normalized,
        trading_days=trading_days,
        label='aiss',
    )


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
    print(f'  Combined start: ${combined_start:,.0f} (SR and AISS each normalize to this)')

    inception_ts = pd.Timestamp(inception_date)

    # 2. SR (backtest + live), normalized to combined_start
    sr_bt = load_sr_equity_backtest()
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

    # 3. AISS (backtest + live), normalized to combined_start
    aiss_bt = load_aiss_equity_backtest()
    aiss_bt_from_inception = aiss_bt[aiss_bt.index >= inception_ts]
    aiss_scale = combined_start / float(aiss_bt_from_inception.iloc[0])

    aiss_bt_portion = aiss_bt_from_inception[aiss_bt_from_inception.index < pd.Timestamp(AISS_LIVE_START)]
    aiss_bt_normalized = (aiss_bt_portion * aiss_scale).round(2)
    aiss_bt_normalized.name = 'aiss'
    print(f'  AISS backtest: {len(aiss_bt_normalized)} days, scale={aiss_scale:.6f}, '
          f'start=${aiss_bt_normalized.iloc[0]:,.0f} end=${aiss_bt_normalized.iloc[-1]:,.0f}')

    aiss_live = load_aiss_equity_live(aiss_bt_normalized, mrpt.index)
    if len(aiss_live) > 0:
        print(f'  AISS live: {len(aiss_live)} days, '
              f'start=${aiss_live.iloc[0]:,.0f} end=${aiss_live.iloc[-1]:,.0f}')
        aiss = pd.concat([aiss_bt_normalized, aiss_live])
    else:
        aiss = aiss_bt_normalized
        print('  AISS live: no live data yet')

    aiss.index = pd.DatetimeIndex(aiss.index)
    print(f'  AISS total: {len(aiss)} days')

    # 4. BDC Private Credit (from private_credit_bdc_performance.json)
    bdc = load_bdc_equity()
    print(f'  BDC: {len(bdc)} days, start=${bdc.iloc[0]:,.0f} end=${bdc.iloc[-1]:,.0f}')

    # 5. Benchmarks (SPY, SMH, SOXX, MAGS) — buy-and-hold, normalized to combined_start.
    #   SPY = broad market; SMH/SOXX = semis (cap-weighted vs equal-ish); MAGS = Mag-7.
    BENCHMARK_TICKERS = ('SPY', 'SMH', 'SOXX', 'MAGS')
    benchmarks = {}
    bm_end = (datetime.now() + pd.Timedelta(days=2)).strftime('%Y-%m-%d')
    # Prefer Polygon (all four are in the AISS store) so benchmarks are sourced the
    # same way as AISS/SSRS; yfinance fallback per ticker with a loud alert.
    bm_prices = None
    try:
        bm_prices = _polygon_price_loader(list(BENCHMARK_TICKERS), inception_date, bm_end)
        if bm_prices is None or bm_prices.empty:
            _polygon_fallback_alert('benchmarks', 'returned no data')
            bm_prices = None
        else:
            print('  benchmarks: prices via Polygon store')
    except Exception as e:
        _polygon_fallback_alert('benchmarks', repr(e))
        bm_prices = None
    for ticker in BENCHMARK_TICKERS:
        try:
            close = None
            if bm_prices is not None and ticker in bm_prices.columns:
                close = bm_prices[ticker].dropna()
            if close is None or len(close) == 0:
                if bm_prices is not None:
                    _polygon_fallback_alert(ticker, 'missing in Polygon store')
                raw = yf.download(ticker, start=inception_date, end=bm_end,
                                  auto_adjust=True, progress=False)
                close = raw['Close'].squeeze().dropna()
            if len(close) > 0:
                shares = combined_start / float(close.iloc[0])
                eq = (close * shares).round(2)
                eq.index = pd.DatetimeIndex([d.strftime('%Y-%m-%d') for d in eq.index])
                eq.name = ticker.lower()
                benchmarks[ticker.lower()] = eq
                print(f'  {ticker}: {len(eq)} days, start=${eq.iloc[0]:,.0f} end=${eq.iloc[-1]:,.0f}')
        except Exception as e:
            print(f'  [WARN] {ticker} download failed: {e}')

    # 6. Merge on common dates
    df = pd.DataFrame({'mrpt': mrpt, 'mtfs': mtfs, 'sr': sr, 'aiss': aiss, 'bdc': bdc})
    for bm_key, bm_eq in benchmarks.items():
        df[bm_key] = bm_eq
    df['sr'] = df['sr'].ffill()
    df['aiss'] = df['aiss'].ffill()
    df['bdc'] = df['bdc'].ffill()
    for bm_key in benchmarks:
        df[bm_key] = df[bm_key].ffill()
    df = df.dropna()

    print(f'\n  Merged: {len(df)} trading days ({df.index[0].date()} -> {df.index[-1].date()})')

    # 6. Compute combined (4 AI strategies) and master (all incl BDC)
    df['combined'] = df['mrpt'] + df['mtfs'] + df['sr'] + df['aiss']
    df['master'] = df['combined'] + df['bdc']

    components = ['mrpt', 'mtfs', 'sr', 'aiss', 'bdc', 'combined', 'master']
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
        # Benchmarks (equity only — used as dashed reference lines)
        for bm_key in benchmarks:
            rec[f'{bm_key}_equity'] = round(float(row[bm_key]), 2)
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
