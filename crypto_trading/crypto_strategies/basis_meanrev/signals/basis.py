"""Plan 01 signal math (`signals/basis.py`): deviation b_t, rolling z, OU
half-life, hysteresis entry/exit state machine.

The composite compositor pattern from sector_rotation degenerates to a single
factor here (Plan 01 §5) — kept as plain functions; the strategy loop applies
regime multipliers on top.

Sign convention: b_t = (mark − index)/index. Mark RICH (b_t high) → SHORT the
perp; mark CHEAP → LONG. Signal ∈ {−1, 0, +1} is the DESIRED position sign.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


def rolling_zscore(s: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    """z of the LAST point vs its own trailing window (PIT: no look-ahead)."""
    mp = min_periods or max(10, window // 3)
    mu = s.rolling(window, min_periods=mp).mean()
    sd = s.rolling(window, min_periods=mp).std(ddof=0)
    return (s - mu) / sd.replace(0.0, np.nan)


def ou_half_life(s: pd.Series) -> float | None:
    """OU half-life via the discrete AR(1) fit Δb_t = a + κ·b_{t-1} + ε.

    HL = −ln(2)/ln(1+κ) in units of the series' sampling interval.
    None ⇒ not mean-reverting on this window (κ ≥ 0) or degenerate input —
    Plan 01 §6: the risk gate treats that as "do not trade".
    """
    x = s.dropna()
    if len(x) < 20:
        return None
    lag = x.shift(1).dropna()
    delta = x.diff().dropna()
    lag, delta = lag.align(delta, join="inner")
    var = float(np.var(lag))
    if var <= 0:
        return None
    kappa = float(np.cov(lag, delta)[0, 1]) / var
    if kappa >= 0:                       # diverging / random walk
        return None
    base = 1.0 + kappa
    if base <= 0:                        # over-damped beyond one step
        return 1.0
    return -math.log(2.0) / math.log(base)


@dataclass(frozen=True)
class BasisParams:
    """WF sweep surface (Plan 01 §6). Defaults = config sketch §9."""
    zscore_window_min: int = 30
    entry_k: float = 2.5
    exit_k: float = 0.5
    time_stop_min: int = 90
    half_life_max_min: float = 60.0
    half_life_window_min: int = 240
    min_abs_bps: float = 5.0            # ignore stretches smaller than spread-scale


def compute_signal_frame(frame: pd.DataFrame, p: BasisParams) -> pd.DataFrame:
    """Vectorised signal state over a 1m basis frame (loader.build_basis_frame).

    Adds: z, half_life_min, desired ∈ {−1,0,+1}, plus the raw gates. The
    hysteresis state machine (enter at |z|≥entry_k, hold until |z|≤exit_k or
    time-stop) is sequential by nature — small explicit loop, PIT-correct.
    """
    out = frame.copy()
    out["z"] = rolling_zscore(out.b_t, p.zscore_window_min)
    out["half_life_min"] = (
        out.b_t.rolling(p.half_life_window_min,
                        min_periods=max(30, p.half_life_window_min // 4))
        .apply(lambda w: ou_half_life(pd.Series(w)) or np.nan, raw=False))

    desired = np.zeros(len(out), dtype=float)
    state = 0
    entry_i = None
    z = out.z.to_numpy()
    hl = out.half_life_min.to_numpy()
    bps = out.b_t_bps.to_numpy()
    for i in range(len(out)):
        zi, hli = z[i], hl[i]
        if state == 0:
            tradeable = (not np.isnan(zi) and not np.isnan(hli)
                         and hli <= p.half_life_max_min and abs(bps[i]) >= p.min_abs_bps)
            if tradeable and zi >= p.entry_k:
                state, entry_i = -1, i          # rich → short
            elif tradeable and zi <= -p.entry_k:
                state, entry_i = +1, i          # cheap → long
        else:
            timed_out = entry_i is not None and (i - entry_i) >= p.time_stop_min
            hl_blown = np.isnan(hli) or hli > p.half_life_max_min
            reverted = not np.isnan(zi) and abs(zi) <= p.exit_k
            if reverted or timed_out or hl_blown:
                state, entry_i = 0, None
        desired[i] = state
    out["desired"] = desired
    return out
