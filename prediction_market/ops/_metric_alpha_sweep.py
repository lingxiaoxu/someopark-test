"""Systematic PIT walk-forward search over EVERY team intra-game metric (+ derived
ratios) for predictive alpha: which of the ~18 API-Football stats, as a prior-match
'net form' rating nudge, actually lower out-of-sample Brier?

Method (identical discipline to the xG study, applied to all metrics):
  * parse all 18 team stats per (fixture, team) from fixture_stats.raw_json
  * for each metric build net_form = (team metric − opponent metric), PIT-averaged over
    each team's matches strictly before the one being predicted, z-scored across teams
  * blend into the FIFA-rating base at a small weight, re-price, score OOS argmax + Brier
  * rank metrics by OOS Brier improvement vs the no-blend baseline

No look-ahead: a match is predicted only from its two teams' EARLIER matches.
"""
from __future__ import annotations

import json
import math

from prediction_market.config.config import CONFIG

_FINISHED = ("FT", "AET", "PEN")


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


# metric key in raw_json → short label
_METRICS = {
    "expected_goals": "xG",
    "Shots on Goal": "shots_on",
    "Total Shots": "shots_tot",
    "Shots insidebox": "shots_in",
    "Shots outsidebox": "shots_out",
    "Blocked Shots": "blocked",
    "Corner Kicks": "corners",
    "Ball Possession": "poss",
    "Passes %": "pass_pct",
    "Total passes": "passes",
    "Goalkeeper Saves": "gk_saves",
    "goals_prevented": "gk_prevent",
    "Fouls": "fouls",
    "Offsides": "offsides",
}


def _parse_metrics(blk: dict) -> dict:
    out = {}
    for s in blk.get("statistics", []):
        k = _METRICS.get(s.get("type"))
        if k:
            out[k] = _num(s.get("value"))
    return out


def run(sweeps: int = 30, weights=(0.15, 0.30)):
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.match_pricing import price_match
    from prediction_market.model.strength import StrengthModel, build_strength

    conn = store.init_db()
    prior = load_prior()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    # per (fixture, team_api) metrics
    fmet = {}
    for r in conn.execute("SELECT fixture_api_id, team_api_id, raw_json FROM fixture_stats WHERE raw_json IS NOT NULL"):
        try:
            fmet[(r["fixture_api_id"], r["team_api_id"])] = _parse_metrics(json.loads(r["raw_json"]))
        except Exception:
            pass
    fx = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, kickoff_ts FROM fixture "
        "WHERE status_short IN ({}) AND home_goals IS NOT NULL ORDER BY kickoff_ts".format(
            ",".join("?" * len(_FINISHED))), _FINISHED).fetchall()
    matches = []
    for r in fx:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            continue
        y = 0 if r["home_goals"] > r["away_goals"] else (1 if r["home_goals"] == r["away_goals"] else 2)
        mh = fmet.get((r["api_id"], r["home_api_id"]), {})
        ma = fmet.get((r["api_id"], r["away_api_id"]), {})
        # derived metrics
        for m, opp in ((mh, ma), (ma, mh)):
            st, so = m.get("shots_tot"), m.get("shots_on")
            m["shot_acc"] = (so / st) if (st and so is not None) else None       # shots-on / total
            xg, s = m.get("xG"), m.get("shots_tot")
            m["xg_per_shot"] = (xg / s) if (xg is not None and s) else None       # finishing quality
        matches.append({"hi": hi, "ai": ai, "y": y, "ko": r["kickoff_ts"], "mh": mh, "ma": ma})
    n = len(matches)

    base = build_strength(prior, CONFIG.model, sweeps=sweeps)

    def z(d):
        xs = list(d.values())
        if len(xs) < 2:
            return {k: 0.0 for k in d}
        mu = sum(xs) / len(xs); sd = (sum((x - mu) ** 2 for x in xs) / len(xs)) ** 0.5 or 1.0
        return {k: (v - mu) / sd for k, v in d.items()}

    def netform(i, metric):
        ko = matches[i]["ko"]; acc = {}
        for j in range(i):
            mj = matches[j]
            if mj["ko"] >= ko:
                continue
            vh, va = mj["mh"].get(metric), mj["ma"].get(metric)
            if vh is not None and va is not None:
                acc.setdefault(mj["hi"], []).append(vh - va)
                acc.setdefault(mj["ai"], []).append(va - vh)
        return z({t: sum(v) / len(v) for t, v in acc.items() if v})

    def adj(zz, w):
        if w <= 0 or not zz:
            return base
        b = getattr(base.cfg, "rating_bound", 1.5); nw = dict(base.ratings)
        for t, zv in zz.items():
            if t in nw:
                nw[t] = max(-b, min(b, nw[t] + w * zv))
        return StrengthModel(ratings=nw, sigma=dict(base.sigma), host_ids=base.host_ids, cfg=base.cfg)

    def brier(p, y):
        oh = [1.0 if k == y else 0.0 for k in range(3)]
        return sum((p[k] - oh[k]) ** 2 for k in range(3))

    MIN_TRAIN = 6
    ys = [m["y"] for m in matches]

    def score(sm_of):
        hits = 0.0; bs = 0.0; nn = 0
        for i in range(MIN_TRAIN, n):
            sm = sm_of(i)
            mp = price_match(sm, matches[i]["hi"], matches[i]["ai"])
            p = [mp.p_home, mp.p_draw, mp.p_away]
            hits += max(range(3), key=lambda k: p[k]) == ys[i]
            bs += brier(p, ys[i]); nn += 1
        return hits / nn, bs / nn

    base_acc, base_br = score(lambda i: base)
    print("=" * 70)
    print(f"PIT METRIC-ALPHA SWEEP — {n} matches, predict from {MIN_TRAIN+1}; baseline Brier {base_br:.4f}")
    print("=" * 70)
    print(f"{'metric':<13}{'w':>6}{'OOS acc':>9}{'OOS Brier':>11}{'ΔBrier':>9}")
    print(f"{'(baseline)':<13}{0.0:>6.2f}{base_acc:>8.1%}{base_br:>11.4f}{0.0:>9.4f}")
    results = []
    metrics = list(_METRICS.values()) + ["shot_acc", "xg_per_shot"]
    for metric in metrics:
        for w in weights:
            acc, br = score(lambda i, _m=metric, _w=w: adj(netform(i, _m), _w))
            results.append((metric, w, acc, br, br - base_br))
    for metric, w, acc, br, dbr in sorted(results, key=lambda r: r[4]):
        flag = "  <-- helps" if dbr < -0.002 else ("  (hurts)" if dbr > 0.002 else "")
        print(f"{metric:<13}{w:>6.2f}{acc:>8.1%}{br:>11.4f}{dbr:>+9.4f}{flag}")


if __name__ == "__main__":
    run()
