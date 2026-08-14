"""Plan 02 perp-leg strategy tests — synthetic gap frames + tapes, no network."""
import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_strategies.event_perp.strategy import (EventPerpParams,
                                                                  _backtest_loop)
from crypto_trading.crypto_strategies.event_perp.signals.dislocation import rolling_z


def mk_gap_frame(gap_z_path, start="2026-07-20 00:00", step_s=90, horizon="H1"):
    idx = pd.to_datetime(pd.date_range(start, periods=len(gap_z_path), freq=f"{step_s}s",
                                       tz="UTC"))
    return pd.DataFrame({"gap_z": gap_z_path, "close_time": horizon,
                         "gap": np.asarray(gap_z_path) * 1e-3,
                         "implied_mean": 64000.0, "perp_spot": 63900.0},
                        index=idx)


def mk_touch(idx, bid=6.40, ask=6.41):
    t = pd.DataFrame({"bid": bid, "ask": ask},
                     index=pd.date_range(idx[0] - pd.Timedelta(minutes=2),
                                         idx[-1] + pd.Timedelta(hours=3), freq="1min",
                                         tz="UTC"))
    return t


def mk_tape(idx, price=6.40, side="ask", every_s=30, count=50.0):
    """Dense tape so maker fills clear the queue; taker_side per fill direction."""
    ts = pd.date_range(idx[0] - pd.Timedelta(minutes=1),
                       idx[-1] + pd.Timedelta(hours=3), freq=f"{every_s}s", tz="UTC")
    return pd.DataFrame({"price": price, "count": count, "taker_side": side}, index=ts)


P = EventPerpParams(entry_k=1.5, exit_k=0.5, max_hold_min=60, contracts=10,
                    queue_frac=0.0)   # queue 0 → fills on first crossing print


def test_entry_exit_state_machine_and_positive_converging_trade():
    # z spikes to +2 (perp cheap → LONG), then decays inside ±0.5 → exit
    path = [0.0, 0.0, 2.2, 2.0, 1.0, 0.3, 0.0]
    g = mk_gap_frame(path)
    touch = mk_touch(g.index)
    # sell prints at 6.40 fill our long entry bid; buy prints at 6.41 fill the exit ask
    tape = pd.concat([mk_tape(g.index, 6.40, "ask"), mk_tape(g.index, 6.41, "bid")]).sort_index()
    r = _backtest_loop(g, touch, tape, P, ticker="KXBTCPERP", fee_scenario="zero")
    s = r["summary"]
    assert s["round_trips"] == 1 and s["entries_filled"] == 1
    tr = r["trade_pnl"].iloc[0]
    assert tr.sign == 1                                   # +z → long
    assert tr.entry_px == 6.40 and tr.exit_px == 6.41     # maker both ways
    assert tr.net == pytest.approx(0.01 * 10)             # (6.41-6.40)×10, zero fees
    assert s["hit_rate"] == 1.0


def test_short_side_on_negative_z():
    path = [0.0, -2.0, -1.8, -0.2, 0.0]
    g = mk_gap_frame(path)
    touch = mk_touch(g.index)
    tape = pd.concat([mk_tape(g.index, 6.41, "bid"), mk_tape(g.index, 6.40, "ask")]).sort_index()
    r = _backtest_loop(g, touch, tape, P, ticker="KXBTCPERP", fee_scenario="zero")
    tp = r["trade_pnl"]
    assert len(tp) == 1 and tp.iloc[0].sign == -1         # −z → short at the ask


def test_max_hold_forces_exit_and_taker_crossing_pays_spread():
    # z never decays; tape has NO opposite prints near the passive exit → cross
    path = [2.5] * 3 + [2.4] * 50                          # stays elevated
    g = mk_gap_frame(path)
    touch = mk_touch(g.index)
    tape = mk_tape(g.index, 6.40, "ask")                   # only sell prints (fill entry)
    p = EventPerpParams(entry_k=1.5, exit_k=0.5, max_hold_min=10, contracts=10,
                        queue_frac=0.0, exit_timeout_min=1)
    r = _backtest_loop(g, touch, tape, p, ticker="KXBTCPERP", fee_scenario="zero")
    tp = r["trade_pnl"]
    assert len(tp) == 1
    tr = tp.iloc[0]
    assert tr.exit_role == "taker"
    assert tr.exit_px == 6.40                              # long crossed out at the BID
    hold = (tr.exit_ts - tr.entry_ts).total_seconds() / 60
    assert hold <= 12                                      # max_hold enforced (~10min)


def test_fee_accounting_maker_vs_taker():
    path = [0.0, 2.2, 0.1, 0.0]
    g = mk_gap_frame(path)
    touch = mk_touch(g.index)
    tape = pd.concat([mk_tape(g.index, 6.40, "ask"), mk_tape(g.index, 6.41, "bid")]).sort_index()
    r = _backtest_loop(g, touch, tape, P, ticker="KXBTCPERP", fee_scenario="projected")
    tr = r["trade_pnl"].iloc[0]
    # maker entry+exit at real rates: 5bps × notional each way
    expected = 0.0005 * 6.40 * 10 + 0.0005 * 6.41 * 10
    assert tr.fee == pytest.approx(expected, rel=1e-6)
    assert tr.net == pytest.approx(tr.gross - tr.fee)


def test_within_horizon_z_no_cross_horizon_leakage():
    # two horizons with wildly different gap LEVELS; within each the gap is
    # constant → z ≈ 0/NaN. Any cross-horizon pooling would fabricate |z| >> 0.
    n = 80
    ts = pd.date_range("2026-07-20", periods=2 * n, freq="45s", tz="UTC")
    close = ["A", "B"] * n                                # interleaved snapshots
    gap = [0.001 if c == "A" else 0.02 for c in close]    # 10bps vs 200bps levels
    df = pd.DataFrame({"gap": gap, "close_time": close}, index=ts)
    z = df.groupby("close_time", sort=False)["gap"].transform(
        lambda s: rolling_z(s, 60))
    finite = z.dropna()
    assert (finite.abs() < 1e-9).all(), "constant-within-horizon gap must give z≈0"


def test_entry_missed_when_no_crossing_prints():
    # price runs away: no sell prints at/below our bid → entry never fills
    path = [0.0, 2.5, 2.5, 0.0]
    g = mk_gap_frame(path)
    touch = mk_touch(g.index)
    tape = mk_tape(g.index, 6.45, "bid")                   # only buys, above our bid
    r = _backtest_loop(g, touch, tape, P, ticker="KXBTCPERP", fee_scenario="zero")
    s = r["summary"]
    assert s["entries_attempted"] >= 1 and s["entries_filled"] == 0
    assert s["round_trips"] == 0
