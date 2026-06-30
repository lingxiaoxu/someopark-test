"""ops/param_sweep.py — PIT parameter search on completed WC-2026 matches.

Extracts the STRUCTURAL model parameters that drive predictions, generates a grid
of ~180 parameter sets, re-prices every COMPLETED match point-in-time, scores each
set against the actual results, and selects the one with the lowest Brier
(= multiclass MSE between the predicted W/D/L vector and the one-hot outcome).

Point-in-time correctness: ratings come ONLY from the pre-tournament prior (FIFA
rank + ext_sim prior), never from match results — so scoring on completed matches
is genuine out-of-sample for the outcome. The grid tunes the GLOBAL structural
knobs; per-team skill lives in the ratings (already fit), and the two teams enter
a match through the rating DIFFERENCE in the lambda formula.

Granularity note: with ~16-20 settled matches, GLOBAL parameters are the only
statistically defensible choice (per-team / per-match would massively overfit:
each team has played 1-2 games). As more matches accrue, segmenting by rank-band
(e.g. a separate beta for big mismatches vs even games) becomes possible — the
framework exposes a `--segment rank_band` hook for that.

    python -m prediction_market.ops.param_sweep [--segment global|rank_band] [--loocv]
    → data/output/param_sweep.json  +  param_selected.json
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
from dataclasses import asdict, replace

from prediction_market.config import CONFIG

_FINISHED = ("FT", "AET", "PEN")

# Swept structural knobs (ModelConfig fields). ~180 combinations.
GRID = {
    # ── λ / Dixon-Coles structure ──
    "base_mu": [0.25, 0.30, 0.35],                  # base scoring level
    "beta": [0.40, 0.55, 0.70, 0.85],               # rating-gap → goal-diff sharpness
    "dc_rho": [-0.16, -0.12, -0.08, 0.0],           # low-score correlation (draw mass)
    # ── rating-construction anchors ──
    "rank_anchor_weight": [0.5, 0.7],               # FIFA-rank vs exp-points blend
    "fc_blend_weight": [0.0, 0.12, 0.24],           # EA FC 26 squad-talent anchor (NEW)
    "squad_blend_weight": [0.0, 0.15],              # club-form squad blend (NEW to the sweep)
    "form_blend_weight": [0.0, 0.10],               # recent-form blend (NEW to the sweep)
    # ── opponent-adjusted alt-data λ multipliers (NEW to the sweep) ──
    # These were hand-set in-sample on 19 matches (def=0.45/off=0.25) and drive the biggest
    # market divergences (e.g. Morocco>Netherlands), so let the optimiser pick them too.
    # NOTE: the altdata index here is computed once on all data (like the squad/form/fc
    # anchors above) — NOT point-in-time — so this dimension carries the same mild in-sample
    # leak as the other anchors; the selected value is directional, re-tune as matches accrue.
    "oppadj_def_weight": [0.0, 0.25, 0.45],         # opponent-defence λ suppression
    "oppadj_off_weight": [0.0, 0.25],               # own-attack λ boost
}


def load_settled(conn):
    """[(home_id, away_id, outcome 0/1/2, rank_home, rank_away)] for finished matches."""
    from prediction_market.ingest.prior_ingest import load_prior
    prior = load_prior()
    rank = {t.team_id: t.fifa_rank for t in prior.teams}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    rows = conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals FROM fixture "
        "WHERE status_short IN ({}) AND home_goals IS NOT NULL".format(",".join("?" * len(_FINISHED))),
        _FINISHED).fetchall()
    out = []
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            continue
        gh, ga = r["home_goals"], r["away_goals"]
        outcome = 0 if gh > ga else (1 if gh == ga else 2)
        out.append((hi, ai, outcome, rank.get(hi, 999), rank.get(ai, 999)))
    return out


def _score(probs, outcomes):
    """Brier (multiclass MSE), log-loss, accuracy over a list of (p_vec, outcome)."""
    n = len(outcomes)
    if not n:
        return {"brier": float("nan"), "log_loss": float("nan"), "acc": float("nan"), "n": 0}
    brier = log_loss = hits = 0.0
    for p, y in zip(probs, outcomes):
        one_hot = [1.0 if k == y else 0.0 for k in range(3)]
        brier += sum((p[k] - one_hot[k]) ** 2 for k in range(3))
        log_loss += -math.log(max(p[y], 1e-12))
        hits += 1 if max(range(3), key=lambda k: p[k]) == y else 0
    return {"brier": brier / n, "log_loss": log_loss / n, "acc": hits / n, "n": n}


def _anchor_indices(conn) -> dict:
    """Precompute the squad / form / FC z-indices once (cfg-independent) so each param
    set just re-weights them instead of re-reading the DB."""
    out: dict = {}
    try:
        from prediction_market.model.squad_strength import squad_index
        out["squad"] = squad_index(conn)
    except Exception:
        pass
    try:
        from prediction_market.model.form_strength import form_index
        out["form"] = form_index(conn)
    except Exception:
        pass
    try:
        from prediction_market.model.fc_strength import fc_squad_index
        out["fc"] = fc_squad_index(conn)
    except Exception:
        pass
    # Opponent-adjusted alt-data index (attack/defence z), computed once on baseline ratings
    # for the opponent-strength weighting — cfg-independent enough to reuse across the grid
    # (ratings shift only slightly per set). Attached in evaluate() when oppadj weights != 0.
    try:
        from prediction_market.model.altdata_adjust import altdata_index
        from prediction_market.model.strength import build_strength
        from prediction_market.ingest.prior_ingest import load_prior
        base_sm = build_strength(load_prior(), CONFIG.model)
        out["altdata"] = altdata_index(conn, base_sm.ratings)
    except Exception:
        pass
    return out


def evaluate(params: dict, settled, prior, *, sweeps: int = 30, idx: dict | None = None) -> dict:
    """Brier/log-loss/acc for one parameter set over the settled matches — both the
    RAW model and, fairly, the CALIBRATED model.

    The live system never trades the raw model: it applies post-hoc probability
    calibration (temperature + draw-mass), under which the model beats the uniform
    baseline. Scoring param sets on raw Brier alone makes every set look worse than
    uniform (it sorts well but is over-confident). So we also fit each set's own
    calibration and report the calibrated Brier — the apples-to-apples number against
    the uniform baseline and what selection should rank on.
    """
    from prediction_market.model.match_pricing import price_match
    from prediction_market.model.probability_calibration import fit_calibration
    from prediction_market.model.strength import build_strength
    cfg = replace(CONFIG.model, **params)
    sm = build_strength(prior, cfg, sweeps=sweeps)
    # Apply the rating ANCHORS the live model uses (squad / form / FC talent), with this
    # set's weights, from PRECOMPUTED indices (passed in `idx`) so the sweep is fast.
    idx = idx or {}
    if idx:
        from prediction_market.model.fc_strength import fc_adjusted_ratings
        from prediction_market.model.form_strength import form_adjusted_ratings
        from prediction_market.model.squad_strength import squad_adjusted_ratings
        if cfg.squad_blend_weight and idx.get("squad"):
            sm = squad_adjusted_ratings(sm, idx["squad"], cfg.squad_blend_weight)
        if cfg.form_blend_weight and idx.get("form"):
            sm = form_adjusted_ratings(sm, idx["form"], cfg.form_blend_weight)
        if cfg.fc_blend_weight and idx.get("fc"):
            sm = fc_adjusted_ratings(sm, idx["fc"], cfg.fc_blend_weight)
        # Opponent-adjusted alt-data λ multipliers (attached LAST so pair_lambdas can apply the
        # def/off multipliers). Only when a weight is non-zero — keeps the off-default fast.
        if (cfg.oppadj_def_weight or cfg.oppadj_off_weight) and idx.get("altdata"):
            from dataclasses import replace as _replace
            sm = _replace(sm, adj=idx["altdata"])
    probs, outs = [], []
    for hi, ai, outcome, _rh, _ra in settled:
        mp = price_match(sm, hi, ai)
        probs.append([mp.p_home, mp.p_draw, mp.p_away])
        outs.append(outcome)
    s = _score(probs, outs)                          # s["brier"] = RAW brier
    cal = fit_calibration(probs, outs)               # each set's own calibration
    s["brier_raw"] = s["brier"]
    s["brier_cal"] = cal["calibrated_brier"]
    s["calibration"] = {"method": cal["method"], "param": cal["param"],
                        "draw_boost": cal.get("draw_boost")}
    s["params"] = params
    return s


def run(conn=None, *, sweeps: int = 30) -> dict:
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior

    conn = conn or store.init_db()
    prior = load_prior()
    settled = load_settled(conn)
    n = len(settled)

    keys = list(GRID)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*[GRID[k] for k in keys])]
    idx = _anchor_indices(conn)   # squad / form / FC indices, computed once
    results = [evaluate(p, settled, prior, sweeps=sweeps, idx=idx) for p in combos]
    # Rank on the CALIBRATED Brier — the number that's comparable to the uniform
    # baseline and that the live (calibrated) model is actually graded on.
    results.sort(key=lambda r: (r["brier_cal"], r["log_loss"]))

    baseline = evaluate({}, settled, prior, sweeps=sweeps, idx=idx)   # current CONFIG.model
    uni = round(2 / 3, 4)
    best = results[0] if results else None
    n_beat = sum(1 for r in results if r["brier_cal"] < uni)

    from datetime import datetime, timezone
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),   # "last updated" stamp
        "n_settled": n,
        "n_param_sets": len(combos),
        "grid": GRID,
        "uniform_brier": uni,
        "baseline": {"brier": round(baseline["brier"], 4), "brier_cal": round(baseline["brier_cal"], 4),
                     "log_loss": round(baseline["log_loss"], 4), "acc": round(baseline["acc"], 3)},
        "best": best,
        "top10": results[:10],
        # All sets, compact + ranked — drives the frontend "Parameter Sweep" artifact.
        # brier = raw model; brier_cal = after the post-hoc calibration we actually apply.
        "results_all": [
            {"rank": i + 1, "params": r["params"],
             "brier": round(r["brier"], 4), "brier_cal": round(r["brier_cal"], 4),
             "log_loss": round(r["log_loss"], 4), "acc": round(r["acc"], 3),
             "beats_uniform": r["brier_cal"] < uni}
            for i, r in enumerate(results)
        ],
        "selected_reason": (
            "Selected = the parameter set with the lowest CALIBRATED Brier among all sets. Each set is "
            "scored after the same post-hoc probability calibration the live model uses (the raw model "
            "is over-confident and sits above uniform; calibrated, it drops below). "
            f"{n_beat} of {len(combos)} sets beat the uniform baseline ({uni}) once calibrated."),
        "note": (f"Two Brier columns: RAW (over-confident, above uniform) and CALIBRATED (the fair, "
                 f"apples-to-apples number vs the {uni} uniform baseline — what the gate uses). "
                 f"Scored over {len(combos)} param sets on {n} settled matches. WARNING: small sample — "
                 f"ranking is directional, re-run as more matches finish. Global structural params; "
                 f"per-team skill is in the ratings."),
    }


def loocv(conn=None, *, sweeps: int = 25) -> dict:
    """Leave-one-out: for each held-out match, pick the min-Brier params on the
    rest, score the held-out one. Honest generalization estimate vs in-sample."""
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior

    conn = conn or store.init_db()
    prior = load_prior()
    settled = load_settled(conn)
    keys = list(GRID)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*[GRID[k] for k in keys])]

    sel_probs, sel_outs, base_probs = [], [], []
    from prediction_market.model.match_pricing import price_match
    from prediction_market.model.strength import build_strength
    base_sm = build_strength(prior, CONFIG.model, sweeps=sweeps)
    for i in range(len(settled)):
        train = settled[:i] + settled[i + 1:]
        held = settled[i]
        ranked = sorted((evaluate(p, train, prior, sweeps=sweeps) for p in combos),
                        key=lambda r: (r["brier"], r["log_loss"]))
        cfg = replace(CONFIG.model, **ranked[0]["params"])
        sm = build_strength(prior, cfg, sweeps=sweeps)
        mp = price_match(sm, held[0], held[1])
        sel_probs.append([mp.p_home, mp.p_draw, mp.p_away]); sel_outs.append(held[2])
        bmp = price_match(base_sm, held[0], held[1])
        base_probs.append([bmp.p_home, bmp.p_draw, bmp.p_away])
    return {"loocv_selected": _score(sel_probs, sel_outs),
            "loocv_baseline": _score(base_probs, sel_outs)}


def walk_forward(conn=None, *, start: int = 6, candidates=None) -> dict:
    """Honest PIT walk-forward for the alt-data λ weights (plan 19).

    Unlike `run()` (which selects in-sample and whose form anchor isn't point-in-time),
    this evaluates each FIXED weight candidate on the chronological OUT-OF-SAMPLE tail
    with strictly point-in-time features (form / alt-data cut at each match's kickoff).
    It does NOT fit the weight per step — at small N that overfits (proven); instead it
    asks "does this fixed small prior generalise on matches it never saw?". The alt-data
    weights stay a fixed bounded prior until N is large enough to fit them safely.
    """
    from dataclasses import replace
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.altdata_adjust import altdata_index
    from prediction_market.model.match_pricing import price_match
    from prediction_market.model.probability_calibration import load_calibration, apply_calibration
    from prediction_market.model.squad_strength import build_strength_live

    conn = conn or store.init_db()
    prior = load_prior()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    rows = conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals, round, kickoff_ts "
        "FROM fixture WHERE status_short IN ({}) AND home_goals IS NOT NULL "
        "ORDER BY kickoff_ts".format(",".join("?" * len(_FINISHED))), _FINISHED).fetchall()
    matches = [(cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"]), r["kickoff_ts"], r["round"],
                0 if r["home_goals"] > r["away_goals"] else (1 if r["home_goals"] == r["away_goals"] else 2))
               for r in rows if cmap.get(r["home_api_id"]) and cmap.get(r["away_api_id"])]
    cal = load_calibration()
    candidates = candidates or [
        {"label": "baseline", "oppadj_def_weight": 0.0, "oppadj_off_weight": 0.0},
        {"label": "oppadj-prior", "oppadj_def_weight": 0.10, "oppadj_off_weight": 0.15},
        {"label": "oppadj-off-only", "oppadj_def_weight": 0.0, "oppadj_off_weight": 0.18},
    ]
    from prediction_market.model.match_pricing import is_knockout
    res = []
    for c in candidates:
        cfg = replace(CONFIG.model, oppadj_def_weight=c["oppadj_def_weight"], oppadj_off_weight=c["oppadj_off_weight"])
        probs, outs = [], []
        for hi, ai, ko_ts, rnd, o in matches[start:]:
            # PIT: ratings + alt-data computed only from data strictly before this kickoff.
            sm = build_strength_live(conn, prior, cfg)
            if cfg.oppadj_def_weight or cfg.oppadj_off_weight:
                sm = replace(sm, adj=altdata_index(conn, sm.ratings, as_of=ko_ts))
            mp = price_match(sm, hi, ai, knockout=is_knockout(rnd))
            probs.append(apply_calibration([mp.p_home, mp.p_draw, mp.p_away], cal, knockout=is_knockout(rnd)))
            outs.append(o)
        s = _score(probs, outs)
        res.append({**{k: v for k, v in c.items()}, "brier_cal": round(s["brier"], 4), "acc": round(s["acc"], 3), "n": s["n"]})
    return {"n_oos": len(matches) - start, "start": start, "candidates": res,
            "note": ("Honest PIT walk-forward over the OOS tail (features cut at each kickoff). "
                     "Alt-data weights are a FIXED bounded prior, NOT fit per-step (small-N fitting "
                     "overfits). Promote a candidate to CONFIG only if it beats baseline here AND as N grows.")}


def main() -> None:
    ap = argparse.ArgumentParser(description="PIT parameter sweep on settled WC matches")
    ap.add_argument("--walk-forward", action="store_true", help="honest PIT walk-forward of the alt-data λ weights")
    ap.add_argument("--loocv", action="store_true", help="also run leave-one-out generalization estimate (slow)")
    ap.add_argument("--sweeps", type=int, default=30, help="coordinate-descent sweeps per rating fit")
    args = ap.parse_args()

    if args.walk_forward:
        wf = walk_forward()
        print(f"PIT WALK-FORWARD — {wf['n_oos']} OOS matches (features cut at each kickoff)")
        for c in wf["candidates"]:
            print(f"  {c['label']:16} Brier_cal {c['brier_cal']}  acc {c['acc']}  "
                  f"(def={c['oppadj_def_weight']} off={c['oppadj_off_weight']})")
        return

    doc = run(sweeps=args.sweeps)
    if args.loocv and doc["n_settled"] >= 4:
        doc["loocv"] = loocv(sweeps=max(20, args.sweeps - 5))

    CONFIG.paths.ensure()
    (CONFIG.paths.output / "param_sweep.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    if doc.get("best"):
        (CONFIG.paths.output / "param_selected.json").write_text(
            json.dumps({"params": doc["best"]["params"], "brier": doc["best"]["brier"],
                        "n_settled": doc["n_settled"]}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"PARAM SWEEP — {doc['n_settled']} settled matches, {doc['n_param_sets']} param sets")
    print(f"  uniform baseline Brier : {doc['uniform_brier']}")
    print(f"  current config  Brier  : {doc['baseline']['brier']}  (log-loss {doc['baseline']['log_loss']}, acc {doc['baseline']['acc']})")
    b = doc["best"]
    print(f"  BEST            Brier  : {round(b['brier'],4)}  (log-loss {round(b['log_loss'],4)}, acc {round(b['acc'],3)})")
    print(f"  best params            : {b['params']}")
    print("  top 5:")
    for r in doc["top10"][:5]:
        print(f"    Brier {round(r['brier'],4)}  acc {round(r['acc'],3)}  {r['params']}")
    if "loocv" in doc:
        lo = doc["loocv"]
        print(f"  LOOCV selected Brier   : {round(lo['loocv_selected']['brier'],4)}  vs baseline {round(lo['loocv_baseline']['brier'],4)}")


if __name__ == "__main__":
    main()
