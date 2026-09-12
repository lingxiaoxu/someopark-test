"""Research cash-out math using explicit candidate inputs.

The live paper lifecycle is independent. Missing paths never mean hold-to-result;
legacy price_tick/milestone/final-event fallbacks cannot enter this calculation.
"""
from __future__ import annotations

from prediction_market_soccer.model.inplay_constants import OVERSHOOT_MARGIN

_HALFTIME_WALL_MIN = 15
_REG_MAX_MATCH_MIN = 95
_SCAN_MAX_RELMIN = 170
_MILESTONE_MIN = {"T15": 15, "T30": 30, "HT": 45, "T60": 60, "T75": 75}
_MIN_MILESTONE_POINTS = 3


def _match_minute(rel_min):
    """Legacy approximate display helper; not used for research execution state."""
    from prediction_market_soccer.util.match_timeline import approximate_clock
    elapsed = approximate_clock(0, rel_min * 60)["elapsed"]
    return elapsed if elapsed is not None else rel_min - _HALFTIME_WALL_MIN


def _milestone_ticks(conn, fid, pick, entry_min):
    """Legacy evidence inspection only. Keep actual clock/source; never bid←ask."""
    if pick not in ("home", "draw", "away"):
        raise ValueError("invalid side")
    rows = conn.execute(
        f"SELECT milestone,ts,elapsed,price_source,kalshi_{pick}_bid AS kb,poly_{pick}_bid AS pb "
        "FROM milestone_snapshot WHERE fixture_api_id=? ORDER BY ts", (fid,))
    return [{"milestone": r["milestone"], "ts": r["ts"], "elapsed": r["elapsed"],
             "price": r["kb"] if r["kb"] is not None else r["pb"],
             "source": r["price_source"], "provenance": "legacy_unknown", "executable": False}
            for r in rows if r["elapsed"] is not None and r["elapsed"] >= entry_min]


def smart_exit_cashout(conn, sm, fid, pick, entry_c, hi, ai, round_name, won,
                       *, margin=OVERSHOOT_MARGIN, entry_min=0, candidate=None, entry_at=None, until=None):
    """Typed result: exited, held_no_trigger, unavailable, or invalid.

    conn/sm remain accepted for call compatibility but are never used as input
    fallbacks. The candidate supplies frozen lambdas, state and price evidence.
    """
    if candidate is None or entry_at is None or until is None:
        return {"status": "unavailable", "reason": "explicit_candidate_and_window_required"}
    if pick not in ("home", "draw", "away") or entry_c is None or not 0 < entry_c < 100:
        return {"status": "invalid", "reason": "invalid_entry"}
    features = candidate.features_at(fid, entry_at)
    path = candidate.exit_path(fid, pick, entry_at, until)
    if features["status"] != "ok" or path["status"] != "ok":
        return {"status": "unavailable", "reason": features["reason"] or path["reason"]}
    values = features["data"]
    lambdas = values.get("base_lambdas", [values.get("lambda_home"), values.get("lambda_away")])
    if any(v is None or v <= 0 for v in lambdas):
        return {"status": "invalid", "reason": "missing_model_lambdas"}
    regulation = [p for p in path["data"] if p["state"].get("period") in ("1H", "HT", "2H") and p["state"].get("elapsed") is not None and max(1,entry_min) <= p["state"]["elapsed"] <= _REG_MAX_MATCH_MIN]
    if len(regulation) < candidate.manifest.get("min_exit_points", 10):
        return {"status": "unavailable", "reason": "insufficient_exit_path"}
    from prediction_market_soccer.model.inplay import live_match_prob
    from prediction_market_soccer.model.inplay_constants import overshoot_trigger
    for point in regulation:
        state, quote = point["state"], point["quote"]
        from prediction_market_soccer.util.match_timeline import quote_state_consistent
        if not quote_state_consistent(quote, state, point["target_at"]):
            return {"status":"unavailable","reason":"quote_precedes_state_change"}
        mn = state.get("elapsed")
        if state.get("period") not in ("1H", "HT", "2H"):
            continue
        if mn is None or mn < max(1, entry_min) or mn > _REG_MAX_MATCH_MIN:
            continue
        price = quote.get("bid") if candidate.manifest.get("require_observed_quotes") else quote.get("price")
        if price is None or not 0 < price < 1:
            return {"status": "unavailable", "reason": "missing_exit_bid_or_reference"}
        lp = live_match_prob(*lambdas, mn, state["home_goals"], state["away_goals"],
                             red_home=state["reds_home"], red_away=state["reds_away"])
        fair = {"home": lp.p_home, "draw": lp.p_draw, "away": lp.p_away}[pick]
        if price >= fair + min(margin, overshoot_trigger(fair)):
            hold = (100 - entry_c) if won else -entry_c
            return {"status": "exited", "sold_min": mn, "sold_at": point["target_at"],
                    "sold_c": round(price * 100, 1), "pnl_c": round(price * 100 - entry_c, 1),
                    "vs_hold_c": round(price * 100 - entry_c - hold, 1),
                    "quote": quote, "state": state, "provenance": path["provenance"]}
    return {"status": "held_no_trigger", "reason": None, "provenance": path["provenance"]}
