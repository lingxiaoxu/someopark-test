"""
Risk Management
===============
Position-level and portfolio-level risk controls for the AEUS strategy.

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
    event_derisk_triggered: bool = False
    event_derisk_reason: str = ""
    exposure_mult: float = 1.0          # 通路③ 敞口放大器 E(1.0 = 未启用/中性)
    shortage_z: float = float("nan")    # 输入:缺电度 z(PIT)
    graph_phi: float = float("nan")     # 图谱敏感度加权的组合暴露系数 φ
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


def compute_downside_vol(
    returns: pd.Series,
    window: int = 20,
) -> float:
    """
    Annualized downside semi-volatility (downside deviation w.r.t. 0).

    公式 = sqrt( mean( min(r, 0)^2 ) ) × sqrt(252) —— 对窗口内全部观测取
    负部均方根,而非只对负收益日取 std(后者样本减半、噪声翻倍且自由度不稳)。
    对称分布下 ≈ 总波动/√2,因此 semivol 参数集的 target 需按 √2 折算
    (见 AEUSStrategyRuns.py Group C 的 semivol_* 注释)。
    用途(AEUS 进化计划 I-1): 反弹期的向上波动不再压制仓位,下行风险刻画不变。

    Parameters
    ----------
    returns : pd.Series
        Daily portfolio returns (simple).
    window : int
        Rolling window in trading days (与 compute_realized_vol 同语义)。

    Returns
    -------
    float
        Annualized downside vol. NaN if insufficient data
        (短窗要求满窗、长窗允许半窗,与两个既有 vol 函数各自一致)。
    """
    min_obs = window if window <= 30 else window // 2
    if len(returns) < min_obs:
        return float("nan")
    recent = returns.iloc[-window:].dropna()
    if len(recent) < max(5, min_obs // 2):
        return float("nan")
    neg = np.minimum(recent.values, 0.0)
    return float(np.sqrt(np.mean(neg ** 2)) * np.sqrt(252))


def vol_scaling_factor(
    realized_vol: float,
    historical_vol: float,
    target_vol: float = 0.30,       # AEUS default (SSRS 0.12)
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

def compute_exposure_amplifier(
    shortage_z: float,
    weights: pd.Series,
    sensitivity: Optional[Dict[str, float]] = None,
    k: float = 0.10,
    lo: float = 0.85,
    hi: float = 1.15,
) -> Tuple[float, float]:
    """通路③ 敞口放大器(AEUS_PLAN §4.2 A4):E = clip(1 + k · z · φ, lo, hi)。

    知识图谱用法(2026-09-02):缺电不是对所有板块一视同仁——它沿图谱里
    ``power_demand_proxy`` / ``power_price_proxy`` 的出边传导。φ 是**当前组合**
    对这两个节点的暴露:φ = Σ_s w_s·sens_s / Σ_s w_s,sens_s 由入边权重归一
    (均值 1,无入边的板块给地板值——"缺电=整条链都受益",只是幅度不同)。
    于是同一个缺电度 z:满仓 IPP/gas_midstream 的组合被放大得多,满仓 water_cooling
    的组合几乎不动。z 缺数(NaN)→ E=1,φ=NaN。sensitivity 为 None → φ=1(纯标量版)。
    """
    if shortage_z is None or not np.isfinite(shortage_z):
        return 1.0, float("nan")
    w = weights.clip(lower=0.0) if weights is not None else pd.Series(dtype=float)
    tot = float(w.sum()) if len(w) else 0.0
    if sensitivity and tot > 0:
        phi = float(sum(float(w[s]) * float(sensitivity.get(s, 1.0)) for s in w.index) / tot)
    else:
        phi = 1.0
    e = 1.0 + k * float(shortage_z) * phi
    return float(min(max(e, lo), hi)), phi


def apply_risk_controls(
    weights: pd.Series,
    portfolio_returns: pd.Series,
    macro: pd.DataFrame,
    sector_returns: Optional[pd.DataFrame] = None,
    benchmark_returns: Optional[pd.Series] = None,
    equity_curve: Optional[pd.Series] = None,
    # Thresholds
    vol_target: float = 0.30,      # AEUS default (SSRS 0.12) — high-beta semis satellite
    vol_estimation_window: int = 20,
    vol_historical_window: int = 252,
    vol_scale_threshold: float = 1.5,
    vol_scaling_enabled: bool = True,
    vol_downside_only: bool = False,   # I-1(2026-07-21): 分子分母同用下行半波动
    vix_emergency_threshold: float = 36.0,   # AEUS (SSRS 35) — only true crises
    emergency_cash_pct: float = 0.45,
    dd_halve_threshold: float = -0.25,        # AEUS (SSRS -0.15) — semis draw 20%+ normally
    dd_recovery_threshold: float = -0.12,
    dd_release_rebound: float = 0.0,   # I-3(2026-07-21): 离底反弹≥此值→跳过砍半; 0=关闭
    beta_min: float = 0.40,        # AEUS (SSRS 0.85)
    beta_max: float = 3.00,        # AEUS (SSRS 1.15) — do not fight the high semis beta
    max_weight: float = 0.55,      # AEUS (SSRS 0.40) — concentrate in winners
    vix_progressive_tiers: Optional[list] = None,
    event_derisk_active: bool = False,
    event_derisk_frac: float = 0.5,
    event_derisk_reason: str = "",
    exposure_mult: float = 1.0,            # 通路③:E(compute_exposure_amplifier);1.0 = 逐字节等价旧行为
    exposure_allow_leverage: bool = False, # False → 放大只能吃掉现金,总仓位封顶 100%
    exposure_note: str = "",
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
    # 1c. Event-risk de-risk (semi event overlay; default off)
    #     Additive-on-remaining (same pattern as DD/vol): sell `frac` of the
    #     currently-invested book to cash. event_derisk_frac=0.5 = sell half.
    #     Driven by an external persisted flag (veto state machine, §8.3);
    #     while active, this re-applies daily → naturally holds (no buy-back).
    # -------------------------------------------------------------------
    if event_derisk_active and event_derisk_frac > 0:
        ev_cash = (1.0 - cash_pct) * float(event_derisk_frac)
        cash_pct = min(cash_pct + ev_cash, 0.95)
        adjusted_weights = adjusted_weights * (1.0 - float(event_derisk_frac))
        flags.event_derisk_triggered = True
        flags.event_derisk_reason = event_derisk_reason
        flags.cash_pct = cash_pct
        flags.notes.append(f"Event de-risk: sold {event_derisk_frac:.0%} → cash"
                           + (f" ({event_derisk_reason})" if event_derisk_reason else ""))
        logger.warning(f"EVENT DE-RISK: sell {event_derisk_frac:.0%} → cash"
                       f"{' — ' + event_derisk_reason if event_derisk_reason else ''}")

    # -------------------------------------------------------------------
    # 2. Drawdown circuit breaker
    # -------------------------------------------------------------------
    current_dd = 0.0
    dd_rebound = 0.0   # I-3: 本轮回撤事件内,净值相对谷底的反弹幅度
    if equity_curve is not None and len(equity_curve) > 0:
        peak = equity_curve.expanding().max()
        dd_series = (equity_curve / peak) - 1.0
        current_dd = float(dd_series.iloc[-1])
        flags.current_dd_pct = current_dd
        if dd_release_rebound > 0 and len(equity_curve) > 1:
            # 本轮事件起点 = 最后一次创新高的位置;谷底 = 该点之后的最低净值。
            # 崩塌下行段谷底即当前 → rebound≈0(保护不变);修复段 rebound 随反弹增长。
            at_peak = dd_series >= -1e-12
            ep_start = at_peak[at_peak].index[-1] if at_peak.any() else equity_curve.index[0]
            trough = float(equity_curve.loc[ep_start:].min())
            if trough > 0:
                dd_rebound = float(equity_curve.iloc[-1]) / trough - 1.0

    if current_dd < dd_halve_threshold:
        if dd_release_rebound > 0 and dd_rebound >= dd_release_rebound:
            # I-3 off-bottom release: DD 仍深但已确认离底反弹 → 不再逐日砍半,
            # 让修复期恢复满仓(VIX 梯度/紧急减仓仍在其上独立生效)
            flags.notes.append(
                f"DD release: rebound {dd_rebound:.1%} ≥ {dd_release_rebound:.0%} "
                f"off trough (DD={current_dd:.2%}) — halve skipped"
            )
            logger.info(
                f"DD circuit released: DD={current_dd:.2%} but rebound "
                f"{dd_rebound:.1%} ≥ {dd_release_rebound:.0%} off trough."
            )
        else:
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
        if vol_downside_only:
            # I-1: 分子分母同族(下行 vs 下行),触发语义不变——下行波动异常放大才缩仓;
            # 纯上涨期 semivol→0 → 不触发缩放(反弹不再被向上波动误伤)
            realized_vol = compute_downside_vol(portfolio_returns, window=vol_estimation_window)
            historical_vol = compute_downside_vol(portfolio_returns, window=vol_historical_window)
            flags.notes.append("vol_mode=downside")
        else:
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

    # -------------------------------------------------------------------
    # 通路③ 敞口放大器(2026-09-02):E 只改 gross,不改选谁。
    # 作用在所有风控档之后:vol/VIX/DD 决定的现金是"防守",E 决定的是"进攻幅度";
    # 不允许杠杆时 E>1 只能吃掉既有现金(满仓时无效),E<1 一律多留现金。
    # 单板块上限 max_weight 仍然成立(放大不能把集中度顶穿)。E=1 → 上面结果逐字节不变。
    # -------------------------------------------------------------------
    if exposure_mult != 1.0 and adjusted_weights.sum() > 0:
        invested = float(adjusted_weights.sum())
        # 防守优先(2026-09-02):vol/VIX/DD/事件档已经决定要留现金时,放大器只许继续减,
        # 不许把防守现金买回去 —— 缺电度尖峰恰好撞上危机时不能自动复仓。
        _defensive = (flags.vol_scaling_triggered or flags.vix_emergency_triggered
                      or flags.dd_circuit_triggered or flags.event_derisk_triggered)
        if _defensive and exposure_mult > 1.0:
            exposure_mult = 1.0
        target = invested * float(exposure_mult)
        if not exposure_allow_leverage:
            target = min(target, 1.0)     # 不许杠杆 → 满仓时 E>1 无效,放大器实为单向减仓器
        scale = target / invested
        wmax = float(adjusted_weights.max())
        if wmax * scale > max_weight + 1e-9:
            scale = min(scale, max_weight / wmax)
        adjusted_weights = adjusted_weights * scale
        cash_pct = max(0.0, 1.0 - float(adjusted_weights.sum()))
        flags.exposure_mult = float(exposure_mult)
        flags.notes.append(f"Exposure amplifier E={exposure_mult:.3f} → gross {invested:.0%}→{adjusted_weights.sum():.0%}"
                           + (f" ({exposure_note})" if exposure_note else ""))
        logger.info("EXPOSURE AMPLIFIER: E=%.3f gross %.1f%% → %.1f%% %s",
                    exposure_mult, invested * 100, adjusted_weights.sum() * 100, exposure_note)

    flags.cash_pct = cash_pct
    return adjusted_weights, cash_pct, flags


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    print("Risk module loaded successfully.")
    print("RiskFlags fields:", [f.name for f in RiskFlags.__dataclass_fields__.values()])
