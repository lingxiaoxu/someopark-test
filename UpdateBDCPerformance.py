#!/usr/bin/env python3
"""
Generate private_credit_bdc_performance.json for the Private Credit BDC + Cash portfolio.

Allocation: 50% BDC, 50% Cash (money market / T-bills)
BDC weights: GBDC 80%, TSLX/OBDC/BXSL/ARCC each 5%
Cash: tracks BIL (SPDR 1-3 Month T-Bill ETF) as money market proxy
Dividends: reinvested (DRIP) at ex-date close price

Output combines BDC + Cash into a single equity series (bdc_equity).

Usage:
    python UpdateBDCPerformance.py
    python UpdateBDCPerformance.py --dry-run
"""

import argparse
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BDC_JSON = os.path.join(BASE_DIR, 'someo-park-investment-management', 'public', 'data',
                        'private_credit_bdc_performance.json')
PERF_JSON = os.path.join(BASE_DIR, 'someo-park-investment-management', 'public', 'data',
                         'strategy_performance.json')

# Holdings are indexed from inventory_bdc.json (single source of truth since
# 2026-08-11) — no longer hard-coded. Edit the inventory file to change the
# sleeve; history snapshots live in inventory_history/inventory_bdc_*.json.
from bdc_inventory import load_inventory, update_shares_from_run

_INV = load_inventory()
BDC_TICKERS = list(_INV['holdings'])
BDC_WEIGHTS = {t: h['weight'] for t, h in _INV['holdings'].items()}
CASH_TICKER = _INV['cash']['ticker']
BDC_ALLOC = _INV['allocation']['bdc']
CASH_ALLOC = _INV['allocation']['cash']


def get_inception_info() -> tuple[str, float]:
    """Get inception date and combined start equity from strategy_performance.json."""
    with open(PERF_JSON) as f:
        data = json.load(f)
    first = data[0]
    return first['date'], first['mrpt_equity'] + first['mtfs_equity']


def build_portfolio(inception_date: str, target_start: float) -> tuple[pd.Series, dict]:
    """Build daily PC BDC + Cash portfolio equity.

    Returns:
        equity_series: combined BDC + cash equity per day
        meta: per-ticker stats
    """
    end_date = (datetime.now() + pd.Timedelta(days=2)).strftime('%Y-%m-%d')
    bdc_capital = target_start * BDC_ALLOC
    cash_capital = target_start * CASH_ALLOC

    # === BDC portion ===
    all_tickers = BDC_TICKERS + [CASH_TICKER]
    prices_raw = yf.download(all_tickers, start=inception_date, end=end_date,
                             auto_adjust=False, progress=False)
    close_prices = prices_raw['Close'].dropna(how='all')
    if close_prices.empty:
        sys.exit('[ERROR] No price data downloaded')

    # Get dividends for BDC tickers + BIL (cash sleeve distributes its yield monthly)
    dividends: dict[str, pd.Series] = {}
    for ticker in BDC_TICKERS + [CASH_TICKER]:
        t = yf.Ticker(ticker)
        divs = t.dividends
        divs = divs[divs.index >= inception_date]
        divs.index = divs.index.tz_localize(None)
        dividends[ticker] = divs

    # Initialize BDC shares (weighted allocation) — every buy is recorded so the
    # full trade ledger regenerates deterministically on each run (auto-sync).
    trades: list[dict] = []
    bdc_shares: dict[str, float] = {}
    init_prices: dict[str, float] = {}
    for ticker in BDC_TICKERS:
        p = float(close_prices[ticker].dropna().iloc[0])
        alloc = bdc_capital * BDC_WEIGHTS[ticker]
        bdc_shares[ticker] = alloc / p
        init_prices[ticker] = p
        trades.append({'date': inception_date, 'ticker': ticker, 'side': 'BUY',
                       'shares': round(bdc_shares[ticker], 4), 'price': round(p, 4),
                       'gross': round(-alloc, 2), 'action': 'OPEN',
                       'dedup_key': f'{inception_date}-{ticker}-BUY-OPEN'})

    # Initialize cash: buy BIL shares
    bil_price_0 = float(close_prices[CASH_TICKER].dropna().iloc[0])
    cash_shares = cash_capital / bil_price_0
    init_prices[CASH_TICKER] = bil_price_0
    trades.append({'date': inception_date, 'ticker': CASH_TICKER, 'side': 'BUY',
                   'shares': round(cash_shares, 4), 'price': round(bil_price_0, 4),
                   'gross': round(-cash_capital, 2), 'action': 'OPEN',
                   'dedup_key': f'{inception_date}-{CASH_TICKER}-BUY-OPEN'})

    # Track stats
    div_stats: dict[str, dict] = {t: {'count': 0, 'total_per_share': 0.0, 'total_cash': 0.0}
                                   for t in BDC_TICKERS + [CASH_TICKER]}

    # Build daily equity
    equity_series = {}
    for date_idx in close_prices.index:
        date_str = date_idx.strftime('%Y-%m-%d')

        # DRIP: reinvest BDC dividends
        for ticker in BDC_TICKERS:
            if ticker in dividends and date_idx in dividends[ticker].index:
                div_per_share = float(dividends[ticker][date_idx])
                div_cash = bdc_shares[ticker] * div_per_share
                close_today = close_prices[ticker].get(date_idx)
                if close_today is not None and not pd.isna(close_today) and float(close_today) > 0:
                    new_shares = div_cash / float(close_today)
                    trades.append({'date': date_str, 'ticker': ticker, 'side': 'BUY',
                                   'shares': round(new_shares, 4),
                                   'price': round(float(close_today), 4),
                                   'gross': round(-div_cash, 2), 'action': 'DRIP',
                                   'div_per_share': div_per_share,
                                   'div_cash': round(div_cash, 2),
                                   'dedup_key': f'{date_str}-{ticker}-BUY-DRIP'})
                    bdc_shares[ticker] += new_shares
                    div_stats[ticker]['count'] += 1
                    div_stats[ticker]['total_per_share'] += div_per_share
                    div_stats[ticker]['total_cash'] += div_cash

        # DRIP: reinvest BIL monthly distributions (T-bill yield is paid out, not in price)
        if CASH_TICKER in dividends and date_idx in dividends[CASH_TICKER].index:
            div_per_share = float(dividends[CASH_TICKER][date_idx])
            div_cash = cash_shares * div_per_share
            close_today = close_prices[CASH_TICKER].get(date_idx)
            if close_today is not None and not pd.isna(close_today) and float(close_today) > 0:
                trades.append({'date': date_str, 'ticker': CASH_TICKER, 'side': 'BUY',
                               'shares': round(div_cash / float(close_today), 4),
                               'price': round(float(close_today), 4),
                               'gross': round(-div_cash, 2), 'action': 'DRIP',
                               'div_per_share': div_per_share,
                               'div_cash': round(div_cash, 2),
                               'dedup_key': f'{date_str}-{CASH_TICKER}-BUY-DRIP'})
                cash_shares += div_cash / float(close_today)
                div_stats[CASH_TICKER]['count'] += 1
                div_stats[CASH_TICKER]['total_per_share'] += div_per_share
                div_stats[CASH_TICKER]['total_cash'] += div_cash

        # Compute BDC equity
        bdc_eq = 0.0
        for ticker in BDC_TICKERS:
            p = close_prices[ticker].get(date_idx)
            if p is not None and not pd.isna(p):
                bdc_eq += bdc_shares[ticker] * float(p)

        # Compute cash equity (BIL)
        bil_p = close_prices[CASH_TICKER].get(date_idx)
        cash_eq = cash_shares * float(bil_p) if bil_p is not None and not pd.isna(bil_p) else 0

        total = bdc_eq + cash_eq
        if total > 0:
            equity_series[date_str] = round(total, 2)

    result = pd.Series(equity_series, name='bdc')
    result.index = pd.DatetimeIndex(result.index)

    meta = {
        'target_start': target_start,
        'bdc_capital': bdc_capital,
        'cash_capital': cash_capital,
        'bdc_weights': BDC_WEIGHTS,
        'init_prices': init_prices,
        'init_shares_bdc': {t: round(bdc_shares[t] - (div_stats[t]['total_cash'] / init_prices[t] if init_prices[t] > 0 else 0), 1)
                            for t in BDC_TICKERS},  # approximate initial
        'final_shares_bdc': {t: round(bdc_shares[t], 4) for t in BDC_TICKERS},
        'cash_shares': round(cash_shares, 4),
        'div_stats': div_stats,
        'trades': trades,          # full deterministic replay → ledger auto-sync
    }
    return result, meta


def main():
    parser = argparse.ArgumentParser(description='Generate private_credit_bdc_performance.json')
    parser.add_argument('--dry-run', action='store_true', help='Print results without writing')
    args = parser.parse_args()

    inception_date, combined_start = get_inception_info()
    target = combined_start
    print(f'PC BDC + Cash Portfolio')
    print(f'  Inception: {inception_date}')
    print(f'  Total: ${target:,.0f} ({BDC_ALLOC:.0%} BDC, {CASH_ALLOC:.0%} Cash/T-Bills)')
    print(f'  BDC allocation: ${target * BDC_ALLOC:,.0f}')
    print('    ' + ', '.join(f'{t}: {w:.0%}' for t, w in BDC_WEIGHTS.items()))
    print(f'  Cash (BIL): ${target * CASH_ALLOC:,.0f}')
    print()

    equity, meta = build_portfolio(inception_date, target)
    print(f'  Days: {len(equity)} ({equity.index[0].date()} -> {equity.index[-1].date()})')
    print(f'  Start: ${equity.iloc[0]:,.0f}  End: ${equity.iloc[-1]:,.0f}')
    ret = (equity.iloc[-1] / equity.iloc[0] - 1) * 100
    print(f'  Total return: {ret:+.2f}%')
    print()

    # Per-ticker summary
    print(f'  BDC holdings:')
    for t in BDC_TICKERS:
        ds = meta['div_stats'][t]
        wt = BDC_WEIGHTS[t] * 100
        print(f'    {t} ({wt:.0f}%): final_shares={meta["final_shares_bdc"][t]:.0f} '
              f'divs={ds["count"]} div_cash=${ds["total_cash"]:,.0f}')
    bil_ds = meta['div_stats'][CASH_TICKER]
    print(f'  Cash (BIL): {meta["cash_shares"]:.0f} shares '
          f'divs={bil_ds["count"]} div_cash=${bil_ds["total_cash"]:,.0f}')

    # Build records
    records = []
    peak = float(equity.iloc[0])
    for i, (date_idx, eq) in enumerate(equity.items()):
        eq_val = float(eq)
        if eq_val > peak:
            peak = eq_val
        records.append({
            'date': str(date_idx.date()),
            'bdc_equity': round(eq_val, 2),
            'bdc_pnl': round(eq_val - float(equity.iloc[i - 1]), 2) if i > 0 else 0.0,
            'bdc_dd': round((eq_val - peak) / peak * 100, 2) if peak > 0 else 0.0,
        })

    if args.dry_run:
        print(f'\n  [DRY RUN] Not writing file')
        return

    with open(BDC_JSON, 'w') as f:
        json.dump(records, f, indent=2)
    print(f'\n  Written: {BDC_JSON} ({len(records)} records)')

    # Write DRIP-evolved shares back to the inventory file (+ history snapshot on change)
    update_shares_from_run(meta, as_of=str(equity.index[-1].date()))


if __name__ == '__main__':
    main()
