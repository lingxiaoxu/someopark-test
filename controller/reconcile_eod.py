"""
controller/reconcile_eod.py — 与三个 performance json 的日终对账(plan §五;M5)。

真源划分:EOD = 三 json(只读,拼接逻辑不碰);盘中 = controller。
锚定映射(plan 定案,2026-08-12 实测):
    mrpt/mtfs → strategy_performance.json          (原始源)
    ssrs/aiss → master_portfolio_performance.json  (sr_equity/aiss_equity,live 段唯一落盘)
    bdc       → private_credit_bdc_performance.json
口径事实:pairs 两套 equity 并存(controller=账本口径,官方=regime×sim/500k)→
    绝对值不互比;对账两条腿:
    ① ratio 漂移:r(d)=controller/official,r 的日变化 = 两口径日收益差(应≈0,
       容差初设 20bp,legit 差异=股息/费用/DRIP/拼接段,逐项归因);
    ② 首日建 baseline(无前日 controller 值时只记录 r,不判定)。
产物:controller/output/reconcile_{date}.json。绝不回写三 json。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from controller.model import REPO
from controller.registry import Registry, strategy_canonical_key

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(_HERE, "output")
TOL_BP = 20.0                                  # 日收益差容差(基点,实测后校准)

_ANCHORS = {   # strategy -> (json relpath, equity column)
    "mrpt": ("someo-park-investment-management/public/data/strategy_performance.json",
             "mrpt_equity"),
    "mtfs": ("someo-park-investment-management/public/data/strategy_performance.json",
             "mtfs_equity"),
    "ssrs": ("someo-park-investment-management/public/data/master_portfolio_performance.json",
             "sr_equity"),
    "aiss": ("someo-park-investment-management/public/data/master_portfolio_performance.json",
             "aiss_equity"),
    "bdc":  ("someo-park-investment-management/public/data/private_credit_bdc_performance.json",
             "bdc_equity"),
}


def official_eod() -> dict:
    """各策略官方最后 EOD 行 {strategy: {date, value}}(原始源,见锚定映射)。"""
    out = {}
    cache: dict[str, list] = {}
    for st, (rel, col) in _ANCHORS.items():
        rows = cache.setdefault(rel, json.load(open(os.path.join(REPO, rel))))
        last = next(r for r in reversed(rows) if r.get(col) is not None)
        out[st] = {"date": last["date"], "value": float(last[col]),
                   "source": os.path.basename(rel), "column": col}
    return out


def controller_strategies() -> dict:
    """controller nav_latest 的策略层值 {strategy: {ts, value, positions_as_of}}。"""
    nav = json.load(open(os.path.join(OUT_DIR, "nav_latest.json")))
    reg = Registry()
    spid_to_st = {}
    for st in _ANCHORS:
        spid_to_st[reg.spid_of("strategy", strategy_canonical_key(st),
                               register_if_new=False)] = st
    out = {}
    for row in nav["nodes"]:
        st = spid_to_st.get(row["node_id"])
        if st:
            out[st] = {"ts": nav["ts"], "value": float(row["value"]),
                       "positions_as_of": row.get("positions_as_of"),
                       "corp_action": bool(row.get("corp_action"))}
    missing = set(_ANCHORS) - set(out)
    if missing:
        raise RuntimeError(f"nav_latest lacks strategies {missing}")
    return out


def _prev_reconcile() -> dict | None:
    files = sorted(f for f in os.listdir(OUT_DIR)
                   if f.startswith("reconcile_") and f.endswith(".json"))
    return json.load(open(os.path.join(OUT_DIR, files[-1]))) if files else None


def reconcile(date: str | None = None) -> dict:
    date = date or datetime.now().strftime("%Y-%m-%d")
    off = official_eod()
    ctl = controller_strategies()
    prev = _prev_reconcile()
    report = {"date": date, "generated_at": datetime.now().isoformat(timespec="seconds"),
              "tolerance_bp": TOL_BP, "strategies": {}, "verdict": "baseline"}
    any_breach, comparable = False, False
    for st in _ANCHORS:
        ratio = ctl[st]["value"] / off[st]["value"] if off[st]["value"] else None
        row = {"official": off[st], "controller": ctl[st],
               "ratio": round(ratio, 6) if ratio else None}
        if prev and st in prev.get("strategies", {}) and \
                prev["strategies"][st].get("ratio"):
            r0 = prev["strategies"][st]["ratio"]
            drift_bp = (ratio / r0 - 1) * 1e4
            row["ratio_drift_bp"] = round(drift_bp, 2)
            row["within_tolerance"] = abs(drift_bp) <= TOL_BP
            row["attribution_hints"] = [h for h, cond in [
                ("corp_action: split effective today — price is post-split, "
                 "position file adjusts at its own pipeline (plan §九-7)",
                 ctl[st].get("corp_action", False)),
                ("dividend/DRIP day (bdc/ssrs/aiss cumulative_dividends)", abs(drift_bp) > 2),
                ("fees", abs(drift_bp) > 2),
                ("regime-weight rescale (pairs official only)", st in ("mrpt", "mtfs")),
            ] if cond] if abs(drift_bp) > TOL_BP else []
            comparable = True
            if abs(drift_bp) > TOL_BP:
                any_breach = True
        report["strategies"][st] = row
    if comparable:
        report["verdict"] = "breach" if any_breach else "ok"
    path = os.path.join(OUT_DIR, f"reconcile_{date}.json")
    tmp = path + ".tmp"
    json.dump(report, open(tmp, "w"), indent=1)
    os.replace(tmp, path)
    print(f"[reconcile] {date} verdict={report['verdict']} -> {os.path.basename(path)}")
    for st, row in report["strategies"].items():
        drift = row.get("ratio_drift_bp")
        print(f"  {st:5s} official={row['official']['value']:>13,.2f} "
              f"controller={row['controller']['value']:>13,.2f} "
              f"ratio={row['ratio']}"
              + (f" drift={drift:+.1f}bp" if drift is not None else "  (baseline)"))
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="EOD reconcile vs three perf jsons (M5)")
    ap.add_argument("--date", default=None)
    ap.parse_args() and None
    reconcile()
