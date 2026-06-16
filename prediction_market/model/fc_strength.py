"""EA Sports FC 26 squad-quality strength anchor (plan 17 B.3 extension).

The FC ratings already ground the golden boot; here they also sharpen the MATCH
model. A national team's strength is well predicted by the quality of its best ~16
players, and EA's `overall` is a clean, current, minutes-independent talent measure —
complementary to the FIFA-rank anchor (results-based) and the squad club-form blend
(goals/assists/minutes). We z-score each team's top-16 mean overall across the field
and blend it into the ratings, exactly like the squad/form anchors.
"""
from __future__ import annotations

import math

_TOP_N = 16   # best N players define a national team's ceiling (XI + key bench)


def fc_squad_index(conn) -> dict[str, float]:
    """{canonical_team_id: z-scored top-16 FC overall} across the WC field."""
    rows = conn.execute(
        "SELECT canonical_team_id cid, overall FROM fc_player "
        "WHERE canonical_team_id IS NOT NULL AND overall IS NOT NULL "
        "ORDER BY canonical_team_id, overall DESC"
    ).fetchall()
    by_team: dict[str, list[int]] = {}
    for r in rows:
        by_team.setdefault(r["cid"], []).append(int(r["overall"]))
    quality = {cid: sum(sorted(v, reverse=True)[:_TOP_N]) / min(_TOP_N, len(v))
               for cid, v in by_team.items() if v}
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
    from prediction_market.model.strength import StrengthModel
    if weight <= 0 or not idx:
        return sm
    b = sm.cfg.rating_bound
    new = dict(sm.ratings)
    for tid, z in idx.items():
        if tid in new:
            new[tid] = max(-b, min(b, new[tid] + weight * z))
    return StrengthModel(ratings=new, sigma=dict(sm.sigma), host_ids=sm.host_ids, cfg=sm.cfg)
