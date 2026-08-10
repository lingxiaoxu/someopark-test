"""Wide/one-sided books must not be marked, and must not trigger exits.

Measured on the live paper book (2026-08-04): 14 of 49 open legs quote >= 15c wide, worst
KXCPIYOY-26SEP-T3.4 at 0.18/0.98. The midpoint of those books accounted for -$3.415 of
the -$10.115 unrealized -- it marked KXCPICORE-26AUG-T0.0, bought at 0.99 on a
near-certainty, down to 0.825. The same midpoint fed ops/exits.py's edge-reversal rule,
where it could liquidate a held position into the 0.18 bid.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import pnl
from prediction_market_macro.strategy.edge import two_sided


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


def _quote(conn, ticker, bid, ask):
    conn.execute(
        "INSERT INTO quotes(ts, ticker, yes_bid, yes_ask, bid_depth, ask_depth)"
        " VALUES(?,?,?,?,?,?)",
        ("2026-08-04T00:00:00+00:00", ticker, bid, ask, 500.0, 500.0))


def _position(conn, ticker, price, count=1, side="yes"):
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair, ask,"
        " net_edge, size_usd, inputs_json, model_version, gate_snapshot, note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-08-01T00:00:00+00:00", "KXCPICORE", "2026-08", "{}", "open", 0.5, 0.5,
         0.1, 1.0, "{}", "cpi/0.1.0", "{}", ""))
    conn.execute(
        "INSERT INTO fills(decision_id, ts_utc, ticker, side, price, count, fee_usd,"
        " mode) VALUES(?,?,?,?,?,?,?, 'paper')",
        (cur.lastrowid, "2026-08-01T00:00:00+00:00", ticker, side, price, count, 0.01))
    return cur.lastrowid


def test_two_sided_threshold():
    assert two_sided(0.49, 0.51)                 # 2c: a real market
    assert two_sided(0.40, 0.54)                 # 14c: still inside
    assert not two_sided(0.40, 0.55)             # 15c: out
    assert not two_sided(0.18, 0.98)             # the real KXCPIYOY-26SEP-T3.4 book
    assert not two_sided(None, 0.01)             # one-sided is not a market either
    assert not two_sided(0.66, None)


def test_mid_refuses_one_sided_and_wide_books(conn):
    _quote(conn, "TIGHT", 0.49, 0.51)
    _quote(conn, "WIDE", 0.66, 0.99)
    _quote(conn, "ONESIDED", None, 0.01)
    assert pnl._mid(conn, "TIGHT") == pytest.approx(0.50)
    assert pnl._mid(conn, "WIDE") is None
    # used to return the lone ask (0.01) as if it were a price
    assert pnl._mid(conn, "ONESIDED") is None
    assert pnl._mid(conn, "NOQUOTE") is None


def test_wide_book_leg_is_carried_at_cost_not_marked_down(conn):
    """The KXCPICORE-26AUG-T0.0 case: bought at 0.99, book 0.66/0.99.

    The midpoint booked -$0.34 of 'loss' on a leg whose ask never moved. Carrying at cost
    charges the fee and nothing else, and flags the leg with mid=NULL.
    """
    did = _position(conn, "WIDE", price=0.99, count=2)
    _quote(conn, "WIDE", 0.66, 0.99)
    assert pnl.mark_all(conn) == 1
    r = conn.execute("SELECT mid, pnl_usd FROM marks WHERE decision_id=?", (did,)).fetchone()
    assert r["mid"] is None
    assert r["pnl_usd"] == pytest.approx(-0.01)          # the fee, nothing invented


def test_tight_book_still_marks_normally(conn):
    did = _position(conn, "TIGHT", price=0.40, count=2)
    _quote(conn, "TIGHT", 0.49, 0.51)
    pnl.mark_all(conn)
    r = conn.execute("SELECT mid, pnl_usd FROM marks WHERE decision_id=?", (did,)).fetchone()
    assert r["mid"] == pytest.approx(0.50)
    assert r["pnl_usd"] == pytest.approx((0.50 - 0.40) * 2 - 0.01)


def test_unmarked_legs_are_never_silent(conn):
    """A leg dropping out of the reported exposure must leave a trace."""
    _position(conn, "WIDE", price=0.99, count=2)
    _quote(conn, "WIDE", 0.66, 0.99)
    pnl.mark_all(conn)
    a = conn.execute("SELECT message FROM alerts WHERE source='pnl.mark_all'").fetchone()
    assert a is not None and "carried at cost" in a["message"]


def test_exits_will_not_dump_a_position_into_a_wide_book(conn):
    """Edge reversal needs a measurable holding edge; a 0.18/0.98 book has none.

    Before the fix the 0.58 midpoint could show a reversal and sell at the 0.18 bid.
    """
    from prediction_market_macro.ops import exits
    did = _position(conn, "WIDE", price=0.90, count=1)
    _quote(conn, "WIDE", 0.18, 0.98)
    conn.execute(
        "INSERT INTO contracts(ticker, event_ticker, series, period, floor_strike,"
        " strike_type, close_time, first_seen_ts) VALUES(?,?,?,?,?,?,?,?)",
        ("WIDE", "KXCPICORE-26AUG", "KXCPICORE", "2026-08", 0.0, "greater",
         "2026-09-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"))
    conn.execute(
        "INSERT INTO preds(series, period, asof, ladder_json, dist_json, model_version,"
        " data_horizon, created_ts) VALUES(?,?,?,?,?,?,?,?)",
        ("KXCPICORE", "2026-08", "2026-08-04T00:00:00+00:00", '{"0.0": 1.0}', "{}",
         "cpi/0.1.0", "2026-08-04T00:00:00+00:00", "2026-08-04T00:00:00+00:00"))
    conn.commit()

    class _S:
        pass

    n = exits.run(conn, _S())
    assert n == 0
    assert conn.execute("SELECT COUNT(*) c FROM decisions WHERE kind='exit'"
                        ).fetchone()["c"] == 0
    assert did  # position is still held


def test_unmarked_alert_is_deduped_but_never_suppressed_on_change(conn):
    """The carried-at-cost disclosure must survive, without burying the alert feed.

    mark_all runs from jobs.tick every 900s. A book that stays illiquid for a day wrote
    ~96 byte-identical warn rows (25 were visible in a single frontend screenshot on
    2026-08-09), which is how a routine disclosure came to read as an outage. Dedupe is
    on the message text, so it is only ever silence about a state that has not changed.
    """
    _position(conn, "WIDE", price=0.90, count=1)
    _quote(conn, "WIDE", 0.18, 0.98)          # unmarkable: 80c wide
    conn.commit()

    for _ in range(5):                         # five tick cycles, same state
        pnl.mark_all(conn)
    rows = conn.execute(
        "SELECT message FROM alerts WHERE source='pnl.mark_all'").fetchall()
    assert len(rows) == 1, f"expected 1 deduped alert, got {len(rows)}"
    assert "1/1 open legs carried at cost" in rows[0]["message"]

    # a CHANGE in the counts must fire immediately, not wait out the 24h window
    _position(conn, "WIDE2", price=0.90, count=1)
    _quote(conn, "WIDE2", 0.10, 0.99)
    conn.commit()
    pnl.mark_all(conn)
    msgs = [r["message"] for r in conn.execute(
        "SELECT message FROM alerts WHERE source='pnl.mark_all' ORDER BY ts").fetchall()]
    assert len(msgs) == 2, f"state change must not be deduped, got {msgs}"
    assert "2/2 open legs carried at cost" in msgs[-1]


def test_no_alert_when_every_leg_is_markable(conn):
    _position(conn, "TIGHT", price=0.50, count=1)
    _quote(conn, "TIGHT", 0.49, 0.51)
    conn.commit()
    pnl.mark_all(conn)
    assert conn.execute("SELECT COUNT(*) c FROM alerts WHERE source='pnl.mark_all'"
                        ).fetchone()["c"] == 0
