"""model/claims.py — weekly initial jobless claims (PLAN §7; the M1 vessel).

Target: DoL initial claims, SA, ADVANCE (first) print — settles Kalshi KXJOBLESSCLAIMS
('At least X', >=). Model claims/0.2.0:

  level:    weighted mean of the last 4 VISIBLE weekly values in logs (0.4/0.3/0.2/0.1)
  seasonal: week-of-year log-deviation from its trailing 4-week mean, estimated PIT from
            the prior 10 years of FIRST prints (distortion weeks: Jul 4 retooling,
            Thanksgiving, Christmas/New Year — estimated for every week, applied always),
            centred by an outlier-SCREENED mean (0.2.0; see below)
  sigma:    MAD-robust std of the last 26 weekly log first-print changes (floored 2%)
  output:   single-component GaussianMix in LEVEL space, discretised on the 250 grid.

All data via FeatureStore (PIT-filtered vintage reads); labels are first prints by
construction (earliest vintage per week). predict(asof) is deterministic given the db.

0.2.0 (#197, PR-11) — THE SEASONAL CENTRE IS SCREENED, NOT AVERAGED
-------------------------------------------------------------------
0.1.0 took a plain MEAN over a 10-year window, and March-April 2020 is inside it. ICSA
went from ~230k to 3.3M in ISO week 12 of 2020 and 6.9M in week 13, so the log-deviations
those weeks contribute are +2.66 and +2.69 — fourteen times the level, against nine other
years that sit inside ±9%. A mean cannot survive that: week 12 read +0.2560 where its
median reads −0.0109, week 13 +0.2550 against −0.0152.

`seasonal_clip` was never a defence against this, it is the SIZE OF THE DAMAGE THAT
SURVIVES CLIPPING — both weeks clipped at 0.25 and still multiplied mu by 1.284, and the
two worst misses in the whole scored history are exactly those two events (z = −3.41 and
−3.72, mu 269k/269k against prints near 210k). Weeks 14, 15 and 18 never reached the clip
at all (+0.1652, +0.0537, −0.0320) and so applied x1.18/x1.06/x0.97 leaving no trace.

Forward-looking, not a historical curiosity: the observation stays inside a 10-year window
until 2030 — until 2035 at the `seasonal_years=15` the argmin lane has adopted before —
and it reaches five of 53 ISO weeks, so four to five releases a year.

WHY `mad_screen:10` AND NOT THE ARM THAT SCORED BEST. On the 45 scored events the ranking
is mad_screen:6 (+0.379 nats/event) > median (+0.373) > trimmed (+0.345) > mad_screen:10
(+0.310). `mad_screen:10` is adopted anyway, because #192's rule is that a constant may
not be chosen by a scan. 10 is derived from the measured shape of the data instead: over
3,107 weekly deviations back to 1967 the MAD-scale is 0.0435, the largest deviation that
is not COVID is 7.64 MAD (1977-02-05) and 7.21 MAD inside a live window (2022-01-15),
while COVID's peak is 61.8 — but its DECAY, May/June 2020, sits at 7.8..10.1 MAD and
OVERLAPS the real range. The only clean gap is above 16.9 MAD. So any k in (7.7, 16.8)
separates "everything real" from "the explosion"; k=6 does not, it discards genuine
2021-22 observations, and its higher score is exactly the kind of reason #192 forbids.
The other three arms stay in `research/param_space.CANDIDATES` so the DSR-deflated
walk-forward can still overrule this on evidence.

The screen is also the only arm that is SURGICAL: it moves 5 of 45 scored events and
leaves the other 40 byte-identical, where `median` moves all 45 and wins only 26. That is
what makes it a data-quality fix rather than a tuning knob — and it is why PR-1, whose
three counted weeks are ISO 32/33/34, is numerically untouched by this version bump.

None of it is echoed into `Pred.inputs`, deliberately: #196 identifies a branch by the
SET of input keys, so adding a key here would make every prediction written after this
commit look like a different branch from the ones already in `preds`, and the parity
veto would block adoption for as long as the old rows live. Which parameters were in
force is already recorded, PIT, by `param_select.params_asof()` — that is where params
belong. `inputs` carries derived quantities, which is why `vol_window` and
`seasonal_years` are not in it either.
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd

from prediction_market_macro.model.common import GaussianMix, Pred, grid_pmf
from prediction_market_macro.model.features import FeatureStore

VERSION = "claims/0.2.0"

# defaults == the registered claims/0.2.0 behaviour; the walk-forward grid
# (research/param_grid.py) passes overrides — the default path NEVER changes without
# a version bump (health replay canary depends on it).
DEFAULT_PARAMS = {
    "level_weights": (0.1, 0.2, 0.3, 0.4),   # oldest→newest of the last 4 weeks
    "seasonal_years": 10,
    "seasonal_clip": 0.25,
    "vol_window": 26,
    "sigma_floor": 0.02,
    "seasonal_estimator": "mad_screen:10",   # mean | median | trimmed | mad_screen:<k>
}

_ESTIMATORS = ("mean", "median", "trimmed", "mad_screen")


def _seasonal_centre(hist, dev, p: dict) -> float:
    """Centre of one ISO week's log-deviations. `hist` is that week's years, `dev` is
    every week's, used only for a robust scale.

    The four are ordered by how much they assume. `mean` assumes the window is clean.
    `median` assumes nothing and pays for it — at n=10 it is ~64% as efficient as the
    mean when the window IS clean, which is 48 of the 53 weeks. `trimmed` drops one at
    each end, so it survives a single 2020 while keeping 8 of 10 years. `mad_screen:k`
    is the only one that says what it means — a deviation fourteen times the level is
    not a seasonal — and the only one that can cost nothing on a clean week, because on
    a clean week it screens nothing and returns the mean.

    THE THRESHOLD IS FUSED INTO THIS ONE STRING RATHER THAN BEING ITS OWN PARAMETER,
    and that is a `param_space` constraint, not a style choice. `live_keys` proves a key
    is alive by perturbing it ALONE against the defaults; a separate `seasonal_screen_k`
    probed while the estimator is still `mean` moves no prediction anywhere, so the grid
    builder would file it as dead and drop it — and the one parameter this module most
    needs searched would be the one it never searches. A parameter that is only alive
    conditionally is not a parameter that check can see.
    """
    spec = str(p.get("seasonal_estimator", "mean"))
    est, _, k_txt = spec.partition(":")
    if est not in _ESTIMATORS or (est == "mad_screen") != bool(k_txt):
        raise ValueError(f"seasonal_estimator {spec!r}: expected one of"
                         f" {_ESTIMATORS[:3]} or 'mad_screen:<k>'")
    v = np.asarray(hist, dtype=float)
    if est == "mean":
        return float(v.mean())
    if est == "median":
        return float(np.median(v))
    if est == "trimmed":
        s = np.sort(v)
        return float(np.mean(s[1:-1])) if len(s) >= 5 else float(np.median(s))
    # mad_screen. The scale comes from EVERY week's deviations (3,107 of them, back to
    # 1967 — scale 0.0435), not from this week's ten. Not because ten points cannot see
    # COVID: they can, week 12's own MAD puts 2020 at 73 sigma. Because a MAD of ten
    # points is itself extremely noisy, so the threshold would wobble week to week — a
    # week whose nine clean years happen to cluster tightly gets a tiny scale and starts
    # screening out ORDINARY variation. A +0.15 deviation is unremarkable (real ones
    # reach 0.33) and would be 3.4 sigma globally but 68 sigma inside such a week.
    d = np.asarray(dev, dtype=float)
    med = float(np.median(d))
    mad = 1.4826 * float(np.median(np.abs(d - med)))
    if mad <= 0:
        return float(v.mean())
    keep = v[np.abs(v - med) <= float(k_txt) * mad]
    # Falling back to the median rather than to the mean: if a screen this wide rejects
    # more than half the window, the window is not a clean sample with one bad point.
    return float(keep.mean()) if len(keep) >= 3 else float(np.median(v))


def _first_prints(conn, asof: datetime) -> pd.Series:
    """First-release ICSA per week, PIT via the single data door (§5-bis.4-1)."""
    from prediction_market_macro.model.features import FeatureStore
    s, _ = FeatureStore(conn).fred_first_prints("ICSA", asof)
    return s


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
    # 4-week mean over the last `seasonal_years` years of first prints.
    target_week = (pd.Timestamp(period) - pd.Timedelta(days=5)).isocalendar().week
    fp = np.log(first)
    trail = fp.rolling(4).mean().shift(1)
    dev = (fp - trail).dropna()
    weeks = pd.Series(dev.index.isocalendar().week.values, index=dev.index)
    hist = dev[(weeks == target_week)].tail(int(p["seasonal_years"]))
    seasonal = _seasonal_centre(hist, dev, p) if len(hist) >= 3 else 0.0
    clip = float(p["seasonal_clip"])
    seasonal = float(np.clip(seasonal, -clip, clip))

    # vol_window counts DIFFERENCES, so it needs one more level than that to difference.
    d_log = np.diff(np.log(first.tail(int(p["vol_window"]) + 1).values))
    sigma_log = max(1.4826 * float(np.median(np.abs(d_log - np.median(d_log)))),
                    float(p["sigma_floor"]))

    ercot_shift = 0.0
    # PR-33: the PJM+ERCOT weather-severity covariate, same gate shape, same default 0.
    wp = float(p.get("pjm_w", 0.0))
    if wp:
        from prediction_market_macro.model import pjm_cov
        ercot_shift += wp * pjm_cov.mu_shift(conn, asof, "KXJOBLESSCLAIMS")
    w = float(p.get("ercot_w", 0.0))
    if w:
        # PR-31 covariate (walk-forward PIT, model/ercot_cov.py); 0 by default
        from prediction_market_macro.model import ercot_cov
        ercot_shift = w * ercot_cov.mu_shift(conn, asof, "KXJOBLESSCLAIMS")
    mu = math.exp(base_log + seasonal + ercot_shift)
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
