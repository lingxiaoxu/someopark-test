"""model/payrolls.py — NFP monthly change (PLAN §7). payrolls/0.1.0

Label discipline (§5-bis.2): the PRINTED headline change = level(t) − level(t−1) both read
from the SAME first vintage of month t — reconstructed exactly from the ALFRED store.
Model: mu = 0.6·(3-month avg of printed changes) + 0.4·claims_signal; claims_signal maps
the change in the 4-week claims average (month-over-month, PIT) at −2 jobs per claim.
Fat tails: two-component mixture (0.8 @ σ=55k, 0.2 @ σ=140k).
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from prediction_market_macro.model.common import GaussianMix, Pred
from prediction_market_macro.model.features import FeatureStore

VERSION = "payrolls/0.1.0"


def printed_changes(conn, asof: datetime) -> pd.Series:
    """Headline NFP change per month, per its own first vintage (PIT-visible only)."""
    rows = conn.execute(
        "SELECT event_time, value, vintage_date, knowledge_time FROM fred_obs"
        " WHERE sid='PAYEMS' AND knowledge_time<=? ORDER BY event_time, vintage_date",
        (asof.isoformat(),)).fetchall()
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


def predict(conn, asof: datetime, period: str, series: str = "KXPAYROLLS") -> Pred:
    fs = FeatureStore(conn)
    ch = printed_changes(conn, asof)
    assert len(ch) >= 12, "payrolls print history too short"
    icsa, h_c = fs.fred_series("ICSA", asof)
    base = float(ch.tail(3).mean())
    c4 = icsa.rolling(4).mean().dropna()
    if len(c4) >= 9:
        claims_delta = float(c4.iloc[-1] - c4.iloc[-5])         # ~1 month apart
        claims_signal = base - 2.0 * claims_delta / 1.0 * 1.0    # −2 jobs per claim
        claims_signal = float(np.clip(claims_signal, base - 150_000, base + 150_000))
    else:
        claims_signal = base
    mu = 0.6 * base + 0.4 * claims_signal
    dist = GaussianMix(((0.8, mu, 55_000.0), (0.2, mu, 140_000.0)))
    h = conn.execute("SELECT MAX(knowledge_time) m FROM fred_obs WHERE sid='PAYEMS'"
                     " AND knowledge_time<=?", (asof.isoformat(),)).fetchone()["m"]
    horizon = max(h or asof.isoformat(), h_c or "")
    return Pred(series="KXPAYROLLS", period=period, dist=dist, asof=asof,
                model_version=VERSION,
                inputs={"base_3m": round(base, 0), "claims_signal": round(claims_signal, 0),
                        "mu": round(mu, 0), "n_prints": len(ch)},
                data_horizon=datetime.fromisoformat(horizon))
