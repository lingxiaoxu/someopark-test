"""A recent bookkeeping timestamp cannot turn missing/old quotes into a market value."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import frontend_export as fx


@pytest.fixture()
def book(tmp_path):
    conn = init_db(tmp_path / "test.db")
    now = datetime.now(timezone.utc)
    return conn, now, SimpleNamespace(output_dir=tmp_path, frontend_data=tmp_path)


def _position(conn, now, name, status, quote_age=30):
    ticker = f"KXCPI-26AUG-{name}"
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc,series,period,structure_json,kind,fair,ask,size_usd,"
        "inputs_json,model_version,gate_snapshot) VALUES(?,'KXCPI','2026-08',?,'open',.6,.4,.4,'{}','test','{}')",
        (now.isoformat(), json.dumps({"desc": name, "legs": [{"ticker": ticker}]})))
    did = cur.lastrowid
    conn.execute("INSERT INTO fills(decision_id,ts_utc,ticker,side,price,count,fee_usd)"
                 " VALUES(?,?,?,'yes',.4,1,.02)", (did, now.isoformat(), ticker))
    if status != "absent":
        conn.execute("INSERT INTO marks(ts,decision_id,ticker,mid,pnl_usd,quote_ts,mark_status)"
                     " VALUES(?,?,?,.6,.18,?,?)", (now.isoformat(), did, ticker,
                     (now-timedelta(seconds=quote_age)).isoformat() if status else None, status))
    conn.commit()
    return did


def test_all_consumers_expose_unavailable_marks_and_preserve_fee_carry(book):
    conn, now, settings = book
    ids = [_position(conn, now, "fresh", "marked"),
           _position(conn, now, "old", "marked", 21*3600),
           _position(conn, now, "legacy", None),
           _position(conn, now, "missing", "absent")]
    fx.run(conn, settings)
    read = lambda name: json.loads((settings.frontend_data / name).read_text())
    dec = read("macro_decisions.json")
    marks = {m["decision_id"]: m for m in dec["latest_marks"]}
    assert [marks[i]["mark_status"] for i in ids] == ["marked", "stale", "unverified", "missing"]
    assert marks[ids[0]]["pnl_usd"] == .18
    for i in ids[1:]:
        assert marks[i]["mid"] is None
        assert marks[i]["pnl_usd"] == -.02
    assert dec["valuation"]["n_unmarked"] == 3
    bets = read("macro_bets.json")
    perf = read("macro_performance.json")
    assert bets["valuation"]["n_unmarked"] == perf["valuation"]["n_unmarked"] == 3
    assert sum(b["unrealized"] for b in bets["open_bets"]) == pytest.approx(.12)
    assert perf["unrealized_usd"] == .12
    assert all("quote_ts" in b["valuation"] for b in bets["open_bets"])
    assert perf["track"]["live"]["open"]["staked"] == 1.68  # entry fees unchanged
    assert all("valuation" in p for p in perf["track"]["live"]["open"]["positions"])
    track = read("macro_pricetrack.json")
    assert track["track"][0]["pnl_usd"] is None
    assert track["track"][0]["carrying_pnl_usd"] == .54  # recorded history retained
    assert track["track"][0]["n_unmarked"] == 2
    # Export cannot repair the historical record by rewriting it.
    assert conn.execute("SELECT pnl_usd FROM marks WHERE decision_id=?", (ids[1],)).fetchone()[0] == .18


def test_closed_positions_do_not_survive_in_latest_marks(book):
    conn, now, _ = book
    did = _position(conn, now, "closed", "marked")
    conn.execute("INSERT INTO decisions(ts_utc,series,period,structure_json,kind,inputs_json,"
                 "model_version,gate_snapshot,closes_decision_id)"
                 " VALUES(?,'KXCPI','2026-08','{}','exit','{}','test','{}',?)", (now.isoformat(), did))
    conn.commit()
    assert fx.current_mark_rows(conn, now) == []


def test_pdf_uses_same_provenance_and_does_not_turn_null_mid_into_zero(book):
    from prediction_market_macro.ops import report
    conn, now, settings = book
    _position(conn, now, "old", "marked", 21*3600)
    conn.execute("INSERT INTO coverage VALUES('KXCPI','2026-08','decided',?)", (now.isoformat(),))
    rows = report._open_marks(conn)
    assert rows[0]["mark_status"] == "stale" and rows[0]["mid"] is None
    story = []
    report._story_common(conn, settings, story, (now-timedelta(days=1)).isoformat())
    strings = []
    for item in story:
        if hasattr(item, "getPlainText"):
            strings.append(item.getPlainText())
        for row in getattr(item, "_cellvalues", []):
            strings.extend(cell.getPlainText() for cell in row if hasattr(cell, "getPlainText"))
    text = " ".join(strings)
    assert "1/1 legs cannot be valued" in text
    assert "stale" in text and "carry PnL $" in text


@pytest.mark.parametrize("side,action,basis,valid", [
    ("yes", "buy", "legacy_yes_ask", True),
    ("no", "buy", "legacy_yes_ask", False),
    ("yes", "sell", "legacy_yes_ask", False),
    ("no", "sell", "legacy_yes_ask", False),
    ("no", "buy", "side_action_v1", True),
    ("no", "sell", "side_action_v1", True),
])
def test_demo_reference_must_match_historical_side_and_action(side, action, basis, valid):
    raw = {"side": side, "action": action, "prod_price_basis": basis, "status": "dryrun",
           "prod_ask_at_send": .2, "avg_price": .2, "fee_usd": .01}
    view = fx._demo_order_view(raw)
    assert view["reference_valid"] is valid
    assert view["invalid_legacy_reference"] is not valid
    assert view["price_is_estimate"] and view["fee_is_estimate"]
    assert view["avg_price"] == (.2 if valid else None)
    assert view["fee_usd"] == (.01 if valid else None)
    assert raw["prod_ask_at_send"] == .2  # no in-place or database mutation


def test_export_excludes_invalid_dark_estimates_from_diff_aggregate(book):
    conn, _, settings = book
    for i, side, basis in [(1, "no", "legacy_yes_ask"), (2, "no", "side_action_v1"),
                            (3, "yes", "legacy_yes_ask")]:
        conn.execute("INSERT INTO demo_orders(fill_id,decision_id,client_order_id,ticker,side,action,"
                     "count_target,paper_ask,prod_ask_at_send,avg_price,fee_usd,status,prod_price_basis)"
                     " VALUES(?,1,?,'X',?,'buy',1,.3,.4,.45,.02,'dryrun',?)", (i, f"test{i}", side, basis))
    conn.commit()
    fx.run_extended(conn, settings)
    doc = json.loads((settings.frontend_data / "macro_demo_exec.json").read_text())
    assert doc["diff_cents"]["n_invalid_legacy"] == 1
    assert doc["diff_cents"]["latency"] == {"n": 2, "mean": 10.0}
    assert doc["diff_cents"]["venue"] == {"n": 2, "mean": 5.0}
    assert doc["orders"][-1]["avg_price"] is None
    assert conn.execute("SELECT avg_price FROM demo_orders WHERE fill_id=1").fetchone()[0] == .45


def test_demo_position_export_keeps_quote_provenance_and_cost_carry(book):
    conn, now, settings = book
    quote_ts = (now-timedelta(hours=21)).isoformat()
    conn.execute("INSERT INTO demo_fills(fill_id,ticker,side,action,price,count,fee_usd,ts)"
                 " VALUES(1,'DEMO-NO','no','buy',.3,2,.02,?)", (now.isoformat(),))
    conn.execute("INSERT INTO quotes VALUES(?,'DEMO-NO',.2,.3,10,10)", (quote_ts,))
    conn.commit()
    fx.run_extended(conn, settings)
    doc = json.loads((settings.frontend_data / "macro_demo_exec.json").read_text())
    position = doc["positions"][0]
    assert position["mark"] is None
    assert position["mark_status"] == "stale"
    assert position["quote_ts"] == quote_ts
    assert position["quote_max_age_seconds"] == 1200
    assert position["cost"] == position["mtm"] == .6
    assert doc["positions_valuation"]["n_unmarked"] == 1
