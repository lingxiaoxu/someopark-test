"""Plan 01 strategy-glue tests (tape construction + target-position logic) —
synthetic, no network, no parquet stores."""
import numpy as np
import pandas as pd

from crypto_trading.crypto_common.backtest.intraday_sim import IntradaySim, SimConfig, SimOrder
from crypto_trading.crypto_strategies.basis_meanrev.strategy import frame_to_tape


def synth_frame(n=60, mark=6.30, index=62_900.0):
    idx = pd.date_range("2026-07-06 00:00", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame({
        "mark_mid_contract": np.full(n, mark),
        "mark_mid_underlying": np.full(n, mark / 1e-4),
        "index_proxy": np.full(n, index), "index_venues": 3,
        "b_t": 0.0, "b_t_bps": 0.0, "desired": 0.0}, index=idx)


def test_frame_to_tape_orders_and_funding_pit():
    frame = synth_frame(60)
    fidx = pd.DatetimeIndex([pd.Timestamp("2026-07-06 00:30", tz="UTC")])
    funding = pd.DataFrame({"funding_rate": [1e-4], "mark_price": [6.30]}, index=fidx)
    tape = frame_to_tape(frame, funding, 1e-4)
    assert len(tape) == 61
    ts_sorted = [e["ts"] for e in tape]
    assert ts_sorted == sorted(ts_sorted)
    fund_events = [e for e in tape if e["type"] == "funding"]
    assert len(fund_events) == 1
    # funding sorts BEFORE the same-timestamp book event (settles on the mark)
    i = tape.index(fund_events[0])
    assert tape[i + 1]["type"] == "book" and tape[i + 1]["ts"] == fund_events[0]["ts"]


def test_funding_outside_frame_window_excluded():
    frame = synth_frame(60)
    fidx = pd.DatetimeIndex([pd.Timestamp("2026-07-08 04:00", tz="UTC")])
    funding = pd.DataFrame({"funding_rate": [1e-4], "mark_price": [6.3]}, index=fidx)
    tape = frame_to_tape(frame, funding, 1e-4)
    assert not [e for e in tape if e["type"] == "funding"]


def test_target_position_diff_logic_round_trips():
    """The strategy trades TO a target — replaying a desired path must end flat."""
    frame = synth_frame(6)
    desired_path = [0, -1, -1, 0, 1, 0]
    frame["desired"] = desired_path
    tape = frame_to_tape(frame, None, 1e-4)
    desired_by_ts = {dt.timestamp(): d for dt, d in frame.desired.items()}

    def strat(ev, sim):
        want = desired_by_ts.get(ev["ts"])
        if ev["type"] != "book" or want is None:
            return []
        diff = want * 10 - sim.state.position
        if diff == 0:
            return []
        return [SimOrder("buy" if diff > 0 else "sell", abs(diff))]

    res = IntradaySim(SimConfig(fee_scenario="zero")).run(tape, strat)
    assert res.state.position == 0
    # -10, back to 0, +10, back to 0 → 4 position changes = 4 fills
    assert len(res.state.fills) == 4
