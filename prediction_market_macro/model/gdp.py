"""model/gdp.py — KXGDP: BEA advance-estimate real GDP growth (SAAR) (PLAN §7 P0/P1).

Anchor = the latest PIT-visible GDPNow vintage for the reference quarter (Atlanta Fed
model, ingested as true ALFRED vintages via ingest/nowcast). Sigma = the historical
error of the LAST pre-release GDPNow reading against the advance first print
(A191RL1Q225SBEA vintages), floored at 0.5pp — the documented GDPNow RMSE regime.

0.2.0 splits the OFF-quarter path off that anchor. GDPNow nowcasts the quarter it is
running on and nothing else; using it unshrunk as the mean of a contract four quarters
out, and widening sigma by a flat 0.5pp however far out that is, was the single largest
modelling error on this series. See `_ar1_offquarter`.

predict(asof) is deterministic given the db; all reads are knowledge_time <= asof.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from prediction_market_macro.model.common import GaussianMix, Pred
from prediction_market_macro.model.features import FeatureStore

VERSION = "gdp/0.2.0"
_LABEL_SID = "A191RL1Q225SBEA"          # real GDP % change SAAR, first prints
SIGMA_FLOOR = 0.5

# 0.1.0's `offquarter_widen` (a flat +0.5pp in quadrature, horizon-independent) is
# retired — accepted so an adopted param set cannot crash the daily run, recorded on every
# stored pred, and able only to widen. KXGDP has no manual_params row and no param_space
# entry today, so nothing in production is passing it.
_RETIRED = ("offquarter_widen",)

# defaults == the registered gdp/0.2.0 behaviour. NOTE (2026-08-04): KXGDP has exactly
# ONE settled event in the db, so this interface exists for uniformity — the series
# cannot support walk-forward selection and must keep running on the defaults until it
# has a history. Do not point a grid at it.
DEFAULT_PARAMS = {
    "sigma_floor": SIGMA_FLOOR,   # floor on the GDPNow-vs-advance error sigma (pp)
    "min_errs": 6,                # error pairs needed before the empirical sigma is used
    "ar_window": 80,              # quarters of first prints behind the off-quarter AR(1)
    "ar_winsor": 2.5,             # per-tail % clipped before the fit (see _ar1_offquarter)
    "ar_min_obs": 45,             # below this the fit degenerates to phi=0 (pure mean)
}


def _quarter_period(period: str) -> str:
    """Reference quarter for a period key. '2026-Q2' → itself. A DATE/month key is
    the RELEASE date — the advance estimate reports the PREVIOUS quarter
    (Oct 30 release = Q3 GDP), so shift back one quarter."""
    if "Q" in period.upper():
        return period.upper()
    ts = pd.Period(period, freq="M") - 3
    return f"{ts.year}-Q{(ts.month - 1) // 3 + 1}"


def _as_quarter(q: str) -> pd.Period:
    return pd.Period(q.upper().replace("-", ""), freq="Q")


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


def _ar1_offquarter(conn, asof: datetime, p: dict) -> tuple[float, float, float, int]:
    """(phi, long-run mean, one-step residual sigma, n) — AR(1) on the visible first
    prints of real GDP growth, winsorised, over a trailing window.

    This is what makes the off-quarter branch horizon-aware. Measured on the ALFRED
    history: scoring a GDPNow reading directly against the advance print of Q+k gives
    RMSE 0.99pp at k=0 but 2.54 / 2.63 / 2.18 / 2.76 at k=1..4 (ex-2020). The jump is a
    STEP at k=1, not a ramp — quarter-to-quarter growth barely persists (phi = 0.364 on
    1947-, 0.338 on 1985-, −0.026 on 2010-), so essentially all of this quarter's news is
    gone by the next one. 0.1.0's flat hypot(1.302, 0.5) = 1.394pp for every k was
    therefore too narrow by ~1.3-1.9x, and its mu anchored 2027 contracts on a 4.03/4.95pp
    current-quarter nowcast against a long-run mean near 2.0-2.5pp.

    Out-of-sample, PIT, 136 records over 37 anchor quarters: RMSE(raw GDPNow) = 2.533 vs
    RMSE(m + phi^k·(GDPNow − m)) = 1.57. Anchor-clustered bootstrap (B=4000, resampled by
    anchor because k=1..4 off one anchor share a mu error): the gap is +0.772pp with a 90%
    interval of [+0.395, +1.146] and P(shrink better) = 100%.

    Winsorising is what replaces a hand-drawn "drop 2020" — production sees the history it
    is standing in, not the one with the pandemic already excised. Without it phi flips to
    −0.23 on a window containing 2020Q2/Q3 and the shrink starts oscillating. At the
    shipped 80 / 2.5% the fit reads phi=0.298, m=2.04, sigma=1.83, and P(|e|>sigma) is 20%
    ex-2020 / 31% including it (a single Gaussian implies 31.7%).
    """
    prints, _ = FeatureStore(conn).fred_first_prints(_LABEL_SID, asof)
    v = prints.dropna().sort_index().to_numpy(dtype=float)
    n = len(v)
    if n < 3:
        raise RuntimeError(f"KXGDP: only {n} visible GDP first prints at {asof}")
    w = v[-int(p["ar_window"]):]
    pct = float(p["ar_winsor"])
    if pct > 0:
        w = np.clip(w, np.percentile(w, pct), np.percentile(w, 100.0 - pct))
    if n < int(p["ar_min_obs"]):
        # too short to identify persistence; shrink all the way to the sample mean rather
        # than to a phi fitted on noise. Well-defined, and the conservative direction.
        m = float(np.mean(w))
        return 0.0, m, _rsd(w - m), n
    phi_raw, c = (float(x) for x in np.polyfit(w[:-1], w[1:], 1))
    sig = _rsd(w[1:] - (c + phi_raw * w[:-1]))          # residual of the fit as fitted
    # A negative or explosive phi is a fit artefact, not persistence that reverses sign
    # every quarter. When the clip bites, the fitted intercept no longer defines a
    # coherent long-run mean either, so fall back to the window mean for both.
    if 0.0 <= phi_raw < 0.95:
        return phi_raw, float(c / (1.0 - phi_raw)), sig, n
    return float(np.clip(phi_raw, 0.0, 0.95)), float(np.mean(w)), sig, n


def _rsd(e) -> float:
    """1.4826·MAD — the same robust scale cpi.py/claims.py/payrolls.py use."""
    e = np.asarray(e, dtype=float)
    return 1.4826 * float(np.median(np.abs(e - np.median(e))))


def predict(conn, asof: datetime, period: str, series: str = "KXGDP",
            params: dict | None = None) -> Pred:
    p = {**DEFAULT_PARAMS, **(params or {})}
    retired = {k: float(p.pop(k)) for k in _RETIRED if k in p}
    sig_kw = {"sigma_floor": float(p["sigma_floor"]), "min_errs": int(p["min_errs"])}
    from prediction_market_macro.ingest.nowcast import latest_nowcast
    q = _quarter_period(period)
    nc = conn.execute(
        "SELECT value, knowledge_time FROM nowcast_vintages WHERE source='GDPNow'"
        " AND event_time=? AND knowledge_time<=? ORDER BY knowledge_time DESC LIMIT 1",
        (q, asof.isoformat())).fetchone()
    extra: dict = {}
    if nc is None:
        got = latest_nowcast(conn, "KXGDP", asof)
        if got is None:
            raise RuntimeError(f"KXGDP: no GDPNow vintage visible at {asof}")
        anchor_q, nowcast, horizon = got
        k = max((_as_quarter(q) - _as_quarter(anchor_q)).n, 1)
        phi, m, sig_ar, n_ar = _ar1_offquarter(conn, asof, p)
        mu = m + (phi ** k) * (float(nowcast) - m)
        # anchor error decays as phi^k; AR shocks accumulate as 1+phi^2+...+phi^(2k-2)
        anchor_sig = _nowcast_error_sigma(conn, asof, **sig_kw) * (phi ** k)
        ar_sig = sig_ar * math.sqrt(sum(phi ** (2 * j) for j in range(k)))
        sigma = max(math.hypot(anchor_sig, ar_sig), float(p["sigma_floor"]))
        mode = "gdpnow_offquarter"
        # `gdpnow` stays the RAW nowcast in both branches and `mu` is what was priced.
        # 0.1.0 stored the mean under the name `gdpnow` because off-quarter they were the
        # same number; now they are not, and a key that silently changes meaning between
        # modes is how a reader ends up comparing a shrunk mean to an unshrunk one.
        extra = {"anchor_quarter": anchor_q, "k_quarters": k,
                 "ar_phi": round(phi, 4), "ar_mean": round(m, 3),
                 "ar_sigma": round(sig_ar, 3), "n_ar": n_ar}
    else:
        mu, horizon = float(nc["value"]), nc["knowledge_time"]
        nowcast, sigma = mu, _nowcast_error_sigma(conn, asof, **sig_kw)
        mode = "gdpnow_anchor"
        extra = {"k_quarters": 0}
    inputs = {"mu": round(float(mu), 3), "gdpnow": round(float(nowcast), 2),
              "sigma": round(sigma, 3), "ref_quarter": q, "mode": mode, **extra}
    # A retired key was supplied and does not do what its name promises — recorded on every
    # pred rather than raised, which would take the series down until a human intervenes.
    # `offquarter_widen` is INERT, not re-applied as a floor: it was an additive fudge for
    # exactly the horizon uncertainty the AR(1) branch now measures, so honouring it would
    # double-count the thing it was standing in for.
    inputs.update({f"{key}_retired": val for key, val in retired.items()})
    # Pred.period = the CALLER'S key (contract-date key from predict_all) so the
    # stored row is findable by decide_all/watchdog; the quarter only routes the
    # nowcast lookup internally
    return Pred(series="KXGDP", period=period,
                dist=GaussianMix(((1.0, float(mu), float(sigma)),)),
                asof=asof, model_version=VERSION, inputs=inputs,
                data_horizon=datetime.fromisoformat(horizon))
