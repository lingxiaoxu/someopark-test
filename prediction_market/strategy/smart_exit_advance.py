"""Model-aware cash-out — 2-WAY "ADVANCE" FORK (plan 24 §5) of smart_exit.py.

Knockout who-advances twin of strategy/smart_exit.py. Differences from the 3-way version:
  * pick ∈ {home, away} (no draw).
  * the advance contract settles on WHO ADVANCES (ET + penalties), NOT the 90' result — so the
    scan window EXTENDS past regulation through extra time and penalties (no ≤95 gate).
  * fair is the live ADVANCE probability (model/inplay_advance.live_advance_prob), period-aware
    (reg/et/pens by match minute), NOT the 90' 3-way fair.
  * ticks come from the separate `price_tick_adv` table (the advance market's per-minute path).
The 3-way smart_exit.py is UNCHANGED and runs in parallel.
"""
from __future__ import annotations

OVERSHOOT_MARGIN = 0.08   # market this far above live advance fair = over-reaction → lock.
# Inherited from the 3-way smart-exit plateau (0.06–0.08); RECALIBRATE on knockout advance
# paths once enough settled KO matches exist (plan 24 §10).

_HALFTIME_WALL_MIN = 15      # typical half-time break (wall-clock minutes with no play)
_ADV_MAX_MATCH_MIN = 132     # through extra time (120) + a buffer for penalties
_SCAN_MAX_RELMIN = 210       # wall-clock ceiling covering ET + the shoot-out


def _match_minute(rel_min: int) -> int:
    """Approximate MATCH minute from wall-clock minutes-since-kickoff (half-time offset is the
    dominant correction; ET adds a further break but we keep the single-offset approximation —
    the advance window is gated loosely at ≤132 so exact ET mapping isn't critical)."""
    if rel_min <= 45:
        return rel_min
    if rel_min <= 45 + _HALFTIME_WALL_MIN:
        return 45
    return rel_min - _HALFTIME_WALL_MIN


def _period_for_minute(mn: int) -> str:
    if mn > 120:
        return "pens"
    if mn > 95:
        return "et"
    return "reg"


def smart_exit_cashout_advance(conn, sm, fid, pick, entry_c, hi, ai, round_name, won,
                               *, margin: float = OVERSHOOT_MARGIN):
    """Return {sold_min, sold_c, pnl_c, vs_hold_c} for a settled ADVANCE pick, or None if no
    advance price ticks / the overshoot never triggered (then held to the advance settlement).

    ``won`` = did ``pick`` actually advance (the caller computes it from the advancer). Fair at
    each tick is the live advance prob for ``pick`` at that match state (period-aware)."""
    if entry_c is None or pick not in ("home", "away"):
        return None
    raw = conn.execute(
        "SELECT rel_min, price FROM price_tick_adv WHERE fixture_api_id=? AND side=? AND rel_min BETWEEN 1 AND ? "
        "ORDER BY ts", (fid, pick, _SCAN_MAX_RELMIN)).fetchall()
    ticks = [(_match_minute(r["rel_min"]), r["price"]) for r in raw]
    ticks = [(mn, px) for (mn, px) in ticks if 1 <= mn <= _ADV_MAX_MATCH_MIN]
    if len(ticks) < 10:
        return None
    from prediction_market.model.inplay_advance import live_advance_prob
    from prediction_market.model.penalties import shootout_win_prob_detailed
    lam_h, lam_a = sm.pair_lambdas(hi, ai, knockout=True, host_neutral=True)
    shootout_home = shootout_win_prob_detailed(sm, hi, ai)
    home_row = conn.execute("SELECT home_api_id FROM fixture WHERE api_id=?", (fid,)).fetchone()
    home_api = home_row[0] if home_row else None
    evs = conn.execute("SELECT minute, team_api_id, detail FROM fixture_event WHERE fixture_api_id=? AND type='Goal' "
                       "ORDER BY minute, seq", (fid,)).fetchall()
    tl, gh, ga = [], 0, 0
    for e in evs:
        if (e["detail"] or "") == "Missed Penalty":
            continue
        if (e["team_api_id"] == home_api) ^ ((e["detail"] or "") == "Own Goal"):
            gh += 1
        else:
            ga += 1
        tl.append((e["minute"] or 0, gh, ga))

    def score_at(mn):
        h = a = 0
        for m, hh, aa in tl:
            if m <= mn:
                h, a = hh, aa
            else:
                break
        return h, a

    for mn, price in ticks:
        sh, sa = score_at(mn)
        period = _period_for_minute(mn)
        lap = live_advance_prob(lam_h, lam_a, mn, sh, sa, period=period,
                                shootout_home=shootout_home, et_home_goals=sh, et_away_goals=sa)
        fair = (lap.p_home_advance if pick == "home" else lap.p_away_advance) * 100.0
        mkt = price * 100.0
        if mkt >= fair + margin * 100.0:
            hold = (100.0 - entry_c) if won else -entry_c
            return {"sold_min": int(mn), "sold_c": round(mkt, 1),
                    "pnl_c": round(mkt - entry_c, 1), "vs_hold_c": round((mkt - entry_c) - hold, 1)}
    return None
