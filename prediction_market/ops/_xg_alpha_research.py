"""Find PIT alpha from the newly-pulled xG data: does an xG-based form adjustment
predict WC results better than the goals-based one — strictly walk-forward?

For each match i (chronological), each team's form is computed from ONLY its prior WC
matches (kickoff < kickoff_i — no leakage). Two signals, isolated on the SAME fixtures:
  * goals form: z-scored net goals / game
  * xG form   : z-scored net xG / game   (xG = 'deserved' result, lower-noise predictor)
We blend each into the FIFA-rating base at a grid of weights, re-price match i, and score
OUT-OF-SAMPLE argmax accuracy + Brier. Baseline (weight 0) reproduces the ~50% number.

Honest by construction: the base strength is FIFA-prior only; the only thing that touches
match i is its two teams' EARLIER xG/goals. The weight is a hyperparameter — we first show
the whole curve (does the signal exist at all?), then a PIT weight selection.
"""
from __future__ import annotations

import math

from prediction_market.config.config import CONFIG

_FINISHED = ("FT", "AET", "PEN")
_BOUND_FALLBACK = 1.5


def _z(vals: dict):
    xs = list(vals.values())
    if len(xs) < 2:
        return {k: 0.0 for k in vals}
    mu = sum(xs) / len(xs)
    sd = (sum((x - mu) ** 2 for x in xs) / len(xs)) ** 0.5 or 1.0
    return {k: (v - mu) / sd for k, v in vals.items()}


def _adjust(sm, zidx: dict, weight: float):
    from prediction_market.model.strength import StrengthModel
    if weight <= 0 or not zidx:
        return sm
    b = getattr(sm.cfg, "rating_bound", _BOUND_FALLBACK)
    new = dict(sm.ratings)
    for tid, z in zidx.items():
        if tid in new:
            new[tid] = max(-b, min(b, new[tid] + weight * z))
    return StrengthModel(ratings=new, sigma=dict(sm.sigma), host_ids=sm.host_ids, cfg=sm.cfg)


def run(sweeps: int = 30):
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.match_pricing import price_match
    from prediction_market.model.strength import build_strength

    conn = store.init_db()
    prior = load_prior()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    # finished WC fixtures, chronological, with each team's xG from fixture_stats
    fx = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, kickoff_ts "
        "FROM fixture WHERE status_short IN ({}) AND home_goals IS NOT NULL "
        "ORDER BY kickoff_ts".format(",".join("?" * len(_FINISHED))), _FINISHED).fetchall()
    xg = {}
    for r in conn.execute("SELECT fixture_api_id, team_api_id, xg FROM fixture_stats"):
        xg[(r["fixture_api_id"], r["team_api_id"])] = r["xg"]

    # per-team chronological history of (kickoff, net_goals, net_xg)
    hist: dict[str, list] = {}
    matches = []
    for r in fx:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            continue
        y = 0 if r["home_goals"] > r["away_goals"] else (1 if r["home_goals"] == r["away_goals"] else 2)
        xh = xg.get((r["api_id"], r["home_api_id"]))
        xa = xg.get((r["api_id"], r["away_api_id"]))
        matches.append({"hi": hi, "ai": ai, "y": y, "ko": r["kickoff_ts"],
                        "ng_h": r["home_goals"] - r["away_goals"], "ng_a": r["away_goals"] - r["home_goals"],
                        "nx_h": (xh - xa) if (xh is not None and xa is not None) else None,
                        "nx_a": (xa - xh) if (xh is not None and xa is not None) else None})
    n = len(matches)

    base = build_strength(prior, CONFIG.model, sweeps=sweeps)

    def form_z_asof(idx_i: int, key: str):
        """z-scored mean net-(goals|xg)/game from each team's matches strictly before match idx_i."""
        ko_i = matches[idx_i]["ko"]
        acc: dict[str, list] = {}
        for j in range(idx_i):
            mj = matches[j]
            if mj["ko"] >= ko_i:
                continue
            for side in ("h", "a"):
                tid = mj["hi"] if side == "h" else mj["ai"]
                v = mj[f"{key}_{side}"]
                if v is not None:
                    acc.setdefault(tid, []).append(v)
        means = {t: sum(v) / len(v) for t, v in acc.items() if v}
        return _z(means)

    def brier(p, y):
        oh = [1.0 if k == y else 0.0 for k in range(3)]
        return sum((p[k] - oh[k]) ** 2 for k in range(3))

    MIN_TRAIN = 6
    WEIGHTS = [0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40]
    rows = {}
    for sig in ("goals", "xg"):
        key = "ng" if sig == "goals" else "nx"
        for w in WEIGHTS:
            hits = nn = 0
            bsum = 0.0
            for i in range(MIN_TRAIN, n):
                z = form_z_asof(i, key) if w > 0 else {}
                sm = _adjust(base, z, w)
                mp = price_match(sm, matches[i]["hi"], matches[i]["ai"])
                p = [mp.p_home, mp.p_draw, mp.p_away]
                pred = max(range(3), key=lambda k: p[k])
                hits += pred == matches[i]["y"]
                bsum += brier(p, matches[i]["y"])
                nn += 1
            rows[(sig, w)] = (hits / nn, bsum / nn, nn)

    print("=" * 66)
    print(f"PIT WALK-FORWARD xG-vs-GOALS FORM ALPHA  ({n} matches, predict from match {MIN_TRAIN+1})")
    print("=" * 66)
    print(f"{'signal':<8}{'weight':>8}{'OOS acc':>10}{'OOS Brier':>12}")
    base_acc = rows[("goals", 0.0)][0]
    for sig in ("goals", "xg"):
        for w in WEIGHTS:
            acc, br, nn = rows[(sig, w)]
            star = "  <-- best" if acc == max(r[0] for r in rows.values()) else ""
            tag = "  (baseline)" if w == 0.0 else ""
            print(f"{sig:<8}{w:>8.2f}{acc:>9.1%}{br:>12.3f}{tag}{star}")
        print()
    return rows


if __name__ == "__main__":
    run()
