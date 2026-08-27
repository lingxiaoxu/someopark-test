"""Position sizing — fractional Kelly with hard caps (plan 04 §6).

Single bet: buy YES @ a with model prob p,
    f* = (p - a) / (1 - a)              (full-Kelly fraction of bankroll)
    stake = bankroll * kelly_fraction * f*

Then clip by the hard limits (single-market, theme, depth). Portfolio-level
correlation (champion + golden-boot + advance on the same team are one bet
thrice, plan 04 §6) is approximated here by a per-theme exposure cap; the full
joint-covariance optimisation is a later upgrade.
"""
from __future__ import annotations

from dataclasses import dataclass

from prediction_market_soccer.config import CONFIG, RiskConfig


@dataclass(frozen=True)
class SizeResult:
    kelly_full: float          # f* (uncapped Kelly fraction)
    kelly_scaled: float        # after kelly_fraction multiplier
    stake: float               # final $ stake after all caps
    capped_by: str             # which limit bound the stake ("kelly"/"market"/"theme"/"depth")


def kelly_fraction(p: float, a: float) -> float:
    """Full-Kelly fraction f* = (p-a)/(1-a) for a binary YES @ a. >=0 only."""
    if a >= 1.0 or a <= 0.0:
        return 0.0
    return max(0.0, (p - a) / (1.0 - a))


def size_position(
    p_eff: float,
    ask: float,
    bankroll: float,
    *,
    theme_room: float | None = None,
    depth_cap: float | None = None,
    risk: RiskConfig | None = None,
) -> SizeResult:
    """Compute a capped stake for one YES position.

    ``theme_room``: remaining $ allowed in this correlated theme (already
    netted of existing exposure). ``depth_cap``: max $ executable without
    pushing through the ask (a fraction of book depth). Both optional.
    """
    risk = risk or CONFIG.risk
    f_full = kelly_fraction(p_eff, ask)
    f_scaled = f_full * risk.kelly_fraction
    stake = bankroll * f_scaled
    capped_by = "kelly"

    market_cap = bankroll * risk.max_single_market_frac
    if stake > market_cap:
        stake, capped_by = market_cap, "market"
    if theme_room is not None and stake > theme_room:
        stake, capped_by = max(0.0, theme_room), "theme"
    if depth_cap is not None and stake > depth_cap:
        stake, capped_by = depth_cap, "depth"

    return SizeResult(kelly_full=f_full, kelly_scaled=f_scaled, stake=stake, capped_by=capped_by)
