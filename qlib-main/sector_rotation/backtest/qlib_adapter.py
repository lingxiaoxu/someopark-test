"""
qlib_adapter.py
===============
Adapters that fit US ETF data and trading calendar into qlib's backtest infrastructure.

We adapt OUR architecture to fit qlib — never the reverse:
    - SectorETFExchange(Exchange)         : injects yfinance prices into qlib Exchange
    - USTradeCalendarManager(TradeCalendarManager) : bypasses Cal.calendar() for US dates
    - SectorSimulatorExecutor(SimulatorExecutor)   : uses USTradeCalendarManager

All qlib infrastructure (Account, Position, Exchange, BaseExecutor, TradeCalendarManager,
CommonInfrastructure) is used as-is. Only the data-loading entry points are overridden
so they pull from our pre-loaded DataFrames instead of D.features().
"""

from __future__ import annotations

import sys
import io as _io
import logging
from typing import List, Optional, Union

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Suppress qlib init noise on import
# ---------------------------------------------------------------------------
_stderr = sys.stderr
sys.stderr = _io.StringIO()
try:
    from qlib.backtest.exchange import Exchange
    from qlib.backtest.utils import TradeCalendarManager, CommonInfrastructure, LevelInfrastructure
    from qlib.backtest.account import Account
    from qlib.backtest.position import BasePosition
    from qlib.backtest.executor import SimulatorExecutor
    from qlib.backtest.decision import Order, OrderDir, TradeDecisionWO
    from qlib.backtest.backtest import backtest_loop
    _QLIB_BACKTEST_AVAILABLE = True
except Exception as _e:
    _QLIB_BACKTEST_AVAILABLE = False
    logger.warning(f"qlib backtest infrastructure not available: {_e}")
sys.stderr = _stderr


# ---------------------------------------------------------------------------
# Helper: convert yfinance wide price DataFrame → qlib-compatible quote_df
# ---------------------------------------------------------------------------

def _prices_to_quote_df(prices: pd.DataFrame) -> pd.DataFrame:
    """
    Convert a wide price DataFrame (rows=dates, cols=tickers) to qlib's
    MultiIndex(instrument, datetime) quote format.

    Required qlib columns:
        $close   – closing price (used for valuation)
        $change  – day-over-day pct change (used for limit detection → all 0 = no limits)
        $factor  – adjustment factor for share-unit rounding (all 1.0 = disable rounding)
        $volume  – daily volume (set to 1.0; unused in weight-based backtest)

    Index:
        MultiIndex(instrument, datetime) — qlib's standard quote format

    Parameters
    ----------
    prices : pd.DataFrame
        Shape (n_days, n_tickers).  Index must be a DatetimeIndex.
    """
    records = []
    tickers = prices.columns.tolist()
    dates = prices.index

    for ticker in tickers:
        close_s = prices[ticker].dropna()
        if close_s.empty:
            continue
        # Pct change for $change; fill first NaN with 0
        change_s = close_s.pct_change().fillna(0.0)
        df = pd.DataFrame(
            {
                "$close": close_s,
                "$change": change_s,
                "$factor": 1.0,
                "$volume": 1.0,
            },
            index=close_s.index,
        )
        df.index = pd.MultiIndex.from_arrays(
            [np.full(len(df), ticker), df.index],
            names=["instrument", "datetime"],
        )
        records.append(df)

    if not records:
        raise ValueError("prices DataFrame is empty — cannot build quote_df")

    quote_df = pd.concat(records).sort_index()
    return quote_df


# ---------------------------------------------------------------------------
# SectorETFExchange
# ---------------------------------------------------------------------------

class SectorETFExchange(Exchange):
    """
    qlib Exchange subclass that injects pre-loaded yfinance ETF prices
    instead of calling D.features().

    Override strategy: only ``get_quote_from_qlib()`` is overridden.
    All other Exchange logic (deal_order, check_limit, generate_orders, etc.)
    is used unchanged from qlib.

    Usage
    -----
    exchange = SectorETFExchange(
        prices=etf_prices_df,          # wide DataFrame (dates × tickers)
        open_cost=0.0005,
        close_cost=0.0005,
        min_cost=0.0,
    )
    """

    def __init__(
        self,
        prices: pd.DataFrame,
        open_cost: float = 0.0005,
        close_cost: float = 0.0005,
        min_cost: float = 0.0,
        impact_cost: float = 0.0,
        **kwargs,
    ) -> None:
        # Pre-build the quote_df BEFORE super().__init__() calls get_quote_from_qlib()
        self._prepared_quote_df = _prices_to_quote_df(prices)
        self._etf_tickers = list(prices.columns)

        super().__init__(
            # Pass a list to avoid Exchange calling D.instruments(codes)
            codes=self._etf_tickers,
            deal_price="$close",
            limit_threshold=None,        # No circuit-breaker limits for US ETFs
            trade_unit=None,             # Disable share-rounding (weight-based backtest)
            open_cost=open_cost,
            close_cost=close_cost,
            min_cost=min_cost,
            impact_cost=impact_cost,
            **kwargs,
        )

    def get_quote_from_qlib(self) -> None:
        """
        Override: inject pre-loaded prices instead of calling D.features().
        Called by Exchange.__init__() — must set self.quote_df.
        """
        self.quote_df = self._prepared_quote_df

        # Disable adjusted-price mode (our prices are already fully adjusted)
        self.trade_w_adj_price = False

        # Build limit_buy / limit_sell columns (all False for US ETFs — no circuit breakers)
        self._update_limit(self.limit_threshold)  # limit_threshold=None → no limits


# ---------------------------------------------------------------------------
# USTradeCalendarManager
# ---------------------------------------------------------------------------

class USTradeCalendarManager(TradeCalendarManager):
    """
    TradeCalendarManager subclass for US market trading dates.

    Bypasses Cal.calendar() and Cal.locate_index() which require a qlib
    data provider configured for Chinese markets.

    Our strategy: override reset() to use np.searchsorted on our own
    pre-computed calendar array.

    NOTE: _custom_calendar MUST be set before super().__init__() because
    super().__init__() calls self.reset() immediately.
    """

    def __init__(
        self,
        freq: str,
        start_time: Union[str, pd.Timestamp],
        end_time: Union[str, pd.Timestamp],
        trading_dates: Union[List, np.ndarray],
        level_infra: Optional[LevelInfrastructure] = None,
    ) -> None:
        # Sorted Timestamp array — must be set BEFORE super().__init__() calls reset()
        self._custom_calendar: np.ndarray = np.array(
            sorted([pd.Timestamp(d) for d in trading_dates])
        )
        super().__init__(
            freq=freq,
            start_time=start_time,
            end_time=end_time,
            level_infra=level_infra,
        )

    def reset(
        self,
        freq: str,
        start_time: Union[str, pd.Timestamp, None] = None,
        end_time: Union[str, pd.Timestamp, None] = None,
    ) -> None:
        """
        Override: use _custom_calendar array instead of Cal.calendar() /
        Cal.locate_index().  Uses np.searchsorted for O(log n) lookup.
        """
        self.freq = freq
        self.start_time = pd.Timestamp(start_time) if start_time else None
        self.end_time = pd.Timestamp(end_time) if end_time else None

        # Use our pre-built calendar
        self._calendar = self._custom_calendar
        n = len(self._calendar)

        if n < 2:
            raise ValueError("USTradeCalendarManager needs at least 2 trading dates")

        # Find start/end indices via binary search
        if start_time is not None:
            _st = pd.Timestamp(start_time)
            # start_index: first calendar date >= start_time
            self.start_index = int(np.searchsorted(self._calendar, _st, side="left"))
        else:
            self.start_index = 0

        if end_time is not None:
            _et = pd.Timestamp(end_time)
            # end_index: last calendar date <= end_time
            # searchsorted "right" gives insert point for end_time, -1 for the last date <= end_time
            self.end_index = int(np.searchsorted(self._calendar, _et, side="right")) - 1
        else:
            # Leave room for get_step_time() which accesses _calendar[end_index + 1]
            self.end_index = n - 2

        # Clip to valid range (must leave room for +1 access in get_step_time)
        self.start_index = max(0, min(self.start_index, n - 2))
        self.end_index = max(self.start_index, min(self.end_index, n - 2))

        self.trade_len = self.end_index - self.start_index + 1
        self.trade_step = 0


# ---------------------------------------------------------------------------
# SectorSimulatorExecutor
# ---------------------------------------------------------------------------

class SectorSimulatorExecutor(SimulatorExecutor):
    """
    SimulatorExecutor subclass that uses USTradeCalendarManager for
    US NYSE trading dates instead of qlib's Cal.calendar().

    NOTE: _trading_dates MUST be stored before super().__init__() because
    super().__init__() → BaseExecutor.__init__() → self.reset() → our
    overridden reset() needs _trading_dates to be available.
    """

    def __init__(
        self,
        *args,
        trading_dates: Union[List, np.ndarray],
        **kwargs,
    ) -> None:
        # Must be set before super().__init__() calls reset()
        self._trading_dates: np.ndarray = np.array(
            sorted([pd.Timestamp(d) for d in trading_dates])
        )
        super().__init__(*args, **kwargs)

    def reset(
        self,
        common_infra: Optional[CommonInfrastructure] = None,
        **kwargs,
    ) -> None:
        """
        Override: inject USTradeCalendarManager into level_infra instead of
        letting LevelInfrastructure.reset_cal() create a TradeCalendarManager
        (which calls Cal.calendar()).
        """
        if "start_time" in kwargs or "end_time" in kwargs:
            start_time = kwargs.get("start_time")
            end_time = kwargs.get("end_time")

            if not self.level_infra.has("trade_calendar"):
                # First initialization: create and inject our calendar
                cal = USTradeCalendarManager(
                    freq=self.time_per_step,
                    start_time=start_time,
                    end_time=end_time,
                    trading_dates=self._trading_dates,
                    level_infra=self.level_infra,
                )
                self.level_infra.reset_infra(trade_calendar=cal)
            else:
                # Subsequent reset: call our calendar's reset()
                self.level_infra.get("trade_calendar").reset(
                    self.time_per_step,
                    start_time=start_time,
                    end_time=end_time,
                )

        if common_infra is not None:
            self.reset_common_infra(common_infra)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "_prices_to_quote_df",
    "SectorETFExchange",
    "USTradeCalendarManager",
    "SectorSimulatorExecutor",
    "_QLIB_BACKTEST_AVAILABLE",
]
