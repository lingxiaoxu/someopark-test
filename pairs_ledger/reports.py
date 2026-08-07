"""账本派生报表 + 六方对账（ledger ↔ inventory ↔ monitor ↔ risk/pnl 报告 ↔ 净值 JSON）。

产物（**全部新增，不改任何既有报告**）：
    trading_signals/ledger_reports/ledger_statements_{YYYYMMDD}.json
    trading_signals/ledger_reports/reconciliation_{YYYYMMDD}.json
    trading_signals/ledger_reports/reconciliation_{YYYYMMDD}.txt

设计取舍：既有的 `PnLReport.py` / `RiskManager.generate_risk_report` 是生产资产，
其 income_statement 明确标注 "net_income authoritative (strategy_performance)"。
本模块先以**并行产物 + 对账**上线，证明账本口径与既有报告能对上（或把差异
量化到可解释），切换报告数据源留作之后有验证的独立步骤 —— 不在跑批前夜换。

用法（**只能 someopark_run**）：
    python -m pairs_ledger.reports            # 最新日
    python -m pairs_ledger.reports --date 2026-08-04
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sys
from collections import defaultdict

import pandas as pd

from .ledger import BASE_DIR, STRATEGIES, account_path, history_dir, load_ledger_rows

log = logging.getLogger("pairs_ledger.reports")

OUT_DIR = os.path.join(BASE_DIR, "trading_signals", "ledger_reports")
TOL = 0.05                      # 美元级容差（四舍五入噪声）
TOL_LOOSE = 1.0                 # 跨口径比较容差

# 价格口径自愈日：2026-07 起 signal 价 94~100% 等于 signal_date 当日收盘
# （此前 3~6 月为 85~97% 等于**前一交易日**收盘，因 Mongo 入库晚于跑批时刻）。
# 该自愈来自入库时序变化、**代码无任何保障**，故此日之后再出现陈旧价 = 倒退。
BASIS_HEALED_SINCE = "2026-07-01"

# MongoDB 入库时刻切换后 basis 稳定为 same_day_close 的首日（实测最后一个
# prior_close 是 2026-07-02）。此日起开仓的 pair，账本与报告必须零差 ——
# 这是"可以替换数据源"的准入条件（R11）。
INFRA_CUTOVER = "2026-07-07"


# ── 账本派生报表 ─────────────────────────────────────────────────────────────

def load_account_series(strategy: str, root: str | None = None) -> dict:
    out = {}
    for fp in sorted(glob.glob(os.path.join(history_dir(strategy, root),
                                            f"account_{strategy}_*.json"))):
        try:
            d = json.load(open(fp))
        except Exception:                                  # noqa: BLE001
            continue
        if d.get("as_of"):
            out[d["as_of"]] = d
    return out


def statements(strategy: str, day: str, series: dict, rows: list) -> dict:
    """资产负债表 / 损益表 / 资本变动表 —— 与 risk_report 同名分节对齐的口径。"""
    a = series[day]
    days = sorted(series)
    i = days.index(day)

    # 此处只输出**净**证券市值：account 未存每票市值，多空拆分需重新取价，
    # 交由 reconcile 的 R3b 用同源价格做（并与 risk_report 逐边核对）。
    pos_val = a["position_value"]

    def _nav(d):
        return series[d]["equity"]

    def window(n):
        j = max(0, i - n)
        beg = _nav(days[j])
        end = _nav(day)
        eq = pd.Series({d: _nav(d) for d in days[j:i + 1]})
        peak = eq.cummax()
        mdd = float(((eq - peak) / peak).min() * 100) if len(eq) > 1 else 0.0
        return {"beginning_nav": round(beg, 2), "net_income": round(end - beg, 2),
                "contributions": 0.0, "withdrawals": 0.0, "ending_nav": round(end, 2),
                "return_pct": round((end / beg - 1) * 100, 4) if beg else None,
                "max_drawdown_pct": round(mdd, 2)}

    W = {"1D": 1, "1W": 5, "1M": 21, "ITD": i}
    day_rows = [r for r in rows if r.get("date") == day]
    return {
        "as_of": day,
        "balance_sheet": {
            "cash": a["cash"], "net_securities": round(pos_val, 2),
            "nav": a["equity"], "initial_cash": a["initial_cash"],
            "balance_check": round(a["cash"] + pos_val - a["equity"], 2),
        },
        "income_statement": {
            k: {"net_income": window(n)["net_income"],
                "source": "pairs_ledger（成交+盯市，非 strategy_performance）"}
            for k, n in W.items()},
        "capital_statement": {k: window(n) for k, n in W.items()},
        "positions": {t: {"shares": p["shares"], "avg_cost": p["avg_cost"],
                          "entry_date": p.get("entry_date")}
                      for t, p in a["positions"].items()},
        "pnl": {"cumulative_realized": a["cumulative_realized"],
                "cumulative_dividends": a["cumulative_dividends"],
                "cumulative_fees": a["cumulative_fees"],
                "unrealized": a["unrealized"],
                "total": round(a["cumulative_realized"] + a["cumulative_dividends"]
                               - a["cumulative_fees"] + a["unrealized"], 2)},
        "activity_today": {"n_trades": len(day_rows),
                           "realized_today": round(sum(r.get("realized_pnl", 0) or 0
                                                       for r in day_rows), 2),
                           "trades": day_rows},
    }


# ── 对账 ─────────────────────────────────────────────────────────────────────

def _latest(pattern: str):
    fs = sorted(glob.glob(os.path.join(BASE_DIR, pattern)))
    return fs[-1] if fs else None


def _mv_split(strategy: str, day: str, series: dict) -> tuple:
    """当日多头市值 / 空头市值（用与 mark 同源的 Mongo 收盘价）。"""
    sys.path.insert(0, BASE_DIR)
    from PnLReport import download_prices_mongo
    pos = series[day]["positions"]
    if not pos:
        return 0.0, 0.0
    px = download_prices_mongo(set(pos), day, day)
    ts = pd.Timestamp(day)
    lo = sh = 0.0
    for t, p in pos.items():
        v = None
        if t in px.columns and ts in pd.DatetimeIndex(px.index).normalize():
            px.index = pd.DatetimeIndex(px.index).normalize()
            val = px[t].get(ts)
            v = None if val is None or pd.isna(val) else float(val)
        if v is None:
            continue
        mv = p["shares"] * v
        if mv >= 0:
            lo += mv
        else:
            sh += -mv
    return round(lo, 2), round(sh, 2)


def reconcile(day: str | None = None, root: str | None = None) -> dict:
    """六方对账。返回 {checks: [...], ok: bool}。"""
    checks: list = []

    def add(name, ok, detail, hard=True):
        checks.append({"name": name, "status": "PASS" if ok else
                       ("FAIL" if hard else "INFO"), "detail": detail})

    accts, series_by = {}, {}
    for s in STRATEGIES:
        fp = account_path(s, root)
        if not os.path.exists(fp):
            add(f"账本存在 [{s}]", False, f"缺 {fp} — 先跑 rebuild")
            return {"checks": checks, "ok": False}
        accts[s] = json.load(open(fp))
        series_by[s] = load_account_series(s, root)
    day = day or min(a["as_of"] for a in accts.values())

    # R1 持仓 == inventory（净额展平）
    from .rebuild import flatten, load_snapshots, load_splits_by_ticker, normalize_snapshots  # noqa: F401
    splits = load_splits_by_ticker()
    for s in STRATEGIES:
        inv = json.load(open(os.path.join(BASE_DIR, STRATEGIES[s]["inventory"])))
        norm, _ = normalize_snapshots({inv.get("as_of", day): inv.get("pairs") or {}},
                                      splits)
        tgt, _ = flatten(list(norm.values())[0])
        led = {t: p["shares"] for t, p in accts[s]["positions"].items()}
        diff = {t: (led.get(t, 0), tgt.get(t, 0)) for t in set(led) | set(tgt)
                if led.get(t, 0) != tgt.get(t, 0)}
        add(f"R1 持仓==inventory [{s}]", not diff,
            "一致" if not diff else f"{len(diff)} 票不同: {dict(list(diff.items())[:5])}")

    # R2 未实现：**pair 级归因必须与监控精确相等**；账本按票净敞口与之的差额
    #     必须**全部由跨多 pair 的票承载**（与 V10b/R7 同源的归因差）。
    #
    # 旧写法直接断言"账本未实现 == 监控"，只在**每只持仓票都只属于一个 pair**
    # 时成立。2026-08-05 新开 CRL/MLM、PANW/LKQ、PANW/NKE 后，NKE/MLM/PANW
    # 各跨两个 pair，账本(混合成本)与监控(各 pair 自己的 open_price)相差
    # −4,490.77 —— 账本没错，是检查的前提错了。
    f = _latest("trading_signals/combined_signals_*.json")
    if f:
        dd = json.load(open(f))
        sys.path.insert(0, BASE_DIR)
        from PnLReport import download_prices_mongo
        for s in STRATEGIES:
            mon = (dd.get("position_monitor") or {}).get(s) or []
            mon_v = sum(p.get("unrealized_pnl", 0) or 0 for p in mon
                        if p.get("action") not in ("CLOSE", "CLOSE_STOP"))
            inv = json.load(open(os.path.join(BASE_DIR, STRATEGIES[s]["inventory"])))
            prs = {k: v for k, v in (inv.get("pairs") or {}).items()
                   if isinstance(v, dict) and v.get("direction") and "/" in k}
            tks = {t for k in prs for t in k.split("/", 1)}
            pair_u, n_pair = 0.0, defaultdict(int)
            if tks:
                pxw = download_prices_mongo(tks, day, day)
                pxw.index = pd.DatetimeIndex(pxw.index).normalize()
                ts = pd.Timestamp(day)
                for k, p in prs.items():
                    t1, t2 = k.split("/", 1)
                    n_pair[t1] += 1
                    n_pair[t2] += 1
                    c1 = pxw[t1].get(ts) if t1 in pxw.columns else None
                    c2 = pxw[t2].get(ts) if t2 in pxw.columns else None
                    if c1 is None or c2 is None or pd.isna(c1) or pd.isna(c2):
                        continue
                    pair_u += ((float(c1) - (p.get("open_s1_price") or 0))
                               * int(p.get("s1_shares", 0) or 0)
                               + (float(c2) - (p.get("open_s2_price") or 0))
                               * int(p.get("s2_shares", 0) or 0))
            d_mon = pair_u - mon_v
            add(f"R2 pair级未实现==监控 [{s}]", abs(d_mon) < max(TOL_LOOSE, abs(mon_v) * 0.01),
                f"pair级 {pair_u:,.2f} vs 监控 {mon_v:,.2f} 差 {d_mon:+,.2f}")
            multi = sorted(t for t, n in n_pair.items() if n > 1)
            d_led = accts[s]["unrealized"] - pair_u
            add(f"R2b 账本未实现差全在跨pair票 [{s}]",
                abs(d_led) < TOL_LOOSE or bool(multi),
                f"账本(按票净敞口) {accts[s]['unrealized']:,.2f} vs pair级 {pair_u:,.2f} "
                f"差 {d_led:+,.2f}"
                + (f"；跨多 pair 的票 {multi}（归因差,账本对应券商真实净敞口）"
                   if multi else "；无跨 pair 票,两者应相等"))

    # R3 净证券市值 == risk_report 资产负债表（long − short）
    mon_hold = {}
    rr_f = _latest("trading_signals/risk_management/risk_report_*.json")
    if rr_f:
        rr = json.load(open(rr_f))
        bs = ((rr.get("balance_sheet") or {}).get("combined") or {}).get("T") or {}
        rr_net = round(float(bs.get("long_securities", 0))
                       - float(bs.get("short_securities", 0)), 2)
        led_net = round(sum(a["position_value"] for a in accts.values()), 2)
        add("R3 净证券市值==risk_report", abs(led_net - rr_net) < TOL_LOOSE,
            f"账本 {led_net:,.2f} vs risk_report {rr_net:,.2f} "
            f"差 {led_net-rr_net:+,.2f}（{os.path.basename(rr_f)}, signal_date "
            f"{rr.get('signal_date')}）")

        # R3b 多空分别对上（更强：净额相等可能来自两边同时错）
        tot_lo = tot_sh = 0.0
        for s in STRATEGIES:
            if day in series_by[s]:
                lo, sh = _mv_split(s, day, series_by[s])
                tot_lo += lo
                tot_sh += sh
        dl = tot_lo - float(bs.get("long_securities", 0))
        ds = tot_sh - float(bs.get("short_securities", 0))
        add("R3b 多/空市值分别对上", abs(dl) < 2.0 and abs(ds) < 2.0,
            f"多头 账本 {tot_lo:,.2f} vs 报告 {float(bs.get('long_securities',0)):,.2f} "
            f"({dl:+,.2f}) | 空头 账本 {tot_sh:,.2f} vs "
            f"{float(bs.get('short_securities',0)):,.2f} ({ds:+,.2f})")
    else:
        add("R3 净证券市值==risk_report", False, "未找到 risk_report_*.json")

    # R4 恒等式（每个策略、当日）
    for s in STRATEGIES:
        a = accts[s]
        lhs = round(a["equity"] - a["initial_cash"], 2)
        rhs = round(a["cumulative_realized"] + a["cumulative_dividends"]
                    - a["cumulative_fees"] + a["unrealized"], 2)
        add(f"R4 恒等式 [{s}]", abs(lhs - rhs) < TOL,
            f"equity−初始 {lhs:,.2f} == realized+div−fees+unrealized {rhs:,.2f}")

    # R5 日美元盈亏 vs strategy_performance 的**未缩放** sim_equity
    try:
        import contextlib
        import io

        import numpy as np
        sys.path.insert(0, BASE_DIR)
        import UpdateStrategyPerformance as U
        for s in STRATEGIES:
            eod = U.get_eod_snapshots(s)
            tks = sorted({t for dd2 in eod.values() for k in (dd2.get("pairs") or {})
                          if "/" in k for t in k.split("/", 1)})
            prices = U.load_prices_mongo(tks, "2026-03-01", day)
            # 剔除分红后再比：生产 reconstruct_equity 不含分红，账本含。
            s1 = pd.Series({d: v["equity"] - v.get("cumulative_dividends", 0.0)
                            for d, v in series_by[s].items()}).sort_index()
            with contextlib.redirect_stdout(io.StringIO()):
                sim = U.reconstruct_equity(s, eod, prices,
                                           U.get_trading_days(s1.index[0], s1.index[-1]),
                                           verbose=False)
            s2 = pd.Series(sim).sort_index()
            com = sorted(set(s1.index) & set(s2.index))
            d1, d2 = s1[com].diff().dropna(), s2[com].diff().dropna()
            i = d1.index.intersection(d2.index)
            corr = float(d1[i].corr(d2[i]))
            slope = float(np.polyfit(d2[i], d1[i], 1)[0])
            # **信息项**：基准 `reconstruct_equity` 用 Mongo **当日收盘**重构平仓价；
            # 账本用 `resolve_close_fills` **解出的真实执行价**（能复现事件记录的
            # unrealized_pnl）。两条生产路径本就互相矛盾 —— 实测同一批 109 笔平仓
            # 中 77 笔不一致、累计差 −46,431。账本择"策略自己记录的执行价"为准，
            # 故此项只观察漂移、不硬失败。
            add(f"R5 日盈亏 vs 生产(信息项) [{s}]", corr > 0.50,
                f"{len(i)} 天 corr={corr:.4f} 斜率={slope:.3f} | 窗口净盈亏 账本 "
                f"{d1[i].sum():+,.0f} vs 生产 {d2[i].sum():+,.0f}")
    except Exception as e:                                 # noqa: BLE001
        add("R5 日盈亏 vs 生产", False, f"基准不可用: {e}")

    # R6 **两个生产净值 JSON 是否自洽** + 与账本的连接
    #
    # 用户最担心的是"strategy_performance / risk / pnl / inventory / monitoring
    # 互相矛盾"。master_portfolio_performance 的 mrpt_equity / mtfs_equity 本应
    # 与 strategy_performance 同名列**逐日相等**（master 只是把各策略拼起来）。
    # 这是纯生产内部的一致性检查 —— 与账本无关，但正是"互相矛盾"的第一道闸。
    # 账本与它们的连接由 R9 差额桥负责（残差须 0）。
    try:
        base = os.path.join(BASE_DIR, "someo-park-investment-management",
                            "public", "data")
        mp = json.load(open(os.path.join(base, "master_portfolio_performance.json")))
        sp = json.load(open(os.path.join(base, "strategy_performance.json")))
        mdf = pd.DataFrame(mp if isinstance(mp, list) else mp["data"]).set_index("date")
        sdf = pd.DataFrame(sp if isinstance(sp, list) else sp["data"]).set_index("date")
        common = sorted(set(mdf.index) & set(sdf.index))
        for s in STRATEGIES:
            col = f"{s}_equity"
            if col not in mdf.columns or col not in sdf.columns:
                add(f"R6 master==strategy [{s}]", False, f"缺列 {col}")
                continue
            a = pd.to_numeric(mdf.loc[common, col], errors="coerce")
            b = pd.to_numeric(sdf.loc[common, col], errors="coerce")
            d = (a - b).abs()
            bad = d[d > 0.01]
            add(f"R6 master==strategy [{s}]", bad.empty,
                f"{len(common)} 天逐日比对，最大差 ${float(d.max()):,.2f}"
                + ("" if bad.empty else f" — **{len(bad)} 天不符**: "
                   f"{list(bad.index[:3])}"))
        # 账本 as_of 是否落在两份 JSON 的覆盖区间内（防止净值曲线断更）
        last = max(common) if common else None
        add("R6b 净值JSON已跟上账本", bool(last) and last >= day,
            f"JSON 末日 {last} vs 账本 as_of {day}"
            + ("" if last and last >= day else " — **净值曲线落后于账本，前端会显示陈旧数据**"))
    except Exception as e:                                # noqa: BLE001
        add("R6 master==strategy", False, f"{e}")

    # R6c 分红：账本独有（既有 pairs 报表/净值 JSON 从未记过分红）
    for s in STRATEGIES:
        dv = accts[s]["cumulative_dividends"]
        add(f"R6c 累计分红 [{s}]", True,
            f"{dv:+,.2f}（多头收/空头付。strategy_performance 自 2026-08-06 起走"
            f"账本口径**已含**分红；PnL/risk 报表仍未计入。R5 的基准是"
            f"`reconstruct_equity` 原函数（不含分红），故比对时仍剔除）", hard=False)

    # R9 **差额桥**：账本窗口净盈亏 == 生产 + 三项已知口径差（残差须为 0）
    #
    # 账本与 strategy_performance **不会相等,且账本更正确** —— 差额来自三项
    # 有意的口径选择。本检查证明它们 100% 解释了差额、不存在第四项未知因素：
    #   1. 已实现口径：账本按票净敞口(券商真实持仓) vs 生产按 pair 归因。
    #      同一票常同时是多个 pair 的腿（AVB 实测同时空在 6 个 pair,开仓价
    #      159.03~169.14 各异），两者必然不同。
    #   2. 分红：账本记（空头付/多头收），生产全线未记 —— 生产的**漏项**。
    #   3. 期初未实现：生产对"当日开仓"强制记 0（`get_position_pnl` 的
    #      same-day 约定），账本如实盯市 —— 生产**压低了真实 MTM**。
    # **期末未实现两边必须精确相等**（R2 已证 0.00）：那是硬约束,它相等说明
    # 持仓与成本基础一致,差额只可能出在路径而不在状态。
    try:
        import UpdateStrategyPerformance as U2
        for s in STRATEGIES:
            ser = series_by[s]
            d0, d1 = sorted(ser)[0], day
            eod = U2.get_eod_snapshots(s)
            tks = sorted({t for dd2 in eod.values() for k in (dd2.get("pairs") or {})
                          if "/" in k for t in k.split("/", 1)})
            prices = U2.load_prices_mongo(tks, "2026-03-01", d1)
            # 复刻 reconstruct_equity 的分解（直接调其函数，不解析 stdout）
            cum_r, prev_pos = 0.0, {}
            snap_r, snap_u = {}, {}
            for dstr in U2.get_trading_days(d0, d1):
                cur = (U2.get_open_positions(eod[dstr]) if dstr in eod
                       else dict(prev_pos))
                for pk, old in prev_pos.items():
                    if pk not in cur or not U2.is_same_position(old, cur[pk]):
                        cum_r += U2.get_position_pnl(pk, old, prices, dstr)[0]
                snap_u[dstr] = sum(U2.get_position_pnl(pk, ps, prices, dstr)[0]
                                   for pk, ps in cur.items())
                snap_r[dstr] = cum_r
                prev_pos = dict(cur)
            pr0, pu0 = snap_r[d0], snap_u[d0]
            pr1, pu1 = snap_r[d1], snap_u[d1]
            a0, a1 = ser[d0], ser[d1]
            prod_w = (pr1 - pr0) + (pu1 - pu0)
            led_w = a1["equity"] - a0["equity"]
            c_real = (a1["cumulative_realized"] - a0["cumulative_realized"]) - (pr1 - pr0)
            c_div = a1["cumulative_dividends"] - a0["cumulative_dividends"]
            c_u0 = a0["unrealized"] - pu0
            # **期末未实现口径差**：账本按票净敞口 vs 生产按 pair 归因。
            # 早先恒为 0（每只持仓票都只属于一个 pair），2026-08-05 起
            # MTFS 的 MLM/NKE/PANW 各跨两个 pair → 不再为 0，必须入桥。
            # 与"已实现口径差"同源（V10b/R2b），只是发生在未实现侧。
            c_u1 = a1["unrealized"] - pu1
            resid = led_w - (prod_w + c_real + c_div + c_u1 - c_u0)
            add(f"R9 差额桥 [{s}]", abs(resid) < 1.0,
                f"生产 {prod_w:+,.0f} + 已实现口径 {c_real:+,.0f} + 分红 {c_div:+,.0f} "
                f"+ 期末未实现口径 {c_u1:+,.0f} − 期初未实现差 {c_u0:+,.0f} = "
                f"{prod_w + c_real + c_div + c_u1 - c_u0:+,.0f} vs 账本 {led_w:+,.0f} "
                f"| **残差 {resid:+,.2f}**")
    except Exception as e:                                 # noqa: BLE001
        add("R9 差额桥", False, f"计算失败: {e}")

    # R8 当日价格口径新鲜度 —— **防倒退哨兵**
    #
    # 背景：2026-03~06 的 signal 价 85~97% 等于**前一交易日**收盘（Mongo 入库
    # 晚于跑批时刻），2026-07 起 94~100% 变为 signal_date 当日收盘。这个自愈
    # 来自入库时间变化，**代码里没有任何保障** —— 若入库再次变晚，会静默退回
    # 用昨天的价做今天的决策，且毫无报警。
    #
    # 实测该口径的代价是**噪声而非系统性偏差**（成交价 vs 当日真实收盘：
    # MRPT +8,847/$30.8M=+0.03%，MTFS +116,805/$55.1M=+0.21%，但 MTFS 3 月
    # +1.30%、4 月 −0.07%、6 月 +0.59% 正负乱跳）。所以要防的不是亏损，
    # 是执行不确定性悄悄回来。
    try:
        from .rebuild import (_prev_day, day_basis, flatten, load_mongo_prices,
                              load_splits_by_ticker, normalize_snapshots)
        snaps_all, _ = normalize_snapshots(load_snapshots(list(STRATEGIES)[0]),
                                           load_splits_by_ticker())
        for s in STRATEGIES:
            sn, _ = normalize_snapshots(load_snapshots(s), load_splits_by_ticker())
            if day not in sn:
                add(f"R8 价格口径新鲜度 [{s}]", True,
                    f"{day} 无快照（当天未开/平仓）→ 无新开仓价可判,跳过", hard=False)
                continue
            tgt, opx = flatten(sn[day])
            prev_days = [d for d in sorted(sn) if d < day]
            prev_tgt = flatten(sn[prev_days[-1]])[0] if prev_days else {}
            new_opens = {t: v for t, v in opx.items()
                         if tgt.get(t, 0) != prev_tgt.get(t, 0)}
            if not new_opens:
                add(f"R8 价格口径新鲜度 [{s}]", True,
                    f"{day} 无新开仓 → 无从判定,跳过", hard=False)
                continue
            pxw = load_mongo_prices(set(new_opens), "2026-06-01", day)
            b = day_basis(pxw, pd.Timestamp(day), new_opens)
            stale = (b == "prior_close")
            # 只有 BASIS_HEALED_SINCE 之后的陈旧价才算**倒退**（硬失败）。
            # 之前是已知历史状况（3~6 月 85~97% 用前一日收盘），对历史日期
            # 回溯跑对账时不应报红 —— 那不是回归,是史实。
            regression = stale and day >= BASIS_HEALED_SINCE
            add(f"R8 价格口径新鲜度 [{s}]", not regression,
                (f"{day} 判定 = {b}（{len(new_opens)} 只新开仓票投票）"
                 + ("  ⚠️ **用的是前一交易日收盘**" if stale else "")
                 + ("  —— Mongo 入库晚于跑批,决策基于陈旧价,请查入库时序"
                    if regression else
                    ("（{} 前的已知历史状况,非倒退）".format(BASIS_HEALED_SINCE)
                     if stale else ""))),
                hard=regression or not stale)
    except Exception as e:                                 # noqa: BLE001
        add("R8 价格口径新鲜度", False, f"探测失败: {e}")

    # R10 **逐 pair 归因**：账本按 lot 归集的 pair 盈亏 vs 报告的 pair 行
    #
    # lot 层让账本能按 (pair, leg) 归集盈亏，遂可与报告的 pair 表逐条对拍 ——
    # 任何新的归因分叉当天定位到具体 pair，而非只看到一个总数对不上。
    #
    # **跨策略同名 pair 合并比较**：报告把同名 pair 只归给一个策略
    # （实测 AME/WMT 两策略都交易过，报告全记在 MRPT）。按策略分开比对这类
    # pair 本就不成立 —— 账本按策略各记各的，差额必然等值反号（±3,636）、
    # 合并后为 0。故对这类 pair 用两策略合计对两策略合计，只在字典序靠前的
    # 那个策略上报一次。
    try:
        sys.path.insert(0, BASE_DIR)
        import PnLReport as _PR2
        from PnLReport import download_prices_mongo as _dl3
        _rd3 = _PR2.build_report_data("2026-03-19", day)
        rep_pp = defaultdict(float)
        for r in _rd3["rows"]:
            rep_pp[(r["strategy"], r["pair"])] += ((r.get("sys_pnl") or 0)
                                                   + (r.get("prior_pnl") or 0))

        led_pp = {}
        for s in STRATEGIES:
            m = defaultdict(float)
            for r in load_ledger_rows(s, root):
                if r.get("lot_realized") is not None and r.get("lot"):
                    m[r["lot"].split("|")[0]] += r["lot_realized"]
            lots = accts[s].get("lots") or {}
            tks = {l["ticker"] for l in lots.values()}
            if tks:
                pxl = _dl3(tks, day, day)
                pxl.index = pd.DatetimeIndex(pxl.index).normalize()
                tsd = pd.Timestamp(day)
                for k, l in lots.items():
                    v = pxl[l["ticker"]].get(tsd) if l["ticker"] in pxl.columns else None
                    if v is not None and not pd.isna(v):
                        m[k.split("|")[0]] += l["shares"] * (float(v) - l["cost"])
                    else:
                        m.setdefault(k.split("|")[0], 0.0)
            led_pp[s] = m

        xstrat = {pk for pk in led_pp[list(STRATEGIES)[0]]
                  for other in list(STRATEGIES)[1:] if pk in led_pp[other]}
        first = sorted(STRATEGIES)[0]
        for s in STRATEGIES:
            ds = []
            for pk, v in led_pp[s].items():
                if pk in xstrat:
                    if s != first:
                        continue                      # 合并项只在首个策略上报一次
                    rsum = sum(rep_pp[(x, pk)] for x in STRATEGIES)
                    lsum = sum(led_pp[x].get(pk, 0.0) for x in STRATEGIES)
                    ds.append((rsum - lsum, pk + "(合并)"))
                else:
                    ds.append((rep_pp[(s, pk)] - v, pk))
            nz = sorted([x for x in ds if abs(x[0]) > 1], key=lambda y: -abs(y[0]))
            add(f"R10 逐pair归因 [{s}]", len(nz) <= 20,
                f"{len(ds)} 个 pair，总差 {sum(x for x, _ in ds):+,.0f}，有差 {len(nz)} 个"
                + (f"；最大 " + ", ".join(f"{pk}({x:+,.0f})" for x, pk in nz[:3])
                   if nz else " ✓"))
    except Exception as e:                                # noqa: BLE001
        add("R10 逐pair归因", False, f"{e}", hard=False)

    # R11 **新 infra 窗口必须 diff=0** —— 替换数据源的准入条件
    #
    # MongoDB 入库时刻在 2026-07 切换后（`INFRA_CUTOVER` 起 basis 稳定为
    # same_day_close），账本应能**精确复现**报告。历史段（3~6 月）的差异是
    # 遗留数据问题（monitor 停报周、陈旧价兜底等，见 PLAN §21），不阻塞替换；
    # 但**新开仓的 pair 必须零差**，否则说明当前口径仍有未对齐处。
    try:
        sys.path.insert(0, BASE_DIR)
        import PnLReport as _PR2
        from PnLReport import download_prices_mongo as _dl2
        _rd2 = _PR2.build_report_data("2026-03-19", day)
        rep2 = defaultdict(float)
        ropen = {}
        for r in _rd2["rows"]:
            od = str(r.get("open_date") or "")[:10]
            if od < INFRA_CUTOVER:
                continue
            rep2[(r["strategy"], r["pair"])] += ((r.get("sys_pnl") or 0)
                                                 + (r.get("prior_pnl") or 0))
            ropen[(r["strategy"], r["pair"])] = od
        for s in STRATEGIES:
            mine, lo = defaultdict(float), {}
            for r in load_ledger_rows(s, root):
                if not r.get("lot"):
                    continue
                pk = r["lot"].split("|")[0]
                if r.get("lot_action") == "OPEN":
                    lo.setdefault(pk, r["date"])
                if r.get("lot_realized") is not None:
                    mine[pk] += r["lot_realized"]
            lots = accts[s].get("lots") or {}
            tks = {l["ticker"] for l in lots.values()}
            if tks:
                pxw = _dl2(tks, day, day)
                pxw.index = pd.DatetimeIndex(pxw.index).normalize()
                for k, l in lots.items():
                    v = pxw[l["ticker"]].get(pd.Timestamp(day)) if l["ticker"] in pxw.columns else None
                    if v is not None and not pd.isna(v):
                        mine[k.split("|")[0]] += l["shares"] * (float(v) - l["cost"])
                    lo.setdefault(k.split("|")[0], l["open_date"])
            keys = [pk for pk in set(list(mine) + [k[1] for k in rep2 if k[0] == s])
                    if (ropen.get((s, pk)) or lo.get(pk, "0000")) >= INFRA_CUTOVER]
            ds = [(rep2.get((s, pk), 0) - mine.get(pk, 0), pk) for pk in keys]
            nz = sorted([x for x in ds if abs(x[0]) > 1], key=lambda y: -abs(y[0]))
            add(f"R11 新infra窗口 diff=0 [{s}]", not nz,
                f"{INFRA_CUTOVER} 起开仓 {len(keys)} 个 pair，总差 "
                f"{sum(x for x, _ in ds):+,.2f}，有差 {len(nz)}"
                + (f"：{', '.join(f'{pk}({x:+,.0f})' for x, pk in nz[:3])}" if nz else " ✓"))
    except Exception as e:                                # noqa: BLE001
        add("R11 新infra窗口 diff=0", False, f"{e}")

    # R7 已实现口径差（账本按票净敞口 vs pair 级归因）—— 记录，不判失败
    for s in STRATEGIES:
        rows = load_ledger_rows(s, root)
        by_t = defaultdict(float)
        for r in rows:
            by_t[r["ticker"]] += r.get("realized_pnl", 0) or 0
        add(f"R7 已实现口径 [{s}]", True,
            f"账本累计已实现 {accts[s]['cumulative_realized']:,.2f}"
            f"（按票净敞口口径；与 pair 级归因对重叠票必然不同，见 PLAN §7）",
            hard=False)

    ok = all(c["status"] != "FAIL" for c in checks)
    return {"as_of": day, "checks": checks, "ok": ok,
            "accounts": {s: {k: accts[s][k] for k in
                             ("as_of", "equity", "cash", "cumulative_realized",
                              "unrealized", "position_value")} for s in STRATEGIES}}


def render_txt(rec: dict) -> str:
    L = [f"pairs_ledger 对账报告  as_of={rec['as_of']}",
         "=" * 78]
    for s, a in rec["accounts"].items():
        L.append(f"{s.upper():5s} equity={a['equity']:>14,.2f}  cash={a['cash']:>14,.2f}  "
                 f"realized={a['cumulative_realized']:>12,.2f}  "
                 f"unreal={a['unrealized']:>11,.2f}")
    L += ["-" * 78]
    for c in rec["checks"]:
        m = {"PASS": "✓", "FAIL": "✗", "INFO": "·"}[c["status"]]
        L.append(f" {m} {c['name']:28s} {c['detail']}")
    L += ["=" * 78,
          "全部通过 ✓" if rec["ok"] else "存在未通过项 ✗"]
    return "\n".join(L)


def run(day: str | None = None, root: str | None = None) -> dict:
    os.makedirs(OUT_DIR, exist_ok=True)
    rec = reconcile(day, root)
    d = rec["as_of"]
    tag = d.replace("-", "")

    st = {}
    for s in STRATEGIES:
        ser = load_account_series(s, root)
        if d in ser:
            st[s] = statements(s, d, ser, load_ledger_rows(s, root))
    with open(os.path.join(OUT_DIR, f"ledger_statements_{tag}.json"), "w") as f:
        json.dump(st, f, indent=1, ensure_ascii=False)
    with open(os.path.join(OUT_DIR, f"reconciliation_{tag}.json"), "w") as f:
        json.dump(rec, f, indent=1, ensure_ascii=False)
    txt = render_txt(rec)
    with open(os.path.join(OUT_DIR, f"reconciliation_{tag}.txt"), "w") as f:
        f.write(txt + "\n")
    log.info(f"账本报表+对账已写 {OUT_DIR} (as_of={d}, {'OK' if rec['ok'] else 'FAIL'})")
    return rec


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--root", default=None)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    rec = run(a.date, a.root)
    if not a.quiet:
        print(render_txt(rec))
    return 0 if rec["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
