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
    # Same confidence input the bet log uses (calibrated Brier vs uniform), so the
    # decision (and stake) here matches performance_report exactly.
    _ub = (cal.get("uniform_brier") if cal else None) or (2.0 / 3.0)
    _cb = cal.get("calibrated_brier") if cal else None
    calib_conf = max(-1.0, min(1.0, (_ub - _cb) / _ub)) if (_cb is not None and _ub) else 0.0

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
            # 90' regulation score (KO ET match settles the 90' Tie market on 1-1@90').
            from prediction_market.util.pricing import reg_score
            gh, ga = reg_score(fx["raw_json"], fx["home_goals"], fx["away_goals"])
            result = "home" if gh > ga else ("draw" if gh == ga else "away")

        # Our pick / win — from the SAME shared function the production bet log uses
        # (performance_report.match_pick), so PriceTrack and Accuracy/PnL reconcile by
        # construction (90-min 3-way for BOTH stages — a draw is a valid knockout outcome;
        # 'advance' is a SEPARATE per-team reach-round product). book_row=None: the pick is
        # book-independent, so we get the identical pick without bookmaker odds. match_pick
        # returns None only when the score isn't final yet → fall back to the plain argmax
        # pick (live / undecided), with no MTM.
        from prediction_market.ops.performance_report import _pit_strength
        from prediction_market.ops.settle_bets import frozen_inplay, frozen_pick
        from prediction_market.strategy.decision_model import quotes_from_milestone_row
        pre_row = next((r for r in rows if r["milestone"] == "PRE"), None)
        quotes = quotes_from_milestone_row(pre_row) if pre_row is not None else None
        # FROZEN decision (PIT strength + PIT calibration, computed once at settle) — read the
        # ledger, never recompute, so PriceTrack never mutates as later matches settle.
        mr = frozen_pick(conn, fx, hi, ai, quotes, None) if settled else None
        bet = bool(mr and mr["bet"])
        if mr is not None:
            # The decision side (value pick) when we bet; else the model argmax for display.
            pick = mr["pick"]
            entry_prob = mr["model_prob"]
        else:
            # Per-match market = 90-min 3-way for BOTH stages → knockout=False (same as the
            # bet log / upcoming card). A draw is a valid knockout outcome here. host_neutral on
            # a KO round drops the host's home-soil edge (neutral venue).
            mp = price_match_calibrated(sm, hi, ai, knockout=False, cal=cal,
                                        host_neutral=is_knockout(fx["round"]))
            model = {"home": mp.p_home, "draw": mp.p_draw, "away": mp.p_away}
            pick = max(model, key=model.get)
            entry_prob = round(model[pick], 4)
        pre = next((m for m in marks if m["milestone"] == "PRE"), None)
        # Entry ¢ for our pick: Poly PRE ask, falling back to Kalshi — the SAME poly-then-kalshi
        # source the bet log uses (performance_report._entry_c). Without the Kalshi fallback a
        # Poly-less side left entry_c=None, which silently skipped the smart-exit here while the
        # accuracy/PnL view (with the fallback) showed it — the two then disagreed (price-track
        # showed hold-to-FT, accuracy showed the cash-out). Now both reconcile.
        entry_c = None
        if pre:
            entry_c = pre["poly_c"].get(pick)
            if entry_c is None:
                entry_c = pre["kalshi_c"].get(pick)
        side_label = {"home": name.get(hi, hi), "draw": "Draw", "away": name.get(ai, ai)}
        # argmax 口径 (most-likely side) — the parallel reference shown under our bet, on
        # EVERY settled match (incl. ones the decision model skipped).
        # argmax / prediction口径 — for knockout this is the 2-way "who advances" pick judged
        # vs who actually advanced (mr["pred_*"]); for the group stage it's the 90' 3-way
        # argmax. Entry ¢: the 90' 3-way contract price for the group pick, OR the 2-way
        # advance price (poly→kalshi) for a knockout pick (from the PRE snapshot's advance
        # columns; None until that price was captured/backfilled).
        from prediction_market.util.pricing import to_cents as _to_cents

        def _adv_entry_c(side):
            if pre_row is None:
                return None
            keys = set(pre_row.keys()) if hasattr(pre_row, "keys") else set()
            for v in ("poly_adv", "kalshi_adv"):
                col = f"{v}_{side}_ask"
                if col in keys and pre_row[col] is not None:
                    return _to_cents(pre_row[col])
            return None

        argmax = None
        if mr is not None:
            am_pick = mr["pred_pick"]
            am_won = mr["pred_won"]
            knockout_adv = mr.get("advance") is not None
            am_entry_c = (_adv_entry_c(am_pick) if knockout_adv
                          else ((pre["poly_c"].get(am_pick) or pre["kalshi_c"].get(am_pick)) if pre else None))
            argmax = {
                "side": am_pick, "pick_team": side_label[am_pick], "entry_cents": am_entry_c,
                "won": am_won, "advance": knockout_adv,
                "mtm": ({"entry_c": am_entry_c, "ft_c": 100.0 if am_won else 0.0,
                         "pnl_c": round((100.0 if am_won else 0.0) - am_entry_c, 1), "won": am_won}
                        if (settled and am_entry_c is not None and am_won is not None) else None),
            }
        our_bet = {
            "side": pick, "entry_prob": entry_prob, "entry_cents": entry_c,
            "pick_team": side_label[pick],
            "bet": bet,                                  # False ⇒ decision model found no edge
            "stake_usd": (mr["stake_usd"] if mr else None),
            "model_pick": (mr["model_pick"] if mr else pick),
            "argmax": argmax,                            # parallel argmax track (side + mtm)
        }

        mtm = None
        if settled and bet and entry_c is not None:
            won = mr["won"]                       # bet-logic win (aligned with the bet log)
            ft_c = 100.0 if won else 0.0
            pnl_c = round(ft_c - entry_c, 1)
            mtm = {"entry_c": entry_c, "ft_c": ft_c, "pnl_c": pnl_c, "won": won,
                   "path_direction": "converging" if pnl_c > 0 else ("diverging" if pnl_c < 0 else "flat")}

        # Model-aware cash-out (the validated smart-exit): what selling the over-reaction
        # would have returned vs holding to FT — surfaced so the desk sees the timing.
        smart_exit = None
        if settled and bet and entry_c is not None:
            try:
                from prediction_market.strategy.smart_exit import smart_exit_cashout
                sm_pit = _pit_strength(conn, fx["kickoff_ts"]) if fx["kickoff_ts"] else sm
                smart_exit = smart_exit_cashout(conn, sm_pit, fid, pick, entry_c, hi, ai,
                                                fx["round"], mr["won"])
            except Exception:
                smart_exit = None

        # FROZEN causal in-play entry (relative-value, PIT) — the SECOND P&L stream, surfaced on
        # the SAME price trajectory so PriceTrack reconciles with the accuracy/PnL views
        # (三视图统一). Its `milestone` marks WHERE on the PRE→FT path we entered, `exit` is its
        # own 盘中离场 smart-exit. Per-contract ¢ (the price-track unit); the report $-sizes it.
        ip = frozen_inplay(conn, fid) if settled else None
        inplay = ({**ip, "pick_team": side_label[ip["side"]]} if ip else None)

        matches.append({
            "fixture_id": fid,
            "home": {"id": hi, "name": name.get(hi, hi), "zh": zh.get(hi, "")},
            "away": {"id": ai, "name": name.get(ai, ai), "zh": zh.get(ai, "")},
            "kickoff": fx["kickoff_ts"],
            "round": fx["round"] or "",
            "settled": settled,
            # 90' regulation score (matches `result`/MTM); show the AET final when it differs.
            "score": ((f"{gh}-{ga}" + (f' (AET {fx["home_goals"]}-{fx["away_goals"]})'
                                       if (gh, ga) != (fx["home_goals"], fx["away_goals"]) else ""))
                      if settled else None),
            "result": result,
            "our_bet": our_bet,
            "mtm": mtm,
            "smart_exit": smart_exit,
            "inplay": inplay,
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
