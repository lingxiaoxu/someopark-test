"""ops/schedule_export.py — full group-stage schedule for the frontend Schedule view.

Lists ALL 72 group-stage fixtures (the schedule is fixed), played + upcoming, sorted
by kickoff: ET time, round (Group Stage 1/2/3), both teams, and status/score for the
ones already played. Knockout fixtures are added automatically once API-Football
populates them (they don't exist as fixtures yet). → schedule.json (output + frontend).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from prediction_market.config import CONFIG

ET = ZoneInfo("America/New_York")
_FINISHED = ("FT", "AET", "PEN")


def _et(ts: str | None) -> str | None:
    try:
        return datetime.fromisoformat(ts).astimezone(ET).strftime("%m-%d %H:%M ET")
    except Exception:
        return None


def build(conn=None, *, group_only: bool = False) -> dict:
    # group_only=False (default): include the knockout fixtures too, appended after the
    # group stage by kickoff — the Schedule view shows the full tournament once the
    # knockout bracket is drawn (group stage still always present).
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior

    conn = conn or store.init_db()
    prior = load_prior()
    name = {t.team_id: t.name for t in prior.teams}
    zh = {t.team_id: t.zh for t in prior.teams}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    where = "WHERE round LIKE 'Group%'" if group_only else ""
    rows = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, kickoff_ts, round, status_short, home_goals, away_goals "
        f"FROM fixture {where} ORDER BY kickoff_ts").fetchall()
    out = []
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        finished = r["status_short"] in _FINISHED and r["home_goals"] is not None
        gh, ga = r["home_goals"], r["away_goals"]
        # Regulation-time scorers + (when it went to a shootout) the shootout score — consumed by
        # the bracket view's hover card. Shootout kicks live in fixture_event with
        # comments='Penalty Shootout' (same convention inplay_export uses) and are excluded from
        # the scorer list; own goals / in-match penalties are annotated (OG)/(P).
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
        })
    return {"as_of": datetime.now(timezone.utc).isoformat(), "n": len(out), "matches": out}


def main() -> None:
    doc = build()
    CONFIG.paths.ensure()
    for d in (CONFIG.paths.output, CONFIG.paths.frontend_data):
        (d / "schedule.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    played = sum(1 for m in doc["matches"] if m["finished"])
    ko = sum(1 for m in doc["matches"] if "group" not in (m["round"] or "").lower())
    print(f"schedule.json: {doc['n']} matches ({played} played, {doc['n'] - played} upcoming; {ko} knockout)")


if __name__ == "__main__":
    main()
