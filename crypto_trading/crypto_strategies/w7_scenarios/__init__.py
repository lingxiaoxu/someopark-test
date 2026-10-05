"""W7 entry-timing x market-scenario tables, rebuilt every 4 hours on a rolling window.

Read-only with respect to trading: this package never imports the order path and
never writes outside trading_signals/w7_scenarios/.
"""
