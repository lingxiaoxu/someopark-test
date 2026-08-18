#!/usr/bin/env python3
"""
Generate private_credit_bdc_performance.json for the Private Credit BDC + Cash portfolio.

Allocation: 50% BDC, 50% Cash (money market / T-bills)
BDC weights: GBDC 80%, TSLX/OBDC/BXSL/ARCC each 5%
Cash: tracks BIL (SPDR 1-3 Month T-Bill ETF) as money market proxy
Dividends: reinvested (DRIP) at ex-date close price

Price/dividend source: Polygon (via PriceDataStore) — same ledger-grade source as
RiskManager/PnLReport. yfinance was dropped 2026-08-18: it silently lost an entire
trading day (8/17), which cost this file a row and blew the R11 red line elsewhere.
A single source is the point — do not add a yfinance fallback.

Output combines BDC + Cash into a single equity series (bdc_equity).

Usage:
    python UpdateBDCPerformance.py
    python UpdateBDCPerformance.py --dry-run
"""

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

from PriceDataStore import PriceDataStore

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


# Polygon v3 dividend_type 已知取值——四者都是现金分配,DRIP 全部再投(与被替换的
# yfinance `Ticker.dividends` 口径一致,后者也是一锅端所有现金分配)。
# CD=regular cash / SC=special cash / LT=long-term cap-gain / ST=short-term cap-gain。
# 出现白名单外的类型不静默吞也不静默收——大声 WARN 后仍计入,由人来判。
_KNOWN_DIV_TYPES = {'CD', 'SC', 'LT', 'ST'}


def _fetch_prices_and_dividends(tickers: list[str], start: str, end: str):
    """Polygon 取收盘价 + 分红。返回 (close_prices, dividends)。

    close_prices: DataFrame[date × ticker],拆股调整、不含分红调整
                  (Polygon adjusted=true == yfinance auto_adjust=False 的 Close)
    dividends:    dict[ticker] -> Series,索引 = ex-date

    两个必须踩住的坑:
      1. PriceDataStore 的索引是 04:00 (Polygon 的 UTC 毫秒戳),yfinance 是 00:00。
         下游 DRIP 用 `date_idx in dividends[t].index` **精确匹配**——不 normalize
         就一笔都对不上,分红静默全丢、净值静默走低。同款坑见 PnLReport.py 的
         section-6 exec-Open overlay 注释。
      2. store.load 内部按 `.loc[start:end]` 裁剪,end 被解析成当日 00:00,而数据点
         在 04:00 → **直接传 end 会把最后一天裁掉**。仓库既有调用方都靠多要 1-2 天
         补偿(RiskManager: signal_date+1;PnLReport: 报告末+2)。这里同样多要一天,
         但不能越过本 ISO 周的周日:整周落在未来的请求 Polygon 回
         403 NOT_AUTHORIZED,store 三连重试后 raise(实测)。
    """
    _end_d = pd.Timestamp(end).normalize()
    _sunday = _end_d + pd.Timedelta(days=6 - _end_d.weekday())
    end_req = str(min(_end_d + pd.Timedelta(days=1), _sunday).date())

    # base_dir 是**仓库根**,不是 price_data —— PriceDataStore.__init__ 自己会拼
    # `os.path.join(base_dir, 'price_data')`。传 'price_data' 会落到
    # price_data/price_data/ 另起一套缓存(仓库里那个多余目录就是这么来的,
    # PnLReport.py:729 至今还这么传),同时也就用不上生产的 dividends_cache.json。
    api_key = os.environ.get('POLYGON_API_KEY', '')
    if not api_key:
        # 空 key 会变成每票每周 401→三连退避重试的风暴,最后抛一个跟根因无关的
        # HTTPError。直说。
        sys.exit('[ERROR] POLYGON_API_KEY 未设置(需先 source .env)')
    store = PriceDataStore(BASE_DIR, api_key)
    raw = store.load(tickers, start, end_req)
    if raw.empty:
        sys.exit('[ERROR] No price data downloaded from Polygon')
    close_prices = raw['Close'].copy()
    close_prices.index = pd.DatetimeIndex(close_prices.index).normalize()  # 坑 1
    close_prices = close_prices[~close_prices.index.duplicated(keep='last')]
    close_prices = close_prices.dropna(how='all')

    dividends: dict[str, pd.Series] = {}
    unknown: dict[str, set] = {}
    for ticker in tickers:
        recs = store._fetch_dividends(ticker, start, end)
        bad = {r.get('dividend_type') for r in recs} - _KNOWN_DIV_TYPES
        if bad:
            unknown[ticker] = bad
        by_ex: dict[str, float] = {}
        for r in recs:                       # 同一 ex-date 多笔(如常规+特别)相加
            by_ex[r['ex_dividend_date']] = (by_ex.get(r['ex_dividend_date'], 0.0)
                                            + float(r['cash_amount']))
        s = pd.Series(by_ex, dtype=float)
        s.index = pd.DatetimeIndex(s.index).normalize()                    # 坑 1
        dividends[ticker] = s.sort_index()
    if unknown:
        print(f'  [WARN] 未知 dividend_type(仍计入 DRIP,请人工确认口径): {unknown}')

    # cash_amount 是**未按其后拆股还原**的每股金额(Polygon 文档明示),而收盘价是
    # 拆股调整后的 → 窗口内一旦发生拆股,分红/股与价格就不同基。同时本脚本的
    # shares 是从 inception 累加的,拆股本身也会打断股数链(替换前的 yfinance 版
    # 同样如此,非本次引入)。所以只做检测 + 大声报警,不做假装无事的换算。
    try:
        from CorporateActions import _load_splits_cache
        hits = [sp for sp in _load_splits_cache().get('results', [])
                if sp.get('ticker') in set(tickers)
                and start <= sp.get('execution_date', '') <= end]
        if hits:
            print(f'  [WARN] 窗口内有拆股,DRIP 股数链与分红基准均需人工复核: '
                  f'{[(h["ticker"], h.get("execution_date")) for h in hits]}')
    except Exception as e:                                       # noqa: BLE001
        print(f'  [WARN] 拆股检测跳过(splits cache 不可读): {e}')

    return close_prices, dividends


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
    # 终点钳到今天:Polygon 对未来起点回 403 → store 三连重试后 raise。原 yfinance
    # 版用 now+2d 是为了让 yf 的右开区间包含今天,Polygon 是闭区间不需要。
    end_date = str(pd.Timestamp.now().normalize().date())
    bdc_capital = target_start * BDC_ALLOC
    cash_capital = target_start * CASH_ALLOC

    # === BDC portion ===
    all_tickers = BDC_TICKERS + [CASH_TICKER]
    close_prices, dividends = _fetch_prices_and_dividends(
        all_tickers, inception_date, end_date)

    # 缺价必须致命。下面的净值累加对 NaN 是 `if not isna: 加` —— 少一票就**静默**
    # 少一份市值,曲线凭空塌一个坑再弹回来,没有任何报错。yfinance 时代这事真发生
    # 过:2026-07-21/22/31 三天随机丢 GBDC/BXSL/OBDC/TSLX(每次调用丢的票还不一样),
    # 已发布的 bdc_equity 里因此有三天假悬崖(−$65k/−$64k/−$42k),并顺着
    # master_portfolio_performance 传到前端和 controller 官方锚。
    # 宁可整份不出、保留昨天那份好的,也不能发一条假曲线。
    #
    # 整列缺失单独判:否则下面 close_prices[t] 抛 KeyError,报出来的是一个跟根因
    # 无关的堆栈。票改名(见 ticker_aliases.py)或退市会走到这里。
    _missing = [t for t in all_tickers if t not in close_prices.columns]
    if _missing:
        sys.exit(f'[ERROR] Polygon 未返回这些票的任何数据(改名/退市?): {_missing}')
    # **任何** NaN 都致命,包括序列开头的。曾经这里只判 first_valid_index 之后的
    # 缺口,理由是"上市晚的票开头本来就没数据"——但那恰恰也是错的:开头缺价时
    # 建仓价取的是首个非 NaN(晚于 inception),而缺价那几天该票被静默排除在净值
    # 之外,组合凭空少一块再突然补上,和 7/21 那三天是同一种假悬崖。这六票实测
    # 自 inception 起零 NaN(192×6),所以严判不会误伤;真有新票入池时也应该由人
    # 显式处理建仓口径,而不是让守卫替它猜。
    _holes = {t: [str(d.date()) for d in close_prices.index[close_prices[t].isna()]]
              for t in all_tickers}
    _holes = {t: v for t, v in _holes.items() if v}
    if _holes:
        sys.exit(f'[ERROR] 价格序列有缺口,拒绝产出(否则净值出假悬崖): '
                 + '; '.join(f'{t}: {v[:5]}{"..." if len(v) > 5 else ""}'
                             for t, v in _holes.items()))

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
