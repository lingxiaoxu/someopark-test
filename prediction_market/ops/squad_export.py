"""ops/squad_export.py — squad-strength view for the frontend.

Ranks all 48 teams by the squad-strength index (model/squad_strength.py:
minutes-weighted club rating + attacking output) and lists each team's top
players by goals+assists. Read-only, point-in-time (club-2025 data).

Honest note baked into the payload: the squad index is INFORMATIVE (a quality
ranking) but NOT a validated predictive edge — blending it into the model raised
the OOS Brier on the settled matches (the early tournament is draw/upset-heavy and
the API club-rating scale is compressed), so it is NOT wired into the live model;
it's shown for context. Re-validate via param_sweep/backtest as more data accrues.

    python -m prediction_market.ops.squad_export  →  data/output/squad.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market.config import CONFIG


def _top_players(conn, team_api_ids: dict, top_n: int = 4) -> dict:
    """{canonical_team_id: [{name, goals, assists, rating}]} top by goals+assists."""
    rows = conn.execute(
        "SELECT tm.canonical_team_id cid, p.name, ps.goals, ps.assists, ps.rating "
        "FROM squad s JOIN team_meta tm ON tm.api_id = s.team_api_id "
        "JOIN player p ON p.api_id = s.player_api_id "
        "JOIN player_stat ps ON ps.player_api_id = s.player_api_id "
        "WHERE tm.canonical_team_id IS NOT NULL").fetchall()
    by_team: dict[str, list] = {}
    for r in rows:
        by_team.setdefault(r["cid"], []).append({
            "name": r["name"], "goals": r["goals"] or 0, "assists": r["assists"] or 0,
            "rating": round(r["rating"], 2) if r["rating"] is not None else None,
        })
    for cid, lst in by_team.items():
        lst.sort(key=lambda x: -(x["goals"] + x["assists"]))
        by_team[cid] = lst[:top_n]
    return by_team


def build(conn=None) -> dict:
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.squad_strength import squad_index

    conn = conn or store.init_db()
    prior = load_prior()
    name = {t.team_id: t.name for t in prior.teams}
    zh = {t.team_id: t.zh for t in prior.teams}
    idx = squad_index(conn)
    tops = _top_players(conn, {})
    ranked = sorted(idx.values(), key=lambda s: -s.score_z)
    teams = []
    for i, s in enumerate(ranked, 1):
        teams.append({
            "rank": i, "team_id": s.team_id, "name": name.get(s.team_id, s.team_id), "zh": zh.get(s.team_id, ""),
            "score_z": s.score_z, "mw_rating": s.mw_rating, "ga_per90": s.ga_per90, "n_players": s.n_players,
            "top_players": tops.get(s.team_id, []),
        })
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "n_teams": len(teams),
        "teams": teams,
        "note": ("Squad quality (minutes-weighted club rating + goals/assists per 90). "
                 "Informative ranking only — blending this into the model RAISED the OOS Brier "
                 "on settled matches, so it is NOT used by the live predictions."),
    }


def main() -> None:
    doc = build()
    CONFIG.paths.ensure()
    (CONFIG.paths.output / "squad.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"squad.json: {doc['n_teams']} teams")
    for t in doc["teams"][:10]:
        tp = ", ".join(f"{p['name']}({p['goals']}g)" for p in t["top_players"][:2])
        print(f"  #{t['rank']:<2} {t['name']:<14} z={t['score_z']:+.2f}  rating={t['mw_rating']:.2f}  ga/90={t['ga_per90']:.2f}  | {tp}")


if __name__ == "__main__":
    main()
