"""Sanitized availability metadata shared by soccer snapshots."""
from __future__ import annotations

import json


def value(row, key, default=None):
    return row[key] if key in row.keys() else default


def source_as_of(rows):
    stamps = [value(row, "source_as_of") or value(row, "updated_at") for row in rows]
    return max((str(s) for s in stamps if s), default=None)


def data_status(rows, issues=()):
    found = list(issues)
    for row in rows:
        if row.get("pricing_state") == "unavailable":
            found.append({"code": row.get("unavailable_reason", "pricing_failed"),
                          "fixture_id": row.get("fixture_id"), "league": row.get("league")})
        for venue, state in (row.get("quote_status") or {}).items():
            if state == "unavailable":
                found.append({"code": "quote_unavailable", "fixture_id": row.get("fixture_id"),
                              "league": row.get("league"), "venue": venue})
        advance = row.get("advance") or {}
        if advance.get("pricing_state") == "unavailable":
            found.append({"code": advance.get("unavailable_reason", "pricing_failed"),
                          "fixture_id": row.get("fixture_id"), "market": "advance"})
        for venue, state in (advance.get("quote_status") or {}).items():
            if state == "unavailable":
                found.append({"code": "quote_unavailable", "fixture_id": row.get("fixture_id"),
                              "league": row.get("league"), "venue": venue, "market": "advance"})
    return {"state": "degraded" if found else "ok", "issues": found}


def fixture_unavailable(fx, comp, cmap, names, zh, reason, *, with_venues=False):
    """Keep a known fixture even when mapping/strength/pricing is unavailable."""
    from prediction_market_soccer.config.leagues import caps_dict, caps_for, stage_of
    try:
        raw_teams = json.loads(value(fx, "raw_json") or "{}").get("teams", {})
    except (ValueError, TypeError):
        raw_teams = {}
    def team(side):
        api_id = value(fx, f"{side}_api_id")
        club_id = cmap.get(api_id)
        raw = raw_teams.get(side) or {}
        return {"id": club_id, "api_id": api_id,
                "name": names.get(club_id) or raw.get("name") or club_id or f"Team {api_id}",
                "zh": zh.get(club_id, "")}
    round_name = value(fx, "round") or ""
    return {"fixture_id": value(fx, "api_id"), "league": comp.key if comp else None,
            "league_zh": comp.zh if comp else "", "round": round_name,
            "kickoff": value(fx, "kickoff_ts"), "status": value(fx, "status_short", ""),
            "source_as_of": value(fx, "updated_at"),
            "home": team("home"), "away": team("away"),
            "home_goals": value(fx, "home_goals"), "away_goals": value(fx, "away_goals"),
            "elapsed": value(fx, "elapsed"),
            "caps": caps_dict(caps_for(comp.key, round_name), stage_of(comp.key, round_name)) if comp else {},
            "model": None, "pricing_state": "unavailable", "unavailable_reason": reason,
            "quote_status": {v: "not_requested" for v in ("kalshi", "poly_us")},
            "book_devig": None, "kalshi": None, "poly_us": None, "decision": None,
            "edge": {"vs_book": None, "vs_kalshi": None, "vs_poly_us": None, "best": None},
            "lock_arb": None, "advance": None, "tentative": False}
