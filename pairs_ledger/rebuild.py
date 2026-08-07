"""从 inventory_history 重建 MRPT/MTFS 成交账本（3/19 → 今日）。

**逐日按票净额差分**（与 AISS/SSRS 的 process_day 同法，2026-08-06 修正）：
  target = 当日快照全部 pair 展平后的每票净股数（s2 腿为负 = 融券做空）
  delta  = target − 账本当前持仓 → 逐票成交
  先卖后买（现金流序）

  **为何不用 pair 级开/平事件**：同一票常同时在多个 pair（MTFS 实测最多 4 个），
  且频繁「同日平掉重开」（MRPT 24 次）。只按 pair 开/平配对会在票级留下
  互相抵消的幽灵敞口；净额差分对任何组合方式都成立。
  （注：持有期内股数本身几乎不漂移 —— MRPT 0 次、MTFS 2 次且均为拆股，
   见 normalize_snapshots。所以差分出来的成交都是真实交易。）

成交价 = **当日决策价**（盯市另用当日真实收盘，见 rebuild 内注）：
  · 该票当日有 pair 开仓 → 用其 open_s{1,2}_price（账面事实，最优先）
  · 否则 → Mongo 当日 basis 对应的收盘价
  · 当日 basis 由"当天开仓价属于哪个候选"投票判定（逐日，不按月切分），
    无开仓则沿用上一已判定 basis —— 天然处理 2026-03~06 与 07~08
    两个口径时期的逐日摇摆（MongoDB 入库时间变化所致）。

账本粒度：**按票**（blended 成本），镜像 AISS/SSRS 且与 QuantConnect 的
per-symbol 持仓兼容。pair 级盈亏是策略归因，与按票已实现的切分不同
（MTFS 有 53 天单票跨多 pair），但**总盈亏（已实现+未实现）恒等** —— 这是 V10。

用法（**只能 someopark_run**）：
    python -m pairs_ledger.rebuild mrpt --root /tmp/xxx   # 沙盒
    python -m pairs_ledger.rebuild all  --root /tmp/xxx
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date as _date

import numpy as np
import pandas as pd

from .ledger import (Account, BASE_DIR, INITIAL_CASH, STRATEGIES, _cfg, account_path,
                     append_ledger, history_dir, ledger_path, load_ledger_rows)

log = logging.getLogger("pairs_ledger.rebuild")
PRICE_TOL = 0.02          # PnL 复现容差（美元级，覆盖四舍五入）
EXIT_BASIS = os.environ.get("PAIRS_LEDGER_EXIT_BASIS", "market_close")


# ── 快照 ─────────────────────────────────────────────────────────────────────

def load_snapshots(strategy: str) -> dict:
    """{as_of: {pair_key: pair_dict}}；同日多快照取文件名最新者。"""
    out: dict = {}
    for fp in sorted(glob.glob(os.path.join(BASE_DIR, _cfg(strategy)["snap_glob"]))):
        try:
            d = json.load(open(fp))
        except Exception:  # noqa: BLE001
            log.warning(f"快照不可读,跳过: {os.path.basename(fp)}")
            continue
        a = d.get("as_of")
        if a:
            out[a] = {k: v for k, v in (d.get("pairs") or {}).items()
                      if isinstance(v, dict)}
    return dict(sorted(out.items()))


def load_monitor_pnl(strategy: str) -> dict:
    """{(signal_date, pair): 账面平仓盈亏}；来源 = 监控与信号里的 CLOSE 事件。

    排除回测注入的模拟平仓（note 含 'Injected position closed in simulation'）——
    2026-08-06 实测信号侧 228 事件里仅 153 为真实。
    """
    out: dict = {}
    for fp in sorted(glob.glob(os.path.join(BASE_DIR,
                                            "trading_signals/combined_signals_*.json"))):
        try:
            d = json.load(open(fp))
        except Exception:  # noqa: BLE001
            continue
        sd = d.get("signal_date")
        if not sd:
            continue
        sec = d.get(strategy) or {}
        pool = list(sec.get("signals") or []) + list(sec.get("step1_closes") or [])
        pool += list((d.get("position_monitor") or {}).get(strategy) or [])
        for s in pool:
            if s.get("action") not in ("CLOSE", "CLOSE_STOP"):
                continue
            if "Injected position closed in simulation" in (s.get("note") or ""):
                continue
            pk = s.get("pair")
            if not pk:
                continue
            v = s.get("unrealized_pnl")
            if v is not None:
                out[(sd, pk)] = float(v)
    return out


MONITOR_RAW_USD_SINCE = "2026-07-03"   # 此日起 monitor 的 uPnL 不再 ×regime scale


def _raw_factor(ticker: str, on_date: str, splits_by_ticker: dict) -> float:
    """当前(拆股调整后)口径 → **该日原始口径** 的价格乘数。

    与 `to_current_caliber` 互逆；此处只用于把 Mongo 调整后价格还原成事件当时
    的原始口径，以便与记录的 `unrealized_pnl` 逐笔对拍。
    """
    f = 1.0
    for sp in splits_by_ticker.get(ticker, []):
        if sp.get("execution_date", "") > on_date:
            try:
                f *= float(sp["split_to"]) / float(sp["split_from"])
            except Exception:                              # noqa: BLE001
                continue
    return f


INFRA_CUTOVER = "2026-07-07"   # MongoDB 入库时刻切换后 basis 稳定为 same_day_close
_REPORT_CACHE: dict = {}       # build_report_data 很贵(全量扫 signal 文件+下载价格),
                               # 两个策略共用同一份 → 缓存,钩子耗时从 304s 降到可接受


def _report_data(start: str, end: str):
    k = (start, end)
    if k not in _REPORT_CACHE:
        sys.path.insert(0, BASE_DIR)
        import PnLReport as _PR
        _REPORT_CACHE[k] = _PR.build_report_data(start, end)
    return _REPORT_CACHE[k]


def resolve_close_fills(strategy: str, splits_by_ticker: dict,
                        px: pd.DataFrame) -> tuple[dict, dict, dict]:
    """**解出**每笔真实平仓的成交价 —— 不猜，而是找能复现记录 P&L 的那一组。

    返回 ({(date, ticker): 当前口径价格}, 判定统计,
          {(date, pair): {ticker: 平仓时的股数}} —— 供识别"隐藏平仓"）。

    做法：对每个真实 CLOSE/CLOSE_STOP 事件，取前序快照的 shares/open_price
    （均为**原始口径**），逐一试候选价格组合，看哪组能重算出事件记录的
    `unrealized_pnl`：
      候选价：事件自带 s{1,2}_price / Mongo 当日收盘 / Mongo 前一交易日收盘
      候选口径：raw 或 ×regime scale

    **两个必须考虑的时点变化**：
      1. `MONITOR_RAW_USD_SINCE`(2026-07-03) 之前 monitor 的 uPnL **含 regime
         scale**（DailySignal: "历史 monitor_log 的 ±11% 失真不回改"）。
      2. MongoDB 入库时刻在 2026-07 变过：3~6 月 signal 价多为**前一交易日**
         收盘，7 月起多为当日收盘。故候选里两个日子都要试 —— 不能按月硬切。

    **排除模拟注入**（note 含 'Injected position closed in simulation'）：
    实测 231 笔平仓事件中 91 笔是模拟、从未真实发生，它们不该进账本。

    实测匹配率约 93%；未匹配的全部是 `unrealized_pnl == 0` 的**当日开平仓**
    （生产对同日开仓强制记 0），这类用开仓价作平仓价，P&L 自然为 0，一致。
    """
    snaps = load_snapshots(strategy)          # **原始口径**，不归一
    out: dict = {}
    closed_legs: dict = {}
    stat: dict = defaultdict(int)
    days_sorted = sorted(snaps)

    for fp in sorted(glob.glob(os.path.join(
            BASE_DIR, "trading_signals/combined_signals_*.json"))):
        try:
            d = json.load(open(fp))
        except Exception:                                  # noqa: BLE001
            continue
        sd = d.get("signal_date")
        if not sd:
            continue
        sf = (d.get(strategy) or {}).get("scale_factor") or 1.0
        evs = [e for e in ((d.get("position_monitor") or {}).get(strategy) or [])
               if e.get("action") in ("CLOSE", "CLOSE_STOP")]
        # **真实平仓先处理、注入的后补** —— 同一 (日期,票) 先到者占位,
        # 若让注入事件抢先，会顶替掉真实平仓解出的价格
        # （实测会把 MRPT 归因差从 +796 推回 +7,195）。
        evs.sort(key=lambda e: "Injected position closed in simulation"
                 in (e.get("note") or ""))
        # **不排除模拟注入平仓**：它们虽非真实交易,但确实改变了 inventory 快照,
        # 且 `PnLReport.load_close_events` 也照记其 `unrealized_pnl`。账本若排除,
        # 这些平仓会退回重构价、与报告用不同的价。它们无 s{1,2}_price,
        # 但有 unrealized_pnl,仍可用 Mongo 候选价解出。
        for e in evs:
            # **排除模拟注入平仓**：`PnLReport.load_close_events` 有 §8.2 生命周期
            # 去重（"fake-close re-emissions inflated prior_pnl by ~$25k,
            # must be dropped"），实际会丢弃它们。账本纳入反而与报告不同口径 ——
            # 实测纳入会把 MRPT 归因差从 **+796 推回 +7,195**。
            # 这些注入仓位仍会改变快照,其平仓由 lot 层按重构价平掉。
            _is_inj = "Injected position closed in simulation" in (e.get("note") or "")
            if _is_inj:
                stat["injected"] += 1
            U, pk = e.get("unrealized_pnl"), e.get("pair")
            if U is None or not pk or "/" not in pk:
                stat["skip_no_pnl"] += 1
                continue
            t1, t2 = pk.split("/", 1)
            pos = None
            for k in reversed([x for x in days_sorted if x < sd]):
                if pk in snaps[k] and snaps[k][pk].get("direction"):
                    pos = snaps[k][pk]
                    break
            if not pos:
                stat["skip_no_snapshot"] += 1
                continue
            s1, s2 = int(pos.get("s1_shares", 0) or 0), int(pos.get("s2_shares", 0) or 0)
            o1, o2 = pos.get("open_s1_price"), pos.get("open_s2_price")
            if not o1 or not o2:
                stat["skip_no_open_px"] += 1
                continue

            ts = pd.Timestamp(sd)
            idx = [i for i in px.index if i <= ts]

            def _mp(t, back):
                if t not in px.columns or back >= len(idx):
                    return None
                v = px[t].loc[idx[-1 - back]]
                return (None if pd.isna(v)
                        else float(v) * _raw_factor(t, sd, splits_by_ticker))

            # 候选价**保守取三种**：事件自带 / Mongo 当日 / Mongo 前一日。
            # 试过扩充（两腿混合口径、d2、单腿 recorded）—— **反而更差**：
            # 候选越多越容易有某组合"碰巧"复现 U 而实际是错价（假阳性），
            # 实测 MTFS 逐 pair 总差从 −20,262 恶化到 −25,634，
            # 且让原本正确的 AIG/AVB 冒出 −2,914 的新差异。宁可解不出走回退。
            cands = [("recorded", e.get("s1_price"), e.get("s2_price")),
                     ("same_day", _mp(t1, 0), _mp(t2, 0)),
                     ("prior_day", _mp(t1, 1), _mp(t2, 1))]
            hit = None
            for name, p1, p2 in cands:
                if not p1 or not p2:
                    continue
                calc = (p1 - o1) * s1 + (p2 - o2) * s2
                for mode, exp in (("raw", calc), ("scaled", calc * sf)):
                    if sd >= MONITOR_RAW_USD_SINCE and mode == "scaled":
                        continue          # 该日起记录值已是原始美元
                    if abs(exp - U) < max(1.0, abs(U) * 0.005):
                        hit = (name, mode, p1, p2)
                        break
                if hit:
                    break

            if hit is None and _is_inj and s2:
                # **模拟注入平仓且无候选价可复现** —— 这类仓位从未真实成交，
                # 其 `unrealized_pnl` 是模拟逻辑自算的、不对应任何市场价
                # （实测 CVS/LEN 7/23 记 −3,289.01，而 7/22 是 −2,644.62、
                #  7/23 是 −3,784.65，都不是）。既然没有真实成交，
                # **记录值就是唯一权威**：保持 s1 用当日市价，解出 s2 的价
                # 使 pair 盈亏精确等于记录值。这样报告与账本对这类合成事件
                # 完全一致（否则每笔注入平仓都会留下一个无法消除的差）。
                p1m = _mp(t1, 0)
                if p1m:
                    need = (U if sd >= MONITOR_RAW_USD_SINCE else U / sf)
                    p2s = (need - (p1m - o1) * s1) / s2 + o2
                    hit = ("injected_solved", "exact", p1m, p2s)
                    stat["injected_solved"] += 1
            if hit is None:
                if abs(float(U)) < 1e-9:
                    # 当日开平仓：生产记 0；用开仓价作平仓价 → P&L 恒为 0，自洽
                    hit = ("same_day_open_close", "zero", float(o1), float(o2))
                else:
                    stat["unresolved"] += 1
                    log.warning(f"[{strategy}] {sd} {pk} 平仓价无法解出"
                                f"（记录 uPnL={U}）— 该笔回退重构价")
                    continue
            name, mode, p1, p2 = hit
            stat[f"{name}/{mode}"] += 1
            # 原始口径 → 当前口径（账本工作在调整后空间）
            # **按 (日期, "pair|腿") 存**，不能只按 (日期, 票)：同一票可能在同一天
            # 于多个 pair 同时平仓，各 pair 记录的 unrealized_pnl 隐含的价格未必一致
            # （实测 2026-07-06 NKE 同时平于 PWR/NKE 与 CVS/NKE，按票存会让先解出的
            #  占槽、另一个拿到别人的价 → CVS/NKE 差 +1,432）。lot 结构下每条腿
            # 本就该有自己的成交价；按票的键保留作回退。
            for leg, t, praw in (("s1", t1, p1), ("s2", t2, p2)):
                adj = praw / _raw_factor(t, sd, splits_by_ticker)
                out.setdefault((sd, f"{pk}|{leg}"), adj)
                out.setdefault((sd, t), adj)
            # 平仓股数也归一到当前口径（供识别隐藏平仓）
            f1 = _raw_factor(t1, sd, splits_by_ticker)
            f2 = _raw_factor(t2, sd, splits_by_ticker)
            closed_legs[(sd, pk)] = {t1: int(round(s1 * f1)), t2: int(round(s2 * f2))}
    return out, dict(stat), closed_legs


# ── 价格 ─────────────────────────────────────────────────────────────────────

def load_mongo_prices(tickers: set, start: str, end: str) -> pd.DataFrame:
    """MongoDB stock_data 收盘价宽表（与 PnLReport 同源，含拆股回溯调整）。"""
    sys.path.insert(0, BASE_DIR)
    from PnLReport import download_prices_mongo
    px = download_prices_mongo(set(tickers), start, end)
    px.index = pd.DatetimeIndex(px.index).normalize()
    return px.sort_index()


def _px(px: pd.DataFrame, t: str, day: pd.Timestamp):
    if t not in px.columns:
        return None
    s = px[t]
    if day not in s.index:
        return None
    v = s.loc[day]
    return None if pd.isna(v) else float(v)


def _prev_day(px: pd.DataFrame, day: pd.Timestamp):
    idx = px.index[px.index < day]
    return idx[-1] if len(idx) else None


# ── 拆股归一 ─────────────────────────────────────────────────────────────────

def load_splits_by_ticker() -> dict:
    """{ticker: [split, ...]}，**只读** price_data/splits_cache.json，不发 API。

    单一真源：拆股/合股的检测、缓存、应用全部归 `CorporateActions.py`
    （DailySignal 在 Step 1 monitor 之前调用 `apply_to_inventory` 维护该 cache）。
    本函数只把它读成 ticker 索引，供 `adjust_position_view` 与分红口径换算复用
    —— **不复制任何拆股判定逻辑**。
    """
    try:
        sys.path.insert(0, BASE_DIR)
        from CorporateActions import _load_splits_cache
    except Exception as e:                                 # noqa: BLE001
        log.warning(f"CorporateActions 不可导入({e}) — 按无拆股处理（拆股票会失真!）")
        return {}
    out: dict = {}
    for sp in (_load_splits_cache().get("results") or []):
        if sp.get("ticker"):
            out.setdefault(sp["ticker"], []).append(sp)
    return out


def normalize_snapshots(snaps: dict, splits_by_ticker: dict) -> tuple[dict, list]:
    """把历史快照的 shares/open_price 归一到**当前口径**（与回溯调整后的价格源对齐）。

    **不自己实现拆股逻辑** —— 逐 pair 委托 `CorporateActions.adjust_position_view()`，
    即 `RiskManager.positions_on()`、`PnLReport` equity curve、
    `UpdateStrategyPerformance.compute_pnl_mongo()` 正在用的**同一个函数**。
    这保证账本与既有报表对历史仓位的换算完全一致（实测 R3b：多头 720,083.16 /
    空头 515,139.48 与 risk_report 分到分吻合）。

    它的三重守卫（比只比日期稳健 —— execution 当日早晨的快照仍是旧口径）：
      1. polygon_id 不在 pos 的 applied_corporate_actions 留痕中
      2. open_date < execution_date（拆股后开的仓天然新口径）
      3. execution_date <= today（未来生效的 split 价格源尚未调整）

    合股同样成立：factor = split_to/split_from；合股 10:1 → f=0.1
    （股数 ×0.1、开仓价 ÷0.1 即 ×10），无需分支。

    **为何必须归一**：价格源在拆股后全历史回溯调整（实测日志
    "KLAC rows before 2026-06-12 ÷10.0"），而 inventory 只在拆股当日被
    CorporateActions 改写一次。两者混用时逐日净额差分会把「67 股 → 670 股」
    读成买入 603 股、再用旧成本 $2011 卖新价 $201 —— 实测 KLAC 一只票
    就给 MTFS 虚增 20.4 万亏损。
    """
    if not splits_by_ticker:
        return snaps, []
    try:
        sys.path.insert(0, BASE_DIR)
        from CorporateActions import adjust_position_view
    except Exception as e:                                 # noqa: BLE001
        log.warning(f"adjust_position_view 不可导入({e}) — 跳过归一（拆股票会失真!）")
        return snaps, []

    applied_log: list = []
    out: dict = {}
    for day, pairs in snaps.items():
        np_: dict = {}
        for pk, p in pairs.items():
            if "/" not in pk or not p.get("direction"):
                np_[pk] = p
                continue
            t1, t2 = pk.split("/", 1)
            # open_date 缺失兜底：adjust_position_view 的守卫 2 是
            # `open_date < execution_date`，空 open_date 会被判为"需归一"。
            # 快照必然满足 as_of >= open_date，故用 as_of 代入是保守且良定义的
            # （只会少归一、不会误归一）。生产数据里 pair 都带 open_date，
            # 这条兜底是防御性的。
            pin = p if p.get("open_date") else {**p, "open_date": day}
            q = adjust_position_view(pin, t1, t2, snapshot_as_of=day,
                                     splits_by_ticker=splits_by_ticker)
            for leg, t in (("1", t1), ("2", t2)):
                a, b = p.get(f"s{leg}_shares"), q.get(f"s{leg}_shares")
                if a != b:
                    applied_log.append((day, pk, t, round(b / a, 6) if a else None))
            np_[pk] = q
        out[day] = np_
    return out, applied_log


def to_current_caliber(values: dict, on_date: str, splits_by_ticker: dict) -> dict:
    """{ticker: 某日原始口径的每股金额} → {ticker: 当前(拆股调整后)口径}。

    **复用 `CorporateActions.adjust_position_view`**，不自写 factor 计算 ——
    拆股/合股的判定与换算在本仓库只有一处真源（用户要求 2026-08-06）。
    做法：把每股金额塞进 `open_s{1,2}_price`、`open_date` 设为该金额所属日期，
    其三重守卫（留痕 / open_date<execution_date / execution_date<=today）
    正好就是"该日之后发生的拆股才需要调整"的语义。

    适用于两类每股量：**平仓执行价**（事件里记的是当日原始口径）与
    **分红每股金额**（ex-date 当日口径）。
    """
    if not values or not splits_by_ticker:
        return dict(values)
    try:
        sys.path.insert(0, BASE_DIR)
        from CorporateActions import adjust_position_view
    except Exception as e:                                 # noqa: BLE001
        log.warning(f"adjust_position_view 不可导入({e}) — 每股金额未做拆股归一")
        return dict(values)
    out = {}
    items = list(values.items())
    for i in range(0, len(items), 2):                      # 一次处理两腿(函数是 pair 形状)
        chunk = items[i:i + 2]
        t1, v1 = chunk[0]
        t2, v2 = chunk[1] if len(chunk) > 1 else (t1, 0.0)
        pos = {"open_date": on_date, "s1_shares": 1, "s2_shares": 1,
               "open_s1_price": float(v1), "open_s2_price": float(v2)}
        adj = adjust_position_view(pos, t1, t2, snapshot_as_of=on_date,
                                   splits_by_ticker=splits_by_ticker)
        out[t1] = adj.get("open_s1_price", v1)
        if len(chunk) > 1:
            out[t2] = adj.get("open_s2_price", v2)
    return out


def load_dividends(tickers: set, start: str, end: str,
                   splits_by_ticker: dict) -> tuple[dict, dict]:
    """{ticker: [(ex_date, 当前口径每股金额)]}, 覆盖率统计。

    **只读** price_data/dividends_cache.json —— 不调 Polygon、不回写。
    该缓存是 PriceDataStore / portfolio_ledger 共用的生产文件，pairs 侧没有
    写入的必要，只读可彻底消除在跑批时段污染共享缓存的风险。缺票不静默：
    计入 `missing` 并由调用方 log 出来。

    **空头付股息**：`Account.dividend` 用带符号股数,空腿自然为负现金流 ——
    pairs 有一半是融券空腿,漏记分红会系统性高估收益。
    """
    try:
        with open(os.path.join(BASE_DIR, "price_data", "dividends_cache.json")) as f:
            cache = json.load(f)
    except Exception as e:                                 # noqa: BLE001
        log.warning(f"dividends_cache 不可读({e}) — 本次不计分红")
        return {}, {"missing": sorted(tickers), "covered": []}
    out, missing, covered = {}, [], []
    for t in sorted(tickers):
        entry = cache.get(t)
        if entry is None:
            missing.append(t)
            continue
        covered.append(t)
        sel = []
        for d in entry.get("dividends", []):
            ex = d.get("ex_dividend_date", "")
            if not (start <= ex <= end):
                continue
            amt = float(d.get("cash_amount", 0) or 0)
            if amt:
                adj = to_current_caliber({t: amt}, ex, splits_by_ticker).get(t, amt)
                sel.append((ex, adj))
        if sel:
            out[t] = sorted(sel)
    return out, {"missing": missing, "covered": covered}


# ── 展平与口径 ───────────────────────────────────────────────────────────────

def flatten(pairs: dict) -> tuple[dict, dict]:
    """pair 字典 → ({ticker: 净股数}, {ticker: 当日开仓价})。

    净额语义: 同一票可同时在多个 pair(MTFS 实测最多 4 个),账本记净敞口,
    否则会凭空造出互相抵消的成交。
    """
    net: dict = defaultdict(int)
    opx: dict = {}
    for pk, p in pairs.items():
        if not p.get("direction") or "/" not in pk:
            continue
        s1, s2 = pk.split("/", 1)
        net[s1] += int(p.get("s1_shares", 0) or 0)
        net[s2] += int(p.get("s2_shares", 0) or 0)
        if p.get("open_s1_price"):
            opx.setdefault(s1, float(p["open_s1_price"]))
        if p.get("open_s2_price"):
            opx.setdefault(s2, float(p["open_s2_price"]))
    return {t: v for t, v in net.items() if v}, opx


def day_basis(px: pd.DataFrame, ts: pd.Timestamp, new_opens: dict) -> str:
    """当日 price_basis: 用"当天新开仓的价格落在哪个候选"投票判定。"""
    votes = defaultdict(int)
    for t, price in new_opens.items():
        same = _px(px, t, ts)
        pv = _prev_day(px, ts)
        prior = _px(px, t, pv) if pv is not None else None
        tol = max(0.005, price * 1e-5)
        hs = same is not None and abs(same - price) < tol
        hp = prior is not None and abs(prior - price) < tol
        if hs and hp:
            votes["ambiguous"] += 1
        elif hs:
            votes["same_day_close"] += 1
        elif hp:
            votes["prior_close"] += 1
        else:
            votes["unmatched"] += 1
    for b in ("same_day_close", "prior_close"):        # 明确口径优先于 ambiguous
        if votes.get(b):
            return b if votes[b] >= votes.get("prior_close" if b == "same_day_close"
                                              else "same_day_close", 0) else b
    return "ambiguous" if votes else "carry"


# ── 重演 ─────────────────────────────────────────────────────────────────────

class _State:
    """跨日滚动状态：只保留 price basis 沿用。

    lot 版 `process_day` 直接从快照取目标 lot，不再需要 prev/last target。
    """

    def __init__(self, basis_cur: str = "prior_close"):
        self.basis_cur = basis_cur


def process_day(acct: Account, day: str, px: pd.DataFrame, snaps: dict,
                st: _State, seen: set, root: str, stats=None,
                divs: dict | None = None, close_px: dict | None = None) -> list:
    """一个交易日的完整记账：分红 → **按 pair-leg lot 对账** → mark → 落盘。

    **为何按 lot 而非净额差分**（2026-08-06 结构性改造）：
      1. 报告按 **pair 归因**（每个 pair 用自己的开仓价）。账本若只记每票净敞口,
         已实现的切分与报告对不上。lot 层按 (pair, leg) 独立记成本 → 同口径。
      2. 逐日净额差分**看不见「同日平掉又重开」**：pair 在相邻两个快照里都在,
         中间那次平仓被吞掉。实测 MRPT 99 次真实平仓中快照只见 84 次,
         漏掉的 15 次报告计作 `prior_pnl`（DXCM/UDR −5,935.16 与逐 pair 对拍
         的差分毫不差）。**lot 用 `open_date` 变化即可识别重开**。

    每票净敞口 = 该票所有 lot 之和 = 快照展平值 → V1/V2/R1/R3 等状态检查照常。

    无快照日 = 当天没跑系统 = 零交易，持仓顺延、照常 mark。
    """
    stats = stats if stats is not None else defaultdict(int)
    ts = pd.Timestamp(day)
    if ts not in px.index:
        return []                                          # 非交易日

    rows = []
    for t, items in (divs or {}).items():
        for ex, amt in items:
            if ex == day:
                r = acct.dividend(day, t, amt)             # 多头收、空头付
                if r:
                    rows.append(r)
                    stats["dividends"] += 1

    if day not in snaps:
        stats["carry_day"] += 1
        acct.mark(day, px.loc[ts])
        acct.save_history(day)
        append_ledger(acct.strategy, rows, seen, root=root)
        return rows

    # ── 当日目标 lot（直接取自快照：快照本就是按 pair 给腿的）────────────
    target: dict = {}
    for pk, p in snaps[day].items():
        if not p.get("direction") or "/" not in pk:
            continue
        for leg, t in zip(("s1", "s2"), pk.split("/", 1)):
            q = int(p.get(f"{leg}_shares", 0) or 0)
            if not q:
                continue
            target[f"{pk}|{leg}"] = (t, q, p.get(f"open_{leg}_price"),
                                     str(p.get("open_date") or day))

    def _fallback(t):
        """无记录价时的回退：按当日 basis 估计 prices_today。"""
        pv = _prev_day(px, ts)
        v = (_px(px, t, ts) if st.basis_cur == "same_day_close"
             else _px(px, t, pv) if pv is not None else None)
        return v if v is not None else _px(px, t, ts)

    # 口径投票（沿用：当天新开仓价落在哪个候选收盘上）
    new_opens = {}
    for key, (t, q, opx, od) in target.items():
        cur = (acct.data.get("lots") or {}).get(key)
        if opx and (cur is None or cur["open_date"] != od):
            new_opens.setdefault(t, float(opx))
    b = day_basis(px, ts, new_opens)
    if b not in ("carry", "ambiguous"):
        st.basis_cur = b
    stats[f"basis_{b}"] += 1

    # ── 先平（含同日重开的旧 lot）────────────────────────────────────────
    for key, lot in list((acct.data.get("lots") or {}).items()):
        tgt = target.get(key)
        if tgt is not None and tgt[3] == lot["open_date"]:
            continue                                       # 仍是同一 lot
        # 先按该 lot 自己的键取价（同票多 pair 同日平仓时各有各的价），再退回按票
        _hit = (close_px or {}).get((day, key))
        if _hit is None:
            _hit = (close_px or {}).get((day, lot["ticker"]))
        pxc = _hit if _hit is not None else _fallback(lot["ticker"])
        if pxc is None:
            stats["close_unpriced"] += 1
            log.warning(f"[{acct.strategy}] {day} {key} 平仓缺价 — 跳过(持仓将与快照不符)")
            continue
        r = acct.lot_close(day, key, float(pxc))
        if r:
            r["price_basis"] = "close_resolved" if _hit is not None else b
            rows.append(r)
            stats["lot_close"] += 1
            if tgt is not None:
                stats["hidden_reopen"] += 1                # 同日平掉又重开

    # ── 后开 / 调整 ──────────────────────────────────────────────────────
    for key, (t, q, opx, od) in target.items():
        lot = (acct.data.get("lots") or {}).get(key)
        if lot is None:
            price = float(opx) if opx else _fallback(t)
            if price is None:
                stats["open_unpriced"] += 1
                log.warning(f"[{acct.strategy}] {day} {key} 开仓缺价 — 跳过")
                continue
            r = acct.lot_open(day, key, t, q, price, od)
            if r:
                r["price_basis"] = "open_price" if opx else b
                rows.append(r)
                stats["lot_open"] += 1
        elif int(lot["shares"]) != q:
            price = float(opx) if opx else _fallback(t)
            if price is None:
                continue
            r = acct.lot_resize(day, key, q, price)
            if r:
                r["price_basis"] = "lot_resize"
                rows.append(r)
                stats["lot_resize"] += 1

    # **持久化 basis_cur**：无记录价的平仓走回退,回退价取决于当日 basis。
    # 若不存，daily_update 从中途冷启动会退回默认 "prior_close"，
    # 与全量重建取到不同的价（实测 MTFS 8/04 两笔注入平仓因此分叉）。
    acct.data["price_basis_state"] = st.basis_cur
    acct.mark(day, px.loc[ts])
    acct.save_history(day)
    append_ledger(acct.strategy, rows, seen, root=root)
    return rows


def _prepare(strategy: str, root: str, upto: str | None):
    """快照(含拆股归一) + 价格 + 交易日历 —— rebuild / daily_update 共用前置。"""
    cfg = _cfg(strategy)
    snaps = load_snapshots(strategy)
    if not snaps:
        raise RuntimeError(f"[{strategy}] 无快照")

    # **inventory 主文件是当日权威状态,必须覆盖同日的历史快照**（2026-08-06 修）
    #
    # `inventory_history/` 的快照是**跑批中途**写的,可能落后于最终 inventory：
    # 实测 2026-08-05 跑批后 —— MTFS 快照 as_of=08-05 只有 3 个 pair，
    # 而 inventory_mtfs.json 同日有 **9 个 pair**；MRPT 更是连 08-05 的快照
    # 都没有（最新快照仍是 08-04）。当天新开的仓只落在主文件里。
    # 只读 history 会漏掉当日全部新开仓 —— 实测导致 R1 失败
    # （MRPT 漏 NVDA/AMD、MTFS 漏 11 只）、R3b 多头少算 115 万。
    #
    # 历史日不受影响：过往快照完整（V2 逐日持仓全对、8/04 的 R1 通过），
    # 只有"当天新开仓尚未落快照"这一情形需要主文件兜底。
    try:
        _inv_now = json.load(open(os.path.join(BASE_DIR, cfg["inventory"])))
        _ia = _inv_now.get("as_of")
        _ip = {k: v for k, v in (_inv_now.get("pairs") or {}).items()
               if isinstance(v, dict)}
        if _ia and _ip:
            _n_old = sum(1 for v in (snaps.get(_ia) or {}).values()
                         if v.get("direction"))
            _n_new = sum(1 for v in _ip.values() if v.get("direction"))
            snaps[_ia] = _ip                      # 主文件覆盖同日快照
            snaps = dict(sorted(snaps.items()))
            if _n_new != _n_old:
                log.info(f"[{strategy}] {_ia} 用 inventory 主文件覆盖快照: "
                         f"在持 pair {_n_old} → {_n_new}（当日新开仓未落快照）")
    except Exception as e:                                 # noqa: BLE001
        log.warning(f"[{strategy}] inventory 主文件读取失败({e}) — 仅用历史快照,"
                    f"当日新开仓可能漏记")
    splits = load_splits_by_ticker()
    snaps, sp_applied = normalize_snapshots(snaps, splits)
    if sp_applied:
        _tk = sorted({a[2] for a in sp_applied})
        log.info(f"[{strategy}] 拆股/合股归一: {len(sp_applied)} 处快照腿, 涉及 {_tk}")
    if root:
        with open(os.path.join(root, f"snapshots_normalized_{strategy}.json"), "w") as f:
            json.dump({"applied": [list(a) for a in sp_applied], "snapshots": snaps}, f)

    # 终点 = max(最新快照日, inventory 的 as_of)。inventory 每次跑批都写,
    # 可能比 inventory_history 新(当日无开/平事件则不落快照)。
    inv_as_of = None
    try:
        inv_as_of = json.load(open(os.path.join(BASE_DIR, cfg["inventory"]))).get("as_of")
    except Exception:                                      # noqa: BLE001
        pass
    end_day = max([max(snaps)] + ([inv_as_of] if inv_as_of else []))
    if upto:
        end_day = min(end_day, upto)

    # 票池必须并入**当前 inventory**：当日新开仓的票可能尚未出现在任何历史快照里
    # （或快照写入与本次读取存在时序差），漏加载会导致该笔成交缺价被跳过，
    # 持仓随即与快照不符（下一次对账 R1 会红）。宁可多载几只。
    tickers = {t for d in snaps.values() for k in d if "/" in k for t in k.split("/", 1)}
    try:
        _inv = json.load(open(os.path.join(BASE_DIR, cfg["inventory"])))
        tickers |= {t for k, v in (_inv.get("pairs") or {}).items()
                    if "/" in k and isinstance(v, dict) and v.get("direction")
                    for t in k.split("/", 1)}
    except Exception:                                      # noqa: BLE001
        pass
    px = load_mongo_prices(tickers, "2026-03-01",
                           str((pd.Timestamp(end_day) + pd.Timedelta(days=3)).date()))
    divs, cov = load_dividends(tickers, cfg["live_start"], end_day, splits)
    n_ev = sum(len(v) for v in divs.values())
    if cov["missing"]:
        log.warning(f"[{strategy}] 分红缓存缺 {len(cov['missing'])}/{len(tickers)} 票 "
                    f"(前 8: {cov['missing'][:8]}) — 这些票的分红未入账")
    log.info(f"[{strategy}] 分红 {n_ev} 笔 / {len(divs)} 票（覆盖 {len(cov['covered'])}"
             f"/{len(tickers)}）")
    close_px, _cstat, closed_legs = resolve_close_fills(strategy, splits, px)
    # **旧段报告值补丁：实测无效，默认关闭**（`PAIRS_LEDGER_LEGACY_PATCH=1` 可开）
    # 两种策略都试过、都更差：
    #   · 无差别覆盖 → MRPT 全窗口 −3,478 恶化到 +13,977、有差 pair 8→22
    #   · 只补"对不上"的 lifecycle → 仍恶化到 +12,514 / 有差 22
    # 根因：报告行的 `prior_pnl` 跨生命周期，把它摊到某一次平仓上会连累其他次。
    # 而**不打补丁时 6/01–7/06 本就只剩 1 个 pair 有差**（MRPT −770、MTFS +1,432），
    # 补丁反而破坏了本来对的部分。保留实现供后续研究，生产不启用。
    log.info(f"[{strategy}] 平仓价解出 {len(close_px)} 条(日-票) | "
             + " ".join(f"{k}={v}" for k, v in sorted(_cstat.items(), key=lambda x: -x[1])))
    return cfg, snaps, px, end_day, divs, close_px


def rebuild(strategy: str, root: str, upto: str | None = None) -> dict:
    """从 live_start 全量重建（清空既有产物）。"""
    cfg, snaps, px, end_day, divs, close_px = _prepare(strategy, root, upto)
    start = cfg["live_start"]
    log.info(f"[{strategy}] Mongo 价格 {px.shape[1]} 票 × {px.shape[0]} 日")

    for fp in (account_path(strategy, root), ledger_path(strategy, root)):
        if os.path.exists(fp):
            os.remove(fp)
    hd = history_dir(strategy, root)
    os.makedirs(hd, exist_ok=True)
    for f in glob.glob(os.path.join(hd, f"account_{strategy}_*.json")):
        os.remove(f)

    trading_days = [str(d.date()) for d in px.index if start <= str(d.date()) <= end_day]
    log.info(f"[{strategy}] 交易日 {len(trading_days)} 天（其中有快照 "
             f"{len([d for d in trading_days if d in snaps])} 天,"
             f"其余为当天未跑系统=零交易,持仓顺延）")

    # 期初：若 live_start 前有快照则**按 lot 带仓建账**（见下方注释）
    pre = [d for d in sorted(snaps) if d < start]
    acct = Account.open_flat(strategy, trading_days[0], root=root)
    if pre:
        # **按 lot 播种**：live_start 前一日的每个 pair-leg 各建一个 lot，
        # 成本取其 open_price。若只按票播种，第一天所有 lot 会被当成新开仓，
        # 凭空造出往返（并把既有仓位的成本重置）。
        n = 0
        seed_cost = 0.0
        for pk, p in snaps[pre[-1]].items():
            if not p.get("direction") or "/" not in pk:
                continue
            for leg, t in zip(("s1", "s2"), pk.split("/", 1)):
                q = int(p.get(f"{leg}_shares", 0) or 0)
                opx = p.get(f"open_{leg}_price")
                if not q or not opx:
                    continue
                acct.data.setdefault("lots", {})[f"{pk}|{leg}"] = {
                    "ticker": t, "shares": q, "cost": round(float(opx), 6),
                    "open_date": str(p.get("open_date") or pre[-1])}
                pos = acct.data["positions"].setdefault(
                    t, {"shares": 0, "avg_cost": 0.0, "entry_date": pre[-1]})
                pos["shares"] += q
                seed_cost += q * float(opx)
                n += 1
        acct.data["positions"] = {t: v for t, v in acct.data["positions"].items()
                                  if v["shares"]}
        acct.data["cash"] = round(INITIAL_CASH - seed_cost, 2)
        acct._sync_avg_cost_from_lots()
        log.info(f"[{strategy}] 期初按 lot 带仓建账 @ {pre[-1]}: {n} 个 lot / "
                 f"{len(acct.data['positions'])} 票, cash={acct.data['cash']:,.2f}")
    else:
        log.info(f"[{strategy}] 期初空仓建账（{start} 前无快照）")
    st, seen, stats = _State(), set(), defaultdict(int)
    for day in trading_days:
        process_day(acct, day, px, snaps, st, seen, root, stats, divs, close_px)
    acct.save()

    log.info(f"[{strategy}] equity={acct.data['equity']:,.2f} "
             f"realized={acct.data['cumulative_realized']:,.2f} "
             f"unrealized={acct.data['unrealized']:,.2f} "
             f"持仓 {len(acct.data['positions'])} 票")
    return {"strategy": strategy, "days": len(trading_days), "stats": dict(stats),
            "equity": acct.data["equity"], "realized": acct.data["cumulative_realized"],
            "unrealized": acct.data["unrealized"], "cash": acct.data["cash"],
            "n_positions": len(acct.data["positions"]), "account": acct.data}


def daily_update(strategy: str, upto: str | None = None,
                 root: str | None = None) -> int:
    """每日增量：从 account.as_of 的次日补到最新可用日（含）。**幂等**。

    供 DailySignal 尾部调用。幂等来自三处：
      1) `upto <= acct.as_of` 直接返回 0（同日重跑不重复记账）
      2) ledger 行 dedup_key 去重
      3) account_history 日切片按日期覆盖写

    账户不存在时**不自动建账**——必须先跑 rebuild，避免半截账本悄悄上线。
    """
    root = root or BASE_DIR
    fp = account_path(strategy, root)
    if not os.path.exists(fp):
        log.warning(f"[{strategy}] 无账户文件 {fp} — 先运行 rebuild 建账,跳过")
        return 0
    acct = Account(strategy, json.load(open(fp)), root=root)
    _cfg2, snaps, px, end_day, divs, close_px = _prepare(strategy, root, upto)
    if end_day <= acct.data["as_of"]:
        log.info(f"[{strategy}] ledger 已是最新 (as_of={acct.data['as_of']}) — 无需更新")
        return 0

    st = _State(acct.data.get("price_basis_state") or "prior_close")
    seen = {r.get("dedup_key") for r in load_ledger_rows(strategy, root)}
    days = [str(d.date()) for d in px.index
            if acct.data["as_of"] < str(d.date()) <= end_day]
    n = 0
    for day in days:
        process_day(acct, day, px, snaps, st, seen, root, None, divs, close_px)
        n += 1
    acct.save()
    log.info(f"[{strategy}] ledger 更新 {n} 天 → as_of={acct.data['as_of']} "
             f"equity=${acct.data['equity']:,.2f} "
             f"realized=${acct.data['cumulative_realized']:,.2f}")
    return n


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("strategy", choices=["mrpt", "mtfs", "all"])
    ap.add_argument("--root", default=None, help="产物根目录（沙盒请用 /tmp 下路径；缺省=仓库根）")
    ap.add_argument("--upto", default=None)
    ap.add_argument("--daily", action="store_true",
                    help="增量更新而非全量重建（幂等，供 DailySignal 调用）")
    a = ap.parse_args()
    targets = list(STRATEGIES) if a.strategy == "all" else [a.strategy]
    res = {}
    for s in targets:
        if a.daily:
            res[s] = {"updated_days": daily_update(s, a.upto, a.root)}
        else:
            res[s] = rebuild(s, a.root, a.upto)
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "account"}
                      for k, v in res.items()}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
