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
coordinate separates the two classes with a clean threshold. #181 must report **which
coordinate does it** before anyone concludes the labor panel's generator is beyond repair.

> **Correction, same day.** The sentence that stood here guessed the answer — "the answer
> may be one un-inverted standardisation" — and the guess is **refuted**. `auc1_probe.py`
> scored the Mann-Whitney AUC of all 36 coordinates one at a time: the largest
> |AUC − 0.5| is **0.026** (`local`) and **0.050** (`global`). No raw number separates the
> classes, so there is no scale, offset or clipped tail to find. The real mechanism is
> §4e-A, and the difference matters: a standardisation bug is a typo, a missing
> discretisation is a modelling omission that had to be designed around.

**Consequence for §8's gates: `dfm_gate` stays shut.** Extending coverage to the traded
universe (#183) would widen the reach of a generator that has not passed the one test that
asks whether its output is real. #183 is **gated on the §4e fixes, not abandoned** — the
standing instruction is that the DFM must be used and must be repaired until its output is
valid statistically and in utility, not shelved.

Artifacts: `/tmp/dfm_verify/boot_inversion{,2,3}.py`, `boot_inversion3.json`, `bootinv3.log`,
`c2st_control3.py`, `c2st_control3.json`, `auc1_probe.py`.

## 4e. Why it separates: three named defects, each with a working control (#181, 2026-08-27)

§4d left one question — *which coordinate* — and answering it honestly turned up three
distinct defects rather than one. All three are measured against `boot`/`knn`, which
resample **real** rows: a defect the controls also exhibit is the harness's, and a defect
only the DFM arms exhibit is the generator's. On every number below the controls behave,
which is what makes the diagnosis load-bearing.

### A. The generator does not know that macro data is *printed*

Every series here is published on a grid, and a continuous diffusion emits none of it.
Measured on the cached pools (`/tmp/dfm_verify/lattice.py`) and then on the levels
themselves (`lattice_v2.py`), the grid is unmistakable and the DFM misses it completely:

| panel | column | grid (level) | real | `local` | `global` | `boot` | `knn` |
|---|---|---|---|---|---|---|---|
| labor_monthly | payems | 1.0 thousand → **1000 jobs** | 100.0% | 0.0% | 0.0% | 100.0% | 100.0% |
| labor_monthly | unrate | **0.1 pp** | 100.0% | 0.0% | 0.0% | 100.0% | 100.0% |

A tree splitting on the fractional part separates those two classes with one threshold.
That is exactly an AUC of 1.000, it is why `labor_monthly` alone reaches it, and no amount
of model quality could ever have moved it.

**It is not only a validity bug.** Kalshi settles on the *printed* value and a ladder strike
sits **on** a grid point (KXU3 at 4.2/4.3, KXPAYROLLS on 25k boundaries). An un-quantised
world therefore puts probability mass on outcomes the settlement rule cannot produce, and
hands `param_argmin` a bucket structure the real process does not have. Statistical and
utility invalidity here are the same defect seen from two sides.

Discovering the grid rather than declaring it was worth the extra work, and the output
proves it. `measure_lattice` finds a grid for **11 of the 12** generated columns across the
four panels:

| panel | column | transform | grid | precision | step / increment sd |
|---|---|---|---|---|---|
| labor_monthly | payems | diff | 1 | f64 | 0.0009 |
| labor_monthly | claims | dlog | 10 | f64 | 0.0003 |
| labor_monthly | unrate | diff | 0.1 | f64 | **0.1798** |
| inflation_monthly | cpi | pct100 | 0.001 | f64 | 0.0011 |
| inflation_monthly | cpi_core | pct100 | 0.001 | f64 | 0.0023 |
| inflation_monthly | pce_core | pct100 | 0.001 | f64 | 0.0062 |
| inflation_monthly | gas_retail | dlog | **NONE** | — | — |
| claims_weekly | claims | dlog | 1000 | f64 | 0.0452 |
| energy_weekly | gas_retail | dlog | 0.001 | f64 | 0.0129 |
| energy_weekly | wti | dlog | 0.01 | f32 | 0.0023 |
| energy_weekly | natgas | dlog | 0.001 | f32 | 0.0048 |
| energy_weekly | rbob | dlog | 0.0001 | f32 | 0.0006 |

Four things in that table are the reason it is measured and not hardcoded:

* **`gas_retail` appears twice with two different answers.** Same FRED series, 0.001 in
  `energy_weekly` (`agg="last"`, the published price survives) and **nothing** in
  `inflation_monthly` (`agg="mean"`, the monthly average of four weekly prints lands off
  every grid). A hardcoded table would have forced GASREGW's three decimals onto a column
  that provably does not have them.
* **wti/natgas/rbob are 0.01 / 0.001 / 0.0001 — the CME tick sizes** — and not one value is
  an exact multiple of them, because `fut_closes` returns float32-precision doubles
  (71.41 comes back as 71.41000366). Discovery therefore runs two passes: exact first, and
  a float32-aware pass only if the first fails, fenced by `g ≥ 8·eps32·max|x|` so a grid
  finer than the number's own storage resolution can never be "found". The fix must also
  **cast the quantised synth through float32**: emitting an exact 71.41 against a real class
  that stores 71.41000366 would not close the hole, it would invert it.
* **Monthly `claims` comes back on a grid of 10**, which is an artefact of averaging four
  or five thousand-grid weekly prints, not a Bureau decision. It is still the right target:
  the C2ST exploits an arithmetic regularity exactly as happily as an institutional one.
* **The cost column is the honest part.** Nine of the eleven grids move a path by under 5%
  of the column's own increment sd — free. `unrate` moves it by **18%**, and that one is a
  real change to the sample. It is also correct: a monthly UNRATE change genuinely only
  takes 0.0, ±0.1, ±0.2, and KXU3 settles on precisely that grid. A generator that emits
  −0.037 is not being more precise than the data, it is being wrong in a way the ladder
  can price.

**Where the fix goes, and why nowhere else.** Quantisation belongs on the reconstructed
**level**, because that is where the grid lives. For a `diff` column level and increment
grids coincide (payems' 1.0-thousand level grid × scale 1000 = the 1000-job increment
grid), but for `dlog`/`pct100` the log change of grid-spaced levels is *not* grid-spaced,
so rounding the increment would emit levels that are off-grid while claiming to be on it.
Rounding the level and re-differencing also bounds the error at half a step forever, where
difference-then-round lets it accumulate along the horizon.

`Generator.level_paths` is the single choke point — `synth/build.py:544` (worlds → utility)
and `generator.validate` (→ C2ST/CRPS) are its only two call sites — so one change fixes
the utility path and the validity path together. `Generator.sample_printed` re-differences
the quantised levels so `validate` scores **the object `worlds.py` writes** rather than a
smoother intermediate no consumer ever sees; that scoring gap is why the omission survived
two rounds of validation.

**The measured grid also grades the one that was already there.** `build.Settle.level_step`
rounds the SETTLE column only, and derives its step from the *contract's* `round_rule` —
ladder granularity — rather than from publication precision. Set side by side:

| series | column | `level_step` (ladder) | measured grid | |
|---|---|---|---|---|
| KXPAYROLLS | payems | 1.0 | 1.0 | agree |
| KXU3 | unrate | 0.1 | 0.1 | agree |
| KXWTIW | wti | 0.01 | 0.01 | agree |
| KXNATGASW | natgas | 0.001 | 0.001 | agree |
| KXJOBLESSCLAIMS | claims | **250** | **1000** | ladder is 4× too fine |
| KXCPI / CORE / YOY / PCECORE | cpi, cpi_core, pce_core | **None** | **0.001** | grid declared absent |

Four of six agree exactly, which is independent corroboration that the discovery finds real
structure and not arithmetic noise. The two disagreements are both the hand-declared rule
being wrong: ICSA prints on the thousand and never on 231,250, and the CPI indices are
published to three decimals — `level_step`'s docstring argues correctly that rounding the
index to the ladder's 0.1 would inject a third of a bucket into the settlement, and then
concludes "no such grid exists", which does not follow. Rounding to the real 0.001 moves the
implied MoM by ~0.0003pp, under a percent of a bucket.

No behaviour depends on resolving this today: every ladder step above divides the measured
grid, so `level_step` is now an idempotent no-op running after `quantise_levels`. It stays
as the fallback for a series whose panel column has no measured grid.

**Two residual gaps, downstream of the choke point** and deliberately not fixed in this
pass, because neither touches the settlement value: `_sub_monthly` disaggregates a monthly
mean into weekly prints and `_daily_bridge` expands a weekly close into business days, both
by adding noise, so the *auxiliary* rows a world writes are off-grid even when the settle
column is on it. A model reading those rows sees prints the real series never made. Tracked
separately.

#### What the fix actually bought, on all four panels — and the rule that predicts it

`fixA_pools.py` regenerates every arm twice, raw and printed, and `fixA_score.py` scores both.
The move is the printed AUC minus the raw AUC on the `local` arm; negative is the fix working.

| panel | column grids | real on-grid (`boot`) | RAW | PRT | move | move (global) |
|---|---|---|---|---|---|---|
| labor_monthly | payems 1, claims 10, unrate 0.1 | 100% / 0.5% / 100% | 1.000 | 0.775 | **−0.224** | −0.251 |
| claims_weekly | claims 1000 | 3.4% | 0.570 | 0.491 | −0.078 | −0.076 |
| inflation_monthly | 0.001 ×3, gas NONE | 0.4 / 0.2 / 0.0% | 0.737 | 0.727 | −0.009 | +0.016 |
| energy_weekly | 0.001 / 0.01 / 0.001 / 0.0001 | 0.5 / 0.8 / 0.4 / 2.3% | 0.790 | 0.795 | +0.004 | +0.006 |

**The hypothesis I went in with was wrong and the data says so plainly.** I predicted the move
would scale with grid COARSENESS relative to the increment sd, so `claims_weekly` — a grid of
1000, by far the coarsest step/sd in the table at 0.045 — was supposed to move like
`labor_monthly`. It moved −0.078, a third as much. Recording the refutation rather than
softening it, because the replacement is a sharper rule and I would not have found it by
protecting the first one.

**The rule that does hold, 12 of 12 columns with no exceptions, is the TRANSFORM.** Read the
`real on-grid` column against §4e-A's transform column: every column at ~100% is `diff`, and
every column at 0–3.4% is `dlog` or `pct100`. That is arithmetic, not a coincidence:

* a `diff` column reconstructs as `L = anchor + cumsum(inc)`, so a real anchor sitting on the
  grid plus increments that are grid multiples stays on the grid **no matter which rows the
  increments came from** — which is why `boot` and `knn`, resampled real history, come back at
  100% and the DFM at 0%. That is a free classifier split, and it is the whole of the
  `labor_monthly` AUC of 1.000.
* a `dlog`/`pct100` column reconstructs as `L = anchor × exp(cumsum(inc))`. A product of a
  grid-spaced anchor and an exponential lands off-grid even when every increment is real, and
  the 0.4–3.4% figures in the table are `boot` — REAL resampled history — failing its own
  lattice test. There is nothing there for a classifier to exploit, so there is nothing for
  quantisation to remove.

So the fix's ceiling is set by the panel's transforms, not by its grids, and `labor_monthly` is
the only panel in the current universe where a large move was ever available. `energy_weekly`'s
+0.004 and `inflation_monthly`'s +0.016 are the correct size for "no signal here" and are not
evidence the quantisation is harmful. This also tells us where the remaining lattice work is:
on a `dlog` column the tell would have to be *anchor-conditional* — on-grid given the anchor
that generated the path — which the current `measure_lattice` does not test for. #203 stays
open for that.

#### §4d's remedy does not work, and the per-fold C2ST is corrupted too

The raw `boot` AUCs in the Fix A run came back at 0.235–0.287, below the measured null band of
roughly [0.43, 0.58], and were left flagged as unexplained. **They were already explained.**
§4d "Death 2" diagnosed this exact inversion — fold concatenation puts a row held out in fold 1
into folds 2–5's training set, where the bootstrap draws it verbatim, so the same path carries
label 1 once and label 0 *k* times. `boot_auc_anomaly.py` re-derived that from scratch (H1
GroupKFold dead: stratifying moves labor 0.235 → 0.309; H3 aggregation dead: every fold is
individually below 0.5; 265 of 265 unique pool rows are exact rows of the real side) at real
CPU cost, because I did not check the section of this document that had already recorded it.
Noted so the next reader does not pay for it a third time.

What follows is the part that is **not** in §4d, and it matters more than the part that is.

§4d closes with a remedy: *"a per-fold-honest C2ST that never pools is the only version worth
running, and that is #181."* Production `_separability` implements exactly that — one call per
fold, pool drawn from that fold's own `tr`, real side that fold's `te`. `prod_dupes.py` confirms
the inverting form is gone: **`cross = 0` on every fold and every arm**, so no pool row is ever
an exact row of the real side. And the C2ST is corrupted anyway:

| panel | fold | arm | pool rows | unique | dup | `auc` | `auc` after dedup |
|---|---|---|---|---|---|---|---|
| labor_monthly | 0 | boot | 111 | 84 | 24.3% | 0.763 | **0.600** |
| labor_monthly | 0 | knn | 111 | 57 | 48.6% | 0.961 | **0.794** |
| labor_monthly | 1 | boot | 110 | 87 | 20.9% | 0.808 | 0.727 |
| labor_monthly | 1 | knn | 110 | 67 | 39.1% | 0.914 | **0.669** |
| labor_monthly | 2 | boot | 110 | 89 | 19.1% | 0.937 | 0.850 |
| labor_monthly | 2 | knn | 110 | 73 | 33.6% | 0.878 | 0.738 |
| claims_weekly | 0 | boot | 230 | 179 | 22.2% | 0.620 | **0.440** |
| claims_weekly | 0 | knn | 230 | 147 | 36.1% | 0.708 | **0.429** |
| claims_weekly | 1 | boot | 230 | 187 | 18.7% | 0.615 | **0.486** |
| claims_weekly | 1 | knn | 230 | 139 | 39.6% | 0.746 | **0.389** |
| claims_weekly | 2 | boot | 230 | 186 | 19.1% | 0.638 | **0.468** |
| claims_weekly | 2 | knn | 230 | 162 | 29.6% | 0.718 | 0.508 |
| inflation_monthly | 0 | boot | 111 | 84 | 24.3% | 0.770 | 0.634 |
| inflation_monthly | 0 | knn | 111 | 57 | 48.6% | 0.922 | **0.556** |
| inflation_monthly | 1 | boot | 110 | 87 | 20.9% | 0.785 | 0.675 |
| inflation_monthly | 1 | knn | 110 | 67 | 39.1% | 0.823 | **0.484** |
| inflation_monthly | 2 | boot | 110 | 89 | 19.1% | 0.764 | 0.731 |
| inflation_monthly | 2 | knn | 110 | 73 | 33.6% | 0.867 | 0.650 |
| energy_weekly | 0 | boot | 230 | 179 | 22.2% | 0.760 | 0.685 |
| energy_weekly | 0 | knn | 230 | 147 | 36.1% | 0.852 | **0.627** |
| energy_weekly | 1 | boot | 230 | 187 | 18.7% | 0.746 | 0.608 |
| energy_weekly | 1 | knn | 230 | 139 | 39.6% | 0.868 | **0.683** |
| energy_weekly | 2 | boot | 230 | 186 | 19.1% | 0.866 | 0.800 |
| energy_weekly | 2 | knn | 230 | 162 | 29.6% | 0.885 | 0.828 |

Twenty-four arm-folds, four panels, and **`cross = 0` in every one of them** — the inverting
form really is gone, exactly as §4d predicted. `dup` is 18.7–24.3% on `boot` and 29.6–48.6% on
`knn`, with no panel exempt, and dedup lowers the AUC in **24 of 24** cases — by 0.033–0.180 on
`boot` and 0.057–0.366 on `knn`, never once raising it. A one-sided result across every cell is
what makes this a bias rather than noise.

`claims_weekly` is the panel that shows what this costs, because it is the one where the honest
floor lands **below** 0.5: deduped, five of its six arms score 0.389–0.486, i.e. a real
resampled block is *harder* to tell from held-out history than a coin flip, which is what a
short weekly panel with 230-row pools should look like. Undeduped it reads 0.615–0.746 and
looks like a floor that is nearly separable. Every DFM excess on that panel has been measured
against the wrong one of those two numbers.

The duplicates are now **within one class** rather than straddling both, because
`block_bootstrap` copies whole rows (`Z[rng.integers(0, len(Z), size=n)]`) and two held-out
anchors can draw the same training row, while `knn_bootstrap` draws its rows from a
40-neighbour candidate set and collides constantly. A memorized duplicate on the label-0 side
raises the AUC exactly as a straddling one lowers it, so the floors come out **too high by
0.033–0.180 points on `boot` and 0.057–0.366 on `knn`**.

`boot_twin.py` isolates that claim on real rows with no model at all, and it began by refuting a
prediction of mine: I expected the disjoint-halves control — real half A against a bootstrap
resample of real half B, distributionally identical, **zero** twins — to land at ~0.5. It came
back well above, on every panel, and the four values sit in a band 0.03 wide:

| panel | twin100 (same rows resampled) | **twin000 (disjoint halves)** | observed cached pool | twinned p(real) vs untwinned |
|---|---|---|---|---|
| labor_monthly | 0.334 | **0.712** | 0.316 | 0.282 / 0.844 |
| inflation_monthly | 0.329 | **0.739** | 0.301 | 0.266 / 0.794 |
| claims_weekly | 0.285 | **0.758** | 0.295 | 0.249 / 0.727 |
| energy_weekly | 0.277 | **0.738** | 0.311 | 0.270 / 0.823 |

Resampling alone, with no cross-class contamination whatever, is enough — and it is enough by
about the same amount everywhere, which is what makes it a property of the procedure rather than
of any one panel. The twin100 column reproduces the inverting form for comparison, and inside
the observed cached pool the ~80% of real rows that have a twin score p(real) ≈ 0.27 against
≈ 0.80 for those that do not.

So the general statement is stronger than §4d's: **any bootstrap pool with duplicate rows
breaks a cross-validated C2ST**, and the sign only tells you which side of the label boundary
the duplicates sit on. Never pooling across folds removes the inversion and does not make the
test honest.

**The direction of the error is the uncomfortable part.** `floor_boot` is the reference every
arm is read against, so a floor inflated by ~0.1 makes every `excess_over_boot` in this study
**too negative** — it has been flattering the DFM. That is the #185 error with the sign
reversed: #185 read the AUC against a floor that was too *low* and condemned the generator,
and this reads it against a floor that is too *high*. Both come from not measuring the
baseline.

**The fix, as landed.** `_unique_rows` in `research/synth/generator.py` returns the indices of
the first occurrence of each distinct row, keyed on bytes after rounding to 1e-9 so a row that
survived a float round-trip still counts as one row while a genuine near neighbour at 1e-4 does
not. `_separability` calls it **once**, at the top, before any of the three legs sees a pool, so
the C2ST, `mem` and the new dependence leg all score the same rows. Dropping a verbatim copy
cannot change whether two *distributions* differ, which is the only question a C2ST asks, so
this removes a bias without spending any of the evidence.

`dup_frac` is kept per arm and printed, rather than absorbed silently, because a high value is a
fact about the **generator**: a `knn` arm at 0.49 is offering 57 distinct worlds where the run
header says 111. Tests:
`test_unique_rows_drops_verbatim_copies_and_keeps_genuine_neighbours` pins the 1e-9/1e-4
boundary, and `test_a_duplicate_heavy_pool_no_longer_scores_above_a_clean_one` reproduces the
whole effect small — two pools drawn from the *same* real training rows, one repeating 40 rows
three times, scoring 0.929 against 0.565 before the fix and indistinguishable after it. That
test asserts against the clean arm rather than against a number, for the same reason every other
metric here is read against `boot`.

What still has to happen: `data/synth/` and `data/synth_wf/` were written by the pre-fix code,
so **no `excess_over_boot` in §4e may be quoted again until those artifacts are regenerated.**
Production λ is unaffected — the corrupted quantity is the validation report, not the sample —
but the study's headline numbers are, and in the direction that flatters the DFM.

Scope of what still stands: `local`/`global` rows are generated and never exactly equal each
other or a real row (§4d measured 0 duplicates on all four panels), so the raw-vs-printed moves
in the table above are unaffected — they are within-arm comparisons on duplicate-free pools.

### B. Under-dispersion — the failure `validate`'s own docstring calls disqualifying

Variance ratio synth/real, per panel and column (`/tmp/dfm_verify/underdisp.py`):

| panel | column | `local` | `global` | `boot` | `knn` |
|---|---|---|---|---|---|
| claims_weekly | claims | 0.784 | 0.854 | 0.998 | 0.954 |
| inflation_monthly | cpi | 0.468 | 0.557 | 1.100 | 0.918 |
| inflation_monthly | cpi_core | 0.438 | 0.528 | 1.129 | 0.459 |
| inflation_monthly | pce_core | 0.622 | 0.603 | 1.042 | 0.648 |
| inflation_monthly | gas_retail | 0.509 | 0.628 | 1.066 | 0.963 |
| labor_monthly | payems | **0.412** | **0.407** | 0.998 | 0.694 |
| labor_monthly | claims | 0.721 | 0.762 | 1.041 | 0.971 |
| labor_monthly | unrate | 0.681 | 0.750 | 0.959 | 0.823 |
| energy_weekly | gas_retail | 0.531 | 0.619 | 1.047 | 0.737 |
| energy_weekly | wti | 0.564 | 0.748 | 0.990 | 0.678 |
| energy_weekly | natgas | 0.752 | 0.975 | 1.028 | 0.847 |
| energy_weekly | rbob | 0.567 | 0.714 | 1.039 | 0.703 |

`boot` sits at 0.96–1.13 everywhere; the DFM arms sit at 0.41–0.98 everywhere. The stored
#180 battery says the same thing in calibration units (`cov_table.py`): `global payems`
**cover50 = 0.189, cover80 = 0.333** against nominal 0.50/0.80, and the `sd` moment verdict
falls outside the bootstrap CI for essentially every DFM arm/column while `boot` is inside
everywhere.

This is not a new discovery so much as one that was measured and not treated as blocking —
§4b reports the S2 coverage numbers and mentions "the analog draw's under-dispersion
(cover50 0.382 → 0.456 against a nominal 0.50)" without the full per-panel table. It should
have been blocking: `generator.validate`'s docstring already names it as *"the failure that
would make a synthetic sample actively harmful: a too-narrow generator makes every candidate
parameter set look more skilful than it is"* — which is a precise description of a
`param_argmin` that trusts this sample.

Under-dispersion is **not** the labor 1.000, and it was tempting to stop there: the best
single dispersion scalar reaches AUC 0.72, and the multivariate test reads 1.000. A. is the
remainder. Both are real; neither explains the other.

#### The first suspect was wrong, and the real root is in the sampler

The suspect named here was `Generator._ridge_guidance`, which returns `-w·(x − m_t)/v_t` —
a literal pull toward the ridge-predicted conditional mean, and the textbook variance
killer. It is **refuted before it can be tested**: `GenConfig.guidance` defaults to
`"none"`, and neither `validate` nor `build` overrides it, so `_ridge_guidance` is never
called on the path that produced the table above. Written down because the shape of the
error is worth keeping — the guidance term *looks* exactly like the defect, and reading a
plausible mechanism off the source without checking that it runs is how a week goes missing.

**The root is the reverse-SDE initialization, and it is a genuine bug, not an estimation
limit.** `dfm/football/generate.reverse_sample` starts the reverse diffusion at
`x ~ N(0, I)`. That is correct only if the forward process has actually reached its prior by
`T`. It has not. The fork runs β = 1 over `T = 1.0` (`dfm/config.DIFFUSION_CONFIG`), so
`a_T = exp(−T/2) = 0.607` and the true marginal at the top of the diffusion is

>  `S_T = a_T²·Σ + h_T·I = 0.368·Σ + 0.632·I`

with **37% of the signal variance still present**. A standard VP schedule ramps β from 0.1
to 20 so that `∫β dt ≈ 10` and `a_T ≈ 0.007`; here the integral is 1. Because `Z` is
standardized, `diag(Σ) = 1` and therefore `diag(S_T) = 1` — which is why the identity start
*looks* right and survived review. It is wrong in every direction that is not an eigenvector
of eigenvalue 1: a direction of variance `L` should start at `0.368·L + 0.632` and starts at
`1.0` instead. Large factors are born too tight, the tail is born too wide, and the reverse
SDE is contracting, so the large directions never recover.

This was established with the score set to the **exact analytic score** of the true Gaussian
marginal (`/tmp/dfm_verify/fixB_euler.py`) — the network is perfect by construction there,
so nothing in the result can be blamed on estimation. Terminal variance ratio `V/L` for the
production sampler:

| eigenvalue `L` | 240 steps | 960 | 3840 | uniform 240, **exact start** |
|---|---|---|---|---|
| 4.0 | 0.630 | 0.630 | 0.630 | 0.996 |
| 2.0 | 0.850 | 0.850 | 0.850 | 0.994 |
| 1.0 | 0.991 | 0.990 | 0.990 | 0.991 |
| 0.1 | 0.953 | 0.940 | 0.937 | 0.926 |
| 0.01 | 0.558 | 0.515 | 0.505 | 0.554 |

Aggregated over a plausible macro spectrum (d = 120, 8 factors carrying 70%) that is a
panel-level ratio of **0.539** at 240 steps and **0.538** at 3840 — against **0.991** from
the same integrator started correctly.

Two knobs die here. **`noise_steps` is not the fix**: the column is flat to three decimals
across a 16× increase. **The time grid is not the fix either**: bunching steps where the
drift is stiff (`t₀ + (T−t₀)u²`) moves nothing. Both were on the original suspect list and
both are now excluded on measurement rather than on argument.

The signature reproduces on the real fitted nets (`/tmp/dfm_verify/fixB_diag.py`, fitted
with `cond_pcs=0` so the target is exactly 1.000 — a *conditional* sample is supposed to be
tighter than the pooled real marginal, so a conditional ratio below 1 could never have
settled this):

| panel | `d_flat` | 240 | 960 | 3840 | quad grid | Tweedie off |
|---|---|---|---|---|---|---|
| labor_monthly | 36 | **0.683** | 0.682 | 0.683 | 0.685 | 0.695 |
| claims_weekly | 13 | **0.898** | 0.913 | 0.914 | 0.901 | 0.909 |
| inflation_monthly | 48 | **0.605** | 0.604 | 0.607 | 0.607 | 0.619 |
| energy_weekly | 52 | **0.800** | 0.798 | 0.799 | 0.800 | 0.812 |

#### What correcting the start buys, and what it does not

Starting at `N(0, a_T²·Σ̂ + h_T·I)` with `Σ̂` from the **training rows only**
(`/tmp/dfm_verify/fixB_fix.py`, fit on the early 70%, graded on the late 30%):

| panel | production | corrected start | gap closed | top-4 eigen-direction ratio, prod → fixed |
|---|---|---|---|---|
| labor_monthly | 0.656 | **0.812** | 45% | 0.21 0.39 0.92 0.68 → 0.64 0.65 0.86 0.81 |
| claims_weekly | 0.863 | **0.923** | 44% | 0.78 0.79 0.81 0.85 → 0.83 0.94 0.91 0.87 |
| inflation_monthly | 0.686 | **0.779** | 30% | 0.26 0.29 0.50 0.49 → 0.46 0.53 0.68 0.71 |
| energy_weekly | 0.818 | **0.906** | 48% | 0.47 0.56 0.60 0.68 → 0.89 0.86 0.78 0.80 |

(`var/train`, not `var/test`: held-out z-variance is 0.851–1.409 across these panels, which
is a real regime difference between the early and late spans and not something a generator
should be graded on.)

Two internal checks that the number is not an artifact of the new code path. Shrinking `Σ̂`
toward the identity walks the result **monotonically** back to production on all four
panels, and at γ = 1 it reproduces it (labor 0.776 vs 0.761, claims 1.090 vs 1.090,
inflation 0.502 vs 0.504, energy 0.478 vs 0.478) — γ = 1 *is* `N(0, I)`, so this is the
identity the fix must satisfy. And the per-direction pattern is the predicted one rather
than a uniform rescaling: the correction lands almost entirely on the dominant factors,
which is where the analytic control says the loss was.

**It closes 30–48% of the deficit, not all of it.** With a perfect score net the corrected
start returns 0.991, so the residual 8–22% is the network. The architecture says where:
`CondFactorScoreNet` returns `subspace − dtx`, the Woodbury form `−Dx + DVMV'Dx` of the
Gaussian score, and the correction term lives **entirely in `span(V)`**, which has
`factor_dim = 8` columns. Every direction outside that span is scored as diagonal. On these
panels the top 8 eigen-directions carry only **49–77%** of the variance — and the measured
residual is exactly that shape: after the fix the top directions are still short (0.46–0.89)
while the **tail is over-dispersed at 1.81–3.30**. Too tight where the structure is, too
loose where it isn't. Capacity sweep (`factor_dim` × `epochs`, with a nearest-neighbour
memorization guard so that a config cannot win on moments by copying the training set) is
`/tmp/dfm_verify/fixB_m2.py`. Tracked as #181B.

#### The capacity sweep, all four panels — and it refutes the hypothesis it was built to test

`fixB_m2.py`, `factor_dim ∈ {8, 16, 24, 32}` × `epochs ∈ {6000, 18000}` × `start ∈ {N(0,I),
Σ̂}`, each cell fit on the early 70% and graded on the late 30%, with the `mem` guard alongside
so a config cannot win on variance by copying. `claims_weekly` admits only `factor_dim = 8`
(`d_flat = 13`); the other three run the full grid. Σ̂ rows only, at 6000 epochs, since the
epochs axis is discussed below:

| panel (`d_flat`, top-8 share) | metric | fd 8 | fd 16 | fd 24 | fd 32 |
|---|---|---|---|---|---|
| labor_monthly (36, 61%) | `var/train` | **0.814** | 0.729 | 0.703 | 0.982 |
| | `tail` | 1.79 | 1.12 | **0.84** | 0.93 |
| | `mem` | 1.07 | 0.94 | 0.84 | 0.88 |
| | `acf1` (real −0.179) | −0.034 | −0.090 | **−0.121** | −0.100 |
| claims_weekly (13, 77%) | `var/train` | **0.929** | — | — | — |
| | `tail` / `mem` | 1.01 / 1.00 | — | — | — |
| | `acf1` (real −0.212) | −0.228 | — | — | — |
| inflation_monthly (48, 59%) | `var/train` | **0.791** | 0.640 | 0.573 | 0.631 |
| | `tail` | 3.38 | 2.19 | 1.28 | **0.91** |
| | `mem` | 1.01 | 0.89 | 0.82 | 0.80 |
| | `acf1` (real +0.133) | 0.060 | 0.059 | 0.061 | **0.076** |
| energy_weekly (52, 49%) | `var/train` | **0.919** | 0.802 | 0.765 | 0.788 |
| | `tail` | 2.16 | 1.20 | 1.13 | **1.07** |
| | `mem` | 0.85 | 0.79 | 0.75 | 0.76 |
| | `acf1` (real −0.028) | 0.051 | **0.014** | 0.021 | 0.023 |

**The identity holds without exception.** Σ̂ beats `N(0,I)` on `var/train` in **all 26**
(panel, fd, epochs) cells in the sweep — no panel, no capacity, no training length where the
corrected start is not the wider one. That is the strongest form of the check §4e-B's γ-shrink
already passed, and it is why the start fix is not in question here.

**The capacity hypothesis is refuted.** The prediction written above was that a residual caused
by `span(V)` being only 8 columns wide would shrink as `factor_dim` grows. `var/train` **falls**
with capacity on all three panels that can run the grid — 0.814 → 0.703, 0.791 → 0.573,
0.919 → 0.765 — and the one apparent exception, labor at `fd = 32` (0.982), is not one: the same
cell at 18000 epochs collapses to 0.723, its KS p is 0.003 (the worst in the whole sweep), and
its top-4 eigen-ratios are 0.94/0.88/**1.19**/**1.13**, i.e. it bought its variance by
*over*-shooting two of the four dominant directions. It is an unstable cell, not an optimum.

**The mechanism is visible in the two halves of the spectrum, and it is the opposite of what
was wanted.** The `tail` prediction is *confirmed* — over-dispersion outside the span falls
monotonically toward 1 as the span widens (labor 1.79 → 0.84, inflation 3.38 → 0.91, energy
2.16 → 1.07), crossing 1 around `fd = 24`. But the top-4 directions get **worse** at the same
time (labor 0.64/0.67/0.85/0.84 → 0.72/0.59/0.70/0.66; inflation 0.50/0.53/0.71/0.73 →
0.39/0.52/0.60/0.58; energy 0.86/0.88/0.85/0.87 → 0.76/0.76/0.71/0.77). Added capacity does not
add width, it **moves** width off the dominant factors and onto the tail. Since the top
directions carry 49–77% of the variance, the panel-level ratio falls. The `span(V)` diagnosis
was right about *where* the residual lives and wrong about the sign of the remedy.

**More epochs is not a knob either, and it points the wrong way.** 6000 → 18000 lowers
`var/train` in 10 of the 13 Σ̂ cells, ties one (labor `fd = 16`, 0.729 both) and raises two, both
on `inflation_monthly` and both by ≤ 0.014 — so the direction is clear without being universal,
and the exceptions are recorded rather than rounded away. Training the score net three times
longer makes the sample *tighter*, not wider. Whatever it is converging to, it is not the
training covariance, and no amount of the training budget already on the table gets it there.

**`mem` closes the question.** It falls monotonically with capacity on every panel — labor 1.07
→ 0.84, inflation 1.01 → 0.80, energy 0.85 → 0.75 — so the large-`fd` cells are not merely
failing to help, they are drifting into copying. Read against #206's **measured** bands rather
than an implicit 1.0:

| panel | measured band | passing cells |
|---|---|---|
| claims_weekly | [0.933, 1.088] | `fd 8` at both epoch settings (1.00, 0.95) |
| inflation_monthly | [0.879, 1.156] | `fd 8` only (1.01, 1.00); `fd 16` at 0.89/0.85 is the boundary |
| labor_monthly | [0.956, 1.052] | **none** — `fd 8` is 1.07/1.06, *above* the band |
| energy_weekly | [0.953, 1.043] | **none** — `fd 8` is 0.85, and it only falls from there |

So the two panels that pass do so at the **smallest** capacity in the grid, and the two that
fail fail in opposite directions: labor is over-dispersed relative to a genuine held-out row and
energy is copying. That is #208, reproduced here from a completely independent code path.

**And `acf1` moves the other way, which makes this a trade and not an oversight.** Capacity
*helps* persistence on labor (−0.034 → −0.121 against real −0.179, closing 60% of the gap) and
on energy (0.051 → 0.014 against −0.028, closing 47%), while it is simultaneously what destroys
the variance ratio and the memorization guard. The single knob that would fix C is the knob that
breaks B. **No cell in
this 26-cell sweep satisfies B, C and the `mem` band at once**, and after this sweep that is an
architectural statement rather than a search that was not run long enough: it is the same
`span(V)` bottleneck seen from both ends. Which is exactly the case for #207 — rotate the basis
so the span is spent where the variance is, instead of buying more span.

Honest note on the two things the start fix does **not** buy. It corrects the second moment
by construction, so the only informative question is whether anything it does not construct
moves with it. Marginal *shape* (per-coordinate KS against held-out real) gets slightly
worse on three of four panels, and `acf1` moves toward the real value on two panels and away
on two. So this is a variance fix and nothing more.

Worse than nothing more, on one axis, and it is recorded here rather than in a footnote: the
corrected start makes **persistence measurably worse at every `factor_dim`** — on
`labor_monthly`, `acf1` goes −0.069 → −0.034 at `fd = 8` and −0.160 → −0.122 at `fd = 24`,
each time *away* from the real −0.179. B and C therefore trade against each other under the
current architecture, which is itself evidence for the shared `span(V)` root diagnosed in
§4e-C: width added outside the span is width with no persistence in it.

**End-to-end A/B, all four panels (`/tmp/dfm_verify/fixB_ab.py`).** Everything above is
measured in the sampler's own z-space, which is where the defect lives but not where the
product is. So the fix was run through `Generator.validate` itself — two passes per panel,
identical in every argument but `start`, with the `boot`/`knn` control arms asserted
**bit-identical** between the passes (they are, on all four panels, which is what makes the
DFM columns' movement attributable). Direction was committed before the run: coverage must
rise, KS must fall.

| panel | c50 identity → marginal | c80 identity → marginal | boot c50 / c80 | ΔKS | share of gap closed |
|---|---|---|---|---|---|
| labor_monthly | 0.332 → 0.335 | 0.532 → 0.542 | 0.415 / 0.634 | −0.000 | 3.6% |
| inflation_monthly | 0.220 → **0.241** | 0.406 → **0.427** | 0.495 / 0.733 | −0.010 | 7.6% |
| energy_weekly | 0.459 → **0.478** | 0.710 → **0.730** | 0.509 / 0.772 | −0.005 | 38% |
| claims_weekly | 0.701 → 0.701 | 0.865 → 0.862 | 0.520 / 0.767 | −0.004 | n/a |

KS falls on all four. Coverage rises on three and is flat on the fourth — and the flat one is
`claims_weekly`, which at c50 = 0.701 against a nominal 0.50 is **over**-covered, so a fix
that widens it would have been a fix doing damage. That it declines to widen the one panel
that does not need widening is a better argument for the correction being real than any of
the panels where it helped.

**Verdict: real, directionally correct on every panel, never harmful, and small.** It closes
3.6% of the DFM-to-`boot` coverage gap on labor and 7.6% on inflation. The 38% on
`energy_weekly` is not a counter-example to "small" — energy's gap was only 0.050 wide to
begin with, so the fix closes a large share of very little. Sharpness is unharmed: CRPS
against `boot` comes out 0.959 (t = −2.29, DFM sharper), 1.069, 1.011 and 0.949.

One reading trap, recorded because it cost time. `validate`'s printed `sd` barely moves under
this fix and that is not a contradiction: it is `paths.std(axis=1)`, the **within-path** std
along the horizon, while the fix restores **across-draw** variance. In z-space over the same
run, `var/train` goes 0.76 → 0.94. The two quantities are not the same number and only one of
them is what B is about.

### C. Persistence

The `acf1` moment verdict is outside the bootstrap CI for most DFM arm/column pairs; real
`payems` acf1 = **+0.083** against synth **−0.026**, i.e. the generator emits paths that
mean-revert slightly where the real series persists slightly.

The plan was to measure this again **after** B, on the theory that a guidance term crushing
the variance was a plausible common root and a C that disappeared with B would need no
separate treatment. That theory died twice over — the guidance term never ran, and the start
fix moves `acf1` toward the real value on two panels and away on two — and the conclusion
drawn from that, *"C is its own defect"*, **was wrong**. It confused B's *primary* root (the
reverse-SDE start) with B's *residual* root (`span(V)`). C shares the second one.

**C and the residual of B are one defect.** The diagonal-outside-`span(V)` score model emits
**white** noise in every direction the factor basis cannot reach, and lag-1 autocorrelation
along the horizon is exactly an off-diagonal structure — so `|acf1|` is dragged toward zero
in proportion to how much variance sits outside the span. Every measurement in hand agrees,
and none of them was collected to test this:

| panel | top-8 share | real `acf1` | prod `acf1` | recovered |
|---|---|---|---|---|
| claims_weekly | **77%** | −0.212 | −0.202 | **95%** |
| labor_monthly | 61% | −0.179 | −0.071 | 40% |
| inflation_monthly | 59% | +0.133 | +0.016 | 12% |
| energy_weekly | 49% | −0.028 | +0.020 | (both ≈ 0, uninformative) |

The cross-panel ordering is suggestive at n = 3; the within-panel sweep is not. On
`labor_monthly`, widening `factor_dim` walks `acf1` monotonically toward the real value **in
lockstep with the tail eigen-ratio falling toward 1** — two different symptoms of one cause
moving together, which is far harder to get by coincidence than either alone:

| `factor_dim` | `acf1` (real −0.179) | tail ratio | `mem` | KS p |
|---|---|---|---|---|
| 8 | −0.069 | 1.91 | 1.03 | 0.074 |
| 16 | −0.130 | 1.13 | 0.88 | 0.060 |
| 24 | **−0.160** | 0.82 | **0.74** | 0.050 |
| 32 | −0.130 | 0.69 | **0.71** | 0.036 |

And, as in B, **capacity is not the remedy**: `fd = 24` buys 89% of the persistence at
`mem = 0.74`, i.e. the sample sits closer to the training rows than a genuine held-out
observation does. That is copying, and a copying generator would score better on every moment
test here while being `boot` with extra steps.

#### Three candidate fixes, all three refuted — and the diagnosis above refuted with them

Each was pre-registered with its own kill criterion before it ran, and each died on its own
criterion rather than on judgement.

**1. z-space recolouring** (`fixC_recolour.py`, 4 panels). Carry the generated covariance onto
an α-blend with `Σ̂` from the fit's own rows. Legitimate for the production estimator because
`fit_local` fits *unconditionally* on a k-neighbourhood, so the neighbourhood covariance is
the right target. Registered criterion: the C2ST must **fall**, or the change is invisible to
a discriminator and therefore not a fix. It rises on three of four — labor 0.881 → 0.934,
inflation 0.694 → 0.746, claims 0.515 → 0.525 — and falls only on energy. REJECTED.

**2. `arch='plain'`** (`fixC_plain.py`, 4 panels), dfm's own full-dimensional ablation, which
removes the `span(V)` bottleneck outright and needs no change to `dfm/`. Registered criteria:
`acf1`, kurtosis and the tail ratio must improve **together**, and `mem` well below 1 is a
veto because copying wins every moment test. `mem` comes out 0.78 / 0.77 / 0.76 / 0.70 at
6000 epochs and 0.71 / 0.66 / 0.69 / 0.59 at 18000, the tail ratio *inverts* from
over-dispersed to collapsed (0.44 / 0.44 / 0.91 / 0.79), and the C2ST worsens on three panels.
REJECTED, unanimously, on the veto. Removing the bottleneck does not free the model; it frees
it to memorize 231–482 training rows.

**3. Panel splitting** (`fixC_split.py`, 3 multi-column panels). The idea the cross-panel table
above points at: raise coverage not by widening `factor_dim` — already rejected for buying
persistence at `mem = 0.74` — but by lowering `d_flat`, one generator per column or per
consuming-model block. `claims_weekly`, the one panel whose persistence matches real, is the
one panel that is a single generated column, so production already contained the treatment.
Registered verdict criterion: the **joint-space** C2ST must beat the joint panel, since better
marginals bought with broken cross-column dependence is a bad trade, not a fix.

| panel | `cov8` J → 2-blk → full | joint C2ST | `xcorr` err | `incorr` err |
|---|---|---|---|---|
| labor_monthly | 61% → 77% → 84% | 0.907 → 0.957 → 0.971 | 0.127 → 0.174 → 0.178 | 0.120 → 0.164 → 0.176 |
| inflation_monthly | 59% → 65% → 84% | 0.791 → 0.876 → 0.902 | 0.276 → 0.296 → 0.298 | 0.362 → 0.396 → 0.357 |
| energy_weekly | 49% → 58% → 73% | 0.866 → **0.839** → 0.950 | 0.083 → 0.093 → 0.098 | 0.089 → 0.091 → 0.102 |

The premise holds — coverage rises exactly as intended, 49–61% up to 73–84%. The verdict fails
anyway: the joint C2ST gets **monotonically worse** on two panels and on the third only the
2-block arm helps. And `incorr_err`, which was registered as the control that should be
*unaffected* by splitting, degrades too. So the split generators are not differently-scoped,
they are simply worse. REJECTED.

**The diagnosis this section was built on does not survive #3.** Coverage was raised by 23
points on `labor_monthly` and the sample got more separable, not less — the exact opposite of
what "quality tracks `span(V)` coverage" predicts. So the cross-panel ordering was a
confound, and once each panel is scored against **its own measured floor** it disappears
entirely:

| panel | top-8 share | DFM C2ST | `boot` floor | excess over floor |
|---|---|---|---|---|
| claims_weekly | 77% | 0.563 | 0.611 | **−0.048** |
| energy_weekly | 49% | 0.866 | 0.854 | +0.012 |
| labor_monthly | 61% | 0.907 | 0.863 | +0.044 |
| inflation_monthly | 59% | 0.791 | 0.725 | +0.066 |

Sorted by excess, the coverage column is unordered — 77 / 49 / 61 / 59. The apparent
relationship was raw C2ST read across panels whose floors differ by 0.24, which is the same
absolute-versus-relative error §4d catches one level down, made again one level up.

Two reachability facts, measured in `fixC_plain.py` before any generator was graded, that
reframe what C was ever asking for:

* **The held-out `acf1` is not a reachable target.** The training rows' own `acf1` differs
  from it on every panel and on `energy_weekly` differs in **sign** (train +0.044, held-out
  −0.028). Part of what §4e-C called a defect is a train/test regime difference that no
  generator fitted on train can close. Re-scored against the reachable target, the DFM's gaps
  are 0.072 / 0.048 / 0.070 / **0.006**, an order tighter and far more uniform than the
  0.163 / 0.012 / 0.100 / 0.067 measured against held-out.
* **`acf1` is mostly a second-moment quantity, and the claim that it is not was `labor_monthly`
  alone.** A draw from `N(μ̂, Σ̂)` recovers 93% and 94% of the training rows' `acf1` on
  claims and inflation, 73% on energy — and only 47% on labor. That single panel was
  generalized from once here and should not have been. It also gives recolouring's failure a
  simpler cause than "acf1 lives in higher moments": recolouring pulls toward the *training*
  covariance, and on `claims_weekly` the training span is **more** persistent than held-out
  (−0.272 vs −0.212), so matching it overshoots to −0.239 and the discriminator sees the
  overshoot.

**Where C stands.** Not "the generator has a persistence defect" but: measured against the
floor that real resampled history sets on each panel, the DFM is +0.012 to +0.066 away on
three panels and **0.048 better than real block-bootstrap on the fourth**. Three fixes aimed
at a defect diagnosed against the wrong baseline all failed, and two of the three failed by
making the sample *more* separable. The remaining honest question is not how to close a gap of
0.012–0.066 but whether a gap that size matters to the consumer — which is a λ-calibration and
utility question (§6), not a generative one. #181C is closed as **premise corrected**; the
utility question is tracked in #183.

Artifacts: `/tmp/dfm_verify/auc1_probe.py`, `underdisp.py`, `lattice.py`, `lattice_v2.py`,
`cov_table.py`, `fut_grid_probe.py`, `fixA_pools.py`, `fixA_score.py`, `fixB_euler.py`,
`fixB_diag.py`, `fixB_fix.py`, `fixB_m2.py`, `fixB_ab.py`, `fixC_recolour.py`,
`fixC_plain.py`, `fixC_split.py`.

## 4f. What is actually generatable, series by series (#183, 2026-08-28)

The standing instruction is that the DFM must serve **all fourteen** traded series, so the
first thing owed is an honest count of where it stands and what each gap actually costs.
`SETTLES` holds **10 of 14** today. The other four were each recorded as excluded, and on
re-examination two of those four exclusions were written down more confidently than the
evidence supported — in opposite directions.

| series | in `SETTLES` | blocker | kind of blocker |
|---|---|---|---|
| KXJOBLESSCLAIMS, KXWTIW, KXNATGASW | ✓ | — | — |
| KXPAYROLLS, KXU3 | ✓ | — | — |
| KXCPI, KXCPICORE, KXCPIYOY, KXCPICOREYOY, KXPCECORE | ✓ | — | — |
| KXAAAGASW | ✗ | 21 observations of `AAA_DAILY`, all after 2026-07-31 | **data** |
| KXGDP | ✗ | 43 quarters of GDPNow — and the nowcast IS the model's mean | **data** |
| KXFED, KXFEDDECISION | ✗ | the settlement variable is 86–97% an atom at zero | **model class** |

**KXAAAGASW — I had this scoped wrong, and `build.py` already had it right.** My note read
"`energy_weekly` already generates `gas_retail`, so this is a one-line `SETTLES` entry that
needs a GASREGW→AAA proxy offset". `build.py`'s module docstring states the actual and
stronger reason: `AAA_DAILY` holds 21 observations and `energy._aaa_drift_fit` predicts the
AAA-minus-GASREGW **gap**, so setting AAA equal to the generated GASREGW hands that regression
a target that is identically zero, while resampling the gap independently destroys the very
dependence the model exists to exploit. Either choice fabricates the answer. Not addable, for
a data reason, and the reason was already written down.

**KXFED — the recorded exclusion is true as written and proves less than it appears to.** It
says KXFED "settles on a policy decision, not a macro variable any panel generates". That is
two claims welded together: no panel generates it *today*, and it *cannot* be generated. Only
the first is established. KXFED settles on `DFEDTARU`, which is in the db with **6463 daily
observations back to 2008-12-16**, and `model/fed.py`'s other three inputs (CPILFESL, DGS2,
UNRATE) are already columns of `_MONTHLY_COLS` carried as context. So the missing piece is one
column, not a panel.

What actually blocks it is the **shape** of that column, and it is worth stating precisely
because §4e-A's lattice work makes this the best-case series on one axis and the worst on
another:

| resampled | n | unchanged | distinct non-zero moves | non-zero moves on the 25bp grid |
|---|---|---|---|---|
| weekly | 924 | **96.6%** | 6 | **100%** |
| monthly | 213 | **85.8%** | 6 | **100%** |

The grid column is the best result in this entire document — 100%, against 0.4–3.4% on the
`dlog` columns of §4e-A — because `DFEDTARU` differences are *exactly* a 25bp lattice and the
transform would be `diff`, which §4e-A's law says is the one case quantisation can fix.
The `unchanged` column is what kills it anyway: a score-based diffusion produces an absolutely
continuous law and cannot represent an 86–97% atom at zero. Quantisation maps a continuum onto
the grid, so it can manufacture *an* atom, but its size would then be an artifact of the
generated dispersion rather than of the FOMC's meeting calendar — and the calendar is the
actual generating mechanism, is deterministic, and is known. So KXFED is blocked on **model
class**, not on data, and the honest next step for it is a calendar-conditional two-part
model (meeting? × move size on a 6-point support), not a wider DFM. That is a different
project and it should not be smuggled in as a panel.

**KXGDP is the one genuinely open case, and its blocker is specific.** `model/gdp.py` reads
two things, and a world missing either cannot be priced at all: first prints of
`A191RL1Q225SBEA` (the settle value, the sigma history, the off-quarter AR(1)) and
`nowcast_vintages(GDPNow, KXGDP)` — which **is** the mean of the predictive distribution, not
a feature of it. The two halves are nothing alike in size:

| panel shape | d | H | rows | `d_flat` | max `factor_dim` at the ≥6-rows-per-factor rule |
|---|---|---|---|---|---|
| truth only, ex-COVID | 1 | 5 | 308 | 5 | 51 |
| truth + nowcast | 2 | 5 | 38 | 10 | 6 |
| truth + nowcast, ex-COVID | 2 | 5 | 34 | 10 | 5 |

The truth half would be the **healthiest panel in the project** — 308 rows for 5 output dims,
against `labor_monthly`'s 331 for 36. The joint half is one to two orders of magnitude
thinner than anything that has been validated here.

#### The measurement that decides whether KXGDP is buildable anyway

If the nowcast cannot be generated jointly, the only remaining construction is to generate the
truth and derive the nowcast as `truth + ε` with ε resampled from real history — the same
idiom `_daily_bridge` and `_sub_monthly` already use to expand a generated column into the
finer observations a model reads. The objection is the AAA one: an independently resampled
error destroys any dependence the model exists to exploit. **So the question is whether that
dependence exists**, and unlike AAA it can be measured — 41 quarters carry both a truth print
and a pre-release vintage, thin for fitting but adequate for testing one coefficient. (44
quarters have GDPNow vintages; 43 of those also have a truth print, which is the number the
panel-shape table counts; 41 have a vintage landing *before* the advance release, which is the
number the regression counts. The gap is PIT filtering, not a different sample.)

Regress `truth = a + b·nowcast` and test H₀: b = 1. (The first cut of this measured
`corr(err, nowcast) = +0.474` and annotated it "the anchor is biased". **That was the wrong
test and the annotation was wrong**: with `err = nowcast − truth`, an unbiased nowcast carrying
independent noise gives `cov(err, nowcast) = var(ε) > 0` mechanically. The errors-in-variables
form below is the right one, and it asks about *reliability*, not bias.)

| window | n | b | se | t(b−1) |
|---|---|---|---|---|
| all | 41 | 0.946 | 0.025 | −2.16 |
| ex-COVID | 37 | **0.776** | 0.070 | **−3.18** |
| ex-COVID, from 2017 | 34 | 0.776 | 0.071 | −3.16 |
| ex-COVID, from 2021 | 22 | 0.784 | 0.090 | −2.39 |

and it survives at real trading horizons rather than only on the best available vintage —
b = 0.744 / 0.785 / 0.765 / **0.701** at 7 / 14 / 30 / 60 days before the release, the last of
those at t = −3.73. Which looked like a live defect: `model/gdp.py` shrinks the **off**-quarter
anchor (`mu = m + φ^k(nowcast − m)`, and its own docstring records that this cut out-of-sample
RMSE from 2.533 to 1.57), while at k = 0 that expression is φ⁰ = 1 and a separate branch sets
`mu = nowcast` unshrunk.

**It is not a defect. The walk-forward killed it, and the thing that killed it is the one
choice the first pass made silently.** `gdp_shrink.py` dropped COVID from the *training*
history as well as from the scored set. `model/gdp.py`'s design note argues the opposite —
*"Winsorising is what replaces a hand-drawn 'drop 2020' — production sees the history it is
standing in"* — so the fit was re-run under all three regimes, same scored quarters, shrink
coefficient fitted only on already-settled quarters, sigma identical in both arms so only `mu`
is on trial:

| regime | min train | n | mean b | RMSE raw | RMSE shrunk | Δ | 90% paired boot | Wilcoxon p | CRPS raw → shrunk |
|---|---|---|---|---|---|---|---|---|---|
| drop | 12 | 25 | 0.818 | 1.060 | 0.987 | +0.073 | [−0.105, +0.239] | 0.353 | 0.578 → 0.530 |
| drop | 20 | 17 | 0.849 | 1.045 | 0.907 | +0.138 | [+0.015, +0.254] | 0.378 | 0.552 → 0.486 |
| **keep** | 12 | 25 | 0.927 | 1.060 | 1.058 | +0.002 | [−0.118, +0.113] | 0.937 | 0.578 → 0.576 |
| **keep** | 20 | 21 | 0.952 | 1.094 | 1.139 | **−0.045** | [−0.170, +0.061] | 0.473 | 0.595 → **0.638** |
| **winsor** | 12 | 25 | 0.936 | 1.060 | 1.031 | +0.029 | [−0.043, +0.092] | 0.731 | 0.578 → 0.560 |
| **winsor** | 20 | 21 | 0.957 | 1.094 | 1.089 | +0.006 | [−0.076, +0.075] | 0.562 | 0.595 → 0.600 |

The single 2020 quarter the nowcast got roughly right moves the fitted `b` from 0.818 to
0.927–0.957, and at b ≈ 0.95 the shrink does nothing. Under the two regimes this codebase's
own philosophy allows, every interval straddles zero, no Wilcoxon is close, and CRPS moves the
wrong way in two of four cells. Only `drop`/20 has an interval excluding zero, on n = 17, at
Wilcoxon p = 0.378, in the regime that was explicitly rejected — and against K = 19 looks. Not
a finding. **Recorded because a hand-drawn "drop 2020" would have produced a confident,
wrong, production change, which is the second independent time that decision has been
load-bearing in this file.**

The consolation is that the refutation is exactly what #183 needed. It says the GDPNow error,
over the 41 quarters that can be scored, is not measurably state-dependent — no reliable slope
(b = 0.95 ± 0.03), no heteroskedasticity (corr(|err|, nowcast) = −0.163, sd by nowcast tercile
1.03 / 0.62 / 0.96, unordered), no persistence (corr(err, err₋₁) = −0.256 on n = 36). So
resampling the error independently is not an assumption imposed on the world; it is the
conclusion the data supports and the alternative was tested and failed. That is the precise
respect in which KXGDP differs from KXAAAGASW, where the same question could not be asked at
all on 21 observations.

**Verdict.** KXGDP is buildable as a `gdp_quarterly` panel generating the truth alone, with
the nowcast derived as `truth + ε`. It would take `SETTLES` from 10 to **11 of 14**, and
11 is the ceiling for this architecture — KXFED/KXFEDDECISION need a different model class and
KXAAAGASW needs data that does not exist yet. What the build costs is not the panel but the
frequency: `build.py` currently has exactly two frequency classes (`_weekly(spec)` switches
the token convention, the observation date, the sub-monthly disaggregation and the clock), and
KXGDP is a third — quarterly data under a *date*-keyed token, since its markets are named for
the release date (2027-01-28) and not the reference quarter. Tracked as #212, with the
explicit note that this refactor sits under ten live series and must not be attempted as a
special case bolted onto `_weekly`.

Artifacts: `/tmp/dfm_verify/gdp_feas.py`, `gdp_shrink.py`, `gdp_shrink2.py`,
`fed_gdp_scope.py`.

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

### 5b-2. Can the market's own price be improved? No (#184, 2026-08-27)

§5b establishes that the market forecasts better than we do. That leaves one obvious route to
edge that does **not** require beating it: take the market price as the base rate and correct
its *calibration*. If prices are systematically over- or under-confident, a monotone map
fitted on settled events would beat the raw price without any forecasting skill at all. This
is worth testing precisely because it is the cheap answer, and it should be killed early if it
is wrong.

Measured on **81 settled events / 1642 legs** across all 14 series, production-model replay at
−1h PIT, walk-forward with a minimum of 8 prior training events so no map is ever fitted on
the event it scores (`/tmp/dfm_verify/market_recal.py`).

| Brier (pooled, 73 scorable events) | |
|---|---|
| raw market | **0.04956** |
| logit-recalibrated market | 0.05082 |
| isotonic-recalibrated market | 0.05318 |
| our model | **0.08902** |

| comparison | mean Δ Brier | 95% CI | P(better) | event win rate |
|---|---|---|---|---|
| logit vs market | +0.00126 | [−0.00260, +0.00583] | 0.298 | 55% |
| isotonic vs market | +0.00363 | [−0.00129, +0.00908] | 0.075 | 42% |
| model vs market | **+0.03946** | [+0.02512, +0.05445] | 0.000 | 29% |

Read three ways, all of them negative for the cheap answer:

1. **The model is 80% worse than the market** in Brier (0.089 vs 0.050), and the CI excludes
   zero by a wide margin. This is what "the model has never beaten the market" means as a
   number, and it is not close.
2. **Recalibration does not help either.** Both maps come out *worse* than the raw price, and
   neither CI excludes zero. The fitted slope is `b ≈ 1.24` pooled and `1.96` by series —
   in-sample the map always wants to sharpen prices toward the extremes, and out-of-sample
   that sharpening costs more than it gains. With 81 events the slope is fitting noise.
3. **The per-series table is a trap.** `logit` wins on 9 of 13 series, which reads like a
   result until the pooled number is checked: the three series where it loses
   (KXJOBLESSCLAIMS n=13, KXWTIW n=14, KXPAYROLLS n=3) lose by more than the nine win by.
   Counting series is not the same as counting money, and the win-rate/mean-diff split — 55%
   of events better, mean worse — is the signature of exactly that.

**What this forecloses, and what it does not.** It forecloses the entire family of "our number
against their number on the same contract": we lose that comparison, and we cannot repair
their number either. It does **not** foreclose the DFM, because a per-contract probability is
not what the DFM produces. Its output is a *joint* object — many series, many horizons, one
coherent draw — and the market prices each contract separately with no mechanism for making
them mutually consistent. So the remaining honest places for value are (a) sizing and
λ-calibration under a correct joint distribution rather than a per-leg one, (b) events with no
liquid quote at all, where there is no market number to lose to, and (c) cross-series
structure the independent per-contract prices cannot express. None of these is measured yet,
and none may be assumed; they are the scope of #183 and are stated here as *hypotheses*, not
as a fallback claim to soften a negative result.

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
