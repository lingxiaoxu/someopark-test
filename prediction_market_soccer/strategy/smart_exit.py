"""Model-aware cash-out (the validated smart-exit) — shared by the price-track and the
performance report so both report the REALISED strategy (we cash out, we don't hold to FT).

Validated PIT on the per-minute price_tick paths (ops/_smart_exit_research): SELL a held
pick when the market price over-reacts above the live model fair (+margin), lock the
overshoot before it reverts; otherwise hold to settlement. On the production bets this
turned 7W-11L / +114¢ (hold) into 12/18 profitable / +302¢ (cash-out).
"""
from __future__ import annotations

# Tactical thresholds live in ONE place (model/inplay_constants): four copies of
# OVERSHOOT_MARGIN had already drifted to two different values.
from prediction_market_soccer.model.inplay_constants import (  # noqa: F401
    OVERSHOOT_MARGIN)


_HALFTIME_WALL_MIN = 15      # typical half-time break (wall-clock minutes with no play)
_REG_MAX_MATCH_MIN = 95      # end of regulation incl. stoppage (same clamp the live model uses)
_SCAN_MAX_RELMIN = 170       # sane wall-clock ceiling (covers full ET); match-clock does the gating

# Milestone fallback (club edition): match minute of each recorded in-play milestone,
# and the minimum number of usable points before the exit rule is allowed to act.
_MILESTONE_MIN = {"T15": 15, "T30": 30, "HT": 45, "T60": 60, "T75": 75}
_MIN_MILESTONE_POINTS = 3


def _milestone_ticks(conn, fid: int, pick: str, entry_min: int) -> list[tuple[int, float]]:
    """[(match_minute, price 0-1)] for the pick side from our own milestone snapshots.

    Selling hits the BID; the ask is used only when no bid was recorded. Kalshi is
    preferred (it is the venue these club markets actually quote on) with Poly as
    the fallback, mirroring decision_backtest._exit_cents."""
    rows = conn.execute(
        "SELECT milestone, kalshi_{s}_bid kb, kalshi_{s}_ask ka, poly_{s}_bid pb, poly_{s}_ask pa "
        "FROM milestone_snapshot WHERE fixture_api_id=?".format(s=pick), (fid,)).fetchall()
    out = []
    for r in rows:
        mn = _MILESTONE_MIN.get(r["milestone"])
        if mn is None or mn < max(1, entry_min):
            continue
        px = next((v for v in (r["kb"], r["ka"], r["pb"], r["pa"]) if v is not None), None)
        if px is not None:
            out.append((mn, float(px)))
    return sorted(out)


def _match_minute(rel_min: int) -> int:
    """Approximate MATCH minute from wall-clock minutes-since-kickoff.

    First half (match 0-45) ≈ wall 0-45; then a ~15' half-time break (no match progress);
    second half (match 45-90) ≈ wall 60-110; extra time ≈ wall 120-150. The half-time offset
    is the dominant correction (1st-half stoppage shifts the break by ~1-3', negligible vs the
    5' regulation buffer)."""
    if rel_min <= 45:
        return rel_min
    if rel_min <= 45 + _HALFTIME_WALL_MIN:        # inside the half-time break
        return 45
    return rel_min - _HALFTIME_WALL_MIN


def smart_exit_cashout(conn, sm, fid, pick, entry_c, hi, ai, round_name, won,
                       *, margin: float = OVERSHOOT_MARGIN, entry_min: int = 0):
    """Return {sold_min, sold_c, pnl_c, vs_hold_c} for a settled pick, or None if no price
    ticks / the overshoot never triggered (then the bet is simply held to FT).

    ``sm`` is the PIT strength model (for pre-match λ); the score is reconstructed from
    fixture_event; the per-minute market price comes from the price_tick backfill."""
    if entry_c is None or pick not in ("home", "draw", "away"):
        return None
    # The per-match contract (KXWCGAME / fwc) is the 90-MIN 3-way for BOTH stages — it SETTLES at
    # 90' (a knockout tie pays 'Tie'; extra time / penalties only decide the SEPARATE reach-round
    # product). Pull the in-game ticks (wall-clock window), then gate each on the MATCH clock
    # below — regulation only — so knockout extra time is excluded by the match minute itself.
    raw = conn.execute(
        "SELECT rel_min, price FROM price_tick WHERE fixture_api_id=? AND side=? AND rel_min BETWEEN 1 AND ? "
        "ORDER BY ts", (fid, pick, _SCAN_MAX_RELMIN)).fetchall()
    # Map each tick to its match minute and keep only the post-entry regulation window
    # (≤ 95 match-min). entry_min > 0 for an IN-PLAY entry → only exit on overshoots AFTER
    # we entered (a pre-match entry uses entry_min=0 → the whole match).
    ticks = [(_match_minute(r["rel_min"]), r["price"]) for r in raw]
    ticks = [(mn, px) for (mn, px) in ticks if max(1, entry_min) <= mn <= _REG_MAX_MATCH_MIN]
    if len(ticks) < 10:
        # CLUB EDITION fallback: the minute-level price_tick series comes from the
        # Polymarket global history, which does not list club fixtures — that table
        # is empty here, so the exit rule could never fire. Our own live loop does
        # record milestone snapshots (T15/T30/HT/T60/T75), so fall back to those.
        # Coarser (5 points instead of ~90) — the cash-out can only land on a
        # milestone minute, which is exactly how the exit-timing table reads it.
        ticks = _milestone_ticks(conn, fid, pick, entry_min)
        if len(ticks) < _MIN_MILESTONE_POINTS:
            return None
    from prediction_market_soccer.model.inplay import live_match_prob
    from prediction_market_soccer.model.match_pricing import is_knockout
    lam_h, lam_a = sm.pair_lambdas(hi, ai, knockout=is_knockout(round_name))
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

    for mn, price in ticks:                 # mn is the MATCH minute (regulation, ≤ 95)
        sh, sa = score_at(mn)
        lp = live_match_prob(lam_h, lam_a, mn, sh, sa)
        fair = {"home": lp.p_home, "draw": lp.p_draw, "away": lp.p_away}[pick] * 100.0
        mkt = price * 100.0
        # `margin` stays the caller-supplied ceiling; the effective trigger also
        # respects how much room is left below 100¢ (see inplay_constants).
        from prediction_market_soccer.model.inplay_constants import overshoot_trigger
        trig = min(margin, overshoot_trigger(fair / 100.0))
        if mkt >= fair + trig * 100.0:
            hold = (100.0 - entry_c) if won else -entry_c
            return {"sold_min": int(mn), "sold_c": round(mkt, 1),   # sold_min = true match minute
                    "pnl_c": round(mkt - entry_c, 1), "vs_hold_c": round((mkt - entry_c) - hold, 1)}
    return None
