"""Daily Backtest Engine — crypto perp rotation (Plan 00 §6, Plan 05 §8).

COPIED from qlib-main/sector_rotation/backtest/engine.py (read-only template),
NATIVE LOOP ONLY: the template ran qlib as primary with this pure-pandas loop
as its tested fallback; crypto_trading has no qlib (isolation invariant 4), so
the qlib branch, attribution, indicator-analysis, and MLflow recording are
REMOVED (not stubbed — per plan the native loop IS the engine) and replaced
with a JSON run-record. Everything else preserves the template's flow:

  daily loop → (emergency w/ cooldown | scheduled rebalance) →
  optimize_weights → z-threshold filter → turnover cap → risk controls →
  risk overlay → position tracker + stop-loss → transaction costs →
  daily mark-to-market → assemble result + metrics.

Crypto adaptations (plan §5 "Change" column):
  * Calendar: 24/7 daily UTC (no NYSE); rebalance daily|weekly (was monthly).
  * FUNDING ACCRUAL per held leg: daily portfolio funding return
    = Σ_i w_i × (−funding_day_i) — the per-notional form of
    crypto_common.costs.funding_payment (longs pay positive rates; the funding
    panel is the per-day SUM of 8h cycle rates).
  * Costs: bps-tier ETF model + expense-ratio drag → crypto model:
    traded_notional × (taker fee [zero|projected via costs.load_fee_rates]
    + slippage estimate bps). Funding replaces the daily fee drag.
  * Benchmarks: SPY → KXBTCPERP-HODL (primary) + equal-weight perp basket.
  * Emergency: VIX → btc_rvol; 365-day annualization via crypto metrics.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.backtest.metrics import (compute_metrics,
                                                           find_drawdown_episodes,
                                                           subperiod_analysis)
from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.costs import load_fee_rates
from crypto_trading.crypto_strategies.perp_rotation.data.loader import load_returns
from crypto_trading.crypto_strategies.perp_rotation.data.universe import (BENCHMARK_TICKER,
                                                                          get_universe)
from crypto_trading.crypto_strategies.perp_rotation.portfolio.optimizer import optimize_weights
from crypto_trading.crypto_strategies.perp_rotation.portfolio.rebalance import (
    apply_zscore_threshold_filter, cap_turnover, get_rebalance_dates,
    should_emergency_rebalance)
from crypto_trading.crypto_strategies.perp_rotation.portfolio.risk import apply_risk_controls
from crypto_trading.crypto_strategies.perp_rotation.signals.composite import (
    compute_composite_signals)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backtest result dataclass — template shape (qlib-only fields removed)
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    """Container for all backtest outputs (template contract preserved)."""
    equity_curve: pd.Series
    daily_returns: pd.Series
    weights_history: pd.DataFrame
    signals_history: pd.DataFrame
    regime_history: pd.Series
    costs_history: pd.DataFrame
    risk_flags: List[dict]
    metrics: Dict
    subperiod_metrics: pd.DataFrame
    drawdown_episodes: pd.DataFrame
    config: dict
    benchmark_returns: Optional[pd.Series] = None          # KXBTCPERP-HODL
    benchmark_equity: Optional[pd.Series] = None
    benchmark_ew_returns: Optional[pd.Series] = None       # EW perp basket (crypto addition)
    benchmark_ew_equity: Optional[pd.Series] = None
    trade_orders: List = field(default_factory=list)       # plain dicts (no qlib Order)
    stop_loss_events: Optional[List] = None
    position_states_history: Optional[Dict] = None
    funding_pnl_daily: Optional[pd.Series] = None          # crypto addition

    def summary(self) -> str:
        m = self.metrics
        lines = [
            "=" * 60,
            "PERP ROTATION BACKTEST SUMMARY",
            "=" * 60,
            f"Period  : {self.equity_curve.index[0].date()} → {self.equity_curve.index[-1].date()}",
            f"Capital : ${self.equity_curve.iloc[0]:,.0f} → ${self.equity_curve.iloc[-1]:,.0f}",
            "",
            f"{'Metric':<30} {'Strategy':>12}",
            "-" * 44,
            f"{'Total Return':<30} {m.get('total_return', float('nan')):>11.1%}",
            f"{'CAGR (365d)':<30} {m.get('annual_return', float('nan')):>11.1%}",
            f"{'Annualized Vol':<30} {m.get('annual_vol', float('nan')):>11.1%}",
            f"{'Sharpe Ratio':<30} {m.get('sharpe', float('nan')):>11.3f}",
            f"{'Calmar Ratio':<30} {m.get('calmar', float('nan')):>11.3f}",
            f"{'Max Drawdown':<30} {m.get('max_drawdown', float('nan')):>11.1%}",
            f"{'Info Ratio vs BTC-HODL':<30} {m.get('info_ratio', float('nan')):>11.3f}",
            f"{'Funding P&L (cum $)':<30} {m.get('funding_pnl_usd', float('nan')):>11.2f}",
            f"{'EW-basket total return':<30} {m.get('ew_total_return', float('nan')):>11.1%}",
            "=" * 60,
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core backtest engine — template class shape
# ---------------------------------------------------------------------------

class PerpRotationBacktest:
    """Daily/weekly perp rotation backtest (template: SectorRotationBacktest)."""

    def __init__(self, config: dict):
        self.cfg = config
        self.bt_cfg = config.get("backtest", {})
        self.sig_cfg = config.get("signals", {})
        self.port_cfg = config.get("portfolio", {})
        self.reb_cfg = config.get("rebalance", {})
        self.risk_cfg = config.get("risk", {})
        self.cost_cfg = config.get("costs", {})
        self.uni_cfg = config.get("universe", {})

    # ------------------------------------------------------------------
    def run(
        self,
        prices: pd.DataFrame,
        funding: pd.DataFrame,
        regime_inputs: pd.DataFrame,
        volumes_notional: Optional[pd.DataFrame] = None,
        oi_notional: Optional[pd.DataFrame] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> BacktestResult:
        """Run the full backtest over injected panels (data layer is external —
        template loaded via its own loader; panels come from
        perp_rotation.data.loader.build_perp_panel / proxy_panel)."""
        bt_start = start or self.bt_cfg.get("start_date") or str(prices.index[0].date())
        bt_end = end or self.bt_cfg.get("end_date") or str(prices.index[-1].date())
        initial_capital = self.bt_cfg.get("initial_capital", 1000.0)

        # Universe (PIT gate as of backtest end for the column set; the
        # listing floor re-applies per-date inside the loop via NaN scores)
        tickers, activated = get_universe(
            prices, volumes_notional, oi_notional,
            floor_days=self.uni_cfg.get("listing_history_floor_days", 30),
            min_daily_notional_usd=self.uni_cfg.get("depth_qualify", {})
                .get("min_daily_notional_usd", 100_000.0),
            min_perps_to_activate=self.uni_cfg.get("min_perps_to_activate", 6),
        )
        if not activated:
            logger.warning("activation gate NOT met — running scaffold/paper backtest anyway")
        if not tickers:
            raise ValueError("no tickers qualify — nothing to backtest")
        perp_prices = prices[tickers]

        # Signals (full history — template step 2). regime_multipliers /
        # defensive_bonus are optional config pass-throughs (research/regime
        # conditioning); absent → compute_composite_signals defaults, behavior
        # unchanged.
        _sig_extra = {}
        if self.sig_cfg.get("regime_multipliers") is not None:
            _sig_extra["regime_multipliers"] = self.sig_cfg["regime_multipliers"]
        if self.sig_cfg.get("defensive_bonus") is not None:
            _sig_extra["defensive_bonus"] = float(self.sig_cfg["defensive_bonus"])
        composite, regime_daily, components = compute_composite_signals(
            perp_prices,
            funding.reindex(columns=tickers).fillna(0.0),
            regime_inputs,
            weights=self.sig_cfg.get("weights"),
            regime_method=self.sig_cfg.get("regime", {}).get("method", "rules"),
            signal_kwargs=self.sig_cfg,
            **_sig_extra,
        )

        rebalance_dates = get_rebalance_dates(
            bt_start, bt_end, self.reb_cfg.get("frequency", "daily"))
        logger.info(f"Backtest: {bt_start} → {bt_end}, {len(rebalance_dates)} rebalance dates")

        daily_ret = load_returns(perp_prices)
        result = self._run_native(
            prices=prices, perp_prices=perp_prices, funding=funding,
            regime_inputs=regime_inputs, tickers=tickers, composite=composite,
            regime_daily=regime_daily, rebalance_dates=rebalance_dates,
            bt_start=bt_start, bt_end=bt_end, initial_capital=initial_capital,
            perp_daily_ret=daily_ret,
        )
        self._record_run(result, bt_start, bt_end)
        return result

    # ------------------------------------------------------------------
    # Crypto rebalance cost (replaces template compute_transaction_costs)
    # ------------------------------------------------------------------
    def _rebalance_cost(self, prev_w: pd.Series, new_w: pd.Series,
                        portfolio_value: float) -> dict:
        all_t = new_w.index.union(prev_w.index)
        traded = (new_w.reindex(all_t, fill_value=0.0)
                  - prev_w.reindex(all_t, fill_value=0.0)).abs().sum() * portfolio_value
        scenario = self.cost_cfg.get("fee_scenario", "projected")
        if scenario == "zero":
            fee_rate = 0.0
        else:
            _, fee_rate = load_fee_rates(BENCHMARK_TICKER)   # taker
        slip_bps = self.cost_cfg.get("slippage_bps", 5.0)
        cost = traded * (fee_rate + slip_bps / 1e4)
        return {"traded_notional_usd": float(traded), "total_cost_usd": float(cost),
                "fee_scenario": scenario}

    # ------------------------------------------------------------------
    # Native loop — template _run_native flow preserved
    # ------------------------------------------------------------------
    def _run_native(
        self,
        prices: pd.DataFrame,
        perp_prices: pd.DataFrame,
        funding: pd.DataFrame,
        regime_inputs: pd.DataFrame,
        tickers: List[str],
        composite: pd.DataFrame,
        regime_daily: pd.Series,
        rebalance_dates: List,
        bt_start: str,
        bt_end: str,
        initial_capital: float,
        perp_daily_ret: pd.DataFrame,
    ) -> BacktestResult:
        bench_series = (perp_prices[BENCHMARK_TICKER]
                        if BENCHMARK_TICKER in perp_prices.columns else None)
        # ── PIT alignment (2026-07-28 audit): a rebalance at row dt may only use
        # information through the PREVIOUS row's close. composite rows are
        # stamped the day their inputs complete (carry uses day-T funding,
        # low-vol/market-risk channels use day-T closes) and the same row's
        # return then accrues to the new weights — lag the whole signal frame.
        # Stops likewise trigger on the prior close, never the close they would
        # be dodging.
        composite = composite.shift(1)
        stop_prices = perp_prices.shift(1)
        stop_bench = bench_series.shift(1) if bench_series is not None else None
        portfolio_value = initial_capital
        current_weights = pd.Series(0.0, index=tickers)
        prev_scores = pd.Series(0.0, index=tickers)

        all_dates = perp_prices.loc[bt_start:bt_end].index
        equity_curve = pd.Series(index=all_dates, dtype=float)
        daily_returns_list = []
        funding_pnl_list = []

        weights_records = {}
        scores_records = {}
        costs_records = []
        risk_flags_records = []
        trade_orders_list = []

        portfolio_daily_returns = pd.Series(dtype=float)

        rebalance_date_set = set(rebalance_dates)
        emergency_active = False
        rvol_threshold = self.reb_cfg.get("emergency_derisk_rvol", 60.0)
        rvol_recovery = rvol_threshold * self.reb_cfg.get("rvol_recovery_factor", 0.80)

        # Stop-loss infrastructure — template verbatim
        _stop_loss_cfg = self.cfg.get("stop_loss", {})
        _stop_loss_events: List = []
        _position_states_history: Dict = {}
        _stop_loss_fn = None
        _position_tracker = None
        if _stop_loss_cfg.get("enabled", False):
            try:
                from crypto_trading.crypto_strategies.perp_rotation.portfolio.stop_loss import (
                    SectorPositionTracker, apply_position_stops)
                _stop_loss_fn = apply_position_stops
                _position_tracker = SectorPositionTracker()
            except ImportError:
                _stop_loss_cfg = {"enabled": False}

        funding_aligned = funding.reindex(all_dates).fillna(0.0)
        pending_cost_usd = 0.0

        for dt in all_dates:
            # emergency cooldown state — template verbatim (vix→rvol)
            if emergency_active and "btc_rvol" in regime_inputs.columns and dt in regime_inputs.index:
                v = regime_inputs.loc[dt, "btc_rvol"]
                current_rvol = float(v) if not pd.isna(v) else rvol_threshold
                if current_rvol < rvol_recovery:
                    emergency_active = False

            trigger_emergency = should_emergency_rebalance(
                regime_inputs.loc[:dt] if dt in regime_inputs.index else regime_inputs,
                current_weights,
                rvol_threshold=rvol_threshold,
                emergency_active=emergency_active,
            )
            if trigger_emergency:
                emergency_active = True

            if dt in rebalance_date_set or trigger_emergency:
                avail_scores = composite.loc[:dt].dropna(how="all")
                if not avail_scores.empty and self.port_cfg.get("long_short", False):
                    # ── LONG-SHORT branch (config-gated; long-only path below is
                    # untouched when portfolio.long_short is absent/false) ──
                    latest_scores = avail_scores.iloc[-1]
                    scores_records[dt] = latest_scores.to_dict()
                    from crypto_trading.crypto_strategies.perp_rotation.long_short import (
                        build_long_short_weights, ls_risk_scale)
                    ls_cfg = self.port_cfg.get("ls", {})
                    raw_ls = build_long_short_weights(
                        latest_scores, prev_weights=current_weights,
                        k=ls_cfg.get("k_per_side", 3),
                        gross=ls_cfg.get("gross", 1.0),
                        band=ls_cfg.get("rank_band", 0),
                        max_weight=self.port_cfg.get("constraints", {})
                            .get("max_weight", 0.45),
                    )
                    regime_slice = (regime_inputs.loc[:dt]
                                    if dt in regime_inputs.index else regime_inputs)
                    ec_so_far = equity_curve.dropna()
                    scale, flags = ls_risk_scale(
                        portfolio_daily_returns.iloc[-365:]
                            if len(portfolio_daily_returns) > 0 else pd.Series(dtype=float),
                        regime_slice,
                        ec_so_far if len(ec_so_far) > 0 else None,
                        vol_target=self.risk_cfg.get("target_vol_annual", 0.20),
                        vol_target_mode=self.risk_cfg.get("vol_target_mode", "absolute"),
                        rvol_emergency_threshold=rvol_threshold,
                        emergency_cash_pct=self.reb_cfg.get("emergency_cash_pct", 0.50),
                        dd_halve_threshold=self.risk_cfg.get("dd_halve_threshold", -0.12),
                        dd_flat_threshold=self.risk_cfg.get("dd_flat_threshold", -0.25),
                    )
                    adj_weights = raw_ls * scale

                    cost_result = self._rebalance_cost(current_weights, adj_weights,
                                                       portfolio_value)
                    costs_records.append({"date": dt, **cost_result})
                    pending_cost_usd += cost_result["total_cost_usd"]
                    for ticker in tickers:
                        delta_w = float(adj_weights.get(ticker, 0.0)) - \
                            float(current_weights.get(ticker, 0.0))
                        if abs(delta_w) > 1e-4:
                            trade_orders_list.append({
                                "date": str(dt), "ticker": ticker,
                                "side": "buy" if delta_w > 0 else "sell",
                                "notional_usd": abs(delta_w) * portfolio_value,
                            })
                    current_weights = adj_weights
                    prev_scores = latest_scores.copy()
                    weights_records[dt] = current_weights.to_dict()
                    risk_flags_records.append({"date": dt, **flags.to_dict()})
                elif not avail_scores.empty:
                    latest_scores = avail_scores.iloc[-1]
                    scores_records[dt] = latest_scores.to_dict()

                    hist_ret = perp_daily_ret.loc[:dt]
                    # PIT: row dt's return has not happened at rebalance time
                    hist_ret = hist_ret[hist_ret.index < dt].iloc[
                        -self.port_cfg.get("cov", {}).get("lookback_days", 365):
                    ]
                    proposed_weights = optimize_weights(
                        scores=latest_scores,
                        returns=hist_ret,
                        method=self.port_cfg.get("optimizer", "inv_vol"),
                        cov_method=self.port_cfg.get("cov", {}).get("method", "ledoit_wolf"),
                        min_periods=self.port_cfg.get("cov", {}).get("min_periods", 21),
                        max_weight=self.port_cfg.get("constraints", {}).get("max_weight", 0.45),
                        min_weight=self.port_cfg.get("constraints", {}).get("min_weight", 0.00),
                        top_n=self.port_cfg.get("top_n", 4),
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

                    max_to = self.reb_cfg.get("max_turnover", 0.80)
                    filtered_weights = cap_turnover(filtered_weights, current_weights, max_to)

                    regime_slice = regime_inputs.loc[:dt] if dt in regime_inputs.index else regime_inputs
                    ec_so_far = equity_curve.dropna()
                    prog_cfg = self.risk_cfg.get("rvol_progressive_derisk", {})
                    prog_tiers = (prog_cfg.get("tiers", [])
                                  if prog_cfg.get("enabled", False) else [])
                    adj_weights, cash_pct, flags = apply_risk_controls(
                        weights=filtered_weights,
                        portfolio_returns=portfolio_daily_returns.iloc[-365:]
                            if len(portfolio_daily_returns) > 0 else pd.Series(dtype=float),
                        regime_inputs=regime_slice,
                        equity_curve=ec_so_far if len(ec_so_far) > 0 else None,
                        vol_target=self.risk_cfg.get("target_vol_annual", 0.40),
                        vol_scaling_enabled=self.risk_cfg.get("vol_scaling_enabled", True),
                        vol_target_mode=self.risk_cfg.get("vol_target_mode", "spike"),
                        rvol_emergency_threshold=rvol_threshold,
                        emergency_cash_pct=self.reb_cfg.get("emergency_cash_pct", 0.50),
                        dd_halve_threshold=self.risk_cfg.get("dd_halve_threshold", -0.10),
                        dd_flat_threshold=self.risk_cfg.get("dd_flat_threshold"),
                        max_weight=self.port_cfg.get("constraints", {}).get("max_weight", 0.45),
                        rvol_progressive_tiers=prog_tiers,
                    )

                    # position tracker + stop-loss — template mechanics, but on
                    # LAGGED prices (see PIT note above)
                    if _position_tracker is not None:
                        _position_tracker.update(dt, adj_weights, stop_prices)

                    if _stop_loss_cfg.get("enabled", False):
                        _stopped, _sl_events, _halve = _stop_loss_fn(
                            current_weights=adj_weights,
                            position_tracker=_position_tracker,
                            sector_prices=stop_prices,
                            benchmark_prices=stop_bench,
                            rebalance_date=dt,
                            config=_stop_loss_cfg,
                        )
                        if _halve:
                            adj_weights = adj_weights * 0.5
                        for _st in _stopped:
                            adj_weights[_st] = 0.0
                        _stop_loss_events.extend(_sl_events)

                    cost_result = self._rebalance_cost(current_weights, adj_weights,
                                                       portfolio_value)
                    costs_records.append({"date": dt, **cost_result})
                    # ADAPTED-fix: the template subtracted cost from equity but
                    # NOT from daily_returns — immaterial at monthly/3bps, but
                    # daily crypto rebalancing makes metrics diverge wildly
                    # from equity. Cost flows through today's return instead.
                    pending_cost_usd += cost_result["total_cost_usd"]

                    # plain-dict trade records (template used qlib Order objects)
                    for ticker in tickers:
                        old_w = float(current_weights.get(ticker, 0.0))
                        new_w = float(adj_weights.get(ticker, 0.0))
                        delta_w = new_w - old_w
                        if abs(delta_w) > 1e-4:
                            trade_orders_list.append({
                                "date": str(dt), "ticker": ticker,
                                "side": "buy" if delta_w > 0 else "sell",
                                "notional_usd": abs(delta_w) * portfolio_value,
                            })

                    current_weights = adj_weights
                    prev_scores = latest_scores.copy()
                    weights_records[dt] = current_weights.to_dict()
                    risk_flags_records.append({"date": dt, **flags.to_dict()})

                    if _position_tracker is not None:
                        _position_tracker.update(dt, current_weights, stop_prices)
                        _position_states_history[dt] = _position_tracker.get_all_states()

            # Daily mark-to-market — template + FUNDING ACCRUAL (crypto)
            if dt in perp_daily_ret.index:
                sector_ret = perp_daily_ret.loc[dt]
                port_ret = float(
                    (current_weights * sector_ret.reindex(current_weights.index,
                                                          fill_value=0.0)).sum())
            else:
                port_ret = 0.0

            # funding: long pays positive rate → holder P&L = −rate × weight
            day_funding = funding_aligned.loc[dt] if dt in funding_aligned.index else None
            funding_ret = 0.0
            if day_funding is not None:
                funding_ret = float(
                    (current_weights * (-day_funding.reindex(current_weights.index,
                                                             fill_value=0.0))).sum())
            funding_pnl_usd = portfolio_value * funding_ret

            cost_ret = (pending_cost_usd / portfolio_value) if portfolio_value > 0 else 0.0
            pending_cost_usd = 0.0
            total_ret = port_ret + funding_ret - cost_ret
            portfolio_value = portfolio_value * (1 + total_ret)
            equity_curve[dt] = portfolio_value
            daily_returns_list.append((dt, total_ret))
            funding_pnl_list.append((dt, funding_pnl_usd))

            _new = pd.Series([total_ret], index=[dt])
            portfolio_daily_returns = pd.concat(
                [s for s in [portfolio_daily_returns, _new] if not s.empty])

        equity_curve = equity_curve.dropna()
        daily_returns = pd.Series(
            [r for _, r in daily_returns_list],
            index=[d for d, _ in daily_returns_list],
            name="portfolio",
        )
        funding_pnl_daily = pd.Series(
            [v for _, v in funding_pnl_list],
            index=[d for d, _ in funding_pnl_list],
            name="funding_pnl_usd",
        )

        return self._assemble_result(
            equity_curve=equity_curve,
            daily_returns=daily_returns,
            weights_records=weights_records,
            scores_records=scores_records,
            costs_records=costs_records,
            risk_flags_records=risk_flags_records,
            regime_daily=regime_daily,
            bt_start=bt_start, bt_end=bt_end,
            initial_capital=initial_capital,
            perp_daily_ret=perp_daily_ret,
            tickers=tickers,
            trade_orders=trade_orders_list,
            stop_loss_events=_stop_loss_events if _stop_loss_events else None,
            position_states_history=_position_states_history if _position_states_history else None,
            funding_pnl_daily=funding_pnl_daily,
        )

    # ------------------------------------------------------------------
    # Result assembly — template shape; dual crypto benchmarks
    # ------------------------------------------------------------------
    def _assemble_result(
        self,
        equity_curve: pd.Series,
        daily_returns: pd.Series,
        weights_records: dict,
        scores_records: dict,
        costs_records: List[dict],
        risk_flags_records: List[dict],
        regime_daily: pd.Series,
        bt_start: str,
        bt_end: str,
        initial_capital: float,
        perp_daily_ret: pd.DataFrame,
        tickers: List[str],
        trade_orders: Optional[List] = None,
        stop_loss_events: Optional[List] = None,
        position_states_history: Optional[Dict] = None,
        funding_pnl_daily: Optional[pd.Series] = None,
    ) -> BacktestResult:
        weights_history = pd.DataFrame(weights_records).T
        weights_history.index.name = "date"
        if not weights_history.empty:
            weights_history = weights_history.fillna(0.0)

        signals_history = pd.DataFrame(scores_records).T
        signals_history.index.name = "date"

        costs_df = (pd.DataFrame(costs_records).set_index("date")
                    if costs_records else pd.DataFrame())
        regime_history = regime_daily.loc[bt_start:bt_end]

        # Benchmark 1: KXBTCPERP HODL
        bench_returns = bench_equity = None
        if BENCHMARK_TICKER in perp_daily_ret.columns:
            bench_returns = perp_daily_ret[BENCHMARK_TICKER].loc[bt_start:bt_end].fillna(0.0)
            bench_equity = (1 + bench_returns).cumprod() * initial_capital

        # Benchmark 2: equal-weight perp basket (crypto addition)
        ew_returns = perp_daily_ret[tickers].loc[bt_start:bt_end].mean(axis=1).fillna(0.0)
        ew_equity = (1 + ew_returns).cumprod() * initial_capital

        metrics = compute_metrics(daily_returns, bench_returns)
        metrics["ew_total_return"] = float((1 + ew_returns).prod() - 1) if len(ew_returns) else float("nan")
        metrics["funding_pnl_usd"] = (float(funding_pnl_daily.sum())
                                      if funding_pnl_daily is not None else 0.0)
        sub_metrics = subperiod_analysis(daily_returns, bench_returns)
        dd_episodes = find_drawdown_episodes(daily_returns)

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
            benchmark_ew_returns=ew_returns,
            benchmark_ew_equity=ew_equity,
            trade_orders=trade_orders or [],
            stop_loss_events=stop_loss_events,
            position_states_history=position_states_history,
            funding_pnl_daily=funding_pnl_daily,
        )

        logger.info(f"\n{result.summary()}")
        return result

    # ------------------------------------------------------------------
    # Run record (replaces template MLflow/_record_experiment — JSON like
    # the existing someopark pipelines)
    # ------------------------------------------------------------------
    def _record_run(self, result: BacktestResult, bt_start: str, bt_end: str) -> None:
        try:
            out = SIGNALS_DIR / "perp_rotation" / "backtests"
            out.mkdir(parents=True, exist_ok=True)
            stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
            m = result.metrics
            clean = {k: (None if isinstance(v, float) and np.isnan(v) else v)
                     for k, v in m.items()
                     if isinstance(v, (int, float, str, bool, type(None)))}
            (out / f"backtest_{stamp}.json").write_text(json.dumps({
                "bt_start": bt_start, "bt_end": bt_end,
                "source": result.config.get("_panel_source", "kalshi"),
                "metrics": clean,
                "n_rebalances": len(result.weights_history),
                "final_equity": float(result.equity_curve.iloc[-1])
                    if len(result.equity_curve) else None,
                "config": {k: v for k, v in result.config.items()
                           if k in ("universe", "signals", "portfolio",
                                    "rebalance", "risk", "costs")},
            }, indent=2, default=str))
        except Exception:
            logger.exception("run record failed (non-fatal)")
