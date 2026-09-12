"""Regressions for live quote provenance, guarded exits and held-book maintenance."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ingest.kalshi_md import KalshiMD, OB
from prediction_market_macro.jobs import tick
from prediction_market_macro.ops import exits, pnl, predict_all, trading_kalshi
from prediction_market_macro.util.quotes import MAX_QUOTE_AGE_SECONDS, quote_status

NOW = datetime(2026, 9, 10, 5, 0, tzinfo=timezone.utc)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.fixture
def conn(tmp_path, monkeypatch):
    for mod in (pnl, exits, tick):
        monkeypatch.setattr(mod, "datetime", Clock)
    c = init_db(tmp_path / "test.db")
    yield c
    c.close()


def seed(c, *, age=0, model="pce/0.1.0", close=None):
    ts = NOW.isoformat()
    c.execute("INSERT INTO decisions(id,ts_utc,series,period,structure_json,kind,"
              "inputs_json,model_version,gate_snapshot) VALUES(1,?,'KXPCECORE',"
              "'2026-09','{}','open','{}','pce/0.1.0','{}')", (ts,))
    c.execute("INSERT INTO fills(decision_id,ts_utc,ticker,side,price,count,fee_usd)"
              " VALUES(1,?,'T1','yes',0.5,1,0.01)", (ts,))
    c.execute("INSERT INTO contracts(ticker,series,event_ticker,period,floor_strike,"
              "strike_type,status,close_time,first_seen_ts) VALUES('T1','KXPCECORE',"
              "'KXPCECORE-26SEP','26SEP',0.1,'greater','active',?,?)",
              (close or (NOW + timedelta(days=20)).isoformat(), ts))
    c.execute("INSERT INTO preds(series,period,asof,model_version,dist_json,"
              "ladder_json,data_horizon,created_ts) VALUES('KXPCECORE','2026-09',"
              "?,?,'{}','{\"0.0\":0.8,\"0.2\":0.2}',?,?)", (ts, model, ts, ts))
    qts = (NOW - timedelta(seconds=age)).isoformat()
    c.execute("INSERT INTO quotes VALUES(?,'T1',0.78,0.80,500,500)", (qts,))
    c.commit()
    return qts


@pytest.mark.parametrize("age,expected", [(0,"marked"), (MAX_QUOTE_AGE_SECONDS,"marked"),
                                         (MAX_QUOTE_AGE_SECONDS+1,"stale"), (-1,"stale")])
def test_freshness_boundary(age, expected):
    assert quote_status({"ts":(NOW-timedelta(seconds=age)).isoformat(),
                         "yes_bid":.4,"yes_ask":.42},NOW)==expected


def test_old_quotes_never_receive_a_fresh_valuation(conn):
    qts = seed(conn, age=20*3600)
    pnl.mark_all(conn)
    m = conn.execute("SELECT * FROM marks").fetchone()
    assert m["ts"] == NOW.isoformat() and m["quote_ts"] == qts
    assert m["mark_status"] == "stale" and m["mid"] is None
    assert m["pnl_usd"] == -.01  # existing cost-carrying/fee convention
    assert conn.execute("SELECT count(*) FROM alerts").fetchone()[0] == 1


def test_fresh_quotes_keep_numeric_marks_and_source_time(conn):
    qts = seed(conn)
    pnl.mark_all(conn)
    m = conn.execute("SELECT * FROM marks").fetchone()
    assert m["quote_ts"] == qts and m["mark_status"] == "marked"
    assert m["mid"] == pytest.approx(.79) and m["pnl_usd"] == pytest.approx(.28)


@pytest.mark.parametrize("cause", ["stale", "future", "closed", "shadow_only", "stale_pred"])
def test_unusable_inputs_cannot_execute_exits(conn, monkeypatch, cause):
    seed(conn, age=20*3600 if cause=="stale" else -10 if cause=="future" else 0,
         model="dfm/1" if cause=="shadow_only" else "pce/0.1.0",
         close=NOW.isoformat() if cause=="closed" else None)
    if cause=="stale_pred":
        conn.execute("UPDATE preds SET asof=?", ((NOW-timedelta(days=3)).isoformat(),))
    monkeypatch.setattr(trading_kalshi,"on_fill",lambda *a:pytest.fail("must not mirror"))
    assert exits.run(conn,SimpleNamespace())==0
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0]==1


def test_fresh_production_model_drives_exit_even_if_shadow_is_newer(conn, monkeypatch):
    seed(conn)
    ts = (NOW+timedelta(seconds=1)).isoformat()
    conn.execute("INSERT INTO preds(series,period,asof,model_version,dist_json,ladder_json,"
                 "data_horizon,created_ts) VALUES('KXPCECORE','2026-09',?,'dfm/1','{}',"
                 "'{\"0.2\":1}',?,?)", (ts,ts,ts))
    monkeypatch.setattr(trading_kalshi,"on_fill",lambda *a:None)
    assert exits.run(conn,SimpleNamespace())==1


def test_forced_exit_also_refuses_stale_quotes(conn,monkeypatch):
    from prediction_market_macro.ops import risk
    seed(conn,age=20*3600)
    monkeypatch.setattr(risk,"breaker_tripped",lambda *a:"test breaker")
    assert exits.run(conn,SimpleNamespace())==0


def test_targeted_snapshot_isolates_failure_and_preserves_old_timestamp(conn,monkeypatch):
    old = seed(conn,age=20*3600)
    md = KalshiMD(conn, spacing=0)
    calls=[]
    def ob(ticker,**kwargs):
        calls.append((ticker,kwargs))
        if ticker=="T1": raise OSError("offline")
        return OB(.4,.42,100,100)
    monkeypatch.setattr(md,"orderbook",ob)
    monkeypatch.setattr(md,"market",lambda ticker,**kwargs:{
        "ticker":ticker,"status":"active","close_time":"2099-01-01T00:00:00Z"})
    out=md.snapshot_tickers(["T1","T2","T2"])
    assert out["refreshed"]==["T2"] and "T1" in out["failed"]
    assert conn.execute("SELECT max(ts) FROM quotes WHERE ticker='T1'").fetchone()[0]==old
    assert len(calls)==2 and all(k["tries"]==1 and k["timeout"]==5 for _,k in calls)


def test_position_cycle_fetches_before_predict_exit_mark_and_export(conn,monkeypatch):
    seed(conn)
    calls=[]
    class MD:
        def snapshot_tickers(self,tickers,**kw):
            assert tickers=={"T1"};calls.append("quotes")
            return {"refreshed":["T1"],"failed":{}}
    monkeypatch.setattr(tick,"_drain_freezes",lambda *a:None)
    monkeypatch.setattr(predict_all,"run",lambda *a,**kw:calls.append("preds"))
    monkeypatch.setattr(exits,"shadow_run",lambda *a,**kw:calls.append("shadow"))
    monkeypatch.setattr(exits,"run",lambda *a,**kw:calls.append("exit"))
    monkeypatch.setattr(pnl,"mark_all",lambda *a:calls.append("mark"))
    monkeypatch.setattr(trading_kalshi,"sync",lambda *a:calls.append("sync"))
    monkeypatch.setattr(tick,"_export_frontend",lambda *a:calls.append("export"))
    tick._maintain_positions(conn,SimpleNamespace(),MD())
    assert calls[:5]==["quotes","preds","shadow","exit","mark"]
    assert "export" in calls and "sync" in calls


def test_schema_migration_preserves_historical_unknown_provenance(tmp_path):
    path=tmp_path/"old.db"
    c=init_db(path)
    c.execute("INSERT INTO marks(ts,decision_id,ticker,mid,pnl_usd) VALUES('old',1,'T',.5,.2)")
    for col in ("quote_ts","mark_status"):
        c.execute(f"ALTER TABLE marks DROP COLUMN {col}")
    c.execute("ALTER TABLE demo_orders DROP COLUMN prod_price_basis")
    c.commit();c.close()
    for _ in range(2):
        c=init_db(path)
        row=c.execute("SELECT * FROM marks").fetchone()
        assert row["mid"]==.5 and row["quote_ts"] is None and row["mark_status"] is None
        assert "prod_price_basis" in {r[1] for r in c.execute("PRAGMA table_info(demo_orders)")}
        c.close()
