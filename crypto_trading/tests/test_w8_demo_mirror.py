"""Execution isolation, short-lived quotes and exchange-confirmed accounting."""
import json
from types import SimpleNamespace

import pytest

from crypto_trading.ops import w8_demo_mirror as m
from crypto_trading.crypto_common.kalshi.rest_event import KalshiEventOrderClient

NOW = 1800000000.
TICKER = 'KXBTC15M-test'


class Client:
    env = 'demo'
    base = m.DEMO_BASE

    def __init__(self):
        self.sent = []
        self.failure = None
        self.code = 201

    def create_order(self, **kw):
        self.sent.append(kw)
        if self.failure:
            raise self.failure
        return dict(status_code=self.code, response=json.dumps(
            {'order_id': 'demo-order'} if self.code == 201 else {'error': 'refused'}))


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'OUT_DIR', tmp_path / 'demo')
    monkeypatch.setattr(m, 'LOG_DIR', tmp_path / 'observer')
    m.OUT_DIR.mkdir()
    m.LOG_DIR.mkdir()
    clock = [NOW]
    monkeypatch.setattr(m.time, 'time', lambda: clock[0])
    monkeypatch.setattr(m, '_emit', lambda r: None)
    source = dict(version=m.VERSION, last_tick_ts=NOW, stopped_new=False,
                  books={'tilted': {'stopped_new': False}})
    (m.LOG_DIR / 'w8_complete_set_state.json').write_text(json.dumps(source))
    market = dict(status='active', close_time=NOW + 500)
    monkeypatch.setattr(m, 'live_demo_market', lambda t: market)
    monkeypatch.setattr(m, 'public_market', lambda t: market)
    monkeypatch.setattr(m, 'demo_exit_quote', lambda t, side: dict(
        status='ready', book_ask_dollars=.28, book_top_quantity=25.,
        book_observed_at=clock[0]))
    client = Client()
    return SimpleNamespace(client=client, mirror=m.Mirror(client=client), clock=clock, source=source,
                           market=market)


def signal(n=1, side='bid', price='0.7100'):
    return dict(ts=NOW, strategy='w8_complete_set', action='paper_order', book='tilted',
                intent=dict(ticker=TICKER, client_order_id=f'w8-paper-{n}', side=side,
                            price=price, count='1.00', post_only=True, expiration_time=int(NOW+20)))


def terminal_order(rec, filled=0., side='yes'):
    return dict(order_id='demo-order', client_order_id=rec['client_order_id'], ticker=TICKER,
                outcome_side=side,
                status='executed' if filled else 'canceled', fill_count_fp=str(filled),
                remaining_count_fp='0.00', maker_fill_cost_dollars=str(filled*.71),
                taker_fill_cost_dollars='0', maker_fees_dollars='0', taker_fees_dollars='0')


def test_contract_wire_roundtrip_and_invalid_side():
    for side, price in [('bid', '0.7100'), ('ask', '0.1400')]:
        intent = dict(side=side, price=price)
        contract, p = m.decode_intent(intent)
        body = KalshiEventOrderClient.v2_body(ticker=TICKER, contract_side=contract,
                                              price_dollars=p, count=1)
        assert (body['side'], body['price']) == (side, price)
    assert m.decode_intent(dict(side='ask', price='0.1400')) == ('no', .86)
    with pytest.raises(ValueError):
        m.decode_intent(dict(side='oops', price='.7'))


def test_preserves_w8_quote_and_default_w7_wire(ctx):
    assert ctx.mirror.handle(signal(side='ask', price='0.1400'))
    actual = ctx.client.sent[0]
    assert actual['side'] == 'no' and actual['price_dollars'] == .86
    assert actual['post_only'] is True and actual['expiration_time'] == NOW + 20
    assert actual['tif'] == 'good_till_canceled' and actual['count'] == 1
    body = KalshiEventOrderClient.v2_body(ticker=TICKER, contract_side='yes',
                                         price_dollars=.8, count=25, client_order_id='fixed')
    assert body == dict(ticker=TICKER, side='bid', count='25.00', price='0.8000',
                        time_in_force='immediate_or_cancel',
                        self_trade_prevention_type='taker_at_cross', client_order_id='fixed')


@pytest.mark.parametrize('change', [dict(book='paired'), dict(strategy='w7_noisefade'),
                                    dict(ts=NOW-16)])
def test_only_fresh_treatment_signals(ctx, change):
    assert not ctx.mirror.handle({**signal(), **change})
    assert not ctx.client.sent


def test_expiry_is_rechecked_after_network_lookup(ctx, monkeypatch):
    def slow(t):
        ctx.clock[0] += 19
        return ctx.market
    monkeypatch.setattr(m, 'live_demo_market', slow)
    assert not ctx.mirror.handle(signal())
    assert not ctx.client.sent


def test_dry_run_has_no_client_or_live_journal(ctx, monkeypatch):
    monkeypatch.setattr(m, 'KalshiEventOrderClient', lambda **k: pytest.fail('dry client'))
    dry = m.Mirror(dry_run=True)
    assert not dry.handle(signal())
    dry.save()
    assert not (m.OUT_DIR / 'state.json').exists()
    assert (m.OUT_DIR / 'dry_run_state.json').exists()


def test_write_ahead_survives_timeout_restart_and_blocks_duplicate(ctx):
    ctx.client.failure = TimeoutError('lost response')
    assert ctx.mirror.handle(signal())
    first = ctx.client.sent[0]['client_order_id']
    disk = json.loads((m.OUT_DIR / 'state.json').read_text())
    assert disk['markets'][TICKER]['orders'][0]['client_order_id'] == first
    restarted = m.Mirror(client=ctx.client)
    assert not restarted.handle(signal(2))
    assert len(ctx.client.sent) == 1


def test_rejection_allows_new_quote_but_not_same_id(ctx):
    ctx.client.code = 400
    assert ctx.mirror.handle(signal())
    assert not ctx.mirror.handle(signal())
    assert ctx.mirror.handle(signal(2))
    assert len(ctx.client.sent) == 2


def test_unknown_absence_stays_reserved(ctx, monkeypatch):
    ctx.client.failure = TimeoutError()
    ctx.mirror.handle(signal())
    monkeypatch.setattr(m, 'read_order', lambda *a: None)
    ctx.mirror.reconcile()
    assert not ctx.mirror.handle(signal(2))
    assert ctx.mirror.pending(ctx.mirror.state['markets'][TICKER])


@pytest.mark.parametrize('remaining,expected', [(1., True), (0., False)])
def test_source_early_cancel_is_mirrored_but_paper_fill_is_not(ctx, remaining, expected):
    ctx.mirror.handle(signal())
    rec = ctx.mirror.state['markets'][TICKER]['orders'][0]
    ctx.source['books']['tilted']['positions'] = {
        TICKER: dict(orders=[dict(id='1', remaining=remaining, cancel_ts=NOW-.1)])}
    (m.LOG_DIR / 'w8_complete_set_state.json').write_text(json.dumps(ctx.source))
    ctx.mirror.sync_source_cancels(NOW)
    assert bool(rec.get('cancel_requested')) is expected


def test_cancel_refusal_keeps_actual_order_reserved(ctx, monkeypatch):
    ctx.mirror.handle(signal())
    rec = ctx.mirror.state['markets'][TICKER]['orders'][0]
    rec['cancel_requested'] = True
    calls = []
    monkeypatch.setattr(ctx.client, 'cancel_order', lambda oid, **kw:
                        calls.append((oid, kw)) or dict(status_code=503), raising=False)
    raw = terminal_order(rec)
    raw.update(status='resting', remaining_count_fp='1.00')
    monkeypatch.setattr(m, 'read_order', lambda *a: raw)
    ctx.mirror.reconcile()
    assert calls == [('demo-order', {'market_ticker': TICKER})]
    assert not rec['terminal']
    assert not ctx.mirror.handle(signal(2))


def test_expired_zero_fill_allows_new_quote(ctx, monkeypatch):
    ctx.mirror.handle(signal())
    rec = ctx.mirror.state['markets'][TICKER]['orders'][0]
    monkeypatch.setattr(m, 'read_order', lambda *a: terminal_order(rec))
    ctx.mirror.reconcile()
    assert rec['terminal']
    assert ctx.mirror.handle(signal(2))


def test_partial_fill_consumes_entry_budget_and_settlement_uses_only_owned_fills(ctx, monkeypatch):
    ctx.mirror.handle(signal())
    rec = ctx.mirror.state['markets'][TICKER]['orders'][0]
    monkeypatch.setattr(m, 'read_order', lambda *a: terminal_order(rec, .37))
    monkeypatch.setattr(m, 'account_rows', lambda *a, **k: [
        dict(order_id='demo-order', count_fp='.37'), dict(order_id='W7-order', count_fp='25')])
    ctx.mirror.reconcile()
    assert not ctx.mirror.handle(signal(2))
    assert rec['filled'] == .37 and rec['terminal']
    ctx.clock[0] += 600
    ctx.market.update(status='settled', result='yes')
    ctx.mirror.reconcile()
    ledger = ctx.mirror.state['markets'][TICKER]
    assert ledger['payout'] == .37
    assert ledger['net_pnl_usd'] == pytest.approx(.1073)
    assert ctx.mirror.state['net_pnl_usd'] == pytest.approx(.1073)


def test_risk_exit_closes_verified_own_fractional_inventory_only(ctx, monkeypatch):
    ctx.mirror.handle(signal())
    rec = ctx.mirror.state['markets'][TICKER]['orders'][0]
    monkeypatch.setattr(m, 'read_order', lambda *a: terminal_order(rec, .37))
    monkeypatch.setattr(m, 'account_rows', lambda *a, **k: [dict(order_id='demo-order', count_fp='.37')])
    ctx.mirror.reconcile()
    exit_row = dict(strategy='w8_complete_set', book='tilted', action='paper_fill',
                    source='risk_flatten', liquidity='taker_depth_model', ticker=TICKER,
                    side='no', price=.28, quantity=25, ts=NOW)
    assert ctx.mirror.handle(exit_row)
    sent = ctx.client.sent[-1]
    assert sent['count'] == .37 and sent['side'] == 'no'
    assert sent['tif'] == 'immediate_or_cancel' and sent['post_only'] is False
    assert 'expiration_time' not in sent
    assert not ctx.mirror.handle(exit_row)


def test_quote_arriving_before_expiry_readback_is_retried(ctx, monkeypatch):
    ctx.mirror.handle(signal())
    rec = ctx.mirror.state['markets'][TICKER]['orders'][0]
    assert not ctx.mirror.handle(signal(2))
    assert ctx.mirror.state['queued']
    monkeypatch.setattr(m, 'read_order', lambda *a: terminal_order(rec))
    ctx.mirror.reconcile()
    assert ctx.mirror.drain_queued() == 1
    assert len(ctx.client.sent) == 2


def test_risk_signal_is_rechecked_after_market_lookup(ctx, monkeypatch):
    ctx.mirror.handle(signal())
    rec = ctx.mirror.state['markets'][TICKER]['orders'][0]
    rec.update(terminal=True, filled=.37)
    def slow(t):
        ctx.clock[0] += 16
        return ctx.market
    monkeypatch.setattr(m, 'public_market', slow)
    assert not ctx.mirror.handle(dict(strategy='w8_complete_set', book='tilted',
        action='paper_fill', source='risk_flatten', ticker=TICKER,
        side='no', price=.28, ts=NOW))
    assert len(ctx.client.sent) == 1


def test_exit_request_supersedes_queued_entry(ctx, monkeypatch):
    ctx.mirror.handle(signal())
    ctx.mirror.handle(signal(2))
    assert TICKER+':entry' in ctx.mirror.state['queued']
    ctx.mirror.handle(dict(strategy='w8_complete_set', book='tilted', action='paper_fill',
        source='risk_flatten', ticker=TICKER, side='no', price=.28, ts=NOW))
    assert TICKER+':entry' not in ctx.mirror.state['queued']
    assert ctx.mirror.state['markets'][TICKER]['exit_requested']
    assert not ctx.mirror.handle(signal(3))


def test_missing_response_recovers_only_after_two_closed_account_audits(ctx, monkeypatch):
    ctx.client.failure = TimeoutError()
    ctx.mirror.handle(signal())
    rec = ctx.mirror.state['markets'][TICKER]['orders'][0]
    monkeypatch.setattr(m, 'read_order', lambda *a: None)
    monkeypatch.setattr(m, 'account_rows', lambda *a, **k: [])
    ctx.clock[0] += 700
    ctx.market.update(status='settled', result='no')
    ctx.mirror.reconcile()
    assert not rec['terminal']
    ctx.clock[0] += 61
    ctx.mirror.reconcile()
    assert rec['terminal'] and rec['status'] == 'absent_after_close'


def test_absence_recovery_requires_every_fill_owner(ctx, monkeypatch):
    ctx.client.failure = TimeoutError()
    ctx.mirror.handle(signal())
    rec = ctx.mirror.state['markets'][TICKER]['orders'][0]
    monkeypatch.setattr(m, 'read_order', lambda *a: None)
    monkeypatch.setattr(m, 'account_rows', lambda c, p, key, **k:
                        [] if key == 'orders' else [dict(order_id='unattributed')])
    def missing(*a, **k):
        raise ValueError('unknown owner')
    monkeypatch.setattr(m, 'account_json', missing)
    ctx.clock[0] += 700
    ctx.market.update(status='settled', result='yes')
    ctx.mirror.reconcile()
    ctx.clock[0] += 61
    ctx.mirror.reconcile()
    assert not rec['terminal']


def test_direct_order_read_and_paginated_recovery(ctx, monkeypatch):
    calls = []
    def query(c, path, **params):
        calls.append((path, params))
        if path.endswith('/id'):
            return dict(order={'order_id': 'id'})
        return dict(orders=[dict(client_order_id='mine')] if params.get('cursor') else [],
                    cursor='' if params.get('cursor') else 'next')
    monkeypatch.setattr(m, 'account_json', query)
    assert m.read_order(ctx.client, dict(order_id='id')) == {'order_id': 'id'}
    assert calls[0][0] == '/portfolio/orders/id'
    assert m.read_order(ctx.client, dict(ticker=TICKER, client_order_id='mine')) == {'client_order_id': 'mine'}
    assert calls[-1][1]['cursor'] == 'next'


@pytest.mark.parametrize('page', [dict(orders=[]), dict(orders=[], cursor='same')])
def test_incomplete_pagination_is_never_evidence_of_no_orders(ctx, monkeypatch, page):
    monkeypatch.setattr(m, 'account_json', lambda *a, **k: page)
    with pytest.raises(ValueError):
        m.account_rows(ctx.client, '/portfolio/orders', 'orders', ticker=TICKER)


def test_version_mismatch_and_corrupt_state_fail_closed(ctx):
    ctx.source['version'] = 'w8_other'
    (m.LOG_DIR / 'w8_complete_set_state.json').write_text(json.dumps(ctx.source))
    with pytest.raises(ValueError, match='version'):
        ctx.mirror.handle(signal())
    (m.OUT_DIR / 'state.json').write_text('{corrupt')
    with pytest.raises(ValueError):
        m.Mirror(client=ctx.client)
    assert not ctx.client.sent


def test_prod_client_is_refused(ctx):
    ctx.client.env = 'prod'
    with pytest.raises(ValueError, match='DEMO'):
        m.Mirror(client=ctx.client)


def test_tail_keeps_partial_lines_and_reads_new_utc_file(ctx, monkeypatch):
    path = m.LOG_DIR / 'day1.jsonl'
    next_path = m.LOG_DIR / 'day2.jsonl'
    path.write_text('{"old":1}\n')
    current = [path]
    monkeypatch.setattr(m.Tail, 'today', staticmethod(lambda: current[0]))
    t = m.Tail()
    with path.open('a') as f:
        f.write('{"new":2}')
    assert t.read() == []
    with path.open('a') as f:
        f.write('\n')
    assert t.read() == [{'new': 2}]
    next_path.write_text('{"next":3}\n')
    current[0] = next_path
    assert t.read() == [{'next': 3}]


def filled_entry(ctx, quantity=1.):
    ctx.mirror.handle(signal())
    rec = ctx.mirror.state['markets'][TICKER]['orders'][0]
    rec.update(status='executed', filled=quantity, cost=quantity*.71,
               remaining=0., terminal=True)
    return rec


def risk_signal(ts=NOW):
    return dict(strategy='w8_complete_set', book='tilted', action='paper_fill',
                source='risk_flatten', ticker=TICKER, side='no',
                price=.001, quantity=99, ts=ts)


def test_persistent_exit_requotes_after_source_ttl_and_partial_fill(ctx, monkeypatch):
    filled_entry(ctx, .37)
    monkeypatch.setattr(m, 'demo_exit_quote', lambda *a: dict(
        status='ready', side='no', book_ask_dollars=.28, book_top_quantity=.12))
    assert ctx.mirror.handle(risk_signal())
    first = ctx.mirror.state['markets'][TICKER]['orders'][-1]
    assert ctx.client.sent[-1]['count'] == .12
    assert ctx.client.sent[-1]['price_dollars'] == .28  # never stale source .001
    first.update(status='executed', filled=.12, cost=.0336, remaining=0., terminal=True)
    # Paper is already flat/offline; this must not abandon real demo inventory.
    (m.LOG_DIR / 'w8_complete_set_state.json').unlink()
    ctx.clock[0] += 31
    monkeypatch.setattr(m, 'demo_exit_quote', lambda *a: dict(
        status='ready', side='no', book_ask_dollars=.32, book_top_quantity=25))
    assert ctx.mirror.manage_exits() == 1
    second = ctx.mirror.state['markets'][TICKER]['orders'][-1]
    assert ctx.client.sent[-1]['count'] == .25
    assert ctx.client.sent[-1]['price_dollars'] == .32
    assert second['client_order_id'] != first['client_order_id']
    second.update(status='executed', filled=.25, cost=.08, remaining=0., terminal=True)
    ctx.clock[0] += 31
    assert ctx.mirror.manage_exits() == 0
    assert ctx.mirror.state['markets'][TICKER]['exit_status'] == 'flat'
    assert len(ctx.client.sent) == 3


@pytest.mark.parametrize('quote_status', ['empty_side', 'orderbook_unavailable'])
def test_exit_empty_book_waits_with_backoff_and_preserves_request(ctx, monkeypatch, quote_status):
    filled_entry(ctx)
    quote_calls = []
    def empty(*args):
        quote_calls.append(args)
        return dict(status=quote_status, side='no', demo_ticker=TICKER,
                    book_ask_dollars=None, book_top_quantity=0)
    monkeypatch.setattr(m, 'demo_exit_quote', empty)
    assert not ctx.mirror.handle(risk_signal())
    market = ctx.mirror.state['markets'][TICKER]
    assert market['exit_requested'] and market['exit_status'] == quote_status
    ctx.clock[0] += 1
    assert ctx.mirror.manage_exits() == 0
    assert len(quote_calls) == 1
    ctx.clock[0] += 30
    monkeypatch.setattr(m, 'demo_exit_quote', lambda *a: dict(
        status='ready', book_ask_dollars=.33, book_top_quantity=1))
    assert ctx.mirror.manage_exits() == 1
    assert ctx.client.sent[-1]['price_dollars'] == .33


def test_unknown_exit_response_blocks_retry_across_restart(ctx):
    filled_entry(ctx)
    ctx.client.failure = TimeoutError('POST response lost')
    ctx.mirror.handle(risk_signal())
    restarted = m.Mirror(client=ctx.client)
    ctx.clock[0] += 31
    assert restarted.manage_exits() == 0
    assert len(ctx.client.sent) == 2
    assert restarted.state['markets'][TICKER]['exit_status'] == 'awaiting_order_confirmation'


def test_confirmed_zero_fill_exit_retries_with_new_journaled_identity(ctx):
    filled_entry(ctx)
    ctx.mirror.handle(risk_signal())
    old = ctx.mirror.state['markets'][TICKER]['orders'][-1]
    old.update(status='canceled', filled=0., remaining=0., terminal=True)
    ctx.mirror.save()
    restarted = m.Mirror(client=ctx.client)
    ctx.clock[0] += 31
    assert restarted.manage_exits() == 1
    new = restarted.state['markets'][TICKER]['orders'][-1]
    assert old['client_order_id'] != new['client_order_id']
    disk = json.loads((m.OUT_DIR / 'state.json').read_text())
    assert disk['markets'][TICKER]['orders'][-1]['client_order_id'] == new['client_order_id']


def test_old_exit_requested_journal_is_migrated(ctx):
    filled_entry(ctx, .37)
    market = ctx.mirror.state['markets'][TICKER]
    market['exit_requested'] = True
    ctx.mirror.save()
    restarted = m.Mirror(client=ctx.client)
    assert restarted.manage_exits() == 1
    assert ctx.client.sent[-1]['count'] == .37
    assert restarted.state['markets'][TICKER]['exit_request_id'].startswith('legacy:')


def test_exit_quantity_floors_available_fraction_and_cannot_overshoot(ctx, monkeypatch):
    filled_entry(ctx, .37)
    monkeypatch.setattr(m, 'demo_exit_quote', lambda *a: dict(
        status='ready', book_ask_dollars=.25, book_top_quantity=.129))
    ctx.mirror.handle(risk_signal())
    assert ctx.client.sent[-1]['count'] == .12


def test_exit_quote_slow_or_market_closes_during_read_does_not_send(ctx, monkeypatch):
    filled_entry(ctx)
    def slow(*a):
        ctx.clock[0] += 3
        return dict(status='ready', book_ask_dollars=.28, book_top_quantity=1)
    monkeypatch.setattr(m, 'demo_exit_quote', slow)
    assert not ctx.mirror.handle(risk_signal())
    assert ctx.mirror.state['markets'][TICKER]['exit_requested']
    assert len(ctx.client.sent) == 1
    ctx.clock[0] = NOW+501
    assert ctx.mirror.manage_exits() == 0
    assert ctx.mirror.state['markets'][TICKER]['exit_status'] == 'awaiting_settlement'


@pytest.mark.parametrize('entry', [True, False])
def test_fresh_same_version_signal_is_not_blocked_by_end_of_cycle_heartbeat(ctx, entry):
    if not entry:
        filled_entry(ctx)
    ctx.source['last_tick_ts'] = NOW-90
    (m.LOG_DIR / 'w8_complete_set_state.json').write_text(json.dumps(ctx.source))
    assert ctx.mirror.handle(signal() if entry else risk_signal())


@pytest.mark.parametrize('change', [dict(ts=NOW-16), dict(ts=NOW+2)])
def test_old_or_future_signal_cannot_create_persistent_exit(ctx, change):
    filled_entry(ctx)
    assert not ctx.mirror.handle({**risk_signal(), **change})
    assert not ctx.mirror.state['markets'][TICKER].get('exit_requested')


def test_explicit_wrong_version_cannot_create_persistent_exit(ctx):
    filled_entry(ctx)
    with pytest.raises(ValueError, match='version'):
        ctx.mirror.handle({**risk_signal(), 'version': 'w8_other'})
    assert not ctx.mirror.state['markets'][TICKER].get('exit_requested')


def test_transient_state_write_defers_only_within_original_signal_ttl(ctx):
    path = m.LOG_DIR / 'w8_complete_set_state.json'
    path.write_text('{partial')
    assert not ctx.mirror.handle(signal())
    assert ctx.mirror.state['deferred']
    ctx.clock[0] += 1
    path.write_text(json.dumps(ctx.source))
    assert ctx.mirror.drain_deferred() == 1
    assert ctx.client.sent[-1]['expiration_time'] == NOW+20


def test_deferred_signal_cannot_gain_a_new_lifetime(ctx):
    path = m.LOG_DIR / 'w8_complete_set_state.json'
    path.write_text('{partial')
    ctx.mirror.handle(signal())
    ctx.clock[0] += 16
    path.write_text(json.dumps(ctx.source))
    assert ctx.mirror.drain_deferred() == 0
    assert not ctx.client.sent


@pytest.mark.parametrize('exit_first', [True, False])
def test_deferred_exit_supersedes_entry_without_mutation_crash(ctx, exit_first):
    ctx.mirror.handle(signal())
    rec = ctx.mirror.state['markets'][TICKER]['orders'][0]
    rec.update(status='canceled', terminal=True, remaining=0.)
    pairs = [(TICKER+':exit', risk_signal()), (TICKER+':entry', signal(2))]
    ctx.mirror.state['deferred'] = dict(pairs if exit_first else reversed(pairs))
    assert ctx.mirror.drain_deferred() == 0
    assert not ctx.mirror.state['deferred']
    assert len(ctx.client.sent) == 1
    assert ctx.mirror.state['markets'][TICKER]['exit_requested']
