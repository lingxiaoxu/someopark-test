"""Causality and coverage safety for the independent paper-only feature helper."""
import copy
import math
import pytest
from crypto_trading.crypto_strategies.downside_paper.features import compute_features


def fixture():
    contexts = [{"recv_ts": float(t), "mid_px": str(100 + (t - 700) / 1000)}
                for t in range(680, 1001, 5)]
    books = [{"recv_ts": 999., "venue_ts": 998000,
              "bids": [["100", "10", 1]], "asks": [["100.1", "9", 1]]}]
    trades = [{"recv_ts": float(t), "venue_ts": (t - 1) * 1000,
               "tid": i, "px": "100", "sz": "2", "side": "B"}
              for i, t in enumerate([985, 990, 995])]
    return contexts, books, trades


def test_valid_inputs_distinguish_price_and_flow_quality():
    result = compute_features(*fixture(), 1000)
    assert result["valid"] and result["flow_valid"]
    assert result["observed_notional_1m"] == 600
    assert result["observed_flow_imbalance_1m"] == 1
    assert result["valid_nonempty_batch_count_1m"] == 3
    assert result["trade_poll_complete"] is False


def test_future_received_rows_cannot_change_features():
    c, b, t = fixture()
    before = compute_features(c, b, t, 1000)
    c.append({"recv_ts": 1001., "mid_px": "900000"})
    b.append(dict(b[0], recv_ts=1001., venue_ts=1001000))
    t.append(dict(t[0], recv_ts=1001., tid=99, sz="999999"))
    assert compute_features(c, b, t, 1000) == before


@pytest.mark.parametrize("venue_ts,error", [
    (970000, "book_venue_age_over15s"),
    (1001000, "book_venue_timestamp_in_future"),
    (None, "book_venue_timestamp_missing"),
])
def test_fresh_receipt_does_not_hide_bad_book_venue_time(venue_ts, error):
    c, b, t = fixture()
    b[0]["venue_ts"] = venue_ts
    result = compute_features(c, b, t, 1000)
    assert not result["valid"] and error in result["errors"]


def test_empty_or_old_prints_are_not_proven_zero_volume():
    c, b, t = fixture()
    result = compute_features(c, b, [], 1000)
    assert result["valid"] and not result["flow_valid"]
    assert result["trade_poll_complete"] is False
    for r in t:
        r["recv_ts"] -= 30
        r["venue_ts"] -= 30000
    assert not compute_features(c, b, t, 1000)["flow_valid"]


def test_three_rows_from_one_batch_do_not_pass_activity_gate():
    c, b, t = fixture()
    for r in t:
        r["recv_ts"] = 995.
    result = compute_features(c, b, t, 1000)
    assert result["valid_nonempty_batch_count_1m"] == 1
    assert not result["flow_valid"]


def test_duplicates_do_not_inflate_observed_flow():
    c, b, t = fixture()
    result = compute_features(c, b, t + copy.deepcopy(t), 1000)
    assert result["observed_notional_1m"] == 600
    assert result["observed_print_count_1m"] == 3


def test_missing_ids_use_hashes_and_anonymous_rows_fail_flow_closed():
    c, b, t = fixture()
    for i, r in enumerate(t):
        r["tid"] = None
        r["hash"] = "0x" + str(i + 1)
    result = compute_features(c, b, t, 1000)
    assert result["observed_print_count_1m"] == 3 and result["flow_valid"]
    for r in t:
        r["hash"] = "0x0000"
    result = compute_features(c, b, t, 1000)
    assert result["observed_print_count_1m"] == 3
    assert not result["flow_valid"]
    assert result["ambiguous_trade_identity_count_1m"] == 3


@pytest.mark.parametrize("bad", ["nan", "inf", "-1"])
def test_nonfinite_or_negative_market_prices_rejected(bad):
    c, b, t = fixture()
    c[-1]["mid_px"] = bad
    result = compute_features(c, b, t, 1000)
    assert not result["valid"]


def test_invalid_or_future_prints_cannot_pollute_signed_flow():
    c, b, t = fixture()
    t.extend([
        {"recv_ts": 996., "venue_ts": 996000, "tid": 40, "px": "nan", "sz": "2", "side": "A"},
        {"recv_ts": 997., "venue_ts": 999000, "tid": 41, "px": "100", "sz": "2", "side": "A"},
    ])
    result = compute_features(c, b, t, 1000)
    assert result["flow_valid"]
    assert result["observed_notional_1m"] == 600
    assert result["discarded_stale_or_invalid_print_count_1m"] == 2


def test_context_gap_not_filled_through():
    c, b, t = fixture()
    c = [r for r in c if not 750 <= r["recv_ts"] <= 780]
    result = compute_features(c, b, t, 1000)
    assert not result["valid"] and "context_gap_over15s" in result["errors"]


def test_24h_volume_field_has_no_influence_and_inputs_unchanged():
    c, b, t = fixture()
    before = copy.deepcopy((c, b, t))
    baseline = compute_features(c, b, t, 1000)
    assert (c, b, t) == before
    for i, r in enumerate(c):
        r["day_ntl_vlm"] = str(i * 999999999)
    assert compute_features(c, b, t, 1000) == baseline
