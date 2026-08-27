"""ops/calibrate_fit.py — fit the probability calibration on settled matches.

Prices every finished match with the LIVE model (squad + form blends), fits the
temperature / shrinkage calibrator (model/probability_calibration.py), and writes
calibration.json. Re-run as the OOS sample grows (refresh_all calls it first), so
the calibration tightens with more data. PIT-safe: ratings are pre-tournament.

    python -m prediction_market_soccer.ops.calibrate_fit  →  data/output/calibration.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG

_FINISHED = ("FT", "AET", "PEN")


def fit(conn=None, *, since_days: float = 60.0) -> dict:
    """Club edition: POOLED calibration across every enabled comp's recently-settled
    fixtures, each priced with ITS OWN league model (cached_strength — per-league
    mu/home_adv). Per-league calibration replaces the pool at n≥30 (§3.5, Phase 6);
    the pooled fit is the honest cold-start."""
    from prediction_market_soccer.config.leagues import neutral_venue_for, active, stage_of, Stage
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.model.match_pricing import price_match
    from prediction_market_soccer.model.probability_calibration import fit_calibration_per_league
    from prediction_market_soccer.model.strength_cache import cached_strength

    conn = conn or store.init_db()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    from prediction_market_soccer.util.pricing import reg_score
    records = []
    for comp in active():
        try:
            sm = cached_strength(conn, comp.key)
        except Exception as e:  # noqa: BLE001
            print(f"[calibrate:{comp.key}] model unavailable ({e}) — skipped")
            continue
        rows = conn.execute(
            "SELECT api_id, round, home_api_id, away_api_id, home_goals, away_goals, raw_json "
            "FROM fixture "
            "WHERE league_id=? AND season=? AND status_short IN ({}) AND home_goals IS NOT NULL "
            "AND kickoff_ts >= datetime('now', ?)".format(",".join("?" * len(_FINISHED))),
            (comp.api_football_id, comp.season, *_FINISHED, f"-{since_days} days")).fetchall()
        for r in rows:
            hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
            if not (hi in sm.ratings and ai in sm.ratings):
                continue
            # The traded per-match contract settles on the 90-MINUTE 3-way in both
            # stages, so the calibrator must be fitted on the same pricing the bets
            # use: knockout=False with host_neutral on a KO round. Fitting on the
            # knockout-scaled λ calibrated a model nothing actually trades.
            ko = stage_of(comp.key, r["round"]) in (Stage.CUP_TWO_LEG, Stage.CUP_SINGLE)
            mp = price_match(sm, hi, ai, knockout=False, host_neutral=neutral_venue_for(comp.key, r["round"], conn, r["api_id"]))
            gh, ga = reg_score(r["raw_json"], r["home_goals"], r["away_goals"])
            records.append({
                "league": comp.key,
                "P": [mp.p_home, mp.p_draw, mp.p_away],
                "Y": 0 if gh > ga else (1 if gh == ga else 2)})
    cal = fit_calibration_per_league(records)
    cal["ts"] = datetime.now(timezone.utc).isoformat()
    cal["note_key"] = "notes.calibration"
    cal["note"] = ("Post-hoc calibration of the live model on settled matches, fitted per "
                   "competition as well as pooled. A competition prices with its own "
                   "calibrator once it has 30 settled matches; until then it uses the pooled "
                   "fit and its trading gate stays shut (§3.5).")
    return cal


def main() -> None:
    cal = fit()
    CONFIG.paths.ensure()
    (CONFIG.paths.output / "calibration.json").write_text(
        json.dumps(cal, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"calibration: method={cal['method']} param={cal['param']}  "
          f"raw Brier={cal['raw_brier']} → calibrated {cal['calibrated_brier']}  "
          f"(uniform {cal['uniform_brier']})  trade_grade={cal['trade_grade']}  n={cal['n']}")


if __name__ == "__main__":
    main()
