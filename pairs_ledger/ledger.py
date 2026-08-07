"""记账内核：账户状态、多空通用成交、恒等式断言、逐日落盘。

schema 与 qlib-main/portfolio_ledger 逐字段同构（只读参考其设计，未修改其代码）：
  account_{strat}.json  as_of/base_currency/initial_cash/cash/positions/
                        cumulative_realized/cumulative_dividends/cumulative_fees/
                        equity/unrealized/position_value/liabilities
  positions[ticker]     shares(带符号)/avg_cost/entry_date
  trade_ledger_{s}.jsonl  date/ticker/side/shares/price/gross/dedup_key
                        (+SELL 侧 avg_cost_at_trade/realized_pnl;+price_basis/ext_order_id)

金额 USD。股数当前口径（拆股归一在读入层完成，账内无拆股事件）。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import pandas as pd

log = logging.getLogger("pairs_ledger")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INITIAL_CASH = 1_000_000.0

STRATEGIES = {
    "mrpt": {"snap_glob": "inventory_history/inventory_mrpt_*.json",
             "inventory": "inventory_mrpt.json", "live_start": "2026-03-19"},
    "mtfs": {"snap_glob": "inventory_history/inventory_mtfs_*.json",
             "inventory": "inventory_mtfs.json", "live_start": "2026-03-19"},
}


def _cfg(strategy: str) -> dict:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}")
    return STRATEGIES[strategy]


def account_path(strategy: str, root: Optional[str] = None) -> str:
    return os.path.join(root or BASE_DIR, f"account_{strategy}.json")


def ledger_path(strategy: str, root: Optional[str] = None) -> str:
    return os.path.join(root or BASE_DIR, f"trade_ledger_{strategy}.jsonl")


def history_dir(strategy: str, root: Optional[str] = None) -> str:
    return os.path.join(root or BASE_DIR, "account_history")


class Account:
    """带符号持仓的记账账户（多空通用）。"""

    def __init__(self, strategy: str, data: dict, root: Optional[str] = None):
        self.strategy = strategy
        self.data = data
        self.root = root or BASE_DIR

    # ── 建账 ──────────────────────────────────────────────────────────────
    @classmethod
    def open_flat(cls, strategy: str, as_of: str, root: Optional[str] = None) -> "Account":
        """期初全现金建账（pairs 在 live_start 前一日为空仓 → 从 $1M 现金起步）。"""
        return cls(strategy, {
            "as_of": as_of, "base_currency": "USD", "initial_cash": INITIAL_CASH,
            "cash": INITIAL_CASH, "positions": {},
            "cumulative_realized": 0.0, "cumulative_dividends": 0.0,
            "cumulative_fees": 0.0, "equity": None, "liabilities": {},
            "lots": {}}, root)

    # ── 成交 ──────────────────────────────────────────────────────────────
    def trade(self, day: str, ticker: str, delta: int, price: float, **extra) -> dict:
        """delta>0 买入 / delta<0 卖出（含开空）。带符号持仓通用记账。

        同向(开仓/加仓): 加权平均成本 (s0·c0 + Δ·p)/(s0+Δ)
        反向(减仓/平仓): realized = (p − c)·q·sign(s0)
                        —— 多头 (p−c)·q;空头 (c−p)·q,方向自动正确
        反向超出持仓(翻向): 先平尽实现盈亏,余量按新方向以 p 起算成本
        现金统一 cash −= Δ·p（买入减少、卖出/开空增加）

        只做多时与 portfolio_ledger 的实现逐位等价（tests 中对拍）。
        """
        p = self.data["positions"].get(ticker, {"shares": 0, "avg_cost": 0.0,
                                                "entry_date": day})
        s0, c0 = int(p["shares"]), float(p["avg_cost"])
        delta = int(delta)
        row = {"date": day, "ticker": ticker,
               "side": "BUY" if delta > 0 else "SELL",
               "shares": abs(delta), "price": round(float(price), 4)}

        if s0 != 0 and (delta > 0) != (s0 > 0):
            close_q = min(abs(delta), abs(s0))
            sign = 1 if s0 > 0 else -1
            realized = round((price - c0) * close_q * sign, 2)
            rest = abs(delta) - close_q
            if rest:                                  # 翻向
                p["shares"] = rest if delta > 0 else -rest
                p["avg_cost"] = round(float(price), 6)
                p["entry_date"] = day
            else:
                p["shares"] = s0 + (close_q if delta > 0 else -close_q)
            self.data["cumulative_realized"] = round(
                self.data["cumulative_realized"] + realized, 2)
            row["avg_cost_at_trade"] = round(c0, 6)
            row["realized_pnl"] = realized
        else:
            new_shares = s0 + delta
            if new_shares == 0:
                raise AssertionError(f"[{self.strategy}] {day} {ticker} 同向成交后持仓为0")
            p["avg_cost"] = round((s0 * c0 + delta * price) / new_shares, 6)
            if s0 == 0:
                p["entry_date"] = day
            p["shares"] = new_shares

        self.data["cash"] = round(self.data["cash"] - delta * price, 2)
        row["gross"] = round(-delta * price, 2)
        if p["shares"] == 0:
            self.data["positions"].pop(ticker, None)
        else:
            self.data["positions"][ticker] = p
        # **dedup_key 必须含 lot**：同一天同一票可能有多个 pair 的 lot 同时平仓
        # （实测 2026-04-02 AVB 有 TJX/AVB、L/AVB、AIG/AVB 三个 lot 同日平），
        # 不含 lot 会让它们撞成同一个 key、被 append_ledger 去重掉,
        # 账户状态虽正确但**成交明细文件丢行**,逐 pair 归因随之失真。
        _lot = extra.get("lot")
        row["dedup_key"] = (f"{day}-{ticker}-{row['side']}"
                            + (f"-{_lot}" if _lot else ""))
        row.update(extra)                             # price_basis / ext_order_id / pair
        return row

    def dividend(self, day: str, ticker: str, per_share: float) -> dict | None:
        """分红：多头收、空头付（融券方需补付股息）。"""
        p = self.data["positions"].get(ticker)
        if not p or p["shares"] == 0 or not per_share:
            return None
        total = round(p["shares"] * per_share, 2)     # 空头为负 = 付出
        self.data["cash"] = round(self.data["cash"] + total, 2)
        self.data["cumulative_dividends"] = round(
            self.data["cumulative_dividends"] + total, 2)
        return {"date": day, "ticker": ticker, "side": "DIV", "shares": p["shares"],
                "price": round(float(per_share), 6), "gross": total,
                "dedup_key": f"{day}-{ticker}-DIV"}

    def fee(self, day: str, amount: float) -> dict | None:
        if not amount or amount <= 0:
            return None
        self.data["cash"] = round(self.data["cash"] - amount, 2)
        self.data["cumulative_fees"] = round(self.data["cumulative_fees"] + amount, 2)
        return {"date": day, "ticker": "*", "side": "FEE", "shares": 0,
                "price": 0.0, "gross": round(-amount, 2),
                "dedup_key": f"{day}-FEE"}

    # ── pair 级 lot ───────────────────────────────────────────────────────
    #
    # **为何需要**：账本按票记净敞口，与报告的 **pair 级归因**对不上，
    # 且逐日快照差分看不见「同日平掉又重开」（实测 MRPT 99 次真实平仓中
    # 快照只见 84 次）。lot 层按 (pair, leg) 独立记成本与数量：
    #   · 平仓的已实现按**该 lot 自己的开仓价**算 → 与 pair 级归因同口径
    #   · open_date 变化即可识别同日重开 → 隐藏平仓不再丢失
    #   · 每票净敞口 = 该票所有 lot 之和 → V1/R1/R3 等状态检查照常成立

    def lot_open(self, day: str, key: str, ticker: str, shares: int,
                 price: float, open_date: str) -> dict | None:
        """建一个 lot 并按其数量成交（现金/净持仓走 trade）。"""
        if not shares:
            return None
        row = self.trade(day, ticker, int(shares), float(price),
                         lot=key, lot_action="OPEN")
        self.data.setdefault("lots", {})[key] = {
            "ticker": ticker, "shares": int(shares),
            "cost": round(float(price), 6), "open_date": open_date}
        return row

    def lot_close(self, day: str, key: str, price: float,
                  qty: int | None = None) -> dict | None:
        """平掉（部分）lot：已实现按**该 lot 自己的成本**算，而非全票混合成本。"""
        lot = self.data.get("lots", {}).get(key)
        if not lot or not lot["shares"]:
            return None
        q = int(lot["shares"]) if qty is None else int(qty)
        if not q:
            return None
        sign = 1 if lot["shares"] > 0 else -1
        q = sign * min(abs(q), abs(int(lot["shares"])))
        realized = round((float(price) - lot["cost"]) * q, 2)
        row = self.trade(day, lot["ticker"], -q, float(price),
                         lot=key, lot_action="CLOSE",
                         lot_cost=lot["cost"], lot_realized=realized)
        lot["shares"] = int(lot["shares"]) - q
        if lot["shares"] == 0:
            self.data["lots"].pop(key, None)
        # trade() 已按**全票混合成本**记了一笔 realized；换成 lot 口径
        self.data["cumulative_realized"] = round(
            self.data["cumulative_realized"] - (row.get("realized_pnl") or 0.0)
            + realized, 2)
        row["realized_pnl"] = realized
        return row

    def lot_resize(self, day: str, key: str, new_shares: int,
                   price: float) -> dict | None:
        """同一 lot 内加减仓：加仓按加权平均更新 lot 成本，减仓按 lot 成本实现。"""
        lot = self.data.get("lots", {}).get(key)
        if lot is None:
            return None
        d = int(new_shares) - int(lot["shares"])
        if not d:
            return None
        if (d > 0) == (int(lot["shares"]) > 0):            # 同向加仓
            row = self.trade(day, lot["ticker"], d, float(price),
                             lot=key, lot_action="ADD")
            tot = int(lot["shares"]) + d
            lot["cost"] = round((int(lot["shares"]) * lot["cost"]
                                 + d * float(price)) / tot, 6)
            lot["shares"] = tot
            return row
        return self.lot_close(day, key, price, qty=-d)     # 反向 → 部分平

    def lot_unrealized(self, prices_row) -> float:
        """按 lot 成本口径的未实现合计（与 pair 级归因同口径）。"""
        u = 0.0
        for lot in (self.data.get("lots") or {}).values():
            px = prices_row.get(lot["ticker"])
            if px is None or pd.isna(px):
                continue
            u += int(lot["shares"]) * (float(px) - float(lot["cost"]))
        return round(u, 2)

    # ── 盯市 ──────────────────────────────────────────────────────────────
    def mark(self, day: str, prices_row: pd.Series) -> float:
        """收盘 mark：equity = cash + Σ市值（空头市值为负）；恒等式断言。"""
        pos_val = 0.0
        for t, p in self.data["positions"].items():
            px = prices_row.get(t)
            if px is None or pd.isna(px):
                raise AssertionError(f"[{self.strategy}] {day} 缺 {t} 价格 — 拒绝 mark")
            pos_val += p["shares"] * float(px)
        equity = round(self.data["cash"] + pos_val, 2)
        # **有 lot 时按 lot 成本算未实现**：已实现也走 lot 口径,两者必须同源，
        # 否则恒等式破。可证同源即恒等：单个 lot 全生命周期的现金净流
        # = q(p−c) = 其已实现；未平的 lot 现金流 −q·c 加市值 q·px = q(px−c)。
        if self.data.get("lots"):
            unrealized = self.lot_unrealized(prices_row)
            self._sync_avg_cost_from_lots()
        else:
            unrealized = round(pos_val - sum(p["shares"] * p["avg_cost"]
                                             for p in self.data["positions"].values()), 2)
        lhs = round(equity - self.data["initial_cash"], 2)
        rhs = round(self.data["cumulative_realized"] + self.data["cumulative_dividends"]
                    - self.data["cumulative_fees"] + unrealized, 2)
        if abs(lhs - rhs) > 0.05:
            raise AssertionError(
                f"[{self.strategy}] {day} 恒等式破裂: equity−1M={lhs} ≠ "
                f"realized+div−fees+unrealized={rhs}")
        self.data.update({"equity": equity, "unrealized": unrealized,
                          "position_value": round(pos_val, 2), "as_of": day})
        return equity

    def _sync_avg_cost_from_lots(self):
        """把票级 avg_cost 重算为其所有 lot 的加权平均（仅展示/兼容用；
        已实现与未实现均走 lot 口径）。"""
        agg: dict = {}
        for lot in (self.data.get("lots") or {}).values():
            a = agg.setdefault(lot["ticker"], [0, 0.0])
            a[0] += int(lot["shares"])
            a[1] += int(lot["shares"]) * float(lot["cost"])
        for t, p in self.data["positions"].items():
            if t in agg and agg[t][0]:
                p["avg_cost"] = round(agg[t][1] / agg[t][0], 6)

    # ── 落盘 ──────────────────────────────────────────────────────────────
    def save(self):
        with open(account_path(self.strategy, self.root), "w") as f:
            json.dump(self.data, f, indent=2)

    def save_history(self, day: str):
        hd = history_dir(self.strategy, self.root)
        os.makedirs(hd, exist_ok=True)
        with open(os.path.join(hd, f"account_{self.strategy}_{day.replace('-','')}.json"),
                  "w") as f:
            json.dump(self.data, f, indent=2)


def append_ledger(strategy: str, rows: list, seen_keys: set, root: Optional[str] = None):
    if not rows:
        return
    with open(ledger_path(strategy, root), "a") as f:
        for r in rows:
            k = r.get("dedup_key")
            if k in seen_keys:
                continue
            seen_keys.add(k)
            f.write(json.dumps(r) + "\n")


def load_ledger_rows(strategy: str, root: Optional[str] = None) -> list:
    fp = ledger_path(strategy, root)
    if not os.path.exists(fp):
        return []
    return [json.loads(l) for l in open(fp) if l.strip()]
