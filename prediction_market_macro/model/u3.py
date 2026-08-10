"""model/u3.py — unemployment rate on the 0.1 grid (PLAN §7). u3/0.1.0

Discrete by nature: the print is the last FIRST-print value plus a step Δ ∈ {−0.3..+0.3}.
Empirical Δ distribution from the last 180 months of first prints (PIT), Laplace-smoothed,
tilted by the claims 4-week direction (±20% mass shift one step toward the claims signal).
Output: Empirical dist encoded on the exact grid via a large sample (grid_pmf-safe).
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from prediction_market_macro.model.common import Empirical, Pred
from prediction_market_macro.model.features import FeatureStore

VERSION = "u3/0.1.0"
STEPS = np.array([-0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3])

# defaults == the registered u3/0.1.0 behaviour. Every key must be able to MOVE the
# output — see tests/test_claims_params.py. _N_SAMPLES is deliberately NOT a param:
# it is the encoding resolution, so varying it would feed the grid pure Monte-Carlo
# noise and the argmin would happily chase it.
DEFAULT_PARAMS = {
    "hist_months": 180,      # months of first-print steps the empirical dist is built from
    "laplace": 0.5,          # add-k smoothing on each step bucket
    "tilt_frac": 0.2,        # share of mass moved one step toward the claims signal
    "tilt_threshold": 8000,  # 4wk-claims change (jobs) needed before any tilt is applied
}
_N_SAMPLES = 20_000


def _first_prints(conn, asof: datetime) -> pd.Series:
    from prediction_market_macro.model.features import FeatureStore
    s, _ = FeatureStore(conn).fred_first_prints("UNRATE", asof)
    return s


def predict(conn, asof: datetime, period: str, series: str = "KXU3",
            params: dict | None = None) -> Pred:
    p = {**DEFAULT_PARAMS, **(params or {})}
    fs = FeatureStore(conn)
    fp = _first_prints(conn, asof)
    assert len(fp) >= 60, "UNRATE history too short"
    last = float(fp.iloc[-1])
    d = np.round(fp.diff().dropna().tail(int(p["hist_months"])), 1)
    probs = np.array([(np.sum(d == s) + float(p["laplace"])) for s in STEPS], dtype=float)
    probs /= probs.sum()
    icsa, h_c = fs.fred_series("ICSA", asof)
    c4 = icsa.rolling(4).mean().dropna()
    tilt = 0
    if len(c4) >= 9:
        delta = float(c4.iloc[-1] - c4.iloc[-5])
        thr = float(p["tilt_threshold"])
        tilt = 1 if delta > thr else (-1 if delta < -thr else 0)
    if tilt != 0:                          # shift tilt_frac of mass one step toward signal
        shifted = probs.copy()
        frac = float(p["tilt_frac"])
        for i, s in enumerate(STEPS):
            j = i + tilt
            if 0 <= j < len(STEPS):
                mv = probs[i] * frac
                shifted[i] -= mv
                shifted[j] += mv
        probs = shifted / shifted.sum()
    # multi-month horizon: the printed month k steps ahead is the k-fold convolution of
    # the monthly Δ distribution (uncertainty widens correctly for far contracts)
    last_month = fp.index.max().to_period("M")
    ahead = max((pd.Period(period) - last_month).n, 1)
    rng = np.random.default_rng(0)                  # deterministic encoding sample
    steps_sum = rng.choice(STEPS, size=(_N_SAMPLES, ahead), p=probs).sum(axis=1)
    samples = np.round(np.clip(last + steps_sum, 2.0, 15.0), 1)
    _, h = FeatureStore(conn).fred_first_prints("UNRATE", asof)
    horizon = max(h or asof.isoformat(), h_c or "")
    return Pred(series="KXU3", period=period, dist=Empirical(tuple(samples.tolist())),
                asof=asof, model_version=VERSION,
                inputs={"last": last, "probs": {f"{s:+.1f}": round(float(p), 4)
                                                for s, p in zip(STEPS, probs)},
                        "claims_tilt": tilt},
                data_horizon=datetime.fromisoformat(horizon))
