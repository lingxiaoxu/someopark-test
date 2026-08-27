"""Edge computation and trade gating (plan 04 §2, §8).

    gross_edge = p - a
    net_edge   = p - a - fee(a) - slippage
    p_eff      = p - k * sigma_p        (conservative shrink by posterior std)
    trade only if net_edge(using p_eff) >= theta

Fees: Kalshi has a documented fee schedule with rounding; Polymarket US fees
must be pulled from the official rate card at go-live (plan 04 §2, 07 §1.6).
Until those are wired, callers pass an explicit ``fee`` — there is deliberately
NO hard-coded fee guess (a wrong fee makes net_edge fictitious).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EdgeResult:
    p_model: float
    p_eff: float            # shrunk model probability
    ask: float              # tradable ask (implied prob)
    gross_edge: float
    net_edge: float         # after fee + slippage, using p_eff
    fee: float
    slippage: float
    theta: float
    tradable: bool

    def __str__(self) -> str:
        flag = "TRADE" if self.tradable else "skip"
        return (f"[{flag}] p={self.p_model:.3f} p_eff={self.p_eff:.3f} ask={self.ask:.3f} "
                f"net_edge={self.net_edge:+.3f} (theta={self.theta:.3f})")


def shrink(p_model: float, sigma_p: float, k: float) -> float:
    """Conservative shrink: p_eff = p - k*sigma, clipped to [0, 1]."""
    return max(0.0, min(1.0, p_model - k * sigma_p))


def compute_edge(
    p_model: float,
    ask: float,
    *,
    sigma_p: float = 0.0,
    k: float = 1.0,
    fee: float = 0.0,
    slippage: float = 0.0,
    theta: float = 0.03,
) -> EdgeResult:
    """Edge for buying YES at ``ask`` when the model says ``p_model``.

    To evaluate the NO side, pass p_model=1-p_yes and ask=no_ask. The caller
    picks whichever side (or neither) clears theta.
    """
    p_eff = shrink(p_model, sigma_p, k)
    gross = p_model - ask
    net = p_eff - ask - fee - slippage
    return EdgeResult(
        p_model=p_model, p_eff=p_eff, ask=ask,
        gross_edge=gross, net_edge=net, fee=fee, slippage=slippage,
        theta=theta, tradable=net >= theta,
    )
