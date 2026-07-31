"""model/energy.py — weekly energy settles (PLAN §7; KXWTIW / KXNATGASW / KXAAAGASW).

Three targets, one entry point (predict dispatches on `series`):

  KXWTIW    WTI front-month NYMEX Friday settle, to the cent ($1-wide BETWEEN buckets).
  KXNATGASW Henry Hub front-month Friday settle ('Above $X.X99' strict >).
  KXAAAGASW AAA national average regular gasoline, Monday reading ('Above X.XX0' strict >).

Model energy/0.1.0:
  WTI/NG:  driftless GBM anchored on the latest visible front-month close (fut_daily via
           FeatureStore.fut_closes — CL=F / NG=F, completed bars only). sigma_daily is the
           MAD-robust std of the last 20 log returns (floored), scaled by sqrt(remaining
           BUSINESS days from the data horizon to the settle date). 20k-sample Empirical
           with deterministic rng(0) (replay-stable).
  AAA gas: EIA weekly retail regular (GASREGW, PIT via ALFRED vintages) is the anchor —
           it is a PROXY for the AAA level (systematic offset, see registry settle_source
           + model card; series stays shadow until the offset is calibrated on settles).
           mu = last print + damped 4-week trend x remaining weeks; sigma = robust std of
           weekly changes x 1.5 (proxy-noise inflation) x sqrt(weeks).

All reads go through FeatureStore (PIT); predict(asof) is deterministic given the db.
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd

from prediction_market_macro.model.common import Empirical, GaussianMix, Pred
from prediction_market_macro.model.features import FeatureStore

VERSION = "energy/0.1.0"

_FUT_ROOT = {"KXWTIW": "CL", "KXNATGASW": "NG"}
_N_SAMPLES = 20_000
_MIN_SIGMA_DAILY = {"CL": 0.008, "NG": 0.015}     # vol floors (fraction/day)


def _remaining_bdays(horizon: datetime, settle: pd.Timestamp) -> float:
    """Business days from the last visible close to the settle date, floored near zero."""
    start = pd.Timestamp(horizon.date()) + pd.Timedelta(days=1)
    if start > settle:
        return 0.05                                # settle day itself: residual intraday risk
    return max(float(np.busday_count(start.date(), (settle + pd.Timedelta(days=1)).date())),
               0.05)


def _gbm_futures(conn, asof: datetime, period: str, series: str) -> Pred:
    root = _FUT_ROOT[series]
    fs = FeatureStore(conn)
    closes, horizon = fs.fut_closes(root, asof, n=60)
    if len(closes) < 25 or horizon is None:
        raise RuntimeError(f"{series}: fut_daily {root} history too short at {asof} "
                           f"(n={len(closes)})")
    s0 = float(closes.iloc[-1])
    rets = np.diff(np.log(closes.values))[-20:]
    sigma_d = max(1.4826 * float(np.median(np.abs(rets - np.median(rets)))),
                  _MIN_SIGMA_DAILY[root])
    h = _remaining_bdays(datetime.fromisoformat(horizon), pd.Timestamp(period))
    sig = sigma_d * math.sqrt(h)

    z = np.random.default_rng(0).standard_normal(_N_SAMPLES)
    samples = s0 * np.exp(-0.5 * sig * sig + sig * z)          # driftless GBM
    dist = Empirical(tuple(np.round(samples, 4).tolist()))
    return Pred(series=series, period=period, dist=dist, asof=asof,
                model_version=VERSION,
                inputs={"root": root, "s0": round(s0, 2),
                        "sigma_daily": round(sigma_d, 5), "h_bdays": round(h, 2),
                        "sigma_h": round(sig, 5), "last_bar": str(closes.index[-1].date())},
                data_horizon=datetime.fromisoformat(horizon))


def _aaa_gas(conn, asof: datetime, period: str) -> Pred:
    fs = FeatureStore(conn)
    s, horizon = fs.fred_series("GASREGW", asof)
    if len(s) < 30 or horizon is None:
        raise RuntimeError(f"KXAAAGASW: GASREGW history too short at {asof} (n={len(s)})")
    last = float(s.iloc[-1])
    dw = s.diff().dropna()
    trend_w = float(dw.tail(4).mean()) * 0.5                   # damped weekly trend
    sig_w = max(1.4826 * float(np.median(np.abs(dw.tail(52) - dw.tail(52).median()))),
                0.01)
    weeks = max((pd.Timestamp(period) - pd.Timestamp(s.index[-1])).days / 7.0, 0.15)
    mu = last + trend_w * weeks
    sigma = sig_w * 1.5 * math.sqrt(weeks)                     # x1.5: EIA-vs-AAA proxy noise
    dist = GaussianMix(((1.0, mu, sigma),))
    return Pred(series="KXAAAGASW", period=period, dist=dist, asof=asof,
                model_version=VERSION,
                inputs={"anchor_gasregw": round(last, 3), "trend_w": round(trend_w, 4),
                        "sigma_w": round(sig_w, 4), "weeks": round(weeks, 2),
                        "mu": round(mu, 3), "sigma": round(sigma, 4),
                        "proxy_note": "GASREGW anchor; AAA level offset uncalibrated"},
                data_horizon=datetime.fromisoformat(horizon))


def predict(conn, asof: datetime, period: str, series: str) -> Pred:
    """period: ISO settle date ('2026-07-31'). Dispatches per energy series."""
    if series in _FUT_ROOT:
        return _gbm_futures(conn, asof, period, series)
    if series == "KXAAAGASW":
        return _aaa_gas(conn, asof, period)
    raise ValueError(f"energy.predict: unknown series {series}")
