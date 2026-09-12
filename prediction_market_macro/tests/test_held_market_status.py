"""Held maintenance observes venue closures before considering cached prices."""
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest import kalshi_md
from prediction_market_macro.ingest.kalshi_md import KalshiMD, OB
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops.exits import _closed_book

NOW = datetime(2026, 9, 10, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(kalshi_md, 'datetime', Clock)
    conn = init_db(tmp_path / 'held.db')
    for ticker in ('T1','T2'):
        conn.execute("INSERT INTO contracts(ticker,series,event_ticker,period,status,close_time,first_seen_ts)"
                     " VALUES(?,'KXCPI','E','26AUG','active',?,'original')",
                     (ticker,(NOW+timedelta(days=1)).isoformat()))
        conn.execute('INSERT INTO quotes VALUES(?,?,.78,.80,100,100)',
                     ((NOW-timedelta(minutes=1)).isoformat(),ticker))
    conn.commit()
    yield conn, KalshiMD(conn,spacing=0)
    conn.close()


@pytest.mark.parametrize('status,close',[('closed',NOW+timedelta(days=1)),
                                        ('active',NOW-timedelta(seconds=1))])
def test_venue_early_close_blocks_exit_despite_recent_cached_quote(setup,monkeypatch,status,close):
    conn,md=setup
    monkeypatch.setattr(md,'market',lambda ticker,**kw:{'ticker':ticker,'status':status,'close_time':close.isoformat()})
    monkeypatch.setattr(md,'orderbook',lambda *a,**kw:pytest.fail('Do not fetch a closed book'))
    result=md.snapshot_tickers(['T1'])
    assert result['refreshed']==[] and 'T1' in result['failed']
    assert _closed_book(conn,{'fills':[{'ticker':'T1'}]},NOW)
    assert conn.execute("SELECT max(ts) FROM quotes WHERE ticker='T1'").fetchone()[0]==(NOW-timedelta(minutes=1)).isoformat()


def test_metadata_failure_isolated_and_not_eligible_for_cycle(setup,monkeypatch):
    conn,md=setup
    def market(ticker,**kw):
        assert kw=={'tries':1,'timeout':5}
        if ticker=='T1': raise OSError('metadata unavailable')
        return {'ticker':ticker,'status':'active','close_time':(NOW+timedelta(hours=1)).isoformat()}
    monkeypatch.setattr(md,'market',market)
    monkeypatch.setattr(md,'orderbook',lambda *a,**kw:OB(.1,.12,500,500))
    result=md.snapshot_tickers(['T1','T2'])
    assert result['refreshed']==['T2'] and set(result['failed'])=={'T1'}
    c=conn.execute("SELECT * FROM contracts WHERE ticker='T2'").fetchone()
    assert c['close_time']==(NOW+timedelta(hours=1)).isoformat() and c['first_seen_ts']=='original'
    assert conn.execute("SELECT max(ts) FROM quotes WHERE ticker='T1'").fetchone()[0]==(NOW-timedelta(minutes=1)).isoformat()
    assert conn.execute("SELECT max(ts) FROM quotes WHERE ticker='T2'").fetchone()[0]==NOW.isoformat()


def test_market_endpoint_rejects_wrong_ticker(setup,monkeypatch):
    _,md=setup
    monkeypatch.setattr(kalshi_md,'_get',lambda *a,**kw:{'market':{'ticker':'OTHER'}})
    with pytest.raises(ValueError,match='invalid market metadata'):
        md.market('T1',tries=1,timeout=5)
