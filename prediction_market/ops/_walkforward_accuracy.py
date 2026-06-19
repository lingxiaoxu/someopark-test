"""Honest walk-forward (PIT) accuracy: for each match in chronological order, select
the model param set by calibrated Brier on ONLY the prior matches, predict THIS match,
and score argmax accuracy + Brier out-of-sample. No look-ahead, no in-sample selection.

Also reports: per-prefix which param set wins (does the optimum drift / would per-regime
switching help?), and the realistic accuracy ceiling vs the static全样本-selected set.
"""
from __future__ import annotations

import itertools
from dataclasses import replace

from prediction_market.config.config import CONFIG

# Focused STRUCTURAL grid (the dims that move 3-way probs most), kept small so each set's
# model is built ONCE and reused across every prefix (walk-forward is then just re-ranking
# cached per-match probs — fast + honest).
GRID = {
    "base_mu": [0.25, 0.30, 0.35],
    "beta": [0.40, 0.55, 0.70, 0.85],
    "dc_rho": [-0.16, -0.12, -0.08, 0.0],
}
_FINISHED = ("FT", "AET", "PEN")


def _brier(p, y):
    oh = [1.0 if k == y else 0.0 for k in range(3)]
    return sum((p[k] - oh[k]) ** 2 for k in range(3))


def run(sweeps: int = 24):
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.match_pricing import price_match
    from prediction_market.model.strength import build_strength

    conn = store.init_db()
    prior = load_prior()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    fx = conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals, kickoff_ts FROM fixture "
        "WHERE status_short IN ({}) AND home_goals IS NOT NULL ORDER BY kickoff_ts".format(
            ",".join("?" * len(_FINISHED))), _FINISHED).fetchall()
    matches = []
    for r in fx:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            continue
        y = 0 if r["home_goals"] > r["away_goals"] else (1 if r["home_goals"] == r["away_goals"] else 2)
        matches.append((hi, ai, y))
    n = len(matches)

    keys = list(GRID)
    combos = [dict(zip(keys, v)) for v in itertools.product(*[GRID[k] for k in keys])]
    # Build each set's model ONCE; cache its prob vector for every match.
    cache = []   # [{params, probs:[[p_h,p_d,p_a]...]}]
    for p in combos:
        sm = build_strength(prior, replace(CONFIG.model, **p), sweeps=sweeps)
        probs = []
        for hi, ai, _y in matches:
            mp = price_match(sm, hi, ai)
            probs.append([mp.p_home, mp.p_draw, mp.p_away])
        cache.append({"params": p, "probs": probs})

    ys = [y for _, _, y in matches]

    # ── static (full-sample-selected) — the optimistic in-sample baseline ──
    def total_brier(probs):
        return sum(_brier(probs[i], ys[i]) for i in range(n))
    static = min(cache, key=lambda c: total_brier(c["probs"]))
    static_acc = sum(1 for i in range(n) if max(range(3), key=lambda k: static["probs"][i][k]) == ys[i]) / n
    static_brier = total_brier(static["probs"]) / n

    # ── walk-forward: predict match i using the set best on matches[0:i] ──
    MIN_TRAIN = 6
    wf_hits = wf_n = 0
    wf_brier = 0.0
    chosen = []
    for i in range(MIN_TRAIN, n):
        # rank sets by Brier on the PRIOR matches only
        best = min(cache, key=lambda c: sum(_brier(c["probs"][j], ys[j]) for j in range(i)))
        pv = best["probs"][i]
        pred = max(range(3), key=lambda k: pv[k])
        wf_hits += pred == ys[i]
        wf_brier += _brier(pv, ys[i])
        wf_n += 1
        chosen.append((i, best["params"]))
    wf_acc = wf_hits / wf_n
    wf_brier /= wf_n

    # how much does the chosen set drift? (would per-regime switching even fire?)
    uniq = {tuple(sorted(p.items())) for _, p in chosen}

    print("=" * 64)
    print(f"WALK-FORWARD ACCURACY (PIT)  —  {n} settled matches, {len(combos)} param sets")
    print("=" * 64)
    print(f"  argmax = the model's single most-likely outcome (home/draw/away)")
    print(f"  uniform-random baseline accuracy        : {1/3:.1%}")
    print(f"  always-home baseline                    : {sum(1 for y in ys if y==0)/n:.1%}")
    print(f"  STATIC set (chosen IN-SAMPLE on all {n})  : acc {static_acc:.1%}  Brier {static_brier:.3f}  <- optimistic")
    print(f"  WALK-FORWARD (PIT, train on prior only) : acc {wf_acc:.1%}  Brier {wf_brier:.3f}  <- honest, n={wf_n}")
    print(f"  distinct param sets chosen over the WF   : {len(uniq)} (drift ⇒ regime-switching could matter)")
    print(f"  actual outcome mix: home {sum(1 for y in ys if y==0)}, draw {sum(1 for y in ys if y==1)}, away {sum(1 for y in ys if y==2)}")
    return {"static_acc": static_acc, "wf_acc": wf_acc, "wf_brier": wf_brier, "n": n}


if __name__ == "__main__":
    run()
