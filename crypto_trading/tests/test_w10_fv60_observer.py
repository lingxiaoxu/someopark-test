"""Prospective FV60 integration tests: all input/output lives in tmp_path."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from crypto_trading.crypto_strategies.w10_entry_paper import fv60_observer as mod


def epoch(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


ENTRY = epoch('2026-09-16T03:06:00Z')
EXPIRY = epoch('2026-09-16T03:15:00Z')
TICKER = 'KXBTC15M-26SEP152315-15'


def quote(number=1, created=ENTRY, *, price=.71, side='yes'):
    return {'id': TICKER+':'+str(number), 'side': side, 'price': price,
            'quantity': 1., 'created_ts': created, 'activate_ts': created+.5,
            'expires_ts': created+20, 'remaining': 1., 'queue_ahead': 5.}


def fill(at=ENTRY+4, *, number=1, price=.71, side='yes'):
    return {'ts': at, 'order_id': TICKER+':'+str(number), 'side': side,
            'price': price, 'quantity': 1., 'fee_usd': .01, 'liquidity': 'maker_model'}


def market(*, orders=None, fills=()):
    return {'ticker': TICKER, 'series': 'KXBTC15M', 'close_ts': EXPIRY,
            'opened_ts': ENTRY-5, 'orders': deepcopy(orders if orders is not None else [quote()]),
            'fills': list(deepcopy(fills)), 'unverified_order_quantity': 0.}


def summary(*, fills=(), result='yes'):
    cost = sum(f['quantity']*f['price'] for f in fills)
    fees = sum(f['fee_usd'] for f in fills)
    payout = sum(f['quantity'] for f in fills if f['side'] == result)
    net = payout-cost-fees
    return {'ticker': TICKER, 'series': 'KXBTC15M', 'close_ts': EXPIRY,
            'result': result, 'settled_at': iso(EXPIRY+180), 'fills': len(fills),
            'quantity': sum(f['quantity'] for f in fills), 'cost_usd': cost,
            'fees_usd': fees, 'payout_usd': payout, 'net_usd': net,
            'paired_net_usd': 0., 'residual_net_usd': net,
            'coverage_gap': False, 'unverified_order_quantity': 0.}


def archive(*, orders=None, fills=(), book='tilted', result='yes'):
    return {'kind': 'settlement', 'ts': EXPIRY+180, 'book': book,
            'summary': summary(fills=fills, result=result),
            'ledger': market(orders=orders, fills=fills)}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = tmp_path/'crypto_trading'
    source = root/'trading_signals/live_watch/w8_complete_set_state.json'
    source.parent.mkdir(parents=True)
    source_code = root/'parent.py'
    own_code = root/'observer_stub.py'
    source_code.write_text('# protected parent\n')
    own_code.write_text('# isolated W10\n')
    parameters = root/'parameters.json'
    settings = json.loads(mod.PARAMETERS.read_text())
    settings.update(policy='fresh_spot_value_entry60_v1',
                    mode='prospective_conditional_source_quote_cohort',
                    entry_creation_max_age_seconds=60,
                    minimum_publication_latency_seconds=.5,
                    source_discovery_max_delay_seconds=90,
                    source_heartbeat_max_age_seconds=90,
                    network_enabled=False, demo_orders_enabled=False,
                    prod_orders_enabled=False, frontend_enabled=False)
    parameters.write_text(json.dumps(settings))
    output = root/'trading_signals/w10_entry_paper'
    clock = [ENTRY-30]
    parent = {'version': 'synthetic-w8-v8a', 'registered_at': '2026-09-14T05:17:35Z',
              'source_sha256': {'kernel.py': 'a'*64}, 'last_tick_ts': clock[0],
              'books': {'paired': {'complete_sets': True, 'positions': {}, 'trades': []},
                        'tilted': {'complete_sets': False, 'positions': {}, 'trades': []}}}
    source.write_text(json.dumps(parent))
    for key, value in {'ROOT': root, 'SOURCE': source, 'RUNTIME': output,
                       'PARAMETERS': parameters, 'PARENT_FILES': [source_code]}.items():
        monkeypatch.setattr(mod, key, value)
    monkeypatch.setattr(mod, 'own_files', lambda: [own_code, parameters])
    feature = {'valid': True, 'probability_proxy': .8, 'edge_proxy': .09}
    archive_queue = []
    feature_calls = []

    class FakeTape:
        def __init__(self, *args, **kwargs):
            self.tail = SimpleNamespace(errors=0)

        def update(self, now):
            pass

        def for_asset(self, asset):
            return [asset], [], []

    class FakeValuationTape:
        def __init__(self, *args, **kwargs):
            self.tail = SimpleNamespace(errors=0)
            self.read_errors = 0

        def update(self, now):
            return {'read_errors': 0}

        def features(self, asset, ticker, side, entry_price, decision_ts, close_ts, hl_features):
            feature_calls.append((asset, ticker, decision_ts))
            return {'decision_ts': decision_ts, **feature}

    class FakeSettlementTape:
        def __init__(self, *args, **kwargs):
            self.cursors = {}
            self.errors = 0
            self.error_details = []

        def seed_end(self, now):
            archive_queue.clear()

        def update(self, now):
            rows = list(archive_queue)
            archive_queue.clear()
            return rows

    monkeypatch.setattr(mod, 'ExistingTape', FakeTape)
    monkeypatch.setattr(mod, 'ValuationTape', FakeValuationTape)
    monkeypatch.setattr(mod, 'SettlementTape', FakeSettlementTape)
    monkeypatch.setattr(mod, 'compute_features', lambda c,b,t,at: {
        'valid': True, 'flow_valid': True, 'decision_ts': at,
        'realized_vol_5m_bp': 10., 'momentum_1m_bp': 1., 'observed_flow_imbalance_1m': .2})
    instances = []

    def new():
        instance = mod.Observer(output, clock=lambda: clock[0])
        instances.append(instance)
        return instance

    def write(*, positions=None, trades=(), paired=None, **extra):
        value = deepcopy(parent)
        value['last_tick_ts'] = clock[0]
        value.update(extra)
        value['books']['tilted']['positions'] = positions or {}
        value['books']['tilted']['trades'] = list(trades)
        if paired is not None:
            value['books']['paired'].update(paired)
        temporary = source.with_suffix('.testtmp')
        temporary.write_text(json.dumps(value))
        temporary.replace(source)

    yield SimpleNamespace(root=root, source=source, source_code=source_code, own_code=own_code,
                          parameters=parameters, output=output, clock=clock, feature=feature,
                          feature_calls=feature_calls, archive_queue=archive_queue,
                          new=new, write=write)
    for instance in instances:
        instance.lock.close()


def admit(sandbox):
    observer = sandbox.new()
    sandbox.clock[0] = ENTRY+2
    sandbox.write(positions={TICKER: market()})
    assert observer.cycle()['new_decisions'] == 1
    return observer


def episode(observer):
    return next(iter(observer.state['episodes'].values()))


def finish(sandbox, observer, *, fills=(None,), orders=None, result='yes'):
    fills = [fill()] if fills == (None,) else list(fills)
    sandbox.clock[0] = EXPIRY+181
    sandbox.archive_queue.append(archive(orders=orders, fills=fills, result=result))
    sandbox.write(trades=[summary(fills=fills, result=result)])
    return observer.cycle()['summary']


def test_new_registration_has_no_historical_results(sandbox):
    sandbox.write(positions={TICKER: market()}, trades=[summary()])
    observer = sandbox.new()
    assert observer.state['episodes'] == {}
    assert TICKER in observer.state['startup_excluded_tickers']
    sandbox.clock[0] = ENTRY+2
    sandbox.write(positions={TICKER: market()})
    assert observer.cycle()['new_decisions'] == 0
    assert observer.state['summary']['historical_research_pnl_included'] is False


def test_startup_paired_market_cannot_be_later_imported_as_new_tilted(sandbox):
    sandbox.write(paired={'positions': {TICKER: market()}})
    observer = sandbox.new()
    sandbox.clock[0] = ENTRY+2
    sandbox.write(positions={TICKER: market()})
    assert observer.cycle()['new_decisions'] == 0


def test_first_quote_features_and_durable_publication(sandbox):
    observer = admit(sandbox)
    row = episode(observer)
    assert row['candidate']['decision_ts'] == ENTRY
    assert sandbox.feature_calls == [('BTC', TICKER, ENTRY)]
    assert row['decision_published_at'] >= ENTRY+2
    first = row['quotes'][TICKER+':1']
    assert first['decision_published_at'] >= row['decision_published_at']
    assert first['decision_effective_at'] == first['decision_published_at']+.5


def test_settlement_summary_alone_cannot_prove_filtered_cashflows(sandbox):
    observer = admit(sandbox)
    sandbox.clock[0] = EXPIRY+181
    sandbox.write(trades=[summary(fills=[fill()])])
    result = observer.cycle()['summary']
    assert result['matched_settled_episodes'] == 0
    assert result['matched_candidate_net_usd'] == 0
    assert episode(observer).get('source_ledger_final') is not True
    sandbox.archive_queue.append(archive(fills=[fill()]))
    result = observer.cycle()['summary']
    assert result['matched_settled_episodes'] == 1
    assert result['matched_candidate_net_usd'] == pytest.approx(.28)


def test_archive_arrives_before_parent_summary_and_is_retained(sandbox):
    observer = admit(sandbox)
    sandbox.clock[0] = EXPIRY+181
    sandbox.archive_queue.append(archive(fills=[fill()]))
    sandbox.write()
    result = observer.cycle()['summary']
    assert result['matched_settled_episodes'] == 0
    assert episode(observer)['source_ledger_final'] is True
    sandbox.clock[0] += 2
    sandbox.write(trades=[summary(fills=[fill()])])
    assert observer.cycle()['summary']['matched_candidate_net_usd'] == pytest.approx(.28)


def test_restart_keeps_registration_and_does_not_duplicate_settlement(sandbox):
    observer = admit(sandbox)
    registered = observer.state['registered_at']
    observer.lock.close()
    restarted = sandbox.new()
    assert restarted.state['registered_at'] == registered
    result = finish(sandbox, restarted)
    assert result['matched_settled_episodes'] == 1
    assert result['matched_candidate_net_usd'] == pytest.approx(.28)
    sandbox.archive_queue.append(archive(fills=[fill()]))
    result = restarted.cycle()['summary']
    assert result['matched_settled_episodes'] == 1
    assert result['matched_candidate_net_usd'] == pytest.approx(.28)


def test_fill_time_before_publication_is_excluded_even_when_archive_arrives_later(sandbox):
    observer = admit(sandbox)
    result = finish(sandbox, observer, fills=[fill(ENTRY+1)])
    assert result['matched_settled_episodes'] == 0
    assert result['matched_control_net_usd'] == 0
    assert result['matched_candidate_net_usd'] == 0


def test_fill_at_publication_plus_latency_is_not_retroactively_accepted(sandbox):
    observer = admit(sandbox)
    result = finish(sandbox, observer, fills=[fill(ENTRY+2.5)])
    assert result['matched_settled_episodes'] == 0


def test_replacement_has_its_own_publication_before_fill(sandbox):
    observer = admit(sandbox)
    orders = [quote(), quote(2, ENTRY+10)]
    sandbox.clock[0] = ENTRY+12
    sandbox.write(positions={TICKER: market(orders=orders)})
    observer.cycle()
    second = episode(observer)['quotes'][TICKER+':2']
    assert second['decision_published_at'] >= ENTRY+12
    result = finish(sandbox, observer, orders=orders, fills=[fill(ENTRY+11, number=2)])
    assert result['matched_settled_episodes'] == 0


def test_creation_deadline_does_not_reset_after_side_change(sandbox):
    observer = admit(sandbox)
    orders = [quote(), quote(2, ENTRY+61, side='no', price=.6)]
    sandbox.clock[0] = ENTRY+62
    sandbox.write(positions={TICKER: market(orders=orders)})
    observer.cycle()
    assert episode(observer)['quotes'][TICKER+':2']['decision']['decision'] == 'skip'
    result = finish(sandbox, observer, orders=orders,
                    fills=[fill(ENTRY+65, number=2, side='no', price=.6)])
    assert result['matched_settled_episodes'] == 1
    assert result['matched_candidate_net_usd'] == 0
    assert result['matched_control_net_usd'] == pytest.approx(-.61)


def test_partial_inventory_is_unknown_and_never_credited_as_avoided_loss(sandbox):
    observer = admit(sandbox)
    orders = [quote(), quote(2, ENTRY+61, side='no', price=.6)]
    sandbox.clock[0] = ENTRY+62
    sandbox.write(positions={TICKER: market(orders=orders)})
    observer.cycle()
    result = finish(sandbox, observer, orders=orders,
                    fills=[fill(), fill(ENTRY+65, number=2, side='no', price=.6)])
    assert result['matched_settled_episodes'] == 0
    assert result['avoided_loss_usd'] == 0


def test_order_created_before_deadline_can_fill_after_deadline(sandbox):
    observer = admit(sandbox)
    orders = [quote(), quote(2, ENTRY+59)]
    sandbox.clock[0] = ENTRY+59.2
    sandbox.write(positions={TICKER: market(orders=orders)})
    observer.cycle()
    assert episode(observer)['quotes'][TICKER+':2']['decision']['decision'] == 'accept'
    result = finish(sandbox, observer, orders=orders, fills=[fill(ENTRY+63, number=2)])
    assert result['matched_settled_episodes'] == 1
    assert result['matched_candidate_net_usd'] == pytest.approx(.28)


def test_archive_revision_does_not_replace_original_final_ledger(sandbox):
    observer = admit(sandbox)
    assert finish(sandbox, observer)['matched_settled_episodes'] == 1
    original = deepcopy(episode(observer)['source_ledger'])
    revised = archive(fills=[fill()])
    revised['ledger']['fills'][0]['price'] = .7
    sandbox.archive_queue.append(revised)
    result = observer.cycle()['summary']
    assert episode(observer)['source_ledger'] == original
    assert result['matched_settled_episodes'] == 0


def test_observed_fill_cannot_disappear_in_final_archive(sandbox):
    observer = admit(sandbox)
    sandbox.clock[0] = ENTRY+5
    sandbox.write(positions={TICKER: market(fills=[fill()])})
    observer.cycle()
    result = finish(sandbox, observer, fills=[])
    assert result['matched_settled_episodes'] == 0
    assert 'source_fill_history_revised' in episode(observer)['issues']
    assert episode(observer)['source_evidence_before_revision']['fills'] == [fill()]


def test_source_guard_applies_to_pending_episode_missing_from_active_positions(sandbox):
    observer = admit(sandbox)
    sandbox.source_code.write_text('# revised source during unsettled market\n')
    result = finish(sandbox, observer)
    assert result['matched_settled_episodes'] == 0
    assert 'source_guard_blocked_during_active_episode' in episode(observer)['issues']


def test_observed_cancellation_cannot_be_removed(sandbox):
    observer = admit(sandbox)
    first = quote()
    first['cancel_ts'] = ENTRY+10
    sandbox.clock[0] = ENTRY+6
    sandbox.write(positions={TICKER: market(orders=[first])})
    observer.cycle()
    result = finish(sandbox, observer)
    assert result['matched_settled_episodes'] == 0
    assert 'source_cancellation_revised' in episode(observer)['issues']


def test_unconfirmed_publication_after_crash_cannot_be_republished_as_causal(sandbox):
    observer = admit(sandbox)
    saved = json.loads((sandbox.output/'state.json').read_text())
    row = next(iter(saved['episodes'].values()))
    row['decision_published_at'] = None
    for record in row['quotes'].values():
        record['decision_published_at'] = None
    (sandbox.output/'state.json').write_text(json.dumps(saved))
    observer.lock.close()
    restarted = sandbox.new()
    result = finish(sandbox, restarted)
    assert result['matched_settled_episodes'] == 0
    assert result['matched_candidate_net_usd'] == 0


@pytest.mark.parametrize('filename', ['source_code', 'own_code'])
def test_changed_source_bytes_block_new_admissions(sandbox, filename):
    observer = sandbox.new()
    getattr(sandbox, filename).write_text('# altered\n')
    sandbox.clock[0] = ENTRY+2
    sandbox.write(positions={TICKER: market()})
    observer.cycle()
    assert observer.state['new_admissions_allowed'] is False
    assert observer.state['episodes'] == {}


def test_stale_parent_blocks_new_admissions(sandbox):
    observer = sandbox.new()
    sandbox.clock[0] = ENTRY+2
    sandbox.write(positions={TICKER: market()}, last_tick_ts=ENTRY-100)
    observer.cycle()
    assert observer.state['new_admissions_allowed'] is False
    assert observer.state['episodes'] == {}


def test_wrong_exact_ticker_expiry_is_not_admitted(sandbox):
    observer = sandbox.new()
    sandbox.clock[0] = ENTRY+2
    wrong = market()
    wrong['close_ts'] += 900
    sandbox.write(positions={TICKER: wrong})
    observer.cycle()
    assert observer.state['episodes'] == {}


def test_paired_book_cannot_change_tilted_profit(sandbox):
    observer = admit(sandbox)
    sandbox.clock[0] = EXPIRY+181
    paired = summary(fills=[fill()])
    paired.update(net_usd=999, residual_net_usd=999, payout_usd=999.72)
    sandbox.archive_queue.append(archive(fills=[fill()]))
    sandbox.write(trades=[summary(fills=[fill()])], paired={'trades': [paired]})
    result = observer.cycle()['summary']
    assert result['matched_control_net_usd'] == pytest.approx(.28)
    assert result['matched_candidate_net_usd'] == pytest.approx(.28)


def test_missing_valuation_is_unclassified_not_successful_avoidance(sandbox):
    sandbox.feature['valid'] = False
    observer = admit(sandbox)
    result = finish(sandbox, observer, result='no')
    assert result['matched_settled_episodes'] == 0
    assert result['avoided_loss_usd'] == 0


@pytest.mark.parametrize('relative', ['trading_signals/live_watch/child',
                                     'trading_signals/w9_rnn_paper', 'price_data/new'])
def test_output_path_cannot_write_other_runtimes(sandbox, relative):
    with pytest.raises(ValueError):
        mod.Observer(sandbox.root/relative, clock=lambda: ENTRY)
