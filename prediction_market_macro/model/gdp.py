"""model/gdp.py — KXGDP: BEA advance-estimate real GDP growth (SAAR) (PLAN §7 P0/P1).

Anchor = the latest PIT-visible GDPNow vintage for the reference quarter (Atlanta Fed
model, ingested as true ALFRED vintages via ingest/nowcast). Sigma = the historical
error of the LAST pre-release GDPNow reading against the advance first print
(A191RL1Q225SBEA vintages), floored at 0.5pp — the documented GDPNow RMSE regime.

predict(asof) is deterministic given the db; all reads are knowledge_time <= asof.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from prediction_market_macro.model.common import GaussianMix, Pred

VERSION = "gdp/0.1.0"
_LABEL_SID = "A191RL1Q225SBEA"          # real GDP % change SAAR, first prints
SIGMA_FLOOR = 0.5


def _quarter_period(period: str) -> str:
    """Accept '2026-Q2' or '2026-07' (month token → its quarter)."""
    if "Q" in period.upper():
        return period.upper()
    ts = pd.Period(period, freq="M")
    return f"{ts.year}-Q{(ts.month - 1) // 3 + 1}"


def _nowcast_error_sigma(conn, asof: datetime) -> float:
    """Std of (final pre-release GDPNow vintage − advance first print) per quarter."""
    labels = conn.execute(
        "SELECT event_time, value, MIN(knowledge_time) kt FROM fred_obs WHERE sid=?"
        " AND knowledge_time<=? GROUP BY event_time", (_LABEL_SID, asof.isoformat())
    ).fetchall()
    errs = []
    for lab in labels:
        q = f"{pd.Timestamp(lab['event_time']).year}-Q" \
            f"{(pd.Timestamp(lab['event_time']).month - 1) // 3 + 1}"
        nc = conn.execute(
            "SELECT value FROM nowcast_vintages WHERE source='GDPNow' AND event_time=?"
            " AND knowledge_time<? ORDER BY knowledge_time DESC LIMIT 1",
            (q, lab["kt"])).fetchone()
        if nc is not None and nc["value"] is not None:
            errs.append(float(nc["value"]) - float(lab["value"]))
    if len(errs) < 6:
        return SIGMA_FLOOR
    return max(float(np.std(errs)), SIGMA_FLOOR)


def predict(conn, asof: datetime, period: str, series: str = "KXGDP") -> Pred:
    from prediction_market_macro.ingest.nowcast import latest_nowcast
    q = _quarter_period(period)
    nc = conn.execute(
        "SELECT value, knowledge_time FROM nowcast_vintages WHERE source='GDPNow'"
        " AND event_time=? AND knowledge_time<=? ORDER BY knowledge_time DESC LIMIT 1",
        (q, asof.isoformat())).fetchone()
    if nc is None:
        got = latest_nowcast(conn, "KXGDP", asof)
        if got is None:
            raise RuntimeError(f"KXGDP: no GDPNow vintage visible at {asof}")
        _, mu, horizon = got
        sigma = math.hypot(_nowcast_error_sigma(conn, asof), 0.5)   # off-quarter widen
        mode = "gdpnow_offquarter"
    else:
        mu, horizon = float(nc["value"]), nc["knowledge_time"]
        sigma = _nowcast_error_sigma(conn, asof)
        mode = "gdpnow_anchor"
    return Pred(series="KXGDP", period=q,
                dist=GaussianMix(((1.0, float(mu), float(sigma)),)),
                asof=asof, model_version=VERSION,
                inputs={"gdpnow": round(float(mu), 2), "sigma": round(sigma, 3),
                        "mode": mode},
                data_horizon=datetime.fromisoformat(horizon))
