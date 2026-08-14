"""The stress→2-4h family: pooled hypothesis + power analysis (Plan 11 §C).

The episode test showed 8/12 candidates with POSITIVE net-of-fee means
(+5..+20bps/episode) but n=20..50 episodes — failing on statistical power, not
on economics. Two disciplined responses:

  A) POOL. Most survivors express ONE economic hypothesis: *after a stress
     print (liquidation burst, OI shock, vol spike, sharp 1h move), the 2-4h
     forward return is predictable in the direction fixed in-sample*. Pooling
     episodes across markets under a single pre-registered hypothesis multiplies
     the sample instead of multiplying the trials — the legitimate fix for low
     power (one test, more observations, rather than more tests).
     Pooling is only honest if the per-market directions AGREE; that agreement
     is reported, not assumed, and disagreeing markets are shown separately.

  B) POWER. Given the observed per-episode mean and dispersion, how many
     episodes does a t>=2 verdict need, and at the observed episode rate, how
     many CALENDAR DAYS of recording is that? This converts "we need more data"
     into a date.

Also reported: direction sign per cell (continuation vs reversal — the
economics differ), and a per-UTC-day P&L table so regime dependence is visible.
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.trade_stats import deflated_sharpe, newey_west_tstat
from crypto_trading.crypto_strategies.ml_directional.features import (GRID,
                                                                      build_feature_frame)
from crypto_trading.crypto_strategies.research_horizon_atlas import (HORIZON_STEPS,
                                                                    ZWIN,
                                                                    build_alt_frame)

logger = logging.getLogger(__name__)

FEE_MM_BPS = 10.0
BOOT_N = 4000
FULL = {"KXBTCPERP", "KXETHPERP"}

# ── the pre-registered family: "stress print → 2-4h forward move" ──
# one hypothesis, expressed across every market where the stress proxy exists.
STRESS_FAMILY = [
    ("KXBTCPERP", "okx_liq_15m", "4h", 2.0),
    ("KXETHPERP", "okx_liq_15m", "4h", 2.0),
    ("KXBTCPERP", "okx_liq_60m", "4h", 1.5),
    ("KXETHPERP", "okx_liq_60m", "4h", 1.5),
    ("KXXRPPERP", "oi_delta_60m", "2h", 1.5),
    ("KXSUIPERP", "oi_delta_60m", "2h", 1.5),
    ("KXLTCPERP", "oi_delta_60m", "2h", 1.5),
    ("KXBCHPERP", "oi_delta_60m", "2h", 1.5),
    ("KXDOGEPERP", "oi_delta_60m", "2h", 1.5),
    ("KXSOLPERP", "oi_delta_60m", "2h", 1.5),
]
# separate family: "sharp 1h move → 2h continuation/reversal" across alts
MOM_FAMILY = [(t, "mom_1h", "2h", 2.0) for t in
              ("KXLTCPERP", "KXXRPPERP", "KXSUIPERP", "KXBCHPERP",
               "KXDOGEPERP", "KXSOLPERP", "KXLINKPERP", "KXNEARPERP")]


def _trailing_z(s: pd.Series) -> pd.Series:
    mu = s.rolling(ZWIN, min_periods=48).mean()
    sd = s.rolling(ZWIN, min_periods=48).std(ddof=0)
    return (s - mu) / sd.replace(0.0, np.nan)


def _frame(ticker: str, cache: dict) -> pd.DataFrame:
    if ticker not in cache:
        cache[ticker] = (build_feature_frame(ticker) if ticker in FULL
                         else build_alt_frame(ticker))
    return cache[ticker]


def episode_series(ticker: str, feature: str, horizon: str, q: float,
                   cache: dict) -> pd.DataFrame | None:
    """Non-overlapping OOS-half episodes; direction fixed on the IS half."""
    frame = _frame(ticker, cache)
    if feature not in frame.columns or frame[feature].dropna().empty:
        return None
    k = HORIZON_STEPS[horizon]
    mark = frame["mark_mid"]
    z = _trailing_z(frame[feature])
    fwd = (mark.shift(-k) / mark - 1.0) * 1e4
    d = pd.DataFrame({"z": z, "fwd": fwd}).dropna()
    if len(d) < 300:
        return None
    half = len(d) // 2
    d_is, d_oos = d.iloc[:half], d.iloc[half:]
    ic_is = d_is.z.corr(d_is.fwd, method="spearman")
    if pd.isna(ic_is) or ic_is == 0:
        return None
    direction = float(np.sign(ic_is))

    hold = pd.Timedelta(GRID) * k
    rows, open_until = [], None
    for ts, r in d_oos.iterrows():
        if abs(r.z) < q or (open_until is not None and ts < open_until):
            continue
        sd = direction * np.sign(r.z)
        rows.append({"ts": ts, "ticker": ticker, "feature": feature,
                     "horizon": horizon, "ic_is": float(ic_is),
                     "direction": direction, "side": float(sd),
                     "net_bps": float(sd * r.fwd) - FEE_MM_BPS})
        open_until = ts + hold
    return pd.DataFrame(rows) if rows else None


def power_needed(net: pd.Series, target_t: float = 2.0) -> dict:
    """Episodes needed for t>=target given the observed mean/sd (t = μ/σ·√n)."""
    mu, sd = float(net.mean()), float(net.std(ddof=1))
    if mu <= 0 or sd == 0:
        return {"feasible": False}
    n_need = (target_t * sd / mu) ** 2
    return {"feasible": True, "mean_bps": round(mu, 2), "sd_bps": round(sd, 2),
            "n_needed_for_t2": int(np.ceil(n_need))}


def assess(name: str, family: list, cache: dict) -> dict:
    parts, per_cell = [], []
    for tk, feat, hz, q in family:
        try:
            ep = episode_series(tk, feat, hz, q, cache)
        except Exception as e:                       # noqa: BLE001
            logger.warning("%s %s: %s", tk, feat, str(e)[:60])
            continue
        if ep is None or len(ep) < 5:
            per_cell.append({"ticker": tk, "feature": feat, "n": 0 if ep is None else len(ep),
                             "note": "thin"})
            continue
        parts.append(ep)
        per_cell.append({
            "ticker": tk, "feature": feat, "horizon": hz, "n": len(ep),
            "ic_is": round(float(ep.ic_is.iloc[0]), 4),
            "direction": float(ep.direction.iloc[0]),
            "mean_net_bps": round(float(ep.net_bps.mean()), 2),
            "hit": round(float((ep.net_bps > 0).mean()), 3),
        })
    if not parts:
        return {"family": name, "error": "no episodes"}
    allep = pd.concat(parts).sort_values("ts")

    # direction agreement across markets (pooling is only honest if they agree)
    dirs = [c["direction"] for c in per_cell if "direction" in c]
    agree = float(np.mean([d == np.sign(np.mean(dirs)) for d in dirs])) if dirs else np.nan

    net = allep.net_bps.reset_index(drop=True)
    nw = newey_west_tstat(net)
    dsr = deflated_sharpe(net, n_trials=len(family))
    days = max((allep.ts.max() - allep.ts.min()).days, 1)
    by_day = allep.groupby(allep.ts.dt.date).net_bps.sum()
    tot = float(net.sum())

    # stationary bootstrap over day blocks
    rng = np.random.default_rng(11)
    groups = [g.net_bps.to_numpy() for _, g in allep.groupby(allep.ts.dt.date)]
    boot_p, ci = None, None
    if len(groups) >= 3:
        boots = np.array([np.concatenate([groups[i] for i in
                          rng.integers(0, len(groups), len(groups))]).mean()
                          for _ in range(BOOT_N)])
        boot_p = round(float((boots <= 0).mean()), 4)
        ci = [round(float(np.percentile(boots, 2.5)), 2),
              round(float(np.percentile(boots, 97.5)), 2)]

    out = {
        "family": name, "cells": per_cell, "n_cells_used": len(parts),
        "direction_agreement": round(agree, 3) if agree == agree else None,
        "n_episodes": len(allep), "span_days": days,
        "episodes_per_day": round(len(allep) / days, 2),
        "mean_net_bps": round(float(net.mean()), 2),
        "median_net_bps": round(float(net.median()), 2),
        "hit_rate": round(float((net > 0).mean()), 3),
        "nw_t": round(float(nw["t_nw"]), 2),
        "sharpe_per_episode": round(float(dsr["sharpe"]), 3),
        "psr": round(float(dsr["psr"]), 3), "dsr": round(float(dsr["dsr"]), 3),
        "boot_p_le_zero": boot_p, "boot_mean_ci95": ci,
        "top1_episode_share": round(float(net.max()) / tot, 3) if tot > 0 else None,
        "best_day_share": round(float(by_day.max()) / tot, 3) if tot > 0 else None,
        "n_profitable_days": int((by_day > 0).sum()), "n_days": int(len(by_day)),
        "power": power_needed(net),
    }
    if len(by_day) > 1 and tot > 0:
        ex = allep[allep.ts.dt.date != by_day.idxmax()].net_bps
        out["mean_net_ex_best_day"] = round(float(ex.mean()), 2) if len(ex) else None
    # days of recording implied by the power requirement
    p = out["power"]
    if p.get("feasible") and out["episodes_per_day"] > 0:
        need = p["n_needed_for_t2"]
        out["days_of_tape_for_t2"] = int(np.ceil(need / out["episodes_per_day"]))
        out["extra_days_needed"] = max(0, out["days_of_tape_for_t2"] - days)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cache: dict = {}
    res = {f: assess(f, fam, cache) for f, fam in
           [("stress_2_4h", STRESS_FAMILY), ("mom1h_2h", MOM_FAMILY)]}

    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"stress_family_{stamp}.json").write_text(json.dumps(res, indent=1, default=str))

    for name, r in res.items():
        print("=" * 100)
        print(f"FAMILY: {name}")
        print("=" * 100)
        if "error" in r:
            print(f"  {r['error']}")
            continue
        cells = pd.DataFrame(r["cells"])
        print(cells.to_string(index=False))
        print(f"\n  direction agreement across markets : {r['direction_agreement']} "
              f"(1.0 = every market same sign)")
        print(f"  POOLED  n={r['n_episodes']} episodes over {r['span_days']}d "
              f"({r['episodes_per_day']}/day)")
        print(f"    mean net {r['mean_net_bps']}bps | median {r['median_net_bps']} | "
              f"hit {r['hit_rate']} | NW-t {r['nw_t']}")
        print(f"    SR/episode {r['sharpe_per_episode']} | PSR {r['psr']} | DSR {r['dsr']}")
        print(f"    bootstrap p(mean<=0) {r['boot_p_le_zero']} | CI95 {r['boot_mean_ci95']}")
        print(f"    concentration: top1 {r['top1_episode_share']} | "
              f"best day {r['best_day_share']} | ex-best-day mean "
              f"{r.get('mean_net_ex_best_day')} | profitable days "
              f"{r['n_profitable_days']}/{r['n_days']}")
        p = r["power"]
        if p.get("feasible"):
            print(f"    POWER: μ={p['mean_bps']}bps σ={p['sd_bps']} → need "
                  f"n={p['n_needed_for_t2']} episodes for t=2 "
                  f"→ {r.get('days_of_tape_for_t2')}d of tape "
                  f"({r.get('extra_days_needed')}d MORE than we have)")
        else:
            print("    POWER: mean<=0 — more data cannot rescue this family")
    print("\nNOTE: pooling is ONE pre-registered hypothesis across markets (more "
          "observations), not more trials. DSR still deflates by the cell count.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
