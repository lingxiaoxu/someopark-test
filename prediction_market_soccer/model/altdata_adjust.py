"""model/altdata_adjust.py — alternative-data λ adjustments (plan 19).

A set of SMALL, BOUNDED, parameter-controlled signals that nudge the Dixon-Coles
lambdas asymmetrically (attack vs defence), so the model can express things a single
team rating can't — most importantly "this underdog is resilient enough to DRAW the
favourite" (suppress the favourite's λ without raising the underdog's win odds).

Every signal is z-scored across the field and applied behind a weight in ModelConfig
(all default 0 → the live model is unchanged until explicitly enabled). Point-in-time
safe: pass `as_of` and only data strictly before it is used.

Signals (each → a per-team z-index):
  * def_z  — opponent-strength-adjusted DEFENSIVE form (held strong attackers to few
             goals): from nt_recent, competitive-weighted, credited by opponent rating.
  * off_z  — opponent-strength-adjusted OFFENSIVE form (scored vs strong defences).
  * xga_z  — recent shot-quality conceded (xGA), a cleaner process metric than goals
             (needs xG history in fixture_stats; thin pre-tournament → small by default).

The match-level VENUE-CLIMATE signal (heat / altitude / closed roof) lives in
`venue_climate.py` because it's a property of the fixture, not a team.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

_FRIENDLY_W = 0.5     # a friendly counts half a competitive game
_RECENT_N = 10   # ~5 weeks of club fixtures (was 6 internationals ≈ 2 years)         # look-back window of recent NT matches


@dataclass(frozen=True)
class TeamAdj:
    def_z: float = 0.0
    off_z: float = 0.0
    xga_z: float = 0.0


def _zscore(d: dict[str, float]) -> dict[str, float]:
    vals = [v for v in d.values() if v is not None]
    if not vals:
        return {k: 0.0 for k in d}
    mu = sum(vals) / len(vals)
    var = sum((v - mu) ** 2 for v in vals) / len(vals)
    sd = math.sqrt(var) or 1.0
    return {k: (v - mu) / sd if v is not None else 0.0 for k, v in d.items()}


def altdata_index(conn, ratings: dict[str, float], *, as_of: str | None = None,
                  unknown_opp: float | None = None, shrink_k: float = 0.0,
                  all_ratings: dict[str, float] | None = None,
                  clubs: set[str] | None = None, mode: str = "wc",
                  mu: float = 0.30, ha: float = 0.15, beta: float = 0.40) -> dict[str, TeamAdj]:
    """{canonical_team_id: TeamAdj} — opponent-adjusted attack/defence form + xGA.

    `ratings` supplies opponent strength (so conceding little vs a strong attacker, or
    scoring vs a strong defence, counts more). `as_of` (ISO ts) makes it point-in-time:
    only nt_recent / fixture_stats strictly before it are used.

    CLUB EDITION knobs (the WC version had none — every WC opponent was one of the
    48 rated teams, so the questions below never arose):

    ``unknown_opp``   rating credited to an opponent that is NOT in ``ratings``. The
        WC default of 1.0 means "treat every unrated opponent as a strong side": in
        club football 7 of a Bundesliga club's last 10 opponents are European or
        friendly opposition outside its own league table, so that default handed out
        large defensive credit for ordinary results. None ⇒ 0.0 (league-average).
    ``all_ratings``   optional cross-competition rating lookup consulted before
        falling back to ``unknown_opp`` — a European opponent usually IS rated, just
        in another competition's model.
    ``shrink_k``      small-sample shrinkage: the raw index is scaled by
        n/(n+shrink_k) before z-scoring, so a club with two recorded matches cannot
        sit at the extreme of the distribution. 0 ⇒ off (WC behaviour).
    ``mode``          how the raw signal is formed.
        "wc"       — the inherited formula: ``opponent_rating − goals_conceded`` for
                     defence and ``goals_scored − 1/opponent_rating`` for attack. It
                     mixes a rating with a goal count, so the same z can mean very
                     different things depending on where a club sits in the rating
                     scale — which is why it did not survive club validation.
        "residual" — performance against the model's OWN expectation, in goals:
                     defence = E[goals conceded vs that opponent, that venue] − actual,
                     attack  = actual scored − E[goals scored]. Dimensionally clean and
                     already opponent- and venue-adjusted by construction.
        ``mu``/``ha``/``beta`` supply that expectation (the competition's fitted
        base_mu / home_adv / beta).
    """
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    rev = {v: k for k, v in cmap.items()}
    # ``clubs`` restricts the index to one competition's field: the historical
    # weight fit rebuilds this index for hundreds of as-of dates, and walking
    # every club in the database each time is what makes that unaffordable.
    if clubs:
        rev = {k: v for k, v in rev.items() if k in clubs}
    _unknown = 1.0 if unknown_opp is None else float(unknown_opp)

    def _orat(opp_cid):
        if opp_cid in ratings:
            return ratings[opp_cid]
        if all_ratings and opp_cid in all_ratings:
            return all_ratings[opp_cid]
        return _unknown

    def_raw: dict[str, float] = {}
    off_raw: dict[str, float] = {}
    xga_raw: dict[str, float] = {}
    for cid, api in rev.items():
        q = ("SELECT opp_api_id, gf, ga, is_friendly, is_home FROM nt_recent WHERE team_api_id=? "
             + ("AND kickoff_ts < ? " if as_of else "")
             + "ORDER BY kickoff_ts DESC LIMIT ?")
        params: list = [api] + ([as_of] if as_of else []) + [_RECENT_N]
        rows = conn.execute(q, params).fetchall()
        if not rows:
            continue
        dn = on = den = 0.0
        for r in rows:
            w = _FRIENDLY_W if r["is_friendly"] else 1.0
            orat = _orat(cmap.get(r["opp_api_id"]))
            gf, ga = (r["gf"] or 0), (r["ga"] or 0)
            if mode == "residual":
                srat = ratings.get(cid)
                if srat is None:
                    continue
                d = srat - orat
                if r["is_home"]:
                    lam_self = math.exp(mu + ha + beta * d)
                    lam_opp = math.exp(mu - beta * d)
                else:
                    lam_self = math.exp(mu + beta * d)
                    lam_opp = math.exp(mu + ha - beta * d)
                dn += (lam_opp - ga) * w        # conceded FEWER than expected → resilient
                on += (gf - lam_self) * w       # scored MORE than expected → in form
            else:
                dn += (orat - ga) * w                            # defensive credit (WC)
                on += (gf - 1.0 / max(orat, 0.3)) * w            # offensive credit (WC)
            den += w
        if den:
            # shrink toward the field average by n/(n+k) before z-scoring, so a club
            # with two recorded matches cannot occupy the tail of the distribution
            sh = (den / (den + shrink_k)) if shrink_k else 1.0
            def_raw[cid], off_raw[cid] = (dn / den) * sh, (on / den) * sh

    # xGA process metric (recent shot-quality conceded). Only where xG history exists.
    for cid, api in rev.items():
        q = ("SELECT fs.xg FROM fixture_stats fs JOIN fixture f ON f.api_id = fs.fixture_api_id "
             "WHERE fs.team_api_id != ? AND (f.home_api_id=? OR f.away_api_id=?) AND fs.xg IS NOT NULL "
             + ("AND f.kickoff_ts < ? " if as_of else "")
             + "ORDER BY f.kickoff_ts DESC LIMIT ?")
        params = [api, api, api] + ([as_of] if as_of else []) + [_RECENT_N]
        xs = [r["xg"] for r in conn.execute(q, params).fetchall() if r["xg"] is not None]
        if xs:
            xga_raw[cid] = -sum(xs) / len(xs)   # less xG conceded → higher (better defence)

    dz, oz, xz = _zscore(def_raw), _zscore(off_raw), _zscore(xga_raw)
    out: dict[str, TeamAdj] = {}
    for cid in set(dz) | set(oz) | set(xz):
        out[cid] = TeamAdj(def_z=round(dz.get(cid, 0.0), 4),
                           off_z=round(oz.get(cid, 0.0), 4),
                           xga_z=round(xz.get(cid, 0.0), 4))
    return out
