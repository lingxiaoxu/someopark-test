# Decision rule for #113 — written BEFORE the comparison was run

Committed 2026-08-04, before `param_wf.run` had produced any output. It is recorded here
rather than decided afterwards because the whole point of #112 was that a rule chosen after
seeing the numbers is not a rule. If the result below is disappointing, the response is to
accept it, not to renegotiate this file.

## The three arms

All three read the SAME frozen grid, built from events closing strictly before 2026-06-01,
so they differ only in how they pick from it.

| arm | rule |
|---|---|
| `default` | always the registered production parameters (index 0) |
| `argmin` | trailing mean Brier argmin, no deflation — what `param_grid.run_grid` does today |
| `dsr` | argmin, adopted only if it clears the deflated hurdle at p >= 0.95 |

## Primary metric

Mean per-leg Brier over every scored event closing in **2026-06-01 .. 2026-08-01**,
aggregated across series weighted by event count.

Brier and not PnL, for the primary. PnL over this window is carried by ~23 trades on the 61
events that have a market candle, which cannot separate a 0.002 Brier improvement from
noise; Brier is measurable on every event in the window. PnL is still checked, as a veto.

## Adoption rule for #115 (wire daily re-selection into production)

Adopt **only if all three hold**:

1. `dsr` aggregate Brier **<** `default` aggregate Brier. Ties go to the incumbent.
2. `dsr` does not lose to `default` on more than **half** the individual series. An
   aggregate win carried entirely by one series is a single lucky draw wearing a total.
3. The walk-forward **PnL** over the same window under `dsr` params is **>=** the PnL under
   `default` params. A Brier improvement that costs money is not an improvement; this is a
   veto, not a target.

If 1 fails: keep fixed defaults, and the answer to "could daily re-selection do better" is
a measured no.
If 1 and 2 pass but 3 fails: keep fixed defaults and record the split — it means the Brier
gain lands on events the strategy does not trade, which is worth knowing but not shipping.

## What `argmin` is for

It is not a candidate. It is the control that measures what the deflation bought. Expected
and specifically watched for: `argmin` beats `default` on the TRAINING scores and loses on
OOS. If `argmin` were to beat `dsr` out of sample, that is evidence the hurdle is set too
high, and the response is to report it — not to lower `adopt_p` and rerun until it passes.

## Result, 2026-08-04 (`/tmp/param_wf_0601_0801.json`)

45 scored OOS events over 12 series (KXNATGASW and KXGDP have no grid).

| arm | aggregate Brier | series record vs default |
|---|---|---|
| `default` | 0.102255 | — |
| `argmin` | 0.098363 | 6W 2L 4T |
| `dsr` | **0.102255** | adopted nothing, on any series, on any day |

**Condition 1 fails** — `dsr` did not beat `default`, it *equals* it. Conditions 2 and 3
are therefore not reached. **#115 is not adopted.** Fixed defaults stay in production.

The pre-registered `argmin` clause is triggered and is recorded here rather than argued
away: `argmin` beat `dsr` out of sample by 0.0039 Brier/event. Paired over the 45 events
that is t = 1.51, one-sided p = 0.066 (fails at 0.05); the Wilcoxon signed-rank on the 28
non-zero differences gives p = 0.033 (passes). Borderline, and the two tests disagree,
which is what an underpowered window looks like — 8 of the 12 series contribute only 2
events each.

Per this document, the response is NOT to lower `adopt_p` until `dsr` passes. The response
is a power check: the same three arms, the same rule, the same 0.95 threshold, run over
2024-01-01 .. 2026-08-01 instead. That window is ~10x the events and can actually separate
"the hurdle is too strict" from "45 events cannot tell". Recorded before that run.

## Power check 1 — 2024-01-01 .. 2026-08-01 (`/tmp/param_wf_long.json`)

262 OOS events, 7 series.

| arm | aggregate Brier | series record vs default |
|---|---|---|
| `default` | **0.072885** | — |
| `argmin` | 0.072992 | 2W 4L 1T |
| `dsr` | 0.072943 | adopted on KXU3 only, and lost (0.08159 vs 0.08108) |

`argmin`'s short-window advantage **did not survive**: mean edge -0.0001 Brier/event,
t = -0.40 (p = 0.65), Wilcoxon p = 0.49. The 6W-2L on 45 events was noise, and the
`argmin` clause is resolved — the hurdle was not too strict.

**Caveat that requires a third window.** Grids here are frozen on pre-2024 events, and
KXJOBLESSCLAIMS, KXAAAGASW and KXNATGASW have ZERO events before 2024 in the db, so they
were skipped. Those are exactly the two series where `argmin` won biggest on the short
window. This check therefore does not cover them and cannot be reported as if it did.

## Power check 2 — 2026-01-01 .. 2026-08-01 (pre-registered before running)

Chosen so KXJOBLESSCLAIMS (~19 pre-window events) and KXAAAGASW (~40) clear the
12-event grid floor and still leave ~30 OOS events each. Same arms, same `adopt_p` = 0.95,
same decision rule. This is a coverage fix, not a second attempt at a passing result: if
`argmin` beats `default` here on the series the long window missed, that is a real finding
and gets reported as one.

### Power check 2 result (`/tmp/param_wf_2026.json`)

141 OOS events, 11 series — KXJOBLESSCLAIMS (30) and KXAAAGASW (29) now covered.

| arm | aggregate Brier | series record vs default |
|---|---|---|
| `default` | 0.108263 | — |
| `argmin` | 0.106551 | 4W **5L** 2T |
| `dsr` | **0.108263** | adopted nothing |

t = 1.24 (p = 0.108), Wilcoxon p = 0.051. Not significant, and `argmin` loses on more
series than it wins — the aggregate edge comes entirely from the three high-count series.

## Verdict

| window | events | `argmin` edge | t-test p | Wilcoxon p | series record |
|---|---|---|---|---|---|
| 6/1 .. 8/1 | 45 | +0.0039 | 0.066 | 0.033 | 6W 2L 4T |
| 2024 .. 2026 | 262 | -0.0001 | 0.655 | 0.493 | 2W 4L 1T |
| 2026 .. 8/1 | 141 | +0.0017 | 0.108 | 0.051 | 4W 5L 2T |

Never significant on the paired t-test in any window. Combined series record 12W-11L-7T,
which is a coin flip. `dsr` adopted on exactly one series-window in the whole exercise
(KXU3 on the long window) and **lost** on it.

**Condition 1 fails on all three windows. #115 is not adopted; fixed defaults stay in
production.** The answer to "could rolling daily re-selection do better" is a measured no:
the parameters are not leaving anything on the table, and — the useful corollary — the
existing backtest result is not a tuning artefact, because tuning does not help.

## The one lead worth keeping

KXJOBLESSCLAIMS is the only series where a candidate beats the default consistently and by
a margin worth having. Over its full 49-event history, aggressive-recency level weights
`(0.0, 0.0, 0.3, 0.7)` with `seasonal_years=10` score 0.1497 against the default's 0.1600 —
**-0.0103 Brier/event**, and the walk-forward argmin picked that family on 30 of 30 days in
the 2026 window.

It still does not clear multiplicity, and the two independent corrections agree closely:

| test | statistic | verdict |
|---|---|---|
| undeflated paired t | t = 2.450, p = 0.0071 | would "pass" — this is the trap |
| Bonferroni over K=13 | p = 0.0929 | fails at 0.05 |
| DSR | SR 0.350 vs hurdle 0.246, p = 0.748 | fails at 0.95 |

The raw t-test is exactly the number that a naive search would have reported as a
discovery. The correct next step is NOT to adopt it on this evidence, and not to keep
searching: it is to **pre-register it as a single hypothesis (K = 1, no multiplicity
penalty) and test it on events that have not happened yet**. At K = 1 an effect this size
would clear on roughly 20-25 fresh releases, i.e. about six months of weekly claims.

## Pre-registered expectation

I expect `dsr` to adopt on very few series and end up close to `default`, because
`param_space` sized most grids at 3-27 sets against 20-70 training events and the
deflation hurdle at that width is high. A near-tie with `default` is the honest likely
outcome and would still answer the user's question: the parameters are not leaving much on
the table, and the 23-trade result is not a tuning artefact.
