"""
MomentumPairSelector.py
=======================
基于 2026-03-11 数据，从 SP500 中选出 30 支股票（15 对）
做 Momentum Trend Following Pair Trading 回测。

逻辑：
1. 从 Wikipedia 获取 SP500 列表（~503支）
2. 过滤掉 2023-07-01 后上市的股票（保留老股票）
3. 计算过去30个交易日收益率（截至2026-03-11）
4. 取 top15 winners + bottom15 losers，按排名配对
5. 回测期：2025-09-11 ~ 2026-03-11（约6个月）
6. 策略：做多winner，做空loser（dollar-neutral），月度rebalance
"""

import os
import sys
import json
import time
import requests
import numpy as np
import pandas as pd
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PriceDataStore import PriceDataStore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_KEY  = os.environ.get('POLYGON_API_KEY', '')

BACKTEST_END    = '2026-03-11'
BACKTEST_START  = '2025-09-01'
MOMENTUM_END    = '2026-03-11'
MOMENTUM_START  = '2026-01-01'   # 넉넉하게 당기기
MOMENTUM_DAYS   = 30
LISTING_CUTOFF  = '2023-07-01'

# ─────────────────────────────────────────────
# STEP 1: SP500 universe
# ─────────────────────────────────────────────
print("=" * 65)
print("STEP 1: Fetching S&P 500 ticker universe...")
print("=" * 65)

sp500_tickers = []
try:
    tables = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies', timeout=15)
    df_sp500 = tables[0]
    # column might be 'Symbol' or 'Ticker symbol'
    sym_col = [c for c in df_sp500.columns if 'symbol' in c.lower() or 'ticker' in c.lower()][0]
    sp500_tickers = df_sp500[sym_col].str.replace('.', '-', regex=False).tolist()
    print(f"  Wikipedia: {len(sp500_tickers)} tickers loaded")
except Exception as e:
    print(f"  Wikipedia failed: {e}")
    # Fallback: Polygon REST — grab XNYS + XNAS, large cap
    for exchange in ['XNYS', 'XNAS']:
        url = (f"https://api.polygon.io/v3/reference/tickers"
               f"?market=stocks&exchange={exchange}&active=true"
               f"&limit=250&apiKey={API_KEY}")
        r = requests.get(url, timeout=30)
        if r.status_code == 200:
            sp500_tickers += [t['ticker'] for t in r.json().get('results', [])]
    print(f"  Polygon fallback: {len(sp500_tickers)} tickers")

# Remove obvious non-common-stock tickers
sp500_tickers = [t for t in sp500_tickers
                 if t.isalpha() and len(t) <= 5 and t == t.upper()]
print(f"  After basic filter: {len(sp500_tickers)} tickers")

# ─────────────────────────────────────────────
# STEP 2: Filter by listing date (batch via Polygon)
# ─────────────────────────────────────────────
print(f"\nSTEP 2: Filtering listing_date <= {LISTING_CUTOFF} ...")

BATCH_SIZE = 50
filtered = []
cutoff_dt = datetime.strptime(LISTING_CUTOFF, '%Y-%m-%d').date()

for i in range(0, len(sp500_tickers), BATCH_SIZE):
    batch = sp500_tickers[i:i+BATCH_SIZE]
    tickers_csv = ','.join(batch)
    url = (f"https://api.polygon.io/v3/reference/tickers"
           f"?ticker.any_of={tickers_csv}&market=stocks&active=true"
           f"&limit={BATCH_SIZE}&apiKey={API_KEY}")
    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 200:
            for t in r.json().get('results', []):
                ld = t.get('list_date') or t.get('primary_exchange', '')
                if ld:
                    try:
                        if datetime.strptime(ld[:10], '%Y-%m-%d').date() <= cutoff_dt:
                            filtered.append(t['ticker'])
                    except:
                        filtered.append(t['ticker'])  # no date → assume old
                else:
                    filtered.append(t['ticker'])  # no date info → keep
        time.sleep(0.15)
    except Exception as e:
        # network hiccup → keep all in batch
        filtered.extend(batch)
        time.sleep(0.5)
    done = min(i + BATCH_SIZE, len(sp500_tickers))
    print(f"  Batch {i//BATCH_SIZE+1}/{(len(sp500_tickers)+BATCH_SIZE-1)//BATCH_SIZE}"
          f"  checked {done}/{len(sp500_tickers)}, passing={len(filtered)}", end='\r')

print(f"\n  Tickers passing listing filter: {len(filtered)}")

# ─────────────────────────────────────────────
# STEP 3: Fetch price data for momentum
# ─────────────────────────────────────────────
print(f"\nSTEP 3: Fetching price data for momentum selection...")
store = PriceDataStore(base_dir=BASE_DIR, polygon_api_key=API_KEY, api_delay=0.15)

# Load 2-month window for momentum calc
prices_mom = store.load(filtered, start_date=MOMENTUM_START, end_date=MOMENTUM_END)
print(f"  Loaded momentum prices: {prices_mom.shape}")

# Extract Close (MultiIndex: level0=Price, level1=Ticker)
if isinstance(prices_mom.columns, pd.MultiIndex):
    price_field = 'Close'
    adj_close_mom = prices_mom.xs(price_field, axis=1, level=0)
else:
    adj_close_mom = prices_mom

# ─────────────────────────────────────────────
# STEP 4: Compute 30-day momentum
# ─────────────────────────────────────────────
print(f"\nSTEP 4: Computing {MOMENTUM_DAYS}-day momentum (as of {MOMENTUM_END})...")

adj_close_mom = adj_close_mom.sort_index()
available = adj_close_mom.columns.tolist()

# Need at least MOMENTUM_DAYS+5 rows
valid = []
for t in available:
    series = adj_close_mom[t].dropna()
    if len(series) >= MOMENTUM_DAYS:
        valid.append(t)

adj_close_mom = adj_close_mom[valid]
print(f"  Tickers with sufficient data: {len(valid)}")

# 30-day return: last price vs price MOMENTUM_DAYS bars ago
last_prices  = adj_close_mom.iloc[-1]
start_prices = adj_close_mom.iloc[-MOMENTUM_DAYS]
momentum = (last_prices / start_prices - 1).dropna().sort_values(ascending=False)

print(f"  Momentum computed for {len(momentum)} tickers")

# ─────────────────────────────────────────────
# STEP 5: Select winners and losers, form pairs
# ─────────────────────────────────────────────
print(f"\nSTEP 5: Selecting top/bottom 15 and forming 15 pairs...")

winners = momentum.head(15)
losers  = momentum.tail(15).sort_values(ascending=True)  # worst first

print("\n" + "=" * 65)
print(f"30-Day Return Rankings (as of {MOMENTUM_END})")
print("=" * 65)
print("\nTop 15 Winners:")
for rank, (ticker, ret) in enumerate(winners.items(), 1):
    print(f"  {rank:2d}. {ticker:<6s}  {ret:+.1%}")

print("\nBottom 15 Losers:")
for rank, (ticker, ret) in enumerate(losers.items(), 1):
    print(f"  {rank:2d}. {ticker:<6s}  {ret:+.1%}")

pairs = list(zip(winners.index.tolist(), losers.index.tolist()))
print("\n" + "=" * 65)
print("15 Pairs  (Long Winner / Short Loser)")
print("=" * 65)
for i, (w, l) in enumerate(pairs, 1):
    print(f"  Pair {i:2d}: {w} (long) / {l} (short)")

# ─────────────────────────────────────────────
# STEP 6: Backtest
# ─────────────────────────────────────────────
print(f"\nSTEP 6: Running backtest ({BACKTEST_START} ~ {BACKTEST_END})...")

all_syms = list(set([s for p in pairs for s in p]))
prices_bt = store.load(all_syms, start_date=BACKTEST_START, end_date=BACKTEST_END)
print(f"  Loaded backtest prices: {prices_bt.shape}")

if isinstance(prices_bt.columns, pd.MultiIndex):
    adj_bt = prices_bt.xs('Close', axis=1, level=0)
else:
    adj_bt = prices_bt

adj_bt = adj_bt.sort_index().ffill()

ANNUAL_FACTOR = np.sqrt(252)

results = []
for w, l in pairs:
    if w not in adj_bt.columns or l not in adj_bt.columns:
        print(f"  Skipping {w}/{l}: missing data")
        continue

    pair_df = adj_bt[[w, l]].dropna()
    if len(pair_df) < 20:
        print(f"  Skipping {w}/{l}: too few rows ({len(pair_df)})")
        continue

    # Daily returns
    w_ret = pair_df[w].pct_change().dropna()
    l_ret = pair_df[l].pct_change().dropna()

    # Dollar-neutral: long winner, short loser
    # pnl = 0.5*(w_ret - l_ret)  → spread return
    spread_ret = (w_ret - l_ret) / 2.0

    # Momentum signal: enter when 20-day momentum of spread is positive
    # Exit (flatten) when it turns negative → simple trend filter
    mom_signal = spread_ret.rolling(20).mean()
    signal = (mom_signal > 0).astype(int)  # 1=long spread, 0=flat
    signal = signal.shift(1).fillna(0)     # trade next day

    strat_ret = spread_ret * signal

    # Metrics
    total_ret   = (1 + strat_ret).prod() - 1
    ann_ret     = (1 + total_ret) ** (252 / len(strat_ret)) - 1
    vol         = strat_ret.std() * ANNUAL_FACTOR
    sharpe      = ann_ret / vol if vol > 0 else 0

    cum = (1 + strat_ret).cumprod()
    roll_max = cum.cummax()
    drawdown = (cum - roll_max) / roll_max
    max_dd   = drawdown.min()

    # Trades: number of times signal flips from 0→1
    trades = int((signal.diff() == 1).sum())
    # Win rate: % of days in trade with positive return
    in_trade = strat_ret[signal == 1]
    win_rate  = (in_trade > 0).mean() if len(in_trade) > 0 else 0

    results.append({
        'Pair'       : f"{w}/{l}",
        'Winner'     : w,
        'Loser'      : l,
        'Sharpe'     : sharpe,
        'TotalRet'   : total_ret,
        'AnnRet'     : ann_ret,
        'MaxDD'      : max_dd,
        'Trades'     : trades,
        'WinRate'    : win_rate,
        'DaysInTrade': int((signal == 1).sum()),
    })

df_results = pd.DataFrame(results).sort_values('Sharpe', ascending=False)

print("\n" + "=" * 65)
print("BACKTEST RESULTS  (2025-09-11 ~ 2026-03-11)")
print("=" * 65)
print(f"\n{'Pair':<14} {'Sharpe':>7} {'TotalRet':>9} {'AnnRet':>8} {'MaxDD':>8} {'Trades':>7} {'WinRate':>8}")
print("-" * 65)
for _, row in df_results.iterrows():
    print(f"{row['Pair']:<14} {row['Sharpe']:>7.2f} {row['TotalRet']:>8.1%} "
          f"{row['AnnRet']:>7.1%} {row['MaxDD']:>7.1%} {row['Trades']:>7d} {row['WinRate']:>7.1%}")

# Portfolio summary
if len(df_results) > 0:
    port_ret_series = None
    for _, row in df_results.iterrows():
        w, l = row['Winner'], row['Loser']
        if w not in adj_bt.columns or l not in adj_bt.columns:
            continue
        pair_df = adj_bt[[w, l]].dropna()
        w_ret   = pair_df[w].pct_change().dropna()
        l_ret   = pair_df[l].pct_change().dropna()
        spread  = (w_ret - l_ret) / 2.0
        mom_sig = spread.rolling(20).mean()
        sig     = (mom_sig > 0).astype(int).shift(1).fillna(0)
        s_ret   = spread * sig
        if port_ret_series is None:
            port_ret_series = s_ret
        else:
            port_ret_series = port_ret_series.add(s_ret, fill_value=0)

    if port_ret_series is not None:
        port_ret_series /= len(df_results)
        port_total = (1 + port_ret_series).prod() - 1
        port_ann   = (1 + port_total) ** (252 / len(port_ret_series)) - 1
        port_vol   = port_ret_series.std() * ANNUAL_FACTOR
        port_sharpe= port_ann / port_vol if port_vol > 0 else 0
        cum_p      = (1 + port_ret_series).cumprod()
        port_mdd   = ((cum_p - cum_p.cummax()) / cum_p.cummax()).min()

        print("\n" + "=" * 65)
        print("PORTFOLIO SUMMARY  (equal-weighted, 15 pairs)")
        print("=" * 65)
        print(f"  Portfolio Sharpe:        {port_sharpe:.2f}")
        print(f"  Portfolio Total Return:  {port_total:+.1%}")
        print(f"  Portfolio Ann. Return:   {port_ann:+.1%}")
        print(f"  Portfolio Max Drawdown:  {port_mdd:.1%}")
        print(f"  Number of Pairs:         {len(df_results)}")

print("\nDone.")
