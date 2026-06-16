"""Walk-forward replay (plan 05 §6, 03 §9) — NO future function.

Replays the model chronologically over played matches: each match is predicted
using ONLY information available before kickoff, then the actual result updates
the strength model for subsequent matches. This is a strict walk-forward (train
window and prediction window never overlap, plan 05 §6.4) — the prediction for
match N never sees match N's or any later result.

Compares two regimes to show whether sequential learning helps:
  * STATIC   — frozen pre-tournament strength for every match;
  * SEQUENTIAL — strength Bayesian-updated after each result (plan 03 §1b).

Reads played fixtures from the local store (real data); zero API calls.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from prediction_market.config import CONFIG
from prediction_market.backtest.metrics import bootstrap_ci, brier_score, log_loss, rolling_brier
from prediction_market.ingest.prior_ingest import load_prior
from prediction_market.model.match_pricing import price_match
from prediction_market.model.strength import build_strength, update_with_results

_FINISHED = ("FT", "AET", "PEN")


@dataclass
class ReplayResult:
    n_matches: int
    static_brier: float
    static_logloss: float
    sequential_brier: float
    sequential_logloss: float
    baseline_brier: float            # always-uniform 1/3,1/3,1/3
    brier_ci95_static: tuple[float, float]
    rolling_brier_sequential: list[float]
    notes: list[str]


def _played(conn) -> list[dict]:
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    out = []
    for r in conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals, kickoff_ts FROM fixture "
        "WHERE status_short IN ({}) AND home_goals IS NOT NULL ORDER BY kickoff_ts".format(
            ",".join("?" * len(_FINISHED))), _FINISHED):
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if hi and ai:
            out.append({"home_id": hi, "away_id": ai,
                        "home_goals": r["home_goals"], "away_goals": r["away_goals"]})
    return out


def _outcome(gh: int, ga: int) -> int:
    return 0 if gh > ga else (1 if gh == ga else 2)


def walk_forward_replay(conn=None, *, lr: float = 0.06) -> ReplayResult:
    from prediction_market.ingest import store

    conn = conn or store.init_db()
    matches = _played(conn)
    prior = load_prior()

    # STATIC regime: one frozen model for every match.
    sm_static = build_strength(prior)
    s_probs, outcomes = [], []
    for m in matches:
        mp = price_match(sm_static, m["home_id"], m["away_id"])
        s_probs.append([mp.p_home, mp.p_draw, mp.p_away])
        outcomes.append(_outcome(m["home_goals"], m["away_goals"]))

    # SEQUENTIAL regime: predict with current model, THEN update from the result.
    sm_seq = build_strength(prior)
    q_probs = []
    for m in matches:
        mp = price_match(sm_seq, m["home_id"], m["away_id"])     # uses only past info
        q_probs.append([mp.p_home, mp.p_draw, mp.p_away])
        sm_seq = update_with_results(sm_seq, [{**m, "days_ago": 0}], lr=lr)  # then learn

    n = len(matches)
    notes = ["Walk-forward: each match predicted before its result is seen (no future function).",
             "STATIC = frozen prior model; SEQUENTIAL = strength updated after each match."]
    if n < 8:
        notes.append(f"only {n} played matches — directional, not statistically powered.")

    if n == 0:
        return ReplayResult(0, *( [float('nan')] * 5 ), (float('nan'), float('nan')), [], notes)

    per_static = [sum((p[k] - (1.0 if outcomes[i] == k else 0.0)) ** 2 for k in range(3))
                  for i, p in enumerate(s_probs)]
    return ReplayResult(
        n_matches=n,
        static_brier=brier_score(s_probs, outcomes),
        static_logloss=log_loss(s_probs, outcomes),
        sequential_brier=brier_score(q_probs, outcomes),
        sequential_logloss=log_loss(q_probs, outcomes),
        baseline_brier=brier_score([[1 / 3, 1 / 3, 1 / 3]] * n, outcomes),
        brier_ci95_static=bootstrap_ci(per_static),
        rolling_brier_sequential=rolling_brier(q_probs, outcomes),
        notes=notes,
    )


def main() -> None:
    res = walk_forward_replay()
    CONFIG.paths.ensure()
    out = CONFIG.paths.output / "backtest_replay.json"
    out.write_text(json.dumps(asdict(res), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Walk-forward replay over {res.n_matches} played matches:")
    if res.n_matches:
        print(f"  baseline (uniform) Brier : {res.baseline_brier:.4f}")
        print(f"  STATIC      Brier/LogLoss: {res.static_brier:.4f} / {res.static_logloss:.4f} "
              f"(95% CI {res.brier_ci95_static[0]:.3f}-{res.brier_ci95_static[1]:.3f})")
        print(f"  SEQUENTIAL  Brier/LogLoss: {res.sequential_brier:.4f} / {res.sequential_logloss:.4f}")
        better = "SEQUENTIAL" if res.sequential_brier < res.static_brier else "STATIC"
        print(f"  → {better} calibrates better on this sample")
    for nnote in res.notes:
        print(f"  • {nnote}")
    print(f"  wrote {out}")


if __name__ == "__main__":
    main()
