"""model/pce.py — core PCE MoM via the CPI bridge (PLAN §7). pce/0.1.0

Bridge: pce_core_mom = a + b·cpi_core_mom, fit PIT on the trailing 60 months where both
prints are visible at asof. After the same-ref-month CPI is released (mid-month, ~2 weeks
before PCE), the bridge input is the ACTUAL core CPI MoM → tight sigma; before that it
chains onto the CPI model's prediction with combined variance. This asymmetry is the
"PCE reprices lazily after CPI day" hunting ground (§19.9).
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd

from prediction_market_macro.model import cpi as cpi_model
from prediction_market_macro.model.common import GaussianMix, Pred
from prediction_market_macro.model.features import FeatureStore

VERSION = "pce/0.1.0"


def predict(conn, asof: datetime, period: str, series: str = "KXPCECORE") -> Pred:
    fs = FeatureStore(conn)
    pce_idx, h_pce = fs.fred_series("PCEPILFE", asof)
    cpi_idx, h_cpi = fs.fred_series("CPILFESL", asof)
    pce_mom = (pce_idx / pce_idx.shift(1) - 1) * 100
    cpi_mom = (cpi_idx / cpi_idx.shift(1) - 1) * 100
    df = pd.DataFrame({"pce": pce_mom, "cpi": cpi_mom}).dropna().tail(60)
    assert len(df) >= 36, "bridge history too short"
    b, a = np.polyfit(df["cpi"].values, df["pce"].values, 1)
    resid_sig = max(float(np.std(df["pce"].values - (a + b * df["cpi"].values))), 0.04)

    ref = pd.Period(period)
    cpi_m = cpi_mom.copy()
    cpi_m.index = cpi_m.index.to_period("M")
    horizon = max(h_pce or "", h_cpi or "")
    if ref in cpi_m.index and not np.isnan(cpi_m.loc[ref]):
        x = float(cpi_m.loc[ref])                     # CPI for the ref month already printed
        mu, sigma = a + b * x, resid_sig
        mode = "bridge_on_actual_cpi"
    else:
        cp = cpi_model.predict_mom(conn, asof, period, core=True)
        _, x, cs = cp.dist.comps[0]
        mu = a + b * x
        sigma = math.hypot(resid_sig, abs(b) * cs)
        mode = "bridge_on_predicted_cpi"
        horizon = max(horizon, cp.data_horizon.isoformat())
    return Pred(series="KXPCECORE", period=period, dist=GaussianMix(((1.0, float(mu),
                float(sigma)),)), asof=asof, model_version=VERSION,
                inputs={"a": round(float(a), 4), "b": round(float(b), 4),
                        "resid_sigma": round(resid_sig, 4), "mode": mode,
                        "cpi_input": round(float(x), 4)},
                data_horizon=datetime.fromisoformat(horizon))
