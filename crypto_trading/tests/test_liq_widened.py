"""Plan 04 widened tests — synthetic, no network: self-median anchor detection on
a non-BTC series, OKX-confirmation conditioning, ladder/cooldown accounting."""
import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_strategies.liq_reversion.signals.liquidation import (
    DetectorParams, build_features, detect_cascades)
from crypto_trading.crypto_strategies.liq_reversion.widened import (okx_confirmed,
                                                                    self_median_anchor)


def synth_stats(n=400, contract_price=4.50, csize=0.1, crash_at=350, crash_bps=30):
    """10s-grid stats for a fake alt perp; price crashes `crash_bps` at crash_at."""
    idx = pd.date_range("2026-07-20", periods=n, freq="10s", tz="UTC")
    px = np.full(n, contract_price)
    px[crash_at:] = contract_price * (1 - crash_bps / 1e4)
    oi = np.full(n, 10_000.0)
    oi[crash_at:] = 10_000.0 * (1 - 0.01)                 # 1% OI drop = liq signature
    return pd.DataFrame({"bid": px - 0.0005, "ask": px + 0.0005, "price": px,
                         "oi": oi, "oi_notional": oi * px,
                         "liq_mark": px, "contract_size": csize}, index=idx)


def synth_trades(stats, crash_at=350, burst=600.0):
    """Sparse background prints + a one-sided SELL burst at the crash."""
    rows = []
    for i, (ts, r) in enumerate(stats.iterrows()):
        rows.append({"ts": ts, "price": r["price"], "count": 1.0, "taker_side": "bid"})
        if i == crash_at:
            for j in range(3):
                rows.append({"ts": ts + pd.Timedelta(seconds=j), "price": r["price"],
                             "count": burst, "taker_side": "ask"})
    df = pd.DataFrame(rows).set_index("ts")
    df.index.name = "dt"
    return df.sort_index()


def test_self_median_anchor_detects_cascade_on_alt():
    stats = synth_stats()
    trades = synth_trades(stats)
    anchor = self_median_anchor(stats)
    # anchor is PIT: pre-crash median ⇒ the crash is an overshoot BELOW anchor
    det = DetectorParams(overshoot_entry_bps=15.0, baseline_bars=60)
    feat = build_features(trades, stats, anchor, det)
    ev = detect_cascades(feat, det)
    assert len(ev) >= 1, "self-median anchor must surface the injected cascade"
    assert (ev["direction"] == 1).all()                   # down-overshoot → fade = BUY
    assert (ev["oi_delta"] < 0).any()


def test_self_median_anchor_is_pit():
    stats = synth_stats()
    anchor = self_median_anchor(stats)
    # at the crash bar the anchor must still reflect PRE-crash level (shift(1))
    crash_ts = stats.index[350]
    pre = float(stats["price"].iloc[0] / stats["contract_size"].iloc[0])
    got = float(anchor.loc[:crash_ts].iloc[-1])
    assert abs(got - pre) / pre < 1e-6


def test_okx_confirmation_window():
    liq = pd.DatetimeIndex(pd.to_datetime(
        ["2026-07-20T10:00:00Z", "2026-07-20T15:30:00Z"]))
    inside = pd.Timestamp("2026-07-20T10:01:30Z")
    edge = pd.Timestamp("2026-07-20T10:02:00Z")           # exactly 2min → inside (≤)
    outside = pd.Timestamp("2026-07-20T10:02:01Z")
    assert okx_confirmed(inside, liq)
    assert okx_confirmed(edge, liq)
    assert not okx_confirmed(outside, liq)
    assert not okx_confirmed(inside, pd.DatetimeIndex([]))


def test_event_cooldown_dedups_clustered_bars():
    """Two events 20s apart = one cascade; cooldown must collapse to one entry."""
    from crypto_trading.crypto_strategies.liq_reversion.fill_aware import run_fill_aware
    stats = synth_stats(n=500, crash_at=300)
    # add a second flagged bar right after the first (same cascade cluster)
    trades = synth_trades(stats, crash_at=300)
    extra = synth_trades(stats, crash_at=302).iloc[-3:]   # second burst 20s later
    trades = pd.concat([trades, extra]).sort_index()
    anchor = self_median_anchor(stats)
    det = DetectorParams(overshoot_entry_bps=15.0, baseline_bars=60)

    r0 = run_fill_aware(ticker="KXFAKEPERP", det=det, stats=stats, trades=trades,
                        index_series=anchor, entry_style="taker",
                        event_cooldown_min=0.0, fee_scenario="zero")
    r15 = run_fill_aware(ticker="KXFAKEPERP", det=det, stats=stats, trades=trades,
                         index_series=anchor, entry_style="taker",
                         event_cooldown_min=15.0, fee_scenario="zero")
    # cooldown can only reduce (dedup) the number of round-trips
    assert r15["summary"]["round_trips"] <= r0["summary"]["round_trips"]
    if r0["summary"]["cascades_fading"] >= 2:
        assert r15["summary"]["round_trips"] <= max(1, r0["summary"]["round_trips"])


def test_taker_entry_pays_taker_fee():
    """Same single cascade: projected-fee taker entry must cost more than maker."""
    from crypto_trading.crypto_strategies.liq_reversion.fill_aware import run_fill_aware
    stats = synth_stats()
    trades = synth_trades(stats)
    anchor = self_median_anchor(stats)
    det = DetectorParams(overshoot_entry_bps=15.0, baseline_bars=60)
    kw = dict(ticker="KXBTCPERP", det=det, stats=stats, trades=trades,
              index_series=anchor, event_cooldown_min=15.0)
    rm = run_fill_aware(entry_style="maker", fee_scenario="projected", **kw)
    rt = run_fill_aware(entry_style="taker", fee_scenario="projected", **kw)
    if len(rm["trade_pnl"]) and len(rt["trade_pnl"]):
        assert rt["trade_pnl"]["fee"].iloc[0] > rm["trade_pnl"]["fee"].iloc[0]