"""The quote whose edge triggered an exit must also price every written leg.

Market ingestion intentionally bypasses execution_lock. Real second connections
replace a quote between evaluation and writing; all files and mirror hooks are local.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import sqlite3

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import exits, ledger, risk, trading_kalshi


NOW = datetime(2026, 9, 10, 6, 30, tzinfo=timezone.utc)


@pytest.fixture()
def book(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    clock = {"now": NOW}
    monkeypatch.setattr(exits, "datetime", Clock)
    monkeypatch.setattr(risk, "breaker_tripped", lambda *args: None)
    mirrors = []
    monkeypatch.setattr(trading_kalshi, "on_fill", lambda db, fid: mirrors.append(fid))
    path = tmp_path / "book.db"
    conn = init_db(path)
    other = sqlite3.connect(path, timeout=.05)
    other.row_factory = sqlite3.Row
    ts = (NOW - timedelta(seconds=2)).isoformat()
    conn.execute("INSERT INTO decisions(id,ts_utc,series,period,structure_json,kind,"
                 "inputs_json,model_version,gate_snapshot) VALUES(1,?,'KXPCECORE',"
                 "'2026-09','{}','open','{}','pce/test','{}')", (ts,))
    conn.execute("INSERT INTO fills(decision_id,ts_utc,ticker,side,price,count,fee_usd)"
                 " VALUES(1,?,'T1','yes',.5,1,.01)", (ts,))
    conn.execute("INSERT INTO contracts(ticker,series,event_ticker,period,floor_strike,"
                 "strike_type,status,close_time,first_seen_ts) VALUES('T1','KXPCECORE',"
                 "'KXPCECORE-26SEP','26SEP',.1,'greater','active',?,?)",
                 ((NOW + timedelta(days=1)).isoformat(), ts))
    conn.execute("INSERT INTO preds(series,period,asof,model_version,dist_json,ladder_json,"
                 "data_horizon,created_ts) VALUES('KXPCECORE','2026-09',?,'pce/test','{}',"
                 "'{\"0.0\":0.8,\"0.2\":0.2}',?,?)", (ts, ts, ts))
    conn.execute("INSERT INTO quotes VALUES(?,'T1',.78,.80,500,500)", (ts,))
    conn.commit()
    yield conn, other, clock, mirrors
    conn.close()
    other.close()


@pytest.mark.parametrize("runner", ["run", "shadow_run"])
@pytest.mark.parametrize("change", ["edge_reversal", "depth_only", "same_timestamp_price"])
def test_concurrent_book_change_invalidates_the_entire_exit(book, monkeypatch, runner, change):
    conn, other, _, mirrors = book
    original = exits.hold_state

    def evaluate_then_ingest(db, pos, now=None):
        state = original(db, pos, now)
        assert state["hold_edge"] < exits.EXIT_EDGE
        if change == "edge_reversal":
            other.execute("INSERT INTO quotes VALUES(?,'T1',.10,.12,500,500)",
                          ((NOW-timedelta(seconds=1)).isoformat(),))
        elif change == "depth_only":
            # Five is positive, but below the live rule's minimum of twenty.
            other.execute("UPDATE quotes SET bid_depth=5")
        else:
            other.execute("UPDATE quotes SET yes_bid=.10,yes_ask=.12")
        other.commit()
        fresh = original(db, pos, now)
        if change != "depth_only":
            assert fresh["hold_edge"] == pytest.approx(.09)
        return state

    monkeypatch.setattr(exits, "hold_state", evaluate_then_ingest)
    assert getattr(exits, runner)(conn, SimpleNamespace()) == 0
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE kind='exit'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM shadow_exits").fetchone()[0] == 0
    assert mirrors == []


def test_forced_exit_also_rejects_changed_execution_price(book, monkeypatch):
    conn, other, _, mirrors = book
    monkeypatch.setattr(risk, "breaker_tripped", lambda *args: "test breaker")
    original = exits._forced_sell_quote
    calls = []

    def evaluate_then_ingest(quote, side):
        result = original(quote, side)
        if not calls:
            other.execute("INSERT INTO quotes VALUES(?,'T1',.10,.12,500,500)",
                          ((NOW-timedelta(seconds=1)).isoformat(),))
            other.commit()
        calls.append(side)
        return result

    monkeypatch.setattr(exits, "_forced_sell_quote", evaluate_then_ingest)
    assert exits.run(conn, SimpleNamespace()) == 0
    assert mirrors == []


@pytest.mark.parametrize("caller_transaction", [False, True])
def test_guard_reserves_writer_through_all_legs_then_releases_before_mirror(
        book, monkeypatch, caller_transaction):
    conn, other, clock, mirrors = book
    conn.execute("INSERT INTO fills(decision_id,ts_utc,ticker,side,price,count,fee_usd)"
                 " SELECT decision_id,ts_utc,'T2',side,price,count,fee_usd FROM fills")
    conn.execute("INSERT INTO contracts SELECT 'T2',series,event_ticker,period,sub_title,"
                 "strike_type,floor_strike,cap_strike,close_time,status,first_seen_ts FROM contracts")
    conn.execute("INSERT INTO quotes SELECT ts,'T2',yes_bid,yes_ask,bid_depth,ask_depth FROM quotes")
    conn.execute("INSERT INTO releases VALUES('BEA_PCE','2026-09',?,NULL)",
                 ((NOW+timedelta(minutes=10, seconds=1)).isoformat(),))
    conn.commit()
    if caller_transaction:
        conn.execute("INSERT INTO alerts(ts,level,source,message) VALUES('now','info','caller','keep')")
    original = exits._record_exit

    def probe_writer_reservation(*args):
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("UPDATE quotes SET bid_depth=5")
        other.rollback()
        return original(*args)

    def slow_mirror(db, fid):
        # The first slow HTTP-equivalent hook sees the whole committed package.
        rows = other.execute("SELECT ts_utc FROM fills WHERE side='close_yes'").fetchall()
        assert len(rows) == 2 and all(r[0] == NOW.isoformat() for r in rows)
        assert not db.in_transaction
        other.execute("UPDATE quotes SET bid_depth=5")
        other.commit()
        mirrors.append(fid)
        clock["now"] += timedelta(seconds=3)  # now inside the freeze

    monkeypatch.setattr(exits, "_record_exit", probe_writer_reservation)
    monkeypatch.setattr(trading_kalshi, "on_fill", slow_mirror)
    assert exits.run(conn, SimpleNamespace()) == 1
    assert len(mirrors) == 2


def test_rejected_exit_preserves_callers_write_transaction(book):
    conn, other, _, mirrors = book
    pos = ledger.open_positions(conn)[0]
    state = exits.hold_state(conn, pos, NOW)
    conn.execute("INSERT INTO alerts(ts,level,source,message) VALUES('now','info','caller','keep')")
    conn.execute("UPDATE quotes SET bid_depth=5")
    assert not exits._write_exit(conn, pos, NOW.isoformat(), state["hold_edge"],
                                 state["legs_exit"], "test", quote_versions=state["quote_versions"])
    assert conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE source='caller'").fetchone()[0] == 1
    assert other.execute("SELECT COUNT(*) FROM alerts WHERE source='caller'").fetchone()[0] == 0
    conn.rollback()
    assert mirrors == []


def test_stale_callers_read_snapshot_is_skipped_without_rollback_or_deadlock(book):
    conn, other, _, mirrors = book
    conn.execute("BEGIN")
    pos = ledger.open_positions(conn)[0]
    state = exits.hold_state(conn, pos, NOW)
    other.execute("UPDATE quotes SET bid_depth=5")
    other.commit()
    assert not exits._write_exit(conn, pos, NOW.isoformat(), state["hold_edge"],
                                 state["legs_exit"], "test", quote_versions=state["quote_versions"])
    assert conn.in_transaction
    conn.rollback()
    assert mirrors == []


@pytest.mark.parametrize("caller_transaction", [False, True])
def test_failed_second_leg_rolls_back_only_the_exit(book, caller_transaction):
    conn, _, _, mirrors = book
    conn.execute("INSERT INTO fills(decision_id,ts_utc,ticker,side,price,count,fee_usd)"
                 " SELECT decision_id,ts_utc,'T2',side,price,count,fee_usd FROM fills")
    conn.execute("INSERT INTO contracts SELECT 'T2',series,event_ticker,period,sub_title,"
                 "strike_type,floor_strike,cap_strike,close_time,status,first_seen_ts FROM contracts")
    conn.execute("INSERT INTO quotes SELECT ts,'T2',yes_bid,yes_ask,bid_depth,ask_depth FROM quotes")
    conn.execute("CREATE TRIGGER fail_second_exit BEFORE INSERT ON fills "
                 "WHEN NEW.side='close_yes' AND NEW.ticker='T2' "
                 "BEGIN SELECT RAISE(ABORT,'second leg failed'); END")
    conn.commit()
    if caller_transaction:
        conn.execute("INSERT INTO alerts(ts,level,source,message) VALUES('now','info','caller','keep')")
    with pytest.raises(sqlite3.IntegrityError, match="second leg failed"):
        exits.run(conn, SimpleNamespace())
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE kind='exit'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fills WHERE side='close_yes'").fetchone()[0] == 0
    assert conn.in_transaction is caller_transaction
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE source='caller'").fetchone()[0] == int(caller_transaction)
    conn.rollback()
    assert mirrors == []


@pytest.mark.parametrize("runner", ["run", "shadow_run"])
def test_failed_held_refresh_cannot_reuse_an_age_fresh_cached_quote(book, runner):
    conn, _, _, mirrors = book
    assert exits.hold_state(conn, ledger.open_positions(conn)[0], NOW) is not None
    assert getattr(exits, runner)(conn, SimpleNamespace(), eligible_tickers=set()) == 0
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE kind='exit'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM shadow_exits").fetchone()[0] == 0
    assert mirrors == []
