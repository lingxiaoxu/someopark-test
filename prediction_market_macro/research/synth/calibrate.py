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
