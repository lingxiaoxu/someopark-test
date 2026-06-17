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
_RECENT_N = 6         # look-back window of recent NT matches


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


def altdata_index(conn, ratings: dict[str, float], *, as_of: str | None = None) -> dict[str, TeamAdj]:
    """{canonical_team_id: TeamAdj} — opponent-adjusted attack/defence form + xGA.

    `ratings` supplies opponent strength (so conceding little vs a strong attacker, or
    scoring vs a strong defence, counts more). `as_of` (ISO ts) makes it point-in-time:
    only nt_recent / fixture_stats strictly before it are used.
    """
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    rev = {v: k for k, v in cmap.items()}

    def_raw: dict[str, float] = {}
    off_raw: dict[str, float] = {}
    xga_raw: dict[str, float] = {}
    for cid, api in rev.items():
        q = ("SELECT opp_api_id, gf, ga, is_friendly FROM nt_recent WHERE team_api_id=? "
             + ("AND kickoff_ts < ? " if as_of else "")
             + "ORDER BY kickoff_ts DESC LIMIT ?")
        params: list = [api] + ([as_of] if as_of else []) + [_RECENT_N]
        rows = conn.execute(q, params).fetchall()
        if not rows:
            continue
        dn = on = den = 0.0
        for r in rows:
            w = _FRIENDLY_W if r["is_friendly"] else 1.0
            orat = ratings.get(cmap.get(r["opp_api_id"]), 1.0)
            dn += (orat - (r["ga"] or 0)) * w                       # defensive credit
            on += ((r["gf"] or 0) - 1.0 / max(orat, 0.3)) * w       # offensive credit
            den += w
        if den:
            def_raw[cid], off_raw[cid] = dn / den, on / den

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
