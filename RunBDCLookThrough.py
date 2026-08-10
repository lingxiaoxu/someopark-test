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


def diff_holdings(cur: pd.DataFrame, prev: pd.DataFrame) -> dict:
    """new / changed / exited by deal_uid + credit-quality early warnings."""
    cur = cur.set_index("deal_uid")
    prev = prev.set_index("deal_uid")
    cur_ids, prev_ids = set(cur.index), set(prev.index)
    new_ids = cur_ids - prev_ids
    exit_ids = prev_ids - cur_ids
    common = cur_ids & prev_ids

    changed, warnings = [], []
    for uid in common:
        c, p = cur.loc[uid], prev.loc[uid]
        if isinstance(c, pd.DataFrame):
            c = c.iloc[0]
        if isinstance(p, pd.DataFrame):
            p = p.iloc[0]
        delta = {}
        for k in DIFF_KEYS:
            cv, pv = pd.to_numeric(c.get(k), errors="coerce"), pd.to_numeric(p.get(k), errors="coerce")
            if pd.notna(cv) and pd.notna(pv) and abs(cv - pv) > (abs(pv) * 1e-4 + 1e-9):
                delta[k] = {"prev": float(pv), "cur": float(cv)}
        if delta:
            changed.append({"deal_uid": uid, "company": c.get("company"),
                            "bdc": c.get("bdc"), "delta": delta})
        # early warnings — tagged with severity so routine quarterly re-marks (info) can be
        # told apart from genuine credit stress (alert). On a quarter-roll every persisting
        # loan re-marks at once, so the bulk is expected drift; only the tail is alarming.
        cm = _mark(c); pm = _mark(p)
        if pd.notna(cm) and pd.notna(pm) and (pm - cm) > 0.05:
            # alert = current mark sits in the genuine-distress band [0.30, 0.90).
            # Deliberately excludes: near-par marks >=0.90 (normal), fv/cost marks >1.1
            # (unreliable — a small/missing cost inflates the ratio, e.g. 3.0 artifacts), and
            # <0.30 (data error, not a real loan mark). A drop-based rule was rejected:
            # on real quarter-roll data it fired on the garbage highs (3.0 -> 0.99).
            sev = "alert" if (0.30 <= cm < 0.90) else "info"
            # company 键名: diff 输入帧的列叫 "company"(见 run() 里 cur_all 的列选择,
            # 历史帧也已 rename issuer→company)。此前误取 c.get("issuer") 恒为 None,
            # 91 条 alert 全部点不出借款人 —— 2026-08-10 修复,并带上 deal_uid 供回溯。
            warnings.append({"type": "mark_deterioration", "severity": sev,
                             "deal_uid": uid, "company": c.get("company"),
                             "bdc": c.get("bdc"), "from": round(pm, 3), "to": round(cm, 3)})
        cpik = pd.to_numeric(c.get("pik_rate"), errors="coerce")
        ppik = pd.to_numeric(p.get("pik_rate"), errors="coerce")
        if pd.notna(cpik) and (pd.isna(ppik) or cpik > (ppik or 0)) and cpik > 0:
            # PIK turning on/up is a soft signal → info (not a standalone alert)
            warnings.append({"type": "pik_increase", "severity": "info",
                             "deal_uid": uid, "company": c.get("company"),
                             "bdc": c.get("bdc"), "from": (float(ppik) if pd.notna(ppik) else 0.0),
                             "to": float(cpik)})

    def _rows(ids, frame):
        return [{"deal_uid": u, "company": frame.loc[u].get("company")
                 if not isinstance(frame.loc[u], pd.DataFrame) else frame.loc[u].iloc[0].get("company"),
                 "bdc": frame.loc[u].get("bdc") if not isinstance(frame.loc[u], pd.DataFrame)
                 else frame.loc[u].iloc[0].get("bdc"),
                 "fair_value": float(pd.to_numeric(
                     frame.loc[u].get("fair_value") if not isinstance(frame.loc[u], pd.DataFrame)
                     else frame.loc[u].iloc[0].get("fair_value"), errors="coerce") or 0)}
                for u in ids]
    return {"new": _rows(new_ids, cur), "exited": _rows(exit_ids, prev),
            "changed": changed, "warnings": warnings,
            "counts": {"new": len(new_ids), "exited": len(exit_ids),
                       "changed": len(changed), "warnings": len(warnings),
                       # additive severity split (existing 'warnings' kept for back-compat):
                       "warnings_alert": sum(1 for w in warnings if w.get("severity") == "alert"),
                       "warnings_info": sum(1 for w in warnings if w.get("severity") == "info")}}


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
    rates_date = date.today().isoformat()

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
    summary["manifest"] = {t: {"adsh": manifest[t]["adsh"], "reportDate": manifest[t]["reportDate"],
                               "gross_net_ratio": manifest[t].get("gross_net_ratio")}
                           for t in manifest}

    # 3) holdings diff (filing-driven) vs previous snapshots
    prev_paths = _prev_snapshot_paths(store, manifest)
    diff = {"counts": {"new": 0, "exited": 0, "changed": 0, "warnings": 0,
                       "warnings_alert": 0, "warnings_info": 0},
            "note": "no prior snapshot for any BDC (first run)"}
    if prev_paths:
        cur_all = deals[["deal_uid", "company", "bdc"] + DIFF_KEYS].copy()
        prev_frames = []
        for t, p in prev_paths.items():
            pf = pd.read_parquet(p).rename(columns={"issuer": "company"})
            prev_frames.append(pf)
        prev_all = pd.concat(prev_frames, ignore_index=True) if prev_frames else pd.DataFrame()
        # only diff BDCs that actually have a prior snapshot
        bdcs_with_prev = set(prev_paths)
        diff = diff_holdings(cur_all[cur_all["bdc"].isin(bdcs_with_prev)],
                             prev_all[prev_all["bdc"].isin(bdcs_with_prev)])
        diff["bdcs_diffed"] = sorted(bdcs_with_prev)

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
                               "bdc_pnl": last.get("bdc_pnl"), "bdc_dd_pct": last.get("bdc_dd")}
        except Exception:  # noqa: BLE001
            pass

    daily = {
        "date": rates_date, "as_of": as_of,
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
        with open(hb, "a") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} "
                     f"asof={as_of} rates={rates_date} deals={summary['deal_count']} "
                     f"diff={diff['counts']}\n")
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
