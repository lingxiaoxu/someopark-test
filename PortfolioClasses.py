import numpy as np
import statsmodels.api as sm
import statsmodels.tsa.stattools as ts
import pandas as pd
import datetime
from datetime import datetime, time
import yfinance as yf

import logging

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter

import openpyxl
from openpyxl.utils import get_column_letter
import os

from pykalman import KalmanFilter
import pandas_market_calendars as mcal
from collections import defaultdict

import quantstats as qs
import math

# Set up logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

class CurrentData:
    def __init__(self, data_slice):
        self.data_slice = data_slice

    def history(self, symbols, field, bar_count, frequency):
        if field != "price":
            raise ValueError("Only 'price' field is supported in this implementation")
        if frequency != "1d":
            raise ValueError("Only '1d' frequency is supported in this implementation")
        return self.data_slice.loc[:, symbols].tail(bar_count)


class date_rules:
    @staticmethod
    def every_day():
        return lambda date: True


class time_rules:
    @staticmethod
    def market_close(minutes):
        return lambda date: True  # For simplicity, always return True


class Trade:
    def __init__(self, date, symbol, amount, price, order_type):
        self.date = date
        self.symbol = symbol
        self.amount = amount
        self.price = price
        self.order_type = order_type  # 'open' or 'close'
        self.direction = 'long' if amount > 0 else 'short'


class Portfolio:
    def __init__(self, initial_cash, initial_loan, interest_rate, strategy_pairs):

        self.initial_cash = initial_cash
        self.initial_loan = initial_loan

        self.processed_dates = []

        self.strategy_pairs = strategy_pairs
        self.positions = {}
        self.trades = []
        self.volatilities_in_window = {}
        self.normalized_volatility = None
        self.current_date = None
        self.signal_history = {}
        self.interest_rate = interest_rate

        # Balance sheet categories
        self.asset_cash = self.initial_cash + self.initial_loan
        self.asset_securities = {}
        self.liability_securities = {}
        self.liability_loan = self.initial_loan

        # New history lists
        self.asset_cash_history = []
        self.asset_securities_history = {}
        self.liability_securities_history = {}
        self.liability_loan_history = []

        # History
        self.statistical_test_history = {}
        
        # Track warnings that have been shown
        self.warnings_shown = set()
        self.hedge_history = {}
        self.asset_history = []
        self.liability_history = []
        self.equity_history = []
        self.value_history = []
        self.price_history = {}
        self.share_history = {}
        self.percentage_history = {}
        self.pair_trade_history = {f"{pair[0]}/{pair[1]}": [] for pair in strategy_pairs}

        self.finished_trades_pnl = {}
        self.cost_basis_history = {}
        self.total_cost_history = {}
        self.interest_expense_history = []
        self.daily_pnl_history = []

        self.acc_security_pnl_history = {}
        self.acc_pair_trade_pnl_history = {}
        self.dod_security_pnl_history = {}
        self.dod_pair_trade_pnl_history = {}

        self.acc_interest_history = []
        self.acc_daily_pnl_history = []

        self.max_drawdown_history = []
        self.peak_value = initial_cash  # Initial equity (total asset - total loan)
        self.leverage = initial_loan / initial_cash  # Initial leverage

    def update_price_history(self, date, prices):
        self.current_date = date
        for symbol, price in prices.items():
            if symbol not in self.price_history:
                self.price_history[symbol] = []
            self.price_history[symbol].append((date, price))

    def update_values(self, prices):
        self.update_values_and_histories(prices)

    def update_values_and_histories(self, prices):
        # Calculate daily interest expense
        daily_interest = self.liability_loan * (self.interest_rate / 365)

        # Deduct interest expense from asset_cash
        self.asset_cash -= daily_interest

        # current_date = self.current_date
        # if current_date >= np.datetime64('2023-07-03T00:00:00.000000000'):
        #     print("it is time")
        # else:
        #     print("not yet")

        # Calculate total asset and liability values
        total_asset = self.asset_cash
        total_liability = self.liability_loan

        for symbol, current_shares in self.positions.items():
            if symbol in prices:
                current_price = prices[symbol]

                # Update price history if needed
                if (symbol not in self.price_history or
                        not self.price_history[symbol] or
                        self.price_history[symbol][-1][0] != self.current_date):
                    self.update_price_history(self.current_date, {symbol: current_price})

                # Update share history
                if symbol not in self.share_history:
                    self.share_history[symbol] = []
                self.share_history[symbol].append((self.current_date, current_shares))

                # Add to total asset or liability based on existing position
                if current_shares > 0:  # Long position
                    total_asset += current_shares * current_price
                    self.asset_securities[symbol] = current_shares * current_price
                    if symbol not in self.asset_securities_history:
                        self.asset_securities_history[symbol] = []
                    self.asset_securities_history[symbol].append((self.current_date, current_shares * current_price))
                elif current_shares < 0:  # Short position
                    total_liability += abs(current_shares) * current_price
                    self.liability_securities[symbol] = abs(current_shares) * current_price
                    if symbol not in self.liability_securities_history:
                        self.liability_securities_history[symbol] = []
                    self.liability_securities_history[symbol].append((self.current_date, abs(current_shares) * current_price))
            else:
                log.warning(f"No price data available for {symbol}")

        # Calculate net equity
        net_equity = total_asset - total_liability

        # Calculate and update daily P&L
        if len(self.equity_history) > 1:
            prev_equity = self.equity_history[-1][1]
            daily_pnl = net_equity - prev_equity
        else:
            daily_pnl = -daily_interest
        self.daily_pnl_history.append((self.current_date, daily_pnl))

        # Update histories
        self.asset_cash_history.append((self.current_date, self.asset_cash))
        self.liability_loan_history.append((self.current_date, self.liability_loan))
        self.asset_history.append((self.current_date, total_asset))
        self.liability_history.append((self.current_date, total_liability))
        self.equity_history.append((self.current_date, net_equity))
        self.value_history.append((self.current_date, net_equity))

        # Update interest expense history
        self.interest_expense_history.append((self.current_date, daily_interest))
        if not self.acc_interest_history:
            self.acc_interest_history.append((self.current_date, daily_interest))
        else:
            prev_acc_interest = self.acc_interest_history[-1][1]
            self.acc_interest_history.append((self.current_date, prev_acc_interest + daily_interest))

        # Update accumulated daily P&L
        if not self.acc_daily_pnl_history:
            self.acc_daily_pnl_history.append((self.current_date, daily_pnl))
        else:
            prev_acc_pnl = self.acc_daily_pnl_history[-1][1]
            self.acc_daily_pnl_history.append((self.current_date, prev_acc_pnl + daily_pnl))

        # Update percentage history
        for symbol in self.positions:
            if symbol not in self.percentage_history:
                self.percentage_history[symbol] = []
            if net_equity > 0:
                percentage = (self.positions[symbol] * prices.get(symbol, 0)) / net_equity
            else:
                percentage = 0
            self.percentage_history[symbol].append((self.current_date, percentage))

    def update_pnl_history(self, portfolio_analysis, data, symbols):
        current_prices = data.history(symbols, "Adj Close", 1, "1d").iloc[-1]

        # Calculate accumulated security PnL
        acc_security_pnl = portfolio_analysis.calculate_acc_security_pnl(current_prices)
        self.acc_security_pnl_history[self.current_date] = acc_security_pnl

        # Calculate accumulated pair trade PnL
        acc_pair_trade_pnl = portfolio_analysis.calculate_acc_pair_trade_pnl(current_prices)
        self.acc_pair_trade_pnl_history[self.current_date] = acc_pair_trade_pnl

        total_security_pnl, total_pair_pnl = portfolio_analysis.reconcile_pnls(acc_security_pnl, acc_pair_trade_pnl)

        current_date = self.current_date
        #if current_date >= np.datetime64('2023-07-05T00:00:00.000000000'):
        #    print("it is time")
        #else:
        #    print("not yet")

        # Calculate DoD PnL
        self.dod_security_pnl_history = portfolio_analysis.calculate_dod_security_pnl()
        self.dod_pair_trade_pnl_history = portfolio_analysis.calculate_dod_pair_trade_pnl()

        return self.daily_pnl_history[-1][1]  # Return the latest daily P&L

    def update_max_drawdown(self):
        # Calculate the current portfolio value (equity)
        total_asset = self.asset_cash + sum(self.asset_securities.values())
        total_liability = self.liability_loan + sum(self.liability_securities.values())
        current_value = total_asset - total_liability

        # Calculate leverage, handling cases where current_value is zero or negative
        if current_value != 0:
            self.leverage = abs(self.liability_loan / current_value)
        else:
            self.leverage = float('inf')  # Handle undefined leverage case

        # Find the peak and trough in equity history
        peak_value = 0
        max_drawdown_percent = 0

        for date, equity in self.equity_history:
            if equity > peak_value:
                peak_value = equity
            drawdown_dollar = peak_value - equity
            drawdown_percent = drawdown_dollar / peak_value if peak_value != 0 else 0

            # Adjust drawdown for leverage
            leveraged_drawdown_percent = self.leverage * drawdown_percent

            if leveraged_drawdown_percent > max_drawdown_percent:
                max_drawdown_percent = leveraged_drawdown_percent

        # Update max drawdown history
        if not self.max_drawdown_history or max_drawdown_percent > self.max_drawdown_history[-1][2]:
            self.max_drawdown_history.append((self.current_date, drawdown_dollar, max_drawdown_percent))
        else:
            self.max_drawdown_history.append(
                (self.current_date, self.max_drawdown_history[-1][1], self.max_drawdown_history[-1][2])
            )

    def record_trade(self, trade):
        self.trades.append(trade)
        if trade.symbol not in self.signal_history:
            self.signal_history[trade.symbol] = []
        self.signal_history[trade.symbol].append((self.current_date, trade.direction))

        # Update pair trade history
        pair = self.get_pair_for_symbol(trade.symbol)
        if pair:
            self.pair_trade_history[pair].append(trade)

    def get_pair_for_symbol(self, symbol):
        for pair in self.strategy_pairs:
            if symbol in pair[:2]:  # Check only the first two elements (stock symbols)
                return f"{pair[0]}/{pair[1]}"
        return None


# ~~~~~~~~~~~~~~~~~~~~~~ TESTS FOR FINDING PAIR TO TRADE ON ~~~~~~~~~~~~~~~~~~~~~~
class ADF(object):
    """
    Augmented Dickey–Fuller (ADF) unit root test
    Source: http://www.pythonforfinance.net/2016/05/09/python-backtesting-mean-reversion-part-2/
    """

    def __init__(self):
        self.p_value = None
        self.five_perc_stat = None
        self.perc_stat = None
        self.p_min = .0
        self.p_max = .05
        self.look_back = 63

    def apply_adf(self, time_series):
        model = ts.adfuller(time_series, 1)
        self.p_value = model[1]
        self.five_perc_stat = model[4]['5%']
        self.perc_stat = model[0]

    def use_P(self):
        return (self.p_value > self.p_min) and (self.p_value < self.p_max)

    def use_critical(self):
        return abs(self.perc_stat) > abs(self.five_perc_stat)


class KPSS(object):
    #Kwiatkowski-Phillips-Schmidt-Shin (KPSS) stationarity tests
    def __init__(self):
        Exception("Not implemented yet")
        self.p_value = None
        self.ten_perc_stat = None
        self.perc_stat = None
        self.p_min = 0.0
        self.p_max = 0.2
        self.look_back = 50

    def apply_kpss(self, time_series):
        self.p_value = ts.adfuller(time_series, 1)[1]
        self.five_perc_stat = ts.adfuller(time_series, 1)[4]['5%'] # possibly make this 10%
        self.ten_perc_stat = ts.adfuller(time_series, 1)[4]['10%']
        self.perc_stat = ts.adfuller(time_series, 1)[0]

    def use(self):
        return (self.p_value > self.p_min) and (self.p_value < self.p_max) and (self.perc_stat > self.five_perc_stat)


class Data:
    def __init__(self, historical_data):
        self.historical_data = historical_data

    def history(self, assets, fields, bar_count, frequency):
        if frequency != '1d':
            raise ValueError("Only daily frequency is supported")

        end_date = self.historical_data.index[-1]
        start_date = end_date - pd.Timedelta(days=bar_count - 1)

        if isinstance(fields, str):
            fields = [fields]

        if 'price' in fields:
            # If 'price' is requested, use 'Close' price
            fields = [f if f != 'price' else 'Adj Close' for f in fields]

        result = self.historical_data.loc[start_date:end_date, (fields, assets)]

        if len(fields) == 1:
            result = result[fields[0]]

        return result


    def load_historical_data(start_date, end_date, symbols):
        data = yf.download(symbols, start=start_date, end=end_date)
        return data


class Half_Life(object):
    """
    Half Life test from the Ornstein-Uhlenbeck process
    Source: http://www.pythonforfinance.net/2016/05/09/python-backtesting-mean-reversion-part-2/
    """

    def __init__(self):
        self.hl_min = 1.0
        self.hl_max = 42.0
        self.look_back = 43
        self.half_life = None

    def apply_half_life(self, time_series):
        lag = np.roll(time_series, 1)
        lag[0] = 0
        ret = time_series - lag
        ret[0] = 0

        # adds intercept terms to X variable for regression
        lag2 = sm.add_constant(lag)

        model = sm.OLS(ret, lag2)
        res = model.fit()

        self.half_life = -np.log(2) / res.params[1]

    def use(self):
        return (self.half_life < self.hl_max) and (self.half_life > self.hl_min)


class Hurst():
    """
    If Hurst Exponent is under the 0.5 value of a random walk, then the series is mean reverting
    Source: https://www.quantstart.com/articles/Basics-of-Statistical-Mean-Reversion-Testing
    """

    def __init__(self):
        self.h_min = 0.0
        self.h_max = 0.4
        self.look_back = 126 #126
        self.lag_max = 100
        self.h_value = None

    def apply_hurst(self, time_series):
        """Returns the Hurst Exponent of the time series vector ts"""
        # Create the range of lag values
        lags = range(2, self.lag_max)

        # Calculate the array of the variances of the lagged differences
        tau = [np.sqrt(np.std(np.subtract(time_series[lag:], time_series[:-lag]))) for lag in lags]

        # Use a linear fit to estimate the Hurst Exponent
        poly = np.polyfit(np.log10(lags), np.log10(tau), 1)

        # Return the Hurst exponent from the polyfit output
        self.h_value = poly[0] * 2.0

    def use(self):
        return (self.h_value < self.h_max) and (self.h_value > self.h_min)


class PortfolioAnalysis:
    def __init__(self, portfolio):
        self.portfolio = portfolio

    def calculate_daily_pnl(self):
        daily_pnl = {}
        for i in range(1, len(self.portfolio.equity_history)):
            prev_date, prev_equity = self.portfolio.equity_history[i - 1]
            curr_date, curr_equity = self.portfolio.equity_history[i]
            daily_pnl[curr_date] = curr_equity - prev_equity
        return daily_pnl

    def calculate_cost_basis(self, symbol, date):
        if date is None:
            return 0

        if symbol not in self.portfolio.cost_basis_history:
            return 0

        symbol_history = self.portfolio.cost_basis_history[symbol]

        # Get the latest cost basis up to the given date
        for entry in reversed(symbol_history):
            if entry[0] <= date:  # Compare the date (first element of each entry)
                return entry[1]  # Return the cost basis (second element of each entry)

        return 0  # Return 0 if no valid entry is found

    def find_remaining_position_date(self, symbol, prev_vs_current):
        current_position = prev_vs_current['current']['current_position']
        current_date = prev_vs_current['current']['current_date']
        prev_position = prev_vs_current['previous']['prev_position']

        # Case 1: Current position is 0
        if current_position == 0:
            return None  # This will lead to new_cost_basis = 0 in the calling function

        # Case 2: Current position and previous position have different signs
        if (current_position > 0 and prev_position < 0) or (current_position < 0 and prev_position > 0):
            return current_date

        # Case 3: Current position magnitude is greater than previous position magnitude
        if abs(current_position) > abs(prev_position):
            return current_date

        # Case 4: Current position magnitude is less than or equal to previous position magnitude
        # We need to find the date when the position was equal to the current position
        share_history = self.portfolio.share_history.get(symbol, [])
        for date, shares in sorted(share_history, reverse=True):
            if shares == current_position:
                return date
            elif (current_position > 0 and shares > current_position) or \
                    (current_position < 0 and shares < current_position):
                # We've found the first date where the position was greater than or equal to the current position
                return date

        # If we couldn't find a matching date, return the earliest date in our history
        return share_history[-1][0] if share_history else current_date

    def get_previous_vs_current_security(self, symbol, current_price):
        current_date = self.portfolio.current_date

        # Find the previous processed date
        prev_date_index = self.portfolio.processed_dates.index(current_date) - 1
        prev_date = self.portfolio.processed_dates[prev_date_index] if prev_date_index >= 0 else None

        current_position = next(
            (shares for date, shares in self.portfolio.share_history.get(symbol, []) if date == current_date), 0)
        prev_position = next(
            (shares for date, shares in reversed(self.portfolio.share_history.get(symbol, [])) if date <= prev_date),
            0) if prev_date else 0

        prev_acc_pnl = self.portfolio.acc_security_pnl_history.get(prev_date, {}).get(symbol, {'pnl_dollar': 0,
                                                                                               'pnl_percent': 0}) if prev_date else {
            'pnl_dollar': 0, 'pnl_percent': 0}

        current_cost_basis = self.calculate_cost_basis(symbol, current_date)
        prev_cost_basis = self.calculate_cost_basis(symbol, prev_date) if prev_date else 0

        trades = [trade for trade in reversed(self.portfolio.trades) if trade.symbol == symbol]
        last_trade = trades[0] if trades else None
        previous_trade = trades[1] if len(trades) > 1 else None

        # Get the previous date price
        previous_price = next(
            (price for date, price in reversed(self.portfolio.price_history.get(symbol, [])) if date <= prev_date),
            None) if prev_date else None

        return {
            'previous': {
                'prev_date': prev_date,
                'prev_position': prev_position,
                'prev_cost_basis': prev_cost_basis,
                'prev_acc_pnl': prev_acc_pnl,
                'previous_trade': previous_trade,
                'previous_price': previous_price
            },
            'current': {
                'current_date': current_date,
                'current_position': current_position,
                'new_cost_basis': current_cost_basis,
                'current_acc_pnl': {'pnl_dollar': None, 'pnl_percent': None},
                'last_trade': last_trade,
                'current_price': current_price
            }
        }

    def get_previous_vs_current_pair(self, pair, current_prices):
        stock1, stock2 = pair.split('/')
        current_date = self.portfolio.current_date

        # Find the previous processed date
        prev_date_index = self.portfolio.processed_dates.index(current_date) - 1
        prev_date = self.portfolio.processed_dates[prev_date_index] if prev_date_index >= 0 else None

        # Get current positions
        current_position_1 = next(
            (shares for date, shares in self.portfolio.share_history.get(stock1, []) if date == current_date), 0)
        current_position_2 = next(
            (shares for date, shares in self.portfolio.share_history.get(stock2, []) if date == current_date), 0)

        # Get previous positions
        prev_position_1 = next(
            (shares for date, shares in reversed(self.portfolio.share_history.get(stock1, [])) if date <= prev_date),
            0) if prev_date else 0
        prev_position_2 = next(
            (shares for date, shares in reversed(self.portfolio.share_history.get(stock2, [])) if date <= prev_date),
            0) if prev_date else 0

        # Get previous accumulated pair PnL
        prev_acc_pair_pnl = self.portfolio.acc_pair_trade_pnl_history.get(prev_date, {}).get(pair, {'pnl_dollar': 0,
                                                                                                    'pnl_percent': 0}) if prev_date else {
            'pnl_dollar': 0, 'pnl_percent': 0}

        # Get cost bases
        current_cost_basis_1 = self.calculate_cost_basis(stock1, current_date)
        current_cost_basis_2 = self.calculate_cost_basis(stock2, current_date)
        prev_cost_basis_1 = self.calculate_cost_basis(stock1, prev_date) if prev_date else 0
        prev_cost_basis_2 = self.calculate_cost_basis(stock2, prev_date) if prev_date else 0

        # Get trades
        trades_1 = [trade for trade in reversed(self.portfolio.trades) if trade.symbol == stock1]
        trades_2 = [trade for trade in reversed(self.portfolio.trades) if trade.symbol == stock2]
        last_trade_1 = trades_1[0] if trades_1 else None
        last_trade_2 = trades_2[0] if trades_2 else None
        previous_trade_1 = trades_1[1] if len(trades_1) > 1 else None
        previous_trade_2 = trades_2[1] if len(trades_2) > 1 else None

        # Get previous prices
        previous_price_1 = next(
            (price for date, price in reversed(self.portfolio.price_history.get(stock1, [])) if date <= prev_date),
            None) if prev_date else None
        previous_price_2 = next(
            (price for date, price in reversed(self.portfolio.price_history.get(stock2, [])) if date <= prev_date),
            None) if prev_date else None

        return {
            'previous': {
                'prev_date': prev_date,
                'prev_position': [prev_position_1, prev_position_2],
                'prev_cost_basis': [prev_cost_basis_1, prev_cost_basis_2],
                'prev_acc_pair_pnl': prev_acc_pair_pnl,
                'previous_trade': [previous_trade_1, previous_trade_2],
                'previous_price': [previous_price_1, previous_price_2]
            },
            'current': {
                'current_date': current_date,
                'current_position': [current_position_1, current_position_2],
                'new_cost_basis': [current_cost_basis_1, current_cost_basis_2],
                'current_acc_pair_pnl': {'pnl_dollar': None, 'pnl_percent': None},
                'last_trade': [last_trade_1, last_trade_2],
                'current_price': [current_prices[stock1], current_prices[stock2]]
            }
        }

    def calculate_weighted_pnl_percentage(self, stock1, stock2, pnl_percent1, pnl_percent2, use_current=True):
        index = -1 if use_current else -2

        stock1_percent = self.portfolio.percentage_history[stock1][index][1] if len(
            self.portfolio.percentage_history[stock1]) >= abs(index) else 0
        stock2_percent = self.portfolio.percentage_history[stock2][index][1] if len(
            self.portfolio.percentage_history[stock2]) >= abs(index) else 0

        total_abs_percent = abs(stock1_percent) + abs(stock2_percent)
        if total_abs_percent != 0:
            stock1_weight = abs(stock1_percent) / total_abs_percent
            stock2_weight = abs(stock2_percent) / total_abs_percent
            weighted_pnl_percent = (
                    pnl_percent1 * stock1_weight +
                    pnl_percent2 * stock2_weight
            )
        else:
            weighted_pnl_percent = 0

        return weighted_pnl_percent

    def update_cost_basis_history(self, symbol, date, new_cost_basis):
        if symbol not in self.portfolio.cost_basis_history:
            self.portfolio.cost_basis_history[symbol] = [(date, new_cost_basis)]
        else:
            existing_entry = next((entry for entry in self.portfolio.cost_basis_history[symbol] if entry[0] == date),
                                  None)
            if existing_entry:
                if existing_entry[1] != new_cost_basis:
                    index = self.portfolio.cost_basis_history[symbol].index(existing_entry)
                    self.portfolio.cost_basis_history[symbol][index] = (date, new_cost_basis)
            else:
                self.portfolio.cost_basis_history[symbol].append((date, new_cost_basis))

    def calculate_dod_security_pnl(self):
        acc_history = self.portfolio.acc_security_pnl_history
        dates = sorted(acc_history.keys())

        # If dod_security_pnl doesn't exist, create it
        if not hasattr(self.portfolio, 'dod_security_pnl'):
            self.portfolio.dod_security_pnl = {}

        # Find the last date that was calculated
        last_calculated_date = max(self.portfolio.dod_security_pnl.keys()) if self.portfolio.dod_security_pnl else None

        # Find the index of the last calculated date, or start from the beginning if it doesn't exist
        start_index = dates.index(last_calculated_date) + 1 if last_calculated_date in dates else 0

        # Calculate DoD PnL only for new dates
        for i in range(start_index, len(dates)):
            date = dates[i]
            self.portfolio.dod_security_pnl[date] = {}

            if i == 0:
                # For the first date, DoD PnL is the same as accumulated PnL
                self.portfolio.dod_security_pnl[date] = acc_history[date]
            else:
                prev_date = dates[i - 1]
                for symbol in acc_history[date]:
                    if symbol in acc_history[prev_date]:
                        current_price = self.portfolio.price_history[symbol][i][1]
                        prev_vs_current = self.get_previous_vs_current_security(symbol, current_price)

                        # Force modify current_acc_pnl with values from acc_history
                        prev_vs_current['current']['current_acc_pnl']['pnl_dollar'] = acc_history[date][symbol][
                            'pnl_dollar']
                        prev_vs_current['current']['current_acc_pnl']['pnl_percent'] = acc_history[date][symbol][
                            'pnl_percent']

                        prev = prev_vs_current['previous']
                        current = prev_vs_current['current']

                        curr_pnl = current['current_acc_pnl']['pnl_dollar']
                        prev_pnl = prev['prev_acc_pnl']['pnl_dollar']
                        dod_pnl_dollar = curr_pnl - prev_pnl

                        # Calculate notional previous day
                        total_cost_prev = abs(prev['prev_position']) * prev['prev_cost_basis']

                        dod_pnl_percent = dod_pnl_dollar / total_cost_prev if total_cost_prev != 0 else 0

                        self.portfolio.dod_security_pnl[date][symbol] = {
                            'pnl_dollar': dod_pnl_dollar,
                            'pnl_percent': dod_pnl_percent
                        }
                    else:
                        self.portfolio.dod_security_pnl[date][symbol] = acc_history[date][symbol]

        return self.portfolio.dod_security_pnl

    def calculate_dod_pair_trade_pnl(self):
        dod_security_pnl = self.calculate_dod_security_pnl()
        dates = sorted(dod_security_pnl.keys())

        if not hasattr(self.portfolio, 'dod_pair_trade_pnl_history'):
            self.portfolio.dod_pair_trade_pnl_history = {}

        last_calculated_date = max(
            self.portfolio.dod_pair_trade_pnl_history.keys()) if self.portfolio.dod_pair_trade_pnl_history else None
        start_index = dates.index(last_calculated_date) + 1 if last_calculated_date in dates else 0

        for i in range(start_index, len(dates)):
            date = dates[i]
            self.portfolio.dod_pair_trade_pnl_history[date] = {}

            # Find the previous processed date
            prev_date_index = self.portfolio.processed_dates.index(date) - 1
            prev_date = self.portfolio.processed_dates[prev_date_index] if prev_date_index >= 0 else None

            for pair in self.portfolio.pair_trade_history.keys():
                stock1, stock2 = pair.split('/')
                if stock1 in dod_security_pnl[date] and stock2 in dod_security_pnl[date]:
                    # Check if positions were closed on this date
                    positions_closed = all(
                        trade.date == date and trade.order_type == 'close'
                        for trade in self.portfolio.pair_trade_history[pair]
                        if trade.date == date
                    )

                    # Check if the trade was opened on the previous day
                    opened_prev_day = any(
                        trade.date == prev_date and trade.order_type == 'open'
                        for trade in self.portfolio.pair_trade_history[pair]
                        if prev_date and trade.date == prev_date
                    )

                    if positions_closed:
                        if opened_prev_day:
                            # Trade lasted one day, use current date's PnL percentages
                            stock1_pnl_percent = dod_security_pnl[date][stock1]['pnl_percent']
                            stock2_pnl_percent = dod_security_pnl[date][stock2]['pnl_percent']
                            use_current = False
                        elif prev_date and prev_date in dod_security_pnl:
                            # Trade lasted more than one day, use previous date's PnL percentages
                            stock1_pnl_percent = dod_security_pnl[prev_date][stock1]['pnl_percent']
                            stock2_pnl_percent = dod_security_pnl[prev_date][stock2]['pnl_percent']
                            use_current = False
                        else:
                            # No previous date available (e.g. first day of OOS period), use current
                            stock1_pnl_percent = dod_security_pnl[date][stock1]['pnl_percent']
                            stock2_pnl_percent = dod_security_pnl[date][stock2]['pnl_percent']
                            use_current = True
                    else:
                        # Trade is still open, use current date's PnL percentages
                        stock1_pnl_percent = dod_security_pnl[date][stock1]['pnl_percent']
                        stock2_pnl_percent = dod_security_pnl[date][stock2]['pnl_percent']
                        use_current = True

                    try:
                        pair_pnl_percent = self.calculate_weighted_pnl_percentage(
                            stock1, stock2, stock1_pnl_percent, stock2_pnl_percent, use_current
                        )
                    except KeyError:
                        # If percentage_history is not available, use a simple average
                        pair_pnl_percent = (stock1_pnl_percent + stock2_pnl_percent) / 2
                        # Only show this warning once per pair
                        warning_key = f"percentage_history_{pair}"
                        if warning_key not in self.portfolio.warnings_shown:
                            log.warning(f"Percentage history not available for {pair}. Using simple average.")
                            self.portfolio.warnings_shown.add(warning_key)

                    pair_pnl_dollar = dod_security_pnl[date][stock1]['pnl_dollar'] + dod_security_pnl[date][stock2][
                        'pnl_dollar']

                    self.portfolio.dod_pair_trade_pnl_history[date][pair] = {
                        'pnl_dollar': pair_pnl_dollar,
                        'pnl_percent': pair_pnl_percent
                    }

        return self.portfolio.dod_pair_trade_pnl_history

    def calculate_acc_security_pnl(self, current_prices):
        acc_security_pnl = {}

        for symbol, current_price in current_prices.items():
            prev_vs_current = self.get_previous_vs_current_security(symbol, current_price)
            prev = prev_vs_current['previous']
            current = prev_vs_current['current']

            # Initialize or get the previous finished trade PnL
            prev_finished_pnl = self.portfolio.finished_trades_pnl.get(symbol, 0)

            # Initialize or get the total cost of all trades for this security
            if symbol not in self.portfolio.total_cost_history:
                self.portfolio.total_cost_history[symbol] = 0
            total_cost_all_trades = self.portfolio.total_cost_history[symbol]

            if current['last_trade'] and current['last_trade'].date == current['current_date']:
                # New trade occurred
                if current['last_trade'].order_type == 'open':
                    # For new open trades, use the trade price as the cost basis
                    new_cost_basis = current['last_trade'].price
                    total_cost = abs(current['current_position']) * new_cost_basis
                    current_pnl_dollar = 0
                    current_pnl_percent = 0

                    # Update total cost history
                    self.portfolio.total_cost_history[symbol] += total_cost

                elif current['last_trade'].order_type == 'close':
                    # For closing trades, use the previous cost basis and position
                    total_cost = abs(prev['prev_position']) * prev['prev_cost_basis']

                    # Calculate PnL for closing trades
                    closing_amount = min(abs(prev['prev_position']), abs(current['last_trade'].amount))
                    if prev['prev_position'] > 0:  # Long position
                        current_pnl_dollar = (current['last_trade'].price - prev['prev_cost_basis']) * closing_amount
                    else:  # Short position
                        current_pnl_dollar = (prev['prev_cost_basis'] - current['last_trade'].price) * closing_amount

                    # Update finished trades PnL
                    self.portfolio.finished_trades_pnl[symbol] = prev_finished_pnl + current_pnl_dollar

                    # Calculate percentage PnL
                    current_pnl_percent = current_pnl_dollar / self.portfolio.total_cost_history[symbol] if \
                    self.portfolio.total_cost_history[symbol] != 0 else 0

                    # Update cost basis for remaining position (if any)
                    remaining_position_date = self.find_remaining_position_date(symbol, prev_vs_current)
                    new_cost_basis = self.calculate_cost_basis(symbol, remaining_position_date)

                # Update cost_basis_history
                self.update_cost_basis_history(symbol, current['current_date'], new_cost_basis)

            else:
                # No new trade, use the current cost basis
                new_cost_basis = current['new_cost_basis']
                total_cost = abs(current['current_position']) * new_cost_basis

                # Calculate PnL for existing positions
                current_pnl_dollar = (current_price - new_cost_basis) * current['current_position']
                current_pnl_percent = current_pnl_dollar / self.portfolio.total_cost_history[symbol] if \
                self.portfolio.total_cost_history[symbol] != 0 else 0

            # Calculate accumulated PnL
            acc_pnl_dollar = prev_finished_pnl + current_pnl_dollar
            acc_pnl_percent = current_pnl_percent  # This is now just the current percentage

            # Update acc_security_pnl
            acc_security_pnl[symbol] = {
                'pnl_dollar': acc_pnl_dollar,
                'pnl_percent': acc_pnl_percent,
            }

        return acc_security_pnl

    def calculate_acc_pair_trade_pnl(self, current_prices):
        acc_pair_trade_pnl = {}
        latest_acc_security_pnl = self.portfolio.acc_security_pnl_history.get(self.portfolio.current_date, {})

        current_date = self.portfolio.current_date

        for pair in self.portfolio.pair_trade_history.keys():
            stock1, stock2 = pair.split('/')
            if stock1 in latest_acc_security_pnl and stock2 in latest_acc_security_pnl:
                # Get previous and current data
                prev_vs_current = self.get_previous_vs_current_pair(pair, current_prices)
                prev_date = prev_vs_current['previous']['prev_date']
                prev_acc_pair_pnl = prev_vs_current['previous']['prev_acc_pair_pnl']
                last_trade_1, last_trade_2 = prev_vs_current['current']['last_trade']

                position1, position2 = prev_vs_current['current']['current_position']
                prev_position1, prev_position2 = prev_vs_current['previous']['prev_position']

                # Calculate PnL dollar as the sum of individual security PnLs
                pnl_dollar = latest_acc_security_pnl[stock1]['pnl_dollar'] + latest_acc_security_pnl[stock2][
                    'pnl_dollar']

                # Determine if a trade occurred on the current date
                trade_today = (last_trade_1 and last_trade_1.date == current_date) or \
                              (last_trade_2 and last_trade_2.date == current_date)

                # Determine if positions were closed on the current date
                positions_closed_today = (position1 == 0 and position2 == 0) and (
                            prev_position1 != 0 or prev_position2 != 0)

                if trade_today and not positions_closed_today:
                    # New trade occurred on current date, use previous PnL
                    pnl_dollar = prev_acc_pair_pnl['pnl_dollar']
                    pnl_percent = prev_acc_pair_pnl['pnl_percent']
                elif positions_closed_today or position1 != 0 or position2 != 0:
                    # Positions were closed today or are still open, calculate new PnL
                    pnl_percent = self.calculate_weighted_pnl_percentage(
                        stock1, stock2,
                        latest_acc_security_pnl[stock1]['pnl_percent'],
                        latest_acc_security_pnl[stock2]['pnl_percent'],
                        use_current=True
                    )
                else:
                    # Both positions are closed and were closed before the current date
                    pnl_dollar = prev_acc_pair_pnl['pnl_dollar']
                    pnl_percent = prev_acc_pair_pnl['pnl_percent']

                # Calculate accumulated PnL
                acc_pair_trade_pnl[pair] = {
                    'pnl_dollar': pnl_dollar,
                    'pnl_percent': pnl_percent
                }

        return acc_pair_trade_pnl

    def reconcile_pnls(self, acc_security_pnl, acc_pair_trade_pnl):
        total_security_pnl = sum(pnl['pnl_dollar'] for pnl in acc_security_pnl.values())
        total_pair_pnl = sum(pnl['pnl_dollar'] for pnl in acc_pair_trade_pnl.values())

        if not math.isclose(total_security_pnl, total_pair_pnl, rel_tol=1e-9):
            log.warning(f"Total PnL mismatch on {self.portfolio.current_date}: "
                        f"Total Security PnL = {total_security_pnl}, Total Pair PnL = {total_pair_pnl}")

        # Check individual pairs
        for pair, pair_pnl in acc_pair_trade_pnl.items():
            stock1, stock2 = pair.split('/')
            security_pnl_sum = acc_security_pnl[stock1]['pnl_dollar'] + acc_security_pnl[stock2]['pnl_dollar']
            if not math.isclose(pair_pnl['pnl_dollar'], security_pnl_sum, rel_tol=1e-9):
                log.warning(f"PnL mismatch for pair {pair} on {self.portfolio.current_date}: "
                            f"Pair PnL = {pair_pnl['pnl_dollar']}, Security PnL sum = {security_pnl_sum}")

        return total_security_pnl, total_pair_pnl

    def calculate_trading_days_percentage(self, portfolio):
        # Get the NYSE calendar
        nyse = mcal.get_calendar('NYSE')

        # Check if there are any trades
        if not any(portfolio.pair_trade_history.values()):
            log.warning("No trades found in pair_trade_history.")
            return 0

        # Find the first trade date
        first_trade_date = min(
            trade.date for pair_trades in portfolio.pair_trade_history.values() for trade in pair_trades)

        log.info(f"First trade date: {first_trade_date} --> Last test date: {portfolio.current_date}")

        # Get all NYSE trading days between first trade and last day of simulation
        trading_days = nyse.valid_days(start_date=first_trade_date, end_date=portfolio.current_date)

        # Total NYSE trading days in the period
        total_trading_days = len(trading_days)
        log.info(f"Total trading days: {total_trading_days}")

        # Log information about share_history structure
        # log.info(f"Share history keys: {list(portfolio.share_history.keys())}")
        # for key, value in portfolio.share_history.items():
        #    log.info(f"Length of share history for {key}: {len(value)}")

        # Create a dictionary to hold the share data
        share_data = defaultdict(dict)
        for stock, history in portfolio.share_history.items():
            for date, shares in history:
                share_data[date][stock] = shares

        # Create DataFrame from the dictionary
        share_df = pd.DataFrame.from_dict(share_data, orient='index')
        share_df.index = pd.to_datetime(share_df.index)
        share_df = share_df.sort_index()

        # Remove rows where all stock columns are 0, NaN, or null
        valid_share_days = share_df[(share_df != 0) & (~share_df.isna())].dropna(how='all').index

        # Convert trading days and valid share days to integers for comparison
        trading_days_int = set(int(date.strftime('%Y%m%d')) for date in trading_days)
        valid_share_days_int = set(int(date.strftime('%Y%m%d')) for date in valid_share_days)

        # Count days with open positions
        days_with_positions = sum(1 for date in trading_days_int if date in valid_share_days_int)
        log.info(f"Days with positions: {days_with_positions}")

        # Check if positions were ever opened
        if days_with_positions == 0:
            log.warning("No days with open positions found.")

        # Log a sample of the share history
        # log.info(f"Sample of share history:\n{share_df.head()}")

        return days_with_positions / total_trading_days if total_trading_days > 0 else 0


class ExportExcel:
    def __init__(self, filename):
        self.filename = filename

    def format_date(self, date):
        if isinstance(date, datetime):
            return date.strftime('%Y-%m-%d')
        return str(date)

    def format_percentage(self, value):
        return f"{value:.6f}"  # Increased precision to 6 decimal places

    def should_keep_time(self, dates):
        return any(isinstance(d, datetime) and d.time() != time(0, 0, 0) for d in dates)

    def export_list_of_tuples(self, sheet, data):
        sheet.delete_rows(1, sheet.max_row)
        sheet.append(['Date', 'Value'])

        formatted_data = [(self.format_date(date), value) for date, value in data]
        for row in formatted_data:
            sheet.append(row)

        # Check if we should keep the time
        if not self.should_keep_time([date for date, _ in data]):
            for cell in sheet['A']:
                if cell.row > 1:  # Skip header
                    cell.value = cell.value.split()[0] if cell.value else cell.value

    def export_dict_of_symbol_tuples(self, sheet, data):
        sheet.delete_rows(1, sheet.max_row)
        all_dates = set()
        for symbol_data in data.values():
            if isinstance(symbol_data, list):
                all_dates.update(date for date, *_ in symbol_data)
            elif isinstance(symbol_data, dict):
                all_dates.update(symbol_data.keys())
        dates = sorted(all_dates)
        symbols = list(data.keys())

        header = ['Date'] + symbols
        sheet.append(header)

        for date in dates:
            row = [self.format_date(date)]
            for symbol in symbols:
                symbol_data = data[symbol]
                if isinstance(symbol_data, list):
                    value = next((v for d, v, *_ in symbol_data if d == date), '')
                elif isinstance(symbol_data, dict):
                    value = symbol_data.get(date, '')
                else:
                    value = ''
                row.append(value)
            sheet.append(row)

        # Check if we should keep the time
        if not self.should_keep_time(dates):
            for cell in sheet['A']:
                if cell.row > 1:  # Skip header
                    cell.value = cell.value.split()[0] if cell.value else cell.value

    def export_statistical_test_history(self, sheet, data):
        sheet.delete_rows(1, sheet.max_row)  # Clear existing data

        # Create headers
        headers = ['Date', 'Pair']
        all_attributes = set()
        for pair_data in data.values():
            for date_data in pair_data.values():
                all_attributes.update(date_data.keys())
        headers.extend(sorted(all_attributes))
        sheet.append(headers)

        # Add data
        for pair, pair_data in data.items():
            for date, test_results in pair_data.items():
                row = [self.format_date(date), pair]
                for attr in headers[2:]:
                    row.append(test_results.get(attr, ''))
                sheet.append(row)

        # Format date column
        for cell in sheet['A']:
            if cell.row > 1:  # Skip header
                cell.value = cell.value.split()[0] if cell.value else cell.value

    def export_daily_pnl_history(self, sheet, data):
        sheet.delete_rows(1, sheet.max_row)  # Clear existing data
        sheet.append(['Date', 'Daily PnL'])
        for date, pnl in data:
            sheet.append([self.format_date(date), pnl])

    def export_dod_security_pnl_history(self, sheet, data):
        sheet.delete_rows(1, sheet.max_row)  # Clear existing data
        headers = ['Date', 'Symbol', 'PnL Dollar', 'PnL Percent']
        sheet.append(headers)
        for date, securities in data.items():
            for symbol, pnl in securities.items():
                row = [self.format_date(date), symbol, pnl['pnl_dollar'], self.format_percentage(pnl['pnl_percent'])]
                sheet.append(row)

    def export_dod_pair_trade_pnl_history(self, sheet, data):
        sheet.delete_rows(1, sheet.max_row)  # Clear existing data
        headers = ['Date', 'Pair', 'PnL Dollar', 'PnL Percent']
        sheet.append(headers)
        for date, pairs in data.items():
            for pair, pnl in pairs.items():
                row = [self.format_date(date), pair, pnl['pnl_dollar'], self.format_percentage(pnl['pnl_percent'])]
                sheet.append(row)

    def export_acc_security_pnl_history(self, sheet, data):
        sheet.delete_rows(1, sheet.max_row)  # Clear existing data
        headers = ['Date', 'Symbol', 'PnL Dollar', 'PnL Percent']
        sheet.append(headers)
        for date, securities in data.items():
            for symbol, pnl in securities.items():
                row = [self.format_date(date), symbol, pnl['pnl_dollar'], self.format_percentage(pnl['pnl_percent'])]
                sheet.append(row)

    def export_acc_pair_trade_pnl_history(self, sheet, data):
        sheet.delete_rows(1, sheet.max_row)  # Clear existing data
        headers = ['Date', 'Pair', 'PnL Dollar', 'PnL Percent']
        sheet.append(headers)
        for date, pairs in data.items():
            for pair, pnl in pairs.items():
                row = [self.format_date(date), pair, pnl['pnl_dollar'], self.format_percentage(pnl['pnl_percent'])]
                sheet.append(row)

    def export_recorded_vars(self, sheet, data):
        sheet.delete_rows(1, sheet.max_row)  # Clear existing data

        # Create headers
        headers = ['Date', 'Pair']
        all_attributes = set()
        for pair_data in data.values():
            for date_data in pair_data.values():
                all_attributes.update(date_data.keys())
        headers.extend(sorted(all_attributes))
        sheet.append(headers)

        # Add data
        for pair, pair_data in data.items():
            for date, vars_data in pair_data.items():
                row = [self.format_date(date), pair]
                for attr in headers[2:]:
                    row.append(vars_data.get(attr, ''))  # Use empty string for missing attributes
                sheet.append(row)

        # Format date column
        for cell in sheet['A']:
            if cell.row > 1:  # Skip header
                cell.value = cell.value.split()[0] if cell.value else cell.value

    def export_stop_loss_history(self, sheet, data):
        sheet.delete_rows(1, sheet.max_row)  # Clear existing data

        # Create headers
        headers = ['Date', 'Pair', 'Reason', 'Price 1', 'Price 2', 'Spread', 'Z-Score',
                   'Volatility Stop Loss Level', 'Price Stop Loss Level',
                   'Max Holding Period', 'Since Last Trade', 'Triggered By']
        sheet.append(headers)

        # Add data
        for pair, stop_loss_events in data.items():
            for event in stop_loss_events:
                row = [
                    self.format_date(event['date']),
                    pair,
                    event['reason'],
                    event['price_1'],
                    event['price_2'],
                    event['spread'],
                    event['z_score'],
                    event['volatility_stop_loss_level'],
                    event['price_stop_loss_level'],
                    event['max_holding_period'],
                    event['since_last_trade'],
                    event['triggered_by']
                ]
                sheet.append(row)

        # Format date column
        for cell in sheet['A']:
            if cell.row > 1:  # Skip header
                cell.value = cell.value.split()[0] if cell.value else cell.value

    def export_pair_trade_history(self, sheet, data):
        sheet.delete_rows(1, sheet.max_row)  # Clear existing data

        # Create headers
        headers = ['Pair', 'Date', 'Symbol', 'Amount', 'Price', 'Order Type', 'Direction']
        sheet.append(headers)

        # Add data
        for pair, trades in data.items():
            for trade in trades:
                row = [
                    pair,
                    self.format_date(trade.date),
                    trade.symbol,
                    trade.amount,
                    trade.price,
                    trade.order_type,
                    trade.direction
                ]
                sheet.append(row)

        # Format date column
        for cell in sheet['B']:  # Assuming 'Date' is in column B
            if cell.row > 1:  # Skip header
                cell.value = cell.value.split()[0] if cell.value else cell.value

    def export_max_drawdown_history(self, sheet, data):
        sheet.delete_rows(1, sheet.max_row)  # Clear existing data
        sheet.append(['Date', 'Max Drawdown ($)', 'Max Drawdown (%)'])
        for date, drawdown_dollar, drawdown_percent in data:
            sheet.append([self.format_date(date), drawdown_dollar, f"{drawdown_percent:.2%}"])

    def export_portfolio_data(self, portfolio, context, statistical_tests=None):
        try:
            if os.path.exists(self.filename):
                workbook = openpyxl.load_workbook(self.filename)
            else:
                workbook = openpyxl.Workbook()
                workbook.remove(workbook.active)  # Remove the default sheet
        except Exception as e:
            print(f"Error opening or creating workbook: {e}")
            workbook = openpyxl.Workbook()
            workbook.remove(workbook.active)  # Remove the default sheet

        # List of all attributes to export
        all_attributes = [
            'acc_daily_pnl_history', 'acc_interest_history',
            'asset_history', 'equity_history',
            'interest_expense_history', 'liability_history', 'value_history',
            'price_history', 'share_history', 'percentage_history', 'cost_basis_history',
            'hedge_history',
            'asset_cash_history', 'asset_securities_history',
            'liability_securities_history', 'liability_loan_history'
        ]

        # Remove any sheets that are not in the all_attributes list, but keep at least one
        sheets_to_remove = [sheet for sheet in workbook.sheetnames if sheet not in all_attributes]
        for sheet_name in sheets_to_remove:
            workbook.remove(workbook[sheet_name])

        # Ensure at least one sheet exists
        if len(workbook.sheetnames) == 0:
            workbook.create_sheet("Sheet1")

        # Export list of tuples data
        list_attributes = [
            'acc_daily_pnl_history', 'acc_interest_history',
            'asset_history', 'equity_history',
            'interest_expense_history', 'liability_history', 'value_history',
            'asset_cash_history', 'liability_loan_history'
        ]
        for attr in list_attributes:
            if hasattr(portfolio, attr):
                data = getattr(portfolio, attr)
                if data:  # Only process non-empty data
                    if attr not in workbook.sheetnames:
                        sheet = workbook.create_sheet(attr)
                    else:
                        sheet = workbook[attr]
                    self.export_list_of_tuples(sheet, data)

        # Export dict of symbol tuples data
        dict_attributes = [
            'price_history', 'share_history', 'percentage_history', 'cost_basis_history',
            'hedge_history',
            'asset_securities_history', 'liability_securities_history'
        ]
        for attr in dict_attributes:
            if hasattr(portfolio, attr):
                data = getattr(portfolio, attr)
                if data:  # Only process non-empty data
                    if attr not in workbook.sheetnames:
                        sheet = workbook.create_sheet(attr)
                    else:
                        sheet = workbook[attr]
                    self.export_dict_of_symbol_tuples(sheet, data)

        # Export new histories
        if 'daily_pnl_history' not in workbook.sheetnames:
            sheet = workbook.create_sheet('daily_pnl_history')
        else:
            sheet = workbook['daily_pnl_history']
        self.export_daily_pnl_history(sheet, portfolio.daily_pnl_history)

        if 'acc_security_pnl_history' not in workbook.sheetnames:
            sheet = workbook.create_sheet('acc_security_pnl_history')
        else:
            sheet = workbook['acc_security_pnl_history']
        self.export_acc_security_pnl_history(sheet, portfolio.acc_security_pnl_history)

        if 'acc_pair_trade_pnl_history' not in workbook.sheetnames:
            sheet = workbook.create_sheet('acc_pair_trade_pnl_history')
        else:
            sheet = workbook['acc_pair_trade_pnl_history']
        self.export_acc_pair_trade_pnl_history(sheet, portfolio.acc_pair_trade_pnl_history)

        if 'dod_security_pnl_history' not in workbook.sheetnames:
            sheet = workbook.create_sheet('dod_security_pnl_history')
        else:
            sheet = workbook['dod_security_pnl_history']
        self.export_dod_security_pnl_history(sheet, portfolio.dod_security_pnl_history)

        if 'dod_pair_trade_pnl_history' not in workbook.sheetnames:
            sheet = workbook.create_sheet('dod_pair_trade_pnl_history')
        else:
            sheet = workbook['dod_pair_trade_pnl_history']
        self.export_dod_pair_trade_pnl_history(sheet, portfolio.dod_pair_trade_pnl_history)

        # Export statistical test history
        if 'statistical_test_history' not in workbook.sheetnames:
            sheet = workbook.create_sheet('statistical_test_history')
        else:
            sheet = workbook['statistical_test_history']
        self.export_statistical_test_history(sheet, portfolio.statistical_test_history)

        # Export recorded vars
        if 'recorded_vars' not in workbook.sheetnames:
            sheet = workbook.create_sheet('recorded_vars')
        else:
            sheet = workbook['recorded_vars']
        self.export_recorded_vars(sheet, context.recorded_vars)

        # Add this new section to export stop_loss_history
        if 'stop_loss_history' not in workbook.sheetnames:
            sheet = workbook.create_sheet('stop_loss_history')
        else:
            sheet = workbook['stop_loss_history']
        self.export_stop_loss_history(sheet, context.execution.stop_loss_history)

        # Add this new section to export pair_trade_history
        if 'pair_trade_history' not in workbook.sheetnames:
            sheet = workbook.create_sheet('pair_trade_history')
        else:
            sheet = workbook['pair_trade_history']
        self.export_pair_trade_history(sheet, portfolio.pair_trade_history)

        if 'max_drawdown_history' not in workbook.sheetnames:
            sheet = workbook.create_sheet('max_drawdown_history')
        else:
            sheet = workbook['max_drawdown_history']
        self.export_max_drawdown_history(sheet, portfolio.max_drawdown_history)

        # Save and close the workbook
        try:
            workbook.save(self.filename)
        except Exception as e:
            print(f"Error saving workbook: {e}")
        finally:
            workbook.close()


class Context:
    def __init__(self):
        self.portfolio = None
        self.execution = None
        self.recorded_vars = {}
        self.strategy_pairs = None
        self.num_pairs = None
        # Add any other necessary attributes


class Execution:
    def __init__(self):
        self.z_back = 36
        self.v_back = 32
        self.mean_back = 30
        self.std_back = 30
        self.entry_z = 0.5
        self.exit_z = 0.0
        self.hedge_lag = None
        self.volatility_stop_loss_multiplier = 2
        self.max_holding_period = 12  # in days
        self.cooling_off_period = 2
        self.volatility_stop_loss_level = None
        self.price_stop_loss_level = None
        self.price_level_stop_loss = {}  # Will be set for each pair
        self.stop_loss_history = {}

        self.amplifier = 2

        self.base_entry_z = 0.75
        self.base_exit_z = 0.0
        self.entry_volatility_factor = 2.25  # the larger volatility_factor is, the harder to enter a long position and a short position
        self.exit_volatility_factor = 0.75  # the larger volatility_factor is, the easier to exit in a long position and a short position
        self.max_entry_z = 2.5
        self.max_exit_z = 0.5


class PortfolioVisualizer:
    def __init__(self, portfolio, context, excel_filename, chart_dir=None):
        self.portfolio = portfolio
        self.context = context
        self.excel_filename = excel_filename
        self.chart_dir = chart_dir  # If set, save charts to this directory instead of plt.show()

    def plot_all_histories(self):
        self.plot_portfolio_history()
        self.plot_individual_stocks()
        self.plot_pair_trades()
        self.plot_additional_histories()

    def plot_portfolio_history(self):
        fig, ax = plt.subplots(figsize=(15, 10))

        dates = [date for date, _ in self.portfolio.equity_history]
        equity = [value for _, value in self.portfolio.equity_history]
        assets = [value for _, value in self.portfolio.asset_history]
        liabilities = [value for _, value in self.portfolio.liability_history]

        ax.plot(dates, equity, label='Net Equity')
        ax.plot(dates, assets, label='Total Assets')
        ax.plot(dates, liabilities, label='Total Liabilities')

        ax.set_title('Portfolio History')
        ax.legend()
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.grid(True)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, p: f'${x:,.0f}'))

        plt.xticks(rotation=45)
        plt.tight_layout()
        if self.chart_dir:
            plt.savefig(os.path.join(self.chart_dir, 'portfolio_history.png'), dpi=150, bbox_inches='tight')
            plt.close(fig)
        else:
            plt.show()

    def plot_individual_stocks(self):
        fig, axs = plt.subplots(len(self.context.strategy_pairs), 1, figsize=(15, 5 * len(self.context.strategy_pairs)),
                                sharex=True)

        trade_types = {'long_open': '^', 'short_open': 'v', 'long_close': 'v', 'short_close': '^'}
        trade_colors = {'long_open': 'green', 'short_open': 'red', 'long_close': 'blue', 'short_close': 'purple'}

        for i, pair in enumerate(self.context.strategy_pairs):
            stock_1, stock_2 = pair[0], pair[1]
            ax = axs[i] if len(self.context.strategy_pairs) > 1 else axs

            dates = [date for date, _ in self.portfolio.price_history[stock_1]]
            prices_1 = [price for _, price in self.portfolio.price_history[stock_1]]
            prices_2 = [price for _, price in self.portfolio.price_history[stock_2]]

            ax.plot(dates, prices_1, label=f'{stock_1} Price')
            ax.plot(dates, prices_2, label=f'{stock_2} Price')
            ax.set_title(f'Pair: {stock_1} - {stock_2}')

            trade_labels = set()
            for trade in self.portfolio.trades:
                if trade.symbol in [stock_1, stock_2]:
                    trade_type = f"{trade.direction}_{trade.order_type}"
                    label = trade_type.replace('_', ' ').title()
                    if label not in trade_labels:
                        ax.plot(trade.date, trade.price, trade_types[trade_type], markersize=7,
                                color=trade_colors[trade_type], label=label)
                        trade_labels.add(label)
                    else:
                        ax.plot(trade.date, trade.price, trade_types[trade_type], markersize=7,
                                color=trade_colors[trade_type])

            ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
            ax.xaxis.set_major_locator(mdates.MonthLocator())
            ax.grid(True)
            ax.yaxis.set_major_formatter(FuncFormatter(lambda x, p: f'${x:,.2f}'))
            ax.legend()

        plt.xticks(rotation=45)
        plt.tight_layout()
        if self.chart_dir:
            plt.savefig(os.path.join(self.chart_dir, 'individual_stocks.png'), dpi=150, bbox_inches='tight')
            plt.close(fig)
        else:
            plt.show()

    def plot_pair_trades(self):
        # Read the Excel file
        xls = pd.ExcelFile(self.excel_filename)

        price_history = pd.read_excel(xls, 'price_history', parse_dates=['Date'])
        hedge_history = pd.read_excel(xls, 'hedge_history', parse_dates=['Date'])
        pair_trade_history = pd.read_excel(xls, 'pair_trade_history', parse_dates=['Date'])

        n_pairs = len(self.context.strategy_pairs)
        fig, axs = plt.subplots(n_pairs, 1, figsize=(15, 6 * n_pairs), sharex=True)
        if n_pairs == 1:
            axs = [axs]

        # Define consistent colors
        price_colors = plt.cm.Set1(np.linspace(0, 1, 3))[:2]  # Two distinct colors for prices
        spread_color = 'gray'
        trade_colors = {'open': 'g', 'close': 'r'}

        for i, pair in enumerate(self.context.strategy_pairs):
            stock_1, stock_2 = pair[0], pair[1]
            ax = axs[i]

            # Get price data
            prices_1 = price_history[['Date', stock_1]].dropna()
            prices_2 = price_history[['Date', stock_2]].dropna()

            # Get hedge data
            hedge_data = hedge_history[['Date', f'{stock_1}/{stock_2}']].dropna()

            # Merge price and hedge data
            merged_data = prices_1.merge(prices_2, on='Date', suffixes=('_1', '_2'))
            merged_data = merged_data.merge(hedge_data, on='Date', how='left')
            merged_data[f'{stock_1}/{stock_2}'] = merged_data[f'{stock_1}/{stock_2}'].ffill()

            # Calculate adjusted prices and spread
            merged_data['adjusted_price_2'] = merged_data[stock_2] * merged_data[f'{stock_1}/{stock_2}']
            merged_data['spread'] = merged_data[stock_1] - merged_data['adjusted_price_2']

            # Plot prices and spread
            ax.plot(merged_data['Date'], merged_data[stock_1], color=price_colors[0], label=f'{stock_1} Price')
            ax.plot(merged_data['Date'], merged_data['adjusted_price_2'], color=price_colors[1],
                    label=f'{stock_2} Adjusted Price')
            ax.plot(merged_data['Date'], merged_data['spread'], color=spread_color, label='Spread', linestyle='--')

            # Plot trade events
            pair_trades = pair_trade_history[pair_trade_history['Pair'] == f'{stock_1}/{stock_2}']
            for _, trade in pair_trades.iterrows():
                color = trade_colors[trade['Order Type']]
                label = f"{trade['Order Type'].capitalize()} {trade['Direction']}"
                ax.axvline(x=trade['Date'], color=color, linestyle='--', label=label)

            ax.set_title(f'Pair Trade: {stock_1} - {stock_2}', fontsize=10)
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
            ax.xaxis.set_major_locator(mdates.MonthLocator())
            ax.grid(True)
            ax.yaxis.set_major_formatter(FuncFormatter(lambda x, p: f'${x:,.2f}'))

            # Create legend with unique labels
            handles, labels = ax.get_legend_handles_labels()
            by_label = dict(zip(labels, handles))
            ax.legend(by_label.values(), by_label.keys(), fontsize=8)

            ax.tick_params(axis='both', which='major', labelsize=8)

        plt.xticks(rotation=45, ha='right')
        plt.tight_layout()
        if self.chart_dir:
            plt.savefig(os.path.join(self.chart_dir, 'pair_trades.png'), dpi=150, bbox_inches='tight')
            plt.close(fig)
        else:
            plt.show()

    def plot_additional_histories(self):
        fig, axs = plt.subplots(len(self.context.strategy_pairs), 2, figsize=(20, 8 * len(self.context.strategy_pairs)),
                                sharex=True)

        for i, pair in enumerate(self.context.strategy_pairs):
            stock_1, stock_2 = pair[0], pair[1]
            pair_key = f"{stock_1}/{stock_2}"

            ax1 = axs[i, 0] if len(self.context.strategy_pairs) > 1 else axs[0]
            ax2 = axs[i, 1] if len(self.context.strategy_pairs) > 1 else axs[1]

            # Get all dates for this pair
            all_dates = sorted(set(self.context.recorded_vars[pair_key].keys()) |
                               set(self.portfolio.dod_pair_trade_pnl_history.keys()))

            # Prepare data for ax1
            z_scores, long_entry_zs, short_exit_zs, short_entry_zs, long_exit_zs, norm_vols, plot_dates = [], [], [], [], [], [], []
            for date in all_dates:
                data = self.context.recorded_vars[pair_key].get(date, {})
                z_score = data.get('Z_tech') or data.get('Z_finance') or data.get('Z_food') or data.get('Z_industrial') or data.get('Z_energy')
                entry_z = data.get('Entry_Z')
                exit_z = data.get('Exit_Z')
                norm_vol = data.get('Normalized_Spread_Sigma')

                if all(v is not None for v in [z_score, entry_z, exit_z, norm_vol]):
                    z_scores.append(z_score)
                    long_entry_zs.append(entry_z)
                    short_exit_zs.append(exit_z)
                    short_entry_zs.append(entry_z * -1)
                    long_exit_zs.append(exit_z * -1)
                    norm_vols.append(norm_vol)
                    plot_dates.append(date)

            # Plot data for ax1
            ax1.plot(plot_dates, z_scores, label='Z-score')
            ax1.plot(plot_dates, long_entry_zs, label='Long Entry Z', linestyle='--')
            ax1.plot(plot_dates, short_exit_zs, label='Short Exit Z', linestyle='--')
            ax1.plot(plot_dates, short_entry_zs, label='Short Entry Z', linestyle='--')
            ax1.plot(plot_dates, long_exit_zs, label='Long Exit Z', linestyle='--')
            ax1.plot(plot_dates, norm_vols, label='Normalized Volatility', alpha=0.5)
            ax1.set_title(f'{pair_key} Z-scores and Volatility')
            ax1.legend()
            ax1.grid(True)

            # Prepare and plot data for ax2
            dod_pnl = self.portfolio.dod_pair_trade_pnl_history
            dod_dates, dod_values = [], []
            for date in all_dates:
                value = dod_pnl.get(date, {}).get(pair_key, {}).get('pnl_dollar', 0)
                if value != 0:
                    dod_dates.append(date)
                    dod_values.append(value)

            ax2.bar(dod_dates, dod_values)
            ax2.set_title(f'{pair_key} DoD Pair Trade PnL')
            ax2.yaxis.set_major_formatter(FuncFormatter(lambda x, p: f'${x:,.0f}'))
            ax2.grid(True)

            # Set x-axis for both subplots
            min_date = min(min(plot_dates or [datetime.max]), min(dod_dates or [datetime.max]))
            max_date = max(max(plot_dates or [datetime.min]), max(dod_dates or [datetime.min]))
            for ax in [ax1, ax2]:
                ax.set_xlim(min_date, max_date)
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
                ax.xaxis.set_major_locator(mdates.AutoDateLocator())

            plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha='right')
            plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha='right')

        plt.tight_layout()
        if self.chart_dir:
            plt.savefig(os.path.join(self.chart_dir, 'z_scores_and_pnl.png'), dpi=150, bbox_inches='tight')
            plt.close(fig)
        else:
            plt.show()


class PortfolioStopLossFunction:
    def __init__(self, constant):
        self.constant = 1
        self.portfolio_order = PortfolioMakeOrder(constant=1)

    def check_volatility_stop_loss(self, context, pair, spread, z_score):
        pair_key = f"{pair[0]}/{pair[1]}"

        if len(spread) < max(context.execution.mean_back, context.execution.std_back):
            return False, None, None

        # Use the last mean_back number of spreads for mean calculation
        mean_spread = np.mean(spread[-context.execution.mean_back:])

        # Use the last std_back number of spreads for std calculation
        std_spread = np.std(spread[-context.execution.std_back:])

        if pair[2]['in_long']:
            context.execution.volatility_stop_loss_level = mean_spread - (
                        context.execution.volatility_stop_loss_multiplier * std_spread)
            if spread[-1] < context.execution.volatility_stop_loss_level:
                return True, "Volatility Stop Loss (Long)", context.execution.volatility_stop_loss_level
        elif pair[2]['in_short']:
            context.execution.volatility_stop_loss_level = mean_spread + (
                        context.execution.volatility_stop_loss_multiplier * std_spread)
            if spread[-1] > context.execution.volatility_stop_loss_level:
                return True, "Volatility Stop Loss (Short)", context.execution.volatility_stop_loss_level

        return False, None, None

    def check_time_based_stop_loss(self, context, pair):
        pair_key = f"{pair[0]}/{pair[1]}"
        if pair_key in context.portfolio.pair_trade_history:
            last_trade = context.portfolio.pair_trade_history[pair_key][-1]

            # Get the historical data for one of the pair's stocks (either will do)
            stock = pair[0]
            historical_data = context.data.history([stock], "price", context.execution.max_holding_period * 2, "1d")

            # If last_trade.date is outside the history window, position held longer than window > max_holding_period
            if last_trade.date not in historical_data.index:
                return True, "Time-based Stop Loss", None

            # Find the index of the last trade date and the current date
            last_trade_index = historical_data.index.get_loc(last_trade.date)
            current_date_index = historical_data.index.get_loc(context.portfolio.current_date)

            # Calculate the number of trading days between the two dates
            trading_days_since_last_trade = current_date_index - last_trade_index

            if trading_days_since_last_trade > context.execution.max_holding_period:
                return True, "Time-based Stop Loss", None
        return False, None, None

    def check_price_level_stop_loss(self, context, pair, spread, z_score):
        pair_key = f"{pair[0]}/{pair[1]}"
        context.execution.price_stop_loss_level = context.execution.price_level_stop_loss.get(pair_key)

        if context.execution.price_stop_loss_level is not None:
            if pair[2]['in_long'] and spread[-1] < context.execution.price_stop_loss_level:
                return True, "Price Level Stop Loss (Long)", context.execution.price_stop_loss_level
            elif pair[2]['in_short'] and spread[-1] > context.execution.price_stop_loss_level:
                return True, "Price Level Stop Loss (Short)", context.execution.price_stop_loss_level

        return False, None, None

    def handle_stop_loss(self, context, pair, reason, z_score):
        stock_1, stock_2 = pair[0], pair[1]
        pair_key = f"{stock_1}/{stock_2}"

        # Get current prices and spread
        current_price_1 = PortfolioMakeOrder.get_current_price(context, stock_1)
        current_price_2 = PortfolioMakeOrder.get_current_price(context, stock_2)
        current_spread = context.strategy_pairs[context.strategy_pairs.index(pair)][2]['spread'][-1]

        # Close the position
        self.portfolio_order.order_target(context, stock_1, 0)
        self.portfolio_order.order_target(context, stock_2, 0)

        # Record the stop loss event
        if pair_key not in context.execution.stop_loss_history:
            context.execution.stop_loss_history[pair_key] = []

        context.execution.stop_loss_history[pair_key].append({
            'date': context.portfolio.current_date.strftime('%Y-%m-%d'),
            'reason': reason,
            'price_1': current_price_1,
            'price_2': current_price_2,
            'spread': current_spread,
            'z_score': z_score,
            'volatility_stop_loss_level': context.execution.volatility_stop_loss_level,
            'price_stop_loss_level': context.execution.price_stop_loss_level,
            'max_holding_period': context.execution.max_holding_period,
            'since_last_trade': (context.portfolio.current_date - context.portfolio.pair_trade_history[pair_key][
                -1].date).days if context.portfolio.pair_trade_history[pair_key] else None,
            'triggered_by': reason
        })

        # Reset the pair's trading state
        for p in context.strategy_pairs:
            if p[0] == stock_1 and p[1] == stock_2:
                p[2]['in_short'] = False
                p[2]['in_long'] = False
                break

        log.info(f"Stop Loss triggered for pair {pair_key}")

        # Implement re-evaluation logic here
        # For now, we'll just avoid re-entering for a certain period
        context.execution.price_level_stop_loss[pair_key] = None  # Reset price-level stop loss

        return [stock_1, stock_2,
                {'in_short': False, 'in_long': False, 'spread': p[2]['spread'], 'hedge_history': p[2]['hedge_history']}]

    def re_evaluate_pair(self, context, pair):
        stock_1, stock_2 = pair[0], pair[1]
        pair_key = f"{stock_1}/{stock_2}"

        # Implement your re-evaluation logic here
        # For example, you might want to:
        # 1. Check for any significant news or events
        # 2. Re-run your cointegration tests
        # 3. Analyze recent price movements

        # For now, we'll implement a simple cooling-off period
        last_stop_loss = context.execution.stop_loss_history[pair_key][-1]['date']

        # Convert last_stop_loss to Timestamp if it's a string
        if isinstance(last_stop_loss, str):
            last_stop_loss = pd.Timestamp(last_stop_loss)

        if (context.portfolio.current_date - last_stop_loss).days < context.execution.cooling_off_period:
            return False  # Don't re-enter yet

        # Instead of clearing the history, add a new event
        context.execution.stop_loss_history[pair_key].append({
            'date': context.portfolio.current_date.strftime('%Y-%m-%d'),
            'reason': "Cooling-off period ended",
            'price_1': None,
            'price_2': None,
            'spread': None,
            'z_score': None,
            'volatility_stop_loss_level': None,
            'price_stop_loss_level': None,
            'max_holding_period': None,
            'since_last_trade': None,
            'triggered_by': "Re-evaluation"
        })

        return True  # Allow re-entry


class PortfolioMakeOrder:
    def __init__(self, constant):
        self.constant = constant
        self.open_orders = {}

    def get_current_price(context, asset):
        if asset in context.portfolio.price_history and context.portfolio.price_history[asset]:
            return context.portfolio.price_history[asset][-1][1]
        else:
            # If we don't have the price in our history, try to get it from the data
            prices = context.data.history([asset], "Adj Close", 1, "1d")
            if not prices.empty:
                price = prices[asset][-1]
                # Update the price history
                if asset not in context.portfolio.price_history:
                    context.portfolio.price_history[asset] = []
                context.portfolio.price_history[asset].append((context.portfolio.current_date, price))
                return price
            else:
                raise ValueError(f"No price data available for {asset}")

    def get_open_orders(self):
        return self.open_orders

    def update_balance_sheet(self, context, asset, shares_to_trade, current_price, current_position):
        cost = shares_to_trade * current_price

        if abs(shares_to_trade) > 0:
            if shares_to_trade > 0:  # Buy or Short Cover
                if current_position >= 0:  # Buy (open or increase long position)
                    context.asset_cash -= cost
                    context.asset_securities[asset] = context.asset_securities.get(asset, 0) + cost
                else:  # Short Cover (close or reduce short position)
                    context.asset_cash -= cost
                    context.liability_securities[asset] = max(0, context.liability_securities.get(asset, 0) - cost)
            else:  # Sell or Short
                if current_position > 0:  # Sell (close or reduce long position)
                    context.asset_cash += abs(cost)
                    context.asset_securities[asset] = max(0, context.asset_securities.get(asset, 0) - abs(cost))
                else:  # Short (open or increase short position)
                    context.asset_cash += abs(cost)
                    context.liability_securities[asset] = context.liability_securities.get(asset, 0) + abs(cost)


    def update_position(self, context, asset, shares_to_trade, current_price):
        current_position = context.positions.get(asset, 0)
        new_position = current_position + shares_to_trade

        if asset not in context.cost_basis_history:
            context.cost_basis_history[asset] = []

        if current_position == 0:
            # Opening a new position (long or short)
            context.cost_basis_history[asset].append((context.current_date, current_price))
        elif (current_position > 0 and shares_to_trade > 0) or (current_position < 0 and shares_to_trade < 0):
            # Adding to an existing position
            total_cost = abs(current_position) * context.cost_basis_history[asset][-1][1] + abs(
                shares_to_trade) * current_price
            new_cost_basis = total_cost / abs(new_position)
            context.cost_basis_history[asset].append((context.current_date, new_cost_basis))
        elif (current_position > 0 and shares_to_trade < 0) or (current_position < 0 and shares_to_trade > 0):
            # Reducing or closing a position
            if new_position == 0:
                context.cost_basis_history[asset].append((context.current_date, 0))
            elif abs(shares_to_trade) >= abs(current_position):
                # Closing or reversing a position
                context.cost_basis_history[asset].append((context.current_date, current_price))
            else:
                # Partially reducing a position, keep the current cost basis
                pass

        context.positions[asset] = new_position


    def computeHoldingsPct(self, stock_1_shares, stock_2_shares, stock_1_price, stock_2_price):
        stock_1_Dol = stock_1_shares * stock_1_price
        stock_2_Dol = stock_2_shares * stock_2_price
        notionalDol = abs(stock_1_Dol) + abs(stock_2_Dol)
        stock_1_perc = stock_1_Dol / notionalDol
        stock_2_perc = stock_2_Dol / notionalDol
        return (stock_1_perc, stock_2_perc)


    def order_target(self, context, asset, target):
        current_position = context.portfolio.positions.get(asset, 0)
        shares_to_trade = target - current_position
        current_price = PortfolioMakeOrder.get_current_price(context, asset)
        cost = shares_to_trade * current_price

        if cost > context.portfolio.asset_cash and shares_to_trade > 0:
            shares_to_trade = context.portfolio.asset_cash // current_price

        if abs(shares_to_trade) > 0:
            self.update_balance_sheet(context.portfolio, asset, shares_to_trade, current_price, current_position)
            self.update_position(context.portfolio, asset, shares_to_trade, current_price)
            trade = Trade(context.portfolio.current_date, asset, shares_to_trade, current_price,
                          'open' if target != 0 else 'close')
            context.portfolio.record_trade(trade)

        return shares_to_trade


    def order_target_percent(self, context, asset, target_percent):
        # Calculate total assets
        total_assets = context.portfolio.asset_cash + sum(
            max(shares, 0) * PortfolioMakeOrder.get_current_price(context, symbol)
            for symbol, shares in context.portfolio.positions.items()
        )

        # Calculate total liabilities
        total_liabilities = sum(
            abs(min(shares, 0)) * PortfolioMakeOrder.get_current_price(context, symbol)
            for symbol, shares in context.portfolio.positions.items()
        )

        # Calculate net equity
        net_equity = total_assets - total_liabilities

        # Calculate target value based on net equity
        target_value = net_equity * target_percent

        current_price = PortfolioMakeOrder.get_current_price(context, asset)
        target_shares = int(target_value / current_price)

        return self.order_target(context, asset, target_shares)


class PortfolioConstruct:
    def __init__(self, constant):
        self.constant = constant

    def hedge_ratio(sel, Y, X):
        # Prepare the data for Kalman Filter
        delta = 1e-5
        trans_cov = delta / (1 - delta) * np.eye(2)
        obs_mat = np.vstack([X, np.ones(X.shape)]).T[:, np.newaxis]

        # Initialize and run Kalman Filter
        kf = KalmanFilter(
            n_dim_obs=1,
            n_dim_state=2,
            initial_state_mean=np.zeros(2),
            initial_state_covariance=np.ones((2, 2)),
            transition_matrices=np.eye(2),
            observation_matrices=obs_mat,
            observation_covariance=1.0,
            transition_covariance=trans_cov
        )

        state_means, _ = kf.filter(Y.values)

        # Return the last slope estimate as the hedge ratio
        return state_means[-1, 0]

    def calculate_dynamic_z_scores_entry_exit(self, context, pair_key, current_volatility_of_spreads):
        # print(f"Start of function. volatilities_in_window for {pair_key}: {context.portfolio.volatilities_in_window[pair_key]}")

        volatilities_in_window = context.portfolio.volatilities_in_window[pair_key]

        # Calculate min and max volatilities from the rolling window
        min_volatility_in_window = min(volatilities_in_window)
        max_volatility_in_window = max(volatilities_in_window)

        # Normalize the current volatility
        if max_volatility_in_window > min_volatility_in_window:
            context.portfolio.normalized_volatility = (current_volatility_of_spreads - min_volatility_in_window) / (
                        max_volatility_in_window - min_volatility_in_window)
        else:
            context.portfolio.normalized_volatility = 0.5  # Default to middle value if min and max are the same

        # Calculate dynamic entry and exit z-scores

        context.execution.entry_z = context.execution.base_entry_z + context.execution.entry_volatility_factor * context.portfolio.normalized_volatility
        context.execution.exit_z = context.execution.base_exit_z + context.execution.exit_volatility_factor * context.portfolio.normalized_volatility

        # Ensure z-scores are within reasonable bounds
        context.execution.entry_z = max(context.execution.base_entry_z,
                                        min(context.execution.max_entry_z, context.execution.entry_z))
        context.execution.exit_z = max(context.execution.base_exit_z,
                                       min(context.execution.max_exit_z, context.execution.exit_z))

        # print(f"End of function. volatilities_in_window for {pair_key}: {context.portfolio.volatilities_in_window[pair_key]}")
        return context.portfolio.normalized_volatility, context.execution.entry_z, context.execution.exit_z

