"""Isolated prospective W10 FV60 observer; reads W8, never executes orders.

The source creation clock selects causal inputs. Actual durable publication
separately determines whether any source paper fill can enter the comparison.
All order lifetimes and management remain conditional on the source path.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import time

from ..downside_paper.features import compute_features
from ..downside_paper.sources import (
    _episode_id, _ticker, make_parent_binding, w8_existing_tickers,
    w8_outcomes_from_state,
)
from ..downside_paper.tape import ExistingTape
from .observer import atomic_json, digest, file_hashes, sync_journal, timestamp
from .fv60_features import ValuationTape
from .archive_tail import SettlementTape, ArchiveReadError
from . import fv60_policy as policy

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'trading_signals/live_watch/w8_complete_set_state.json'
RUNTIME = ROOT / 'trading_signals/w10_entry_paper'
PARAMETERS = Path(__file__).with_name('parameters.json')
PARENT_FILES = [ROOT/'crypto_strategies/live_watch'/n for n in
                ('w8_complete_set.py', 'common.py', 'config.yaml')]
PARENT_FILES += [ROOT/'crypto_strategies/event_binary/complete_set.py']


def own_files():
    shared = Path(__file__).parent.parent/'downside_paper'
    return sorted(list(Path(__file__).parent.glob('*.py')) + [PARAMETERS] +
                  [shared/n for n in ('__init__.py', 'features.py', 'sources.py', 'tape.py', 'policy.py')])


def compact_ledger(market):
    """Preserve complete cashflow evidence, excluding large source trade-ID caches."""
    return deepcopy({k: market[k] for k in ('ticker', 'series', 'close_ts', 'opened_ts',
        'orders', 'fills', 'coverage_gap', 'unverified_order_quantity') if k in market})


def source_progress_error(previous, current):
    """Previously witnessed fills/orders cannot vanish into a cleaner final book."""
    for field in ('ticker', 'series', 'close_ts'):
        if previous.get(field) != current.get(field):
            return 'source_market_identity_revised'
    prior_fills, fills = previous.get('fills', []), current.get('fills', [])
    if not isinstance(fills, list) or fills[:len(prior_fills)] != prior_fills:
        return 'source_fill_history_revised'
    try:
        orders = {order['id']: order for order in current.get('orders', [])}
        for prior in previous.get('orders', []):
            new = orders.get(prior['id'])
            if new is None:
                return 'previously_observed_source_quote_disappeared'
            if policy.quote_fingerprint(prior, previous['ticker']) != policy.quote_fingerprint(new, previous['ticker']):
                return 'source_quote_revised'
            if prior.get('cancel_ts') is not None and new.get('cancel_ts') != prior['cancel_ts']:
                return 'source_cancellation_revised'
    except (KeyError, ValueError, TypeError):
        return 'invalid_source_quote_history'
    return None


class Observer:
    def __init__(self, output=RUNTIME, *, clock=time.time):
        self.clock = clock
        self.output = Path(output)
        resolved, repo = self.output.resolve(), ROOT.parent.resolve()
        if ((resolved == repo or repo in resolved.parents) and resolved != RUNTIME.resolve()) or resolved in repo.parents:
            raise ValueError('W10 writes must use its own dedicated runtime directory')
        self.output.mkdir(parents=True, exist_ok=True)
        self.lock = (self.output/'observer.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.parameters = json.loads(PARAMETERS.read_text())
        if self.parameters['policy'] != policy.POLICY_VERSION:
            raise ValueError('policy/version mismatch')
        for field in ('network_enabled', 'demo_orders_enabled', 'prod_orders_enabled', 'frontend_enabled'):
            if self.parameters.get(field) is not False:
                raise ValueError('W10 is local paper only')
        if (self.parameters['entry_creation_max_age_seconds'] != 60 or
                self.parameters['minimum_publication_latency_seconds'] != .5):
            raise ValueError('unregistered timing parameters')
        self.tape = ExistingTape(ROOT/'price_data/hyperliquid', assets=('BTC', 'ETH', 'DOGE', 'XRP'))
        self.valuation = ValuationTape(ROOT/'price_data')
        self.settlements = SettlementTape(ROOT/'price_data/kalshi/w8_complete_set/prod')
        self.state_path = self.output/'state.json'
        self.journal_index = {}
        self.parent_signature = None
        self.parent = None
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            if (self.state['version'] != policy.POLICY_VERSION or
                    self.state['parameters_sha256'] != digest(self.parameters)):
                raise ValueError('version/parameter change requires separate archived registration')
            self.settlements.cursors = deepcopy(self.state.get('archive_cursors', {}))
            for row in self.state['episodes'].values():
                if row.get('decision_published_at') is None:
                    self.issue(row, 'initial_publication_not_confirmed_before_restart')
                for quote in row['quotes'].values():
                    if quote.get('decision_published_at') is None:
                        self.issue(row, 'quote_publication_not_confirmed_before_restart')
        else:
            if (self.output/'registration.json').exists():
                raise ValueError('registered state missing; refusing experiment reset')
            parent = self.load_parent()
            seed_at = self.clock()
            self.settlements.seed_end(seed_at)
            registered = self.clock()
            if not 0 <= registered-timestamp(parent.get('last_tick_ts')) <= self.parameters['source_heartbeat_max_age_seconds']:
                raise ValueError('fresh parent heartbeat required for registration')
            if parent.get('books', {}).get('tilted', {}).get('complete_sets') is not False:
                raise ValueError('W8 tilted source required')
            self.state = {
                'version': policy.POLICY_VERSION, 'strategy': 'w10', 'mode': policy.MODE,
                'registered_at': registered,
                'registered_utc': datetime.fromtimestamp(registered, timezone.utc).isoformat(),
                'parameters': self.parameters, 'parameters_sha256': digest(self.parameters),
                'parent_binding': make_parent_binding('w8', parent),
                'parent_source_sha256': file_hashes(PARENT_FILES),
                'own_source_sha256': file_hashes(own_files()),
                'startup_excluded_tickers': sorted(w8_existing_tickers(parent)),
                'episodes': {}, 'pending_source_settlements': [],
                'archive_cursors': deepcopy(self.settlements.cursors),
                'source_errors': [], 'source_error_count': 0,
                'cycles': 0, 'last_tick': None,
                'historical_research_pnl_included': False,
                'network_enabled': False, 'demo_orders_enabled': False,
                'prod_orders_enabled': False, 'frontend_enabled': False,
                'limitations': [
                    'Conditional source quote/fill cohort, not independent order execution or queue simulation.',
                    'Initial fair value is a normal-distribution proxy, not calibrated outcome probability.',
                    'Uses existing composite spot proxy, not the official settlement index.',
                    'Same entry inventory inherits every source management fill and fee; partial inventory is unidentified.',
                    '60 seconds limits new quote creation, not the lifetime of existing quotes or inventory.',
                    'Actual durable publication plus 0.5 seconds is required before retained entry fills.',
                    'Unknown, late, revised or unverified evidence never counts as avoided loss.',
                    'W8 registered bytes are preserved; source process memory has not been attested.',
                    'Historical candidate selection is not an untouched out-of-sample proof.',
                ],
            }
            self.state['summary'] = policy.summarize({})
            atomic_json(self.output/'registration.json', self.state)
            atomic_json(self.state_path, self.state)

    @staticmethod
    def issue(row, reason):
        if reason not in row.setdefault('issues', []):
            row['issues'].append(reason)

    def error(self, at, reason, detail=None):
        self.state['source_error_count'] += 1
        self.state['source_errors'].append({'at': at, 'reason': reason, 'detail': detail})
        self.state['source_errors'] = self.state['source_errors'][-100:]

    def load_parent(self):
        stat = SOURCE.stat()
        signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if signature != self.parent_signature:
            self.parent = json.loads(SOURCE.read_text())
            self.parent_signature = signature
        return self.parent

    def check_sources(self, parent, now):
        try:
            age = now-timestamp(parent.get('last_tick_ts'))
        except (ValueError, TypeError):
            age = None
        checks = {
            'parent_binding_matches': make_parent_binding('w8', parent) == self.state['parent_binding'],
            'parent_disk_unchanged': file_hashes(PARENT_FILES) == self.state['parent_source_sha256'],
            'observer_and_adapters_unchanged': file_hashes(own_files()) == self.state['own_source_sha256'],
            'parent_heartbeat_fresh': age is not None and 0 <= age <= self.parameters['source_heartbeat_max_age_seconds'],
            'tilted_source': parent.get('books', {}).get('tilted', {}).get('complete_sets') is False,
        }
        self.state['source_checks'] = checks
        self.state['parent_heartbeat_age_seconds'] = age
        self.state['new_admissions_allowed'] = all(checks.values())

    def candidate(self, ticker, market, now):
        asset, close = _ticker(ticker, 'w8')
        if (market.get('ticker') != ticker or market.get('series') != 'KX'+asset+'15M'
                or timestamp(market.get('close_ts')) != close):
            raise ValueError('exact source market identity/expiry mismatch')
        orders = [o for o in market.get('orders', []) if o.get('id') == ticker+':1']
        if len(orders) != 1:
            raise ValueError('unique original source quote required')
        first = policy.quote_source(orders[0], ticker)
        if first['created_ts'] > now:
            return None
        if first['created_ts'] < self.state['registered_at']:
            return None
        if first['expires_ts'] > close:
            raise ValueError('source quote lifetime exceeds exact market expiry')
        binding = self.state['parent_binding']
        return {
            'id': _episode_id(binding, ticker, 'tilted'), 'strategy': 'w10',
            'ticker': ticker, 'asset': asset, 'side': first['side'],
            'decision_ts': first['created_ts'], 'expires_ts': close,
            'parent_binding': deepcopy(binding),
            'metadata': {'parent_leg': 'tilted', 'source_order_id': first['id'],
                'entry_price': first['price'], 'contracts': first['quantity'],
                'decision_ts_basis': 'parent_order_created_ts_feature_reference_only',
                'source_kind': 'W8_tilted_first_quote_atomic_state',
                'execution_claim': policy.MODE},
        }

    def admit(self, candidate, market, now):
        first_ts = candidate['decision_ts']
        hl = compute_features(*self.tape.for_asset(candidate['asset']), first_ts)
        features = self.valuation.features(candidate['asset'], candidate['ticker'], candidate['side'],
            candidate['metadata']['entry_price'], first_ts, candidate['expires_ts'], hl)
        decision = policy.decide(candidate, features, self.parameters)
        if not 0 <= now-first_ts <= self.parameters['source_discovery_max_delay_seconds'] or now >= candidate['expires_ts']:
            decision = {'decision': 'unclassified', 'reason': 'late_source_discovery'}
        row = {
            'candidate': candidate, 'features': features, 'decision': decision,
            'observed_at': now, 'decision_published_at': None,
            'discovery_lag_seconds': now-first_ts,
            'minimum_publication_latency_seconds': .5,
            'quotes': {}, 'issues': [], 'source_ledger': compact_ledger(market),
            'source_ledger_final': False, 'outcome': None,
        }
        self.state['episodes'][candidate['id']] = row
        return row

    def observe_quotes(self, row, market, now, publish_quotes):
        ticker = row['candidate']['ticker']
        ids = set()
        for raw in market.get('orders', []):
            try:
                source = policy.quote_source(raw, ticker)
            except (ValueError, TypeError):
                self.issue(row, 'invalid_source_quote')
                continue
            identifier = source['id']
            if identifier in ids:
                self.issue(row, 'duplicate_source_quote')
            ids.add(identifier)
            if source['created_ts'] > now:
                continue
            fingerprint = policy.quote_fingerprint(source)
            previous = row['quotes'].get(identifier)
            if previous is not None:
                if previous['source_fingerprint'] != fingerprint:
                    self.issue(row, 'source_quote_revised')
                continue
            decision = policy.decide_quote(row['candidate'], row['decision'], source, self.parameters)
            end = min(source['expires_ts'], timestamp(raw['cancel_ts'])) if raw.get('cancel_ts') is not None else source['expires_ts']
            if decision['decision'] == 'accept' and (now+.5 >= end or now > row['candidate']['decision_ts']+60):
                decision = {'decision': 'unclassified', 'reason': 'source_quote_no_longer_actionable_at_observation'}
            quote = {
                'source': source, 'source_fingerprint': fingerprint, 'decision': decision,
                'observed_at': now, 'decision_published_at': None,
            }
            if decision.get('reason') == 'precommitted_entry_deadline_elapsed':
                initial_pub = row.get('decision_published_at')
                if initial_pub is not None and initial_pub < source['created_ts']:
                    quote['policy_precommitted_at'] = initial_pub
            row['quotes'][identifier] = quote
            publish_quotes.append(quote)

    def attach_archive(self, event, now, publish_quotes):
        if event.get('kind') != 'settlement' or event.get('book') != 'tilted':
            return
        ledger = event['ledger']
        identifier = _episode_id(self.state['parent_binding'], ledger['ticker'], 'tilted')
        row = self.state['episodes'].get(identifier)
        if row is None:
            return
        archived = compact_ledger(ledger)
        if row['source_ledger_final'] and row['source_ledger'] != archived:
            self.issue(row, 'final_source_ledger_revised')
            return
        error = source_progress_error(row['source_ledger'], archived)
        if error:
            self.issue(row, error)
            row.setdefault('source_evidence_before_revision', deepcopy(row['source_ledger']))
        self.observe_quotes(row, ledger, now, publish_quotes)
        row['source_ledger'] = archived
        row['source_ledger_final'] = True
        row['source_archive'] = {'event_sha256': digest(event), 'ts': event['ts'], 'observed_at': now,
                                 'summary': deepcopy(event['summary'])}

    def reconcile(self, parent, now):
        new = 0
        for outcome in w8_outcomes_from_state(parent, self.state['parent_binding'], until_ts=now):
            row = self.state['episodes'].get(outcome['id'])
            if row is None:
                continue
            if row['outcome'] is not None and row['outcome'] != outcome:
                self.issue(row, 'source_outcome_revised')
            elif row['outcome'] is None:
                row['outcome'] = outcome
                new += 1
            if row.get('source_archive'):
                # Independently adapt the archived summary with the same bound
                # source metadata, and compare it with the atomic state outcome.
                archived_parent = {'version': parent['version'], 'registered_at': parent['registered_at'],
                    'source_sha256': parent.get('source_sha256'), 'books': {
                        'tilted': {'trades': [row['source_archive']['summary']], 'complete_sets': False}}}
                archived_outcomes = w8_outcomes_from_state(archived_parent, self.state['parent_binding'], until_ts=now)
                if len(archived_outcomes) != 1 or archived_outcomes[0] != outcome:
                    self.issue(row, 'archive_and_atomic_settlement_disagree')
        return new

    def journals(self):
        decisions, quotes, settlements = [], [], []
        for identifier, row in self.state['episodes'].items():
            decisions.append({'event_id': identifier, **{k: row[k] for k in
                ('candidate', 'features', 'decision', 'observed_at', 'decision_published_at')}})
            for oid, quote in row['quotes'].items():
                quotes.append({'event_id': identifier+':'+oid, 'episode_id': identifier, **quote})
            if row.get('outcome') is not None:
                evaluation = row['evaluation']
                revision = digest({'outcome': row['outcome'], 'evaluation': evaluation})
                settlements.append({'event_id': identifier+':'+revision, 'episode_id': identifier,
                    'outcome': row['outcome'], 'evaluation': evaluation})
        for name, records in (('decisions', decisions), ('quotes', quotes), ('settlements', settlements)):
            sync_journal(self.output/(name+'.jsonl'), records, self.journal_index)

    def cycle(self):
        parent = self.load_parent()
        now = self.clock()
        self.check_sources(parent, now)
        self.tape.update(now)
        self.valuation.update(now)
        pending = list(self.state['pending_source_settlements'])
        try:
            pending.extend(self.settlements.update(now))
            self.state['archive_reader_healthy'] = True
        except ArchiveReadError as error:
            self.error(now, 'archive_read_failed', str(error))
            self.state['archive_reader_healthy'] = False
            self.state['new_admissions_allowed'] = False
        publish_rows, publish_quotes = [], []
        binding_ok = self.state['source_checks']['parent_binding_matches']
        if not self.state['new_admissions_allowed']:
            for row in self.state['episodes'].values():
                if row.get('outcome') is None or not row['source_ledger_final']:
                    self.issue(row, 'source_guard_blocked_during_active_episode')
        for ticker, market in parent.get('books', {}).get('tilted', {}).get('positions', {}).items():
            if not binding_ok:
                break
            identifier = _episode_id(self.state['parent_binding'], ticker, 'tilted')
            row = self.state['episodes'].get(identifier)
            if row is None:
                if not self.state['new_admissions_allowed'] or ticker in self.state['startup_excluded_tickers']:
                    continue
                if not market.get('orders'):
                    continue
                try:
                    candidate = self.candidate(ticker, market, now)
                    if candidate is None:
                        continue
                    row = self.admit(candidate, market, now)
                    publish_rows.append(row)
                except (ValueError, KeyError, TypeError) as error:
                    self.error(now, 'invalid_initial_source', {'ticker': ticker, 'error': str(error)})
                    continue
            if not self.state['new_admissions_allowed']:
                self.issue(row, 'source_guard_blocked_during_active_episode')
            if not row['source_ledger_final']:
                error = source_progress_error(row['source_ledger'], market)
                if error:
                    self.issue(row, error)
                    row.setdefault('source_evidence_before_revision', deepcopy(row['source_ledger']))
                self.observe_quotes(row, market, now, publish_quotes)
                row['source_ledger'] = compact_ledger(market)
        remaining = []
        for event in pending:
            if timestamp(event['ts']) > now or not binding_ok:
                remaining.append(event)
                continue
            self.attach_archive(event, now, publish_quotes)
        self.state['pending_source_settlements'] = remaining
        new_settlements = self.reconcile(parent, now) if binding_ok else 0
        self.state['archive_cursors'] = deepcopy(self.settlements.cursors)
        self.state['last_tick'] = now
        self.state['cycles'] += 1
        self.state['source_read_errors'] = {
            'hyperliquid': getattr(self.tape.tail, 'errors', 0),
            'valuation': getattr(self.valuation, 'read_errors', 0)+getattr(getattr(self.valuation, 'tail', None), 'errors', 0),
            'archive': self.settlements.errors,
        }
        # Publication protocol: the first fsync makes the exact decision durable;
        # a second writes its measured publication bound. A crash between them
        # excludes the unconfirmed record, never guesses its earlier timestamp.
        atomic_json(self.state_path, self.state)
        published = self.clock()
        for row in publish_rows:
            row['decision_published_at'] = published
        for quote in publish_quotes:
            quote['decision_published_at'] = published
            if quote['decision']['decision'] == 'accept':
                quote['decision_effective_at'] = published+.5
        for row in self.state['episodes'].values():
            row['evaluation'] = policy.evaluate_episode(row)
        self.state['summary'] = policy.summarize(self.state['episodes'])
        self.state.pop('last_error', None)
        atomic_json(self.state_path, self.state)
        self.journals()
        return {'at': now, 'version': policy.POLICY_VERSION, 'new_decisions': len(publish_rows),
                'new_quote_decisions': len(publish_quotes), 'new_settlements': new_settlements,
                'new_admissions_allowed': self.state['new_admissions_allowed'],
                'summary': self.state['summary']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--loop', type=float, default=0)
    args = parser.parse_args()
    observer = Observer()
    while True:
        start = time.time()
        try:
            result = observer.cycle()
            if result['new_decisions'] or result['new_settlements'] or observer.state['cycles'] % 60 == 1:
                print(json.dumps(result, ensure_ascii=False), flush=True)
        except Exception as error:
            # Do not checkpoint an interrupted cycle: archive cursors and
            # publication evidence must remain at the last coherent snapshot.
            print(json.dumps({'at': time.time(), 'error': type(error).__name__, 'detail': str(error)}), flush=True)
            raise
        if not args.loop:
            return
        time.sleep(max(.1, args.loop-(time.time()-start)))


if __name__ == '__main__':
    main()
