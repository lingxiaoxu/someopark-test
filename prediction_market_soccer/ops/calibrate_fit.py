"""ops/calibrate_fit.py — fit the probability calibration on settled matches.

Prices every finished match with the model FROZEN BEFORE THAT MATCH'S WEEK
(model/pit_strength), fits the temperature / shrinkage calibrator
(model/probability_calibration.py), and writes calibration.json. Re-run as the OOS
sample grows (refresh_all calls it first), so the calibration tightens with more data.

PIT discipline: the World Cup could say "ratings are pre-tournament" because the model
was frozen before a one-month event. A club season runs continuously and the form /
xG-form / alt-data blends read results as they land, so PIT here means a walk-forward
refit, not a single freeze.

    python -m prediction_market_soccer.ops.calibrate_fit  →  data/output/calibration.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG

_FINISHED = ("FT", "AET", "PEN")


# How many of a competition's most recent settled matches the calibrator is fitted on.
# Sample size, not the calendar, is what a fit needs. The previous window was 60 DAYS,
# which made the verdict depend on where the season happens to sit: Libertadores had 141
# settled matches this season but only 16 inside the window (its group stage ran in the
# spring) and read as cold-start, while Sudamericana — the same competition, the same
# format, the same stage — had 32 in the window and traded. Nothing about either model
# differed; only the calendar did.
MAX_FIT_MATCHES = 120


def fit(conn=None, *, since_days: float | None = None,
        max_matches: int = MAX_FIT_MATCHES) -> dict:
    """Club edition: POOLED calibration across every enabled comp's recently-settled
    fixtures, each priced with ITS OWN league model (cached_strength — per-league
    mu/home_adv). Per-league calibration replaces the pool at n≥30 (§3.5, Phase 6);
    the pooled fit is the honest cold-start."""
    from prediction_market_soccer.config.leagues import neutral_venue_for, active, stage_of, Stage
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.model.match_pricing import price_match
    from prediction_market_soccer.model.pit_strength import WalkForwardStrength
    from prediction_market_soccer.model.probability_calibration import fit_calibration_per_league

    conn = conn or store.init_db()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    from prediction_market_soccer.util.pricing import reg_score
    records = []
    # Each settled match is priced by the model frozen BEFORE its week — see
    # model/pit_strength. cached_strength is the live path (as_of=None); calling it
    # here let the form blends read the very results being calibrated on.
    wf = WalkForwardStrength(conn)
    for comp in active():
        # Most recent `max_matches` of THIS competition's settled fixtures. `since_days`
        # is honoured when a caller passes it explicitly (the research paths do), but the
        # production fit is bounded by count so a gate cannot flip on the calendar alone.
        _q = ("SELECT api_id, round, home_api_id, away_api_id, home_goals, away_goals, raw_json, kickoff_ts "
              "FROM fixture "
              "WHERE league_id=? AND season=? AND status_short IN ({}) AND home_goals IS NOT NULL"
              .format(",".join("?" * len(_FINISHED))))
        _a = [comp.api_football_id, comp.season, *_FINISHED]
        if since_days is not None:
            _q += " AND kickoff_ts >= datetime('now', ?)"
            _a.append(f"-{since_days} days")
        _q += " ORDER BY kickoff_ts DESC LIMIT ?"
        _a.append(int(max_matches))
        rows = conn.execute(_q, _a).fetchall()
        for r in rows:
            sm = wf.for_match(comp.key, r["kickoff_ts"])
            if sm is None:
                break
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
