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

# defaults == the registered payrolls/0.1.0 behaviour. Every key must be able to MOVE
# the output — see tests/test_claims_params.py.
DEFAULT_PARAMS = {
    "base_months": 3,           # trailing printed changes averaged into the base
    "w_base": 0.6,              # weight on base; (1-w_base) goes to the claims signal
    "jobs_per_claim": 2.0,      # jobs lost per extra initial claim
    "claims_clip": 150_000,     # cap on how far the claims signal may pull off base
    "sigma_core": 55_000.0,     # narrow component
    "sigma_tail": 140_000.0,    # fat component
    "w_tail": 0.2,              # weight on the fat component
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


def predict(conn, asof: datetime, period: str, series: str = "KXPAYROLLS",
            params: dict | None = None) -> Pred:
    p = {**DEFAULT_PARAMS, **(params or {})}
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
    wt = float(p["w_tail"])
    dist = GaussianMix(((1.0 - wt, mu, float(p["sigma_core"])),
                        (wt, mu, float(p["sigma_tail"]))))
    _, h = FeatureStore(conn).fred_first_prints("PAYEMS", asof)
    horizon = max(h or asof.isoformat(), h_c or "")
    return Pred(series="KXPAYROLLS", period=period, dist=dist, asof=asof,
                model_version=VERSION,
                inputs={"base_3m": round(base, 0), "claims_signal": round(claims_signal, 0),
                        "mu": round(mu, 0), "n_prints": len(ch)},
                data_horizon=datetime.fromisoformat(horizon))
