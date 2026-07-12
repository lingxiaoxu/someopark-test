"""Plan 03 funding-carry signal — PROTOTYPE for backtest (milestone 2).

Plan 03 gates LIVE deployment of this module behind a 2-week funding measurement
(milestone 1). This prototype is for the milestone-2 proxy/Kalshi BACKTEST only
— it is never wired to live order flow. The forecast + carry-vs-drift gate here
are exactly the mechanics the plan §6 describes.

Sign conventions (single-sourced with crypto_common.costs.funding_payment):
positive funding rate ⇒ longs pay shorts. To COLLECT funding you hold the
opposite of the payer:
    rate > 0  → SHORT  (position sign −1) → receives |rate|·notional
    rate < 0  → LONG   (position sign +1) → receives |rate|·notional
So the carry position sign = −sign(rate). The catch (plan §2): that position is
net-directional; the gate exists to only take it when expected funding beats
expected adverse drift + cost.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CarryParams:
    """WF sweep surface (plan §6/§9). Defaults = config sketch."""
    min_carry_edge_per_cycle: float = 5e-4     # 5 bps/cycle floor to bother (plan gate)
    funding_forecast_window: int = 6           # cycles of autocorrelation for forecast
    skew_percentile_entry: float = 0.0         # only harvest above this |rate| percentile
    drift_proxy: str = "zero"                  # zero | trend (pessimistic drift estimate)
    drift_lookback: int = 6                    # cycles for the trend drift proxy
    vol_regime_cutoff: float | None = None     # annualized rvol above which carry is cut


def forecast_next_funding(rate_hist: pd.Series, window: int) -> float:
    """Forecast next cycle's funding from recent realized funding.

    Simple persistence + mean-reversion blend: EWMA of the last `window`
    cycles. Funding is autocorrelated (perp-index gap moves slowly), so recent
    realized is a better-than-naive predictor (plan §2).
    """
    tail = rate_hist.dropna().tail(window)
    if tail.empty:
        return 0.0
    weights = np.linspace(1.0, 2.0, len(tail))       # recency-weighted
    return float(np.average(tail.to_numpy(), weights=weights))


def expected_drift(price_hist: pd.Series, params: CarryParams) -> float:
    """Pessimistic per-cycle adverse-drift estimate (plan §6: keep it conservative).

    'zero' → assume no favorable drift (the honest default). 'trend' → recent
    realized per-cycle return magnitude as the drift you must overcome.
    """
    if params.drift_proxy == "zero":
        return 0.0
    rets = price_hist.pct_change().dropna().tail(params.drift_lookback)
    return float(abs(rets.mean())) if len(rets) else 0.0


def carry_signal(rate_hist: pd.Series, price_hist: pd.Series, params: CarryParams,
                 *, rate_percentile: float | None = None,
                 realized_vol_annual: float | None = None) -> dict:
    """One-cycle carry decision. Returns {position_sign, expected_edge, gate_pass}.

    position_sign ∈ {−1,0,+1}: 0 when the gate fails (carry doesn't beat drift+cost).
    """
    f_hat = forecast_next_funding(rate_hist, params.funding_forecast_window)
    drift = expected_drift(price_hist, params)
    # you collect |f_hat|; you risk `drift` of adverse move
    expected_edge = abs(f_hat) - drift
    collect_sign = -np.sign(f_hat) if f_hat != 0 else 0.0

    gate = expected_edge >= params.min_carry_edge_per_cycle
    if rate_percentile is not None and rate_percentile < params.skew_percentile_entry:
        gate = False
    if (params.vol_regime_cutoff is not None and realized_vol_annual is not None
            and realized_vol_annual > params.vol_regime_cutoff):
        gate = False                                  # high-vol → cut carry (plan §6)

    return {"position_sign": int(collect_sign) if gate else 0,
            "forecast_funding": f_hat, "expected_drift": drift,
            "expected_edge": expected_edge, "gate_pass": bool(gate)}
