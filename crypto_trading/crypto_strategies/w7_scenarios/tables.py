"""Aggregate labelled W7 entries into the 5 per-coin tables + the pooled table."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import COINS, LABELS, MIN_N, ORDER, POOLED, STAR_N

COLS = LABELS[::-1]          # T-11.25 ... T-3.75
ROWS = ORDER + ["全部"]


def cell(g: pd.DataFrame, ref: pd.DataFrame) -> dict | None:
    """Mean PnL per contract; SE = the larger of (a) dispersion of window-mean PnL across
    ALL scenarios at the same coin(s) x bin / sqrt(windows in cell) and (b) the cell's own
    window-clustered SE, so a small all-win cell cannot fake a tiny error."""
    n = len(g)
    if n < MIN_N:
        return None
    mu = float(g.pnl.mean())
    G = int(g.close_ts.nunique())
    se_ref = float(ref.groupby("close_ts").pnl.mean().std(ddof=1)) / np.sqrt(G)
    r = (g.pnl - mu).groupby(g.close_ts).sum()
    se_own = float(np.sqrt((G / (G - 1)) * (r ** 2).sum()) / n) if G > 1 else float("inf")
    se = max(se_ref, se_own)
    return {"mean": mu, "t": mu / se, "n": n, "windows": G}


def fmt(c: dict | None) -> str:
    if c is None:
        return "—"
    return f"{c['mean']:+.2f}c ({c['t']:+.1f}){'*' if c['n'] < STAR_N else ''}"


def build_tables(labelled: pd.DataFrame) -> dict:
    """labelled: every classified entry snapshot (all prices) with columns
    coin, close_ts, bin, scen, inband, pnl."""
    trades = labelled[labelled.inband]
    out = {}
    for co in list(COINS) + [POOLED]:
        snaps = labelled if co == POOLED else labelled[labelled.coin == co]
        t0 = trades if co == POOLED else trades[trades.coin == co]
        share = snaps.scen.value_counts(normalize=True)
        rows = {}
        for sc in ROWS:
            g = t0 if sc == "全部" else t0[t0.scen == sc]
            cells = {b: cell(g[g.bin == b], t0[t0.bin == b]) for b in COLS}
            rows[sc] = {"share": 1.0 if sc == "全部" else float(share.get(sc, 0.0)), "cells": cells}
        out[co] = rows
    return out


def render_md(tables: dict, meta: dict) -> str:
    lines = [f"# W7 情形 × 下单时点表（滚动 {meta['window_days']} 天）", "",
             f"- 数据窗口（窗口到期时间）: {meta['start_utc']} → {meta['end_utc']} UTC",
             f"- 生成时间: {meta['built_utc']} UTC | 代码版本 {meta['version']}",
             f"- 下单笔数（价格带内）: {meta['trades']} | 无法归类的价格带内快照: {meta['unclassified_inband']}",
             "- 单位: 每张合约平均盈亏（美分，已扣手续费）；括号内为 t（取两种标准误中较保守者）；"
             f"* = 少于 {STAR_N} 笔；— = 少于 {MIN_N} 笔不显示。",
             "- 占比 = 该币（或 5 币合计）全部已归类快照（各时点、各价格）落入该情形的比例，不是下单笔数比例。",
             "- 情形按优先级判定、互斥：大单反向 > 大单同向 > funding 热门方拥挤 > funding 对手方拥挤 > 方向×波动。",
             "- 大单 = 下单时刻 Hyperliquid 过去 1 分钟抽样成交净额失衡（≥0.5，即 ≥75% 在一边）且 1 分钟价格同向，"
             "对热门方取向；与实盘跳单闸同一函数。funding/方向/波动均为窗口开盘时已知。", ""]
    for co, rows in tables.items():
        lines += [f"## {co}", "", "| 情形 | 占比 | " + " | ".join(COLS) + " |", "|---|---|" + "---|" * len(COLS)]
        for sc in ROWS:
            r = rows[sc]
            share = "100%" if sc == "全部" else f"{100 * r['share']:.1f}%"
            lines.append(f"| {sc} | {share} | " + " | ".join(fmt(r["cells"][b]) for b in COLS) + " |")
        lines.append("")
    return "\n".join(lines)


def to_json(tables: dict) -> dict:
    return {co: {sc: {"share": r["share"], "cells": r["cells"]} for sc, r in rows.items()}
            for co, rows in tables.items()}
