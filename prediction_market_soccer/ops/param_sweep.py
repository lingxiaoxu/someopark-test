"""ops/param_sweep.py — RETIRED. Superseded by ops/param_select_club.py.

Kept for historical reference (and for ``walk_forward``, below, which is still
live). The World-Cup grid search is NOT re-axed for clubs and must not be
revived: what it optimises does not exist in this system.

WHY IT WAS RETIRED RATHER THAN RE-AXED — the axes were never the real problem:

  1. WRONG MODEL. ``run()`` builds ONE global StrengthModel from the merged
     ``clubs_all.json`` (399 clubs, 12 competitions) and reverse-fits it as if
     Arsenal and Shkëndija shared a double round-robin. Production prices every
     match with its OWN competition's prior and fitted base_mu/home_adv (C2), so
     the grid's objective is measured on a model nobody trades. Swapping
     ``rank_anchor_weight`` for a club knob would not have touched that.
  2. TWO OF THE AXES ARE INERT. Club ``build_strength`` never reads
     ``rank_anchor_weight`` (the WC group-difficulty patch was deliberately
     dropped — see model/strength.py), and ``base_mu`` is overridden per
     competition by ``leagues.fitted_params``, so sweeping the global is a no-op
     wherever a fit exists. Two of the nine axes doubled the grid for nothing.
  3. NOT RUNNABLE. The grid is 6,912 sets (the "~180" above is a WC leftover),
     each needing a coordinate-descent fit whose ``_expected_ppr`` is O(N²) in
     399 clubs, scored over 4,050 settled matches — before ``robust_select``'s
     ~70 refits. Nothing here could ever have finished.
  4. ADOPTION HAZARD. ``main()`` wrote ``param_selected.json``, which
     ``config._apply_selected_params()`` auto-loads. ``param_select_club`` writes
     that same file behind a margin gate and a ``--dry-run``. Two writers of one
     production file, one of them selecting on the wrong model, is precisely the
     silent-adoption bomb TRANSFORM_PLAN flags for ``param_selected.json``. So
     ``run()``/``loocv()``/``main()`` now refuse loudly instead of writing.

Nothing of value was lost: ``param_select_club`` selects per league, on a
TRAIN/TEST time split, with each candidate's calibration fit on TRAIN — an
out-of-sample guard the WC harness never had. The one idea worth salvaging is
``robust_select`` below (1-SE rule + bootstrap-stable elite pool + grid-edge
parsimony + a PBO diagnostic); it is a selection RULE, not a harness, and porting
it onto param_select_club's TRAIN scores is a clean follow-up.

    python -m prediction_market_soccer.ops.param_select_club   ← run this instead
    python -m prediction_market_soccer.ops.param_sweep --walk-forward   ← still works
"""
from __future__ import annotations

import argparse
import itertools
import math
from dataclasses import replace

from prediction_market_soccer.config import CONFIG

_FINISHED = ("FT", "AET", "PEN")

_RETIRED = (
    "ops.param_sweep is RETIRED (World-Cup global grid over a merged 399-club prior; "
    "production prices per league). Use ops.param_select_club — per-league, TRAIN/TEST "
    "time split, and the only sanctioned writer of param_selected.json. "
    "`param_sweep --walk-forward` is the one entry point still live here."
)

# FROZEN, WORLD-CUP AXES — historical reference only, read by robust_select().
# Do NOT re-point these at club knobs: see the retirement note at the top of the
# file; the axes were never what made this harness wrong.
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
    from prediction_market_soccer.ingest.club_prior import load_prior
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
        from prediction_market_soccer.model.squad_strength import squad_index
        out["squad"] = squad_index(conn)
    except Exception:
        pass
    try:
        from prediction_market_soccer.model.form_strength import form_index
        out["form"] = form_index(conn)
    except Exception:
        pass
    try:
        from prediction_market_soccer.model.fc_strength import fc_squad_index
        out["fc"] = fc_squad_index(conn)
    except Exception:
        pass
    # Opponent-adjusted alt-data index (attack/defence z), computed once on baseline ratings
    # for the opponent-strength weighting — cfg-independent enough to reuse across the grid
    # (ratings shift only slightly per set). Attached in evaluate() when oppadj weights != 0.
    try:
        from prediction_market_soccer.model.altdata_adjust import altdata_index
        from prediction_market_soccer.model.strength import build_strength
        from prediction_market_soccer.ingest.club_prior import load_prior
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
    from prediction_market_soccer.model.match_pricing import price_match
    from prediction_market_soccer.model.probability_calibration import fit_calibration
    from prediction_market_soccer.model.strength import build_strength
    cfg = replace(CONFIG.model, **params)
    sm = build_strength(prior, cfg, sweeps=sweeps)
    # Apply the rating ANCHORS the live model uses (squad / form / FC talent), with this
    # set's weights, from PRECOMPUTED indices (passed in `idx`) so the sweep is fast.
    idx = idx or {}
    if idx:
        from prediction_market_soccer.model.fc_strength import fc_adjusted_ratings
        from prediction_market_soccer.model.form_strength import form_adjusted_ratings
        from prediction_market_soccer.model.squad_strength import squad_adjusted_ratings
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


def _cal_brier_vector(params: dict, settled, prior, *, idx: dict | None = None, sweeps: int = 30) -> list:
    """Per-match CALIBRATED Brier for one param set — the raw material for bootstrap stability
    selection. Mirrors evaluate()'s model construction, but returns the length-N vector instead
    of the aggregate (calibration is fit once on the full sample, then applied per match)."""
    from prediction_market_soccer.model.match_pricing import price_match
    from prediction_market_soccer.model.probability_calibration import apply_calibration, fit_calibration
    from prediction_market_soccer.model.strength import build_strength
    cfg = replace(CONFIG.model, **params)
    sm = build_strength(prior, cfg, sweeps=sweeps)
    idx = idx or {}
    if idx:
        from prediction_market_soccer.model.fc_strength import fc_adjusted_ratings
        from prediction_market_soccer.model.form_strength import form_adjusted_ratings
        from prediction_market_soccer.model.squad_strength import squad_adjusted_ratings
        if cfg.squad_blend_weight and idx.get("squad"):
            sm = squad_adjusted_ratings(sm, idx["squad"], cfg.squad_blend_weight)
        if cfg.form_blend_weight and idx.get("form"):
            sm = form_adjusted_ratings(sm, idx["form"], cfg.form_blend_weight)
        if cfg.fc_blend_weight and idx.get("fc"):
            sm = fc_adjusted_ratings(sm, idx["fc"], cfg.fc_blend_weight)
        if (cfg.oppadj_def_weight or cfg.oppadj_off_weight) and idx.get("altdata"):
            sm = replace(sm, adj=idx["altdata"])
    probs, outs = [], []
    for hi, ai, outcome, _rh, _ra in settled:
        mp = price_match(sm, hi, ai)
        probs.append([mp.p_home, mp.p_draw, mp.p_away])
        outs.append(outcome)
    cal = fit_calibration(probs, outs)
    cp = [apply_calibration(p, cal) for p in probs]
    return [sum((cp[i][k] - (1.0 if outs[i] == k else 0.0)) ** 2 for k in range(3)) for i in range(len(outs))]


def robust_select(results, settled, prior, *, idx=None, sweeps=30, pool_frac=0.01,
                  n_boot=2000, seed=12345) -> dict:
    """Finance-style robust parameter selection — combats the multiple-comparisons optimism of
    raw argmin over thousands of trials (the in-sample best of N sets is upward-biased; some of
    its edge is luck, and it tends to sit on grid edges that regress out-of-sample).

      1. ELITE POOL  — the top ``pool_frac`` by calibrated Brier (≈1% ⇒ ~70 sets). These are
         statistically tied with the best (within sampling noise), so only genuine contenders
         compete — this is the 1-SE-rule idea applied as a percentile cut (the user's framing).
      2. BOOTSTRAP STABILITY — resample the matches ``n_boot`` times (seeded ⇒ reproducible);
         rank every pool member on each resample; SELECT the lowest MEDIAN bootstrap rank — the
         set that is *consistently* good across resamples, not the single-sample lucky winner.
         (Same spirit as DSR/PBO: prefer the configuration whose edge survives resampling.)

    Returns {chosen, diagnostics}. Diagnostics include a PBO-style number: how often the
    in-sample #1 falls OUTSIDE the resampled top-half of the pool (high ⇒ the raw winner is
    overfit). Calibration is fit once per set on the full sample (standard for stability)."""
    import numpy as np
    n_pool = max(2, int(round(len(results) * pool_frac)))
    pool = results[:n_pool]                       # results arrive pre-sorted by brier_cal
    vecs = np.array([_cal_brier_vector(r["params"], settled, prior, idx=idx, sweeps=sweeps)
                     for r in pool])               # (n_pool, n_matches)
    n_m = vecs.shape[1]
    rng = np.random.default_rng(seed)
    boot = np.empty((n_boot, n_pool))
    for b in range(n_boot):
        samp = rng.integers(0, n_m, n_m)           # matches resampled with replacement
        boot[b] = vecs[:, samp].mean(axis=1)       # each set's Brier on this resample
    ranks = boot.argsort(axis=1).argsort(axis=1)   # per-resample rank, 0 = best
    med_rank = np.median(ranks, axis=0)
    se = float(boot[:, 0].std())                   # 1 bootstrap SE of the in-sample best's Brier
    pbo = float((ranks[:, 0] >= n_pool / 2.0).mean())  # raw-#1 outside resampled top-half

    # Count how many SWEPT params sit on a grid boundary (min or max of their axis). Edges where
    # the optimum keeps improving if extended = overfit-prone extrapolation. Dims that are
    # CONSTANT across the pool (e.g. a robustly-selected oppadj_def=0.45) add the same count to
    # every member, so they don't bias the relative choice — only the genuinely-varying dims do.
    edge_vals = {k: (min(v), max(v)) for k, v in GRID.items()}
    def _edges(p):
        return sum(1 for k, (lo, hi) in edge_vals.items() if p.get(k) in (lo, hi))

    # 1-SE rule: among the sets statistically tied with the best (within 1 bootstrap SE of its
    # Brier), pick the most INTERIOR (fewest grid-edge params) — the parsimony/regularisation
    # choice that hedges the boundary overfit. Tie-break toward lower Brier.
    best_b = pool[0]["brier_cal"]
    tied = [i for i in range(n_pool) if pool[i]["brier_cal"] <= best_b + se] or [0]
    win = min(tied, key=lambda i: (_edges(pool[i]["params"]), pool[i]["brier_cal"]))
    return {
        "chosen": pool[win],
        "diagnostics": {
            "method": "1-SE rule + fewest grid-edges, within top-%g%% bootstrap-stable pool" % (pool_frac * 100),
            "pool_frac": pool_frac, "pool_size": n_pool, "n_boot": n_boot, "seed": seed,
            "bootstrap_se": round(se, 4), "n_within_1se": len(tied),
            "chosen_full_rank": win + 1,
            "chosen_brier_cal": pool[win]["brier_cal"], "chosen_grid_edges": _edges(pool[win]["params"]),
            "argmin_brier_cal": pool[0]["brier_cal"], "argmin_grid_edges": _edges(pool[0]["params"]),
            "chosen_median_boot_rank": round(float(med_rank[win]) + 1, 2),
            "argmin_median_boot_rank": round(float(med_rank[0]) + 1, 2),
            "argmin_pbo_outside_top_half": round(pbo, 3),
        },
    }


def run(conn=None, *, sweeps: int = 30) -> dict:
    """RETIRED — see the module docstring. Refuses rather than returning a stub:
    ops/refresh_all.py --with-sweep does `_write("param_sweep.json", run(conn))`, so
    raising prints the pointer and leaves any existing file alone, where a stub
    would have overwritten it."""
    raise RuntimeError(_RETIRED)


def _run_wc_grid(conn=None, *, sweeps: int = 30) -> dict:
    """The retired World-Cup grid, kept verbatim as the historical reference."""
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior

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
    # Robust pick (what production adopts): NOT the raw argmin, but the bootstrap-stable set
    # within the top-1% elite pool — guards against the multiple-comparisons overfit of argmin.
    sel = robust_select(results, settled, prior, idx=idx, sweeps=sweeps) if results else None
    selected = sel["chosen"] if sel else best

    from datetime import datetime, timezone
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),   # "last updated" stamp
        "n_settled": n,
        "n_param_sets": len(combos),
        "grid": GRID,
        "uniform_brier": uni,
        "baseline": {"brier": round(baseline["brier"], 4), "brier_cal": round(baseline["brier_cal"], 4),
                     "log_loss": round(baseline["log_loss"], 4), "acc": round(baseline["acc"], 3)},
        "best": best,            # raw argmin (lowest calibrated Brier) — reference only
        "selected": selected,    # robust pick that PRODUCTION adopts (bootstrap-stable elite)
        "selection": (sel["diagnostics"] if sel else None),
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
    """RETIRED — leave-one-out over the same wrong (global, merged-prior) model."""
    raise RuntimeError(_RETIRED)


def _loocv_wc(conn=None, *, sweeps: int = 25) -> dict:
    """Leave-one-out: for each held-out match, pick the min-Brier params on the
    rest, score the held-out one. Honest generalization estimate vs in-sample."""
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior

    conn = conn or store.init_db()
    prior = load_prior()
    settled = load_settled(conn)
    keys = list(GRID)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*[GRID[k] for k in keys])]

    sel_probs, sel_outs, base_probs = [], [], []
    from prediction_market_soccer.model.match_pricing import price_match
    from prediction_market_soccer.model.strength import build_strength
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
    """Honest PIT walk-forward for the alt-data λ weights (plan 19) — the ONE tool in
    this retired module that is still live and still club-correct.

    It evaluates each FIXED weight candidate on the chronological OUT-OF-SAMPLE tail
    with strictly point-in-time features (ratings / form / alt-data cut at each match's
    kickoff). It does NOT fit the weight per step — at small N that overfits (proven);
    it asks "does this fixed small prior generalise on matches it never saw?".

    Three club corrections vs the WC original, all of which silently falsified it:

      * PER-LEAGUE models via ops.performance_report._pit_strength (memoised per
        kickoff-day × comp) instead of one merged-prior global model rebuilt inside
        the match loop — the WC version priced a model production never quotes, and
        at 399 clubs × 4,050 matches could not have finished.
      * The traded market is the 90-MINUTE 3-way for BOTH stages, so pricing is
        ``knockout=False, host_neutral=is_knockout(round)`` — the WC version passed
        ``knockout=True`` on cup rounds, which scales λ and prices advancement, not
        the contract that settles.
      * The candidates are forced to BITE. ``StrengthModel._adj_lambdas`` prefers a
        competition's fitted alt-data weights (leagues.altdata_weights) over the
        ModelConfig globals these candidates set, so on a per-league model every
        candidate would have collapsed onto the same fitted numbers and the whole
        comparison would have read "no difference" for the wrong reason. Dropping
        ``comp`` (while pinning the per-comp base_mu/home_adv it supplied) hands the
        weight choice back to cfg without touching anything else.

    NOTE this is now a CHECK, not a fitter: ops/fit_altdata_weights.py fits these
    weights per competition on each competition's own history, which is what
    production uses. Use this to ask whether a proposed global prior would have held
    up out of sample, not to pick the numbers that ship.
    """
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.model.altdata_adjust import altdata_index
    from prediction_market_soccer.model.match_pricing import price_match
    from prediction_market_soccer.model.probability_calibration import apply_calibration, load_calibration
    from prediction_market_soccer.ops.param_select_club import load_matches
    from prediction_market_soccer.ops.performance_report import _pit_strength

    conn = conn or store.init_db()
    # One canonical settled-club-match set (season 2026, enabled comps), chronological.
    per_lg = load_matches(conn)
    matches = sorted(((ts, lg, hi, ai, y, ko) for lg, ms in per_lg.items()
                      for hi, ai, y, ts, ko in ms))
    cal = load_calibration()
    candidates = candidates or [
        {"label": "baseline", "oppadj_def_weight": 0.0, "oppadj_off_weight": 0.0},
        {"label": "oppadj-prior", "oppadj_def_weight": 0.10, "oppadj_off_weight": 0.15},
        {"label": "oppadj-off-only", "oppadj_def_weight": 0.0, "oppadj_off_weight": 0.18},
    ]
    res = []
    for c in candidates:
        cfg = replace(CONFIG.model, oppadj_def_weight=c["oppadj_def_weight"],
                      oppadj_off_weight=c["oppadj_off_weight"])
        adj_cache: dict = {}
        probs, outs = [], []
        for ts, lg, hi, ai, y, ko in matches[start:]:
            sm = _pit_strength(conn, ts, lg)
            if hi not in sm.ratings or ai not in sm.ratings:
                continue
            # cfg carries the candidate weights; comp=None so the fitted per-comp
            # weights don't shadow them, with the comp's λ constants pinned explicitly.
            sm = replace(sm, cfg=cfg, comp=None, base_mu=sm._mu, home_adv=sm._ha)
            if cfg.oppadj_def_weight or cfg.oppadj_off_weight:
                ck = (ts[:10], lg)
                if ck not in adj_cache:
                    adj_cache[ck] = altdata_index(conn, sm.ratings, as_of=ts)
                sm = replace(sm, adj=adj_cache[ck])
            mp = price_match(sm, hi, ai, knockout=False, host_neutral=ko)
            probs.append(apply_calibration([mp.p_home, mp.p_draw, mp.p_away], cal, knockout=False))
            outs.append(y)
        s = _score(probs, outs)
        res.append({**dict(c), "brier_cal": round(s["brier"], 4),
                    "acc": round(s["acc"], 3), "n": s["n"]})
    return {"n_oos": max(0, len(matches) - start), "start": start, "candidates": res,
            "note": ("Honest PIT walk-forward over the OOS tail (features cut at each kickoff), "
                     "per-league models, 90-minute 3-way. Alt-data weights are a FIXED bounded "
                     "prior here, NOT fit per-step (small-N fitting overfits). Production fits "
                     "them PER COMPETITION in ops/fit_altdata_weights.py — this only asks whether "
                     "a proposed global prior would have generalised.")}


def main() -> None:
    ap = argparse.ArgumentParser(description="RETIRED — see ops/param_select_club.py")
    ap.add_argument("--walk-forward", action="store_true", help="honest PIT walk-forward of the alt-data λ weights")
    ap.add_argument("--sweeps", type=int, default=30, help="coordinate-descent sweeps per rating fit")
    args = ap.parse_args()

    if args.walk_forward:
        wf = walk_forward()
        print(f"PIT WALK-FORWARD — {wf['n_oos']} OOS matches (features cut at each kickoff)")
        for c in wf["candidates"]:
            print(f"  {c['label']:16} Brier_cal {c['brier_cal']}  acc {c['acc']}  "
                  f"(def={c['oppadj_def_weight']} off={c['oppadj_off_weight']})")
        return

    # The default path used to write param_sweep.json AND param_selected.json, the
    # file config._apply_selected_params() auto-loads. It now writes NOTHING: touching
    # data/output/.enable_sweep (ops/refresh_and_deploy.sh step 4) can no longer put a
    # global-model parameter set into production behind anyone's back.
    print("param_sweep: RETIRED, nothing written.")
    print(f"  {_RETIRED}")
    print("  ops/refresh_and_deploy.sh step 4 should point at param_select_club;"
          " until it does, .enable_sweep is a no-op.")


if __name__ == "__main__":
    main()
