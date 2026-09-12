"""Explicit forward-method epochs, independent of immutable historical books.

Registration never activates a method. This implementation supports switching only
with no outstanding positions/intents; it deliberately cannot load old Python code.
Old entry payloads are never rewritten to acquire an epoch or a new code hash.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from prediction_market_soccer.ops.maintenance_gate import writer, maintenance_active


class MethodConflict(ValueError):
    pass


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise MethodConflict('Method timestamps require an explicit timezone')
    return parsed.astimezone(timezone.utc)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@contextmanager
def transaction(conn):
    """Own a write transaction, or preserve an explicitly owned outer transaction."""
    owns = not conn.in_transaction
    conn.execute('BEGIN IMMEDIATE' if owns else 'SAVEPOINT forward_method_operation')
    try:
        yield
        conn.commit() if owns else conn.execute('RELEASE forward_method_operation')
    except BaseException:
        if owns:
            conn.rollback()
        else:
            conn.execute('ROLLBACK TO forward_method_operation')
            conn.execute('RELEASE forward_method_operation')
        raise


def ensure(conn):
    for sql in (
        '''CREATE TABLE IF NOT EXISTS forward_method_epoch (
            epoch_id TEXT PRIMARY KEY, manifest_id TEXT NOT NULL UNIQUE,
            model_version TEXT NOT NULL, method_version TEXT NOT NULL,
            created_at TEXT NOT NULL, payload TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS forward_method_activation (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, epoch_id TEXT NOT NULL,
            previous_epoch_id TEXT, book_version_id TEXT NOT NULL,
            activated_at TEXT NOT NULL, reason TEXT NOT NULL)''',
    ):
        conn.execute(sql)
    for table, key in (('forward_method_epoch', 'epoch_id=NEW.epoch_id OR manifest_id=NEW.manifest_id'),
                       ('forward_method_activation', 'sequence=NEW.sequence')):
        for action in ('UPDATE', 'DELETE'):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'Forward method history is immutable'); END")
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {key}) BEGIN SELECT RAISE(ABORT,'Forward method history cannot be replaced'); END")


def runtime_snapshot():
    """Behavior dependencies and actual loaded parameters, never a two-file proxy."""
    from prediction_market_soccer.config import CONFIG
    root = Path(__file__).resolve().parents[1]
    paths = set()
    for directory in ('model','strategy','config','ingest','venues','jobs','util','exec'):
        paths.update((root/directory).rglob('*.py'))
    paths.add(root/'config/club_identity.json')
    for name in ('config/config.py', 'ops/paper_trading.py', 'ops/performance_report.py', 'ops/settle_bets.py', 'ingest/club_prior.py', 'exec/demo_forward.py',
                 'exec/kalshi_mirror.py', 'util/paper_store.py', 'util/forward_methods.py',
                 'util/pricing.py', 'util/quote_evidence.py', 'util/market_identity.py',
                 'util/source_history.py', 'util/timing_provenance.py', 'ops/maintenance_gate.py',
                 'ops/live_refresh.py','ops/upcoming_export.py','ops/inplay_export.py',
                 'ops/inplay_export_advance.py','ops/daily_collection.py','ops/refresh_all.py',
                 'ops/settle_reports.py','ops/proc_lock.py','ops/run_status.py',
                 'ops/export_stage.py','ops/export_status.py'):
        path = root / name
        if not path.is_file():
            raise MethodConflict('Required method dependency is missing: ' + name)
        paths.add(path)
    # Dataclass parameters contain tuples. Compare the same canonical JSON shape
    # before and after durable storage; JSON necessarily decodes tuples as lists.
    from prediction_market_soccer.util.club_identity import identity_manifest_id
    from prediction_market_soccer.config import leagues
    # Capture the values the running process actually uses, not a replacement
    # parse of today's files: the established readers retain process caches.
    fitted={key:leagues.fitted_params(key) for key in sorted(leagues.REGISTRY)}
    altdata={key:leagues.altdata_weights(key) for key in sorted(leagues.REGISTRY)}
    parameter_files={name:(hashlib.sha256((CONFIG.paths.priors/name).read_bytes()).hexdigest()
                           if (CONFIG.paths.priors/name).is_file() else None)
                     for name in ('league_params.json','league_altdata.json')}
    return json.loads(canonical({'code': {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in sorted(paths)},
                                'parameters': {'model': asdict(CONFIG.model), 'decision': asdict(CONFIG.decision),
                                               'risk': asdict(CONFIG.risk),
                                               'league_fitted':fitted,'league_altdata':altdata,
                                               'loaded_fitted_cache':leagues._FITTED_CACHE,
                                               'loaded_altdata_cache':leagues._ALTDATA_CACHE,
                                               'league_parameter_file_hashes':parameter_files},
                                'identity_manifest_id':identity_manifest_id()}))


def build_manifest(*, model_version, method_version, compatible_epoch_ids=()):
    for value in (model_version, method_version):
        if not isinstance(value, str) or not value.strip():
            raise MethodConflict('Explicit model and method identities are required')
    body = {'schema_version': 1, 'model_version': model_version, 'method_version': method_version,
            'runtime': runtime_snapshot(), 'compatible_epoch_ids': sorted(set(compatible_epoch_ids)),
            'rules': {'pre': 'hybrid_value_argmax_selected_offer', 'inplay': 'postevent_poly_then_kalshi',
                      'exit': 'recorded_overshoot_same_contract_bid', 'quote': 'persisted_bbo_v1',
                      'sizing': 'fractional_paper_usd_rounded_0.1_cent', 'fees': 'paper_gross',
                      'strength_bucket': 'first_eligible_observed_cutoff_per_utc_day', 'legacy_inputs': 'unknown_not_backdated'}}
    return {**body, 'manifest_id': digest(body)}


def validate_manifest(manifest, *, check_runtime=False):
    body = deepcopy(manifest)
    claimed = body.pop('manifest_id', None)
    if body.get('schema_version') != 1 or claimed != digest(body):
        raise MethodConflict('Method manifest checksum mismatch')
    if not body.get('model_version') or not body.get('method_version') or not isinstance(body.get('runtime'), dict):
        raise MethodConflict('Incomplete method manifest')
    if not isinstance(body.get('compatible_epoch_ids'), list) or not body.get('rules'):
        raise MethodConflict('Method compatibility/rules are required')
    if check_runtime and body['runtime'] != runtime_snapshot():
        raise MethodConflict('Running code or parameters differ from the recorded forward method')
    return manifest


@writer
def register_epoch(conn, manifest, *, confirmed=False, now=None):
    if confirmed is not True:
        raise MethodConflict('Explicit forward-method registration is required')
    validate_manifest(manifest, check_runtime=True)
    at = now or datetime.now(timezone.utc).isoformat()
    timestamp(at)
    epoch_id = digest({'manifest_id': manifest['manifest_id']})
    with transaction(conn):
        ensure(conn)
        for compatible_id in manifest['compatible_epoch_ids']:
            get_epoch(conn, compatible_id)
        row = conn.execute('SELECT payload FROM forward_method_epoch WHERE epoch_id=?', (epoch_id,)).fetchone()
        if row:
            if json.loads(row[0]) != manifest:
                raise MethodConflict('An epoch identity cannot change')
            return epoch_id
        conn.execute('INSERT INTO forward_method_epoch VALUES (?,?,?,?,?,?)',
                     (epoch_id, manifest['manifest_id'], manifest['model_version'], manifest['method_version'], at, canonical(manifest)))
    return epoch_id


def get_epoch(conn, epoch_id):
    try:
        row = conn.execute('SELECT epoch_id,created_at,payload FROM forward_method_epoch WHERE epoch_id=?', (epoch_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        raise MethodConflict('No forward method has been registered') from exc
    if row is None:
        raise MethodConflict('Unknown forward method epoch')
    manifest = json.loads(row[2])
    validate_manifest(manifest)
    if row[0] != digest({'manifest_id': manifest['manifest_id']}):
        raise MethodConflict('Forward epoch checksum mismatch')
    return {'epoch_id': row[0], 'created_at': row[1], 'manifest': manifest,
            'model_version': manifest['model_version'], 'method_version': manifest['method_version']}


def active_epoch(conn):
    try:
        row = conn.execute('SELECT epoch_id,book_version_id,activated_at FROM forward_method_activation ORDER BY sequence DESC LIMIT 1').fetchone()
    except sqlite3.OperationalError:
        return None
    return {**get_epoch(conn, row[0]), 'book_version_id': row[1], 'activated_at': row[2]} if row else None


def activate_epoch(conn, epoch_id, *, expected_book_version, expected_epoch_id, confirmed=False, now=None):
    if confirmed is not True:
        raise MethodConflict('Explicit forward-method activation is required')
    from prediction_market_soccer.util.frozen_strategy_store import active_version, _require_no_open_positions
    if not maintenance_active():
        raise MethodConflict('Forward method activation requires the exclusive maintenance gate')
    with transaction(conn):
        ensure(conn)
        book = active_version(conn)
        current = active_epoch(conn)
        if book['version_id'] != expected_book_version or (current or {}).get('epoch_id') != expected_epoch_id:
            raise MethodConflict('The book or forward method changed before activation')
        candidate = get_epoch(conn, epoch_id)
        validate_manifest(candidate['manifest'], check_runtime=True)
        if current and current['epoch_id'] == epoch_id and current['book_version_id'] == expected_book_version:
            return current
        if current and current['epoch_id'] != epoch_id:
            old, new = current['manifest'], candidate['manifest']
            if old['runtime'] == new['runtime'] and old['rules'] == new['rules'] and old['compatible_epoch_ids'] == new['compatible_epoch_ids']:
                raise MethodConflict('A new label alone is not a changed forward method')
        _require_no_open_positions(conn)
        at = now or datetime.now(timezone.utc).isoformat()
        if timestamp(at) < max(timestamp(book['activated_at']), timestamp(candidate['created_at'])):
            raise MethodConflict('Forward activation cannot be backdated')
        conn.execute('INSERT INTO forward_method_activation(epoch_id,previous_epoch_id,book_version_id,activated_at,reason) VALUES (?,?,?,?,?)',
                     (epoch_id, (current or {}).get('epoch_id'), book['version_id'], at, 'Explicit forward-method activation; historical book unchanged'))
    return active_epoch(conn)


def validate_entry_epoch(conn, entry, *, require_active=False):
    epoch_id = entry.get('forward_epoch_id')
    if not epoch_id:
        raise MethodConflict('Legacy entry has no certified forward epoch')
    epoch = get_epoch(conn, epoch_id)
    if entry.get('method_manifest') != epoch['manifest']:
        raise MethodConflict('Entry manifest does not match its durable epoch')
    if (entry.get('model_version'), entry.get('method_version')) != (epoch['model_version'], epoch['method_version']):
        raise MethodConflict('Entry method identity mismatch')
    validate_manifest(epoch['manifest'], check_runtime=True)
    if require_active:
        current = active_epoch(conn)
        if not current or current['epoch_id'] != epoch_id or current['book_version_id'] != entry['book_version_id']:
            raise MethodConflict('Entry uses an inactive book or forward method')
        if timestamp(entry['decision_at']) < timestamp(current['activated_at']):
            raise MethodConflict('Entry precedes forward method activation')
    return epoch
