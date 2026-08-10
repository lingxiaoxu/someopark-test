"""The backtest has to devig a book the way production devigs it.

`decide()`'s sanity gate compares the model's fair against the DEVIGGED market probability
for the same structure, and in pooled mode the devigged pmf is half of the forecast itself.
So the choice of devig is not a presentation detail — it decides which bets exist.

`decide_all:226` switches on the book's shape: a `between` book is a partition, whose leg
prices normalise multiplicatively, while a `greater` book is a survival curve, whose leg
prices are differenced. `walkforward` called `ladder_implied` unconditionally, so KXWTIW —
the only series that quotes `between` legs — was gated against a market pmf built by
differencing a non-monotone partition.

These tests pin the switch and the size of the error it was making, so the next person to
touch that call site sees both.
"""
from __future__ import annotations

from prediction_market_macro.research.walkforward import _implied
from prediction_market_macro.strategy import devig

# a partition: three mutually exclusive $1-wide buckets plus the two open tails.
# Sums to 1.06 gross, i.e. a normal 6c vig — nothing pathological about this book.
BUCKETS = [
    {"ticker": "T-LESS", "strike": None, "strike_type": "less",
     "yes_bid": 0.08, "yes_ask": 0.12},
    {"ticker": "T-69", "strike": 69.0, "strike_type": "between",
     "yes_bid": 0.18, "yes_ask": 0.22},
    {"ticker": "T-70", "strike": 70.0, "strike_type": "between",
     "yes_bid": 0.38, "yes_ask": 0.42},
    {"ticker": "T-71", "strike": 71.0, "strike_type": "between",
     "yes_bid": 0.23, "yes_ask": 0.27},
    {"ticker": "T-72", "strike": 72.0, "strike_type": "greater",
     "yes_bid": 0.06, "yes_ask": 0.10},
]
LADDER = [
    {"ticker": "L-68", "strike": 68.0, "strike_type": "greater",
     "yes_bid": 0.88, "yes_ask": 0.92},
    {"ticker": "L-70", "strike": 70.0, "strike_type": "greater",
     "yes_bid": 0.48, "yes_ask": 0.52},
    {"ticker": "L-72", "strike": 72.0, "strike_type": "greater",
     "yes_bid": 0.13, "yes_ask": 0.17},
]


def test_a_between_book_is_devigged_as_a_partition_like_decide_all():
    assert _implied(BUCKETS)["pmf"] == devig.bucket_implied(BUCKETS)["pmf"]


def test_a_greater_ladder_is_still_devigged_as_a_survival_curve():
    """The switch must not change the 13 series that were already right — every one of them
    is a `greater` (or `greater_or_equal`) ladder."""
    assert _implied(LADDER)["pmf"] == devig.ladder_implied(LADDER)["pmf"]


def test_the_wrong_devig_moved_most_of_the_mass_into_one_bucket():
    """Why this was worth fixing rather than noting. Read as a survival curve, the
    partition's prices are not monotone, the isotonic fit flattens them, and differencing a
    flat curve puts ~everything in the first cell — so the gate saw a market that was ~sure
    of an outcome the market had priced at 20c."""
    partition = devig.bucket_implied(BUCKETS)["pmf"]
    wrong = devig.ladder_implied(BUCKETS)["pmf"]
    assert max(partition.values()) < 0.45, "the real book is genuinely spread out"
    assert max(wrong.values()) > 0.65, "the old path concentrated the mass"
    # and it is not a relabelling of the same distribution — the 70 bucket, which the
    # market prices at 40c and is the one the favourite leg would buy, comes out at 0.0
    assert partition[70.0] > 0.35 and wrong[70.0] == 0.0
    # the 69 bucket goes the other way — priced at 20c, devigged to 70%
    assert partition[69.0] < 0.25 < 0.65 < wrong[69.0]
    # and the open 'less' tail has no strike, so the ladder path drops it outright and
    # invents an above-the-top cell the partition book does not have
    assert float("-inf") in partition and float("-inf") not in wrong
    assert float("inf") in wrong and float("inf") not in partition


def test_a_book_with_no_strike_types_at_all_still_devigs_as_a_ladder():
    """`meta` rows are built from `contracts`, where `strike_type` is nullable — 588 rows
    carry NULL. A missing type must fall to the ladder branch rather than raise."""
    bare = [{**l, "strike_type": None} for l in LADDER]
    assert _implied(bare)["pmf"] == devig.ladder_implied(bare)["pmf"]
