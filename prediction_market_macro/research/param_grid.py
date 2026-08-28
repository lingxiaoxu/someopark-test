"""research/param_grid.py — walk-forward parameter grid for claims (PLAN §9.4/M4).

24 parameter sets (3 level-weight schemes × 2 seasonal windows × 2 vol windows ×
2 sigma floors) replayed over the FULL settled Kalshi history (deep backfill), scored
per release on the settled legs vs the stored market candles at asof = close − 1h.

Discipline (§9.2/§9.5, hard rules):
  * selection is WALK-FORWARD only: at release t the "selected" set is the best
    performer on releases < t (trailing window) — its score at t is genuine OOS.
    The full-period per-set table is for structural learning, NEVER for picking a
    set by its own full-period score (that's lookahead).
  * the production default stays claims/0.1.0 until a candidate BEATS both the
    default and the market on the walk-forward path AND the §9.5 gate passes;
    switching = version bump + models-table registration, not a config flip.

    python -m prediction_market_macro.research.param_grid [--min-trail 12]
"""
from __future__ import annotations

import argparse
import itertools
import json
from datetime import datetime, timedelta, timezone

import numpy as np

from prediction_market_macro.model import claims as claims_model
from prediction_market_macro.model.common import grid_pmf, leg_fair
from prediction_market_macro.research.backtest import _market_leg_prob, _store_experiment

SERIES = "KXJOBLESSCLAIMS"

_WEIGHTS = [(0.1, 0.2, 0.3, 0.4),      # default: gentle decay
            (0.25, 0.25, 0.25, 0.25),  # flat 4-week mean
            (0.0, 0.0, 0.3, 0.7)]      # aggressive recency
_SEASONAL_YEARS = [6, 10]
_VOL_WINDOWS = [13, 26]
_SIGMA_FLOORS = [0.015, 0.02]

GRID: list[dict] = [
    {"level_weights": w, "seasonal_years": sy, "vol_window": vw, "sigma_floor": sf}
    for w, sy, vw, sf in itertools.product(_WEIGHTS, _SEASONAL_YEARS,
                                           _VOL_WINDOWS, _SIGMA_FLOORS)]
DEFAULT_IX = 0                      # GRID[0] == DEFAULT_PARAMS values


def _set_key(p: dict) -> str:
    return f"w{_WEIGHTS.index(tuple(p['level_weights']))}" \
           f"s{p['seasonal_years']}v{p['vol_window']}f{p['sigma_floor']}"


def _release_universe(conn, cov: dict | None = None) -> list[dict]:
    """Settled claims events with legs + market probs at close−1h, oldest first.

    Legs with no usable market bar at `asof` are dropped, and that is deliberate: the
    market baseline defines the scored universe so no parameter set can win by being
    scored on legs another set never saw. What was NOT deliberate is that the drop used
    to be silent. Across the fourteen macro series 80.4% of settled legs have no
    two-sided quote at close−1h (PLAN_DFM_SYNTH.md §5e), so an unreported count lets a
    Brier computed on a fifth of the book read as if it covered the book. `cov`, if
    given, is filled with `legs_settled` / `legs_scored` and surfaces in the report.
    """
    evs = conn.execute(
        "SELECT s.period, MAX(c.close_time) ct FROM settlements s"
        " JOIN contracts c ON c.ticker=s.ticker WHERE s.series=?"
        " AND s.result IN ('yes','no') GROUP BY s.period ORDER BY ct", (SERIES,)).fetchall()
    from prediction_market_macro.util.periods import kalshi_period_to_key
    if cov is not None:
        # seeded, not accumulated into existence: a coverage dict that omits
        # `legs_scored` when nothing scored is the same silent zero this is here to end.
        cov.setdefault("legs_settled", 0)
        cov.setdefault("legs_scored", 0)
    out = []
    for ev in evs:
        key = kalshi_period_to_key(ev["period"])
        if not key or not ev["ct"]:
            continue
        close_ts = datetime.fromisoformat(ev["ct"].replace("Z", "+00:00"))
        asof = close_ts - timedelta(hours=1)
        legs = []
        for l in conn.execute(
                "SELECT c.ticker, c.floor_strike, c.strike_type, s.result FROM contracts c"
                " JOIN settlements s ON s.ticker=c.ticker WHERE c.series=? AND s.period=?"
                " AND s.result IN ('yes','no')", (SERIES, ev["period"])):
            if cov is not None:
                cov["legs_settled"] += 1
            if l["floor_strike"] is None:
                continue
            mp = _market_leg_prob(conn, l["ticker"], asof)
            if mp is None:
                continue
            if cov is not None:
                cov["legs_scored"] += 1
            legs.append({"strike": float(l["floor_strike"]),
                         "strike_type": l["strike_type"] or "greater_or_equal",
                         "result": l["result"], "market_p": mp})
        if legs:
            out.append({"period": key, "asof": asof, "legs": legs})
    return out


def _brier(pmf: dict, legs: list[dict]) -> tuple[float, float]:
    bm = bk = 0.0
    for l in legs:
        fair = leg_fair(pmf, l["strike_type"], l["strike"], None)
        out = 1.0 if l["result"] == "yes" else 0.0
        bm += (fair - out) ** 2
        bk += (l["market_p"] - out) ** 2
    return bm / len(legs), bk / len(legs)


def run_grid(conn, min_trail: int = 12) -> dict:
    cov: dict = {"legs_settled": 0, "legs_scored": 0}
    universe = _release_universe(conn, cov)
    keys = [_set_key(p) for p in GRID]
    scores: dict[str, list[float]] = {k: [] for k in keys}     # per-release Brier per set
    market: list[float] = []
    rows = []
    for ev in universe:
        row = {"period": ev["period"]}
        ok = True
        for p, k in zip(GRID, keys):
            try:
                pred = claims_model.predict(conn, ev["asof"], ev["period"], params=p)
            except Exception:                                    # noqa: BLE001
                ok = False
                break
            pmf = grid_pmf(pred.dist, 250.0)
            bm, bk = _brier(pmf, ev["legs"])
            row[k] = bm
            row["market"] = bk
        if not ok:
            continue
        for k in keys:
            scores[k].append(row[k])
        market.append(row["market"])
        rows.append(row)
    n = len(rows)
    cov["leg_coverage"] = (round(cov["legs_scored"] / cov["legs_settled"], 4)
                           if cov["legs_settled"] else None)
    if n < min_trail + 4:
        return {"n": n, "leg_universe": cov,
                "error": f"not enough scored releases (need {min_trail + 4})"}

    # walk-forward selection path: at t, pick argmin trailing-mean Brier over (<t)
    wf_sel, wf_default = [], []
    picks = []
    for t in range(min_trail, n):
        trail = slice(max(0, t - 26), t)
        best_k = min(keys, key=lambda k: float(np.mean(scores[k][trail])))
        wf_sel.append(rows[t][best_k])
        wf_default.append(rows[t][keys[DEFAULT_IX]])
        picks.append(best_k)
    agg = {
        "n_releases": n, "n_sets": len(GRID), "wf_start": min_trail,
        "brier_wf_selected": round(float(np.mean(wf_sel)), 5),
        "brier_wf_default": round(float(np.mean(wf_default)), 5),
        "brier_market_wf_window": round(float(np.mean(market[min_trail:])), 5),
        "picks_mode": max(set(picks), key=picks.count),
        "per_set_full_period": {k: round(float(np.mean(v)), 5)
                                for k, v in sorted(scores.items(),
                                                   key=lambda kv: np.mean(kv[1]))},
        "leg_universe": cov,
        "note": "per_set_full_period is descriptive ONLY (lookahead if used to pick);"
                " adoption requires wf_selected < default AND < market + §9.5 gate."
                " leg_universe.leg_coverage is the fraction of SETTLED legs that had a"
                " usable market bar and were therefore scored — every Brier above is on"
                " that subsample, which is selected on liquidity, not at random",
    }
    return {"agg": agg, "per_release": rows, "picks": picks}


def main():
    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-trail", type=int, default=12)
    args = ap.parse_args()
    s = load_settings()
    conn = init_db(s.db_path)
    res = run_grid(conn, min_trail=args.min_trail)
    if "agg" in res:
        _store_experiment(conn, "claims_grid", SERIES,
                          f"wf{res['agg']['n_releases']}", res["agg"],
                          {"grid": len(GRID), "min_trail": args.min_trail})
        (s.output_dir / "bt_claims_grid.json").write_text(json.dumps(res, indent=1,
                                                                     default=str))
    print(json.dumps(res.get("agg", res), indent=1))


if __name__ == "__main__":
    main()
