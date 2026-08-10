"""research/confidence.py — §25.3, the per-bet confidence model.

**This is not a fifteenth forecasting model.** Every other model in this package predicts
a macro number. This one predicts a property of OUR OWN BET: given everything knowable at
the moment the bet is placed, will it make money? The label is already in the data —
`realized > 0` on each simulated trade — so nothing new has to be measured, only learned.

The motivating evidence is #129 §4, which the user restated as a table:

    模型 ≪ 市场 (<= -10%)   ROI -16.54%
    一致                    ROI  -1.58%
    模型 ≫ 市场 (>= +10%)   ROI  -6.66%

Read it the way it actually reads: **all three buckets lose, and the least-bad one is
agreement**. That is not "we are picking the wrong side of the disagreement" — if the
disagreements carried information, one side would be positive. It is "the model's
disagreements are noise, and every disagreement costs a spread". Which is why this module
produces a GATE (do not bet) and not a mixing rule (which side to bet). `abs_div` therefore
enters with a NEGATIVE monotone constraint: the more we disagree, the less we should bet.

## What it may and may not do

It can only ever SUBTRACT bets. There is no branch that opens a position the existing
gates declined, and no branch that resizes one. The downside is bounded by "we skipped
something profitable"; there is no path to a new kind of loss. Same contract as
`strategy/series_enable.py` (§25.4), for the same reason.

## Why a constrained logistic regression and not something stronger

n is about 50-100 trades over the 75-day window. A gradient-boosted tree would find the
one GDP trade that made $5.03 and build a rule around it. So:

  * **Monotone sign constraints on every coefficient but one.** Each sign is fixed from a
    prior stated below, BEFORE fitting. A coefficient that wants the other sign gets
    clipped to zero — that is the model telling us the prior was wrong, and it shows up as
    a zero in the report rather than as a confident backwards rule.
  * **The one free coefficient is `is_argmax`.** That is deliberate: §25.6/#137 asks
    whether the argmax stream beats the edge stream, the 75-day evidence is strong but
    unstable across thirds, and this is the honest way to let the data answer inside a
    model that is cross-validated rather than eyeballed.
  * **L2 at unit scale on standardized features.** A unit-normal prior on standardized
    coefficients. Not tuned; tuning it on this window is the exact overfit §25.1 bans.

## Hierarchical, because "14 separate models" is not available

The user asked for one model per bet series. The data says the largest series has 11
tradeable events (§25.7), so fourteen independent fits would be fourteen memorisations.
The honest implementation of "per-series" at this n is a pooled model plus a per-series
INTERCEPT OFFSET, L2-penalised so that a series with few trades is pulled back to the
pooled fit. `SERIES_TAU` is set so the offset reaches half its unpenalised value at
`skill.MIN_PAIRED` = 6 observations — derived, not tuned; see `SERIES_TAU` below.

`fit_per_series()` fits the fourteen independent models anyway, and
`evaluate()` reports their in-sample-vs-LOEO gap. That number is the argument, not this
docstring.

## LOEO-forward, not K-fold

Validation is leave-one-event-out **and** forward: to score event E, the model is trained
only on trades that SETTLED strictly before E settled. This is the same contract
`pit_gates.asof` enforces, and for the same reason — a K-fold estimate here would train on
August to score June and report a number the live path can never achieve.

## The threshold is the product

Two thresholds live here, and PR-6 changed which one is the gate.

`p_star()` (PR-5's, now a **diagnostic only**) is the break-even win probability implied
by the payoff geometry of the TRAINING trades alone:

    E[roi] = p * w + (1 - p) * l  >= 0    =>    p >= -l / (w - l)

with `w` the mean roi of winning training trades and `l` that of losing ones. Every fold
computes its own from its own training slice. It is honest about *the average trade in the
fold* and that is precisely its defect: it is ONE scalar applied to bets whose break-even
points differ by 70 percentage points.

PR-5 was falsified, and the single located reason was that defect. A structure bought at
`cost` pays $1, so its own break-even is `p = cost` — winning returns `(1-c)/c`, losing
returns -1, and `p·(1-c)/c - (1-p) = 0` at `p = c` exactly. Comparing every bet against one
global `p_star` therefore does not gate on skill at all: it lets through whatever is
expensive and blocks whatever is cheap. PR-5 measured that degeneration directly —
`corr(p, cost) = +0.887`.

`bet_threshold()` (PR-6's, the gate) is that per-bet number: `cost + FEE_WEDGE`. **No new
constant is fitted here.** `cost` comes off the row, and `FEE_WEDGE = 0.026` is the same
measured round-trip net taker cost `series_enable.ON_ROI` already uses.

A consequence worth stating rather than special-casing: for `cost > 1 - FEE_WEDGE` the
threshold exceeds 1.0 and the bet is blocked unconditionally. That is right, not a bug — a
0.98 contract pays 0.02 if it wins, which cannot cover 0.026 of fees at any win rate.

## Failure criterion (pre-registered, §25.3f / PR-5 — do not edit after seeing results)

If the hierarchical gate under LOEO-forward cannot lift hybrid ROI from -23.5% to >= -5%,
or the blocked and allowed samples' realised ROI are not distinguishable (event-clustered
bootstrap CI on the difference crosses zero), this route is falsified and goes into the
TRADEABILITY_129 sequel. It does not get retried with different features.

**That criterion was applied and PR-5 FAILED it.** It belongs to `gate="global"`, which is
kept runnable so the falsification stays reproducible rather than becoming a claim about
code that no longer exists.

PR-6 (`gate="per_bet"`, the default) is registered at **K=2 with a forward-only verdict**:
30 hybrid trades after 2026-08-05, gate-on vs gate-off realised ROI differing by >= 5pp
with an event-clustered CI clear of zero. Every number this module prints on the 75-day
window is a SMOKE TEST, and `_verdict` refuses to print PASS or FALSIFIED for it. Reporting
a better ROI from re-thresholding the same 51/49 trades is exactly the move §25.1 rule 1
bans and exactly the one #128 already paid for.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# ── the feature table ────────────────────────────────────────────────────────────────
# (name, sign, prior)
#   sign  +1 / -1 constrain the coefficient's sign; 0 leaves it free.
# Every sign below is a PRIOR written before the fit. None of them is "whatever the data
# said". Where no defensible prior exists the feature is left out entirely rather than
# admitted with a free coefficient — free coefficients are the expensive kind at n=70.
FEATURES: tuple[tuple[str, int, str], ...] = (
    ("abs_div", 0,
     "|fair - cost|. FREE, and it was not always going to be. This coefficient carried a "
     "NEGATIVE prior until 2026-08-05, taken from the #129 §4 divergence table (agree "
     "-1.58%, tails -16.54%/-6.66%). That table was recomputed on the post-§25.2a data "
     "and it INVERTS: agree -53.08% (n=19), model<<market -1.99% (n=6), model>>market "
     "-5.31% (n=26). Worse, the axis is not clean at all — at cost 0.90 a divergence of "
     "+0.10 needs fair >= 1.00, so the 'agree' bucket structurally collects every "
     "expensive favourite AND every cheap longshot, i.e. exactly the two price regions "
     "where a single loss is -100% of stake. The table measures price level, not "
     "disagreement. With the motivating evidence withdrawn there is no defensible prior "
     "left, so this is freed rather than flipped. See PREREGISTER PR-5 amendment."),
    ("spread", -1,
     "median bid/ask width. A wider book is a worse fill and a less informative mid."),
    ("cost", +1,
     "what we pay. Nearly mechanical: an 0.85 contract wins about 85% of the time. It is "
     "in the model so the other coefficients are read NET of the favourite-longshot axis, "
     "not as proxies for it."),
    ("lead_days", -1,
     "days to settlement. Every model anchors on the latest print; beyond one refresh "
     "cycle it extrapolates blind (>7d entries went 0/4, which is why max_days_to_close "
     "exists)."),
    ("entropy_norm", -1,
     "H(pmf)/log K. Flat pmf = the model claims no information; decide()'s entropy gate "
     "already encodes this direction, this makes it continuous instead of a cliff."),
    ("skill_ratio", -1,
     "PIT Brier(model)/Brier(market) on this series. > 1 means the market forecasts it "
     "better than we do. The sign is definitional, not empirical."),
    ("ser_roi", +1,
     "the series' trailing-12 realised ROI, from §25.4's own fold. NOTE (#146, corrected "
     "2026-08-06): this comment used to say that a coefficient clipping to zero meant "
     "§25.4's premise — losing series keep losing — was unsupported. That reading is "
     "WRONG and it is retracted. The column is observed on 4 of 70 rows and every one of "
     "the four is POSITIVE, because a feature row is only written for a bet that was "
     "PLACED, and §25.4 disables a series exactly when this number goes <= 0. The column "
     "is truncated at the very sign boundary the premise is about, so the coefficient "
     "cannot test it either way. KXNATGASW shows both halves inside one series: roi "
     "-0.8370 on 06-27 disabled it and produced no rows, +0.0402 on 07-25 re-enabled it "
     "and produced four. The sign stays +1 — freeing it is not warranted by 4 informative "
     "rows against 66 median-imputed ones. See §25.17."),
    ("is_argmax", 0,
     "FREE, on purpose. This is §25.6/#137's question asked inside a cross-validated "
     "model instead of by eye on 21 conflicting events."),
)

FEATURE_NAMES = tuple(f[0] for f in FEATURES)
SIGNS = tuple(f[1] for f in FEATURES)

# L2 on the standardized slopes: a unit-normal prior. Not tuned.
RIDGE = 1.0

# L2 on the per-series intercept offsets. Chosen so the offset reaches HALF its
# unpenalised value at n_s = skill.MIN_PAIRED = 6 observations. Logistic Fisher
# information per observation is at most p(1-p) = 0.25, so the shrinkage factor is
# n_s * 0.25 / (n_s * 0.25 + 2 * tau); setting that to 1/2 at n_s = 6 gives tau = 0.75.
# Derived from an existing constant, not fitted to this window.
SERIES_TAU = 0.75

# Below this many training trades the model abstains rather than predicts. Same number as
# `series_enable.MIN_N` / `skill.MIN_PAIRED`, and the same reasoning: under it, a fitted
# probability is a restatement of two or three outcomes.
MIN_TRAIN = 20

# PR-6. One round-trip net taker cost on this book, measured, and the SAME number
# `strategy/series_enable.ON_ROI` is already set from — deliberately re-used rather than
# re-fitted, so PR-6 introduces no free parameter (see the module docstring).
FEE_WEDGE = 0.026

# The two gate settings. "global" is PR-5 as it was falsified, kept reproducible;
# "per_bet" is PR-6 and the default.
GATES = ("per_bet", "global")


# ── featurisation ────────────────────────────────────────────────────────────────────

def raw_features(row: dict) -> dict:
    """The eight numbers, straight off a `walkforward` feature row. Missing stays None."""
    return {
        "abs_div": row.get("abs_div"),
        "spread": row.get("spread"),
        "cost": row.get("cost"),
        "lead_days": row.get("lead_days"),
        "entropy_norm": row.get("entropy_norm"),
        "skill_ratio": row.get("skill_ratio"),
        "ser_roi": row.get("ser_roi"),
        "is_argmax": 1.0 if row.get("stream") == "argmax" else 0.0,
    }


@dataclass(frozen=True)
class Scaler:
    """Train-only imputation + standardisation.

    Missing values are filled with the training MEDIAN and no missingness indicator is
    added. The indicator would be the more informative choice with more data; at n=70 it
    costs a degree of freedom per column to encode a pattern (`skill_ratio` is None
    exactly while a series has fewer than 6 paired events) that `ser_n` already implies.
    """
    med: tuple[float, ...]
    mu: tuple[float, ...]
    sd: tuple[float, ...]

    @staticmethod
    def fit(rows: list[dict]) -> "Scaler":
        cols = [[] for _ in FEATURE_NAMES]
        for r in rows:
            f = raw_features(r)
            for i, n in enumerate(FEATURE_NAMES):
                v = f[n]
                if v is not None:
                    cols[i].append(float(v))
        med = tuple(float(np.median(c)) if c else 0.0 for c in cols)
        X = np.array([[med[i] if raw_features(r)[n] is None
                       else float(raw_features(r)[n])
                       for i, n in enumerate(FEATURE_NAMES)]
                      for r in rows], dtype=float)
        mu = X.mean(axis=0)
        sd = X.std(axis=0)
        # a constant column would divide by zero and, standardized, carries nothing —
        # give it scale 1 so its (centred, all-zero) values contribute exactly nothing
        sd = np.where(sd < 1e-9, 1.0, sd)
        return Scaler(med, tuple(map(float, mu)), tuple(map(float, sd)))

    def transform(self, rows: list[dict]) -> np.ndarray:
        out = np.empty((len(rows), len(FEATURE_NAMES)), dtype=float)
        for j, r in enumerate(rows):
            f = raw_features(r)
            for i, n in enumerate(FEATURE_NAMES):
                v = f[n]
                out[j, i] = self.med[i] if v is None else float(v)
        return (out - np.array(self.mu)) / np.array(self.sd)


# ── the fit ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Model:
    scaler: Scaler
    b0: float
    beta: tuple[float, ...]
    offsets: dict[str, float]        # per-series intercept offset (shrunk)
    p_star: float
    n_train: int

    def predict(self, rows: list[dict]) -> np.ndarray:
        X = self.scaler.transform(rows)
        z = self.b0 + X @ np.array(self.beta)
        z = z + np.array([self.offsets.get(r.get("series"), 0.0) for r in rows])
        return 1.0 / (1.0 + np.exp(-z))

    def coefficients(self) -> dict[str, float]:
        return {n: round(b, 4) for n, b in zip(FEATURE_NAMES, self.beta)}


def p_star(rows: list[dict]) -> float:
    """Break-even win probability from the TRAINING trades' own payoff geometry.

    E[roi] = p*w + (1-p)*l >= 0  =>  p >= -l / (w - l), with w the mean roi of winners and
    l that of losers. Degenerate slices (no winners, or no losers) fall back to 0.5 rather
    than to an extreme — an unbounded threshold on a fold with two trades in it would gate
    everything or nothing on an accident of ordering.
    """
    w = [r["realized"] / r["staked"] for r in rows
         if r.get("staked") and r["realized"] > 0]
    losses = [r["realized"] / r["staked"] for r in rows
              if r.get("staked") and r["realized"] <= 0]
    if not w or not losses:
        return 0.5
    mw, ml = float(np.mean(w)), float(np.mean(losses))
    if mw - ml <= 1e-9:
        return 0.5
    return float(min(max(-ml / (mw - ml), 0.0), 1.0))


def bet_threshold(row: dict, *, fee_wedge: float = FEE_WEDGE) -> float | None:
    """PR-6's per-bet break-even probability: `cost + fee_wedge`.

    `cost` is `Struct.fill_cost` — the NET debit for the whole structure, on the scale
    where the thing pays $1. That is what makes the algebra one line: win returns
    `(1-c)/c`, lose returns -1, and `p*(1-c)/c - (1-p) = 0` at `p = c`. The wedge is one
    round-trip's fees, so the bet has to clear its own cost AND the toll.

    Returns None when the row carries no cost, and every caller must read that as ABSTAIN
    (let the bet through) — this gate may only ever subtract, so it cannot block on a
    missing field. Note the threshold is NOT clipped to <= 1: for `cost > 1 - fee_wedge` an
    unreachable threshold is the correct answer, not a degenerate one.
    """
    c = row.get("cost")
    return None if c is None else float(c) + fee_wedge


def allows(row: dict, *, gate: str = "per_bet", fee_wedge: float = FEE_WEDGE) -> bool:
    """Does the gate let this scored row through?

    Three ways to be allowed, and all three are "the gate declined to act":
    an untrained fold (`p is None`), a row with no cost under `per_bet`, and an actual
    pass. `gate="global"` is PR-5's rule verbatim — `p >= p_star`, one scalar per fold —
    kept so its falsification stays reproducible.
    """
    if gate not in GATES:
        raise ValueError(f"gate must be one of {GATES}, got {gate!r}")
    p = row.get("p")
    if p is None:
        return True
    thr = row.get("p_star") if gate == "global" else bet_threshold(row, fee_wedge=fee_wedge)
    return True if thr is None else float(p) >= float(thr)


def _pearson(a: list[float], b: list[float]) -> float | None:
    """Correlation, or None when it is undefined — a constant column here is a degenerate
    sample, not a correlation of zero, and reporting 0.0 for it would read as evidence."""
    if len(a) < 3:
        return None
    x, y = np.array(a, dtype=float), np.array(b, dtype=float)
    if x.std() < 1e-12 or y.std() < 1e-12:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def _point_biserial(flags: list[bool], vals: list[float]) -> float | None:
    """corr(allow, cost) — PR-6's pre-registered diagnostic.

    PR-5's failure was that the gate correlated with PRICE (+0.887) instead of with skill.
    Note `corr(p, cost)` cannot move: only the threshold changed, not the model. What can
    move is the correlation of the DECISION with cost, which is what this measures.

    Computed over decided rows only. Abstentions are allowed unconditionally, so folding
    them in would report the abstention rate diluted into the diagnostic.
    """
    return _pearson([1.0 if f else 0.0 for f in flags], vals)


def fit(rows: list[dict], *, hierarchical: bool = True) -> Model | None:
    """Sign-constrained logistic regression, optionally with shrunk per-series intercepts.

    Solved with L-BFGS-B, whose box constraints ARE the monotone constraints: a
    coefficient with prior sign -1 gets bounds (-inf, 0]. On standardized features a sign
    constraint on the standardized slope is the same constraint on the raw slope, because
    standardisation only ever multiplies by a positive number.
    """
    from scipy.optimize import minimize
    rows = [r for r in rows if r.get("staked")]
    if len(rows) < MIN_TRAIN:
        return None
    y = np.array([1.0 if r["realized"] > 0 else 0.0 for r in rows])
    if y.min() == y.max():                     # one class only — nothing to separate
        return None
    sc = Scaler.fit(rows)
    X = sc.transform(rows)
    n, k = X.shape

    series = sorted({r["series"] for r in rows}) if hierarchical else []
    sidx = {s: i for i, s in enumerate(series)}
    S = np.zeros((n, len(series)))
    for j, r in enumerate(rows):
        if r["series"] in sidx:
            S[j, sidx[r["series"]]] = 1.0

    def unpack(theta):
        return theta[0], theta[1:1 + k], theta[1 + k:]

    def obj(theta):
        b0, beta, alpha = unpack(theta)
        z = b0 + X @ beta + (S @ alpha if len(series) else 0.0)
        # log-loss, stable form
        ll = float(np.sum(np.logaddexp(0.0, z) - y * z))
        pen = RIDGE * float(beta @ beta) + SERIES_TAU * float(alpha @ alpha)
        p = 1.0 / (1.0 + np.exp(-z))
        g0 = float(np.sum(p - y))
        gb = X.T @ (p - y) + 2.0 * RIDGE * beta
        ga = (S.T @ (p - y) + 2.0 * SERIES_TAU * alpha) if len(series) else np.zeros(0)
        return ll + pen, np.concatenate(([g0], gb, ga))

    bounds = [(None, None)]
    for s in SIGNS:
        bounds.append((None, None) if s == 0 else
                      ((0.0, None) if s > 0 else (None, 0.0)))
    bounds += [(None, None)] * len(series)
    res = minimize(obj, np.zeros(1 + k + len(series)), jac=True, method="L-BFGS-B",
                   bounds=bounds, options={"maxiter": 500})
    b0, beta, alpha = unpack(res.x)
    return Model(sc, float(b0), tuple(map(float, beta)),
                 {s: float(alpha[i]) for s, i in sidx.items()},
                 p_star(rows), len(rows))


def fit_per_series(rows: list[dict]) -> dict[str, Model]:
    """The literal "one model per bet" the user asked for, fitted independently.

    Kept as the CONTROL, not as a candidate. With 11 events at the largest series most of
    these will return None for want of `MIN_TRAIN`, and the ones that do fit are what the
    over-fitting gap in `evaluate()` is measured on.
    """
    out: dict[str, Model] = {}
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["series"], []).append(r)
    for s, rs in by.items():
        m = fit(rs, hierarchical=False)
        if m is not None:
            out[s] = m
    return out


# ── LOEO-forward validation ──────────────────────────────────────────────────────────

def _folds(rows: list[dict]):
    """Yield `(train, test)` for the LOEO-forward split, oldest fold first.

    Every trade settling on the SAME day lands in one fold and is scored by one model.
    Splitting within a settlement date would let a morning event's outcome inform an
    afternoon one, which the live path cannot do — settlements land in a batch.

    Factored out so `loeo_forward` and `coef_path` walk IDENTICAL boundaries. They are two
    readings of one validation, and two copies of this loop is how they would stop being
    that — the same failure mode as #141 and #144.
    """
    rows = sorted([r for r in rows if r.get("staked")], key=lambda r: r["settle"])
    i = 0
    while i < len(rows):
        j = i
        while j < len(rows) and rows[j]["settle"] == rows[i]["settle"]:
            j += 1
        yield rows[:i], rows[i:j]
        i = j


def loeo_forward(rows: list[dict], *, hierarchical: bool = True) -> list[dict]:
    """Score every trade with a model that saw only strictly-earlier SETTLEMENTS.

    Returns the input rows with `p` and `p_star` attached (both None when the fold had
    too little history to fit). A None `p` means ABSTAIN, and every consumer must read
    abstain as "let the bet through" — this gate may only ever subtract, so an untrained
    gate must subtract nothing.
    """
    out: list[dict] = []
    for train, test in _folds(rows):
        m = fit(train, hierarchical=hierarchical) if len(train) >= MIN_TRAIN else None
        p = m.predict(test) if m is not None else [None] * len(test)
        for r, pv in zip(test, p):
            out.append({**r, "p": None if pv is None else float(pv),
                        "p_star": None if m is None else m.p_star})
    return out


def coef_path(rows: list[dict], *, hierarchical: bool = True) -> dict:
    """Each feature's coefficient refitted at every LOEO-forward fold that could train.

    #137 is what this exists for. §25.6 measured the argmax stream beating the edge stream
    on 21 conflicting events and the task's own instruction was NOT to ship that flip but
    to "let §25.3 learn `stream` as a feature so the answer comes shrunk and LOEO-
    validated". `is_argmax` has been in `FEATURES` since #136 and it is shrunk — but the
    report only ever printed ONE full-sample number for it, which is not a validated
    answer, it is the same eyeball with a ridge penalty on it. A coefficient that is +0.4
    early and -0.4 late averages to a confident-looking nothing.

    Per feature: `n_folds` fits it was in, `first`/`last`/`mean`/`min`/`max`, `n_zero`
    (folds where it clipped to exactly zero — for a signed feature that is the prior being
    contradicted), and `n_sign_flips` between consecutive folds.

    The folds are NESTED and EXPANDING, so these are not independent draws and no interval
    is computed from them. That also governs how `n_sign_flips` reads: fold k trains on
    everything before it, so a late reversal the earlier data outweighs shows up as the
    coefficient DECAYING toward zero, not as a flip. `n_sign_flips == 0` therefore means
    "it never wanted the other sign at any training size" — which is worth knowing — and
    NOT "n independent folds agreed". Both behaviours are pinned in
    `tests/test_confidence.py`.
    """
    per: dict[str, list[float]] = {n: [] for n in FEATURE_NAMES}
    for train, _test in _folds(rows):
        if len(train) < MIN_TRAIN:
            continue
        m = fit(train, hierarchical=hierarchical)
        if m is None:
            continue
        for n, v in m.coefficients().items():
            per[n].append(v)
    out = {}
    for n, vals in per.items():
        if not vals:
            out[n] = {"n_folds": 0}
            continue
        out[n] = {"n_folds": len(vals), "first": round(vals[0], 4),
                  "last": round(vals[-1], 4),
                  "mean": round(sum(vals) / len(vals), 4),
                  "min": round(min(vals), 4), "max": round(max(vals), 4),
                  "n_zero": sum(1 for v in vals if v == 0.0),
                  "n_sign_flips": _sign_flips(vals)}
    return out


def _sign_flips(vals: list[float]) -> int:
    """Reversals in a coefficient path, comparing each value to the last NON-ZERO one.

    Zeros are stepped over rather than treated as a sign. A path that runs +1.6 → 0.0 →
    -1.2 has reversed once, and pairwise comparison would call it zero reversals — the
    exact case a constrained coefficient sitting on its bound produces, and the one
    `Model.coefficients`' rounding to 4dp produces for free on any path that crosses.
    Under-reporting a reversal is the failure that matters here: this number exists to
    stop an unstable coefficient reading as a stable one.
    """
    flips, last = 0, 0.0
    for v in vals:
        if v == 0.0:
            continue
        if last != 0.0 and (v > 0) != (last > 0):
            flips += 1
        last = v
    return flips


def _roi(rows) -> float | None:
    stk = sum(r["staked"] for r in rows)
    return None if stk <= 0 else sum(r["realized"] for r in rows) / stk


def _hybrid(rows: list[dict]) -> list[dict]:
    """The live rule: the edge bet where it fired, else the favourite.

    Rebuilt here rather than taken from `walkforward.streams` because the gate changes
    WHICH bets exist, and dropping an edge bet promotes that event's argmax bet exactly as
    it would in `decide_all` — the argmax leg is independent of the edge leg there.
    """
    edge = {(r["series"], r["period"]): r for r in rows if r["stream"] == "edge"}
    out = list(edge.values())
    out += [r for r in rows if r["stream"] == "argmax"
            and (r["series"], r["period"]) not in edge]
    return out


def bootstrap_diff(blocked: list[dict], allowed: list[dict], *, n: int = 2000,
                   seed: int = 0) -> dict:
    """Event-clustered bootstrap on ROI(allowed) - ROI(blocked).

    Clustered by (series, period): the two streams' bets on one event settle on the same
    number, so resampling trades independently would treat one outcome as two and report a
    CI about sqrt(2) too tight. `research/eval` learned this the expensive way (#129) —
    unclustered CIs there came out about 6x too tight.
    """
    if not blocked or not allowed:
        return {"n_blocked": len(blocked), "n_allowed": len(allowed), "ci": None}
    rng = np.random.default_rng(seed)
    cl: dict[tuple, list] = {}
    for r in blocked:
        cl.setdefault((r["series"], r["period"]), []).append(("b", r))
    for r in allowed:
        cl.setdefault((r["series"], r["period"]), []).append(("a", r))
    keys = list(cl)
    diffs = []
    for _ in range(n):
        pick = rng.integers(0, len(keys), len(keys))
        b, a = [], []
        for idx in pick:
            for tag, r in cl[keys[idx]]:
                (b if tag == "b" else a).append(r)
        rb, ra = _roi(b), _roi(a)
        if rb is not None and ra is not None:
            diffs.append(ra - rb)
    if not diffs:
        return {"n_blocked": len(blocked), "n_allowed": len(allowed), "ci": None}
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return {"n_blocked": len(blocked), "n_allowed": len(allowed),
            "roi_blocked": round(_roi(blocked), 5),
            "roi_allowed": round(_roi(allowed), 5),
            "ci": [round(float(lo), 5), round(float(hi), 5)],
            "crosses_zero": bool(lo <= 0.0 <= hi)}


CRITERION = {
    "global": ("§25.3f / PR-5: hierarchical gate must lift hybrid ROI to >= -5% under "
               "LOEO-forward AND separate blocked from allowed (clustered bootstrap CI "
               "not crossing zero). Both, or the route is falsified. ALREADY APPLIED — "
               "PR-5 failed it; this setting is kept only to keep that reproducible."),
    "per_bet": ("PR-6, K=2, FORWARD-ONLY: no number computed on this 75-day window "
                "judges the per-bet threshold, because the threshold was changed after "
                "seeing this window. The verdict comes from 30 hybrid trades placed "
                "after 2026-08-05: gate-on vs gate-off realised ROI differing by >= 5pp "
                "with an event-clustered 95% CI clear of zero. The pre-registered "
                "SMOKE-test expectation is only that corr(allow, cost) falls well below "
                "PR-5's +0.887, i.e. the gate stops being a price sieve."),
}


def evaluate(rows: list[dict], *, gate: str = "per_bet",
             fee_wedge: float = FEE_WEDGE) -> dict:
    """The whole §25.3 report in one dict. Reads the pre-registered criterion out loud.

    Nothing here picks a threshold, a feature set or a shrinkage from the results. If the
    verdict is negative the correct response is to record it, per §25.1 rule 1 and the
    PREREGISTER entry — this window is for discovery, and a route that fails its own
    pre-registered bar does not get re-tuned until it passes.

    `gate="global"` is PR-5 (`p >= p_star`) and it reproduces the falsified record
    verbatim. `gate="per_bet"` is PR-6 (`p >= cost + fee_wedge`) and is the default; its
    ROI figures on this window are SMOKE ONLY and `_verdict` will not grade them.
    """
    if gate not in GATES:
        raise ValueError(f"gate must be one of {GATES}, got {gate!r}")
    scored = loeo_forward(rows, hierarchical=True)
    n_abstain = sum(1 for r in scored if r["p"] is None)

    def _ok(r):
        return allows(r, gate=gate, fee_wedge=fee_wedge)
    allowed = [r for r in scored if _ok(r)]
    blocked = [r for r in scored if not _ok(r)]

    base_h, gated_h = _hybrid(scored), _hybrid(allowed)
    base_roi, gated_roi = _roi(base_h), _roi(gated_h)

    # PR-6's diagnostic. Restricted to rows the gate actually decided (`p is not None`)
    # and that carry a cost, which is the population `corr(p, cost) = +0.887` was measured
    # on. Reported for BOTH gates from the same scored rows, so the two are comparable.
    dec = [r for r in scored if r["p"] is not None and r.get("cost") is not None]
    corr = {g: _point_biserial([allows(r, gate=g, fee_wedge=fee_wedge) for r in dec],
                               [r["cost"] for r in dec]) for g in GATES}
    p_cost = _pearson([r["p"] for r in dec], [r["cost"] for r in dec])

    # the control: fourteen independent fits, in-sample vs LOEO-forward
    per = fit_per_series(rows)
    ins = {}
    for s, m in per.items():
        rs = [r for r in rows if r["series"] == s and r.get("staked")]
        pv = m.predict(rs)
        keep = [r for r, p in zip(rs, pv)
                if allows({**r, "p": float(p), "p_star": m.p_star},
                          gate=gate, fee_wedge=fee_wedge)]
        ins[s] = {"n": len(rs), "n_kept": len(keep), "roi_kept": _roi(keep),
                  "roi_all": _roi(rs)}
    per_loeo = loeo_forward(rows, hierarchical=False)
    per_allowed = [r for r in per_loeo if _ok(r)]

    full = fit(rows, hierarchical=True)
    sep = bootstrap_diff(blocked, allowed)
    return {
        "gate": gate,
        "fee_wedge": fee_wedge if gate == "per_bet" else None,
        "n_rows": len(rows), "n_scored": len(scored), "n_abstained": n_abstain,
        "n_blocked": len(blocked), "n_allowed": len(allowed),
        # how often PR-6's threshold is unreachable by construction (cost > 1 - wedge).
        # A 0.98 contract pays 0.02 if it wins and cannot cover 0.026 of fees, so this is
        # a correct block, but the count belongs in the report rather than in a footnote.
        "n_threshold_above_one": sum(
            1 for r in dec if (bet_threshold(r, fee_wedge=fee_wedge) or 0.0) > 1.0),
        "hybrid_roi_base": None if base_roi is None else round(base_roi, 5),
        "hybrid_roi_gated": None if gated_roi is None else round(gated_roi, 5),
        "hybrid_n_base": len(base_h), "hybrid_n_gated": len(gated_h),
        "lift_pp": (None if base_roi is None or gated_roi is None
                    else round((gated_roi - base_roi) * 100, 2)),
        "separation": sep,
        "corr_allow_cost": {g: None if v is None else round(v, 4)
                            for g, v in corr.items()},
        "corr_p_cost": None if p_cost is None else round(p_cost, 4),
        "corr_note": ("PR-5's located failure was corr(p, cost) = +0.887 — the global "
                      "threshold made the gate a price sieve. corr(p, cost) is unchanged "
                      "by PR-6 (same model, only the threshold moved); what PR-6 predicts "
                      "is a smaller corr(allow, cost)."),
        "coefficients_full_sample": None if full is None else full.coefficients(),
        # #137: the full-sample row above is ONE number per feature and cannot show a
        # coefficient that changed its mind halfway through the window. See `coef_path`.
        "coef_path": coef_path(rows),
        "series_offsets_full_sample": (None if full is None else
                                       {s: round(v, 4)
                                        for s, v in sorted(full.offsets.items())}),
        # PR-6 demoted this to a diagnostic: it is no longer the gate under "per_bet".
        "p_star_full_sample": None if full is None else round(full.p_star, 4),
        "per_series_control": {"n_fitted": len(per), "in_sample": ins,
                               "loeo_roi": _roi(_hybrid(per_allowed))},
        "criterion": CRITERION[gate],
        "verdict": _verdict(gated_roi, sep, gate=gate),
    }


def _verdict(gated_roi, sep, *, gate: str = "global") -> str:
    """PR-5's grade, or PR-6's refusal to grade.

    `per_bet` returns SMOKE ONLY unconditionally — never PASS, never FALSIFIED. The
    threshold was chosen after seeing this window's failure, so a good number here is
    evidence about the choosing, not about the rule. #128 is the precedent: a re-thresholded
    replay of the same trades once got reported as a result and it was wrong.
    """
    if gate == "per_bet":
        return ("SMOKE ONLY — PR-6 is judged forward, never on this window. Read "
                "corr_allow_cost, not the ROI." +
                ("" if gated_roi is None else f" (gated ROI here: {gated_roi:.2%})"))
    if gated_roi is None:
        return "no-data"
    ok_roi = gated_roi >= -0.05
    ok_sep = sep.get("ci") is not None and not sep.get("crosses_zero", True)
    if ok_roi and ok_sep:
        return "PASS — both pre-registered conditions met; proceed to forward test"
    parts = []
    if not ok_roi:
        parts.append(f"gated ROI {gated_roi:.2%} < -5%")
    if not ok_sep:
        parts.append("blocked vs allowed CI crosses zero" if sep.get("ci")
                     else "too few blocked/allowed trades to separate")
    return "FALSIFIED — " + "; ".join(parts)


def load_rows(conn, config_hash: str | None = None) -> list[dict]:
    """The newest `walkforward_features` row, or a specific run's."""
    import json
    if config_hash:
        r = conn.execute("SELECT metrics_json FROM experiments"
                         " WHERE name='walkforward_features' AND config_hash=?",
                         (config_hash,)).fetchone()
    else:
        r = conn.execute("SELECT metrics_json FROM experiments"
                         " WHERE name='walkforward_features'"
                         " ORDER BY created_ts DESC LIMIT 1").fetchone()
    if not r:
        return []
    return (json.loads(r["metrics_json"]) or {}).get("feature_rows") or []


def main():
    import argparse
    import json

    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-hash", default=None)
    ap.add_argument("--gate", default="per_bet", choices=list(GATES),
                    help="per_bet = PR-6 (p >= cost + fee wedge, forward-judged only); "
                         "global = PR-5 (p >= p_star), kept to reproduce its falsification")
    ap.add_argument("--both", action="store_true",
                    help="report both gates side by side on the same scored rows")
    a = ap.parse_args()
    conn = init_db(load_settings().db_path)
    rows = load_rows(conn, a.config_hash)
    if not rows:
        raise SystemExit("no walkforward_features row — run research.walkforward first")
    out = ({g: evaluate(rows, gate=g) for g in GATES} if a.both
           else evaluate(rows, gate=a.gate))
    print(json.dumps(out, ensure_ascii=False, indent=1, default=float))


if __name__ == "__main__":
    main()
