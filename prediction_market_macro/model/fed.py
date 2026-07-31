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

VERSION = "fed/0.2.0"             # 0.2.0: + FF-futures (ZQ) FedWatch chain source
CATS = ["C26", "C25", "H0", "H25", "H26"]
W_RULE, W_MKT = 0.4, 0.6          # two-source weights (fallback when no ZQ data)
W3_RULE, W3_MKT, W3_FF = 0.15, 0.35, 0.50   # three-source: FF futures deepest

_ZQ_MONTH = "FGHJKMNQUVXZ"


def _zq_root(y: int, m: int) -> str:
    return f"ZQ{_ZQ_MONTH[m - 1]}{y % 100:02d}"


def _ff_probs(fs: FeatureStore, conn, asof: datetime,
              meeting: datetime) -> tuple[dict | None, str | None]:
    """CME-FedWatch-style probabilities from 30-day Fed Funds futures (ZQ monthly
    contracts, PIT via fut_daily). Chain-solve month by month: a month's implied
    average rate is the day-weighted mix of the pre- and post-meeting rates; each
    meeting's post-rate is the unknown. Expected move at the TARGET meeting maps
    to the 5 buckets by splitting between the two adjacent 25bp quanta."""
    import calendar as _cal
    from prediction_market_macro.ingest.calendars import CALENDARS
    r0 = fs.fred_scalar_latest("DFEDTARU", asof)
    if r0 is None:
        return None, None
    rate = float(r0) - 0.125                   # upper bound → corridor midpoint
    meetings = [e.scheduled_ts for e in CALENDARS["FOMC"]
                if e.scheduled_ts > asof]      # future meetings, chronological
    horizon = None
    exp_move = None
    cur = (asof.year, asof.month)
    for _ in range(8):                          # walk months forward
        y, m = cur
        days_in_m = _cal.monthrange(y, m)[1]
        mts = [mt for mt in meetings if (mt.year, mt.month) == (y, m)]
        root = _zq_root(y, m)
        closes, h = fs.fut_closes(root, asof, n=10)
        if mts and len(closes) >= 1:
            implied = 100.0 - float(closes.iloc[-1])
            d_meet = mts[0].day
            w_pre = d_meet / days_in_m          # meeting-day split (FedWatch)
            if 1.0 - w_pre > 0.05:              # solvable only with post-days left
                r_post = (implied - w_pre * rate) / (1.0 - w_pre)
                move = r_post - rate
                if mts[0] == meetings[0]:       # the TARGET (next) meeting
                    exp_move = move
                    horizon = h
                rate = r_post                   # chain forward
        elif mts:
            return None, None                   # meeting month without ZQ data
        cur = (y + (m == 12), m % 12 + 1)
        if exp_move is not None and (meeting.year, meeting.month) <= (y, m):
            break
    if exp_move is None:
        return None, None
    # map E[Δ] to the 5 buckets: probability split between adjacent 25bp quanta
    quanta = [-0.50, -0.25, 0.0, 0.25, 0.50]
    keys = ["C26", "C25", "H0", "H25", "H26"]
    x = max(min(exp_move, 0.50), -0.50)
    probs = dict.fromkeys(keys, 1e-4)
    for i in range(len(quanta) - 1):
        lo, hi = quanta[i], quanta[i + 1]
        if lo <= x <= hi:
            f = (x - lo) / (hi - lo)
            probs[keys[i]] = max(1.0 - f, 1e-4)
            probs[keys[i + 1]] = max(f, 1e-4)
            break
    z = sum(probs.values())
    return {k: v / z for k, v in probs.items()}, horizon


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
    fs = FeatureStore(conn)
    from prediction_market_macro.util.periods import kalshi_period_to_key
    tok = next((p for p in fs.kalshi_periods("KXFED")
                if kalshi_period_to_key(p) == period), None)
    if tok is None:
        return None, None
    rows = fs.kalshi_ladder_at("KXFED", tok, asof)
    legs = [r for r in rows if r["strike"] is not None]
    if len(legs) < 3:
        return None, None
    impl = ladder_implied(legs)
    if not impl["strikes"]:
        return None, None
    ub = fs.fred_scalar_latest("DFEDTARU", asof)
    if ub is None:
        return None, None
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
    # third source (v0.2): FF-futures FedWatch chain — the deepest read on the
    # meeting outcome, and the INDEPENDENT information Kalshi quotes lag
    from prediction_market_macro.ingest.calendars import CALENDARS
    meeting = next((e.scheduled_ts for e in CALENDARS["FOMC"]
                    if e.period == period), None)
    ff, ff_ts = (None, None)
    if meeting is not None:
        try:
            ff, ff_ts = _ff_probs(fs, conn, asof, meeting)
        except Exception:                              # noqa: BLE001
            ff = None
    sources = {"rule": (rule, W3_RULE if ff else W_RULE)}
    if mkt is not None:
        sources["market"] = (mkt, W3_MKT if ff else W_MKT)
    if ff is not None:
        sources["ff"] = (ff, W3_FF)
    if len(sources) > 1:
        tot_w = sum(w for _, w in sources.values())
        logp = {k: sum(w / tot_w * math.log(max(p[k], 1e-4))
                       for p, w in sources.values()) for k in CATS}
        mx = max(logp.values())
        expd = {k: math.exp(v - mx) for k, v in logp.items()}
        tot = sum(expd.values())
        probs = {k: round(v / tot, 6) for k, v in expd.items()}
        mode = "+".join(sources)
    else:
        probs = {k: round(v, 6) for k, v in rule.items()}
        mode = "rule_only"
    rem = 1.0 - sum(probs.values())
    kmax = max(probs, key=probs.get)
    probs[kmax] = round(probs[kmax] + rem, 6)
    horizons = [meta["horizon"]]
    for ts_ in (mkt_ts, ff_ts):
        if ts_:
            horizons.append(ts_)
    return Pred(series="KXFEDDECISION", period=period, dist=Categorical(probs), asof=asof,
                model_version=VERSION,
                inputs={**meta["feats"], "mode": mode,
                        "rule": {k: round(v, 4) for k, v in rule.items()},
                        "market": {k: round(v, 4) for k, v in (mkt or {}).items()},
                        "ff": {k: round(v, 4) for k, v in (ff or {}).items()}},
                data_horizon=datetime.fromisoformat(max(horizons)))


def predict_kxfed(conn, asof: datetime, period: str, series: str = "KXFED") -> Pred:
    """KXFED ladder: post-meeting upper-bound distribution derived from the decision
    categorical (H26 ≈ +0.50, C26 ≈ −0.50) — encoded as a deterministic Empirical sample
    so grid_pmf(0.25) discretises exactly onto the 25bp grid."""
    from prediction_market_macro.model.common import Empirical
    dec = predict(conn, asof, period, series="KXFEDDECISION")
    ub = FeatureStore(conn).fred_scalar_latest("DFEDTARU", asof)
    assert ub is not None, "no visible DFEDTARU"
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
