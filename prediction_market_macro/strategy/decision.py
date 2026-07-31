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
    if (entropy_norm is not None
            and entropy_norm > gates.get("max_entropy_norm", 0.95)):
        return Decision("pass", None, 0.0, 0,
                        (f"entropy_gate:{entropy_norm:.3f}",), dict(gates))

    best: tuple[float, Struct] | None = None
    for st in structs:
        ne = st.net_edge()
        if ne < gates["min_net_edge"]:
            continue
        if any(l.depth < gates["min_leg_depth_usd"] for l in st.legs):
            reasons.append(f"depth_fail:{st.desc}")
            continue
        # sanity gate is UNCONDITIONAL: compare against the devigged market prob when
        # available (spread noise removed), else raw cost — a model that disagrees with
        # the market by >0.25 is presumed wrong, whatever the structure type
        mkt = (market_implied or {}).get(st.desc, st.cost)
        gap = abs(st.fair - mkt)
        if gap > gates["max_model_market_gap"]:
            reasons.append(f"sanity_gap:{st.desc}:{gap:.2f}")
            continue
        if best is None or ne > best[0]:
            best = (ne, st)
    if best is None:
        reasons.append("no_struct_cleared_gates")
        return Decision("pass", None, 0.0, 0, tuple(reasons), dict(gates))
    ne, st = best
    usd = quarter_kelly_usd(st.fair, st.cost, bankroll, cap=gates["max_size_usd"])
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
        return Decision("pass", None, 0.0, 0,
                        (f"kelly_below_one_contract {usd:.2f}<{st.cost:.2f}",),
                        dict(gates))
    count = int(usd / max(st.cost, 0.01))
    return Decision("open", st, usd, count,
                    (f"net_edge={ne:.4f}", f"fair={st.fair:.4f}", f"cost={st.cost:.4f}"),
                    dict(gates))
