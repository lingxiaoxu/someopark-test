"""
PortfolioMTFSRun.py — Backtest engine for Momentum Trend Following Strategy (MTFS).

Adapted from PortfolioMRPTRun.py.  The portfolio infrastructure (Portfolio, Trade,
PortfolioAnalysis, ExportExcel, PortfolioVisualizer, PortfolioMakeOrder, etc.) is
reused from PortfolioClasses.py.  Only the signal generation, entry/exit logic,
and stop-loss checks are replaced with momentum-based equivalents.

Usage (standalone):
    python PortfolioMTFSRun.py

Usage (from PortfolioMTFSStrategyRuns.py):
    import PortfolioMTFSRun as PortfolioRun
    result = PortfolioRun.main(config={...})
"""

import numpy as np
import statsmodels.api as sm
import statsmodels.tsa.stattools as ts
import pandas as pd
import datetime
from datetime import datetime, time
import yfinance as yf

import logging
import json
import os
import requests
import time as time_module
import sys
from PriceDataStore import PriceDataStore

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter

import openpyxl
from openpyxl.utils import get_column_letter

from pykalman import KalmanFilter
import pandas_market_calendars as mcal
from collections import defaultdict
from PortfolioClasses import (
    CurrentData, date_rules, time_rules, Trade, Portfolio, Data,
    PortfolioAnalysis, ExportExcel, Context, Execution, PortfolioVisualizer,
    PortfolioMakeOrder, PortfolioConstruct,
    # MTFS-specific classes
    MomentumSignal, MTFSExecution, MTFSPortfolioConstruct, MTFSStopLossFunction,
    # MTFS statistical tests
    MomentumDecayTest, TrendStrengthTest, MomentumConsistencyTest,
    VolatilityRegimeTest, SMACrossoverTest,
)

import quantstats as qs
import math

# Set up logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ~~~~~~~~~~~~~~~~~~~~~~ DATA SOURCE CONFIGURATION ~~~~~~~~~~~~~~~~~~~~~~
DATA_SOURCE = 'polygon'
POLYGON_API_KEY = os.environ['POLYGON_API_KEY']


# ~~~~~~~~~~~~~~~~~~~~~~ FUNCTIONS FOR FILING AN ORDER ~~~~~~~~~~~~~~~~~~~~~~

def schedule_function(func, date_rule, time_rule):
    scheduled_functions = []
    scheduled_functions.append((func, date_rule, time_rule))


DEFAULT_PAIRS = [
    ['MSCI', 'LII'],
    ['D', 'MCHP'],
    ['DG', 'MOS'],
    ['ESS', 'EXPD'],
    ['ACGL', 'UHS'],
]


def initialize(context, pairs=None, params=None, pair_params=None):
    """Initialize the MTFS strategy context.
    Structure mirrors MRPT's initialize() but uses MTFSExecution."""
    raw_pairs = pairs if pairs is not None else DEFAULT_PAIRS
    context.strategy_pairs = [
        [p[0], p[1], {
            'in_short': False, 'in_long': False,
            'momentum_history': {},  # {date: {stock: scores_dict}}
        }]
        for p in raw_pairs
    ]
    context.pair_params = pair_params or {}

    context.initial_cash = 500000
    context.initial_loan = 500000
    context.interest_rate = 0.05
    context.num_pairs = len(context.strategy_pairs)

    context.portfolio = Portfolio(
        initial_cash=context.initial_cash,
        initial_loan=context.initial_loan,
        interest_rate=context.interest_rate,
        strategy_pairs=context.strategy_pairs
    )

    # Use MTFSExecution instead of Execution
    context.execution = MTFSExecution()

    # Apply parameter overrides
    p = params or {}
    for key, value in p.items():
        if hasattr(context.execution, key):
            setattr(context.execution, key, value)

    context.portfolio_construct = MTFSPortfolioConstruct(constant=1)
    context.portfolio_order = PortfolioMakeOrder(constant=1)
    context.portfolio_stoploss_function = MTFSStopLossFunction(constant=1)

    context.recorded_vars = {}

    # Day counter for rebalancing
    context.days_since_rebalance = 0

    log.info("MTFS Algorithm initialized")


def my_handle_data(context, data):
    """Called every trading day — MTFS version."""
    context.data = data

    if context.portfolio_order.get_open_orders():
        return

    # Get all prices
    all_prices = {}
    try:
        for pair in context.strategy_pairs:
            stock_1, stock_2 = pair[0], pair[1]
            try:
                prices = data.history([stock_1, stock_2], "Adj Close", 1, "1d")
            except Exception as e:
                log.warning(f"'Adj Close' not available, falling back to 'Close': {e}")
                prices = data.history([stock_1, stock_2], "Close", 1, "1d")
            all_prices[stock_1] = prices[stock_1].iloc[-1]
            all_prices[stock_2] = prices[stock_2].iloc[-1]
    except Exception as e:
        log.error(f"Error getting prices: {e}")
        return

    context.portfolio.update_price_history(context.portfolio.current_date, all_prices)

    # Increment rebalance counter
    context.days_since_rebalance += 1

    # Process each pair
    for i in range(len(context.strategy_pairs)):
        pair = context.strategy_pairs[i]
        new_pair = process_pair(pair, context, data)
        context.strategy_pairs[i] = new_pair

    context.portfolio.update_values(all_prices)
    context.portfolio.update_max_drawdown()


def process_pair(pair, context, data):
    """Process a single pair for MTFS strategy.
    Mirrors MRPT's process_pair with per-pair param override support."""
    stock_1 = pair[0]
    stock_2 = pair[1]
    context.current_pair = (stock_1, stock_2)
    pair_key = f"{stock_1}/{stock_2}"

    # Apply per-pair parameter overrides
    _saved_exec_params = {}
    _PAIR_PARAM_KEYS = [
        'momentum_windows', 'momentum_weights', 'skip_days', 'use_vams',
        'sma_short', 'sma_long', 'require_trend_confirmation',
        'entry_momentum_threshold', 'exit_momentum_decay_threshold',
        'reversal_sma_lookback', 'momentum_decay_short_window', 'momentum_decay_long_window',
        'exit_on_reversal', 'exit_on_momentum_decay',
        'target_annual_vol', 'vol_scale_window', 'max_vol_scale_factor',
        'crash_vol_percentile', 'crash_scale_factor',
        'amplifier', 'use_vol_weighted_sizing',
        'hedge_method', 'hedge_lag',
        'volatility_stop_loss_multiplier', 'max_holding_period', 'cooling_off_period',
        'pair_stop_loss_pct', 'rebalance_frequency',
        'mean_back', 'std_back', 'v_back',
    ]
    if pair_key in context.pair_params:
        pp = context.pair_params[pair_key]
        for k in _PAIR_PARAM_KEYS:
            if k in pp:
                _saved_exec_params[k] = getattr(context.execution, k)
                setattr(context.execution, k, pp[k])

    def _restore_exec():
        for k, v in _saved_exec_params.items():
            setattr(context.execution, k, v)

    try:
        return _process_pair_body(pair, stock_1, stock_2, pair_key, context, data)
    finally:
        _restore_exec()


def _process_pair_body(pair, stock_1, stock_2, pair_key, context, data):
    """Core MTFS pair processing logic."""
    # ── Get historical prices ─────────────────────────────────────────────
    try:
        try:
            prices = data.history([stock_1, stock_2], "Adj Close", 300, "1d")
        except Exception as e:
            log.warning(f"'Adj Close' not available for historical data, falling back to 'Close': {e}")
            prices = data.history([stock_1, stock_2], "Close", 300, "1d")
        stock_1_P = prices[stock_1]
        stock_2_P = prices[stock_2]
    except Exception as e:
        log.error(f"Error getting historical prices for {stock_1}/{stock_2}: {e}")
        return [stock_1, stock_2, pair[2]]

    in_short = pair[2].get('in_short', False)
    in_long = pair[2].get('in_long', False)
    momentum_history = pair[2].get('momentum_history', {})

    ms = context.portfolio_construct.momentum_signal

    # ── Update MomentumSignal parameters from execution ───────────────────
    ms.windows = context.execution.momentum_windows
    ms.weights = context.execution.momentum_weights
    ms.skip_days = context.execution.skip_days
    ms.SMA_SHORT = context.execution.sma_short
    ms.SMA_LONG = context.execution.sma_long

    # ── Always compute available per-window data (even during warmup) ─────
    per_win_ret_1 = ms.per_window_returns(stock_1_P)
    per_win_ret_2 = ms.per_window_returns(stock_2_P)
    consistency_1 = ms.momentum_consistency(stock_1_P)
    consistency_2 = ms.momentum_consistency(stock_2_P)
    trend_long_1 = bool(ms.trend_confirmed_long(stock_1_P))
    trend_short_2 = bool(ms.trend_confirmed_short(stock_2_P))
    trend_short_1 = bool(ms.trend_confirmed_short(stock_1_P))
    trend_long_2 = bool(ms.trend_confirmed_long(stock_2_P))

    # Build per-window vars dict
    per_win_vars = {}
    for w in ms.windows:
        r1 = per_win_ret_1.get(w, np.nan)
        r2 = per_win_ret_2.get(w, np.nan)
        v1 = ms.compute_vams(stock_1_P, w)
        v2 = ms.compute_vams(stock_2_P, w)
        if not np.isnan(r1):
            per_win_vars[f'Ret1_{w}d'] = r1
        if not np.isnan(r2):
            per_win_vars[f'Ret2_{w}d'] = r2
        if not np.isnan(r1) and not np.isnan(r2):
            per_win_vars[f'RetSpread_{w}d'] = r1 - r2
        if not np.isnan(v1):
            per_win_vars[f'VAMS1_{w}d'] = v1
        if not np.isnan(v2):
            per_win_vars[f'VAMS2_{w}d'] = v2

    # ── Statistical tests (always run — works with partial data) ──────────
    _run_statistical_tests(pair_key, stock_1_P, stock_2_P, ms, context)

    # ── Volatility scaling (portfolio-level, always available) ────────────
    vol_scale = context.portfolio_construct.compute_vol_scale_factor(
        context.portfolio.daily_pnl_history, context.execution)

    # ── Check if we have enough data for composite scoring ────────────────
    max_window = max(context.execution.momentum_windows)
    required_bars = max_window + ms.skip_days + 1
    if len(stock_1_P) < required_bars or len(stock_2_P) < required_bars:
        record_vars(context, Y_pct=0, X_pct=0,
                    Consistency_1=consistency_1, Consistency_2=consistency_2,
                    Trend_Long_1=trend_long_1, Trend_Short_2=trend_short_2,
                    Vol_Scale=vol_scale,
                    Normalized_Vol=context.portfolio.normalized_volatility,
                    in_long=in_long, in_short=in_short,
                    data_bars=len(stock_1_P), required_bars=required_bars,
                    **per_win_vars)
        return [stock_1, stock_2, pair[2]]

    # ── Compute composite momentum scores ─────────────────────────────────
    if context.execution.use_vams:
        score_1 = ms.composite_vams(stock_1_P)
        score_2 = ms.composite_vams(stock_2_P)
    else:
        score_1 = ms.composite_raw_momentum(stock_1_P)
        score_2 = ms.composite_raw_momentum(stock_2_P)

    if np.isnan(score_1) or np.isnan(score_2):
        record_vars(context, Y_pct=0, X_pct=0,
                    Consistency_1=consistency_1, Consistency_2=consistency_2,
                    Trend_Long_1=trend_long_1, Trend_Short_2=trend_short_2,
                    Vol_Scale=vol_scale,
                    Normalized_Vol=context.portfolio.normalized_volatility,
                    in_long=in_long, in_short=in_short,
                    data_bars=len(stock_1_P), required_bars=required_bars,
                    **per_win_vars)
        return [stock_1, stock_2, pair[2]]

    # Momentum spread: positive means stock_1 has stronger momentum
    momentum_spread = score_1 - score_2

    # ── Compute hedge ratio for dollar-neutral sizing ─────────────────────
    hedge = context.portfolio_construct.compute_pair_hedge_ratio(
        stock_1_P, stock_2_P, method=context.execution.hedge_method)

    if pair_key not in context.portfolio.hedge_history:
        context.portfolio.hedge_history[pair_key] = []
    context.portfolio.hedge_history[pair_key].append(
        (context.portfolio.current_date, hedge))

    # ── Dynamic thresholds ────────────────────────────────────────────────
    momentum_strength, entry_threshold, exit_threshold = \
        context.portfolio_construct.calculate_dynamic_momentum_thresholds(
            context, pair_key, stock_1_P, stock_2_P)

    # Store momentum history
    momentum_history[context.portfolio.current_date] = {
        stock_1: {'score': score_1, 'consistency': consistency_1},
        stock_2: {'score': score_2, 'consistency': consistency_2},
    }

    # ── Record variables (full data available) ────────────────────────────
    record_vars(context,
                Y_pct=0, X_pct=0,
                Momentum_1=score_1, Momentum_2=score_2,
                Momentum_Spread=momentum_spread,
                Hedge=hedge,
                Consistency_1=consistency_1, Consistency_2=consistency_2,
                Momentum_Strength=momentum_strength,
                Entry_Threshold=entry_threshold, Exit_Threshold=exit_threshold,
                Vol_Scale=vol_scale,
                Normalized_Vol=context.portfolio.normalized_volatility,
                Trend_Long_1=trend_long_1, Trend_Short_2=trend_short_2,
                in_long=in_long, in_short=in_short,
                **per_win_vars)

    # ── Stop loss checks (if in a position) ───────────────────────────────
    if in_short or in_long:
        # 1. Momentum reversal stop (MTFS-specific, most important)
        reversal_stop, reversal_reason, _ = \
            context.portfolio_stoploss_function.check_momentum_reversal_stop(
                context, pair, stock_1_P, stock_2_P)
        if reversal_stop:
            record_vars(context, Y_pct=0, X_pct=0,
                        in_long=in_long, in_short=in_short,
                        stop_loss_triggered=True, stop_loss_reason=reversal_reason)
            return context.portfolio_stoploss_function.handle_stop_loss(
                context, pair, reversal_reason, momentum_spread)

        # 2. Pair P&L stop
        pnl_stop, pnl_reason, _ = \
            context.portfolio_stoploss_function.check_pair_pnl_stop_loss(context, pair)
        if pnl_stop:
            record_vars(context, Y_pct=0, X_pct=0,
                        in_long=in_long, in_short=in_short,
                        stop_loss_triggered=True, stop_loss_reason=pnl_reason)
            return context.portfolio_stoploss_function.handle_stop_loss(
                context, pair, pnl_reason, momentum_spread)

        # 3. Volatility stop (price ratio based)
        vol_stop, vol_reason, vol_level = \
            context.portfolio_stoploss_function.check_volatility_stop_loss(
                context, pair, stock_1_P, stock_2_P)
        if vol_stop:
            record_vars(context, Y_pct=0, X_pct=0,
                        in_long=in_long, in_short=in_short,
                        stop_loss_triggered=True, stop_loss_reason=vol_reason)
            return context.portfolio_stoploss_function.handle_stop_loss(
                context, pair, vol_reason, momentum_spread)

        # 4. Time-based stop
        time_stop, time_reason, _ = \
            context.portfolio_stoploss_function.check_time_based_stop_loss(context, pair)
        if time_stop:
            record_vars(context, Y_pct=0, X_pct=0,
                        in_long=in_long, in_short=in_short,
                        stop_loss_triggered=True, stop_loss_reason=time_reason)
            return context.portfolio_stoploss_function.handle_stop_loss(
                context, pair, time_reason, momentum_spread)

        # ── Exit logic: momentum consistency decay ────────────────────────
        avg_consistency = (consistency_1 + consistency_2) / 2.0
        if avg_consistency < exit_threshold:
            # Momentum signal has weakened — close position
            context.portfolio_order.order_target(context, stock_1, 0)
            context.portfolio_order.order_target(context, stock_2, 0)
            in_short = False
            in_long = False
            record_vars(context, Y_pct=0, X_pct=0,
                        in_long=in_long, in_short=in_short, action='CLOSE_DECAY')
            return [stock_1, stock_2,
                    {'in_short': in_short, 'in_long': in_long,
                     'momentum_history': momentum_history}]

    # ── Skip order logic during warmup ────────────────────────────────────
    if getattr(context, 'warmup_mode', False):
        return [stock_1, stock_2,
                {'in_short': in_short, 'in_long': in_long,
                 'momentum_history': momentum_history}]

    # ── Entry logic ───────────────────────────────────────────────────────
    # In MTFS, the pairs are pre-ranked: stock_1 = winner, stock_2 = loser
    # (or vice versa, depending on current momentum).
    # We go long the one with stronger momentum, short the weaker one.

    # Check if enough time has passed since last rebalance
    # (Only open NEW positions at rebalance intervals, but allow exits anytime)
    can_open = (context.days_since_rebalance >= context.execution.rebalance_frequency
                or context.days_since_rebalance == 1)  # also allow on first day

    if not in_long and not in_short and can_open:
        # Check cooling-off period
        if pair_key in context.execution.stop_loss_history and \
                context.execution.stop_loss_history[pair_key]:
            last_event = context.execution.stop_loss_history[pair_key][-1]
            if last_event['reason'] != "Cooling-off period ended":
                if not context.portfolio_stoploss_function.re_evaluate_pair(context, pair):
                    return [stock_1, stock_2,
                            {'in_short': in_short, 'in_long': in_long,
                             'momentum_history': momentum_history}]

        # Determine direction based on momentum scores
        # stock_1 is the "winner candidate", stock_2 is the "loser candidate"
        # Long stock_1 / Short stock_2 if stock_1 has stronger momentum
        open_long_pair = False  # long stock_1, short stock_2
        open_short_pair = False  # short stock_1, long stock_2

        # Check if we have enough data for SMA trend confirmation
        sma_long_period = context.execution.sma_long
        has_sma_data_1 = len(stock_1_P) >= sma_long_period
        has_sma_data_2 = len(stock_2_P) >= sma_long_period

        if score_1 > score_2 and momentum_spread > entry_threshold:
            # stock_1 is the winner → long stock_1, short stock_2
            if context.execution.require_trend_confirmation:
                if has_sma_data_1 and has_sma_data_2:
                    # Full SMA confirmation available
                    if trend_long_1 and trend_short_2:
                        open_long_pair = True
                    elif consistency_1 > 0.7 and consistency_2 < 0.5:
                        open_long_pair = True
                else:
                    # Insufficient data for SMA — fall back to consistency check
                    if consistency_1 > 0.6:
                        open_long_pair = True
            else:
                open_long_pair = True

        elif score_2 > score_1 and (-momentum_spread) > entry_threshold:
            # stock_2 is the winner → short stock_1, long stock_2
            if context.execution.require_trend_confirmation:
                if has_sma_data_1 and has_sma_data_2:
                    if trend_long_2 and trend_short_1:
                        open_short_pair = True
                    elif consistency_2 > 0.7 and consistency_1 < 0.5:
                        open_short_pair = True
                else:
                    if consistency_2 > 0.6:
                        open_short_pair = True
            else:
                open_short_pair = True

        if open_long_pair:
            # Long stock_1, short stock_2 (dollar-neutral via hedge ratio)
            stock_1_shares = 1
            stock_2_shares = -hedge
            in_long = True
            in_short = False

            (stock_1_perc, stock_2_perc) = context.portfolio_order.computeHoldingsPct(
                stock_1_shares, stock_2_shares,
                stock_1_P.iloc[-1], stock_2_P.iloc[-1])

            effective_amplifier = context.execution.amplifier * vol_scale
            context.portfolio_order.order_target_percent(
                context, stock_1,
                stock_1_perc * effective_amplifier / context.num_pairs)
            context.portfolio_order.order_target_percent(
                context, stock_2,
                stock_2_perc * effective_amplifier / context.num_pairs)

            record_vars(context, Y_pct=stock_1_perc, X_pct=stock_2_perc,
                        in_long=in_long, in_short=in_short, action='OPEN_LONG')

            # Reset rebalance counter
            context.days_since_rebalance = 0

            return [stock_1, stock_2,
                    {'in_short': in_short, 'in_long': in_long,
                     'momentum_history': momentum_history}]

        elif open_short_pair:
            # Short stock_1, long stock_2
            stock_1_shares = -1
            stock_2_shares = hedge
            in_short = True
            in_long = False

            (stock_1_perc, stock_2_perc) = context.portfolio_order.computeHoldingsPct(
                stock_1_shares, stock_2_shares,
                stock_1_P.iloc[-1], stock_2_P.iloc[-1])

            effective_amplifier = context.execution.amplifier * vol_scale
            context.portfolio_order.order_target_percent(
                context, stock_1,
                stock_1_perc * effective_amplifier / context.num_pairs)
            context.portfolio_order.order_target_percent(
                context, stock_2,
                stock_2_perc * effective_amplifier / context.num_pairs)

            record_vars(context, Y_pct=stock_1_perc, X_pct=stock_2_perc,
                        in_long=in_long, in_short=in_short, action='OPEN_SHORT')

            context.days_since_rebalance = 0

            return [stock_1, stock_2,
                    {'in_short': in_short, 'in_long': in_long,
                     'momentum_history': momentum_history}]

    return [stock_1, stock_2,
            {'in_short': in_short, 'in_long': in_long,
             'momentum_history': momentum_history}]


def _run_statistical_tests(pair_key, stock_1_P, stock_2_P, ms, context):
    """Run all momentum statistical tests and store to statistical_test_history.
    Works with partial data — each test handles insufficient data gracefully."""
    try:
        decay_test_1 = MomentumDecayTest()
        decay_test_1.short_window = context.execution.momentum_decay_short_window
        decay_test_1.long_window = context.execution.momentum_decay_long_window
        decay_test_1.apply(stock_1_P, ms)

        decay_test_2 = MomentumDecayTest()
        decay_test_2.short_window = context.execution.momentum_decay_short_window
        decay_test_2.long_window = context.execution.momentum_decay_long_window
        decay_test_2.apply(stock_2_P, ms)

        trend_str_1 = TrendStrengthTest()
        trend_str_1.apply(stock_1_P)
        trend_str_2 = TrendStrengthTest()
        trend_str_2.apply(stock_2_P)

        consist_test_1 = MomentumConsistencyTest()
        consist_test_1.apply(stock_1_P, ms)
        consist_test_2 = MomentumConsistencyTest()
        consist_test_2.apply(stock_2_P, ms)

        vol_regime_1 = VolatilityRegimeTest()
        vol_regime_1.apply(stock_1_P)
        vol_regime_2 = VolatilityRegimeTest()
        vol_regime_2.apply(stock_2_P)

        sma_test_1 = SMACrossoverTest()
        sma_test_1.sma_short_period = context.execution.sma_short
        sma_test_1.sma_long_period = context.execution.sma_long
        sma_test_1.apply(stock_1_P, ms)
        sma_test_2 = SMACrossoverTest()
        sma_test_2.sma_short_period = context.execution.sma_short
        sma_test_2.sma_long_period = context.execution.sma_long
        sma_test_2.apply(stock_2_P, ms)

        if pair_key not in context.portfolio.statistical_test_history:
            context.portfolio.statistical_test_history[pair_key] = {}

        test_results = {}
        for test, prefix in [
            (decay_test_1, 'momentum_decay_s1'),
            (trend_str_1, 'trend_strength_s1'),
            (consist_test_1, 'consistency_s1'),
            (vol_regime_1, 'vol_regime_s1'),
            (sma_test_1, 'sma_crossover_s1'),
            (decay_test_2, 'momentum_decay_s2'),
            (trend_str_2, 'trend_strength_s2'),
            (consist_test_2, 'consistency_s2'),
            (vol_regime_2, 'vol_regime_s2'),
            (sma_test_2, 'sma_crossover_s2'),
        ]:
            for attr, value in test.__dict__.items():
                test_results[f"{prefix}_{attr}"] = value

        context.portfolio.statistical_test_history[pair_key][context.portfolio.current_date] = test_results

    except Exception as e:
        log.warning(f"Error in momentum statistical tests for {pair_key}: {str(e)}")


def record_vars(context, **kwargs):
    """Record strategy variables for debugging and visualization.
    Mirrors MRPT record_vars with sector-based key prefixing for Momentum/Hedge."""
    current_date = context.portfolio.current_date
    pair = f"{context.current_pair[0]}/{context.current_pair[1]}"
    stock_1, stock_2 = context.current_pair

    if pair not in context.recorded_vars:
        context.recorded_vars[pair] = {}
    if current_date not in context.recorded_vars[pair]:
        context.recorded_vars[pair][current_date] = {}

    # Sector mapping (mirrors MRPT exactly)
    _tech_stocks       = {'AAPL', 'META', 'NVDA', 'TSM', 'D', 'MCHP', 'CART', 'DASH'}
    _finance_stocks    = {'GS', 'ALLY', 'ACGL', 'UHS', 'ARES', 'CG', 'AMG', 'BEN', 'TW', 'CME'}
    _industrial_stocks = {'ALGN', 'UAL', 'MSCI', 'LII', 'LYFT', 'UBER'}
    _energy_stocks     = {'CL', 'USO', 'ESS', 'EXPD'}
    _food_stocks       = {'DG', 'MOS', 'YUM', 'MCD'}

    pair_stocks = {stock_1, stock_2}
    if pair_stocks & _tech_stocks:
        sector = 'tech'
    elif pair_stocks & _finance_stocks:
        sector = 'finance'
    elif pair_stocks & _industrial_stocks:
        sector = 'industrial'
    elif pair_stocks & _energy_stocks:
        sector = 'energy'
    elif pair_stocks & _food_stocks:
        sector = 'food'
    else:
        sector = 'other'

    for key, value in kwargs.items():
        if key == 'Momentum_Spread':
            key = f'Momentum_Spread_{sector}'
        elif key == 'Hedge':
            key = f'Hedge_{sector}'
        context.recorded_vars[pair][current_date][key] = value


def summarize_pair_trade_history(pair_trade_history, acc_pair_trade_pnl_history):
    """Same as MRPT version — reusable."""
    summary = {}
    for pair, trades in pair_trade_history.items():
        long_trades = [t for t in trades if t.direction == 'long' and t.order_type == 'open']
        short_trades = [t for t in trades if t.direction == 'short' and t.order_type == 'open']

        pair_pnl_history = []
        for date, pair_pnls in acc_pair_trade_pnl_history.items():
            if pair in pair_pnls:
                try:
                    pair_pnl_history.append((date, pair_pnls[pair]['pnl_dollar']))
                except KeyError:
                    log.warning(f"Missing 'pnl_dollar' for pair {pair} on date {date}")

        latest_pnl = pair_pnl_history[-1][1] if pair_pnl_history else 0

        summary[pair] = {
            'total_trades': len(long_trades) + len(short_trades),
            'long_trades': len(long_trades),
            'short_trades': len(short_trades),
            'total_volume': sum(abs(t.amount) for t in trades),
            'net_pnl': latest_pnl
        }
    return summary


def check_data_structure(data):
    """Check the structure of the data and fix it if necessary — same as MRPT."""
    log.info(f"Checking data structure: shape={data.shape}, columns={data.columns.names}")

    if not isinstance(data.columns, pd.MultiIndex):
        log.info("Data is not multi-indexed. Converting to proper format...")

        if 'Adj Close' in data.columns or 'Close' in data.columns:
            symbols = []
            for col in data.columns:
                if '.' in col:
                    symbol, field = col.split('.')
                    symbols.append(symbol)
            if not symbols:
                symbols = list(set([col.split(' ')[0] for col in data.columns if ' ' in col]))
            if not symbols:
                price_cols = ['Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume']
                num_symbols = len(data.columns) // len(price_cols)
                symbols = [f'Symbol{i+1}' for i in range(num_symbols)]

            new_data = pd.DataFrame(index=data.index)
            for symbol in symbols:
                for field in ['Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume']:
                    if f'{symbol}.{field}' in data.columns:
                        new_data[(field, symbol)] = data[f'{symbol}.{field}']
                    elif f'{field} {symbol}' in data.columns:
                        new_data[(field, symbol)] = data[f'{field} {symbol}']
                    elif field in data.columns and symbols.index(symbol) == 0:
                        new_data[(field, symbol)] = data[field]
            data = new_data
            log.info(f"Converted data to multi-index with shape={data.shape}")

        if not isinstance(data.columns, pd.MultiIndex):
            fields = ['Close', 'Adj Close', 'Open', 'High', 'Low', 'Volume']
            tuples = []
            for col in data.columns:
                for field in fields:
                    if field.lower() in col.lower():
                        symbol = col.replace(field, '').strip()
                        if not symbol:
                            symbol = col
                        tuples.append((field, symbol))
                        break
                else:
                    tuples.append(('Close', col))
            data.columns = pd.MultiIndex.from_tuples(tuples, names=['Price', 'Symbol'])
            log.info(f"Created MultiIndex columns: {data.columns}")

    all_fields = set([field for field, _ in data.columns])
    if 'Adj Close' not in all_fields and 'Close' in all_fields:
        log.info("'Adj Close' not found in data, creating it from 'Close'")
        for symbol in set([symbol for _, symbol in data.columns]):
            if ('Close', symbol) in data.columns:
                data[('Adj Close', symbol)] = data[('Close', symbol)]

    return data


def _fetch_polygon_dividends(symbol, start_date, end_date):
    """Fetch dividend data from Polygon.io — same as MRPT."""
    all_divs = []
    url = (
        f"https://api.polygon.io/v3/reference/dividends"
        f"?ticker={symbol}"
        f"&ex_dividend_date.gte={start_date}"
        f"&ex_dividend_date.lte={end_date}"
        f"&order=asc&limit=1000"
        f"&apiKey={POLYGON_API_KEY}"
    )
    while url:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        all_divs.extend(body.get('results', []))
        next_url = body.get('next_url')
        if next_url:
            url = f"{next_url}&apiKey={POLYGON_API_KEY}"
        else:
            url = None
    return all_divs


def _compute_dividend_adjusted_close(df, dividends):
    """Compute dividend-adjusted close price — same as MRPT."""
    adj = df['c'].copy()
    for div in reversed(dividends):
        ex_date = pd.Timestamp(div['ex_dividend_date'])
        amount = div['cash_amount']
        mask = adj.index < ex_date
        if not mask.any():
            continue
        pre_ex_close = df.loc[adj.index[mask][-1], 'c']
        if pre_ex_close == 0:
            continue
        factor = 1 - (amount / pre_ex_close)
        adj.loc[mask] *= factor
    return adj


def load_historical_data_polygon(start_date, end_date, symbols):
    """Load historical data from Polygon.io — same as MRPT."""
    log.info(f"Downloading data from Polygon.io for {len(symbols)} symbols...")

    all_data = {}
    for i, symbol in enumerate(symbols):
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
            f"{start_date}/{end_date}?adjusted=true&sort=asc&limit=50000"
            f"&apiKey={POLYGON_API_KEY}"
        )
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            body = resp.json()

            if body.get('status') not in ('OK', 'DELAYED') or not body.get('results'):
                raise ValueError(f"Polygon API error for {symbol}: status={body.get('status')}, "
                                 f"resultsCount={body.get('resultsCount', 0)}")

            results = body['results']
            df = pd.DataFrame(results)
            df['date'] = pd.to_datetime(df['t'], unit='ms')
            df = df.set_index('date')

            dividends = _fetch_polygon_dividends(symbol, start_date, end_date)
            adj_close = _compute_dividend_adjusted_close(df, dividends)
            div_count = len(dividends)

            all_data[symbol] = {
                'Open': df['o'],
                'High': df['h'],
                'Low': df['l'],
                'Close': df['c'],
                'Adj Close': adj_close,
                'Volume': df['v'],
            }
            log.info(f"  [{i+1}/{len(symbols)}] {symbol}: {len(results)} bars, {div_count} dividends")
        except Exception as e:
            log.error(f"  [{i+1}/{len(symbols)}] Failed to get {symbol}: {e}")
            raise

        if i < len(symbols) - 1:
            time_module.sleep(0.2)

    fields = ['Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume']
    tuples = []
    arrays = {}
    for field in fields:
        for symbol in symbols:
            col_key = (field, symbol)
            tuples.append(col_key)
            arrays[col_key] = all_data[symbol][field]

    multi_index = pd.MultiIndex.from_tuples(tuples, names=['Price', 'Ticker'])
    data = pd.DataFrame(arrays, columns=multi_index)
    data.index.name = 'Date'

    log.info(f"Polygon.io: built DataFrame with {len(data)} rows, {len(data.columns)} columns")
    return data


def load_historical_data_yahoo(start_date, end_date, symbols):
    """Load historical data from Yahoo Finance — same as MRPT."""
    max_retries = 3
    retry_delay = 5

    for retry in range(max_retries):
        try:
            log.info(f"Attempt {retry+1} to download data from Yahoo Finance...")
            data = yf.download(symbols, start=start_date, end=end_date, progress=True)
            if data.empty or len(data) < 5:
                raise ValueError("Downloaded data is empty or has too few rows")
            log.info(f"Successfully downloaded data with {len(data)} rows")
            return data
        except Exception as e:
            log.error(f"Failed to download data: {str(e)}")
            if retry < max_retries - 1:
                log.info(f"Retrying in {retry_delay} seconds...")
                time_module.sleep(retry_delay)
                retry_delay *= 2
            else:
                log.error("All Yahoo Finance download attempts failed.")
                raise


def load_historical_data(start_date, end_date, symbols, data_source=None):
    """Load historical data — same as MRPT."""
    if data_source is None:
        data_source = DATA_SOURCE

    log.info(f"Attempting to load historical data for {len(symbols)} symbols "
             f"from {start_date} to {end_date} [source={data_source}]")

    try:
        if data_source == 'polygon':
            store = PriceDataStore(
                base_dir=os.path.dirname(os.path.abspath(__file__)),
                polygon_api_key=POLYGON_API_KEY,
            )
            data = store.load(symbols, start_date, end_date)
        else:
            data = load_historical_data_yahoo(start_date, end_date, symbols)

        data = check_data_structure(data)
        return data
    except Exception as e:
        log.error(f"Failed to load data from {data_source}: {e}")
        log.error("Cannot proceed without real market data. Exiting.")
        sys.exit(1)


class CustomData(Data):
    """Extension of the Data class — same as MRPT."""

    def history(self, assets, fields, bar_count, frequency):
        if frequency != '1d':
            raise ValueError("Only daily frequency is supported")

        end_date = self.historical_data.index[-1]
        start_date = end_date - pd.Timedelta(days=bar_count - 1)

        if isinstance(fields, str):
            fields = [fields]

        field_mapping = {
            'price': ['Adj Close', 'Close'],
            'Adj Close': ['Adj Close', 'Close'],
            'Close': ['Close', 'Adj Close']
        }

        available_fields = set([col[0] for col in self.historical_data.columns])

        result = pd.DataFrame(index=self.historical_data.loc[start_date:end_date].index)

        for asset in assets:
            for field in fields:
                if field in field_mapping:
                    for alt_field in field_mapping[field]:
                        if alt_field in available_fields and (alt_field, asset) in self.historical_data.columns:
                            result[asset] = self.historical_data.loc[start_date:end_date, (alt_field, asset)]
                            break
                    else:
                        raise ValueError(f"Could not find suitable alternative for field '{field}' for asset '{asset}'")
                else:
                    if (field, asset) in self.historical_data.columns:
                        result[asset] = self.historical_data.loc[start_date:end_date, (field, asset)]
                    else:
                        raise ValueError(f"Field '{field}' not available for asset '{asset}'")

        if len(fields) == 1:
            return result
        return result


class MTFSPortfolioVisualizer(PortfolioVisualizer):
    """Extended visualizer for MTFS — overrides MRPT charts with momentum-equivalent charts."""

    # sector → recorded_vars column name for momentum spread
    _SPREAD_COL = {
        'tech': 'Momentum_Spread_tech', 'finance': 'Momentum_Spread_finance',
        'food': 'Momentum_Spread_food', 'industrial': 'Momentum_Spread_industrial',
        'energy': 'Momentum_Spread_energy',
    }
    _SPREAD_COLS = list(_SPREAD_COL.values())

    def _get_momentum_spread(self, data):
        """Return the first non-None momentum spread value from sector-specific keys."""
        for col in self._SPREAD_COLS:
            v = data.get(col)
            if v is not None:
                return v
        return None

    def plot_all_histories(self):
        self.plot_portfolio_history()
        self.plot_individual_stocks()
        self.plot_pair_trades()           # MTFS version: momentum spread + trade markers
        self.plot_additional_histories()  # MTFS version: momentum scores + DoD PnL
        self.plot_vams_windows()          # MTFS-specific: per-window VAMS decomposition

    def plot_pair_trades(self):
        """
        MTFS analogue of MRPT's pair_trades chart.
        Plots:
          - Left axis: stock_1 price and hedge-adjusted stock_2 price (like MRPT)
          - Right axis (twin): momentum spread signal with entry/exit thresholds
          - Vertical lines at open/close trade events
        """
        try:
            xls = pd.ExcelFile(self.excel_filename)
            price_history = pd.read_excel(xls, 'price_history', parse_dates=['Date'])
            pair_trade_history = pd.read_excel(xls, 'pair_trade_history', parse_dates=['Date'])
            recorded_vars = pd.read_excel(xls, 'recorded_vars', parse_dates=['Date'])

            # Use hedge_history if available (kalman/beta), else build 1.0 ratio
            if 'hedge_history' in xls.sheet_names:
                hedge_history = pd.read_excel(xls, 'hedge_history', parse_dates=['Date'])
            else:
                hedge_history = None
        except Exception as e:
            log.warning(f"Skipping pair_trades plot: {e}")
            return

        n_pairs = len(self.context.strategy_pairs)
        fig, axs = plt.subplots(n_pairs, 1, figsize=(15, 7 * n_pairs), sharex=True)
        if n_pairs == 1:
            axs = [axs]

        price_colors = plt.cm.Set1(np.linspace(0, 1, 3))[:2]
        trade_colors = {'open': 'green', 'close': 'red'}

        for i, pair in enumerate(self.context.strategy_pairs):
            stock_1, stock_2 = pair[0], pair[1]
            pair_key = f"{stock_1}/{stock_2}"
            ax = axs[i]

            # ── Prices ──────────────────────────────────────────────────────
            p1 = price_history[['Date', stock_1]].dropna() if stock_1 in price_history.columns else None
            p2 = price_history[['Date', stock_2]].dropna() if stock_2 in price_history.columns else None

            if p1 is not None and p2 is not None:
                merged = p1.merge(p2, on='Date', suffixes=('_1', '_2'))

                # Hedge-adjusted stock_2 price
                if hedge_history is not None:
                    hcol = f'{pair_key}'
                    # hedge stored in sector-specific cols like Hedge_tech
                    hcols = [c for c in recorded_vars.columns if c.startswith('Hedge_')]
                    rv_pair = recorded_vars[recorded_vars['Pair'] == pair_key][['Date'] + hcols].copy()
                    if not rv_pair.empty:
                        rv_pair['hedge'] = rv_pair[hcols].bfill(axis=1).iloc[:, 0]
                        merged = merged.merge(rv_pair[['Date', 'hedge']], on='Date', how='left')
                        merged['hedge'] = merged['hedge'].ffill().fillna(1.0)
                    else:
                        merged['hedge'] = 1.0
                else:
                    merged['hedge'] = 1.0

                merged['adj_price_2'] = merged[stock_2] * merged['hedge']
                merged['price_spread'] = merged[stock_1] - merged['adj_price_2']

                ax.plot(merged['Date'], merged[stock_1], color=price_colors[0],
                        label=f'{stock_1} Price', alpha=0.8)
                ax.plot(merged['Date'], merged['adj_price_2'], color=price_colors[1],
                        label=f'{stock_2} Adjusted Price', alpha=0.8)
                ax.plot(merged['Date'], merged['price_spread'], color='gray',
                        label='Price Spread', linestyle='--', alpha=0.6)
                ax.yaxis.set_major_formatter(FuncFormatter(lambda x, p: f'${x:,.2f}'))

            # ── Momentum spread on twin axis ─────────────────────────────────
            rv_pair = recorded_vars[recorded_vars['Pair'] == pair_key].copy()
            spread_col = next((c for c in self._SPREAD_COLS if c in rv_pair.columns
                               and rv_pair[c].notna().any()), None)
            if spread_col is not None and not rv_pair.empty:
                rv_ms = rv_pair[['Date', spread_col, 'Entry_Threshold', 'Exit_Threshold']].dropna(
                    subset=[spread_col])
                if not rv_ms.empty:
                    ax2 = ax.twinx()
                    ax2.plot(rv_ms['Date'], rv_ms[spread_col], color='purple',
                             label='Mom Spread', linewidth=1.5, alpha=0.7)
                    if 'Entry_Threshold' in rv_ms.columns:
                        ax2.plot(rv_ms['Date'], rv_ms['Entry_Threshold'], color='orange',
                                 linestyle=':', alpha=0.5, label='Entry Thresh')
                        ax2.plot(rv_ms['Date'], -rv_ms['Entry_Threshold'], color='orange',
                                 linestyle=':', alpha=0.5)
                    ax2.set_ylabel('Momentum Spread', fontsize=8)
                    ax2.yaxis.set_major_formatter(FuncFormatter(lambda x, p: f'{x:.3f}'))
                    ax2.legend(loc='upper right', fontsize=7)

            # ── Trade events ────────────────────────────────────────────────
            pair_trades = pair_trade_history[pair_trade_history['Pair'] == pair_key]
            labels_used = set()
            for _, trade in pair_trades.iterrows():
                otype = trade['Order Type']
                color = trade_colors.get(otype, 'blue')
                lbl = f"{otype.capitalize()} {trade['Direction']}"
                ax.axvline(x=trade['Date'], color=color, linestyle='--', alpha=0.6,
                           label=lbl if lbl not in labels_used else '_')
                labels_used.add(lbl)

            ax.set_title(f'MTFS Pair: {pair_key}  —  Price Spread & Momentum Signal', fontsize=10)
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
            ax.xaxis.set_major_locator(mdates.MonthLocator())
            ax.grid(True, alpha=0.4)

            handles, labels_list = ax.get_legend_handles_labels()
            by_label = dict(zip(labels_list, handles))
            ax.legend(by_label.values(), by_label.keys(), fontsize=7, loc='upper left')

        plt.xticks(rotation=45, ha='right')
        plt.tight_layout()
        if self.chart_dir:
            plt.savefig(os.path.join(self.chart_dir, 'pair_trades.png'), dpi=150, bbox_inches='tight')
            plt.close(fig)
        else:
            plt.show()

    def plot_additional_histories(self):
        """
        MTFS analogue of MRPT's z_scores_and_pnl chart.
        Left panel: Momentum_1, Momentum_2, MomentumSpread, entry/exit thresholds, consistency.
        Right panel: DoD pair PnL bar chart.
        Saved as momentum_scores_and_pnl.png (same location as MRPT's z_scores_and_pnl.png).
        """
        n_pairs = len(self.context.strategy_pairs)
        fig, axs = plt.subplots(n_pairs, 2, figsize=(20, 8 * n_pairs), sharex=True)

        for i, pair in enumerate(self.context.strategy_pairs):
            stock_1, stock_2 = pair[0], pair[1]
            pair_key = f"{stock_1}/{stock_2}"

            ax1 = axs[i, 0] if n_pairs > 1 else axs[0]
            ax2 = axs[i, 1] if n_pairs > 1 else axs[1]

            all_dates = sorted(set(self.context.recorded_vars.get(pair_key, {}).keys()) |
                               set(self.portfolio.dod_pair_trade_pnl_history.keys()))

            mom_1_vals, mom_2_vals, spread_vals = [], [], []
            consistency_vals, entry_thresh_vals, exit_thresh_vals = [], [], []
            plot_dates = []

            for date in all_dates:
                data = self.context.recorded_vars.get(pair_key, {}).get(date, {})
                m1 = data.get('Momentum_1')
                m2 = data.get('Momentum_2')
                ms = self._get_momentum_spread(data)   # ← fixed: sector-specific lookup
                et = data.get('Entry_Threshold')
                xt = data.get('Exit_Threshold')
                c1 = data.get('Consistency_1')
                c2 = data.get('Consistency_2')

                if all(v is not None for v in [m1, m2, ms]):
                    mom_1_vals.append(m1)
                    mom_2_vals.append(m2)
                    spread_vals.append(ms)
                    entry_thresh_vals.append(et if et is not None else 0)
                    exit_thresh_vals.append(xt if xt is not None else 0.5)
                    consistency_vals.append(((c1 or 0) + (c2 or 0)) / 2.0)
                    plot_dates.append(date)

            if plot_dates:
                ax1.plot(plot_dates, mom_1_vals, label=f'{stock_1} Mom', alpha=0.8, linewidth=1.2)
                ax1.plot(plot_dates, mom_2_vals, label=f'{stock_2} Mom', alpha=0.8, linewidth=1.2)
                ax1.plot(plot_dates, spread_vals, label='Mom Spread', linewidth=2, color='black')
                ax1.plot(plot_dates, entry_thresh_vals, label='Entry Thresh',
                         linestyle='--', color='orange', alpha=0.7)
                ax1.plot(plot_dates, [-v for v in entry_thresh_vals], label='Short Entry',
                         linestyle='--', color='orange', alpha=0.7)
                ax1.axhline(0, color='gray', linewidth=0.5, alpha=0.5)
                ax1.fill_between(plot_dates, 0, consistency_vals,
                                 alpha=0.12, color='steelblue', label='Avg Consistency')

            ax1.set_title(f'{pair_key}  Momentum Scores & Spread', fontsize=9)
            ax1.legend(fontsize=7)
            ax1.grid(True, alpha=0.4)

            # DoD PnL bar chart (identical to MRPT)
            dod_pnl = self.portfolio.dod_pair_trade_pnl_history
            dod_dates, dod_values = [], []
            for date in all_dates:
                value = dod_pnl.get(date, {}).get(pair_key, {}).get('pnl_dollar', 0)
                if value != 0:
                    dod_dates.append(date)
                    dod_values.append(value)

            if dod_dates:
                colors = ['green' if v >= 0 else 'red' for v in dod_values]
                ax2.bar(dod_dates, dod_values, color=colors, alpha=0.7)
            ax2.set_title(f'{pair_key}  DoD Pair Trade PnL', fontsize=9)
            ax2.yaxis.set_major_formatter(FuncFormatter(lambda x, p: f'${x:,.0f}'))
            ax2.axhline(0, color='black', linewidth=0.5)
            ax2.grid(True, alpha=0.4)

            all_plot_dates = (plot_dates or []) + (dod_dates or [])
            if all_plot_dates:
                min_date, max_date = min(all_plot_dates), max(all_plot_dates)
                for ax in [ax1, ax2]:
                    ax.set_xlim(min_date, max_date)
                    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
                    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
                plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha='right')
                plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha='right')

        plt.tight_layout()
        if self.chart_dir:
            plt.savefig(os.path.join(self.chart_dir, 'momentum_scores_and_pnl.png'),
                        dpi=150, bbox_inches='tight')
            plt.close(fig)
        else:
            plt.show()

    def plot_vams_windows(self):
        """
        MTFS-specific chart — analogue of MRPT's z-score decomposition.
        For each pair: shows per-window VAMS scores (6d, 12d, 30d, 60d, 120d, 150d)
        for both stocks, showing which windows drive the composite momentum signal.
        Saved as vams_window_decomposition.png.
        """
        n_pairs = len(self.context.strategy_pairs)
        windows = [6, 12, 30, 60, 120, 150]
        colors = plt.cm.tab10(np.linspace(0, 1, len(windows)))

        fig, axs = plt.subplots(n_pairs, 2, figsize=(22, 7 * n_pairs), sharex=True)

        for i, pair in enumerate(self.context.strategy_pairs):
            stock_1, stock_2 = pair[0], pair[1]
            pair_key = f"{stock_1}/{stock_2}"

            ax1 = axs[i, 0] if n_pairs > 1 else axs[0]
            ax2 = axs[i, 1] if n_pairs > 1 else axs[1]

            all_dates = sorted(self.context.recorded_vars.get(pair_key, {}).keys())

            vams1 = {w: [] for w in windows}
            vams2 = {w: [] for w in windows}
            ret_spread = {w: [] for w in windows}
            plot_dates = []

            for date in all_dates:
                data = self.context.recorded_vars.get(pair_key, {}).get(date, {})
                # Check at least one window has data
                if data.get(f'VAMS1_{windows[0]}d') is None:
                    continue
                plot_dates.append(date)
                for w in windows:
                    vams1[w].append(data.get(f'VAMS1_{w}d', 0) or 0)
                    vams2[w].append(data.get(f'VAMS2_{w}d', 0) or 0)
                    ret_spread[w].append(data.get(f'RetSpread_{w}d', 0) or 0)

            if plot_dates:
                # ax1: VAMS scores per window for stock_1 vs stock_2 spread
                for j, w in enumerate(windows):
                    rs = ret_spread[w]
                    ax1.plot(plot_dates, rs, label=f'{w}d RetSpread',
                             color=colors[j], alpha=0.75, linewidth=1.2)
                ax1.axhline(0, color='black', linewidth=0.5, alpha=0.5)
                ax1.set_title(f'{pair_key}  Return Spread by Window', fontsize=9)
                ax1.legend(fontsize=6, ncol=2)
                ax1.grid(True, alpha=0.4)
                ax1.yaxis.set_major_formatter(FuncFormatter(lambda x, p: f'{x:.1%}'))

                # ax2: VAMS1 vs VAMS2 composite (which stock stronger per window)
                for j, w in enumerate(windows):
                    diff = [v1 - v2 for v1, v2 in zip(vams1[w], vams2[w])]
                    ax2.plot(plot_dates, diff, label=f'{w}d VAMS diff',
                             color=colors[j], alpha=0.75, linewidth=1.2)
                ax2.axhline(0, color='black', linewidth=0.8)
                ax2.set_title(f'{pair_key}  VAMS({stock_1}−{stock_2}) by Window', fontsize=9)
                ax2.legend(fontsize=6, ncol=2)
                ax2.grid(True, alpha=0.4)
                ax2.yaxis.set_major_formatter(FuncFormatter(lambda x, p: f'{x:.2f}'))

            for ax in [ax1, ax2]:
                if plot_dates:
                    ax.set_xlim(min(plot_dates), max(plot_dates))
                    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
                    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
                plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha='right')

        plt.tight_layout()
        if self.chart_dir:
            plt.savefig(os.path.join(self.chart_dir, 'vams_window_decomposition.png'),
                        dpi=150, bbox_inches='tight')
            plt.close(fig)
        else:
            plt.show()


def main(config=None):
    """Run a single MTFS backtest.

    Args:
        config: Optional dict with keys:
            - pairs: list of [stock1, stock2] pairs
            - params: dict of execution parameter overrides
            - pair_params: per-pair param overrides keyed by "S1/S2"
            - run_label: string label for this run
            - output_dir: base output directory
            - historical_data: pre-loaded DataFrame
            - start_date: backtest start date
            - end_date: backtest end date
            - trade_start_date: optional warmup cutoff

    Returns:
        dict with run results
    """
    config = config or {}
    pairs = config.get('pairs')
    params = config.get('params', {})
    pair_params = config.get('pair_params', {})
    run_label = config.get('run_label', '')
    base_dir = config.get('output_dir', os.path.dirname(os.path.abspath(__file__)))
    preloaded_data = config.get('historical_data')
    cfg_start = config.get('start_date', '2024-12-01')
    from datetime import timedelta
    one_month_ago = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    cfg_end = config.get('end_date', one_month_ago)
    cfg_trade_start = config.get('trade_start_date')

    # Set up output directories
    charts_dir = os.path.join(base_dir, 'charts')
    logs_dir = os.path.join(base_dir, 'logs')
    runs_dir = os.path.join(base_dir, 'historical_runs')
    os.makedirs(charts_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)
    os.makedirs(runs_dir, exist_ok=True)

    run_timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    run_name = f"MTFS_{run_label}_{run_timestamp}" if run_label else f"MTFS_{run_timestamp}"

    # Set up file logging
    log_file = os.path.join(logs_dir, f'run_{run_name}.log')
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logging.getLogger().addHandler(file_handler)

    try:
        return _run_backtest(config, pairs, params, pair_params, run_name, base_dir,
                             charts_dir, runs_dir, preloaded_data, cfg_start, cfg_end,
                             cfg_trade_start)
    finally:
        logging.getLogger().removeHandler(file_handler)
        file_handler.close()


def _run_backtest(config, pairs, params, pair_params, run_name, base_dir,
                  charts_dir, runs_dir, preloaded_data, start_date, end_date,
                  trade_start_date=None):
    """Internal backtest execution for MTFS."""
    context = Context()
    initialize(context, pairs=pairs, params=params, pair_params=pair_params)
    context.trade_start_date = pd.Timestamp(trade_start_date) if trade_start_date else None
    if context.trade_start_date:
        log.info(f"Warmup-only until {trade_start_date}; trading starts from {trade_start_date}")

    run_charts_dir = os.path.join(charts_dir, run_name)
    os.makedirs(run_charts_dir, exist_ok=True)

    context.output_filename = os.path.join(runs_dir, f'portfolio_history_{run_name}.xlsx')

    log.info(f"MTFS backtest: {start_date} to {end_date}")

    symbols = list(dict.fromkeys(
        sym for pair in context.strategy_pairs for sym in [pair[0], pair[1]]
    ))

    if preloaded_data is not None:
        historical_data = preloaded_data
        log.info(f"Using pre-loaded data with {len(historical_data)} rows")
    else:
        historical_data = load_historical_data(start_date, end_date, symbols)

    log.info(f"Data sample:\n{historical_data.head()}")
    log.info(f"Data columns: {historical_data.columns}")

    if historical_data.empty:
        log.error("No data available. Exiting.")
        return None

    full_data = historical_data.copy()
    if full_data.empty:
        log.error("No data available. Exiting.")
        return None

    log.info(f"Starting MTFS backtest with {len(full_data)} days of data")
    log.info(f"Params: {params}")

    portfolio_analysis = PortfolioAnalysis(context.portfolio)

    for date in full_data.index:
        try:
            context.portfolio.current_date = date
            context.portfolio.processed_dates.append(date)
            current_historical_data = historical_data.loc[:date]
            data = CustomData(current_historical_data)

            context.warmup_mode = bool(context.trade_start_date and date < context.trade_start_date)
            if context.warmup_mode:
                _saved_rate = context.portfolio.interest_rate
                context.portfolio.interest_rate = 0.0

            my_handle_data(context, data)

            if context.warmup_mode:
                context.portfolio.interest_rate = _saved_rate
                for hist_attr in (
                    'asset_cash_history', 'liability_loan_history',
                    'asset_history', 'liability_history',
                    'equity_history', 'value_history',
                    'daily_pnl_history',
                    'interest_expense_history', 'acc_interest_history',
                    'acc_daily_pnl_history',
                ):
                    lst = getattr(context.portfolio, hist_attr)
                    if lst:
                        lst.pop()
            else:
                daily_pnl = context.portfolio.update_pnl_history(
                    portfolio_analysis, data, symbols)

            if len(context.portfolio.processed_dates) % 100 == 0:
                log.info(f"Processed {len(context.portfolio.processed_dates)} days. Current date: {date}")
        except Exception as e:
            log.error(f"Error processing date {date}: {str(e)}")
            import traceback
            log.error(traceback.format_exc())
            continue

    if not context.portfolio.asset_history:
        log.error("No portfolio history was created. Strategy did not execute properly.")
        return None

    # Final analysis
    final_equity = context.portfolio.equity_history[-1][1]
    final_asset = context.portfolio.asset_history[-1][1]
    final_liability = context.portfolio.liability_history[-1][1]
    final_cash = context.portfolio.asset_cash_history[-1][1]

    log.info("Final MTFS Portfolio State:")
    log.info(f"Total Asset: {final_asset}")
    log.info(f"Total Liability: {final_liability}")
    log.info(f"Net Equity: {final_equity}")
    log.info(f"Cash: {final_cash}")

    acc_pnl = None
    if context.portfolio.value_history:
        log.info(f"Value History: {context.portfolio.value_history[-1][1]}")
    if context.portfolio.acc_interest_history:
        log.info(f"Accumulated Interest: {context.portfolio.acc_interest_history[-1][1]}")
    if context.portfolio.acc_daily_pnl_history:
        acc_pnl = context.portfolio.acc_daily_pnl_history[-1][1]
        log.info(f"Accumulated Total P&L Including Interest: {acc_pnl}")

    max_dd_dollar = 0
    max_dd_pct = 0
    if context.portfolio.max_drawdown_history:
        max_drawdown = context.portfolio.max_drawdown_history[-1]
        max_dd_dollar = max_drawdown[1]
        max_dd_pct = max_drawdown[2]
        log.info(f"Final Max Drawdown: ${max_dd_dollar:.2f} ({max_dd_pct:.2%})")

    trading_days_percentage = portfolio_analysis.calculate_trading_days_percentage(
        context.portfolio)
    log.info(f"Percentage of days with open positions: {trading_days_percentage:.2%}")

    trade_summary = {}
    if context.portfolio.pair_trade_history and context.portfolio.acc_pair_trade_pnl_history:
        trade_summary = summarize_pair_trade_history(
            context.portfolio.pair_trade_history,
            context.portfolio.acc_pair_trade_pnl_history)
        log.info("Pair Trade History Summary:")
        for pair, stats in trade_summary.items():
            log.info(f"{pair}:")
            log.info(f"  Total trades: {stats['total_trades']} "
                     f"(Long: {stats['long_trades']}, Short: {stats['short_trades']})")
            log.info(f"  Total volume: {stats['total_volume']:.2f}")
            log.info(f"  Accumulated P&L: ${stats['net_pnl']:.2f}")
            log.info("--------------------")

    # Write Excel
    try:
        exporter = ExportExcel(context.output_filename)
        exporter.export_portfolio_data(context.portfolio, context)
        log.info(f"Excel saved to {context.output_filename}")
    except Exception as e:
        log.error(f"Error writing Excel: {str(e)}")
        import traceback
        log.error(traceback.format_exc())

    # Create visualizations — use MTFS-specific visualizer
    try:
        visualizer = MTFSPortfolioVisualizer(
            context.portfolio, context, context.output_filename,
            chart_dir=run_charts_dir)
        visualizer.plot_all_histories()
        log.info(f"Charts saved to {run_charts_dir}")
    except Exception as e:
        log.error(f"Error creating visualizations: {str(e)}")
        import traceback
        log.error(traceback.format_exc())

    # Compute Sharpe Ratio
    sharpe_ratio = None
    if len(context.portfolio.equity_history) > 2:
        equities = [v for _, v in context.portfolio.equity_history]
        daily_returns = [(equities[i] - equities[i-1]) / equities[i-1]
                         for i in range(1, len(equities)) if equities[i-1] != 0]
        if daily_returns:
            avg_ret = np.mean(daily_returns)
            std_ret = np.std(daily_returns)
            if std_ret > 0:
                sharpe_ratio = (avg_ret / std_ret) * np.sqrt(252)
    log.info(f"Sharpe Ratio: {sharpe_ratio}")

    results = {
        'run_name': run_name,
        'params': params,
        'pairs': [[p[0], p[1]] for p in context.strategy_pairs],
        'final_equity': final_equity,
        'final_asset': final_asset,
        'final_liability': final_liability,
        'acc_pnl': acc_pnl,
        'max_drawdown_dollar': max_dd_dollar,
        'max_drawdown_pct': max_dd_pct,
        'sharpe_ratio': sharpe_ratio,
        'trading_days_pct': trading_days_percentage,
        'trade_summary': trade_summary,
        'output_file': context.output_filename,
        'charts_dir': run_charts_dir,
    }
    return results


if __name__ == "__main__":
    main()
