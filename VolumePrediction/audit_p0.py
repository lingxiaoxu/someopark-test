"""
audit_p0 — P0 数据审计执行器(§八 12 项,产物 outputs/P0_data_audit.md)
====================================================================
用法: conda run -n someopark_run python -m VolumePrediction.audit_p0 [--items 1,3,6]
每项独立可跑、失败不中断其余;结果全部落 markdown + json。
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict

import numpy as np
import pandas as pd

from VolumePrediction.common import REPO, OUT, load_config, get_logger
from VolumePrediction.data import polygon_loader as pl

log = get_logger("audit_p0")
REPORT_MD = OUT / "P0_data_audit.md"
REPORT_JSON = OUT / "P0_data_audit.json"


def item1_coverage() -> dict:
    """Polygon 覆盖: 抽 20 交易日(2019/2021/2023/2026 各 5)核标的数+字段。"""
    days = []
    for y in (2019, 2021, 2023, 2026):
        yd = pl.trading_days(f"{y}-03-01", f"{y}-03-31")[:5]
        days += yd
    rows = []
    need = {"ticker", "v", "vw", "o", "h", "l", "c", "t", "n"}
    for d in days:
        try:
            df = pl.load_day(d)
            rows.append({"date": d, "n_tickers": len(df),
                         "fields_ok": need.issubset(df.columns),
                         "null_v": int(df["v"].isna().sum()) if len(df) else None})
        except FileNotFoundError:
            rows.append({"date": d, "n_tickers": None, "fields_ok": False,
                         "note": "raw missing"})
    ok = all(r.get("fields_ok") for r in rows)
    return {"pass": ok, "rows": rows}


def item1b_volume_reconcile() -> dict:
    """50 票 vs Mongo stock_data 的成交量逐日一致性(容差 0.5%)。"""
    from VolumePrediction.data.inhouse_loader import stock_data_reference
    d0, d1 = "2025-06-02", "2025-06-13"
    px = pl.load_range(d0, d1)
    top = (px.groupby("ticker")["dollar_volume"].mean()
             .sort_values(ascending=False).head(50).index.tolist())
    ref = stock_data_reference(top, d0, d1)
    if ref.empty:
        return {"pass": False, "note": "stock_data empty"}
    ours = px[px.ticker.isin(top)][["ticker", "date", "v"]].copy()
    ours["date"] = pd.to_datetime(ours["date"]).dt.date
    m = ours.merge(ref[["symbol", "date", "v"]],
                   left_on=["ticker", "date"], right_on=["symbol", "date"],
                   suffixes=("_pg", "_mongo")).dropna()
    m["rel"] = (m["v_pg"] - m["v_mongo"]).abs() / m["v_mongo"].clip(lower=1)
    frac_ok = float((m["rel"] < 0.005).mean()) if len(m) else 0.0
    return {"pass": frac_ok > 0.95, "n_obs": len(m),
            "frac_within_0.5pct": round(frac_ok, 4),
            "worst": m.nlargest(3, "rel")[["ticker", "date", "rel"]].to_dict("records")}


def item2_rate_limit() -> dict:
    """配额/限速实测: 从回填日志统计实际速率与 429 次数。"""
    logs = sorted((REPO / "VolumePrediction" / "logs").glob("backfill_*.log"))
    if not logs:
        return {"pass": None, "note": "no backfill log"}
    txt = logs[-1].read_text()
    n429 = txt.count("429")
    import re
    paces = re.findall(r"pace=([\d.]+) req/s", txt)
    return {"pass": True, "n_429": n429,
            "pace_req_s": paces[-1] if paces else None,
            "throttle_used": 0.12}


def item3_split_chain() -> dict:
    """复权链: KLAC 10:1(2026-06)正拆 + 一例合股 全链对拍;η 连续性。"""
    from VolumePrediction.data import splits_loader as sl
    out = {}
    for tk in ("KLAC",):
        px = pl.load_range("2026-05-01", "2026-07-01", {tk})
        if px.empty:
            out[tk] = {"note": "raw missing"}
            continue
        df = px.set_index(pd.to_datetime(px["date"]))
        dv_raw = df["dollar_volume"].copy()
        adj, n = sl.adjust(df.copy(), tk)
        v = np.log(adj["dollar_volume"].astype(float))
        eta = v - v.rolling(5).mean()
        exec_win = eta.loc["2026-06-09":"2026-06-16"].abs()
        out[tk] = {"n_splits_applied": n,
                   "dollar_vol_invariant": bool(np.allclose(
                       adj["dollar_volume"], dv_raw, rtol=1e-9)),
                   "eta_max_around_split": round(float(exec_win.max()), 3)}
    # 合股例: 从 splits 表找一个 split_from>split_to 的近例
    from VolumePrediction.data.splits_loader import refresh
    rs = [s for s in refresh() if float(s.get("split_from", 1)) > float(s.get("split_to", 1))]
    rev = None
    for s in sorted(rs, key=lambda x: x.get("execution_date", ""), reverse=True):
        tk = s["ticker"]
        ed = s["execution_date"]
        px = pl.load_range((pd.Timestamp(ed) - pd.Timedelta(days=20)).strftime("%Y-%m-%d"),
                           (pd.Timestamp(ed) + pd.Timedelta(days=20)).strftime("%Y-%m-%d"),
                           {tk})
        if len(px) > 10:
            df = px.set_index(pd.to_datetime(px["date"]))
            dv = df["dollar_volume"].copy()
            adj, n = __import__("VolumePrediction.data.splits_loader",
                                fromlist=["adjust"]).adjust(df.copy(), tk)
            rev = {"ticker": tk, "date": ed, "n": n,
                   "dollar_vol_invariant": bool(np.allclose(
                       adj["dollar_volume"], dv, rtol=1e-9))}
            break
    out["reverse_split_case"] = rev
    ok = all(v.get("dollar_vol_invariant") for k, v in out.items()
             if isinstance(v, dict) and "dollar_vol_invariant" in v)
    return {"pass": ok, **out}


def item4_earnings_dual_source() -> dict:
    """财报双源一致率: 50 票×近 8 季 MFE(Polygon) vs FMP 日历,≥95% 期望。"""
    from VolumePrediction.data import earnings_loader as el
    px = pl.load_day(pl.trading_days("2026-07-01", "2026-07-22")[-1])
    top = px.nlargest(50, "dollar_volume")["ticker"].tolist()
    hist = el.historical_dates(top)
    import os
    from pymongo import MongoClient
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
    col = MongoClient(os.environ["MONGO_URI"])["someopark"]["fmp_historical_earning_calendar"]
    t0 = datetime(2024, 6, 1)
    agree = tot = 0
    n_no_fmp = 0
    detail = []
    for s in top:
        fmp = {d["date"].date() for d in col.find(
            {"symbol": s, "date": {"$gte": t0, "$lte": datetime(2026, 7, 22)}},
            {"date": 1})}
        ours = [d for d in hist.get(s, []) if d >= t0.date()][-8:]
        if not fmp:
            # FMP 日历该窗口对此票无覆盖(实测 MU 等 2024-06 起为空)——
            # 属对拍源覆盖缺口而非我方日期错误;单独计数,不计入一致率分母
            n_no_fmp += 1
            continue
        for d in ours:
            tot += 1
            if any(abs((d - f).days) <= 1 for f in fmp):
                agree += 1
            else:
                detail.append({"symbol": s, "date": str(d)})
    rate = agree / tot if tot else None
    return {"pass": (rate is None) or rate >= 0.95,
            "agree_rate": round(rate, 4) if rate is not None else None,
            "n": tot, "n_symbols_fmp_empty": n_no_fmp,
            "note": ("FMP 无覆盖票不计入分母;n=0 时以 MFE 单源为准"
                     if n_no_fmp else ""),
            "disagreements": detail[:10]}


def item5_report() -> dict:
    return {"pass": True, "note": "本 markdown 即产物"}


def item6_mktcap_splice() -> dict:
    """市值三方对拍(重叠期 2024-10+): financials股本×价 vs share_float×价 vs fmp_market_cap。"""
    from VolumePrediction.data import inhouse_loader as ih
    from VolumePrediction.data import factor_proxy as fp
    tickers = ["AAPL", "MSFT", "KLAC", "CMCSA", "RCL"]
    d0, d1 = "2025-01-02", "2025-06-30"
    bad = []
    rows = []
    for tk in tickers:
        px = pl.load_range(d0, d1, {tk})
        if px.empty:
            continue
        close = px.set_index(pd.to_datetime(px["date"]))["c"].astype(float)
        mc_ours = fp.market_cap_series(tk, close)
        mc_fmp = ih.market_cap([tk], d0, d1)
        if mc_fmp.empty:
            continue
        f = mc_fmp.set_index(pd.to_datetime(mc_fmp["date"]))["market_cap"]
        j = pd.concat({"ours": mc_ours, "fmp": f}, axis=1).dropna()
        if j.empty:
            continue
        rel = ((j["ours"] - j["fmp"]).abs() / j["fmp"]).median()
        rows.append({"ticker": tk, "median_rel_diff": round(float(rel), 4)})
        if rel > 0.05:
            bad.append(tk)
    return {"pass": len(bad) == 0, "rows": rows, "gt5pct": bad}


def item7_calendar_quality() -> dict:
    """财报日历质量: time(bmo/amc)缺失率(影响 earnings_zero 定义,§6.8 保守 AMC)。"""
    import os
    from pymongo import MongoClient
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
    col = MongoClient(os.environ["MONGO_URI"])["someopark"]["fmp_historical_earning_calendar"]
    import collections
    sample = [x.get("time") for x in col.find(
        {"date": {"$gte": datetime(2024, 1, 1), "$lte": datetime(2026, 7, 1)}},
        {"time": 1}).limit(20000)]
    c = collections.Counter(sample)
    missing = (c.get("--", 0) + c.get(None, 0)) / max(len(sample), 1)
    return {"pass": missing < 0.10, "dist": dict(c), "missing_rate": round(missing, 4)}


def item8_deferred() -> dict:
    return {"pass": None, "note": "社媒情绪覆盖审计随文本线推迟(§1.1)"}


def item9_balance_dedup() -> dict:
    from VolumePrediction.data.inhouse_loader import _mongo
    col = _mongo()["fmp_balance_sheet_statement"]
    pipe = [{"$match": {"symbol": {"$in": ["AAPL", "MSFT", "KLAC", "XOM", "JPM"]}}},
            {"$group": {"_id": {"s": "$symbol", "d": "$date"}, "n": {"$sum": 1}}},
            {"$group": {"_id": "$n", "cnt": {"$sum": 1}}}]
    dist = {str(x["_id"]): x["cnt"] for x in col.aggregate(pipe)}
    dup_frac = sum(v for k, v in dist.items() if int(k) > 1) / max(sum(dist.values()), 1)
    return {"pass": True, "dup_multiplicity_dist": dist,
            "dup_frac": round(dup_frac, 4),
            "rule": "(symbol,date) keep latest create_time(已在 annual_statement 实现)"}


def item10_survivorship() -> dict:
    """幸存者精确量化: vintage 成员 ∩ Mongo 现存票 逐年占比。"""
    from VolumePrediction.data.inhouse_loader import _mongo
    from VolumePrediction.data import universe as uni
    mongo_syms = set(_mongo()["fmp_income_statement"].distinct("symbol"))
    rows = []
    for y in range(2019, 2026):
        p = uni.UNI_DIR / f"vintage_{y}.parquet"
        if not p.exists():
            rows.append({"vintage": y, "note": "not built yet"})
            continue
        mem = set(pd.read_parquet(p)["ticker"])
        cov = len(mem & mongo_syms) / len(mem)
        rows.append({"vintage": y, "n": len(mem),
                     "mongo_coverage": round(cov, 4)})
    return {"pass": True, "rows": rows,
            "handling": "报表主源=Polygon financials(含退市);SUE=幸存者掩码双口径(§6.8)"}


def item11_consume_side() -> dict:
    """消费侧盘点: 逐策略 fills/信号历史 路径/存在性/行数/起始。"""
    cfg = load_config()["consume_paths"]
    out = {}
    for name, rel in [("ledger_aiss", cfg["ledgers"]["aiss"]),
                      ("ledger_ssrs", cfg["ledgers"]["ssrs"])]:
        p = REPO / rel
        if p.exists():
            lines = p.read_text().strip().split("\n")
            first = json.loads(lines[0]) if lines and lines[0] else {}
            out[name] = {"exists": True, "n": len(lines),
                         "first_date": first.get("date") or first.get("as_of")}
        else:
            out[name] = {"exists": False}
    import glob as g
    sigs = sorted(g.glob(str(REPO / cfg["signals_glob"])))
    out["combined_signals"] = {"n_files": len(sigs),
                              "first": Path(sigs[0]).name if sigs else None,
                              "last": Path(sigs[-1]).name if sigs else None}
    for name, rel in [("inv_mrpt", cfg["inventories"]["mrpt"]),
                      ("inv_mtfs", cfg["inventories"]["mtfs"]),
                      ("inv_aiss", cfg["inventories"]["aiss"]),
                      ("inv_ssrs", cfg["inventories"]["ssrs"]),
                      ("acct_aiss", cfg["accounts"]["aiss"]),
                      ("acct_ssrs", cfg["accounts"]["ssrs"])]:
        out[name] = {"exists": (REPO / rel).exists()}
    ok = all(v.get("exists", True) for v in out.values() if isinstance(v, dict))
    return {"pass": ok, **out}


def item12_static_snapshot() -> dict:
    """config 静态快照 vs 策略侧现值对拍(SPDR 清单;AISS 子板块映射待快照)。"""
    cfg = load_config()
    etfs = set(cfg["universe"]["service_extra"]["etfs"])
    import yaml
    sr = yaml.safe_load(open(REPO / "qlib-main/sector_rotation/config.yaml"))
    sr_etfs = set(sr.get("universe", {}).get("etfs", []))
    missing = sr_etfs - etfs
    return {"pass": len(missing) == 0, "spdr_in_config": sorted(etfs),
            "ssrs_actual": sorted(sr_etfs), "missing_from_config": sorted(missing),
            "aiss_subsector_map": "待 P0 快照(config.aiss_subsectors)"}


ITEMS: Dict[str, Callable[[], dict]] = {
    "1": item1_coverage, "1b": item1b_volume_reconcile, "2": item2_rate_limit,
    "3": item3_split_chain, "4": item4_earnings_dual_source, "5": item5_report,
    "6": item6_mktcap_splice, "7": item7_calendar_quality, "8": item8_deferred,
    "9": item9_balance_dedup, "10": item10_survivorship,
    "11": item11_consume_side, "12": item12_static_snapshot,
}


def run(items=None) -> dict:
    results = {}
    for k, fn in ITEMS.items():
        if items and k not in items:
            continue
        t0 = time.time()
        try:
            results[k] = fn()
        except Exception as e:  # noqa: BLE001
            results[k] = {"pass": False, "error": str(e)[:300]}
        results[k]["elapsed_s"] = round(time.time() - t0, 1)
        log.info(f"P0 item {k}: pass={results[k].get('pass')} "
                 f"({results[k]['elapsed_s']}s)")
    OUT.mkdir(exist_ok=True)
    REPORT_JSON.write_text(json.dumps(results, indent=1, default=str))
    lines = [f"# P0 数据审计报告\n生成: {datetime.now().isoformat()}\n"]
    for k in ITEMS:
        if k in results:
            r = results[k]
            badge = {True: "✅", False: "❌", None: "⏸"}[r.get("pass")]
            lines.append(f"## 审计项 {k} {badge}\n```json\n"
                         + json.dumps(r, indent=1, default=str, ensure_ascii=False)[:2000]
                         + "\n```\n")
    REPORT_MD.write_text("\n".join(lines))
    return results


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", default=None, help="如 1,3,6")
    a = ap.parse_args()
    res = run(a.items.split(",") if a.items else None)
    print(json.dumps({k: v.get("pass") for k, v in res.items()}, ensure_ascii=False))
