"""
controller/reconcile_eod.py — 日终对账(plan §五;M5)。

方法 v3(2026-08-12 用户令:**全面去除对 ratio 的依赖**——ratio 变化很多,
只能计算出来展示,绝不能进任何判定;收益率比较也含隐性等比例假设,一并废除。
**判定只用持仓数额 shares × 价格 × 持仓详情**):

  判定(唯一 verdict 来源)= 持仓级独立重算:
      controller 16:00 ET 收盘值(nav_stream 截断末笔,带 structure_hash)
        vs
      Σ( 结构快照 golden shares × Polygon 官方日收盘 daily_close ) + cash_flat
      —— shares 来自当时生效的 structure_snapshot_{hash}.json(由 golden
      持仓文件装配),价格走与盘中 snapshot 独立的日 bar 路径。
      差异超容差(初设 20bp:收盘竞价 vs 最后成交 + 分钟时差)→ breach,
      逐票列出缺价/差额,绝不静默。PORTFOLIO 合计同查。

  官方三 json 对照 = 纯信息展示(official 值/日期、账本值、ratio_display、
  两口径日收益差)——**不参与任何判定**;两本账刻度不同属口径事实,
  差异注明 informational。

产物:controller/output/reconcile_{date}.json。绝不回写三 json。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from controller.model import REPO
from controller.registry import Registry, strategy_canonical_key
from controller.prices import PriceFeed

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(_HERE, "output")
TOL_BP = 20.0                # 持仓级重算容差(收盘竞价 vs lastTrade + 分钟时差)
_ET = ZoneInfo("America/New_York")

_ANCHORS = {   # strategy -> (json relpath, equity column) —— 仅信息展示用
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


# ── controller 侧:16:00 ET 截断收盘 + 当时结构 hash ─────────────────────────
def _stream_segments(date_iso: str) -> list[str]:
    """该日 nav_stream 的全部分段:schema 轮转段 .v1,.v2…(旧→新)+ 主文件。"""
    d8 = date_iso.replace("-", "")
    main = os.path.join(OUT_DIR, f"nav_stream_{d8}.csv")
    import re
    parts = []
    for f in os.listdir(OUT_DIR) if os.path.isdir(OUT_DIR) else []:
        m = re.fullmatch(rf"nav_stream_{d8}\.csv\.v(\d+)", f)
        if m:
            parts.append((int(m.group(1)), os.path.join(OUT_DIR, f)))
    out = [p for _, p in sorted(parts)]
    if os.path.exists(main):
        out.append(main)
    return out


def stream_close(date_iso: str) -> dict:
    """{node_id: {"value": v, "hash": h}} 该日 nav_stream ≤16:00 ET 的末笔
    (合并 schema 轮转分段,各段用自己的表头)。"""
    y, m, d = (int(x) for x in date_iso.split("-"))
    cutoff = datetime(y, m, d, 16, 0, tzinfo=_ET)
    out: dict[str, dict] = {}
    for p in _stream_segments(date_iso):
        with open(p) as fh:
            header = fh.readline().strip().split(",")
            try:
                i_ts, i_node, i_val = (header.index(k)
                                       for k in ("ts", "node_id", "value"))
            except ValueError:
                continue
            i_hash = (header.index("structure_hash")
                      if "structure_hash" in header else None)
            for line in fh:
                parts = line.rstrip("\n").split(",")
                try:
                    ts = datetime.fromisoformat(parts[i_ts])
                except ValueError:
                    continue
                if ts.astimezone(_ET) > cutoff:
                    continue              # 16:00 后(盘后/夜间平移)不算收盘
                out[parts[i_node]] = {"value": float(parts[i_val]),
                                      "hash": (parts[i_hash]
                                               if i_hash is not None else None)}
    return out


# ── 结构快照 → 拍平 shares / cash(golden 持仓文件的当时定格)────────────────
def flatten_snapshot(snap: dict):
    """→ (exp(nid)->{leaf: eff_shares}, cash_flat(nid)->float)。"""
    nodes = snap["nodes"]
    memo_e: dict[str, dict] = {}
    memo_c: dict[str, float] = {}

    def exp(nid: str) -> dict:
        if nid in memo_e:
            return memo_e[nid]
        out: dict[str, float] = {}
        for c, q in nodes[nid]["children"]:
            if c in nodes:
                for leaf, eff in exp(c).items():
                    out[leaf] = out.get(leaf, 0.0) + q * eff
            else:
                out[c] = out.get(c, 0.0) + q
        memo_e[nid] = {k: v for k, v in out.items() if v != 0.0}
        return memo_e[nid]

    def cash_flat(nid: str) -> float:
        if nid in memo_c:
            return memo_c[nid]
        tot = float(nodes[nid]["attrs"].get("cash_const") or 0.0)
        for c, q in nodes[nid]["children"]:
            if c in nodes:
                tot += q * cash_flat(c)
        memo_c[nid] = tot
        return tot

    return exp, cash_flat


def official_info() -> dict:
    """官方三 json 最后两行(**纯信息展示**,不参与判定)。"""
    out = {}
    cache: dict[str, list] = {}
    for st, (rel, col) in _ANCHORS.items():
        rows = cache.setdefault(rel, json.load(open(os.path.join(REPO, rel))))
        nn = [(r["date"], float(r[col])) for r in rows if r.get(col) is not None]
        out[st] = {"rows": nn[-2:], "source": os.path.basename(rel), "column": col}
    return out


def _candidate_dates() -> list[str]:
    """有 nav_stream 的日期,新→旧。"""
    ds = sorted({f[11:19] for f in os.listdir(OUT_DIR)
                 if f.startswith("nav_stream_") and f.endswith(".csv")}, reverse=True)
    return [f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in ds]


def reconcile(date: str | None = None) -> dict:
    reg = Registry()
    feed = PriceFeed(reg)
    spid_to_st = {reg.spid_of("strategy", strategy_canonical_key(st),
                              register_if_new=False): st for st in _ANCHORS}
    st_to_spid = {v: k for k, v in spid_to_st.items()}

    # 目标日:指定日,否则最近一个"有 ≤16:00 收盘行"的 nav_stream 日期
    dates = [date] if date else _candidate_dates()[:5]
    closes, target = {}, None
    for d in dates:
        closes = stream_close(d)
        if closes:
            target = d
            break
    report = {"date": target or (date or datetime.now(_ET).strftime("%Y-%m-%d")),
              "generated_at": datetime.now(_ET).isoformat(timespec="seconds"),
              "method": "position-level recompute: snapshot golden shares × "
                        "polygon daily_close + cash_flat vs controller 16:00 close "
                        "(NO ratio / NO return-proportionality in verdict; "
                        "official jsons informational only)",
              "tolerance_bp": TOL_BP, "strategies": {}, "verdict": "baseline"}
    if not target:
        report["note"] = "no nav_stream close rows yet"
        _emit(report)
        return report

    # 当时结构快照(shares/cash 的 golden 定格)
    hashes = {v["hash"] for v in closes.values() if v.get("hash")}
    snap = None
    if len(hashes) == 1:
        h = hashes.pop()
        sp = os.path.join(OUT_DIR, f"structure_snapshot_{h}.json")
        if os.path.exists(sp):
            snap = json.load(open(sp))
        else:
            report["note"] = f"snapshot {h} missing"
    elif hashes:
        report["note"] = f"mixed structure hashes at close {sorted(hashes)} — 用各行自身值仍成立,重算跳过"
    else:
        report["note"] = "stream rows lack structure_hash (pre-upgrade rows) — 重算跳过"

    off = official_info()
    any_breach, comparable, any_skipped = False, False, False
    exp = cash_flat = None
    px: dict[str, float] = {}
    if snap:
        exp, cash_flat = flatten_snapshot(snap)
        leaves = sorted({leaf for st in _ANCHORS
                         for leaf in exp(st_to_spid[st])})
        px = feed.daily_close(leaves, target)

    for st in _ANCHORS:
        spid = st_to_spid[st]
        row: dict = {}
        ctl = closes.get(spid)
        if ctl:
            row["controller_close"] = ctl["value"]
        # ① 判定:持仓级独立重算(唯一 verdict 来源)
        if snap and ctl and spid in snap["nodes"]:
            holdings = exp(spid)
            missing = sorted(reg.render(leaf) for leaf in holdings if leaf not in px)
            if missing:
                row["position_check"] = {"status": "skipped",
                                         "missing_close_px": missing}
                any_skipped = True
            else:
                indep = cash_flat(spid) + sum(eff * px[leaf]
                                              for leaf, eff in holdings.items())
                diff = ctl["value"] - indep
                diff_bp = diff / indep * 1e4 if indep else 0.0
                row["position_check"] = {
                    "status": "ok" if abs(diff_bp) <= TOL_BP else "breach",
                    "independent_value": round(indep, 2),
                    "diff_usd": round(diff, 2),
                    "diff_bp": round(diff_bp, 2),
                    "n_positions": len(holdings),
                    "cash": round(cash_flat(spid), 2),
                }
                comparable = True
                if abs(diff_bp) > TOL_BP:
                    any_breach = True
                    row["position_check"]["per_leaf"] = {
                        reg.render(leaf): {"shares": round(eff, 4),
                                           "close": px[leaf],
                                           "value": round(eff * px[leaf], 2)}
                        for leaf, eff in sorted(holdings.items(),
                                                key=lambda kv: -abs(kv[1] * px[kv[0]]))}
        # ② 官方 json 对照:纯信息(口径不同,不比对不判定)
        info = {"source": off[st]["source"], "column": off[st]["column"],
                "rows": off[st]["rows"],
                "note": "different accounting basis — informational only"}
        if off[st]["rows"] and ctl:
            d_last, v_last = off[st]["rows"][-1]
            info["ratio_display"] = round(ctl["value"] / v_last, 6) if v_last else None
        row["official_info"] = info
        report["strategies"][st] = row

    # PORTFOLIO 合计同查
    if snap and exp:
        root = snap["nodes"]
        pf = next((nid for nid, n in root.items() if n["kind"] == "portfolio"), None)
        ctl_pf = closes.get(pf)
        if pf and ctl_pf:
            holdings = exp(pf)
            if all(leaf in px for leaf in holdings):
                indep = cash_flat(pf) + sum(eff * px[leaf]
                                            for leaf, eff in holdings.items())
                diff_bp = (ctl_pf["value"] - indep) / indep * 1e4 if indep else 0.0
                report["portfolio_check"] = {
                    "status": "ok" if abs(diff_bp) <= TOL_BP else "breach",
                    "controller_close": ctl_pf["value"],
                    "independent_value": round(indep, 2),
                    "diff_bp": round(diff_bp, 2)}
                if abs(diff_bp) > TOL_BP:
                    any_breach = True

    if comparable:
        report["verdict"] = ("breach" if any_breach
                             else "partial" if any_skipped else "ok")
    _emit(report)
    return report


def _emit(report: dict) -> None:
    path = os.path.join(OUT_DIR, f"reconcile_{report['date']}.json")
    tmp = path + ".tmp"
    json.dump(report, open(tmp, "w"), indent=1)
    os.replace(tmp, path)
    print(f"[reconcile] {report['date']} verdict={report['verdict']} "
          f"-> {os.path.basename(path)}")
    for st, row in report.get("strategies", {}).items():
        pc = row.get("position_check")
        if pc and "diff_bp" in pc:
            print(f"  {st:5s} ctl={row['controller_close']:>13,.2f} "
                  f"indep={pc['independent_value']:>13,.2f} "
                  f"diff={pc['diff_bp']:+.1f}bp [{pc['status'].upper()}] "
                  f"({pc['n_positions']} pos, cash {pc['cash']:,.0f})")
        elif pc:
            print(f"  {st:5s} position check skipped: {pc.get('missing_close_px')}")
        else:
            print(f"  {st:5s} baseline (no close/snapshot yet)")
    if "portfolio_check" in report:
        p = report["portfolio_check"]
        print(f"  PORTF ctl={p['controller_close']:>13,.2f} "
              f"indep={p['independent_value']:>13,.2f} "
              f"diff={p['diff_bp']:+.1f}bp [{p['status'].upper()}]")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="EOD position-level reconcile (M5, v3)")
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    reconcile(a.date)
