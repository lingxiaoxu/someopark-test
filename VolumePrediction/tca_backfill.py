"""tca_backfill — 从历史成交回捞 fills,让 λ 从纸面先验转为实测(E2)。

问题(2026-08-04 审计): `service.tca.record_fill` 有定义但**无人调用**,fills 库为空
→ `calibrate_lambda_from_fills` 一直落回论文先验(k=0.1, exponent=1),
pairs 的 μ 也是冷启动值。成本模型没有本账本的实证支撑。

数据源(全部已有,只读):
  AISS/SSRS — `trade_ledger_{strat}.jsonl`(逐笔 BUY/SELL,含实际成交价)
  MRPT/MTFS — `combined_signals_*.json` 的开平仓信号(每事件两腿,含股数与价格)

两个回归输入的构造:
  participation = |成交额| / 该票当日美元成交额     (来自 VP raw 缓存)
  impact        = (成交价 − 到达价)/到达价 × 方向符号
    到达价 = **前一交易日收盘**(决策时可知的价格)。这是标准的 implementation
    shortfall / arrival-price 基准。它确实混入了当日市场漂移,但漂移与
    participation 不相关,log-log 回归提取的正是随参与率变化的那部分,
    漂移进入残差 —— 这是 TCA 的通行做法,不是近似取巧。

纪律:
  - 幂等: 每笔按 (strategy,ticker,date,side,shares) 去重,重复运行不会灌重
  - 只写 outputs/tca/fills_{strategy}.jsonl,不碰账本/信号/生产工件
  - impact ≤ 0 的笔(成交优于到达价)会被 calibrate 的 log 回归天然滤除,
    这里如实记录不做删改,以免人为制造正偏

用法:
    python -m VolumePrediction.tca_backfill --dry-run     # 只报告将写多少笔
    python -m VolumePrediction.tca_backfill               # 真写
    python -m VolumePrediction.tca_backfill --calibrate   # 写完顺带跑一次校准
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from VolumePrediction.common import REPO

log = logging.getLogger("VolumePrediction.tca_backfill")

LEDGERS = {
    "aiss": REPO / "qlib-main" / "semiconductor_strategy" / "trade_ledger_aiss.jsonl",
    "ssrs": REPO / "qlib-main" / "sector_rotation" / "trade_ledger_ssrs.jsonl",
}
SIGNALS_GLOB = str(REPO / "trading_signals" / "combined_signals_*.json")
TRADE_SIDES = {"BUY", "SELL"}          # DIV / FEE 不是成交,排除


# ── 事件收集 ─────────────────────────────────────────────────────────────────

def _from_ledgers() -> list[dict]:
    out = []
    for strat, p in LEDGERS.items():
        if not p.exists():
            log.warning(f"账本缺失: {p}")
            continue
        for line in p.read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("side") not in TRADE_SIDES:
                continue
            sh = float(r.get("shares", 0) or 0)
            px = float(r.get("price", 0) or 0)
            if sh <= 0 or px <= 0:
                continue
            out.append({"strategy": strat, "ticker": r["ticker"], "date": r["date"],
                        "shares": sh if r["side"] == "BUY" else -sh,
                        "price": px, "side": r["side"], "src": "ledger"})
    return out


def _from_pairs_signals() -> list[dict]:
    """开平仓信号 → 每事件两腿。s1_shares 已含 hedge ratio 与规模。"""
    out, seen = [], set()
    for f in sorted(glob.glob(SIGNALS_GLOB)):
        try:
            d = json.load(open(f))
        except Exception:  # noqa: BLE001
            continue
        sd = d.get("signal_date", "")
        for strat in ("mrpt", "mtfs"):
            for s in (d.get(strat, {}) or {}).get("signals", []):
                act = s.get("action")
                if act not in ("OPEN_LONG", "OPEN_SHORT", "CLOSE", "CLOSE_STOP"):
                    continue
                key = (strat, sd, s.get("pair"), act)
                if key in seen:
                    continue
                seen.add(key)
                closing = act in ("CLOSE", "CLOSE_STOP")
                for tk, sh, px in ((s.get("s1"), s.get("s1_shares"), s.get("s1_price")),
                                   (s.get("s2"), s.get("s2_shares"), s.get("s2_price"))):
                    if not tk or sh in (None, 0) or not px:
                        continue
                    sh = float(sh)
                    # 平仓 = 反向成交
                    sh = -sh if closing else sh
                    out.append({"strategy": strat, "ticker": tk, "date": sd,
                                "shares": sh, "price": float(px),
                                "side": "BUY" if sh > 0 else "SELL", "src": "signals"})
    return out


# ── participation / impact ──────────────────────────────────────────────────

def _enrich(events: list[dict], svc) -> list[dict]:
    """补 participation(当日美元量)与 impact(相对前收的实现冲击)。"""
    dates = sorted({e["date"] for e in events})
    raw_days = svc._raw_dates()
    day_cache: dict[str, pd.DataFrame] = {}

    def day(d: str) -> Optional[pd.DataFrame]:
        if d not in day_cache:
            try:
                x = svc._load_day(d)
                day_cache[d] = x.set_index("ticker") if x is not None and not x.empty else None
            except Exception:  # noqa: BLE001
                day_cache[d] = None
        return day_cache[d]

    prev_of = {}
    for d in dates:
        earlier = [x for x in raw_days if x < d]
        prev_of[d] = earlier[-1] if earlier else None

    out, n_no_vol, n_no_arr = [], 0, 0
    for e in events:
        dd = day(e["date"])
        if dd is None or e["ticker"] not in dd.index:
            n_no_vol += 1
            continue
        dv = float(dd.loc[e["ticker"], "dollar_volume"])
        if not np.isfinite(dv) or dv <= 0:
            n_no_vol += 1
            continue
        pd_ = prev_of.get(e["date"])
        arrival = None
        if pd_:
            pdd = day(pd_)
            if pdd is not None and e["ticker"] in pdd.index:
                for col in ("close", "c", "adj_close", "price"):
                    if col in pdd.columns:
                        v = float(pdd.loc[e["ticker"], col])
                        if np.isfinite(v) and v > 0:
                            arrival = v
                            break
        if arrival is None:
            n_no_arr += 1
            continue
        notional = abs(e["shares"]) * e["price"]
        sign = 1.0 if e["shares"] > 0 else -1.0
        # impact 只有在存在**独立于决策价的成交价**时才可测。
        # 2026-08-04 实证: pairs 的账面开仓价与信号价逐位相同(DGX/NKE 233.01/41.71),
        # 即 MRPT/MTFS 是按决策价记仓的纸面账本 —— 没有滑点记录,
        # 强行用次日收盘当"成交价"测到的是一整天的市场漂移而非冲击,故显式留空。
        # AISS/SSRS 走 portfolio_ledger,price 是实际成交价,可测。
        impact = ((e["price"] - arrival) / arrival * sign
                  if e["src"] == "ledger" else None)
        out.append({**e,
                    "participation": notional / dv,
                    "impact": impact,
                    "impact_measurable": impact is not None,
                    "arrival_price": arrival, "notional": notional,
                    "dollar_volume": dv})
    log.info(f"补全: {len(out)}/{len(events)} 笔 "
             f"(缺当日量 {n_no_vol}, 缺到达价 {n_no_arr})")
    return out


# ── 主流程 ───────────────────────────────────────────────────────────────────

def main(dry_run: bool = False, calibrate: bool = False) -> dict:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    from VolumePrediction.service import VolumeService
    svc = VolumeService()

    events = _from_ledgers() + _from_pairs_signals()
    log.info(f"原始成交事件: {len(events)}")
    rows = _enrich(events, svc)
    if not rows:
        log.error("无可用成交 — 不写入")
        return {"written": 0}

    # 幂等去重(含已存在的 fills)
    existing = set()
    for strat in {r["strategy"] for r in rows}:
        df = svc.tca._load_fills(strat) if hasattr(svc, "tca") else pd.DataFrame()
        if not df.empty:
            for _, r in df.iterrows():
                existing.add((r.get("strategy"), r.get("ticker"), str(r.get("date")),
                              r.get("side"), round(float(r.get("shares", 0)), 4)))
    fresh = [r for r in rows
             if (r["strategy"], r["ticker"], r["date"], r["side"],
                 round(r["shares"], 4)) not in existing]
    log.info(f"去重后待写: {len(fresh)}(已存在 {len(rows) - len(fresh)})")

    by_strat: dict[str, int] = {}
    if not dry_run:
        rec = svc.tca.record_fill if hasattr(svc, "tca") else svc.record_fill
        for r in fresh:
            kw = dict(participation=r["participation"],
                      impact_measurable=r["impact_measurable"],
                      arrival_price=r["arrival_price"], notional=r["notional"],
                      dollar_volume=r["dollar_volume"], src=r["src"])
            if r["impact"] is not None:
                kw["impact"] = r["impact"]      # 不可测则不写该列(而非写 0)
            rec(strategy=r["strategy"], ticker=r["ticker"], date=r["date"],
                shares=r["shares"], price=r["price"], side=r["side"], **kw)
            by_strat[r["strategy"]] = by_strat.get(r["strategy"], 0) + 1
        log.info(f"已写入: {by_strat}")
    else:
        for r in fresh:
            by_strat[r["strategy"]] = by_strat.get(r["strategy"], 0) + 1
        log.info(f"[DRY RUN] 将写入: {by_strat}")

    p = pd.Series([r["participation"] for r in rows])
    meas = [r["impact"] for r in rows if r["impact"] is not None]
    log.info(f"participation: 中位 {p.median():.5f} p90 {p.quantile(.9):.5f} "
             f"max {p.max():.5f}  (全部 {len(rows)} 笔)")
    if meas:
        i = pd.Series(meas)
        log.info(f"impact 可测 {len(meas)} 笔: 中位 {i.median():+.5f} "
                 f"|中位| {i.abs().median():.5f} 正比例 {(i > 0).mean():.1%}")
    log.info(f"impact 不可测 {len(rows) - len(meas)} 笔(pairs 纸面账本,"
             f"账面价=决策价,无滑点记录)")

    out = {"events": len(events), "usable": len(rows), "written": sum(by_strat.values()),
           "by_strategy": by_strat}
    if calibrate and not dry_run:
        from VolumePrediction.econ.calibration import calibrate_lambda_from_fills
        for strat in sorted(by_strat):
            df = svc.tca._load_fills(strat) if hasattr(svc, "tca") else pd.DataFrame()
            res = calibrate_lambda_from_fills(df, strategy=strat)
            log.info(f"λ({strat}): {res}")
            out.setdefault("lambda", {})[strat] = res
        allf = pd.concat([svc.tca._load_fills(s) for s in by_strat], ignore_index=True)
        res = calibrate_lambda_from_fills(allf, strategy="all")
        log.info(f"λ(all): {res}")
        out.setdefault("lambda", {})["all"] = res
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    a = ap.parse_args()
    r = main(dry_run=a.dry_run, calibrate=a.calibrate)
    print(json.dumps({k: v for k, v in r.items() if k != "lambda"},
                     ensure_ascii=False))
    sys.exit(0)
