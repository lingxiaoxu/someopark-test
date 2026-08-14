"""Where is the gross edge, and does selection improve with more tape? (Plan 11 §E)

The walk-forward selection test left the project 1.3bps short of breakeven with
Spearman(IS,OOS)=+0.065. Two questions decide whether more recording can close
that gap — i.e. whether this venue is worth waiting out:

  1. GROSS EDGE BY HORIZON. Fees are a constant 10bps per round trip, so the
     only thing that matters is where gross per-episode return is largest.
     Reported per horizon over all cells (no selection), plus the fraction of
     cells whose gross clears 10bps.

  2. DOES SELECTION SCALE? Re-run the pick-on-past/live-on-future experiment at
     several IS fractions (40/50/60/70% of the tape). If the IS→OOS rank
     correlation and the top-K edge GROW with IS length, more tape mechanically
     buys a better selection and a date can be put on viability. If they stay
     flat near zero, cell performance is not persistent on this venue and no
     amount of tape fixes it — which is a decisive, money-saving answer.

Same PIT discipline throughout: direction and ranking use only the IS window;
episodes are non-overlapping; economics are net of 10bps maker-maker fees.
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.trade_stats import newey_west_tstat
from crypto_trading.crypto_strategies.ml_directional.features import (FEATURES, GRID,
                                                                      build_feature_frame)
from crypto_trading.crypto_strategies.research_horizon_atlas import (ALT_FEATURES,
                                                                    ALT_MARKETS,
                                                                    FULL_MARKETS,
                                                                    HORIZON_STEPS,
                                                                    ZWIN,
                                                                    build_alt_frame)
from crypto_trading.crypto_strategies.research_wf_selection import (FEE_MM_BPS,
                                                                    MIN_IS_EPISODES,
                                                                    THRESHOLDS,
                                                                    _episodes,
                                                                    _trailing_z)

logger = logging.getLogger(__name__)

IS_FRACTIONS = (0.4, 0.5, 0.6, 0.7)
TOP_K = 10
RANDOM_DRAWS = 300


def load_frames() -> dict:
    frames = {}
    for t in FULL_MARKETS + ALT_MARKETS:
        try:
            frames[t] = (build_feature_frame(t) if t in FULL_MARKETS
                         else build_alt_frame(t))
        except Exception as e:                            # noqa: BLE001
            logger.warning("%s skipped: %s", t, str(e)[:60])
    return frames


def cells_at_split(frames: dict, is_frac: float) -> pd.DataFrame:
    out = []
    for ticker, frame in frames.items():
        feats = FEATURES if ticker in FULL_MARKETS else ALT_FEATURES
        mark = frame["mark_mid"]
        split_ts = frame.index[int(len(frame) * is_frac)]
        for feat in feats:
            if feat not in frame.columns or frame[feat].dropna().empty:
                continue
            z = _trailing_z(frame[feat])
            for hname, k in HORIZON_STEPS.items():
                fwd = (mark.shift(-k) / mark - 1.0) * 1e4
                d = pd.DataFrame({"z": z, "fwd": fwd}).dropna()
                if len(d) < 400:
                    continue
                is_d, oos_d = d[d.index < split_ts], d[d.index >= split_ts]
                if len(is_d) < 150 or len(oos_d) < 150:
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
                        "ticker": ticker, "feature": feat, "horizon": hname, "q": q,
                        "is_n": len(ep_is), "is_mean": float(ep_is.net_bps.mean()),
                        "oos_n": len(ep_oos), "oos_mean": float(ep_oos.net_bps.mean()),
                        "_oos": ep_oos.net_bps.tolist(),
                    })
    return pd.DataFrame(out)


def horizon_profile(cells: pd.DataFrame) -> pd.DataFrame:
    """Gross (= net + fee) economics per horizon over ALL cells — no selection."""
    c = cells.copy()
    c["oos_gross"] = c.oos_mean + FEE_MM_BPS
    g = c.groupby("horizon").agg(
        cells=("oos_mean", "size"),
        median_gross_bps=("oos_gross", "median"),
        mean_gross_bps=("oos_gross", "mean"),
        p90_gross_bps=("oos_gross", lambda s: float(np.percentile(s, 90))),
        frac_gross_over_fee=("oos_gross", lambda s: float((s > FEE_MM_BPS).mean())),
        mean_episodes=("oos_n", "mean"),
    ).reset_index()
    order = list(HORIZON_STEPS)
    g["_o"] = g.horizon.map({h: i for i, h in enumerate(order)})
    return g.sort_values("_o").drop(columns="_o").round(2)


def selection_at_split(cells: pd.DataFrame, k: int = TOP_K) -> dict:
    if len(cells) < k:
        return {}
    rng = np.random.default_rng(5)
    top = cells.nlargest(k, "is_mean")
    pooled = np.concatenate([np.asarray(s) for s in top["_oos"]])
    rand = np.array([np.concatenate([np.asarray(s) for s in
                     cells.iloc[rng.choice(len(cells), k, replace=False)]["_oos"]]).mean()
                     for _ in range(RANDOM_DRAWS)])
    nw = newey_west_tstat(pd.Series(pooled))
    return {
        "n_cells": len(cells),
        "spearman_is_oos": round(float(cells.is_mean.corr(cells.oos_mean, method="spearman")), 4),
        "topk_oos_mean": round(float(pooled.mean()), 2),
        "topk_oos_n": int(pooled.size),
        "topk_nw_t": round(float(nw["t_nw"]), 2),
        "random_mean": round(float(rand.mean()), 2),
        "selection_edge": round(float(pooled.mean() - rand.mean()), 2),
        "all_cell_mean": round(float(cells.oos_mean.mean()), 2),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    frames = load_frames()

    res: dict = {"is_fractions": {}, "fee_mm_bps": FEE_MM_BPS}
    base = None
    for frac in IS_FRACTIONS:
        cells = cells_at_split(frames, frac)
        if cells.empty:
            continue
        s = selection_at_split(cells)
        res["is_fractions"][f"{frac:.0%}"] = s
        logger.info("IS %.0f%% → cells %d, spearman %.3f, top%d %.2f",
                    frac * 100, len(cells), s.get("spearman_is_oos", np.nan),
                    TOP_K, s.get("topk_oos_mean", np.nan))
        if frac == 0.5:
            base = cells
    if base is not None:
        res["horizon_profile"] = horizon_profile(base).to_dict("records")

    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"selection_scaling_{stamp}.json").write_text(json.dumps(res, indent=1, default=str))

    print("=" * 100)
    print("A) GROSS EDGE BY HORIZON (all cells, no selection; fee wall = 10bps RT)")
    print("=" * 100)
    if "horizon_profile" in res:
        print(pd.DataFrame(res["horizon_profile"]).to_string(index=False))
    print("\n" + "=" * 100)
    print(f"B) DOES SELECTION SCALE WITH TAPE? (top-{TOP_K} by IS mean, then held)")
    print("=" * 100)
    rows = [{"is_frac": k, **v} for k, v in res["is_fractions"].items()]
    print(pd.DataFrame(rows)[["is_frac", "n_cells", "spearman_is_oos", "all_cell_mean",
                              "topk_oos_mean", "topk_nw_t", "random_mean",
                              "selection_edge"]].to_string(index=False))
    print("\nREAD: spearman_is_oos rising with is_frac ⇒ more tape buys better selection "
          "(viability has a DATE). Flat near zero ⇒ cell performance is not persistent "
          "here and more tape cannot fix it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
