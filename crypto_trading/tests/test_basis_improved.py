"""Plan 01 selective-variant tests — synthetic Prep, no network."""
import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_strategies.basis_meanrev.improved import (ImprovedParams, Prep,
                                                                     TICK, flow_is_fading,
                                                                     oi_dropped, run_config)


def minutes(n, start="2026-07-20T00:00:00Z"):
    return pd.date_range(start, periods=n, freq="1min", tz="UTC")


def make_prep(n=40, bid=6.40, ask=6.41, bps=None, z=None, ask_path=None,
              trades_rows=(), vol_bid=None) -> Prep:
    idx = minutes(n)
    frame = pd.DataFrame(index=idx)
    frame["bid"] = bid
    frame["ask"] = ask_path if ask_path is not None else ask
    frame["b_t_bps"] = bps if bps is not None else 0.0
    frame["z"] = z if z is not None else 0.0
    frame["hl"] = 10.0
    frame["b_t"] = frame.b_t_bps / 1e4
    tr_idx = pd.to_datetime([r[0] for r in trades_rows], utc=True)
    trades = pd.DataFrame({"price": [r[1] for r in trades_rows],
                           "count": [r[2] for r in trades_rows],
                           "taker_side": [r[3] for r in trades_rows]}, index=tr_idx)
    mv = pd.DataFrame({"bid": vol_bid if vol_bid is not None else np.zeros(n),
                       "ask": np.zeros(n)}, index=idx)
    oi = pd.Series(1000.0, index=idx)
    return Prep(frame=frame, trades=trades.sort_index(), minute_vol=mv, oi=oi, csize=1e-4)


def test_fading_filter_blocks_accelerating_admits_fading():
    idx = minutes(10)
    accel = pd.DataFrame({"bid": [0, 0, 0, 0, 5, 5, 5, 5, 50, 50],
                          "ask": np.zeros(10)}, index=idx)
    fading = pd.DataFrame({"bid": [0, 0, 0, 50, 50, 50, 50, 5, 5, 5],
                           "ask": np.zeros(10)}, index=idx)
    assert not flow_is_fading(accel, 9, "bid", 2, 4)      # recent 100 > prior 15
    assert flow_is_fading(fading, 9, "bid", 2, 4)         # recent 10 < prior 150
    nosweep = pd.DataFrame({"bid": np.zeros(10), "ask": np.zeros(10)}, index=idx)
    assert not flow_is_fading(nosweep, 9, "bid", 2, 4)    # no sweep existed


def test_oi_drop_signature():
    idx = minutes(10)
    dropped = pd.Series([1000] * 8 + [1000, 997.0], index=idx)   # −0.3% ≥ 0.1%
    flat = pd.Series(1000.0, index=idx)
    assert oi_dropped(dropped, 9, 5, 0.001)
    assert not oi_dropped(flat, 9, 5, 0.001)


def _rich_short_prep(n=40, ask_path=None, sweep_price=6.4105):
    """basis rich → desired −1 from i=2 onward; one buy-taker sweep at t2+30s."""
    z = np.full(n, 3.0)
    z[:2] = 0.0                                   # enter at i=2
    bps = np.full(n, 15.0)
    return make_prep(n=n, bps=bps, z=z, ask_path=ask_path,
                     trades_rows=[("2026-07-20T00:02:30Z", sweep_price, 50, "bid")])


def test_deeper_post_only_fills_on_bigger_sweeps():
    base = dict(flow_filter=False, oi_confirm=False, queue_frac=0.0,
                min_abs_bps=10.0, entry_k=2.5, abort_bps=None)
    # sweep reaches touch (6.41) + 5 ticks = 6.4105
    at_touch = run_config(_rich_short_prep(), ImprovedParams(offset_ticks=0, **base))
    behind = run_config(_rich_short_prep(), ImprovedParams(offset_ticks=10, **base))
    assert at_touch["summary"]["fills"] == 1          # 6.4105 ≥ 6.4100 → filled
    assert behind["summary"]["fills"] == 0            # 6.4105 < 6.4110 → sweep too shallow


def test_abort_caps_loss_on_continuing_move():
    n = 40
    ask_path = np.full(n, 6.41)
    ask_path[4:] = 6.4200                             # adverse move after entry
    ask_path[32:] = 6.4400                            # keeps running
    bps = np.full(n, 15.0)
    bps[4:] = 45.0                                    # basis EXTENDS +30bps past entry
    z = np.full(n, 3.0)
    z[:2] = 0.0

    def prep():
        return make_prep(n=n, bps=bps, z=z, ask_path=ask_path,
                         trades_rows=[("2026-07-20T00:02:30Z", 6.4105, 50, "bid")])

    base = dict(flow_filter=False, oi_confirm=False, queue_frac=0.0,
                min_abs_bps=10.0, entry_k=2.5, offset_ticks=0)
    with_abort = run_config(prep(), ImprovedParams(abort_bps=15.0, **base))
    no_abort = run_config(prep(), ImprovedParams(abort_bps=None, **base))

    ta, tn = with_abort["trade_pnl"], no_abort["trade_pnl"]
    assert len(ta) == 1 and bool(ta.aborted.iloc[0])
    assert with_abort["summary"]["abort_frac"] == 1.0
    assert len(tn) == 1 and not bool(tn.aborted.iloc[0])
    # abort exits at 6.42 (crossed) vs no-abort rides to 6.44 → loss capped
    assert ta.net.iloc[0] < 0 and tn.net.iloc[0] < 0
    assert abs(ta.net.iloc[0]) < abs(tn.net.iloc[0])


def test_flow_filter_blocks_entry_in_run_config():
    prep = _rich_short_prep()
    # accelerating adverse flow at the entry minutes → no attempts at all
    prep.minute_vol["bid"] = np.linspace(0, 100, len(prep.frame))
    r = run_config(prep, ImprovedParams(flow_filter=True, oi_confirm=False,
                                        queue_frac=0.0, min_abs_bps=10.0,
                                        entry_k=2.5, offset_ticks=0, abort_bps=None))
    assert r["summary"]["fills"] == 0 and r["summary"]["flow_blocked"] > 0
