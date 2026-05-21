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

# BDC tickers and weights (within the 50% BDC allocation)
BDC_TICKERS = ['GBDC', 'TSLX', 'OBDC', 'BXSL', 'ARCC']
BDC_WEIGHTS = {'GBDC': 0.80, 'TSLX': 0.05, 'OBDC': 0.05, 'BXSL': 0.05, 'ARCC': 0.05}

# Cash proxy
CASH_TICKER = 'BIL'  # SPDR Bloomberg 1-3 Month T-Bill ETF

# Allocation split
BDC_ALLOC = 0.50   # 50% to BDC
CASH_ALLOC = 0.50   # 50% to cash (money market)


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

    # Get dividends for BDC tickers
    dividends: dict[str, pd.Series] = {}
    for ticker in BDC_TICKERS:
        t = yf.Ticker(ticker)
        divs = t.dividends
        divs = divs[divs.index >= inception_date]
        divs.index = divs.index.tz_localize(None)
        dividends[ticker] = divs

    # Initialize BDC shares (weighted allocation)
    bdc_shares: dict[str, float] = {}
    init_prices: dict[str, float] = {}
    for ticker in BDC_TICKERS:
        p = float(close_prices[ticker].dropna().iloc[0])
        alloc = bdc_capital * BDC_WEIGHTS[ticker]
        bdc_shares[ticker] = alloc / p
        init_prices[ticker] = p

    # Initialize cash: buy BIL shares
    bil_price_0 = float(close_prices[CASH_TICKER].dropna().iloc[0])
    cash_shares = cash_capital / bil_price_0
    init_prices[CASH_TICKER] = bil_price_0

    # Track stats
    div_stats: dict[str, dict] = {t: {'count': 0, 'total_per_share': 0.0, 'total_cash': 0.0}
                                   for t in BDC_TICKERS}

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
                    bdc_shares[ticker] += new_shares
                    div_stats[ticker]['count'] += 1
                    div_stats[ticker]['total_per_share'] += div_per_share
                    div_stats[ticker]['total_cash'] += div_cash

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
        'final_shares_bdc': {t: round(bdc_shares[t], 1) for t in BDC_TICKERS},
        'cash_shares': round(cash_shares, 1),
        'div_stats': div_stats,
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
    print(f'  Total: ${target:,.0f} (50% BDC, 50% Cash/T-Bills)')
    print(f'  BDC allocation: ${target * BDC_ALLOC:,.0f}')
    print(f'    GBDC: 80%, TSLX/OBDC/BXSL/ARCC: 5% each')
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
    print(f'  Cash (BIL): {meta["cash_shares"]:.0f} shares')

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


if __name__ == '__main__':
    main()
