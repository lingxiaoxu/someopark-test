"""Derived table: entry time and size tier per coin x scenario, read off the main tables.

Rules (user, 2026-10-01):
1. For each coin and scenario take the bin with the highest mean PnL per contract among
   the bins the coin's own table shows (n >= MIN_N). If the coin's row shows no bin at
   all, use the same row of the 5-coin pooled table. Ties go to the bin closest to T-8.25.
2. Entries only between T-9.75 and T-5.25 (user, 2026-10-02): a T-3.75 winner is moved
   to T-5.25 and a T-11.25 winner to T-9.75 (same table; the pooled cell if that table
   does not show one).
3. If the chosen bin loses money on average (or has no data), skip that coin x scenario.
4. Size tier by the chosen bin's mean: at or above the 90th percentile of every scenario
   cell on the five per-coin tables -> x1.5; at or below the 10th percentile of the
   coins' traded picks -> x0.5; otherwise x1.
5. top2 (user, 2026-10-02): the best ALLOWED bin other than top1's bin, from the same
   table, traded only if its mean > 0; x1, or x0.5 if <= the same 10th percentile, never
   x1.5. It is also taken when top1 is skipped. This module is the single source of
   truth for both: live_plan.rules_from and check.py read/recompute it from here.
"""
from __future__ import annotations

import numpy as np

from .config import COINS, LIVE_BIN, POOLED
from .tables import COLS, ROWS

ALLOWED = ("T-9.75", "T-8.25", "T-6.75", "T-5.25")       # tradeable bins, earliest first
CLAMP = {"T-11.25": "T-9.75", "T-3.75": "T-5.25"}        # out-of-range winner -> nearest allowed bin
LATEST_ALLOWED = "T-5.25"                                # kept for older callers/tests
HI_Q, LO_Q = 90, 10
MULT = {"high": 1.5, "normal": 1.0, "low": 0.5}


def _centre(b: str) -> float:
    return float(b.split("-")[1])


def _pick(cells: dict) -> str | None:
    avail = [b for b in COLS if cells.get(b)]
    if not avail:
        return None
    return max(avail, key=lambda b: (cells[b]["mean"], -abs(_centre(b) - _centre(LIVE_BIN))))


def best_times(tables: dict) -> dict:
    out = {}
    for co in list(COINS) + [POOLED]:
        rows = {}
        for sc in ROWS:
            own = tables[co][sc]["cells"]
            b = _pick(own)
            source = "own" if b else None
            cells = own
            if b is None and co != POOLED:
                cells = tables[POOLED][sc]["cells"]
                b = _pick(cells)
                source = "pooled" if b else None
            pick, pick_cell, pick_source = b, (cells[b] if b else None), source
            if b in CLAMP:
                pick = CLAMP[b]
                pick_cell = cells.get(pick)
                if pick_cell is None and source == "own" and co != POOLED:
                    pick_cell = tables[POOLED][sc]["cells"].get(pick)
                    pick_source = "pooled" if pick_cell else source
            action = None if not b else ("trade" if pick_cell and pick_cell["mean"] >= 0 else "skip")
            top2 = None
            if b:
                others = [x for x in ALLOWED if x != pick and cells.get(x)]
                if others:
                    x2 = max(others, key=lambda x: (cells[x]["mean"], -abs(_centre(x) - _centre(LIVE_BIN))))
                    if cells[x2]["mean"] > 0:
                        top2 = {"pick": x2, "cell": cells[x2], "source": source, "mult": 1.0}
            rows[sc] = {"best": b, "source": source, "cell": cells[b] if b else None,
                        "pick": pick, "pick_source": pick_source, "pick_cell": pick_cell,
                        "action": action, "top2": top2, "live": (cells.get(LIVE_BIN) if b else None)}
        out[co] = rows
    # size tiers
    every = [c["mean"] for co in COINS for sc in ROWS if sc != "全部"
             for c in tables[co][sc]["cells"].values() if c]
    picks = [out[co][sc]["pick_cell"]["mean"] for co in COINS for sc in ROWS
             if sc != "全部" and out[co][sc]["action"] == "trade"]
    hi = float(np.percentile(every, HI_Q)) if every else None
    lo = float(np.percentile(picks, LO_Q)) if picks else None
    for co in list(COINS) + [POOLED]:
        for sc in ROWS:
            r = out[co][sc]
            if r["action"] != "trade":
                r["tier"], r["mult"] = None, 0.0
                continue
            v = r["pick_cell"]["mean"]
            r["tier"] = ("high" if hi is not None and v >= hi else
                         "low" if lo is not None and v <= lo else "normal")
            r["mult"] = MULT[r["tier"]]
    for co in list(COINS) + [POOLED]:
        for sc in ROWS:
            t2 = out[co][sc].get("top2")
            if t2:
                t2["mult"] = 0.5 if (lo is not None and t2["cell"]["mean"] <= lo) else 1.0
    out["_thresholds"] = {"hi_c": hi, "hi_rule": f"P{HI_Q} of {len(every)} scenario cells on the 5 coin tables",
                          "lo_c": lo, "lo_rule": f"P{LO_Q} of {len(picks)} traded coin picks"}
    return out


def _v(c: dict | None) -> str:
    return "—" if not c else f"{c['mean']:+.2f}c (t {c['t']:+.1f}, n {c['n']})"


def top1_cell(r: dict) -> str:
    if not r["best"]:
        return "无数据"
    if r["action"] == "skip":
        return "跳过"
    s = r["pick"] + ("†" if r["pick_source"] == "pooled" else "")
    return s + {"high": " ×1.5", "low": " ×0.5"}.get(r["tier"], "")


def top2_cell(r: dict) -> str:
    t2 = r.get("top2")
    if not t2:
        return "—"
    return t2["pick"] + ("†" if t2["source"] == "pooled" else "") + (" ×0.5" if t2["mult"] == 0.5 else "")


def matrix_cell(r: dict) -> str:
    """'top1 / top2' as traded live (top2 is taken even when top1 is skipped)."""
    return f"{top1_cell(r)} / {top2_cell(r)}"


def render_md(best: dict, meta: dict) -> str:
    cols = list(COINS) + [POOLED]
    th = best["_thresholds"]
    fc = lambda x: "—" if x is None else f"{x:+.2f}c"
    lines = [f"# W7 每种情形的下单时点与仓位（派生自同一次运行的情形表，滚动 {meta['window_days']} 天）", "",
             f"- 数据窗口（窗口到期时间）: {meta['start_utc']} → {meta['end_utc']} UTC | 生成时间 {meta['built_utc']} UTC",
             "- 时点: 每个币每种情形，在该币自己的表里从有数据的时点（≥30 笔）中取每张平均盈亏最高者；"
             "该币这一行一个时点都没有数据时，改用 5 币合计表同一行（标 †）；都没有则为“无数据”。",
             f"- 下单时点只在 {ALLOWED[0]} 到 {ALLOWED[-1]} 之间: 最优是 T-11.25 时改在 T-9.75、最优是 T-3.75 时改在 T-5.25 下单"
             "（取同一张表该格，没有则取 5 币合计的）。",
             "- 跳过: 最终选定时点的平均是亏的（或没有数据），这种情形不下单；明细里仍列出原始最佳时点。",
             f"- 仓位: 选定时点每张盈亏 ≥ {fc(th['hi_c'])}（五张分币表全部情形格子的第 {HI_Q} 百分位）→ ×1.5；"
             f"≤ {fc(th['lo_c'])}（各币下单点里的第 {LO_Q} 百分位）→ ×0.5；其余 ×1。"
             "×1.5 即白天 BTC 40→60、其他 30→45；×0.5 即 40→20、30→15（夜盘 BTC 和其他币都是 20，同比例）；"
             "实盘每币每窗口 top1+top2 实际买到的合计不超过 1.5×基础仓位（白天 60/45，夜盘 30/30）。",
             "- top2: 除 top1 时点外最好的允许时点，为正才下单；×1，≤ 上述低档门槛则 ×0.5，不加倍；top1 跳过时 top2 照下。"
             "矩阵格式 = top1 / top2。",
             f"- 实盘（prod）按本表下单（时点只在 T-9.75 到 T-5.25，入场价 0.80–0.97）；表缺失、超过 6 小时或情形判不出时回退旧规则 T-8（表中 {LIVE_BIN} 档）。"
             "这些都是对过去 14 天的事后挑选，单格噪声通常有几美分。", "",
             "## 下单时点与仓位（top1 / top2）", "",
             "| 情形 | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for sc in ROWS:
        lines.append(f"| {sc} | " + " | ".join(matrix_cell(best[co][sc]) for co in cols) + " |")
    lines += ["", "## 明细", "",
              "| 币 | 情形 | 动作 | 下单时点 | 仓位 | 来源 | 该时点每张盈亏 | top2 时点 | top2 每张盈亏 | 原始最佳 | 同一行 T-8.25 |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for co in cols:
        for sc in ROWS:
            r = best[co][sc]
            src = {"own": "本表" if co == POOLED else "分币", "pooled": "5币合计", None: "—"}[r["pick_source"]]
            act = {"trade": "下单", "skip": "跳过", None: "—"}[r["action"]]
            size = f"×{r['mult']:g}" if r["action"] == "trade" else "—"
            orig = "—" if not r["best"] else f"{r['best']} {r['cell']['mean']:+.2f}c"
            t2 = r.get("top2")
            lines.append(f"| {co} | {sc} | {act} | {r['pick'] or '无数据'} | {size} | {src} | {_v(r['pick_cell'])} "
                         f"| {top2_cell(r)} | {_v(t2['cell']) if t2 else '—'} | {orig} | {_v(r['live'])} |")
    return "\n".join(lines) + "\n"
