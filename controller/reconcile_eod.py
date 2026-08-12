"""
controller/reconcile_eod.py — 与三个 performance json 的日终对账(plan §五;M5)。

真源划分:EOD = 三 json(只读,拼接逻辑不碰);盘中 = controller。
锚定映射(plan 定案,2026-08-12 实测):
    mrpt/mtfs → strategy_performance.json          (原始源)
    ssrs/aiss → master_portfolio_performance.json  (sr_equity/aiss_equity,live 段唯一落盘)
    bdc       → private_credit_bdc_performance.json

对账方法(2026-08-12 用户令修订:不依赖"ratio 恒定"假设):
    两口径绝对值不可互比(账本 $1M 起账 vs 官方 regime×sim / 引擎复利),且
    ratio 会因合法原因移动(regime capital 日变、分红、费用)——ratio 只做
    信息记录,不做判定。判定用**同日期对齐的日收益差**:
        r_off  = off(D)/off(D_prev) − 1          官方最后两行
        r_ctl  = ctl_close(D)/ctl_close(D_prev) − 1
                 (controller 16:00 ET 收盘值,取自 nav_stream_{D}.csv)
        diff_bp = (r_ctl − r_off) × 1e4
    同持仓 → 同日收益应接近;超容差(初设 20bp,影子周实测校准)→ 逐项归因
    (股息/DRIP、费用、pairs regime 资本重标定、corp_action split 日)。
    controller 侧还没有对应两日收盘数据时 → baseline(只记录,不判定)。
产物:controller/output/reconcile_{date}.json。绝不回写三 json。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from controller.model import REPO
from controller.registry import Registry, strategy_canonical_key

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(_HERE, "output")
TOL_BP = 20.0                                  # 日收益差容差(基点,实测后校准)
_ET = ZoneInfo("America/New_York")

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


def official_last_two() -> dict:
    """各策略官方最后两行非空 EOD {st: [(date, value), (date, value)]}(可能只有一行)。"""
    out = {}
    cache: dict[str, list] = {}
    for st, (rel, col) in _ANCHORS.items():
        rows = cache.setdefault(rel, json.load(open(os.path.join(REPO, rel))))
        nn = [(r["date"], float(r[col])) for r in rows if r.get(col) is not None]
        out[st] = nn[-2:]
    return out


def ctl_close(date_iso: str) -> dict | None:
    """controller 在 date(ET)16:00 前最后一笔的各节点值 {node_id: value}。
    平移续写(闭市 carry)下,16:00 截断即为当日收盘;文件缺失 → None。"""
    p = os.path.join(OUT_DIR, f"nav_stream_{date_iso.replace('-', '')}.csv")
    if not os.path.exists(p):
        return None
    y, m, d = (int(x) for x in date_iso.split("-"))
    cutoff = datetime(y, m, d, 16, 0, tzinfo=_ET)
    out: dict[str, float] = {}
    with open(p) as fh:
        header = fh.readline().strip().split(",")
        i_ts, i_node, i_val = (header.index(k) for k in ("ts", "node_id", "value"))
        for line in fh:
            parts = line.rstrip("\n").split(",")
            try:
                ts = datetime.fromisoformat(parts[i_ts])
            except ValueError:
                continue
            if ts.astimezone(_ET) > cutoff:
                continue                      # 16:00 后(盘后/夜间 carry)不算收盘
            out[parts[i_node]] = float(parts[i_val])
    return out or None


def controller_now() -> dict:
    """nav_latest 的策略层现值(信息记录用){st: {ts, value, positions_as_of, corp_action}}。"""
    nav = json.load(open(os.path.join(OUT_DIR, "nav_latest.json")))
    reg = Registry()
    spid_to_st = {reg.spid_of("strategy", strategy_canonical_key(st),
                              register_if_new=False): st for st in _ANCHORS}
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
    return out, spid_to_st


def reconcile(date: str | None = None) -> dict:
    date = date or datetime.now(_ET).strftime("%Y-%m-%d")
    off2 = official_last_two()
    now, spid_to_st = controller_now()
    st_to_spid = {v: k for k, v in spid_to_st.items()}
    closes: dict[str, dict | None] = {}       # date_iso -> {node_id: value}|None

    report = {"date": date, "generated_at": datetime.now(_ET).isoformat(timespec="seconds"),
              "tolerance_bp": TOL_BP, "method": "same-date daily-return diff "
              "(no ratio-constancy assumption; ratio logged as info only)",
              "strategies": {}, "verdict": "baseline"}
    any_breach, comparable = False, False
    for st in _ANCHORS:
        pair = off2[st]
        row: dict = {"controller_now": now[st]}
        if pair:
            d_last, v_last = pair[-1]
            row["official"] = {"date": d_last, "value": v_last,
                               "source": os.path.basename(_ANCHORS[st][0]),
                               "column": _ANCHORS[st][1]}
            # ratio 仅信息记录(绝对值刻度对照),不参与判定
            row["ratio_info"] = round(now[st]["value"] / v_last, 6) if v_last else None
        if len(pair) == 2:
            (d0, v0), (d1, v1) = pair
            for d in (d0, d1):
                if d not in closes:
                    closes[d] = ctl_close(d)
            spid = st_to_spid[st]
            c0 = (closes[d0] or {}).get(spid)
            c1 = (closes[d1] or {}).get(spid)
            if c0 and c1 and v0:
                r_off = v1 / v0 - 1
                r_ctl = c1 / c0 - 1
                diff_bp = (r_ctl - r_off) * 1e4
                row.update({
                    "window": [d0, d1],
                    "r_official_bp": round(r_off * 1e4, 2),
                    "r_controller_bp": round(r_ctl * 1e4, 2),
                    "diff_bp": round(diff_bp, 2),
                    "within_tolerance": abs(diff_bp) <= TOL_BP,
                })
                row["attribution_hints"] = [h for h, cond in [
                    ("corp_action: split effective — price post-split, position "
                     "file adjusts at its own pipeline (plan §九-7)",
                     now[st].get("corp_action", False)),
                    ("dividend/DRIP (bdc/ssrs/aiss cumulative_dividends; pairs DIV rows)", True),
                    ("fees", True),
                    ("regime-capital rescale (pairs official = regime×sim/500k; "
                     "legit scale move, not PnL)", st in ("mrpt", "mtfs")),
                ] if cond] if abs(diff_bp) > TOL_BP else []
                comparable = True
                if abs(diff_bp) > TOL_BP:
                    any_breach = True
            else:
                row["note"] = ("controller close missing for "
                               f"{[d for d in (d0, d1) if not (closes[d] or {}).get(spid)]}"
                               " — baseline (needs nav_stream for both dates)")
        report["strategies"][st] = row
    if comparable:
        report["verdict"] = "breach" if any_breach else "ok"
    path = os.path.join(OUT_DIR, f"reconcile_{date}.json")
    tmp = path + ".tmp"
    json.dump(report, open(tmp, "w"), indent=1)
    os.replace(tmp, path)
    print(f"[reconcile] {date} verdict={report['verdict']} -> {os.path.basename(path)}")
    for st, row in report["strategies"].items():
        if "diff_bp" in row:
            print(f"  {st:5s} window={row['window'][0]}→{row['window'][1]} "
                  f"r_off={row['r_official_bp']:+.1f}bp "
                  f"r_ctl={row['r_controller_bp']:+.1f}bp "
                  f"diff={row['diff_bp']:+.1f}bp "
                  f"{'OK' if row['within_tolerance'] else 'BREACH'}")
        else:
            print(f"  {st:5s} baseline ({row.get('note', 'insufficient official rows')})")
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="EOD reconcile vs three perf jsons (M5)")
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    reconcile(a.date)
