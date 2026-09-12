"""W8 research kernel: sequential binary inventory, never a free-money pair.

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
    version: str = "w8_v1_20260910"
    clip: float = 5.0
    max_net: float = 15.0
    max_gross_per_market: float = 250.0
    pair_cost_cap: float = 0.98  # BOTH actual fill costs + fees, per paired unit
    maker_coefficient: float = 0.0  # current quadratic series: taker only
    taker_coefficient: float = 0.07
    latency_s: float = 0.5
    quote_ttl_s: float = 20.0
    stop_new_before_s: float = 90.0
    flatten_before_s: float = 30.0
    start_after_s: float = 15.0
    max_spread: float = 0.15
    residual_ratio_cap: float = 1.44  # reference hypothesis, NOT recovered model
    max_window_loss: float = 12.0

    def as_dict(self):
        return asdict(self)


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
    return dict(ticker=m["ticker"], series=m["series"], close_ts=m["close_ts"],
                result=result, fills=len(m["fills"]), quantity=m["turnover"],
                cost_usd=m["cost_usd"], fees_usd=m["fees_usd"], payout_usd=payout,
                net_usd=net, paired_quantity=q, paired_net_usd=m["paired_net_usd"],
                paired_cost_vwap=(1-m["paired_net_usd"]/q if q else None),
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
            if abs(price-order["price"]) < 1e-9:
                queue_used = min(available, order["queue_ahead"])
                order["queue_ahead"] -= queue_used
                available -= queue_used
            else:
                order["queue_ahead"] = 0.0
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
        price = targets[side]
        if opp > 0:
            worst = max(l["price"]+l["fee_per_unit"] for l in m["lots"][other])
            price = min(book[side+"_bid"], p.pair_cost_cap-worst)
        price = floor_price(price)
        while price > 0 and opp > 0 and (price+fee_usd(price, p.clip, p.maker_coefficient)/p.clip+worst > p.pair_cost_cap+1e-9):
            price = floor_price(price-.001)
        cap = p.max_net if residual else p.clip
        quantity = min(p.clip, max(0., cap-own+opp),
                       max(0., p.max_gross_per_market-m["turnover"]-_unread_order_risk(m, other, now)))
        if own > 0:
            # Extra inventory only on the local model's side; do not enforce
            # a hindsight ratio or pretend its excess has been hedged.
            favored = "yes" if fair > .5 else "no"
            allowed = p.residual_ratio_cap*max(p.clip, m["paired_quantity"])
            allowance = min(p.max_net, max(0., allowed-m["paired_quantity"]))
            if not residual or side != favored:
                quantity = 0.
            else:
                quantity = min(quantity, max(0., allowance-own))
        if m["stop_new"]:
            quantity = min(quantity, opp)  # keep only risk-reducing pair quotes
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
