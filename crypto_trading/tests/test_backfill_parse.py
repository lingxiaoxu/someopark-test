"""Candle parsing + sentinel cleaning tests — no network."""
from crypto_trading.crypto_common.kalshi.backfill import _clean_price, parse_candle


def test_clean_price_normal_and_sentinels():
    assert _clean_price("6.3819") == 6.3819
    assert _clean_price("0.0000") is None            # empty-book zero
    assert _clean_price("922337203685477.5807") is None   # int64-max sentinel
    assert _clean_price(None) is None
    assert _clean_price("not-a-number") is None


def test_parse_candle_normal():
    row = parse_candle({
        "end_period_ts": 1783296000,
        "price": {"open": "6.37", "high": "6.40", "low": "6.35", "close": "6.38"},
        "bid": {"open": "6.36", "high": "6.39", "low": "6.34", "close": "6.37"},
        "ask": {"open": "6.38", "high": "6.41", "low": "6.36", "close": "6.39"},
        "open_interest": "708446.00",
        "open_interest_notional_value_dollars": "4505645.7154",
        "volume": "1234.00",
    })
    assert row["ts"] == 1783296000
    assert row["price_close"] == 6.38 and row["ask_high"] == 6.41
    assert row["oi"] == 708446.0 and row["volume"] == 1234.0
    assert row["had_sentinel"] is False


def test_parse_candle_launch_day_sentinels_flagged():
    row = parse_candle({
        "end_period_ts": 1780545600,
        "ask": {"open": "6.67", "high": "922337203685477.5807", "low": "6.13", "close": "6.42"},
        "bid": {"open": "0.0000", "high": "6.68", "low": "0.0000", "close": "6.42"},
        "price": {"open": "6.67", "high": "6.68", "low": "6.13", "close": "6.42"},
    })
    assert row["had_sentinel"] is True
    assert row["ask_high"] is None and row["bid_open"] is None
    assert row["price_close"] == 6.42          # good fields survive


def test_parse_candle_missing_ts_dropped():
    assert parse_candle({"price": {"close": "6.38"}}) is None
