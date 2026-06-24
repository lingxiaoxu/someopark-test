"""ops/calibrate_fit.py — fit the probability calibration on settled matches.

Prices every finished match with the LIVE model (squad + form blends), fits the
temperature / shrinkage calibrator (model/probability_calibration.py), and writes
calibration.json. Re-run as the OOS sample grows (refresh_all calls it first), so
the calibration tightens with more data. PIT-safe: ratings are pre-tournament.

    python -m prediction_market.ops.calibrate_fit  →  data/output/calibration.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market.config import CONFIG

_FINISHED = ("FT", "AET", "PEN")


def fit(conn=None) -> dict:
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.match_pricing import price_match
    from prediction_market.model.probability_calibration import fit_calibration
    from prediction_market.model.squad_strength import build_strength_live

    conn = conn or store.init_db()
    prior = load_prior()
    sm = build_strength_live(conn, prior)
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    rows = conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals, raw_json FROM fixture "
        "WHERE status_short IN ({}) AND home_goals IS NOT NULL".format(",".join("?" * len(_FINISHED))),
        _FINISHED).fetchall()
    from prediction_market.util.pricing import reg_score
    P, Y = [], []
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            continue
        mp = price_match(sm, hi, ai)
        P.append([mp.p_home, mp.p_draw, mp.p_away])
        # 90' regulation score → the 3-way label the market calibrates against.
        gh, ga = reg_score(r["raw_json"], r["home_goals"], r["away_goals"])
        Y.append(0 if gh > ga else (1 if gh == ga else 2))
    cal = fit_calibration(P, Y)
    cal["ts"] = datetime.now(timezone.utc).isoformat()
    cal["note"] = ("Post-hoc calibration of the live model on settled matches. The raw model "
                   "sorts outcomes but is over-confident; this softens it to the empirically "
                   "optimal level. Applied to live predictions and the trade-grade gate.")
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
