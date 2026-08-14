"""Plan 04 cascade-detector tests — synthetic tape, no network. Injects a
liquidation signature (OI drop + one-sided burst + overshoot) and asserts the
detector fires with the right direction; a calm tape fires nothing."""
import numpy as np
import pandas as pd

from crypto_trading.crypto_strategies.liq_reversion.signals.liquidation import (
    DetectorParams, build_features, detect_cascades)

CSIZE = 1e-4          # BTC contract size


def _grid(n, freq_s=10, start="2026-07-07"):
    return pd.date_range(start, periods=n, freq=f"{freq_s}s", tz="UTC")


def make_data(n=400, cascade_at=None, direction="down"):
    """Build (trades, stats, index) with an optional injected cascade.

    ``direction='down'`` = forced SELLING pushes the mark BELOW the index
    (overshoot < 0) → detector should say direction=+1 (buy toward index).
    """
    idx_t = _grid(n)
    underlying = 64_000.0
    index = pd.Series(underlying, index=idx_t)               # flat index
    mid_contract = np.full(n, underlying * CSIZE)            # mark tracks index
    oi = np.full(n, 1_000_000.0)
    # baseline calm trades: tiny two-sided flow every bar
    tr_rows = []
    for i, t in enumerate(idx_t):
        tr_rows.append({"dt": t, "price": underlying * CSIZE, "count": 1.0,
                        "taker_side": "bid" if i % 2 else "ask"})

    if cascade_at is not None:
        # inject: mark dislocates from index, OI drops, one-sided aggressive burst
        sgn = -1 if direction == "down" else +1
        for j in range(cascade_at, min(cascade_at + 3, n)):
            mid_contract[j] = underlying * CSIZE * (1 + sgn * 0.0008)   # ~8bps off
            oi[j] = 1_000_000.0 * (1 - 0.01)                            # 1% OI drop
            side = "ask" if direction == "down" else "bid"             # aggressive sells/buys
            for _ in range(60):                                        # volume burst
                tr_rows.append({"dt": idx_t[j], "price": mid_contract[j],
                                "count": 5.0, "taker_side": side})

    trades = pd.DataFrame(tr_rows).set_index("dt").sort_index()
    spread = underlying * CSIZE * 1e-4
    stats = pd.DataFrame({
        "bid": mid_contract - spread / 2, "ask": mid_contract + spread / 2,
        "price": mid_contract, "oi": oi, "oi_notional": oi,
        "liq_mark": mid_contract, "contract_size": CSIZE}, index=idx_t)
    return trades, stats, index


def test_detector_fires_on_injected_down_cascade():
    tr, st, ix = make_data(cascade_at=300, direction="down")
    p = DetectorParams(overshoot_entry_bps=5.0, oi_drop_min=0.002,
                       intensity_threshold=2.0, baseline_bars=120)
    feat = build_features(tr, st, ix, p)
    ev = detect_cascades(feat, p)
    assert len(ev) >= 1
    assert (ev["direction"] == 1).all()             # mark below index → buy toward index
    assert (ev["overshoot_bps"] < 0).all()


def test_detector_up_cascade_direction():
    tr, st, ix = make_data(cascade_at=300, direction="up")
    p = DetectorParams(overshoot_entry_bps=5.0, oi_drop_min=0.002,
                       intensity_threshold=2.0, baseline_bars=120)
    ev = detect_cascades(build_features(tr, st, ix, p), p)
    assert len(ev) >= 1 and (ev["direction"] == -1).all()   # mark above index → sell


def test_calm_tape_fires_nothing():
    tr, st, ix = make_data(cascade_at=None)
    p = DetectorParams(overshoot_entry_bps=5.0, oi_drop_min=0.002,
                       intensity_threshold=2.0, baseline_bars=120)
    ev = detect_cascades(build_features(tr, st, ix, p), p)
    assert len(ev) == 0                              # no dislocation → no cascade


def test_oi_drop_required():
    # same price dislocation + burst but NO OI drop → not a liquidation signature
    tr, st, ix = make_data(cascade_at=300, direction="down")
    st["oi"] = 1_000_000.0                           # flatten OI (remove the drop)
    p = DetectorParams(overshoot_entry_bps=5.0, oi_drop_min=0.002,
                       intensity_threshold=2.0, baseline_bars=120)
    ev = detect_cascades(build_features(tr, st, ix, p), p)
    assert len(ev) == 0                              # OI-drop is a required confirmation


def test_overshoot_threshold_gates():
    tr, st, ix = make_data(cascade_at=300, direction="down")
    # require a 50bps overshoot — the injected 8bps one must NOT qualify
    p = DetectorParams(overshoot_entry_bps=50.0, oi_drop_min=0.002,
                       intensity_threshold=2.0, baseline_bars=120)
    ev = detect_cascades(build_features(tr, st, ix, p), p)
    assert len(ev) == 0


def test_fading_flag_present_and_boolean():
    tr, st, ix = make_data(cascade_at=300, direction="down")
    p = DetectorParams(overshoot_entry_bps=5.0, oi_drop_min=0.002,
                       intensity_threshold=2.0, baseline_bars=120)
    ev = detect_cascades(build_features(tr, st, ix, p), p)
    assert "fading" in ev.columns and ev["fading"].dtype == bool
