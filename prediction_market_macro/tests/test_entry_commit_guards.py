"""Paper structures commit together; live guards run after the SQLite writer wait."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import decide_all, ledger, risk, trading_kalshi
from prediction_market_macro.strategy import arb
from prediction_market_macro.strategy.decision import Decision, GATES
from prediction_market_macro.strategy.edge import Leg, Struct

START = datetime(2026, 9, 10, 12, 19, 58, tzinfo=timezone.utc)
SERIES, PERIOD = "KXCPI", "2026-08"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "entry.db"
    conn = init_db(path)
    clock = [START]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    for module in (ledger, decide_all, arb):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(risk, "check", lambda *args: None)
    monkeypatch.setattr(risk, "breaker_tripped", lambda *args: None)
    monkeypatch.setattr(trading_kalshi, "on_fill", lambda *args: None)
    for ticker in ("KXCPI-26AUG-T0.1", "KXCPI-26AUG-T0.2"):
        conn.execute(
            "INSERT INTO contracts(ticker,series,event_ticker,period,status,close_time,"
            "first_seen_ts) VALUES(?,?,?,?,?,?,?)",
            (ticker, SERIES, "KXCPI-26AUG", "26AUG", "active",
             (START + timedelta(days=1)).isoformat(), START.isoformat()))
    conn.execute("INSERT INTO releases(cal,period,scheduled_ts) VALUES(?,?,?)",
                 (REGISTRY[SERIES].calendar, PERIOD, "2026-09-10T12:30:00+00:00"))
    conn.commit()
    yield conn, path, clock
    conn.close()


def structure():
    return Struct("bucket", (Leg("KXCPI-26AUG-T0.1", "yes", .9, 1000),
                             Leg("KXCPI-26AUG-T0.2", "no", .9, 1000)),
                  .7, .8, .8, "test bucket")


def write(conn, kind, before_write=None):
    st = structure()
    if kind == "open":
        return ledger.record(conn, series=SERIES, period=PERIOD,
                             decision=Decision("open", st, .8, 1, (), dict(GATES)),
                             pred_inputs={}, model_version="cpi/test", before_write=before_write)
    if kind == "arb":
        return arb._record(conn, SERIES, PERIOD,
                           [dict(ticker=l.ticker, side=l.side, price=l.price) for l in st.legs],
                           .1, .05, 1, "test arb", before_write=before_write)
    return decide_all._place_argmax(conn, REGISTRY[SERIES], PERIOD, [st], START,
                                    before_write=before_write)


@pytest.mark.parametrize("kind", ["open", "arb", "argmax"])
def test_all_legs_are_committed_before_first_slow_mirror(env, monkeypatch, kind):
    conn, path, clock = env
    observations = []

    def mirror(db, fid):
        with sqlite3.connect(path) as reader:
            observations.append((reader.execute("SELECT COUNT(*) FROM fills").fetchone()[0],
                                 db.in_transaction, clock[0]))
        clock[0] += timedelta(seconds=3)

    monkeypatch.setattr(trading_kalshi, "on_fill", mirror)
    assert write(conn, kind)
    assert [x[:2] for x in observations] == [(2, False), (2, False)]
    assert observations[1][2] > START + timedelta(seconds=2)
    assert {r[0] for r in conn.execute("SELECT ts_utc FROM fills")} == {START.isoformat()}


@pytest.mark.parametrize("kind", ["open", "arb", "argmax"])
def test_second_leg_failure_rolls_back_structure_and_preserves_caller_work(env, kind):
    conn, _, _ = env
    conn.executescript("CREATE TRIGGER reject_second BEFORE INSERT ON fills "
                       "WHEN NEW.ticker='KXCPI-26AUG-T0.2' BEGIN "
                       "SELECT RAISE(ABORT,'second leg rejected'); END;")
    conn.execute("INSERT INTO alerts(ts,level,source,message) VALUES('now','info','test','prior')")
    with pytest.raises(sqlite3.IntegrityError, match="second leg rejected"):
        write(conn, kind)
    assert conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM shadow_argmax").fetchone()[0] == 0
    assert conn.execute("SELECT message FROM alerts").fetchone()[0] == "prior"


@pytest.mark.parametrize("kind", ["open", "arb", "argmax"])
def test_sqlite_writer_wait_cannot_carry_entry_into_freeze(env, kind):
    conn, path, clock = env
    holder = sqlite3.connect(path)
    holder.execute("BEGIN IMMEDIATE")
    waiting = threading.Event()
    errors = []

    def worker():
        db = sqlite3.connect(path, timeout=3)
        db.row_factory = sqlite3.Row
        db.set_trace_callback(lambda sql: waiting.set() if sql.startswith("UPDATE decisions") else None)
        try:
            write(db, kind, before_write=lambda: ledger.ensure_live_entry_window(
                db, SERIES, PERIOD, [l.ticker for l in structure().legs]))
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert waiting.wait(2)
        clock[0] += timedelta(seconds=3)
    finally:
        holder.rollback()
        holder.close()
        thread.join(4)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ledger.EntryWindowClosed)
    assert str(errors[0]) == "freeze_window"
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


def test_live_arb_rechecks_after_same_book_pricing_crosses_freeze(env, monkeypatch):
    conn, _, clock = env
    for strike, bid, ask in ((.1, .60, .62), (.2, .70, .74)):
        ticker = f"KXCPI-26AUG-T{strike}"
        conn.execute("UPDATE contracts SET strike_type='greater',floor_strike=? WHERE ticker=?",
                     (strike, ticker))
        conn.execute("INSERT INTO quotes VALUES(?,?,?,?,?,?)",
                     (START.isoformat(), ticker, bid, ask, 200, 200))
    conn.commit()
    monkeypatch.setattr(decide_all, "REGISTRY", {SERIES: REGISTRY[SERIES]})
    monkeypatch.setattr(decide_all, "_warn_unevaluated_series_gate", lambda _: None)
    original = decide_all._legs_meta

    def price(*args):
        legs = original(*args)
        clock[0] += timedelta(seconds=3)
        return legs

    monkeypatch.setattr(decide_all, "_legs_meta", price)
    with pytest.raises(ledger.EntryWindowClosed, match="freeze_window"):
        decide_all.run(conn, SimpleNamespace())
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


def test_future_post_print_snipe_stops_when_first_mirror_crosses_close(env, monkeypatch):
    from prediction_market_macro.strategy import snipe
    conn, _, clock = env
    conn.execute("UPDATE releases SET scheduled_ts=?", ((START - timedelta(minutes=3)).isoformat(),))
    conn.execute("UPDATE contracts SET close_time=?", ((START + timedelta(seconds=2)).isoformat(),))
    conn.commit()
    legs = [{"ticker": leg.ticker, "strike": .1, "strike_type": "greater",
             "cap_strike": None, "yes_ask": .2, "yes_bid": .18,
             "ask_depth": 5, "bid_depth": 5} for leg in structure().legs]
    monkeypatch.setattr(snipe, "datetime", ledger.datetime)
    monkeypatch.setattr(decide_all, "_legs_meta", lambda *args: legs)
    monkeypatch.setattr("prediction_market_macro.ops.pnl._realized_print", lambda *args: 4.0)
    monkeypatch.setattr("prediction_market_macro.research.health._leg_expected", lambda *args: "yes")
    calls = []

    def mirror(db, fid):
        calls.append((fid, db.in_transaction))
        clock[0] += timedelta(seconds=3)

    monkeypatch.setattr(trading_kalshi, "on_fill", mirror)
    assert snipe.run_for(conn, SERIES, PERIOD) == 1
    assert len(calls) == 1 and calls[0][1] is False
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE kind='snipe'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    assert "SNIPE-WINDOW-CLOSED" in conn.execute("SELECT message FROM alerts ORDER BY id DESC LIMIT 1").fetchone()[0]


@pytest.mark.parametrize("crossed_freeze", [False, True])
def test_live_deferred_argmax_obeys_same_final_guard_as_placed_arm(env, crossed_freeze):
    conn, _, clock = env
    st = replace(structure(), fair=.9)
    if crossed_freeze:
        clock[0] += timedelta(seconds=3)
    checked = []

    def before_write():
        checked.append(clock[0])
        ledger.ensure_live_entry_window(conn, SERIES, PERIOD, [leg.ticker for leg in st.legs])

    def defer():
        return decide_all._place_argmax(conn, REGISTRY[SERIES], PERIOD, [st], START,
                                        before_write=before_write)

    if crossed_freeze:
        with pytest.raises(ledger.EntryWindowClosed, match="freeze_window"):
            defer()
        assert conn.execute("SELECT COUNT(*) FROM shadow_argmax").fetchone()[0] == 0
    else:
        assert defer() is False
        row = conn.execute("SELECT arm,ts_utc FROM shadow_argmax").fetchone()
        assert tuple(row) == ("deferred", START.isoformat())
    assert checked == [clock[0]]
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
