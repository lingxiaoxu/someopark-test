"""model/fed.py — FOMC decision (PLAN §7). fed/0.3.0

Two products from one engine:
  * KXFEDDECISION categorical {H26,H25,H0,C25,C26} — the move AT one meeting
  * KXFED 'Above X%' ladder over the post-meeting target UPPER bound — the LEVEL

The distinction between those two lines is the whole of the v0.3 rewrite. v0.1-0.2 blurred
them and the blur only shows up at long horizons, which is exactly where the money was
lost (see §27.1). Both surviving archived losers, KXFEDDECISION 2026-12 (-$0.75) and
2027-03 (-$0.92), were opened off a model that priced 2027-03's H0 at 0.0137 — a 1.4%
chance the Fed holds at a meeting 20 months out, against a measured unconditional hold
rate of 0.780. Three independent defects stacked to produce that:

  1. `_market_prior` devigged the KXFED ladder for period P into a distribution over the
     rate LEVEL in P, then classified it by `level - ub_today`. For the NEXT meeting those
     coincide. For any later meeting they do not: P(level in Mar-2027 == level today) is
     small simply because 20 months of drift intervene, and that small number was being
     reported as P(hold at the March meeting). The move at a meeting is level(P) minus
     level(P-1), never level(P) minus today.
  2. `_ff_probs` recorded the expected move only for `meetings[0]` and returned it for
     whatever period the caller asked about. Every horizon got the NEXT meeting's number
     at the largest weight (0.50) — which is why 2027-06 through 2027-12 printed
     byte-identical distributions.
  3. Nothing widened with horizon. A meeting 17 months out was pooled from the same
     sources at the same confidence as one 6 weeks out.

v0.3 sources, log-pooled over whichever are available (WEIGHTS, renormalised):
  * rule  — the verified 51-hike-history discriminant, PIT from the meeting panel.
    A policy-MOMENTUM term was built here in 2026-08 off the §28 statement backfill and
    then NOT shipped; the measurement and the reason are in PLAN §29.2, and
    FeatureStore.fomc_meeting_moves is the door it would use if revisited.
  * market — devig of KXFED at P *and* at the preceding meeting; the DIFFERENCE of the
    two expected levels is the move. Unavailable prior ladder ⇒ no market source.
  * ff    — ZQ 30-day FF futures chained month by month to the TARGET meeting. Our store
    carries ZQ through H27, so this reaches 2027-03 and no further.
  * dgs2  — beyond the ZQ chain, the 2y slope. The map from slope to realised drift is
    re-fit PIT on every call rather than hardcoded (measured beta 1.33, corr 0.62).

Then the pooled result is shrunk toward the measured unconditional base rate by
lambda(h) = h/(h + 9 months), which is what puts a horizon-dependent floor under H0. The
blend weights and the 9-month half-life are v0.3 priors, re-fit at M4 via replay.
"""
from __future__ import annotations

import json
import math
from datetime import datetime

import numpy as np

from prediction_market_macro.model.common import Categorical, Pred
from prediction_market_macro.model.features import FeatureStore
from prediction_market_macro.strategy.devig import ladder_implied

VERSION = "fed/0.3.0"    # 0.3.0: per-meeting move (not level-vs-today) + horizon shrink
CATS = ["C26", "C25", "H0", "H25", "H26"]
QUANTA = [-0.50, -0.25, 0.0, 0.25, 0.50]

# One weight per source, renormalised over whichever are present on the call. ZQ is the
# deepest read on a near meeting; the Kalshi ladder is next; the rule is a prior, not
# evidence; DGS2 is a coarse path anchor that only appears when ZQ cannot reach.
WEIGHTS = {"ff": 0.50, "market": 0.35, "dgs2": 0.30, "rule": 0.15}

# Shrink half-life: lambda = months / (months + SHRINK_HALFLIFE_M). 9 months is where the
# ZQ strip stops being liquid enough to carry information (CME publishes FedWatch out to
# roughly a year, and volume collapses past the fourth contract), so it is the horizon at
# which the pooled sources deserve equal billing with the base rate.
SHRINK_HALFLIFE_M = 9.0
MEETINGS_PER_YEAR = 8.0

# defaults == the registered fed/0.3.0 behaviour. Only the pooling weights and the shrink
# half-life are exposed, and that is deliberate: MIN_POST_FRAC / MIN_WINDOW_MASS /
# MAX_MASS_GAP are DATA-COMPARABILITY guards, not model knobs — loosening them does not
# make the model better, it makes it answer off inputs that do not mean the same thing.
# A grid must not be allowed to buy Brier by disabling a guard.
#
# Sizing note (2026-08-04): KXFED has 28 usable settled events and KXFEDDECISION has 1.
# That supports a handful of sets, not hundreds.
DEFAULT_PARAMS = {
    "w_ff": 0.50,
    "w_market": 0.35,
    "w_dgs2": 0.30,
    "w_rule": 0.15,
    "shrink_halflife_m": SHRINK_HALFLIFE_M,
}


def _p(params: dict | None) -> dict:
    return {**DEFAULT_PARAMS, **(params or {})}

_ZQ_MONTH = "FGHJKMNQUVXZ"


def _zq_root(y: int, m: int) -> str:
    return f"ZQ{_ZQ_MONTH[m - 1]}{y % 100:02d}"


def _move_to_probs(exp_move: float) -> dict:
    """Split an expected move between the two adjacent 25bp quanta.

    Deliberately a two-point distribution: it carries a first moment and nothing else, and
    the honest widening happens once, centrally, in the horizon shrink. Spreading it here
    too would double-count the uncertainty.
    """
    x = max(min(exp_move, QUANTA[-1]), QUANTA[0])
    probs = dict.fromkeys(CATS, 1e-4)
    for i in range(len(QUANTA) - 1):
        lo, hi = QUANTA[i], QUANTA[i + 1]
        if lo <= x <= hi:
            f = (x - lo) / (hi - lo)
            probs[CATS[i]] = max(1.0 - f, 1e-4)
            probs[CATS[i + 1]] = max(f, 1e-4)
            break
    z = sum(probs.values())
    return {k: v / z for k, v in probs.items()}


def _base_rates(fs: FeatureStore, asof: datetime) -> tuple[dict, str | None]:
    """Unconditional per-meeting outcome frequencies, counted PIT from DFEDTARU.

    The target-rate series only records CHANGES, so holds are the unobserved bulk: the
    meeting count is reconstructed from the span at 8 meetings a year. Over the visible
    history (2008-12 onward) that is 31 changes across ~141 meetings, i.e. H0 = 0.780 —
    the number the shrink pulls toward, and the reason an H0 of 0.0137 twenty months out
    was never defensible.

    v0.4 note — the statement backfill makes the real meeting calendar available, so this
    denominator no longer HAS to be assumed (fs.fomc_meeting_moves counts 242 meetings
    1994->2026 directly, H0 0.639). Counting it was tried and NOT adopted: scored PIT as a
    constant predictor over 202 meetings the assumed 0.780 wins on Brier, 0.4850 against
    0.5141, and the counted panel only edges ahead on the 2015+ (0.5185 vs 0.5223) and
    2020+ (0.5557 vs 0.5629) subsamples — margins well inside noise at n=96 and n=55.
    The reason is regime, not arithmetic: 1994-2007 moved at far more meetings than the
    ZIRP decade, so a full-history count is not the base rate of the world we trade in
    while 0.780 happens to sit near the middle of both regimes. See PLAN §29.2.
    """
    tgt, h = fs.fred_series("DFEDTARU", asof)
    tgt = tgt.dropna()
    if len(tgt) < 260:
        return {"C26": 0.02, "C25": 0.06, "H0": 0.78, "H25": 0.10, "H26": 0.04}, h
    d = tgt.diff().fillna(0.0)
    chg = d[d != 0.0]
    years = max((tgt.index[-1] - tgt.index[0]).days / 365.25, 1.0)
    n_meet = max(round(years * MEETINGS_PER_YEAR), len(chg) + 1)
    cnt = dict.fromkeys(CATS, 0.0)
    for v in chg.values:
        k = ("C26" if v <= -0.45 else "C25" if v < -0.05
             else "H25" if v < 0.45 else "H26")
        cnt[k] += 1.0
    cnt["H0"] = float(n_meet - len(chg))
    tot = sum(cnt.values())
    return {k: v / tot for k, v in cnt.items()}, h


def _shrink_lambda(asof: datetime, meeting: datetime,
                   halflife_m: float = SHRINK_HALFLIFE_M) -> float:
    months = max((meeting - asof).days, 0) / 30.44
    return months / (months + halflife_m)


MIN_POST_FRAC = 0.25      # below this the day-weighted solve levers errors past 4x


def _ff_path(fs: FeatureStore, asof: datetime,
             meeting: datetime) -> tuple[float | None, float | None, str | None]:
    """(pre-meeting rate, move at THAT meeting, horizon) from the ZQ strip.

    CME-FedWatch chain: a month's implied average rate is the day-weighted mix of the pre-
    and post-meeting rates, so each meeting's post-rate solves out and feeds the next
    month. Two corrections to v0.2, both of which the live strip exposes:

    * v0.2 recorded the move only for `meetings[0]` and handed that back for whatever
      period was asked, so every horizon shared the next meeting's number. This walks to
      the requested meeting, and returns None when the strip cannot reach it — the store
      carries ZQ through H27, so nothing past 2027-03 is priceable here.
    * The day-weighted solve divides by the post-meeting fraction of the month, so a
      meeting in the last week levers every upstream error 8-10x. Oct-2026 (28th, frac
      0.097) and Jan-2027 (27th, frac 0.129) both hit that, and the compounding is what
      drove the chain to price a +50bp hike at Mar-2027. Two unlevered reads replace it:
      a month with no meeting quotes the prevailing rate DIRECTLY, and a meeting's
      post-rate is read off the following month's contract whenever that month is
      meeting-free. Only when neither is available does the levered solve run, and then
      only above MIN_POST_FRAC. On the 2026-08-04 strip this yields per-meeting moves of
      +16.9/+7.0/+11.9/+5.5/+7.8bp for Sep..Mar, summing to the +49bp the strip actually
      prices, instead of dumping it all on one meeting.
    """
    import calendar as _cal

    from prediction_market_macro.ingest.calendars import CALENDARS
    r0 = fs.fred_scalar_latest("DFEDTARU", asof)
    if r0 is None:
        return None, None, None
    rate = float(r0) - 0.125                   # upper bound → corridor midpoint
    meetings = [e.scheduled_ts for e in CALENDARS["FOMC"] if e.scheduled_ts > asof]
    if meeting not in meetings:
        return None, None, None

    def _implied(y: int, m: int) -> tuple[float | None, str | None]:
        closes, h = fs.fut_closes(_zq_root(y, m), asof, n=10)
        if len(closes) < 1:
            return None, None
        return 100.0 - float(closes.iloc[-1]), h

    horizon = None
    cur = (asof.year, asof.month)
    for _ in range(30):                         # walk months forward to the target
        y, m = cur
        nxt = (y + (m == 12), m % 12 + 1)
        mts = [mt for mt in meetings if (mt.year, mt.month) == (y, m)]
        implied, h = _implied(y, m)
        if h:
            horizon = max(horizon or h, h)
        if not mts:                             # no meeting: the contract IS the rate
            if implied is not None:
                rate = implied
            cur = nxt
            continue
        if implied is None:
            return None, None, None             # meeting month without ZQ data
        pre = rate
        # preferred, unlevered: the next month reads the post-meeting rate outright
        nxt_implied, nxt_h = (_implied(*nxt)
                              if not any((mt.year, mt.month) == nxt for mt in meetings)
                              else (None, None))
        if nxt_implied is not None:
            r_post = nxt_implied
            if nxt_h:
                horizon = max(horizon or nxt_h, nxt_h)
        else:
            w_pre = mts[0].day / _cal.monthrange(y, m)[1]
            if 1.0 - w_pre < MIN_POST_FRAC:
                # would lever the chain past 4x. The rate going INTO this meeting is
                # still known, and predict_kxfed needs exactly that to anchor the level
                # ladder, so hand it back with no move rather than nothing at all.
                return (pre, None, horizon) if mts[0] == meeting else (None, None, None)
            r_post = (implied - w_pre * pre) / (1.0 - w_pre)
        if mts[0] == meeting:                   # the meeting actually asked about
            return pre, r_post - pre, horizon
        rate = r_post                           # chain forward
        cur = nxt
        if (y, m) > (meeting.year, meeting.month):
            break
    return None, None, None


def _dgs2_path(fs: FeatureStore, asof: datetime,
               meeting: datetime) -> tuple[float | None, float | None, str | None]:
    """(pre-meeting rate, per-meeting move, horizon) from the 2y Treasury slope.

    The fallback for meetings past the end of the ZQ strip, which is every FOMC date after
    2027-03 in this store — and those are precisely the contracts that used to be priced
    off a stale copy of the next meeting's number.

    DGS2 minus the funds midpoint is the market's 2y average-path signal, but it also
    carries a term premium, so it is not used raw: the map from today's slope to the
    realised 1y drift in the funds midpoint is re-fit on every call over the history
    visible at asof (measured beta 1.33, intercept -0.118, corr 0.62 over ~4100 overlapping
    days). Re-fitting rather than hardcoding keeps the replay canary honest. Drift is
    extrapolated at most 2 years — the horizon the 2y point actually spans — and held flat
    beyond.
    """
    r_ub = fs.fred_scalar_latest("DFEDTARU", asof)
    if r_ub is None:
        return None, None, None
    r_now = float(r_ub) - 0.125
    dgs2, h1 = fs.fred_series("DGS2", asof)
    tgt, h2 = fs.fred_series("DFEDTARU", asof)
    dgs2, tgt = dgs2.dropna(), tgt.dropna()
    idx = dgs2.index.intersection(tgt.index)
    if len(idx) < 756:                                   # ~3y of overlap to fit anything
        return None, None, None
    mid = tgt[idx] - 0.125
    slope = (dgs2[idx] - mid).astype(float)
    drift = (mid.shift(-252) - mid).astype(float)        # realised 1y move in the funds mid
    ok = (~slope.isna()) & (~drift.isna())
    if int(ok.sum()) < 500:
        return None, None, None
    xs = slope[ok].values
    if float(np.std(xs)) < 0.05:
        return None, None, None      # no slope variation to regress on — a fit here is
                                     # a singular system, not an anchor
    b, a = np.polyfit(xs, drift[ok].values, 1)
    slope_now = float(dgs2.iloc[-1]) - r_now
    ann = float(a + b * slope_now)
    yrs = max((meeting - asof).days, 0) / 365.25
    per_meeting = ann / MEETINGS_PER_YEAR
    pre = r_now + ann * min(yrs, 2.0) - per_meeting      # rate going INTO that meeting
    horizon = max(x for x in (h1, h2) if x)
    return pre, per_meeting, horizon


def _rule_probs(fs: FeatureStore, conn, asof: datetime) -> tuple[dict, dict]:
    """Prior over the decision, bucketed by labor direction x core band.

    Read the returned `base` before trusting the name: the four dicts below are HARDCODED
    priors, and the historical panel assembled above them feeds only `hike_evidence` /
    `n_panel_moves`, which go into meta and never into the probabilities. The panel is
    diagnostic, not evidence — worth knowing when a change to the panel's inputs appears
    to do nothing (it does nothing by construction). Whether the panel should actually
    take over `base` is a separate question with its own A/B; see PLAN §29.2.

    The panel is also DFEDTARU-only, i.e. 2008-12 onward, despite an earlier docstring
    here claiming 1990->. FeatureStore.fed_target_upper splices 1982-2008 back on for
    callers that want it; §29.2 measures why _base_rates does not take it.
    """
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


MIN_WINDOW_MASS = 0.70    # a conditional mean off less than this isn't comparable
MAX_MASS_GAP = 0.05       # ...nor is one off a window that means different things


def _level_pmf(fs: FeatureStore, asof: datetime,
               period: str) -> tuple[dict | None, str | None]:
    """Devigged pmf over the post-meeting upper bound for one FOMC period.

    Keys are the settlement levels the ladder resolves onto; the open top bucket is
    assigned one 25bp step above the highest strike. Note that BOTH end buckets are open —
    the lowest key is P(level <= lowest strike) and the highest is P(level > highest
    strike) — so neither carries a trustworthy level, only a mass. `_market_move` excludes
    them.
    """
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
    xs, surv = impl["strikes"], impl["survival"]
    if not xs:
        return None, None
    pmf, prev_s = {}, 1.0
    for x, sv in zip(xs, surv):
        m = prev_s - sv                     # P(level in (previous strike, x])
        if m > 0:
            pmf[round(x, 2)] = pmf.get(round(x, 2), 0.0) + m
        prev_s = sv
    if prev_s > 0:
        top = round(xs[-1] + 0.25, 2)
        pmf[top] = pmf.get(top, 0.0) + prev_s
    tot = sum(pmf.values())
    if tot < 0.5:
        return None, None
    return {k: v / tot for k, v in pmf.items()}, max(r["ts"] for r in rows)


def _conditional_mean(pmf: dict, lo: float, hi: float) -> tuple[float | None, float]:
    """(E[level | lo < level <= hi], retained mass). Half-open low side on purpose: `lo`
    is a ladder's lowest strike, whose bucket is unbounded below."""
    kept = {k: v for k, v in pmf.items() if lo < k <= hi}
    mass = sum(kept.values())
    if mass <= 0:
        return None, 0.0
    return sum(k * v for k, v in kept.items()) / mass, mass


def _market_move(fs: FeatureStore, asof: datetime, period: str,
                 meeting: datetime) -> tuple[float | None, str | None]:
    """Expected move AT `meeting`: E[level after P] − E[level going into P].

    The level going into P is the level after the previous FOMC meeting; when P is the
    next meeting there is no previous one still open, and the level going in is simply
    today's target — which is the only case v0.2's `level - ub_today` ever got right.

    The two ladders must be compared over a COMMON strike window. Kalshi does not use the
    same strike set across periods: on 2026-08-04 the 2026 KXFED ladders run 2.75..5.25
    while the 2027 ones run 0.00..4.25, and differencing their raw means manufactured a
    -41bp "cut" at the Jan-2027 meeting out of nothing but the change in strike coverage.
    Conditioning both on the overlap removes that; it also biases the surviving estimate
    toward zero when the ladders disagree in the tails, which is the safe direction here.

    Returns None rather than a guess when the preceding ladder is missing or the overlap
    is too thin to mean anything. Fabricating a move for a meeting whose predecessor is
    unpriced is how 2027-03 came to be quoted at P(hold) = 0.045 alongside a simultaneous
    24% double-cut and 36.5% double-hike.
    """
    from prediction_market_macro.ingest.calendars import CALENDARS
    pmf_p, ts_p = _level_pmf(fs, asof, period)
    if pmf_p is None:
        return None, None
    prior = [e for e in CALENDARS["FOMC"]
             if e.scheduled_ts > asof and e.scheduled_ts < meeting]
    if not prior:
        ub = fs.fred_scalar_latest("DFEDTARU", asof)
        if ub is None:
            return None, None
        ev, mass = _conditional_mean(pmf_p, -math.inf, math.inf)
        return (ev - float(ub), ts_p) if ev is not None else (None, None)
    pmf_prev, ts_prev = _level_pmf(fs, asof, prior[-1].period)
    if pmf_prev is None:
        return None, None
    # Window over the CLOSED buckets of both ladders. The lowest key of each is
    # P(level <= that strike) and the highest is P(level > the top strike): both are
    # unbounded, so their nominal levels are fictions whose size depends only on where
    # Kalshi chose to stop quoting. Including them is what let the 2027 ladders' 4.25 cap
    # pile all the upside into one point and read as a cut.
    lo = max(min(pmf_p), min(pmf_prev))
    hi = min(max(pmf_p), max(pmf_prev)) - 0.25          # drop the open top bucket
    if hi <= lo:
        return None, None
    ev_p, mass_p = _conditional_mean(pmf_p, lo, hi)
    ev_prev, mass_prev = _conditional_mean(pmf_prev, lo, hi)
    if ev_p is None or ev_prev is None or min(mass_p, mass_prev) < MIN_WINDOW_MASS:
        return None, None
    if abs(mass_p - mass_prev) > MAX_MASS_GAP:
        # Two conditional means are only differenceable when they condition on events of
        # comparable size. Live case: the 2027-04 ladder puts 20% above its top strike
        # against 2027-03's 10.5%, and excluding those unequal tails moved the difference
        # from -11bp to -25.5bp — i.e. the answer was mostly an artefact of how much of
        # each ladder had been discarded. -25.5bp maps to P(cut) = 0.98 at a meeting 21
        # months out, which no read of an illiquid ladder earns.
        return None, None
    return ev_p - ev_prev, max(ts_p, ts_prev)


def _meeting_event(period: str):
    """Resolve a period token to its FOMC calendar entry, or None.

    Two spellings reach here and only one of them is a month. KXFED writes `24MAR`, which
    `kalshi_period_to_key` turns into `2024-03` and which matches the calendar's own key.
    KXFEDDECISION sometimes writes the STATEMENT DATE instead — `24MAR20` -> `2024-03-20`,
    `24JAN31` -> `2024-01-31` — and a plain `e.period == period` misses those. The miss was
    silent and expensive: `meeting` came back None, so the market/ZQ/DGS2 legs were all
    skipped and the meeting was scored on the unconditional base rate alone.

    Match the date form against the meeting's own date rather than truncating it to a
    month, because truncating would also match a same-month meeting on a different day.
    The canonical (month) key comes back on the entry, so callers that then need to find
    the KXFED ladder — `_market_move` -> `_level_pmf` — must use `ev.period`, not `period`.
    """
    from prediction_market_macro.ingest.calendars import CALENDARS
    cal = CALENDARS["FOMC"]
    ev = next((e for e in cal if e.period == period), None)
    if ev is not None:
        return ev
    return next((e for e in cal
                 if e.scheduled_ts.date().isoformat() == period), None)


def predict(conn, asof: datetime, period: str, series: str = "KXFEDDECISION",
            params: dict | None = None) -> Pred:
    p_ = _p(params)
    weights = {"ff": float(p_["w_ff"]), "market": float(p_["w_market"]),
               "dgs2": float(p_["w_dgs2"]), "rule": float(p_["w_rule"])}
    fs = FeatureStore(conn)
    rule, meta = _rule_probs(fs, conn, asof)
    _ev = _meeting_event(period)
    meeting = _ev.scheduled_ts if _ev is not None else None

    mkt = ff = dgs2 = None
    mkt_ts = ff_ts = dgs2_ts = None
    pre_rate = None
    if meeting is not None:
        try:
            mv, mkt_ts = _market_move(fs, asof, _ev.period, meeting)
            mkt = _move_to_probs(mv) if mv is not None else None
        except Exception:                              # noqa: BLE001
            mkt = None
        try:
            pre_rate, mv, ff_ts = _ff_path(fs, asof, meeting)
            ff = _move_to_probs(mv) if mv is not None else None
        except Exception:                              # noqa: BLE001
            ff, pre_rate = None, None
        if ff is None:                                 # past the end of the ZQ strip
            try:
                pre_d, mv, dgs2_ts = _dgs2_path(fs, asof, meeting)
                dgs2 = _move_to_probs(mv) if mv is not None else None
                if pre_rate is None:                   # ZQ's pre-rate wins when it exists
                    pre_rate = pre_d
            except Exception:                          # noqa: BLE001
                dgs2 = None

    sources = {"rule": rule}
    for name, p in (("market", mkt), ("ff", ff), ("dgs2", dgs2)):
        if p is not None:
            sources[name] = p
    if len(sources) > 1:
        tot_w = sum(weights[k] for k in sources)
        logp = {k: sum(weights[s] / tot_w * math.log(max(p[k], 1e-4))
                       for s, p in sources.items()) for k in CATS}
        mx = max(logp.values())
        expd = {k: math.exp(v - mx) for k, v in logp.items()}
        tot = sum(expd.values())
        probs = {k: v / tot for k, v in expd.items()}
        mode = "+".join(sources)
    else:
        probs = dict(rule)
        mode = "rule_only"

    # horizon shrink toward the measured unconditional base. Every source above is a
    # first moment dressed as a distribution; none of them knows more about a meeting 17
    # months out than the base rate does, and this is what stops H0 from collapsing.
    base, base_h = _base_rates(fs, asof)
    lam = (_shrink_lambda(asof, meeting, float(p_["shrink_halflife_m"]))
           if meeting is not None else 0.0)
    probs = {k: round((1 - lam) * probs[k] + lam * base[k], 6) for k in CATS}
    rem = 1.0 - sum(probs.values())
    kmax = max(probs, key=probs.get)
    probs[kmax] = round(probs[kmax] + rem, 6)

    horizons = [meta["horizon"]] + [t for t in (mkt_ts, ff_ts, dgs2_ts, base_h) if t]
    return Pred(series="KXFEDDECISION", period=period, dist=Categorical(probs), asof=asof,
                model_version=VERSION,
                inputs={**meta["feats"], "mode": mode,
                        "shrink_lambda": round(lam, 4),
                        "base_rate": {k: round(v, 4) for k, v in base.items()},
                        "pre_meeting_rate": (round(pre_rate, 4)
                                             if pre_rate is not None else None),
                        "rule": {k: round(v, 4) for k, v in rule.items()},
                        "market": {k: round(v, 4) for k, v in (mkt or {}).items()},
                        "ff": {k: round(v, 4) for k, v in (ff or {}).items()},
                        "dgs2": {k: round(v, 4) for k, v in (dgs2 or {}).items()}},
                data_horizon=datetime.fromisoformat(max(horizons)))


def predict_kxfed(conn, asof: datetime, period: str, series: str = "KXFED",
                  params: dict | None = None) -> Pred:
    """KXFED ladder: post-meeting upper-bound distribution derived from the decision
    categorical (H26 ≈ +0.50, C26 ≈ −0.50) — encoded as a deterministic Empirical sample
    so grid_pmf(0.25) discretises exactly onto the 25bp grid.

    Anchored on the rate going INTO the meeting, not on today's target. For the next
    meeting they are the same number; for a meeting a year out they are not, and pinning
    the ladder to today's level is the level-vs-move confusion of §27.1 running in the
    opposite direction — it would have priced every 2027 KXFED strike as if no move had
    happened in between. Falls back to today's target only when no path source reached
    that meeting, in which case the shrink has already flattened the categorical.
    """
    from prediction_market_macro.model.common import Empirical
    dec = predict(conn, asof, period, series="KXFEDDECISION", params=params)
    ub = FeatureStore(conn).fred_scalar_latest("DFEDTARU", asof)
    assert ub is not None, "no visible DFEDTARU"
    pre = dec.inputs.get("pre_meeting_rate")
    anchor = round(pre + 0.125, 4) if pre is not None else ub   # midpoint → upper bound
    move = {"C26": -0.50, "C25": -0.25, "H0": 0.0, "H25": 0.25, "H26": 0.50}
    probs = dec.dist.probs
    vals, ps = zip(*[(round(anchor + move[k], 2), p) for k, p in probs.items()])
    import numpy as _np
    rng = _np.random.default_rng(0)
    samples = rng.choice(vals, size=20000, p=_np.array(ps) / sum(ps))
    return Pred(series="KXFED", period=period, dist=Empirical(tuple(samples.tolist())),
                asof=asof, model_version=VERSION,
                inputs={**dec.inputs, "current_ub": ub, "anchor_ub": anchor},
                data_horizon=dec.data_horizon)
