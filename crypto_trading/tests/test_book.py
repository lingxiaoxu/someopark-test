"""BookMirror tests — synthetic messages, no network."""
from decimal import Decimal

from crypto_trading.crypto_common.kalshi.book import BookMirror


def snapshot_msg():
    return {"orderbook": {"asks": [["6.40", "10"], ["6.39", "5"]],
                          "bids": [["6.37", "7"], ["6.38", "3"]]}}


def test_snapshot_and_views():
    b = BookMirror("KXBTCPERP")
    b.apply_snapshot(snapshot_msg(), seq=10)
    assert b.synced and b.last_seq == 10
    assert b.best_bid() == (Decimal("6.38"), Decimal("3"))
    assert b.best_ask() == (Decimal("6.39"), Decimal("5"))
    assert b.mid() == Decimal("6.385")
    # microprice weighted toward the thinner side's price
    mp = b.microprice()
    assert Decimal("6.38") < mp < Decimal("6.39")
    assert mp == (Decimal("6.38") * 5 + Decimal("6.39") * 3) / 8


def test_delta_add_reduce_remove():
    b = BookMirror("KXBTCPERP")
    b.apply_snapshot(snapshot_msg(), seq=1)
    assert b.apply_delta({"side": "bid", "price": "6.38", "delta": "2"}, seq=2)
    assert b.bids[Decimal("6.38")] == Decimal("5")
    assert b.apply_delta({"side": "bid", "price": "6.38", "delta": "-5"}, seq=3)
    assert Decimal("6.38") not in b.bids
    assert b.best_bid()[0] == Decimal("6.37")


def test_seq_gap_marks_unsynced():
    b = BookMirror("KXBTCPERP")
    b.apply_snapshot(snapshot_msg(), seq=1)
    ok = b.apply_delta({"side": "ask", "price": "6.40", "delta": "1"}, seq=5)  # gap: 1 → 5
    assert not ok and not b.synced and b.gaps == 1
    # a fresh snapshot resyncs
    b.apply_snapshot(snapshot_msg(), seq=6)
    assert b.synced and b.last_seq == 6


def test_yes_no_side_aliases():
    b = BookMirror("X")
    b.apply_snapshot({"yes_dollars": [["0.40", "10"]], "no_dollars": [["0.55", "4"]]}, seq=1)
    assert b.best_bid() == (Decimal("0.40"), Decimal("10"))
    assert b.best_ask() == (Decimal("0.55"), Decimal("4"))
