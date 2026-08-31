"""model/cpi.py — CPI MoM/YoY, headline & core (PLAN §7, §19.1). cpi/0.4.0

Serves KXCPI, KXCPICORE (MoM ladders) and KXCPIYOY, KXCPICOREYOY (YoY ladders).

Mechanics (all inputs via PIT vintage reads):
  * UNROUNDED index modelling (§19.1): MoM % computed from the CPI index level series
    (3-decimal precision), predicted in continuous space, rounded ONLY at discretisation.
  * core MoM:    mu = 0.5·m[-1] + 0.5·(trailing-12 mean); sigma = MAD of the last 18 MoM
                 values (floor 0.06pp). 0.2.0 dropped a 0.3 weight on the SECOND lag,
                 which was redundant given the first and cost only variance — §29.6.
  * headline MoM: core pred + gasoline effect: CPI gasoline weight (~3.1%) × month-avg
                 pump-price %Δ (GASREGW weekly PIT; RB=F month change × 0.55 passthrough
                 fills the unobserved remainder of the month) + food drift constant;
                 sigma widened by energy share still unobserved
  * YoY (exact): index recursion I_t = I_{t-1}(1+mom/100); YoY = (I_t/I_{t-12}-1)·100 —
                 the MoM distribution maps DETERMINISTICALLY onto YoY (known base), so the
                 YoY ladder is a change of variables, not a second model.
  * 0.3.0 — BOTH YoY mus are anchored on the Cleveland Fed daily nowcast (PIT
                 `cleveland_nowcast.latest`; headline reads measure 'cpi', core reads
                 'corecpi'), sigma and everything else unchanged. The evidence behind
                 each, stated separately because it is NOT the same strength
                 (leak-free T-26h replay on settled ladder legs, adopted params):
                   headline  45 events 2022→2026: per-leg Brier 0.0904→0.0610 (−33%),
                             29/45 improve, EVERY year slice 2023-2026 improves —
                             decisive, anchored on the evidence.
                   core      44 events: 0.0618→0.0613 (Δ−0.0005), 19/44 — a wash; the
                             internal core model already sits at the nowcast's level.
                             Anchored by EXPLICIT USER DECISION 2026-08-15 (uniformity
                             across the YoY pair), not by the evidence. If the forward
                             confirmation window turns against core, this is the line
                             to revisit first.
                 Missing/stale nowcast (>NOWCAST_MAX_AGE_D) falls back to the internal
                 chain, so the feed dying degrades to 0.2.0 behaviour, never to an error.
  * 0.4.0 — the SAME anchor on the HEADLINE MoM leg (KXCPI only), reading measure 'cpi'
                 freq 'mom'. Those vintages were ingested from the start and never read.
                 PR-10; the evidence and the explicit core rejection are in _nowcast's
                 docstring, and the anchor sits in predict_mom (not _predict_mom) so
                 predict_yoy and model/pce.py are bit-identical to 0.3.0.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime

import numpy as np
import pandas as pd

from prediction_market_macro.ingest.cleveland_nowcast import latest as _nowcast_latest
from prediction_market_macro.model.common import GaussianMix, Pred, grid_pmf
from prediction_market_macro.model.features import FeatureStore

VERSION = "cpi/0.4.0"
NOWCAST_MAX_AGE_D = 7        # nowcast is business-daily; older than this = feed died,
                             # fall back to the internal chain rather than anchor stale
GAS_WEIGHT = 0.031
FOOD_DRIFT = 0.03            # pp/month, long-run food contribution
RB_PASSTHROUGH = 0.55

# defaults == the registered cpi/0.2.0 behaviour, and this module backs FOUR series
# (KXCPI, KXCPICORE, KXCPIYOY, KXCPICOREYOY) — one param set is selected on their pooled
# history, not four independent ones. Every key must be able to MOVE the output; see
# tests/test_claims_params.py for the failure mode that rule exists to catch.
#
# w_last is exposed but it is NOT the free knob it looks like: §29.6 measured the RMSE
# grid as flat across 0.3..0.5, so a grid that "finds" 0.35 has found noise. The second
# lag stays out entirely — it is redundant given lag-1 and cost only variance.
DEFAULT_PARAMS = {
    "w_last": 0.5,             # weight on m[-1]; the remainder goes to the trailing mean
    "mean_window": 12,         # months in the trailing mean
    "mad_window": 18,          # months the MAD sigma is measured over
    "sigma_floor": 0.06,       # pp
    "gas_weight": GAS_WEIGHT,  # CPI gasoline relative importance
    "rb_passthrough": RB_PASSTHROUGH,   # RB=F -> pump passthrough on the unobserved tail
    "gas_clip": 0.8,           # pp cap on the gasoline contribution
    "gas_sigma_base": 0.04,    # extra sigma when the month is fully observed
    "gas_sigma_unobs": 0.08,   # additional extra sigma at zero observation
    "food_drift": FOOD_DRIFT,
    "horizon_widen": 0.10,     # sigma inflation per month beyond the next unprinted one
    # PR-8 (0.3.0, YoY) and PR-10 (0.4.0, headline MoM) each registered a SIX-EVENT forward
    # criterion, and both are graded by `research/shadow_nowcast.py` (#195). Grading needs
    # the UN-anchored arm produced by this same code at the same asof — reconstructing it
    # from `inputs["yoy_mu_model"]` would round the mu to 3dp, and importing an old copy of
    # the module would let something other than the anchor differ between the arms.
    #
    # This is not a new branch. `_nowcast` already returns None when the feed is stale or
    # the target month has no row yet, and production already falls back to the internal
    # chain there; the key only makes that existing path reachable on demand. Default True
    # is bit-identical — proved on the live db, 2026-08-27, against HEAD's copy of this
    # module loaded side by side: 200/200 settled CPI-family predictions match exactly,
    # comps + inputs + data_horizon. So VERSION is deliberately NOT bumped. Flipped, it
    # moves 151/151 of the events where an anchor is available, so the arm it exposes is
    # a real counterfactual and not a silently-dead switch.
    #
    # Deliberately absent from BOTH selection lanes (`param_space.CANDIDATES` and
    # `param_argmin.SPACES`): whether to anchor is a registered hypothesis with a
    # preregistered falsification rule. Letting a lane flip it would answer by dollar
    # argmin on ~10 events a question PR-10 says is answerable only by six forward events
    # of paired interval log-likelihood — and would be able to un-adopt the anchor without
    # anyone writing down that the registration failed.
    "nowcast_anchor": True,
}


def _p(params: dict | None) -> dict:
    return {**DEFAULT_PARAMS, **(params or {})}


def _mom_series(idx: pd.Series) -> pd.Series:
    return (idx / idx.shift(1) - 1) * 100


def _core_mu_sigma(mom: pd.Series, params: dict | None = None) -> tuple[float, float]:
    p = _p(params)
    m = mom.dropna()
    assert len(m) >= 24, "CPI history too short"
    # The second lag used to carry 0.3 of its own. It should not: MoM's lag-2
    # autocorrelation (+0.657 core) is almost entirely THROUGH lag-1, so once L1 is in
    # the blend L2 adds no incremental signal — only the variance of a single noisy
    # print, where the 12-month mean estimates the same persistent level from 12
    # observations. Rerouting L2's weight into M12 at an UNCHANGED L1 weight of 0.5
    # (so the gain is attributable to that one move) cuts walk-forward RMSE on every
    # cut, Diebold-Mariano significant on all four: core 1995 .1121->.1057 p=.033,
    # core 2010 .1296->.1183 p=.027, headline 1995 .2915->.2661 p=.003, headline 2010
    # .2570->.2348 p=.003. The weight on L1 itself is NOT the issue — the RMSE grid is
    # flat across w=0.3..0.5, i.e. the momentum term was already about right. See §29.6.
    w = float(p["w_last"])
    mu = w * m.iloc[-1] + (1.0 - w) * float(m.tail(int(p["mean_window"])).mean())
    mw = int(p["mad_window"])
    resid = m.tail(mw) - m.tail(mw).mean()
    sigma = max(1.4826 * float(np.median(np.abs(resid))), float(p["sigma_floor"]))
    return float(mu), float(sigma)


def _gas_effect(fs: FeatureStore, asof: datetime, ref_month: str,
                params: dict | None = None) -> tuple[float, float]:
    """(gasoline pp contribution to headline MoM, extra sigma). PIT: GASREGW weekly +
    RB=F for the unobserved remainder."""
    p = _p(params)
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
    rbp = float(p["rb_passthrough"])
    if len(cur) > 0:
        cur_avg = float(cur.mean())
        if obs_frac < 1.0 and len(rb) >= 5:
            rb_chg = float(rb.iloc[-1] / rb.iloc[-min(10, len(rb))] - 1)
            cur_avg *= (1 + rb_chg * rbp * (1 - obs_frac))
    else:
        rb_chg = float(rb.iloc[-1] / rb.iloc[-min(22, len(rb))] - 1)
        cur_avg = prev_avg * (1 + rb_chg * rbp)
    pump_pct = (cur_avg / prev_avg - 1) * 100
    contrib = float(p["gas_weight"]) * pump_pct
    extra_sigma = float(p["gas_sigma_base"]) + float(p["gas_sigma_unobs"]) * (1 - obs_frac)
    clip = float(p["gas_clip"])
    return float(np.clip(contrib, -clip, clip)), float(extra_sigma)


def _horizon_widen(sigma: float, idx: pd.Series, ref_month: str,
                   params: dict | None = None) -> float:
    """Uncertainty inflation for far-out reference months: +10% sigma per month beyond
    the next unprinted month (parameter drift; near month unchanged)."""
    p = _p(params)
    last = idx.dropna().index.max().to_period("M") if hasattr(
        idx.dropna().index.max(), "to_period") else pd.Period(str(idx.dropna().index.max())[:7])
    ahead = max((pd.Period(ref_month) - last).n, 1)
    return sigma * (1.0 + float(p["horizon_widen"]) * (ahead - 1))


def _predict_mom(conn, asof: datetime, ref_month: str, core: bool,
                 params: dict | None = None) -> tuple[Pred, pd.Series]:
    p = _p(params)
    fs = FeatureStore(conn)
    sid = "CPILFESL" if core else "CPIAUCSL"
    idx, h1 = fs.fred_series(sid, asof)
    mom = _mom_series(idx)
    mu_c, sg_c = _core_mu_sigma(_mom_series(fs.fred_series("CPILFESL", asof)[0]), p)
    horizon = h1
    if core:
        mu, sigma = mu_c, _horizon_widen(sg_c, idx, ref_month, p)
        inputs = {"core_mu": round(mu_c, 4), "core_sigma": round(sigma, 4)}
    else:
        gas_pp, gas_sig = _gas_effect(fs, asof, ref_month, p)
        food = float(p["food_drift"])
        mu = mu_c + gas_pp + food
        sigma = _horizon_widen(math.hypot(sg_c, gas_sig), idx, ref_month, p)
        w = float(p.get("ercot_w", 0.0))
        if w:
            # PR-31 covariate (walk-forward PIT, model/ercot_cov.py); 0 by default
            from prediction_market_macro.model import ercot_cov
            mu += w * ercot_cov.mu_shift(conn, asof, "KXCPI")
        inputs = {"core_mu": round(mu_c, 4), "gas_pp": round(gas_pp, 4),
                  "food_drift": food, "sigma": round(sigma, 4)}
    series = "KXCPICORE" if core else "KXCPI"
    pred = Pred(series=series, period=ref_month, dist=GaussianMix(((1.0, mu, sigma),)),
                asof=asof, model_version=VERSION, inputs=inputs,
                data_horizon=datetime.fromisoformat(horizon))
    return pred, idx


def predict_mom(conn, asof: datetime, ref_month: str, core: bool,
                params: dict | None = None) -> Pred:
    """The MoM ladders (KXCPI, KXCPICORE).

    0.4.0 anchors HEADLINE mu on the Cleveland MoM nowcast — see _nowcast, PR-10.
    The anchor lives HERE and not in _predict_mom on purpose: predict_yoy and
    model/pce.py both call _predict_mom, and neither had this change measured on it, so
    the YoY channel and the PCE bridge stay bit-identical.
    """
    pred = _predict_mom(conn, asof, ref_month, core, params)[0]
    if core:
        return pred                       # PR-10 rejects core; see _nowcast's docstring
    if not _p(params)["nowcast_anchor"]:
        return pred                       # PR-10's baseline arm; see DEFAULT_PARAMS
    anch = _nowcast(conn, asof, ref_month, "cpi", "mom")
    if anch is None:
        return pred
    nc_day, val = anch
    _w, mu, sg = pred.dist.comps[0]
    inputs = {**pred.inputs, "mom_mu_model": round(float(mu), 4),
              "nowcast_date": nc_day, "mom_mu": round(val, 4)}
    return Pred(series=pred.series, period=pred.period,
                dist=GaussianMix(((1.0, float(val), sg),)), asof=pred.asof,
                model_version=VERSION, inputs=inputs,
                data_horizon=pred.data_horizon)


def _nowcast(conn, asof: datetime, ref_month: str, measure: str,
             freq: str) -> tuple[str, float] | None:
    """Cleveland nowcast (measure 'cpi'|'corecpi', freq 'yoy'|'mom') visible at `asof`,
    or None when the table is absent (bare test DBs), the target has no row yet, or the
    feed is stale.

    Both anchored branches read their OWN measure. Crosstalk here would silently feed
    core the headline nowcast, which no validation ever measured.

    WHY 'mom' IS ANCHORED ONLY FOR HEADLINE (PR-10, 2026-08-27). The MoM vintages have
    been in the db since the feed was ingested — 4,639 per measure back to 2013-08 — and
    nothing read them until now. On 60 settled KXCPI events the replacement takes the mu
    bias from +0.0432 to -0.0026, the PIT from KS 0.182 (p=0.029, 17 of 62 events in the
    bottom decile) to KS 0.099 (p=0.56), and the interval log-likelihood from -2.3313 to
    -1.9196; expanding-window OOS over 48 events is +0.5678 nats/event, DM p=0.0068.
    KXCPICORE was measured the same way and REJECTED: +0.0298 nats, DM p=0.325, 22/47
    wins, median -0.0035, and a single event carrying 101.8% of the mean gain. That is
    the same split the 0.3.0 YoY anchor found, and the reason core stays on the internal
    chain here. Do not "retry core with a blend" — PR-10 registers that as closed.

    Pre-declared risk, recorded before the forward window: the 2021 and 2022 year slices
    LOSE (-0.25 and -0.13 nats). Those are the acceleration years, when this nowcast
    systematically underestimated. If inflation re-accelerates the anchor is expected to
    lag, and that is not allowed to be discovered as an excuse afterwards.
    """
    try:
        got = _nowcast_latest(conn, measure, freq, ref_month, asof)
    except sqlite3.OperationalError:
        return None
    if got is None:
        return None
    nc_day, val = got
    if (asof.date() - date.fromisoformat(nc_day)).days > NOWCAST_MAX_AGE_D:
        return None
    return nc_day, float(val)


def _nowcast_yoy(conn, asof: datetime, ref_month: str,
                 measure: str) -> tuple[str, float] | None:
    return _nowcast(conn, asof, ref_month, measure, "yoy")


def predict_yoy(conn, asof: datetime, ref_month: str, core: bool,
                params: dict | None = None) -> Pred:
    """Exact change of variables: YoY grid pmf from the MoM pmf + the known index base."""
    mom_pred, idx = _predict_mom(conn, asof, ref_month, core, params)
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
    inputs = {**mom_pred.inputs, "i_prev": round(i_prev, 3), "i_base": round(i_base, 3)}
    # 0.3.0 anchor (module docstring): nowcast REPLACES mu, sigma stays. Headline and
    # core each read their OWN measure — crosstalk here would silently feed core the
    # headline nowcast, which no validation ever measured.
    anch = (_nowcast_yoy(conn, asof, ref_month, "corecpi" if core else "cpi")
            if _p(params)["nowcast_anchor"] else None)   # PR-8's baseline arm
    if anch is not None:
        inputs["yoy_mu_model"] = round(yoy_mu, 3)
        inputs["nowcast_date"], yoy_mu = anch
    inputs["yoy_mu"] = round(yoy_mu, 3)
    series = "KXCPICOREYOY" if core else "KXCPIYOY"
    return Pred(series=series, period=ref_month, dist=GaussianMix(((1.0, yoy_mu, yoy_sigma),)),
                asof=asof, model_version=VERSION, inputs=inputs,
                data_horizon=mom_pred.data_horizon)


def predict(conn, asof: datetime, period: str, series: str = "KXCPI",
            params: dict | None = None) -> Pred:
    if series == "KXCPI":
        return predict_mom(conn, asof, period, core=False, params=params)
    if series == "KXCPICORE":
        return predict_mom(conn, asof, period, core=True, params=params)
    if series == "KXCPIYOY":
        return predict_yoy(conn, asof, period, core=False, params=params)
    if series == "KXCPICOREYOY":
        return predict_yoy(conn, asof, period, core=True, params=params)
    raise ValueError(series)
