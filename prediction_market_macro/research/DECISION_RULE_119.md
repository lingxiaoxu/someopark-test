# Decision rule for #119 — written BEFORE the PnL comparison was run

Committed 2026-08-04, before `param_wf.run(objective="pnl")` had produced any output.
Same discipline as `DECISION_RULE_113.md`: if the result is disappointing, the response is
to accept it, not to renegotiate this file.

## Why #113 is being redone rather than appealed

#113 scored candidates on mean per-leg Brier and concluded "#115 is not adopted". That
conclusion answered the wrong question. A CPI ladder has 10-20 legs; the live hybrid touches
exactly **one** per event — the structure `decide()` opens, else the argmax favourite. A
candidate can sharpen the far tails, win on mean Brier, and never move a bet. The user's
correction ("我们不是做过混合几种模式下注吗 … 如果不是 重新做选参") is right on the
merits and this document is the redo.

## The proxy was tested and it FAILED — this is an input, not a result

Before committing to the expensive objective, `banded` Brier (legs whose model fair lands in
the tradeable [0.10, 0.90] window) was proposed as a powered stand-in for realised dollars:
581 events instead of 63, no market candle needed. It was licensed by measurement, not by
assumption. Spearman across parameter sets, metric vs realised hybrid PnL:

| series | sets | events | −brier vs pnl | −banded vs pnl |
|---|---|---|---|---|
| KXJOBLESSCLAIMS | 13 | 10 | 0.658 (p=0.014) | 0.532 (p=0.062) |
| KXWTIW | 10 | 8 | 0.256 (p=0.475) | 0.037 (p=0.920) |
| KXNATGASW | 4 | 11 | −0.333 (p=0.667) | 0.333 (p=0.667) |

Signs are inconsistent and only one cell is significant. **No licensed proxy.** Selection
runs on PnL itself, and the `banded` objective stays in the code as a measured negative
result rather than being deleted, so the next person does not re-propose it.

(The per-series "best PnL" figures from that same scan are an in-sample maximum over 4-13
sets on 8-11 events — pure selection bias. They are deliberately NOT carried forward as a
finding.)

## The objective

`pnl` — realised dollars of the live hybrid rule, replayed per event by
`research/pnl_score.py`, which reproduces the production decision stack (structure
enumeration → `decide()` gates → `_place_argmax` fallback → entry-day loop). Pinned
42/42 trade-for-trade against the stored 60-day walkforward on default params, on both
realised dollars and entry day.

Carried as **negative** dollars so every arm stays a minimiser and the paired DSR statistic
needs no per-objective sign branch.

## The sample, and its hard ceiling

PnL needs a stored market candle. Kalshi retains candles ~75 days, rolling — verified by
direct API probe (KXJOBLESSCLAIMS 25DEC31/26JAN29/26FEB26/26MAR26/26APR30 all 404;
26MAY28/26JUN25 return 7 candles each). 0 of 5152 settled legs closing before 2026 have
candles and 0/14 probed gaps inside the current window are recoverable. This is a data
reality, not an ingest bug.

Quotable events as of 2026-08-04, total 63:

    KXWTIW 11  KXNATGASW 11  KXAAAGASW 11  KXJOBLESSCLAIMS 10  KXPCECORE 3
    KXCPI/KXCPIYOY/KXCPICORE/KXCPICOREYOY/KXPAYROLLS/KXU3/KXFED/KXFEDDECISION 2 each
    KXGDP 1

**No single series reaches `dsr.MIN_OBS` = 12.** That is the central constraint and it is
recorded here, before the run, so that a null result cannot later be presented as a
discovery about the parameters when it is a fact about the sample size.

## Pooling — the one legitimate way to clear the floor

Naive pooling by MODULE is ruled out: `param_space` documents that liveness is per-SERIES
because two modules hide two branches (`cpi`: `gas_*`/`food_drift` are dead for the core
series; `energy`: `fut_*` for WTIW/NATGAS vs `aaa_*` for AAAGAS, never both). Pooling across
a branch boundary would difference against parameters that cannot move half the sample.

Pooling **within** a branch is legitimate: the same parameters, the same code path, the same
$1-scale payoff, and events remain independent observations of the same contrast. The
pairing removes event difficulty; cross-series heterogeneity inflates the sd, which makes
the test *more* conservative, never less.

Exactly one branch clears the floor today:

    fut_*  =  KXWTIW (11) + KXNATGASW (11)  =  22 events

The pooled grid is the branch's full live product (`fut_vol_window` × `fut_pool_bars` = 9
sets + the incumbent), not either series' individually size-capped grid, because the cap is
a function of the pooled history.

## What ships, and what is under test — these are different questions

The user's instruction is explicit: "选参数功能必须上线 就像混合下注必须上线一样".

**The mechanism ships regardless of the result below.** Daily DSR-gated re-selection is
wired into production `predict_all`: every day it rebuilds the trailing score matrix from
events that closed strictly earlier, runs `dsr.select`, and uses the winner. What is under
test is not *whether* the feature runs — it runs — but whether it is permitted to MOVE
parameters, which is governed by the gate it carries:

    n < MIN_OBS (12)      → incumbent, reason logged
    no candidate beats it → incumbent, reason logged
    p < 0.95              → incumbent, reason logged
    otherwise             → adopt

Shipping a selector that currently adopts nothing is not shipping a no-op. It is shipping a
mechanism with a bounded downside that begins adopting as the sample grows, and the sample
does grow: `com.someopark.macroweekly` runs `refresh --weekly` → `weekly_backtest_all` →
`backfill_candles` every week, comfortably inside the 75-day retention window
(`macroweekly.log` confirms it firing: KXWTIW +189, KXNATGASW +195, KXJOBLESSCLAIMS +63,
KXGDP +104, KXPCECORE +53). The four weekly series gain ~1 event each per week, so each
crosses `MIN_OBS` within weeks without any further work.

## Adoption rule — whether the gate may move parameters

Adopt **only if all three hold**, on the pooled `fut_*` sample (the only one with n ≥ 12):

1. `dsr` total PnL **>** `default` total PnL over the OOS window. Ties go to the incumbent.
2. `dsr` does not lose to `default` on either constituent series taken alone. A pooled win
   carried entirely by one series is one lucky draw wearing a total.
3. `dsr` adopts on at least 3 distinct simulated days with a stated `dsr_p`. A single
   adoption on the last day is an artefact of the sample crossing 12, not a signal.

If any fails: the gate stays in production and keeps returning the incumbent. That is the
designed behaviour, not a failure to ship.

## `argmin` is the control and is NOT shippable under any result

Stated in advance because `argmin` will very likely look better here: on 22 events with
~$1 payoffs, a 10-column argmin is a maximum of 10 noisy means and it will find something.
If `argmin` beats `dsr` out of sample, that is reported as evidence about the hurdle, and
the response is a power check on a longer window — **not** lowering `adopt_p` until `dsr`
passes. This is the same clause as #113 and it resolved *against* `argmin` there (its 6W-2L
on 45 events did not survive 262 events).

## Pre-registered expectation (written before the run)

I expect `dsr` to adopt nothing on 22 events: the paired dollar differences have enormous
variance relative to their mean when a single bet resolves ±$1, so the Sharpe on `d` will
not clear a 10-trial hurdle. I expect `argmin` to show a positive in-sample-looking edge
that means nothing. The honest likely outcome of this document is: **the selector ships,
the gate holds, production parameters do not move today, and the sample crosses the floor
in a few weeks.** That is a real answer to "参数是不是最优的" — with 63 tradeable events
in existence, nobody can tell yet, and the machinery to tell is now running.

---

# Result, 2026-08-04 (`/tmp/param_wf_pnl.json`)

Window 2026-06-01 .. 2026-08-01, `--objective pnl --pools energy_fut`. 45 OOS events across
11 series, plus the 18 in-window events of the pooled `fut_*` branch.

## The pooled branch — the only sample with the power to decide anything

| arm | total PnL over 18 OOS events | sets moved |
|---|---|---|
| `default` | −$0.03 | — |
| `argmin` | **+$3.91** | 18 of 18 |
| `dsr` | −$0.03 | **0 of 18** |

Pooling did what it was supposed to do mechanically: the trailing sample reaches n = 21 by
2026-07-31 and crosses `MIN_OBS` = 12 on 2026-07-02, so the gate was genuinely tested rather
than short-circuited. On the full 22-event pooled sample it reports:

    best candidate 2 (fut_pool_bars=1500, fut_vol_window=10), +$0.232/event,
    SR 0.425 vs the 10-trial deflation hurdle 0.344, p = 0.725 < 0.95 — incumbent kept

**Condition 1 fails** — `dsr` ties `default` rather than beating it, and ties go to the
incumbent. Conditions 2 and 3 are not reached. **Production parameters do not move today.**

## The `argmin` clause fires, and is reported rather than argued away

`argmin` beat `default` by +$3.94 over the 18 OOS events (+$0.219/event). Taken alone that
is one-sided t = 1.73, p = 0.051 — the kind of number a naive search would announce. It does
not survive being looked at:

- Only **7 of 18** events differ at all, and those 7 split **4W-3L**. Wilcoxon p = 0.148.
- The entire edge is one series. KXWTIW goes −$2.86 → +$0.98; KXNATGASW goes +$2.83 → +$2.93
  — i.e. nothing. This is exactly the "aggregate win carried entirely by one series" that
  pre-registered condition 2 exists to catch, and it is caught.

Per the standing clause, the response is not to lower `adopt_p`. The prescribed response —
a power check on a longer window — **is not available on this objective**, and that is
recorded here rather than quietly skipped: candles expire at ~75 days, so 2026-05-22 is the
oldest event that can ever be PnL-scored. There is no longer window to check on. The
substitute is forward, not backward, and it is already running (below).

## One observation that is worth writing down but is NOT a finding

On the full 22-event pooled sample, **8 of the 9 candidates beat the default** (+$0.49 to
+$3.98 against the default's −$1.27). The 9th, set 5, is `fut_vol_window=20,
fut_pool_bars=1500` — the incumbent's own configuration, which `param_space` deliberately
includes, so its paired difference is identically zero and it scores exactly the default.

This is suggestive and it is not evidence. The nine candidates are heavily correlated (they
are a 3×3 product over two parameters), so "8 of 9" is nothing like 8 independent successes,
and the winners point in incoherent directions — `fut_vol_window` 10 and 40 both beat 20,
with no story that covers both. Naming set 2 or set 7 as "the better parameters" would be
picking the maximum of ten noisy means, which is the precise thing `dsr` was written to
refuse. It is left un-adopted on purpose.

## KXAAAGASW: the sharpest confirmation that #113 asked the wrong question

The `aaa_*` grid has **73 sets**. All 73 produce **byte-identical** realised PnL — $8.38
over 11 events — while their Brier scores differ enough to rank them confidently. The
Spearman between Brier and PnL is undefined because the PnL vector is constant.

A Brier-scored selector would have searched 73 candidates, declared a winner, and moved a
production parameter that cannot change a single bet. That is not a hypothetical failure
mode of the old objective; it is the measured behaviour of the old objective on the widest
grid in the system.

## What shipped

`dsr` adopting nothing does not block the feature — per the pre-registration, the mechanism
ships and the gate governs. Landed:

| piece | what it does |
|---|---|
| `research/param_select.py` | daily per-series selection on the PnL objective, DSR-gated |
| `param_selection` table | one row per (series, day): params, adopted, n_obs, dsr_p, full report |
| `ops/refresh.py` | `param_select` step, immediately before `predict_all` |
| `ops/predict_all.py` | reads today's row, passes `params` into the model |

Verified on the live db: all 14 series scored, all 14 on registered defaults, KXWTIW and
KXNATGASW carrying the pooled `n_obs=22, p=0.725`. A cached re-run of the whole selector
takes **1.1 s**, so the daily pipeline pays nothing on a day when no new event settled; it
rescores one series on the day that series settles.

## The forward test is the selector itself

Because the effect above cannot be checked on a longer past window, it gets checked on
future events — and no separate apparatus is needed, because the shipped selector *is* that
test. Every week the pooled `fut_*` sample gains ~2 events. If the candidate family is real,
`dsr` will adopt on its own, on a pre-registered threshold, with the reason written to
`param_selection`. If it is noise, the sample will grow and the gate will keep holding.

No parameter is being adopted on today's evidence, and none has to be.
