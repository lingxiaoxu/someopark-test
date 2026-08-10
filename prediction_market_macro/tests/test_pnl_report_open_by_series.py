"""#151/F9 — `pnl.report`'s two open-book breakdowns must describe the same book.

`report()` returns `open_by_series` and `open_by_kind` side by side in one dict. They were
computed from different sources: `open_by_kind` from `ledger.open_positions`, `open_by_series`
from a raw `SELECT ... WHERE kind='open' GROUP BY series` that joined nothing. Two defects
stacked in that one SELECT — no close accounting (every position ever opened still counted),
and `kind='open'` alone (argmax/arb/snipe holdings invisible). On the live ledger that read
107 positions / $105.36 against a truth of 8 / $6.39.

The fix derives both from one pass over one source, so the assertion worth pinning is not a
figure but the agreement: whatever `open_by_kind` totals, `open_by_series` totals the same.
#149 and #150 each edited this function and walked past the bad line, so the tests below are
written against the invariant rather than against today's numbers.
"""
from __future__ import annotations

import json

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import pnl

TS = "2026-08-06T18:00:00+00:00"


@pytest.fixture()
def conn(tmp_path):
    return init_db(str(tmp_path / "t.db"))


def _open(conn, series, kind="open", usd=1.0, period="2026-08-07"):
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, size_usd,"
        " inputs_json, model_version, gate_snapshot, note)"
        " VALUES(?,?,?,'{\"desc\":\"YES T\"}',?,?,'{}','m/1.0','{}','')",
        (TS, series, period, kind, usd))
    conn.commit()
    return cur.lastrowid


def _close(conn, series, closes, kind="exit", realized=0.0, period="2026-08-07"):
    conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, size_usd,"
        " inputs_json, model_version, gate_snapshot, note, closes_decision_id)"
        " VALUES(?,?,?,'{}',?,?,?,'m/1.0','{}','',?)",
        (TS, series, period, kind, realized, json.dumps({"realized_usd": realized}), closes))
    conn.commit()


def _totals(rows, key):
    return (sum(r["n"] for r in rows), round(sum(r["staked"] for r in rows), 4))


def test_a_closed_position_leaves_the_open_book(conn):
    """The dominant term: no close accounting at all. Both an `exit` and a `settle_note`
    retire a position, and neither used to."""
    a = _open(conn, "KXWTIW", usd=2.0)
    b = _open(conn, "KXCPI", usd=3.0)
    c = _open(conn, "KXU3", usd=4.0)
    _close(conn, "KXWTIW", closes=a, kind="exit", realized=-0.2)
    _close(conn, "KXCPI", closes=b, kind="settle_note", realized=0.5)

    assert pnl.report(conn)["open_by_series"] == [
        {"series": "KXU3", "n": 1, "staked": 4.0}]
    assert c  # the one still standing


@pytest.mark.parametrize("kind", ["open", "argmax", "arb", "snipe"])
def test_every_open_kind_shows_up_by_series(conn, kind):
    """Parametrised over all of OPEN_KINDS. `arb` and `snipe` have never fired on the live
    book, which is exactly why their absence from a report would go unnoticed."""
    _open(conn, "KXWTIW", kind=kind, usd=1.5)
    assert pnl.report(conn)["open_by_series"] == [
        {"series": "KXWTIW", "n": 1, "staked": 1.5}]


def test_the_two_breakdowns_of_the_same_book_agree(conn):
    """The invariant the bug violated. A mixed book — several series, several kinds, some
    closed — must total identically whichever way `report` slices it."""
    ids = [_open(conn, "KXWTIW", "open", 2.0), _open(conn, "KXWTIW", "argmax", 0.9),
           _open(conn, "KXCPI", "open", 1.25), _open(conn, "KXCPI", "snipe", 1.75),
           _open(conn, "KXU3", "arb", 3.0)]
    _close(conn, "KXCPI", closes=ids[2], kind="settle_note", realized=0.4)

    rep = pnl.report(conn)
    assert _totals(rep["open_by_series"], "series") == _totals(rep["open_by_kind"], "kind")
    assert _totals(rep["open_by_series"], "series") == (4, 7.65)


def test_an_empty_book_reports_nothing_open(conn):
    """Degenerate case, and the one the old SELECT could never produce once the book had
    ever traded: a fully closed book must read empty, not read as its own history."""
    a = _open(conn, "KXWTIW", usd=2.0)
    _close(conn, "KXWTIW", closes=a, kind="settle_note", realized=0.1)
    rep = pnl.report(conn)
    assert rep["open_by_series"] == [] and rep["open_by_kind"] == []
    assert rep["settled_by_series"] == [
        {"series": "KXWTIW", "n": 1, "realized": 0.1}], "the close still lands in settled"
