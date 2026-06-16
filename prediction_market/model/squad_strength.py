"""Squad-strength feature (plan 03 §1c, 17 B.3) — team quality from player data.

Each national team's squad (25-26 players) is mapped to those players' CLUB-season
stats (rating / goals / assists / minutes). We summarise a squad with two signals:

  * **minutes-weighted club rating** — the quality of the players who actually
    play (a bench full of stars who never feature shouldn't inflate the team);
  * **attacking output** — squad goals + assists per 90, a cross-club proxy for
    cutting edge.

The primary index is the z-scored minutes-weighted rating across the 48 teams.
`squad_adjusted_ratings` blends it into the model ratings (a third anchor beside
FIFA rank and the prior expected points) behind a tunable weight, so the PIT
backtest can decide whether it helps before it touches the live model.

All inputs are pre-tournament (club 2025 season) → point-in-time safe.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SquadSummary:
    team_id: str
    n_players: int
    mw_rating: float          # minutes-weighted club rating
    ga_per90: float           # squad goals+assists per 90
    score_z: float            # z-scored mw_rating across all teams (the index)


def _mean_std(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return 0.0, 1.0
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / len(xs)
    return m, (math.sqrt(var) or 1.0)


def squad_index(conn) -> dict[str, SquadSummary]:
    """{canonical_team_id: SquadSummary} from squad ↔ player_stat (club season)."""
    rows = conn.execute(
        "SELECT tm.canonical_team_id cid, ps.rating, ps.goals, ps.assists, ps.minutes "
        "FROM squad s JOIN team_meta tm ON tm.api_id = s.team_api_id "
        "JOIN player_stat ps ON ps.player_api_id = s.player_api_id "
        "WHERE tm.canonical_team_id IS NOT NULL").fetchall()
    agg: dict[str, dict] = {}
    for r in rows:
        d = agg.setdefault(r["cid"], {"rw": 0.0, "mins": 0.0, "ga": 0.0, "n": 0})
        mins = float(r["minutes"] or 0)
        if r["rating"] is not None and mins > 0:
            d["rw"] += float(r["rating"]) * mins
            d["mins"] += mins
        d["ga"] += float((r["goals"] or 0) + (r["assists"] or 0))
        d["n"] += 1

    mw = {cid: (d["rw"] / d["mins"] if d["mins"] > 0 else None) for cid, d in agg.items()}
    mu, sd = _mean_std(list(mw.values()))
    out: dict[str, SquadSummary] = {}
    for cid, d in agg.items():
        rating = mw[cid] if mw[cid] is not None else mu
        ga90 = d["ga"] / (d["mins"] / 90.0) if d["mins"] > 0 else 0.0
        out[cid] = SquadSummary(team_id=cid, n_players=d["n"],
                                mw_rating=round(rating, 4),
                                ga_per90=round(ga90, 4),
                                score_z=round((rating - mu) / sd, 4))
    return out


def squad_adjusted_ratings(sm, idx: dict[str, SquadSummary], weight: float):
    """Blend the squad z-index into the model ratings (third anchor). weight=0 ⇒
    unchanged. Returns a new StrengthModel; clipped to the rating bound."""
    from prediction_market.model.strength import StrengthModel
    if weight <= 0 or not idx:
        return sm
    b = sm.cfg.rating_bound
    new = dict(sm.ratings)
    for tid, s in idx.items():
        if tid in new:
            new[tid] = max(-b, min(b, new[tid] + weight * s.score_z))
    return StrengthModel(ratings=new, sigma=dict(sm.sigma), host_ids=sm.host_ids, cfg=sm.cfg)


def build_strength_live(conn, prior=None, cfg=None):
    """The LIVE strength model: base ratings (prior + structural params) with the
    squad-strength blend applied at ``cfg.squad_blend_weight`` (0 ⇒ base only).
    Single entry point so every user-facing export uses the same model."""
    from prediction_market.config import CONFIG
    from prediction_market.model.strength import build_strength
    cfg = cfg or CONFIG.model
    sm = build_strength(prior, cfg)
    w = getattr(cfg, "squad_blend_weight", 0.0)
    if w and conn is not None:
        try:
            sm = squad_adjusted_ratings(sm, squad_index(conn), w)
        except Exception:
            pass
    return sm


if __name__ == "__main__":
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    conn = store.init_db()
    name = {t.team_id: t.name for t in load_prior().teams}
    idx = squad_index(conn)
    ranked = sorted(idx.values(), key=lambda s: -s.score_z)
    print(f"squad strength (top 12 of {len(idx)} teams):")
    for s in ranked[:12]:
        print(f"  {name.get(s.team_id, s.team_id):<14} z={s.score_z:+.2f}  mw_rating={s.mw_rating:.2f}  ga/90={s.ga_per90:.2f}  ({s.n_players}p)")
