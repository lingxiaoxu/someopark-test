"""Plan 09 GATE 2 — small regularized models vs the single-best-feature baseline.

Pre-registered from gate 1 (before any model was fit):
  * baseline: signal = −sign(basis_z) when |basis_z| ≥ 1.0 (basis_z had the top
    |NW-t| in gate 1 on both markets; negative IC → fade the dislocation);
  * models: L2 logistic (standardized, median-imputed) and
    HistGradientBoostingClassifier (max_depth ≤ 3, early stopping) — nothing else;
  * CV: purged K-fold (k=5) with embargo = 12 grid steps (≥ the 60m label
    overlap) via trade_stats.purged_kfold_indices;
  * verdict: a model must beat the baseline OOS on BOTH directional accuracy and
    mean per-signal edge (bps), with the ordering holding on each market
    separately, else the deliverable is the linear baseline signal.

Trial accounting (for gate-3 deflation): gate 1 examined 60 cells; gate 2
examines 12 configs (2 models + baseline) × 2 horizons × 2 markets.

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.ml_directional.model
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.trade_stats import (newey_west_tstat,
                                                      purged_kfold_indices)
from crypto_trading.crypto_strategies.ml_directional.features import (FEATURES, HORIZONS,
                                                                      cached_feature_frame)

logger = logging.getLogger(__name__)

MODEL_FEATURES = FEATURES + ["session"]
BASELINE_Z = 1.0            # pre-registered |basis_z| threshold
EMBARGO = 12                # grid steps; ≥ longest label horizon
K_FOLDS = 5
N_TRIALS_G1 = 60
N_TRIALS_G2 = 12


def _eval_signals(sig: pd.Series, fwd: pd.Series, lags: int) -> dict:
    """OOS metrics on emitted (nonzero) signals: n, hit rate vs realized sign,
    mean edge (bps), NW-t of the edge series."""
    on = sig != 0
    if on.sum() < 30:
        return {"n": int(on.sum()), "acc": np.nan, "edge_bps": np.nan, "t_nw": np.nan}
    s, r = sig[on], fwd[on]
    nonflat = r != 0
    acc = float((np.sign(r[nonflat]) == s[nonflat]).mean()) if nonflat.any() else np.nan
    edge = (s * r * 1e4).astype(float)
    nw = newey_west_tstat(pd.Series(edge.to_numpy()), lags=lags)
    return {"n": int(on.sum()), "acc": round(acc, 4),
            "edge_bps": round(float(edge.mean()), 2), "t_nw": round(nw["t_nw"], 2)}


def _fit_predict(name: str, Xtr: pd.DataFrame, ytr: pd.Series,
                 Xte: pd.DataFrame, seed: int = 0) -> np.ndarray:
    if name == "logit_l2":
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        clf = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                            LogisticRegression(C=1.0, max_iter=2000))
    elif name == "hgb":
        from sklearn.ensemble import HistGradientBoostingClassifier
        clf = HistGradientBoostingClassifier(
            max_depth=3, max_iter=200, learning_rate=0.1,
            early_stopping=True, validation_fraction=0.15, random_state=seed)
    else:
        raise ValueError(name)
    clf.fit(Xtr, ytr)
    pred = clf.predict(Xte)
    return pred.astype(int)


def run_gate2() -> dict:
    results = []
    for ticker in ("KXBTCPERP", "KXETHPERP"):
        frame = cached_feature_frame(ticker)
        for hname, hsteps in HORIZONS.items():
            f = frame.dropna(subset=[f"label_{hname}"])
            X = f[MODEL_FEATURES]
            y = f[f"label_{hname}"].astype(int)
            fwd = f[f"fwd_{hname}"]
            lags = max(hsteps, 6)

            # baseline needs no fitting — evaluate on the SAME pooled test rows
            folds = purged_kfold_indices(len(f), k=K_FOLDS, embargo=EMBARGO)
            test_all = np.sort(np.concatenate([t for _, t in folds]))
            bz = f["basis_z"].to_numpy()
            base_sig = pd.Series(
                np.where(np.abs(bz) >= BASELINE_Z, -np.sign(bz), 0.0),
                index=f.index).iloc[test_all]
            results.append({"ticker": ticker, "horizon": hname, "model": "baseline_basis_z",
                            **_eval_signals(base_sig, fwd.iloc[test_all], lags)})

            for mname in ("logit_l2", "hgb"):
                preds = pd.Series(0, index=f.index, dtype=int)
                for tr, te in folds:
                    preds.iloc[te] = _fit_predict(mname, X.iloc[tr], y.iloc[tr], X.iloc[te])
                sig = preds.iloc[test_all].astype(float)
                results.append({"ticker": ticker, "horizon": hname, "model": mname,
                                **_eval_signals(sig, fwd.iloc[test_all], lags)})
            logger.info("gate2 %s %s done", ticker, hname)

    df = pd.DataFrame(results)
    # verdict: does any model beat the baseline on acc AND edge in EVERY market?
    winners = []
    for mname in ("logit_l2", "hgb"):
        for hname in HORIZONS:
            ok = True
            for ticker in ("KXBTCPERP", "KXETHPERP"):
                b = df[(df.ticker == ticker) & (df.horizon == hname)
                       & (df.model == "baseline_basis_z")].iloc[0]
                m = df[(df.ticker == ticker) & (df.horizon == hname)
                       & (df.model == mname)].iloc[0]
                if not (m.n >= 30 and m.acc > b.acc and m.edge_bps > b.edge_bps):
                    ok = False
            if ok:
                winners.append({"model": mname, "horizon": hname})
    verdict = "PASS" if winners else "FAIL_USE_LINEAR"

    out = {"table": df.to_dict("records"), "winners": winners, "gate2": verdict,
           "baseline": f"-sign(basis_z) @ |z|>={BASELINE_Z}",
           "n_trials_so_far": N_TRIALS_G1 + N_TRIALS_G2}
    art = SIGNALS_DIR / "research"
    art.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (art / f"ml_gate2_{stamp}.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = run_gate2()
    df = pd.DataFrame(out["table"])
    print("=" * 78)
    print("PLAN 09 GATE 2 — purged-CV OOS: models vs baseline "
          f"(baseline = {out['baseline']})")
    print("=" * 78)
    print(df.to_string(index=False))
    print(f"\nwinners (beat baseline on acc AND edge in BOTH markets): {out['winners']}")
    print(f"GATE 2: {out['gate2']}  (trials so far: {out['n_trials_so_far']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
