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

## 6. Calibrating lambda (S5)

For each weekly series (KXWTIW, KXNATGASW, KXAAAGASW, KXJOBLESSCLAIMS — n_real 10–11):

1. Build synthetic worlds for that series and run the full grid on them.
2. Take the synthetic argmin winner.
3. Score that winner on the **real** events it never saw.
4. Compare against (a) the default set and (b) the real-events argmin winner.

Repeat over folds and seeds. The fraction of the synthetic-claimed improvement that
survives on real events is the exchange rate between a synthetic and a real observation.
`lambda` is set at the lower end of its bootstrap interval, not its point estimate.

## 7. Storage and cadence (S7)

* Generator weights + the panel that produced them: `prediction_market_macro/data/synth/`
  as `.pt` + `.npz` keyed by a config hash. Regenerable; not in git.
* The consumable output — per (series, config hash, parameter-set hash) synthetic PnL
  aggregates — in `macro.db`, small, and covered by `ops/backup_db.py`.
* Regeneration cadence: monthly is enough for the panel; the **conditioning** vector is
  re-read every run, so "close to the current environment" tracks daily without retraining.

## 8. Order of work and gates between stages

| stage | done when |
|---|---|
| S1 panel | **DONE** (`bd8540f`+). Leakage test passes: no row contains a value dated after the window's right edge, `integrate` inverts the increment transform exactly, and each column reads the vintage its consuming model reads. See the point-in-time caveat in §4 |
| S2 generator | purged blocked k-fold, three arms. **Paired CRPS vs `block_bootstrap` must be < 1 with t < −2** (the conditional arm has to be sharper than the unconditional resample of its own history), **and** rank calibration must not degrade materially against it. `knn_bootstrap` clears this on all five panels; the global conditional DFM does not and is a **documented negative**. The shipped arm is `fit_local`, which must additionally TIE its own `knn` on identical rows — see §4b |
| S3 worlds | **round-trip proof**: materialize the *real* history through the same writer and reproduce production `event_pnl` numbers exactly. Nothing downstream is trustworthy until this passes |
| S4 book | the transplanted books reproduce the real events' spread and devigged-width distribution |
| S5 lambda | a bootstrap interval on lambda, reported whatever it says |
| S6 wiring | `n_eff` feeds `sample_cap`; gate logs distinguish real from synthetic sample |
| S7 ops | regeneration runs unattended and its output is backed up |

Nothing writes to production state until S5 has a number.
