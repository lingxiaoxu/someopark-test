"""Contract/data-integrity tests for the isolated read-only publication layer."""
import json
import hashlib
import os
from pathlib import Path
import pytest
from crypto_trading.ops import export_prediction_frontend as e

T='KXBTC15M-26SEP150000-00'
NOW=1789430520.0


def attempt(oid='own', side='no', filled='0.37'):
    return dict(ts=e.iso(NOW-100), contracts=1, price_dollars=.8,prod_close=e.iso(NOW-50),
                _ticker=T,_side=side,_receipt=dict(order_id=oid,fill_count=filled))


def fill(oid='own', side='no', qty='.37', price='.8'):
    return dict(order_id=oid,ticker=T,outcome_side=side,count_fp=qty,
                no_price_dollars=price,yes_price_dollars=str(1-e.dec(price)),fee_cost='.003')


def account(fills):
    return dict(as_of=e.iso(NOW),order_ids=['own'],fills=fills,settlements=[dict(ticker=T,market_result='no',settled_time=e.iso(NOW-10))])


def test_fave_main_excludes_wide_observation_and_discovery():
    registered=e.iso(NOW-100)
    base=dict(leg='band',cost=.78,opened=e.iso(NOW-50))
    assert e.main_trade(base,registered)
    assert not e.main_trade(dict(base,cost=.77),registered)
    assert not e.main_trade(dict(base,leg='obs'),registered)
    assert not e.main_trade(dict(base,opened=e.iso(NOW-200)),registered)


def test_order_id_attribution_excludes_other_strategy_same_market():
    p,orders,positions,settled=e.fave_demo([attempt()],account([fill(),fill('pfme')]),[])
    assert len(orders)==1 and len(settled)==1 and not positions
    assert settled[0]['quantity']==.37
    assert settled[0]['net_usd']==pytest.approx(.071)
    assert p['net_pnl_usd']==pytest.approx(.071)


def test_identity_mismatch_is_not_a_missing_fill():
    with pytest.raises(ValueError,match='identity'):
        e.fave_demo([attempt()],account([dict(fill(),ticker='KXETH15M-other')]),[])


def test_missing_fill_does_not_become_zero_pnl():
    p,orders,_,_=e.fave_demo([attempt()],account([]),[])
    assert p['status']=='partial' and p['unresolved_count']==1
    assert orders[0]['filled'] is None and not orders[0]['verified']


def test_no_account_never_substitutes_paper():
    p,orders,_,_=e.fave_demo([attempt()],None,[])
    assert p['net_pnl_usd'] is None and p['status']=='unavailable'
    assert orders[0]['cost_usd'] is None


def test_losing_contract_payout_is_zero():
    a=account([fill()]);a['settlements'][0]['market_result']='yes'
    _,_,_,rows=e.fave_demo([attempt()],a,[])
    assert rows[0]['payout_usd']==0
    assert rows[0]['net_usd']==pytest.approx(-.299)


def test_source_fee_not_rounded_receipt_mean():
    _,_,_,rows=e.fave_demo([attempt()],account([dict(fill(),fee_cost='.000123')]),[])
    assert rows[0]['fees_usd']==.000123


def test_binary_book_complement_and_decimal_depth():
    raw=dict(orderbook_fp=dict(no_dollars=[['.0160','3.21'],['.0120','25']],yes_dollars=[['.70','9.13']]))
    assert e.best_ask(raw,'yes')==(.984,3.21)
    assert e.best_ask(raw,'no')==(.3,9.13)
    raw['orderbook_fp']['no_dollars']=[]
    assert e.best_ask(raw,'yes')==(None,0)


def test_malformed_book_not_empty_side():
    with pytest.raises(KeyError):e.best_ask({},'yes')
    with pytest.raises(ValueError):e.best_ask(dict(orderbook_fp=dict(no_dollars=[['NaN','2']])),'yes')


def test_pagination_requires_end_and_rejects_repeated_cursor():
    class R:
        def page(self,*_):return dict(fills=[],cursor='again')
    with pytest.raises(ValueError,match='repeated'):e.paged(R(),'/portfolio/fills','fills',NOW)
    class Missing:
        def page(self,*_):return dict(fills=[])
    with pytest.raises(ValueError,match='shape'):e.paged(Missing(),'/portfolio/fills','fills',NOW)


def test_cache_retains_original_time_and_prevents_frequent_get(tmp_path):
    cached=dict(as_of=e.iso(NOW-60),order_ids=[],fills=[],settlements=[])
    e.atomic_json(tmp_path/'verified_account.json',cached)
    def forbidden():raise AssertionError('must not GET inside 180 seconds')
    result,errs=e.account_snapshot([],NOW-1000,NOW,tmp_path,forbidden)
    assert result==cached and not errs


def test_failed_get_preserves_timestamp_and_emits_safe_issue(tmp_path):
    cached=dict(as_of=e.iso(NOW-999),order_ids=[],fills=[],settlements=[])
    e.atomic_json(tmp_path/'verified_account.json',cached)
    def fail():raise RuntimeError('SECRET-KEY /private/key.pem')
    result,errs=e.account_snapshot([],NOW-1000,NOW,tmp_path,fail)
    assert result==cached and errs[0]['code']=='READ_FAILED'
    assert 'SECRET' not in json.dumps(errs) and 'key.pem' not in json.dumps(errs)


def test_public_orders_are_whitelisted():
    a=account([dict(fill(),secret='do not publish')])
    p,orders,positions,closed=e.fave_demo([dict(attempt(),headers='secret')],a,[])
    output=json.dumps([p,orders,positions,closed])
    assert 'secret' not in output and 'headers' not in output


def test_atomic_write_rejects_nan_without_replacing_previous(tmp_path):
    path=tmp_path/'snapshot.json';e.atomic_json(path,dict(v=3))
    with pytest.raises(ValueError):e.atomic_json(path,dict(v=float('nan')))
    assert json.loads(path.read_text())==dict(v=3)
    assert not list(tmp_path.glob('*.tmp'))


def test_pfme_paper_excludes_paired_and_keeps_management_gap():
    r=dict(close_ts=NOW,fills=1,quantity=1,net_usd=.3,fees_usd=0,coverage_gap=True,unverified_order_quantity=0)
    state=dict(books=dict(tilted=dict(trades=[r],cum_net_usd=.3,positions={}),paired=dict(cum_net_usd=999)),verdicts={})
    p=e.paper_performance('pfme',state,NOW)
    assert p['net_pnl_usd']==.3 and p['sample_windows']==1 and p['status']=='verified'
    r['unverified_order_quantity']=.01
    assert e.paper_performance('pfme',state,NOW)['sample_windows']==0


def test_pfme_exit_is_opposite_purchase_with_net_inventory():
    orders=[]
    for side,qty,entry in [('yes',1,True),('no',.4,False)]:
        orders.append(dict(order_id=side,client_order_id=side,submitted_ts=NOW-50,side=side,entry=entry,quantity=qty,price=.6,filled=qty,cost=qty*.6,fees=0,terminal=True,fills=[{}],status='executed'))
    state=dict(markets={T:dict(orders=orders,close_ts=NOW+50,exit_requested=True)},net_pnl_usd=0)
    p,os_,positions,closed=e.pfme_demo(state,NOW)
    assert positions[0]['net_quantity']==pytest.approx(.6)
    assert positions[0]['paired_quantity']==.4
    assert os_[1]['entry'] is False and os_[1]['side']=='no'
    assert not closed and p['net_pnl_usd']==0


def test_failed_pfme_read_does_not_claim_verified_from_checked_ts():
    o=dict(client_order_id='u',submitted_ts=NOW,side='yes',entry=True,quantity=1,price=.7,filled=0,cost=0,fees=0,status='unknown',checked_ts=NOW,terminal=False,read_error='not visible')
    state=dict(markets={T:dict(orders=[o],close_ts=NOW+100)},net_pnl_usd=0)
    p,orders,_,_=e.pfme_demo(state,NOW)
    assert not orders[0]['verified'] and orders[0]['filled'] is None and p['unresolved_count']==1


def test_no_order_client_or_write_http_capability_imported():
    source=Path(e.__file__).read_text()
    assert 'KalshiEventOrderClient' not in source and '.post(' not in source and '.delete(' not in source
    assert 'execution_events import' not in source and 'live_watch import' not in source


def test_failed_account_reads_are_paced_too(tmp_path):
    calls=[]
    def fail():calls.append(1);raise RuntimeError('offline')
    e.account_snapshot([],NOW-1000,NOW,tmp_path,fail)
    _,issues=e.account_snapshot([],NOW-1000,NOW+60,tmp_path,fail)
    assert len(calls)==1 and issues[0]['code']=='RETRY_WAIT'


def test_duplicate_official_fill_is_rejected_not_double_counted():
    f=dict(fill(),fill_id='same')
    with pytest.raises(ValueError,match='duplicate'):
        e.fave_demo([attempt(filled='.74')],account([f,f]),[])


def test_market_exact_ticker_and_close_do_not_borrow_nearby_book(tmp_path):
    folder=tmp_path/'price_data/kalshi/event_strips/prod/KXBTC15M'
    day=e.iso(NOW)[:10]
    m=dict(recv_ts=NOW,spot_est=100,markets=[dict(ticker=T,close_time=e.iso(NOW+10),floor_strike=99,status='active')])
    b=dict(recv_ts=NOW,ticker=T,close_time=e.iso(NOW+900),ob=dict(orderbook_fp=dict(yes_dollars=[['.8','3']],no_dollars=[['.1','4']])))
    for kind,row in [('markets',m),('orderbook',b)]:
        p=folder/kind/(day+'.jsonl');p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(row)+'\n')
    rows,_=e.markets('fave',NOW,tmp_path)
    assert len(rows)==1 and rows[0]['quote_status']=='unavailable'
    assert rows[0]['yes_ask'] is None and rows[0]['no_ask'] is None


def test_missing_sources_still_emit_contract_with_explicit_unavailable(tmp_path):
    s=e.build_snapshot(now=NOW,signals=tmp_path,crypto=tmp_path,private_dir=tmp_path/'owned',offline=True)
    for v in s['strategies'].values():
        assert v['demo']['status']=='unavailable' and v['demo']['net_pnl_usd'] is None
        assert v['execution']['recent']['filled_orders'] is None
        assert v['execution']['exits']['all']['accepted'] is None
        assert v['issues'][0]['code']=='SOURCE_INVALID'


def test_http_503_without_order_id_remains_unknown_not_zero_fill():
    a=dict(attempt(),_receipt=dict(error='service unavailable'),status_code=503,body_sent=dict(client_order_id='attempt-client',count='1'))
    p,orders,_,_=e.fave_demo([a],account([]),[])
    assert p['status']=='partial' and p['unresolved_count']==1
    assert orders[0]['id']=='attempt-client' and orders[0]['status']=='unknown'
    assert orders[0]['filled'] is None and orders[0]['cost_usd'] is None
    x=e.execution_stats('test',NOW-500,NOW,[dict(contracts=1)],orders)
    assert x['unavailable']==1 and x['other_skips']==0 and x['zero_fills']==0


def test_pending_send_is_not_exchange_acceptance():
    o=dict(status='pending_send',verified=False,filled=None,quantity=1)
    x=e.execution_stats('test',NOW-100,NOW,[dict(quantity=1)],[o])
    assert x['accepted']==0 and x['filled_orders']==0 and x['other_skips']==0


def test_unknown_submission_does_not_make_position_list_look_flat():
    a=dict(attempt(),_receipt=dict(error='503'),status_code=503,body_sent=dict(client_order_id='unknown'))
    _,_,positions,_=e.fave_demo([a],account([]),[])
    assert len(positions)==1 and not positions[0]['verified']
    assert positions[0]['net_quantity'] is None


def health_state(tmp_path):
    kernel=tmp_path/'crypto_strategies/event_binary/complete_set.py'
    kernel.parent.mkdir(parents=True,exist_ok=True);kernel.write_text('value = 1\n')
    os.utime(kernel,(NOW-200,NOW-200))
    return dict(last_tick_ts=NOW-10,registered_at=e.iso(NOW-86400),
                source_sha256={str(kernel):hashlib.sha256(kernel.read_bytes()).hexdigest()},
                gaps=[],http_read_control=dict(requests=100,rate_limited=0,last_rate_ts=0)),kernel


def test_health_hash_compares_fixed_file_without_inferring_semantic_change(tmp_path):
    state,kernel=health_state(tmp_path)
    records,issues=e.pfme_observation_health(state,NOW-5,tmp_path)
    assert not issues and records[0]['status']=='verified'
    kernel.write_text('# added documentation only\nvalue = 1\n')
    os.utime(kernel,(NOW-20,NOW-20))
    records,issues=e.pfme_observation_health(state,NOW-5,tmp_path)
    assert [i['code'] for i in issues]==['SOURCE_HASH_MISMATCH']
    assert records[0]['as_of']==e.iso(NOW-10)
    assert e.iso(NOW-20) in issues[0]['detail']
    assert '不能推断差异仅为注释或交易逻辑变更' in issues[0]['detail']
    assert str(tmp_path) not in json.dumps([records,issues])


def test_health_hash_never_reads_state_supplied_paths(tmp_path,monkeypatch):
    state,kernel=health_state(tmp_path)
    state['source_sha256']={'/private/SECRET-key.pem':'a'*64}
    def forbidden(_):raise AssertionError('No file should be read without its registered hash')
    monkeypatch.setattr(Path,'read_bytes',forbidden)
    records,issues=e.pfme_observation_health(state,NOW-5,tmp_path)
    assert issues[0]['code']=='SOURCE_HASH_UNAVAILABLE'
    assert 'SECRET' not in json.dumps([records,issues])


def test_gap_window_uses_observer_time_and_preserves_coverage_limits(tmp_path):
    state,_=health_state(tmp_path)
    source=NOW-7200;state['last_tick_ts']=source
    state['gaps']=[dict(start=source-3700,end=source-3600),
                   dict(start=source-100,end=source-40),
                   dict(start=source-4000,end=source-3601)]
    records,issues=e.pfme_observation_health(state,NOW-5,tmp_path)
    gap=records[1]
    assert gap['as_of']==e.iso(source)
    assert '保留记录 2 条' in gap['detail'] and '100.00 秒' in gap['detail']
    assert e.iso(source-40) in gap['detail']
    assert '最近 1000 条' in gap['detail'] and '重叠记录未合并' in gap['detail']
    assert [i['code'] for i in issues]==['RECENT_OBSERVATION_GAPS']
    assert gap['status']=='recent_event'
    assert '不等于当前停机或已确认漏记成交' in gap['detail']


def test_current_gap_uses_only_latest_cycle_and_keeps_historical_events(tmp_path):
    state,_=health_state(tmp_path)
    state['gaps']=[dict(start=NOW-900,end=NOW-850)]
    state['last_cycle']=dict(markets={'KXBTC15M':dict(ticker='active',status='OK')})
    state['inputs']={'settled':dict(interval_complete=False,last_cycle_ts=NOW-900),
                     'active':dict(interval_complete=True,last_cycle_ts=NOW-15,last_book_ts=NOW-15)}
    records,issues=e.pfme_observation_health(state,NOW-5,tmp_path,now=NOW)
    assert records[1]['status']=='recent_event'
    assert [i['code'] for i in issues]==['RECENT_OBSERVATION_GAPS']
    state['inputs']['active']['interval_complete']=False
    records,issues=e.pfme_observation_health(state,NOW-5,tmp_path,now=NOW)
    assert records[1]['status']=='degraded'
    assert {i['code'] for i in issues}=={'RECENT_OBSERVATION_GAPS','CURRENT_OBSERVATION_GAP'}
    historical=next(i for i in issues if i['code']=='RECENT_OBSERVATION_GAPS')
    assert '当前周期' not in historical['detail']
    # A stale snapshot is evidence about its old cycle, never a present fault.
    records,issues=e.pfme_observation_health(state,NOW-5,tmp_path,now=NOW+400)
    assert records[1]['status']=='recent_event'
    assert [i['code'] for i in issues]==['RECENT_OBSERVATION_GAPS']


@pytest.mark.parametrize('status,book_age,expected',[
    ('CATCHING_UP',15,True),('DATA_ERROR',15,True),('OK',45,True),
    ('OK',15,False),('NO_ACTIVE_MARKET',45,False),('AWAITING_SETTLEMENT',45,False),
])
def test_current_gap_requires_current_cycle_evidence(tmp_path,status,book_age,expected):
    state,_=health_state(tmp_path)
    state['last_cycle']=dict(markets={'KXBTC15M':dict(ticker='active',status=status)})
    state['inputs']={'active':dict(last_book_ts=NOW-book_age,interval_complete=True)}
    _,issues=e.pfme_observation_health(state,NOW-5,tmp_path,now=NOW)
    assert ('CURRENT_OBSERVATION_GAP' in [i['code'] for i in issues])==expected


def test_invalid_gap_records_are_partial_not_a_clean_zero(tmp_path):
    state,_=health_state(tmp_path)
    state['gaps']=[dict(start=NOW-20,end=NOW+1),dict(start=NOW,end=NOW-30),None]
    records,issues=e.pfme_observation_health(state,NOW-5,tmp_path)
    assert records[1]['status']=='partial'
    assert '3 条时间不可验证' in records[1]['detail']
    assert issues[0]['code']=='GAP_HISTORY_PARTIAL'
    state.pop('gaps')
    records,issues=e.pfme_observation_health(state,NOW-5,tmp_path)
    assert records[1]['status']=='unavailable'
    assert issues[0]['code']=='GAP_HISTORY_UNAVAILABLE'


@pytest.mark.parametrize('limited,last,expected',[
    (7,NOW-50,'RECENT_RATE_LIMIT'),(7,NOW-4000,None),(0,0,None),
    (7,None,'RATE_LIMIT_HISTORY_UNAVAILABLE'),(7,NOW+1,'RATE_LIMIT_HISTORY_UNAVAILABLE'),
    (0,NOW-50,'RATE_LIMIT_HISTORY_UNAVAILABLE'),
])
def test_rate_limit_count_is_cumulative_with_real_last_event_time(tmp_path,limited,last,expected):
    state,_=health_state(tmp_path)
    state['http_read_control'].update(rate_limited=limited,last_rate_ts=last)
    records,issues=e.pfme_observation_health(state,NOW-5,tmp_path)
    assert records[2]['as_of']==e.iso(state['last_tick_ts'])
    assert [i['code'] for i in issues]==([expected] if expected else [])
    if expected!='RATE_LIMIT_HISTORY_UNAVAILABLE':
        assert '累计计数不是最近一小时次数' in records[2]['detail']
        if limited:assert e.iso(last) in records[2]['detail']


def test_rate_limit_recovery_needs_success_evidence_not_only_a_new_heartbeat(tmp_path):
    state,_=health_state(tmp_path)
    state.update(status='OBSERVING',http_resume_ts=NOW-800)
    state['http_read_control'].update(rate_limited=7,last_rate_ts=NOW-900)
    records,issues=e.pfme_observation_health(state,NOW-5,tmp_path,now=NOW)
    assert [i['code'] for i in issues]==['RECENT_RATE_LIMIT']
    assert records[2]['status']=='recent_event'
    assert '不据此宣称已恢复' in records[2]['detail']
    state['http_read_control']['last_success_ts']=NOW-30
    records,issues=e.pfme_observation_health(state,NOW-5,tmp_path,now=NOW)
    assert '已记录成功读取' in records[2]['detail']
    assert records[2]['status']=='recent_event'  # history is not erased


@pytest.mark.parametrize('status,resume,source_age,expected',[
    ('RATE_LIMIT_BACKOFF',NOW+90,10,True),
    ('RATE_LIMIT_BACKOFF',NOW-1,10,False),
    ('OBSERVING',NOW+90,10,False),
    ('RATE_LIMIT_BACKOFF',NOW+90,400,False),
    ('RATE_LIMIT_BACKOFF',NOW+90,-30,False),
])
def test_current_rate_limit_requires_fresh_status_and_unexpired_cooldown(tmp_path,status,resume,source_age,expected):
    state,_=health_state(tmp_path)
    state.update(status=status,http_resume_ts=resume,last_tick_ts=NOW-source_age)
    state['http_read_control'].update(rate_limited=7,last_rate_ts=NOW-500)
    records,issues=e.pfme_observation_health(state,NOW-5,tmp_path,now=NOW)
    assert ('CURRENT_RATE_LIMIT_BACKOFF' in [i['code'] for i in issues])==expected
    assert records[2]['status']==('degraded' if expected else 'recent_event')


def test_snapshot_exposes_health_without_changing_paper_or_demo_profit(tmp_path,monkeypatch):
    state,kernel=health_state(tmp_path)
    kernel.write_text('value = 2\n')
    state.update(version='w8_v8a',status='OBSERVING',
        books=dict(tilted=dict(trades=[],cum_net_usd=0,positions={})),
        parameters=dict(entry_band_lo=.62,entry_band_hi=.86,entry_rem_lo_s=420,entry_rem_hi_s=660,clip=1,max_net=3,quote_ttl_s=20))
    state['gaps']=[dict(start=NOW-100,end=NOW-30)]
    e.atomic_json(tmp_path/'live_watch/w8_complete_set_state.json',state)
    e.atomic_json(tmp_path/'w8_demo_mirror/state.json',dict(version='w8_v8a',mode='demo',
        last_heartbeat=e.iso(NOW-5),started_at=e.iso(NOW-100),markets={},net_pnl_usd=0))
    monkeypatch.setattr(e,'markets',lambda *_:([],[]))
    snapshot=e.build_snapshot(now=NOW,signals=tmp_path,crypto=tmp_path,private_dir=tmp_path/'owned',offline=True)
    pfme=snapshot['strategies']['pfme']
    assert len(pfme['runtime'])==5
    assert {i['code'] for i in pfme['issues']}=={'SOURCE_HASH_MISMATCH','RECENT_OBSERVATION_GAPS'}
    assert pfme['paper']['net_pnl_usd']==pfme['demo']['net_pnl_usd']==0
    assert pfme['paper']['status']==pfme['demo']['status']=='verified'
