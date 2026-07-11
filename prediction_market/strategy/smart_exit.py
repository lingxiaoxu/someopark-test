"""Model-aware cash-out (the validated smart-exit) — shared by the price-track and the
performance report so both report the REALISED strategy (we cash out, we don't hold to FT).

Validated PIT on the per-minute price_tick paths (ops/_smart_exit_research): SELL a held
pick when the market price over-reacts above the live model fair (+margin), lock the
overshoot before it reverts; otherwise hold to settlement. On the production bets this
turned 7W-11L / +114¢ (hold) into 12/18 profitable / +302¢ (cash-out).
"""
from __future__ import annotations

OVERSHOOT_MARGIN = 0.08   # market this far above live model fair = over-reaction → lock.
# Re-tuned on the CORRECTED match-clock basis (after _match_minute fixed the wall-clock-as-
# minute fair): a margin sweep over the settled bets showed a stable plateau ~0.06–0.08 (total
# vs-hold +285¢, 17 fires, 12 helped / 5 hurt) vs the old 0.12 (+230¢, 14 fires). Below 0.06 a
# cliff (noise → an extra false sell, value drops); above 0.08 it decays back toward 0.12. On the
# real sample 0.072 and 0.08 are identical — 0.08 is chosen for the larger safety buffer (further
# from the 0.05→0.06 cliff, more robust to the unseen knockout regime). Group sample; recheck on KO.

# price_tick.rel_min is WALL-CLOCK minutes since kickoff, NOT the match minute — the ~15-min
# half-time break means a 90' game runs to ~110-115 wall-clock and extra time to ~120-150.
# We map wall-clock → MATCH minute and gate on the match clock so the cash-out logic is
# venue/stoppage-independent: the fair is priced at the true match minute, and the scan stops
# at end of regulation (match-min ≤ 95 = 90' + stoppage) — which excludes knockout extra time
# (the 90' 3-way contract has already settled) for ANY wall-clock drift, no magic number.
_HALFTIME_WALL_MIN = 15      # typical half-time break (wall-clock minutes with no play)
_REG_MAX_MATCH_MIN = 95      # end of regulation incl. stoppage (same clamp the live model uses)
_SCAN_MAX_RELMIN = 170       # sane wall-clock ceiling (covers full ET); match-clock does the gating


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

    for mn, price in ticks:                 # mn is the MATCH minute (regulation, ≤ 95)
        sh, sa = score_at(mn)
        lp = live_match_prob(lam_h, lam_a, mn, sh, sa)
        fair = {"home": lp.p_home, "draw": lp.p_draw, "away": lp.p_away}[pick] * 100.0
        mkt = price * 100.0
        if mkt >= fair + margin * 100.0:
            hold = (100.0 - entry_c) if won else -entry_c
            return {"sold_min": int(mn), "sold_c": round(mkt, 1),   # sold_min = true match minute
                    "pnl_c": round(mkt - entry_c, 1), "vs_hold_c": round((mkt - entry_c) - hold, 1)}
    return None
