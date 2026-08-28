"""Squad-strength feature (plan 03 §1c, 17 B.3) — team quality from player data.

Every player who appeared for a club carries his own season stats (rating / goals /
assists / minutes), so a club squad is summarised with two signals:

  * **minutes-weighted club rating** — the quality of the players who actually
    play (a bench full of stars who never feature shouldn't inflate the team);
  * **attacking output** — squad goals + assists per 90, a cross-club proxy for
    cutting edge.

The primary index is the z-scored minutes-weighted rating across every registered
club. `squad_adjusted_ratings` blends it into the model ratings (a third anchor
beside the club prior's expected points and the recent-form blend) behind a tunable
weight, so the PIT backtest can decide whether it helps before it touches the live
model.

Point-in-time safety comes from the ``as_of`` cut in ``build_strength_live`` — the
roster index itself is a season aggregate, so a historical caller must pass one.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from prediction_market_soccer.config import CONFIG


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
# Premier League. The tiers still bind in the club edition even though our own twelve
# competitions are all top-flight: a European tie pairs a UECL qualifier with a Ligue 1
# side, and a player's stat row can come from any league he appeared in. Everything not
# listed (long tail + cups/friendlies/None) gets _LEAGUE_DEFAULT. Tiers track
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


def squad_index(conn, *, as_of: str | None = None) -> dict[str, SquadSummary]:
    """{canonical_team_id: SquadSummary} from the per-fixture lineup feed. The
    minutes-weighted rating is also LEAGUE-STRENGTH weighted, so minutes in weaker leagues
    count less (a 7.2 in the Saudi league ≠ a 7.2 in the Premier League).

    ``as_of`` (ISO ts) makes it POINT-IN-TIME: only appearances in fixtures that kicked
    off strictly before it contribute. This is the same contract form_index and
    xg_form_index already honour, and it was the one blend of the five that did not —
    so a walk-forward model for a July week was scoring July matches with squad quality
    computed from August appearances. Measured over the 673-match walk-forward, cutting
    it is not merely more honest but more accurate: Brier 0.6273 → 0.6234 (paired
    t = 3.37), and the skill over base rates rises from +0.0148 to +0.0188.

    Under a cut the season-aggregate topscorer feed (``player_stat``) is NOT consulted.
    That table carries one row per player per SEASON with no match dates, so there is no
    honest way to ask it what was known in July; including it would smuggle the whole
    season back in through the door the cut just closed. A club with no appearances
    before ``as_of`` is therefore absent from the index, and squad_adjusted_ratings
    leaves its rating untouched — which is the truth about what we knew.
    """
    # CLUB EDITION (§2.3-#5): the WC squad-table indirection (player → national squad →
    # club stats) is gone — the club is on the stat row itself; aggregate directly.
    #
    # SOURCE (fixed 2026-08-26): `player_stat` is the TOPSCORERS feed — only each
    # league's ~20 leading scorers — so aggregating it produced a club index built
    # from 1-3 hand-picked attackers (AS Roma ranked #1 on two players, Real Madrid
    # absent entirely) that also leaked into the live ratings via squad_blend_weight.
    # `fixture_player_stats` is the per-fixture lineup feed: EVERY player who
    # appeared, with real minutes and match ratings. Use it when present and fall
    # back to the topscorer feed only for clubs it does not cover yet.
    # The LEFT JOIN becomes an INNER JOIN under a cut: a stat row whose fixture we
    # cannot date cannot be placed before or after `as_of`, so it has no place in a
    # point-in-time index.
    _join = "JOIN" if as_of else "LEFT JOIN"
    _where = "WHERE tm.canonical_team_id IS NOT NULL" + (" AND f.kickoff_ts < ?" if as_of else "")
    rows = conn.execute(
        "SELECT tm.canonical_team_id cid, fps.rating, fps.goals, fps.assists, fps.minutes, "
        "       f.league_id "
        "FROM fixture_player_stats fps "
        "JOIN team_meta tm ON tm.api_id = fps.team_api_id "
        f"{_join} fixture f ON f.api_id = fps.fixture_api_id "
        f"{_where}", ((as_of,) if as_of else ())).fetchall()
    if not as_of:
        covered = {r["cid"] for r in rows}
        rows = list(rows) + [r for r in conn.execute(
            "SELECT tm.canonical_team_id cid, ps.rating, ps.goals, ps.assists, ps.minutes, ps.league_id "
            "FROM player_stat ps JOIN team_meta tm ON tm.api_id = ps.team_api_id "
            "WHERE tm.canonical_team_id IS NOT NULL").fetchall()
            if r["cid"] not in covered]
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
    from prediction_market_soccer.model.strength import StrengthModel
    if weight <= 0 or not idx:
        return sm
    from dataclasses import replace as _replace
    b = sm.cfg.rating_bound
    new = dict(sm.ratings)
    for tid, s in idx.items():
        if tid in new:
            new[tid] = max(-b, min(b, new[tid] + weight * s.score_z))
    return _replace(sm, ratings=new)   # keeps comp/base_mu/home_adv (per-league, C2)


def build_strength_live(conn, prior=None, cfg=None, *, as_of: str | None = None,
                        xg_form: bool = False, league: str | None = None):
    """The LIVE strength model: base ratings (prior + structural params) with the
    squad-strength AND recent-form blends applied (cfg.squad_blend_weight /
    cfg.form_blend_weight; 0 ⇒ off). Single entry point so every user-facing export
    uses the same model.

    ``as_of`` (ISO ts) makes the recent-form blends POINT-IN-TIME: only data strictly
    before it contributes (``None`` ⇒ all data, correct for an upcoming/live match).
    This matters because nt_recent (the recent-results table, club rows projected into it
    by ``project_results_to_club_recent``) carries results from the whole season, so a
    historical/backtest caller MUST pass as_of=kickoff or the form blend leaks the match's
    own later results.

    ``xg_form`` opt-in (PIT/live prediction paths only) applies the xG-form alpha
    (cfg.xg_form_blend_weight). It is OFF by default so the param sweep / calibration —
    which score one model across ALL settled matches — can never leak a match's own xG.
    """
    from prediction_market_soccer.model.strength import StrengthModel, build_strength
    cfg = cfg or CONFIG.model
    sm = build_strength(prior, cfg, league=league)
    # Host rating boost — a no-op in the club edition: a league has no host nation, and
    # StrengthModel.host_ids is the always-empty back-compat shim (model/strength.py), so
    # this branch never fires. Kept (not deleted) because per-league home advantage lives
    # in the registry instead, and a future neutral-venue final could reuse the hook.
    hb = getattr(cfg, "host_rating_boost", 0.0)
    if hb and sm.host_ids:
        b = sm.cfg.rating_bound
        from dataclasses import replace as _replace
        nw = {t: (max(-b, min(b, r + hb)) if t in sm.host_ids else r) for t, r in sm.ratings.items()}
        sm = _replace(sm, ratings=nw)
    if conn is None:
        return sm
    sw = getattr(cfg, "squad_blend_weight", 0.0)
    if sw:
        try:
            sm = squad_adjusted_ratings(sm, squad_index(conn, as_of=as_of), sw)
        except Exception as e:
            print(f"[build_strength_live] squad blend skipped: {type(e).__name__}: {e}")
    fw = getattr(cfg, "form_blend_weight", 0.0)
    if fw:
        try:
            from prediction_market_soccer.model.form_strength import form_adjusted_ratings, form_index
            sm = form_adjusted_ratings(sm, form_index(conn, as_of=as_of), fw)
        except Exception as e:
            print(f"[build_strength_live] form blend skipped: {type(e).__name__}: {e}")
    xw = getattr(cfg, "xg_form_blend_weight", 0.0)
    if xg_form and xw:
        try:
            from prediction_market_soccer.model.xg_form import xg_form_adjusted_ratings, xg_form_index
            sm = xg_form_adjusted_ratings(sm, xg_form_index(conn, as_of=as_of), xw)
        except Exception as e:
            print(f"[build_strength_live] xg-form blend skipped: {type(e).__name__}: {e}")
    cw = getattr(cfg, "fc_blend_weight", 0.0)
    if cw:
        try:
            from prediction_market_soccer.model.fc_strength import fc_adjusted_ratings, fc_squad_index
            sm = fc_adjusted_ratings(sm, fc_squad_index(conn), cw)
        except Exception as e:
            print(f"[build_strength_live] fc blend skipped: {type(e).__name__}: {e}")
    # Alt-data λ adjustments (plan 19): attach LAST (ratings/blends already applied) so
    # pair_lambdas can apply the opponent-adjusted form / xGA multipliers. Only computed
    # when at least one weight is non-zero → zero cost + prod unchanged at the 0 default.
    if any(getattr(cfg, w, 0.0) for w in ("oppadj_def_weight", "oppadj_off_weight", "xga_weight")):
        try:
            from dataclasses import replace as _replace
            from prediction_market_soccer.model.altdata_adjust import altdata_index
            # as_of-cut so the opponent-adjusted alt-data is POINT-IN-TIME (it was a leak:
            # without as_of it used a team's later/own matches). Historical/backtest callers
            # MUST pass as_of=kickoff; live callers pass None (= all data, correct).
            sm = _replace(sm, adj=altdata_index(conn, sm.ratings, as_of=as_of))
        except Exception as e:
            print(f"[build_strength_live] altdata adj skipped: {type(e).__name__}: {e}")
    return sm


if __name__ == "__main__":
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    conn = store.init_db()
    name = {t.club_id: t.name for t in load_prior().teams}
    idx = squad_index(conn)
    ranked = sorted(idx.values(), key=lambda s: -s.score_z)
    print(f"squad strength (top 12 of {len(idx)} teams):")
    for s in ranked[:12]:
        print(f"  {name.get(s.team_id, s.team_id):<14} z={s.score_z:+.2f}  mw_rating={s.mw_rating:.2f}  ga/90={s.ga_per90:.2f}  ({s.n_players}p)")
