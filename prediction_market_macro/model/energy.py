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

VERSION = "energy/0.4.0"          # 0.4.0: AAA daily anchor + NG storage tilt
                                  # (both light up as their feeds accumulate)

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


def _ng_storage_tilt(conn, fs: FeatureStore, asof: datetime) -> tuple[float, int]:
    """v0.4 NG drift term from the weekly storage surprise (activates only when
    ingest/eia.py has data — needs EIA_API_KEY). Surprise = latest weekly build
    vs the 5y same-week average; sign: bigger-than-normal build ⇒ bearish.
    Coefficient fit walk-forward on past (surprise, next-close-move) pairs."""
    st, _h = fs.fred_series("NG_STORAGE_WEEKLY", asof)
    if len(st) < 60:
        return 0.0, len(st)
    chg = st.diff().dropna()
    try:
        weeks = chg.index.to_series().dt.isocalendar().week
    except Exception:                                     # noqa: BLE001
        return 0.0, len(st)
    seasonal = chg.groupby(weeks.values).transform("mean")
    surprise = (chg - seasonal).dropna()
    closes, _h2 = fs.fut_closes("NG", asof, n=600)
    if len(closes) < 60 or len(surprise) < 30:
        return 0.0, len(st)
    xs, ys = [], []
    for ts, sp in surprise.tail(150).items():
        after = closes[closes.index > ts]
        before = closes[closes.index <= ts]
        if len(after) >= 4 and len(before) >= 1:
            ys.append(float(after.iloc[3] / before.iloc[-1] - 1))
            xs.append(float(sp))
    if len(xs) < 20:
        return 0.0, len(st)
    b = float(np.polyfit(xs, ys, 1)[0])
    return b * float(surprise.iloc[-1]), len(st)


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
    drift = 0.0
    n_stor = 0
    if series == "KXNATGASW":
        try:
            drift, n_stor = _ng_storage_tilt(conn, fs, asof)
            drift = float(np.clip(drift, -0.05, 0.05))    # ≤5% weekly tilt cap
        except Exception:                                 # noqa: BLE001
            drift = 0.0
    z = np.random.default_rng(0).standard_normal(_N_SAMPLES)
    samples = s0 * np.exp(drift - 0.5 * sig * sig + sig * z)   # GBM (+storage tilt)
    dist = Empirical(tuple(np.round(samples, 4).tolist()))
    return Pred(series=series, period=period, dist=dist, asof=asof,
                model_version=VERSION,
                inputs={"root": root, "s0": round(s0, 2),
                        "sigma_daily": round(sigma_d, 5), "h_bdays": round(h, 2),
                        "sigma_h": round(sig, 5),
                        "storage_tilt": round(drift, 5), "n_storage_obs": n_stor,
                        "last_bar": str(closes.index[-1].date())},
                data_horizon=datetime.fromisoformat(horizon))


def _aaa_settled_mids(conn, asof: datetime) -> list[tuple[datetime, float]]:
    """(settle_ts, interval-censored print mid) for every PAST settled AAA event:
    the print lies in (max YES strike, min NO strike] on the 0.002 grid."""
    rows = conn.execute(
        "SELECT s.period, s.settled_ts, c.floor_strike, s.result FROM settlements s"
        " JOIN contracts c ON c.ticker=s.ticker WHERE s.series='KXAAAGASW'"
        " AND s.result IN ('yes','no') AND c.floor_strike IS NOT NULL"
        " AND s.settled_ts <= ?", (asof.isoformat(),)).fetchall()
    by_ev: dict[str, dict] = {}
    for r in rows:
        ev = by_ev.setdefault(r["period"], {"yes": [], "no": [], "ts": r["settled_ts"]})
        ev[r["result"]].append(float(r["floor_strike"]))
    out = []
    for ev in by_ev.values():
        if not ev["yes"] or not ev["no"]:
            continue
        lo, hi = max(ev["yes"]), min(ev["no"])
        if hi <= lo:
            continue
        out.append((datetime.fromisoformat(ev["ts"].replace("Z", "+00:00")),
                    (lo + hi) / 2.0))
    out.sort()
    return out


def _aaa_drift_fit(conn, fs: FeatureStore, asof: datetime):
    """Walk-forward drift regression for the AAA weekly settle (2026-07-31, after
    the decision replay exposed the failure): the settle-vs-proxy gap is NOT a
    level offset — it is the CURRENT week's price move, sign-flipping with the
    gasoline trend, which a damped 4-week average chronically lags. Regress
    (settled mid − latest GASREGW) on [last GASREGW weekly diff, RB futures
    10-bday move] over past settled events (PIT: everything <= asof).
    Returns (coef intercept/b1/b2, resid_sigma, n) or None when n < 10."""
    mids = _aaa_settled_mids(conn, asof)
    if len(mids) < 10:
        return None
    X, y = [], []
    for ts, mid in mids:
        g, _ = fs.fred_series("GASREGW", ts)
        if len(g) < 6:
            continue
        g_last = float(g.iloc[-1])
        g_diff = float(g.iloc[-1] - g.iloc[-2])
        rb, _h = fs.fut_closes("RB", ts, n=15)
        rb_mv = (float(rb.iloc[-1] / rb.iloc[0] - 1) * g_last
                 if len(rb) >= 10 else 0.0)
        X.append([1.0, g_diff, rb_mv])
        y.append(mid - g_last)
    if len(y) < 10:
        return None
    Xa, ya = np.asarray(X), np.asarray(y)
    coef = np.linalg.lstsq(Xa, ya, rcond=None)[0]
    resid = ya - Xa @ coef
    sigma = max(float(np.std(resid)), 0.02)
    return coef, sigma, len(y)


def _aaa_gas(conn, asof: datetime, period: str) -> Pred:
    fs = FeatureStore(conn)
    s, horizon = fs.fred_series("GASREGW", asof)
    if len(s) < 30 or horizon is None:
        raise RuntimeError(f"KXAAAGASW: GASREGW history too short at {asof} (n={len(s)})")
    last = float(s.iloc[-1])
    dw = s.diff().dropna()
    sig_w = max(1.4826 * float(np.median(np.abs(dw.tail(52) - dw.tail(52).median()))),
                0.01)
    weeks = max((pd.Timestamp(period) - pd.Timestamp(s.index[-1])).days / 7.0, 0.15)
    # v0.4: the DAILY AAA reading (the settle number itself) beats any proxy when
    # fresh — anchor on it directly; residual risk is only the days to settle
    aaa, h_aaa = fs.fred_series("AAA_DAILY", asof)
    if len(aaa) >= 1 and (asof.date() - aaa.index[-1].date()).days <= 3:
        last_aaa = float(aaa.iloc[-1])
        d_daily = aaa.diff().dropna()
        drift_d = float(d_daily.tail(5).mean()) if len(d_daily) >= 3 else 0.0
        days_left = max((pd.Timestamp(period) - pd.Timestamp(aaa.index[-1])).days, 0.2)
        sig_d = (max(1.4826 * float(np.median(np.abs(d_daily.tail(30)
                                                     - d_daily.tail(30).median()))),
                     0.004) if len(d_daily) >= 5 else sig_w / math.sqrt(7))
        mu = last_aaa + drift_d * days_left
        sigma = max(sig_d * math.sqrt(days_left), 0.008)
        dist = GaussianMix(((1.0, mu, sigma),))
        horizon2 = max(horizon or "", h_aaa or "")
        return Pred(series="KXAAAGASW", period=period, dist=dist, asof=asof,
                    model_version=VERSION,
                    inputs={"anchor_aaa_daily": round(last_aaa, 3),
                            "drift_d": round(drift_d, 4),
                            "days_left": round(days_left, 1),
                            "n_daily_obs": len(aaa),
                            "mu": round(mu, 3), "sigma": round(sigma, 4),
                            "mode": "aaa_daily_anchor"},
                    data_horizon=datetime.fromisoformat(horizon2))
    fit = _aaa_drift_fit(conn, fs, asof)
    if fit is not None:
        coef, resid_sig, n_fit = fit
        g_diff = float(s.iloc[-1] - s.iloc[-2])
        rb, _h = fs.fut_closes("RB", asof, n=15)
        rb_mv = (float(rb.iloc[-1] / rb.iloc[0] - 1) * last
                 if len(rb) >= 10 else 0.0)
        drift = float(coef[0] + coef[1] * g_diff + coef[2] * rb_mv)
        mu = last + drift
        sigma = resid_sig * math.sqrt(max(weeks, 1.0))
        mode = f"drift_regression(n={n_fit})"
    else:
        trend_w = float(dw.tail(4).mean()) * 0.5           # cold-start fallback
        mu = last + trend_w * weeks
        sigma = sig_w * 1.5 * math.sqrt(weeks)
        drift, mode = trend_w * weeks, "damped_trend_fallback"
    dist = GaussianMix(((1.0, mu, sigma),))
    return Pred(series="KXAAAGASW", period=period, dist=dist, asof=asof,
                model_version=VERSION,
                inputs={"anchor_gasregw": round(last, 3), "drift": round(drift, 4),
                        "mode": mode, "weeks": round(weeks, 2),
                        "mu": round(mu, 3), "sigma": round(sigma, 4)},
                data_horizon=datetime.fromisoformat(horizon))


def predict(conn, asof: datetime, period: str, series: str) -> Pred:
    """period: ISO settle date ('2026-07-31'). Dispatches per energy series."""
    if series in _FUT_ROOT:
        return _gbm_futures(conn, asof, period, series)
    if series == "KXAAAGASW":
        return _aaa_gas(conn, asof, period)
    raise ValueError(f"energy.predict: unknown series {series}")
