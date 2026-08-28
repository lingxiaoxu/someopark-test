# PLAN_DFM_SYNTH — DFM-generated samples for parameter selection

Status: **design + build in progress** (2026-08-20). Owner: `research/synth/`.
Companion to the sample gate shipped in `bd8540f` (`research/param_argmin.sample_cap`).

## 0. The problem this exists to solve

`param_argmin` picks a parameter set by raw argmin of realised hybrid PnL over the
quotable events of the last 75 days. On the weekly series that is 10–11 events. On the
monthly ones it is **2 or 3**, because Kalshi quote recording only began around 2026-06
(`pnl_score.quotable_events` needs a candle stamped before the event's own close, and
there are 63 such events across all 14 series).

Argmin of `K` candidates on `n` events overstates the winner by roughly
`sqrt(2 ln K / n)` per-event sd even when every candidate is identical. On 2026-08-20 the
live board read KXPAYROLLS at n=2 / K=97 = **2.14 sd of pure selection bias**. The gate
now caps `K <= exp(n t^2 / 2)` at `t = 1.0`, which shuts monthly search off entirely
(n=2 → width 1, i.e. defaults only) and leaves the weekly series untouched.

The gate is correct but it is a *bar*, not a *fix*. The sample does not grow until roughly
2027-05. This plan is the fix: manufacture the missing sample with the diffusion factor
model in `dfm/`, conditioned on the current macro environment and on the bet's own level
(WTI price, natgas storage, claims level, …), so the monthly series recover a sample and
the gate re-opens **because the evidence arrived**, not because the bar was lowered.

## 1. What must be true for this to be worth anything

State the trap up front, because the whole design is shaped by avoiding it.

> Synthetic samples drawn from a generator fitted on the same history do **not**
> automatically buy statistical license. They add information about which parameter set
> wins **under the generator's distribution**. If the generator is right where it matters,
> that is worth a lot. If it is wrong — thin tails, wrong market behaviour — the argmin
> will confidently find the set that best exploits *our own simulator*.

So the synthetic sample size is never fed to `sample_cap` as if it were real. It enters
discounted:

```
n_eff = n_real + lambda * n_synth,    lambda measured, not assumed  (§6)
```

`lambda` is estimated on the series where both exist — the weekly ones, n_real = 10–11 —
by selecting on synthetic samples only and then measuring, on the real events, how much
of the claimed improvement survives. If lambda comes out at zero, the honest outcome of
this project is a documented zero and the gate stays shut. That result would be worth
having; it is not a failure mode to engineer around.

## 2. What one "sample" actually is

A scored observation is one call to `pnl_score.event_pnl(conn, series, tok, key,
close_ts, params)`. It reads three things from a connection:

| ingredient | table | who generates it here |
|---|---|---|
| the model's view under the candidate params | `fred_obs`, `fut_daily` (via `model/features.FeatureStore`) | **DFM** — synthesize the history, then run the real model function on it |
| the market's view | `contracts` (strikes) + `candles` (`yes_bid_close`/`yes_ask_close`) | **empirical transplant** — §5, never invented |
| the outcome | `settlements.result` per leg | **DFM** — the synthetic release value, laddered through the real strike rule |

The model is *not* re-implemented: a synthetic world is a schema-identical sqlite db and
`event_pnl` runs against it unmodified. That is the single most important structural
decision in this plan — it means the candidate parameters propagate through exactly the
code production runs, including `decide()`, the gates, the exit rules and the fee model.

## 3. Architecture

```
prediction_market_macro/research/synth/
  panel.py       S1  fred_obs/fut_daily  ->  overlapping windows Z (n,d) + conditions C
  generator.py   S2  calls dfm/ unmodified (train_conditional, reverse_sample)
  worlds.py      S3  a synthetic path  ->  a schema-identical sqlite db
  book.py        S4  the market-book descriptor, estimated from the 63 real events
  calibrate.py   S5  measures lambda on the weekly series
  score.py       S6  runs pnl_score.score_matrix over synthetic worlds, aggregates
```

`dfm/` is **called, never modified** (user constraint, 2026-08-20). The football fork
already added exactly the piece the parent repo lacks — conditioning — in
`dfm/football/model.py::CondFactorScoreNet` / `train_conditional` and
`dfm/football/generate.py::reverse_sample`. Those three symbols are generic (the
football-specific parts are `cond_vector` / `transform` / the CFA blocks, which we do not
use). They are imported by file path under private module names so that neither `dfm/`
nor `dfm/football/` is touched and no name collides with `prediction_market_macro.model`.

## 4. The panel (S1)

A DFM row is an iid cross-section; macro data is a time series. The bridge is to put the
time dimension *inside* the row: **one row = one window** of the macro panel, e.g. 36
months of `[PAYEMS 1m change, ICSA 4wk avg, UNRATE, CPIAUCSL mom, CPILFESL mom, PCEPILFE
mom, GASREGW, CL, NG, crude/gasoline/natgas storage, DGS2, DGS10, T5YIE]`. Autocorrelation
and cross-correlation then live in the covariance the factor model estimates, which is
precisely what a factor model is good at.

Rules:

* **First prints only** (`FeatureStore.fred_first_prints`). The label a Kalshi contract
  settles on is the first print, and the models that matter here (payrolls, u3, claims)
  are print-anchored. Using latest vintages would train the generator on a series nobody
  ever traded.
* Windows overlap by one period. That inflates the row count without inflating
  information: effective independent draws are about `n_rows / window`, and every count
  reported downstream says which one it is.
* Coverage: monthly series reach back to 1947–1990, weekly claims to 1967, futures to
  2000, EIA storage to 1982/1990/2010. The panel starts at the latest common start of the
  columns it actually uses, per family, rather than truncating everything to 2010.
* The generator trains on data ending **before** the argmin evaluation window
  (`now - 75d`). Otherwise a set selected on synthetic samples has, through the
  generator's fit, seen the outcomes of the very events it is scored on — a diffuse but
  real version of the leak the grid75 protocol exists to prevent.

Conditioning vector `C` = the state at the window's right edge, standardized: for each
column its level **measured against its own trailing `level_lag` mean**, its recent drift,
its recent volatility, plus calendar sin/cos.

The level enters as a deviation rather than as itself, and that is not a cosmetic choice.
The first build fed the raw log level in; the leading principal component of the resulting
condition matrix correlated **0.955 with calendar time**, because CPI, core CPI, core PCE
and PAYEMS are indices that only ever go up. Conditioning on the year cannot generalize to
a held-out year, and the sweep showed it: calibration degraded monotonically in the number
of condition dimensions admitted, from cover80 0.74 unconditional down to 0.50 at the full
38 dims, i.e. conditioning was *strictly worse than not conditioning*. After the deviation
encoding, PC1's correlation with time is −0.32.

The bet's own **absolute** number is not carried by `C` and does not need to be: paths are
integrated forward from today's real levels (`Generator.level_paths`), so a generated WTI
path starts at today's settle by construction. `C` says what kind of environment this is;
the anchor says where it starts.

**On "point in time".** A panel row dated T contains, for each column, the value whose
*event time* is T — not the value that was *published* by T. Core PCE for month T prints
about four weeks later, so the state at T knows a little that a forecaster standing at T
did not. This is stated rather than fixed because it costs nothing here and fixing it would
cost real anchors: the definition is applied identically at training and at generation, so
there is no train/serve skew, and this package manufactures *environments*, it does not
issue forecasts. Publication lag does matter one layer down, where a synthetic release has
to be stamped with a plausible knowledge time so the real models read it point-in-time —
that belongs to `worlds.py` (S3) and is handled there from each series' measured lag.

## 4b. What the S2 measurements changed (2026-08-20)

The generator was built as designed — one conditional score net learning `p(z | c)` over
all of history — and it **failed its gate**, then the diagnosis inverted the design. The
sequence is recorded because each step killed a hypothesis the next one would otherwise
have re-raised.

**1. The global conditional DFM loses to doing nothing.** Purged 5-fold on `core_monthly`
(331 anchors, n_eff ≈ 27, 144 output dims), against `block_bootstrap` — an unconditional
resample of the same history:

| cond_pcs | cover50 | cover80 | KS |
|---|---|---|---|
| 0 (unconditional DFM) | 0.480 | 0.724 | 0.113 |
| 2 | 0.448 | 0.692 | 0.137 |
| 8 | 0.371 | 0.592 | 0.177 |
| **block bootstrap** | 0.450 | **0.760** | **0.067** |

Conditioning degrades monotonically, and `cond_pcs=0` loses too — so this was not the
condition encoding (already once rebuilt, §4) but the estimator or the panel.

**2. It is not dimensionality.** `Column.generate` was added so each panel generates only
what its consuming models read (audited: every model reads 2–4 series, **nothing reads the
storage series**). That cut 144 dims to 13–52. Calibration improved — but the bootstrap
still won every panel, and `claims_weekly`, with the *best* dims-to-draws ratio of all
(13 dims, 53 effective draws), had the *worst* relative loss and was **over**-dispersed
(cover50 0.711 against a nominal 0.50).

**3. The gate itself was blind to the thing being tested.** KS uniformity rewards
calibration and is indifferent to sharpness, so a conditional generator that is calibrated
*and narrower* — strictly more informative — scores the same as an unconditional one that
is calibrated and wide. `validate` now also reports **CRPS**, which is proper, relative to
`block_bootstrap` and paired by anchor.

**4. Under a proper scoring rule, conditioning wins — by analogy, not by network.**
`knn_bootstrap` resamples blocks from the k nearest conditions: conditional, but
non-parametric. It beats the unconditional bootstrap on **every** panel.

| panel | best CRPS/boot | paired t |
|---|---|---|
| labor_monthly | 0.905 (k=20) | −4.25 |
| energy_weekly | 0.941 (k=40) | −3.58 |
| inflation_monthly | 0.952 (k=160) | −7.91 |
| core_monthly | 0.970 (k=80) | −3.25 |
| claims_weekly | 0.978 (k=160) | −4.54 |

The gain is not autocorrelation leaking round the edge of the fold: forcing every
neighbour to be ≥2y and ≥5y away in time leaves it intact (labor 0.908 → 0.910 → 0.895;
inflation 0.955 → 0.956 → 0.960). At ≥10y it collapses, which is pool depletion — removing
a decade from a 30-year panel leaves the "40 nearest" no longer near — not evidence.

So the information the project needs **is there**, and the global score net was the wrong
instrument: on `claims_weekly` it scored CRPS/boot **1.032 (t = +3.30, significantly
worse)** where the analog draw scored 0.978.

**5. Which arm to ship, measured properly on both families.** 130 held-out anchors per
panel, one local fit each, 3000 epochs. **This corrects the "the global DFM loses"
headline above**: under CRPS the global net is *fine on labor* (0.932, t = −3.56) and only
*insignificant on energy* (0.983, t = −1.42). What replicates is not that it loses but
that it is **inconsistent**, while `fit_local` is the strongest arm against the baseline in
both panels.

| arm | labor CRPS/boot (t) | energy CRPS/boot (t) |
|---|---|---|
| boot | 1.000 | 1.000 |
| dfm (global, 2 PCs) | 0.932 (−3.56) | 0.983 (−1.42) |
| knn40 | 0.914 (−3.50) | 0.931 (−1.98) |
| knn80 | 0.936 (−3.88) | 0.925 (−2.62) |
| **loc40** | 0.935 (**−5.20**) | 0.957 (**−2.87**) |
| **loc80** | 0.940 (**−5.53**) | 0.956 (**−3.42**) |

`loc` vs its OWN `knn` on identical rows — the test of what the smoothing costs — is a tie
in both panels (labor t = +0.94 / +0.39; energy t = +0.75 / +1.14, none significant).

One claim was made from labor alone and **does not replicate**: there, smoothing repaired
the analog draw's under-dispersion (cover50 0.382 → 0.456 against a nominal 0.50). On
energy it did not (0.417 → 0.410). That is a panel property, not a property of the method,
and it is not part of the case for shipping `loc`.

The case that does survive is two measured facts and one structural one:

* `loc` has the best paired t against the unconditional baseline in **both** panels.
* `loc` ties `knn` on CRPS, so the smoothing is not bought at the cost of accuracy.
* **Only `loc` can emit a path that never happened.** `knn` at k=80 is 80 distinct worlds
  no matter how many times it is drawn from; §0 needs thousands. This is structural, holds
  without measurement, and is the reason the generative model is in the design at all.

**Consequence for the architecture.** `knn_bootstrap` is not shippable on its own: k
neighbours are k distinct worlds, and §0's purpose is thousands of them. So the DFM keeps
its place with its job inverted — `Generator.fit_local` fits **unconditionally on the
analog neighbourhood**, moving conditioning out of the network and into the sample, and
leaving the diffusion model the one task it is actually good at: smoothing a small
empirical cloud into a density that can emit paths which never literally happened. The bar
for it is `loc` must at least **tie** its own `knn` on identical rows, since novelty is
what it is being paid for. Validation costs one fit per held-out anchor; deployment costs
one fit per run, on today's state — the expensive direction is the one that runs offline.

`dfm/` is still only called, never modified: `fit_local` is `fit` on a row subset with
`cond_pcs=0`, and factors are budgeted out of the neighbourhood (`factor_dim <= k // 6`).

## 4c. What the S3 round trip settled (2026-08-21)

**The gate passes: 14/14 series, 75 events, 0 mismatches, 0 unrebuildable events.** Every
real quotable event was read back out of the database as an `EventPlan`, re-settled from a
recovered outcome, and re-written through the same `write_event` a synthetic world uses;
`event_pnl` then returned identical `edge`/`argmax`/`hybrid`/`staked`/`traded`/`stream`
against the rebuilt world and against production.

Passing was not free, and the four things it cost are the point of having run it:

**1. `settled_ts` is read, and it is point-in-time.** `energy._aaa_settled_mids` builds the
AAA drift prior from `settlements.settled_ts <= asof`. A world that stamped settlement at
the market close — or left it NULL — would hand that model outcomes before they were
knowable, with no symptom other than an energy model that looks better than it is. It is a
separate field on `EventPlan`, defaulting to the close, which is the conservative direction.

**2. Close time is per leg, not per event.** 10 of 808 real events quote legs with
different closes (KXFED/22DEC spans five). `quotable_events` filters candles against *each
leg's* close, so flattening them to the event maximum makes legs quotable that production
never quoted. Per-leg values win over the event-level one.

**3. Categorical markets cannot be laddered at all.** KXFEDDECISION is five mutually
exclusive `custom` legs with no numeric strike anywhere — the first round-trip run refused
all of it, correctly, via `_implied_outcome`. It is now settled by category (the ticker
suffix, which is exactly the key `decide_all._structs_categorical` prices against), and
which rule applies is read from `REGISTRY[series].structure`, never inferred from the legs.

**4. A world must be copied through sqlite's backup API, not `shutil.copyfile`.**
`macro.db` is WAL with a live writer, so everything committed since the last checkpoint is
in the `-wal` sidecar. A file copy drops it *intermittently*, depending on when the last
checkpoint landed — the worst available failure mode for a gate, since it would pass most
days.

Two decisions were made by refusing rather than guessing, and both stay: `custom`/NULL
strike types on a *ladder* raise instead of settling by a plausible default, and an event
whose stored settlements pin no consistent value is reported as unrebuildable rather than
approximated. Neither fired on today's data; both would have been silent wrong numbers.

## 4d. The C2ST was dead twice over, and what it says once revived (#185, 2026-08-27)

§1 names the trap: a generator fitted on the same history buys no license by itself. The
discriminative test — can a classifier tell a synthetic window from a real one? — is the
direct check on that, and for two rounds it was not running at all.

**Death 1: the duplicate detector could not find a duplicate.** It hashed each path after
dividing by a per-column scale computed *from that array*:

```python
s = np.abs(X).max(axis=0)          # real's own max for real, the pool's own max for the pool
hash((X[i] / s).round(9).tobytes())
```

Two identical paths on the two sides got two different divisors and therefore two different
hashes. It reported **0 duplicates on every panel and every arm**, and that zero was relayed
as "the duplicate hypothesis is refuted". It was never tested. Re-run with ONE scale vector
derived from the real paths and applied to both sides (`/tmp/dfm_verify/boot_inversion3.py`),
the duplicates are not merely present, they are the majority:

| panel | real paths | `boot` copies | `knn` copies |
|---|---|---|---|
| inflation_monthly | 331 | **265** | 199 |
| labor_monthly | 331 | **265** | 199 |
| claims_weekly | 690 | **540** | 410 |
| energy_weekly | 690 | **540** | 410 |

**Death 2: the pooling, not `block_bootstrap`.** `block_bootstrap` draws from fold *f*'s
training rows and `splits` purges, so no fold leaks on its own. The C2ST pooled across
folds: the real class was the union of every fold's held-out block, and a row held out in
fold 1 sits in the training set of folds 2..5, where the bootstrap draws it verbatim. Each
duplicated real path then carries label 1 once and label 0 *k* times, the Bayes output at
that point is 1/(1+*k*), and the real class ranks *below* its own copies.

That is the whole explanation of the inversion — `boot` and `knn` scoring 0.235–0.447, i.e.
**below** chance, which no honest two-sample test can do at scale. **Those numbers are void.**
Not "weak evidence", not "a baseline that ties": void. And they cannot be rescued by dedup,
because dropping the duplicated rows leaves those two arms with **zero** pool rows. A
per-fold-honest C2ST that never pools is the only version worth running, and that is #181.

**What survives, and it is not good news.** The two DFM arms — `local` (`fit_local`) and
`global` (`fit`) — have **0 duplicates on all four panels**, so their AUCs were never
touched by the bug and are unchanged by dedup:

| panel | `local` | `global` |
|---|---|---|
| inflation_monthly | 0.737 | 0.755 |
| labor_monthly | **1.000** | **1.000** |
| claims_weekly | 0.570 | 0.600 |
| energy_weekly | 0.790 | 0.861 |

A classifier separates DFM output from real windows **well above chance on all four panels,
and perfectly on `labor_monthly`.** Against §1's standard the answer is currently no: these
draws are not a valid diffusion of the real data in the discriminative sense, and the
measured `lambda = 0` of §6a now has a mechanism behind it rather than only a number.

One caveat that is a lead, not a hedge. An AUC of exactly 1.000 is bug-shaped, not
model-shaped: a generator that is merely *poor* lands at 0.7–0.9, whereas 1.000 means some
coordinate separates the two classes with a clean threshold — a scale, an offset, a
clipped tail. #181 must report **which coordinate does it** before anyone concludes the
labor panel's generator is beyond repair; the answer may be one un-inverted standardisation.

**Consequence for §8's gates: `dfm_gate` stays shut and #183 stays blocked.** Extending
coverage from 7 series to the traded universe would be widening the reach of a generator
that has not passed the one test that asks whether its output is real.

Artifacts: `/tmp/dfm_verify/boot_inversion{,2,3}.py`, `boot_inversion3.json`, `bootinv3.log`,
`c2st_control3.py`, `c2st_control3.json`.

## 5. The market book (S4) — the part that can kill the project

The model's side of the trade can be synthesized honestly. The market's side cannot be
invented: a fabricated counterparty means the argmin optimizes against our own pricing
error, which is worse than not running at all.

So the book is a **transplant**. For each of the ~63 real quotable events, extract a
unit-free descriptor of the book at each replay day, measured against the **incumbent**
(registered-default) prediction:

```
z_m = (market_mean - P_mean) / P_sd      how far the market sits from our view
r   = market_sd / P_sd                   how wide it is relative to our view
s   = half-spread in cents, per leg-price bucket
z_y = (outcome - P_mean) / P_sd          where the truth landed
```

A synthetic event supplies `z_y` (DFM) and `P_mean, P_sd` (the incumbent run on the
synthetic history). The book is drawn from the empirical joint of `(z_m, r, s)` given
`z_y`, pooled across all series — 63 events is thin for a per-series fit and ample for a
pooled one with a stated CI. The dependence of `z_m` on `z_y` is exactly the quantity that
decides whether edge exists at all, so it is estimated, never assumed.

The descriptor is anchored to the incumbent and held fixed across the grid, for the same
reason `pnl_score.gate_history` builds gate state once from the incumbent: a candidate
that was never deployed did not move the real market either.

### 5b. The measurement (2026-08-21) — the market knows, and that changes the design

Measured on **75 events / 496 replay-days**, all 13 numeric series (KXFEDDECISION is
categorical and has no numeric book moments). Open-ended `±inf` devig buckets are pinned
one `round_rule` step past the outermost finite strike, the same convention
`_implied_outcome` uses; the pinned mass is carried as `tail_mass` rather than dropped,
because renormalising it away would narrow the market's view for free.

| | pooled | by series |
|---|---|---|
| `corr(z_m, z_y)` | **+0.703** (day-rows), **+0.711** (event means) | +0.29 … +0.98, positive on all 12 fittable |
| our error `mean\|z_y\|` | **0.90** | — |
| market error `mean\|z_y − z_m\|` | **0.78** | — |
| `r` (market sd / our sd) | median **1.04**, IQR 0.66–1.63 | 0.40 (KXPAYROLLS) … 3.01 (KXCPICOREYOY) |
| half-spread | median **1.0¢**, p95 9.6¢ | — |

Two consequences, and they are the reason this section is "the part that can kill the
project":

**1. The market is a better forecaster than we are on this sample** — 0.78 against 0.90,
measured in *our own* sd units. Any synthetic book drawn with `z_m` independent of `z_y`
would be an uninformed counterparty, and the strategy would print money against it. So the
+0.70 dependence is not a nuisance parameter to be smoothed over; reproducing it *is* the
deliverable, and a synthetic world that loses it is worse than no synthetic world.

**2. Therefore the transplant is at EVENT granularity, not day granularity.** A whole
donor event's trajectory — its `(z_m, r, half-spread)` path across replay days, its
standardized market pmf shape, and its ladder offsets — moves across together, selected by
nearest `z_y`. Drawing each day independently would destroy both the market's convergence
toward the close and its correlation with the outcome, which are the only two things here
worth preserving.

`r`'s spread across series (0.40 to 3.01) also rules out a pooled constant width: a
transplant that used the pooled median would make KXPAYROLLS' market 2.5× too wide and
KXCPICOREYOY's 3× too tight, which is a direct thumb on the edge the strategy is measured
finding.

**Gate for S4 (added):** the synthetic `z_y*` distribution must OVERLAP the donor `z_y`
range. If the incumbent's `P_sd` on synthetic worlds is systematically different from its
`P_sd` on real ones, `z_y*` lands outside the donor pool and every draw is an
extrapolation. That is checkable, so it is checked and reported rather than assumed.

### 5c. What the S4 build measured (2026-08-21) — and the two things that were wrong

Built end to end on KXJOBLESSCLAIMS: 8 DFM paths × 12 weekly events = **96/96 synthetic
events**, every one quotable, all scored by unmodified `pnl_score.event_pnl`.

**The overlap gate passes.** `n_donors=75, n_synth=96`; donor `z_y` range
[−5.14, +4.60] contains the synthetic range [−2.23, +4.30]; median gap to the nearest
donor 0.020, p95 0.143, **outside = 0.0**. No draw is an extrapolation.

The gate that mattered, though, was the one §5b argued for: re-measure the descriptor on
the finished worlds — reading candles back out through `_implied`/devig, the same path the
real measurement used — and check that the +0.571 dependence actually arrived. The first
build delivered **+0.276**, less than half. Decomposed link by link (`/tmp/wfdev/diag_chain.py`):

| link | measured |
|---|---|
| 1. real `corr(donor z_m, donor z_y)` | **+0.571** |
| 2. selection `corr(donor z_y, target z_y)` | +0.898 |
| 3. transplant `corr(donor z_m, target z_y)` | +0.445 |
| 4. devig round trip `corr(recovered z_m, donor z_m)` | +0.908 |
| 5. delivered `corr(recovered z_m, target z_y)` | **+0.258** |

Link 4 is a floor we cannot cross: quotes are integer cents, so a book written at `z_m`
and read back through devig returns `z_m` with 0.908 correlation no matter what. Links 2
and 3 were defects.

**Defect 1 — ladder geometry is a venue fact, not a market view.** The original `draw`
moved the whole donor across, offsets included, so a 40-leg KXNATGASW ladder could be
stamped onto a claims event. Saturation (fraction of quotes pinned at the 1¢/99¢ band) by
series on the real book: claims **19.7%**, KXWTIW 29.3%, KXAAAGASW 63.0%, KXNATGASW
**75.9%**. A saturated ladder carries no information about `z_m` — the price is at the band
regardless — so mixing geometries drowned the signal: synthetic saturation came out 49.1%
against the real 19.7%. `draw_ladder` now takes offsets from the **target series' own**
donors and refuses a series whose geometry has never been observed. **+0.276 → +0.406.**

**Defect 2 — the k=10 selection smear attenuates, and is correctable in closed form.**
k=10 buys donor diversity at a `z_y` mismatch of 0.24 mean, biased −0.12. Since
`z_m = a + b·z_y_donor + e` on the pool, shifting every day of the drawn donor by
`b·(z_y_target − z_y_donor)` yields `a + b·z_y_target + e`: the real slope restored, the
donor's own residual `e` untouched. It cannot overshoot into a fabricated dependence
precisely because `e` is not touched — there is a regression test pinning that. `b` is
re-measured on the pool it is handed (`zm_slope`), never hardcoded, so a pool rebuilt after
more events settle re-measures. **+0.406 → +0.500.**

**+0.500 is the ceiling, not a shortfall.** 0.571 × 0.908 = 0.518, and the remaining 0.018
is sampling noise on n=672 day-rows. The transplant now delivers everything the cent
ladder can carry.

Final descriptor, real donors vs synthetic worlds re-measured (q25/q50/q75):

| | real | synthetic |
|---|---|---|
| `z_m` | −0.245 / +0.262 / +0.557 | +0.326 / +0.603 / +0.910 |
| `r` | 0.657 / 1.055 / 1.628 | 0.660 / 0.873 / 1.143 |
| half-spread | 0.007 / 0.010 / 0.019 | 0.006 / 0.008 / 0.018 |
| `tail_mass` | 0.005 / 0.011 / 0.053 | 0.015 / 0.035 / 0.121 |
| `corr(z_m, z_y)` | **+0.571** (n=496) | **+0.500** (n=672) |
| our error `mean\|z_y\|` | **0.964** | **0.725** |

**The number S5 has to price.** `mean|z_y|` is 0.725 synthetic against 0.964 real: the
incumbent is materially *more accurate* on synthetic worlds than on real ones, which
inflates strategy PnL there directly. The residual dependence gap (+0.500 vs +0.571) points
the same way — a slightly less informed counterparty. Both biases say the synthetic world
is **easier to trade than reality**, which is precisely what `lambda` exists to discount.
Strategy on the 96 synthetic events for the record: `hybrid mean +1.022, sd 7.143,
se 0.729, win 45%, traded 96/96, staked mean 1.136` at `BANKROLL = 100.0`.

Two smaller defects the build caught, both now regression-tested:

* `fred_obs` is keyed `(sid, event_time, vintage_date)`, so `INSERT OR REPLACE` landed
  synthetic prints **beside** real ones whenever the vintage date differed by a day, and
  `FeatureStore` would have served a mixture of the two worlds. `write_fred`/`write_fut`
  now DELETE from the generated path's start before inserting.
* Real Kalshi events of the target series survived in the world alongside the synthetic
  ones, so `quotable_events` would have scored both. `clear_series` removes them.

### 5d. The settlement transform, checked against every real settlement (2026-08-21)

`SETTLES` is ten one-line claims of the form "KXU3 settles on the LEVEL of `unrate`, which
the panel carries as a difference" or "KXPAYROLLS settles in jobs while PAYEMS is in
thousands". Each is a sentence someone can write confidently and get backwards, and a world
built on a backwards one is perfectly self-consistent while scoring a market nobody trades.
`build.verify_settle` closes that by recomputing every real settled outcome from the real
panel and asking it to fall inside the interval the legs actually paid — interval, not
equality, because the stored outcome is a bucket midpoint and is up to half a ladder step
from the truth by construction.

Across all ten series against the production db at 2026-08-21: **446 consistent, 38 not.**
A units error would be 100% wrong, not 8%, so **every transform in `SETTLES` is confirmed.**

| series | ok | bad | skipped | what the residual is |
|---|---|---|---|---|
| KXPAYROLLS | 38 | 0 | 3 | — |
| KXU3 | 42 | 0 | 19 | — |
| KXJOBLESSCLAIMS | 50 | 1 | 0 | the 2025 shutdown, below |
| KXWTIW | 139 | 4 | 12 | reference contract |
| KXNATGASW | 15 | 4 | 1 | reference contract |
| KXCPI / KXCPICORE | 35 / 37 | 8 / 5 | 19 / 8 | seasonal revision |
| KXCPIYOY / KXCPICOREYOY | 34 / 39 | 7 / 4 | 4 / 1 | seasonal revision |
| KXPCECORE | 17 | 5 | 1 | seasonal revision |

Three residual causes, all measured rather than assumed:

* **CPI family — annual reseasonalization.** Every one of the 29 CPI/PCE misses is exactly
  **one 0.1pp bucket** outside the interval. These columns read the LATEST vintage on
  purpose (§4: BEA rebases PCEPILFE and a first-print chain across a rebase produced an 8.8%
  "core PCE print"), and BLS re-estimates seasonal factors each January, so the current
  vintage's MoM differs from the one that settled by about a tenth. The cost is a settlement
  value up to one bucket off in the synthetic world, which is the same order as the bucket
  midpoint error already present by construction. Reading first prints instead would trade a
  0.1pp error for a several-percent one.
* **WTI / natgas — a different reference contract.** For all four bad NG weeks the implied
  interval matches a *neighbouring session's* bar and never the Friday settle
  (e.g. 2026-07-24 target [2.899, 2.999], our Friday bar 2.871, matching only Jul 22–23).
  That is a front-month roll or a different settlement reference, not a units error; the
  generator's own path is internally consistent either way.
* **KXJOBLESSCLAIMS — one week, and it is the shutdown.** For the week ending 2025-09-27
  the earliest ALFRED vintage is **2025-11-20**, a 54-day lag against a normal 5: the release
  cycle was suspended and the advance print that settled the contract does not exist in the
  vintage record at all. Every ICSA week from 2025-09-27 to 2025-11-08 carries the same gap.

**The one actionable finding: `claims` was training on the wrong vintage.** `_WEEKLY_COLS`
carried ICSA at `prints="latest"` while `claims.predict` reads it through
`fred_first_prints` and KXJOBLESSCLAIMS settles on the advance print. Measured on the
production db: 1042 of 3110 weeks carry a revision, mean |rev| 4,228 claims, and the advance
print is the **noisier** series — `dlog` sd **0.07150 first vs 0.06729 latest**. Training the
generator on the revised chain therefore understated settlement noise by ~6%, a direct and
measurable contribution to §5c's finding that the synthetic world is easier to trade than
reality. Changed to `prints="first"`; `verify_settle` on KXJOBLESSCLAIMS went **44/7 → 50/1**,
the remaining one being the shutdown week above. `_MONTHLY_COLS` keeps `"latest"`, because
there ICSA is a context feature for `payrolls`/`u3`, which read it through `fred_series`.

## 6. Calibrating lambda (S5)

For each weekly series (KXWTIW, KXNATGASW, KXAAAGASW, KXJOBLESSCLAIMS — n_real 10–11):

1. Build synthetic worlds for that series and run the full grid on them.
2. Take the synthetic argmin winner.
3. Score that winner on the **real** events it never saw.
4. Compare against (a) the default set and (b) the real-events argmin winner.

Repeat over folds and seeds. The fraction of the synthetic-claimed improvement that
survives on real events is the exchange rate between a synthetic and a real observation.
`lambda` is set at the lower end of its bootstrap interval, not its point estimate.

### 6a. What it measured — 2026-08-21: **lambda = 0**

Three series carried a scoreable real sample (KXJOBLESSCLAIMS n=4, KXWTIW, KXNATGASW).
Everything below is on **improvement against `grid[0]`**, never on the PnL level, because
§5c already showed the synthetic world is uniformly easier to trade — a common level shift
has to cancel or it would be read as agreement.

* **Reference-free decision test.** Take the synthetic argmin's pick, look up where its
  *real* improvement falls in the distribution of real improvements over the whole grid.
  A useless sample lands at the 50th percentile. Measured: **86.8% / 19.0% / 28.6%**,
  mean **44.8%**. On two of three the synthetic pick is worse than picking the default.
* **Pooled correlation.** Bootstrapped cross-series mean `rho` **+0.166**, 5th percentile
  **−0.095**, `P(mean rho <= 0) = 0.20`. Taking the lower end as the method requires gives
  `lambda = 0.0000`. The degenerate min-across-series rule and the fairer mean rule agree.
* **Saturation** is incoherent across series (last doubling of `n_paths` moves rho by
  −0.024 / +0.012 / +0.111), which is itself evidence that rho here is mostly noise.

**The caveat that matters, and it is not a formality.** Split-half reliability with a
Spearman-Brown correction on the *real* improvement vector is **+0.439 / −0.272 / −0.561**.
`rho` is bounded above by `sqrt(rel_real * rel_synth)`, so on KXWTIW and KXNATGASW the real
reference cannot correlate with *itself* and therefore cannot correlate with anything.
That is **non-identification, not refutation**: those two series say nothing about the
synthetic sample either way, and NG is noise-dominated on both sides (synth reliability
−0.522). Only KXJOBLESSCLAIMS is identified, and it is n=4. `calibrate.py` now reports
`rel_real`, `rel_synth`, `rho_disattenuated` and an `identified` flag by construction, so
this can never again be read as a verdict when it is an absence of evidence.

So `lambda = 0` is **the measured value, not a placeholder** — but it is measured on a
sample too small to have found a positive value if one existed. The store and the weekly
regeneration ship anyway: the sample exists, stays fresh, and the lane switches on with no
code change the day a lambda row lands. The honest next measurement is a walk-forward
calibration on the **monthly** series — rolling cutoffs over ~2 years, pooling many more
real events, and removing the weekly→monthly extrapolation this one had to make.

### 6b. The walk-forward that is actually possible (S5-WF, built 2026-08-21)

The "~2 years" above was written without checking the book. Checked: **candles begin
2026-05-16** (verified in every backup db — they all start there), Kalshi deletes them at
75 days, and the real reference for any month before May 2026 therefore does not exist
and can never be recovered. Each monthly series has exactly **3** quotable settled
releases today (26MAY/26JUN/26JUL periods; KXPCECORE 26APR–26JUN). This is a data-reality
ceiling of the same species as the 14% K-line coverage ceiling — documented, not coded
around.

What ships instead is an **accrual** (`calibrate.wf_accrue` / `wf_aggregate`, weekly
steps): every monthly release that settles adds one real improvement row per series,
scored point-in-time — generator spliced at `close − 75d`, the exact S5 geometry — and
stored in `synth_wf_mats` (worlds deleted after scoring; the matrices are the
measurement). Aggregation intersects drifted grids by `set_hash`, correlates at
`n_real ≥ 3`, identifies at `≥ 4`, and **persists the series' own measured lambda** when
warranted; `synth_lambda()`'s read order then makes it govern over the pooled `'*'` row
with no code change.

The persistence gate is asymmetric on purpose: `lam_lo > 0` persists at `n ≥ 4`; a
**zero** does not persist until `n ≥ 8`, because at n=4 the bootstrap lower bound of
anything is 0 (§6a measured exactly that on a series whose pick beat the default at the
86.8th percentile) and a measured per-series row SHADOWS `'*'` — an artifact zero would
re-kill the feature on schedule every month.

Two honesty notes carried on every stored row's meta: the grid is TODAY's ladder union
(a PIT grid design cannot exist — the pre-cutoff probe window is empty before 2026-05),
so key-liveness leaks backwards while outcomes never enter design; and the donor book
pools the whole recorded span including post-release quotes, the same practice §5b
measured under. Timeline: with the 26AUG releases settling in September, every monthly
series reaches the n=4 identification floor; n=8 lands around New Year.

**First backfill, 2026-08-21 (21 releases, 3.1 h).** All seven series stored 3/3
releases; every aggregate correctly reports *measured but unidentifiable* (n=3 < 4) and
correctly persists nothing. The preliminary readings, reported for the record and NOT for
conclusions — at n_real=3 these are noise-level:

| series | rho | pick_pct (strict, as first read) | pick_pct (mid-rank, correct) | rel_synth | note |
|---|---|---|---|---|---|
| KXPAYROLLS | +0.182 | 77.0% | **88.3%** | −0.653 | pick beats null; synth side unreliable |
| KXCPIYOY | +0.217 | 26.9% | **63.4%** | −0.638 | synth side unreliable |
| KXPCECORE | 0.000 | 0.0% | **50.0%** | 0.000 | K=5; real matrix entirely flat — no information, not a bad pick |
| KXCPICORE | −0.283 | 34.7% | **40.6%** | +0.699 | |
| KXCPI | −0.132 | 14.9% | **15.7%** | +0.122 | barely tied; a genuine below-null reading |
| KXU3 | −0.068 | 9.9% | **14.9%** | +0.633 | |
| KXCPICOREYOY | −0.009 | 0.0% | **13.8%** | +0.539 | |

**The strict column is wrong — see §6c.** Mean pick percentile is **41.0%** pooled per
series (not ≈27%), and per-release **48.7%** against a null of 50, sign test 7/21 above
null, **p = 0.19**. The monthly finding at n=3 is therefore *no information either way* —
NOT the apparent refutation the first read produced. It is consistent with, not opposite
to, the weekly decision test (44.8%).

The accrual behaves identically either way: it keeps refusing to persist zeros (the n≥8
rule). What changed is what a reader should conclude today, which is nothing. Decision
points unchanged: first identified readings in September (n=4); evidence-grade by New
Year (n=8). Nothing here changes the daily lane — the gate, the discount and the blend
behave exactly as registered.

### 6c. The tie rule that manufactured a refutation (found and fixed 2026-08-26)

`pick_percentile` scored with a strict `<`: `(mr < mr[j]).mean()`. On a 75-day window most
candidate parameter sets move **no decision at all**, so real improvement vectors are
heavily tied — and when *every* candidate ties, that expression returns 0.0 for every `j`,
including the oracle. A release carrying zero information was therefore recorded as a **0%
pick**, the most damning value in the range, instead of the 50% that "no information" means.

This was not a rounding detail. **8 of the 21** stored monthly releases have a completely
flat real matrix. Scored strictly they read mean **16.7%**, sign test **p = 0.000** — which
looks like hard evidence that the synthetic sample actively picks badly. Scored with the
standard mid-rank `(#below + 0.5·#tied)/k` — 50% under the null with or without ties — the
same 21 releases read **48.7%**, **p = 1.000**.

Three things make this survivable rather than serious:

1. **It never touched a lambda.** `pick_percentile` feeds reports and `detail_json` only;
   lambda comes from `agreement` / `_disattenuated_lam`. No persisted lambda changes, and
   the production `'*' = 0.1356` is unaffected.
2. **Its direction is always conservative.** mid-rank ≥ strict identically, so the bug could
   only ever *understate* the synthetic sample. It biased the project toward distrusting
   DFM, never toward over-trusting it.
3. **The §6a weekly readings stand.** Those matrices are deleted and cannot be re-scored —
   but all three have a non-zero `real_improve_of_synth_pick` on record (+0.0675 / −0.090 /
   −0.076) and non-zero oracles, so none is the degenerate flat case. 44.8% remains the
   weekly number and λ = 0 there remains correctly derived.

Fixed in `pick_percentile`: `percentile` is now the mid-rank; `percentile_strict`,
`tied_frac`, `n_distinct` and `uninformative` ride alongside so a flat matrix can never
again be mistaken for a result. `wf_aggregate` surfaces all four. 5 tests pin it, including
the exact degenerate case and the mid-rank ≥ strict invariant over 200 random matrices.

**Method note worth keeping:** this came from checking the null against the data, not from
reading the code. A statistic whose null is 50% returning a **median of exactly 0.0%** across
21 independent samples is not a strong result, it is a broken estimator. "Too strong to be
true" is a testable signal and should be tested before it is reported.

## 7. Storage and cadence (S7) — as built, 2026-08-21

`research/synth/regen.py`, wired into `ops/refresh.py`'s **weekly** block as
`weekly_synth_regen`. The split is the point: this is the only step that imports torch, and
the morning `param_argmin` pass reads `synth_scores` and never touches a world again.

| where | what | lifetime |
|---|---|---|
| `macro.db synth_runs` | one row per (series, build): cutoff, splice, grid hash, provenance | newest 3 per series, then pruned |
| `macro.db synth_scores` | one row per candidate: `set_hash`, mean and sd of synthetic PnL | with its run |
| `data/synth/<series>/world_*.db` | the worlds themselves | one generation, gitignored |
| `data/synth/donors.json` | the pooled donor book | 30 days (`DONOR_MAX_AGE_DAYS`) |

The consumable half lives in `macro.db`, so `ops/backup_db.py` already covers it. The worlds
do not, deliberately: ~40 MB each, ~290 MB per series per run, and reproducible from the
snapshot plus the seed. They are kept between runs only so `rescore_latest` can re-score a
drifted grid without paying for generation again, and last week's copies are deleted **after**
the new ones exist so a crash mid-generation leaves the old sample in place rather than none.

Three decisions worth recording, each of which was a bug first:

* **Cutoff is `now`, not the start of the real window.** `calibrate` splices at the window
  start so synthetic and real events overlay and can be compared; production must not, because
  the parameters being chosen will be used on the *next* month. Conditioning on today's anchor
  is what makes the draws sit close to 当前环境, and it also makes every generated event
  genuinely out-of-sample — it has not happened yet.
* **What is scored is the LADDER UNION, not today's grid.** The morning grid is a function of
  `n_eff = n_real + lambda * n_synth`, and both halves move between this job running and the
  sample being read. `param_argmin.grid_ladder` enumerates every grid the cap ladder can
  reach and scores their union. Measured on KXPAYROLLS: 10 reachable grids of width
  1/3/4/5/9/13/17/33/49/97 unioning to **222** candidates — not 97, because narrowing drops
  whole *keys*, so a narrow grid is not a subset of a wide one. That 2.3× premium is what
  makes the sample usable at whatever lambda turns out to be, and it is paid once a week in
  a 3am job rather than every morning before the board trades.
* **It stores at lambda 0.** Otherwise the table stays empty until lambda is measured, and is
  then empty at the exact moment it is first needed. Storing unconditionally is what lets the
  lane switch on with no code change the day a lambda row lands.

### 7a. What it costs, and why scoring is a pool

Scoring is effectively the whole bill; generation is ~2 min a market. `event_pnl` costs
**~215 ms per (event, candidate) pair**, and the seven monthly markets come to **111,536
pairs ≈ 380 min**. KXPAYROLLS alone is 88 × 222 = 19,536 and had run 70 min when it was
timed.

| market | n_synth | K_union | pairs | serial |
|---|---|---|---|---|
| KXPAYROLLS | 88 | 222 | 19,536 | 70.0 min |
| KXCPICORE | 88 | 228 | 20,064 | 71.9 min |
| KXCPICOREYOY | 88 | 228 | 20,064 | 71.9 min |
| KXCPI | 88 | 201 | 17,688 | 63.4 min |
| KXCPIYOY | 88 | 201 | 17,688 | 63.4 min |
| KXU3 | 88 | 121 | 10,648 | 38.2 min |
| KXPCECORE | 88 | 5 | 440 | 1.6 min |
| | | | | **6.3 h** |

That does not fit inside `refresh`'s flock, which the weekly block holds for the duration —
it would have pushed `weekly_eval_gates` and everything behind it into Sunday afternoon and
made every 15-minute tick in between take `RefreshBusy`.

**It is not a scan that could be indexed away, and that was worth checking.** 215 ms looks
exactly like an unindexed query, and the generated worlds do carry fewer indices than
`macro.db`. But `event_pnl` re-runs the *forecasting model* for each candidate — `params`
changes the model — so the quote tape genuinely cannot be loaded once and reused across the
grid, and `_pmf_for` re-runs it again per held day. The function is also required to stay
bit-identical to `walkforward`'s inner loop, and both S5 and S6 are comparisons against the
real-sample matrix formed by that same code, so reimplementing a fast path would invalidate
the measurement it exists to serve.

So the only lever that changes nothing about *what* is computed is concurrency, and worlds
are independent SQLite files. `score_matrix` pools over **worlds** — not over the grid,
which would reopen each world K times, and not over events, which would put workers in
contention for one file's page cache.

**Measured, full seven-market pass, 2026-08-21: 3,039.8 s = 50.7 min** against the 6.3 h
serial projection — **7.5×** on 8 workers, on a machine already carrying the crypto
recorders and the controller at load ~19. Per market, `kept/generated × K_union`:

| market | kept/generated | K_union | ladder |
|---|---|---|---|
| KXPAYROLLS | 80/88 | 222 | 1/3/4/5/9/13/17/33/49/97 |
| KXU3 | 85/88 | 121 | 1/4/10/28/82 |
| KXCPI | 56/88 | 201 | 1/3/4/7/10/19/28/55/82 |
| KXCPICORE | 85/88 | 228 | 1/3/4/7/10/19/28/55/109 |
| KXCPIYOY | 72/80 | 201 | 1/3/4/7/10/19/28/55/82 |
| KXCPICOREYOY | 78/80 | 228 | 1/3/4/7/10/19/28/55/109 |
| KXPCECORE | 84/88 | 5 | 1/5 |

Two things in that table are worth naming rather than glossing. **KXCPI keeps 56/88 where
its own core twin keeps 85/88** — same panel, same generator hash, same ladder width, so
the drop is a property of that series' quotable events, not of the CPI family; it is
logged, not silently absorbed, and is the first thing to look at if KXCPI's blend ever
reads oddly. And **KXCPI/KXCPIYOY share generator hash `3d906423f0f81007` while
KXCPICORE/KXCPICOREYOY share `d57899f2f4e34186`** — the year-over-year and month-over-month
markets on one index are priced off one generator, which is the intended de-duplication and
a useful integrity check that the panel routing is not crossing indices.

The pass runs under `python -m`, which is how `ops/refresh.py` enters it, and that path was
verified separately: spawn re-imports the parent's `__main__` in each child, so a caller
without an `if __name__ == "__main__"` guard dies with `BrokenProcessPool`. A `-m` harness
driving the real `_score_world` against real world files printed its module body exactly
once and matched the serial matrix.

The hazard that needed a test rather than a review: `mat[i]` is paired to `kept[i]`, every
downstream use is a paired comparison, and `as_completed` yields in *completion* order — so
extending as futures land would permute the sample silently, looking like noise rather than
a bug. Results are slotted by world index and concatenated in the same sorted order the
serial path walks; two tests drive a pool whose futures complete backwards, one checking the
order and one checking each row still names its own world, because "right events, rows
shifted by one world" survives an order check.

### 7b. Does the stored sample actually reach the morning lane?

Directly tested rather than argued, by running `param_argmin.rescore` on a copy of the live
DB three ways against KXPCECORE's stored run:

| | lambda | n_eff | cap | K | outcome |
|---|---|---|---|---|---|
| A | no row (as shipped) | 2.0 | 1 | **1** | refused: `"lambda is zero — the synthetic sample carries no weight"` |
| B | 0.136 | 4.99 | 11 | 5 | `blended=True` |
| C | 1.0 | 24.0 | 20 | 5 | `blended=True` |

The point is arm A. With `n_real = 2` the sample gate allows **one** candidate — the market
cannot rank anything at all, which is the gate working as designed on a market with almost
no settlements. A synthetic sample is the only thing that reopens it, and at lambda 0.136 it
does: `cap` goes 1 → 11 and the whole reachable ladder becomes searchable. So the wiring is
live end to end and the only thing standing between it and effect is the measured lambda.

On this market the reopened search then chose the default anyway (`best = {}`,
`pnl 2.5 → 2.5`), which is the honest outcome to report: the mechanism is proven, the
improvement is not.

Cadence is weekly, not monthly: a weekly regeneration keeps every stored run inside
`param_argmin.SYNTH_MAX_AGE_DAYS = 45`, which is the daily lane's own staleness limit.
Scope is the **monthly** markets only, derived from the panel frequency rather than tabulated
— the weekly ones settle 10–11 times in a 75-day window, so `sample_cap` sits far above their
static `CAP` and a synthetic sample buys them nothing. The weekly series are still generated
on request by `synth/calibrate.py`, because they are the only place lambda can be measured
against a real sample at all.

### 7c. The chain is complete and it is inert — `synth_lambda` has no writer

The full-pass run stored what it was supposed to store: 7 rows in `synth_runs`, **1,206** in
`synth_scores` (one per candidate per market — 222+121+201+228+201+228+5, so every union
member has a mean), and 8 world dbs per market on disk. Every market reported `ok`. And
every market also reported `weight=0.0`, and the daily-lane view of all seven reads:

```
{"lambda": 0.0, "source": null,
 "note": "no lambda measured — synthetic sample unused",
 "skipped": "lambda is zero — the synthetic sample carries no weight"}
```

`synth_lambda` is **empty — 0 rows**, and `grep` finds no writer anywhere in the package:
`param_argmin.synth_lambda` reads it, `regen.run` reads it to compute `weight`, and
`calibrate.py` *computes* the quantity but never persists it and no job calls it. So the
pipeline is a complete circuit with an open switch. This is the honest status against the
requirement that the feature be *integrated and effective*: **integrated yes, effective no.**

Note what this is not. It is not the §6a measurement deciding against the sample — §6a's
`lambda = 0` came out of a comparison whose *reference* had negative split-half reliability
(−0.272 KXWTIW, −0.561 KXNATGASW), i.e. non-identification, and §7b showed a merely
plausible 0.136 takes KXPCECORE's cap from 1 to 11. It is a missing persistence step. The
distinction matters because the fix for a refutation is to abandon the feature and the fix
for an open switch is to close it, and only one of those is warranted here.

Two consequences to carry forward: the pooled `'*'` row is the intended vehicle for the
monthly markets (`synth_lambda`'s docstring already commits it to be the *min* of the
per-series lower bounds, so an unmeasured market inherits the worst case rather than an
average it has no claim to); and any row written must record on its face whether it was
measured or pre-registered, because `lam_point`/`lam_lo`/`lam_hi` are the only place that
distinction can survive into the daily log.

### 7d. Residual: the weekly block holds `refresh.lock` for the whole pass

`weekly_synth_regen` runs inside `refresh.run()`, which takes a non-blocking flock on
`data/output/refresh.lock` (`ebdf6a9`). At 50.7 min the Sunday 10:30Z weekly therefore holds
that lock for most of an hour, and the 15-minute ticks landing in the window will log
`✗ daily_refresh: pid=… started=…` and `mark_late`, then be picked up by the next tick.
That is the designed behaviour — refuse rather than queue — and it is contained, because
only the three daily tasks route through `refresh.run()`. It is recorded here so the Sunday
log is not mistaken for a fault.

## 8. Order of work and gates between stages

| stage | done when |
|---|---|
| S1 panel | **DONE** (`bd8540f`+). Leakage test passes: no row contains a value dated after the window's right edge, `integrate` inverts the increment transform exactly, and each column reads the vintage its consuming model reads. See the point-in-time caveat in §4 |
| S2 generator | purged blocked k-fold, three arms. **Paired CRPS vs `block_bootstrap` must be < 1 with t < −2** (the conditional arm has to be sharper than the unconditional resample of its own history), **and** rank calibration must not degrade materially against it. `knn_bootstrap` clears this on all five panels; the global conditional DFM does not and is a **documented negative**. The shipped arm is `fit_local`, which must additionally TIE its own `knn` on identical rows — see §4b |
| S3 worlds | **DONE** (2026-08-21). **Round-trip proof passes 14/14 series, 75 events, 0 mismatches**: every real event rebuilt through `write_event` reproduces production `event_pnl` on `edge`/`argmax`/`hybrid`/`staked`/`traded`/`stream`. Re-run with `worlds.roundtrip(conn, series)`. See §4c for the four defects it caught |
| S4 book | **DONE** (2026-08-21). Overlap gate passes with **outside = 0.0** (median gap 0.020, p95 0.143; synthetic `z_y` [−2.23,+4.30] inside donor [−5.14,+4.60]). Delivered `corr(z_m, z_y) = +0.500` against the real **+0.571**, the residual accounted for by the measured devig round trip of +0.908 (0.571×0.908 = 0.518) — i.e. at the cent-ladder ceiling. Half-spread q25/q50/q75 synthetic 0.006/0.008/0.018 vs real 0.007/0.010/0.019; `r` 0.660/0.873/1.143 vs 0.657/1.055/1.628. **Carry into S5:** `mean\|z_y\|` is 0.725 synthetic vs 0.964 real — the synthetic world is easier to trade than reality. See §5c |
| S5 lambda | **DONE** (2026-08-21). Reported whatever it said, and it said **zero**: pooled `rho` 5th pct **−0.095**, `P(mean rho <= 0) = 0.20`, and the reference-free decision test puts the synthetic pick at the **44.8th** percentile of real improvement against a null of 50 — worse than the default on 2 of 3 series. **Read the identification caveat before reading that as a refutation**: real-sample split-half reliability is −0.272 (KXWTIW) and −0.561 (KXNATGASW), so on those two the reference cannot correlate with itself, which caps `rho` at zero by construction. Only KXJOBLESSCLAIMS is identified and it is n=4. See §6a |
| S6 wiring | **DONE** (2026-08-21). `n_eff = n_real + lambda*n_synth` feeds `sample_cap`; `_objective` blends only a grid the stored run fully covers and logs the refusal otherwise; every gate log carries `n_eff` and a `synth` block naming the run, its age and its lambda. Building it exposed a **real defect in the gate itself**: it sized the grid on the *quotable* universe while `score_matrix` keeps only what replays, so KXJOBLESSCLAIMS — skill-BLOCKED since 2026-07-09, `0/91` sets scoring on its last 6 events — was ranking 91 sets on **4** events (1.50 sd of selection bias, worse than the KXPAYROLLS case that motivated the gate) while the log read n=10. `resolve_grid` now re-narrows on the scored sample, never widens, and says which count it resized on |
| S7 ops | **DONE** (2026-08-21). See §7. `weekly_synth_regen` in `ops/refresh.py`; one market's generator failure cannot starve the other six; `synth_runs`/`synth_scores` ride `backup_db`; worlds are one generation deep and gitignored. **Proven on a full seven-market pass**: 50.7 min wall (7.5× the serial projection), 7/7 `ok`, 7 `synth_runs` + 1,206 `synth_scores` rows, `-m` spawn path verified. **All seven report `weight=0.0`** — see §7c, `synth_lambda` has no writer |
| S8 lambda persistence | **DONE** (2026-08-21, `cb9a04f`). `calibrate.persist` writes per-series rows under the committed lower-bound rule (zeros included — a measured refusal must shadow a positive pool) and the pooled `'*'` row under a three-step policy: measured `min(lam_lo)` over IDENTIFIED series when positive, else the pre-registered min squared **disattenuated** rho over identified series, else nothing (no invented floor). On the 2026-08-21 measurement: `'*' = 0.1356` from KXJOBLESSCLAIMS (`rho_disatt +0.3682` — the same number §7b probed as "plausible", now with a derivation). Production seeded (rows + runs via the same `regen.run` the Sunday weekly uses; pre-change snapshot `macro_pre_s8_lambda.db`). `weekly_synth_lambda` states the switch position in the weekly log. 9 tests |
| S5-WF monthly accrual | **BUILT** (2026-08-21, see §6b). "2 years of cutoffs" is impossible — candles begin 2026-05-16, 3 releases per series exist. `wf_accrue`/`wf_aggregate` accrue one PIT-scored row per release into `synth_wf_mats` and self-persist a series' own measured lambda at identification, with the asymmetric zero gate (n≥8) so an underpowered zero cannot shadow `'*'`. First 21-release backfill running 2026-08-21 overnight; n=4 identification lands with the September settlements |

Nothing writes to production state until S5 has a number. S5 now has one, and it is zero,
so what ships is the machinery and not yet its influence: `weekly_synth_regen` builds and
stores the sample every week, `param_argmin` reads it every morning, and `read_synth`
refuses it out loud — `"lambda is zero — the synthetic sample carries no weight"` — so
`n_eff == n_real` and the chosen parameters are bit-identical to the pre-S6 lane. The one
thing that is *not* deferred is the gate defect S6 uncovered, which was real, independent
of any of this, and is fixed.

That paragraph was written before the full pass, and it credits the inertness to S5's
number. §7c corrects it: **even a non-zero S5 would change nothing today**, because
`synth_lambda` has no writer at all — `read_synth` takes the `source: null` branch, not the
`lam <= 0` branch reached through a stored row. Both roads end at the same log line, which
is exactly why it went unnoticed.

**S8 closed the switch the same day** (`cb9a04f`): `'*' = 0.1356` is live in production,
the runs are seeded, and the first blended morning pass is 2026-08-22 09:00Z. The upgrade
path from pre-registered to measured is the §6b accrual, and it completes itself as
monthly releases land — no further code is waiting on anything.
