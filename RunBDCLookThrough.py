#!/usr/bin/env python3
"""
RunBDCLookThrough.py — D5: the daily production driver + holdings diff engine for the
private-credit BDC look-through.

Two cadences, one run (§7.1):
  * EVERY DAY  — re-value the latest disclosed book against today's rate curve and
    rewrite the look-through (rates move daily even though holdings are quarterly).
  * FILING-DRIVEN — when RefreshBDCHoldings ingested a NEW 10-Q/10-K, diff the new
    snapshot against the previous one (by stable deal_uid) into new / changed / exited,
    and surface credit-quality early warnings (mark deterioration, new non-accrual,
    PIK rising). This is the systematic incremental/change/exit handling.

Outputs (all under the module's bdc_results/, plus a public/data latest for the agent):
  bdc_lookthrough_{asof}.json     full per-deal + aggregation (filing-driven rebuild)
  daily_report_{date}.json        re-valuation summary + diff summary + freshness
  diff_{asof}.json                new/changed/exited detail
  someo-park-.../public/data/bdc_lookthrough_latest.json

Idempotent: a (manifest-hash, rates-date) already processed is not recomputed. Designed
to be invoked by conductor/bdc_daily_pipeline.sh; scheduling is arranged externally.

Env: someopark_run. Use --sandbox to keep every output + the snapshot diff state in a
throwaway dir (zero production impact).
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.abspath(__file__))
_MODULE = os.path.join(_ROOT, "portfolio_of_private_credit_deals")
sys.path.insert(0, _ROOT)
sys.path.insert(0, _MODULE)

BDC_STORE = os.path.join(_ROOT, "price_data", "bdc_holdings")
RESULTS_DIR = os.path.join(_MODULE, "bdc_results")
PUBLIC_DATA = os.path.join(_ROOT, "someo-park-investment-management", "public", "data")

DIFF_KEYS = ["principal", "spread", "all_in_rate", "pik_rate", "fair_value", "cost"]

# canon 匹配层(2026-09-23):deal_uid 对分隔符/内嵌 par 金额敏感(A 通道 " | " vs
# B 通道 ", ";TSLX 每季摊销改写 par 文本),同一笔贷款换通道/换季度会得到全新 uid,
# 9/22 实测 1978 new / 1950 exited 中约一半是这种伪漂移。存量 uid 禁改(改
# normalize_issuer/uid 输入=全量重排、diff 大爆炸),因此 diff 时用即时计算的
# canon key 做第二遍匹配;drop_subtotal_rows 同源复用,对旧快照对称剔除小计行。
from RefreshBDCHoldings import deal_match_key, drop_subtotal_rows  # noqa: E402


def _canon_keys(df: pd.DataFrame) -> list:
    if "cik" not in df.columns or "identifier" not in df.columns:
        return [None] * len(df)
    def _cik(c):
        cs = str(c).strip()
        return (cs[:-2] if cs.endswith(".0") else cs).lstrip("0")
    return [deal_match_key(_cik(c), i) if (pd.notna(c) and pd.notna(i)) else None
            for c, i in zip(df["cik"], df["identifier"])]


def _alert(msg: str) -> None:
    banner = "!" * 70
    for stream in (sys.stderr, sys.stdout):
        print(f"\n{banner}\n[BDC_LOOKTHROUGH ALERT] {msg}\n{banner}", file=stream)


def _jdump(obj, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w"), indent=2, default=str)


# ── holdings diff engine (filing-driven; by stable deal_uid) ────────────────
def _prev_snapshot_paths(store: str, manifest: dict) -> dict:
    """For each BDC, the most recent PRIOR snapshot parquet (not the current adsh)."""
    prev = {}
    for t, mf in manifest.items():
        cur = f"soi_{mf['reportDate']}_{mf['adsh']}.parquet"
        snaps = sorted(glob.glob(os.path.join(store, t, "soi_*.parquet")),
                       key=os.path.getmtime)
        older = [p for p in snaps if os.path.basename(p) != cur]
        if older:
            prev[t] = older[-1]
    return prev


def _compare_pair(uid, c, p, changed, warnings, counters, matched_by):
    """一对已配对 (cur, prev) 行的全部比较;结果就地追加。"""
    delta = {}
    for k in DIFF_KEYS:
        cv, pv = pd.to_numeric(c.get(k), errors="coerce"), pd.to_numeric(p.get(k), errors="coerce")
        if pd.notna(cv) and pd.notna(pv) and abs(cv - pv) > (abs(pv) * 1e-4 + 1e-9):
            delta[k] = {"prev": float(pv), "cur": float(cv)}
    if delta:
        changed.append({"deal_uid": uid, "company": c.get("company"),
                        "bdc": c.get("bdc"), "matched_by": matched_by, "delta": delta})
    # early warnings — severity 区分季度例行重标(info)与真实信用恶化(alert)。
    cm = _mark(c); pm = _mark(p)
    if pd.isna(cm) or pd.isna(pm):
        counters["mark_check_skipped"] += 1          # 缺 fv/cost 无法验 mark,如实计数
    elif (pm - cm) > 0.05:
        # 2026-09-23 修订:旧规则只对 [0.30,0.90) 报 alert,cm<0.30 反而静音 ——
        # 恰好把跌得最惨的静音了。现在 cm≥0.90(仍近平价)或 pm>3(垃圾前值)
        # 归 info,其余全 alert;cm<0.30 加 suspect_data 标(<0.05 几乎必是解析伪值)。
        sev = "info" if (cm >= 0.90 or pm > 3.0) else "alert"
        # company 键名: diff 输入帧统一 rename issuer→company(见 run());此前误取
        # c.get("issuer") 恒 None,91 条 alert 点不出借款人 —— 2026-08-10 修复。
        w = {"type": "mark_deterioration", "severity": sev,
             "deal_uid": uid, "company": c.get("company"),
             "bdc": c.get("bdc"), "from": round(pm, 3), "to": round(cm, 3)}
        if cm < 0.30:
            w["suspect_data"] = bool(cm < 0.05)
        warnings.append(w)
    cpik = pd.to_numeric(c.get("pik_rate"), errors="coerce")
    ppik = pd.to_numeric(p.get("pik_rate"), errors="coerce")
    if pd.notna(cpik) and (pd.isna(ppik) or cpik > (ppik or 0)) and cpik > 0:
        # PIK turning on/up is a soft signal → info (not a standalone alert)
        warnings.append({"type": "pik_increase", "severity": "info",
                         "deal_uid": uid, "company": c.get("company"),
                         "bdc": c.get("bdc"), "from": (float(ppik) if pd.notna(ppik) else 0.0),
                         "to": float(cpik)})
    # 2026-09-23:模块 docstring 一直宣称监控 "new non-accrual" 但从未实现 —— 落地。
    cna, pna = c.get("non_accrual"), p.get("non_accrual")
    cna_t = bool(cna) and pd.notna(cna)
    pna_t = bool(pna) and pd.notna(pna)
    if cna_t and not pna_t:
        warnings.append({"type": "new_non_accrual", "severity": "alert",
                         "deal_uid": uid, "company": c.get("company"), "bdc": c.get("bdc"),
                         "fair_value": float(pd.to_numeric(c.get("fair_value"),
                                                           errors="coerce") or 0)})


def diff_holdings(cur: pd.DataFrame, prev: pd.DataFrame) -> dict:
    """new / changed / exited + credit-quality early warnings。

    两遍匹配(2026-09-23 重建):第 1 遍 deal_uid 精确匹配(与历史行为一致);
    第 2 遍对残余 new/exited 按 canon key(cik+规范化 identifier)再配,吸收
    分隔符/par 金额造成的 uid 伪漂移;canon key 撞多条时先按 (key, round(fv,1))
    精确配,余下按 fv 排序顺配并计入 ambiguous_matches。"""
    counters = {"mark_check_skipped": 0, "ambiguous_matches": 0,
                "matched_uid": 0, "matched_canon": 0}
    cur = cur.copy(); prev = prev.copy()
    cur["_ck"] = _canon_keys(cur)
    prev["_ck"] = _canon_keys(prev)
    cur = cur.set_index("deal_uid")
    prev = prev.set_index("deal_uid")
    counters["dup_uid_cur"] = int(cur.index.duplicated().sum())
    counters["dup_uid_prev"] = int(prev.index.duplicated().sum())
    cur = cur[~cur.index.duplicated(keep="first")]     # 旧实现 iloc[0] 同语义,现计数
    prev = prev[~prev.index.duplicated(keep="first")]
    cur_ids, prev_ids = set(cur.index), set(prev.index)
    common = cur_ids & prev_ids
    changed, warnings = [], []
    for uid in sorted(common):
        _compare_pair(uid, cur.loc[uid], prev.loc[uid], changed, warnings, counters, "deal_uid")
    counters["matched_uid"] = len(common)
    new_ids = cur_ids - prev_ids
    exit_ids = prev_ids - cur_ids

    # 第 2 遍:canon key 匹配残余
    resid_cur = cur.loc[sorted(new_ids)]
    resid_prev = prev.loc[sorted(exit_ids)]
    ck_cur, ck_prev = {}, {}
    for uid, ck in resid_cur["_ck"].items():
        if ck: ck_cur.setdefault(ck, []).append(uid)
    for uid, ck in resid_prev["_ck"].items():
        if ck: ck_prev.setdefault(ck, []).append(uid)
    matched_new, matched_exit = set(), set()
    def _fv1(frame, u):
        return round(float(pd.to_numeric(frame.loc[u].get("fair_value"), errors="coerce") or 0), 1)
    for ck in sorted(ck_cur):
        cu, pu = ck_cur[ck], ck_prev.get(ck)
        if not pu:
            continue
        if len(cu) == 1 and len(pu) == 1:
            pairs = [(cu[0], pu[0])]
        else:
            pairs, rest_cu = [], []
            by_fv = {}
            for u in pu: by_fv.setdefault(_fv1(resid_prev, u), []).append(u)
            for u in cu:
                b = by_fv.get(_fv1(resid_cur, u))
                if b: pairs.append((u, b.pop(0)))
                else: rest_cu.append(u)
            rest_pu = [u for b in by_fv.values() for u in b]
            rest_cu.sort(key=lambda u: _fv1(resid_cur, u))
            rest_pu.sort(key=lambda u: _fv1(resid_prev, u))
            n_ord = min(len(rest_cu), len(rest_pu))
            pairs += list(zip(rest_cu[:n_ord], rest_pu[:n_ord]))
            counters["ambiguous_matches"] += n_ord
        for cu_id, pu_id in pairs:
            _compare_pair(cu_id, resid_cur.loc[cu_id], resid_prev.loc[pu_id],
                          changed, warnings, counters, "canon_key")
            matched_new.add(cu_id); matched_exit.add(pu_id)
    counters["matched_canon"] = len(matched_new)
    new_ids -= matched_new
    exit_ids -= matched_exit

    def _rows(ids, frame):
        return [{"deal_uid": u, "company": frame.loc[u].get("company"),
                 "bdc": frame.loc[u].get("bdc"),
                 "fair_value": float(pd.to_numeric(frame.loc[u].get("fair_value"),
                                                   errors="coerce") or 0)}
                for u in sorted(ids)]
    return {"new": _rows(new_ids, cur), "exited": _rows(exit_ids, prev),
            "changed": changed, "warnings": warnings,
            "counts": {"new": len(new_ids), "exited": len(exit_ids),
                       "changed": len(changed), "warnings": len(warnings),
                       # additive severity split (existing 'warnings' kept for back-compat):
                       "warnings_alert": sum(1 for w in warnings if w.get("severity") == "alert"),
                       "warnings_info": sum(1 for w in warnings if w.get("severity") == "info"),
                       **counters}}


def _mark(row):
    fv = pd.to_numeric(row.get("fair_value"), errors="coerce")
    cost = pd.to_numeric(row.get("cost"), errors="coerce")
    return (fv / cost) if (pd.notna(fv) and pd.notna(cost) and cost) else np.nan


# ── daily driver ────────────────────────────────────────────────────────────
def run(store=BDC_STORE, results_dir=RESULTS_DIR, public_dir=PUBLIC_DATA,
        run_cashflows=True, write=True) -> dict:
    import bdc_deal_loader
    import bdc_lookthrough

    manifest_path = os.path.join(store, "latest_manifest.json")
    if not os.path.exists(manifest_path):
        _alert(f"no manifest at {manifest_path}; run RefreshBDCHoldings first")
        return {}
    manifest = json.load(open(manifest_path))
    as_of = max(mf["reportDate"] for mf in manifest.values())
    rds = sorted({mf["reportDate"] for mf in manifest.values()})
    if len(rds) > 1:
        _alert(f"mixed reportDates across BDCs {rds} — as_of=max;逐家日期见 freshness")
    asof_age_days = (date.today() - date.fromisoformat(as_of)).days
    if asof_age_days > 135:
        _alert(f"holdings as_of {as_of} 已 {asof_age_days} 天(>135)——下季 10-Q 逾期或 ingest 卡死")
    rates_date = date.today().isoformat()
    # rates_date 是**运行日**;曲线真实新鲜度 = fred_rates.csv 末行日期(通常 T-1)。
    # 2026-09-23 前两者混用,STEP A 静默失败时日报会顶着当天日期用陈旧曲线。
    rates_date_actual = None
    try:
        _rc = os.path.join(_MODULE, "fred_rates.csv")
        if os.path.exists(_rc):
            _rd = pd.read_csv(_rc, usecols=[0]).iloc[:, 0]
            rates_date_actual = str(pd.to_datetime(_rd, errors="coerce").max().date())
            _lag = (date.today() - date.fromisoformat(rates_date_actual)).days
            if _lag > 4:
                _alert(f"rate curve stale: fred_rates.csv 末行 {rates_date_actual} 落后今天 {_lag} 天")
    except Exception as e:  # noqa: BLE001
        _alert(f"fred_rates.csv freshness check failed: {e!r}")

    # idempotency guard
    mhash = hashlib.sha1(json.dumps({t: manifest[t]["adsh"] for t in sorted(manifest)},
                                    sort_keys=True).encode()).hexdigest()[:12]
    guard_path = os.path.join(results_dir, "last_run.json")
    if write and os.path.exists(guard_path):
        last = json.load(open(guard_path))
        if last.get("mhash") == mhash and last.get("rates_date") == rates_date:
            print(f"[idempotent] manifest {mhash} + rates {rates_date} already done; skip")
            return last

    # 1) build the combined deal table from the latest snapshots — always under
    # results_dir (production: module bdc_results/; sandbox: the sandbox). compute_
    # lookthrough reads this path directly, so it need not live in deals_data/.
    os.makedirs(results_dir, exist_ok=True)
    csv_path = os.path.join(results_dir, "bdc_deal_start.csv")
    bdc_deal_loader.write_bdc_deal_start(store, csv_path)

    # 2) full look-through (re-valuation against today's rates inside the cashflow engine);
    #    pass the BDC-level non-accrual rates parsed during ingest (manifest)
    bdc_na = {t: (manifest[t].get("non_accrual") or {}).get("non_accrual_pct_fv")
              for t in manifest}
    lt = bdc_lookthrough.compute_lookthrough(csv_path, as_of=as_of, run_cashflows=run_cashflows,
                                             bdc_non_accrual=bdc_na)
    summary, deals = lt["summary"], lt["deals"]
    summary["rates_date"] = rates_date
    summary["rates_date_actual"] = rates_date_actual

    # 2b) §7.3 scenario stress matrix — rate ladder ±300bp + three macro scenarios
    # (mild/severe recession, stagflation) over the SAME enriched book, survival-
    # weighted expected cashflows. Loud-fail (project convention), never silent.
    if run_cashflows:
        import time as _time
        import bdc_stress
        try:
            _st0 = _time.time()
            summary["stress"] = bdc_stress.run_stress_matrix(deals, as_of)
            summary["stress"]["runtime_s"] = round(_time.time() - _st0, 1)
            print(f"[stress] {len(bdc_stress.SCENARIOS)} scenarios in "
                  f"{summary['stress']['runtime_s']}s — worst {summary['stress']['worst']}")
        except Exception as e:  # noqa: BLE001
            _alert(f"stress matrix failed: {e!r}")
            summary["stress"] = {"error": repr(e)}
    summary["manifest"] = {t: {"adsh": manifest[t]["adsh"], "reportDate": manifest[t]["reportDate"],
                               "gross_net_ratio": manifest[t].get("gross_net_ratio")}
                           for t in manifest}

    # 3) holdings diff (filing-driven) vs previous snapshots
    prev_paths = _prev_snapshot_paths(store, manifest)
    diff = {"counts": {"new": 0, "exited": 0, "changed": 0, "warnings": 0,
                       "warnings_alert": 0, "warnings_info": 0},
            "note": "no prior snapshot for any BDC (first run)"}
    if prev_paths:
        # 2026-09-23 重建:cur 侧此前取自 deals CSV(loader 输出),不带 cik/identifier,
        # canon 二次匹配无从谈起;现两侧一律直读快照 parquet(schema 相同),并对称
        # 剔除小计行(旧快照由旧版 ingest 写入,仍含发行人合计行,不剔会伪造 exited)。
        cur_frames, prev_frames, diffed = [], [], []
        sub_dropped = {}
        for t in sorted(prev_paths):
            mf_t = manifest[t]
            cp = os.path.join(store, t, f"soi_{mf_t['reportDate']}_{mf_t['adsh']}.parquet")
            if not os.path.exists(cp):
                _alert(f"{t}: current snapshot missing ({os.path.basename(cp)}) — excluded from diff")
                continue
            cf, n_cur, _ = drop_subtotal_rows(pd.read_parquet(cp))
            pf, n_prev, _ = drop_subtotal_rows(pd.read_parquet(prev_paths[t]))
            if n_cur or n_prev:
                sub_dropped[t] = {"cur": int(n_cur), "prev": int(n_prev)}
            cur_frames.append(cf); prev_frames.append(pf); diffed.append(t)
        if cur_frames:
            cur_all = pd.concat(cur_frames, ignore_index=True).rename(columns={"issuer": "company"})
            prev_all = pd.concat(prev_frames, ignore_index=True).rename(columns={"issuer": "company"})
            diff = diff_holdings(cur_all, prev_all)
            diff["bdcs_diffed"] = diffed
            if sub_dropped:
                diff["subtotal_rows_dropped"] = sub_dropped

    # G7: stock layer (BDC share-price sleeve equity, daily) alongside the look-through
    # layer (latest disclosed holdings × today's rates) — each with its own as-of.
    stock_layer = None
    perf_path = os.path.join(public_dir, "private_credit_bdc_performance.json")
    if os.path.exists(perf_path):
        try:
            perf = json.load(open(perf_path))
            if isinstance(perf, list) and perf:
                last = perf[-1]
                stock_layer = {"as_of": last.get("date"), "bdc_equity": last.get("bdc_equity"),
                               "bdc_pnl": last.get("bdc_pnl"), "bdc_dd_pct": last.get("bdc_dd"),
                               # 出处如实声明(2026-09-23):perf json 末行的逐字复制,
                               # 不是独立计算 —— qc_reconcile 拿它当"旁证"是循环验证;
                               # 改判定属 M4 对账红线,等用户拍板,这里先把出处钉死。
                               "source": "copy_of_perf_last_row"}
                try:
                    _age = (date.today() - date.fromisoformat(str(last.get("date")))).days
                    stock_layer["age_days"] = _age
                    if _age > 4:
                        _alert(f"stock_layer stale: perf 末行 {last.get('date')} 已 {_age} 天未更新")
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            _alert(f"stock_layer read failed: {e!r}")

    daily = {
        "date": rates_date, "as_of": as_of,
        "asof_age_days": asof_age_days, "mixed_asof": (len(rds) > 1),
        "rates_date_actual": rates_date_actual,
        "stock_layer": stock_layer,                       # G7: share-price sleeve (daily)
        "lookthrough_layer": {                            # disclosed holdings × today's rates
            "as_of": as_of, "revaluation": summary["weighted"],
            "rate_sensitivity": summary.get("rate_sensitivity"),
            "maturity_ladder": summary.get("maturity_ladder"),
        },
        "mark_distribution": summary["mark_distribution"],
        "diff_summary": diff["counts"],
        "early_warning": summary["early_warning"],
        "bdc_non_accrual": summary.get("bdc_non_accrual"),
        "stress": summary.get("stress"),                  # §7.3: same-level scenario block
        "freshness": {t: manifest[t]["reportDate"] for t in manifest},
    }

    if write:
        _jdump(summary, os.path.join(results_dir, f"bdc_lookthrough_{as_of}.json"))
        _jdump(daily, os.path.join(results_dir, f"daily_report_{rates_date}.json"))
        _jdump(diff, os.path.join(results_dir, f"diff_{as_of}.json"))
        _jdump(summary, os.path.join(public_dir, "bdc_lookthrough_latest.json"))
        _jdump({"mhash": mhash, "rates_date": rates_date, "as_of": as_of,
                "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, guard_path)
        hb = os.path.join(store, "lookthrough_heartbeat.log")
        _stw = (summary.get("stress") or {}).get("worst") or {}
        with open(hb, "a") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} "
                     f"asof={as_of} rates={rates_date} deals={summary['deal_count']} "
                     f"diff={diff['counts']} "
                     f"stress_worst={_stw.get('name')}:{_stw.get('delta_ev_pct')}\n")
    return {"summary": summary, "daily": daily, "diff": diff}


def main():
    ap = argparse.ArgumentParser(description="Daily BDC look-through + holdings diff")
    ap.add_argument("--sandbox", metavar="DIR")
    ap.add_argument("--no-cashflows", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.sandbox:
        res = run(store=os.path.join(a.sandbox, "bdc_holdings"),
                  results_dir=os.path.join(a.sandbox, "results"),
                  public_dir=os.path.join(a.sandbox, "public"),
                  run_cashflows=not a.no_cashflows, write=not a.dry_run)
    else:
        res = run(run_cashflows=not a.no_cashflows, write=not a.dry_run)
    if res:
        print(json.dumps(res.get("daily", res), indent=2, default=str)[:1800])


if __name__ == "__main__":
    main()
