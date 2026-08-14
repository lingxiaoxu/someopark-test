"""Fill model tests — synthetic trade tapes, no network."""
import pandas as pd

from crypto_trading.crypto_common.backtest.fill_model import (simulate_maker_fill,
                                                              simulate_taker_fill)


def tape(rows):
    idx = pd.to_datetime([r[0] for r in rows], utc=True)
    return pd.DataFrame({"price": [r[1] for r in rows], "count": [r[2] for r in rows],
                         "taker_side": [r[3] for r in rows]}, index=idx)


T0 = "2026-07-20T00:00:00Z"


def test_maker_buy_fills_on_sell_through_price():
    # buy limit at 6.40 fills when a SELL-initiated trade prints ≤ 6.40
    t = tape([("2026-07-20T00:00:05Z", 6.41, 5, "bid"),   # buy-taker, above → no
             ("2026-07-20T00:00:10Z", 6.40, 5, "ask")])   # sell-taker at 6.40 → fill
    fr = simulate_maker_fill(6.40, "bid", pd.Timestamp(T0), t,
                             timeout=pd.Timedelta(minutes=1), queue_ahead=0)
    assert fr.filled and fr.fill_price == 6.40 and fr.reason == "filled"


def test_maker_not_filled_if_price_runs_away():
    # buy limit at 6.40 but market only trades UP → never filled (missed trade)
    t = tape([("2026-07-20T00:00:05Z", 6.45, 5, "bid"),
             ("2026-07-20T00:00:10Z", 6.50, 5, "bid")])
    fr = simulate_maker_fill(6.40, "bid", pd.Timestamp(T0), t,
                             timeout=pd.Timedelta(minutes=1), queue_ahead=0)
    assert not fr.filled and fr.reason == "timeout_unfilled"


def test_queue_ahead_delays_fill():
    # 3 units trade through, queue_ahead=5 → not enough volume to clear our queue
    t = tape([("2026-07-20T00:00:05Z", 6.40, 3, "ask")])
    fr = simulate_maker_fill(6.40, "bid", pd.Timestamp(T0), t,
                             timeout=pd.Timedelta(minutes=1), queue_ahead=5)
    assert not fr.filled
    # queue_ahead=2 → the 3 units clear it → fill
    fr2 = simulate_maker_fill(6.40, "bid", pd.Timestamp(T0), t,
                              timeout=pd.Timedelta(minutes=1), queue_ahead=2)
    assert fr2.filled


def test_maker_sell_fills_on_buy_through_price():
    t = tape([("2026-07-20T00:00:05Z", 6.60, 5, "bid")])   # buy-taker at 6.60 ≥ 6.55
    fr = simulate_maker_fill(6.55, "ask", pd.Timestamp(T0), t,
                             timeout=pd.Timedelta(minutes=1), queue_ahead=0)
    assert fr.filled and fr.fill_price == 6.55


def test_empty_window_no_trades():
    t = tape([("2026-07-20T01:00:00Z", 6.40, 5, "ask")])   # outside timeout
    fr = simulate_maker_fill(6.40, "bid", pd.Timestamp(T0), t,
                             timeout=pd.Timedelta(minutes=1), queue_ahead=0)
    assert not fr.filled and fr.reason == "no_trades"


def test_taker_fill_is_immediate():
    fr = simulate_taker_fill(6.50, "ask", pd.Timestamp(T0))
    assert fr.filled and fr.fill_price == 6.50 and fr.reason == "crossed"
