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


def _first_prints(conn, asof: datetime) -> pd.Series:
    rows = conn.execute(
        "SELECT event_time, value, MIN(vintage_date) FROM fred_obs WHERE sid='UNRATE'"
        " AND knowledge_time<=? GROUP BY event_time ORDER BY event_time",
        (asof.isoformat(),)).fetchall()
    return pd.Series({pd.Timestamp(r["event_time"]): r["value"] for r in rows}, dtype=float)


def predict(conn, asof: datetime, period: str, series: str = "KXU3") -> Pred:
    fs = FeatureStore(conn)
    fp = _first_prints(conn, asof)
    assert len(fp) >= 60, "UNRATE history too short"
    last = float(fp.iloc[-1])
    d = np.round(fp.diff().dropna().tail(180), 1)
    probs = np.array([(np.sum(d == s) + 0.5) for s in STEPS], dtype=float)
    probs /= probs.sum()
    icsa, h_c = fs.fred_series("ICSA", asof)
    c4 = icsa.rolling(4).mean().dropna()
    tilt = 0
    if len(c4) >= 9:
        delta = float(c4.iloc[-1] - c4.iloc[-5])
        tilt = 1 if delta > 8000 else (-1 if delta < -8000 else 0)
    if tilt != 0:                                   # shift 20% of mass one step toward signal
        shifted = probs.copy()
        for i, s in enumerate(STEPS):
            j = i + tilt
            if 0 <= j < len(STEPS):
                mv = probs[i] * 0.2
                shifted[i] -= mv
                shifted[j] += mv
        probs = shifted / shifted.sum()
    # multi-month horizon: the printed month k steps ahead is the k-fold convolution of
    # the monthly Δ distribution (uncertainty widens correctly for far contracts)
    last_month = fp.index.max().to_period("M")
    ahead = max((pd.Period(period) - last_month).n, 1)
    rng = np.random.default_rng(0)                  # deterministic encoding sample
    steps_sum = rng.choice(STEPS, size=(20000, ahead), p=probs).sum(axis=1)
    samples = np.round(np.clip(last + steps_sum, 2.0, 15.0), 1)
    h = conn.execute("SELECT MAX(knowledge_time) m FROM fred_obs WHERE sid='UNRATE'"
                     " AND knowledge_time<=?", (asof.isoformat(),)).fetchone()["m"]
    horizon = max(h or asof.isoformat(), h_c or "")
    return Pred(series="KXU3", period=period, dist=Empirical(tuple(samples.tolist())),
                asof=asof, model_version=VERSION,
                inputs={"last": last, "probs": {f"{s:+.1f}": round(float(p), 4)
                                                for s, p in zip(STEPS, probs)},
                        "claims_tilt": tilt},
                data_horizon=datetime.fromisoformat(horizon))
