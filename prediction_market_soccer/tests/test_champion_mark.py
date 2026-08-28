"""A season-champion mark must be a price someone could actually get.

The inherited order took `previous_price_dollars` — the previous CLOSE — ahead of
`last_price_dollars`, the actual last trade, and then reported it unbounded. A champion
contract is thin, so its last print can be days old: a club knocked out in the round of
16 kept showing the 5c someone paid while it was still alive, with a live book of
bid 0 / ask 1c. Clamping the trade into the book is what makes the mark executable, and
it is the elimination guard the board was missing.
"""
from __future__ import annotations

from prediction_market_soccer.venues.champion_prices import _champ_mark


def _mkt(**kw):
    base = {"status": "active"}
    base.update(kw)
    return base


def test_the_last_trade_beats_the_previous_close():
    m = _mkt(last_price_dollars=0.19, previous_price_dollars=0.18,
             yes_bid_dollars=0.16, yes_ask_dollars=0.19)
    assert _champ_mark(m) == 19.0


def test_a_collapsed_book_overrides_a_stale_print():
    """The elimination case: knocked out, last trade 5c, market now pays 1c."""
    m = _mkt(last_price_dollars=0.05, previous_price_dollars=0.05,
             yes_bid_dollars=0.00, yes_ask_dollars=0.01)
    assert _champ_mark(m) == 1.0


def test_a_trade_above_the_ask_is_clamped_to_the_ask():
    m = _mkt(last_price_dollars=0.20, previous_price_dollars=0.20,
             yes_bid_dollars=0.07, yes_ask_dollars=0.18)
    assert _champ_mark(m) == 18.0


def test_a_trade_below_the_bid_is_clamped_to_the_bid():
    m = _mkt(last_price_dollars=0.02, yes_bid_dollars=0.10, yes_ask_dollars=0.14)
    assert _champ_mark(m) == 10.0


def test_a_settled_market_is_read_from_its_result_not_its_book():
    assert _champ_mark(_mkt(status="settled", result="yes")) == 100.0
    assert _champ_mark(_mkt(status="settled", result="no")) == 0.0
    assert _champ_mark(_mkt(status="finalized", result="yes")) == 100.0


def test_the_dollar_placeholder_ask_is_not_treated_as_a_book():
    """ask = $1.00 is Kalshi's "no offer", not a price — it must not clamp anything."""
    m = _mkt(last_price_dollars=0.30, yes_bid_dollars=0.25, yes_ask_dollars=1.00)
    assert _champ_mark(m) == 30.0


def test_no_trade_and_no_book_yields_nothing_rather_than_a_guess():
    assert _champ_mark(_mkt()) is None
