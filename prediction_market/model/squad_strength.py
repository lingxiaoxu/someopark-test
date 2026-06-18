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

from prediction_market.config import CONFIG


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


# League-strength multipliers (API-Football league_id → relative strength), so a 7.2
# match rating in the Saudi/South-African league isn't counted the same as a 7.2 in the
# Premier League. Covers the ~22 leagues that hold >90% of WC-squad minutes; everything
# else (46-league long tail + cups/friendlies/None) gets _LEAGUE_DEFAULT. Tiers track
# widely-accepted league strength (UEFA coefficients / market value), top-5 Europe = 1.0.
# This is an explicit, tunable HEURISTIC — not a validated predictive coefficient.
_LEAGUE_STRENGTH = {
    39: 1.00,   # England — Premier League
    140: 0.97,  # Spain — La Liga
    135: 0.95,  # Italy — Serie A
    78: 0.95,   # Germany — Bundesliga
    61: 0.90,   # France — Ligue 1
    94: 0.80,   # Portugal — Primeira Liga
    88: 0.78,   # Netherlands — Eredivisie
    71: 0.78,   # Brazil — Serie A
    144: 0.75,  # Belgium — Jupiler Pro League
    40: 0.72,   # England — Championship
    203: 0.70,  # Turkey — Süper Lig
    253: 0.66,  # USA — MLS
    262: 0.66,  # Mexico — Liga MX
    179: 0.62,  # Scotland — Premiership
    119: 0.60,  # Denmark — Superliga
    307: 0.60,  # Saudi Arabia — Pro League
    345: 0.58,  # Czechia — Czech Liga
    197: 0.58,  # Greece — Super League
    103: 0.55,  # Norway — Eliteserien
    188: 0.48,  # Australia — A-League
    288: 0.42,  # South Africa — PSL
    233: 0.42,  # Egypt — Premier League
}
_LEAGUE_DEFAULT = 0.50   # long-tail leagues / cups / friendlies / unknown


def _league_mult(league_id) -> float:
    return _LEAGUE_STRENGTH.get(league_id, _LEAGUE_DEFAULT)


def squad_index(conn) -> dict[str, SquadSummary]:
    """{canonical_team_id: SquadSummary} from squad ↔ player_stat (club season). The
    minutes-weighted rating is also LEAGUE-STRENGTH weighted, so minutes in weaker leagues
    count less (a 7.2 in the Saudi league ≠ a 7.2 in the Premier League)."""
    rows = conn.execute(
        "SELECT tm.canonical_team_id cid, ps.rating, ps.goals, ps.assists, ps.minutes, ps.league_id "
        "FROM squad s JOIN team_meta tm ON tm.api_id = s.team_api_id "
        "JOIN player_stat ps ON ps.player_api_id = s.player_api_id "
        "WHERE tm.canonical_team_id IS NOT NULL").fetchall()
    agg: dict[str, dict] = {}
    for r in rows:
        d = agg.setdefault(r["cid"], {"rw": 0.0, "mins": 0.0, "ga": 0.0, "n": 0})
        mins = float(r["minutes"] or 0)
        if r["rating"] is not None and mins > 0:
            lm = _league_mult(r["league_id"])
            d["rw"] += float(r["rating"]) * lm * mins   # league-strength + minutes weighted
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
    squad-strength AND recent-form blends applied (cfg.squad_blend_weight /
    cfg.form_blend_weight; 0 ⇒ off). Single entry point so every user-facing export
    uses the same model."""
    from prediction_market.model.strength import build_strength
    cfg = cfg or CONFIG.model
    sm = build_strength(prior, cfg)
    if conn is None:
        return sm
    sw = getattr(cfg, "squad_blend_weight", 0.0)
    if sw:
        try:
            sm = squad_adjusted_ratings(sm, squad_index(conn), sw)
        except Exception:
            pass
    fw = getattr(cfg, "form_blend_weight", 0.0)
    if fw:
        try:
            from prediction_market.model.form_strength import form_adjusted_ratings, form_index
            sm = form_adjusted_ratings(sm, form_index(conn), fw)
        except Exception:
            pass
    cw = getattr(cfg, "fc_blend_weight", 0.0)
    if cw:
        try:
            from prediction_market.model.fc_strength import fc_adjusted_ratings, fc_squad_index
            sm = fc_adjusted_ratings(sm, fc_squad_index(conn), cw)
        except Exception:
            pass
    # Alt-data λ adjustments (plan 19): attach LAST (ratings/blends already applied) so
    # pair_lambdas can apply the opponent-adjusted form / xGA multipliers. Only computed
    # when at least one weight is non-zero → zero cost + prod unchanged at the 0 default.
    if any(getattr(cfg, w, 0.0) for w in ("oppadj_def_weight", "oppadj_off_weight", "xga_weight")):
        try:
            from dataclasses import replace as _replace
            from prediction_market.model.altdata_adjust import altdata_index
            sm = _replace(sm, adj=altdata_index(conn, sm.ratings))
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
