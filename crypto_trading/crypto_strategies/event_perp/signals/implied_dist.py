"""Plan 02 `signals/implied_dist.py`: strike strip → risk-neutral distribution
+ static no-arbitrage violation detector (the plan's highest-confidence signal).

Inputs are captured strip snapshots (strips.py recorder): event markets with
``strike_type='greater'``, ``floor_strike=K``, ``yes_{bid,ask}_dollars`` ∈ [0,1].
YES(K) pays 1 iff S ≥ K at settlement, so its price IS the survival function
P(S ≥ K). Therefore:

  * Monotonicity: K↑ ⇒ P(S≥K)↓. A TRADEABLE violation is bid(K_hi) > ask(K_lo)
    for K_hi > K_lo: buy YES(K_lo) at ask, sell YES(K_hi) at bid → payoff
    1{K_lo ≤ S < K_hi} ≥ 0 for a NET CREDIT. (Thin books produce these;
    Plan 02 §2c.)
  * CDF: F(K) = 1 − P(S≥K); PDF via adjacent-strike differencing after
    isotonic (PAV) regularisation of the survival mids.
  * Moments: mean/var/skew of the discretised PDF; tail mass beyond the
    quoted strikes is assigned to the boundary strikes ± half the median
    strike gap (documented approximation — fine for dislocation z-scores,
    not for absolute pricing).

Kalshi event fee ≈ 0.07·P·(1−P) per contract (rounded up per contract in
cents) — the violation detector nets a configurable per-leg fee buffer so
"violations" are only flagged when they survive costs.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrikeQuote:
    strike: float
    yes_bid: float
    yes_ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0

    @property
    def mid(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2.0

    @property
    def spread(self) -> float:
        return self.yes_ask - self.yes_bid

    @property
    def quoted(self) -> bool:
        """False for the empty-book default (bid=0, ask=1) — uninformative."""
        return not (self.yes_bid <= 0.0 and self.yes_ask >= 1.0)


def parse_strip(markets: list[dict]) -> list[StrikeQuote]:
    """Raw captured market dicts → sorted SURVIVAL quotes P(S ≥ K).

    Uses both threshold flavours (captured strips carry greater/less/between):
      * ``greater`` at K: YES = 1{S ≥ K} → survival directly.
      * ``less`` at K:    YES = 1{S < K} → survival = 1 − p, so
        surv_bid = 1 − yes_ask,  surv_ask = 1 − yes_bid (sides flip).
      * ``between`` (range bins) are direct PDF quotes — not folded into the
        survival curve here (future refinement; violations across
        range↔threshold combos need their own detector).
    Same-strike duplicates merge to the TIGHTEST market (max bid, min ask);
    a crossed merge (bid>ask across the two flavours) is itself an arb — v1
    clamps to the crossing point and leaves pair-detection to find_violations.
    """
    by_strike: dict[float, StrikeQuote] = {}
    for m in markets:
        st = m.get("strike_type")
        try:
            if st == "greater" and m.get("floor_strike") is not None:
                k = float(m["floor_strike"])
                bid = float(m.get("yes_bid_dollars") or 0.0)
                ask = float(m.get("yes_ask_dollars") or 1.0)
                bsz = float(m.get("yes_bid_size_fp") or 0.0)
                asz = float(m.get("yes_ask_size_fp") or 0.0)
            elif st == "less" and (m.get("cap_strike") is not None
                                   or m.get("floor_strike") is not None):
                k = float(m.get("cap_strike") or m.get("floor_strike"))
                y_bid = float(m.get("yes_bid_dollars") or 0.0)
                y_ask = float(m.get("yes_ask_dollars") or 1.0)
                bid, ask = 1.0 - y_ask, 1.0 - y_bid          # sides flip
                bsz = float(m.get("yes_ask_size_fp") or 0.0)  # sizes flip too
                asz = float(m.get("yes_bid_size_fp") or 0.0)
            else:
                continue
        except (TypeError, ValueError):
            continue
        q = StrikeQuote(k, bid, ask, bsz, asz)
        if not q.quoted:
            continue
        prev = by_strike.get(k)
        if prev is not None:
            bid = max(prev.yes_bid, q.yes_bid)
            ask = min(prev.yes_ask, q.yes_ask)
            if bid > ask:                                    # crossed across flavours
                bid = ask = (bid + ask) / 2
            q = StrikeQuote(k, bid, ask, max(prev.bid_size, q.bid_size),
                            max(prev.ask_size, q.ask_size))
        by_strike[k] = q
    return [by_strike[k] for k in sorted(by_strike)]


def event_fee(price: float, rate: float = 0.07) -> float:
    """Kalshi event trading fee per contract ≈ rate·P·(1−P) (cent-rounded up)."""
    return np.ceil(rate * price * (1 - price) * 100) / 100 if 0 < price < 1 else 0.0


@dataclass(frozen=True)
class Violation:
    k_lo: float
    k_hi: float
    ask_lo: float               # you pay this for YES(k_lo)
    bid_hi: float               # you receive this for YES(k_hi)
    gross_credit: float         # bid_hi − ask_lo (> 0 = free money before fees)
    net_credit: float           # after 2-leg fees
    size: float                 # min tradable size across the two legs


def find_violations(quotes: list[StrikeQuote], *, fee_rate: float = 0.07,
                    min_net_credit: float = 0.0) -> list[Violation]:
    """All strike pairs where selling the HIGHER strike funds buying the LOWER
    one at a net credit — static arbitrage in a monotone-decreasing curve."""
    out = []
    for i, lo in enumerate(quotes):
        for hi in quotes[i + 1:]:
            gross = hi.yes_bid - lo.yes_ask
            if gross <= 0:
                continue
            net = gross - event_fee(lo.yes_ask, fee_rate) - event_fee(hi.yes_bid, fee_rate)
            if net > min_net_credit:
                out.append(Violation(lo.strike, hi.strike, lo.yes_ask, hi.yes_bid,
                                     gross, net, min(lo.ask_size, hi.bid_size)))
    out.sort(key=lambda v: -v.net_credit)
    return out


def pav_decreasing(y: np.ndarray) -> np.ndarray:
    """Pool-adjacent-violators: least-squares DECREASING fit (isotonic on −y)."""
    y = np.asarray(y, dtype=float)
    n = len(y)
    vals = list(y)
    wts = [1.0] * n
    i = 0
    while i < len(vals) - 1:
        if vals[i] < vals[i + 1] - 1e-15:          # violates decreasing
            merged = (vals[i] * wts[i] + vals[i + 1] * wts[i + 1]) / (wts[i] + wts[i + 1])
            vals[i:i + 2] = [merged]
            wts[i:i + 2] = [wts[i] + wts[i + 1]]
            i = max(i - 1, 0)
        else:
            i += 1
    out = []
    for v, w in zip(vals, wts):
        out.extend([v] * int(w))
    return np.clip(np.array(out), 0.0, 1.0)


@dataclass(frozen=True)
class BinQuote:
    """One 'between' range market: YES pays 1 iff k_lo ≤ S ≤ k_hi (a PDF bin).

    Captured reality (2026-07-07): Kalshi BTC/ETH strips quote almost ONLY the
    range bins (186 of 188 quoted markets on the nearest horizon) — the bins,
    not the thresholds, are the primary distribution input.
    """
    k_lo: float
    k_hi: float
    yes_bid: float
    yes_ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0

    @property
    def mid(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2.0

    @property
    def center(self) -> float:
        return (self.k_lo + self.k_hi) / 2.0

    @property
    def quoted(self) -> bool:
        return not (self.yes_bid <= 0.0 and self.yes_ask >= 1.0)


def parse_bins(markets: list[dict]) -> list[BinQuote]:
    """'between' markets → sorted quoted PDF bins."""
    out = []
    for m in markets:
        if m.get("strike_type") != "between":
            continue
        if m.get("floor_strike") is None or m.get("cap_strike") is None:
            continue
        try:
            q = BinQuote(float(m["floor_strike"]), float(m["cap_strike"]),
                         float(m.get("yes_bid_dollars") or 0.0),
                         float(m.get("yes_ask_dollars") or 1.0),
                         float(m.get("yes_bid_size_fp") or 0.0),
                         float(m.get("yes_ask_size_fp") or 0.0))
        except (TypeError, ValueError):
            continue
        if q.quoted:
            out.append(q)
    out.sort(key=lambda q: q.k_lo)
    return out


@dataclass(frozen=True)
class TileArb:
    """Full-tile sum arbitrage over a CONTIGUOUS bin tile (+ optional tails).

    A complete partition of outcomes must price to 1. If Σ ask < 1 − fees you
    buy every leg for a guaranteed net credit at settlement; if Σ bid > 1 +
    fees you sell every leg. ``coverage_complete`` is False when the tile has
    gaps or missing tails — then the bound is one-sided only (documented, not
    tradeable as pure arb).
    """
    n_legs: int
    sum_ask: float
    sum_bid: float
    buy_credit_net: float       # 1 − Σask − fees  (> 0 ⇒ buy-the-tile arb)
    sell_credit_net: float      # Σbid − 1 − fees  (> 0 ⇒ sell-the-tile arb)
    coverage_complete: bool
    min_leg_size: float


def tile_arb(bins: list[BinQuote], *, tail_lo: StrikeQuote | None = None,
             tail_hi: StrikeQuote | None = None, fee_rate: float = 0.07,
             gap_tol: float = 1e-6) -> TileArb | None:
    """Evaluate the sum-to-one arbitrage over the contiguous quoted tile."""
    if not bins:
        return None
    complete = True
    for a, b in zip(bins, bins[1:]):
        if b.k_lo - a.k_hi > gap_tol + max(1.0, abs(a.k_hi)) * 1e-9:
            complete = False
    legs_ask = [b.yes_ask for b in bins]
    legs_bid = [b.yes_bid for b in bins]
    sizes = [min(b.ask_size, b.bid_size) or max(b.ask_size, b.bid_size) for b in bins]
    # tails: P(S < first k_lo) and P(S > last k_hi) via threshold quotes
    if tail_lo is not None:      # tail_lo is a survival quote at bins[0].k_lo
        legs_ask.append(1.0 - tail_lo.yes_bid)    # buy "below" = sell survival
        legs_bid.append(1.0 - tail_lo.yes_ask)
        sizes.append(tail_lo.bid_size)
    else:
        complete = False
    if tail_hi is not None:      # survival quote at bins[-1].k_hi = "above" leg
        legs_ask.append(tail_hi.yes_ask)
        legs_bid.append(tail_hi.yes_bid)
        sizes.append(tail_hi.ask_size)
    else:
        complete = False
    fees_ask = sum(event_fee(p, fee_rate) for p in legs_ask)
    fees_bid = sum(event_fee(p, fee_rate) for p in legs_bid)
    sum_ask, sum_bid = sum(legs_ask), sum(legs_bid)
    return TileArb(len(legs_ask), sum_ask, sum_bid,
                   buy_credit_net=1.0 - sum_ask - fees_ask,
                   sell_credit_net=sum_bid - 1.0 - fees_bid,
                   coverage_complete=complete,
                   min_leg_size=min(sizes) if sizes else 0.0)


def implied_distribution_from_bins(bins: list[BinQuote], *,
                                   tail_lo: StrikeQuote | None = None,
                                   tail_hi: StrikeQuote | None = None,
                                   min_bins: int = 4) -> "ImpliedDist | None":
    """Direct PDF from range-bin mids (+ tail masses), renormalised to 1."""
    if len(bins) < min_bins:
        return None
    nodes = [b.center for b in bins]
    masses = [max(0.0, b.mid) for b in bins]
    gap = float(np.median([b.k_hi - b.k_lo for b in bins]))
    if tail_lo is not None:
        nodes.append(bins[0].k_lo - gap / 2)
        masses.append(max(0.0, 1.0 - tail_lo.mid))     # P(S < K) = 1 − survival mid
    if tail_hi is not None:
        nodes.append(bins[-1].k_hi + gap / 2)
        masses.append(max(0.0, tail_hi.mid))
    mass = np.array(masses)
    total = mass.sum()
    if total <= 0:
        return None
    order = np.argsort(nodes)
    nodes_arr = np.array(nodes)[order]
    mass = (mass / total)[order]
    strikes = np.array([b.k_lo for b in bins] + [bins[-1].k_hi])
    surv = np.clip(1.0 - np.cumsum(mass)[: len(strikes)], 0.0, 1.0)
    return ImpliedDist(strikes, surv, mass, nodes_arr)


@dataclass
class ImpliedDist:
    strikes: np.ndarray
    survival: np.ndarray        # regularised P(S ≥ K)
    pdf_mass: np.ndarray        # probability mass per node (sums to 1)
    nodes: np.ndarray           # mass locations (strike midpoints + tails)
    mean: float = field(init=False)
    sd: float = field(init=False)
    skew: float = field(init=False)

    def __post_init__(self):
        m = float(np.sum(self.nodes * self.pdf_mass))
        var = float(np.sum((self.nodes - m) ** 2 * self.pdf_mass))
        sd = var ** 0.5
        self.mean = m
        self.sd = sd
        self.skew = (float(np.sum((self.nodes - m) ** 3 * self.pdf_mass)) / sd ** 3
                     if sd > 0 else 0.0)


def implied_distribution(quotes: list[StrikeQuote], *, min_strikes: int = 4) -> ImpliedDist | None:
    """Survival mids → PAV-regularised CDF → discrete PDF → moments."""
    if len(quotes) < min_strikes:
        return None
    strikes = np.array([q.strike for q in quotes])
    surv = pav_decreasing(np.array([q.mid for q in quotes]))
    gap = float(np.median(np.diff(strikes))) if len(strikes) > 1 else 0.0

    # interval masses: below first strike, between strikes, above last strike
    masses = [1.0 - surv[0]]
    nodes = [strikes[0] - gap / 2]
    for i in range(len(strikes) - 1):
        masses.append(surv[i] - surv[i + 1])
        nodes.append((strikes[i] + strikes[i + 1]) / 2)
    masses.append(surv[-1])
    nodes.append(strikes[-1] + gap / 2)

    mass = np.clip(np.array(masses), 0.0, None)
    total = mass.sum()
    if total <= 0:
        return None
    return ImpliedDist(strikes, surv, mass / total, np.array(nodes))


def _nearest_survival(quotes: list[StrikeQuote], k: float,
                      tol: float) -> StrikeQuote | None:
    best = min(quotes, key=lambda q: abs(q.strike - k), default=None)
    return best if best is not None and abs(best.strike - k) <= tol else None


def analyze_snapshot(record: dict, *, fee_rate: float = 0.07) -> dict:
    """One captured strips 'markets' line → dislocation/violation summary row.

    Primary path = range-bin distribution + full-tile sum arb (that's where
    the quotes live); threshold survival quotes contribute tails + the
    pairwise crossing detector when present.
    """
    markets = record.get("markets") or []
    surv_quotes = parse_strip(markets)
    bins = parse_bins(markets)
    row = {"recv_ts": record.get("recv_ts"), "close_time": record.get("close_time"),
           "spot_est": record.get("spot_est"), "n_markets": record.get("n_markets"),
           "n_bins": len(bins), "n_thresholds": len(surv_quotes)}

    tail_lo = tail_hi = None
    if bins:
        gap = float(np.median([b.k_hi - b.k_lo for b in bins]))
        tail_lo = _nearest_survival(surv_quotes, bins[0].k_lo, gap)
        tail_hi = _nearest_survival(surv_quotes, bins[-1].k_hi, gap)

    dist = (implied_distribution_from_bins(bins, tail_lo=tail_lo, tail_hi=tail_hi)
            or implied_distribution(surv_quotes))
    if dist is not None:
        row.update({"implied_mean": dist.mean, "implied_sd": dist.sd,
                    "implied_skew": dist.skew,
                    "mean_vs_spot_bps": (1e4 * (dist.mean - row["spot_est"]) / row["spot_est"]
                                         if row.get("spot_est") else None)})

    ta = tile_arb(bins, tail_lo=tail_lo, tail_hi=tail_hi, fee_rate=fee_rate)
    if ta is not None:
        row.update({"tile_sum_ask": ta.sum_ask, "tile_sum_bid": ta.sum_bid,
                    "tile_buy_credit_net": ta.buy_credit_net,
                    "tile_sell_credit_net": ta.sell_credit_net,
                    "tile_complete": ta.coverage_complete,
                    "tile_min_size": ta.min_leg_size})

    viols = find_violations(surv_quotes, fee_rate=fee_rate)
    row.update({"n_violations_net": len(viols),
                "max_net_credit": viols[0].net_credit if viols else 0.0,
                "max_credit_size": viols[0].size if viols else 0.0})
    return row


def scan_file(path: str, *, fee_rate: float = 0.07) -> list[dict]:
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(analyze_snapshot(json.loads(line), fee_rate=fee_rate))
            except Exception:
                logger.exception("bad snapshot line — skipped")
    return rows


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", help="captured strips markets jsonl file")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    import pandas as pd
    df = pd.DataFrame(scan_file(args.path))
    if df.empty:
        print("no snapshots")
        return 1
    cols = [c for c in ("n_bins", "implied_mean", "implied_sd", "mean_vs_spot_bps",
                        "tile_sum_ask", "tile_sum_bid", "tile_buy_credit_net",
                        "tile_sell_credit_net", "n_violations_net") if c in df.columns]
    print(df[cols].describe().to_string())
    print("\nlast rows:")
    show = [c for c in ("close_time", "n_bins", "implied_mean", "mean_vs_spot_bps",
                        "tile_buy_credit_net", "tile_complete") if c in df.columns]
    print(df.tail(5)[show].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
