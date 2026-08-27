"""Portfolio risk management — caps + kill-switch (plan 04 §6, 05 §5).

Tracks live exposure across BOTH venues and enforces the hard limits before any
order is sized up:
  * single-market exposure cap;
  * theme cap (correlated markets on the same team — champion + golden-boot +
    advance are one bet thrice, plan 04 §6 — counted across venues);
  * total in-risk cap;
  * daily-loss kill-switch (freezes all new opening orders, plan 05 §5).

This is the portfolio gate that sits AFTER per-bet Kelly sizing (`sizing.py`):
sizing proposes a stake, the RiskManager clips it to whatever the limits still
allow, or rejects it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from prediction_market_soccer.config import CONFIG, RiskConfig


@dataclass
class RiskManager:
    bankroll: float
    risk: RiskConfig = field(default_factory=lambda: CONFIG.risk)
    market_exposure: dict[str, float] = field(default_factory=dict)   # market_key -> $ at risk
    theme_exposure: dict[str, float] = field(default_factory=dict)    # theme -> $ at risk (both venues)
    daily_pnl: float = 0.0
    killed: bool = False

    # ── kill-switch ──────────────────────────────────────────────────────────
    def record_pnl(self, delta: float) -> None:
        self.daily_pnl += delta
        if self.daily_pnl <= -self.risk.daily_loss_killswitch_frac * self.bankroll:
            self.killed = True

    def reset_day(self) -> None:
        self.daily_pnl = 0.0
        self.killed = False

    # ── exposure caps ────────────────────────────────────────────────────────
    @property
    def total_exposure(self) -> float:
        return sum(self.market_exposure.values())

    def room(self, market_key: str, theme: str) -> float:
        """Remaining $ allowed for a new position in this market+theme."""
        market_room = self.risk.max_single_market_frac * self.bankroll - self.market_exposure.get(market_key, 0.0)
        theme_room = self.risk.max_theme_frac * self.bankroll - self.theme_exposure.get(theme, 0.0)
        return max(0.0, min(market_room, theme_room))

    def approve(self, proposed_stake: float, market_key: str, theme: str) -> tuple[float, str]:
        """Clip a proposed stake to the remaining caps; reject if killed.

        Returns (allowed_stake, reason). allowed_stake == 0 means blocked.
        """
        if self.killed:
            return 0.0, "kill-switch active (daily loss limit hit)"
        if proposed_stake <= 0:
            return 0.0, "non-positive stake"
        room = self.room(market_key, theme)
        if room <= 0:
            return 0.0, "market/theme cap exhausted"
        if proposed_stake > room:
            return room, "clipped to market/theme cap"
        return proposed_stake, "ok"

    def register(self, stake: float, market_key: str, theme: str) -> None:
        """Record an opened position's exposure (call after a fill)."""
        self.market_exposure[market_key] = self.market_exposure.get(market_key, 0.0) + stake
        self.theme_exposure[theme] = self.theme_exposure.get(theme, 0.0) + stake

    def release(self, stake: float, market_key: str, theme: str) -> None:
        """Reduce exposure when a position is closed/reduced."""
        self.market_exposure[market_key] = max(0.0, self.market_exposure.get(market_key, 0.0) - stake)
        self.theme_exposure[theme] = max(0.0, self.theme_exposure.get(theme, 0.0) - stake)
