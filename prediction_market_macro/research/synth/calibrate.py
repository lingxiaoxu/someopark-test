"""Calibrate lambda — what one synthetic observation is worth, in real observations (S5).

`param_argmin.sample_cap` bounds the selection bias of an argmin over K candidates on n
events by `sqrt(2 ln K / n)`. The synthetic sample is allowed to enter that n only
discounted:

    n_eff = n_real + lambda * n_synth

and this module measures lambda rather than choosing it.

## What lambda has to mean

A synthetic sample does not reduce selection bias by being large. It reduces it only to the
extent that ranking candidates on synthetic events ranks them the same way real events do.
Write the synthetic per-candidate mean as

    m_synth[j] = mu[j] + b[j] + noise

where `mu[j]` is the candidate's true real-world expectation and `b[j]` is the part of the
synthetic world's verdict that is about the synthetic world rather than the real one. No
number of DFM paths shrinks `b`. So the ceiling on what the synthetic sample can be worth is
set by how much of the real ranking it recovers, and the natural measure of that is the
correlation across candidates between the synthetic and real improvement vectors.

`lambda = rho^2` is the standard attenuation factor for a noisy proxy: the fraction of the
real signal's variance the proxy carries. It is 1 when synthetic ranking reproduces real
ranking exactly and 0 when the two are unrelated.

## Why this estimate is deliberately biased low

`rho` is measured against `m_real`, which on a weekly series is a mean over ten events and
is therefore itself noisy. Noise in the reference attenuates a correlation. So the measured
`rho` is below the true agreement, `rho^2` more so, and lambda comes out conservative — the
direction an unverifiable quantity should err in. On top of that the reported lambda is the
LOWER end of a paired bootstrap interval, not the point estimate, per the plan's own rule.

Two known biases push the same way and are recorded rather than corrected (§5c): the
incumbent is more accurate on synthetic worlds than real ones (`mean|z_y|` 0.725 vs 0.964),
and the synthetic market is slightly less informed (`corr(z_m,z_y)` +0.500 vs +0.571). Both
make the synthetic world easier to trade than reality. They inflate the LEVEL of synthetic
PnL; lambda is estimated from the cross-candidate CORRELATION of improvements, which a
common level shift leaves alone — but a bias that interacts with a candidate's aggression
would not, and that is precisely what `rho < 1` is absorbing.

## The extrapolation this cannot avoid

Lambda is measurable only where a real sample exists to check against, which is the weekly
series (n_real 10-11). It is USED on the monthly ones (n_real 2-3), which are the whole
reason the gate binds. That is an extrapolation across cadence, it is not testable today,
and the honest mitigations are the two above: take the lower CI bound, and re-run this when
the monthly series have a sample of their own.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from prediction_market_macro.research import param_argmin as PA
from prediction_market_macro.research import pnl_score as ps
from prediction_market_macro.research.synth import book as B
from prediction_market_macro.research.synth import build as BD
from prediction_market_macro.util.periods import kalshi_period_to_key

UTC = timezone.utc


@dataclass
class SeriesLambda:
    series: str
    n_real: int
    n_synth: int
    k: int
    rho: float
    lam_point: float
    lam_lo: float
    lam_hi: float
    real_improve_of_synth_pick: float
    real_improve_oracle: float
    default_real: float
    pick_percentile: float = 0.5
    rel_real: float = 0.0
    rel_synth: float = 0.0
    detail: dict = field(default_factory=dict)


def _means(mat: list[list[float]]) -> np.ndarray:
    return np.asarray(mat, dtype=float).mean(axis=0)


def agreement(mat_real: list[list[float]], mat_synth: list[list[float]],
              seed: int = 0, reps: int = 2000) -> dict:
    """rho between the real and synthetic per-candidate IMPROVEMENT vectors, bootstrapped.

    Improvement, not level: every entry is measured against the default set at index 0, so a
    synthetic world that is uniformly more profitable than reality — which §5c measured this
    one to be — does not register as disagreement. What registers is a candidate the
    synthetic world likes and the real one does not.

    The bootstrap resamples EVENTS on each side independently, because the two samples are
    not paired: they are different events in different worlds. Resampling candidates instead
    would be answering a different question (how stable is rho across this grid) and would
    understate the uncertainty that actually matters here, which comes from ten real events.
    """
    R = np.asarray(mat_real, dtype=float)
    S = np.asarray(mat_synth, dtype=float)
    if R.shape[1] != S.shape[1]:
        raise ValueError(f"agreement: grid width differs, real {R.shape[1]} vs synthetic "
                         f"{S.shape[1]} — the two matrices must score the SAME grid in the "
                         "same order or the correlation is between unrelated candidates")
    if R.shape[1] < 3:
        raise ValueError("agreement: fewer than 3 candidates — a correlation across "
                         "candidates is not defined on a grid this narrow")

    def _rho(r: np.ndarray, s: np.ndarray) -> float:
        a = r.mean(axis=0) - r.mean(axis=0)[0]
        b = s.mean(axis=0) - s.mean(axis=0)[0]
        if a.std() == 0 or b.std() == 0:
            # every candidate ties on one side. That is real information — it means the
            # sample cannot tell them apart — and it maps to zero agreement, not to nan.
            return 0.0
        return float(np.corrcoef(a, b)[0, 1])

    point = _rho(R, S)
    rng = np.random.default_rng(seed)
    boot = np.array([
        _rho(R[rng.integers(len(R), size=len(R))], S[rng.integers(len(S), size=len(S))])
        for _ in range(reps)])
    lam = np.clip(boot, 0.0, None) ** 2
    rel_r, rel_s = reliability(R, seed=seed), reliability(S, seed=seed)
    return {"rho": point, "rho_lo": float(np.percentile(boot, 5)),
            "rho_hi": float(np.percentile(boot, 95)),
            "lam_point": float(max(point, 0.0) ** 2),
            "lam_lo": float(np.percentile(lam, 5)),
            "lam_hi": float(np.percentile(lam, 95)),
            "rel_real": rel_r, "rel_synth": rel_s,
            "rho_disattenuated": (float(point / (rel_r * rel_s) ** 0.5)
                                  if rel_r > 0 and rel_s > 0 else None),
            "identified": rel_r > 0 and rel_s > 0}


def reliability(mat, seed: int = 0, reps: int = 400) -> float:
    """How well an improvement vector correlates with ITSELF, at this sample size.

    Split the events in half at random, build the improvement vector on each half, correlate
    the two, and step the result back up to full length with Spearman-Brown
    (`r_full = 2*r_hh / (1 + r_hh)`). This is the quantity that decides whether `rho` means
    anything: `rho` is bounded above by `sqrt(rel_real * rel_synth)`, so a reference with
    zero reliability forces `rho` to zero no matter how good the synthetic sample is.

    Measured 2026-08-21 on the three weekly markets, and it is the reason the lambda headline
    cannot be read as a refutation: KXJOBLESSCLAIMS real **+0.439** (on 4 events), KXWTIW
    **−0.272**, KXNATGASW **−0.561**. On two of three the real reference does not correlate
    with itself, so `lambda = 0` there is non-identification rather than disagreement — and it
    independently indicts running a 21-set argmin on those two markets at all. KXNATGASW's
    SYNTHETIC side is **−0.522** on 104 events, which says its scoring is noise-dominated on
    both sides and no amount of generation would fix it.

    A negative value is returned as measured, not clipped. It means "worse than chance at
    reproducing itself", which is information, and clipping it to zero would make an
    unidentified measurement look merely weak.
    """
    M = np.asarray(mat, dtype=float)
    n = len(M)
    if n < 4:
        return 0.0
    rng = np.random.default_rng(seed)
    rs = []
    for _ in range(reps):
        p = rng.permutation(n)
        half = n // 2
        a = M[p[:half]].mean(axis=0)
        b = M[p[half:2 * half]].mean(axis=0)
        a, b = a - a[0], b - b[0]
        if a.std() and b.std():
            rs.append(np.corrcoef(a, b)[0, 1])
    if not rs:
        return 0.0
    hh = float(np.median(rs))
    return 2 * hh / (1 + hh) if hh > -1 else -1.0


def pick_percentile(mat_real, mat_synth) -> dict:
    """Where the SYNTHETIC argmin's real improvement lands among all candidates' real ones.

    The one statistic here that needs no reliable reference, and therefore the only one that
    survives the finding above. It asks the decision-level question directly: take the set the
    synthetic sample would have adopted, and ask how it actually did. Under the null that the
    synthetic sample knows nothing, the percentile is uniform on [0, 1] and its mean is 50%.

    Measured 2026-08-21: KXJOBLESSCLAIMS **86.8%**, KXWTIW **19.0%**, KXNATGASW **28.6%** —
    mean **44.8%**, and on two of three the synthetic pick was WORSE than doing nothing. That
    is the finding that sets lambda to zero, and unlike rho it cannot be explained away by an
    unreliable reference.
    """
    mr, ms = _means(mat_real), _means(mat_synth)
    mr, ms = mr - mr[0], ms - ms[0]
    j = int(np.argmax(ms))
    return {"pick_idx": j, "pick_real_improve": float(mr[j]),
            "oracle_real_improve": float(mr.max()),
            "percentile": float((mr < mr[j]).mean()),
            "beats_default": bool(mr[j] > 0)}


def run(src: sqlite3.Connection, series: str, now: datetime, *,
        donors: list[B.Donor], out_dir: Path | str, n_paths: int = 8,
        seed: int = 0, log=None) -> SeriesLambda | None:
    """Measure lambda for one series: real 75-day window vs synthetic worlds spliced at it.

    The cutoff is the START of the real window. That is the point of the whole arrangement:
    the generator is fitted on history the real evaluation events had not happened in, so the
    agreement measured here is between a forecast and an outcome rather than between two
    readings of the same data.
    """
    say = log or (lambda *_a, **_k: None)
    lo = now - timedelta(days=PA.WINDOW_DAYS)
    uni = [{**e, "key": kalshi_period_to_key(e["tok"])}
           for e in ps.quotable_events(src, series, before=now)
           if e["close_ts"] >= lo]
    uni = [e for e in uni if e["key"]]
    if not uni:
        say(f"{series}: no real events in the window — nothing to calibrate against")
        return None

    # The UNCAPPED production grid. Capping it by the real sample here would measure lambda
    # on the narrow search the gate already permits, when the question lambda answers is
    # whether a WIDER one is supportable.
    grid, grep = PA.build(src, series, lo, n_events=None)
    if len(grid) < 3:
        say(f"{series}: grid width {len(grid)} — too narrow to correlate")
        return None
    say(f"{series}: {len(uni)} real events, grid {len(grid)} sets ({grep.get('live')})")

    kept_r, mat_r, _ = ps.score_matrix(src, series, grid, uni, log=say)
    if len(kept_r) < 3:
        say(f"{series}: only {len(kept_r)} real events survive the all-sets keep rule")
        return None

    built = BD.build(src, series, lo, donors=donors, out_dir=Path(out_dir) / series,
                     n_paths=n_paths, seed=seed, log=say)
    say(f"  coverage {built.coverage}")
    kept_s, mat_s = BD.score_matrix(built.events, grid, log=say)
    if len(kept_s) < 3:
        say(f"{series}: only {len(kept_s)} synthetic events survive the keep rule")
        return None

    ag = agreement(mat_r, mat_s, seed=seed)
    pp = pick_percentile(mat_r, mat_s)
    mr, msy = _means(mat_r), _means(mat_s)
    pick = int(np.argmax(msy))
    oracle = int(np.argmax(mr))
    say(f"  rho {ag['rho']:+.3f} [{ag['rho_lo']:+.3f}, {ag['rho_hi']:+.3f}]  "
        f"lambda {ag['lam_point']:.3f} [{ag['lam_lo']:.3f}, {ag['lam_hi']:.3f}]")
    say(f"  reliability real {ag['rel_real']:+.3f} synth {ag['rel_synth']:+.3f}"
        f"{'' if ag['identified'] else '  <- rho NOT identified, not refuted'}")
    say(f"  synth pick lands at {pp['percentile']:.1%} of the real improvement "
        f"distribution (null 50%), {'beats' if pp['beats_default'] else 'LOSES TO'}"
        " the default")
    return SeriesLambda(
        series=series, n_real=len(kept_r), n_synth=len(kept_s), k=len(grid),
        rho=ag["rho"], lam_point=ag["lam_point"], lam_lo=ag["lam_lo"],
        lam_hi=ag["lam_hi"],
        real_improve_of_synth_pick=float(mr[pick] - mr[0]),
        real_improve_oracle=float(mr[oracle] - mr[0]),
        default_real=float(mr[0]),
        pick_percentile=pp["percentile"],
        rel_real=ag["rel_real"], rel_synth=ag["rel_synth"],
        detail={"grid_report": grep, "coverage": built.coverage,
                "rho_disattenuated": ag["rho_disattenuated"],
                "identified": ag["identified"],
                "synth_pick_idx": pick, "oracle_idx": oracle,
                "synth_pick_params": grid[pick], "oracle_params": grid[oracle],
                "splice": built.splice.isoformat(), "meta": built.meta})


def pool(results: list[SeriesLambda]) -> dict:
    """One lambda for the board, from the per-series measurements.

    The MINIMUM of the per-series lower bounds, not their average. Lambda is applied to
    monthly series that were never part of the measurement, so the aggregation has to
    survive the series that agreed least — averaging would let a series where the synthetic
    world happens to rank well pay for one where it does not, on markets that resemble
    neither.

    The min rule is degenerate by construction — adding series can only lower it — so the
    bootstrapped cross-series MEAN is reported alongside as `lambda_mean_rule`, to keep the
    difference between "the evidence is against it" and "the rule cannot go up" visible. On
    the 2026-08-21 measurement the two agree: mean rho +0.166 with a 5th percentile of
    **−0.095** and P(mean rho <= 0) = 0.20, so the fairer rule also lands on zero.

    `mean_pick_percentile` is the number to read first. It is the only figure here that does
    not depend on the real reference being reliable, and on two of three series that
    reference is not (see `reliability`). Measured **44.8%** against a null of 50%.
    """
    if not results:
        return {"lambda": 0.0, "n_series": 0, "note": "no series measured"}
    lo = min(r.lam_lo for r in results)
    pcts = [r.pick_percentile for r in results]
    ident = [r for r in results if r.rel_real > 0 and r.rel_synth > 0]
    return {"lambda": float(lo), "n_series": len(results),
            "mean_pick_percentile": round(float(np.mean(pcts)), 3),
            "n_beating_default": sum(1 for r in results
                                     if r.real_improve_of_synth_pick > 0),
            "n_identified": len(ident),
            "lambda_mean_rule": round(float(max(np.mean(
                [r.lam_lo for r in results]), 0.0)), 4),
            "per_series": {r.series: {"rho": round(r.rho, 3),
                                      "lam_point": round(r.lam_point, 3),
                                      "lam_lo": round(r.lam_lo, 3),
                                      "lam_hi": round(r.lam_hi, 3),
                                      "pick_pct": round(r.pick_percentile, 3),
                                      "rel_real": round(r.rel_real, 3),
                                      "rel_synth": round(r.rel_synth, 3),
                                      "n_real": r.n_real, "n_synth": r.n_synth,
                                      "k": r.k}
                           for r in results},
            "note": ("min of per-series 5th-percentile bootstrap bounds; read "
                     "mean_pick_percentile first — it needs no reliable reference")}


def _disattenuated_lam(r: SeriesLambda) -> float | None:
    """`(rho / sqrt(rel_real * rel_synth))^2`, clipped into [0, 1] — or None if unidentified.

    The standard errors-in-variables correction: the measured `rho` is attenuated by noise
    in BOTH improvement vectors, and dividing by the geometric mean of their reliabilities
    estimates what the correlation would have been against noiseless references. Only
    defined when both reliabilities are positive; when either is not, the series is
    unidentified and has no business contributing a number at all.
    """
    if r.rel_real <= 0 or r.rel_synth <= 0:
        return None
    rho_d = r.rho / (r.rel_real * r.rel_synth) ** 0.5
    return float(min(max(rho_d, 0.0), 1.0) ** 2)


def persist(conn, results: list[SeriesLambda], *, now: datetime, log=None,
            pooled: bool = True) -> dict:
    """Write the measurement into `synth_lambda` — the step whose absence was §7c.

    Everything upstream of this function existed and ran (worlds generated weekly, scores
    stored, the daily lane reading them) while the lane refused every market every day,
    because `calibrate` computed lambda and nothing ever persisted it. This closes that
    switch, and it does so with the basis of every number written on the row's face, because
    the daily log quotes these rows and a pre-registered value read as a measured one would
    poison every later reading of that log.

    ## Per-series rows: the committed rule, even when it writes zero

    Each measured series gets a row at `lam = max(lam_lo, 0)` — the LOWER end of the
    bootstrap interval, which is the rule §6 registered before any number existed. On the
    2026-08-21 sample that is 0.0 for all three weekly series, and those zeros are written
    anyway: a per-series row is preferred by `synth_lambda()` over the pooled one, and a
    weekly series with an unidentified reference SHOULD refuse a synthetic sample it cannot
    price. Suppressing the zeros would let the pooled row apply to series the measurement
    explicitly declined to vouch for.

    ## The pooled '*' row: identified series only, measured before pre-registered

    The '*' row is what the monthly markets read — they have no measurement of their own,
    which is the §6 extrapolation. Three-step policy, first that produces a positive number
    wins, `basis` recorded in `detail_json`:

    1. **measured**: `min(lam_lo)` over IDENTIFIED series. The original pool rule, restricted
       to series whose reference can correlate with itself. Unidentified series are excluded
       not to flatter the result but because their `lam_lo = 0` is an artifact of a broken
       reference (rho is bounded by `sqrt(rel_real*rel_synth)`), and an artifact zero in a
       min() silently converts "no evidence" into "evidence of nothing".
    2. **preregistered**: `min` over identified series of the squared DISATTENUATED rho.
       Reached when every identified lower bound is 0 — which at n_real=4 is a property of
       the bootstrap, not of the generator (KXJOBLESSCLAIMS's synthetic pick lands at the
       86.8th percentile of real improvement and beats the default, and its lower bound is
       still 0). The point estimate is corrected for reference noise and labeled as what it
       is: a provisional exchange rate pending the walk-forward measurement on the monthly
       series themselves, which supersedes this row the day it lands.
    3. **none**: no identified series — nothing is written, and the lane keeps refusing.
       There is no floor value: a synthetic sample with no identified evidence anywhere has
       no claim to a weight, whatever the cost of having generated it.

    `lam_point`/`lam_lo`/`lam_hi` on the '*' row carry the sourcing series' numbers so the
    uncertainty band survives onto the row the daily log will quote.
    """
    say = log or (lambda *_a, **_k: None)
    ts = now.isoformat()
    for r in results:
        conn.execute(
            "INSERT OR REPLACE INTO synth_lambda(series, measured_ts, lam, lam_point,"
            " lam_lo, lam_hi, rho, n_real, n_synth, k, detail_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (r.series, ts, float(max(r.lam_lo, 0.0)), float(r.lam_point),
             float(r.lam_lo), float(r.lam_hi), float(r.rho), r.n_real, r.n_synth, r.k,
             json.dumps({"basis": "measured_lower_bound",
                         "rel_real": r.rel_real, "rel_synth": r.rel_synth,
                         "identified": r.rel_real > 0 and r.rel_synth > 0,
                         "pick_percentile": r.pick_percentile,
                         "real_improve_of_synth_pick": r.real_improve_of_synth_pick,
                         "real_improve_oracle": r.real_improve_oracle,
                         "disattenuated_lam": _disattenuated_lam(r)})))
        say(f"  synth_lambda[{r.series}] = {max(r.lam_lo, 0.0):.4f} "
            f"(lower bound; point {r.lam_point:.4f})")

    ident = [r for r in results if r.rel_real > 0 and r.rel_synth > 0]
    rep: dict = {"n_series": len(results), "n_identified": len(ident),
                 "per_series": {r.series: float(max(r.lam_lo, 0.0)) for r in results}}
    if not pooled:
        # A partial measurement (e.g. one series from the monthly walk-forward accrual)
        # may write ITS row, but must not recompute the board-wide '*' row from a set
        # that is not the board-wide measurement.
        rep["pooled"] = "skipped (partial measurement)"
        conn.commit()
        return rep
    if not ident:
        rep["pooled"] = None
        rep["note"] = ("no identified series — '*' not written, the lane keeps refusing; "
                       "this is absence of evidence, and it stays absent on the record")
        say("  synth_lambda['*']: NOT written — no identified series")
        conn.commit()
        return rep

    measured = min(r.lam_lo for r in ident)
    if measured > 0:
        lam, basis = float(measured), "measured_min_lo_identified"
        src = min(ident, key=lambda r: r.lam_lo)
    else:
        disatt = [(r, _disattenuated_lam(r)) for r in ident]
        lam = float(min(d for _, d in disatt))
        src = min(disatt, key=lambda t: t[1])[0]
        basis = "preregistered_disattenuated_point"
    if lam <= 0:
        rep["pooled"] = None
        rep["note"] = ("identified series exist but both the measured lower bound and the "
                       "disattenuated point are zero — '*' not written")
        say("  synth_lambda['*']: NOT written — identified evidence is zero either way")
        conn.commit()
        return rep

    conn.execute(
        "INSERT OR REPLACE INTO synth_lambda(series, measured_ts, lam, lam_point,"
        " lam_lo, lam_hi, rho, n_real, n_synth, k, detail_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("*", ts, lam, float(src.lam_point), float(src.lam_lo), float(src.lam_hi),
         float(src.rho), src.n_real, src.n_synth, src.k,
         json.dumps({"basis": basis,
                     "identified_series": [r.series for r in ident],
                     "sourced_from": src.series,
                     "sourced_rel_real": src.rel_real, "sourced_rel_synth": src.rel_synth,
                     "sourced_pick_percentile": src.pick_percentile,
                     "note": ("applies to monthly markets with no measurement of their "
                              "own (the §6 extrapolation); superseded by the walk-forward "
                              "monthly calibration when it lands")})))
    rep["pooled"] = lam
    rep["basis"] = basis
    rep["sourced_from"] = src.series
    say(f"  synth_lambda['*'] = {lam:.4f} ({basis}, from {src.series})")
    conn.commit()
    return rep


# ── S5-WF: the accruing monthly measurement ─────────────────────────────────
#
# The §6a plan line said "rolling cutoffs over ~2 years". The book says otherwise:
# candles begin 2026-05-16 and Kalshi deletes them at 75 days, so the real reference
# for any month before May 2026 does not exist and can never be recovered. That is a
# data-reality ceiling (same species as the 14% K-line coverage ceiling), not a bug.
# What IS possible is an accrual: every monthly release that settles adds one real
# improvement row per series, this job scores it point-in-time (generator spliced 75d
# before the release's close, exactly the S5 geometry) and stores the matrices; when a
# series has enough rows, its OWN lambda gets measured and persisted, superseding the
# pooled '*' row through `synth_lambda()`'s read order with no code change.
#
# Two honesty notes carried on every row's meta:
#   * the grid is TODAY's ladder union, not a grid designed at the historical cutoff —
#     the pre-cutoff probe window is empty before 2026-05 so a PIT grid design does not
#     exist. Key-LIVENESS therefore leaks backwards; outcomes never enter grid design.
#   * the donor book pools quotes from the whole recorded span, including post-release
#     ones — the same practice S5 measured under (§5b: the book moves with venue
#     liquidity, not with the macro state).

MONTHLY_WF_MIN_REAL = 3     # below this a cross-candidate correlation is not defined
MONTHLY_WF_MIN_IDENT = 4    # below this split-half reliability is not defined
MONTHLY_WF_MIN_N_FOR_ZERO = 8
# A measured per-series row SHADOWS the pooled '*' row, so writing one is only justified
# when it carries information. lam_lo > 0 always does. A ZERO lower bound does not until
# the sample is big enough that zero is evidence rather than the bootstrap floor — at
# n_real=4 the lower bound of ANY quantity is 0 (S5 measured exactly that on a series
# whose pick beat the default at the 86.8th percentile), and letting that artifact
# shadow a positive pooled prior would re-kill the feature on schedule every month.


def wf_targets() -> list[str]:
    from prediction_market_macro.research.synth import regen as RG
    return RG.targets()


def wf_accrue(conn, s, *, now: datetime | None = None, series: list[str] | None = None,
              n_paths: int = 8, seed: int = 0, log=None) -> dict:
    """Score every settled-but-unstored monthly release, PIT, and store the matrices.

    Idempotent and incremental: a release already in `synth_wf_mats` is never redone, so
    the weekly call pays ~10 minutes once a month per series when a new release lands and
    is a no-op every other week. Worlds are deleted after scoring — the matrices are the
    measurement, and 21 releases of kept worlds would be ~6 GB of reproducible files.
    """
    import shutil

    from prediction_market_macro.research import pnl_score as ps2
    from prediction_market_macro.research.synth import regen as RG
    from prediction_market_macro.research.synth import worlds as W

    say = log or (lambda *_a, **_k: None)
    now = now or datetime.now(UTC)
    root = Path(s.db_path).parent / "synth_wf"
    root.mkdir(parents=True, exist_ok=True)
    lo_today = now - timedelta(days=PA.WINDOW_DAYS)
    out: dict[str, list[str]] = {}
    snap = None
    src = None
    try:
        for name in (series or wf_targets()):
            done = {r[0] for r in conn.execute(
                "SELECT release_tok FROM synth_wf_mats WHERE series=?", (name,))}
            todo = [e for e in ps2.quotable_events(conn, name, before=now)
                    if e["tok"] not in done]
            if not todo:
                out[name] = []
                continue
            if src is None:                       # snapshot lazily, once, like regen.run
                snap = W.snapshot(s.db_path, root / "snapshot.db")
                src = sqlite3.connect(snap)
                src.row_factory = sqlite3.Row
            book = RG.donors(src, Path(s.db_path).parent / "synth", now=now, log=say)
            _, union = PA.grid_ladder(conn, name, lo_today)
            if len(union) < 3:
                out[name] = [f"grid too narrow ({len(union)}) — nothing to correlate"]
                continue
            stored = []
            for e in todo:
                cutoff = e["close_ts"] - timedelta(days=PA.WINDOW_DAYS)
                say(f"{name} {e['tok']}: cutoff {cutoff.date()} (close {e['close_ts'].date()})")
                out_dir = root / name / e["tok"]
                try:
                    built = BD.build(src, name, cutoff, donors=book, out_dir=out_dir,
                                     n_paths=n_paths, seed=seed, log=say)
                    kept_s, mat_s = BD.score_matrix(built.events, union, log=say)
                    kept_r, mat_r, _ = ps2.score_matrix(conn, name, union, [e], log=say)
                finally:
                    shutil.rmtree(out_dir, ignore_errors=True)
                conn.execute(
                    "INSERT OR REPLACE INTO synth_wf_mats(series, release_tok, cutoff_ts,"
                    " built_ts, grid_json, grid_hash, real_json, synth_json, meta_json)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (name, e["tok"], cutoff.isoformat(), now.isoformat(),
                     json.dumps(union, sort_keys=True), PA.grid_hash(union),
                     json.dumps(mat_r), json.dumps(mat_s),
                     json.dumps({"n_synth_kept": len(kept_s),
                                 "n_synth_generated": built.n_synth,
                                 "real_scored": len(kept_r),
                                 "coverage": built.coverage,
                                 "grid_note": "today's ladder union — key-liveness leaks "
                                              "backwards, outcomes never enter design",
                                 "close_ts": e["close_ts"].isoformat()}, default=str)))
                conn.commit()
                stored.append(f"{e['tok']}: real {len(kept_r)} synth {len(kept_s)}")
                say(f"  {name} {e['tok']}: stored (real {len(kept_r)} rows, "
                    f"synth {len(kept_s)} rows x {len(union)})")
            out[name] = stored
    finally:
        if src is not None:
            src.close()
    return out


def wf_aggregate(conn, series: str, *, now: datetime | None = None,
                 log=None) -> dict:
    """Pool a series' stored release matrices; measure and PERSIST its lambda when warranted.

    Candidates are intersected by `set_hash` across releases (grids drift when live_keys
    move), keeping the newest release's order with the default first. The persistence gate
    is asymmetric on purpose (see MONTHLY_WF_MIN_N_FOR_ZERO): positive evidence persists
    at n>=MONTHLY_WF_MIN_IDENT, a zero only once it means something.
    """
    say = log or (lambda *_a, **_k: None)
    now = now or datetime.now(UTC)
    rows = conn.execute("SELECT * FROM synth_wf_mats WHERE series=?"
                        " ORDER BY release_tok", (series,)).fetchall()
    rep: dict = {"series": series, "n_releases": len(rows)}
    if not rows:
        rep["status"] = "no releases stored yet"
        return rep
    grids = [json.loads(r["grid_json"]) for r in rows]
    hashes = [[PA.set_hash(p) for p in g] for g in grids]
    common = set(hashes[0]).intersection(*hashes[1:]) if len(hashes) > 1 else set(hashes[0])
    newest = hashes[-1]
    keep = [h for h in newest if h in common]
    default_h = PA.set_hash({})
    if default_h in keep:                       # default must be column 0 — it is the
        keep = [default_h] + [h for h in keep if h != default_h]   # improvement baseline
    rep["k_common"] = len(keep)
    if len(keep) < 3:
        rep["status"] = f"only {len(keep)} candidates survive the grid intersection"
        return rep
    mat_r, mat_s = [], []
    for r, hs in zip(rows, hashes):
        idx = {h: j for j, h in enumerate(hs)}
        cols = [idx[h] for h in keep]
        for row in json.loads(r["real_json"]):
            mat_r.append([row[j] for j in cols])
        for row in json.loads(r["synth_json"]):
            mat_s.append([row[j] for j in cols])
    rep["n_real"] = len(mat_r)
    rep["n_synth"] = len(mat_s)
    if len(mat_r) < MONTHLY_WF_MIN_REAL or len(mat_s) < MONTHLY_WF_MIN_REAL:
        rep["status"] = (f"accruing — {len(mat_r)} real rows, need "
                         f">={MONTHLY_WF_MIN_REAL} to correlate")
        return rep
    ag = agreement(mat_r, mat_s, seed=0)
    pp = mat_s and pick_percentile(mat_r, mat_s)
    rep.update({"rho": round(ag["rho"], 4), "lam_point": round(ag["lam_point"], 4),
                "lam_lo": round(ag["lam_lo"], 4), "lam_hi": round(ag["lam_hi"], 4),
                "rel_real": round(ag["rel_real"], 4),
                "rel_synth": round(ag["rel_synth"], 4),
                "identified": ag["identified"],
                "pick_percentile": round(pp["percentile"], 4) if pp else None})
    if len(mat_r) < MONTHLY_WF_MIN_IDENT:
        rep["status"] = (f"measured but unidentifiable — reliability needs "
                         f">={MONTHLY_WF_MIN_IDENT} real rows")
        return rep
    if not ag["identified"]:
        rep["status"] = "unidentified — the real reference cannot correlate with itself"
        return rep
    informative = ag["lam_lo"] > 0 or len(mat_r) >= MONTHLY_WF_MIN_N_FOR_ZERO
    if not informative:
        rep["status"] = (f"identified, lower bound 0 at n={len(mat_r)} — the bootstrap "
                         f"floor, not evidence; not persisted until "
                         f"n>={MONTHLY_WF_MIN_N_FOR_ZERO} (would shadow '*')")
        return rep
    sl = SeriesLambda(
        series=series, n_real=len(mat_r), n_synth=len(mat_s), k=len(keep),
        rho=ag["rho"], lam_point=ag["lam_point"], lam_lo=ag["lam_lo"],
        lam_hi=ag["lam_hi"],
        real_improve_of_synth_pick=0.0, real_improve_oracle=0.0, default_real=0.0,
        pick_percentile=pp["percentile"] if pp else 0.5,
        rel_real=ag["rel_real"], rel_synth=ag["rel_synth"],
        detail={"source": "monthly_walkforward",
                "releases": [r["release_tok"] for r in rows]})
    persist(conn, [sl], now=now, log=say, pooled=False)
    rep["status"] = f"PERSISTED measured per-series row lam={max(ag['lam_lo'], 0.0):.4f}"
    return rep
