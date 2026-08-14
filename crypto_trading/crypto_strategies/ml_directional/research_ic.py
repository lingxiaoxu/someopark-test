"""Plan 09 GATE 1 — per-feature IC study with autocorrelation-robust significance.

For each (feature, horizon, market): Spearman IC between feature_t and the
forward return, with an NW-t computed on the demeaned rank-product series
(robust to the heavy overlap of 15/60-min labels on a 5-min grid — the NW lag
is forced ≥ the label horizon). Multiple-testing discipline: ~60 cells are
examined; the report flags which clear |t|≥2 raw AND which survive a
Bonferroni-style 60-cell bar (|t| ≥ ~3.2, the honest deflated line).

Gate verdict: PASS if ≥2 non-momentum features clear the raw bar with
consistent signs across both markets (features we'd actually keep), else FAIL.

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.ml_directional.research_ic
        [--refresh]
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.trade_stats import newey_west_tstat
from crypto_trading.crypto_strategies.ml_directional.features import (FEATURES, HORIZONS,
                                                                      cached_feature_frame)

logger = logging.getLogger(__name__)

N_CELLS = len(FEATURES) * len(HORIZONS) * 2          # the deflation denominator
RAW_BAR = 2.0
DEFLATED_BAR = 3.2                                    # ≈ two-sided Bonferroni for ~60 cells


def ic_cell(f: pd.DataFrame, feature: str, horizon: str,
            min_n: int = 500) -> dict | None:
    x = f[feature]
    y = f[f"fwd_{horizon}"]
    ok = x.notna() & y.notna()
    if ok.sum() < min_n:
        return None
    xr = x[ok].rank(pct=True) - 0.5
    yr = y[ok].rank(pct=True) - 0.5
    ic = float(np.corrcoef(xr, yr)[0, 1])
    # NW-t on the rank-product series; lags at least the label overlap length
    prod = (xr * yr) / (xr.std(ddof=0) * yr.std(ddof=0))
    lags = max(HORIZONS[horizon], int(4 * (len(prod) / 100) ** (2 / 9)))
    nw = newey_west_tstat(pd.Series(prod.to_numpy()), lags=lags)
    return {"feature": feature, "horizon": horizon, "n": int(ok.sum()),
            "ic": round(ic, 4), "t_nw": round(nw["t_nw"], 2), "lags": lags}


def run_gate1(refresh: bool = False) -> dict:
    rows = []
    for ticker in ("KXBTCPERP", "KXETHPERP"):
        f = cached_feature_frame(ticker, refresh=refresh)
        for feat in FEATURES:
            for h in HORIZONS:
                c = ic_cell(f, feat, h)
                if c:
                    c["ticker"] = ticker
                    rows.append(c)
    df = pd.DataFrame(rows)
    df["raw_sig"] = df.t_nw.abs() >= RAW_BAR
    df["deflated_sig"] = df.t_nw.abs() >= DEFLATED_BAR

    # gate verdict: ≥2 NON-momentum features, raw-significant, sign-consistent
    non_mom = df[~df.feature.str.startswith("mom_") & df.raw_sig]
    keep = []
    for feat in non_mom.feature.unique():
        sub = df[(df.feature == feat) & df.raw_sig]
        signs = set(np.sign(sub.ic))
        markets = set(sub.ticker)
        if len(signs) == 1 and len(markets) >= 1:
            keep.append({"feature": feat, "cells": len(sub),
                         "both_markets": len(markets) == 2,
                         "sign": int(list(signs)[0]),
                         "best_t": float(sub.t_nw.abs().max())})
    verdict = "PASS" if len(keep) >= 2 else "FAIL"

    out = {"n_cells_examined": N_CELLS, "raw_bar": RAW_BAR,
           "deflated_bar": DEFLATED_BAR, "table": df.to_dict("records"),
           "kept_features": keep, "gate1": verdict}
    art = SIGNALS_DIR / "research"
    art.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (art / f"ml_gate1_{stamp}.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = run_gate1(refresh=args.refresh)
    df = pd.DataFrame(out["table"])
    print("=" * 78)
    print(f"PLAN 09 GATE 1 — feature IC ({out['n_cells_examined']} cells examined; "
          f"raw bar |t|>={RAW_BAR}, deflated bar |t|>={DEFLATED_BAR})")
    print("=" * 78)
    with pd.option_context("display.width", 120):
        print(df.sort_values("t_nw", key=abs, ascending=False)
                .head(20).to_string(index=False))
    print(f"\nraw-significant cells: {int(df.raw_sig.sum())}/{len(df)} | "
          f"deflated-significant: {int(df.deflated_sig.sum())}")
    print(f"kept (non-momentum, sign-consistent): "
          f"{[k['feature'] for k in out['kept_features']]}")
    print(f"GATE 1: {out['gate1']}")
    return 0 if out["gate1"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
