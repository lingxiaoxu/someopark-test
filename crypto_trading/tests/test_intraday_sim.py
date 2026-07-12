"""IntradaySim tests — synthetic tapes, no network. Fee scenario 'zero' unless
the test is about fees, so P&L arithmetic is exact."""
import pandas as pd

from crypto_trading.crypto_common.backtest.intraday_sim import (IntradaySim, SimConfig,
                                                                SimOrder)


def book(ts, bid, ask, sz=100):
    return {"type": "book", "ts": ts, "bids": [[bid, sz]], "asks": [[ask, sz]]}


def test_ioc_round_trip_pnl_exact():
    tape = [book(1, 6.38, 6.39), book(2, 6.48, 6.49), book(3, 6.48, 6.49)]

    def strat(ev, sim):
        if ev["ts"] == 1:
            return [SimOrder("buy", 10)]        # fill 10 @ 6.39 (walk ask)
        if ev["ts"] == 2:
            return [SimOrder("sell", 10)]       # fill 10 @ 6.48 (walk bid)
        return []

    res = IntradaySim(SimConfig(fee_scenario="zero")).run(tape, strat)
    s = res.summary()
    assert abs(s["realized_pnl"] - 0.9) < 1e-9          # (6.48-6.39)*10
    assert s["end_position"] == 0 and s["n_fills"] == 2
    assert abs(s["net_pnl"] - 0.9) < 1e-9


def test_ioc_partial_fill_when_depth_exhausted():
    tape = [{"type": "book", "ts": 1, "bids": [[6.38, 100]], "asks": [[6.39, 3]]}]
    res = IntradaySim(SimConfig(fee_scenario="zero", force_flat_at_end=False)).run(
        tape, lambda ev, sim: [SimOrder("buy", 10)] if ev["ts"] == 1 else [])
    assert res.state.position == 3                       # only 3 available


def test_post_only_fills_when_crossed_at_limit_price():
    tape = [book(1, 6.38, 6.39), book(2, 6.35, 6.36), book(3, 6.35, 6.36)]

    def strat(ev, sim):
        if ev["ts"] == 1:
            return [SimOrder("buy", 5, order_type="post_only", limit_price=6.37)]
        return []

    res = IntradaySim(SimConfig(fee_scenario="zero", force_flat_at_end=False)).run(tape, strat)
    fills = res.state.fills
    assert len(fills) == 1 and fills[0].role == "maker"
    assert fills[0].price == 6.37                        # our limit, not the crossed ask
    assert res.state.position == 5


def test_post_only_does_not_fill_without_cross():
    tape = [book(1, 6.38, 6.39), book(2, 6.38, 6.39)]
    res = IntradaySim(SimConfig(fee_scenario="zero", force_flat_at_end=False)).run(
        tape, lambda ev, sim: [SimOrder("buy", 5, order_type="post_only",
                                        limit_price=6.37)] if ev["ts"] == 1 else [])
    assert not res.state.fills and res.state.resting


def test_funding_event_pays_short_receives():
    tape = [book(1, 6.38, 6.40),
            {"type": "funding", "ts": 2, "rate": 1e-3},
            book(3, 6.38, 6.40)]

    def strat(ev, sim):
        if ev["ts"] == 1:
            return [SimOrder("sell", 10)]                # short 10 @ 6.38
        return []

    res = IntradaySim(SimConfig(fee_scenario="zero", force_flat_at_end=False)).run(tape, strat)
    # short receives rate × pos × mid = 1e-3 × 10 × 6.39
    assert abs(res.state.funding_pnl - 0.0639) < 1e-9


def test_fees_projected_reduce_equity():
    tape = [book(1, 6.38, 6.39), book(2, 6.38, 6.39)]
    res = IntradaySim(SimConfig(fee_scenario="projected", force_flat_at_end=False)).run(
        tape, lambda ev, sim: [SimOrder("buy", 10)] if ev["ts"] == 1 else [])
    assert res.state.fees_paid > 0


def test_flip_through_flat_sets_new_avg_price():
    tape = [book(1, 6.38, 6.39), book(2, 6.40, 6.41), book(3, 6.40, 6.41)]

    def strat(ev, sim):
        if ev["ts"] == 1:
            return [SimOrder("buy", 5)]                  # long 5 @ 6.39
        if ev["ts"] == 2:
            return [SimOrder("sell", 12)]                # flip to short 7 @ 6.40
        return []

    res = IntradaySim(SimConfig(fee_scenario="zero", force_flat_at_end=False)).run(tape, strat)
    s = res.state
    assert s.position == -7 and abs(s.avg_price - 6.40) < 1e-9
    assert abs(s.realized_pnl - (6.40 - 6.39) * 5) < 1e-9


def test_equity_series_and_daily_returns_shape():
    tape = [book(t, 6.38, 6.39) for t in range(1, 20)]
    res = IntradaySim(SimConfig(fee_scenario="zero")).run(tape, lambda ev, sim: [])
    assert isinstance(res.equity, pd.Series) and len(res.equity) > 0
    assert res.daily_returns.empty or isinstance(res.daily_returns, pd.Series)
