"""ops/schedule_export.py — rolling schedule for the frontend Schedule view (club edition).

The WC exported all 104 fixtures of one tournament; twelve club competitions
carry ~3,300 fixtures a season, so this exports a ROLLING WINDOW (past 7 days +
next 30) per competition, each row tagged with its ``league`` (frontend groups
by league chips, §3.7). Scorer/shootout enrichment for finished rows is kept
verbatim from the WC version. → schedule.json (output + frontend).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import active

ET = ZoneInfo("America/New_York")
_FINISHED = ("FT", "AET", "PEN")


def _et(ts: str | None) -> str | None:
    try:
        return datetime.fromisoformat(ts).astimezone(ET).strftime("%m-%d %H:%M ET")
    except Exception:
        return None


def build(conn=None, *, days_back: float = 7.0, days_fwd: float = 30.0) -> dict:
    from prediction_market_soccer.ingest import store

    conn = conn or store.init_db()
    name, zh = {}, {}
    for r in conn.execute("SELECT DISTINCT club_id, name, zh FROM club_registry"):
        name[r["club_id"]] = r["name"]
        zh[r["club_id"]] = r["zh"] or ""
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    out = []
    for comp in active():
        rows = conn.execute(
            "SELECT api_id, home_api_id, away_api_id, kickoff_ts, round, status_short, "
            "home_goals, away_goals, venue_name, venue_city "
            "FROM fixture WHERE league_id=? AND season=? "
            "AND kickoff_ts >= datetime('now', ?) AND kickoff_ts <= datetime('now', ?) "
            "ORDER BY kickoff_ts",
            (comp.api_football_id, comp.season, f"-{days_back} days", f"+{days_fwd} days")).fetchall()
        for r in rows:
            hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
            finished = r["status_short"] in _FINISHED and r["home_goals"] is not None
            gh, ga = r["home_goals"], r["away_goals"]
            scorers, shootout = None, None
            if finished:
                scorers = {"home": [], "away": []}
                pens = {"home": 0, "away": 0}
                for e in conn.execute(
                    "SELECT team_api_id, minute, extra, detail, comments, raw_json FROM fixture_event "
                    "WHERE fixture_api_id=? AND type='Goal' ORDER BY seq", (r["api_id"],)):
                    side = "home" if e["team_api_id"] == r["home_api_id"] else "away"
                    if (e["comments"] or "") == "Penalty Shootout":
                        if (e["detail"] or "") == "Penalty":
                            pens[side] += 1
                        continue
                    if (e["detail"] or "") == "Missed Penalty":
                        continue
                    try:
                        nm = (json.loads(e["raw_json"]).get("player") or {}).get("name")
                    except Exception:
                        nm = None
                    label = nm or "?"
                    if e["detail"] == "Own Goal":
                        label += " (OG)"
                    elif e["detail"] == "Penalty":
                        label += " (P)"
                    scorers[side].append({"name": label, "min": (e["minute"] or 0) + (e["extra"] or 0)})
                if r["status_short"] == "PEN":
                    shootout = f"{pens['home']}-{pens['away']}"
            out.append({
                "league": comp.key, "league_zh": comp.zh,
                "kickoff": r["kickoff_ts"], "et": _et(r["kickoff_ts"]), "round": r["round"] or "",
                "home": {"id": hi, "name": name.get(hi, hi) if hi else "TBD", "zh": zh.get(hi, "") if hi else ""},
                "away": {"id": ai, "name": name.get(ai, ai) if ai else "TBD", "zh": zh.get(ai, "") if ai else ""},
                "status": r["status_short"],
                "finished": finished,
                "score": (f"{gh}-{ga}" if finished else None),
                "result": ("home" if finished and gh > ga else
                           ("draw" if finished and gh == ga else ("away" if finished else None))),
                "scorers": scorers,
                "shootout": shootout,
                "venue": ({"name": r["venue_name"], "city": r["venue_city"]}
                          if (r["venue_name"] or r["venue_city"]) else None),
            })
    out.sort(key=lambda m: m["kickoff"] or "")
    return {"as_of": datetime.now(timezone.utc).isoformat(), "n": len(out),
            "window": {"days_back": days_back, "days_fwd": days_fwd}, "matches": out}


def main() -> None:
    doc = build()
    CONFIG.paths.ensure()
    for d in (CONFIG.paths.output, CONFIG.paths.frontend_data):
        (d / "schedule.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    played = sum(1 for m in doc["matches"] if m["finished"])
    print(f"schedule.json: {doc['n']} matches in window ({played} played)")


if __name__ == "__main__":
    main()
