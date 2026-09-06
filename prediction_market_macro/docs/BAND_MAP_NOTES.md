# Where are the macro ladders profitable? — the band-map study (2026-09-06)

User question: crypto W7 makes its money only in one price band ([0.78, 0.98] favourite);
can each of the 14 macro markets be cut into "profitable segments" and traded only there?

Method: the production-identical **leg universe** (`research/leg_universe.py` — every
settled ladder leg at −1h and −24h, `fair` from the production replay with the params then
in force): **3,708 legs, 90 events, 14 series, 2026-05-25 → 2026-09-04**. Six conditioning
dimensions mapped in parallel (market price band, model-edge band, rung position, lead time
× liquidity, series/family, and the W7 analogue as a single pre-stated rule), each naming at
most ONE pre-stated candidate cell with its economic reason written before the number was
computed; every candidate then attacked by three adversarial lenses (multiplicity over all
512 cells, chronological/series/event stability, execution realism with ask/bid + fee +
staleness + the production gates). Every statistic is **event-cluster-robust** — legs of one
event settle together and are not independent samples (the W7 v3.1 lesson).

## Verdict: 512 cells, 6 pre-stated candidates, 18/18 adversarial votes REFUTED, 0 survivors

No band, edge bucket, rung, lead/liquidity regime or series clears the bar. The map did
produce four facts that are robust, and they are worth more than a false band:

**1. The market is calibrated where W7 finds its premium.** Favourite legs in [0.78, 0.98)
at −1h hit 92.3% against an implied 91.3%. The favourite-longshot premium W7 harvests on
15-minute binaries is not there on macro ladders an hour before the print. The W7 analogue
itself (one most-favoured in-band leg per event, taker + fee) prints **+1.16%/$, t 0.95,
85/86 hits against a breakeven of 84.1/86** — one loss decides the sign, the halves split
+2.4% / −0.1% on that single loss (KXCPIYOY 2026-06 T3.6). Not a bet.

**2. The model is a significantly worse forecaster than the market on tradeable legs:**
Brier 0.118 vs 0.069, delta −0.049, cluster t −4.0, negative at t ≤ −2 in essentially every
cell with ≥ 20 events. And it is worse at −1h (−6.2%/$) than at −24h (−2.6%/$): **the market
sharpens into the close and the model does not.**

**3. Where the model disagrees with the market, most of the disagreement is error.** The
realized share of the model's claimed edge (β of win−mp on fair−mp) falls from 0.72 at
|edge| < 0.02 to 0.15–0.31 beyond 0.05. The edge band the production gate admits (> 0.05 net)
is exactly where the model's edge is least real.

**4. The only cut the sample supports is the coarsest one — by series:**

| series | events | P&L / $ | cluster t | 90% CI | reading |
|---|---|---|---|---|---|
| KXJOBLESSCLAIMS | 15 | **−19.8%** | −3.04 | [−8.4%, −2.4%] | clearly lost |
| KXAAAGASW | 15 | **−9.6%** | −0.98 | [−12.8%, −5.3%] | clearly lost |
| KXWTIW | 15 | +2.5% | +1.24 | [−0.2%, +0.2%] | flat |
| KXNATGASW | 16 | +0.5% | +0.14 | [−0.1%, +0.2%] | flat |
| every monthly series | 1–4 | +11% … +37% | — | — | 1–4 events, half of them one event: unjudgeable |

**Production already acts on this.** `strategy/skill.py` computes the same model-vs-market
ratio and today reads KXAAAGASW 7.60 and KXJOBLESSCLAIMS 1.68 — both **blocked**; WTIW 1.06
(defensive), NATGASW 1.04 (open). The one conclusion the map licenses is a mechanism the
desk has been running since #184.

## What is NOT adopted, and why it is written down anyway

The loudest cell in the whole map — rung [0.5, 0.7) × energy, **+37%/$, t 3.18, 26 events,
44 legs, both halves positive, 3/3 series** — was **not pre-registered by any dimension** and
is recorded here exactly the way W7 records its off-primary buckets: as a MAP entry. Against
512 cells its nominal significance is not a finding; energy as a family is −0.3%/$ and
KXAAAGASW inside it is clearly negative, so the cell is most likely the WTIW between-buckets.
Anyone who wants to bet it must register it as its own primary cell and judge it FORWARD, on
new events, with an event-cluster t ≥ 2.5 — never on this sample.

## Why the W7 recipe does not transfer

W7 had tens of thousands of 15-minute windows and a venue-structural premium (a maker
selling a 5c contract risks 95c). Macro ladders give **90 events** with a public print the
whole market prices in advance, and the map shows the market is calibrated where the premium
would have to live. Searching 512 cells on 90 events will always produce pretty cells; the
adversarial pass exists to kill them, and it killed all of them.

Artifacts: `/tmp/dfm_verify/leg_universe.csv` (rebuild with `research/leg_universe.py`),
`band_map_*.py`, `refute_*.py`, `exec_*.py`; workflow transcript `wf_b253fec3-8c0`.
