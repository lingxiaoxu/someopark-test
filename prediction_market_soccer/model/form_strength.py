"""Recent-form feature (plan 17 B.3) — club momentum from recent results.

The club prior says how good a club was LAST season (standings, ClubElo, champion prices);
FORM says how it is playing THIS one. From each club's recent competitive matches we build
a time-weighted goal-difference score, then z-score it across the field.
`form_adjusted_ratings` blends it into the model ratings behind a weight, PIT-safe via
`as_of` (only matches kicked off before the cutoff are used).

STORAGE NOTE. The rows still live in the `nt_recent` table. The name is a national-team
leftover from the World Cup schema, but the contents are club results (10,442 rows across
the 12 competitions, `league_id` populated per row) — renaming the table belongs to the
store layer, so this module reads what is there and treats `league_id` as the competition
axis.

WHAT CHANGED FROM THE NATIONAL-TEAM VERSION, AND WHY
-----------------------------------------------------
1. FRIENDLIES ARE DROPPED, NOT DISCOUNTED. The WC version half-weighted friendlies because
   a national team plays little else between tournaments. A club plays 40-60 competitive
   matches a season, so a pre-season friendly adds nothing worth the contamination — and,
   unlike a discount factor, dropping needs no invented constant. `is_friendly` is 0 for
   every club row today; the filter is there so that if a club friendly is ever ingested it
   cannot quietly enter the index. `n_friendly` now counts what was EXCLUDED.

2. COMPETITION WEIGHTING IS BUILT BUT LEFT EQUAL. The club analogue of the friendly
   discount is "should a Champions League result count the same as a league result?". We
   tested it: regress match goal difference on (recent domestic-league form, recent
   continental-cup form), both PIT, standardised. Across nine specifications (decay
   0.02/0.03/0.05 × minimum 3/4/6 prior games) the cup coefficient is consistently the
   smaller one — implied cup:league ratio 0.51 to 0.77 — but the difference is never
   significant (|z| ≤ 1.72, mostly < 1.3, n = 591-733). Consistent direction, no
   significance: so `COMPETITION_WEIGHT` ships at 1.0 for everything and the machinery
   waits for the sample. Shipping 0.65 because the point estimate says so would be fitting
   noise into a live rating.

3. THE DECAY IS SLOWER, NOT FASTER. TRANSFORM_PLAN §2.2 expected ~0.03 (a ~23-day
   half-life) on the reasoning that clubs play every 3-4 days. The fit says the opposite.
   Grid over xi with a forward time split (fit on the first 70% of kickoffs, score the last
   30%), out-of-sample R² on match goal difference:

       xi      half-life   R²(out)
       0.0025    277 d      0.0676
       0.0050    139 d      0.0584
       0.0100     69 d      0.0396
       0.0200     35 d      0.0212
       0.0300     23 d      0.0159   <- the value the plan assumed
       0.0500     14 d      0.0137

   Slower is monotonically better, out of sample as well as in. Three weeks of results is
   mostly noise; a club's season-to-date record is the signal. We stop at xi = 0.005 (mean
   observation age 1/xi = 200 days ≈ one club season) rather than at the grid edge, because
   beyond a season the window reaches across a transfer window into a squad that no longer
   exists — a limit that comes from the calendar, not from R².

4. DO NOT ADD A SECOND, FASTER "MOMENTUM" TERM ON TOP. Regressing goal difference on both
   a season-long term and a short-window term, the SHORT term is significantly NEGATIVE at
   every horizon tested (t = -3.0 to -3.8, n = 3,237), and opponent-adjusting each past
   result strengthens it (t = -3.4 to -3.9). Conditional on a club's season record, a hot
   recent run predicts UNDER-performance, not over-performance. That is why this module
   deliberately exposes one slow index instead of a fast one — and it is a live warning for
   `form_blend_weight`, which is 0.10 and positive: see the note on `form_adjusted_ratings`.

Opponent strength is not factored into the index itself — a documented v2 refinement that
`model/altdata_adjust.py` addresses on its own axis.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

# exp(-xi * days_ago); 139-day half-life, ~200-day mean observation age ≈ one club season.
# Chosen by forward-time-split R² (see the module docstring) — NOT the 0.03 the plan assumed.
DECAY_XI = 0.005
GD_CLIP = 3.0             # cap a single result's goal difference
# Small-sample shrinkage: a club with ONE played match otherwise carries the same z-weight
# as one with fifty, so a single 3-0 in a qualifier put OFI top of the form table above Roma
# and Inter — and that z leaks into live ratings via form_blend_weight. Shrink toward the
# pool mean by n/(n+PRIOR_N): one match keeps ~1/4 of its raw signal, eight keep ~2/3.
# ops/form_export.py needs the same constant for its per-league z; import it from here.
PRIOR_N = 3.0

# Per-competition weight on a past result. All 1.0 by fit (see docstring point 2): the
# cup-vs-league difference is directionally real but never significant, so equal weight is
# what the data supports. Any competition absent from this map also gets 1.0.
COMPETITION_WEIGHT: dict[str, float] = {
    "epl": 1.0, "laliga": 1.0, "seriea": 1.0, "bundesliga": 1.0, "ligue1": 1.0,
    "brasileirao": 1.0, "argentina": 1.0,
    "ucl": 1.0, "uel": 1.0, "uecl": 1.0, "libertadores": 1.0, "sudamericana": 1.0,
}


@dataclass(frozen=True)
class FormSummary:
    team_id: str
    n: int
    n_friendly: int           # friendlies EXCLUDED from the index (0 for every club today)
    weighted_gd: float        # time+competition weighted mean goal difference
    form_z: float             # z-scored across all teams (the index)
    recent: list              # [{opp, gf, ga, friendly, comp, date}] most-recent first


def _mean_std(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return 0.0, 1.0
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / len(xs)
    return m, (math.sqrt(var) or 1.0)


def _comp_key_map() -> dict[int, str]:
    """API-Football league_id -> registry competition key, built once per call."""
    from prediction_market_soccer.config import leagues as _lg
    return {c.api_football_id: c.key for c in _lg.REGISTRY.values()}


def form_index(conn, ref: datetime | None = None, *, as_of: str | None = None) -> dict[str, FormSummary]:
    """{canonical_team_id: FormSummary} from the club recent-results table.

    `as_of` (ISO ts) makes it POINT-IN-TIME: only matches strictly before it are used
    (so scoring a past match never sees that team's later results — the leak the param
    walk-forward must avoid). `ref` is the decay reference (defaults to now / as_of)."""
    ref = ref or (datetime.fromisoformat(as_of) if as_of else datetime.now(timezone.utc))
    where = "WHERE tm.canonical_team_id IS NOT NULL" + (" AND n.kickoff_ts < ?" if as_of else "")
    params = (as_of,) if as_of else ()
    rows = conn.execute(
        "SELECT tm.canonical_team_id cid, n.opp_api_id, n.kickoff_ts, n.is_friendly, "
        "       n.league_id, n.gf, n.ga FROM nt_recent n "
        "JOIN team_meta tm ON tm.api_id = n.team_api_id " + where, params).fetchall()
    comp_of = _comp_key_map()
    agg: dict[str, dict] = {}
    for r in rows:
        d = agg.setdefault(r["cid"], {"num": 0.0, "den": 0.0, "n": 0, "nf": 0, "recent": []})
        if r["is_friendly"]:
            d["nf"] += 1            # excluded, not discounted — see the module docstring
            continue
        try:
            days = max(0.0, (ref - datetime.fromisoformat(r["kickoff_ts"])).total_seconds() / 86400.0)
        except Exception:
            days = 0.0
        comp = comp_of.get(r["league_id"])
        cw = COMPETITION_WEIGHT.get(comp, 1.0)
        gd = max(-GD_CLIP, min(GD_CLIP, (r["gf"] or 0) - (r["ga"] or 0)))
        w = math.exp(-DECAY_XI * days) * cw
        d["num"] += w * gd
        d["den"] += w
        d["n"] += 1
        d["recent"].append({"opp": r["opp_api_id"], "gf": r["gf"], "ga": r["ga"],
                            "friendly": False, "comp": comp,
                            "date": r["kickoff_ts"], "_days": days})

    wgd = {cid: (d["num"] / d["den"] if d["den"] > 0 else 0.0) for cid, d in agg.items()}
    mu, sd = _mean_std(list(wgd.values()))
    out: dict[str, FormSummary] = {}
    for cid, d in agg.items():
        rec = sorted(d["recent"], key=lambda x: x["_days"])[:5]
        for x in rec:
            x.pop("_days", None)
        shrink = d["n"] / (d["n"] + PRIOR_N) if d["n"] else 0.0
        z_raw = (wgd[cid] - mu) / sd
        out[cid] = FormSummary(team_id=cid, n=d["n"], n_friendly=d["nf"],
                               weighted_gd=round(wgd[cid], 3),
                               form_z=round(z_raw * shrink, 4), recent=rec)
    return out


def form_adjusted_ratings(sm, idx: dict[str, FormSummary], weight: float):
    """Blend the form z-index into model ratings. weight=0 ⇒ unchanged.

    The sign lives entirely in `weight`, and that is deliberate: docstring point 4 shows
    that on club data a SHORT-window form term is negatively predictive once long-run
    strength is controlled. This index is deliberately a slow, season-length one — which
    the fit does like — but anyone raising `form_blend_weight` (currently 0.10, positive)
    should re-run that decomposition against whatever the base ratings already encode
    before trusting the sign, rather than inheriting it from the World Cup module."""
    from prediction_market_soccer.model.strength import StrengthModel
    if weight <= 0 or not idx:
        return sm
    b = sm.cfg.rating_bound
    new = dict(sm.ratings)
    for tid, s in idx.items():
        if tid in new:
            new[tid] = max(-b, min(b, new[tid] + weight * s.form_z))
    return __import__('dataclasses').replace(sm, ratings=new)  # keeps per-league fields (C2)


if __name__ == "__main__":
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    conn = store.init_db()
    name = {t.team_id: t.name for t in load_prior().teams}
    idx = form_index(conn)
    ranked = sorted(idx.values(), key=lambda s: -s.form_z)
    print(f"recent form (top 10 / bottom 5 of {len(idx)}), decay xi={DECAY_XI} "
          f"({math.log(2) / DECAY_XI:.0f}-day half-life):")
    for s in ranked[:10] + ranked[-5:]:
        print(f"  {name.get(s.team_id, s.team_id):<22} form_z={s.form_z:+.2f}  "
              f"wGD={s.weighted_gd:+.2f}  ({s.n} games, {s.n_friendly} friendly excluded)")
