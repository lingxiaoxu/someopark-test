"""Episode-level adjudication of the horizon-atlas hits (Plan 11 §B).

The atlas reported n=121..605 "signals" per cell — but on a 5-min grid a 4h
horizon overlaps 48-fold, and the OOS half is only ~10.6 days, i.e. AT MOST
~63 independent 4h windows. Overlapping bars inflate every count and flatter
every t-stat. This module re-adjudicates each candidate honestly:

  1. EPISODES, not bars: a signal starts a trade only if no trade is open
     (cooldown = the horizon itself) → strictly non-overlapping returns.
  2. CONCENTRATION: what fraction of P&L comes from the single best episode
     and the best UTC day? An "edge" that is one crash is not an edge.
  3. BLOCK BOOTSTRAP: stationary bootstrap over day-blocks (preserves
     intraday autocorrelation and regime clustering) → empirical p-value that
     does not assume independence.
  4. DEFLATION: DSR across the number of candidate cells adjudicated.
  5. DAY-BY-DAY: mean net per episode per UTC day, so regime dependence is
     visible rather than averaged away.

Direction for every cell is inherited from the atlas protocol (fixed on the
FIRST half); everything here is measured on the SECOND half only.
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
BOOT_N = 2000

# candidates = the economically coherent clusters from the atlas
CANDIDATES = [
    # (ticker, feature, horizon, z_threshold)
    ("KXBTCPERP", "okx_liq_15m", "4h", 2.0),
    ("KXBTCPERP", "okx_liq_60m", "4h", 1.5),
    ("KXBTCPERP", "okx_liq_60m", "2h", 1.5),
    ("KXETHPERP", "okx_liq_60m", "4h", 1.5),
    ("KXETHPERP", "okx_liq_15m", "4h", 2.0),
    ("KXXRPPERP", "oi_delta_60m", "2h", 1.5),
    ("KXXRPPERP", "oi_delta_15m", "4h", 2.0),
    ("KXXRPPERP", "vol_pct_24h", "2h", 2.0),
    ("KXSUIPERP", "oi_delta_60m", "2h", 1.5),
    ("KXLTCPERP", "mom_1h", "2h", 2.0),
    ("KXLTCPERP", "mom_15m", "2h", 2.0),
    ("KXLTCPERP", "flow_imb_60m", "30m", 1.5),
]

FULL = {"KXBTCPERP", "KXETHPERP"}


def _trailing_z(s: pd.Series) -> pd.Series:
    mu = s.rolling(ZWIN, min_periods=48).mean()
    sd = s.rolling(ZWIN, min_periods=48).std(ddof=0)
    return (s - mu) / sd.replace(0.0, np.nan)


def _frame(ticker: str, cache: dict) -> pd.DataFrame:
    if ticker not in cache:
        cache[ticker] = (build_feature_frame(ticker) if ticker in FULL
                         else build_alt_frame(ticker))
    return cache[ticker]


def episodes_for(ticker: str, feature: str, horizon: str, q: float,
                 cache: dict) -> dict:
    frame = _frame(ticker, cache)
    if feature not in frame.columns:
        return {"error": f"{feature} missing"}
    k = HORIZON_STEPS[horizon]
    mark = frame["mark_mid"]
    z = _trailing_z(frame[feature])
    fwd = (mark.shift(-k) / mark - 1.0) * 1e4
    d = pd.DataFrame({"z": z, "fwd": fwd}).dropna()
    if len(d) < 300:
        return {"error": "thin"}

    half = len(d) // 2
    d_is, d_oos = d.iloc[:half], d.iloc[half:]
    ic_is = d_is.z.corr(d_is.fwd, method="spearman")
    if pd.isna(ic_is) or ic_is == 0:
        return {"error": "no IS direction"}
    direction = float(np.sign(ic_is))

    # ── non-overlapping episodes on the OOS half ──
    hold = pd.Timedelta(GRID) * k
    rows, open_until = [], None
    for ts, r in d_oos.iterrows():
        if abs(r.z) < q:
            continue
        if open_until is not None and ts < open_until:
            continue                      # a trade is already running
        net_dir = direction * np.sign(r.z)
        rows.append({"ts": ts, "dir": net_dir,
                     "gross_bps": float(net_dir * r.fwd),
                     "net_bps": float(net_dir * r.fwd) - FEE_MM_BPS})
        open_until = ts + hold
    ep = pd.DataFrame(rows)
    out = {"ticker": ticker, "feature": feature, "horizon": horizon, "q": q,
           "direction": direction, "ic_is": round(float(ic_is), 4),
           "n_episodes": len(ep)}
    if len(ep) < 8:
        out["verdict"] = "INSUFFICIENT_EPISODES"
        return out

    net = ep["net_bps"]
    days = max((ep.ts.max() - ep.ts.min()).days, 1)
    out.update({
        "span_days": days,
        "episodes_per_day": round(len(ep) / days, 2),
        "mean_net_bps": round(float(net.mean()), 2),
        "median_net_bps": round(float(net.median()), 2),
        "hit_rate": round(float((net > 0).mean()), 3),
        "nw_t": round(float(newey_west_tstat(net.reset_index(drop=True))["t_nw"]), 2),
    })
    # ── concentration ──
    tot = float(net.sum())
    best = float(net.max())
    by_day = net.groupby(ep.ts.dt.date).sum()
    out["total_net_bps"] = round(tot, 1)
    out["top1_episode_share"] = round(best / tot, 3) if tot > 0 else None
    out["best_day_share"] = round(float(by_day.max()) / tot, 3) if tot > 0 else None
    out["n_profitable_days"] = int((by_day > 0).sum())
    out["n_days_traded"] = int(len(by_day))
    # drop-the-best-day robustness
    if len(by_day) > 1 and tot > 0:
        worst_case = net[ep.ts.dt.date != by_day.idxmax()]
        out["mean_net_ex_best_day"] = round(float(worst_case.mean()), 2) if len(worst_case) else None
    # ── stationary block bootstrap over DAY blocks ──
    rng = np.random.default_rng(7)
    day_groups = [g["net_bps"].to_numpy() for _, g in ep.groupby(ep.ts.dt.date)]
    if len(day_groups) >= 3:
        boots = []
        for _ in range(BOOT_N):
            pick = rng.integers(0, len(day_groups), len(day_groups))
            sample = np.concatenate([day_groups[i] for i in pick])
            boots.append(sample.mean())
        boots = np.array(boots)
        out["boot_mean_ci95"] = [round(float(np.percentile(boots, 2.5)), 2),
                                 round(float(np.percentile(boots, 97.5)), 2)]
        out["boot_p_le_zero"] = round(float((boots <= 0).mean()), 4)
    out["_net_series"] = net.tolist()          # consumed by the deflation pass
    return out


def run(candidates=CANDIDATES) -> dict:
    cache: dict = {}
    results = []
    for tk, feat, hz, q in candidates:
        try:
            r = episodes_for(tk, feat, hz, q, cache)
        except Exception as e:                    # noqa: BLE001 - research script
            r = {"ticker": tk, "feature": feat, "horizon": hz, "q": q,
                 "error": str(e)[:80]}
        results.append(r)
        logger.info("%s %s %s q=%.1f → %s", tk, feat, hz, q,
                    r.get("verdict") or r.get("error") or
                    f"n={r.get('n_episodes')} mean={r.get('mean_net_bps')}")
    # deflation across the adjudicated set (n_trials = cells adjudicated)
    ok = [r for r in results if r.get("n_episodes", 0) >= 8 and "_net_series" in r]
    for r in ok:
        d = deflated_sharpe(pd.Series(r["_net_series"]), n_trials=len(ok))
        r["sharpe_per_episode"] = round(float(d["sharpe"]), 3)
        r["psr"] = round(float(d["psr"]), 3)
        r["dsr"] = round(float(d["dsr"]), 3)
    for r in results:
        r.pop("_net_series", None)
    return {"results": results, "n_candidates": len(candidates),
            "fee_mm_bps": FEE_MM_BPS, "bootstrap_draws": BOOT_N}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    res = run()
    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"episode_test_{stamp}.json").write_text(json.dumps(res, indent=1, default=str))

    df = pd.DataFrame([r for r in res["results"] if "mean_net_bps" in r])
    print("=" * 110)
    print("EPISODE-LEVEL ADJUDICATION — non-overlapping trades, OOS half only, "
          f"fees {FEE_MM_BPS}bps maker-maker RT")
    print("=" * 110)
    if df.empty:
        print("no candidate produced >=8 non-overlapping episodes — "
              "the atlas hits were overlap artifacts.")
        return 0
    cols = ["ticker", "feature", "horizon", "q", "n_episodes", "episodes_per_day",
            "mean_net_bps", "hit_rate", "nw_t", "dsr", "top1_episode_share",
            "best_day_share", "mean_net_ex_best_day", "boot_p_le_zero"]
    print(df[[c for c in cols if c in df.columns]].to_string(index=False))
    print("\nREAD: a cell is credible only if — mean_net>0 AND nw_t>=2 AND "
          "boot_p<=0.05 AND top1_share<0.5 AND best_day_share<0.6 AND "
          "mean_net_ex_best_day>0 AND dsr>=0.9")
    surv = df[(df.mean_net_bps > 0) & (df.nw_t >= 2.0)
              & (df.get("boot_p_le_zero", 1) <= 0.05)
              & (df.get("top1_episode_share", 1) < 0.5)
              & (df.get("best_day_share", 1) < 0.6)
              & (df.get("mean_net_ex_best_day", -1) > 0)
              & (df.get("dsr", 0) >= 0.9)]
    print(f"\nSURVIVORS: {len(surv)}")
    if len(surv):
        print(surv[[c for c in cols if c in surv.columns]].to_string(index=False))
    else:
        print("none — see which criterion each candidate fails above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
