"""Live-execution modules for the four watchlist candidates (W1-W4).

Built 2026-08-10 per the standing plan: modules are COMPLETE but DISARMED —
every run is a dry-run that logs the exact orders it would place, unless BOTH
(a) the strategy is enabled in live_watch/config.yaml AND (b) the global
execution gates pass (prod env + ALLOW_LIVE_ORDERS=1 + margin enabled).
Arming is therefore a deliberate two-step human act, never a default.

Entry point:  python -m crypto_trading.crypto_strategies.live_watch.runner
"""
