"""W8 regressions found by the independent forward-observation audit."""
from dataclasses import replace

import pytest

from crypto_trading.crypto_strategies.event_binary import complete_set as k

# These two regressions exercise cancel/replace mechanics at ~80s remaining.
# v2 widened the risk windows (stop_new 90->180, flatten 30->120) and turned
# maker fees on (0 -> 0.0175, which shifts the re-quoted hedge one tick down
# and behind the displayed queue). The mechanics under test are independent
# of both tunings, so pin the v1 constants they were written against.
V1_WINDOWS = dict(stop_new_before_s=90., flatten_before_s=30.,
                  maker_coefficient=0.)


def test_smaller_same_price_hedge_is_cancelled_before_replacement():
    p = replace(k.Parameters(), **V1_WINDOWS)
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

    # v8: a print through the quote decrements the displayed queue instead of
    # zeroing it, so the sweep must be big enough to clear the 10 contracts
    # shown at .48 before it reaches our 3. (A lone 5-lot print at .47 with no
    # .48 print means that level was cancelled, not traded - nobody is filled.)
    k.process_trades(m, [dict(trade_id="after-cancel", ticker="BTC-test", ts=922.,
                              yes_price=.47, quantity=15, taker_side="no")], p)
    assert old["remaining"] == 5
    assert k.net_quantity(m) == 0
    assert m["paired_quantity"] == 3


def test_smaller_hedge_still_counts_fills_during_cancel_latency():
    p = replace(k.Parameters(), **V1_WINDOWS)
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


def test_deep_print_does_not_teleport_past_the_displayed_queue():
    """v8: a print THROUGH the quote decrements the queue, it does not zero it.

    v1-v7 ran `order["queue_ahead"] = 0.0` on any through-print, which granted
    85.2% of all simulated maker volume: 762 of 893 fully-taped through-fills
    happened while cumulative at-or-better print volume was still below the
    size displayed ahead at posting. The live case this reproduces is
    KXETH15M-26SEP131400-00 order :3 - posted behind 786 displayed contracts,
    filled by a 1-lot print four ticks through.
    """
    p = replace(k.Parameters(), **V1_WINDOWS)
    m = k.new_market("BTC-test", "KXBTC15M", 1000., 100.)
    order = dict(id="q", side="yes", price=.64, quantity=5., remaining=5.,
                 queue_ahead=786., created_ts=900., activate_ts=900.5,
                 expires_ts=935.)
    m["orders"].append(order)
    k.process_trades(m, [dict(trade_id="deep", ticker="BTC-test", ts=905.,
                              yes_price=.60, quantity=1, taker_side="no")], p)
    assert k.net_quantity(m) == 0
    assert order["remaining"] == 5
    assert order["queue_ahead"] == 785.
    # A sweep genuinely large enough to clear the displayed size still fills.
    k.process_trades(m, [dict(trade_id="sweep", ticker="BTC-test", ts=906.,
                              yes_price=.60, quantity=800, taker_side="no")], p)
    assert k.net_quantity(m) == 5
    assert order["queue_ahead"] == 0.


def test_completion_switch_off_never_buys_the_second_leg():
    """v8 treatment arm: hold the favoured leg, never hedge it.

    Completing at <= 0.98 on Kalshi's single book is algebraically "buy the
    underdog at its ask"; measured at -3.9c/contract against simply holding
    (window-clustered t -5.75, 0/12 days positive).
    """
    p = replace(k.Parameters(), complete_sets=False)
    m = k.new_market("BTC-test", "KXBTC15M", 1000., 100.)
    k.add_fill(m, "yes", 3, .70, 400, 0, liquidity="maker_model", source="entry")
    b = k.normalize_book({"yes_dollars": [[.70, 10]], "no_dollars": [[.29, 10]]})
    # 480s left: inside the entry window, and NO is quotable as a completion
    # under the control rule - but the treatment arm must not quote it at all.
    created = k.update_quotes(m, b, 520., p, residual=True)
    assert all(o["side"] != "no" for o in created), created
    assert k.take_pair(m, b, 520., p, residual=True) == []
    assert m["paired_quantity"] == 0
    # Same state under the control rule DOES quote the completing side, so the
    # switch is what differs and not the market.
    c = k.new_market("BTC-test2", "KXBTC15M", 1000., 100.)
    k.add_fill(c, "yes", 3, .70, 400, 0, liquidity="maker_model", source="entry")
    control = k.update_quotes(c, b, 520., k.Parameters(), residual=True)
    assert any(o["side"] == "no" for o in control), control


def test_treatment_arm_takes_one_entry_per_market_even_if_the_favourite_flips():
    """v8: `complete_sets=False` was measured as ONE entry then hold.

    A mid-window flip used to open a second fresh leg on the other side, which
    FIFO then netted into a "pair" the arm is registered never to make.
    """
    p = replace(k.Parameters(), complete_sets=False, clip=1.)
    m = k.new_market("BTC-test", "KXBTC15M", 1000., 100.)
    # NO is the favourite and we are filled one clip inside the box.
    b_no = k.normalize_book({"yes_dollars": [[.29, 50]], "no_dollars": [[.70, 50]]})
    created = k.update_quotes(m, b_no, 520., p, residual=True)
    assert [o["side"] for o in created] == ["no"]
    k.add_fill(m, "no", 1., .70, 521., p.maker_coefficient,
               liquidity="maker_model", source="entry")
    # Now the market flips: YES is the favourite, still inside band and window.
    b_yes = k.normalize_book({"yes_dollars": [[.70, 50]], "no_dollars": [[.29, 50]]})
    assert k.update_quotes(m, b_yes, 540., p, residual=True) == []
    assert k.inventory(m, "no") == 1. and k.inventory(m, "yes") == 0.
    assert m["paired_quantity"] == 0.
    # The control arm is unchanged: it still quotes the completing side.
    c = k.new_market("BTC-test2", "KXBTC15M", 1000., 100.)
    k.add_fill(c, "no", 1., .70, 521., 0., liquidity="maker_model", source="entry")
    assert any(o["side"] == "yes"
               for o in k.update_quotes(c, b_yes, 540.,
                                        replace(k.Parameters(), clip=1.), residual=True))
