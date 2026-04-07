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
from PortfolioClasses import CurrentData, date_rules, time_rules, Trade, Portfolio, ADF, KPSS, Data, Half_Life, Hurst, PortfolioAnalysis, ExportExcel, Context, Execution, PortfolioVisualizer, PortfolioStopLossFunction, PortfolioMakeOrder, PortfolioConstruct

import quantstats as qs
import math

# Set up logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ~~~~~~~~~~~~~~~~~~~~~~ DATA SOURCE CONFIGURATION ~~~~~~~~~~~~~~~~~~~~~~
# 'mongo' (default), 'polygon', or 'yahoo'
# Waterfall: mongo → polygon (PriceDataStore + Parquet cache) → yahoo
DATA_SOURCE = 'mongo'
POLYGON_API_KEY = os.environ['POLYGON_API_KEY']

# ~~~~~~~~~~~~~~~~~~~~~~ FUNCTIONS FOR FILING AN ORDER ~~~~~~~~~~~~~~~~~~~~~~

def schedule_function(func, date_rule, time_rule):
    scheduled_functions = []
    scheduled_functions.append((func, date_rule, time_rule))


from pair_universe import mrpt_pairs as _mrpt_pairs
DEFAULT_PAIRS = [[s1, s2] for s1, s2 in _mrpt_pairs()]


def _is_earnings_blackout(context, s1, s2, date):
    """True if date falls within the earnings blackout window for s1 or s2.

    Timing-aware blackout windows (relative to earnings_date = market reaction date):
      AMC (after market close): block day-1 and day 0
            day-1 = the filing day (you open a position, then get gapped out next morning)
            day 0 = the reaction day (gap already happened, spread is dislocated)
      BMO (before market open): block day 0 only
            earnings release happens before open → spread dislocation on day 0 itself
      INTRADAY: block day 0 only
      UNKNOWN (no acceptance_datetime — usually annual filings): treat as AMC, block day-1 and day 0

    Called each day fresh — stateless.
    """
    d = date.date()
    for sym in (s1, s2):
        for ed_str, timing in context.earnings_dates.get(sym, []):
            ed = pd.Timestamp(ed_str).date()
            delta = (d - ed).days  # positive = d is after earnings date
            if timing == 'AMC':
                if delta in (-1, 0):
                    return True
            elif timing in ('BMO', 'INTRADAY'):
                if delta == 0:
                    return True
            else:  # UNKNOWN — treat as AMC (conservative: annual filings are almost always AMC)
                if delta in (-1, 0):
                    return True
    return False


def initialize(context, pairs=None, params=None, pair_params=None):
    # Build strategy pairs with state dicts
    raw_pairs = pairs if pairs is not None else DEFAULT_PAIRS
    context.strategy_pairs = [
        [p[0], p[1], {'in_short': False, 'in_long': False, 'spread': np.array([]), 'hedge_history': np.array([])}]
        for p in raw_pairs
    ]
    # pair_params: dict keyed by "STOCK1/STOCK2" -> param dict override for that pair
    context.pair_params = pair_params or {}

    context.initial_cash = 500000
    context.initial_loan = 500000
    context.interest_rate = 0.05  # 5% annual interest rate
    context.num_pairs = len(context.strategy_pairs)

    # Create the portfolio with strategy_pairs
    context.portfolio = Portfolio(
        initial_cash=context.initial_cash,
        initial_loan=context.initial_loan,
        interest_rate=context.interest_rate,
        strategy_pairs=context.strategy_pairs
    )

    context.execution = Execution()

    # Apply parameter overrides from params dict
    p = params or {}
    context.execution.z_back = p.get('z_back', 36)
    context.execution.v_back = p.get('v_back', 32)
    context.execution.hedge_lag = p.get('hedge_lag', 1)
    context.execution.base_entry_z = p.get('base_entry_z', 0.75)
    context.execution.base_exit_z = p.get('base_exit_z', 0.0)
    context.execution.entry_volatility_factor = p.get('entry_volatility_factor', 2.25)
    context.execution.exit_volatility_factor = p.get('exit_volatility_factor', 0.75)
    context.execution.amplifier = p.get('amplifier', 2)
    context.execution.capital_utilization = p.get('capital_utilization', 0.70)
    context.execution.volatility_stop_loss_multiplier = p.get('volatility_stop_loss_multiplier', 2)
    context.execution.max_holding_period = p.get('max_holding_period', 12)
    context.execution.cooling_off_period = p.get('cooling_off_period', 2)

    context.portfolio_construct = PortfolioConstruct(constant=1)
    context.portfolio_order = PortfolioMakeOrder(constant=1)
    context.portfolio_stoploss_function = PortfolioStopLossFunction(constant=1)

    # Initialize recorded_vars
    context.recorded_vars = {}

    # Load earnings blackout dates (MRPT-specific: no new opens near earnings)
    # Stores list of (earnings_date, release_timing) per symbol for timing-aware blackout windows.
    _ec_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'price_data', 'earnings_cache.json')
    context.earnings_dates = {}
    if os.path.exists(_ec_path):
        import json as _json
        _ec = _json.load(open(_ec_path))
        for _sym, _entries in _ec.get('symbols', {}).items():
            context.earnings_dates[_sym] = sorted(
                (e['earnings_date'], e.get('release_timing', 'UNKNOWN'))
                for e in _entries
            )
        log.info(f"Earnings blackout filter loaded: {len(context.earnings_dates)} symbols")
    else:
        log.warning("price_data/earnings_cache.json not found — earnings blackout filter disabled")

    log.info("Algorithm initialized")


def my_handle_data(context, data):
    """
    Called every day.
    """
    context.data = data

    if context.portfolio_order.get_open_orders():
        return

    # Get all prices first
    all_prices = {}
    try:
        for pair in context.strategy_pairs:
            stock_1, stock_2 = pair[0], pair[1]
            # Try to get 'Adj Close' first, then fall back to 'Close' if needed
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

    # Update price history
    context.portfolio.update_price_history(context.portfolio.current_date, all_prices)

    # Process pairs and update other portfolio values
    for i in range(len(context.strategy_pairs)):
        pair = context.strategy_pairs[i]
        new_pair = process_pair(pair, context, data)
        context.strategy_pairs[i] = new_pair

    # Update other portfolio values
    context.portfolio.update_values(all_prices)
    context.portfolio.update_max_drawdown()


def process_pair(pair, context, data):
    """
    Main function that will execute an order for every pair.
    NOTE: export_portfolio_data is NOT called here — all data accumulates in memory
    and is written to Excel once at the end of _run_backtest().
    """
    # Get stock data
    stock_1 = pair[0]
    stock_2 = pair[1]

    context.current_pair = (stock_1, stock_2)
    pair_key = f"{stock_1}/{stock_2}"

    # Apply per-pair parameter overrides to context.execution
    _saved_exec_params = {}
    _PAIR_PARAM_KEYS = [
        'z_back', 'v_back', 'hedge_lag', 'base_entry_z', 'base_exit_z',
        'entry_volatility_factor', 'exit_volatility_factor', 'amplifier',
        'volatility_stop_loss_multiplier', 'max_holding_period', 'cooling_off_period',
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
    """Inner body of process_pair — called with per-pair params already applied."""
    try:
        # Try to get 'Adj Close' first, then fall back to 'Close' if needed
        try:
            prices = data.history([stock_1, stock_2], "Adj Close", 300, "1d")
        except Exception as e:
            log.warning(f"'Adj Close' not available for historical data, falling back to 'Close': {e}")
            prices = data.history([stock_1, stock_2], "Close", 300, "1d")

        stock_1_P = prices[stock_1]
        stock_2_P = prices[stock_2]
    except Exception as e:
        log.error(f"Error getting historical prices for {stock_1}/{stock_2}: {e}")
        return [stock_1, stock_2, pair[2]]  # Return unchanged pair on error
        
    in_short = pair[2]['in_short']
    in_long = pair[2]['in_long']
    spread = pair[2]['spread']
    hedge_history = pair[2]['hedge_history']

    # Get hedge ratio (look into using Kalman Filter)
    try:
        hedge = context.portfolio_construct.hedge_ratio(stock_1_P, stock_2_P)
        pair_key = f"{stock_1}/{stock_2}"
        if pair_key not in context.portfolio.hedge_history:
            context.portfolio.hedge_history[pair_key] = []
        context.portfolio.hedge_history[pair_key].append((context.portfolio.current_date, hedge))

    except ValueError as e:
        log.error(e)
        record_vars(context, Y_pct=0, X_pct=0)
        return [stock_1, stock_2,
                {'in_short': in_short, 'in_long': in_long, 'spread': spread, 'hedge_history': hedge_history}]

    hedge_history = np.append(hedge_history, hedge)

    if hedge_history.size < context.execution.hedge_lag:
        log.debug("Hedge history too short!")
        record_vars(context, Y_pct=0, X_pct=0)
        return [stock_1, stock_2,
                {'in_short': in_short, 'in_long': in_long, 'spread': spread, 'hedge_history': hedge_history}]

    hedge = hedge_history[-context.execution.hedge_lag]
    spread = np.append(
        spread, stock_1_P.iloc[-1] - hedge * stock_2_P.iloc[-1])
    # handle the case hedge ratio is negative
    spread_length = spread.size

    # Calculate z_score earlier in the function
    if spread_length >= context.execution.z_back:
        spreads_for_z_score = spread[-context.execution.z_back:]
        z_score = (spreads_for_z_score[-1] - spreads_for_z_score.mean()) / spreads_for_z_score.std()
    else:
        z_score = None  # or some default value

    # Calculate z_score earlier in the function
    if spread_length >= context.execution.v_back:
        spreads_for_volatility = spread[-context.execution.v_back:]
        current_volatility_of_spreads = np.std(spreads_for_volatility)
    else:
        current_volatility_of_spreads = None  # or some default value

    # Check if current window size is large enough for Z score
    if spread_length < context.execution.z_back or spread_length < context.execution.v_back:
        record_vars(context, Y_pct=0, X_pct=0)
        return [stock_1, stock_2,
                {'in_short': in_short, 'in_long': in_long, 'spread': spread, 'hedge_history': hedge_history}]

    # Update volatilities_in_window for this pair
    if pair_key not in context.portfolio.volatilities_in_window:
        context.portfolio.volatilities_in_window[pair_key] = []

    context.portfolio.volatilities_in_window[pair_key].append(current_volatility_of_spreads)

    max_volatility_in_window_back = context.execution.v_back // 2

    # Keep only the last max_volatility_in_window_back days for this pair
    context.portfolio.volatilities_in_window[pair_key] = context.portfolio.volatilities_in_window[pair_key][-max_volatility_in_window_back:]

    # Calculate dynamic entry and exit z-scores
    normalized_volatility, entry_z, exit_z = context.portfolio_construct.calculate_dynamic_z_scores_entry_exit(context, pair_key, current_volatility_of_spreads)

    record_vars(context, Z=z_score, Hedge=hedge,
                Normalized_Spread_Sigma=normalized_volatility, Entry_Z=entry_z, Exit_Z=exit_z,
                in_long=in_long, in_short=in_short)

    # Check for stop loss conditions
    if in_short or in_long:
        volatility_stop, volatility_reason, volatility_level = context.portfolio_stoploss_function.check_volatility_stop_loss(context, pair, spread,
                                                                                          z_score)
        time_stop, time_reason, _ = context.portfolio_stoploss_function.check_time_based_stop_loss(context, pair)
        price_stop, price_reason, price_level = context.portfolio_stoploss_function.check_price_level_stop_loss(context, pair, spread, z_score)

        if volatility_stop:
            record_vars(context, Z=z_score, Hedge=hedge, Y_pct=0, X_pct=0,
                        in_long=in_long, in_short=in_short, stop_loss_triggered=True, stop_loss_reason=volatility_reason)
            return context.portfolio_stoploss_function.handle_stop_loss(context, pair, volatility_reason, z_score)
        elif time_stop:
            record_vars(context, Z=z_score, Hedge=hedge, Y_pct=0, X_pct=0,
                        in_long=in_long, in_short=in_short, stop_loss_triggered=True, stop_loss_reason=time_reason)
            return context.portfolio_stoploss_function.handle_stop_loss(context, pair, time_reason, z_score)
        elif price_stop:
            record_vars(context, Z=z_score, Hedge=hedge, Y_pct=0, X_pct=0,
                        in_long=in_long, in_short=in_short, stop_loss_triggered=True, stop_loss_reason=price_reason)
            return context.portfolio_stoploss_function.handle_stop_loss(context, pair, price_reason, z_score)

    adf = ADF()
    half_life = Half_Life()
    hurst = Hurst()
    kpss = KPSS()

    # Check if current window size is large enough for adf, half life, and hurst exponent
    if (spread_length < adf.look_back) or (spread_length < half_life.look_back) or (spread_length < hurst.look_back) or (spread_length < kpss.look_back):
        record_vars(context, Z=z_score, Hedge=hedge, Y_pct=0, X_pct=0)
        return [stock_1, stock_2,
                {'in_short': in_short, 'in_long': in_long, 'spread': spread, 'hedge_history': hedge_history}]

    # possible "SVD did not converge" error because of OLS
    try:
        adf.apply_adf(spread[-adf.look_back:])
        half_life.apply_half_life(spread[-half_life.look_back:])
        hurst.apply_hurst(spread[-hurst.look_back:])
        kpss.apply_kpss(spread[-kpss.look_back:])

        # Record statistical test results
        if pair_key not in context.portfolio.statistical_test_history:
            context.portfolio.statistical_test_history[pair_key] = {}

        test_results = {}
        for test in [adf, half_life, hurst, kpss]:
            test_name = test.__class__.__name__.lower()
            for attr, value in test.__dict__.items():
                test_results[f"{test_name}_{attr}"] = value

        context.portfolio.statistical_test_history[pair_key][context.portfolio.current_date] = test_results

    except Exception as e:
        log.warning(f"Error in statistical tests for {stock_1}/{stock_2}: {str(e)}")
        record_vars(context, Z=z_score, Hedge=hedge, Y_pct=0, X_pct=0)
        return [stock_1, stock_2,
                {'in_short': in_short, 'in_long': in_long, 'spread': spread, 'hedge_history': hedge_history}]

    # Check if they are in fact a stationary (or possibly trend stationary...need to avoid this) time series
    # * Only cancel if all measures believe it isn't stationary
    if not adf.use_P() and not adf.use_critical() and not half_life.use() and not hurst.use() and not kpss.use():
        if in_short or in_long:
            # Enter logic here for how to handle open positions after mean reversion
            # of spread breaks down.
            log.info('Tests have failed. Exiting open positions')
            # Close the position
            context.portfolio_order.order_target(context, stock_1, 0)
            context.portfolio_order.order_target(context, stock_2, 0)
            in_short = in_long = False
            record_vars(context, Z=z_score, Hedge=hedge, Y_pct=0, X_pct=0)
            return [stock_1, stock_2,
                    {'in_short': in_short, 'in_long': in_long, 'spread': spread, 'hedge_history': hedge_history}]

        log.debug("Not Stationary!")
        return [stock_1, stock_2,
                {'in_short': in_short, 'in_long': in_long, 'spread': spread, 'hedge_history': hedge_history}]

    # Skip all order logic during warmup — only build up state (spread, hedge, z-score)
    if getattr(context, 'warmup_mode', False):
        return [stock_1, stock_2,
                {'in_short': in_short, 'in_long': in_long, 'spread': spread, 'hedge_history': hedge_history}]

    # Close order logic
    if in_short and z_score < exit_z:
        context.portfolio_order.order_target(context, stock_1, 0)
        context.portfolio_order.order_target(context, stock_2, 0)
        in_short = False
        in_long = False
        record_vars(context, Y_pct=0, X_pct=0, in_long=in_long, in_short=in_short, action='CLOSE')
        return [stock_1, stock_2,
                {'in_short': in_short, 'in_long': in_long, 'spread': spread, 'hedge_history': hedge_history}]
    elif in_long and z_score > -exit_z:
        context.portfolio_order.order_target(context, stock_1, 0)
        context.portfolio_order.order_target(context, stock_2, 0)
        in_short = False
        in_long = False
        record_vars(context, Y_pct=0, X_pct=0, in_long=in_long, in_short=in_short, action='CLOSE')
        return [stock_1, stock_2,
                {'in_short': in_short, 'in_long': in_long, 'spread': spread, 'hedge_history': hedge_history}]

    # Earnings blackout: skip opening NEW positions near either symbol's earnings date.
    # AMC/UNKNOWN: block day-1 and day 0; BMO/INTRADAY: block day 0 only.
    # Closes and stop-losses above are unaffected. Stateless — checked fresh each day.
    if not in_long and not in_short:
        if _is_earnings_blackout(context, stock_1, stock_2, context.portfolio.current_date):
            log.debug(f"Earnings blackout: skipping {pair_key} open on {context.portfolio.current_date.date()}")
            _blackout_syms = []
            for _sym in (stock_1, stock_2):
                for ed_str, timing in context.earnings_dates.get(_sym, []):
                    ed = pd.Timestamp(ed_str).date()
                    d  = context.portfolio.current_date.date()
                    if ed == d or (timing in ('AMC', 'UNKNOWN') and ed == d + pd.Timedelta(days=1).to_pytimedelta().__class__(days=1)):
                        _blackout_syms.append(f"{_sym} {ed_str}")
            record_vars(context, Y_pct=0, X_pct=0, in_long=in_long, in_short=in_short,
                        action='BLACKOUT', earnings_blackout=True,
                        earnings_blackout_reason=', '.join(_blackout_syms) if _blackout_syms else 'earnings window')
            return [stock_1, stock_2,
                    {'in_short': in_short, 'in_long': in_long, 'spread': spread, 'hedge_history': hedge_history}]

    # Open order logic
    if (z_score < -entry_z) and (not in_long):
        if pair_key in context.execution.stop_loss_history and context.execution.stop_loss_history[pair_key]:
            last_stop_loss_event = context.execution.stop_loss_history[pair_key][-1]
            # Check if the last event was a stop loss (not a cooling-off period end)
            if last_stop_loss_event['reason'] != "Cooling-off period ended":
                if not context.portfolio_stoploss_function.re_evaluate_pair(context, pair):
                    return [stock_1, stock_2,
                            {'in_short': in_short, 'in_long': in_long, 'spread': spread,
                             'hedge_history': hedge_history}]

        stock_1_shares = 1
        stock_2_shares = -hedge
        in_long = True
        in_short = False
        (stock_1_perc, stock_2_perc) = context.portfolio_order.computeHoldingsPct(stock_1_shares, stock_2_shares, stock_1_P.iloc[-1], stock_2_P.iloc[-1])
        context.portfolio_order.order_target_percent(context, stock_1, stock_1_perc * context.execution.amplifier * context.execution.capital_utilization / context.num_pairs)
        context.portfolio_order.order_target_percent(context, stock_2, stock_2_perc * context.execution.amplifier * context.execution.capital_utilization / context.num_pairs)
        record_vars(context, Y_pct=stock_1_perc, X_pct=stock_2_perc,
                    in_long=in_long, in_short=in_short, action='OPEN_LONG')

        # Set price-level stop loss
        context.execution.price_level_stop_loss[pair_key] = spread[-1] * 0.8  # Example: 20% below entry level

        return [stock_1, stock_2,
                {'in_short': in_short, 'in_long': in_long, 'spread': spread, 'hedge_history': hedge_history}]
    elif (z_score > entry_z) and (not in_short):
        if pair_key in context.execution.stop_loss_history and context.execution.stop_loss_history[pair_key]:
            last_stop_loss_event = context.execution.stop_loss_history[pair_key][-1]
            # Check if the last event was a stop loss (not a cooling-off period end)
            if last_stop_loss_event['reason'] != "Cooling-off period ended":
                if not context.portfolio_stoploss_function.re_evaluate_pair(context, pair):
                    return [stock_1, stock_2,
                            {'in_short': in_short, 'in_long': in_long, 'spread': spread,
                             'hedge_history': hedge_history}]

        stock_1_shares = -1
        stock_2_shares = hedge
        in_short = True
        in_long = False
        (stock_1_perc, stock_2_perc) = context.portfolio_order.computeHoldingsPct(stock_1_shares, stock_2_shares, stock_1_P.iloc[-1], stock_2_P.iloc[-1])
        context.portfolio_order.order_target_percent(context, stock_1, stock_1_perc * context.execution.amplifier * context.execution.capital_utilization / context.num_pairs)
        context.portfolio_order.order_target_percent(context, stock_2, stock_2_perc * context.execution.amplifier * context.execution.capital_utilization / context.num_pairs)
        record_vars(context, Y_pct=stock_1_perc, X_pct=stock_2_perc,
                    in_long=in_long, in_short=in_short, action='OPEN_SHORT')

        # Set price-level stop loss
        context.execution.price_level_stop_loss[pair_key] = spread[-1] * 1.5  # Example: 50% beyond entry level

        return [stock_1, stock_2,
                {'in_short': in_short, 'in_long': in_long, 'spread': spread, 'hedge_history': hedge_history}]

    return [stock_1, stock_2,
            {'in_short': in_short, 'in_long': in_long, 'spread': spread, 'hedge_history': hedge_history}]


def record_vars(context, **kwargs):
    current_date = context.portfolio.current_date
    pair = f"{context.current_pair[0]}/{context.current_pair[1]}"
    stock_1, stock_2 = context.current_pair

    if pair not in context.recorded_vars:
        context.recorded_vars[pair] = {}

    if current_date not in context.recorded_vars[pair]:
        context.recorded_vars[pair][current_date] = {}

    # Determine the sector based on the stocks in the pair
    from pair_universe import sector_sets_mrpt as _ss
    _ss_map = _ss()
    _tech_stocks       = _ss_map.get('tech', set())
    _finance_stocks    = _ss_map.get('finance', set())
    _industrial_stocks = _ss_map.get('industrial', set())
    _energy_stocks     = _ss_map.get('energy', set())
    _food_stocks       = _ss_map.get('food', set())

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
        sector = 'food'  # fallback

    for key, value in kwargs.items():
        if key == 'Z':
            key = f'Z_{sector}'
        elif key == 'Hedge':
            key = f'Hedge_{sector}'
        context.recorded_vars[pair][current_date][key] = value


def summarize_pair_trade_history(pair_trade_history, acc_pair_trade_pnl_history):
    summary = {}
    for pair, trades in pair_trade_history.items():
        long_trades = [t for t in trades if t.direction == 'long' and t.order_type == 'open']
        short_trades = [t for t in trades if t.direction == 'short' and t.order_type == 'open']

        # Get the latest P&L for this pair
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
    """Check the structure of the data and fix it if necessary"""
    log.info(f"Checking data structure: shape={data.shape}, columns={data.columns.names}")
    
    # If the data is not multi-indexed, convert it
    if not isinstance(data.columns, pd.MultiIndex):
        log.info("Data is not multi-indexed. Converting to proper format...")
        
        # Check if we have price columns without multi-index
        if 'Adj Close' in data.columns or 'Close' in data.columns:
            # Create a new DataFrame with proper MultiIndex
            symbols = []
            for col in data.columns:
                if '.' in col:  # Handle column names like 'AAPL.Close'
                    symbol, field = col.split('.')
                    symbols.append(symbol)
            
            if not symbols:  # If we didn't find symbols with dot notation
                symbols = list(set([col.split(' ')[0] for col in data.columns if ' ' in col]))
            
            if not symbols:  # If we still didn't find symbols, use all columns
                # Assume first set of columns are for first symbol, second set for second symbol, etc.
                price_cols = ['Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume']
                num_symbols = len(data.columns) // len(price_cols)
                symbols = [f'Symbol{i+1}' for i in range(num_symbols)]
            
            # Create new multi-index columns
            new_data = pd.DataFrame(index=data.index)
            for symbol in symbols:
                for field in ['Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume']:
                    if f'{symbol}.{field}' in data.columns:
                        new_data[(field, symbol)] = data[f'{symbol}.{field}']
                    elif f'{field} {symbol}' in data.columns:
                        new_data[(field, symbol)] = data[f'{field} {symbol}']
                    elif field in data.columns and symbols.index(symbol) == 0:
                        # If only one symbol, use the field directly
                        new_data[(field, symbol)] = data[field]
            
            data = new_data
            log.info(f"Converted data to multi-index with shape={data.shape}")
        
        # If we couldn't convert it, create multi-index columns
        if not isinstance(data.columns, pd.MultiIndex):
            # Create a new MultiIndex
            fields = ['Close', 'Adj Close', 'Open', 'High', 'Low', 'Volume']
            tuples = []
            for col in data.columns:
                for field in fields:
                    if field.lower() in col.lower():
                        symbol = col.replace(field, '').strip()
                        if not symbol:
                            symbol = col  # If we couldn't extract a symbol, use the column name
                        tuples.append((field, symbol))
                        break
                else:
                    # If no field was found, assume it's 'Close'
                    tuples.append(('Close', col))
            
            # Create the MultiIndex
            data.columns = pd.MultiIndex.from_tuples(tuples, names=['Price', 'Symbol'])
            log.info(f"Created MultiIndex columns: {data.columns}")
    
    # Verify that 'Adj Close' is available, and if not, copy from 'Close'
    all_fields = set([field for field, _ in data.columns])
    if 'Adj Close' not in all_fields and 'Close' in all_fields:
        log.info("'Adj Close' not found in data, creating it from 'Close'")
        for symbol in set([symbol for _, symbol in data.columns]):
            if ('Close', symbol) in data.columns:
                data[('Adj Close', symbol)] = data[('Close', symbol)]
    
    return data


def _fetch_polygon_dividends(symbol, start_date, end_date):
    """Fetch dividend data from Polygon.io for a given symbol and date range."""
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
    """Compute dividend-adjusted close price (matching Yahoo Adj Close).

    Polygon adjusted=true only adjusts for splits. This function additionally
    adjusts for dividends using the CRSP standard formula:
        factor = 1 - (cash_amount / close_day_before_ex_date)
    Applied cumulatively from latest dividend to earliest.
    """
    adj = df['c'].copy()
    # Process dividends from latest to earliest
    for div in reversed(dividends):
        ex_date = pd.Timestamp(div['ex_dividend_date'])
        amount = div['cash_amount']
        mask = adj.index < ex_date
        if not mask.any():
            continue
        # Get the close price on the day before (or last trading day before) ex-date
        pre_ex_close = df.loc[adj.index[mask][-1], 'c']
        if pre_ex_close == 0:
            continue
        factor = 1 - (amount / pre_ex_close)
        adj.loc[mask] *= factor
    return adj


def load_historical_data_mongo(start_date, end_date, symbols):
    """Load historical data from MongoDB stock_data collection.

    Fields: symbol, o, h, l, c, v, t (ms timestamp).
    Adj Close is computed using dividends_cache.json (same as Polygon path).
    Falls back per-symbol to Polygon PriceDataStore if a symbol has no data
    or insufficient coverage for the requested date range.

    Raises RuntimeError if MongoDB is unreachable or the result is unusable.
    All per-symbol outcomes are logged to the run log file in logs/.
    """
    from db.connection import get_main_db

    start_dt = pd.Timestamp(start_date)
    end_dt   = pd.Timestamp(end_date)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp() * 1000)
    min_expected_rows = max(1, int((end_dt - start_dt).days * 0.5))  # ≥50% of calendar days

    log.info("=" * 60)
    log.info(f"[DataLoader/MRPT] source=mongo  symbols={len(symbols)}  "
             f"range={start_date} → {end_date}  min_rows≥{min_expected_rows}")
    log.info("=" * 60)

    # ── Connect ──────────────────────────────────────────────────────────────
    try:
        db  = get_main_db()
        col = db["stock_data"]
        log.info("[DataLoader/MRPT] MongoDB connection OK")
    except Exception as e:
        log.error(f"[DataLoader/MRPT] FAIL  MongoDB connection error: {e}")
        raise RuntimeError(f"MongoDB connection failed: {e}") from e

    all_data         = {}
    fallback_symbols = []   # per-symbol polygon fallback list

    # ── Per-symbol fetch ─────────────────────────────────────────────────────
    for symbol in symbols:
        docs      = None
        fetch_err = None

        for attempt in range(2):
            try:
                docs = list(
                    col.find(
                        {"symbol": symbol, "t": {"$gte": start_ms, "$lte": end_ms}},
                        {"o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "t": 1, "_id": 0}
                    ).sort("t", 1)
                )
                fetch_err = None
                break
            except Exception as e:
                fetch_err = e
                if attempt == 0:
                    log.warning(f"[DataLoader/MRPT] {symbol}  query error (attempt 1), retrying: {e}")
                else:
                    log.error(f"[DataLoader/MRPT] {symbol}  FAIL  query error after retry: {e}  "
                              f"→ per-symbol polygon fallback")
                    docs = []

        n_rows = len(docs) if docs else 0

        # ── Decide: sufficient data? ──────────────────────────────────────
        if fetch_err and not docs:
            reason = f"query exception after retry: {fetch_err}"
            log.warning(f"[DataLoader/MRPT] {symbol}  FALLBACK  reason=query_error  {reason}")
            fallback_symbols.append(symbol)
            continue

        if n_rows == 0:
            log.warning(f"[DataLoader/MRPT] {symbol}  FALLBACK  reason=zero_rows  "
                        f"MongoDB returned 0 rows for range {start_date}→{end_date}")
            fallback_symbols.append(symbol)
            continue

        if n_rows < min_expected_rows:
            # Check actual coverage to distinguish "partial" from "wrong range"
            actual_start = pd.Timestamp(docs[0]['t'],  unit='ms').date()
            actual_end   = pd.Timestamp(docs[-1]['t'], unit='ms').date()
            log.warning(f"[DataLoader/MRPT] {symbol}  FALLBACK  reason=insufficient_rows  "
                        f"got={n_rows}  expected≥{min_expected_rows}  "
                        f"actual_coverage={actual_start}→{actual_end}  "
                        f"requested={start_date}→{end_date}")
            fallback_symbols.append(symbol)
            continue

        # ── Build DataFrame ───────────────────────────────────────────────
        actual_start = pd.Timestamp(docs[0]['t'],  unit='ms').date()
        actual_end   = pd.Timestamp(docs[-1]['t'], unit='ms').date()

        # Check tail coverage: if MongoDB data doesn't reach end_date,
        # the last trading day(s) will be NaN — fall back to Polygon.
        requested_end = pd.Timestamp(end_date).date()
        if actual_end < requested_end - pd.Timedelta(days=3):
            log.warning(f"[DataLoader/MRPT] {symbol}  FALLBACK  reason=stale_tail  "
                        f"mongo_end={actual_end}  requested_end={requested_end}  "
                        f"gap={requested_end - actual_end}")
            fallback_symbols.append(symbol)
            continue

        df = pd.DataFrame(docs)
        df['date'] = pd.to_datetime(df['t'], unit='ms').dt.normalize()
        df = df.set_index('date')
        df = df[~df.index.duplicated(keep='last')]

        log.info(f"[DataLoader/MRPT] {symbol}  mongo OK  rows={n_rows}  "
                 f"coverage={actual_start}→{actual_end}")

        # ── Adj Close via dividends_cache.json ────────────────────────────
        try:
            store     = PriceDataStore(
                base_dir=os.path.dirname(os.path.abspath(__file__)),
                polygon_api_key=POLYGON_API_KEY,
            )
            dividends = store._fetch_dividends(symbol, start_date, end_date)
            adj_close = df['c'].copy()
            divs_applied = 0
            for div in reversed(dividends):
                ex_date = pd.Timestamp(div['ex_dividend_date'])
                amount  = div['cash_amount']
                mask    = adj_close.index < ex_date
                if not mask.any():
                    continue
                pre_ex_close = df.loc[adj_close.index[mask][-1], 'c']
                if pre_ex_close == 0:
                    continue
                factor = 1.0 - (amount / pre_ex_close)
                adj_close.loc[mask] *= factor
                divs_applied += 1
            log.info(f"[DataLoader/MRPT] {symbol}  adj_close OK  "
                     f"dividends_fetched={len(dividends)}  applied={divs_applied}")
        except Exception as e:
            log.warning(f"[DataLoader/MRPT] {symbol}  adj_close WARN  "
                        f"dividend fetch/apply failed ({e}), using raw Close as Adj Close")
            adj_close = df['c'].copy()

        all_data[symbol] = {
            'Open':      df['o'].astype(float),
            'High':      df['h'].astype(float),
            'Low':       df['l'].astype(float),
            'Close':     df['c'].astype(float),
            'Adj Close': adj_close.astype(float),
            'Volume':    df['v'].astype(float),
        }

    # ── Per-symbol polygon fallback ──────────────────────────────────────────
    if fallback_symbols:
        log.info(f"[DataLoader/MRPT] per-symbol polygon fallback  count={len(fallback_symbols)}  "
                 f"symbols={fallback_symbols}")
        try:
            store       = PriceDataStore(
                base_dir=os.path.dirname(os.path.abspath(__file__)),
                polygon_api_key=POLYGON_API_KEY,
            )
            fallback_df = store.load(fallback_symbols, start_date, end_date)
            for symbol in fallback_symbols:
                sym_data = {}
                for field in ('Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume'):
                    if (field, symbol) in fallback_df.columns:
                        sym_data[field] = fallback_df[(field, symbol)]
                if sym_data:
                    all_data[symbol] = sym_data
                    rows = len(next(iter(sym_data.values())))
                    log.info(f"[DataLoader/MRPT] {symbol}  polygon fallback OK  rows={rows}")
                else:
                    log.error(f"[DataLoader/MRPT] {symbol}  FAIL  polygon fallback returned no data")
                    raise RuntimeError(f"Polygon fallback also returned no data for {symbol}")
        except RuntimeError:
            raise
        except Exception as e:
            log.error(f"[DataLoader/MRPT] polygon fallback FAIL  error: {e}")
            raise RuntimeError(f"Polygon fallback failed: {e}") from e

    if not all_data:
        log.error("[DataLoader/MRPT] FAIL  no usable data from any source for any symbol")
        raise RuntimeError("MongoDB returned no usable data for any symbol")

    # ── Summary ──────────────────────────────────────────────────────────────
    mongo_ok   = [s for s in symbols if s in all_data and s not in fallback_symbols]
    poly_ok    = [s for s in fallback_symbols if s in all_data]
    failed     = [s for s in symbols if s not in all_data]
    log.info(f"[DataLoader/MRPT] SUMMARY  mongo_ok={len(mongo_ok)}  "
             f"polygon_fallback={len(poly_ok)}  failed={len(failed)}")
    if mongo_ok:
        log.info(f"[DataLoader/MRPT]   via mongo:   {mongo_ok}")
    if poly_ok:
        log.info(f"[DataLoader/MRPT]   via polygon: {poly_ok}")
    if failed:
        log.error(f"[DataLoader/MRPT]   FAILED:      {failed}")

    # Build MultiIndex DataFrame matching yfinance/Polygon format: (Price, Ticker)
    fields = ['Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume']
    tuples = []
    arrays = {}
    for field in fields:
        for symbol in symbols:
            if symbol not in all_data:
                continue
            col_key = (field, symbol)
            tuples.append(col_key)
            arrays[col_key] = all_data[symbol][field]

    multi_index = pd.MultiIndex.from_tuples(tuples, names=['Price', 'Ticker'])
    data = pd.DataFrame(arrays, columns=multi_index)
    data.index.name = 'Date'
    data = data.sort_index()

    log.info(f"MongoDB: built DataFrame with {len(data)} rows, {len(data.columns)} columns")
    return data


def load_historical_data_polygon(start_date, end_date, symbols):
    """Load historical data from Polygon.io API with dividend-adjusted close."""
    log.info(f"Downloading data from Polygon.io for {len(symbols)} symbols...")

    all_data = {}
    for i, symbol in enumerate(symbols):
        # Fetch OHLCV bars
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

            # Fetch dividends and compute dividend-adjusted close
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

        # Small delay between requests to be polite to the API
        if i < len(symbols) - 1:
            time_module.sleep(0.2)

    # Build MultiIndex DataFrame matching yfinance format: (Price, Ticker)
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
    """Load historical data from Yahoo Finance"""
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
    """Load historical data with waterfall fallback: mongo → polygon → yahoo.

    mongo:   Direct MongoDB stock_data query. No Parquet cache. Per-symbol
             polygon fallback if a symbol is missing or has insufficient rows.
             Adj Close computed from dividends_cache.json.
    polygon: PriceDataStore with weekly Parquet cache. Polygon API on miss.
    yahoo:   yf.download() fallback, no cache. Last resort.
    """
    if data_source is None:
        data_source = DATA_SOURCE

    log.info(f"Attempting to load historical data for {len(symbols)} symbols "
             f"from {start_date} to {end_date} [source={data_source}]")

    # ── Stage 1: MongoDB ────────────────────────────────────────────────────
    if data_source == 'mongo':
        try:
            data = load_historical_data_mongo(start_date, end_date, symbols)
            data = check_data_structure(data)
            return data
        except Exception as e:
            log.warning(f"MongoDB load failed ({e}). Falling back to polygon.")
            data_source = 'polygon'  # cascade

    # ── Stage 2: Polygon (PriceDataStore + Parquet cache) ──────────────────
    if data_source == 'polygon':
        try:
            store = PriceDataStore(
                base_dir=os.path.dirname(os.path.abspath(__file__)),
                polygon_api_key=POLYGON_API_KEY,
            )
            data = store.load(symbols, start_date, end_date)
            data = check_data_structure(data)
            return data
        except Exception as e:
            log.warning(f"Polygon load failed ({e}). Falling back to yahoo.")

    # ── Stage 3: Yahoo Finance (last resort) ────────────────────────────────
    try:
        data = load_historical_data_yahoo(start_date, end_date, symbols)
        data = check_data_structure(data)
        return data
    except Exception as e:
        log.error(f"All data sources failed. Last error: {e}")
        log.error("Cannot proceed without real market data. Exiting.")
        sys.exit(1)


class CustomData(Data):
    """Extension of the Data class to handle different column structures"""
    
    def history(self, assets, fields, bar_count, frequency):
        """Overridden history method to handle different column names"""
        if frequency != '1d':
            raise ValueError("Only daily frequency is supported")

        end_date = self.historical_data.index[-1]
        start_date = end_date - pd.Timedelta(days=bar_count - 1)

        if isinstance(fields, str):
            fields = [fields]

        field_mapping = {
            'price': ['Adj Close', 'Close'],  # Try Adj Close first, then Close
            'Adj Close': ['Adj Close', 'Close'],
            'Close': ['Close', 'Adj Close']
        }

        # Get all available fields in the data
        available_fields = set([col[0] for col in self.historical_data.columns])
        
        result = pd.DataFrame(index=self.historical_data.loc[start_date:end_date].index)
        
        for asset in assets:
            for field in fields:
                # Handle price field specially
                if field in field_mapping:
                    # Try each alternative field
                    for alt_field in field_mapping[field]:
                        if alt_field in available_fields and (alt_field, asset) in self.historical_data.columns:
                            result[asset] = self.historical_data.loc[start_date:end_date, (alt_field, asset)]
                            break
                    else:
                        # If no alternative worked, raise error
                        raise ValueError(f"Could not find suitable alternative for field '{field}' for asset '{asset}'")
                else:
                    # Regular field
                    if (field, asset) in self.historical_data.columns:
                        result[asset] = self.historical_data.loc[start_date:end_date, (field, asset)]
                    else:
                        raise ValueError(f"Field '{field}' not available for asset '{asset}'")

        if len(fields) == 1:
            return result

        return result


def main(config=None):
    """Run a single backtest.

    Args:
        config: Optional dict with keys:
            - pairs: list of [stock1, stock2] pairs
            - params: dict of execution parameter overrides
            - run_label: string label for this run (used in filenames)
            - output_dir: base output directory (default: project dir)
            - historical_data: pre-loaded DataFrame to skip data download
            - start_date: backtest start date string
            - end_date: backtest end date string

    Returns:
        dict with run results (equity, pnl, drawdown, sharpe, etc.)
    """
    config = config or {}
    pairs = config.get('pairs')
    params = config.get('params', {})
    pair_params = config.get('pair_params', {})  # per-pair param overrides keyed by "S1/S2"
    run_label = config.get('run_label', '')
    base_dir = config.get('output_dir', os.path.dirname(os.path.abspath(__file__)))
    preloaded_data = config.get('historical_data')
    cfg_start = config.get('start_date', '2024-12-01')
    from datetime import timedelta
    one_month_ago = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    cfg_end = config.get('end_date', one_month_ago)
    # trade_start_date: if set, strategy only opens/closes positions on or after this date.
    # Data before this date is still loaded for warmup (z-score history, hedge ratio, etc.)
    cfg_trade_start = config.get('trade_start_date')

    # ---- Set up output directories ----
    charts_dir = os.path.join(base_dir, 'charts')
    logs_dir = os.path.join(base_dir, 'logs')
    runs_dir = os.path.join(base_dir, 'historical_runs')
    os.makedirs(charts_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)
    os.makedirs(runs_dir, exist_ok=True)

    run_timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    run_name = f"{run_label}_{run_timestamp}" if run_label else run_timestamp

    # ---- Set up file logging ----
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
        # Remove file handler so subsequent runs don't stack handlers
        logging.getLogger().removeHandler(file_handler)
        file_handler.close()


def _run_backtest(config, pairs, params, pair_params, run_name, base_dir,
                  charts_dir, runs_dir, preloaded_data, start_date, end_date,
                  trade_start_date=None):
    """Internal backtest execution."""
    context = Context()
    initialize(context, pairs=pairs, params=params, pair_params=pair_params)
    # trade_start_date: before this date the strategy loads data for warmup only,
    # no orders are placed. None means trade from the very first day.
    context.trade_start_date = pd.Timestamp(trade_start_date) if trade_start_date else None
    if context.trade_start_date:
        log.info(f"Warmup-only until {trade_start_date}; trading starts from {trade_start_date}")

    # Chart sub-directory for this run
    run_charts_dir = os.path.join(charts_dir, run_name)
    os.makedirs(run_charts_dir, exist_ok=True)

    context.output_filename = os.path.join(runs_dir, f'portfolio_history_{run_name}.xlsx')

    log.info(f"Using full data period from {start_date} to {end_date}")

    # Collect unique symbols from pairs
    symbols = list(dict.fromkeys(
        sym for pair in context.strategy_pairs for sym in [pair[0], pair[1]]
    ))

    # Load or reuse historical data
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

    log.info(f"Starting backtest with {len(full_data)} days of data from {start_date} to {end_date}")
    log.info(f"Params: {params}")

    portfolio_analysis = PortfolioAnalysis(context.portfolio)

    # Process the data day by day
    for date in full_data.index:
        try:
            context.portfolio.current_date = date
            context.portfolio.processed_dates.append(date)
            current_historical_data = historical_data.loc[:date]
            data = CustomData(current_historical_data)
            # Set warmup_mode: run full my_handle_data (builds spread/z-score state)
            # but order logic is skipped inside _process_pair_body when warmup_mode=True.
            # During warmup: suppress interest accrual and skip PnL/accounting histories —
            # equity/PnL tracking only starts from trade_start_date.
            context.warmup_mode = bool(context.trade_start_date and date < context.trade_start_date)
            if context.warmup_mode:
                # Temporarily zero out the interest rate so update_values_and_histories
                # charges nothing and records no equity/interest history.
                _saved_rate = context.portfolio.interest_rate
                context.portfolio.interest_rate = 0.0
            my_handle_data(context, data)
            if context.warmup_mode:
                # Restore interest rate and undo the equity/cash/history side-effects
                # of update_values_and_histories by popping the entries it just appended.
                context.portfolio.interest_rate = _saved_rate
                # Pop the one entry appended to each accounting history this warmup day
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
                # Also undo the cash deduction (interest_rate=0 means daily_interest=0,
                # so nothing was actually deducted — no cash correction needed)
            else:
                daily_pnl = context.portfolio.update_pnl_history(portfolio_analysis, data, symbols)
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

    # ---- Final analysis ----
    final_equity = context.portfolio.equity_history[-1][1]
    final_asset = context.portfolio.asset_history[-1][1]
    final_liability = context.portfolio.liability_history[-1][1]
    final_cash = context.portfolio.asset_cash_history[-1][1]

    log.info("Final Portfolio State:")
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
        log.info(f"Accumulated Total P&L Including Interest Expenses: {acc_pnl}")

    max_dd_dollar = 0
    max_dd_pct = 0
    if context.portfolio.max_drawdown_history:
        max_drawdown = context.portfolio.max_drawdown_history[-1]
        max_dd_dollar = max_drawdown[1]
        max_dd_pct = max_drawdown[2]
        log.info(f"Final Max Drawdown: ${max_dd_dollar:.2f} ({max_dd_pct:.2%})")

    trading_days_percentage = portfolio_analysis.calculate_trading_days_percentage(context.portfolio)
    log.info(f"Percentage of days with open positions: {trading_days_percentage:.2%}")

    # Trade summary
    trade_summary = {}
    if context.portfolio.pair_trade_history and context.portfolio.acc_pair_trade_pnl_history:
        trade_summary = summarize_pair_trade_history(context.portfolio.pair_trade_history,
                                                     context.portfolio.acc_pair_trade_pnl_history)
        log.info("Pair Trade History Summary:")
        for pair, stats in trade_summary.items():
            log.info(f"{pair}:")
            log.info(f"  Total trades: {stats['total_trades']} (Long: {stats['long_trades']}, Short: {stats['short_trades']})")
            log.info(f"  Total volume: {stats['total_volume']:.2f}")
            log.info(f"  Accumulated P&L: ${stats['net_pnl']:.2f}")
            log.info("--------------------")

    # ---- Write Excel once — all data is now fully accumulated in memory ----
    try:
        exporter = ExportExcel(context.output_filename)
        exporter.export_portfolio_data(context.portfolio, context)
        log.info(f"Excel saved to {context.output_filename}")
    except Exception as e:
        log.error(f"Error writing Excel: {str(e)}")
        import traceback
        log.error(traceback.format_exc())

    # Create visualizations — save to charts dir
    try:
        visualizer = PortfolioVisualizer(context.portfolio, context, context.output_filename,
                                         chart_dir=run_charts_dir)
        visualizer.plot_all_histories()
        log.info(f"Charts saved to {run_charts_dir}")
    except Exception as e:
        log.error(f"Error creating visualizations: {str(e)}")

    # ---- Compute Sharpe Ratio ----
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