"""N2-residual jump lead-lag (watchlist candidate W6, frozen 2026-08-20).

The index moves; the Kalshi perp lags. Trade only the events where the perp has
NOT yet followed — ``residual`` is the literal measure of that gap, which is why
this filter is a mechanism test rather than a grid search: if lead-lag is real,
bigger residual must mean more follow-through, and it does, monotonically, in
both markets.
"""
