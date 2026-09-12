"""
controller/reconcile_eod.py — 日终对账(plan §五;M5)。

方法 v3(2026-08-12 用户令:**全面去除对 ratio 的依赖**——ratio 变化很多,
只能计算出来展示,绝不能进任何判定;收益率比较也含隐性等比例假设,一并废除。
**判定只用持仓数额 shares × 价格 × 持仓详情**):

  判定(唯一 verdict 来源)= 持仓级独立重算,**时点同步版**(v4,2026-08-14):
      controller 16:00 ET 收盘值(nav_stream 截断末笔,带 structure_hash)
        vs
      Σ( 结构快照 golden shares × 该行价格时点的分钟 bar ) + cash_flat
      —— 订阅是 15 分钟延迟行情,收盘行里装的是 ~15:45 的价;比官方 daily_close
      量出来的是行情漂移不是算错。全局扫描唯一延迟 lag(残差总和最小),
      该时点重算的残差 > SYNC_TOL_BP → breach,逐票列出,绝不静默。
      shares 错误任何 lag 都拟合不掉。PORTFOLIO 合计同查。
      官方 daily_close 对比降级为 close_drift 纯信息(漂移量)。
      策略覆盖取当时结构快照的全部 strategy 节点,不由官方展示列决定;
      策略/合计缺收盘或同步分钟价时明确 incomplete,不能由其余 ok 掩盖。

  官方三 json 对照 = 纯信息展示(official 值/日期、账本值、ratio_display、
  两口径日收益差)——**不参与任何判定**;两本账刻度不同属口径事实,
  差异注明 informational。

产物:controller/output/reconcile_{date}.json。绝不回写三 json。
"""
from __future__ import annotations

import bisect
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from controller.model import REPO
from controller.registry import Registry
from controller.prices import PriceFeed

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(_HERE, "output")
# ── 判定口径(2026-08-14 改版 A):时点同步完整性检查 ─────────────────────────
# Polygon 订阅是 **15 分钟延迟行情**(nav_latest.feed_delay_min 一直如实在报),
# 所以 controller 的"16:00 截断收盘行"里装的是 ~15:45 的价格。拿它去比官方
# daily_close,量出来的是"最后 15 分钟行情漂移"(±10-50bp),不是算错——
# 真正的 shares/cash/引擎错误会被漂移淹没。
# 判定改为:扫描 0..LAG_SCAN_MIN 分钟的全局延迟,取让各策略残差总和最小的
# 唯一 lag(feed 延迟是全局参数,只允许一个),用该时点的分钟 bar 重算。
# 链条若正确,残差 ≈ 0-3bp;shares 若错,**任何** lag 都拟合不掉。
# 官方收盘对比保留为 close_drift 纯信息(漂移量,不判定)。
SYNC_TOL_BP = 5.0            # 时点同步残差容忍(gross 口径;分钟 bar 粒度 ≈1-3bp)
LAG_SCAN_MIN = 25            # 延迟扫描上限(订阅 15min + lastTrade 逐票参差)
# 分母 = gross exposure(Σ|shares×px|):定价误差按被定价的名义额缩放,
# 净账面对市场中性簿是退化统计量(净额→0 时 bp→∞)。net 口径仅存参考。
CLOSE_GAP_TOL_MIN = 10.0     # 末笔离 16:00 ET 超过此值 ⇒ 当日无收盘行,判定 incomplete
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
    "aeus": ("someo-park-investment-management/public/data/master_portfolio_performance.json",
             "aeus_equity"),
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
    """{node_id: {"value": v, "hash": h, "epoch": s}} 该日 nav_stream ≤16:00 ET
    的末笔(合并 schema 轮转分段,各段用自己的表头)。epoch = 该行 ts(秒)。"""
    y, m, d = (int(x) for x in date_iso.split("-"))
    cutoff = datetime(y, m, d, 16, 0, tzinfo=_ET)
    out: dict[str, dict] = {}
    last_et: datetime | None = None
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
                ts_et = ts.astimezone(_ET)
                if ts_et > cutoff:
                    continue              # 16:00 后(盘后/夜间平移)不算收盘
                if last_et is None or ts_et > last_et:
                    last_et = ts_et
                out[parts[i_node]] = {"value": float(parts[i_val]),
                                      "hash": (parts[i_hash]
                                               if i_hash is not None else None),
                                      "epoch": ts.timestamp()}
    if out and last_et is not None:
        # 覆盖度: 末笔离 16:00 多远。循环若盘中死掉(2026-08-13 14:04 DNS 事故),
        # 末笔就是死亡时刻的值 —— 拿它当"收盘"去比 daily_close 会产出**假判定**。
        out["__coverage__"] = {"last_et": last_et.isoformat(timespec="seconds"),
                               "gap_min": round((cutoff - last_et).total_seconds()
                                                / 60.0, 1)}
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


def _px_at(bars: list, cut_epoch: float):
    """升序 (epoch, close) 序列里 ≤ cut_epoch 的末笔 close;无 → None。"""
    i = bisect.bisect_right(bars, (cut_epoch, float("inf"))) - 1
    return bars[i][1] if i >= 0 else None


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


def _snapshot_strategies(snap: dict, reg: Registry) -> dict[str, str]:
    """当时结构里的全部策略(name -> node_id),与官方展示列/今日注册策略解耦。"""
    out = {}
    for nid, node in snap["nodes"].items():
        if node["kind"] != "strategy":
            continue
        key = reg.nodes.get(nid, {}).get("canonical_key", "")
        name = (key[len("strategy:"):] if key.startswith("strategy:")
                else str(node.get("attrs", {}).get("display_name")
                         or reg.render(nid)).lower())
        # 即使历史显示名重复,也不能覆盖掉一个待检查节点。
        out[name if name not in out else nid] = nid
    return out


def reconcile(date: str | None = None, *, write_report: bool = True) -> dict:
    """独立重算日终值;write_report=False 仍读取行情,但不写对账报告。"""
    reg = Registry()
    feed = PriceFeed(reg)

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
              "method": "position-level recompute v4 (time-synced): snapshot "
                        "golden shares × minute-bar px at the row's own price "
                        "time (global lag scan, feed is 15-min delayed) + "
                        "cash_flat vs controller 16:00 close (NO ratio / NO "
                        "return-proportionality in verdict; daily_close drift "
                        "and official jsons informational only)",
              "tolerance_bp": SYNC_TOL_BP,
              "tolerance_basis": "gross_exposure @ sync-lag minute bars",
              "strategies": {}, "verdict": "incomplete",
              "portfolio_check": {"status": "incomplete",
                                  "note": "no synchronized portfolio close available"}}
    coverage = closes.pop("__coverage__", None)
    report["coverage"] = coverage
    if not target:
        report["note"] = "no nav_stream close rows yet"
        _emit(report, write_report=write_report)
        return report
    # 覆盖度闸门: 末笔离 16:00 太远 ⇒ 该日没有真正的收盘值,拒绝出判定。
    # 宁可 incomplete 也不要拿盘中值冒充收盘产出一个假 pass/breach。
    if coverage and coverage["gap_min"] > CLOSE_GAP_TOL_MIN:
        report["verdict"] = "incomplete"
        report["note"] = (f"nav_stream 末笔 {coverage['last_et']} 距 16:00 ET "
                          f"{coverage['gap_min']:.0f} 分钟(>{CLOSE_GAP_TOL_MIN}) "
                          f"—— 当日无收盘行(常驻循环盘中中断),不出判定")
        report["portfolio_check"]["note"] = report["note"]
        _emit(report, write_report=write_report)
        return report

    # 盘中退役节点的化石行(当日平仓的 pair,末笔停在退役时刻、挂着旧 hash)
    # 不属于收盘簿 —— 只保留末笔贴着全局末 tick 的节点,否则一行化石就把
    # 全天判成 mixed hashes 拒算(2026-08-14 DGX/NKE 上午平仓即触发)。
    if coverage:
        last_epoch = datetime.fromisoformat(coverage["last_et"]).timestamp()
        retired = [nid for nid, v in closes.items()
                   if last_epoch - v["epoch"] > CLOSE_GAP_TOL_MIN * 60]
        if retired:
            report["retired_intraday"] = sorted(reg.render(n) for n in retired)
            for nid in retired:
                closes.pop(nid)

    # 当时结构快照(shares/cash 的 golden 定格)
    hashes = {v["hash"] for v in closes.values() if v.get("hash")}
    missing_hashes = [reg.render(nid) for nid, row in closes.items()
                      if not row.get("hash")]
    snap = None
    if missing_hashes:
        report["note"] = f"stream rows lack structure_hash: {sorted(missing_hashes)} — 重算跳过"
    elif len(hashes) == 1:
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

    if snap is None:
        report["portfolio_check"]["note"] = report["note"]
        _emit(report, write_report=write_report)
        return report

    st_to_spid = _snapshot_strategies(snap, reg)
    portfolios = [nid for nid, node in snap["nodes"].items()
                  if node["kind"] == "portfolio"]
    pf = portfolios[0] if len(portfolios) == 1 else None
    if not st_to_spid:
        report["note"] = "snapshot has no strategy nodes — 无法验证策略覆盖"
    off = official_info()
    any_breach, comparable, any_skipped = False, False, False
    exp = cash_flat = None
    px: dict[str, float] = {}
    mins: dict[str, list] = {}
    if snap:
        exp, cash_flat = flatten_snapshot(snap)
        # 价格集合覆盖全部策略与合计的实际叶子(包括组合直接持有的票)。
        # _ANCHORS 只决定信息展示,不能限制独立对账的覆盖范围。
        check_nodes = list(st_to_spid.values()) + portfolios
        leaves = sorted({leaf for nid in check_nodes for leaf in exp(nid)})
        px = feed.daily_close(leaves, target)
        mins = feed.minute_closes(leaves, target)

    # nid 在 lag 分钟延迟假设下的重算值与 gross(任一叶缺 bar → (None, None))
    def _indep_at(nid: str, lag_min: int):
        cut = closes[nid]["epoch"] - lag_min * 60
        tot, gross = cash_flat(nid), 0.0
        for leaf, eff in exp(nid).items():
            p = _px_at(mins.get(leaf) or [], cut)
            if p is None:
                return None, None
            tot += eff * p
            gross += abs(eff * p)
        return tot, gross

    # 全局延迟估计:feed 延迟是全局参数,只允许唯一 lag —— 取让全部策略
    # 残差总和最小的那个。shares/cash 若错,不存在能拟合掉误差的 lag。
    sync_lag = None
    if snap:
        spids = [nid for nid in st_to_spid.values() if closes.get(nid)]
        best = None
        for lag in range(LAG_SCAN_MIN + 1):
            tot, n = 0.0, 0
            for nid in spids:
                v, _ = _indep_at(nid, lag)
                if v is not None:
                    tot += abs(closes[nid]["value"] - v)
                    n += 1
            if n and (best is None or tot < best[0]):
                best = (tot, lag)
        if best:
            sync_lag = best[1]
    report["price_lag_min"] = sync_lag

    def _missing_at(nid: str) -> list[str]:
        cut = closes[nid]["epoch"] - (sync_lag or 0) * 60
        return sorted(reg.render(leaf) for leaf in exp(nid)
                      if _px_at(mins.get(leaf) or [], cut) is None)

    for st, spid in st_to_spid.items():
        row: dict = {}
        ctl = closes.get(spid)
        if ctl:
            row["controller_close"] = ctl["value"]
        # ① 判定:时点同步完整性重算(唯一 verdict 来源)
        if snap and ctl and spid in snap["nodes"]:
            holdings = exp(spid)
            v_sync, gross_sync = (_indep_at(spid, sync_lag)
                                  if sync_lag is not None else (None, None))
            if v_sync is None:
                row["position_check"] = {
                    "status": "incomplete",
                    "note": "missing minute bars at synchronized price time",
                    "missing_minute_bars": _missing_at(spid)}
                any_skipped = True
            else:
                diff = ctl["value"] - v_sync
                base = gross_sync or abs(v_sync)    # 全现金簿退回净额
                bp = diff / base * 1e4 if base else 0.0
                row["position_check"] = {
                    "status": "ok" if abs(bp) <= SYNC_TOL_BP else "breach",
                    "lag_min": sync_lag,
                    "independent_value": round(v_sync, 2),
                    "gross_exposure": round(gross_sync, 2),
                    "diff_usd": round(diff, 2),
                    "diff_bp_gross": round(bp, 2),
                    "n_positions": len(holdings),
                    "cash": round(cash_flat(spid), 2),
                }
                comparable = True
                if abs(bp) > SYNC_TOL_BP:
                    any_breach = True
                    cut = ctl["epoch"] - sync_lag * 60
                    row["position_check"]["per_leaf"] = {
                        reg.render(leaf): {
                            "shares": round(eff, 4),
                            "px_sync": _px_at(mins.get(leaf) or [], cut),
                            "value": round(
                                eff * (_px_at(mins.get(leaf) or [], cut) or 0.0), 2)}
                        for leaf, eff in sorted(
                            holdings.items(),
                            key=lambda kv: -abs(kv[1] * (px.get(kv[0]) or 0.0)))}
            # ①b 信息:官方收盘漂移(延迟行情 vs 收盘竞价的行情移动,不判定)
            if all(l in px for l in holdings):
                v_close = cash_flat(spid) + sum(eff * px[l]
                                                for l, eff in holdings.items())
                g_close = sum(abs(eff * px[l]) for l, eff in holdings.items())
                d_close = ctl["value"] - v_close
                b_close = g_close or abs(v_close)
                row["close_drift"] = {
                    "note": "vs official daily_close — market drift over the "
                            "feed delay window, informational only",
                    "independent_value": round(v_close, 2),
                    "diff_usd": round(d_close, 2),
                    "diff_bp_gross": round(d_close / b_close * 1e4
                                           if b_close else 0.0, 2),
                    "diff_bp_net": round(d_close / v_close * 1e4
                                         if v_close else 0.0, 2)}
        else:
            row["position_check"] = {"status": "incomplete",
                                     "note": "strategy close row missing"}
            any_skipped = True
        # ② 官方 json 对照:纯信息(口径不同,不比对不判定)。新策略即使尚无
        # 官方展示列,也照常参与持仓级判定,不能被展示配置挡掉。
        anchor = off.get(st)
        info = {"source": anchor["source"] if anchor else None,
                "column": anchor["column"] if anchor else None,
                "rows": anchor["rows"] if anchor else [],
                "note": "different accounting basis — informational only"}
        if info["rows"] and ctl:
            d_last, v_last = info["rows"][-1]
            info["ratio_display"] = round(ctl["value"] / v_last, 6) if v_last else None
        row["official_info"] = info
        report["strategies"][st] = row

    # PORTFOLIO 合计同查(同一 sync_lag)
    if snap and exp and sync_lag is not None:
        ctl_pf = closes.get(pf)
        if pf and ctl_pf:
            v_sync, gross_sync = _indep_at(pf, sync_lag)
            if v_sync is not None:
                diff = ctl_pf["value"] - v_sync
                base = gross_sync or abs(v_sync)
                diff_bp = diff / base * 1e4 if base else 0.0
                report["portfolio_check"] = {
                    "status": "ok" if abs(diff_bp) <= SYNC_TOL_BP else "breach",
                    "lag_min": sync_lag,
                    "controller_close": ctl_pf["value"],
                    "independent_value": round(v_sync, 2),
                    "gross_exposure": round(gross_sync, 2),
                    "diff_usd": round(diff, 2),
                    "diff_bp_gross": round(diff_bp, 2)}
                if abs(diff_bp) > SYNC_TOL_BP:
                    any_breach = True
            else:
                report["portfolio_check"] = {
                    "status": "incomplete",
                    "note": "missing minute bars at synchronized price time",
                    "missing_minute_bars": _missing_at(pf)}
        else:
            report["portfolio_check"]["note"] = (
                "snapshot must contain exactly one portfolio node" if not pf
                else "portfolio close row missing")
    else:
        report["portfolio_check"]["note"] = "no synchronized price lag available"

    # 缺策略/合计覆盖不能被其余策略的 ok 掩盖;已测出的 breach 仍优先报告。
    complete = (bool(st_to_spid) and comparable and not any_skipped
                and report["portfolio_check"]["status"] in ("ok", "breach"))
    report["verdict"] = "breach" if any_breach else "ok" if complete else "incomplete"
    _emit(report, write_report=write_report)
    return report


def _emit(report: dict, *, write_report: bool = True) -> None:
    path = os.path.join(OUT_DIR, f"reconcile_{report['date']}.json")
    if write_report:
        tmp = path + ".tmp"
        json.dump(report, open(tmp, "w"), indent=1)
        os.replace(tmp, path)
    print(f"[reconcile] {report['date']} verdict={report['verdict']} "
          + (f"-> {os.path.basename(path)}" if write_report else "(dry-run; not written)"))
    for st, row in report.get("strategies", {}).items():
        pc = row.get("position_check")
        if pc and "diff_bp_gross" in pc:
            drift = row.get("close_drift", {}).get("diff_bp_gross")
            print(f"  {st:5s} ctl={row['controller_close']:>13,.2f} "
                  f"sync={pc['independent_value']:>13,.2f} "
                  f"resid={pc['diff_bp_gross']:+.1f}bp [{pc['status'].upper()}] "
                  f"({pc['n_positions']} pos, cash {pc['cash']:,.0f}"
                  + (f", drift {drift:+.1f}bp" if drift is not None else "") + ")")
        elif pc:
            print(f"  {st:5s} [{pc['status'].upper()}] {pc.get('note', '')} "
                  f"{pc.get('missing_minute_bars', [])}")
        else:
            print(f"  {st:5s} baseline (no close/snapshot yet)")
    if "portfolio_check" in report:
        p = report["portfolio_check"]
        if p["status"] == "incomplete":
            print(f"  PORTF [INCOMPLETE] {p['note']} "
                  f"{p.get('missing_minute_bars', [])}")
            return
        print(f"  PORTF ctl={p['controller_close']:>13,.2f} "
              f"sync={p['independent_value']:>13,.2f} "
              f"resid={p['diff_bp_gross']:+.1f}bp [{p['status'].upper()}] "
              f"(lag {p['lag_min']}min)")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="EOD position-level reconcile (M5, v4 time-synced)")
    ap.add_argument("--date", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="read inputs and prices, print verdict without writing a report")
    a = ap.parse_args()
    reconcile(a.date, write_report=not a.dry_run)
