"""Model-aware cash-out (the validated smart-exit) — shared by the price-track and the
performance report so both report the REALISED strategy (we cash out, we don't hold to FT).

Validated PIT on the per-minute price_tick paths (ops/_smart_exit_research): SELL a held
pick when the market price over-reacts above the live model fair (+margin), lock the
overshoot before it reverts; otherwise hold to settlement. On the production bets this
turned 7W-11L / +114¢ (hold) into 12/18 profitable / +302¢ (cash-out).
"""
from __future__ import annotations

OVERSHOOT_MARGIN = 0.12   # market this far above live model fair = over-reaction → lock


def smart_exit_cashout(conn, sm, fid, pick, entry_c, hi, ai, round_name, won,
                       *, margin: float = OVERSHOOT_MARGIN):
    """Return {sold_min, sold_c, pnl_c, vs_hold_c} for a settled pick, or None if no price
    ticks / the overshoot never triggered (then the bet is simply held to FT).

    ``sm`` is the PIT strength model (for pre-match λ); the score is reconstructed from
    fixture_event; the per-minute market price comes from the price_tick backfill."""
    if entry_c is None or pick not in ("home", "draw", "away"):
        return None
    ticks = conn.execute(
        "SELECT rel_min, price FROM price_tick WHERE fixture_api_id=? AND side=? AND rel_min BETWEEN 1 AND 125 "
        "ORDER BY ts", (fid, pick)).fetchall()
    if len(ticks) < 10:
        return None
    from prediction_market.model.inplay import live_match_prob
    from prediction_market.model.match_pricing import is_knockout
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

    for t in ticks:
        mn = min(95, max(1, t["rel_min"]))
        sh, sa = score_at(mn)
        lp = live_match_prob(lam_h, lam_a, mn, sh, sa)
        fair = {"home": lp.p_home, "draw": lp.p_draw, "away": lp.p_away}[pick] * 100.0
        mkt = t["price"] * 100.0
        if mkt >= fair + margin * 100.0:
            hold = (100.0 - entry_c) if won else -entry_c
            return {"sold_min": int(t["rel_min"]), "sold_c": round(mkt, 1),
                    "pnl_c": round(mkt - entry_c, 1), "vs_hold_c": round((mkt - entry_c) - hold, 1)}
    return None
