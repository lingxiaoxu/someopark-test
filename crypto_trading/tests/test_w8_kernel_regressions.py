"""W8 regressions found by the independent forward-observation audit."""
import pytest

from crypto_trading.crypto_strategies.event_binary import complete_set as k


def test_smaller_same_price_hedge_is_cancelled_before_replacement():
    p = k.Parameters()
    m = k.new_market("BTC-test", "KXBTC15M", 1000., 100.)
    k.add_fill(m, "no", 3, .50, 910, 0, liquidity="maker_model", source="first")
    old = dict(id="old-hedge", side="yes", price=.48, quantity=5., remaining=5.,
               queue_ahead=0., created_ts=915., activate_ts=915.5, expires_ts=935.)
    m["orders"].append(old)
    m["trade_watermark_ts"] = 920.
    b = k.normalize_book({"yes_dollars": [[.48, 10]], "no_dollars": [[.50, 10]]})

    # At 80 seconds remaining only the existing NO 3 may be hedged. The price
    # remains .48, so a price-only replace check incorrectly kept YES 5 alive.
    assert k.update_quotes(m, b, 920., p, residual=False) == []
    assert m["stop_new"]
    assert old["cancel_ts"] == 920.5
    assert k.update_quotes(m, b, 921., p, residual=False) == []  # unread cancel risk
    m["trade_watermark_ts"] = 921.
    created = k.update_quotes(m, b, 921., p, residual=False)
    assert len(created) == 1
    assert created[0]["side"] == "yes" and created[0]["quantity"] == 3

    k.process_trades(m, [dict(trade_id="after-cancel", ticker="BTC-test", ts=922.,
                              yes_price=.47, quantity=5, taker_side="no")], p)
    assert old["remaining"] == 5
    assert k.net_quantity(m) == 0
    assert m["paired_quantity"] == 3


def test_smaller_hedge_still_counts_fills_during_cancel_latency():
    p = k.Parameters()
    m = k.new_market("BTC-test", "KXBTC15M", 1000., 100.)
    k.add_fill(m, "no", 3, .50, 910, 0, liquidity="maker_model", source="first")
    m["orders"].append(dict(id="old", side="yes", price=.48, quantity=5., remaining=5.,
                            queue_ahead=0., created_ts=915., activate_ts=915.5, expires_ts=935.))
    b = k.normalize_book({"yes_dollars": [[.48, 10]], "no_dollars": [[.50, 10]]})
    m["trade_watermark_ts"] = 920.
    k.update_quotes(m, b, 920., p, residual=False)
    k.process_trades(m, [dict(trade_id="cancel-race", ticker="BTC-test", ts=920.25,
                              yes_price=.48, quantity=1, taker_side="no")], p)
    assert k.net_quantity(m) == -2
    m["trade_watermark_ts"] = 921.
    created = k.update_quotes(m, b, 921., p, residual=False)
    assert len(created) == 1 and created[0]["quantity"] == 2


@pytest.mark.parametrize("held", ["yes", "no"])
def test_one_sided_bid_book_closes_only_executable_residual(held):
    p = k.Parameters()
    other = "no" if held == "yes" else "yes"
    raw = {held+"_dollars": [[.60, 2.5]], other+"_dollars": []}
    assert k.normalize_book(raw) is None  # existing entry contract unchanged
    b = k.normalize_book(raw, allow_one_sided=True)
    assert b["two_sided"] is False
    assert b[held+"_bid"] == .60 and b[other+"_ask"] == .40
    assert b[other+"_bid"] is None and b[held+"_ask"] is None
    m = k.new_market("BTC-test", "KXBTC15M", 1000., 100.)
    k.add_fill(m, held, 5, .50, 100., 0, liquidity="maker_model", source="entry")
    mark = k.liquidation_value(m, b, p)
    assert mark["shortfall"] == 2.5
    fills = k.flatten(m, b, 980., p)
    assert sum(f["quantity"] for f in fills) == 2.5
    assert k.inventory(m, held) == 2.5
    assert all(f["side"] == other and f["price"] == .40 for f in fills)
    assert m["fees_usd"] == k.fee_usd(.40, 2.5, p.taker_coefficient)
    assert k.flatten(m, k.normalize_book({other+"_dollars": [[.30, 10]]},
                                        allow_one_sided=True), 981., p) == []


def test_one_sided_books_never_feed_entry_or_direction_models():
    b = k.normalize_book({"yes_dollars": [[.60, 5]]}, allow_one_sided=True)
    m = k.new_market("BTC-test", "KXBTC15M", 1000., 100.)
    with pytest.raises(ValueError, match="two-sided book"):
        k.update_quotes(m, b, 200., k.Parameters())
    with pytest.raises(ValueError, match="two-sided book"):
        k.take_pair(m, b, 200., k.Parameters())
    assert not m["orders"] and not m["fills"]
    assert k.normalize_book({}, allow_one_sided=True) is None
    assert k.normalize_book({"yes_dollars": [[.6, 5]], "no_dollars": [[.4, 5]]},
                            allow_one_sided=True) is None  # still reject locked
