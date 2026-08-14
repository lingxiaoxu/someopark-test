"""The decisive test: can PAST data pick cells that keep working? (Plan 11 §D)

Every earlier result was contaminated by MY choice of which cells to examine —
I read the whole-sample atlas, then adjudicated the winners. That is exactly the
selection bias the atlas was meant to avoid. This module removes the analyst
from the loop:

    IS half  → build EVERY (market × feature × horizon × threshold) cell,
               rank by in-sample non-overlapping episode economics,
               commit to the top-K (direction included).
    OOS half → trade exactly those K. No re-ranking, no substitutions.

That is one pre-registered experiment whose result cannot be cherry-picked, and
it is the honest simulation of live operation (choose from history, then live
with the choice). Reported for K ∈ {1,3,5,10}, plus:

  * the RANK CORRELATION between IS and OOS cell economics — the deepest
    diagnostic: if it is ~0, past performance carries NO information about
    future performance in this market, and no selection rule can ever work,
    regardless of sample size.
  * a random-selection control (mean over random K-subsets) so the selection's
    value-add is measured against luck rather than against zero.

Fees: 10bps maker-maker round trip. Episodes are non-overlapping (cooldown =
horizon), so returns are independent draws.
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.trade_stats import newey_west_tstat
from crypto_trading.crypto_strategies.ml_directional.features import (GRID,
                                                                      build_feature_frame)
from crypto_trading.crypto_strategies.research_horizon_atlas import (ALT_FEATURES,
                                                                    ALT_MARKETS,
                                                                    FULL_MARKETS,
                                                                    HORIZON_STEPS,
                                                                    ZWIN,
                                                                    build_alt_frame)
from crypto_trading.crypto_strategies.ml_directional.features import FEATURES

logger = logging.getLogger(__name__)

FEE_MM_BPS = 10.0
THRESHOLDS = (1.0, 1.5, 2.0)
MIN_IS_EPISODES = 10          # a cell must have traded enough IS to be rankable
TOP_K = (1, 3, 5, 10)
RANDOM_DRAWS = 500


def _trailing_z(s: pd.Series) -> pd.Series:
    mu = s.rolling(ZWIN, min_periods=48).mean()
    sd = s.rolling(ZWIN, min_periods=48).std(ddof=0)
    return (s - mu) / sd.replace(0.0, np.nan)


def _episodes(z: pd.Series, fwd: pd.Series, direction: float, q: float,
              k_steps: int) -> pd.DataFrame:
    """Non-overlapping episodes from a (z, fwd) frame."""
    d = pd.DataFrame({"z": z, "fwd": fwd}).dropna()
    hold = pd.Timedelta(GRID) * k_steps
    rows, open_until = [], None
    for ts, r in d.iterrows():
        if abs(r.z) < q or (open_until is not None and ts < open_until):
            continue
        side = direction * np.sign(r.z)
        rows.append({"ts": ts, "net_bps": float(side * r.fwd) - FEE_MM_BPS})
        open_until = ts + hold
    return pd.DataFrame(rows)


def build_cells() -> pd.DataFrame:
    """Every cell, with IS-fitted direction and IS/OOS episode economics."""
    cache: dict = {}
    out = []
    plan = ([(t, FEATURES) for t in FULL_MARKETS]
            + [(t, ALT_FEATURES) for t in ALT_MARKETS])
    for ticker, feats in plan:
        try:
            frame = (build_feature_frame(ticker) if ticker in FULL_MARKETS
                     else build_alt_frame(ticker))
        except Exception as e:                          # noqa: BLE001
            logger.warning("%s skipped: %s", ticker, str(e)[:60])
            continue
        cache[ticker] = frame
        mark = frame["mark_mid"]
        half_ts = frame.index[len(frame) // 2]
        for feat in feats:
            if feat not in frame.columns or frame[feat].dropna().empty:
                continue
            z = _trailing_z(frame[feat])
            for hname, k in HORIZON_STEPS.items():
                fwd = (mark.shift(-k) / mark - 1.0) * 1e4
                d = pd.DataFrame({"z": z, "fwd": fwd}).dropna()
                if len(d) < 400:
                    continue
                is_d, oos_d = d[d.index < half_ts], d[d.index >= half_ts]
                if len(is_d) < 200 or len(oos_d) < 200:
                    continue
                ic_is = is_d.z.corr(is_d.fwd, method="spearman")
                if pd.isna(ic_is) or ic_is == 0:
                    continue
                direction = float(np.sign(ic_is))
                for q in THRESHOLDS:
                    ep_is = _episodes(is_d.z, is_d.fwd, direction, q, k)
                    ep_oos = _episodes(oos_d.z, oos_d.fwd, direction, q, k)
                    if len(ep_is) < MIN_IS_EPISODES or len(ep_oos) < 5:
                        continue
                    out.append({
                        "ticker": ticker, "feature": feat, "horizon": hname,
                        "q": q, "direction": direction,
                        "is_n": len(ep_is), "is_mean": float(ep_is.net_bps.mean()),
                        "is_t": float(newey_west_tstat(ep_is.net_bps.reset_index(drop=True))["t_nw"]),
                        "oos_n": len(ep_oos), "oos_mean": float(ep_oos.net_bps.mean()),
                        "_oos_series": ep_oos.net_bps.tolist(),
                    })
        logger.info("%s cells built (total %d)", ticker, len(out))
    return pd.DataFrame(out)


def evaluate(cells: pd.DataFrame) -> dict:
    res: dict = {"n_cells": len(cells)}
    if cells.empty:
        return res
    # ── the deepest diagnostic: does IS rank predict OOS rank at all? ──
    res["spearman_is_vs_oos_mean"] = round(float(
        cells.is_mean.corr(cells.oos_mean, method="spearman")), 4)
    res["pearson_is_vs_oos_mean"] = round(float(
        cells.is_mean.corr(cells.oos_mean)), 4)
    res["spearman_is_t_vs_oos_mean"] = round(float(
        cells.is_t.corr(cells.oos_mean, method="spearman")), 4)
    res["oos_mean_of_all_cells"] = round(float(cells.oos_mean.mean()), 2)
    res["frac_cells_oos_positive"] = round(float((cells.oos_mean > 0).mean()), 3)

    rng = np.random.default_rng(3)
    sel = {}
    for K in TOP_K:
        if len(cells) < K:
            continue
        top = cells.nlargest(K, "is_mean")
        pooled = np.concatenate([np.asarray(s) for s in top["_oos_series"]])
        nw = newey_west_tstat(pd.Series(pooled))
        # random-K control
        rand_means = []
        for _ in range(RANDOM_DRAWS):
            idx = rng.choice(len(cells), K, replace=False)
            r = np.concatenate([np.asarray(s) for s in cells.iloc[idx]["_oos_series"]])
            rand_means.append(r.mean())
        rand_means = np.array(rand_means)
        sel[f"top{K}"] = {
            "selected": top[["ticker", "feature", "horizon", "q", "is_mean",
                             "is_t", "is_n"]].to_dict("records"),
            "oos_n_episodes": int(pooled.size),
            "oos_mean_net_bps": round(float(pooled.mean()), 2),
            "oos_median_net_bps": round(float(np.median(pooled)), 2),
            "oos_hit_rate": round(float((pooled > 0).mean()), 3),
            "oos_nw_t": round(float(nw["t_nw"]), 2),
            "random_control_mean": round(float(rand_means.mean()), 2),
            "selection_edge_vs_random": round(float(pooled.mean() - rand_means.mean()), 2),
            "pct_of_random_draws_beaten": round(float((pooled.mean() > rand_means).mean()), 3),
        }
    res["selection"] = sel
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cells = build_cells()
    res = evaluate(cells)

    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    dump = {k: v for k, v in res.items()}
    (outdir / f"wf_selection_{stamp}.json").write_text(json.dumps(dump, indent=1, default=str))

    print("=" * 100)
    print("WALK-FORWARD SELECTION — pick on the IS half, live with it on the OOS half")
    print("=" * 100)
    print(f"cells evaluated: {res['n_cells']}")
    if not res.get("selection"):
        print("no cells qualified")
        return 0
    print(f"\nDOES THE PAST PREDICT THE FUTURE HERE?")
    print(f"  Spearman(IS mean, OOS mean)   = {res['spearman_is_vs_oos_mean']:+.3f}")
    print(f"  Pearson (IS mean, OOS mean)   = {res['pearson_is_vs_oos_mean']:+.3f}")
    print(f"  Spearman(IS t,    OOS mean)   = {res['spearman_is_t_vs_oos_mean']:+.3f}")
    print(f"  mean OOS net across ALL cells = {res['oos_mean_of_all_cells']:+.2f} bps "
          f"| {res['frac_cells_oos_positive']:.0%} of cells OOS-positive")
    print("\nSELECTION RESULTS (OOS, fees included):")
    for key, s in res["selection"].items():
        print(f"\n  [{key}] n={s['oos_n_episodes']} episodes | mean {s['oos_mean_net_bps']:+.2f} bps "
              f"| median {s['oos_median_net_bps']:+.2f} | hit {s['oos_hit_rate']:.0%} "
              f"| NW-t {s['oos_nw_t']:+.2f}")
        print(f"        vs random-{key[3:]} control {s['random_control_mean']:+.2f} bps → "
              f"selection edge {s['selection_edge_vs_random']:+.2f} bps "
              f"(beat {s['pct_of_random_draws_beaten']:.0%} of random draws)")
        for c in s["selected"][:5]:
            print(f"        · {c['ticker']:11} {c['feature']:16} {c['horizon']:3} "
                  f"q={c['q']} | IS mean {c['is_mean']:+.1f} t={c['is_t']:+.2f} n={c['is_n']}")
    print("\nREAD: if Spearman(IS,OOS) ≈ 0 and the selection edge over random ≈ 0, then "
          "past cell performance carries no information about future performance — "
          "no amount of extra data makes a selection rule work on this venue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
