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

VERSION = "energy/0.6.0"          # 0.6.0: storage/inventory drift removed (measured noise)
                                  # 0.5.0: empirical (bootstrap) innovations everywhere,
                                  #        AR(1) gasoline drift, 1.5x inflation removed
                                  # 0.4.0: AAA daily anchor + NG storage tilt

_FUT_ROOT = {"KXWTIW": "CL", "KXNATGASW": "NG"}
_N_SAMPLES = 20_000
_MIN_SIGMA_DAILY = {"CL": 0.008, "NG": 0.015}     # vol floors (fraction/day)
_MIN_POOL = 120                   # innovations needed before a bootstrap beats a normal

# defaults == the registered energy/0.6.0 behaviour. This module is TWO models behind one
# entry point, and a grid should treat them that way: the fut_* keys only reach KXWTIW /
# KXNATGASW, the aaa_* keys only reach KXAAAGASW. Varying the wrong half for a series is
# free width that cannot change a single prediction — the exact way claims.py wasted 21
# of its 24 sets (see tests/test_claims_params.py).
#
# _N_SAMPLES is deliberately NOT a param: it is the Monte-Carlo encoding resolution, so a
# grid over it would feed argmin pure sampling noise.
#
# aaa_proxy_inflation is exposed but the default is MEASURED, not guessed — see the long
# comment at its use site. A grid that moves it off 1.5 needs to beat 0.0862 LOO.
DEFAULT_PARAMS = {
    # --- futures branch (KXWTIW / KXNATGASW) ---
    "fut_vol_window": 20,          # bars of log returns setting today's vol scale
    "fut_pool_bars": 1500,         # bars the bootstrap SHAPE is drawn from
    "fut_min_sigma_daily": dict(_MIN_SIGMA_DAILY),   # per-root vol floor (fraction/day)
    "fut_min_pool": _MIN_POOL,
    # #192. A scalar on the horizon sigma, DEFAULT 1.0 — a bit-identical no-op until a
    # selection lane moves it. It exists because the over-width at KXNATGASW survived
    # every attempt to kill it and there was no searchable key that could express it:
    # `fut_vol_window` and `fut_pool_bars` change the width only as a side effect of
    # changing what is estimated, and `fut_min_sigma_daily` (the key the ticket originally
    # nominated) is not the mechanism — the floor binds on 1/21 NG events and 1/156 CL.
    #
    # What survived, measured 2026-08-27 on 21/21 NG events with NO censoring drop
    # (`interval kinds: {interior: 21}`, so the "it is my own bracket-midpoint artifact"
    # hypothesis is refuted for this series): the randomized-PIT deciles are
    # [0.4,1.3,2.1,2.2,3.3,3.3,2.8,3.1,2.1,0.4] against 2.1 flat, realizations land
    # outside the 10/90 band 3.8% of the time (expected 20%) and outside 5/95 0.9%
    # (expected 10%). Four independent width estimators all point the same way and
    # nowhere near each other: interior width_ratio 0.61, CRPS lambda* 0.65,
    # censoring-aware interval-LL lambda* 0.45, PIT-KS lambda* 0.30.
    #
    # That spread is the reason this is a GRID KEY and not a number. Do not hand-set it;
    # the ladder in `param_space.CANDIDATES['energy']` is deliberately coarse, symmetric
    # about 1.0 in log space and able to WIDEN, so the walk-forward can refute the
    # hypothesis rather than only confirm it. See PR-12 in docs/PREREGISTER.md for the
    # judgment criterion and the K count.
    #
    # VERSION is deliberately NOT bumped for this. A bump is a claim that predictions
    # changed; this one is exact (`x * 1.0` in IEEE754), pinned by
    # test_fut_sigma_scale_defaults_to_a_byte_identical_no_op. Bumping anyway would force
    # `param_argmin` to rescore all of energy and the d75 WF pin to be regenerated, for a
    # difference of zero. The bump belongs to whatever LANDS a non-1.0 default, if one
    # ever earns it.
    "fut_sigma_scale": 1.0,
    # --- AAA gasoline branch (KXAAAGASW) ---
    "aaa_fresh_days": 3,           # daily AAA reading counts as fresh within this many days
    "aaa_drift_days": 5,           # daily diffs averaged into the short drift
    "aaa_sig_d_window": 30,        # daily diffs in the daily-anchor sigma
    "aaa_sig_d_floor": 0.004,
    "aaa_sigma_floor": 0.008,      # floor on the daily-anchor sigma
    "aaa_sig_w_window": 52,        # weekly diffs in the proxy sigma
    "aaa_sig_w_floor": 0.01,
    "aaa_min_fit": 10,             # settled events needed before the drift regression runs
    "aaa_resid_floor": 0.02,
    "aaa_trend_window": 4,         # weeks in the cold-start trend
    "aaa_trend_damp": 0.5,
    "aaa_proxy_inflation": 1.5,
}


def _p(params: dict | None) -> dict:
    return {**DEFAULT_PARAMS, **(params or {})}


def _mad_sigma(x: np.ndarray) -> float:
    """Robust sigma: 1.4826 x median absolute deviation about the median. Equals the
    ordinary sd for a Gaussian, and is unmoved by the outliers that dominate energy."""
    x = np.asarray(x, dtype=float)
    return float(1.4826 * np.median(np.abs(x - np.median(x))))


def _innovation_pool(x: np.ndarray, min_pool: int = _MIN_POOL) -> np.ndarray | None:
    """Centered innovations to bootstrap from, or None when the sample is too thin.

    Deliberately NOT rescaled here. Scale is set by the caller from a RECENT window
    (so today's vol regime governs), while shape comes from the whole history (so the
    tails are the ones the market actually prints). Mixing the two is the point.
    """
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < min_pool:
        return None
    return x - np.median(x)


def _bootstrap_z(rng, pool: np.ndarray, steps: int, n: int) -> np.ndarray:
    """`steps` iid draws summed, renormalised to MAD-sigma 1.

    `pool` must be CENTERED — pass it through _innovation_pool. Nothing here recenters,
    so an uncentered pool silently becomes a drift on top of whatever mu the caller
    already computed.

    Two properties are load-bearing:
      - summing `steps` draws (rather than scaling one draw by sqrt(steps)) lets the
        CLT thin the tail at multi-day horizons exactly as much as it really does;
      - the MAD-sigma renormalisation pins the BODY to whatever scale the caller
        passes, so swapping normal -> bootstrap changes the shape and nothing else.
        Without it we would silently also change the width, and no before/after
        comparison would mean anything.
    """
    z = np.zeros(n)
    for _ in range(max(steps, 1)):
        z += rng.choice(pool, size=n, replace=True)
    s = _mad_sigma(z)
    return z / s if s > 0 else z


def _remaining_bdays(horizon: datetime, settle: pd.Timestamp) -> float:
    """Business days from the last visible close to the settle date, floored near zero."""
    start = pd.Timestamp(horizon.date()) + pd.Timedelta(days=1)
    if start > settle:
        return 0.05                                # settle day itself: residual intraday risk
    return max(float(np.busday_count(start.date(), (settle + pd.Timedelta(days=1)).date())),
               0.05)


def _gbm_futures(conn, asof: datetime, period: str, series: str,
                 params: dict | None = None) -> Pred:
    p = _p(params)
    root = _FUT_ROOT[series]
    fs = FeatureStore(conn)
    vw = int(p["fut_vol_window"])
    closes, horizon = fs.fut_closes(root, asof, n=max(60, vw + 40))
    if len(closes) < 25 or horizon is None:
        raise RuntimeError(f"{series}: fut_daily {root} history too short at {asof} "
                           f"(n={len(closes)})")
    s0 = float(closes.iloc[-1])
    rets = np.diff(np.log(closes.values))[-vw:]
    sigma_d = max(_mad_sigma(rets), float(p["fut_min_sigma_daily"][root]))
    h = _remaining_bdays(datetime.fromisoformat(horizon), pd.Timestamp(period))
    # #192: the scale multiplies the HORIZON sigma, after the floor. Putting it on
    # sigma_daily instead would let a narrowing be silently eaten by `fut_min_sigma_daily`
    # on exactly the events where vol is lowest, which is the opposite of a clean scalar.
    sig = sigma_d * math.sqrt(h) * float(p["fut_sigma_scale"])
    # v0.6: NO storage/inventory drift. The v0.4 tilt regressed the next 3-bday move on
    # the EIA inventory surprise and applied b * latest_surprise. Replayed the way the
    # model actually used it over 782 prints (rolling 150-pair fit, PIT): sign hit rate
    # 0.487, RMSE 0.07666 against 0.07700 for no drift at all, and the fitted b changed
    # sign 23 times with 51% of fits positive -- i.e. the coefficient is a coin flip, and
    # its stated economics (bigger build => bearish) is not even the sign it usually took.
    # The earlier justification came from a 154-print window where corr was +0.253; on the
    # full 863-print history that same corr is +0.073. See PLAN §29.3.
    drift = 0.0
    # v0.5: shape from the long history, scale from the last 20 bars. Measured
    # 2026-08-04 on the stored bars: CL kurtosis 9.3, NG 37.1 -- the normal that used
    # to sit here is not a defensible description of either.
    pool_closes, _hp = fs.fut_closes(root, asof, n=int(p["fut_pool_bars"]))
    # CL 2020-04-20 settled at -37.63. That is a real print, and the only bar in 6,512
    # where a log-return does not exist: np.log makes BOTH diffs touching it NaN and
    # _innovation_pool drops them. Keep it that way. Filtering the bar out before the log
    # instead would splice 04-17 straight to 04-21, across the May expiry, and hand the
    # pool a single -45% daily innovation that no tradeable day ever delivered. errstate
    # silences the warning only — it changes no value.
    with np.errstate(invalid="ignore"):
        pool = _innovation_pool(np.diff(np.log(pool_closes.values))
                                if len(pool_closes) > 2 else np.array([]),
                                int(p["fut_min_pool"]))
    rng = np.random.default_rng(0)                        # replay-stable
    if pool is not None:
        z = _bootstrap_z(rng, pool, int(math.ceil(h)), _N_SAMPLES)
        shape = f"bootstrap(n={len(pool)})"
    else:
        z = rng.standard_normal(_N_SAMPLES)
        shape = "normal_fallback"
    samples = s0 * np.exp(-0.5 * sig * sig + sig * z)          # driftless GBM
    dist = Empirical(tuple(np.round(samples, 4).tolist()))
    return Pred(series=series, period=period, dist=dist, asof=asof,
                model_version=VERSION,
                inputs={"root": root, "s0": round(s0, 2),
                        "sigma_daily": round(sigma_d, 5), "h_bdays": round(h, 2),
                        "sigma_h": round(sig, 5), "shape": shape,
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


def _aaa_drift_fit(conn, fs: FeatureStore, asof: datetime, min_fit: int = 10,
                   resid_floor: float = 0.02):
    """Walk-forward drift regression for the AAA weekly settle (2026-07-31, after
    the decision replay exposed the failure): the settle-vs-proxy gap is NOT a
    level offset — it is the CURRENT week's price move, sign-flipping with the
    gasoline trend, which a damped 4-week average chronically lags. Regress
    (settled mid − latest GASREGW) on [last GASREGW weekly diff, RB futures
    10-bday move] over past settled events (PIT: everything <= asof).
    Returns (coef intercept/b1/b2, resid_sigma, n) or None when n < 10."""
    mids = _aaa_settled_mids(conn, asof)
    if len(mids) < min_fit:
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
        # NOTE: a gasoline-stocks 4th regressor was tried 2026-07-31 and
        # REVERTED — with only ~28 fit points it overfit (60d WF: AAA
        # +9.32 → +2.44). Revisit when the settled sample doubles.
        X.append([1.0, g_diff, rb_mv])
        y.append(mid - g_last)
    if len(y) < min_fit:
        return None
    Xa, ya = np.asarray(X), np.asarray(y)
    coef = np.linalg.lstsq(Xa, ya, rcond=None)[0]
    # v0.5: sigma from LEAVE-ONE-OUT residuals, not in-sample. Three parameters on 28
    # points with corr(g_diff, rb_mv) = +0.79 is exactly the regime where in-sample
    # residuals flatter the fit: measured 2026-08-04, in-sample sd is 0.0716 against a
    # LOO 0.0862, so the old number made every AAA quote 20% overconfident. The mean
    # model itself is sound and stays -- LOO 0.0862 beats a full-history AR(1) (0.1196)
    # and a no-drift naive (0.1449) on the same 28 events.
    loo = []
    for i in range(len(ya)):
        m = np.ones(len(ya), dtype=bool)
        m[i] = False
        cf = np.linalg.lstsq(Xa[m], ya[m], rcond=None)[0]
        loo.append(ya[i] - Xa[i] @ cf)
    sigma = max(float(np.sqrt(np.mean(np.square(loo)))), float(resid_floor))
    return coef, sigma, len(y)


def _gas_ar1(dw: pd.Series, min_pool: int = _MIN_POOL):
    """AR(1) on the GASREGW weekly change: d[t+1] = a + b*d[t] + e.

    Used ONLY for its residual pool — the shape the bootstrap draws from. Fit on the
    whole visible history (1868 weekly changes as of 2026-08) because 28 settled AAA
    events is not a bootstrap pool, and the shape of a gasoline week does not depend
    on whether Kalshi happened to list a contract on it.

    Not used for the AAA mean, and that is a measured decision, not an oversight.
    Retail gasoline weekly changes are strongly MOMENTUM (b=+0.555, R^2=+0.308 vs a
    zero forecast), so an AR(1) drift looks like an obvious upgrade over the settled-
    event regression. Tested head to head on the 28 settled events it is not: LOO RMSE
    0.1196 for the AR(1) against 0.0862 for the regression. The regression wins because
    it carries `rb_mv`, a forward-looking RBOB signal the AR(1) has no access to.

    `b` is clipped to [0, 0.9] so a freak sample can never make the path explosive.
    Returns (a, b, residual pool) or None when the history is too short.
    """
    x = dw.values[:-1]
    y = dw.values[1:]
    if len(x) < min_pool:
        return None
    b, a = np.polyfit(x, y, 1)
    b = float(np.clip(b, 0.0, 0.9))
    pool = _innovation_pool(y - (a + b * x), min_pool)
    return (float(a), b, pool) if pool is not None else None


def _emp_dist(mu: float, sigma: float, pool: np.ndarray | None, weeks: float,
              rng) -> tuple:
    """(dist, shape tag). Empirical bootstrap when a pool exists, else the old normal.

    sigma keeps its meaning across the swap: it is the MAD-sigma of the result either
    way, so any before/after difference is shape and shape only.
    """
    if pool is None:
        return GaussianMix(((1.0, mu, sigma),)), "normal_fallback"
    z = _bootstrap_z(rng, pool, int(math.ceil(max(weeks, 0.0))), _N_SAMPLES)
    return (Empirical(tuple(np.round(mu + sigma * z, 5).tolist())),
            f"bootstrap(n={len(pool)})")


def _aaa_gas(conn, asof: datetime, period: str, params: dict | None = None) -> Pred:
    p = _p(params)
    fs = FeatureStore(conn)
    s, horizon = fs.fred_series("GASREGW", asof)
    if len(s) < 30 or horizon is None:
        raise RuntimeError(f"KXAAAGASW: GASREGW history too short at {asof} (n={len(s)})")
    last = float(s.iloc[-1])
    dw = s.diff().dropna()
    sig_w = max(_mad_sigma(dw.tail(int(p["aaa_sig_w_window"])).values),
                float(p["aaa_sig_w_floor"]))
    weeks = max((pd.Timestamp(period) - pd.Timestamp(s.index[-1])).days / 7.0, 0.15)
    ar = _gas_ar1(dw, int(p["fut_min_pool"]))
    pool = ar[2] if ar else None
    rng = np.random.default_rng(0)                        # replay-stable
    # v0.4: the DAILY AAA reading (the settle number itself) beats any proxy when
    # fresh — anchor on it directly; residual risk is only the days to settle
    aaa, h_aaa = fs.fred_series("AAA_DAILY", asof)
    if len(aaa) >= 1 and (asof.date() - aaa.index[-1].date()).days <= int(p["aaa_fresh_days"]):
        last_aaa = float(aaa.iloc[-1])
        d_daily = aaa.diff().dropna()
        drift_d = (float(d_daily.tail(int(p["aaa_drift_days"])).mean())
                   if len(d_daily) >= 3 else 0.0)
        days_left = max((pd.Timestamp(period) - pd.Timestamp(aaa.index[-1])).days, 0.2)
        sig_d = (max(_mad_sigma(d_daily.tail(int(p["aaa_sig_d_window"])).values),
                     float(p["aaa_sig_d_floor"]))
                 if len(d_daily) >= 5 else sig_w / math.sqrt(7))
        mu = last_aaa + drift_d * days_left
        sigma = max(sig_d * math.sqrt(days_left), float(p["aaa_sigma_floor"]))
        dist, shape = _emp_dist(mu, sigma, pool, days_left / 7.0, rng)
        horizon2 = max(horizon or "", h_aaa or "")
        return Pred(series="KXAAAGASW", period=period, dist=dist, asof=asof,
                    model_version=VERSION,
                    inputs={"anchor_aaa_daily": round(last_aaa, 3),
                            "drift_d": round(drift_d, 4),
                            "days_left": round(days_left, 1),
                            "n_daily_obs": len(aaa),
                            "mu": round(mu, 3), "sigma": round(sigma, 4),
                            "shape": shape, "mode": "aaa_daily_anchor"},
                    data_horizon=datetime.fromisoformat(horizon2))
    fit = _aaa_drift_fit(conn, fs, asof, int(p["aaa_min_fit"]), float(p["aaa_resid_floor"]))
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
        trend_w = (float(dw.tail(int(p["aaa_trend_window"])).mean())
                   * float(p["aaa_trend_damp"]))          # cold-start fallback
        mu = last + trend_w * weeks
        # The 1.5x is NOT a guess to be removed -- I tried, and measurement put it
        # back. sig_w is the UNCONDITIONAL weekly GASREGW move (0.0571 at 2026-08);
        # the quantity we actually need is the AAA forecast error, which is the
        # unpredictable part of the gas move PLUS the AAA-vs-proxy gap. Measured
        # leave-one-out over the 28 settled events that error is 0.0862, against
        # 1.5 * 0.0571 = 0.0857. The factor is right to three decimals by luck, but
        # it is right. (For contrast, the gas move's own AR(1) residual is 0.0243 --
        # dropping the inflation would have made this branch 3.5x overconfident.)
        sigma = sig_w * float(p["aaa_proxy_inflation"]) * math.sqrt(weeks)
        drift, mode = trend_w * weeks, "damped_trend_fallback"
    dist, shape = _emp_dist(mu, sigma, pool, weeks, rng)
    return Pred(series="KXAAAGASW", period=period, dist=dist, asof=asof,
                model_version=VERSION,
                inputs={"anchor_gasregw": round(last, 3), "drift": round(drift, 4),
                        "mode": mode, "weeks": round(weeks, 2), "shape": shape,
                        "mu": round(mu, 3), "sigma": round(sigma, 4)},
                data_horizon=datetime.fromisoformat(horizon))


def predict(conn, asof: datetime, period: str, series: str,
            params: dict | None = None) -> Pred:
    """period: ISO settle date ('2026-07-31'). Dispatches per energy series."""
    if series in _FUT_ROOT:
        pred = _gbm_futures(conn, asof, period, series, params)
        w = float((params or {}).get("ercot_w", 0.0))
        # PR-31: the ERCOT covariate, walk-forward PIT (model/ercot_cov.py). w=0 —
        # the default everywhere — is bit-identical to production; the shift is a
        # log-return added to the settle distribution, every existing input kept.
        if w:
            from prediction_market_macro.model import ercot_cov
            shift = ercot_cov.mu_shift(conn, asof, series)
            if shift:
                k = float(np.exp(w * shift))
                if isinstance(pred.dist, Empirical):
                    dist = Empirical(tuple(round(v * k, 5) for v in pred.dist.values))
                else:
                    dist = GaussianMix(tuple((c[0], c[1] * k, c[2])
                                             for c in pred.dist.comps))
                pred = Pred(series=pred.series, period=pred.period, dist=dist,
                            asof=pred.asof, model_version=pred.model_version,
                            inputs={**pred.inputs, "ercot_shift": round(shift, 6)},
                            data_horizon=pred.data_horizon)
        return pred
    if series == "KXAAAGASW":
        return _aaa_gas(conn, asof, period, params)
    raise ValueError(f"energy.predict: unknown series {series}")
