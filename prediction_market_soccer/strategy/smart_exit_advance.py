"""Advance research cash-out, with explicit phase and settlement contract."""
from __future__ import annotations

from prediction_market_soccer.model.inplay_constants import OVERSHOOT_MARGIN
from prediction_market_soccer.strategy.smart_exit import _match_minute

_HALFTIME_WALL_MIN = 15
_ADV_MAX_MATCH_MIN = 132
_SCAN_MAX_RELMIN = 210


def _period_for_minute(mn):
    raise ValueError("advance period requires an observed phase, not a minute heuristic")


def smart_exit_cashout_advance(conn, sm, fid, pick, entry_c, hi, ai, round_name, won,
                               *, margin=OVERSHOOT_MARGIN, candidate=None, entry_at=None, until=None):
    if candidate is None or entry_at is None or until is None:
        return {"status": "unavailable", "reason": "explicit_candidate_and_window_required"}
    if pick not in ("home", "away") or entry_c is None or not 0 < entry_c < 100:
        return {"status": "invalid", "reason": "invalid_advance_entry"}
    features = candidate.features_at(fid, entry_at)
    path = candidate.exit_path(fid, pick, entry_at, until, market_kind="advance")
    if features["status"] != "ok" or path["status"] != "ok":
        return {"status": "unavailable", "reason": features["reason"] or path["reason"]}
    v = features["data"]
    if not all(k in v for k in ("lambda_home", "lambda_away", "shootout_home")):
        return {"status": "unavailable", "reason": "missing_advance_model_inputs"}
    if len(path["data"]) < candidate.manifest.get("min_exit_points", 10):
        return {"status": "unavailable", "reason": "insufficient_exit_path"}
    from prediction_market_soccer.model.inplay_advance import live_advance_prob
    for point in path["data"]:
        s, q = point["state"], point["quote"]
        from prediction_market_soccer.util.match_timeline import quote_state_consistent
        if not quote_state_consistent(q, s, point["target_at"]):
            return {"status":"unavailable","reason":"quote_precedes_state_change"}
        period = {"1H": "reg", "HT": "reg", "2H": "reg", "ET1": "et", "ETHT": "et", "ET2": "et", "PEN": "pens"}.get(s.get("period"))
        if period is None:
            return {"status": "unavailable", "reason": "advance_phase_unknown"}
        if period in ("et", "pens") and any(k not in s for k in ("et_home_goals", "et_away_goals")):
            return {"status": "unavailable", "reason": "extra_time_state_unknown"}
        price = q.get("bid") if candidate.manifest.get("require_observed_quotes") else q.get("price")
        if price is None or not 0 < price < 1:
            return {"status": "unavailable", "reason": "missing_advance_exit_price"}
        lp = live_advance_prob(v["lambda_home"], v["lambda_away"], s["elapsed"], s["home_goals"], s["away_goals"], period=period,
                               shootout_home=v["shootout_home"], et_home_goals=s.get("et_home_goals", 0), et_away_goals=s.get("et_away_goals", 0))
        fair = lp.p_home_advance if pick == "home" else lp.p_away_advance
        if price >= fair + margin:
            hold = (100 - entry_c) if won else -entry_c
            return {"status": "exited", "sold_min": s["elapsed"], "sold_at": point["target_at"],
                    "sold_c": round(price * 100, 1), "pnl_c": round(price * 100 - entry_c, 1),
                    "vs_hold_c": round(price * 100 - entry_c - hold, 1), "quote": q, "state": s}
    return {"status": "held_no_trigger", "reason": None}
