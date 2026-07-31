"""model/cpi.py — CPI MoM/YoY, headline & core (PLAN §7, §19.1). cpi/0.1.0

Serves KXCPI, KXCPICORE (MoM ladders) and KXCPIYOY, KXCPICOREYOY (YoY ladders).

Mechanics (all inputs via PIT vintage reads):
  * UNROUNDED index modelling (§19.1): MoM % computed from the CPI index level series
    (3-decimal precision), predicted in continuous space, rounded ONLY at discretisation.
  * core MoM:    mu = 0.5·m[-1] + 0.3·m[-2] + 0.2·(trailing-12 mean); sigma = MAD of the
                 last 18 MoM values (floor 0.06pp)
  * headline MoM: core pred + gasoline effect: CPI gasoline weight (~3.1%) × month-avg
                 pump-price %Δ (GASREGW weekly PIT; RB=F month change × 0.55 passthrough
                 fills the unobserved remainder of the month) + food drift constant;
                 sigma widened by energy share still unobserved
  * YoY (exact): index recursion I_t = I_{t-1}(1+mom/100); YoY = (I_t/I_{t-12}-1)·100 —
                 the MoM distribution maps DETERMINISTICALLY onto YoY (known base), so the
                 YoY ladder is a change of variables, not a second model.
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd

from prediction_market_macro.model.common import GaussianMix, Pred, grid_pmf
from prediction_market_macro.model.features import FeatureStore

VERSION = "cpi/0.1.0"
GAS_WEIGHT = 0.031
FOOD_DRIFT = 0.03            # pp/month, long-run food contribution
RB_PASSTHROUGH = 0.55


def _mom_series(idx: pd.Series) -> pd.Series:
    return (idx / idx.shift(1) - 1) * 100


def _core_mu_sigma(mom: pd.Series) -> tuple[float, float]:
    m = mom.dropna()
    assert len(m) >= 24, "CPI history too short"
    mu = 0.5 * m.iloc[-1] + 0.3 * m.iloc[-2] + 0.2 * float(m.tail(12).mean())
    resid = m.tail(18) - m.tail(18).mean()
    sigma = max(1.4826 * float(np.median(np.abs(resid))), 0.06)
    return float(mu), float(sigma)


def _gas_effect(fs: FeatureStore, asof: datetime, ref_month: str) -> tuple[float, float]:
    """(gasoline pp contribution to headline MoM, extra sigma). PIT: GASREGW weekly +
    RB=F for the unobserved remainder."""
    gas, _ = fs.fred_series("GASREGW", asof)
    gas = gas.dropna()
    ref = pd.Period(ref_month)
    cur = gas[gas.index.to_period("M") == ref]
    prev = gas[gas.index.to_period("M") == ref - 1]
    rb, _ = fs.fut_closes("RB", asof, n=40)
    obs_frac = min(len(cur) / 4.3, 1.0)
    if len(prev) == 0 or (len(cur) == 0 and len(rb) < 5):
        return 0.0, 0.10
    prev_avg = float(prev.mean())
    if len(cur) > 0:
        cur_avg = float(cur.mean())
        if obs_frac < 1.0 and len(rb) >= 5:
            rb_chg = float(rb.iloc[-1] / rb.iloc[-min(10, len(rb))] - 1)
            cur_avg *= (1 + rb_chg * RB_PASSTHROUGH * (1 - obs_frac))
    else:
        rb_chg = float(rb.iloc[-1] / rb.iloc[-min(22, len(rb))] - 1)
        cur_avg = prev_avg * (1 + rb_chg * RB_PASSTHROUGH)
    pump_pct = (cur_avg / prev_avg - 1) * 100
    contrib = GAS_WEIGHT * pump_pct
    extra_sigma = 0.04 + 0.08 * (1 - obs_frac)
    return float(np.clip(contrib, -0.8, 0.8)), float(extra_sigma)


def _horizon_widen(sigma: float, idx: pd.Series, ref_month: str) -> float:
    """Uncertainty inflation for far-out reference months: +10% sigma per month beyond
    the next unprinted month (parameter drift; near month unchanged)."""
    last = idx.dropna().index.max().to_period("M") if hasattr(
        idx.dropna().index.max(), "to_period") else pd.Period(str(idx.dropna().index.max())[:7])
    ahead = max((pd.Period(ref_month) - last).n, 1)
    return sigma * (1.0 + 0.10 * (ahead - 1))


def _predict_mom(conn, asof: datetime, ref_month: str, core: bool) -> tuple[Pred, pd.Series]:
    fs = FeatureStore(conn)
    sid = "CPILFESL" if core else "CPIAUCSL"
    idx, h1 = fs.fred_series(sid, asof)
    mom = _mom_series(idx)
    mu_c, sg_c = _core_mu_sigma(_mom_series(fs.fred_series("CPILFESL", asof)[0]))
    horizon = h1
    if core:
        mu, sigma = mu_c, _horizon_widen(sg_c, idx, ref_month)
        inputs = {"core_mu": round(mu_c, 4), "core_sigma": round(sigma, 4)}
    else:
        gas_pp, gas_sig = _gas_effect(fs, asof, ref_month)
        mu = mu_c + gas_pp + FOOD_DRIFT
        sigma = _horizon_widen(math.hypot(sg_c, gas_sig), idx, ref_month)
        inputs = {"core_mu": round(mu_c, 4), "gas_pp": round(gas_pp, 4),
                  "food_drift": FOOD_DRIFT, "sigma": round(sigma, 4)}
    series = "KXCPICORE" if core else "KXCPI"
    pred = Pred(series=series, period=ref_month, dist=GaussianMix(((1.0, mu, sigma),)),
                asof=asof, model_version=VERSION, inputs=inputs,
                data_horizon=datetime.fromisoformat(horizon))
    return pred, idx


def predict_mom(conn, asof: datetime, ref_month: str, core: bool) -> Pred:
    return _predict_mom(conn, asof, ref_month, core)[0]


def predict_yoy(conn, asof: datetime, ref_month: str, core: bool) -> Pred:
    """Exact change of variables: YoY grid pmf from the MoM pmf + the known index base."""
    mom_pred, idx = _predict_mom(conn, asof, ref_month, core)
    ref = pd.Period(ref_month)
    idx_m = idx.copy()
    idx_m.index = idx_m.index.to_period("M")
    try:
        i_base = float(idx_m.loc[ref - 12])
    except KeyError as e:
        raise RuntimeError(f"missing CPI YoY base month for {ref_month}: {e}") from e
    # previous-month index: printed if available; otherwise CHAIN the MoM model through
    # the unprinted intermediate months (mu compounds, variance adds per month) — the
    # mathematically exact treatment for far-out YoY contracts, not a fallback.
    last_printed = idx_m.dropna().index.max()
    w, mu, sg = mom_pred.dist.comps[0]
    n_chain = max(((ref - 1) - last_printed).n, 0)
    if n_chain == 0:
        i_prev = float(idx_m.loc[ref - 1])
        chain_sigma = 0.0
    else:
        i_prev = float(idx_m.loc[last_printed]) * (1 + mu / 100) ** n_chain
        chain_sigma = sg * math.sqrt(n_chain)
    a = i_prev / i_base
    yoy_mu = (a * (1 + mu / 100) - 1) * 100
    yoy_sigma = a * math.hypot(sg, chain_sigma)
    series = "KXCPICOREYOY" if core else "KXCPIYOY"
    return Pred(series=series, period=ref_month, dist=GaussianMix(((1.0, yoy_mu, yoy_sigma),)),
                asof=asof, model_version=VERSION,
                inputs={**mom_pred.inputs, "i_prev": round(i_prev, 3),
                        "i_base": round(i_base, 3), "yoy_mu": round(yoy_mu, 3)},
                data_horizon=mom_pred.data_horizon)


def predict(conn, asof: datetime, period: str, series: str = "KXCPI") -> Pred:
    if series == "KXCPI":
        return predict_mom(conn, asof, period, core=False)
    if series == "KXCPICORE":
        return predict_mom(conn, asof, period, core=True)
    if series == "KXCPIYOY":
        return predict_yoy(conn, asof, period, core=False)
    if series == "KXCPICOREYOY":
        return predict_yoy(conn, asof, period, core=True)
    raise ValueError(series)
