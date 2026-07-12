"""Single-instrument sizing (Plan 00 §5 `sizing.py`).

Chain (each step can only SHRINK the size):
    vol-target notional → fractional-Kelly cap → hard leverage ceiling →
    integer-contract rounding (fractional_trading_enabled=false, probe) →
    min-size / cost-refusal gates.

At $500–1500 the rounding and refusal gates dominate — they are explicit
outputs, not silent clamps: ``SizeDecision`` says what bound.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SizingConfig:
    target_vol_annual: float = 0.40     # sleeve vol target (365-day basis)
    kelly_fraction: float = 0.25        # fraction of full Kelly
    leverage_max: float = 2.0           # hard ceiling (Plan 00: 1–2×)
    min_contracts: int = 1
    max_contracts: int | None = None    # optional per-strategy hard cap


@dataclass(frozen=True)
class SizeDecision:
    contracts: int                      # 0 ⇒ refused
    notional: float
    leverage: float
    binding: str                        # which constraint bound the size
    reasons: list[str] = field(default_factory=list)


def kelly_cap_notional(equity: float, edge_per_notional: float,
                       vol_per_notional: float, kelly_fraction: float) -> float:
    """Fractional-Kelly notional cap: f* = edge/var, scaled, floored at 0."""
    if vol_per_notional <= 0:
        return 0.0
    f_star = edge_per_notional / (vol_per_notional ** 2)
    return max(0.0, kelly_fraction * f_star * equity)


def size_position(*, equity: float, contract_price: float,
                  realized_vol_annual: float, expected_edge_dollars: float | None = None,
                  round_trip_cost_dollars: float | None = None,
                  edge_per_notional: float | None = None,
                  cfg: SizingConfig = SizingConfig()) -> SizeDecision:
    """Full sizing chain → integer contracts (0 = refuse, with reasons).

    ``expected_edge_dollars`` + ``round_trip_cost_dollars`` power the refusal
    gate at the FINAL rounded size (Plan 01 §7: refuse signals whose edge <
    round-trip cost at the smallest tradable count).
    """
    reasons: list[str] = []
    if equity <= 0 or contract_price <= 0:
        return SizeDecision(0, 0.0, 0.0, "invalid-inputs", ["equity/price <= 0"])

    # 1. vol target
    if realized_vol_annual and realized_vol_annual > 0:
        vt_scalar = cfg.target_vol_annual / realized_vol_annual
    else:
        vt_scalar = 1.0
        reasons.append("no realized vol — vol-target skipped")
    notional = equity * vt_scalar
    binding = "vol-target"

    # 2. fractional-Kelly cap (optional — needs an edge estimate)
    if edge_per_notional is not None and realized_vol_annual:
        kc = kelly_cap_notional(equity, edge_per_notional, realized_vol_annual,
                                cfg.kelly_fraction)
        if kc < notional:
            notional, binding = kc, "kelly"

    # 3. hard leverage ceiling
    lev_cap = cfg.leverage_max * equity
    if lev_cap < notional:
        notional, binding = lev_cap, "leverage"

    # 4. integer contracts
    contracts = math.floor(notional / contract_price)
    if cfg.max_contracts is not None:
        if contracts > cfg.max_contracts:
            contracts, binding = cfg.max_contracts, "max-contracts"
    if contracts < cfg.min_contracts:
        reasons.append(f"size {contracts} < min {cfg.min_contracts} contracts")
        return SizeDecision(0, 0.0, 0.0, "min-size", reasons)

    # 5. cost-refusal gate at the actual rounded size
    if expected_edge_dollars is not None and round_trip_cost_dollars is not None:
        if expected_edge_dollars <= round_trip_cost_dollars:
            reasons.append(
                f"edge ${expected_edge_dollars:.4f} <= round-trip cost "
                f"${round_trip_cost_dollars:.4f} — refused")
            return SizeDecision(0, 0.0, 0.0, "cost-gate", reasons)

    final_notional = contracts * contract_price
    return SizeDecision(contracts, final_notional, final_notional / equity,
                        binding, reasons)
