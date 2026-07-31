"""model/claims.py — weekly initial jobless claims (PLAN §7; the M1 vessel).

Target: DoL initial claims, SA, ADVANCE (first) print — settles Kalshi KXJOBLESSCLAIMS
('At least X', >=). Model claims/0.1.0:

  level:    weighted mean of the last 4 VISIBLE weekly values in logs (0.4/0.3/0.2/0.1)
  seasonal: week-of-year log-deviation from its trailing 4-week mean, estimated PIT from
            the prior 10 years of FIRST prints (distortion weeks: Jul 4 retooling,
            Thanksgiving, Christmas/New Year — estimated for every week, applied always)
  sigma:    MAD-robust std of the last 26 weekly log first-print changes (floored 2%)
  output:   single-component GaussianMix in LEVEL space, discretised on the 250 grid.

All data via FeatureStore (PIT-filtered vintage reads); labels are first prints by
construction (earliest vintage per week). predict(asof) is deterministic given the db.
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd

from prediction_market_macro.model.common import GaussianMix, Pred, grid_pmf
from prediction_market_macro.model.features import FeatureStore

VERSION = "claims/0.1.0"

# defaults == the registered claims/0.1.0 behaviour; the walk-forward grid
# (research/param_grid.py) passes overrides — the default path NEVER changes without
# a version bump (health replay canary depends on it).
DEFAULT_PARAMS = {
    "level_weights": (0.1, 0.2, 0.3, 0.4),   # oldest→newest of the last 4 weeks
    "seasonal_years": 10,
    "seasonal_clip": 0.25,
    "vol_window": 26,
    "sigma_floor": 0.02,
}


def _first_prints(conn, asof: datetime) -> pd.Series:
    """First-release ICSA per week, restricted to vintages visible at asof (PIT)."""
    rows = conn.execute(
        "SELECT event_time, value, MIN(vintage_date) FROM fred_obs "
        "WHERE sid='ICSA' AND knowledge_time<=? GROUP BY event_time ORDER BY event_time",
        (asof.isoformat(),)).fetchall()
    return pd.Series({pd.Timestamp(r["event_time"]): r["value"] for r in rows}, dtype=float)


def predict(conn, asof: datetime, period: str, series: str = "KXJOBLESSCLAIMS",
            params: dict | None = None) -> Pred:
    """period: release date ISO (calendar key). Predicts the NEXT advance print at asof."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    fs = FeatureStore(conn)
    latest_visible, horizon = fs.fred_series("ICSA", asof)
    if len(latest_visible) < 60:
        raise RuntimeError(f"claims history too short at {asof} (n={len(latest_visible)})")
    first = _first_prints(conn, asof)

    lw = list(p["level_weights"])
    logs = np.log(latest_visible.tail(len(lw)).values)
    w = np.array(lw[-len(logs):])
    base_log = float(np.sum(logs * w) / np.sum(w))

    # PIT seasonal: for the TARGET week (the reference week ends the Saturday before the
    # release date), mean log-deviation of that ISO week's first print vs its trailing
    # 4-week mean over the last 10 years of first prints.
    target_week = (pd.Timestamp(period) - pd.Timedelta(days=5)).isocalendar().week
    fp = np.log(first)
    trail = fp.rolling(4).mean().shift(1)
    dev = (fp - trail).dropna()
    weeks = pd.Series(dev.index.isocalendar().week.values, index=dev.index)
    hist = dev[(weeks == target_week)].tail(10)
    seasonal = float(hist.mean()) if len(hist) >= 3 else 0.0
    seasonal = float(np.clip(seasonal, -0.25, 0.25))

    d_log = np.diff(np.log(first.tail(27).values))
    sigma_log = max(1.4826 * float(np.median(np.abs(d_log - np.median(d_log)))), 0.02)

    mu = math.exp(base_log + seasonal)
    sigma = mu * sigma_log
    dist = GaussianMix(((1.0, mu, sigma),))
    return Pred(series="KXJOBLESSCLAIMS", period=period, dist=dist, asof=asof,
                model_version=VERSION,
                inputs={"base_log": round(base_log, 5), "seasonal": round(seasonal, 5),
                        "sigma_log": round(sigma_log, 5), "mu": round(mu, 1),
                        "n_hist_weeks": len(hist), "target_week": int(target_week)},
                data_horizon=datetime.fromisoformat(horizon))


def ladder(pred: Pred, grid_step: float = 250.0) -> dict[float, float]:
    return grid_pmf(pred.dist, grid_step)
