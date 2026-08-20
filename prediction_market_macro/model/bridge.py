"""model/bridge.py — MIDAS-style bridge nowcast, the ensemble's third source
(PLAN_EXTENSION §23.2-2; GDPNow methodology in miniature).

Idea: map already-ingested HIGH-frequency proxies onto the monthly target with an
Almon-weighted lag polynomial + robust regression — deliberately independent of the
production model's target-autoregressive structure, which is what makes it a genuinely
diverse ensemble member rather than a rebranding of the same signal. That independence
is the whole reason this file exists, and it is why no autoregressive term appears
below even though adding one measurably lowers standalone RMSE — see WHY NO AR TERM.

Supported (proxies already in fred_obs, zero new data feeds):
  KXCPI      GASREGW weekly level changes → headline MoM tilt
  KXPAYROLLS ICSA weekly claims, reference-week aligned → NFP change
  KXU3       ICSA 4-week trend → U3 level

§7-bis: SHADOW ONLY. shadow_run() writes preds with model_version=VERSION; decide_all's
production-model guard keeps these out of decisions until the adoption gate promotes
them. All reads via FeatureStore (PIT).


v0.2.0 — what was wrong with v0.1.0
-----------------------------------
v0.1.0 printed a KXPAYROLLS mean of 484,862 jobs against production's 74,967. A monthly
NFP print of +484k is not a forecast, it is a broken calibration. Four stacked defects,
in descending order of damage:

1. THE TARGET WAS NOT THE SETTLED QUANTITY. v0.1.0 built y as `first_prints.diff()` —
   the difference between two DIFFERENT vintages of PAYEMS. BLS computes the headline
   change from a single vintage: level(m) − level(m−1) both read from the release in
   which m first appears. Diffing across vintages folds the revision to m−1 into the
   target, and every January it folds in the annual BENCHMARK revision. For 2026-01
   that made y = −899k where BLS actually printed +130k. The model was being fit on a
   quantity Kalshi does not settle on. Fixed by `_published_changes`, which mirrors
   payrolls.printed_changes — the production model already did this correctly.

2. NO SAMPLE WINDOW. `_fit_bridge` regressed on every overlapping month back to 1967.
   That put 2020-04 (−20.5M jobs) in the sample with leverage h=0.434 against an
   average of 0.0028, and that single observation flipped the slope's sign: all-sample
   b=−2.239 (R²=0.345), drop that one month and b=+0.118 (R²=0.003). Fixed with a
   rolling window plus explicit COVID exclusion — see WINDOW LENGTH.

3. NON-STATIONARY REGRESSOR. A change in jobs was regressed on the LEVEL of claims.
   Fixed: the regressor is now the month-over-month change in log claims.

4. SIGMA WAS THE WRONG OBJECT. v0.1.0 emitted the in-sample residual std of a fit that
   included 2020 — hence sigma=687,782, roughly 9x the ~76k implied by the BLS 90%
   confidence interval on the monthly change (±122k at the 1.6-SE convention). The
   ensemble consumes bridge's ladder, so a mis-scaled sigma poisons it even when mu is
   fine. Fixed: robust (5%-trimmed) residual scale in a two-component mixture, chosen on
   log score over {plain std, MAD, IQR, 5%-trimmed} x 4 mixture shapes (n=187):
   trimmed+0.85@s/0.15@3s scored logS −6.297 / CRPS 74.8 / cov95 0.95, against −6.474 /
   78.8 / 0.95 for the shipped single-Gaussian shape. The resulting effective sd is 156k
   against a realised OOS RMSE of 150k, i.e. the emitted width is honest.

A fifth defect, found while fixing the above and not visible in the original symptom,
is documented at INTERCEPT CORRECTION.

Measured effect (expanding-origin, PIT, one-step-ahead, 2010-01..2026-07, 2020 dropped
from scoring; RMSE in thousands of jobs; "blend" = equal-weight with production):

    member                              solo    corr(prod)   blend   vs production
    production payrolls/0.1.0          135.3         1.00       --        --
    bridge v0.1.0 as shipped           250.2         0.51    169.7     −34.4
    v0.2.0 without intercept corr.     161.9         0.62    130.7      +4.6
    v0.2.0 as shipped                  150.1         0.66    129.3      +6.0
    constant-only benchmark            185.8         0.61    140.4      −5.1

Recent sub-sample 2023-01..2026-07 (n=43), production solo 97.8:

    bridge v0.1.0 as shipped                                          −66.7
    v0.2.0 without intercept corr.     126.1         0.55     97.5      +0.3
    v0.2.0 as shipped                  103.9         0.68     92.6      +5.2

Read the blend column, not the solo column. v0.1.0 made the ensemble 34k WORSE than
production alone (67k worse on recent data) — the complaint that it "pollutes the
ensemble by +12k" understated the damage by a factor of three. v0.2.0 improves the
ensemble by 6.0k / 5.2k. That is a real but modest contribution, and the honest framing
is that this member earns its place by being DECORRELATED (0.66), not by being accurate:
solo it is still 15k worse than production. It beats the constant-only benchmark with
DM t=−2.39 (p=0.017), a real but modest signal.


STATE DEPENDENCE — WHY A MODEST GAIN IS THE EXPECTED RESULT
-----------------------------------------------------------
The most robust finding in the claims→payrolls literature is that claims are worth very
little in expansions and a great deal in recessions, and it is the right lens for the
+6.0k above. Gavin & Kliesen (FRB St. Louis Review 84(3), 2002) — the only genuinely
recursive out-of-sample payroll test in the literature — find AR(12) beats claims on
RMSE in 8 of 10 expansion-month payroll cells, with OOS-F negative in 8 of 10. McConnell
(1998) puts a number on both sides: adding claims HURTS expansion forecasts by 11.0k
(DM p=0.09) and HELPS recession forecasts by 34.2k (p=0.01).

2010-2026 ex-COVID is almost entirely expansion, so a small positive contribution is the
best a claims-only member should be expected to show over this sample, and the measured
+6.0k is consistent with that rather than disappointing against it. The corollary is the
reason to keep this member even though it looks marginal: its contribution should be
strongly convex in a downturn, which is precisely the regime where production's momentum
term breaks. Do not retire this on expansion-sample evidence alone.

Two warnings that follow from the same literature. First, beware the encompassing tests:
G&K's ENC-CM statistics reject "AR encompasses claims" in cells where claims LOSE on
RMSE — read RMSE and OOS-F, not the encompassing columns. Second, the sign of this
result is not stable across studies (Kliesen & Wheelock 2012 report the reverse, but on
full-sample fitted values rather than forecasts), so treat state dependence as a reason
to keep the member, not as a licence to add a regime switch without testing it here.


WHY NO AR TERM
--------------
Adding lagged published changes to the right-hand side lowers standalone RMSE from 164.6
to 132.6, which looks like an easy win and is not one. Production payrolls/0.1.0 is
`0.6·(3-month average of printed changes) + 0.4·claims_signal` — it is already the
momentum model. Regressing on lagged NFP raises this member's error correlation with
production from 0.61 to 0.89, i.e. it converges on being a re-estimated copy of the
thing it is supposed to diversify. The blend barely moves (132.3 → 130.2) because the
accuracy gain and the diversity loss very nearly cancel. What does NOT show up in that
average is the concentration risk: two of the ensemble's three members would then be
computing the same trailing mean, and they would be wrong together precisely when
momentum breaks — which is the regime the ensemble exists to survive. Declined
deliberately; do not "improve" this by adding AR lags without re-running lab round 3.


INTERCEPT CORRECTION — the fifth defect
---------------------------------------
With only a claims-CHANGE regressor, the intercept alone carries the LEVEL. Fix defects
1-4 and the model still predicted a 2026 mean of 149.5k against an actual 76.7k, because
the fitted intercept is a 20-year average of monthly NFP change and the current run rate
is less than half of it. Claims are informative about layoffs; they say nothing about the
hiring slowdown that actually took NFP from ~190k to ~77k, so no claims specification
recovers the level. Two things that did NOT fix it, both tested and rejected:

  - A SHORTER WINDOW makes it worse, not better. 60mo puts the intercept at 220k vs
    240mo's 144k, because 2021-24 averaged HIGHER than 2011-19. On recent data every
    window below 240 loses to production in the blend (−16 to −32). This is a break,
    not a window-length problem.
  - A CLAIMS-GAP LEVEL TERM (log claims vs their own trailing mean) does not help:
    solo RMSE 179.1 with the gap vs 164.6 without, and the 2026 mean stays at ~197k.

What does work is the standard remedy for forecasting after a structural break — Clements
& Hendry intercept correction: add back the mean of the last IC_MONTHS in-sample
residuals. It corrects the CONSTANT while leaving the dynamics alone, which is why it is
not a backdoor AR term: error correlation with production stays at 0.66 (an AR3 term put
it at 0.89 — see WHY NO AR TERM). Effect on the 2026 mean: 149.5k → 82.0k, against an
actual 76.7k.

IC_MONTHS=12 was chosen over {3, 6, 24} on the blend, and the choice is NOT close in the
way the full-sample column suggests. K=6 scores best over 2010-2026 (+6.8 vs +6.0) but
collapses on recent data (+1.1, corr 0.88) — it chases the correction too fast and turns
the member into a copy of production. K=12 is the only setting positive and decorrelated
in both panels. K=24 is too slow (2026 mean 137.9k). Do not re-tune this on the full
sample alone.


COVID EXCLUSION SPAN
--------------------
COVID_TO is 2022-12, not 2020-12. That is a longer exclusion than Schorfheide & Song
(IJCB 20(4), 2024) recommend for VARs — "dropping observations from March to June 2020
but including the subsequent data points" — and the reason is that this is a claims→NFP
bridge, not a VAR. The 2021-22 reopening months are not merely volatile; the mapping
itself inverts, with +700k prints arriving alongside normalising claims, so they are
misleading rather than just noisy. Empirically, excluding through 2022 beat both the
2020-only and 2020-2021 spans at EVERY window length tested (15 of 15 cells).

Note the formal equivalence, since it governs how to change this: excluding observations
is downweighting them to zero. Lenza & Primiceri (JAE 37(4), 2022) show dropping is
"essentially assuming s̄ = ∞" in their volatility-scaling scheme; Schorfheide & Song put
it as "letting the scale tend to infinity is equivalent to dropping observations."
Carriero-Clark-Marcellino-Mertens add that a full set of monthly COVID dummies is
"tantamount to ignoring data since March" for point estimation. So drop-vs-dummy is not
a choice; the real choice is zero weight (here) vs an estimated intermediate weight (LP).
The known cost is that treating outlier timing as KNOWN compresses forecast uncertainty
— LP warn that dropping "vastly underestimates uncertainty." That is a live concern here
because this model emits a ladder, not a point, and it is why sigma is deliberately
calibrated against realised OOS error (156k emitted vs 150k realised) rather than taken
from the surviving in-sample residuals at face value.


WINDOW LENGTH
-------------
240 months (20y), not the 180 of the first draft. With the 2020-2022 hole this leaves
~206 usable observations. 240 is the only window that improves the blend in BOTH the
full and the recent panel (+6.0 / +5.2); 120 wins the full panel marginally (+5.9) but
loses on recent (+4.5), and everything shorter degrades sharply. Verified 2026-08.


WHY REFERENCE-WEEK ALIGNMENT
----------------------------
CES counts the pay period including the 12th of the month, so the claims weeks that map
onto month m end in that week, not at month end. `_claims_refweek_logavg` uses that
window. Honesty about how much this earns: it is a wash empirically (164.6 vs 168.6 for
plain month-end averaging, and it loses in the most recent sub-sample). It is kept
because it is the defensible window and costs nothing, NOT because it is carrying the
result.

There is no canonical alignment rule — at least four conventions are in live use, and
the choice is a judgement call rather than a solved problem:

  1. week containing the 12th vs the same week of the prior month — the de facto market
     convention (Chicago Fed LMI, Calculated Risk, Reuters). This is what we do.
  2. claims at t+1 against the employment change at t — Cajner et al. (FEDS 2020-055)
     adopt the lead to absorb filing and processing lags.
  3. a cumulative sum from the week after last month's reference week through this
     month's — same paper, Table 3.
  4. the average of the middle two weeks — McConnell (1998) fn.9, motivated by exactly
     the same pay-period logic and used there as a robustness check.

The deeper reason none of these is "correct": the CES reference period is a PAY PERIOD
straddling the 12th, and it is establishment-specific — weekly, biweekly, semimonthly or
monthly. Any calendar-week rule, including ours, is a weekly-payroll approximation.
Convention 2 is the one worth testing next if this is revisited; it is the only one with
a stated mechanism (lags) rather than a stated definition.


OPEN LEADS — the two things most likely to actually improve this
-----------------------------------------------------------------
Recorded after a literature sweep, in priority order. Neither is implemented.

1. CONTINUING CLAIMS (CCSA). Gavin & Kliesen's in-sample standard errors rank
   AR 0.171 > ICSA 0.157 > CCSA 0.143 — continuing claims dominate initial claims in
   every one of their payroll panels, and the series is essentially unstudied for this
   purpose (McConnell, Kliesen-Wheelock and Braxton all test ICSA only). The mechanism
   is exactly the gap documented at INTERCEPT CORRECTION: Cajner et al. (FEDS 2020-055)
   argue insured unemployment "responds to gross job gains as well as gross job losses,"
   whereas initial claims see only separations — which is why no ICSA specification here
   could recover the hiring-side level shift and an intercept correction had to. CCSA is
   NOT currently in fred_obs; this needs an ingest addition with correct PIT release
   timing (same Thursday 08:30 ET release as ICSA, but CCSA lags one week).
2. THE t+1 ALIGNMENT of convention 2 above.

Explicitly NOT worth pursuing: tuning the Almon polynomial. Foroni, Marcellino &
Schumacher tested frequency ratios of 3, 12 and 60 — weekly-to-monthly (~4.33) falls in
an untested gap, and their result that small mismatches favour UNRESTRICTED lags argues
against more polynomial structure, not for it. No source found puts weekly claims into a
MIDAS specification against monthly CES payrolls at all; this cell of the literature is
empty. If it is ever revisited, `midasr`'s agk.test (flat weights as the null) and
hAh.test (is the Almon restriction acceptable) turn it into an in-sample specification
test on our own data instead of an appeal to authority.


TARGET NOISE IS RISING — A LIVE CAVEAT ON SIGMA
------------------------------------------------
Sigma is calibrated on 2010-2026 residuals, and the noise in the target itself has grown
over that window: monthly first-to-third payroll revisions have more than doubled as a
share of employment (0.028% → 0.065%), the CES collection initiation rate has fallen from
~80% to ~30%, and the birth/death share of large benchmark revisions dropped from 82%
(2009-2012) to 34% (2022-2025). Anything calibrated on pre-2020 CES noise understates
current noise. The emitted effective sd (156k) currently sits just above realised OOS
RMSE (150k), which is honest on average but is an average over a period whose second half
is noisier than its first. If coverage starts failing on the wide side of the ladder,
suspect this before suspecting mu.

Related, and the reason defect 1 was worth treating as the top priority: the payroll
first print is NOT an efficient forecast of the revised figure. Aruoba (2008) rejects
both the news and the noise hypotheses for employment, finding a significantly positive
mean revision correlated with the initial announcement; Stark finds a +17.8k mean
revision (t=4.3) whose size is predictable from the reported job change itself. Because
of that, the vintage convention is not a detail — Koenig, Dolmas & Piger showed that
choice alone flipping a model from beating to losing against Blue Chip.


CALIBRATION FLOOR — READ BEFORE "IMPROVING" THIS
-------------------------------------------------
The BLS 90% CI on the monthly NFP change is ±122,000 (Technical Note, July 2026, at the
1.6-SE convention), implying a sampling SE around 76,000 before any forecasting error.
This is NOT a fixed anchor and should be re-read, not memorised: it has widened with the
shrinking CES sample — ±100k (2012) → 110k (2020) → 130k (2024) → 136k (2025) → 122k
(2026). It is also sampling error ONLY, excluding birth/death model error, nonresponse
and benchmark revisions.

Reference points, all against a FIRST print, monthly change, RMSE in thousands:

    Bloomberg consensus (Klein Table 1, OOS 2017-2019)              65.5
    ADP-FRB microdata model (NBER c14272)                           70.7
    CES sampling SE, i.e. the measurement floor                    ~65
    trailing 12-month mean (Klein, calm 2017-2019 window)           77.8
    trailing 3-month mean (own BLS-API calc, total NFP 1986-2019)  118.1
    unconditional mean (same calc)                                 197.4
    THIS MODEL, 2010-2026 ex-COVID                                 150.1

NBER c14272 states the bar directly: with an OOS RMSE of 70,700 against a sampling SE of
65,000, "any forecast that achieved better performance would be forecasting sampling
error." Any claims-only specification here that backtests materially below ~65-70k is
reporting look-ahead bias, not skill.

Two caveats on the comparison, so it is not over-read. FEDS 2018-005 Table 8 (194k
constant-only / 154k with real-time labor data / 113k with Bloomberg / 98k with ADP) is
CES *private* payrolls, a different and less noisy series than the total nonfarm target
here — do not read our 150.1k as beating its 154k row. And the two independent
constant-only estimates (194k there, 197.4k from our own calculation, 185.8k measured on
our sample) agree closely, which is the useful cross-check: this model sits well inside
the naive-benchmark band and nowhere near the consensus.

The uncomfortable implication, worth stating plainly: the gap between a trailing 6-month
mean (~81k in a calm expansion) and a full real-time ADP-augmented model (70.7k) is only
a few thousand jobs, and by Gürkaynak & Wolfers' numbers roughly 62% of even the
consensus error variance is noise in the target rather than forecaster error. There is
very little room here for a claims-only bridge to be good. It is not supposed to be —
see the DECORRELATION note in the header.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from prediction_market_macro.model.common import GaussianMix, Pred
from prediction_market_macro.model.features import FeatureStore

VERSION = "bridge/0.2.0"
SUPPORTED = ("KXCPI", "KXPAYROLLS", "KXU3")

WINDOW_MONTHS = 240          # 20y rolling — see WINDOW LENGTH below
MIN_FIT_N = 30
# Mar 2020 - Dec 2022, excluded from every fit. See COVID EXCLUSION SPAN below.
COVID_FROM = pd.Period("2020-03", freq="M")
COVID_TO = pd.Period("2022-12", freq="M")
# Clements & Hendry intercept correction: re-centre on the last IC_MONTHS residuals.
# See INTERCEPT CORRECTION below. 0 disables.
IC_MONTHS = 12
# Fat-tail shape, chosen on log score over {MAD, IQR, trimmed, plain} x 4 shapes.
TAIL_WEIGHT = 0.15
TAIL_MULTIPLE = 3.0


def almon_weights(n: int, theta1: float = 0.0, theta2: float = -0.15) -> np.ndarray:
    """Exponential Almon lag weights over n lags (0 = most recent), normalized."""
    ks = np.arange(n, dtype=float)
    w = np.exp(theta1 * ks + theta2 * ks * ks)
    return w / w.sum()


# ------------------------------------------------------------------ estimation

def _huber(X: np.ndarray, y: np.ndarray, delta: float = 1.345,
           iters: int = 50) -> np.ndarray:
    """IRLS Huber regression. delta is in units of a robust scale estimate.

    The scale is floored at a fraction of the plain residual std. On a target like the
    U3 monthly delta — which is mostly exactly 0.0 or ±0.1 — more than half the
    residuals coincide, the MAD collapses to ~0, the IRLS weights explode, and lstsq
    raises LinAlgError. Flooring keeps that degenerate case finite.
    """
    X1 = np.column_stack([np.ones(len(X)), X])
    beta = np.linalg.lstsq(X1, y, rcond=None)[0]
    floor = 1e-6 * max(float(np.std(y)), 1e-12)
    for _ in range(iters):
        r = y - X1 @ beta
        s = max(1.4826 * float(np.median(np.abs(r - np.median(r)))),
                0.1 * float(np.std(r)), floor)
        if not np.isfinite(s) or s <= 0:
            break
        u = np.abs(r) / s
        w = np.sqrt(np.where(u <= delta, 1.0, delta / np.maximum(u, 1e-9)))
        nb = np.linalg.lstsq(X1 * w[:, None], y * w, rcond=None)[0]
        done = np.max(np.abs(nb - beta)) < 1e-8
        beta = nb
        if done:
            break
    return beta


def _sig(v: float, digits: int = 6) -> float:
    """Round to `digits` SIGNIFICANT figures, not decimal places.

    The coefficients here span ten orders of magnitude — KXPAYROLLS' b is ~1e5 (jobs per
    unit log-claims) while KXU3's is ~1e-6 (percentage points per person). A flat
    round(b, 5) reported KXU3's slope as exactly 0.0, which reads as "the regressor is
    doing nothing" when in fact it is doing the whole job at a different scale.
    """
    if v == 0 or not math.isfinite(v):
        return float(v)
    return float(round(v, -int(math.floor(math.log10(abs(v)))) + (digits - 1)))


def _robust_scale(resid: np.ndarray) -> float:
    """5%-trimmed residual std — the core sigma.

    A plain std over Huber residuals is inflated by exactly the outliers the Huber fit
    was told to discount, which is how v0.1.0 arrived at sigma=687,782. Trimming scored
    best on log score and CRPS across MAD, IQR and untrimmed alternatives.
    """
    s = np.sort(resid)
    lo, hi = int(0.05 * len(s)), int(0.95 * len(s))
    core = s[lo:hi] if hi > lo else s
    return max(float(np.std(core)), 1e-9)


# --------------------------------------------------------------- PIT targets

def _vintage_index(conn, sid: str, asof: datetime):
    """PIT view of one FRED series, indexed two ways.

    Returns (by_month, firsts) where

      by_month[m] = [(knowledge_time, value), ...] ascending — every vintage of month m
      firsts[m]   = (knowledge_time, value)        — m's FIRST print

    Read through FeatureStore so the PIT door (knowledge_time <= asof) is enforced once.
    """
    rows = FeatureStore(conn).fred_vintages(sid, asof)
    by_month: dict[pd.Period, list[tuple[str, float]]] = {}
    for r in rows:
        by_month.setdefault(pd.Period(r["event_time"][:7], freq="M"), []).append(
            (r["knowledge_time"], float(r["value"])))
    firsts: dict[pd.Period, tuple[str, float]] = {}
    for m, obs in by_month.items():
        obs.sort(key=lambda kv: kv[0])
        firsts[m] = obs[0]
    return by_month, firsts


def _prev_as_known(by_month, month: pd.Period, kt: str) -> float | None:
    """Value of `month` as it stood at knowledge time `kt` — the PIT previous level.

    This is the rule that makes the printed change reconstructable for EVERY series,
    and getting it wrong is defect 1. The obvious-looking alternative — require m and
    m−1 to carry the same vintage_date — happens to work for PAYEMS, because ALFRED
    ships three months of history in each payrolls vintage, so m−1 is physically present
    in m's release row-set. It silently fails for CPIAUCSL and UNRATE, which store ONE
    row per vintage (verified: 3102 rows / 954 months / 668 vintages for CPI). Under the
    same-vintage rule those two series can never form a single pair and the model raises
    "insufficient overlapping history" forever.

    "Latest print of m−1 known at m's release" is equivalent to the same-vintage rule
    wherever the same-vintage row exists (one vintage carries exactly one knowledge_time,
    so `<= kt` selects it), and well-defined where it does not.
    """
    obs = by_month.get(month)
    if not obs:
        return None
    prior = [v for k, v in obs if k <= kt]
    return prior[-1] if prior else None


def _published_changes(conn, sid: str, asof: datetime, scale: float = 1.0) -> pd.Series:
    """Headline month-over-month CHANGE per month, as printed.

    level(m) at m's first release minus level(m−1) as it stood at that same moment —
    the figure BLS headlines and the quantity Kalshi settles on. Mirrors
    payrolls.printed_changes. See defect 1 for why diffing first prints across vintages
    is not a near-equivalent.
    """
    by_month, firsts = _vintage_index(conn, sid, asof)
    out: dict[pd.Period, float] = {}
    for m, (kt, val) in firsts.items():
        prev = _prev_as_known(by_month, m - 1, kt)
        if prev is not None:
            out[m] = (val - prev) * scale
    return pd.Series(out).sort_index()


def _published_mom_pct(conn, sid: str, asof: datetime) -> pd.Series:
    """Headline MoM % per month, on the same PIT basis as _published_changes."""
    by_month, firsts = _vintage_index(conn, sid, asof)
    out: dict[pd.Period, float] = {}
    for m, (kt, val) in firsts.items():
        prev = _prev_as_known(by_month, m - 1, kt)
        if prev:
            out[m] = (val / prev - 1.0) * 100.0
    return pd.Series(out).sort_index()


def _first_print_levels(conn, sid: str, asof: datetime) -> pd.Series:
    """First-print level per month — the settled figure for a level series (U3)."""
    _by_month, firsts = _vintage_index(conn, sid, asof)
    return pd.Series({m: val for m, (_kt, val) in firsts.items()}).sort_index()


# ------------------------------------------------------------------ regressors

def _hf_complete(hf: pd.Series, cutoff) -> bool:
    """Has the high-frequency series actually reached this month's window end?

    Bridge is horizon-agnostic by design — one nowcast serves every open period — so
    predict() is routinely called for months whose data window has not begun. Both
    regressor helpers below take `.tail(n)` of everything at-or-before a cutoff, which
    for a future month silently returns the SAME latest n observations for month m and
    month m−1, making the difference identically 0.0. The model then degrades to
    intercept-only, which is a reasonable prior for a month with no data but is not the
    same statement as "claims were flat" — and in the emitted inputs the two were
    indistinguishable. This flag separates them.
    """
    return len(hf) > 0 and hf.index[-1] >= cutoff


def _almon_agg(hf: pd.Series, month: pd.Period, n_lags: int) -> float | None:
    """Almon-weighted mean of the n_lags weekly obs ending inside `month`."""
    win = hf[hf.index <= month.to_timestamp(how="end")].tail(n_lags)
    if len(win) < n_lags:
        return None
    return float(np.dot(almon_weights(n_lags), win.values[::-1]))


def _refweek_end(month: pd.Period) -> pd.Timestamp:
    """Saturday ending the CES reference week — the first Saturday on/after the 12th.

    CES counts the pay period including the 12th, and initial-claims weeks are indexed
    by their Saturday end date, so the claims week that maps onto month m is the one
    ending on the first Saturday at or after the 12th. Computing this exactly, rather
    than using the 12th + 6 days worst case, matters only for `_hf_complete`: for the
    selector below the two agree, because no claims week can end between them. Using
    the worst case in the completeness check reported a month as incomplete when its
    reference week had in fact already closed (e.g. 2026-08, where the 12th is a
    Wednesday and the reference week ended on the 15th, not the 18th).
    """
    twelfth = month.to_timestamp(how="start") + pd.Timedelta(days=11)
    return twelfth + pd.Timedelta(days=(5 - twelfth.weekday()) % 7)   # 5 = Saturday


def _claims_refweek_logavg(claims: pd.Series, month: pd.Period,
                           n_weeks: int = 4) -> float | None:
    """log of the n_weeks claims average ending in the CES reference week."""
    win = claims[claims.index <= _refweek_end(month)].tail(n_weeks)
    if len(win) < n_weeks or (win <= 0).any():
        return None
    return float(np.log(win.mean()))


def _claims_refweek_dlog(claims: pd.Series, month: pd.Period,
                         n_weeks: int = 4) -> float | None:
    """Month-over-month change in log reference-week claims — the stationary regressor."""
    a = _claims_refweek_logavg(claims, month, n_weeks)
    b = _claims_refweek_logavg(claims, month - 1, n_weeks)
    if a is None or b is None:
        return None
    return a - b


# ------------------------------------------------------------------------ fit

def _fit_bridge(y: pd.Series, x_of_month, ref: pd.Period,
                window: int = WINDOW_MONTHS, min_n: int = MIN_FIT_N,
                ic_months: int = IC_MONTHS):
    """Robust y_m = a + b·x_m over a bounded, COVID-excluded, strictly past window.

    `y` is indexed by pd.Period. Only months STRICTLY BEFORE `ref` enter the fit, so a
    call for the open month cannot see its own outcome.

    Returns (a, b, sigma, n, n_covid, ic) where `a` is ALREADY intercept-corrected and
    `ic` is the correction applied, reported separately so the raw fit stays auditable.
    """
    months = [m for m in y.index if m < ref][-window:]
    xs, ys, n_covid = [], [], 0
    for m in months:
        if COVID_FROM <= m <= COVID_TO:
            n_covid += 1
            continue
        xv = x_of_month(m)
        yv = y.loc[m]
        if xv is None or not np.isfinite(xv) or not np.isfinite(yv):
            continue
        xs.append(xv)
        ys.append(float(yv))
    if len(xs) < min_n:
        return None
    X = np.asarray(xs, dtype=float)[:, None]
    yv = np.asarray(ys, dtype=float)
    beta = _huber(X, yv)
    resid = yv - np.column_stack([np.ones(len(X)), X]) @ beta
    # Intercept correction — the fitted line's own recent bias, added back. `xs` is in
    # ascending month order, so the tail is the most recent non-COVID months.
    ic = float(np.mean(resid[-ic_months:])) if ic_months and len(resid) >= ic_months \
        else 0.0
    return (float(beta[0]) + ic, float(beta[1]), _robust_scale(resid),
            len(xs), n_covid, ic)


def _mixture(mu: float, sigma: float) -> GaussianMix:
    """Two-component fat-tailed mixture around mu (see defect 4)."""
    return GaussianMix(((1.0 - TAIL_WEIGHT, mu, sigma),
                        (TAIL_WEIGHT, mu, sigma * TAIL_MULTIPLE)))


def predict(conn, asof: datetime, period: str, series: str) -> Pred:
    """Bridge nowcast for `period` (ISO month). Raises if unsupported/insufficient."""
    if series not in SUPPORTED:
        raise ValueError(f"bridge unsupported for {series}")
    fs = FeatureStore(conn)
    ref = pd.Period(period, freq="M")
    extra: dict = {}

    if series == "KXCPI":
        # Gasoline -> headline CPI is a stable mapping; the v0.1.0 spec backtested fine
        # here (0.249 vs 0.250 RMSE against the rebuilt one — a wash). Kept as-is; only
        # the target construction, window, estimator and sigma changed.
        y = _published_mom_pct(conn, "CPIAUCSL", asof)
        gas, h = fs.fred_series("GASREGW", asof)
        gas_ch = gas.diff().dropna()
        xf = lambda m: _almon_agg(gas_ch, m, 12)           # noqa: E731
        fit = _fit_bridge(y, xf, ref)
        if fit is None:
            raise RuntimeError("bridge KXCPI: insufficient overlapping history")
        a, b, sig, n, n_cov, ic = fit
        x = xf(ref)
        if x is None:
            raise RuntimeError("bridge KXCPI: no weekly gas data for ref month")
        mu = a + b * x
        extra["x_complete"] = _hf_complete(gas_ch, ref.to_timestamp(how="end"))
        unit_note = "%mom"
    elif series == "KXPAYROLLS":
        y = _published_changes(conn, "PAYEMS", asof, scale=1000.0)   # -> jobs
        icsa, h = fs.fred_series("ICSA", asof)
        xf = lambda m: _claims_refweek_dlog(icsa, m)        # noqa: E731
        fit = _fit_bridge(y, xf, ref)
        if fit is None:
            raise RuntimeError("bridge KXPAYROLLS: insufficient overlapping history")
        a, b, sig, n, n_cov, ic = fit
        x = xf(ref)
        if x is None:
            raise RuntimeError("bridge KXPAYROLLS: no weekly claims for ref month")
        mu = a + b * x
        extra["x_complete"] = _hf_complete(icsa, _refweek_end(ref))
        unit_note = "jobs_change"
    else:                                                     # KXU3
        # Level series: predict the monthly DELTA off the last published level. The
        # regressor stays the Almon-weighted 4-week claims trend, which backtested
        # best of the variants tried (0.737 vs 0.768 for a dlog regressor).
        lv = _first_print_levels(conn, "UNRATE", asof)
        y = lv.diff().dropna()
        icsa, h = fs.fred_series("ICSA", asof)
        dc4 = icsa.rolling(4).mean().dropna().diff().dropna()
        xf = lambda m: _almon_agg(dc4, m, 8)                # noqa: E731
        fit = _fit_bridge(y, xf, ref)
        if fit is None:
            raise RuntimeError("bridge KXU3: insufficient overlapping history")
        a, b, sig, n, n_cov, ic = fit
        x = xf(ref)
        if x is None:
            raise RuntimeError("bridge KXU3: no weekly claims trend for ref month")
        prior = [m for m in lv.index if m < ref]
        if not prior:
            raise RuntimeError("bridge KXU3: no published level before ref month")
        last = float(lv.loc[prior[-1]])
        mu = last + a + b * x                                 # level = last + delta
        sig = math.hypot(sig, 0.05)
        extra["last_level"] = round(last, 3)
        extra["x_complete"] = _hf_complete(dc4, ref.to_timestamp(how="end"))
        unit_note = "u3_level"

    horizon = h or asof.isoformat()
    return Pred(series=series, period=period, dist=_mixture(float(mu), float(sig)),
                asof=asof, model_version=VERSION,
                inputs={"a": _sig(a), "b": _sig(b), "sigma": _sig(sig),
                        "x": _sig(float(x)), "ic": _sig(ic), "n_fit": n,
                        "n_covid_excluded": n_cov, "ic_months": IC_MONTHS,
                        "window_months": WINDOW_MONTHS, "unit": unit_note, **extra},
                data_horizon=datetime.fromisoformat(horizon))


def shadow_run(conn, settings) -> int:
    """Write bridge shadow preds for every open supported (series, period)."""
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.model.common import grid_pmf
    from prediction_market_macro.model.common import pred_to_row
    from prediction_market_macro.ops.predict_all import _open_periods
    now = datetime.now(timezone.utc)
    n = 0
    for series in SUPPORTED:
        spec = REGISTRY.get(series)
        if spec is None:
            continue
        for _tok, key in _open_periods(conn, series):
            try:
                p = predict(conn, now, key, series=series)
                pmf = grid_pmf(p.dist, spec.round_rule)
                ladder = {str(k): round(v, 6) for k, v in pmf.items()}
                conn.execute(
                    "INSERT OR REPLACE INTO preds(series, period, asof, model_version,"
                    " dist_json, ladder_json, inputs_json, data_horizon, created_ts)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", pred_to_row(p, ladder))
                n += 1
            except Exception:                                # noqa: BLE001
                continue                                      # shadow: silent skip
    conn.commit()
    return n
