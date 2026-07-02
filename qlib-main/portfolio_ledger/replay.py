"""历史重建（replay）：从 live start 以 $1M 起步重演每个交易日的记账。

用法（qlib_run 或 someopark_run 均可，纯 pandas/json）：
    cd qlib-main && python -m portfolio_ledger.replay [aiss|ssrs|all] [--force]

产出：account_{strat}.json + account_history/*.json + trade_ledger_{strat}.jsonl
验证：期末持仓 == 当前 inventory；恒等式全程断言（process_day 内）；
     拆股日（KLAC 6/12）equity 连续（口径归一在读入层完成，账内无拆股事件）。
"""
from __future__ import annotations

import json
import logging
import os
import sys

import pandas as pd

from .ledger import (STRATEGIES, Account, account_path, history_dir, ledger_path,
                     load_snapshots, load_splits_by_ticker, load_store_prices,
                     load_fees_by_date, process_day, _prepare_dividends, _cfg)

log = logging.getLogger("portfolio_ledger.replay")


def replay(strategy: str, force: bool = False) -> Account:
    cfg = _cfg(strategy)
    if os.path.exists(account_path(strategy)) and not force:
        raise SystemExit(f"[{strategy}] account 已存在；--force 重建")
    if force:
        for fp in (account_path(strategy), ledger_path(strategy)):
            if os.path.exists(fp):
                os.remove(fp)
        hd = history_dir(strategy)
        if os.path.isdir(hd):
            for f in os.listdir(hd):
                if f.startswith(f"account_{strategy}_"):
                    os.remove(os.path.join(hd, f))

    splits = load_splits_by_ticker()
    snaps = load_snapshots(strategy, splits)
    live_start = cfg["live_start"]
    snap_days = sorted(d for d in snaps if d >= live_start)
    if not snap_days:
        raise SystemExit(f"[{strategy}] {live_start} 起无快照")
    first, last = snap_days[0], snap_days[-1]
    tickers = sorted({t for d in snap_days for t in snaps[d]})
    log.info(f"[{strategy}] replay {first} → {last}, universe={tickers}")

    prices = load_store_prices(strategy, tickers)
    divs = _prepare_dividends(strategy, tickers, first, last, splits)
    fees = load_fees_by_date(strategy)

    # 期初建账（plan §4.5-1：opening balance，不合成虚拟交易）
    acct = Account.open_from_snapshot(strategy, first, snaps[first])
    ts0 = pd.Timestamp(first)
    if ts0 not in prices.index:
        raise SystemExit(f"[{strategy}] live start {first} 不是交易日？")
    acct.mark(first, prices.loc[ts0])
    acct.save_history(first)
    log.info(f"[{strategy}] 期初 {first}: cash=${acct.data['cash']:,.2f} "
             f"持仓 {len(acct.data['positions'])} 只 equity=${acct.data['equity']:,.2f}")

    seen: set = set()
    days = [str(d.date()) for d in prices.index if first < str(d.date()) <= last]
    for day in days:
        process_day(acct, day, prices, snaps, divs, fees, seen)
    acct.save()

    _validate(strategy, acct, snaps[last])
    d = acct.data
    print(f"\n[{strategy}] REPLAY 完成 {first} → {d['as_of']}")
    print(f"  equity        ${d['equity']:>14,.2f}   (初始 $1,000,000)")
    print(f"  cash          ${d['cash']:>14,.2f}")
    print(f"  持仓市值      ${d['position_value']:>14,.2f}")
    print(f"  已实现(交易)  ${d['cumulative_realized']:>14,.2f}")
    print(f"  分红收入      ${d['cumulative_dividends']:>14,.2f}")
    print(f"  费用          ${d['cumulative_fees']:>14,.2f}")
    print(f"  未实现        ${d['unrealized']:>14,.2f}")
    return acct


def _validate(strategy: str, acct: Account, last_snap: dict):
    """期末持仓必须与最新快照逐票一致。"""
    mism = []
    snap_shares = {t: h["shares"] for t, h in last_snap.items() if h["shares"]}
    acct_shares = {t: p["shares"] for t, p in acct.data["positions"].items()}
    for t in sorted(set(snap_shares) | set(acct_shares)):
        a, b = acct_shares.get(t, 0), snap_shares.get(t, 0)
        if a != b:
            mism.append(f"{t}: account={a} snapshot={b}")
    if mism:
        raise AssertionError(f"[{strategy}] 期末持仓与快照不符: {mism}")
    log.info(f"[{strategy}] 期末持仓校验通过（{len(acct_shares)} 只）")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    force = "--force" in sys.argv
    targets = list(STRATEGIES) if (not args or args[0] == "all") else [args[0]]
    for s in targets:
        replay(s, force=force)


if __name__ == "__main__":
    main()
