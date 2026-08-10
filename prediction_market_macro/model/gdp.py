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

# defaults == the registered gdp/0.1.0 behaviour. NOTE (2026-08-04): KXGDP has exactly
# ONE settled event in the db, so this interface exists for uniformity — the series
# cannot support walk-forward selection and must keep running on the defaults until it
# has a history. Do not point a grid at it.
DEFAULT_PARAMS = {
    "sigma_floor": SIGMA_FLOOR,   # floor on the GDPNow-vs-advance error sigma (pp)
    "offquarter_widen": 0.5,      # extra sigma added in quadrature off-quarter
    "min_errs": 6,                # error pairs needed before the empirical sigma is used
}


def _quarter_period(period: str) -> str:
    """Reference quarter for a period key. '2026-Q2' → itself. A DATE/month key is
    the RELEASE date — the advance estimate reports the PREVIOUS quarter
    (Oct 30 release = Q3 GDP), so shift back one quarter."""
    if "Q" in period.upper():
        return period.upper()
    ts = pd.Period(period, freq="M") - 3
    return f"{ts.year}-Q{(ts.month - 1) // 3 + 1}"


def _nowcast_error_sigma(conn, asof: datetime, sigma_floor: float = SIGMA_FLOOR,
                         min_errs: int = 6) -> float:
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
    if len(errs) < min_errs:
        return sigma_floor
    return max(float(np.std(errs)), sigma_floor)


def predict(conn, asof: datetime, period: str, series: str = "KXGDP",
            params: dict | None = None) -> Pred:
    p = {**DEFAULT_PARAMS, **(params or {})}
    sig_kw = {"sigma_floor": float(p["sigma_floor"]), "min_errs": int(p["min_errs"])}
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
        sigma = math.hypot(_nowcast_error_sigma(conn, asof, **sig_kw),
                           float(p["offquarter_widen"]))
        mode = "gdpnow_offquarter"
    else:
        mu, horizon = float(nc["value"]), nc["knowledge_time"]
        sigma = _nowcast_error_sigma(conn, asof, **sig_kw)
        mode = "gdpnow_anchor"
    # Pred.period = the CALLER'S key (contract-date key from predict_all) so the
    # stored row is findable by decide_all/watchdog; the quarter only routes the
    # nowcast lookup internally
    return Pred(series="KXGDP", period=period,
                dist=GaussianMix(((1.0, float(mu), float(sigma)),)),
                asof=asof, model_version=VERSION,
                inputs={"gdpnow": round(float(mu), 2), "sigma": round(sigma, 3),
                        "ref_quarter": q, "mode": mode},
                data_horizon=datetime.fromisoformat(horizon))
