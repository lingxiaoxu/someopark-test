"""EA Sports FC 26 squad-quality strength anchor (plan 17 B.3 extension).

The FC ratings already ground the golden boot; here they also sharpen the MATCH model. A
club's strength is well predicted by the quality of its best players, and EA's `overall`
is a clean, current, minutes-independent talent measure — complementary to the club-prior
anchor (last-season results / ClubElo) and the squad club-form blend (goals/assists/
minutes). We z-score each club's top-N mean overall across the field and blend it into the
ratings, exactly like the squad/form anchors.

WHY _TOP_N = 18 AND NOT THE WORLD CUP'S 16. Two facts, and only one of them is a fit:
  * Fit: the correlation between a club's top-N mean overall and its actual goal
    difference per match is flat in N — 0.501 at N=11, 0.496 at N=16, 0.493 at N=18,
    0.483 at N=25 (152 clubs with ≥10 played matches). The differences are inside the
    noise at that sample, so N is a free choice, not an optimum to be found. Recording
    that is the point: nobody should later "tune" N on this data and believe the result.
  * Judgement: clubs play Saturday–Wednesday–Saturday and rotate through a wider group
    than a national team does in a tournament, so the squad depth that actually takes the
    pitch is larger. 18 ≈ an XI plus a full bench.
"""
from __future__ import annotations

import math

_TOP_N = 18   # best N players define a club's ceiling (XI + a rotation bench)
# Below this many listed players the mean is a subsample of the club, not its top end:
# 6 clubs in the current FC table have fewer than 16 (one has 9), and a 9-player mean is
# whichever players EA happens to carry, on a scale where the z-index feeds live ratings.
# Such a club is dropped from the index entirely — it then simply keeps its base rating,
# which is the honest "no information" outcome rather than a confident wrong nudge.
_MIN_ROSTER = 16


def fc_squad_index(conn) -> dict[str, float]:
    """{canonical_team_id: z-scored top-18 FC overall} across the club field.

    Clubs with fewer than `_MIN_ROSTER` listed players are excluded — from the z-score pool
    as well as from the output, so their unrepresentative means cannot drag the mean/sd the
    other clubs are scored against."""
    rows = conn.execute(
        "SELECT canonical_team_id cid, overall FROM fc_player "
        "WHERE canonical_team_id IS NOT NULL AND overall IS NOT NULL "
        "ORDER BY canonical_team_id, overall DESC"
    ).fetchall()
    by_team: dict[str, list[int]] = {}
    for r in rows:
        by_team.setdefault(r["cid"], []).append(int(r["overall"]))
    quality = {cid: sum(sorted(v, reverse=True)[:_TOP_N]) / min(_TOP_N, len(v))
               for cid, v in by_team.items() if len(v) >= _MIN_ROSTER}
    if not quality:
        return {}
    vals = list(quality.values())
    mu = sum(vals) / len(vals)
    var = sum((x - mu) ** 2 for x in vals) / len(vals)
    sd = math.sqrt(var) or 1.0
    return {cid: round((q - mu) / sd, 4) for cid, q in quality.items()}


def fc_adjusted_ratings(sm, idx: dict[str, float], weight: float):
    """Blend the FC squad-quality z-index into the model ratings. weight=0 ⇒ unchanged.
    Returns a new StrengthModel, clipped to the rating bound."""
    from prediction_market_soccer.model.strength import StrengthModel
    if weight <= 0 or not idx:
        return sm
    b = sm.cfg.rating_bound
    new = dict(sm.ratings)
    for tid, z in idx.items():
        if tid in new:
            new[tid] = max(-b, min(b, new[tid] + weight * z))
    return __import__('dataclasses').replace(sm, ratings=new)  # keeps per-league fields (C2)
