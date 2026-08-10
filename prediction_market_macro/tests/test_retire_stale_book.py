"""#121 — a settled period must free its slot, and a superseded book must be retirable.

Two defects kept the live segment at zero settled trades for the whole week after the
2026-07-31 cutover, and they are different in kind:

  * `ledger.has_open` counted only `exit`/`cancel` as closing, while `open_positions`,
    `arb._has_open_arb`, `snipe._has_open_snipe` and `decide_all._has_any_open` all counted
    `settle_note` too. A settled position therefore stayed "held" for `decide_all` forever.
  * 43 positions opened before the cutover under a rule set with no `max_days_to_close`
    were holding 50 periods — 842 of the 1090 passes since the cutover were
    `already_open_no_averaging_down` — the longest until 2028-01.

The second is retired by rule, never by outcome. The tests below pin that: the selector
reads the decision's own `gate_snapshot` horizon, and it is blind to PnL.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import ledger, retire_stale_book as rsb
from prediction_market_macro.strategy.decision import GATES

CUT = "2026-07-31T16:14:00+00:00"
OPEN_TS = "2026-07-28T12:00:00+00:00"


@pytest.fixture()
def conn(tmp_path):
    return init_db(str(tmp_path / "t.db"))


def _open(conn, series, period, ticker, close_time, ts=OPEN_TS, size=1.0):
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, size_usd,"
        " inputs_json, model_version, gate_snapshot, note) VALUES(?,?,?,?,'open',?,"
        " '{}','m/1.0','{}','')",
        (ts, series, period,
         json.dumps({"kind": "single", "desc": f"YES {ticker}",
                     "legs": [{"ticker": ticker, "side": "yes", "price": 0.5}]}), size))
    conn.execute("INSERT INTO contracts(ticker, series, event_ticker, period, close_time,"
                 " first_seen_ts) VALUES(?,?,?,?,?,?)",
                 (ticker, series, f"{series}-EV", period, close_time, ts))
    conn.commit()
    return cur.lastrowid


def _note(conn, series, period, kind="settle_note"):
    conn.execute("INSERT INTO decisions(ts_utc, series, period, structure_json, kind,"
                 " inputs_json, model_version, gate_snapshot, note)"
                 " VALUES('2026-08-01T00:00:00+00:00',?,?,'{}',?,'{}','m/1.0','{}','')",
                 (series, period, kind))
    conn.commit()


# ── the has_open / open_positions disagreement ───────────────────────────────────────

def test_a_settled_period_is_no_longer_held(conn):
    _open(conn, "KXFED", "2026-07", "T-1", "2026-07-30T18:00:00+00:00")
    assert ledger.has_open(conn, "KXFED", "2026-07")
    _note(conn, "KXFED", "2026-07")
    assert not ledger.has_open(conn, "KXFED", "2026-07")


def test_has_open_and_open_positions_agree(conn):
    """The two were allowed to disagree once; that is what locked the slots."""
    _open(conn, "KXFED", "2026-07", "T-1", "2026-07-30T18:00:00+00:00")
    _open(conn, "KXCPI", "2026-07", "T-2", "2026-07-30T18:00:00+00:00")
    _note(conn, "KXFED", "2026-07")
    live = {(d["series"], d["period"]) for d in ledger.open_positions(conn)}
    for s, p in (("KXFED", "2026-07"), ("KXCPI", "2026-07")):
        assert ledger.has_open(conn, s, p) == ((s, p) in live), (s, p)


@pytest.mark.parametrize("closing_kind", ["exit", "cancel", "settle_note"])
def test_every_closing_kind_releases_the_slot(conn, closing_kind):
    _open(conn, "KXCPI", "2026-08", "T-9", "2026-08-30T18:00:00+00:00")
    _note(conn, "KXCPI", "2026-08", kind=closing_kind)
    assert not ledger.has_open(conn, "KXCPI", "2026-08")


# ── the retirement rule ──────────────────────────────────────────────────────────────

def test_retires_only_what_todays_gate_would_reject(conn):
    far = datetime.fromisoformat(OPEN_TS) + timedelta(days=GATES["max_days_to_close"] + 30)
    near = datetime.fromisoformat(OPEN_TS) + timedelta(days=GATES["max_days_to_close"] - 1)
    _open(conn, "KXFEDDECISION", "2027-04", "T-FAR", far.isoformat())
    _open(conn, "KXJOBLESSCLAIMS", "2026-08-06", "T-NEAR", near.isoformat())
    got = {c["series"] for c in rsb.candidates(conn, CUT)}
    assert got == {"KXFEDDECISION"}


def test_post_cutover_positions_are_left_alone(conn):
    far = (datetime.fromisoformat("2026-08-01T09:00:00+00:00")
           + timedelta(days=GATES["max_days_to_close"] + 30))
    _open(conn, "KXNATGASW", "2026-09-04", "T-P", far.isoformat(),
          ts="2026-08-01T09:00:00+00:00")
    assert rsb.candidates(conn, CUT) == []


def test_retirement_is_append_only_and_frees_the_slot(conn):
    far = datetime.fromisoformat(OPEN_TS) + timedelta(days=300)
    did = _open(conn, "KXFEDDECISION", "2027-04", "T-FAR", far.isoformat())
    before = conn.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"]

    out = rsb.retire(conn, CUT)

    assert out["retired"] == 1 and out["capital_released"] == 1.0
    assert not ledger.has_open(conn, "KXFEDDECISION", "2027-04")
    # the open row survives verbatim, and the cancel points back at it
    row = conn.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
    assert row["kind"] == "open" and row["ts_utc"] == OPEN_TS
    assert conn.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"] == before + 1
    cx = conn.execute("SELECT * FROM decisions WHERE kind='cancel'").fetchone()
    assert json.loads(cx["inputs_json"])["retires_decision_id"] == did


def test_retiring_twice_is_a_no_op(conn):
    far = datetime.fromisoformat(OPEN_TS) + timedelta(days=300)
    _open(conn, "KXFEDDECISION", "2027-04", "T-FAR", far.isoformat())
    assert rsb.retire(conn, CUT)["retired"] == 1
    assert rsb.retire(conn, CUT)["retired"] == 0


def test_the_rule_cannot_see_outcome(conn):
    """Retirement must not be selectable on PnL — otherwise it launders the record.

    Same horizon, opposite settlements: the selector must treat them identically.
    """
    far = datetime.fromisoformat(OPEN_TS) + timedelta(days=300)
    for s, result in (("KXFED", "yes"), ("KXCPI", "no")):
        _open(conn, s, "2027-04", f"T-{s}", far.isoformat())
        conn.execute("INSERT INTO settlements(ticker, series, period, result,"
                     " first_seen_ts) VALUES(?,?,?,?,?)",
                     (f"T-{s}", s, "2027-04", result, OPEN_TS))
    conn.commit()
    assert {c["series"] for c in rsb.candidates(conn, CUT)} == {"KXFED", "KXCPI"}
