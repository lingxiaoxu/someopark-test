"""model/bridge.py — MIDAS-style bridge nowcast, the ensemble's third source
(PLAN_EXTENSION §23.2-2; GDPNow methodology in miniature).

Idea: map already-ingested HIGH-frequency proxies onto the monthly target with an
Almon-weighted lag polynomial + OLS — completely independent of the production model's
target-autoregressive structure, which is what makes it a genuinely diverse ensemble
member rather than a rebranding of the same signal.

Supported (v0.1 — proxies already in fred_obs, zero new data feeds):
  KXCPI      GASREGW weekly level changes → headline MoM tilt
  KXPAYROLLS ICSA weekly claims (Almon-weighted) → NFP change
  KXU3       ICSA 4-week trend → U3 delta

§7-bis: SHADOW ONLY. shadow_run() writes preds with model_version='bridge/0.1.0';
decide_all's production-model guard keeps these out of decisions until the adoption
gate promotes them. All reads via FeatureStore (PIT).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from prediction_market_macro.model.common import GaussianMix, Pred
from prediction_market_macro.model.features import FeatureStore

VERSION = "bridge/0.1.0"
SUPPORTED = ("KXCPI", "KXPAYROLLS", "KXU3")


def almon_weights(n: int, theta1: float = 0.0, theta2: float = -0.15) -> np.ndarray:
    """Exponential Almon lag weights over n lags (0 = most recent), normalized."""
    ks = np.arange(n, dtype=float)
    w = np.exp(theta1 * ks + theta2 * ks * ks)
    return w / w.sum()


def _monthly_target_firsts(conn, sid: str, asof: datetime) -> pd.Series:
    rows = conn.execute(
        "SELECT event_time, value, MIN(knowledge_time) kt FROM fred_obs WHERE sid=?"
        " AND knowledge_time<=? GROUP BY event_time ORDER BY event_time",
        (sid, asof.isoformat())).fetchall()
    return pd.Series({pd.Timestamp(r["event_time"]): float(r["value"]) for r in rows})


def _weekly_agg(hf: pd.Series, month: pd.Period, n_lags: int = 12) -> float | None:
    """Almon-weighted mean of the n_lags weekly obs ending inside `month` (or the
    latest available before month end)."""
    end = month.to_timestamp(how="end")
    win = hf[hf.index <= end].tail(n_lags)
    if len(win) < n_lags:
        return None
    w = almon_weights(n_lags)
    return float(np.dot(w, win.values[::-1]))       # lag 0 = newest


def _fit_bridge(y: pd.Series, x_of_month, min_n: int = 30):
    """Walk-forward-safe OLS y_m = a + b·x_m on months where both exist."""
    xs, ys = [], []
    for ts, yv in y.items():
        m = pd.Period(ts, freq="M")
        xv = x_of_month(m)
        if xv is not None and np.isfinite(xv) and np.isfinite(yv):
            xs.append(xv)
            ys.append(yv)
    if len(xs) < min_n:
        return None
    b, a = np.polyfit(xs, ys, 1)
    resid = np.array(ys) - (a + b * np.array(xs))
    sig = max(float(np.std(resid)), 1e-6)
    return float(a), float(b), sig, len(xs)


def predict(conn, asof: datetime, period: str, series: str) -> Pred:
    """Bridge nowcast for `period` (ISO month). Raises if unsupported/insufficient."""
    if series not in SUPPORTED:
        raise ValueError(f"bridge unsupported for {series}")
    fs = FeatureStore(conn)
    ref = pd.Period(period, freq="M")

    if series == "KXCPI":
        idx = _monthly_target_firsts(conn, "CPIAUCSL", asof)
        y = (idx / idx.shift(1) - 1).dropna() * 100          # MoM %
        gas, h = fs.fred_series("GASREGW", asof)
        gas_ch = gas.diff().dropna()
        fit = _fit_bridge(y, lambda m: _weekly_agg(gas_ch, m))
        if fit is None:
            raise RuntimeError("bridge KXCPI: insufficient overlapping history")
        a, b, sig, n = fit
        x = _weekly_agg(gas_ch, ref)
        if x is None:
            raise RuntimeError("bridge KXCPI: no weekly gas data for ref month")
        mu = a + b * x
        unit_note = "%mom"
    elif series == "KXPAYROLLS":
        pay = _monthly_target_firsts(conn, "PAYEMS", asof)
        y = (pay.diff().dropna()) * 1000                      # change in jobs
        icsa, h = fs.fred_series("ICSA", asof)
        fit = _fit_bridge(y, lambda m: _weekly_agg(icsa, m))
        if fit is None:
            raise RuntimeError("bridge KXPAYROLLS: insufficient overlapping history")
        a, b, sig, n = fit
        x = _weekly_agg(icsa, ref)
        if x is None:
            raise RuntimeError("bridge KXPAYROLLS: no weekly claims for ref month")
        mu = a + b * x
        unit_note = "jobs_change"
    else:                                                     # KXU3
        u3 = _monthly_target_firsts(conn, "UNRATE", asof)
        y = u3.diff().dropna()                                # monthly delta
        icsa, h = fs.fred_series("ICSA", asof)
        c4 = icsa.rolling(4).mean().dropna()
        fit = _fit_bridge(y, lambda m: _weekly_agg(c4.diff().dropna(), m, n_lags=8))
        if fit is None:
            raise RuntimeError("bridge KXU3: insufficient overlapping history")
        a, b, sig, n = fit
        x = _weekly_agg(c4.diff().dropna(), ref, n_lags=8)
        if x is None:
            raise RuntimeError("bridge KXU3: no weekly claims trend for ref month")
        last = float(u3.iloc[-1])
        mu = last + a + b * x                                 # level = last + delta
        sig = math.hypot(sig, 0.05)
        unit_note = "u3_level"

    horizon = h or asof.isoformat()
    return Pred(series=series, period=period,
                dist=GaussianMix(((1.0, float(mu), float(sig)),)),
                asof=asof, model_version=VERSION,
                inputs={"a": round(a, 5), "b": round(b, 5), "sigma": round(sig, 5),
                        "x": round(float(x), 5), "n_fit": n, "unit": unit_note},
                data_horizon=datetime.fromisoformat(horizon))


def shadow_run(conn, settings) -> int:
    """Write bridge shadow preds for every open supported (series, period)."""
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.model.common import grid_pmf
    from prediction_market_macro.model.common import pred_to_row
    from prediction_market_macro.ops.predict_all import _open_periods
    now = datetime.now(timezone.utc)
    n = 0
    for series in SUPPORTED:
        spec = REGISTRY.get(series)
        if spec is None:
            continue
        for _tok, key in _open_periods(conn, series):
            try:
                p = predict(conn, now, key, series=series)
                pmf = grid_pmf(p.dist, spec.round_rule)
                ladder = {str(k): round(v, 6) for k, v in pmf.items()}
                conn.execute(
                    "INSERT OR REPLACE INTO preds(series, period, asof, model_version,"
                    " dist_json, ladder_json, inputs_json, data_horizon, created_ts)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", pred_to_row(p, ladder))
                n += 1
            except Exception:                                # noqa: BLE001
                continue                                      # shadow: silent skip
    conn.commit()
    return n
