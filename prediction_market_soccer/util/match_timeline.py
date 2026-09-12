"""Pure football clock/event reconstruction; approximate time stays approximate.

No database reads here. Callers choose a complete, available event-set revision.
An event's minute/extra gives an interval, never a fabricated second timestamp.
"""
from __future__ import annotations

import math

from prediction_market_soccer.util.research_inputs import epoch

CLOCK_VERSION = "match-timeline-v1"
APPROXIMATE_HALFTIME_MIN = 15
_PERIOD_ORDER = {"PRE": 0, "1H": 1, "HT": 2, "2H": 3, "ET1": 4, "ETHT": 5, "ET2": 6, "PEN": 7, "FT": 8}


def approximate_clock(kickoff, target_at):
    """Explicit legacy +15 research clock, never evidence of actual phase time."""
    wall = (epoch(target_at) - epoch(kickoff)) / 60
    if wall < 0:
        period, elapsed = "PRE", 0
    elif wall <= 45:
        period, elapsed = "1H", wall
    elif wall <= 60:
        period, elapsed = "HT", 45
    elif wall <= 105:
        period, elapsed = "2H", wall - 15
    else:
        period, elapsed = "unknown", None
    return {"period": period, "elapsed": elapsed, "extra": None, "target_at": epoch(target_at),
            "clock_basis": "nominal_kickoff_plus_15", "certainty": "approximate",
            "reason": "actual_phase_boundaries_unobserved", "clock_version": CLOCK_VERSION}


def milestone_target(kickoff, milestone):
    """Legacy target mapping remains explicit and is not a true-whistle claim."""
    wall = {"PRE": -5, "T15": 15, "T30": 30, "HT": 47, "T60": 75, "T75": 90}.get(milestone)
    if wall is None:
        raise ValueError("FT is a result, not a scheduled trading quote")
    return approximate_clock(kickoff, epoch(kickoff) + wall * 60)


def _period(minute, extra, explicit=None):
    if explicit in _PERIOD_ORDER:
        return explicit
    if minute <= 45:
        return "1H"
    if minute <= 90:
        return "2H"
    if minute <= 105:
        return "ET1"
    if minute <= 120:
        return "ET2"
    return "unknown"


def normalize_events(events, home_id, away_id):
    out = []
    for ordinal, raw in enumerate(events):
        e = dict(raw)
        typ, detail = e.get("type"), (e.get("detail") or "").casefold()
        goal = typ == "Goal" and detail != "missed penalty"
        red = typ == "Card" and detail in {"red card", "second yellow card", "second yellow"}
        if not (goal or red):
            continue
        team = e.get("team_api_id", (e.get("team") or {}).get("id"))
        if team not in (home_id, away_id) or home_id == away_id:
            raise ValueError("event_team_identity_unresolved")
        minute = e.get("minute", (e.get("time") or {}).get("elapsed"))
        extra = e.get("extra", (e.get("time") or {}).get("extra")) or 0
        if isinstance(minute, bool) or isinstance(extra, bool) or not isinstance(minute, (int, float)) or not isinstance(extra, (int, float)) or not math.isfinite(minute) or not math.isfinite(extra) or minute < 0 or extra < 0:
            raise ValueError("invalid_event_clock")
        period = _period(minute, extra, e.get("period"))
        side = "home" if team == home_id else "away"
        if goal and detail == "own goal":
            side = "away" if side == "home" else "home"
        out.append({"ordinal": ordinal, "minute": minute, "extra": extra, "period": period,
                    "kind": "goal" if goal else "red", "side": side,
                    "event_at": e.get("event_at"), "precision": "second" if e.get("event_at") else "minute_interval"})
    return sorted(out, key=lambda e: (_PERIOD_ORDER.get(e["period"], 99), e["minute"] + e["extra"], e["ordinal"]))


def score_at_clock(events, home_id, away_id, *, elapsed, extra=0, period=None):
    """Reference score at an explicit phase/minute. No availability/PIT claim."""
    period = period or _period(elapsed, extra)
    state = {"home_goals": 0, "away_goals": 0, "reds_home": 0, "reds_away": 0}
    for event in normalize_events(events, home_id, away_id):
        ep, tp = _PERIOD_ORDER.get(event["period"], 99), _PERIOD_ORDER.get(period, 99)
        if ep > tp or (ep == tp and event["minute"] + event["extra"] > elapsed + extra):
            continue
        key = f"{event['side']}_goals" if event["kind"] == "goal" else f"reds_{event['side']}"
        state[key] += 1
    return {**state, "elapsed": elapsed, "extra": extra, "period": period,
            "certainty": "approximate", "clock_basis": "event_minute_only", "reason": "no_event_available_time"}


def timeline_state(revision, *, home_id, away_id, kickoff, target_at, cutoff, phase_observations=()):
    """Select state using only a complete event revision available by cutoff.

    Phase observations contain period, started_at and available_at, optionally
    revision_id. Missing phase boundaries retain approximate clock. An event
    interval overlapping target makes state unavailable, even with a causal price.
    """
    at, cut = epoch(target_at), epoch(cutoff)
    base = {**approximate_clock(kickoff, at), "home_goals": None, "away_goals": None,
            "reds_home": None, "reds_away": None, "source_revision_ids": [],
            "known_at": None, "wall_time_interval": None, "status": "unavailable"}
    if at > cut:
        return {**base, "reason": "target_after_cutoff"}
    if not revision or not revision.get("complete") or revision.get("available_at") is None:
        return {**base, "reason": "complete_event_revision_unavailable"}
    if epoch(revision["available_at"]) > cut:
        return {**base, "reason": "event_revision_after_cutoff"}
    available = [p for p in phase_observations if p.get("available_at") is not None and epoch(p["available_at"]) <= cut and epoch(p["started_at"]) <= at]
    starts = {p["period"]: epoch(p["started_at"]) for p in available}
    clock = approximate_clock(kickoff, at)
    current = max(available, key=lambda p: epoch(p["started_at"])) if available else None
    if current:
        period = current["period"]
        offset = {"1H": 0, "HT": 45, "2H": 45, "ET1": 90, "ETHT": 105, "ET2": 105}.get(period)
        if offset is not None:
            elapsed = offset if period in {"HT", "ETHT"} else offset + (at - epoch(current["started_at"])) / 60
            clock = {**clock, "period": period, "elapsed": elapsed, "extra": max(0, elapsed - {"1H":45,"2H":90,"ET1":105,"ET2":120}.get(period,elapsed)),
                     "clock_basis": "observed_phase_start", "certainty": "verified", "reason": None}
    base.update(clock)
    base["known_at"] = max([revision["available_at"]] + [p["available_at"] for p in available], key=epoch)
    base["source_revision_ids"] = [revision.get("revision_id")] + [p.get("revision_id") for p in available if p.get("revision_id")]
    try:
        events = normalize_events(revision["events"], home_id, away_id)
    except (TypeError, ValueError) as exc:
        return {**base, "status": "invalid", "reason": str(exc)}
    state = {"home_goals": 0, "away_goals": 0, "reds_home": 0, "reds_away": 0}
    intervals = []
    for e in events:
        if e["event_at"]:
            low = high = epoch(e["event_at"])
        else:
            start = starts.get(e["period"])
            offset = {"1H": 0, "2H": 45, "ET1": 90, "ET2": 105}.get(e["period"])
            if start is None or offset is None:
                return {**base, "reason": "event_phase_boundary_unknown"}
            # Provider minute precision differs; retain a conservative interval.
            minute = e["minute"] + e["extra"] - offset
            low, high = start + max(0, minute - 1) * 60, start + (minute + 1) * 60
        intervals.append({"ordinal": e["ordinal"], "start": low, "end": high})
        if low <= at < high:
            return {**base, "reason": "event_quote_time_overlap", "wall_time_interval": [low, high]}
        if high <= at:
            key = f"{e['side']}_goals" if e["kind"] == "goal" else f"reds_{e['side']}"
            state[key] += 1
    if base["certainty"] != "verified":
        return {**base, "reason": "actual_phase_boundaries_unobserved"}
    return {**base, **state, "status": "ok", "event_intervals": intervals}


def quote_state_consistent(quote, state, target_at):
    """Reject an old price paired with a later event/state transition."""
    sample=quote.get('sample_ts',quote.get('provider_sample_at'))
    if sample is None:
        return False
    sample,target=epoch(sample),epoch(target_at)
    if state.get('state_changed_at') is not None and sample<epoch(state['state_changed_at'])<=target:
        return False
    for event in state.get('event_intervals',[]):
        low,high=epoch(event['start']),epoch(event['end'])
        if low<=target and sample<high:
            return False
    return True
