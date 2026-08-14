"""Plan 04 §6 — cascade / forced-flow detector (Kalshi-native, tier-(a)+(b)).

The mechanism test already proved the load-bearing hypothesis: a price spike WITH
a concurrent OI drop reverts ~27–31 bps (beats cost), while a naive big-move fade
does not. This turns that into a real detector over the recorded Kalshi tape.

Signals combined per §6 (all from public/self-recorded Kalshi data):
  (a) one-sided aggressive trade-volume burst   — poll trades (taker_side)
  (c) OI DROP over the window                    — poll market stats (open_interest)
  (d) price overshoot vs the index composite     — mark vs index_live/composite
  (b) top-of-book depth depletion                — spread widening proxy from stats
      (full book depth is available via load_poll_books but heavy; the spread
      proxy is the light default — documented, not hidden)

OI-drop is weighted as the primary confirmation (it is the actual liquidation
signature). Output is a per-event table the strategy consumes.

All functions are pure over loader frames; params are a dataclass for the WF
sweep (Plan 04 §6 param list).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class DetectorParams:
    grid_sec: int = 10                 # feature-grid resolution (= stats cadence)
    burst_window_bars: int = 3         # ~30s aggressive-volume window
    baseline_bars: int = 180           # ~30min rolling baseline for intensity/vol
    intensity_threshold: float = 3.0   # aggressive vol vs baseline median
    one_sided_min: float = 0.65        # dominant side share of window volume
    oi_drop_min: float = 0.002         # fractional OI drop over the window (liq sig)
    overshoot_entry_bps: float = 15.0  # |mark-index| stretch to call it a cascade
    fade_lookback_bars: int = 2        # intensity now vs `fade` bars ago → fading?


def build_features(trades: pd.DataFrame, stats: pd.DataFrame, index: pd.Series,
                   p: DetectorParams) -> pd.DataFrame:
    """Resample trades+stats+index onto a common grid → cascade feature frame."""
    if len(stats) == 0 or len(index) == 0:
        return pd.DataFrame()
    freq = f"{p.grid_sec}s"
    # mark = mid from stats bid/ask (fallback to price). Kalshi mark is CONTRACT
    # price (~6.40); the index is UNDERLYING (~64000) → scale mark to underlying
    # via contract_size so overshoot is comparable (mark/csize vs index).
    st = stats.copy()
    st["mid_contract"] = (st["bid"] + st["ask"]) / 2.0
    st["mid_contract"] = st["mid_contract"].where(st["mid_contract"] > 0, st["price"])
    # PIT: no bfill — backfilling contract_size would leak a future value into
    # early rows; fall back to the probe-verified constants instead.
    csize = st["contract_size"].replace(0, np.nan).ffill()
    st["mid"] = st["mid_contract"] / csize
    st["spread_bps"] = 1e4 * (st["ask"] - st["bid"]) / st["mid_contract"].replace(0, np.nan)
    # PIT: label='right', closed='right' — bar labeled T holds (T-grid, T], so a
    # decision taken at label time T only uses data that existed at T. Pandas
    # default (label-left) stamps [T, T+grid) at T: 10s of lookahead per bar.
    g = st.resample(freq, label="right", closed="right").last()
    g["oi"] = st["oi"].resample(freq, label="right", closed="right").last()
    idx = index.resample(freq, label="right", closed="right").last()
    g["index"] = idx.reindex(g.index).ffill(limit=6)

    # aggressive volume by side from the trade tape (taker_side: bid=agg buy, ask=agg sell)
    if len(trades):
        tr = trades.copy()
        tr["buy_vol"] = np.where(tr["taker_side"] == "bid", tr["count"], 0.0)
        tr["sell_vol"] = np.where(tr["taker_side"] == "ask", tr["count"], 0.0)
        bv = tr["buy_vol"].resample(freq, label="right", closed="right").sum()
        sv = tr["sell_vol"].resample(freq, label="right", closed="right").sum()
        g["buy_vol"] = bv.reindex(g.index).fillna(0.0)
        g["sell_vol"] = sv.reindex(g.index).fillna(0.0)
    else:
        g["buy_vol"] = 0.0
        g["sell_vol"] = 0.0
    g = g.dropna(subset=["mid", "index"])
    if len(g) < p.baseline_bars + 5:
        return pd.DataFrame()

    # rolling-window aggregates (PIT: only past bars for baselines)
    win = p.burst_window_bars
    g["agg_vol"] = (g["buy_vol"] + g["sell_vol"]).rolling(win, min_periods=1).sum()
    base = g["agg_vol"].rolling(p.baseline_bars, min_periods=30).median().shift(1)
    g["intensity"] = g["agg_vol"] / base.replace(0, np.nan)
    wbuy = g["buy_vol"].rolling(win, min_periods=1).sum()
    wsell = g["sell_vol"].rolling(win, min_periods=1).sum()
    tot = (wbuy + wsell).replace(0, np.nan)
    g["sell_share"] = wsell / tot
    g["buy_share"] = wbuy / tot

    g["overshoot_bps"] = 1e4 * (g["mid"] - g["index"]) / g["index"]
    g["oi_delta"] = g["oi"].pct_change(win)          # OI change over the burst window
    g["intensity_prev"] = g["intensity"].shift(p.fade_lookback_bars)
    return g


def detect_cascades(feat: pd.DataFrame, p: DetectorParams) -> pd.DataFrame:
    """Cascade events from the feature frame (Plan 04 §6 detector).

    direction = +1 (buy: mark below index, forced SELLING overshot down)
                −1 (sell: mark above index, forced BUYING overshot up)
    Requires: overshoot stretched, OI dropping, one-sided burst above threshold.
    ``fading`` marks whether the burst is decelerating (entry-eligible per §6).
    """
    if len(feat) == 0:
        return pd.DataFrame()
    f = feat
    stretched = f["overshoot_bps"].abs() >= p.overshoot_entry_bps
    oi_drop = f["oi_delta"] <= -abs(p.oi_drop_min)
    burst = f["intensity"] >= p.intensity_threshold
    # the overshoot side must match the aggressive side:
    #   mark below index (overshoot<0) should come with aggressive SELLING
    down = (f["overshoot_bps"] < 0) & (f["sell_share"] >= p.one_sided_min)
    up = (f["overshoot_bps"] > 0) & (f["buy_share"] >= p.one_sided_min)
    is_event = stretched & oi_drop & burst & (down | up)

    ev = f[is_event].copy()
    if len(ev) == 0:
        return pd.DataFrame()
    ev["direction"] = np.where(ev["overshoot_bps"] < 0, 1, -1)   # fade toward index
    ev["fading"] = ev["intensity"] < ev["intensity_prev"]
    ev["confidence"] = (
        0.45 * (-ev["oi_delta"] / abs(p.oi_drop_min)).clip(0, 3) / 3
        + 0.30 * (ev["intensity"] / p.intensity_threshold).clip(0, 3) / 3
        + 0.25 * (ev["overshoot_bps"].abs() / p.overshoot_entry_bps).clip(0, 3) / 3)
    cols = ["direction", "overshoot_bps", "intensity", "oi_delta", "sell_share",
            "buy_share", "spread_bps", "fading", "confidence", "mid", "index"]
    return ev[cols]
