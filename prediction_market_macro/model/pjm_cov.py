"""model/pjm_cov.py — the PJM+ERCOT weather-severity covariate, PIT walk-forward (PR-33).

The deliberate twin of `model/ercot_cov.py`, and it exists for one measured reason: the
PJM screening (docs/PJM_NOTES.md) found exactly one candidate with a market, a pre-stated
mechanism and the right sign — grid demand ANOMALY SEVERITY as a weather proxy for initial
jobless claims — and it is the one place PJM supplies what ERCOT structurally could not.
ERCOT alone covers Texas and its |z| → ICSA correlation was −0.02; PJM covers 65M people
across 13 states, and the two together read r = +0.195 (n = 251, perm p = 0.0016).

`pjm_w = 0` (the default everywhere) is bit-identical to production.

PIT declarations, identical to the ERCOT lane's and for the same reasons:
  * EIA-930 day-D values are treated as knowable at D+2 00:00 UTC — published next-day
    with small revisions, so D+2 admits knowing it LATER than the world did.
  * The climatology (per day-of-year, ±7d window) uses PRIOR CALENDAR YEARS only.
  * The regression weight is fit at each asof on weeks that END fully before that asof —
    an expanding window that sees one more week per week, never the future. Below
    `_MIN_OBS` pairs the weight is 0 and the covariate is silent.
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd

from prediction_market_macro.model import ercot_cov

_LAG_DAYS = 2
_MIN_OBS = 52
_SHIFT_CLIP = 1.5

_cache: dict = {}


def _sev(conn) -> pd.Series:
    """mean(|z_PJM|, |z_ERCOT|) of daily demand anomaly — the screening's signal.

    Reuses `ercot_cov._clim_z` for both grids so there is exactly one implementation of
    the prior-years-only climatology in the codebase.
    """
    if "sev" not in _cache:
        z_e = ercot_cov._clim_z(conn, "eia_demand_mwh").abs()
        rows = conn.execute("SELECT date, value FROM pjm_daily WHERE"
                            " metric='eia_demand_mwh' ORDER BY date").fetchall()
        if not rows:
            _cache["sev"] = pd.Series(dtype=float)
            return _cache["sev"]
        s = pd.Series({pd.Timestamp(r[0]): float(r[1]) for r in rows})
        doy, yr, v = s.index.dayofyear.values, s.index.year.values, s.values
        z = np.full(len(s), np.nan)
        for i in range(len(s)):
            m = (yr < yr[i]) & (np.abs(((doy - doy[i] + 182) % 365) - 182) <= 7)
            if m.sum() >= 20:
                sd = v[m].std()
                if sd > 0:
                    z[i] = (v[i] - v[m].mean()) / sd
        z_p = pd.Series(z, index=s.index).dropna().abs()
        both = pd.concat([z_p.rename("p"), z_e.rename("e")], axis=1).dropna()
        _cache["sev"] = both.mean(axis=1) if len(both) else pd.Series(dtype=float)
    return _cache["sev"]


def mu_shift(conn, asof: datetime, series: str) -> float:
    """The covariate's additive shift in log-mu for KXJOBLESSCLAIMS at `asof`, 0 otherwise.

    0 whenever the walk-forward window is short, the signal is absent, or anything errs —
    the covariate must never be the reason a prediction fails.
    """
    if series != "KXJOBLESSCLAIMS":
        return 0.0
    try:
        sev = _sev(conn)
        if not len(sev):
            return 0.0
        cut = pd.Timestamp(asof.date()) - pd.Timedelta(days=_LAG_DAYS)
        vis = sev[sev.index <= cut]
        if not len(vis):
            return 0.0
        sig_w = vis.resample("W-FRI").mean().dropna()
        tgt = ercot_cov._icsa_weekly(conn, asof)
        tgt.index = tgt.index + pd.offsets.Week(weekday=4)
        cur = vis.tail(5)
        cur_sig = float(cur.mean()) if len(cur) else float("nan")
        if not math.isfinite(cur_sig):
            return 0.0
        return ercot_cov._beta_shift(sig_w, tgt, asof, cur_sig, _MIN_OBS)
    except Exception:                                            # noqa: BLE001
        return 0.0


def clear_cache() -> None:
    _cache.clear()
    ercot_cov.clear_cache()
