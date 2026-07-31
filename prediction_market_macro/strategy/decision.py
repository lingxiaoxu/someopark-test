"""strategy/decision.py — gates + the single decide() entry (PLAN §11; mother-template
discipline: production, backtest and reports ALL call this one pure function).

Gates (all configurable, PLAN §11 + §19-4):
  net_edge >= 0.04 ; per-leg depth >= $50 ; |fair - market_devig| <= 0.25 (sanity,
  against the DEVIGGED market prob when available, raw cost otherwise);
  entropy gate: normalized pmf entropy > 0.95 ⇒ the model claims no information ⇒ PASS;
  > 30 min to close ; no re-entry on the same (series, period);
  freeze window: no decisions within 10 min before the scheduled release;
  size <= 20% of the thinnest leg's depth (铁律 5 — never eat the book).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from prediction_market_macro.strategy.edge import Struct, quarter_kelly_usd

GATES = {
    "min_net_edge": 0.04,
    "min_leg_depth_usd": 50.0,
    "max_model_market_gap": 0.25,
    "min_minutes_to_close": 30.0,
    "freeze_minutes_before_release": 10.0,
    "max_size_usd": 1.0,
    "max_depth_frac": 0.20,          # 铁律 5: size ≤ 20% of thinnest-leg depth
    "max_entropy_norm": 0.95,        # §19-4: flat pmf ⇒ no informational edge ⇒ PASS
    # §24-D favorite-compounder second entry path: small absolute edges on
    # near-certain short-dated legs, admitted by ANNUALIZED yield instead
    "fav_min_fair": 0.90,
    "fav_min_edge_per_day": 0.008,   # net edge per day to settlement
    "fav_min_net_edge": 0.005,       # still must clear fees with margin
    "fav_size_frac": 0.5,            # half the normal size cap
    # entry-timing gate (30d daily WF diagnostic, 2026-07-31): every model anchors
    # on the latest print — beyond one weekly refresh cycle it extrapolates blind
    # while the market keeps absorbing information (>7d entries went 0/4). A
    # PRIOR-justified structural cap, not a tuned threshold; live-forward judges.
    "max_days_to_close": 7.0,
    # penny-lottery floor: at p<0.10 the ceil'd taker fee alone is 10-25% of the
    # price, and favorite-longshot bias (our own FL map measures it) says cheap
    # tails are systematically OVERpriced — fat model tails "clear" the edge gate
    # on fiction. Fee geometry + documented bias ⇒ structural, not tuned.
    "min_leg_price": 0.10,
}


@dataclass(frozen=True)
class Decision:
    action: str                 # "open" | "pass"
    struct: Struct | None
    size_usd: float
    count: int
    reasons: tuple[str, ...]
    gate_snapshot: dict


def decide(structs: list[Struct], *, now: datetime, close_time: datetime | None,
           release_ts: datetime | None, market_implied: dict[str, float] | None,
           already_open: bool, bankroll: float, gates: dict = GATES,
           entropy_norm: float | None = None) -> Decision:
    """market_implied: {struct.desc: devigged market prob} — the sanity gate compares
    fair against the DEVIGGED mid when present (PLAN §11), raw cost otherwise.
    entropy_norm: H(pmf)/log(K) of the model distribution — flat ⇒ PASS (§19-4)."""
    reasons: list[str] = []
    if already_open:
        return Decision("pass", None, 0.0, 0, ("already_open_no_averaging_down",), dict(gates))
    if release_ts is not None:
        dt_min = (release_ts - now).total_seconds() / 60.0
        if 0 <= dt_min <= gates["freeze_minutes_before_release"]:
            return Decision("pass", None, 0.0, 0, ("freeze_window",), dict(gates))
    if close_time is not None:
        if (close_time - now).total_seconds() / 60.0 < gates["min_minutes_to_close"]:
            return Decision("pass", None, 0.0, 0, ("too_close_to_close",), dict(gates))
        dtc = (close_time - now).total_seconds() / 86400.0
        if dtc > gates.get("max_days_to_close", 7.0):
            return Decision("pass", None, 0.0, 0,
                            (f"too_far_from_close {dtc:.1f}d",), dict(gates))
    if (entropy_norm is not None
            and entropy_norm > gates.get("max_entropy_norm", 0.95)):
        return Decision("pass", None, 0.0, 0,
                        (f"entropy_gate:{entropy_norm:.3f}",), dict(gates))

    days_to_close = (max((close_time - now).total_seconds() / 86400.0, 0.5)
                     if close_time is not None else None)
    best: tuple[float, Struct] | None = None
    fav_best: tuple[float, Struct] | None = None      # (edge_per_day, struct)
    for st in structs:
        ne = st.net_edge()
        if any(l.depth < gates["min_leg_depth_usd"] for l in st.legs):
            reasons.append(f"depth_fail:{st.desc}")
            continue
        if any(l.price < gates.get("min_leg_price", 0.10) for l in st.legs):
            reasons.append(f"penny_leg:{st.desc}")
            continue
        # sanity gate is UNCONDITIONAL: compare against the devigged market prob when
        # available (spread noise removed), else raw cost — a model that disagrees with
        # the market by >0.25 is presumed wrong, whatever the structure type
        mkt = (market_implied or {}).get(st.desc, st.cost)
        gap = abs(st.fair - mkt)
        if gap > gates["max_model_market_gap"]:
            reasons.append(f"sanity_gap:{st.desc}:{gap:.2f}")
            continue
        if ne >= gates["min_net_edge"]:
            if best is None or ne > best[0]:
                best = (ne, st)
        elif (days_to_close is not None
              and st.fair >= gates.get("fav_min_fair", 0.90)
              and ne >= gates.get("fav_min_net_edge", 0.005)):
            # §24-D: small edge, near-certain, short-dated — annualized lens
            epd = ne / days_to_close
            if epd >= gates.get("fav_min_edge_per_day", 0.008):
                if fav_best is None or epd > fav_best[0]:
                    fav_best = (epd, st)
    fav_path = False
    if best is None and fav_best is not None:
        best = (fav_best[1].net_edge(), fav_best[1])
        fav_path = True
        reasons.append(f"favorite_path epd={fav_best[0]:.4f}")
    if best is None:
        reasons.append("no_struct_cleared_gates")
        return Decision("pass", None, 0.0, 0, tuple(reasons), dict(gates))
    ne, st = best
    size_cap = gates["max_size_usd"] * (gates.get("fav_size_frac", 0.5)
                                        if fav_path else 1.0)
    usd = quarter_kelly_usd(st.fair, st.cost, bankroll, cap=size_cap)
    if usd <= 0.0:
        return Decision("pass", None, 0.0, 0, ("kelly_zero",), dict(gates))
    # 铁律 5 first half: never take more than max_depth_frac of the thinnest leg
    depth_cap = gates.get("max_depth_frac", 0.20) * min(l.depth for l in st.legs)
    if depth_cap < st.cost:                      # can't even fit one contract
        return Decision("pass", None, 0.0, 0,
                        (f"depth_cap {depth_cap:.2f}<cost {st.cost:.2f}",), dict(gates))
    if usd > depth_cap:
        usd = round(depth_cap, 2)
    if usd < st.cost:                            # Kelly sized below one contract
        if fav_path and depth_cap >= st.cost:
            usd = st.cost                        # favorites cost ~0.9+: buy exactly 1
        else:
            return Decision("pass", None, 0.0, 0,
                            (f"kelly_below_one_contract {usd:.2f}<{st.cost:.2f}",),
                            dict(gates))
    count = int(usd / max(st.cost, 0.01))
    open_reasons = [f"net_edge={ne:.4f}", f"fair={st.fair:.4f}", f"cost={st.cost:.4f}"]
    if fav_path:
        open_reasons.insert(0, "favorite_path")
    return Decision("open", st, usd, count, tuple(open_reasons), dict(gates))
