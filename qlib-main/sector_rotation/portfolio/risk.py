"""
Risk Management
===============
Position-level and portfolio-level risk controls for the sector rotation strategy.

Controls implemented:
    1. Volatility scaling    — reduce exposure when realized vol exceeds target
    2. VIX emergency de-risk — move to 50% cash when VIX > threshold
    3. Drawdown circuit breaker — halve position when cumulative DD > -15%
    4. Beta constraint       — keep portfolio beta within 0.85–1.15 vs SPY
    5. Concentration check   — enforce single-sector max weight

All risk checks are applied AFTER initial weight optimization.
Returns a (scaled_weights, cash_pct, risk_flags) tuple.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Risk flag dataclass
# ---------------------------------------------------------------------------

@dataclass
class RiskFlags:
    """Record which risk controls were triggered at a given rebalance date."""
    date: pd.Timestamp
    vol_scaling_triggered: bool = False
    vix_emergency_triggered: bool = False
    dd_circuit_triggered: bool = False
    beta_adjusted: bool = False
    realized_vol_annual: float = float("nan")
    historical_vol_annual: float = float("nan")
    current_vix: float = float("nan")
    portfolio_beta: float = float("nan")
    current_dd_pct: float = float("nan")
    cash_pct: float = 0.0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "notes"}
        d["notes"] = "; ".join(self.notes)
        return d


# ---------------------------------------------------------------------------
# Volatility scaling
# ---------------------------------------------------------------------------

def compute_realized_vol(
    returns: pd.Series,
    window: int = 20,
) -> float:
    """
    Compute annualized 20-day realized volatility from daily returns.

    Parameters
    ----------
    returns : pd.Series
        Daily portfolio returns (simple).
    window : int
        Rolling window in trading days.

    Returns
    -------
    float
        Annualized volatility. NaN if insufficient data.
    """
    if len(returns) < window:
        return float("nan")
    recent = returns.iloc[-window:]
    return float(recent.std() * np.sqrt(252))


def compute_historical_vol(
    returns: pd.Series,
    window: int = 252,
) -> float:
    """
    Compute annualized historical (long-run average) volatility.

    Parameters
    ----------
    returns : pd.Series
        Daily portfolio returns.
    window : int
        Long-run window.

    Returns
    -------
    float
        Annualized vol. NaN if insufficient data.
    """
    if len(returns) < window // 2:
        return float("nan")
    hist = returns.iloc[-window:]
    return float(hist.std() * np.sqrt(252))


def vol_scaling_factor(
    realized_vol: float,
    historical_vol: float,
    target_vol: float = 0.12,
    scale_threshold: float = 1.5,
) -> float:
    """
    Compute the volatility scaling factor for position sizing.

    If realized_vol > scale_threshold * historical_vol, scale down.
    The scaling factor is min(target_vol / realized_vol, 1.0).

    Parameters
    ----------
    realized_vol : float
        Current 20-day realized annualized vol.
    historical_vol : float
        Long-run average annualized vol.
    target_vol : float
        Target portfolio annualized vol (default 12%).
    scale_threshold : float
        Only trigger scaling when realized_vol > threshold * historical_vol.

    Returns
    -------
    float
        Scaling factor in [0, 1.0]. 1.0 = no scaling.
    """
    if np.isnan(realized_vol) or np.isnan(historical_vol):
        return 1.0

    if realized_vol > scale_threshold * historical_vol:
        factor = min(target_vol / realized_vol, 1.0)
        logger.info(
            f"Vol scaling triggered: realized={realized_vol:.2%} > "
            f"{scale_threshold}x historical={historical_vol:.2%}. "
            f"Scale factor: {factor:.3f}"
        )
        return factor
    return 1.0


# ---------------------------------------------------------------------------
# Portfolio beta estimation
# ---------------------------------------------------------------------------

def estimate_sector_betas(
    sector_returns: pd.DataFrame,
    benchmark_returns: pd.Series,
    window: int = 252,
) -> pd.Series:
    """
    Estimate beta for each sector vs benchmark using OLS over ``window`` days.

    Returns pd.Series {ticker: beta}.
    """
    betas = {}
    for col in sector_returns.columns:
        sec_ret = sector_returns[col].iloc[-window:].dropna()
        bench_ret = benchmark_returns.iloc[-window:].dropna()
        aligned = pd.concat([sec_ret, bench_ret], axis=1).dropna()
        if len(aligned) < 20:
            betas[col] = 1.0  # Default beta = 1 if insufficient data
            continue
        x = aligned.iloc[:, 1].values
        y = aligned.iloc[:, 0].values
        cov_xy = np.cov(x, y)[0, 1]
        var_x = np.var(x)
        betas[col] = cov_xy / var_x if var_x > 1e-10 else 1.0

    return pd.Series(betas)


# ---------------------------------------------------------------------------
# Main risk management pipeline
# ---------------------------------------------------------------------------

def apply_risk_controls(
    weights: pd.Series,
    portfolio_returns: pd.Series,
    macro: pd.DataFrame,
    sector_returns: Optional[pd.DataFrame] = None,
    benchmark_returns: Optional[pd.Series] = None,
    equity_curve: Optional[pd.Series] = None,
    # Thresholds
    vol_target: float = 0.12,
    vol_estimation_window: int = 20,
    vol_historical_window: int = 252,
    vol_scale_threshold: float = 1.5,
    vol_scaling_enabled: bool = True,
    vix_emergency_threshold: float = 35.0,
    emergency_cash_pct: float = 0.50,
    dd_halve_threshold: float = -0.15,
    dd_recovery_threshold: float = -0.10,
    beta_min: float = 0.85,
    beta_max: float = 1.15,
    max_weight: float = 0.40,
    vix_progressive_tiers: Optional[list] = None,
) -> Tuple[pd.Series, float, RiskFlags]:
    """
    Apply all risk controls and return adjusted weights + cash allocation.

    Parameters
    ----------
    weights : pd.Series
        Initial optimized weights (sum = 1.0, pre-risk).
    portfolio_returns : pd.Series
        Historical daily portfolio returns (for vol estimation).
    macro : pd.DataFrame
        Daily macro data (must contain 'vix' column).
    sector_returns : pd.DataFrame, optional
        Daily sector returns (for beta estimation).
    benchmark_returns : pd.Series, optional
        Daily benchmark returns (for beta estimation).
    equity_curve : pd.Series, optional
        Cumulative equity curve (for drawdown calculation).
    vol_target, vol_estimation_window, vol_historical_window, vol_scale_threshold:
        Volatility scaling parameters.
    vol_scaling_enabled : bool
        Enable/disable vol scaling.
    vix_emergency_threshold : float
        VIX level triggering emergency cash.
    emergency_cash_pct : float
        Cash allocation in emergency (default 50%).
    dd_halve_threshold : float
        Cumulative DD below this → halve position (default -15%).
    dd_recovery_threshold : float
        DD must recover to this before resuming full position (default -10%).
    beta_min, beta_max : float
        Acceptable portfolio beta range vs benchmark.
    max_weight : float
        Maximum single-sector weight.
    vix_progressive_tiers : list of dict, optional
        Graduated cash tiers below the emergency threshold.
        Each entry: {"vix_above": <float>, "cash_pct": <float>}.
        Applied only when VIX is below vix_emergency_threshold.
        Pass [] or None to disable (default behavior = emergency-only at VIX=35).

    Returns
    -------
    adjusted_weights : pd.Series
        Risk-adjusted weights (may sum < 1 if cash allocation > 0).
    cash_pct : float
        Allocated cash fraction [0, 1].
    flags : RiskFlags
        Triggered risk flags and diagnostic values.
    """
    from datetime import datetime
    date = macro.index[-1] if len(macro) > 0 else pd.Timestamp.now()
    flags = RiskFlags(date=date)

    adjusted_weights = weights.copy()
    cash_pct = 0.0

    # -------------------------------------------------------------------
    # 1. VIX emergency de-risk (highest priority)
    # -------------------------------------------------------------------
    current_vix = float("nan")
    if "vix" in macro.columns and len(macro) > 0:
        vix_series = macro["vix"].dropna()
        if len(vix_series) > 0:
            current_vix = float(vix_series.iloc[-1])
            flags.current_vix = current_vix

    if not np.isnan(current_vix) and current_vix > vix_emergency_threshold:
        logger.warning(
            f"VIX EMERGENCY: VIX={current_vix:.1f} > {vix_emergency_threshold}. "
            f"Reducing to {emergency_cash_pct:.0%} cash."
        )
        cash_pct = emergency_cash_pct
        # Scale down all sector weights proportionally
        adjusted_weights = adjusted_weights * (1.0 - cash_pct)
        flags.vix_emergency_triggered = True
        flags.cash_pct = cash_pct
        flags.notes.append(f"VIX emergency: {current_vix:.1f}")

    # -------------------------------------------------------------------
    # 1b. Progressive VIX de-risking (graduated tiers below emergency)
    # -------------------------------------------------------------------
    elif not np.isnan(current_vix) and vix_progressive_tiers:
        # Find the highest applicable tier (tiers sorted descending by vix_above)
        prog_cash = 0.0
        prog_vix_hit = None
        for tier in sorted(vix_progressive_tiers, key=lambda t: t["vix_above"], reverse=True):
            if current_vix >= tier["vix_above"]:
                prog_cash = float(tier["cash_pct"])
                prog_vix_hit = tier["vix_above"]
                break

        if prog_cash > cash_pct + 1e-9:
            prev_invested = 1.0 - cash_pct
            new_invested  = 1.0 - prog_cash
            if prev_invested > 0:
                adjusted_weights = adjusted_weights * (new_invested / prev_invested)
            cash_pct = prog_cash
            flags.cash_pct = cash_pct
            flags.notes.append(
                f"Progressive VIX: VIX={current_vix:.1f} ≥ {prog_vix_hit} → {prog_cash:.0%} cash"
            )
            logger.info(
                f"Progressive VIX de-risk: VIX={current_vix:.1f} ≥ {prog_vix_hit}, "
                f"cash_pct={prog_cash:.0%}"
            )

    # -------------------------------------------------------------------
    # 2. Drawdown circuit breaker
    # -------------------------------------------------------------------
    current_dd = 0.0
    if equity_curve is not None and len(equity_curve) > 0:
        peak = equity_curve.expanding().max()
        dd_series = (equity_curve / peak) - 1.0
        current_dd = float(dd_series.iloc[-1])
        flags.current_dd_pct = current_dd

    if current_dd < dd_halve_threshold:
        logger.warning(
            f"DRAWDOWN CIRCUIT: DD={current_dd:.2%} < {dd_halve_threshold:.2%}. "
            "Halving position size."
        )
        # Additional 50% reduction (on top of any VIX-triggered reduction)
        additional_cash = (1.0 - cash_pct) * 0.5
        cash_pct = min(cash_pct + additional_cash, 0.90)  # Cap at 90% cash
        # Renormalize to new invested_pct (1 - cash_pct)
        if adjusted_weights.sum() > 0:
            adjusted_weights = adjusted_weights / adjusted_weights.sum() * (1.0 - cash_pct)
        flags.dd_circuit_triggered = True
        flags.cash_pct = cash_pct
        flags.notes.append(f"DD circuit breaker: {current_dd:.2%}")

    # -------------------------------------------------------------------
    # 3. Volatility scaling
    # -------------------------------------------------------------------
    if vol_scaling_enabled and len(portfolio_returns) >= vol_estimation_window:
        realized_vol = compute_realized_vol(portfolio_returns, window=vol_estimation_window)
        historical_vol = compute_historical_vol(portfolio_returns, window=vol_historical_window)
        flags.realized_vol_annual = realized_vol
        flags.historical_vol_annual = historical_vol

        scale = vol_scaling_factor(
            realized_vol=realized_vol,
            historical_vol=historical_vol,
            target_vol=vol_target,
            scale_threshold=vol_scale_threshold,
        )

        if scale < 1.0:
            # Additional cash from vol scaling
            vol_cash = (1.0 - cash_pct) * (1.0 - scale)
            cash_pct = min(cash_pct + vol_cash, 0.90)
            adjusted_weights = adjusted_weights * scale
            flags.vol_scaling_triggered = True
            flags.cash_pct = cash_pct
            flags.notes.append(f"Vol scaling: {realized_vol:.2%} realized, scale={scale:.3f}")

    # -------------------------------------------------------------------
    # 4. Concentration constraint (max weight)
    # -------------------------------------------------------------------
    if adjusted_weights.max() > max_weight + 1e-9:
        n_active = int((adjusted_weights > 1e-6).sum())
        invested_pct = 1.0 - cash_pct

        if n_active > 0 and n_active * max_weight < invested_pct - 1e-9:
            # Infeasible: fewer sectors than needed to deploy full capital.
            # Add a cash buffer so each sector stays ≤ max_weight of total portfolio.
            concentration_cash = invested_pct - n_active * max_weight
            cash_pct += concentration_cash
            invested_pct = n_active * max_weight
            flags.notes.append(
                f"Concentration cash buffer: +{concentration_cash:.1%} "
                f"(only {n_active} sector(s), max_weight={max_weight:.0%})"
            )

        # Iterative water-filling to enforce max_weight among the selected sectors
        for _ in range(100):
            over = adjusted_weights > max_weight + 1e-9
            if not over.any():
                break
            adjusted_weights = adjusted_weights.clip(upper=max_weight)
            s = adjusted_weights.sum()
            if s > 0:
                adjusted_weights = adjusted_weights / s * invested_pct

        flags.notes.append("Concentration constraint applied")

    # -------------------------------------------------------------------
    # 5. Beta constraint (soft, iterative scaling)
    # -------------------------------------------------------------------
    if sector_returns is not None and benchmark_returns is not None:
        sector_betas = estimate_sector_betas(sector_returns, benchmark_returns)
        active_tickers = adjusted_weights[adjusted_weights > 0].index
        port_beta = float((adjusted_weights[active_tickers] * sector_betas[active_tickers]).sum())
        flags.portfolio_beta = port_beta

        if port_beta < beta_min or port_beta > beta_max:
            logger.info(
                f"Portfolio beta {port_beta:.3f} outside [{beta_min}, {beta_max}]. "
                "Adjusting weights..."
            )
            # Simple heuristic: scale each weight by (1 / beta_i) normalized
            # This nudges toward lower-beta sectors if beta too high
            target_beta = (beta_min + beta_max) / 2.0
            beta_adj = sector_betas.reindex(adjusted_weights.index).fillna(1.0)
            # Mix toward equal weight at degree proportional to beta deviation
            deviation = abs(port_beta - target_beta) / target_beta
            mix_alpha = min(deviation, 0.3)  # Max 30% adjustment
            ew = pd.Series(1.0 / len(active_tickers), index=active_tickers)
            adj_w = adjusted_weights.copy()
            adj_w[active_tickers] = (1 - mix_alpha) * adj_w[active_tickers] + mix_alpha * ew
            invested_pct = 1.0 - cash_pct
            if adj_w.sum() > 0:
                adj_w = adj_w / adj_w.sum() * invested_pct
            adjusted_weights = adj_w
            flags.beta_adjusted = True
            flags.notes.append(f"Beta adjusted: {port_beta:.3f} → target ~{target_beta:.2f}")

    # -------------------------------------------------------------------
    # Final normalization
    # -------------------------------------------------------------------
    adjusted_weights = adjusted_weights.clip(lower=0.0)
    if adjusted_weights.sum() > 0:
        invested_pct = 1.0 - cash_pct
        adjusted_weights = adjusted_weights / adjusted_weights.sum() * invested_pct

    flags.cash_pct = cash_pct
    return adjusted_weights, cash_pct, flags


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    print("Risk module loaded successfully.")
    print("RiskFlags fields:", [f.name for f in RiskFlags.__dataclass_fields__.values()])
