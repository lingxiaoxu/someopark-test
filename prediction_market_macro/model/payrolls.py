"""model/payrolls.py — NFP monthly change (PLAN §7). payrolls/0.2.0

Label discipline (§5-bis.2): the PRINTED headline change = level(t) − level(t−1) both read
from the SAME first vintage of month t — reconstructed exactly from the ALFRED store.
Model: mu = 0.6·(3-month avg of printed changes) + 0.4·claims_signal; claims_signal maps
the change in the 4-week claims average (month-over-month, PIT) at −2 jobs per claim.
Fat tails: two-component mixture whose SHAPE is the registered one (0.8/0.2, tail 2.55×
core) but whose SCALE is measured from the model's own recent residuals instead of being
pinned to a constant — see `_residual_sigma` for why the constant was wrong.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from prediction_market_macro.model.common import GaussianMix, Pred
from prediction_market_macro.model.features import FeatureStore

VERSION = "payrolls/0.2.0"

# 0.1.0 declared absolute widths (sigma_core=55k, sigma_tail=140k). They are retired, not
# renamed: `predict` still ACCEPTS them so an adopted manual_params row does not crash the
# daily run, but they can only widen (they act as a floor) and every stored pred records
# that it was handed a retired key. Silently ignoring an adopted parameter is the failure
# mode tests/test_claims_params.py exists to prevent.
_RETIRED = ("sigma_core", "sigma_tail")

# At least this many residuals before the MAD estimate is trusted. Not a parameter: with
# `assert len(ch) >= 12` and base_months<=6 there are always >=6 residuals in production,
# so this can only fire on a fixture, where a tunable would be width with no payoff.
_MIN_RESID = 6

# defaults == the registered payrolls/0.2.0 behaviour. Every key must be able to MOVE
# the output — see tests/test_payrolls_params.py.
DEFAULT_PARAMS = {
    "base_months": 3,           # trailing printed changes averaged into the base
    "w_base": 0.6,              # weight on base; (1-w_base) goes to the claims signal
    "jobs_per_claim": 2.0,      # jobs lost per extra initial claim
    "claims_clip": 150_000,     # cap on how far the claims signal may pull off base
    "sigma_window": 24,         # months of own-residuals behind the scale estimate
    "sigma_mult": 1.0,          # multiplier on that robust scale
    "tail_mult": 2.55,          # sigma_tail / sigma_core (0.1.0 shipped 140/55 = 2.545)
    "w_tail": 0.2,              # weight on the fat component
    "sigma_floor": 25_000.0,    # degenerate-history guard, not a tuning knob
}


def printed_changes(conn, asof: datetime) -> pd.Series:
    """Headline NFP change per month, per its own first vintage (PIT-visible only,
    via the single data door §5-bis.4-1)."""
    rows = FeatureStore(conn).fred_vintages("PAYEMS", asof)
    by_vintage: dict[str, dict[str, float]] = {}
    firsts: dict[str, tuple[str, float]] = {}
    for r in rows:
        by_vintage.setdefault(r["vintage_date"], {})[r["event_time"]] = r["value"]
        if r["event_time"] not in firsts:
            firsts[r["event_time"]] = (r["vintage_date"], r["value"])
    out = {}
    for ev, (vd, val) in firsts.items():
        prev_month = (pd.Period(ev[:7]) - 1).strftime("%Y-%m")
        vint = by_vintage.get(vd, {})
        prev_keys = [k for k in vint if k[:7] == prev_month]
        if prev_keys:
            out[pd.Timestamp(ev)] = (val - vint[prev_keys[0]]) * 1000   # PAYEMS in thousands
    return pd.Series(out).sort_index()


def _residual_sigma(ch: pd.Series, p: dict) -> tuple[float, int]:
    """(core sigma, residuals used) — robust scale of this model's OWN recent misses.

    Why this replaced the constant 55k. Scored against the 2010-2026 first prints ex-2020
    (1080 anchor/target pairs, 185 anchors), the registered mixture 0.8·N(0,55k)+0.2·N(0,140k)
    has sd 79,624 and implies P(|e|>1sd)=23.2%, P(|e|>2sd)=5.4%. Observed: 41.3% and 15.0%.
    A constant-sigma MLE refit puts the sd at 137,443 (h=1) with an anchor-clustered
    bootstrap 90% CI of [120,396, 160,772] — the live width sits OUTSIDE that interval, so
    this was not a tuning quibble, it was a distribution the data rejects.

    But no constant is right either: the robust sd by era runs 74,328 (2010-14) / 64,691
    (2015-19) / 149,446 (2021-23) / 93,997 (2024-26). So track it. resid_t = ch_t −
    mean(ch_{t-base_months..t-1}) is the base leg's miss; it omits the claims leg, and on
    the matched 185 target months that proxy measures 93,898 against the full model's
    98,642 (ratio 0.95, RMSE 138,160 vs 137,443) — close enough to calibrate on, and it
    needs nothing predict() has not already loaded.

    Calibration at the shipped defaults (W=24, mult=1.0): P(|e|>sigma_core)=31.6% against
    the 31.7% a single Gaussian implies, and on the FULL sample including 2020 the mixture
    covers 23.4% beyond 1sd against 23.5% implied. The card shows p5/p50/p95, so that band
    was checked too: 11.8% of outcomes land outside [p5, p95] on the full sample (target
    10%) and 6.4% ex-2020 — i.e. right on the sample production actually faces, and erring
    WIDE in ordinary times, which is the safe direction for a maker. 0.1.0's live width put
    41.3% of outcomes beyond 1sd. Note what that means for tail_mult: the
    constant-sigma fit wanted a 3.5× tail, but once sigma tracks the regime most of that
    "fat tail" turns out to have been heteroskedasticity — standardised residuals fit a
    ratio near 2.08, so the registered 2.55 is if anything still generous.
    """
    bm = int(p["base_months"])
    resid = (ch - ch.rolling(bm).mean().shift(1)).dropna()
    w = resid.tail(int(p["sigma_window"])).to_numpy(dtype=float)
    floor = float(p["sigma_floor"])
    if len(w) < _MIN_RESID:
        return floor, len(w)
    mad = float(np.median(np.abs(w - np.median(w))))
    return max(float(p["sigma_mult"]) * 1.4826 * mad, floor), len(w)


def predict(conn, asof: datetime, period: str, series: str = "KXPAYROLLS",
            params: dict | None = None) -> Pred:
    p = {**DEFAULT_PARAMS, **(params or {})}
    retired = {k: float(p.pop(k)) for k in _RETIRED if k in p}
    fs = FeatureStore(conn)
    ch = printed_changes(conn, asof)
    assert len(ch) >= 12, "payrolls print history too short"
    icsa, h_c = fs.fred_series("ICSA", asof)
    base = float(ch.tail(int(p["base_months"])).mean())
    c4 = icsa.rolling(4).mean().dropna()
    if len(c4) >= 9:
        claims_delta = float(c4.iloc[-1] - c4.iloc[-5])         # ~1 month apart
        claims_signal = base - float(p["jobs_per_claim"]) * claims_delta
        clip = float(p["claims_clip"])
        claims_signal = float(np.clip(claims_signal, base - clip, base + clip))
    else:
        claims_signal = base
    w = float(p["w_base"])
    mu = w * base + (1.0 - w) * claims_signal
    # `period` is deliberately NOT in the scale. The obvious fix — widen with the horizon,
    # the way cpi._horizon_widen does — is wrong here and the data says so: over h=1..6
    # months ahead the robust sd of the error runs 1.00 / 0.96 / 1.00 / 0.90 / 0.95 / 0.93
    # relative to h=1 (IQR and MAE ratios likewise ~1). mu is a slow 3-month average that
    # carries almost no month-specific information, so the six-months-out forecast is
    # neither better nor worse than the one-month-out one. What WAS broken is the level of
    # sigma at every horizon; that is what _residual_sigma fixes. Do not re-add a horizon
    # term without re-running the measurement — it would only make a calibrated model wide.
    sigma_core, n_resid = _residual_sigma(ch, p)
    if "sigma_core" in retired:
        sigma_core = max(sigma_core, retired["sigma_core"])
    sigma_tail = max(float(p["tail_mult"]) * sigma_core,
                     retired.get("sigma_tail", 0.0))
    wt = float(p["w_tail"])
    dist = GaussianMix(((1.0 - wt, mu, sigma_core), (wt, mu, sigma_tail)))
    _, h = FeatureStore(conn).fred_first_prints("PAYEMS", asof)
    horizon = max(h or asof.isoformat(), h_c or "")
    inputs = {"base_3m": round(base, 0), "claims_signal": round(claims_signal, 0),
              "mu": round(mu, 0), "n_prints": len(ch),
              "sigma_core": round(sigma_core, 0), "sigma_tail": round(sigma_tail, 0),
              "n_resid": n_resid}
    # loud, per-pred, permanent: a retired key was supplied and did not do what its name
    # promises. Reported here rather than raised because raising would take the series
    # down every day until a human edits the adopted param set.
    inputs.update({f"{k}_retired": v for k, v in retired.items()})
    return Pred(series="KXPAYROLLS", period=period, dist=dist, asof=asof,
                model_version=VERSION, inputs=inputs,
                data_horizon=datetime.fromisoformat(horizon))
