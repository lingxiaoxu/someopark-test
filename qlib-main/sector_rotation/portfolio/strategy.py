"""
strategy.py
===========
SectorRotationWeightStrategy — adapts our sector rotation logic into qlib's
WeightStrategyBase / BaseStrategy hierarchy.

Architecture
------------
We inherit from WeightStrategyBase (qlib) so that:
  - qlib's TradeCalendarManager drives the iteration
  - qlib's OrderGenWOInteract converts our target weights → share-based Orders
  - qlib's SimulatorExecutor executes those Orders against SectorETFExchange
  - qlib's Account / Position tracks portfolio state

We override:
  - generate_trade_decision()          : control when to rebalance (monthly)
  - generate_target_weight_position()  : our signal→weight→risk-control logic

Tracking data (signals, regime, costs, risk_flags) is accumulated in the
strategy so the engine can extract it after backtest_loop completes.
"""

from __future__ import annotations

import copy
import logging
import sys
import io as _io
from typing import Dict, List, Optional, Set

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# qlib strategy imports (graceful fallback)
# ---------------------------------------------------------------------------
_stderr = sys.stderr
sys.stderr = _io.StringIO()
try:
    from qlib.contrib.strategy.signal_strategy import WeightStrategyBase
    from qlib.backtest.decision import Order, OrderDir, TradeDecisionWO
    from qlib.backtest.position import Position
    _QLIB_STRATEGY_AVAILABLE = True
except Exception as _e:
    _QLIB_STRATEGY_AVAILABLE = False
    logger.warning(f"qlib strategy infrastructure not available: {_e}")
sys.stderr = _stderr

# Our own portfolio modules
from .optimizer import optimize_weights
from .risk import apply_risk_controls
from .rebalance import (
    apply_zscore_threshold_filter,
    compute_turnover,
    cap_turnover,
    should_emergency_rebalance,
)
from ..backtest.costs import compute_transaction_costs


if not _QLIB_STRATEGY_AVAILABLE:
    # Stub so the module can still be imported in environments without qlib
    class SectorRotationWeightStrategy:  # type: ignore
        def __init__(self, *a, **kw):
            raise RuntimeError("qlib strategy infrastructure not available")
else:
    class SectorRotationWeightStrategy(WeightStrategyBase):
        """
        Monthly sector rotation strategy integrated into qlib's WeightStrategyBase.

        Responsibilities
        ----------------
        1. Maintain the set of monthly rebalance dates.
        2. On rebalance days: compute target weights using our signal+optimizer+risk pipeline.
        3. Return qlib TradeDecisionWO with Orders for the SimulatorExecutor.
        4. On non-rebalance days: return empty TradeDecisionWO (no trading).
        5. Accumulate tracking data (weights, scores, costs, risk_flags) for engine extraction.

        Parameters
        ----------
        composite_signals : pd.DataFrame
            Monthly composite z-scores, shape (n_months, n_etfs).  Index = month-end dates.
        etf_prices : pd.DataFrame
            Daily ETF prices, shape (n_days, n_etfs).
        macro : pd.DataFrame
            Daily macro indicators (vix, yield_curve, hy_spread), shape (n_days, 3+).
        rebalance_dates : set
            Set of pd.Timestamp for monthly rebalance dates.
        port_cfg : dict
            Portfolio section of config (optimizer, top_n_sectors, constraints, etc.)
        reb_cfg : dict
            Rebalance section of config (zscore_change_threshold, max_monthly_turnover, etc.)
        risk_cfg : dict
            Risk section of config (vol_scaling, drawdown, etc.)
        cost_cfg : dict
            Costs section of config (etf_fee_bps, etc.)
        initial_capital : float
            Starting portfolio value in USD.
        """

        def __init__(
            self,
            *,
            composite_signals: pd.DataFrame,
            etf_prices: pd.DataFrame,
            macro: pd.DataFrame,
            rebalance_dates: Set[pd.Timestamp],
            port_cfg: dict,
            reb_cfg: dict,
            risk_cfg: dict,
            cost_cfg: dict,
            initial_capital: float = 1_000_000.0,
            **kwargs,
        ) -> None:
            # WeightStrategyBase requires `signal` kwarg; we pass None because
            # we override generate_trade_decision() and fetch scores ourselves.
            super().__init__(signal=None, **kwargs)

            self._composite_signals = composite_signals
            self._etf_prices = etf_prices
            self._macro = macro
            self._rebalance_dates: Set[pd.Timestamp] = set(rebalance_dates)
            self._port_cfg = port_cfg
            self._reb_cfg = reb_cfg
            self._risk_cfg = risk_cfg
            self._cost_cfg = cost_cfg
            self._initial_capital = initial_capital

            # Mutable per-step state
            self._current_weights: pd.Series = pd.Series(dtype=float)
            self._prev_scores: pd.Series = pd.Series(dtype=float)
            self._portfolio_daily_returns: pd.Series = pd.Series(dtype=float)
            self._equity_level: float = initial_capital

            # Tracking data (accumulated across all rebalance steps)
            self.weights_records: Dict[pd.Timestamp, dict] = {}
            self.scores_records: Dict[pd.Timestamp, dict] = {}
            self.costs_records: List[dict] = []
            self.risk_flags_records: List[dict] = []
            self.regime_records: Dict[pd.Timestamp, str] = {}

            # ETF ticker universe
            self._etf_tickers: List[str] = list(etf_prices.columns)

            # Daily returns pre-computed for risk controls
            self._etf_daily_ret: pd.DataFrame = etf_prices.pct_change().fillna(0.0)

        # ------------------------------------------------------------------
        # Core override: generate_trade_decision
        # ------------------------------------------------------------------

        def generate_trade_decision(self, execute_result=None) -> "TradeDecisionWO":
            """
            Called once per calendar step by SimulatorExecutor.

            Monthly rebalance days → compute weights and return orders.
            All other days → return empty decision.
            """
            trade_step = self.trade_calendar.get_trade_step()
            trade_start_time, trade_end_time = self.trade_calendar.get_step_time(trade_step)

            # Update daily portfolio returns for risk-control lookback
            self._update_daily_return(trade_start_time)

            # Non-rebalance day: return empty decision immediately
            if trade_start_time not in self._rebalance_dates:
                # Check emergency re-balance condition
                macro_slice = self._macro.loc[:trade_start_time] if trade_start_time in self._macro.index else self._macro
                if not should_emergency_rebalance(
                    macro_slice,
                    self._current_weights,
                    vix_threshold=self._reb_cfg.get("emergency_derisk_vix", 35.0),
                ):
                    return TradeDecisionWO([], self)

            # Get latest composite scores up to this date
            avail_scores = self._composite_signals.loc[:trade_start_time].dropna(how="all")
            if avail_scores.empty:
                return TradeDecisionWO([], self)

            latest_scores = avail_scores.iloc[-1]
            self.scores_records[trade_start_time] = latest_scores.to_dict()

            # Get current position as weight dict from qlib's Account/Position
            current_temp: Position = copy.deepcopy(self.trade_position)

            # Compute proposed weights
            proposed_weights = self._compute_proposed_weights(latest_scores, trade_start_time)

            # Threshold filter: skip re-weighting if signal change < threshold
            thresh = self._reb_cfg.get("zscore_change_threshold", 0.5)
            filtered_weights, rebalanced, held = apply_zscore_threshold_filter(
                new_scores=latest_scores,
                prev_scores=self._prev_scores,
                new_weights=proposed_weights,
                prev_weights=self._current_weights,
                threshold=thresh,
            )

            # Turnover cap
            max_to = self._reb_cfg.get("max_monthly_turnover", 0.80)
            filtered_weights = cap_turnover(filtered_weights, self._current_weights, max_to)

            # Risk controls (vol scaling, VIX emergency, DD circuit)
            macro_slice = self._macro.loc[:trade_start_time] if trade_start_time in self._macro.index else self._macro
            adj_weights, cash_pct, flags = apply_risk_controls(
                weights=filtered_weights,
                portfolio_returns=self._portfolio_daily_returns.iloc[-252:] if len(self._portfolio_daily_returns) > 0 else pd.Series(dtype=float),
                macro=macro_slice,
                equity_curve=None,
                vol_target=self._risk_cfg.get("vol_scaling", {}).get("target_vol_annual", 0.12),
                vol_scaling_enabled=self._risk_cfg.get("vol_scaling", {}).get("enabled", True),
                vix_emergency_threshold=self._reb_cfg.get("emergency_derisk_vix", 35.0),
                emergency_cash_pct=self._reb_cfg.get("emergency_cash_pct", 0.50),
                dd_halve_threshold=self._risk_cfg.get("drawdown", {}).get("cumulative_dd_halve", -0.15),
                max_weight=self._port_cfg.get("constraints", {}).get("max_weight", 0.40),
            )

            # Transaction costs tracking
            portfolio_value = current_temp.calculate_value() if current_temp.get_stock_list() else self._equity_level
            cost_result = compute_transaction_costs(self._current_weights, adj_weights, portfolio_value)
            self.costs_records.append({"date": trade_start_time, **cost_result})

            # Record tracking data
            self.weights_records[trade_start_time] = adj_weights.to_dict()
            self.risk_flags_records.append({"date": trade_start_time, **flags.to_dict()})

            # Update mutable state
            self._current_weights = adj_weights.copy()
            self._prev_scores = latest_scores.copy()

            # Convert target weights (cash_pct portion = 0-weight positions)
            target_weight_position = {
                ticker: float(adj_weights.get(ticker, 0.0))
                for ticker in self._etf_tickers
                if float(adj_weights.get(ticker, 0.0)) > 1e-6
            }

            if not target_weight_position:
                return TradeDecisionWO([], self)

            # Use WeightStrategyBase's order generator to convert weights → Orders
            order_list = self.order_generator.generate_order_list_from_target_weight_position(
                current=current_temp,
                trade_exchange=self.trade_exchange,
                risk_degree=self.get_risk_degree(trade_step),
                target_weight_position=target_weight_position,
                pred_start_time=trade_start_time,
                pred_end_time=trade_end_time,
                trade_start_time=trade_start_time,
                trade_end_time=trade_end_time,
            )

            return TradeDecisionWO(order_list, self)

        # ------------------------------------------------------------------
        # Required override: generate_target_weight_position
        # ------------------------------------------------------------------

        def generate_target_weight_position(
            self,
            score: pd.Series,
            current: "Position",
            trade_start_time: pd.Timestamp,
            trade_end_time: pd.Timestamp,
        ) -> dict:
            """
            Convert composite signal scores → target weight dict {ticker: weight}.

            Called by WeightStrategyBase.generate_trade_decision() when using
            the standard signal-based path.  In our strategy we short-circuit via
            the override of generate_trade_decision(), so this method is a fallback.
            """
            proposed_weights = self._compute_proposed_weights(score, trade_start_time)
            macro_slice = self._macro.loc[:trade_start_time] if trade_start_time in self._macro.index else self._macro
            adj_weights, cash_pct, flags = apply_risk_controls(
                weights=proposed_weights,
                portfolio_returns=self._portfolio_daily_returns.iloc[-252:] if len(self._portfolio_daily_returns) > 0 else pd.Series(dtype=float),
                macro=macro_slice,
            )
            return {
                ticker: float(adj_weights.get(ticker, 0.0))
                for ticker in self._etf_tickers
                if float(adj_weights.get(ticker, 0.0)) > 1e-6
            }

        # ------------------------------------------------------------------
        # Internal helpers
        # ------------------------------------------------------------------

        def _compute_proposed_weights(
            self,
            scores: pd.Series,
            as_of: pd.Timestamp,
        ) -> pd.Series:
            """Run optimize_weights for this rebalance date."""
            hist_ret = self._etf_daily_ret.loc[:as_of].iloc[
                -self._port_cfg.get("cov", {}).get("lookback_days", 252):
            ]
            return optimize_weights(
                scores=scores,
                returns=hist_ret,
                method=self._port_cfg.get("optimizer", "inv_vol"),
                cov_method=self._port_cfg.get("cov", {}).get("method", "ledoit_wolf"),
                max_weight=self._port_cfg.get("constraints", {}).get("max_weight", 0.40),
                min_weight=self._port_cfg.get("constraints", {}).get("min_weight", 0.00),
                top_n=self._port_cfg.get("top_n_sectors", 4),
                min_score=self._port_cfg.get("min_zscore", -0.5),
            )

        def _update_daily_return(self, dt: pd.Timestamp) -> None:
            """Approximate portfolio daily return from current weights and ETF returns."""
            if dt not in self._etf_daily_ret.index or self._current_weights.empty:
                return
            sector_ret = self._etf_daily_ret.loc[dt]
            port_ret = float(
                (self._current_weights * sector_ret.reindex(self._current_weights.index, fill_value=0.0)).sum()
            )
            self._portfolio_daily_returns = pd.concat([
                self._portfolio_daily_returns,
                pd.Series([port_ret], index=[dt]),
            ])
            self._equity_level *= (1.0 + port_ret)

        def get_risk_degree(self, trade_step=None) -> float:
            """Use 100% of available capital (no cash buffer from risk_degree)."""
            return 1.0
