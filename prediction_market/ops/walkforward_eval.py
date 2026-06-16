"""ops/walkforward_eval.py — walk-forward PIT validation of the result-update (Elo).

Tests whether nudging team ratings with completed results (model/strength.py
`update_with_results`, a goals-based Elo-style move) improves out-of-sample
prediction, the PIT-correct way: matches are processed in kickoff order, and each
match is predicted using ONLY the prior + the results of matches that kicked off
BEFORE it (no leakage from the match itself or later ones). For a grid of learning
rates it reports the walk-forward Brier vs the static (prior-only) baseline.

It also reports how many matches actually had usable prior data — early in a
tournament most teams are playing their first game, so the result-update is inert
until the second round of group games finishes. This harness is the gate: the
update is only worth wiring into the live model once it demonstrably lowers the
walk-forward Brier here.

    python -m prediction_market.ops.walkforward_eval  →  data/output/walkforward_eval.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market.config import CONFIG

_FINISHED = ("FT", "AET", "PEN")


def _brier(p, y):
    return sum((p[k] - (1.0 if k == y else 0.0)) ** 2 for k in range(3))


def load_chrono(conn):
    """Settled matches in kickoff order: (hi, ai, gh, ga, outcome, kickoff_dt)."""
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    rows = conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals, kickoff_ts FROM fixture "
        "WHERE status_short IN ({}) AND home_goals IS NOT NULL AND kickoff_ts IS NOT NULL "
        "ORDER BY kickoff_ts".format(",".join("?" * len(_FINISHED))), _FINISHED).fetchall()
    out = []
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            continue
        gh, ga = r["home_goals"], r["away_goals"]
        y = 0 if gh > ga else (1 if gh == ga else 2)
        try:
            dt = datetime.fromisoformat(r["kickoff_ts"])
        except Exception:
            continue
        out.append((hi, ai, gh, ga, y, dt))
    return out


def walkforward(matches, prior, base_sm, lr: float):
    """Walk-forward Brier at a learning rate. lr=0 ⇒ static prior baseline."""
    from prediction_market.model.match_pricing import price_match
    from prediction_market.model.strength import update_with_results

    total = 0.0
    n = 0
    n_with_prior = 0
    for k in range(len(matches)):
        hi, ai, _gh, _ga, y, kdt = matches[k]
        sm = base_sm
        if lr > 0:
            prior_results = []
            teams_seen = set()
            for j in range(k):
                ph, pa, pgh, pga, _py, pdt = matches[j]
                days_ago = max(0.0, (kdt - pdt).total_seconds() / 86400.0)
                prior_results.append({"home_id": ph, "away_id": pa,
                                      "home_goals": pgh, "away_goals": pga, "days_ago": days_ago})
                teams_seen.update((ph, pa))
            if prior_results and (hi in teams_seen or ai in teams_seen):
                sm = update_with_results(base_sm, prior_results, lr=lr)
                n_with_prior += 1
        mp = price_match(sm, hi, ai)
        total += _brier([mp.p_home, mp.p_draw, mp.p_away], y)
        n += 1
    return {"lr": lr, "brier": round(total / n, 4) if n else None, "n": n, "n_with_prior_data": n_with_prior}


def run(conn=None) -> dict:
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.strength import build_strength

    conn = conn or store.init_db()
    prior = load_prior()
    base_sm = build_strength(prior)
    matches = load_chrono(conn)

    grid = [0.0, 0.03, 0.05, 0.08, 0.12, 0.20]
    results = [walkforward(matches, prior, base_sm, lr) for lr in grid]
    baseline = next(r for r in results if r["lr"] == 0.0)
    scored = [r for r in results if r["lr"] > 0 and r["brier"] is not None]
    best = min(scored, key=lambda r: r["brier"]) if scored else None
    n_repeat = max((r["n_with_prior_data"] for r in results), default=0)

    if n_repeat == 0:
        verdict = ("No team has played a second game yet — every settled match is a first "
                   "game, so the result-update has no prior data to learn from and is INERT "
                   "(walk-forward == baseline). It activates after round-2 group games finish.")
        helps = False
    else:
        helps = bool(best and best["brier"] < baseline["brier"])
        verdict = (f"Walk-forward result-update {'IMPROVES' if helps else 'does NOT improve'} OOS Brier "
                   f"(best lr={best['lr']} → {best['brier']} vs baseline {baseline['brier']}). "
                   f"{'Wire into the live model.' if helps else 'Keep prior-only for now.'}")

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "n_settled": baseline["n"],
        "n_matches_with_prior_data": n_repeat,
        "baseline_brier": baseline["brier"],
        "grid": results,
        "best": best,
        "improves": helps,
        "verdict": verdict,
    }


def main() -> None:
    doc = run()
    CONFIG.paths.ensure()
    (CONFIG.paths.output / "walkforward_eval.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WALK-FORWARD RESULT-UPDATE — {doc['n_settled']} settled, "
          f"{doc['n_matches_with_prior_data']} with prior data")
    print(f"  baseline (prior-only) Brier : {doc['baseline_brier']}")
    for r in doc["grid"]:
        if r["lr"] > 0:
            print(f"  lr={r['lr']:<5} Brier {r['brier']}  (used prior data in {r['n_with_prior_data']} matches)")
    print(f"  → {doc['verdict']}")


if __name__ == "__main__":
    main()
