"""arb.execute: the fee-gate rejection must be VISIBLE, and the alert must not spam.

Why this exists: the 2026-08-10 audit found 9 MONOTONE-ARB detections against 0 arb
decisions, and nothing in the system could say whether the fee gate always bound or
the detect→execute path was broken. The replay showed fees (2-4c, ceil per leg)
swallowing every 1-2c gross. These tests pin the disambiguation alert:

1. A violation rejected by the fee gate writes ONE 'arb'-source ARB-REJECTED alert
   with the net/gross/fees arithmetic in the text.
2. Re-detection the same day does not write a second identical alert.
3. A violation that CLEARS the gates trades (decision row) and writes no rejection.
"""
from __future__ import annotations

import sqlite3

import pytest

from prediction_market_macro.strategy import arb


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE alerts(ts TEXT, level TEXT, source TEXT, message TEXT);
    CREATE TABLE decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT,
      series TEXT, period TEXT, structure_json TEXT, kind TEXT, fair REAL, ask REAL,
      net_edge REAL, size_usd REAL, inputs_json TEXT, model_version TEXT,
      gate_snapshot TEXT, note TEXT, closes_decision_id INTEGER);
    CREATE TABLE fills(decision_id INTEGER, ts_utc TEXT, ticker TEXT, side TEXT,
      price REAL, count INTEGER, fee_usd REAL, mode TEXT);
    """)
    return c


def _monotone(lo_ask, hi_bid, depth=100.0):
    """legs_meta + violations for one monotone pair, at controllable prices."""
    legs = [{"ticker": "T-A", "yes_ask": lo_ask, "yes_bid": lo_ask - 0.01,
             "ask_depth": depth, "bid_depth": depth},
            {"ticker": "T-B", "yes_ask": hi_bid + 0.02, "yes_bid": hi_bid,
             "ask_depth": depth, "bid_depth": depth}]
    v = [{"buy": {"ticker": "T-A", "yes_ask": lo_ask},
          "sell": {"ticker": "T-B", "yes_bid": hi_bid},
          "gross": round(hi_bid - lo_ask, 4)}]
    return legs, v


def test_fee_gate_rejection_is_alerted_once(conn):
    # the real CPIYOY case: gross 2c, mid-book prices, 2-leg fees 4c -> net -2c
    legs, v = _monotone(lo_ask=0.50, hi_bid=0.52)
    assert arb.execute(conn, "KXCPIYOY", "26SEP", legs, v) == 0
    assert arb.execute(conn, "KXCPIYOY", "26SEP", legs, v) == 0   # re-detected later
    rows = conn.execute("SELECT message FROM alerts WHERE source='arb'").fetchall()
    assert len(rows) == 1
    msg = rows[0]["message"]
    assert msg.startswith("ARB-REJECTED KXCPIYOY/26SEP")
    assert "net -0.020" in msg and "fees 0.040" in msg and "path alive" in msg
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_clearing_arb_trades_and_does_not_alert_rejection(conn):
    # gross 8c at extreme prices: fees 1c+1c, net +6c > MIN_NET
    legs, v = _monotone(lo_ask=0.03, hi_bid=0.11)
    assert arb.execute(conn, "KXTEST", "26SEP", legs, v) == 1
    d = conn.execute("SELECT kind, note FROM decisions").fetchone()
    assert d["kind"] == "arb"
    assert not conn.execute(
        "SELECT 1 FROM alerts WHERE message LIKE 'ARB-REJECTED%'").fetchone()


def test_depth_cap_is_counted_in_contracts_not_dollars(conn):
    """铁律 5 caps at 20% of the THINNEST LEG, and that cap is a CONTRACT COUNT.

    The sizing block used to read
        usd = min(MAX_ARB_USD, DEPTH_FRAC * min_depth);  count = int(usd / cost1)
    which put a contract count into a variable named `usd` and divided it by
    $/contract — inflating the depth cap by 1/cost1. The cheaper the structure, the
    worse it got, and a cheap structure is exactly what a big arb looks like.

    Priced here at lo_ask 0.03 / hi_bid 0.95, so cost1 = 0.03 + 0.05 = 0.08:
      depth 6  -> 铁律 5 allows int(0.2*6) = 1 contract.
                  Old form: min(2.00, 1.20) / 0.08 = 15 contracts — 250% of a
                  six-deep book, against a rule that meant to take a fifth of it.
      depth 500 -> the DOLLAR cap binds first: int(2.00 / 0.08) = 25 contracts.
    Both directions pinned, because fixing the first by clamping harder everywhere
    would silently shrink the second."""
    legs, v = _monotone(lo_ask=0.03, hi_bid=0.95, depth=6.0)
    assert arb.execute(conn, "KXTHIN", "26SEP", legs, v) == 1
    n = conn.execute("SELECT count FROM fills WHERE decision_id="
                     "(SELECT id FROM decisions WHERE series='KXTHIN')").fetchone()[0]
    assert n == 1, f"depth 6 must cap at 1 contract (was 15 before the fix), got {n}"

    legs, v = _monotone(lo_ask=0.03, hi_bid=0.95, depth=500.0)
    assert arb.execute(conn, "KXDEEP", "26SEP", legs, v) == 1
    n = conn.execute("SELECT count FROM fills WHERE decision_id="
                     "(SELECT id FROM decisions WHERE series='KXDEEP')").fetchone()[0]
    assert n == 25, f"deep book must cap at the $2 budget = 25 contracts, got {n}"


def test_too_thin_to_size_is_alerted_not_silent(conn):
    """Cleared the fee gate, killed by depth — its own alert, distinct from the fee
    gate. Free money nobody could take is a different fact from free money that was
    never free, and before this both returned 0 saying nothing. (The old form took
    int(0.80 / 0.08) = 10 contracts on a four-deep book here.)"""
    legs, v = _monotone(lo_ask=0.03, hi_bid=0.95, depth=4.0)   # int(0.2*4) = 0
    assert arb.execute(conn, "KXMICRO", "26SEP", legs, v) == 0
    msg = conn.execute(
        "SELECT message FROM alerts WHERE message LIKE 'ARB-TOO-THIN%'").fetchone()
    assert msg is not None and "thinnest leg 4 contracts" in msg[0]
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
