"""Plan 09 feature engineering — PIT-safe features on a 5-min grid.

Every feature at grid time t uses ONLY data with timestamp ≤ t:
  * trailing windows are closed on the right at t (pandas rolling on time index);
  * as-of joins (event gap, funding) use merge_asof backward with staleness caps;
  * vol percentile is an EXPANDING percentile (rank vs history up to t).
Labels use strictly FUTURE marks (shift(-h) on the grid) with a ±12bps dead zone.

The frame builder is deliberately dumb-and-cachable: one call loads everything
for a market, computes all features, and returns a single DataFrame. Research
scripts cache the result to trading_signals/research/ (parquet) keyed by market.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.loader import (load_funding, load_index_composite,
                                                 load_poll_market_stats,
                                                 load_poll_trades)

logger = logging.getLogger(__name__)

GRID = "5min"
DEAD_ZONE_BPS = 12.0            # ≈ maker+maker round trip — only bigger moves matter
HORIZONS = {"15m": 3, "60m": 12}   # label horizons in grid steps

ASSET_OF = {"KXBTCPERP": "BTC", "KXETHPERP": "ETH"}
OKX_OF = {"KXBTCPERP": "BTCUSDT", "KXETHPERP": "ETHUSDT"}
STRIP_OF = {"KXBTCPERP": "KXBTC", "KXETHPERP": "KXETH"}

# feature name → description (the gate-1 sweep iterates this registry)
FEATURES = [
    "flow_imb_5m", "flow_imb_15m", "flow_imb_60m",
    "oi_delta_15m", "oi_delta_60m",
    "basis_z",
    "event_gap_z",
    "funding_streak_signed", "funding_mag",
    "okx_liq_15m", "okx_liq_60m",
    "vol_pct_24h",
    "mom_15m", "mom_1h", "mom_4h",
]


def _signed_flow(trades: pd.DataFrame, grid: pd.DatetimeIndex,
                 window: str) -> pd.Series:
    """(taker-buy − taker-sell)/total volume over the trailing window at each
    grid point. taker_side=='bid' = aggressive BUY (verified fill semantics)."""
    signed = trades["count"].where(trades.taker_side == "bid", -trades["count"])
    total = trades["count"]
    s_sum = signed.resample(GRID, label="right", closed="right").sum()
    t_sum = total.resample(GRID, label="right", closed="right").sum()
    k = max(1, int(pd.Timedelta(window) / pd.Timedelta(GRID)))
    num = s_sum.rolling(k, min_periods=1).sum()
    den = t_sum.rolling(k, min_periods=1).sum()
    out = (num / den.replace(0.0, np.nan)).reindex(grid)
    return out


def _streak_series(funding: pd.DataFrame) -> pd.Series:
    """Signed streak at each settlement: +n for n consecutive positive nonzero
    cycles, −n for negative; 0-rate cycles keep the previous sign's streak
    frozen (they neither extend nor break it — matches the XS research)."""
    vals = []
    cur, prev_sign = 0, 0
    for r in funding["funding_rate"]:
        s = np.sign(r)
        if s != 0 and s == prev_sign:
            cur += 1
        elif s != 0:
            cur, prev_sign = 1, s
        vals.append(cur * (prev_sign if prev_sign != 0 else 0))
    return pd.Series(vals, index=funding.index)


def build_feature_frame(ticker: str) -> pd.DataFrame:
    """All Plan-09 features + labels for one market on the 5-min grid."""
    stats = load_poll_market_stats(ticker)
    trades = load_poll_trades(ticker).sort_index()
    if stats.empty or trades.empty:
        raise RuntimeError(f"no recorded tape for {ticker}")
    csize = float(stats.contract_size.dropna().median())

    mid = ((stats.bid + stats.ask) / 2.0).dropna()
    grid_mid = mid.resample(GRID, label="right", closed="right").last().dropna()
    grid = grid_mid.index
    f = pd.DataFrame(index=grid)
    f["mark_mid"] = grid_mid
    f["mark_underlying"] = grid_mid / csize

    # order flow
    for w, name in [("5min", "flow_imb_5m"), ("15min", "flow_imb_15m"),
                    ("60min", "flow_imb_60m")]:
        f[name] = _signed_flow(trades, grid, w)

    # OI deltas (pct change over trailing window)
    oi = stats.oi.dropna().resample(GRID, label="right", closed="right").last()
    oi = oi.reindex(grid).ffill(limit=3)
    for k, name in [(3, "oi_delta_15m"), (12, "oi_delta_60m")]:
        f[name] = oi.pct_change(k)

    # basis vs composite (24h z on the grid)
    comp = load_index_composite(ASSET_OF[ticker])["vw_close"]
    comp_g = comp.resample(GRID, label="right", closed="right").last().reindex(grid).ffill(limit=3)
    basis = (f["mark_underlying"] - comp_g) / comp_g
    mu = basis.rolling(288, min_periods=48).mean()
    sd = basis.rolling(288, min_periods=48).std(ddof=0)
    f["basis_z"] = (basis - mu) / sd.replace(0.0, np.nan)

    # event-implied gap z (as-of last strip snapshot, ≤10min stale)
    try:
        from crypto_trading.crypto_strategies.event_perp.strategy import build_gap_frame
        gap = build_gap_frame(STRIP_OF[ticker])[["gap_z"]].sort_index()
        gap.index = gap.index.tz_convert("UTC")
        f["event_gap_z"] = pd.merge_asof(
            pd.DataFrame(index=grid).reset_index(names="dt"),
            gap.reset_index(names="dt"), on="dt", direction="backward",
            tolerance=pd.Timedelta("10min")).set_index("dt")["gap_z"]
    except Exception:
        logger.warning("no strips gap for %s — event_gap_z all-NaN", ticker)
        f["event_gap_z"] = np.nan

    # funding state (as-of last REALIZED settlement — estimates weren't recorded)
    fund = load_funding(ticker).sort_index()
    fund = fund.assign(streak_signed=_streak_series(fund))
    fstate = fund[["funding_rate", "streak_signed"]].reset_index(names="dt")
    merged = pd.merge_asof(pd.DataFrame(index=grid).reset_index(names="dt"),
                           fstate, on="dt", direction="backward").set_index("dt")
    f["funding_streak_signed"] = merged["streak_signed"]
    f["funding_mag"] = merged["funding_rate"]

    # OKX liquidation bursts
    from crypto_trading.crypto_strategies.liq_reversion.widened import load_okx_liq_times
    liq_ts = load_okx_liq_times(OKX_OF[ticker])
    if len(liq_ts):
        liq = pd.Series(1.0, index=liq_ts).sort_index()
        liq_g = liq.resample(GRID, label="right", closed="right").sum().reindex(grid).fillna(0.0)
        f["okx_liq_15m"] = liq_g.rolling(3, min_periods=1).sum()
        f["okx_liq_60m"] = liq_g.rolling(12, min_periods=1).sum()
    else:
        f["okx_liq_15m"] = f["okx_liq_60m"] = 0.0

    # vol regime: trailing-24h realized vol, EXPANDING percentile (PIT)
    ret5 = f["mark_mid"].pct_change()
    rv = ret5.rolling(288, min_periods=48).std(ddof=0)
    f["vol_pct_24h"] = rv.expanding(min_periods=96).apply(
        lambda x: (x[:-1] <= x[-1]).mean() if len(x) > 1 else np.nan, raw=True)

    # session bucket (categorical; model-only, excluded from IC sweep)
    f["session"] = (grid.hour // 8).astype(int)

    # momentum baselines
    f["mom_15m"] = f["mark_mid"].pct_change(3)
    f["mom_1h"] = f["mark_mid"].pct_change(12)
    f["mom_4h"] = f["mark_mid"].pct_change(48)

    # labels: strictly-future forward returns + dead-zone classes
    for hname, k in HORIZONS.items():
        fwd = f["mark_mid"].shift(-k) / f["mark_mid"] - 1.0
        f[f"fwd_{hname}"] = fwd
        bps = fwd * 1e4
        f[f"label_{hname}"] = np.where(bps > DEAD_ZONE_BPS, 1,
                                       np.where(bps < -DEAD_ZONE_BPS, -1, 0))
        f.loc[fwd.isna(), f"label_{hname}"] = np.nan
    return f


def cached_feature_frame(ticker: str, *, refresh: bool = False) -> pd.DataFrame:
    cache = SIGNALS_DIR / "research" / f"ml_features_{ticker}.parquet"
    if cache.exists() and not refresh:
        return pd.read_parquet(cache)
    f = build_feature_frame(ticker)
    cache.parent.mkdir(parents=True, exist_ok=True)
    f.to_parquet(cache)
    return f
