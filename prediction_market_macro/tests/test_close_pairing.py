"""#150 — a close is paired to the POSITION it closed, everywhere money is reported.

#149 fixed the four write-side "do I already hold this?" checks. It did not touch the
read side, and three consumers there were still asking the old question:

    ops/frontend_export  "is there a settle_note on this (series, period) with a bigger id?"
    ops/pnl.report       dedupe closures with GROUP BY series, period
    ops/risk             same dedupe, plus NOT EXISTS(any later close on this period)

Each fails in a different direction and all three surface as money:

  * an EXIT is not a settle_note, so an exited position stayed on the DISPLAYED open book
    forever with its realized PnL nowhere. Live: 12 open / $9.55 shown against a true
    8 / $6.39, and -$0.48 of closed PnL missing from the track record.
  * `LIMIT 1` credits one settlement to every position on the period. KXWTIW 2026-08-07
    holds three and settles the next day; three phantoms sat on the same period.
  * `GROUP BY series, period` keeps one closure per PERIOD when the thing that closes is
    a POSITION, so two thirds of that settlement would never reach `pnl.report` or the
    drawdown breaker.

The tests below are behavioural, not source-text, because every one of these has an
observable wrong answer on a three-line fixture.
"""
from __future__ import annotations

import json

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import ledger

TS = "2026-08-06T18:00:00+00:00"
S, P = "KXWTIW", "2026-08-07"


@pytest.fixture()
def conn(tmp_path):
    return init_db(str(tmp_path / "t.db"))


def _open(conn, kind="open", series=S, period=P, usd=1.0, ts=TS):
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, size_usd,"
        " inputs_json, model_version, gate_snapshot, note)"
        " VALUES(?,?,?,'{\"desc\":\"YES T\"}',?,?,'{}','m/1.0','{}','')",
        (ts, series, period, kind, usd))
    conn.commit()
    return cur.lastrowid


def _close(conn, kind="exit", closes=None, realized=None, series=S, period=P, ts=TS,
           origin=None):
    payload = {}
    if realized is not None:
        payload["realized_usd"] = realized
    if origin is not None:
        payload["origin"] = origin
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, size_usd,"
        " inputs_json, model_version, gate_snapshot, note, closes_decision_id)"
        " VALUES(?,?,?,'{}',?,?,?,'m/1.0','{}','',?)",
        (ts, series, period, kind, realized if kind == "settle_note" else 1.0,
         json.dumps(payload), closes))
    conn.commit()
    return cur.lastrowid


# ── the pairing itself ────────────────────────────────────────────────────────────────

def test_every_open_is_either_still_open_or_closed_exactly_once(conn):
    """The invariant the three broken readers each violated in their own way. It holds on
    the live ledger too: 8 open + 103 closed = 111 open-kind rows."""
    a, b, c_ = _open(conn), _open(conn), _open(conn, "argmax")
    _close(conn, "exit", closes=b, realized=-0.07)

    still = {d["id"] for d in ledger.open_positions(conn)}
    closed = {d["id"] for d in ledger.closures(conn)}
    assert still == {a, c_} and closed == {b}
    assert not (still & closed), "a position cannot be both"
    assert still | closed == {a, b, c_}, "and none may be neither"


def test_an_exit_closes_a_position_not_only_a_settle_note(conn):
    """The display bug. `frontend_export` looked for `settle_note` alone, so these
    positions never left the open book and their loss never reached the track record."""
    a = _open(conn)
    _close(conn, "exit", closes=a, realized=-0.27)
    assert [d["id"] for d in ledger.open_positions(conn)] == []
    assert [d["close"]["kind"] for d in ledger.closures(conn)] == ["exit"]


def test_three_positions_on_one_period_settle_separately(conn):
    """The live KXWTIW 2026-08-07 shape. Under `LIMIT 1` all three were credited the
    FIRST settle_note's realized — a payout counted three times — and under
    `GROUP BY series, period` two of the three vanished from every aggregate."""
    ids = [_open(conn), _open(conn), _open(conn, "argmax")]
    for i, rz in zip(ids, (0.12, 0.12, 0.09)):
        _close(conn, "settle_note", closes=i, realized=rz)

    got = {d["id"]: ledger.realized_usd(d["close"]) for d in ledger.closures(conn)}
    assert got == {ids[0]: 0.12, ids[1]: 0.12, ids[2]: 0.09}
    assert round(sum(got.values()), 6) == 0.33, "not 3x the first one, and not just one"


def test_a_close_on_one_period_never_pairs_to_another(conn):
    a = _open(conn)
    b = _open(conn, period="2026-08-14")
    _close(conn, "exit", closes=b, realized=-0.05, period="2026-08-14")
    assert [d["id"] for d in ledger.open_positions(conn)] == [a]
    assert [d["id"] for d in ledger.closures(conn)] == [b]


def test_closures_can_be_narrowed_to_one_close_kind(conn):
    """`pnl.report` wants settlements only; the drawdown breaker wants settle+exit and
    must NOT see `cancel` — those are #121's rule-based retirement of the disavowed book,
    and a bookkeeping action may not move a risk control."""
    a, b, c_ = _open(conn), _open(conn), _open(conn)
    _close(conn, "settle_note", closes=a, realized=0.20)
    _close(conn, "exit", closes=b, realized=-0.07)
    _close(conn, "cancel", closes=c_)

    assert len(ledger.closures(conn)) == 3
    assert [d["id"] for d in ledger.closures(conn, ("settle_note",))] == [a]
    assert [d["id"] for d in ledger.closures(conn, ("settle_note", "exit"))] == [a, b]


def test_the_duplicate_closes_of_one_position_count_as_one_closure(conn):
    """The pre-#149 settle_pass re-settled positions it had already finished: 23 live
    settle_note rows retire only 7 positions. Counting each row as an independent closure
    books the same payout again (-6.07 against a true -4.37) and lets one trade fill the
    20-slot drawdown window."""
    a = _open(conn)
    for _ in range(6):
        _close(conn, "exit", closes=a, realized=-0.07)
    assert len(ledger.closures(conn)) == 1


def test_legacy_unattributed_closes_still_pair_fifo(conn):
    """ALL 103 live closures predate `closes_decision_id` — #149 landed the write side but
    no close row has been written since. If NULL paired to nothing, every historical
    position would read as open forever."""
    a, b = _open(conn), _open(conn)
    _close(conn, "exit", closes=None, realized=-0.10)
    assert [d["id"] for d in ledger.closures(conn)] == [a]
    assert [d["id"] for d in ledger.open_positions(conn)] == [b]


# ── realized_usd: absent is not zero ──────────────────────────────────────────────────

def test_a_close_without_a_recorded_figure_reports_none_not_zero(conn):
    """42 of the 53 live exits predate the field. `.get(..., 0.0)` here produces a clean
    $0.00 for a position that in fact lost money — the §25.20(b) mistake."""
    a = _open(conn)
    _close(conn, "exit", closes=a, realized=None)
    assert ledger.realized_usd(ledger.closures(conn)[0]["close"]) is None


def test_settle_note_realized_falls_back_to_size_usd(conn):
    """All 23 live settle_notes write the figure to both places and agree to the cent;
    `inputs_json` is read first so the two cannot silently diverge."""
    a = _open(conn)
    cid = _close(conn, "settle_note", closes=a, realized=-1.20)
    conn.execute("UPDATE decisions SET inputs_json='{}' WHERE id=?", (cid,))
    conn.commit()
    assert ledger.realized_usd(ledger.closures(conn)[0]["close"]) == -1.20


# ── the three consumers ───────────────────────────────────────────────────────────────

def test_pnl_report_counts_every_settled_position_not_one_per_period(conn):
    from prediction_market_macro.ops import pnl
    for i, rz in zip([_open(conn), _open(conn), _open(conn)], (0.12, 0.12, 0.09)):
        _close(conn, "settle_note", closes=i, realized=rz)

    rep = pnl.report(conn)
    row = [r for r in rep["settled_by_series"] if r["series"] == S][0]
    assert row["n"] == 3 and row["realized"] == 0.33
    assert sum(r["n"] for r in rep["settled_by_origin"]) == 3


def test_pnl_report_open_by_kind_drops_closed_positions(conn):
    from prediction_market_macro.ops import pnl
    a = _open(conn, "open")
    _open(conn, "argmax")
    _close(conn, "exit", closes=a, realized=-0.07)
    assert pnl.report(conn)["open_by_kind"] == [{"kind": "argmax", "n": 1, "staked": 1.0}]


def test_risk_open_exposure_counts_positions_not_periods(conn):
    """`NOT EXISTS(any later close on this period)` is not a count: one close hid every
    position on the period, and an exposure cap that under-reads fails open."""
    from prediction_market_macro.ops import risk
    a = _open(conn, usd=1.0)
    _open(conn, usd=2.0)
    _open(conn, usd=3.0)
    _close(conn, "exit", closes=a, realized=-0.07)
    exp = risk._open_exposure(conn)
    assert len(exp) == 2 and sum(e["size_usd"] for e in exp) == 5.0


def test_the_drawdown_breaker_ignores_administrative_cancels(conn):
    from prediction_market_macro.ops import risk
    for _ in range(25):
        i = _open(conn)
        _close(conn, "cancel", closes=i)
    assert risk.check_rolling20(conn) is None, "cancels must not fill the window"
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE source='circuit_breaker'"
                        ).fetchone()[0] == 0


def test_the_breaker_says_so_when_it_is_short_of_data(conn):
    """It returned None either way — 'no drawdown' and 'I cannot see' were the same
    answer. On the live ledger that silence has hidden a permanently disarmed control:
    42 unrecorded exits leave 18 scored closures against a required 20."""
    from prediction_market_macro.ops import risk
    for _ in range(5):
        i = _open(conn)
        _close(conn, "exit", closes=i, realized=None)
    assert risk.check_rolling20(conn) is None
    a = conn.execute("SELECT level, message FROM alerts WHERE source='risk'").fetchall()
    assert len(a) == 1 and a[0]["level"] == "warn"
    assert "disarmed" in a[0]["message"] and "0/20" in a[0]["message"]

    risk.check_rolling20(conn)          # same day: one row, not one per cycle
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE source='risk'"
                        ).fetchone()[0] == 1


def test_the_breaker_still_trips_on_a_real_drawdown(conn):
    """The point of all of the above. 20 scored closures deep enough to breach
    -2x per_event_usd must still fire the circuit breaker."""
    from prediction_market_macro.ops import risk
    for _ in range(20):
        i = _open(conn)
        _close(conn, "exit", closes=i, realized=-1.0)
    reason = risk.check_rolling20(conn)
    assert reason is not None and "rolling20_pnl" in reason
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE source='circuit_breaker'"
                        ).fetchone()[0] == 1
