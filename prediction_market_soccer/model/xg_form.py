"""xG-based form rating adjustment (PIT) — alpha from the API-Football match-stats pull.

A team's recent **expected goals** (xG) is a lower-noise predictor of future results than
its actual scoreline: xG measures chance quality created/conceded, stripping the finishing
and goalkeeping variance that makes a 1-0 and a 3-0 look different when the underlying
performances were similar. Validated walk-forward (ops/_xg_alpha_research): blending net-xG
form into the FIFA-rating base lowers OOS Brier monotonically (0.604→0.594) and lifts the
decision model's OOS ROI (+13→+16%), whereas the goals-based equivalent does NOT help.

`xg_form_index(conn, as_of)` is strictly POINT-IN-TIME: only fixtures whose kickoff is
before `as_of` contribute, so scoring/predicting a match never sees its own or later xG.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

# Half-life-style decay so a team's most recent xG performances weigh more, matched to the
# goals-form module's conventions (form_strength.py) for consistency.
DECAY_XI = 0.03   # club calendar (was 0.012 int'l)          # per-day exponential decay (~58-day half-life)
NETXG_CLIP = 2.5          # clip a single game's net xG so one blowout can't dominate


@dataclass(frozen=True)
class XgFormSummary:
    team_id: str
    n: int                 # prior matches with xG
    weighted_net_xg: float # time-weighted mean (xG_for − xG_against) per game
    form_z: float          # z-scored across all teams with data (the index)


def _mean_std(xs):
    if not xs:
        return 0.0, 1.0
    mu = sum(xs) / len(xs)
    sd = (sum((x - mu) ** 2 for x in xs) / len(xs)) ** 0.5
    return mu, (sd or 1.0)


def xg_form_index(conn, ref: datetime | None = None, *, as_of: str | None = None) -> dict[str, XgFormSummary]:
    """{canonical_team_id: XgFormSummary} from each team's prior-match team xG (fixture_stats).

    `as_of` (ISO ts) → POINT-IN-TIME: only fixtures kicking off strictly before it count.
    Net xG per game = this team's xG − the opponent's xG in the same fixture.
    """
    ref = ref or (datetime.fromisoformat(as_of) if as_of else datetime.now(timezone.utc))
    cond = "AND f.kickoff_ts < ?" if as_of else ""
    params = (as_of,) if as_of else ()
    # Each finished fixture contributes one (team xG, opponent xG) pair per side.
    rows = conn.execute(
        f"""
        SELECT tm.canonical_team_id AS cid, f.kickoff_ts AS ko,
               s.xg AS xg_for, o.xg AS xg_against
        FROM fixture f
        JOIN fixture_stats s ON s.fixture_api_id = f.api_id
        JOIN fixture_stats o ON o.fixture_api_id = f.api_id AND o.team_api_id != s.team_api_id
        JOIN team_meta tm ON tm.api_id = s.team_api_id
        WHERE tm.canonical_team_id IS NOT NULL AND s.xg IS NOT NULL AND o.xg IS NOT NULL {cond}
        """, params).fetchall()
    agg: dict[str, dict] = {}
    for r in rows:
        d = agg.setdefault(r["cid"], {"num": 0.0, "den": 0.0, "n": 0})
        try:
            days = max(0.0, (ref - datetime.fromisoformat(r["ko"])).total_seconds() / 86400.0)
        except Exception:
            days = 0.0
        net = max(-NETXG_CLIP, min(NETXG_CLIP, (r["xg_for"] or 0.0) - (r["xg_against"] or 0.0)))
        w = math.exp(-DECAY_XI * days)
        d["num"] += w * net
        d["den"] += w
        d["n"] += 1
    wnx = {cid: (d["num"] / d["den"] if d["den"] > 0 else 0.0) for cid, d in agg.items()}
    mu, sd = _mean_std(list(wnx.values()))
    return {cid: XgFormSummary(team_id=cid, n=d["n"], weighted_net_xg=round(wnx[cid], 3),
                               form_z=round((wnx[cid] - mu) / sd, 4))
            for cid, d in agg.items()}


def xg_form_adjusted_ratings(sm, idx: dict[str, XgFormSummary], weight: float):
    """Blend the xG-form z-index into model ratings (bounded). weight=0 ⇒ unchanged."""
    from prediction_market_soccer.model.strength import StrengthModel
    if weight <= 0 or not idx:
        return sm
    b = sm.cfg.rating_bound
    new = dict(sm.ratings)
    for tid, s in idx.items():
        if tid in new:
            new[tid] = max(-b, min(b, new[tid] + weight * s.form_z))
    from dataclasses import replace as _replace
    return _replace(sm, ratings=new)   # keeps per-league comp/base_mu/home_adv (C2)


if __name__ == "__main__":
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    conn = store.init_db()
    name = {t.team_id: t.name for t in load_prior().teams}
    idx = xg_form_index(conn)
    print("xG-form ranking (net xG/game, all teams with data):")
    for s in sorted(idx.values(), key=lambda x: -x.form_z)[:16]:
        print(f"  {name.get(s.team_id, s.team_id):<14} z={s.form_z:+.2f}  netxG/g={s.weighted_net_xg:+.2f}  ({s.n} games)")
