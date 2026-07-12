"""Risk Management — crypto perp rotation (Plan 05 §7).

COPIED from qlib-main/sector_rotation/portfolio/risk.py (read-only template).
All five controls preserved with identical mechanics and ordering:
  1. volatility scaling  2. emergency de-risk  3. drawdown circuit breaker
  4. concentration (water-filling + cash buffer)  5. soft beta constraint
plus the progressive de-risk tier pattern (template: vix_progressive_derisk).

Adaptations (plan "Change" column only):
  * √252 → √365 (24/7 annualization); windows 252d → 365d.
  * VIX → btc_rvol (column of the crypto regime-input frame); thresholds are
    crypto brackets (emergency 60, matching regime.py's crisis bracket —
    calibrate on recorded data).
  * Defaults: vol_target 0.40 (plan §9), max_weight 0.45, DD tiers −10%/−20%
    aligned with Plan 06 §3.
  * Beta vs KXBTCPERP (was SPY).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TRADING_DAYS = 365


# ---------------------------------------------------------------------------
# Risk flag dataclass — template verbatim (vix field renamed rvol)
# ---------------------------------------------------------------------------

@dataclass
class RiskFlags:
    """Record which risk controls were triggered at a given rebalance date."""
    date: pd.Timestamp
    vol_scaling_triggered: bool = False
    emergency_triggered: bool = False
    dd_circuit_triggered: bool = False
    beta_adjusted: bool = False
    realized_vol_annual: float = float("nan")
    historical_vol_annual: float = float("nan")
    current_rvol: float = float("nan")
    portfolio_beta: float = float("nan")
    current_dd_pct: float = float("nan")
    cash_pct: float = 0.0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "notes"}
        d["notes"] = "; ".join(self.notes)
        return d


# ---------------------------------------------------------------------------
# Volatility scaling — template verbatim, 365-day
# ---------------------------------------------------------------------------

def compute_realized_vol(returns: pd.Series, window: int = 20) -> float:
    """Annualized short-window realized vol (√365)."""
    if len(returns) < window:
        return float("nan")
    recent = returns.iloc[-window:]
    return float(recent.std() * np.sqrt(TRADING_DAYS))


def compute_historical_vol(returns: pd.Series, window: int = 365) -> float:
    """Annualized long-run vol (√365)."""
    if len(returns) < window // 2:
        return float("nan")
    hist = returns.iloc[-window:]
    return float(hist.std() * np.sqrt(TRADING_DAYS))


def vol_scaling_factor(
    realized_vol: float,
    historical_vol: float,
    target_vol: float = 0.40,
    scale_threshold: float = 1.5,
) -> float:
    """min(target/realized, 1) when realized > threshold × historical (verbatim)."""
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
# Portfolio beta estimation — template verbatim (benchmark = KXBTCPERP)
# ---------------------------------------------------------------------------

def estimate_sector_betas(
    sector_returns: pd.DataFrame,
    benchmark_returns: pd.Series,
    window: int = 365,
) -> pd.Series:
    """OLS beta per perp vs the BTC-perp benchmark."""
    betas = {}
    for col in sector_returns.columns:
        sec_ret = sector_returns[col].iloc[-window:].dropna()
        bench_ret = benchmark_returns.iloc[-window:].dropna()
        aligned = pd.concat([sec_ret, bench_ret], axis=1).dropna()
        if len(aligned) < 20:
            betas[col] = 1.0
            continue
        x = aligned.iloc[:, 1].values
        y = aligned.iloc[:, 0].values
        cov_xy = np.cov(x, y)[0, 1]
        var_x = np.var(x)
        betas[col] = cov_xy / var_x if var_x > 1e-10 else 1.0

    return pd.Series(betas)


# ---------------------------------------------------------------------------
# Main risk management pipeline — template flow preserved
# ---------------------------------------------------------------------------

def apply_risk_controls(
    weights: pd.Series,
    portfolio_returns: pd.Series,
    regime_inputs: pd.DataFrame,
    sector_returns: Optional[pd.DataFrame] = None,
    benchmark_returns: Optional[pd.Series] = None,
    equity_curve: Optional[pd.Series] = None,
    # Thresholds (crypto defaults; template parameter surface preserved)
    vol_target: float = 0.40,
    vol_estimation_window: int = 20,
    vol_historical_window: int = 365,
    vol_scale_threshold: float = 1.5,
    vol_scaling_enabled: bool = True,
    rvol_emergency_threshold: float = 60.0,
    emergency_cash_pct: float = 0.50,
    dd_halve_threshold: float = -0.10,
    dd_recovery_threshold: float = -0.05,
    beta_min: float = 0.85,
    beta_max: float = 1.15,
    max_weight: float = 0.45,
    rvol_progressive_tiers: Optional[list] = None,
) -> Tuple[pd.Series, float, RiskFlags]:
    """All risk controls → (adjusted_weights, cash_pct, flags). Template
    ordering: emergency → progressive tiers → DD circuit → vol scaling →
    concentration → beta. Weights may sum < 1; remainder = cash."""
    date = regime_inputs.index[-1] if len(regime_inputs) > 0 else pd.Timestamp.now(tz="UTC")
    flags = RiskFlags(date=date)

    adjusted_weights = weights.copy()
    cash_pct = 0.0

    # 1. Emergency de-risk (btc_rvol; was VIX)
    current_rvol = float("nan")
    if "btc_rvol" in regime_inputs.columns and len(regime_inputs) > 0:
        rvol_series = regime_inputs["btc_rvol"].dropna()
        if len(rvol_series) > 0:
            current_rvol = float(rvol_series.iloc[-1])
            flags.current_rvol = current_rvol

    if not np.isnan(current_rvol) and current_rvol > rvol_emergency_threshold:
        logger.warning(
            f"RVOL EMERGENCY: btc_rvol={current_rvol:.1f} > {rvol_emergency_threshold}. "
            f"Reducing to {emergency_cash_pct:.0%} cash."
        )
        cash_pct = emergency_cash_pct
        adjusted_weights = adjusted_weights * (1.0 - cash_pct)
        flags.emergency_triggered = True
        flags.cash_pct = cash_pct
        flags.notes.append(f"rvol emergency: {current_rvol:.1f}")

    # 1b. Progressive de-risking tiers (template vix_progressive pattern)
    elif not np.isnan(current_rvol) and rvol_progressive_tiers:
        prog_cash = 0.0
        prog_hit = None
        for tier in sorted(rvol_progressive_tiers, key=lambda t: t["rvol_above"], reverse=True):
            if current_rvol >= tier["rvol_above"]:
                prog_cash = float(tier["cash_pct"])
                prog_hit = tier["rvol_above"]
                break

        if prog_cash > cash_pct + 1e-9:
            prev_invested = 1.0 - cash_pct
            new_invested = 1.0 - prog_cash
            if prev_invested > 0:
                adjusted_weights = adjusted_weights * (new_invested / prev_invested)
            cash_pct = prog_cash
            flags.cash_pct = cash_pct
            flags.notes.append(
                f"Progressive rvol: {current_rvol:.1f} ≥ {prog_hit} → {prog_cash:.0%} cash"
            )
            logger.info(
                f"Progressive rvol de-risk: {current_rvol:.1f} ≥ {prog_hit}, "
                f"cash_pct={prog_cash:.0%}"
            )

    # 2. Drawdown circuit breaker — template verbatim
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
        additional_cash = (1.0 - cash_pct) * 0.5
        cash_pct = min(cash_pct + additional_cash, 0.90)
        if adjusted_weights.sum() > 0:
            adjusted_weights = adjusted_weights / adjusted_weights.sum() * (1.0 - cash_pct)
        flags.dd_circuit_triggered = True
        flags.cash_pct = cash_pct
        flags.notes.append(f"DD circuit breaker: {current_dd:.2%}")

    # 3. Volatility scaling — template verbatim
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
            vol_cash = (1.0 - cash_pct) * (1.0 - scale)
            cash_pct = min(cash_pct + vol_cash, 0.90)
            adjusted_weights = adjusted_weights * scale
            flags.vol_scaling_triggered = True
            flags.cash_pct = cash_pct
            flags.notes.append(f"Vol scaling: {realized_vol:.2%} realized, scale={scale:.3f}")

    # 4. Concentration constraint — template verbatim (water-filling + buffer)
    if adjusted_weights.max() > max_weight + 1e-9:
        n_active = int((adjusted_weights > 1e-6).sum())
        invested_pct = 1.0 - cash_pct

        if n_active > 0 and n_active * max_weight < invested_pct - 1e-9:
            concentration_cash = invested_pct - n_active * max_weight
            cash_pct += concentration_cash
            invested_pct = n_active * max_weight
            flags.notes.append(
                f"Concentration cash buffer: +{concentration_cash:.1%} "
                f"(only {n_active} perp(s), max_weight={max_weight:.0%})"
            )

        for _ in range(100):
            over = adjusted_weights > max_weight + 1e-9
            if not over.any():
                break
            adjusted_weights = adjusted_weights.clip(upper=max_weight)
            s = adjusted_weights.sum()
            if s > 0:
                adjusted_weights = adjusted_weights / s * invested_pct

        flags.notes.append("Concentration constraint applied")

    # 5. Beta constraint (soft) — template verbatim, benchmark KXBTCPERP
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
            target_beta = (beta_min + beta_max) / 2.0
            deviation = abs(port_beta - target_beta) / target_beta
            mix_alpha = min(deviation, 0.3)
            ew = pd.Series(1.0 / len(active_tickers), index=active_tickers)
            adj_w = adjusted_weights.copy()
            adj_w[active_tickers] = (1 - mix_alpha) * adj_w[active_tickers] + mix_alpha * ew
            invested_pct = 1.0 - cash_pct
            if adj_w.sum() > 0:
                adj_w = adj_w / adj_w.sum() * invested_pct
            adjusted_weights = adj_w
            flags.beta_adjusted = True
            flags.notes.append(f"Beta adjusted: {port_beta:.3f} → target ~{target_beta:.2f}")

    # Final normalization — template verbatim
    adjusted_weights = adjusted_weights.clip(lower=0.0)
    if adjusted_weights.sum() > 0:
        invested_pct = 1.0 - cash_pct
        adjusted_weights = adjusted_weights / adjusted_weights.sum() * invested_pct

    flags.cash_pct = cash_pct
    return adjusted_weights, cash_pct, flags
