"""ops/milestone_export.py — per-match milestone price tracks for the frontend
PriceTrack / Mark-to-Market view (plan 18 §2.4).

Reads milestone_snapshot (populated live by live_refresh + historically by
backfill_milestones) and emits, for each match, the price (¢) AND probability path
across the 6 milestones (PRE → T15 → T30 → HT → T60 → T75 → FT) for home/draw/away,
plus our model's pre-match pick and its mark-to-market trajectory (entry ¢ → FT ¢).

This is the artifact that answers "did our pre-match read get confirmed by the
market?" — the six-point comparison the desk uses to grade pre-match accuracy.

    python -m prediction_market.ops.milestone_export  →  data/output/milestone_marks.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market.config import CONFIG
from prediction_market.util.pricing import to_cents

_ORDER = {"PRE": 0, "T15": 1, "T30": 2, "HT": 3, "T60": 4, "T75": 5, "FT": 6}
_FINISHED = ("FT", "AET", "PEN")


def _mark(row) -> dict:
    """One milestone row → {milestone, minute, score, poly{¢}, devig{prob}}."""
    def c(side):  # poly per-contract ¢ for a side (ask==bid==price for history)
        return to_cents(row[f"poly_{side}_ask"])
    poly = {s: c(s) for s in ("home", "draw", "away")}
    devig = None
    if row["devig_home"] is not None:
        devig = {s: row[f"devig_{s}"] for s in ("home", "draw", "away")}
    model = None
    if row["p_model_home"] is not None:
        model = {s: row[f"p_model_{s}"] for s in ("home", "draw", "away")}
    return {
        "milestone": row["milestone"],
        "minute": row["elapsed"],
        "score": f'{row["home_goals"] if row["home_goals"] is not None else "?"}-'
                 f'{row["away_goals"] if row["away_goals"] is not None else "?"}',
        "poly_c": poly,
        "kalshi_c": {s: to_cents(row[f"kalshi_{s}_ask"]) for s in ("home", "draw", "away")},
        "devig": devig,
        "model": model,
    }


def build(conn=None) -> dict:
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.match_pricing import is_knockout, price_match_calibrated
    from prediction_market.model.probability_calibration import load_calibration
    from prediction_market.model.squad_strength import build_strength_live

    conn = conn or store.init_db()
    prior = load_prior()
    name = {t.team_id: t.name for t in prior.teams}
    zh = {t.team_id: t.zh for t in prior.teams}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    sm = build_strength_live(conn, prior)
    cal = load_calibration()

    fids = [r["fixture_api_id"] for r in conn.execute(
        "SELECT DISTINCT fixture_api_id FROM milestone_snapshot")]
    matches = []
    for fid in fids:
        fx = conn.execute(
            "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, kickoff_ts, round, status_short, raw_json "
            "FROM fixture WHERE api_id=?", (fid,)).fetchone()
        if not fx:
            continue
        hi, ai = cmap.get(fx["home_api_id"]), cmap.get(fx["away_api_id"])
        if not (hi and ai):
            continue
        rows = conn.execute(
            "SELECT * FROM milestone_snapshot WHERE fixture_api_id=?", (fid,)).fetchall()
        rows = sorted(rows, key=lambda r: _ORDER.get(r["milestone"], 9))
        marks = [_mark(r) for r in rows]

        settled = fx["status_short"] in _FINISHED and fx["home_goals"] is not None
        result = None
        if settled:
            gh, ga = fx["home_goals"], fx["away_goals"]
            result = "home" if gh > ga else ("draw" if gh == ga else "away")

        # Our pick / win — from the SAME shared function the production bet log uses
        # (performance_report.match_pick), so PriceTrack and Accuracy/PnL reconcile by
        # construction (group 3-way + knockout advance). book_row=None: the pick is
        # book-independent, so we get the identical pick without bookmaker odds. Returns
        # None only for a knockout that can't be settled yet → fall back to the plain
        # argmax pick (live / undecided), with no MTM.
        from prediction_market.ops.performance_report import match_pick
        mr = match_pick(sm, cal, hi, ai, fx, None) if settled else None
        if mr is not None:
            pick = mr["pick"]
            entry_prob = mr["model_prob"]
        else:
            mp = price_match_calibrated(sm, hi, ai, knockout=is_knockout(fx["round"]), cal=cal)
            model = {"home": mp.p_home, "draw": mp.p_draw, "away": mp.p_away}
            pick = max(model, key=model.get)
            entry_prob = round(model[pick], 4)
        pre = next((m for m in marks if m["milestone"] == "PRE"), None)
        entry_c = (pre["poly_c"].get(pick) if pre else None)
        our_bet = {
            "side": pick, "entry_prob": entry_prob, "entry_cents": entry_c,
            "pick_team": {"home": name.get(hi, hi), "draw": "Draw", "away": name.get(ai, ai)}[pick],
        }

        mtm = None
        if settled and mr is not None and entry_c is not None:
            won = mr["won"]                       # bet-logic win (aligned with the bet log)
            ft_c = 100.0 if won else 0.0
            pnl_c = round(ft_c - entry_c, 1)
            mtm = {"entry_c": entry_c, "ft_c": ft_c, "pnl_c": pnl_c, "won": won,
                   "path_direction": "converging" if pnl_c > 0 else ("diverging" if pnl_c < 0 else "flat")}

        matches.append({
            "fixture_id": fid,
            "home": {"id": hi, "name": name.get(hi, hi), "zh": zh.get(hi, "")},
            "away": {"id": ai, "name": name.get(ai, ai), "zh": zh.get(ai, "")},
            "kickoff": fx["kickoff_ts"],
            "round": fx["round"] or "",
            "settled": settled,
            "score": (f'{fx["home_goals"]}-{fx["away_goals"]}' if settled else None),
            "result": result,
            "our_bet": our_bet,
            "mtm": mtm,
            "marks": marks,
        })

    matches.sort(key=lambda m: m["kickoff"] or "", reverse=True)
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "n": len(matches),
        "milestones": ["PRE", "T15", "T30", "HT", "T60", "T75", "FT"],
        "note": "Per-contract ¢ (=quote×100) and probability at each match milestone, "
                "from Polymarket (history) + live capture. PRE→FT shows whether the market "
                "confirmed our pre-match pick (mark-to-market).",
        "matches": matches,
    }


def main() -> None:
    doc = build()
    CONFIG.paths.ensure()
    for d in (CONFIG.paths.output, CONFIG.paths.frontend_data):
        (d / "milestone_marks.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"milestone_marks.json: {doc['n']} matches")
    for m in doc["matches"][:5]:
        b = m["our_bet"]
        mtm = m["mtm"]
        tail = (f"  bet {b['side']} entry {b['entry_cents']}¢ → "
                f"{'WON +' if mtm and mtm['won'] else 'lost '}{mtm['pnl_c']}¢" if mtm else "")
        print(f"  {m['home']['name']} vs {m['away']['name']}  ({len(m['marks'])} marks){tail}")


if __name__ == "__main__":
    main()
