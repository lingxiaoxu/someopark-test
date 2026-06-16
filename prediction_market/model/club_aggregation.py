"""Squad club-form aggregation (plan 03 §1c).

National-team samples are sparse, so the plan aggregates each squad's players'
CLUB-level output into a "squad quality" score that supplements the rating base.
This module computes, per team, a minutes-weighted attacking-output rate from
the ``player_stat`` table, z-scores it across teams, and blends it into the
calibrated strength ratings.

It is parametrized by ``season``: pass the club season (e.g. 2025) once club
stats are ingested (`soccer_ingest.sync_player_club_stats`, a deliberate opt-in
pull — one request per player), or the tournament season (2026) to use the
topscorers/player stats already in the store. The method is identical; only the
input season changes.
"""
from __future__ import annotations

import statistics

from prediction_market.config import CONFIG
from prediction_market.model.strength import StrengthModel


def squad_attack_quality(conn=None, *, season: int = 2026) -> dict[str, float]:
    """Per-team z-scored attacking quality from players' goal involvement / 90.

    rate_team = Σ(goals + 0.5·assists) / Σ(minutes/90) over the team's players
    in ``season``; then z-scored across the teams that have data. Teams with no
    player data are omitted (caller treats them as neutral, z=0).
    """
    from prediction_market.ingest import store

    conn = conn or store.init_db()
    # Map each player to their NATIONAL team via the squad table. Critical for
    # club-season data, where player_stat.team_api_id is the CLUB (e.g. Real
    # Madrid), not the national team. Falls back to a direct team_meta join for
    # rows whose team_api_id is itself a national team (e.g. WC topscorers).
    rows = conn.execute(
        "SELECT m.canonical_team_id AS tid, "
        "       SUM(COALESCE(ps.goals,0) + 0.5*COALESCE(ps.assists,0)) AS contrib, "
        "       SUM(COALESCE(ps.minutes,0)) AS minutes "
        "FROM player_stat ps "
        "JOIN team_meta m ON m.api_id = COALESCE( "
        "     (SELECT sq.team_api_id FROM squad sq WHERE sq.player_api_id = ps.player_api_id LIMIT 1), "
        "     ps.team_api_id) "
        "WHERE ps.season = ? AND m.canonical_team_id IS NOT NULL "
        "GROUP BY m.canonical_team_id HAVING minutes > 0",
        (season,),
    ).fetchall()
    rates = {r["tid"]: (r["contrib"] / (r["minutes"] / 90.0)) for r in rows}
    if len(rates) < 2:
        return {tid: 0.0 for tid in rates}
    vals = list(rates.values())
    mu = statistics.mean(vals)
    sd = statistics.pstdev(vals) or 1.0
    return {tid: (v - mu) / sd for tid, v in rates.items()}


def blend_into_strength(sm: StrengthModel, quality: dict[str, float], *, weight: float = 0.10) -> StrengthModel:
    """Nudge ratings by the (z-scored) squad quality: R_i += weight·z_i (clipped).

    ``weight`` is small — club form is one signal among several (plan 03 §1),
    and the in-tournament sample is noisy. Teams absent from ``quality`` are
    left unchanged (treated as neutral).
    """
    b = sm.cfg.rating_bound
    new_ratings = dict(sm.ratings)
    for tid, z in quality.items():
        if tid in new_ratings:
            new_ratings[tid] = max(-b, min(b, new_ratings[tid] + weight * z))
    return StrengthModel(ratings=new_ratings, sigma=dict(sm.sigma),
                         host_ids=sm.host_ids, cfg=sm.cfg)


if __name__ == "__main__":
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.strength import build_strength

    prior = load_prior()
    name = {t.team_id: t.name for t in prior.teams}
    q = squad_attack_quality(season=2026)  # uses ingested topscorers
    print(f"squad attacking-quality z-scores from {len(q)} teams with player data:")
    for tid, z in sorted(q.items(), key=lambda kv: -kv[1])[:8]:
        print(f"  {name.get(tid, tid):<16} z={z:+.2f}")
    sm = build_strength(prior)
    blended = blend_into_strength(sm, q, weight=0.10)
    moved = sorted(((name.get(t, t), blended.ratings[t] - sm.ratings[t])
                    for t in q), key=lambda x: -abs(x[1]))[:5]
    print("biggest rating nudges from club/tournament form:")
    for n, d in moved:
        print(f"  {n:<16} {d:+.3f}")
