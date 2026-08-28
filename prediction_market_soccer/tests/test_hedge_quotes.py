"""The hedge desk must be handed the whole book, not one leg of it.

`_match_hedge` solves six hedge shapes but built `Quotes(draw_ask=…)` alone, leaving
both other asks and all three bids None. Partial cash-out needs the held side's bid and
dutching needs all three asks, so those two reported "not available with the quotes on
hand" on every match ever exported — a structural gap wearing the costume of a quiet
market. These tests pin the difference.
"""
from __future__ import annotations

from prediction_market_soccer.ops.inplay_export import _hedge_alternatives
from prediction_market_soccer.strategy import inplay_hedge as ih

_POS = ih.Position(shares=10.0, entry_c=45.0, side="home")


def _full_book():
    return ih.Quotes(draw_ask=28.0, home_ask=55.0, away_ask=22.0,
                     home_bid=53.0, draw_bid=26.0, away_bid=20.0, minute=60, score="1-0")


def test_full_book_prices_every_shape():
    out = _hedge_alternatives(ih, _POS, _full_book(), "home")
    for shape in ("maximin", "delta_neutral", "draw_protection",
                  "partial_cashout_half", "dutch_lock", "lay"):
        assert shape in out, f"{shape} did not price against a complete book"
    assert "_unavailable" not in out


def test_draw_only_book_cannot_cash_out_or_dutch():
    """The old shape, kept as a regression witness: these two are the ones that were
    silently dead, and they must still degrade gracefully rather than raise."""
    out = _hedge_alternatives(ih, _POS, ih.Quotes(draw_ask=28.0, minute=60, score="1-0"), "home")
    assert "partial_cashout_half" not in out and "dutch_lock" not in out
    assert set(out["_unavailable"]) == {"partial_cashout_half", "dutch_lock"}
    # …while the shapes that only need the draw leg keep working.
    assert {"maximin", "delta_neutral", "draw_protection", "lay"} <= set(out)


def test_cash_out_realises_against_the_bid_not_the_ask():
    out = _hedge_alternatives(ih, _POS, _full_book(), "home")
    plan = out["partial_cashout_half"]
    # Half of 10 shares sold into a 53¢ bid = 265¢ realised, not 275¢ at the 55¢ ask.
    assert abs(plan["realised_c"] - 265.0) < 1e-6


def test_dutch_lock_reports_an_untradable_basket_rather_than_hiding_it():
    """55 + 28 + 22 = 105¢ for a 100¢ payout — a real book, and correctly NOT an arb."""
    out = _hedge_alternatives(ih, _POS, _full_book(), "home")
    assert out["dutch_lock"]["tradable"] is False


def test_no_nonfinite_value_reaches_the_payload():
    """A bare NaN is not valid JSON; one unpriceable solver must not blank the card."""
    from prediction_market_soccer.ops.inplay_export import _has_nonfinite
    for q in (_full_book(), ih.Quotes(draw_ask=28.0, minute=60, score="1-0")):
        assert not _has_nonfinite(_hedge_alternatives(ih, _POS, q, "home"))
