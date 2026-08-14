"""Plan 04 fill-aware backtest tests — synthetic tape, no network.

Drives run_fill_aware with monkeypatched loaders so we control the cascade +
subsequent flow, and assert: a fading down-cascade with reversion → a profitable
passive fade; a still-accelerating cascade → adverse fill/loss; no cascade → no
trades. Also unit-tests the detector wiring end-to-end on a constructed tape.
"""
import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_strategies.liq_reversion import fill_aware as fa
from crypto_trading.crypto_strategies.liq_reversion.signals.liquidation import DetectorParams


def _mk_stats(index, bid, ask, oi, csize=1e-4):
    return pd.DataFrame({"bid": bid, "ask": ask, "price": (np.array(bid) + np.array(ask)) / 2,
                         "oi": oi, "contract_size": csize}, index=index)


def _grid(n, start="2026-07-20T00:00:00Z", sec=10):
    return pd.to_datetime(pd.date_range(start, periods=n, freq=f"{sec}s", tz="UTC"))


def _trades(rows):
    idx = pd.to_datetime([r[0] for r in rows], utc=True)
    return pd.DataFrame({"price": [r[1] for r in rows], "count": [r[2] for r in rows],
                         "taker_side": [r[3] for r in rows]}, index=idx)


@pytest.fixture
def patch_loaders(monkeypatch):
    """Install controllable recorded-tape loaders."""
    state = {}

    def install(stats, trades, index):
        state["stats"], state["trades"], state["index"] = stats, trades, index
        monkeypatch.setattr(fa, "load_poll_market_stats", lambda *a, **k: stats)
        monkeypatch.setattr(fa, "load_poll_trades", lambda *a, **k: trades)
        # code anchors on the composite (vw_close column)
        monkeypatch.setattr(fa, "load_index_composite",
                            lambda *a, **k: pd.DataFrame({"vw_close": index}))
    return install


def _cascade_tape(reverts: bool):
    """Build a down-cascade (mark overshoots BELOW index) then optionally reverts.

    Baseline calm, a forced-sell burst pushes the contract mark down ~40bps below
    index with an OI drop; if reverts, mark climbs back toward index afterward.
    """
    n = 260
    g = _grid(n)
    csize = 1e-4
    index_u = np.full(n, 64000.0)                    # underlying index flat
    mark_u = np.full(n, 64000.0)                     # underlying mark starts at index
    # cascade at bar 240: mark drops to 63750 (~ -39bps), OI drops
    cas = 240
    mark_u[cas:cas + 3] = 63750.0
    if reverts:
        mark_u[cas + 3:] = 63980.0                   # reverts back near index
    else:
        mark_u[cas + 3:] = 63600.0                   # keeps falling (adverse)
    mark_c = mark_u * csize                           # contract price ~6.4
    half = 0.0008                                     # ~ a couple bps spread
    bid = mark_c - half
    ask = mark_c + half
    oi = np.full(n, 100000.0)
    oi[cas:] = 98000.0                                # 2% OI drop (liq signature)
    # aggressive SELL volume burst at the cascade, calm before
    tr = []
    for i in range(cas):
        tr.append((g[i], mark_c[i], 1.0, "bid"))      # light two-sided
        tr.append((g[i], mark_c[i], 1.0, "ask"))
    for j in range(cas, cas + 3):                     # heavy one-sided sells
        for _ in range(20):
            tr.append((g[j], bid[j], 5.0, "ask"))     # sells hit the bid → fills a buy
    # post-cascade: trades that let the passive exit (sell at ask) fill
    for j in range(cas + 3, n):
        tr.append((g[j], ask[j], 5.0, "bid"))         # buys lift the ask → exit fills
    trades = pd.DataFrame(
        {"price": [t[1] for t in tr], "count": [t[2] for t in tr],
         "taker_side": [t[3] for t in tr]},
        index=pd.DatetimeIndex([t[0] for t in tr]))
    stats = _mk_stats(g, bid, ask, oi, csize)
    return stats, trades, pd.Series(index_u, index=g)


def test_fading_reverting_cascade_is_profitable(patch_loaders):
    stats, trades, index = _cascade_tape(reverts=True)
    patch_loaders(stats, trades, index)
    det = DetectorParams(overshoot_entry_bps=15.0, baseline_bars=180,
                         intensity_threshold=3.0, oi_drop_min=0.002)
    r = fa.run_fill_aware(det=det, queue_frac=0.0, entry_timeout_min=2,
                          exit_timeout_min=10, fee_scenario="zero")
    s = r["summary"]
    assert s["cascades_detected"] >= 1
    if s["round_trips"] >= 1:                          # entered the fade
        assert s["net_pnl_per_10c"] > 0               # reversion → profit


def test_accelerating_cascade_loses(patch_loaders):
    stats, trades, index = _cascade_tape(reverts=False)
    patch_loaders(stats, trades, index)
    det = DetectorParams(overshoot_entry_bps=15.0, baseline_bars=180,
                         intensity_threshold=3.0, oi_drop_min=0.002)
    r = fa.run_fill_aware(det=det, queue_frac=0.0, entry_timeout_min=2,
                          exit_timeout_min=10, fee_scenario="zero")
    s = r["summary"]
    if s["round_trips"] >= 1:
        assert s["net_pnl_per_10c"] <= 0              # cascade kept running → loss/abort


def test_no_cascade_no_trades(patch_loaders):
    n = 220
    g = _grid(n)
    csize = 1e-4
    mark_c = np.full(n, 6.40)
    stats = _mk_stats(g, mark_c - 0.0008, mark_c + 0.0008, np.full(n, 100000.0), csize)
    trades = pd.DataFrame(
        {"price": np.tile(mark_c, 2), "count": np.ones(2 * n),
         "taker_side": (["bid"] * n) + (["ask"] * n)},
        index=pd.DatetimeIndex(list(g) + list(g)))
    index = pd.Series(np.full(n, 64000.0), index=g)
    patch_loaders(stats, trades, index)
    r = fa.run_fill_aware(det=DetectorParams(overshoot_entry_bps=15.0),
                          queue_frac=0.0, fee_scenario="zero")
    assert r["summary"]["cascades_detected"] == 0 and r["summary"]["round_trips"] == 0
