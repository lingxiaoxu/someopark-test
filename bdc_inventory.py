#!/usr/bin/env python3
"""
bdc_inventory.py — BDC sleeve 持仓文件层(2026-08-11,用户令:持仓不再写死代码)

与其它策略对齐的持仓文件体系:
  inventory_bdc.json                    当前持仓(root,与 inventory_mrpt/mtfs 并列)
  inventory_history/inventory_bdc_*.json  历史快照(事件驱动: inception + 每次 DRIP)

文件是唯一 source of truth,三个消费方从这里索引(不再各自写死):
  UpdateBDCPerformance.py   tickers/weights/cash_ticker/allocation(+运行后回写 shares)
  RefreshBDCHoldings.py     BDC_UNIVERSE(cik + sleeve_w)
  portfolio_of_private_credit_deals/bdc_lookthrough.py   BDC_ALLOC

结构(weight = sleeve 内相对权重,和为 1;sleeve 占 allocation.bdc):
  {"strategy": "bdc_sleeve", "as_of": "...", "inception_date": "...",
   "allocation": {"bdc": 0.5, "cash": 0.5},
   "cash": {"ticker": "BIL", "shares": ...},
   "holdings": {"GBDC": {"weight": 0.8, "cik": 1476765, "shares": ..,
                          "entry_date": "...", "drip_events": n}, ...}}

shares 仅由 DRIP 演化(HOLD 不改仓,与 pairs inventory 的纪律一致)。
校验:holdings 权重和=1、allocation 和=1,load 失败大声 raise(绝不静默回退,
文件随 repo 提交,缺失即配置错误)。

python bdc_inventory.py --backfill   重放 DRIP 生成全部历史快照 + 当前文件
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INVENTORY_PATH = os.path.join(BASE_DIR, 'inventory_bdc.json')
HISTORY_DIR = os.path.join(BASE_DIR, 'inventory_history')

# 用于 --backfill 首次生成的种子定义(生成之后,文件即唯一真源;这里只是初始化种子,
# 与 2025-11-11 建仓时的实际配置一致,不再被任何运行路径读取)。
_SEED = {
    "allocation": {"bdc": 0.50, "cash": 0.50},
    "cash_ticker": "BIL",
    "holdings": {
        "GBDC": {"weight": 0.80, "cik": 1476765},
        "TSLX": {"weight": 0.05, "cik": 1508655},
        "OBDC": {"weight": 0.05, "cik": 1655888},
        "BXSL": {"weight": 0.05, "cik": 1736035},
        "ARCC": {"weight": 0.05, "cik": 1287750},
    },
}


def load_inventory(path: str = INVENTORY_PATH) -> dict:
    """读 + 校验。失败 raise(文件随 repo 存在,缺失/破损=配置错误,绝不静默)。"""
    if not os.path.exists(path):
        raise RuntimeError(f"BDC inventory missing: {path} — run "
                           f"`python bdc_inventory.py --backfill` once to create it")
    inv = json.load(open(path))
    w = sum(h["weight"] for h in inv["holdings"].values())
    a = sum(inv["allocation"].values())
    if abs(w - 1.0) > 1e-6 or abs(a - 1.0) > 1e-6:
        raise RuntimeError(f"BDC inventory invalid: holdings weights sum {w}, "
                           f"allocation sum {a} (both must be 1.0)")
    return inv


def save_inventory(inv: dict, path: str = INVENTORY_PATH,
                   snapshot: bool = True) -> None:
    """原子写当前文件;shares/权重相对上一版有变化时另存历史快照。"""
    inv = dict(inv)
    inv["last_updated"] = datetime.now().isoformat(timespec="seconds")
    changed = True
    if os.path.exists(path):
        try:
            prev = json.load(open(path))
            key = lambda d: ({t: (round(h.get("shares", 0), 4), h["weight"])
                              for t, h in d["holdings"].items()},
                             round(d.get("cash", {}).get("shares", 0), 4))
            changed = key(prev) != key(inv)
        except Exception:  # noqa: BLE001 — 旧文件破损按有变化处理
            pass
    tmp = path + ".tmp"
    json.dump(inv, open(tmp, "w"), indent=2)
    os.replace(tmp, path)
    if snapshot and changed:
        os.makedirs(HISTORY_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap = os.path.join(HISTORY_DIR, f"inventory_bdc_{ts}.json")
        json.dump(inv, open(snap, "w"), indent=2)
        print(f"  [bdc_inventory] snapshot -> {os.path.basename(snap)}")


def write_ledger(trades: list[dict], path: str | None = None) -> None:
    """原子重写逐笔流水(全量确定性重放产物,幂等——自动同步无需手动维护)。"""
    p = path or LEDGER_PATH
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        for tr in trades:
            fh.write(json.dumps(tr) + "\n")
    os.replace(tmp, p)


def update_shares_from_run(meta: dict, as_of: str,
                           path: str = INVENTORY_PATH) -> None:
    """UpdateBDCPerformance 跑完后回写 DRIP 演化的 shares + 自动同步 ledger
    (dry-run 不调用)。meta['trades'] 是当次全期重放的逐笔流水 → 原子重写
    trade_ledger_bdc.jsonl,新 DRIP 自动入账,持仓/历史快照/流水三层始终一致。"""
    inv = load_inventory(path)
    for t, h in inv["holdings"].items():
        if t in meta.get("final_shares_bdc", {}):
            h["shares"] = float(meta["final_shares_bdc"][t])
            h["drip_events"] = int(meta["div_stats"][t]["count"])
    inv["cash"]["shares"] = float(meta["cash_shares"])
    cash_t = inv["cash"]["ticker"]
    if cash_t in meta.get("div_stats", {}):
        inv["cash"]["drip_events"] = int(meta["div_stats"][cash_t]["count"])
    inv["as_of"] = as_of
    save_inventory(inv, path)
    if meta.get("trades"):
        write_ledger(meta["trades"])
        print(f"  [bdc_inventory] ledger auto-synced: {len(meta['trades'])} trades")


# ── 历史回算(--backfill,一次性)──────────────────────────────────────────
LEDGER_PATH = os.path.join(BASE_DIR, 'trade_ledger_bdc.jsonl')


def backfill(write: bool = True, write_ledger: bool = False) -> dict:
    """重放 build_portfolio 的 DRIP 口径(收盘价再投,同 UpdateBDCPerformance),
    在 inception 与每个 DRIP 事件日生成历史快照,最终态写 inventory_bdc.json。
    历史快照文件名用事件日 16:00(收盘)合成时间戳,as_of=事件日。

    write_ledger: 同一次重放顺带产出逐笔流水 trade_ledger_bdc.jsonl(与
    trade_ledger_mrpt/mtfs 同放 repo 根;行格式对齐 pairs ledger 的共通核心
    date/ticker/side/shares/price/gross/dedup_key,外加 BDC 语义 action:
    OPEN=建仓买入,DRIP=股息再投买入(带 div_per_share/div_cash)。"""
    import pandas as pd
    import yfinance as yf

    perf = os.path.join(BASE_DIR, 'someo-park-investment-management', 'public',
                        'data', 'strategy_performance.json')
    first = json.load(open(perf))[0]
    inception = first['date']
    target = first['mrpt_equity'] + first['mtfs_equity']

    tickers = list(_SEED["holdings"])
    cash_t = _SEED["cash_ticker"]
    alloc = _SEED["allocation"]
    end = (datetime.now() + pd.Timedelta(days=2)).strftime('%Y-%m-%d')
    px = yf.download(tickers + [cash_t], start=inception, end=end,
                     auto_adjust=False, progress=False)['Close'].dropna(how='all')
    divs = {}
    for t in tickers + [cash_t]:
        d = yf.Ticker(t).dividends
        d = d[d.index >= inception]
        d.index = d.index.tz_localize(None)
        divs[t] = d

    trades: list[dict] = []
    shares = {}
    for t in tickers:
        p0 = float(px[t].dropna().iloc[0])
        shares[t] = target * alloc["bdc"] * _SEED["holdings"][t]["weight"] / p0
        trades.append({"date": inception, "ticker": t, "side": "BUY",
                       "shares": round(shares[t], 4), "price": round(p0, 4),
                       "gross": round(-shares[t] * p0, 2), "action": "OPEN",
                       "dedup_key": f"{inception}-{t}-BUY-OPEN"})
    p0c = float(px[cash_t].dropna().iloc[0])
    cash_shares = target * alloc["cash"] / p0c
    trades.append({"date": inception, "ticker": cash_t, "side": "BUY",
                   "shares": round(cash_shares, 4), "price": round(p0c, 4),
                   "gross": round(-cash_shares * p0c, 2), "action": "OPEN",
                   "dedup_key": f"{inception}-{cash_t}-BUY-OPEN"})
    drip_n = {t: 0 for t in tickers + [cash_t]}

    def state(as_of: str) -> dict:
        return {
            "strategy": "bdc_sleeve", "as_of": as_of,
            "inception_date": inception,
            "allocation": dict(alloc),
            "cash": {"ticker": cash_t, "shares": round(cash_shares, 4),
                     "entry_date": inception, "drip_events": drip_n[cash_t]},
            "holdings": {t: {"weight": _SEED["holdings"][t]["weight"],
                             "cik": _SEED["holdings"][t]["cik"],
                             "shares": round(shares[t], 4),
                             "entry_date": inception,
                             "drip_events": drip_n[t]} for t in tickers},
            "note": "weights are within-sleeve (sum 1); sleeve = allocation.bdc of the "
                    "PC book. shares evolve by DRIP only (close-price reinvest, same "
                    "protocol as UpdateBDCPerformance.build_portfolio).",
        }

    snapshots = [state(inception)]
    for dt in px.index:
        d_str = dt.strftime('%Y-%m-%d')
        moved = False
        for t in tickers:
            if dt in divs[t].index:
                c = px[t].get(dt)
                if c is not None and not pd.isna(c) and float(c) > 0:
                    dps = float(divs[t][dt])
                    div_cash = shares[t] * dps
                    new_sh = div_cash / float(c)
                    trades.append({"date": d_str, "ticker": t, "side": "BUY",
                                   "shares": round(new_sh, 4),
                                   "price": round(float(c), 4),
                                   "gross": round(-div_cash, 2), "action": "DRIP",
                                   "div_per_share": dps,
                                   "div_cash": round(div_cash, 2),
                                   "dedup_key": f"{d_str}-{t}-BUY-DRIP"})
                    shares[t] += new_sh
                    drip_n[t] += 1
                    moved = True
        if dt in divs[cash_t].index:
            c = px[cash_t].get(dt)
            if c is not None and not pd.isna(c) and float(c) > 0:
                dps = float(divs[cash_t][dt])
                div_cash = cash_shares * dps
                new_sh = div_cash / float(c)
                trades.append({"date": d_str, "ticker": cash_t, "side": "BUY",
                               "shares": round(new_sh, 4),
                               "price": round(float(c), 4),
                               "gross": round(-div_cash, 2), "action": "DRIP",
                               "div_per_share": dps,
                               "div_cash": round(div_cash, 2),
                               "dedup_key": f"{d_str}-{cash_t}-BUY-DRIP"})
                cash_shares += new_sh
                drip_n[cash_t] += 1
                moved = True
        if moved:
            snapshots.append(state(d_str))

    if write:
        os.makedirs(HISTORY_DIR, exist_ok=True)
        for s in snapshots:
            ts = s["as_of"].replace("-", "") + "_160000"
            p = os.path.join(HISTORY_DIR, f"inventory_bdc_{ts}.json")
            json.dump({**s, "last_updated": f"{s['as_of']}T16:00:00"},
                      open(p, "w"), indent=2)
        cur = dict(snapshots[-1])
        cur["as_of"] = px.index[-1].strftime('%Y-%m-%d')
        save_inventory(cur, snapshot=False)   # 当前文件;历史已逐事件落盘
        print(f"[bdc_inventory] backfill: {len(snapshots)} snapshots "
              f"({inception} -> {cur['as_of']}) + inventory_bdc.json")
    if write_ledger:
        with open(LEDGER_PATH, "w") as fh:
            for tr in trades:
                fh.write(json.dumps(tr) + "\n")
        print(f"[bdc_inventory] ledger: {len(trades)} trades -> {LEDGER_PATH}")
    return {"n_snapshots": len(snapshots), "n_trades": len(trades),
            "final_shares": {t: round(shares[t], 1) for t in tickers},
            "cash_shares": round(cash_shares, 1),
            "drip_events": drip_n}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description="BDC sleeve inventory file layer")
    ap.add_argument("--backfill", action="store_true",
                    help="重放 DRIP 生成历史快照系列 + 当前 inventory_bdc.json")
    ap.add_argument("--ledger", action="store_true",
                    help="同一次重放顺带产出逐笔流水 trade_ledger_bdc.jsonl")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.backfill or a.ledger:
        res = backfill(write=(a.backfill and not a.dry_run),
                       write_ledger=(a.ledger and not a.dry_run))
        print(json.dumps(res, indent=2))
    else:
        print(json.dumps(load_inventory(), indent=2)[:800])
