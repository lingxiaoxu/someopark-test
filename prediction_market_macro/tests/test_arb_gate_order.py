"""#157 — where §24-A's arb call sits in `decide_all.run`'s gate chain.

An arb is a statement that two of these QUOTES contradict each other. It needs no
prediction, so it must not die on a gate about predictions. Until 2026-08-20 it did:
the call sat inside the `else` branch, below `if pr is None: continue`, below the
§8.2-5 staleness gate (whose PRED_STALE_H half is purely about the model) and below
§27.4's information gate (entirely about the model's disadvantage on gasoline prints).
Measured over the ledger, 29% of detected violations were dropped by a gate that had
nothing to say about them.

The fix is an ordering change, and orderings are exactly what a unit test on behaviour
cannot see — every one of these arrangements produces the same output on a day with no
violations, which is almost every day. So this pins the ORDER, by source, plus the two
things that must NOT move with it:

  * the circuit breaker (铁律 10) stays ABOVE arb. "Tripped series open NOTHING new" is
    about capital and about our confidence in the whole series; an arb is still a new
    position. Hoisting arb above the breaker would trade while halted, which is the one
    ordering the rule exists to forbid.
  * quote staleness still applies to arb — just its own half. A "riskless" trade priced
    off a six-hour-old book is a bet that the book has not moved, which is the opposite
    of riskless.
"""
from __future__ import annotations

import inspect
import sqlite3

import pytest

from prediction_market_macro.ops import decide_all
from prediction_market_macro.strategy import arb


def _src() -> str:
    return inspect.getsource(decide_all.run)


def _pos(needle: str) -> int:
    src = _src()
    i = src.find(needle)
    assert i >= 0, f"anchor vanished from decide_all.run: {needle!r}"
    return i


def test_arb_runs_before_the_three_model_gates():
    """The whole of the change, stated as the inequality it is."""
    arb_call = _pos("n += arb.execute(")
    assert arb_call < _pos("if pr is None:\n                continue"), \
        "arb must not require a prediction to exist"
    assert arb_call < _pos("pred_age_h > PRED_STALE_H"), \
        "arb must not die on a STALE prediction either"
    assert arb_call < _pos("blind = _aaa_information_gate("), \
        "§27.4 is about the model's information, not about the book"


def test_the_circuit_breaker_still_outranks_arb():
    """铁律 10. The one gate that must stay above, and the reason the whole block was
    not simply hoisted to the top of the loop."""
    assert _pos("trip = risk.breaker_tripped(") < _pos("n += arb.execute("), \
        "hoisting arb above the breaker would trade while the series is halted"


def test_arb_still_answers_to_quote_staleness():
    """Its own half of §8.2-5, and the half that is actually about the book."""
    src = _src()
    blk = src[_pos("impl = None"):_pos("if pr is None:")]
    assert "quote_age_h is not None and quote_age_h <= QUOTE_STALE_H" in blk
    # ...and a stale-book drop is ALERTED, not silent: "the book is too old to trust"
    # and "there was no violation" are different facts, and this module spent months
    # unable to tell them apart (see test_arb_reject_alert.py for the same argument
    # applied to the fee gate).
    assert "ARB-STALE-BOOK" in blk


def test_the_devig_is_computed_once_and_reused():
    """Not a micro-optimisation. Recomputing `impl` in the model branch would re-derive
    the market pmf from a book read at a different instant than the one arb priced
    against, so §19-4's market fair and the arb trade would disagree about what the book
    said — on exactly the events where the book is moving fast enough for that to
    matter."""
    src = _src()
    assert src.count("devig.bucket_implied(legs)") == 1
    assert src.count("devig.ladder_implied(legs)") == 1
    assert _pos("devig.bucket_implied(legs)") < _pos("if pr is None:")


def test_a_breaker_trip_with_no_prediction_still_writes_nothing():
    """`pr` moved above the breaker so the arb leg could run without it, which put a
    `pr["model_version"]` dereference on a path where `pr` can now be None. The ledger
    row is a MODEL decision ("we would have looked and we passed"), so with no model
    there is nothing to record — the old code `continue`d one line earlier and wrote
    nothing, and this asserts the rearrangement kept that rather than crashing or
    inventing a decision with no model attached."""
    src = _src()
    blk = src[_pos("trip = risk.breaker_tripped("):_pos("# freshest quote on this book")]
    assert "if pr is not None:" in blk
    assert blk.index("if pr is not None:") < blk.index('pr["model_version"]')


# ── the stale-book alert, end to end ─────────────────────────────────────────

@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("CREATE TABLE alerts(ts TEXT, level TEXT, source TEXT,"
                    " message TEXT);")
    return c


def test_stale_book_alert_dedups_like_every_other_arb_alert(conn):
    """It reuses `arb._alert_once`, so a violation standing all day costs one row rather
    than one per tick. Pinned because the alert is written from a DIFFERENT module than
    the one that owns the dedup rule, and a copy of the rule would drift out of step."""
    msg = "ARB-STALE-BOOK KXCPIYOY/26SEP: 1 violation(s) detected but the book is old"
    arb._alert_once(conn, msg, "warn")
    arb._alert_once(conn, msg, "warn")
    rows = conn.execute("SELECT level, message FROM alerts WHERE source='arb'").fetchall()
    assert len(rows) == 1
    assert rows[0]["level"] == "warn"      # a droppable arb is worth more than 'info'
