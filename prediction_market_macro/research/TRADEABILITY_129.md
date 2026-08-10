# #129 — Can a profitable model be built on this book? Measured answer: no.

Written 2026-08-04, after the honest 75-day display (#123/#128) went up at **−9.17%** and
the user asked to keep looking for a profitable model. This records what was measured and
why the search stopped, so the next person does not re-run the same five dead ends.

Same discipline as `DECISION_RULE_113.md` / `DECISION_RULE_119.md`: the result is
disappointing and the response is to accept it, not to renegotiate this file. Nothing below
was tuned on the PnL window — every number is a measurement of the archive as it stands.

## Setup

Everything is replayed off `d75:model:end2026-07-31` (2026-05-17..07-31, db-state gates ON,
daily DSR selector ON, bucket-devig fixed): **52 hybrid trades, 51.9% win, $58.81 staked,
−$5.39, ROI −9.165%.**

> **Correction, 2026-08-05.** Two harness defects found while fixing #130 moved this basis
> to **52 trades, 46.2% win, $41.88 staked, −$10.65, ROI −25.43%**. Neither is a change of
> strategy nor a re-tune:
>
> * the harness booked a bucket's **gross leg notional** as `staked` where production books
>   the net debit (`count * fill_cost`), inflating the ROI *denominator* only — same trades,
>   same pnl, −9.17% → **−13.21%**;
> * #130 itself (a wide book no longer switches the sanity gate off) blocks 5 wide-book
>   entries, and because the harness allows one open per (series, period) the block lets a
>   *later* day fill the slot with a different, larger structure — −13.21% → **−25.43%**
>   over 9 changed trades.
>
> Nothing below is invalidated: §1/§2/§4 are per-structure measurements that never touch
> `staked`, and §3's per-spread ROIs shift but keep their sign and ordering. The verdict is
> unchanged, and slightly more strongly supported.

The replay reconstructs every structure `enumerate_structs` builds for each of the 62 traded
(series, period, day) events — **3857 scoreable structures** — using walkforward's own
event, quote and gate machinery rather than a paraphrase of it, so a divergence between the
diagnostic and the harness is impossible by construction.

Two statistical rules apply throughout. Structures inside one event settle together, so
every CI is a **bootstrap clustered on events**, not on structures (unclustered would be
~6× too tight). And `taker_fee` rounds UP to a whole cent per order, so every gross figure
is shown with its net at size 1 and size 10.

## 1. The winner's-curse hypothesis — real in size, but not the disease

The traded legs are badly calibrated: the model says 92% on legs that settle 55%. The
hypothesis was that this is a selection artifact — `decide()` takes the leg maximising
`fair − cost` out of a 10-40 leg book, which is a maximum over noisy estimates and therefore
picks the leg where the model's error is largest and positive.

| set | n | Brier(model) | mean bias |
|---|---|---|---|
| all structures | 3857 | 0.0629 | −0.001 |
| selected (traded) | 72 | 0.2257 | **+0.117** |

So the curse is there in size. But it does not survive as an *explanation*, for two reasons.

* Matched on the model's own fair bucket, selected legs are only **+0.049 [t=0.83, 34/65]**
  more over-confident than their peers — not significant.
* The all-structures Brier is flattered by 2771 deep-tail legs the strategy never considers.
  Restricted to the range argmax actually draws from (fair ≥ 0.5): model **0.1150** vs
  devigged market **0.0973**. The model loses in exactly the range it bets.

A multiplicity correction cannot rescue a forecast that loses to the market at every
claimed probability. **Route closed.**

## 2. Model-free: is the book beatable by anyone?

Strip the model out. Every structure is quoted at an ask and settles 0/1, so realised edge
= hit_rate − ask, measurable in price bands with no forecast at all.

| ask band | n | events | paid | settled | gross | 95% CI (clustered) | net@10 |
|---|---|---|---|---|---|---|---|
| 1–10% | 2114 | 51 | 2.5% | 0.5% | −2.01% | [−0.025, −0.013] | −2.28% |
| 10–25% | 517 | 47 | 17.0% | 9.7% | −7.38% | [−0.110, −0.025] | −8.84% |
| 25–50% | 374 | 43 | 35.4% | 26.2% | −9.20% | [−0.137, −0.044] | −11.25% |
| 50–75% | 191 | 40 | 61.9% | 47.1% | −14.82% | [−0.233, −0.048] | −16.63% |
| 75–90% | 145 | 43 | 82.2% | 57.2% | −25.00% | [−0.376, −0.112] | −26.08% |
| 90–100% | 516 | 51 | 96.6% | 93.8% | −2.79% | [−0.061, −0.000] | −3.07% |
| **ALL** | **3857** | **52** | 26.2% | 21.2% | **−5.03%** | **[−0.072, −0.033]** | **−5.74%** |

Every band negative, the pooled CI clear of zero. The YES side is −6.51%, the NO side
−0.29% [−0.038, +0.038] — the classic favourite-longshot asymmetry, and the NO side is
*flat*, not positive.

**The one non-negative pocket did not survive the split.** NO at 50–75% showed +8.66%
gross over 58 structures, but that is 1 positive cell out of 18 (side × band) tested and its
CI already spanned zero; split by entry day it is +4.33% in the first half and +13.29% in
the held-out half, and the wider NO 25–90% pocket **flips sign** (−1.52% → +9.43%). Slice,
not edge. **Route closed.**

## 3. Execution: the "wide quotes" lead, and why it died

Splitting the bands by quoted spread looked, for one hour, like the answer. On tight (≤2c)
quotes a buyer taking the ask is break-even at every band (−5.4%, +2.3%, −3.1%, +0.9%, all
CIs spanning zero); on wide quotes the same bands run −12.9%, −28.5%, **−40.4%**, −11.8%.
The whole measured "vig" lives in illiquid legs whose ask is a fishing offer — and even the
*mid* of those legs sits far above what they settle, so it is not a spread to be split.

That indicted a real piece of code: `decide()` measures `median_spread` against
`WIDE_SPREAD`, but a wide book only causes `market_fairs` to be withheld — the sanity gate
is switched **off** — rather than the trade being blocked. The rule trades most freely
exactly where the quote is least informative.

**But the strategy's own trades do not sort by spread**, so the defect is not what cost the
money:

| spread of the leg taken | n | win | staked | pnl | roi |
|---|---|---|---|---|---|
| tight (≤2c) | 16 | 56.2% | 12.75 | −4.13 | −32.4% |
| medium (2–5c) | 17 | 52.9% | 12.85 | −3.37 | −26.2% |
| wide (>5c) | 13 | 38.5% | 10.68 | −4.04 | −37.8% |

A spread filter would have removed a third of the trades and roughly a third of the losses.
**Route closed as a profit lead** — the `WIDE_SPREAD` asymmetry is still worth fixing on
its own merits (logged separately), just not as a way to make money.

## 4. The decisive test: does disagreement with the market carry information?

The strategy's whole premise is that a gap between model and market is a signal. Realised
edge over all 3857 structures, bucketed by that gap:

| model fair − market ask | n | events | paid | settled | realised edge |
|---|---|---|---|---|---|
| −100%..−10% | 697 | 44 | 48.7% | 32.1% | **−16.54%** |
| −10%..−2% | 770 | 50 | 27.2% | 23.5% | −3.70% |
| **−2%..+2% (agreement)** | 1948 | 52 | 18.0% | 16.4% | **−1.58%** |
| +2%..+10% | 290 | 46 | 25.0% | 21.7% | −3.24% |
| +10%..+100% | 152 | 35 | 25.1% | 18.4% | **−6.66%** |

Realised edge is **maximised where the model agrees with the market** and degrades
symmetrically in both directions. Disagreement is not signal; it is noise that predicts its
own cost. The live gate (`net_edge ≥ 4%`) selects the +2%..+100% region, which realises
−3.2% to −6.7%.

And the model's *pick*, matched against same-event legs at the same market price, is
**+0.034 [−0.086, +0.154], 21/42 events positive** — statistically indistinguishable from
buying a random leg at that price. (The −32% on the 16 tight-quote trades in §3 is n=16
noise; properly powered, the selection carries neither positive nor negative information.)

## The arithmetic that closes it

The floor is the agreement bucket: **−1.58% gross, ≈−2.6% net** of fees. That is what
taking liquidity on this book costs when your forecast is right. To clear it you must beat
the market by ~2.6 points per trade. Measured, the model is **1.8 points worse**
(Brier 0.0629 vs devigged 0.0553 over all structures; 0.1150 vs 0.0973 in the tradeable
range), and it beats the market on no series with a stable verdict — KXAAAGASW is 0/11 at
ratio 8.35, KXJOBLESSCLAIMS 1/10 at 1.88, natgas/WTI/Fed at parity, and the two apparent
winners are n=2 and n=1.

**Answer: no. Not with a forecasting model, on this universe, at these sizes.** The gap is
~4.4 points per trade and nothing measured here closes it.

## What is NOT closed

* **Making instead of taking.** The entire −2.6% is the cost of crossing. A resting order
  earns it instead of paying it. This is the only untested route with a plausible mechanism
  — and **this archive cannot test it**: candles are daily bars with no depth and no queue
  position, so adverse selection (the thing that decides whether making works) is exactly
  what is unobservable. Claiming it would work is not licensed by anything here.
* **A different universe.** Nothing above is a statement about prediction markets; it is a
  statement about 14 macro series on Kalshi over 75 days, where the market is a
  well-calibrated aggregator of the same public data our model reads.

## What must NOT happen next

The temptation from here is to slice: 18 cells were tested in §2 and one came back positive
before the split-half killed it. Any future pocket found in this window needs
pre-registration and a forward test — the `:nofav` precedent (#126) is the standing example
of a large, mechanistically-explained in-sample improvement that did not validate on the
held-out third.

Also note **#120**: Kalshi candles expire at ~75 days, so this archive is perishable. These
measurements cannot be reproduced after roughly 2026-10, and no archival cron is running.
