"""Postponed matches settle at the venue's declared final price; finished ones keep the regulation path."""
import json
import sqlite3

import pytest

from prediction_market_soccer.util import paper_store, venue_settlement
from prediction_market_soccer.util.pricing import sized_pnl_cents

FID = 1570389
ORIGINAL = "2026-09-16T19:30:00+00:00"


def _db(kickoff=ORIGINAL, status="NS", goals=(None, None), provider="poly_us"):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    paper_store.ensure(conn)
    conn.execute("""CREATE TABLE quote_receipt_v1 (receipt_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
        payload_hash TEXT NOT NULL, raw_ref TEXT NOT NULL, recorded_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE fixture (api_id INTEGER PRIMARY KEY, league_id INTEGER, season INTEGER, round TEXT,
        kickoff_ts TEXT, status_short TEXT, status_long TEXT, elapsed INTEGER, home_api_id INTEGER,
        away_api_id INTEGER, home_goals INTEGER, away_goals INTEGER, venue_name TEXT, venue_city TEXT,
        raw_json TEXT, updated_at TEXT)""")
    raw = {"score": {"fulltime": {"home": goals[0], "away": goals[1]}}}
    conn.execute("INSERT INTO fixture (api_id, kickoff_ts, status_short, home_api_id, away_api_id, home_goals, away_goals, raw_json) "
                 "VALUES (?,?,?,?,?,?,?,?)", (FID, kickoff, status, 539, 531, goals[0], goals[1], json.dumps(raw)))
    binding = {"provider": provider, "market_id": "atc-lal-lev-ath-2026-09-16-lev", "outcome": "yes",
               "kickoff_ts": ORIGINAL, "fixture_api_id": FID}
    conn.execute("INSERT INTO quote_receipt_v1 VALUES ('r1', ?, 'h', 'ref', ?)",
                 (json.dumps({"binding": binding}), "2026-09-16T19:20:25+00:00"))
    entry = {"decision_id": "e1", "book_version_id": "b1", "decision_at": "2026-09-16T19:21:37+00:00",
             "side": "home", "entry_cents": 29.0, "stake_usd": 0.75, "bet_kind": "value", "ledger_venue": "poly_us",
             "home": "Levante", "away": "Athletic Club", "home_id": "levante", "away_id": "athletic_club",
             "comp": "laliga", "kickoff_ts": ORIGINAL, "stage": "group", "model": {"home": 0.3665},
             "raw_model": {"home": 0.37, "draw": 0.27, "away": 0.36},
             "model_pick": "home", "net_edge": 0.0658, "model_version": "m", "method_version": "v",
             "forward_epoch_id": "ep", "method_manifest": {"manifest_id": "x"},
             "selected_quote": {"receipt_id": "r1", "venue": provider, "price": 0.29}}
    conn.execute("INSERT INTO paper_entry VALUES ('e1','b1',?,'pre',?,?)",
                 (FID, entry["decision_at"], json.dumps(entry)))
    conn.commit()
    return conn


def _closed(price):
    return {"poly_us": lambda market_id: (price, {"market": {"slug": market_id, "closed": True},
                                                  "settlement": {"slug": market_id, "settlement": price}})}


def test_postponed_match_settles_at_the_venue_price():
    conn = _db(kickoff="2026-10-21T18:00:00+00:00")
    out = venue_settlement.observe(conn, fetchers=_closed(0.28))
    assert out["recorded"] == 1 and not out["errors"]
    assert paper_store.settle(conn) == 1
    completion = paper_store.completed_records(conn)[0]
    record = completion["record"]
    assert completion["settlement_basis"] == "venue_final_settlement"
    assert record["result"] == "postponed" and record["score"] is None and record["won"] is None
    assert record["settle_cents"] == 28.0
    assert record["realized_pnl_cents"] == sized_pnl_cents(29.0, -1.0, 0.75)
    assert completion["venue_settlements"]["pre"]["settle_price"] == 0.28
    assert paper_store.positions(conn)[0]["status"] == "settled"


def test_evidence_is_recorded_once_and_immutable():
    conn = _db(kickoff="2026-10-21T18:00:00+00:00")
    venue_settlement.observe(conn, fetchers=_closed(0.28))
    calls = []
    venue_settlement.observe(conn, fetchers={"poly_us": lambda m: calls.append(m)})
    assert calls == []
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE paper_venue_settlement SET settle_price=0.9")


def test_finished_match_keeps_the_regulation_settlement():
    conn = _db(status="FT", goals=(2, 1))
    calls = []
    assert venue_settlement.observe(conn, fetchers={"poly_us": lambda m: calls.append(m)})["displaced_legs"] == 0
    assert calls == []


def test_on_schedule_match_is_never_queried_or_sealed():
    conn = _db(kickoff="2026-09-16T21:00:00+00:00")
    calls = []
    venue_settlement.observe(conn, fetchers={"poly_us": lambda m: calls.append(m)})
    assert calls == [] and paper_store.settle(conn) == 0


def test_open_venue_market_leaves_the_position_open():
    conn = _db(status="PST")
    out = venue_settlement.observe(conn, fetchers={"poly_us": lambda m: None})
    assert out["pending"] == 1 and out["recorded"] == 0
    assert paper_store.settle(conn) == 0


def test_unsupported_venue_is_reported_not_sealed():
    conn = _db(status="PST", provider="other")
    out = venue_settlement.observe(conn)
    assert out["errors"] and paper_store.settle(conn) == 0


def test_short_side_contract_takes_the_complement():
    conn = _db(kickoff="2026-10-21T18:00:00+00:00")
    payload = json.loads(conn.execute("SELECT payload FROM quote_receipt_v1").fetchone()[0])
    payload["binding"]["outcome"] = "no"
    conn.execute("UPDATE quote_receipt_v1 SET payload=?", (json.dumps(payload),))
    venue_settlement.observe(conn, fetchers=_closed(0.28))
    assert conn.execute("SELECT settle_price FROM paper_venue_settlement").fetchone()[0] == 0.72


def test_postponed_settlement_is_not_calibration_evidence():
    from prediction_market_soccer.ops import paper_trading
    conn = _db(kickoff="2026-10-21T18:00:00+00:00")
    venue_settlement.observe(conn, fetchers=_closed(0.28))
    paper_store.settle(conn)
    epoch = {"epoch_id": "ep", "manifest": {"compatible_epoch_ids": []}}
    assert paper_trading._calibration_records(conn, epoch) == []
