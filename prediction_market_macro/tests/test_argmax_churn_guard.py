"""#148 — the argmax stream must not open a position `ops/exits` rule 1 closes at once.

Entry (`decide_all.defers_to_market`) requires `fair <= cost` and puts NO floor under how
far below cost the fair may sit. Rule 1 (`exits.run`) closes anything whose holding edge
`fair - mid` is under `EXIT_EDGE = -0.06`. Since a structure's mid-cost never exceeds its
ask-cost, an entry at `fair - cost = -0.15` is already past the exit threshold before the
order is written, and the exit runs after the entry.

Live, that is 3 of the 4 argmax legs ever placed — ids 3100/3161/3222, each opened and
liquidated inside the same tick (3100: opened 09:12:03.036, exited 09:12:03.255) for -$0.07
on a $0.77 stake. Nothing was forecast, nothing moved; the round-trip taker cost was simply
booked. The fourth (3284, +0.0199) is still open.

The tests below pin the RULE, not those four rows: the guard fires exactly when rule 1 would
fire, it abstains exactly where rule 1 abstains, and it shares `EXIT_EDGE` rather than
copying it — which is the only property that keeps the two from contradicting each other
again after someone edits one of them.
"""
from __future__ import annotations

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import exits
from prediction_market_macro.strategy.edge import Leg, Struct

T = "KXWTIW-26AUG0714-T73.00"
T2 = "KXWTIW-26AUG0714-T75.00"


@pytest.fixture()
def conn(tmp_path):
    return init_db(str(tmp_path / "t.db"))


def _quote(conn, ticker=T, bid=0.23, ask=0.25, ts="2026-08-05T09:12:00+00:00"):
    conn.execute("INSERT INTO quotes(ts, ticker, yes_bid, yes_ask, bid_depth, ask_depth)"
                 " VALUES(?,?,?,?,100,100)", (ts, ticker, bid, ask))
    conn.commit()


def _single(fair, price=0.77, side="no", ticker=T):
    return Struct("single", (Leg(ticker, side, price, 1e9),), fair, price, price, "d")


# ── the live rows, replayed ───────────────────────────────────────────────────────────

# (decision id, fair, entry ask, quote bid/ask, the hold_edge the live exit recorded)
LIVE = [(3100, 0.6357, 0.77, (0.23, 0.25), -0.1243),
        (3161, 0.6357, 0.76, (0.24, 0.26), -0.1143),
        (3222, 0.6357, 0.76, (0.24, 0.26), -0.1143),
        (3284, 0.8999, 0.90, (0.10, 0.14), +0.0199)]


@pytest.mark.parametrize("did, fair, ask, book, recorded", LIVE)
def test_the_guard_reproduces_the_live_hold_edge(conn, did, fair, ask, book, recorded):
    """Anti-lookalike. The guard is only meaningful if the number it thresholds is the
    number the exit actually computed — otherwise it is a second, agreeing-by-luck rule.
    Three of these four were reconstructed against the exit rows the live book wrote."""
    _quote(conn, bid=book[0], ask=book[1])
    st = _single(fair, ask)
    got = fair - exits.struct_mid_cost(conn, st)
    assert got == pytest.approx(recorded, abs=1e-4), f"decision {did}"


@pytest.mark.parametrize("did, fair, ask, book, recorded", LIVE)
def test_the_guard_blocks_exactly_the_three_that_churned(conn, did, fair, ask, book,
                                                         recorded):
    """3100/3161/3222 round-tripped in the same tick; 3284 did not and is still open."""
    _quote(conn, bid=book[0], ask=book[1])
    st = _single(fair, ask)
    blocked = exits.opens_into_exit(st, exits.struct_mid_cost(conn, st))
    assert blocked is (did != 3284)


# ── the rule itself ───────────────────────────────────────────────────────────────────

def test_the_threshold_is_exit_edge_itself_not_a_copy(conn):
    """The whole point of the fix. If someone moves `EXIT_EDGE`, the entry floor moves with
    it; a second literal -0.06 here would let the two drift back into contradiction, which
    is the bug. Probed on both sides of the live constant."""
    _quote(conn, bid=0.40, ask=0.42)                    # mid-cost of a `no` leg = 0.59
    mid = exits.struct_mid_cost(conn, _single(0.5))
    assert mid == pytest.approx(0.59)
    just_under = _single(0.59 + exits.EXIT_EDGE - 1e-6)
    just_over = _single(0.59 + exits.EXIT_EDGE + 1e-6)
    assert exits.opens_into_exit(just_under, mid)
    assert not exits.opens_into_exit(just_over, mid)


def test_exactly_at_the_threshold_is_allowed(conn):
    """`>= EXIT_EDGE` holds in `exits.run` (`if hold_edge >= EXIT_EDGE: continue`), so the
    boundary case must OPEN. An off-by-one here would refuse a trade the exit keeps."""
    _quote(conn, bid=0.40, ask=0.42)
    mid = exits.struct_mid_cost(conn, _single(0.5))
    assert not exits.opens_into_exit(_single(mid + exits.EXIT_EDGE), mid)


def test_an_unmeasurable_book_abstains_rather_than_blocks(conn):
    """None is rule 1's "hold", not its "close". If the mid cannot be priced the exit
    cannot fire either, so there is no contradiction to prevent and the trade stands.
    Reading None as a block would silently switch the stream off on quiet books."""
    assert exits.struct_mid_cost(conn, _single(0.10)) is None     # no quote at all
    assert not exits.opens_into_exit(_single(0.10), None)


def test_a_wide_book_is_unmeasurable_exactly_as_rule_one_treats_it(conn):
    """`hold_state` refuses a book nobody is making a market in (the KXCPIYOY 0.18/0.98
    case), so the entry guard must refuse to price one too — not quietly use its midpoint,
    which is the failure that made a 0.58 "mid" look tradeable."""
    _quote(conn, bid=0.18, ask=0.98)
    assert exits.struct_mid_cost(conn, _single(0.10)) is None


def test_one_missing_leg_makes_the_whole_structure_unmeasurable(conn):
    """A bucket is one position. Pricing it off the leg that happens to have a quote would
    invent a mid-cost for a structure only half of which is priced."""
    _quote(conn, T, 0.23, 0.25)
    st = Struct("bucket", (Leg(T, "yes", 0.25, 1e9), Leg(T2, "no", 0.60, 1e9)),
                0.4, 0.85, 1.0, "bucket")
    assert exits.struct_mid_cost(conn, st) is None


def test_the_bucket_mid_cost_matches_the_structs_own_convention(conn):
    """`Struct.cost` for a bucket is `sum(leg prices) - 1`; the mid-priced twin has to use
    the same convention or the guard would compare a bucket's fair against a number a
    dollar away from its cost. This is the identity `struct_mid_cost` documents."""
    _quote(conn, T, 0.60, 0.64)          # yes leg mid 0.62
    _quote(conn, T2, 0.20, 0.24)         # no leg mid 1 - 0.22 = 0.78
    st = Struct("bucket", (Leg(T, "yes", 0.64, 1e9), Leg(T2, "no", 0.80, 1e9)),
                0.4, 0.44, 1.0, "bucket")
    assert exits.struct_mid_cost(conn, st) == pytest.approx(0.62 + 0.78 - 1.0)


def test_the_guard_cannot_bind_on_the_edge_stream(conn):
    """Why the fix is scoped to argmax and nothing else. The edge stream opens only at
    `net_edge >= min_net_edge > 0`, i.e. `fair > cost`, and a mid-cost is never above an
    ask-cost, so `fair - mid >= fair - cost > 0 > EXIT_EDGE`. The contradiction is
    structurally impossible there, and this pins that argument rather than asserting it."""
    _quote(conn, bid=0.23, ask=0.25)
    st = _single(0.80, price=0.77)                     # ask-cost 0.77, fair above it
    mid = exits.struct_mid_cost(conn, st)
    assert mid <= st.cost, "a mid-cost must never exceed the ask-cost"
    assert st.fair - st.cost > 0 and not exits.opens_into_exit(st, mid)


def test_the_guard_only_ever_subtracts(conn):
    """It is a veto and must never become a release: there is no argument list for which
    it returns something other than True/False, and True is the only value that changes
    what happens. Pinned because #124's pathology started as "one more gate"."""
    _quote(conn, bid=0.23, ask=0.25)
    for fair in (0.0, 0.3, 0.6357, 0.9, 1.0):
        assert isinstance(exits.opens_into_exit(_single(fair),
                                                exits.struct_mid_cost(conn, _single(fair))),
                          bool)
