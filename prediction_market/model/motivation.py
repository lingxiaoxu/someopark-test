"""Match-motivation λ multipliers — group-progression psychology.

A LIVE betting-only tilt (NOT used in calibration / OOS / the trade-grade gate, so the
gate stays validated on the clean base model). Two effects, factor 1 > factor 2:

  Factor 1 — wounded favourite bounce-back. A strong team (fifa_rank ≤ motiv_strong_rank)
  that dropped points in game 1 goes all-out against a WEAKER, NON-host opponent:
    * lost game 1   → full intensity in game 2 AND game 3 (must win to survive);
    * drew game 1   → full intensity in game 2, ~10% relaxed in game 3 (still hard);
    * won game 1    → no boost (state is fine).

  Factor 2 — clinched rotation. A team already through (points ≥ r3_clinch_points after 2)
  eases off a little in game 3. (The knockout opponent that would justify resting is still
  undecided pre-game-3, so this stays a small, flat reduction — deliberately conservative.)

Returns (mult_home, mult_away, info) — multipliers on each side's attacking λ, plus a small
dict describing what fired (for transparency in upcoming.json). (1.0, 1.0, None) when nothing
applies, when disabled, or for knockout / game-1 fixtures.
"""
from __future__ import annotations

import re

HOSTS = {"united_states", "canada", "mexico"}   # 2026 co-hosts — never treated as the pushover


def _round_num(round_name) -> int | None:
    """Group-stage round number (1/2/3), else None (knockout or unparseable)."""
    s = str(round_name or "")
    if "group" not in s.lower():
        return None
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


def _played_group(conn, cmap: dict, before_round: int | None = None) -> dict[str, list[tuple[int, str, int]]]:
    """canonical_team_id → [(round_num, 'W'/'D'/'L', points), …] for FINISHED group games.

    PIT guard: when ``before_round`` is given, ONLY games in strictly earlier rounds are
    counted — so replaying a game-2 match sees game 1 only (never the game-2/3 results we
    are pricing or that came later). Live (game not played) is naturally leak-free too."""
    out: dict[str, list[tuple[int, str, int]]] = {}
    for r in conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals, round FROM fixture "
        "WHERE round LIKE '%roup%' AND status_short IN ('FT','AET','PEN') AND home_goals IS NOT NULL"
    ):
        rn = _round_num(r["round"])
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if rn is None or not (hi and ai) or (before_round is not None and rn >= before_round):
            continue
        hg, ag = r["home_goals"], r["away_goals"]
        for tid, gf, ga in ((hi, hg, ag), (ai, ag, hg)):
            res = "W" if gf > ga else ("L" if gf < ga else "D")
            out.setdefault(tid, []).append((rn, res, 3 if res == "W" else (1 if res == "D" else 0)))
    return out


def motivation_multipliers(conn, fifa_rank: dict, home_id: str, away_id: str,
                           round_name, cfg) -> tuple[float, float, dict | None]:
    if not getattr(cfg, "motiv_enabled", False) or conn is None:
        return 1.0, 1.0, None
    rn = _round_num(round_name)
    if rn not in (2, 3):                       # factor 1 = games 2/3; factor 2 = game 3
        return 1.0, 1.0, None

    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    # PIT: only count games BEFORE this match's round (no look-ahead in the retro replay).
    gp = _played_group(conn, cmap, before_round=rn)

    inc = cfg.motiv_bounce_attack - 1.0        # full attacking increment (e.g. 0.12)
    deftrim = 1.0 - cfg.motiv_bounce_def       # full opponent λ trim (e.g. 0.04)
    rank = lambda t: fifa_rank.get(t, 999)

    mult = {home_id: 1.0, away_id: 1.0}
    reason: dict[str, str] = {}
    conviction_side: str | None = None   # 'home'/'away': the wounded favourite to BET (factor 1)
    for X, O in ((home_id, away_id), (away_id, home_id)):
        gx = gp.get(X, [])
        pts = sum(p for _, _, p in gx)
        g1 = next((res for r_, res, _ in gx if r_ == 1), None)
        side = "home" if X == home_id else "away"

        # Factor 1 — wounded strong favourite vs a weaker, non-host opponent.
        if rank(X) <= cfg.motiv_strong_rank and rank(O) > rank(X) and O not in HOSTS:
            scale = 1.0 if g1 == "L" else (1.0 if (g1 == "D" and rn == 2) else (0.9 if g1 == "D" else 0.0))
            if scale > 0:
                mult[X] *= 1.0 + inc * scale
                mult[O] *= 1.0 - deftrim * scale
                reason[X] = f"wounded_bounce(g1={g1},r{rn},×{1.0 + inc * scale:.3f})"
                conviction_side = side   # a wounded favourite going all-out → conviction to bet it

        # Factor 2 — clinched team eases off in game 3 (small, opponent-agnostic).
        if rn == 3 and len(gx) >= 2 and pts >= cfg.r3_clinch_points:
            mult[X] *= cfg.motiv_rotation_attack
            reason[X] = f"clinched_rotation(pts={pts},×{cfg.motiv_rotation_attack:.3f})"

    mh, ma = round(mult[home_id], 4), round(mult[away_id], 4)
    if mh == 1.0 and ma == 1.0:
        return 1.0, 1.0, None
    return mh, ma, {"mult_home": mh, "mult_away": ma, "reason": reason,
                    "conviction_side": conviction_side}
