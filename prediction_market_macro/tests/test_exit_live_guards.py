"""Live exit timing and liquidity guards, with fake time and no exchange calls."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import exits, risk, trading_kalshi


START = datetime(2026, 9, 10, 12, 19, 58, tzinfo=timezone.utc)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    clock = {"now": START}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(exits, "datetime", Clock)
    monkeypatch.setattr(risk, "breaker_tripped", lambda *args: None)
    mirrored = []
    monkeypatch.setattr(trading_kalshi, "on_fill", lambda conn, fid: mirrored.append(fid))
    c = init_db(tmp_path / "exits.db")
    yield c, clock, mirrored
    c.close()


def seed(c, did=1, *, side="yes", status="active", close=None, quote_ts=None,
         bid=.78, ask=.80, bid_depth=500, ask_depth=500):
    ticker = f"T{did}"
    ts = START.isoformat()
    c.execute("INSERT INTO decisions(id,ts_utc,series,period,structure_json,kind,"
              "inputs_json,model_version,gate_snapshot,size_usd) VALUES(?,?,'KXPCECORE',"
              "'2026-09','{}','open','{}','pce/0.1.0','{}',.50)", (did, ts))
    c.execute("INSERT INTO fills(decision_id,ts_utc,ticker,side,price,count,fee_usd)"
              " VALUES(?,?,?,?,.50,1,.01)", (did, ts, ticker, side))
    c.execute("INSERT INTO contracts(ticker,series,event_ticker,period,floor_strike,"
              "strike_type,status,close_time,first_seen_ts) VALUES(?,'KXPCECORE',"
              "'KXPCECORE-26SEP','26SEP',.1,'greater',?,?,?)",
              (ticker, status, close or (START + timedelta(days=1)).isoformat(), ts))
    c.execute("INSERT INTO quotes VALUES(?,?,?,?,?,?)",
              (quote_ts or ts, ticker, bid, ask, bid_depth, ask_depth))
    c.execute("INSERT OR IGNORE INTO preds(series,period,asof,model_version,dist_json,"
              "ladder_json,data_horizon,created_ts) VALUES('KXPCECORE','2026-09',?,"
              "'pce/0.1.0','{}','{\"0.0\":0.8,\"0.2\":0.2}',?,?)", (ts, ts, ts))
    c.commit()


def freeze_at_1220(c):
    c.execute("INSERT INTO releases(cal,period,scheduled_ts) VALUES('BEA_PCE',"
              "'2026-09','2026-09-10T12:30:00+00:00')")
    c.commit()


@pytest.mark.parametrize("status", [None, "inactive", "initialized", "closed", "settled"])
def test_exit_requires_explicit_active_contract(env, status):
    c, _, mirrored = env
    seed(c, status=status)
    assert exits.run(c, SimpleNamespace()) == 0
    assert not mirrored


@pytest.mark.parametrize("close", [None, "bad-time", "2026-09-11T12:00:00", START.isoformat()])
def test_exit_requires_verified_future_close(env, close):
    c, _, mirrored = env
    seed(c)
    c.execute("UPDATE contracts SET close_time=?", (close,))
    c.commit()
    assert exits.run(c, SimpleNamespace()) == 0
    assert not mirrored


def test_missing_contract_blocks_even_a_forced_exit(env, monkeypatch):
    c, _, mirrored = env
    seed(c)
    c.execute("DELETE FROM contracts")
    c.commit()
    monkeypatch.setattr(risk, "breaker_tripped", lambda *args: "red")
    assert exits.run(c, SimpleNamespace()) == 0
    assert not mirrored


def test_first_exit_delay_cannot_push_second_exit_into_freeze(env, monkeypatch):
    c, clock, mirrored = env
    seed(c, 1)
    seed(c, 2)
    freeze_at_1220(c)

    def slow_mirror(conn, fid):
        mirrored.append(fid)
        clock["now"] += timedelta(seconds=3)

    monkeypatch.setattr(trading_kalshi, "on_fill", slow_mirror)
    assert exits.run(c, SimpleNamespace()) == 1
    closed = c.execute("SELECT closes_decision_id FROM decisions WHERE kind='exit'").fetchall()
    assert [r[0] for r in closed] == [1]
    assert len(mirrored) == 1


@pytest.mark.parametrize("boundary", ["freeze", "close", "quote_age", "inactive", "empty_book"])
def test_write_rechecks_after_decision_work_crosses_a_boundary(env, monkeypatch, boundary):
    c, clock, mirrored = env
    qts = (START - timedelta(minutes=20) + timedelta(seconds=1)).isoformat()
    seed(c, quote_ts=qts if boundary == "quote_age" else None,
         close=(START + timedelta(seconds=1)).isoformat() if boundary == "close" else None)
    if boundary == "freeze":
        freeze_at_1220(c)
    original = exits.hold_state

    def delayed_state(conn, pos, now=None):
        state = original(conn, pos, now)
        assert state is not None
        clock["now"] += timedelta(seconds=3)
        if boundary == "inactive":
            conn.execute("UPDATE contracts SET status='inactive'")
        elif boundary == "empty_book":
            conn.execute("UPDATE quotes SET bid_depth=0")
        return state

    monkeypatch.setattr(exits, "hold_state", delayed_state)
    assert exits.run(c, SimpleNamespace()) == 0
    assert not mirrored
    assert c.execute("SELECT COUNT(*) FROM decisions WHERE kind='exit'").fetchone()[0] == 0


@pytest.mark.parametrize("side", ["yes", "no"])
@pytest.mark.parametrize("bid_value,depth", [(0, 100), (.01, 100), (.20, 0), (.20, -1)])
def test_forced_exit_requires_sell_side_liquidity(env, monkeypatch, side, bid_value, depth):
    c, _, mirrored = env
    seed(c, side=side, bid=bid_value if side == "yes" else .10,
         ask=1 - bid_value if side == "no" else .90,
         bid_depth=depth if side == "yes" else 100,
         ask_depth=depth if side == "no" else 100)
    monkeypatch.setattr(risk, "breaker_tripped", lambda *args: "red")
    assert exits.run(c, SimpleNamespace()) == 0
    assert not mirrored


@pytest.mark.parametrize("side", ["yes", "no"])
@pytest.mark.parametrize("one_sided", [False, True])
def test_forced_exit_can_sell_into_wide_or_one_sided_valid_bid(env, monkeypatch, side, one_sided):
    c, _, mirrored = env
    seed(c, side=side, bid=None if side == "no" and one_sided else .20,
         ask=None if side == "yes" and one_sided else .80)
    monkeypatch.setattr(risk, "breaker_tripped", lambda *args: "red")
    assert exits.run(c, SimpleNamespace()) == 1
    fill = c.execute("SELECT side,price FROM fills WHERE side LIKE 'close_%'").fetchone()
    assert fill["side"] == f"close_{side}" and fill["price"] == pytest.approx(.19)
    assert len(mirrored) == 1


def test_shadow_does_not_record_an_exit_if_calculation_crosses_freeze(env, monkeypatch):
    c, clock, mirrored = env
    seed(c)
    freeze_at_1220(c)
    original = exits.hold_state

    def delayed_state(conn, pos, now=None):
        state = original(conn, pos, now)
        clock["now"] += timedelta(seconds=3)
        return state

    monkeypatch.setattr(exits, "hold_state", delayed_state)
    assert exits.shadow_run(c, SimpleNamespace()) == 0
    assert c.execute("SELECT COUNT(*) FROM shadow_exits").fetchone()[0] == 0
    assert not mirrored
