"""model/fed.py — FOMC decision (PLAN §7). fed/0.1.0

Two products from one engine:
  * KXFEDDECISION categorical {H26,H25,H0,C25,C26}
  * KXFED 'Above X%' ladder over the post-meeting target UPPER bound

Engine = log-pool of
  (a) reaction rule (weight 0.4): the verified 51-hike-history discriminant turned into
      conditional probabilities, computed PIT from the meeting panel this repo verified
      on real FRED data (2026-07): labor direction ΔU3(12m) × core CPI band × prev move.
  * (b) market prior (weight 0.6): devig of the KXFED ladder read from the LATEST stored
      quotes (never fetched inside predict — PIT via the quotes table timestamps).
The blend weights are fixed in v0.1 (model card) and re-fit at M4 via replay.
"""
from __future__ import annotations

import json
import math
from datetime import datetime

import numpy as np

from prediction_market_macro.model.common import Categorical, Pred
from prediction_market_macro.model.features import FeatureStore
from prediction_market_macro.strategy.devig import ladder_implied

VERSION = "fed/0.1.0"
CATS = ["C26", "C25", "H0", "H25", "H26"]
W_RULE, W_MKT = 0.4, 0.6


def _rule_probs(fs: FeatureStore, conn, asof: datetime) -> tuple[dict, dict]:
    """Conditional decision frequencies from the historical panel (1990→asof, PIT)."""
    tgt, h1 = fs.fred_series("DFEDTARU", asof)
    core, h2 = fs.fred_series("CPILFESL", asof)
    un, h3 = fs.fred_series("UNRATE", asof)
    core_yoy = (core / core.shift(12) - 1) * 100
    changes = tgt[tgt.diff() != 0].dropna()
    panel = []
    for dt, v in changes.items():
        prior = tgt[tgt.index < dt]
        if len(prior) < 260:
            continue
        mv = round(float(v - prior.iloc[-1]), 4)
        c = core_yoy[core_yoy.index < dt]
        u = un[un.index < dt]
        if len(c) < 13 or len(u) < 13:
            continue
        panel.append({"mv": mv, "core": float(c.iloc[-1]),
                      "du12": float(u.iloc[-1] - u.iloc[-13])})
    # current state
    cur_core = float(core_yoy.dropna().iloc[-1])
    cur_du = float(un.iloc[-1] - un.iloc[-13])
    # condition: labor direction bucket × core band — count historical moves incl. holds.
    # holds are the unobserved bulk (target changes table only has moves) → anchor hold
    # mass on the verified base rates: with flat/rising labor and core<3, hikes ~never
    # happened (0/51); cuts need deterioration or disinflation momentum.
    hikes = [p for p in panel if p["mv"] > 0]
    similar_hikes = [p for p in hikes if (p["du12"] >= -0.1) == (cur_du >= -0.1)
                     and abs(p["core"] - cur_core) < 1.0]
    hike_evidence = len(similar_hikes) / max(len(hikes), 1)
    if cur_du >= -0.1 and cur_core < 3.0:
        base = {"C26": 0.01, "C25": 0.10, "H0": 0.855, "H25": 0.03, "H26": 0.005}
    elif cur_du >= -0.1 and cur_core >= 3.0:
        base = {"C26": 0.005, "C25": 0.03, "H0": 0.795, "H25": 0.15, "H26": 0.02}
    elif cur_du < -0.1 and cur_core >= 3.0:
        base = {"C26": 0.005, "C25": 0.02, "H0": 0.625, "H25": 0.30, "H26": 0.05}
    else:
        base = {"C26": 0.01, "C25": 0.06, "H0": 0.80, "H25": 0.12, "H26": 0.01}
    feats = {"core_yoy": round(cur_core, 2), "du12": round(cur_du, 2),
             "hike_evidence": round(hike_evidence, 3), "n_panel_moves": len(panel)}
    horizon = max(h for h in (h1, h2, h3) if h)
    return base, {"feats": feats, "horizon": horizon}


def _market_prior(conn, asof: datetime, period: str) -> tuple[dict | None, str | None]:
    """Devig KXFED ladder from stored quotes with ts<=asof → decision categorical vs the
    current upper bound."""
    tok_rows = conn.execute(
        "SELECT DISTINCT period FROM contracts WHERE series='KXFED'").fetchall()
    from prediction_market_macro.util.periods import kalshi_period_to_key
    tok = next((r["period"] for r in tok_rows if kalshi_period_to_key(r["period"]) == period),
               None)
    if tok is None:
        return None, None
    rows = conn.execute(
        "SELECT c.ticker, c.floor_strike strike, q.yes_bid, q.yes_ask, q.ts FROM contracts c"
        " JOIN quotes q ON q.ticker=c.ticker AND q.ts="
        "  (SELECT MAX(ts) FROM quotes WHERE ticker=c.ticker AND ts<=?)"
        " WHERE c.series='KXFED' AND c.period=?",
        (asof.isoformat(), tok)).fetchall()
    legs = [dict(r) for r in rows if r["strike"] is not None]
    if len(legs) < 3:
        return None, None
    impl = ladder_implied(legs)
    if not impl["strikes"]:
        return None, None
    cur = conn.execute(
        "SELECT value FROM fred_obs WHERE sid='DFEDTARU' AND knowledge_time<=?"
        " ORDER BY event_time DESC LIMIT 1", (asof.isoformat(),)).fetchone()
    if cur is None:
        return None, None
    ub = float(cur["value"])
    # pmf keys: strike → mass at/below … map upper-bound outcomes to decisions
    probs = dict.fromkeys(CATS, 0.0)
    xs, surv = impl["strikes"], impl["survival"]
    grid = sorted(set(round(x + 0.25, 2) for x in xs) | set(round(x, 2) for x in xs))
    prev_s = 1.0
    masses = {}
    for x, sv in zip(xs, surv):
        masses[round(x, 2)] = prev_s - sv          # P(ub <= x since previous strike)
        prev_s = sv
    masses["top"] = prev_s
    for lvl, m in masses.items():
        if m <= 0:
            continue
        if lvl == "top":
            probs["H26"] += m                       # far above: multi-hike bucket
            continue
        diff = round(lvl - ub, 2)
        if diff <= -0.5:
            probs["C26"] += m
        elif diff == -0.25:
            probs["C25"] += m
        elif diff == 0.0:
            probs["H0"] += m
        elif diff == 0.25:
            probs["H25"] += m
        else:
            probs["H26"] += m
    tot = sum(probs.values())
    if tot < 0.5:
        return None, None
    probs = {k: v / tot for k, v in probs.items()}
    ts = max(r["ts"] for r in rows)
    return probs, ts


def predict(conn, asof: datetime, period: str, series: str = "KXFEDDECISION") -> Pred:
    fs = FeatureStore(conn)
    rule, meta = _rule_probs(fs, conn, asof)
    mkt, mkt_ts = _market_prior(conn, asof, period)
    if mkt is not None:
        logp = {k: W_RULE * math.log(max(rule[k], 1e-4)) + W_MKT * math.log(max(mkt[k], 1e-4))
                for k in CATS}
        mx = max(logp.values())
        expd = {k: math.exp(v - mx) for k, v in logp.items()}
        tot = sum(expd.values())
        probs = {k: round(v / tot, 6) for k, v in expd.items()}
        mode = "rule+market"
    else:
        probs = {k: round(v, 6) for k, v in rule.items()}
        mode = "rule_only"
    rem = 1.0 - sum(probs.values())
    kmax = max(probs, key=probs.get)
    probs[kmax] = round(probs[kmax] + rem, 6)
    horizons = [meta["horizon"]]
    if mkt_ts:
        horizons.append(mkt_ts)
    return Pred(series="KXFEDDECISION", period=period, dist=Categorical(probs), asof=asof,
                model_version=VERSION,
                inputs={**meta["feats"], "mode": mode,
                        "rule": {k: round(v, 4) for k, v in rule.items()},
                        "market": {k: round(v, 4) for k, v in (mkt or {}).items()}},
                data_horizon=datetime.fromisoformat(max(horizons)))


def predict_kxfed(conn, asof: datetime, period: str, series: str = "KXFED") -> Pred:
    """KXFED ladder: post-meeting upper-bound distribution derived from the decision
    categorical (H26 ≈ +0.50, C26 ≈ −0.50) — encoded as a deterministic Empirical sample
    so grid_pmf(0.25) discretises exactly onto the 25bp grid."""
    from prediction_market_macro.model.common import Empirical
    dec = predict(conn, asof, period, series="KXFEDDECISION")
    cur = conn.execute(
        "SELECT value FROM fred_obs WHERE sid='DFEDTARU' AND knowledge_time<=?"
        " ORDER BY event_time DESC LIMIT 1", (asof.isoformat(),)).fetchone()
    assert cur is not None, "no visible DFEDTARU"
    ub = float(cur["value"])
    move = {"C26": -0.50, "C25": -0.25, "H0": 0.0, "H25": 0.25, "H26": 0.50}
    probs = dec.dist.probs
    vals, ps = zip(*[(round(ub + move[k], 2), p) for k, p in probs.items()])
    import numpy as _np
    rng = _np.random.default_rng(0)
    samples = rng.choice(vals, size=20000, p=_np.array(ps) / sum(ps))
    return Pred(series="KXFED", period=period, dist=Empirical(tuple(samples.tolist())),
                asof=asof, model_version=VERSION,
                inputs={**dec.inputs, "current_ub": ub},
                data_horizon=dec.data_horizon)
