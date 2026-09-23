"""Prospective W10 entry gate, isolated from every running trading strategy.

Reuse pure source adapters and receipt-clock features, never the live W8 runner.
The accounting unit is an entire tilted market including every management leg.
This remains a conditional parent cohort, not an independent execution engine.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import time

from ..downside_paper.features import compute_features
from ..downside_paper.sources import (SourceNotReady, make_parent_binding,
    w8_candidate_from_order, w8_existing_tickers, w8_outcomes_from_state)
from ..downside_paper.tape import ExistingTape, JsonlTail
from . import policy
from .diagnostics import collect_quote_diagnostics

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'trading_signals/live_watch/w8_complete_set_state.json'
RUNTIME = ROOT / 'trading_signals/w10_entry_paper'
PARAMETERS = Path(__file__).with_name('parameters_v1.json')
PARENT_FILES = [ROOT/'crypto_strategies/live_watch'/n for n in
                ('w8_complete_set.py', 'common.py', 'config.yaml')]
PARENT_FILES += [ROOT/'crypto_strategies/event_binary/complete_set.py']


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_hashes(paths):
    return {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def own_files():
    shared = Path(__file__).parent.parent/'downside_paper'
    return sorted(list(Path(__file__).parent.glob('*.py')) + [PARAMETERS] +
                  [shared/n for n in ('__init__.py', 'features.py', 'sources.py', 'tape.py', 'policy.py')])


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_journal(path, records, cache=None):
    """Repair only our own incomplete final line; state retains authoritative data."""
    cached = (cache or {}).get(str(path))
    stat = path.stat() if path.exists() else None
    signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns) if stat else None
    existing = cached['ids'] if cached and cached['signature'] == signature else set()
    if path.exists() and not (cached and cached['signature'] == signature):
        good = 0
        with path.open('rb+') as stream:
            while line := stream.readline():
                if not line.endswith(b'\n'):
                    # Preserve torn bytes for diagnosis before repairing this local journal.
                    with path.with_suffix('.torn').open('ab') as quarantine:
                        quarantine.write(line)
                    stream.truncate(good)
                    break
                event = json.loads(line)
                existing.add(event['event_id'])
                good = stream.tell()
    additions = [r for r in records if r['event_id'] not in existing]
    if additions:
        with path.open('a') as stream:
            for row in additions:
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        existing.update(r['event_id'] for r in additions)
    if cache is not None:
        stat = path.stat() if path.exists() else None
        cache[str(path)] = {'ids': existing,
            'signature': (stat.st_ino, stat.st_size, stat.st_mtime_ns) if stat else None}


def log_paths(now):
    date = datetime.fromtimestamp(now, timezone.utc)
    return [SOURCE.parent/('log_'+(date-timedelta(days=d)).strftime('%Y-%m-%d')+'.jsonl') for d in (1, 0)]


def timestamp(value):
    if isinstance(value, bool):
        raise ValueError('boolean timestamp')
    if isinstance(value, (float, int)):
        number = float(value)
    else:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('source timestamp requires a timezone')
        number = parsed.timestamp()
    if not math.isfinite(number) or number < 0:
        raise ValueError('invalid timestamp')
    return number


def record_candidate(state, candidate, feature, observed_at, parameters, source_event):
    identifier = candidate['id']
    if identifier in state['episodes']:
        return False
    lag = observed_at-candidate['decision_ts']
    if not 0 <= lag <= parameters['source_discovery_max_delay_seconds'] or observed_at >= candidate['expires_ts']:
        decision = legacy = {'decision': 'unclassified', 'reason': 'late_source_discovery'}
    else:
        decision = policy.decide(candidate, feature, parameters)
        legacy = policy.legacy_decide(candidate, feature, parameters)
    state['episodes'][identifier] = {
        'candidate': candidate, 'features': feature, 'decision': decision,
        'legacy_decision': legacy, 'original_event': source_event,
        'observed_at': observed_at, 'discovery_lag_seconds': lag,
        'decision_published_at': None, 'outcome': None,
        'first_parent_fill_ts': None,
    }
    return True


class Observer:
    def __init__(self, output=RUNTIME, *, clock=time.time):
        self.clock = clock
        self.output = Path(output)
        # No CLI output override. Tests use temporary paths outside the repository.
        resolved, repo = self.output.resolve(), ROOT.parent.resolve()
        if (resolved == repo or repo in resolved.parents) and resolved != RUNTIME.resolve():
            raise ValueError('W10 writes must use its own dedicated runtime directory')
        if resolved in repo.parents:
            raise ValueError('W10 cannot write at an ancestor of the repository')
        self.output.mkdir(parents=True, exist_ok=True)
        self.lock = (self.output/'observer.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.parameters = json.loads(PARAMETERS.read_text())
        if self.parameters['policy'] != policy.POLICY_VERSION:
            raise ValueError('policy/version mismatch')
        for field in ('network_enabled', 'demo_orders_enabled', 'prod_orders_enabled', 'frontend_enabled'):
            if self.parameters.get(field) is not False:
                raise ValueError('W10 is local paper only')
        self.tape = ExistingTape(ROOT/'price_data/hyperliquid', assets=('BTC','ETH','DOGE','XRP'))
        self.logs = JsonlTail()
        self.state_path = self.output/'state.json'
        self.journal_index = {}
        self.parent_signature = None
        self.parent = None
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            if self.state['parameters_sha256'] != digest(self.parameters):
                raise ValueError('changed parameters require a new registration')
            self.logs.cursors = {k: tuple(v) for k,v in self.state.get('log_cursors', {}).items()}
            for row in self.state['episodes'].values():
                if row.get('decision_published_at') is None:
                    row['pit_evidence_issue'] = 'decision_publication_not_confirmed_before_restart'
        else:
            if (self.output/'registration.json').exists():
                raise ValueError('registered state is missing: do not reset the existing experiment')
            parent = self.load_parent()
            before = self.clock()
            for path in log_paths(before):
                self.logs.seed_end(path)
            registered = self.clock()
            self.state = {
                'version': policy.POLICY_VERSION, 'strategy': 'w10',
                'mode': self.parameters['mode'], 'registered_at': registered,
                'registered_utc': datetime.fromtimestamp(registered, timezone.utc).isoformat(),
                'parameters': self.parameters, 'parameters_sha256': digest(self.parameters),
                'parent_binding': make_parent_binding('w8', parent),
                'parent_source_sha256': file_hashes(PARENT_FILES),
                'own_source_sha256': file_hashes(own_files()),
                'startup_excluded_tickers': sorted(w8_existing_tickers(parent)),
                'episodes': {}, 'quote_diagnostics': {}, 'pending_events': [],
                'source_errors': [], 'unresolved_source_tickers': {}, 'blocked_source_events': {},
                'cycles': 0, 'last_tick': None, 'log_cursors': self.logs.cursors,
                'historical_research_pnl_included': False,
                'limitations': [
                    'Conditional whole W8 tilted market cohort, not an independent fill/queue/inventory simulation.',
                    'Paper fills and all management legs/fees inherited; paired control never added.',
                    'W8 registered hashes differ from disk; process loaded bytes remain unverified.',
                    'Observed print flow is sampled, not full venue volume.',
                    'Unknown, late, revised and unverified episodes excluded from matched economics.',
                    'Replacement quote diagnostics produce no counterfactual profit.',
                    'Review after 300 matched parent-filled expiry windows; no repeated rule selection.'
                ],
            }
            atomic_json(self.output/'registration.json', self.state)
            atomic_json(self.state_path, self.state)

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
        self.state['source_checks'] = {
            'parent_binding_matches': make_parent_binding('w8', parent) == self.state['parent_binding'],
            'parent_disk_unchanged': file_hashes(PARENT_FILES) == self.state['parent_source_sha256'],
            'observer_and_adapters_unchanged': file_hashes(own_files()) == self.state['own_source_sha256'],
            'parent_heartbeat_fresh': age is not None and 0 <= age <= self.parameters['source_heartbeat_max_age_seconds'],
            'parent_entries_unchanged': not any(e.get('source_candidate_changed') for e in self.state['episodes'].values()),
        }
        self.state['parent_heartbeat_age_seconds'] = age
        self.state['new_admissions_allowed'] = all(self.state['source_checks'].values())

    def error(self, now, ticker, reason, detail=None):
        value = {'at': now, 'ticker': ticker, 'reason': reason, 'detail': detail}
        self.state['source_errors'].append(value)
        if ticker:
            self.state['unresolved_source_tickers'].setdefault(ticker, value)

    def audit_active(self, parent, now):
        positions = parent.get('books', {}).get('tilted', {}).get('positions', {})
        for row in self.state['episodes'].values():
            ticker = row['candidate']['ticker']
            market = positions.get(ticker)
            if market is None:
                continue
            try:
                observed = w8_candidate_from_order(row['original_event'], parent, self.state['parent_binding'],
                    since_ts=self.state['registered_at'], until_ts=now)
                if observed != row['candidate']:
                    raise ValueError('first quote fields changed without a new registration')
            except (ValueError, KeyError, TypeError) as error:
                row['source_candidate_changed'] = True
                row.setdefault('source_candidate_revision', {'at': now, 'error': str(error)})
                self.state['source_checks']['parent_entries_unchanged'] = False
                self.state['new_admissions_allowed'] = False
            for fill in market.get('fills', []):
                try:
                    at = timestamp(fill['ts'])
                except (KeyError, ValueError, TypeError):
                    row['pit_evidence_issue'] = 'invalid_parent_fill_timestamp'
                    continue
                if at > now:
                    # The atomic parent snapshot can be newer than this cycle's
                    # frozen clock. Audit this fill on the next cycle instead.
                    continue
                if at < row['candidate']['decision_ts']:
                    row['pit_evidence_issue'] = 'parent_fill_precedes_first_quote'
                    continue
                previous = row['first_parent_fill_ts']
                row['first_parent_fill_ts'] = at if previous is None else min(at, previous)
            published, first = row.get('decision_published_at'), row.get('first_parent_fill_ts')
            if published is not None and first is not None and first <= published:
                row['pit_evidence_issue'] = 'parent_fill_at_or_before_decision_publication'

    def persist(self, active_tickers=None, settled_tickers=None):
        pending = [r for r in self.state['episodes'].values()
                   if r.get('decision_published_at') is None and not r.get('pit_evidence_issue')]
        atomic_json(self.state_path, self.state)
        if pending:
            # This timestamp follows durable atomic publication of the decisions.
            published = self.clock()
            for row in pending:
                row['decision_published_at'] = published
                first = row.get('first_parent_fill_ts')
                if first is not None and first <= published:
                    row['pit_evidence_issue'] = 'parent_fill_at_or_before_decision_publication'
            self.state['summary'] = policy.summarize(self.state['episodes'])
            atomic_json(self.state_path, self.state)
        decisions, settlements, revisions = [], [], []
        for identifier, row in self.state['episodes'].items():
            decisions.append({'event_id': 'decision:'+identifier, **{k:v for k,v in row.items()
                if k not in ('outcome', 'outcome_revision', 'first_parent_fill_ts', 'source_candidate_revision')}})
            if row.get('outcome'):
                settlements.append({'event_id': 'settlement:'+identifier, **row['outcome']})
            for key in ('pit_evidence_issue', 'source_candidate_revision', 'outcome_revision'):
                if row.get(key):
                    revisions.append({'event_id': 'audit:'+identifier+':'+key+':'+digest(row[key]),
                                      'episode_id': identifier, 'kind': key, 'evidence': row[key]})
        sync_journal(self.output/'decisions.jsonl', decisions, self.journal_index)
        sync_journal(self.output/'settlements.jsonl', settlements, self.journal_index)
        sync_journal(self.output/'audit_events.jsonl', revisions, self.journal_index)
        sync_journal(self.output/'quote_diagnostics.jsonl',
                     [{'event_id': key, **r} for key,r in self.state['quote_diagnostics'].items()], self.journal_index)
        if active_tickers is not None and settled_tickers is not None:
            archived = set(settled_tickers)-set(active_tickers)
            retained = {key:r for key,r in self.state['quote_diagnostics'].items()
                        if r.get('ticker') not in archived}
            pruned = len(self.state['quote_diagnostics'])-len(retained)
            if pruned:
                # Archive fsync must precede pruning. A crash before this save
                # replays the same IDs safely, without losing diagnostic rows.
                self.state['archived_quote_diagnostic_records'] = self.state.get('archived_quote_diagnostic_records',0)+pruned
                self.state['archived_quote_tickers'] = sorted(set(self.state.get('archived_quote_tickers', [])) |
                    {r['ticker'] for r in self.state['quote_diagnostics'].values() if r.get('ticker') in archived})
                self.state['quote_diagnostics'] = retained
                atomic_json(self.state_path, self.state)

    def cycle(self):
        now = self.clock()
        parent = self.load_parent()
        self.check_sources(parent, now)
        self.tape.update(now)
        if self.state['source_checks']['parent_binding_matches']:
            self.audit_active(parent, now)
        events = self.state['pending_events'][:]
        for path in log_paths(now):
            events.extend(self.logs.read(path))
        self.state['pending_events'] = []
        new_decisions = new_settlements = 0
        for raw in events:
            event = raw.get('event', raw)
            if not isinstance(event, dict):
                self.error(now, None, 'invalid_source_event', 'event wrapper is not a mapping')
                continue
            if (event.get('strategy'), event.get('action'), event.get('book')) != ('w8_complete_set','paper_order','tilted'):
                continue
            intent = event.get('intent')
            if not isinstance(intent, dict):
                self.error(now, None, 'invalid_source_order', 'intent is not a mapping')
                continue
            ticker = intent.get('ticker')
            if ticker in self.state['startup_excluded_tickers']:
                continue
            if intent.get('client_order_id') != 'w8-paper-'+str(ticker)+':1':
                continue
            if not self.state['new_admissions_allowed']:
                self.state['blocked_source_events'].setdefault(str(ticker), {'at':now,'event':event,'source_checks':self.state['source_checks'].copy()})
                continue
            try:
                if timestamp(event.get('ts')) > now:
                    self.state['pending_events'].append(raw)
                    continue
                candidate = w8_candidate_from_order(event, parent, self.state['parent_binding'],
                    since_ts=self.state['registered_at'], until_ts=now)
            except SourceNotReady:
                raw.setdefault('_w10_first_seen', now)
                if now-raw['_w10_first_seen'] <= self.parameters['source_discovery_max_delay_seconds']:
                    self.state['pending_events'].append(raw)
                else:
                    self.error(now, ticker, 'order_state_not_ready_before_deadline')
                continue
            except (ValueError, KeyError, TypeError) as error:
                self.error(now, ticker, 'invalid_source_order', str(error))
                continue
            if candidate is None:
                if timestamp(event.get('ts')) >= self.state['registered_at']:
                    self.error(now, ticker, 'source_candidate_not_admitted')
                continue
            prior = self.state['episodes'].get(candidate['id'])
            if prior:
                if prior['candidate'] != candidate:
                    prior['source_candidate_changed'] = True
                    prior['source_candidate_revision'] = {'at': now, 'candidate': candidate}
                    self.state['new_admissions_allowed'] = False
                continue
            feature = compute_features(*self.tape.for_asset(candidate['asset']), candidate['decision_ts'])
            new_decisions += record_candidate(self.state, candidate, feature, self.clock(), self.parameters, event)
        if self.state['source_checks']['parent_binding_matches']:
            self.audit_active(parent, now)
            diagnostics = collect_quote_diagnostics(parent,
                self.state['startup_excluded_tickers']+self.state.get('archived_quote_tickers', []),
                self.state['registered_at'], now, self.clock(), self.state['quote_diagnostics'],
                lambda asset, at: compute_features(*self.tape.for_asset(asset), at))
            for record in diagnostics['records']:
                self.state['quote_diagnostics'].setdefault(record['id'], record)
            for error in diagnostics['errors']:
                self.error(now, error.get('ticker'), 'quote_diagnostic_error', error)
            for outcome in w8_outcomes_from_state(parent, self.state['parent_binding'], until_ts=now):
                row = self.state['episodes'].get(outcome['id'])
                if row is None:
                    continue
                if not outcome['metadata']['zero_fill'] and row.get('first_parent_fill_ts') is None:
                    row['pit_evidence_issue'] = 'parent_fill_timing_not_observed_before_settlement'
                if row['outcome'] is None:
                    row['outcome'] = outcome
                    new_settlements += 1
                elif row['outcome'] != outcome:
                    row['outcome_revision'] = outcome
                    row['source_outcome_changed'] = True
        self.state['summary'] = policy.summarize(self.state['episodes'])
        self.state['cycles'] += 1
        self.state['last_tick'] = now
        self.state['last_tick_utc'] = datetime.fromtimestamp(now, timezone.utc).isoformat()
        self.state['log_cursors'] = self.logs.cursors
        self.state['source_candidate_unresolved_count'] = len(self.state['unresolved_source_tickers'])
        self.state['blocked_source_candidate_count'] = len(self.state['blocked_source_events'])
        self.state['source_errors'] = self.state['source_errors'][-1000:]
        self.state['parse_errors'] = {'features':self.tape.tail.errors,'orders':self.logs.errors}
        book = parent.get('books', {}).get('tilted', {})
        self.persist(set(book.get('positions', {})), {r['ticker'] for r in book.get('trades', [])})
        return {'at': now, 'new_decisions': new_decisions, 'new_settlements': new_settlements,
                'new_admissions_allowed': self.state['new_admissions_allowed'], 'summary': self.state['summary']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--loop', type=float, default=0)
    args = parser.parse_args()
    observer = Observer()
    while True:
        start = time.time()
        try:
            result = observer.cycle()
            if result['new_decisions'] or result['new_settlements'] or observer.state['cycles'] % 30 == 1:
                print(json.dumps(result, ensure_ascii=False), flush=True)
        except Exception as error:
            observer.state['last_error'] = {'at':time.time(),'type':type(error).__name__,'detail':str(error)}
            atomic_json(observer.state_path, observer.state)
            print(json.dumps({'error':observer.state['last_error']}, ensure_ascii=False), flush=True)
            if not args.loop:
                raise
        if not args.loop:
            return
        time.sleep(max(.1, args.loop-(time.time()-start)))


if __name__ == '__main__':
    main()
