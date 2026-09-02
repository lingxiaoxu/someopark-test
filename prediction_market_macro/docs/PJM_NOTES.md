# PJM grid data — the screening study (2026-09-02)

User-directed: mine PJM the way ERCOT was mined, reusing the AEUS strategy's credentials
and the ERCOT lane's discipline, and decide by backtest. This file is the record. Read it
with `ERCOT_NOTES.md` — the two screenings answer the same question about two markets and
reach the same verdict by different arithmetic.

## What was built (landed, SHADOW per §7-bis)

`ingest/pjm.py`, table `pjm_daily`, wired into the daily refresh as `step("pjm")` +
`step("pjm_mirror")`. Two tiers, mirroring ERCOT file-for-file:

* **PJM Data Miner 2** (`api.pjm.com/api/v1/gen_by_fuel`, header
  `Ocp-Apim-Subscription-Key` ← `PJM_API_KEY`, the AEUS strategy's own key): hourly
  generation by fuel, accrued forward, ONE request per refresh (non-member limit ~6/min;
  AEUS owns the bulk pulls, and the two lanes must not compete for that budget).
* **EIA-930** (`EIA_API_KEY`, the same key the ERCOT lane uses): daily demand and net
  generation by fuel, **2019-01-01 → present, 22,407 rows, zero gaps**. `facets[timezone][]
  =Eastern` is mandatory — without it the API returns one row per US timezone per day with
  DIFFERENT values, and a naive sum inflates everything 5×.
* `PJM_GASBURN_W` mirrored into `fred_obs` (400 weeks), the same synthetic-sid path
  `ERCOT_GASBURN_W` uses to reach the DFM panels.

Scale, measured: PJM burns **927,636 MWh/day** of gas on the 2019-2026 average against
ERCOT's ~744,000 — 1.25×, and PJM is winter-peaking where ERCOT is summer-peaking.

## The screening: 598 tests, six mechanism angles, three adversarial lenses per hit

Every angle stated its causal chain before testing; every signal used a prior-calendar-
years-only day-of-year climatology and a D+2 knowability cut; every p-value is a ≥2000-draw
permutation, re-checked under 13-week block permutation because the signal's lag-1
autocorrelation is 0.44.

### What is real (mechanism), and it is stronger than ERCOT's

| signal (PIT, storage week) | → EIA weekly storage surprise | n |
|---|---|---|
| **PJM thermal load z (gas+coal)** | **r = −0.518** | 294 |
| PJM total demand z | r = −0.462 … −0.548 | 293 |
| PJM+ERCOT combined burn z | r = −0.379 | 294 |
| ERCOT burn z (the earlier finding, re-derived here) | r = −0.357 | 292 |
| ERCOT burn z, winter only | r = −0.752 | 71 |

All survived ≥2 of 3 adversarial lenses (multiplicity / leakage / robustness), with the
verifiers imposing three corrections that are adopted here: (1) report the permutation
**floor** honestly — `p = 0.0002` is 1/5001 with zero exceedances, not a measurement;
(2) the relation is **contemporaneous nowcast**, not forecast — the grid week and the
storage week are the same week; (3) the mechanism for the demand leg is **heating/cooling
demand**, not power burn, so it is relabelled accordingly.

### What is not real: the tradeable leg, and the arithmetic that closes it

**All 24 burn → NG-price tests are noise**, and this time the screening produced the reason
rather than just the null. The decisive measurement:

> the storage surprise itself barely moves front-month NG on print day: **r = +0.091,
> p = 0.121, n = 287.**

That caps the whole chain: |r(burn, surprise)| = 0.39 × |r(surprise, price)| = 0.091 implies
a ceiling of **|r| ≈ 0.036** on burn → print-day return, and the largest value observed
across all six signals is 0.054. The chain closes to within noise. **The tradeable leg is
not dead because burn fails to predict storage — it is dead because the storage surprise
does not move the price.** Four price tests did clear raw p<0.05, and every one of them
carries the **wrong sign** (the mechanism predicts price *up* on more burn; all four are
negative), which reads as weather-spike mean reversion; against 598 tests they are nothing.

### Does PJM add to ERCOT? On storage yes, on price no

Combined does **not** reliably beat ERCOT alone on the storage surprise: |r| improves by
+0.023 (full week) / +0.040 (strict), and the 5000-draw bootstrap CI on that difference
straddles zero both times. Partial r(PJM | ERCOT) = −0.113 … −0.140; R² goes 0.124 → 0.152.
PJM adds a couple of points of R² — real, small, and not significant at 598 tests.

The *diagnostic* that came out of this is worth more than the number: **PJM's burn is
weaker than ERCOT's despite being 1.8× larger**, because ERCOT's surprise-generating
variance comes from unforecastable wind (ERCOT wind → surprise r = +0.243; PJM wind
r = +0.096, null). Size of burn is not what makes a grid informative — *unforecastability*
is.

### The four other angles, all null

* **Gas/coal switching** (a margin PJM has and ERCOT lacks): the burn × switch interaction
  in the storage-surprise regression is null three ways (p ≥ 0.26 under every specification).
* **CPI family**, monthly, with 1–2 month pass-through leads: 84 tests, 6 at nominal p<0.05
  against 3.6 expected, **nothing survives BH at q=0.05**.
* **Crude / retail gasoline**: 207 tests, every tradeable leg null (PJM burns 9 GWh/day of
  oil against 2.2 TWh of demand — the prior was no channel, and the data agrees).
* **KXPAYROLLS**: the single biggest-looking result in the whole screen (r = −0.446,
  p = 0.0002) is a **trend artifact** — PJM demand grew +15% on data-center load while
  payrolls trended the other way. Caught by the verifier, recorded here so it is never
  rediscovered as a finding.

## Verdict for the 14 markets

**No PJM signal reaches adoption on the screening.** One candidate has a market, a
pre-stated mechanism, the right sign and unconditional significance — weather severity
(mean |z| of PJM+ERCOT demand) → KXJOBLESSCLAIMS, r = +0.195, n = 251, p = 0.0016 — and it
is the one place PJM adds what ERCOT structurally could not: 65M people across 13 states
against Texas alone (ERCOT's own |z| → ICSA was r = −0.02 in the earlier screen). It does
not survive full-screen multiplicity (0.0016 × 598 ≫ 0.05). It is therefore taken to a
**registered prospective judgment** (PR-33) rather than adopted or discarded — the same
route PR-31 gave the ERCOT covariate, with the same honest prior: PR-31 lost.

## Banked, for the day it matters

The burn/thermal → storage-surprise relation is the strongest anchor this project has
measured for a quantity **no listed market settles on**. If Kalshi lists an EIA storage
market, the ready-made signal is **PJM thermal load z** (r = −0.518), not ERCOT burn, and a
joint PJM-demand + ERCOT-burn model reaches R² = 0.394 on the surprise. Until then the
ingest keeps accruing and no model reads the table.

Artifacts: `/tmp/dfm_verify/pjm_*.py`, the six probe scripts and 48 verification runs under
the workflow transcript `wf_20b4cece-373`.


## Addendum — PR-33 judged (2026-09-02)

The one candidate went to the full production machinery: `model/pjm_cov.py` (the ERCOT
covariate's twin — D+2 knowability, prior-years-only climatology, expanding walk-forward
OLS strictly before each asof), gated into `model/claims.py` behind `params["pjm_w"]`
(default 0 = bit-identical), both arms through `replay_series` on every settled event.

**NOT ADOPTED.** −1h per-leg Brier 0.15132 → 0.15303 (**−1.13%** against a +2% bar);
coverage parity 14/14; falsifier (c) clean — KXNATGASW and KXCPI are bit-identical across
the two arms, so the gate touches only claims as registered.

K = 2 on this family, and the two judgments agree: PR-31 tested ERCOT alone (−0.9%), PR-33
tested PJM+ERCOT combined (−1.13%). **Widening the weather proxy from one state to
thirteen did not rescue the channel** — the screening's r = +0.195 was too weak to survive
a walk-forward beta, exactly as the registration predicted. The weather-severity → claims
channel is closed.

What stays in production: the ingest (accruing daily), the mirror, and the inert gate.
What is banked: PJM thermal load → EIA storage surprise, r = −0.518 — waiting for a market
that settles on that number.
