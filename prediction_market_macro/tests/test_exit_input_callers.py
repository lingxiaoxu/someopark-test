"""Every live caller requires fresh held metadata, even with age-fresh cached quotes."""
from types import SimpleNamespace

import pytest

from prediction_market_macro.jobs import scheduler, tick
from prediction_market_macro.ops import decide_all, exits, pnl, position_inputs, predict_all, refresh, trading_kalshi
from prediction_market_macro.tests.test_exit_quote_consistency import NOW, book


def _quiet_event(monkeypatch):
    monkeypatch.setattr(tick, '_top_up_stale_quotes', lambda *args: {})
    monkeypatch.setattr(tick, '_drain_freezes', lambda *args: 0)
    monkeypatch.setattr(tick, '_export_frontend', lambda *args: None)
    monkeypatch.setattr(scheduler, 'expire_if_overdue', lambda *args: False)
    monkeypatch.setattr(decide_all, 'run', lambda *args: None)
    def predict(*args, fail_on_error):
        assert fail_on_error is True
    monkeypatch.setattr(predict_all, 'run', predict)
    monkeypatch.setattr(pnl, 'mark_all', lambda *args: None)
    monkeypatch.setattr(trading_kalshi, 'sync', lambda *args: None)


@pytest.mark.parametrize('failure', ['metadata', 'whole_pass'])
def test_event_caller_does_not_exit_cached_book_after_held_refresh_failure(book, monkeypatch, failure):
    conn, _, _, mirrors = book
    _quiet_event(monkeypatch)
    calls = []

    def held(tickers, before_each=None):
        calls.append(set(tickers))
        if failure == 'whole_pass':
            raise OSError('held refresh unavailable')
        return {'refreshed': [], 'failed': {'T1': 'metadata timeout'}}

    md = SimpleNamespace(snapshot_series=lambda *args: None, snapshot_tickers=held)
    tick._exec_task(conn, SimpleNamespace(), md,
                    {'task': 'decide', 'series': 'KXPCECORE', 'period': '2026-09'})
    assert calls == [{'T1'}]
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE kind='exit'").fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM shadow_exits').fetchone()[0] == 0
    assert mirrors == []
    # The fixture would really exit under the old unfiltered caller.
    assert exits.run(conn, SimpleNamespace()) == 1


def test_daily_exit_steps_remain_ineligible_when_step_swallows_input_exception(book, monkeypatch):
    conn, _, _, mirrors = book
    def broken(*args):
        raise OSError('metadata unavailable')
    monkeypatch.setattr(position_inputs, 'fresh_held_tickers', broken)
    steps = []
    def step(name, fn):
        steps.append(name)
        try:
            fn()
        except OSError:
            pass  # refresh._run's existing best-effort step contract
    refresh._run_exit_steps(conn, SimpleNamespace(), None, step)
    assert steps == ['held_exit_inputs', 's2_shadow', 'exits']
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE kind='exit'").fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM shadow_exits').fetchone()[0] == 0
    assert mirrors == []


def test_daily_exit_steps_refresh_once_then_shadow_before_live(book):
    conn, _, _, mirrors = book
    calls = []
    def snapshot(tickers, **kwargs):
        calls.append(set(tickers))
        return {'refreshed': ['T1'], 'failed': {}}
    def step(name, fn):
        calls.append(name)
        return fn()
    refresh._run_exit_steps(conn, SimpleNamespace(), SimpleNamespace(snapshot_tickers=snapshot), step)
    assert calls == ['held_exit_inputs', {'T1'}, 's2_shadow', 'exits']
    assert conn.execute('SELECT COUNT(*) FROM shadow_exits').fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE kind='exit'").fetchone()[0] == 1
    assert len(mirrors) == 1


def test_a_failed_leg_blocks_whole_position_without_blocking_another_position(book):
    conn, _, _, mirrors = book
    conn.execute("INSERT INTO fills(decision_id,ts_utc,ticker,side,price,count,fee_usd)"
                 " SELECT decision_id,ts_utc,'T2',side,price,count,fee_usd FROM fills")
    conn.execute("INSERT INTO contracts SELECT 'T2',series,event_ticker,period,sub_title,"
                 "strike_type,floor_strike,cap_strike,close_time,status,first_seen_ts FROM contracts")
    conn.execute("INSERT INTO quotes SELECT ts,'T2',yes_bid,yes_ask,bid_depth,ask_depth FROM quotes")
    conn.execute("INSERT INTO decisions(ts_utc,series,period,structure_json,kind,inputs_json,"
                 "model_version,gate_snapshot) SELECT ts_utc,series,period,structure_json,kind,"
                 "inputs_json,model_version,gate_snapshot FROM decisions WHERE id=1")
    other_id = conn.execute('SELECT max(id) FROM decisions').fetchone()[0]
    conn.execute("INSERT INTO fills(decision_id,ts_utc,ticker,side,price,count,fee_usd)"
                 " SELECT ?,ts_utc,ticker,side,price,count,fee_usd FROM fills WHERE ticker='T1'", (other_id,))
    conn.commit()
    md = SimpleNamespace(snapshot_tickers=lambda *a, **kw:
                         {'refreshed': ['T1'], 'failed': {'T2': 'metadata unavailable'}})
    eligible = position_inputs.fresh_held_tickers(conn, md)
    assert eligible == {'T1'}
    assert exits.run(conn, SimpleNamespace(), eligible_tickers=eligible) == 1
    closed = conn.execute("SELECT closes_decision_id FROM decisions WHERE kind='exit'").fetchall()
    assert [r[0] for r in closed] == [other_id]
    assert len(mirrors) == 1
