"""W8 research kernel: sequential binary inventory, never a free-money pair.

WHAT W8 DOES NOW (v8a, 2026-09-14). On Kalshi's 15-minute BTC/ETH/DOGE/XRP
up/down binaries, post a small passive bid ONLY on the market's favoured side,
ONLY at $0.62-$0.86, and ONLY between 11 and 7 minutes before close. What
happens next is now the EXPERIMENT rather than a constant, and the two books
differ in exactly this one registered way:

    paired   CONTROL, complete_sets=True  - the v1..v7 rule: buy the opposite
             side whenever the pair costs <= 0.98, for a +2c lock
    tilted   TREATMENT, complete_sets=False - never buy a second leg. ONE entry
             per market, ridden to official settlement

because an independent audit measured the completion overlay, not the entry, as
where the money goes: sweeping the cap on a causal replay gives never-complete
+3.64c/contract (window-clustered t 3.71) against cap-0.98's -0.29c, a -3.92c
delta at t -5.75 with 0 of 12 days positive. That delta is an ALGEBRAIC
IDENTITY (pnl_completed - pnl_hold == the completion leg's own hold-to-
settlement P&L), so it is the one number here that does not depend on the fill
model. Mechanism: on a SINGLE order book a resting YES bid @p IS a NO offer
@(1-p), so "complete the set at <= 0.98" is arithmetically "buy the underdog at
its ask" - the overpriced longshot. It is the favourite-longshot bias that
earns the entry, run in reverse.
(SOL is deliberately excluded - see SERIES in the observer module.)

    entry      quote 5 contracts, maker, on the favoured side (market mid > .5
               -> yes, < .5 -> no), only while the price is inside
               [entry_band_lo, entry_band_hi] = [0.62, 0.86] AND the window
               has between 7 and 11 minutes left. Outside that box the edge
               is measured at zero or negative, so nothing is opened
    completion CONTROL book only: when the opposite side can be bought for
               <= pair_cost_cap total (0.98), take it. The treatment book never
               does, and takes no second entry either if the favourite flips -
               one entry per market is the rule the +3.64c was measured under
    residual   residual_ratio_cap (1.44x) of the PAIRED quantity on the favoured
               side. v7 computed 1.44 x max(clip, paired_quantity): that clip
               floor was undocumented and inverted the rule, handing the biggest
               directional allowance to the least hedged state
    exit       none by the clock: no leg stop (leg_stop_c = 0) and no timed
               unwind (flatten_before_s = 0) - both were measured to cost more
               than holding at this latency. A favoured leg rides to official
               settlement. Hard backstops still fire: max_window_loss
               ($12/window), the one-sided-book exit, $100 per book
    books      TWO independent ledgers - "tilted" (with residual) and "paired"
               (control, pairs only) - each with its own stop and its own
               verdict, so one can never truncate the other's sample

WHY EACH PIECE IS THERE - every number below was measured, not guessed:

 * FAVOURED SIDE ONLY (v3). Symmetric two-sided quoting just accumulates the
   anti-model leg on a single calibrated book: W8's own filled maker contracts
   priced below $0.50 (i.e. the unfavoured side) lost -5.09c each across 4,339
   contracts - over half of all volume.
 * THE ENTRY BAND (v4). The edge is WHERE you quote, not which side. Same
   8,177 filled contracts, by posted price: [.50,.60) -1.91c, [.60,.70)
   +1.95c, [.70,.78) +2.57c, [.78,.99) -3.16c. An idealised replay (one
   observation per window, pooled per contract, window-clustered) agrees:
   [.60,.70) +2.66c, t 2.04, both tails negative. Being the passive side earns
   the SPREAD, and the spread is proportionally largest mid-book; at $0.95 it
   earns nothing while the tail still costs -95c.
 * NO DIRECTION MODEL. A 17-day race over 4,464 settled windows (fair value
   Phi(dist/sigma*sqrt(tau)), momentum logistic, GBM on every feature, and a
   fair-value/mid blend) found NOTHING that beats the market mid at picking
   the side: where each model disagrees with the market it is right 49-53% of
   the time (all p > .10). Log-loss gains came from calibration, which does
   not help a side decision. Trap worth remembering: an early version of that
   test paired quotes up to 120s stale against 5s-fresh spot and "found"
   +0.023 log-loss at t 8.84; it inverts on fresh quotes. Evaluate a signal at
   the QUOTE's timestamp, never at a later one.
 * NO TIMED UNWIND EITHER (v6). Same lesson one level up: closing a losing
   favoured leg two minutes before settlement cost -5.39c/contract more than
   holding it (461 contracts over 13h). The exit buys the other side at a
   median 0.800 for something that wins 62.9%. Both the stop and the unwind
   were attempts to bound a loss that is already bounded by position size;
   what they actually bought was a guaranteed spread payment.
 * NO LEG STOP. v2 ran a 6c leg stop for 10 hours: it realised -31c/contract,
   because the observer's 4s pacing plus 429 cooldowns executes the exit 30-60s
   after the trigger. Riding to settlement cost -20c. At this latency a tight
   stop destroys value, so protection is structural (band + favoured side +
   early two-sided unwind), not a price trigger. Set leg_stop_c > 0 only with
   faster execution.
 * REAL MAKER FEES, THEN THE VENUE'S ACTUAL ONES. v1 booked maker fills at zero
   fee on 92% of volume and overstated its paired edge 15-18%, so v2 set
   maker_coefficient to 0.0175 "until the venue proves less". The venue has
   since proven less, hourly: _refresh_fees records fee_type=quadratic for every
   series, whose maker coefficient is 0. v7 charged itself 0.41c/contract -
   13.7% of the paired book's loss - on a fee that is not levied. v8 sets it 0.
 * THE QUEUE MODEL MUST NOT TELEPORT. Until v8, any print one tick through a
   resting quote set queue_ahead = 0 and filled it in full. That granted 85.2%
   of all simulated maker volume, and on 85.3% of the fully-taped cases the
   cumulative at-or-better print volume was still below the size displayed ahead
   when the order was posted (median 19.3% of it). A level can also be vacated
   by cancellation, which fills nobody. Both branches now decrement.
 * PAIRS ARE BOOKED IN THREE KINDS, because FIFO netting hides three different
   trades under one word: set (cost .975-1.00, the designed complete set),
   drift (< .975 - the second leg was cheap because the first had already won,
   i.e. directional P&L), and stop (> 1.00 - paying to get out). v1 reported
   all three as "paired profit" and 75% of it was actually direction.

HONEST STATUS. This is an OBSERVATION, not a proven edge. The reference
account's Polymarket economics do not port: two independent books there allow
a real sub-$1 mechanical arb (on Kalshi's single book it is arithmetically
impossible - 0 occurrences in 2,646 markets), it executes sub-second, and its
~40%-per-trade return implies the money was in 5-minute retail mispricing of
13-19c that Kalshi's calibrated book does not offer. v5 is the nearest thing
that IS executable here. Each book decides once, at 300 clean windows
(pooled per-window sums, cluster t >= 2.5), with an always-valid evidence kill
before that. Multiplicity is on the record: 4 signal families and 6 price
buckets x 2 execution styles were tried; [.60,.78] is a post-hoc bucket
supported by two independent measurements, so the live book is the test.

The public reference account's private model is unknown. This kernel implements
the observable mechanism (small bilateral orders, sequential netting, bounded
residual inventory) with an explicitly labelled local microprice proxy.
Pure functions are shared by replay and the live observation module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math


@dataclass(frozen=True)
class Parameters:
    """v2 (2026-09-12): re-registered after the v1 audit. Three measured
    defects drove v1's -$101/-$87: (a) maker fees were booked at zero on 92%
    of executed volume, overstating the paired edge 15-18%; (b) the unwind
    deadline sat INSIDE the final 30s where 91.5% of recorded books are
    one-sided, so zero risk_flatten fills ever happened and every unpaired
    leg rode to settlement (-$161 residual vs +$60 paired); (c) "complete
    the set" is byte-identical to "close the leg" on Kalshi's single book
    (fee symmetric under p -> 1-p), so pair_cost_cap=0.98 was a +2c
    take-profit with NO matching stop - negative skew by construction.
    v2 keeps the reference account's shape (98c sets, 1.44x residual, small
    clips) and adds the missing half: leg_stop_c, and an unwind moved to
    where the book is still two-sided (one-sided rate outside the last 30s:
    1.1%). Stops/windows chosen a priori, not fitted; observation judges."""
    version: str = "w8_v8a_20260914"
    # Research default. The LIVE clip comes from config (`contracts`), which
    # v8 cuts 5 -> 1 so the $100 backstop stops binding five times earlier than
    # the 300-window latch; `as_dict()` records the effective value.
    clip: float = 5.0
    max_net: float = 15.0
    max_gross_per_market: float = 250.0
    pair_cost_cap: float = 0.98  # BOTH actual fill costs + fees, per paired unit
    # v8: the venue's own /series data has been reporting fee_type=quadratic
    # with an hourly re-check since registration (see _refresh_fees), which is
    # the release condition fee_usd's docstring states for a zero maker
    # coefficient. v7 left the placeholder 0.0175 in place anyway and charged
    # itself 0.41c/contract - 13.7% of the paired book's loss - for a fee the
    # exchange does not levy.
    maker_coefficient: float = 0.0
    taker_coefficient: float = 0.07
    # v8: completion is the EXPERIMENT, not a constant. See the v8 note below.
    complete_sets: bool = True
    latency_s: float = 0.5
    quote_ttl_s: float = 20.0
    stop_new_before_s: float = 180.0
    # v6 (2026-09-13): the TIMED unwind is OFF. It was added in v2 to stop
    # unpaired legs riding to settlement, but that diagnosis was wrong: v1's
    # residual bled because it held the ANTI-favoured leg, which v3 fixed by
    # quoting the favoured side only. Measured on 13h of v5 fills, the timed
    # unwind costs -5.39c per contract MORE than simply holding (461
    # contracts, -$24.86; the flattened leg's own cost is sunk in both
    # branches, so this is the clean incremental). Why: it crosses the spread
    # at a median price of 0.800 for a side that then wins only 62.9% - about
    # 17c of adverse pricing plus 0.8c of fee, with two minutes left and a
    # thin book. A favoured leg held to settlement is a fair bet with bounded
    # loss (max_net caps the size); paying 5c to avoid it is not insurance,
    # it is a fee. Structural guards remain: max_window_loss force_flatten,
    # the one-sided-book exit, and the per-book dollar backstop.
    # Set > 0 again only with evidence that exits have become cheap.
    flatten_before_s: float = 0.0
    start_after_s: float = 15.0
    max_spread: float = 0.15
    residual_ratio_cap: float = 1.44  # reference hypothesis, NOT recovered model
    max_window_loss: float = 12.0
    # v3: leg stop DISABLED by default. v2 measured it for 10 hours: the 6c
    # trigger realised -31c/contract because the observer's 4s-paced polling
    # (plus 429 cooldowns) executes the exit 30-60s after the trigger in a
    # market that moves cents per second - WORSE than v1's ride-to-settlement
    # tail (-20c/contract). At this latency a tight stop destroys value; the
    # protections that remain are structural: fresh inventory only on the
    # favored side, completion as the profit lock, and the T-120s unwind
    # while books are still two-sided. Set > 0 only with faster execution.
    leg_stop_c: float = 0.0
    # v4 ENTRY BAND (2026-09-12). A 17-day signal race found NO direction
    # signal that beats the market mid at picking the side: fair value
    # Phi(dist/sigma*sqrt(tau)) -0.002 logloss (flip accuracy 49.3%, p .54),
    # momentum +0.0006 (50.1%, p 1.0), GBM +0.0008 (52.6%, p .11) - every
    # family is a coin flip exactly where it disagrees with the market. (An
    # apparent edge of +0.023 logloss was an artifact: the base table paired
    # quotes up to 120s stale against 5s-fresh spot; it vanishes and inverts
    # on fresh quotes, which is the live condition.)
    # What IS measurable is WHERE to quote, not WHICH side. W8's own 8,177
    # actually-filled maker contracts, by the price posted:
    #   [0.02,0.50) 4339 ct -5.09c   (the anti-favored leg v3 already removed)
    #   [0.50,0.60) 1961 ct -1.91c
    #   [0.60,0.70)  992 ct +1.95c
    #   [0.70,0.78)  378 ct +2.57c
    #   [0.78,0.99)  497 ct -3.16c
    # Idealised passive entry (one observation per window, pooled per contract,
    # window-clustered) agrees: [0.60,0.70) +2.66c t 2.04, both tails negative.
    # So fresh inventory is quoted ONLY inside this band; completion quotes and
    # risk-reducing exits are exempt (they close, they do not open).
    # v7 (2026-09-13): band widened UP and the entry confined to a time
    # window. Both edges of the old band were dead weight and the old timing
    # spanned a zone that loses money. Measured on 17 days / 4 coins, one
    # observation per window, CAUSAL entry (first moment the price enters the
    # band inside the time window - no conditioning on the later path):
    #   T-14..T-2 x [.60,.78]  (v4/v6 rule)  +1.90c  t 3.13  334 win/day 13/17 days +
    #   T-11..T-7 x [.62,.86]  (v7 rule)     +3.37c  t 5.59  287 win/day 16/17 days +
    # The price margin: [.55,.62) is flat-to-negative at every decision time,
    # while [.78,.95) pays +2 to +4.4c - the favourite-longshot bias, which the
    # literature reports at 2-5% concentrated above 80c and which W7 trades
    # independently. The time margin: T-1..T-3 is -4.02c (the book thins and
    # the spread is crossed against you) while T-7..T-11 is +3.4 to +3.9c.
    # Per coin at the new rule: XRP +3.82 (t 3.40), DOGE +3.60 (t 3.18),
    # BTC +3.23 (t 2.66), ETH +1.70 (t 1.43) - all positive, ETH weakest.
    # WATCH OUT: an earlier version of this study read +9 to +11c because it
    # selected, per window, the LAST moment the price sat in the band. That
    # conditions on where the price went afterwards. Every number above takes
    # the FIRST qualifying moment, which is what the live rule can actually do.
    entry_band_lo: float = 0.62
    entry_band_hi: float = 0.86
    # Fresh inventory only inside this remaining-time window (seconds).
    entry_rem_lo_s: float = 420.0    # stop opening at T-7min
    entry_rem_hi_s: float = 660.0    # start opening at T-11min
    # v8 (2026-09-14): COMPLETION IS THE LOSS. Seven versions tuned the entry
    # and never touched the overlay the strategy is named after. Causal replay
    # on the 90s strips (4 coins, 2026-09-03..09-14, 3,157 entries / 980
    # distinct closes, entry = first snapshot with rem in [420,660]s and the
    # favoured bid in [0.62,0.86]), sweeping the cap:
    #   never complete      +3.64c/ct  window-clustered t  3.71
    #   cap 0.90            +1.46c     (vs hold: -2.17c, t -4.10)
    #   cap 0.94            +0.63c     (vs hold: -3.01c, t -4.89)
    #   cap 0.98 (v1..v7)   -0.29c     (vs hold: -3.92c, t -5.75), 85.1% done
    # The delta is an ALGEBRAIC IDENTITY - pnl_completed - pnl_hold == the
    # completion leg's own hold-to-settlement P&L - so unlike every other
    # number here it does not depend on whether the entry quote fills. 0 of 12
    # days positive; every coin and every remaining-time bucket negative; still
    # -2.38c (t -3.27) under an optimistic maker completion. The completion leg
    # alone: 2,686 legs bought at avg $0.162 where the underdog realised 12.6%.
    # WHY, and this is the whole answer to "the reference account does 98c sets
    # profitably": on Kalshi's SINGLE book a resting YES bid @p IS a NO offer
    # @(1-p), so "complete a set" is byte-identical to "buy the underdog at its
    # ask" - the overpriced longshot. It is the favourite-longshot bias that
    # earns the entry, run in REVERSE. Polymarket can mint a real set for $1;
    # here both legs come from the same ladder, so the overlay buys variance
    # reduction (per-window sd 100.3c -> 65.0c) at 3.9c on a 3.6c edge -
    # negative expectancy by construction.
    # The forward books therefore test exactly this, paired on the same
    # markets: "paired" = control, the v1..v7 rule (complete_sets True);
    # "tilted" = treatment, no completion at all (buy the favourite inside the
    # box, hold to settlement). Registered before observation, one change.
    # HONESTY: +3.64c assumes every bid quote fills with no maker adverse
    # selection, so it is an upper bound; the -3.92c DELTA is not.

    def as_dict(self):
        return asdict(self)


# A pair costing at least this is the designed complete set; below it, the
# second leg was cheap because the market had already moved (drift pair).
SET_PAIR_COST_FLOOR = 0.975


def fee_usd(price: float, quantity: float, coefficient: float) -> float:
    """July 7, 2026 schedule: round (cost+fee) up to $0.0001.

    Fee at each partial fill; no rebates are assumed. Maker coefficient zero
    is allowed only after the live adapter verifies series fee_type=quadratic.
    """
    p, q, c = map(lambda x: Decimal(str(x)), (price, quantity, coefficient))
    if not (0 < p < 1) or q <= 0 or c < 0:
        raise ValueError("invalid price, quantity or coefficient")
    if c == 0:
        return 0.0
    cost = p * q
    total = (cost + c * q * p * (1-p)).quantize(Decimal(".0001"), rounding=ROUND_CEILING)
    return float(total-cost)


def floor_price(p: float) -> float:
    """Kalshi tapered_deci_cent: .001 below .10/above .90, .01 in middle."""
    if not math.isfinite(p) or p < .001:
        return 0.0
    p = min(p, .999)
    tick = Decimal(".001") if p < .10 or p >= .90 else Decimal(".01")
    return float((Decimal(str(p))/tick).to_integral_value(rounding=ROUND_FLOOR)*tick)


def normalize_book(raw: dict, *, allow_one_sided: bool = False) -> dict | None:
    """Normalize depth; optionally retain a one-sided book for risk exits.

    A YES holding can be closed against YES bids even if NO bids disappear.
    Such books expose absent bid/ask values as None and two_sided=False;
    only walk_buy, liquidation_value and flatten may consume them. Quotes and
    direction decisions still require both sides, including an unlocked spread.
    """
    fp = raw.get("orderbook_fp", raw)
    sides = {}
    for side in ("yes", "no"):
        merged = {}
        for row in fp.get(side+"_dollars", []):
            try:
                p, q = float(row[0]), float(row[1])
            except (ValueError, TypeError, IndexError):
                continue
            if math.isfinite(p) and math.isfinite(q) and 0 < p < 1 and q > 0:
                merged[p] = merged.get(p, 0.0)+q
        if not merged and not allow_one_sided:
            return None
        sides[side] = sorted(merged.items(), reverse=True)
    if not sides["yes"] and not sides["no"]:
        return None
    yb = sides["yes"][0][0] if sides["yes"] else None
    nb = sides["no"][0][0] if sides["no"] else None
    two_sided = yb is not None and nb is not None
    if two_sided and yb+nb >= 1.0-1e-9:  # crossed/locked payload is not an executable arb
        return None
    sides.update(yes_bid=yb, no_bid=nb,
                 yes_ask=1-nb if nb is not None else None,
                 no_ask=1-yb if yb is not None else None, two_sided=two_sided)
    return sides


def walk_buy(book: dict, side: str, qty: float, max_price: float = .999) -> list:
    """Consume the opposite bids, preserving fractional quantities and levels."""
    other = "no" if side == "yes" else "yes"
    left, fills = qty, []
    for bid, size in book[other]:
        price = round(1-bid, 4)
        if price > max_price+1e-9:
            break
        q = min(left, size)
        if q > 1e-9:
            fills.append((price, q))
            left -= q
        if left <= 1e-9:
            break
    return fills


def new_market(ticker: str, series: str, close_ts: float, opened_ts: float) -> dict:
    return dict(ticker=ticker, series=series, close_ts=close_ts,
                opened_ts=opened_ts, lots={"yes": [], "no": []}, orders=[],
                fills=[], pairs=[], seen_trade_ids=[], bought={"yes": 0., "no": 0.},
                cost_usd=0., fees_usd=0., paired_quantity=0., paired_net_usd=0.,
                turnover=0., sequence=0, mid_history=[], stop_new=False)


def inventory(m: dict, side: str) -> float:
    return sum(l["quantity"] for l in m["lots"][side])


def net_quantity(m: dict) -> float:
    return inventory(m, "yes")-inventory(m, "no")


def add_fill(m: dict, side: str, quantity: float, price: float, ts: float,
             coefficient: float, *, liquidity: str, source: str) -> dict:
    """FIFO netting. Pair proceeds and residual settlement reconcile to cash."""
    if side not in ("yes", "no") or quantity <= 0:
        raise ValueError("bad fill")
    fee = fee_usd(price, quantity, coefficient)
    row = dict(side=side, quantity=quantity, price=price, fee_usd=fee, ts=ts,
               liquidity=liquidity, source=source)
    m["fills"].append(row)
    m["cost_usd"] += quantity*price
    m["fees_usd"] += fee
    m["bought"][side] += quantity
    m["turnover"] += quantity
    other = "no" if side == "yes" else "yes"
    remaining, per_fee = quantity, fee/quantity
    while remaining > 1e-9 and m["lots"][other]:
        lot = m["lots"][other][0]
        matched = min(remaining, lot["quantity"])
        pair_cost = price+per_fee+lot["price"]+lot["fee_per_unit"]
        net = matched*(1-pair_cost)
        m["pairs"].append(dict(quantity=matched, cost_per_pair=pair_cost,
                               net_usd=net, first_ts=lot["ts"], second_ts=ts,
                               lag_s=ts-lot["ts"], first_side=other))
        m["paired_quantity"] += matched
        m["paired_net_usd"] += net
        remaining -= matched
        lot["quantity"] -= matched
        if lot["quantity"] <= 1e-9:
            m["lots"][other].pop(0)
    if remaining > 1e-9:
        m["lots"][side].append(dict(quantity=remaining, price=price,
                                   fee_per_unit=per_fee, ts=ts))
    return row


def settle(m: dict, result: str | None) -> dict | None:
    if result not in ("yes", "no"):
        return None  # never drop missing settlements or infer them from spot
    payout = m["bought"][result]
    net = payout-m["cost_usd"]-m["fees_usd"]
    residual = sum(l["quantity"]*((1 if s == result else 0)-l["price"]-l["fee_per_unit"])
                   for s in ("yes", "no") for l in m["lots"][s])
    if abs(net-m["paired_net_usd"]-residual) > 1e-6:
        raise AssertionError("pair/residual ledger does not reconcile")
    q = m["paired_quantity"]
    # v2: a pair completed near the cap is the DESIGNED maker/maker set; a
    # pair completed far below it exists because the first leg had already
    # won before the second was bought - that is directional PnL which FIFO
    # netting happens to label a pair (v1 audit: 75% of "+paired" was this).
    # Both are real money; they are just different trades, so book them apart.
    set_q = set_net = drift_q = drift_net = stop_q = stop_net = 0.0
    for pair in m["pairs"]:
        if pair["cost_per_pair"] > 1.0:
            # a pair costing over $1 is a realised LOSS - the leg-stop (or a
            # forced unwind) buying the way out. Booking it as a "set" hid
            # the stop's true cost in the first v2 hours (-31c/contract
            # realised against a 6c trigger = latency slippage; measured
            # 2026-09-12). It gets its own line.
            stop_q += pair["quantity"]; stop_net += pair["net_usd"]
        elif pair["cost_per_pair"] >= SET_PAIR_COST_FLOOR:
            set_q += pair["quantity"]; set_net += pair["net_usd"]
        else:
            drift_q += pair["quantity"]; drift_net += pair["net_usd"]
    return dict(ticker=m["ticker"], series=m["series"], close_ts=m["close_ts"],
                result=result, fills=len(m["fills"]), quantity=m["turnover"],
                cost_usd=m["cost_usd"], fees_usd=m["fees_usd"], payout_usd=payout,
                net_usd=net, paired_quantity=q, paired_net_usd=m["paired_net_usd"],
                paired_cost_vwap=(1-m["paired_net_usd"]/q if q else None),
                set_pair_quantity=set_q, set_pair_net_usd=round(set_net, 6),
                drift_pair_quantity=drift_q, drift_pair_net_usd=round(drift_net, 6),
                stop_pair_quantity=stop_q, stop_pair_net_usd=round(stop_net, 6),
                residual_net_usd=residual, residual_quantity=abs(net_quantity(m)),
                pairs_below_one=sum(p["quantity"] for p in m["pairs"] if p["cost_per_pair"] < 1),
                stopped=m["stop_new"], coverage_gap=bool(m.get("coverage_gap")),
                unverified_order_quantity=m.get("unverified_order_quantity", 0.),
                fill_model="trade_print_queue_conservative")


def liquidation_value(m: dict, book: dict, p: Parameters) -> dict:
    """Net realised-pair PnL + executable residual mark, with fee and depth."""
    value, shortfall = m["paired_net_usd"], 0.0
    for side in ("yes", "no"):
        qty = inventory(m, side)
        if qty <= 0:
            continue
        opposite = "no" if side == "yes" else "yes"
        fills = walk_buy(book, opposite, qty)
        filled = sum(q for _, q in fills)
        basis = sum(l["quantity"]*(l["price"]+l["fee_per_unit"]) for l in m["lots"][side])
        value -= basis
        value += sum(q*(1-price)-fee_usd(price, q, p.taker_coefficient) for price, q in fills)
        shortfall += qty-filled  # unfillable inventory conservatively valued zero
    return {"net_usd": value, "shortfall": shortfall}


def process_trades(m: dict, trades: list[dict], p: Parameters) -> list[dict]:
    """Only subsequent opposite-aggressor prints can fill a resting order.

    At the quoted level, pre-existing displayed size is ahead of us. Trades
    strictly through our quote may fill at most their observed quantity.
    Neither a touched quote, cancellation, nor a trade before activation fills.
    Counterfactual queue fills remain a model, not actual account fills.
    """
    seen = set(m["seen_trade_ids"])
    seen_order = list(m["seen_trade_ids"])
    out = []
    for t in sorted(trades, key=lambda x: (x["ts"], x["trade_id"])):
        tid, ts = t["trade_id"], t["ts"]
        if tid in seen:
            continue
        seen.add(tid)
        seen_order.append(tid)
        if t.get("ticker") != m["ticker"] or t.get("is_block_trade"):
            continue
        maker_side = "no" if t.get("taker_side") == "yes" else "yes" if t.get("taker_side") == "no" else None
        if maker_side is None:
            continue
        price = t["yes_price"] if maker_side == "yes" else 1-t["yes_price"]
        available = t["quantity"]
        for order in sorted(m["orders"], key=lambda o: (-o["price"], o["activate_ts"])):
            end = min(order["expires_ts"], order.get("cancel_ts", float("inf")))
            if (order["side"] != maker_side or not order["activate_ts"] < ts < end
                    or order["remaining"] <= 1e-9 or price > order["price"]+1e-9):
                continue
            # v8: a print THROUGH the quote used to zero the whole queue
            # (`order["queue_ahead"] = 0.0`), which granted 85.2% of all
            # simulated maker volume - 762 of 893 fully-taped through-fills
            # happened while cumulative at-or-better print volume was still
            # BELOW the size displayed ahead at posting (median 19.3% of it).
            # One 1-lot print four ticks through teleported a 5-lot quote past
            # 786 displayed contracts. A level can also be vacated by
            # cancellation, in which case nobody is filled at the old price.
            # Decrementing in both branches is the same bookkeeping: a genuine
            # sweep emits its own at-or-better prints and still fills.
            queue_used = min(available, order["queue_ahead"])
            order["queue_ahead"] -= queue_used
            available -= queue_used
            q = min(available, order["remaining"])
            if q > 1e-9:
                row = add_fill(m, maker_side, q, order["price"], ts,
                               p.maker_coefficient, liquidity="maker_model", source=tid)
                row["order_id"] = order["id"]
                order["remaining"] -= q
                available -= q
                out.append(row)
            if available <= 1e-9:
                break
    m["seen_trade_ids"] = seen_order[-30000:]
    return out


def _reserve(m: dict, side: str, now: float) -> float:
    return sum(o["remaining"] for o in m["orders"] if o["side"] == side
               and min(o["expires_ts"], o.get("cancel_ts", float("inf"))) > now)


def _unread_order_risk(m: dict, side: str, now: float) -> float:
    """A cancelled order is not cleared until its whole life is in the tape."""
    watermark = min(now, m.get("trade_watermark_ts", now))
    return _reserve(m, side, watermark)


def update_quotes(m: dict, book: dict, now: float, p: Parameters,
                  *, residual: bool = True) -> list[dict]:
    """Bounded bilateral maker quotes, then inventory-aware pairing.

    Directional tilt is a transparent top-of-book microprice proxy. It is not
    inferred to be the reference trader's private probability model. Paired-only
    runs the same policy without the extra directional allowance.
    """
    if not book.get("two_sided", True):
        raise ValueError("two-sided book required for quoting")
    # Keep expired/cancelled orders until market settlement. Public prints may
    # arrive after cancellation while carrying an earlier exchange timestamp;
    # deleting the order here would silently discard those delayed fills.
    remaining = m["close_ts"]-now
    if remaining <= p.stop_new_before_s or m["turnover"] >= p.max_gross_per_market:
        m["stop_new"] = True
    if liquidation_value(m, book, p)["net_usd"] < -p.max_window_loss:
        m["stop_new"] = True
        m["force_flatten"] = True
    # Leg-level stop (v3: off by default, see Parameters.leg_stop_c).
    for held in (("yes", "no") if p.leg_stop_c > 0 else ()):
        if inventory(m, held) <= 1e-9 or not m["lots"][held]:
            continue
        basis = max(l["price"]+l["fee_per_unit"] for l in m["lots"][held])
        bid = book.get(held+"_bid")
        if bid is not None and basis-bid >= p.leg_stop_c/100-1e-9:
            m["stop_new"] = True
            m["force_flatten"] = True
            m["leg_stop_hit"] = dict(ts=now, side=held, basis=round(basis, 4),
                                     bid=bid)
    mid = (book["yes_bid"]+book["yes_ask"])/2
    ys, ns = book["yes"][0][1], book["no"][0][1]
    micro = (book["yes_ask"]*ys+book["yes_bid"]*ns)/(ys+ns)
    tilt = max(-.01, min(.01, micro-mid)) if residual else 0.
    fair = min(.999, max(.001, mid+tilt))
    m["last_fair_proxy"] = {"ts": now, "mid": mid, "microprice": micro,
                             "p_yes_proxy": fair, "model": "top_depth_microprice_not_calibrated"}
    m["mid_history"] = (m["mid_history"]+[[now, mid]])[-60:]
    if (book["yes_ask"]-book["yes_bid"] > p.max_spread or
            remaining > 900-p.start_after_s or remaining <= p.flatten_before_s):
        for order in m["orders"]:
            order.setdefault("cancel_ts", now+p.latency_s)
        return []
    # Leave half the desired complete-set discount on each initial quote.
    half = (1-p.pair_cost_cap)/2
    targets = {"yes": min(book["yes_bid"], fair-half),
               "no": min(book["no_bid"], 1-fair-half)}
    desired = {}
    for side in ("yes", "no"):
        other = "no" if side == "yes" else "yes"
        own, opp = inventory(m, side), inventory(m, other)
        # v8: with completion off, opposite inventory grants no exemption and
        # no completing quote - the leg is simply held to settlement.
        exempt = opp if p.complete_sets else 0.
        price = targets[side]
        if exempt > 0:
            worst = max(l["price"]+l["fee_per_unit"] for l in m["lots"][other])
            price = min(book[side+"_bid"], p.pair_cost_cap-worst)
        price = floor_price(price)
        while price > 0 and exempt > 0 and (price+fee_usd(price, p.clip, p.maker_coefficient)/p.clip+worst > p.pair_cost_cap+1e-9):
            price = floor_price(price-.001)
        cap = p.max_net if residual else p.clip
        quantity = min(p.clip, max(0., cap-own+opp),
                       max(0., p.max_gross_per_market-m["turnover"]-_unread_order_risk(m, other, now)))
        # v3 (imitating the reference account where it can be imitated): FRESH
        # inventory only on the model-favored side; the other side quotes
        # purely to COMPLETE - "complete set负责底仓对冲,多出来的留给模型更
        # 看好的方向". v1/v2 quoted both sides symmetrically, which on
        # Kalshi's single calibrated book just accumulates the anti-model leg
        # (drift pairs, the one measured profit line, all start favored-side).
        if side != ("yes" if fair > .5 else "no"):
            quantity = min(quantity, exempt)
        # v4: FRESH inventory (no opposite lot to complete) only inside the
        # measured entry band and time window. A completion quote is exempt
        # because it reduces risk - but only for the part that actually
        # completes. v7 guarded both gates with `opp <= 1e-9`, so ANY quote
        # with opposite inventory skipped the box entirely at full clip size;
        # when the favourite flipped mid-window the surplus opened FRESH risk
        # outside [lo,hi] and outside the time window, at a price pinned low by
        # the completion formula - i.e. it could only fill on a violent move
        # against the held leg, precisely the tail the box exists to avoid.
        # (Never observed live in 156 v7 markets; the invariant was resting on
        # that luck rather than on the code.)
        if max(0., quantity-exempt) > 1e-9 and not (
                p.entry_band_lo - 1e-9 <= price <= p.entry_band_hi + 1e-9):
            quantity = min(quantity, exempt)
        if max(0., quantity-exempt) > 1e-9 and not (
                p.entry_rem_lo_s <= remaining <= p.entry_rem_hi_s):
            quantity = min(quantity, exempt)
        if own > 0:
            # Extra inventory only on the local model's side; do not enforce
            # a hindsight ratio or pretend its excess has been hedged.
            favored = "yes" if fair > .5 else "no"
            # v8: was `residual_ratio_cap*max(p.clip, paired_quantity)`. The
            # clip floor was undocumented and inverted the stated rule - it
            # granted the LARGEST directional allowance (7.19 contracts) in the
            # least hedged state, paired_quantity == 0, and only 2.2 once five
            # were actually paired. 8 of 160 v7 tilted markets carried 7.19
            # residual contracts with zero pairs; the ~15.9 contracts held above
            # one clip are worth about -$9.54, larger than the entire -$6.85
            # gap between the two arms. take_pair at the bottom of this module
            # always used the documented form; now both agree.
            allowed = p.residual_ratio_cap*m["paired_quantity"]
            allowance = min(p.max_net, max(0., allowed-m["paired_quantity"]))
            if not residual or side != favored:
                quantity = 0.
            else:
                quantity = min(quantity, max(0., allowance-own))
        if not p.complete_sets:
            # ONE entry per market, which is the rule the +3.64c was measured
            # under ("first snapshot with rem inside the window and the favoured
            # bid inside the band", then hold). Without it the treatment arm
            # opens a SECOND fresh leg whenever the favourite flips mid-window:
            # that leg is legitimately inside the box, but FIFO then nets it
            # against the first and the book reports a "pair" no completion
            # quote ever made - a rule the forward verdict is not registered
            # for. Seen within 90 minutes of v8 going live (DOGE yes 0.79 while
            # holding NO), so this is a live path, not a hypothetical.
            quantity = min(quantity, max(0., p.clip-m["turnover"]))
        if m["stop_new"]:
            quantity = min(quantity, exempt)  # keep only risk-reducing pair quotes
        desired[side] = (price, math.floor(quantity*100)/100)
    for o in m["orders"]:
        target, quantity = desired[o["side"]]
        # A smaller hedge must also replace the order. Keeping an old larger
        # order at the same price can reverse inventory after stop_new begins.
        # Cancellation latency and unread fills remain reserved below.
        if (abs(o["price"]-target) > 1e-8 or quantity <= 0
                or o["remaining"] > quantity+1e-9):
            o.setdefault("cancel_ts", now+p.latency_s)
    created = []
    for side, (price, quantity) in desired.items():
        # Cancellation has latency: wait before replacing, never double reserve.
        if _unread_order_risk(m, side, now) > 1e-9 or price <= 0 or quantity <= 0:
            continue
        if price >= book[side+"_ask"]-1e-9:
            continue
        quantity = min(quantity, max(0., p.max_gross_per_market-m["turnover"]
                                     -_unread_order_risk(m, "yes", now)-_unread_order_risk(m, "no", now)))
        if quantity <= 1e-9:
            continue
        queue = sum(q for bid, q in book[side] if bid >= price-1e-9)
        m["sequence"] += 1
        order = dict(id=f'{m["ticker"]}:{m["sequence"]}', side=side, price=price,
                     quantity=quantity, remaining=quantity, queue_ahead=queue,
                     created_ts=now, activate_ts=now+p.latency_s,
                     expires_ts=math.floor(min(now+p.quote_ttl_s, m["close_ts"]-p.flatten_before_s)))
        m["orders"].append(order)
        created.append(order.copy())
    return created


def flatten(m: dict, book: dict, now: float, p: Parameters) -> list[dict]:
    """Flatten residual at executable levels after maker cancellation takes effect."""
    for order in m["orders"]:
        order.setdefault("cancel_ts", now+p.latency_s)
    if any(_unread_order_risk(m, s, now) > 1e-9 for s in ("yes", "no")):
        return []
    out = []
    for held in ("yes", "no"):
        quantity = inventory(m, held)
        other = "no" if held == "yes" else "yes"
        for price, q in walk_buy(book, other, quantity):
            out.append(add_fill(m, other, q, price, now, p.taker_coefficient,
                                liquidity="taker_depth_model", source="risk_flatten"))
    return out


def take_pair(m: dict, book: dict, now: float, p: Parameters,
              *, residual: bool = True) -> list[dict]:
    """Take a profitable second leg, including its real taker fee and depth.

    The reference account is empirically mixed maker/taker. This is the
    observable second-leg mechanism, not a reconstruction of its private
    first-leg alpha. Existing resting orders are cancelled before an IOC
    hedge, and residual quantity is limited explicitly rather than hidden.
    """
    if not book.get("two_sided", True):
        raise ValueError("two-sided book required for directional pairing")
    if not p.complete_sets:
        return []  # v8 treatment arm: the first leg is held, never hedged.
    out = []
    mid = (book["yes_bid"]+book["yes_ask"])/2
    favored = "yes" if mid > .5 else "no"
    for held in ("yes", "no"):
        owned = inventory(m, held)
        if owned <= 1e-9:
            continue
        keep = 0.
        if residual and held == favored and not m["stop_new"]:
            keep = min(p.max_net, (p.residual_ratio_cap-1)*m["paired_quantity"])
        qty = min(p.clip, max(0., owned-keep), max(0., p.max_gross_per_market-m["turnover"]
                                                    -_unread_order_risk(m, held, now)))
        if qty <= 1e-9:
            continue
        opposite = "no" if held == "yes" else "yes"
        worst = max(l["price"]+l["fee_per_unit"] for l in m["lots"][held])
        possible = []
        for price, q in walk_buy(book, opposite, qty):
            all_in = worst+price+fee_usd(price, q, p.taker_coefficient)/q
            if all_in > p.pair_cost_cap+1e-9:
                break
            possible.append((price, q))
        if not possible:
            continue
        for order in m["orders"]:
            if order["side"] == opposite:
                order.setdefault("cancel_ts", now+p.latency_s)
        if _unread_order_risk(m, opposite, now) > 1e-9:
            continue
        # IOC cannot consume the same displayed quantity in the next flatten.
        for price, q in possible:
            row = add_fill(m, opposite, q, price, now, p.taker_coefficient,
                           liquidity="taker_depth_model", source="profitable_pair_hedge")
            out.append(row)
    return out


# ── v2 verdict statistics — copied VERBATIM from w7_noisefade.py (2026-09-12)
# rather than imported: W8 must stay standalone so that retiring either
# experiment can never break the other (the runner-import landmine, 9/11).

def always_valid_bound(n_windows: int, rho: float = 300.0,
                       alpha: float = 0.05) -> float:
    """|t| threshold that stays valid under CONTINUOUS monitoring.

    A fixed-n bar re-tested every cycle is an optional-stopping rule (W7
    measured 6.2% type-I where 0.6% was advertised). One-sided normal-mixture
    boundary (Howard et al.): ~3.66 at n=300, looser than nothing but honest.
    Being stricter than a fixed -2 only delays a kill - the safe direction.
    """
    import math
    if n_windows < 2:
        return float("inf")
    return math.sqrt(((n_windows + rho) / n_windows)
                     * (2 * math.log(1 / alpha) + math.log((n_windows + rho) / rho)))


def window_sum_stats(sums: list[float]) -> tuple[int, float, float]:
    """(n, mean_usd, t) over independent window SUMS - the money per window.
    BTC and ETH settle the same 15-minute macro move, so the caller must sum
    them into one number per close_ts before calling (cluster = window)."""
    import math
    n = len(sums)
    if n < 2:
        return n, (sums[0] if sums else 0.0), 0.0
    mu = sum(sums) / n
    var = sum((x - mu) ** 2 for x in sums) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else float("inf")
    return n, mu, (mu / se if se > 0 else 0.0)
