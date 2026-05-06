"""
Backtest Engine
===============
Event-driven monthly backtest for the sector rotation strategy.

Architecture
------------
Primary path uses qlib's full backtest infrastructure:
    - SectorETFExchange          (qlib Exchange)            : price data
    - Account + Position         (qlib account layer)       : portfolio state
    - USTradeCalendarManager     (qlib TradeCalendarManager): US trading dates
    - SectorRotationWeightStrategy (qlib WeightStrategyBase): signal→weight logic
    - SectorSimulatorExecutor    (qlib SimulatorExecutor)   : order execution
    - CommonInfrastructure       (qlib)                     : shared infra
    - backtest_loop              (qlib)                     : main execution loop
    - decompose_portofolio       (qlib profit_attribution)  : sector weight+return decomposition
    - indicator_analysis         (qlib contrib.evaluate)    : trade execution quality (pa, pos, ffr)
    - Account turnover           (qlib Account metrics)     : total_turnover, turnover columns
    - QlibRecorder + MLflowExpManager (qlib.workflow)       : experiment tracking (MLflow backend)

Fallback path (if qlib not available) runs a pure-Python weight-based loop with
identical semantics.

IS/OOS split:
    Supports in-sample/out-of-sample window specification.
    Walk-forward: roll IS/OOS window forward step_months at a time.

Output:
    BacktestResult dataclass containing:
        equity_curve     : Daily portfolio value
        daily_returns    : Daily portfolio returns
        weights_history  : Monthly weights at each rebalance
        signals_history  : Monthly composite z-scores
        regime_history   : Monthly regime labels
        costs_history    : Monthly transaction costs
        risk_flags       : Monthly risk control flags
        metrics          : Full performance metrics dict
"""

from __future__ import annotations

import logging
import sys
import io as _io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

# ---------------------------------------------------------------------------
# qlib infrastructure imports
# ---------------------------------------------------------------------------
_engine_stderr = sys.stderr
sys.stderr = _io.StringIO()
try:
    from qlib.backtest.decision import Order, OrderDir
    from qlib.backtest.account import Account
    from qlib.backtest.utils import CommonInfrastructure
    from qlib.backtest.backtest import backtest_loop
    _QLIB_BACKTEST_AVAILABLE = True
except Exception:
    _QLIB_BACKTEST_AVAILABLE = False

try:
    from qlib.backtest.profit_attribution import (
        decompose_portofolio_weight as _qlib_decompose_weight,
        decompose_portofolio as _qlib_decompose_portfolio,
    )
    _QLIB_ATTRIBUTION_AVAILABLE = True
except Exception:
    _QLIB_ATTRIBUTION_AVAILABLE = False

try:
    from qlib.contrib.evaluate import indicator_analysis as _qlib_indicator_analysis
    _QLIB_INDICATOR_AVAILABLE = True
except Exception:
    _QLIB_INDICATOR_AVAILABLE = False

try:
    from qlib.workflow.expm import MLflowExpManager
    from qlib.workflow import QlibRecorder
    _QLIB_WORKFLOW_AVAILABLE = True
except Exception:
    _QLIB_WORKFLOW_AVAILABLE = False

sys.stderr = _engine_stderr

from .costs import compute_transaction_costs, compute_daily_fee_drag
from .metrics import compute_metrics, subperiod_analysis, find_drawdown_episodes
from ..data.loader import load_all, load_returns, load_config
from ..data.universe import get_tickers, UNIVERSE_START
from ..portfolio.optimizer import optimize_weights
from ..portfolio.rebalance import (
    get_monthly_rebalance_dates,
    apply_zscore_threshold_filter,
    compute_turnover,
    cap_turnover,
    should_emergency_rebalance,
)
from ..portfolio.risk import apply_risk_controls
from ..signals.composite import compute_composite_signals

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backtest result dataclass
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    """Container for all backtest outputs."""
    equity_curve: pd.Series            # Daily portfolio value (USD)
    daily_returns: pd.Series           # Daily portfolio returns (simple)
    weights_history: pd.DataFrame      # Monthly weights at rebalance (index=date, cols=tickers)
    signals_history: pd.DataFrame      # Monthly composite z-scores
    regime_history: pd.Series          # Monthly regime labels
    costs_history: pd.DataFrame        # Monthly transaction costs
    risk_flags: List[dict]             # Per-rebalance risk flag records
    metrics: Dict                      # Full performance metrics
    subperiod_metrics: pd.DataFrame    # Subperiod breakdown
    drawdown_episodes: pd.DataFrame    # Top 5 worst drawdowns
    config: dict                       # Config used for this run
    # Optional benchmark comparison
    benchmark_returns: Optional[pd.Series] = None
    benchmark_equity: Optional[pd.Series] = None
    # Trade orders (qlib Order objects if available, else empty list)
    trade_orders: List = field(default_factory=list)
    # Sector attribution: dict with "group_weight" and "group_return" DataFrames
    # (from qlib decompose_portofolio — sector-level weight + return decomposition)
    attribution: Optional[dict] = None
    # Trade execution quality from qlib indicator_analysis (pa, pos, ffr)
    trade_indicators: Optional[pd.DataFrame] = None
    # qlib Account turnover tracking: columns [total_turnover, turnover]
    qlib_turnover: Optional[pd.DataFrame] = None

    def summary(self) -> str:
        """Print a one-page performance summary."""
        m = self.metrics
        lines = [
            "=" * 60,
            "SECTOR ROTATION BACKTEST SUMMARY",
            "=" * 60,
            f"Period  : {self.equity_curve.index[0].date()} → {self.equity_curve.index[-1].date()}",
            f"Capital : ${self.equity_curve.iloc[0]:,.0f} → ${self.equity_curve.iloc[-1]:,.0f}",
            "",
            f"{'Metric':<30} {'Strategy':>12} {'Benchmark':>12}",
            "-" * 56,
            f"{'Total Return':<30} {m.get('total_return', float('nan')):>11.1%}",
            f"{'CAGR':<30} {m.get('annual_return', float('nan')):>11.1%}",
            f"{'Annualized Vol':<30} {m.get('annual_vol', float('nan')):>11.1%}",
            f"{'Sharpe Ratio':<30} {m.get('sharpe', float('nan')):>11.3f}",
            f"{'Calmar Ratio':<30} {m.get('calmar', float('nan')):>11.3f}",
            f"{'Max Drawdown':<30} {m.get('max_drawdown', float('nan')):>11.1%}",
            f"{'CVaR 95%':<30} {m.get('cvar_95', float('nan')):>11.3%}",
            f"{'Monthly Win Rate':<30} {m.get('monthly_win_rate', float('nan')):>11.1%}",
            f"{'Info Ratio vs SPY':<30} {m.get('info_ratio', float('nan')):>11.3f}",
            f"{'Active Return':<30} {m.get('active_return', float('nan')):>11.1%}",
            "=" * 60,
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core backtest engine
# ---------------------------------------------------------------------------

class SectorRotationBacktest:
    """
    Monthly sector rotation backtest engine.

    Primary execution uses qlib's full infrastructure:
        Exchange → Account/Position → TradeCalendarManager →
        WeightStrategyBase → SimulatorExecutor → backtest_loop

    Usage:
        bt = SectorRotationBacktest(config)
        result = bt.run()
        print(result.summary())
    """

    def __init__(self, config: dict, mlflow_experiment: str = "sector_rotation_backtest"):
        self.cfg = config
        self.bt_cfg = config.get("backtest", {})
        self.sig_cfg = config.get("signals", {})
        self.port_cfg = config.get("portfolio", {})
        self.reb_cfg = config.get("rebalance", {})
        self.risk_cfg = config.get("risk", {})
        self.cost_cfg = config.get("costs", {})
        self._mlflow_experiment = mlflow_experiment

    def run(
        self,
        prices: Optional[pd.DataFrame] = None,
        macro: Optional[pd.DataFrame] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        oos_only: bool = False,
    ) -> BacktestResult:
        """
        Run the full backtest.

        Parameters
        ----------
        prices : pd.DataFrame, optional
            Pre-loaded price data. If None, loaded from config.
        macro : pd.DataFrame, optional
            Pre-loaded macro data. If None, loaded from config.
        start : str, optional
            Override backtest start date.
        end : str, optional
            Override backtest end date.
        oos_only : bool
            If True, skip IS warmup period (for walk-forward).
        """
        # ---------------------------------------------------------------
        # 1. Load data
        # ---------------------------------------------------------------
        if prices is None or macro is None:
            logger.info("Loading price and macro data...")
            prices, macro = load_all(config=self.cfg)

        bt_start = start or self.bt_cfg.get("start_date", str(UNIVERSE_START))
        bt_end = end or self.bt_cfg.get("end_date") or prices.index[-1].strftime("%Y-%m-%d")
        initial_capital = self.bt_cfg.get("initial_capital", 1_000_000.0)

        universe_cfg = self.cfg.get("universe", {})
        etf_tickers = universe_cfg.get("etfs", get_tickers(include_benchmark=False))
        benchmark_ticker = universe_cfg.get("benchmark", "SPY")

        etf_prices = prices[[t for t in etf_tickers if t in prices.columns]]
        bench_prices = prices[[benchmark_ticker]] if benchmark_ticker in prices.columns else None

        # ---------------------------------------------------------------
        # 2. Compute all signals (full history)
        # ---------------------------------------------------------------
        logger.info("Computing composite signals for full history...")
        sig_weights = self.sig_cfg.get("weights")
        regime_method = self.sig_cfg.get("regime", {}).get("method", "rules")
        regime_kwargs = {
            k: v for k, v in self.sig_cfg.get("regime", {}).items()
            if k not in ("method", "regime_weights", "defensive_sectors", "defensive_bonus_risk_off")
        }

        # value_source: "constituents" builds TTM P/E from yfinance quarterly earnings;
        #               "proxy" is the price-based fallback (used in tests / offline).
        value_source = self.sig_cfg.get("value_source", "constituents")
        value_cache_dir = self.cfg.get("data", {}).get("cache_dir")

        # Build signal_kwargs for new bonus signals
        stm_cfg = self.sig_cfg.get("short_term_momentum", {})
        erm_cfg = self.sig_cfg.get("earnings_revision", {})
        rsb_cfg = self.sig_cfg.get("relative_strength_breakout", {})
        signal_kwargs = {
            "signal_version": self.sig_cfg.get("signal_version", "v1"),
            "stm_enabled": stm_cfg.get("enabled", False),
            "stm_lookback": stm_cfg.get("lookback_months", 6),
            "stm_skip": stm_cfg.get("skip_months", 1),
            "stm_zscore_window": stm_cfg.get("zscore_window", 24),
            "erm_enabled": erm_cfg.get("enabled", False),
            "erm_lookback_quarters": erm_cfg.get("lookback_quarters", 4),
            "rsb_enabled": rsb_cfg.get("enabled", False),
            "rsb_lookback_days": rsb_cfg.get("lookback_days", 63),
        }

        # Inject bonus weights into signal weights dict
        if sig_weights is None:
            sig_weights = {}
        sig_weights.setdefault("short_term_momentum_bonus",
                               stm_cfg.get("weight_bonus", 0.0))
        sig_weights.setdefault("earnings_revision_bonus",
                               erm_cfg.get("weight_bonus", 0.0))
        sig_weights.setdefault("rs_breakout_bonus",
                               rsb_cfg.get("weight_bonus", 0.0))

        # Benchmark prices for RS breakout signal
        bench_series = bench_prices.iloc[:, 0] if bench_prices is not None else None

        composite, regime_monthly, components = compute_composite_signals(
            etf_prices,
            macro,
            weights=sig_weights,
            regime_method=regime_method,
            value_source=value_source,
            value_cache_dir=value_cache_dir,
            regime_kwargs=regime_kwargs,
            signal_kwargs=signal_kwargs,
            benchmark_prices=bench_series,
        )

        # ---------------------------------------------------------------
        # 3. Get rebalance schedule
        # ---------------------------------------------------------------
        rebalance_dates = get_monthly_rebalance_dates(bt_start, bt_end)
        logger.info(f"Backtest: {bt_start} → {bt_end}, {len(rebalance_dates)} rebalance dates")

        # Daily returns (pre-computed for both paths)
        daily_ret = load_returns(prices)
        etf_daily_ret = daily_ret[[t for t in etf_tickers if t in daily_ret.columns]]
        bench_daily_ret = daily_ret[[benchmark_ticker]] if benchmark_ticker in daily_ret.columns else None

        # ---------------------------------------------------------------
        # 4. Run backtest: qlib path (primary) → native loop (fallback)
        # ---------------------------------------------------------------
        result: Optional[BacktestResult] = None
        if _QLIB_BACKTEST_AVAILABLE:
            try:
                result = self._run_qlib(
                    prices=prices,
                    macro=macro,
                    etf_prices=etf_prices,
                    bench_prices=bench_prices,
                    etf_tickers=etf_tickers,
                    benchmark_ticker=benchmark_ticker,
                    composite=composite,
                    regime_monthly=regime_monthly,
                    rebalance_dates=rebalance_dates,
                    bt_start=bt_start,
                    bt_end=bt_end,
                    initial_capital=initial_capital,
                    bench_daily_ret=bench_daily_ret,
                    etf_daily_ret=etf_daily_ret,
                )
            except Exception as _qlib_err:
                logger.warning(
                    f"qlib backtest execution failed ({_qlib_err}), "
                    f"falling back to native loop"
                )

        if result is None:
            result = self._run_native(
                prices=prices,
                macro=macro,
                etf_prices=etf_prices,
                bench_prices=bench_prices,
                etf_tickers=etf_tickers,
                benchmark_ticker=benchmark_ticker,
                composite=composite,
                regime_monthly=regime_monthly,
                rebalance_dates=rebalance_dates,
                bt_start=bt_start,
                bt_end=bt_end,
                initial_capital=initial_capital,
                bench_daily_ret=bench_daily_ret,
                etf_daily_ret=etf_daily_ret,
                signal_kwargs=signal_kwargs,
            )

        # ---------------------------------------------------------------
        # 5. Record experiment to qlib.workflow (MLflow backend)
        # ---------------------------------------------------------------
        self._record_experiment(result, bt_start, bt_end)

        return result

    # -----------------------------------------------------------------------
    # qlib-backed execution path
    # -----------------------------------------------------------------------

    def _run_qlib(
        self,
        prices: pd.DataFrame,
        macro: pd.DataFrame,
        etf_prices: pd.DataFrame,
        bench_prices: Optional[pd.DataFrame],
        etf_tickers: List[str],
        benchmark_ticker: str,
        composite: pd.DataFrame,
        regime_monthly: pd.Series,
        rebalance_dates: List,
        bt_start: str,
        bt_end: str,
        initial_capital: float,
        bench_daily_ret: Optional[pd.DataFrame],
        etf_daily_ret: pd.DataFrame,
    ) -> BacktestResult:
        """
        Full qlib infrastructure backtest:
            SectorETFExchange + Account/Position + USTradeCalendarManager +
            SectorRotationWeightStrategy + SectorSimulatorExecutor + backtest_loop
        """
        from .qlib_adapter import SectorETFExchange, SectorSimulatorExecutor
        from ..portfolio.strategy import SectorRotationWeightStrategy

        # --- 1. Exchange: inject yfinance prices into qlib Exchange ---
        exchange = SectorETFExchange(
            prices=etf_prices.loc[bt_start:bt_end],
            open_cost=self.cost_cfg.get("transaction_cost_bps", 5) / 10000,
            close_cost=self.cost_cfg.get("transaction_cost_bps", 5) / 10000,
            min_cost=0.0,
            impact_cost=self.cost_cfg.get("impact_cost_bps", 0) / 10000,
        )

        # --- 2. Account + Position: qlib portfolio state tracker ---
        # Pass benchmark as pd.Series to avoid D.features() call
        bench_ret_series: Optional[pd.Series] = None
        if bench_daily_ret is not None and benchmark_ticker in bench_daily_ret.columns:
            bench_ret_series = bench_daily_ret[benchmark_ticker].loc[bt_start:bt_end]

        account = Account(
            init_cash=initial_capital,
            position_dict={},           # start all-cash
            freq="day",
            benchmark_config={"benchmark": bench_ret_series},  # None → skip benchmark
            port_metr_enabled=True,     # enables hist_positions tracking
        )

        # --- 3. CommonInfrastructure: links Account + Exchange ---
        common_infra = CommonInfrastructure(
            trade_account=account,
            trade_exchange=exchange,
        )

        # --- 4. SectorRotationWeightStrategy (WeightStrategyBase) ---
        #        Wraps our signal→optimizer→risk-control pipeline in qlib's strategy hierarchy
        strategy = SectorRotationWeightStrategy(
            composite_signals=composite,
            etf_prices=etf_prices,
            macro=macro,
            rebalance_dates=set(rebalance_dates),
            port_cfg=self.port_cfg,
            reb_cfg=self.reb_cfg,
            risk_cfg=self.risk_cfg,
            cost_cfg=self.cost_cfg,
            initial_capital=initial_capital,
            common_infra=common_infra,
        )

        # --- 5. SectorSimulatorExecutor (SimulatorExecutor / BaseExecutor) ---
        #        Uses USTradeCalendarManager (TradeCalendarManager) for US NYSE dates
        trading_dates = list(etf_prices.loc[bt_start:bt_end].index)
        executor = SectorSimulatorExecutor(
            time_per_step="day",
            start_time=bt_start,
            end_time=bt_end,
            generate_portfolio_metrics=True,    # enables portfolio_df output
            common_infra=common_infra,
            trading_dates=trading_dates,
        )

        # --- 6. backtest_loop: qlib's main execution loop ---
        logger.info("Running qlib backtest_loop ...")
        portfolio_dict, indicator_dict = backtest_loop(bt_start, bt_end, strategy, executor)

        # --- 7. Extract results from qlib Account ---
        #        get_portfolio_metrics() → (portfolio_df, hist_positions)
        portfolio_df, hist_positions = account.get_portfolio_metrics()

        # equity_curve: daily account total value from Position mark-to-market
        equity_curve: pd.Series = portfolio_df["account"].rename("portfolio")
        # daily_returns: portfolio return rate per day (pre-cost gross return)
        daily_returns: pd.Series = portfolio_df["return"].rename("portfolio")

        # qlib Account turnover (portfolio_df["total_turnover"] / "turnover")
        qlib_turnover: Optional[pd.DataFrame] = None
        turnover_cols = [c for c in ["total_turnover", "turnover"] if c in portfolio_df.columns]
        if turnover_cols:
            qlib_turnover = portfolio_df[turnover_cols].copy()

        # Extract indicator_df from indicator_dict for qlib indicator_analysis
        # indicator_dict: {freq_key → (indicator_df, indicator_obj)}
        indicator_df: Optional[pd.DataFrame] = None
        if indicator_dict:
            for _key, (_ind_df, _ind_obj) in indicator_dict.items():
                if _ind_df is not None and not _ind_df.empty:
                    indicator_df = _ind_df
                    break

        # --- 8. Build BacktestResult from qlib data + strategy tracking ---
        return self._assemble_result(
            equity_curve=equity_curve,
            daily_returns=daily_returns,
            weights_records=strategy.weights_records,
            scores_records=strategy.scores_records,
            costs_records=strategy.costs_records,
            risk_flags_records=strategy.risk_flags_records,
            regime_monthly=regime_monthly,
            bt_start=bt_start,
            bt_end=bt_end,
            initial_capital=initial_capital,
            bench_daily_ret=bench_daily_ret,
            benchmark_ticker=benchmark_ticker,
            hist_positions=hist_positions,
            etf_tickers=etf_tickers,
            etf_daily_ret=etf_daily_ret,
            portfolio_df_qlib=portfolio_df,
            indicator_df=indicator_df,
            qlib_turnover=qlib_turnover,
        )

    # -----------------------------------------------------------------------
    # Native (non-qlib) fallback execution path
    # -----------------------------------------------------------------------

    def _run_native(
        self,
        prices: pd.DataFrame,
        macro: pd.DataFrame,
        etf_prices: pd.DataFrame,
        bench_prices: Optional[pd.DataFrame],
        etf_tickers: List[str],
        benchmark_ticker: str,
        composite: pd.DataFrame,
        regime_monthly: pd.Series,
        rebalance_dates: List,
        bt_start: str,
        bt_end: str,
        initial_capital: float,
        bench_daily_ret: Optional[pd.DataFrame],
        etf_daily_ret: pd.DataFrame,
        signal_kwargs: Optional[dict] = None,
    ) -> BacktestResult:
        """
        Pure-Python weight-based backtest (fallback when qlib unavailable).
        Uses qlib Order/OrderDir for trade representation if available.
        """
        signal_kwargs = signal_kwargs or {}
        bench_series = bench_prices.iloc[:, 0] if bench_prices is not None else None
        portfolio_value = initial_capital
        current_weights = pd.Series(0.0, index=etf_tickers)
        prev_scores = pd.Series(0.0, index=etf_tickers)

        all_dates = prices.loc[bt_start:bt_end].index
        equity_curve = pd.Series(index=all_dates, dtype=float)
        daily_returns_list = []

        weights_records = {}
        scores_records = {}
        costs_records = []
        risk_flags_records = []
        trade_orders_list = []

        portfolio_daily_returns = pd.Series(dtype=float)
        equity_level = initial_capital

        rebalance_date_set = set(rebalance_dates)
        emergency_active = False           # cooldown state: True = already in emergency mode
        vix_threshold    = self.reb_cfg.get("emergency_derisk_vix", 35.0)
        vix_recovery     = vix_threshold * self.reb_cfg.get("vix_recovery_factor", 0.80)

        for dt in all_dates:
            # Update emergency_active state: clear when VIX recovers
            if emergency_active and "vix" in macro.columns and dt in macro.index:
                current_vix = float(macro.loc[dt, "vix"]) if not pd.isna(macro.loc[dt, "vix"]) else vix_threshold
                if current_vix < vix_recovery:
                    emergency_active = False

            trigger_emergency = should_emergency_rebalance(
                macro.loc[:dt] if dt in macro.index else macro,
                current_weights,
                vix_threshold=vix_threshold,
                emergency_active=emergency_active,
            )
            if trigger_emergency:
                emergency_active = True

            if dt in rebalance_date_set or trigger_emergency:
                avail_scores = composite.loc[:dt].dropna(how="all")
                if not avail_scores.empty:
                    latest_scores = avail_scores.iloc[-1]
                    scores_records[dt] = latest_scores.to_dict()

                    hist_ret = etf_daily_ret.loc[:dt].iloc[
                        -self.port_cfg.get("cov", {}).get("lookback_days", 252):
                    ]
                    proposed_weights = optimize_weights(
                        scores=latest_scores,
                        returns=hist_ret,
                        method=self.port_cfg.get("optimizer", "inv_vol"),
                        cov_method=self.port_cfg.get("cov", {}).get("method", "ledoit_wolf"),
                        max_weight=self.port_cfg.get("constraints", {}).get("max_weight", 0.40),
                        min_weight=self.port_cfg.get("constraints", {}).get("min_weight", 0.00),
                        top_n=self.port_cfg.get("top_n_sectors", 4),
                        min_score=self.port_cfg.get("min_zscore", -0.5),
                    )

                    thresh = self.reb_cfg.get("zscore_change_threshold", 0.5)
                    filtered_weights, rebalanced, held = apply_zscore_threshold_filter(
                        new_scores=latest_scores,
                        prev_scores=prev_scores,
                        new_weights=proposed_weights,
                        prev_weights=current_weights,
                        threshold=thresh,
                    )

                    max_to = self.reb_cfg.get("max_monthly_turnover", 0.80)
                    filtered_weights = cap_turnover(filtered_weights, current_weights, max_to)

                    macro_slice = macro.loc[:dt] if dt in macro.index else macro
                    # equity_curve is a pd.Series of portfolio values; dropna() gives
                    # only the completed trading days prior to today's rebalance.
                    ec_so_far = equity_curve.dropna()
                    prog_cfg = self.risk_cfg.get("vix_progressive_derisk", {})
                    prog_tiers = (
                        prog_cfg.get("tiers", [])
                        if prog_cfg.get("enabled", False)
                        else []
                    )
                    adj_weights, cash_pct, flags = apply_risk_controls(
                        weights=filtered_weights,
                        portfolio_returns=portfolio_daily_returns.iloc[-252:] if len(portfolio_daily_returns) > 0 else pd.Series(dtype=float),
                        macro=macro_slice,
                        equity_curve=ec_so_far if len(ec_so_far) > 0 else None,
                        vol_target=self.risk_cfg.get("vol_scaling", {}).get("target_vol_annual", 0.12),
                        vol_scaling_enabled=self.risk_cfg.get("vol_scaling", {}).get("enabled", True),
                        vix_emergency_threshold=self.reb_cfg.get("emergency_derisk_vix", 35.0),
                        emergency_cash_pct=self.reb_cfg.get("emergency_cash_pct", 0.50),
                        dd_halve_threshold=self.risk_cfg.get("drawdown", {}).get("cumulative_dd_halve", -0.15),
                        max_weight=self.port_cfg.get("constraints", {}).get("max_weight", 0.40),
                        vix_progressive_tiers=prog_tiers,
                    )

                    # ── Risk Overlay (v2): entry/exit gate + optional market/DD multipliers
                    if signal_kwargs.get("signal_version") == "v2":
                        risk_overlay_cfg = self.cfg.get("risk_overlay", {})
                        if risk_overlay_cfg.get("enabled", True):
                            from ..signals.risk_overlay import apply_risk_overlay
                            adj_weights = apply_risk_overlay(
                                target_weights=adj_weights,
                                sector_prices=etf_prices,
                                benchmark_prices=bench_series,
                                portfolio_equity=ec_so_far if len(ec_so_far) > 0 else None,
                                vix=macro.get("vix") if "vix" in macro.columns else None,
                                rebalance_date=dt,
                                config=risk_overlay_cfg,
                            )

                    cost_result = compute_transaction_costs(
                        current_weights, adj_weights, portfolio_value
                    )
                    costs_records.append({"date": dt, **cost_result})
                    portfolio_value -= cost_result["total_cost_usd"]

                    # qlib Order objects for trade representation
                    if _QLIB_BACKTEST_AVAILABLE:
                        for ticker in etf_tickers:
                            old_w = float(current_weights.get(ticker, 0.0))
                            new_w = float(adj_weights.get(ticker, 0.0))
                            delta_w = new_w - old_w
                            if abs(delta_w) > 1e-4:
                                trade_usd = abs(delta_w) * portfolio_value
                                trade_orders_list.append(Order(
                                    stock_id=ticker,
                                    amount=trade_usd,
                                    direction=OrderDir.BUY if delta_w > 0 else OrderDir.SELL,
                                    start_time=pd.Timestamp(dt),
                                    end_time=pd.Timestamp(dt),
                                ))

                    current_weights = adj_weights
                    prev_scores = latest_scores.copy()
                    weights_records[dt] = current_weights.to_dict()
                    risk_flags_records.append({"date": dt, **flags.to_dict()})

            # Daily mark-to-market
            if dt in etf_daily_ret.index:
                sector_ret = etf_daily_ret.loc[dt]
                port_ret = float(
                    (current_weights * sector_ret.reindex(current_weights.index, fill_value=0.0)).sum()
                )
            else:
                port_ret = 0.0

            fee_drag = compute_daily_fee_drag(
                current_weights, portfolio_value,
                annual_fee_bps=self.cost_cfg.get("etf_fee_bps", 9)
            )
            portfolio_value = portfolio_value * (1 + port_ret) - fee_drag
            equity_level = portfolio_value
            equity_curve[dt] = portfolio_value
            daily_returns_list.append((dt, port_ret))

            _new = pd.Series([port_ret], index=[dt])
            portfolio_daily_returns = pd.concat(
                [s for s in [portfolio_daily_returns, _new] if not s.empty]
            )

        equity_curve = equity_curve.dropna()
        daily_returns = pd.Series(
            [r for _, r in daily_returns_list],
            index=[d for d, _ in daily_returns_list],
            name="portfolio",
        )

        return self._assemble_result(
            equity_curve=equity_curve,
            daily_returns=daily_returns,
            weights_records=weights_records,
            scores_records=scores_records,
            costs_records=costs_records,
            risk_flags_records=risk_flags_records,
            regime_monthly=regime_monthly,
            bt_start=bt_start,
            bt_end=bt_end,
            initial_capital=initial_capital,
            bench_daily_ret=bench_daily_ret,
            benchmark_ticker=benchmark_ticker,
            hist_positions=None,
            etf_tickers=etf_tickers,
            etf_daily_ret=etf_daily_ret,
            portfolio_df_qlib=None,
            trade_orders=trade_orders_list,
        )

    # -----------------------------------------------------------------------
    # Common result assembly
    # -----------------------------------------------------------------------

    def _assemble_result(
        self,
        equity_curve: pd.Series,
        daily_returns: pd.Series,
        weights_records: dict,
        scores_records: dict,
        costs_records: List[dict],
        risk_flags_records: List[dict],
        regime_monthly: pd.Series,
        bt_start: str,
        bt_end: str,
        initial_capital: float,
        bench_daily_ret: Optional[pd.DataFrame],
        benchmark_ticker: str,
        hist_positions: Optional[dict],
        etf_tickers: List[str],
        etf_daily_ret: Optional[pd.DataFrame] = None,
        portfolio_df_qlib: Optional[pd.DataFrame] = None,
        indicator_df: Optional[pd.DataFrame] = None,
        qlib_turnover: Optional[pd.DataFrame] = None,
        trade_orders: Optional[List] = None,
    ) -> BacktestResult:
        """Build BacktestResult from execution tracking data."""
        if trade_orders is None:
            trade_orders = []

        weights_history = pd.DataFrame(weights_records).T
        weights_history.index.name = "date"
        if not weights_history.empty:
            weights_history = weights_history.fillna(0.0)

        signals_history = pd.DataFrame(scores_records).T
        signals_history.index.name = "date"

        costs_df = pd.DataFrame(costs_records).set_index("date") if costs_records else pd.DataFrame()
        regime_history = regime_monthly.loc[bt_start:bt_end]

        # Benchmark
        bench_returns: Optional[pd.Series] = None
        bench_equity: Optional[pd.Series] = None
        if bench_daily_ret is not None and benchmark_ticker in bench_daily_ret.columns:
            bench_returns = bench_daily_ret[benchmark_ticker].loc[bt_start:bt_end]
            bench_equity = (1 + bench_returns).cumprod() * initial_capital

        # Performance metrics (use qlib evaluate functions via compute_metrics)
        metrics = compute_metrics(daily_returns, bench_returns)
        sub_metrics = subperiod_analysis(daily_returns, bench_returns)
        dd_episodes = find_drawdown_episodes(daily_returns)

        # Sector attribution via qlib decompose_portofolio (weight + return decomposition)
        attribution: Optional[dict] = None
        if _QLIB_ATTRIBUTION_AVAILABLE and hist_positions and not weights_history.empty:
            try:
                attribution = self._compute_attribution(
                    hist_positions=hist_positions,
                    etf_tickers=etf_tickers,
                    etf_daily_ret=etf_daily_ret,
                )
            except Exception as _e:
                logger.debug(f"Attribution skipped: {_e}")

        # Trade execution quality via qlib indicator_analysis (pa, pos, ffr)
        trade_indicators: Optional[pd.DataFrame] = None
        if _QLIB_INDICATOR_AVAILABLE and indicator_df is not None:
            try:
                trade_indicators = _qlib_indicator_analysis(indicator_df)
            except Exception as _e:
                logger.debug(f"indicator_analysis skipped: {_e}")

        result = BacktestResult(
            equity_curve=equity_curve,
            daily_returns=daily_returns,
            weights_history=weights_history,
            signals_history=signals_history,
            regime_history=regime_history,
            costs_history=costs_df,
            risk_flags=risk_flags_records,
            metrics=metrics,
            subperiod_metrics=sub_metrics,
            drawdown_episodes=dd_episodes,
            config=self.cfg,
            benchmark_returns=bench_returns,
            benchmark_equity=bench_equity,
            trade_orders=trade_orders,
            attribution=attribution,
            trade_indicators=trade_indicators,
            qlib_turnover=qlib_turnover,
        )

        logger.info(f"\n{result.summary()}")
        return result

    def _compute_attribution(
        self,
        hist_positions: dict,
        etf_tickers: List[str],
        etf_daily_ret: Optional[pd.DataFrame] = None,
    ) -> Optional[dict]:
        """
        Compute sector-level weight and return decomposition using qlib's
        decompose_portofolio(stock_weight_df, stock_group_df, stock_ret_df).

        Each ETF is treated as its own sector group. Group IDs are integers
        (required by decompose_portofolio's np.isnan filter on group values).

        Parameters
        ----------
        hist_positions : dict
            qlib hist_positions from account.get_portfolio_metrics()
        etf_tickers : list of str
            ETF tickers in the universe
        etf_daily_ret : pd.DataFrame, optional
            Daily ETF returns (rows=dates, cols=tickers) for return decomposition

        Returns
        -------
        dict with:
            "group_weight" : DataFrame (dates × tickers) — daily sector weight
            "group_return" : DataFrame (dates × tickers) or None — daily sector return
        """
        # Build weight_df from qlib hist_positions (daily Position snapshots)
        rows = {}
        for dt, pos in hist_positions.items():
            try:
                w_dict = pos.get_stock_weight_dict(only_stock=False)
                rows[dt] = {t: w_dict.get(t, 0.0) for t in etf_tickers}
            except Exception:
                pass

        if not rows:
            return None

        weight_df = pd.DataFrame(rows).T.fillna(0.0)
        tickers_present = [t for t in etf_tickers if t in weight_df.columns]
        weight_df = weight_df[tickers_present].fillna(0.0)

        # Build stock_group_df: rows=dates, cols=tickers, values=numeric group ID
        # decompose_portofolio uses np.isnan() to filter groups — values MUST be float
        # Each ETF is its own sector group → unique float per ticker
        ticker_to_gid = {t: float(i) for i, t in enumerate(tickers_present)}
        stock_group_df = pd.DataFrame(
            {t: ticker_to_gid[t] for t in tickers_present},
            index=weight_df.index,
        )

        # Return decomposition via qlib decompose_portofolio (weight + return)
        group_ret_df: Optional[pd.DataFrame] = None
        if etf_daily_ret is not None:
            stock_ret_df = (
                etf_daily_ret[tickers_present]
                .reindex(weight_df.index)
                .fillna(0.0)
            )
            group_weight_df, group_ret_df = _qlib_decompose_portfolio(
                weight_df, stock_group_df, stock_ret_df
            )
            # Rename numeric group IDs back to ticker names for readability
            gid_to_ticker = {v: k for k, v in ticker_to_gid.items()}
            group_weight_df = group_weight_df.rename(columns=gid_to_ticker)
            group_ret_df = group_ret_df.rename(columns=gid_to_ticker)
        else:
            # Weight-only decomposition via qlib decompose_portofolio_weight
            group_weight_dict, _ = _qlib_decompose_weight(weight_df, stock_group_df)
            group_weight_df = pd.DataFrame(group_weight_dict)
            gid_to_ticker = {v: k for k, v in ticker_to_gid.items()}
            group_weight_df = group_weight_df.rename(columns=gid_to_ticker)

        return {
            "group_weight": group_weight_df,
            "group_return": group_ret_df,
        }

    def _record_experiment(
        self,
        result: "BacktestResult",
        bt_start: str,
        bt_end: str,
    ) -> None:
        """
        Record backtest results to qlib.workflow experiment store (MLflow backend).

        Uses QlibRecorder + MLflowExpManager directly with a local file URI,
        so no qlib.init() or Chinese data provider is required.

        Stores:
            params   : flattened config sections (portfolio, risk, rebalance, etc.)
            metrics  : sharpe, cagr, max_drawdown, calmar, annual_vol, info_ratio,
                       monthly_win_rate (NaN values skipped — mlflow rejects them)
            tags     : bt_start, bt_end, strategy label
        """
        if not _QLIB_WORKFLOW_AVAILABLE:
            return
        try:
            # MLflow 3.x deprecated the file:// backend (FileStore) unconditionally.
            # MlflowClient accepts sqlite:/// natively; data is stored in mlflow.db
            # in the same mlruns/ directory. No migration needed — first run creates it.
            mlruns_path = Path(__file__).parent.parent.parent.resolve() / "mlruns"
            mlruns_path.mkdir(parents=True, exist_ok=True)
            uri = f"sqlite:///{mlruns_path}/mlflow.db"
            exp_manager = MLflowExpManager(uri=uri, default_exp_name="sector_rotation")
            recorder_client = QlibRecorder(exp_manager)

            with recorder_client.start(experiment_name=self._mlflow_experiment):
                # Log config as params (flatten each config section)
                flat_params: dict = {}
                for section, vals in result.config.items():
                    if isinstance(vals, dict):
                        for k, v in vals.items():
                            flat_params[f"{section}.{k}"] = str(v)[:500]
                    else:
                        flat_params[section] = str(vals)[:500]
                recorder_client.log_params(**flat_params)

                # Log key performance metrics (skip NaN — mlflow rejects them)
                m = result.metrics
                metric_vals = {
                    "sharpe":           m.get("sharpe"),
                    "cagr":             m.get("annual_return"),
                    "max_drawdown":     m.get("max_drawdown"),
                    "calmar":           m.get("calmar"),
                    "annual_vol":       m.get("annual_vol"),
                    "info_ratio":       m.get("info_ratio"),
                    "monthly_win_rate": m.get("monthly_win_rate"),
                }
                clean_metrics = {
                    k: float(v)
                    for k, v in metric_vals.items()
                    if v is not None and not (isinstance(v, float) and np.isnan(v))
                }
                if clean_metrics:
                    recorder_client.log_metrics(**clean_metrics)

                # Tag start/end dates and strategy label
                recorder_client.set_tags(
                    bt_start=bt_start,
                    bt_end=bt_end,
                    strategy="sector_rotation",
                )

            logger.debug(f"qlib.workflow run recorded to {uri}")
        except Exception as _e:
            logger.debug(f"qlib.workflow recording skipped: {_e}")


# ---------------------------------------------------------------------------
# Walk-forward runner
# ---------------------------------------------------------------------------

def run_walk_forward(
    config: dict,
    prices: pd.DataFrame,
    macro: pd.DataFrame,
    is_years: int = 3,
    oos_months: int = 12,
    step_months: int = 6,
) -> List[BacktestResult]:
    """
    Run rolling IS/OOS walk-forward backtests.

    Parameters
    ----------
    config : dict
        Full config.
    prices, macro : pd.DataFrame
        Pre-loaded data.
    is_years : int
        In-sample window length (years).
    oos_months : int
        Out-of-sample evaluation period (months).
    step_months : int
        Advance the window by this many months each fold.

    Returns
    -------
    list of BacktestResult
        One per OOS fold.
    """
    bt_cfg = config.get("backtest", {})
    full_start = pd.Timestamp(bt_cfg.get("start_date", str(UNIVERSE_START)))
    full_end = pd.Timestamp(bt_cfg.get("end_date") or prices.index[-1].strftime("%Y-%m-%d"))

    results = []
    oos_start = full_start + pd.DateOffset(years=is_years)

    fold = 0
    while oos_start < full_end:
        oos_end = oos_start + pd.DateOffset(months=oos_months)
        if oos_end > full_end:
            oos_end = full_end

        logger.info(
            f"Walk-forward fold {fold + 1}: "
            f"IS={full_start.date()} → {oos_start.date()}, "
            f"OOS={oos_start.date()} → {oos_end.date()}"
        )

        engine = SectorRotationBacktest(config)
        result = engine.run(
            prices=prices,
            macro=macro,
            start=oos_start.strftime("%Y-%m-%d"),
            end=oos_end.strftime("%Y-%m-%d"),
        )
        result.config["_fold"] = fold
        result.config["_oos_start"] = str(oos_start.date())
        result.config["_oos_end"] = str(oos_end.date())
        results.append(result)

        oos_start += pd.DateOffset(months=step_months)
        fold += 1

    logger.info(f"Walk-forward complete: {len(results)} OOS folds")
    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Run sector rotation backtest")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--start", default=None, help="Override backtest start date")
    parser.add_argument("--end", default=None, help="Override backtest end date")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Run full walk-forward IS/OOS analysis (all 59 param sets)")
    parser.add_argument("--wf-mode", default="both", choices=["anchored", "rolling", "both"],
                        help="Walk-forward IS mode (default: both)")
    parser.add_argument("--wf-step-days", type=int, default=15,
                        help="WF step size in trading days (default: 10 ≈ 2 weeks)")
    parser.add_argument("--wf-oos-months", type=int, default=6,
                        help="WF OOS window in months (default: 12)")
    parser.add_argument("--force-refresh", action="store_true", help="Skip data cache")
    parser.add_argument(
        "--param-set", default="selected", metavar="NAME",
        help=(
            "Named param set to apply on top of config.yaml. "
            "Default='selected' reads selected_param_set.json. "
            "Use 'none' to run config.yaml defaults as-is. "
            "Use 'list' to print all 59 names and exit."
        ),
    )
    args = parser.parse_args()

    import json
    import sys
    from pathlib import Path
    from sector_rotation.SectorRotationStrategyRuns import PARAM_SETS, apply_param_set

    if args.param_set == "list":
        print("Available param sets:")
        for name in sorted(PARAM_SETS):
            print(f"  {name}")
        sys.exit(0)

    # base_cfg: used for load_all (data/universe keys — never changed by param sets)
    # active_cfg: used for SectorRotationBacktest (strategy keys — overridden by param set)
    # This mirrors batch/select/tearsheet exactly: load_all always sees base_cfg.
    base_cfg = load_config(Path(args.config) if args.config else None)
    active_cfg = base_cfg

    # ── Resolve param set ────────────────────────────────────────────────────
    _sel_json = Path(__file__).parent.parent / "selected_param_set.json"

    if args.param_set == "none":
        print("Using config.yaml defaults (no param set applied)")
    elif args.param_set == "selected":
        if _sel_json.exists():
            with open(_sel_json) as _f:
                _sel = json.load(_f)
            _ps_name = _sel.get("param_set")
            if _ps_name and _ps_name in PARAM_SETS:
                active_cfg = apply_param_set(base_cfg, PARAM_SETS[_ps_name])
                print(f"Using param set: {_ps_name} (from selected_param_set.json)")
            else:
                print(f"WARN: selected_param_set.json has unknown param set '{_ps_name}', using config.yaml defaults")
        else:
            print("WARN: selected_param_set.json not found, using config.yaml defaults")
    elif args.param_set in PARAM_SETS:
        active_cfg = apply_param_set(base_cfg, PARAM_SETS[args.param_set])
        print(f"Using param set: {args.param_set}")
    else:
        print(f"ERROR: unknown param set '{args.param_set}'. Use --param-set list to see all.", file=sys.stderr)
        sys.exit(1)

    # load_all uses base_cfg (data/universe settings) — same as batch/tearsheet
    prices, macro = load_all(config=base_cfg, force_refresh=args.force_refresh)

    if args.walk_forward:
        from sector_rotation.walk_forward import WalkForwardAnalyzer, run_dual_mode
        wf_kwargs = dict(
            is_years_min=base_cfg.get("backtest", {}).get("is_years", 3),
            oos_months=args.wf_oos_months,
            step_days=args.wf_step_days,
            embargo_days=5,
        )
        if args.wf_mode == "both":
            wf_results = run_dual_mode(base_cfg, prices, macro, **wf_kwargs)
            for mode_name, wf_r in wf_results.items():
                print(wf_r.summary())
        else:
            analyzer = WalkForwardAnalyzer(
                base_cfg=base_cfg, prices=prices, macro=macro,
                mode=args.wf_mode, **wf_kwargs,
            )
            wf_r = analyzer.run()
            print(wf_r.summary())
    else:
        engine = SectorRotationBacktest(active_cfg)
        result = engine.run(prices, macro, start=args.start, end=args.end)
        print(result.summary())
