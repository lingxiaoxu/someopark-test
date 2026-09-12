"""Immutable observations and conservative local availability for Soccer only.

Capture content is immutable. Availability markers are written only after the
data transaction commits. Old projection rows are never assigned invented
historical availability. This module performs no network or broker operations.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _utc(value):
    value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Observation timestamps must have an explicit timezone')
    return value.astimezone(timezone.utc)


def ensure(conn):
    statements = [
        '''CREATE TABLE IF NOT EXISTS source_observation_v1 (
            revision_id TEXT PRIMARY KEY, source TEXT NOT NULL, entity_key TEXT NOT NULL,
            captured_at TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
            supersedes_id TEXT, complete INTEGER NOT NULL, raw_ref TEXT, batch_id TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS source_availability_v1 (
            revision_id TEXT PRIMARY KEY REFERENCES source_observation_v1(revision_id),
            available_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS quote_receipt_v1 (
            receipt_id TEXT PRIMARY KEY, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
            raw_ref TEXT NOT NULL, recorded_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS quote_availability_v1 (
            receipt_id TEXT PRIMARY KEY REFERENCES quote_receipt_v1(receipt_id),
            available_at TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS source_lookup_v1 ON source_observation_v1(source, entity_key, captured_at)',
        "CREATE UNIQUE INDEX IF NOT EXISTS daily_strength_identity_v1 ON source_observation_v1(entity_key) WHERE source='derived:daily_strength'",
    ]
    for sql in statements:
        conn.execute(sql)
    for table, key in (('source_observation_v1', 'revision_id'), ('source_availability_v1', 'revision_id'),
                       ('quote_receipt_v1', 'receipt_id'), ('quote_availability_v1', 'receipt_id')):
        for action in ('UPDATE', 'DELETE'):
            conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()}_guard
                BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable observation'); END''')
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_replace_guard BEFORE INSERT ON {table}
            WHEN EXISTS (SELECT 1 FROM {table} WHERE {key}=NEW.{key})
            BEGIN SELECT RAISE(ABORT, 'immutable observation identity'); END''')


def write_content(payload, *, root=None):
    """Durable content-addressed JSON; same-second responses cannot overwrite."""
    if root is None:
        from prediction_market_soccer.config import CONFIG
        root = CONFIG.paths.raw_snapshots / 'objects_v1'
    root = Path(root).resolve()
    data = canonical(payload).encode()
    sha = hashlib.sha256(data).hexdigest()
    directory = root / sha[:2]
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (sha + '.json')
    if target.exists():
        if target.read_bytes() != data:
            raise ValueError('Raw content hash collision or corrupt stored object')
        return target
    fd, tmp = tempfile.mkstemp(prefix='.capture-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        # A concurrent identical writer is harmless; the address fixes the bytes.
        os.replace(tmp, target)
        dirfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return target


def stage_version(conn, source, entity_key, payload, *, captured_at=None,
                  complete=True, raw_ref=None):
    """Stage with the projection transaction; caller commits and finalizes."""
    ensure(conn)
    at = captured_at or now_iso()
    _utc(at)
    key = canonical(entity_key)
    body = canonical(payload)
    sha = hashlib.sha256(body.encode()).hexdigest()
    previous = conn.execute('''SELECT revision_id,payload_hash,complete FROM source_observation_v1
        WHERE source=? AND entity_key=? ORDER BY rowid DESC LIMIT 1''', (source, key)).fetchone()
    if previous and previous[1] == sha and bool(previous[2]) == bool(complete):
        return previous[0]
    batch = getattr(conn, 'source_batch_id', None) or uuid.uuid4().hex
    revision = digest([source, key, at, sha, batch])
    conn.execute('INSERT INTO source_observation_v1 VALUES (?,?,?,?,?,?,?,?,?,?)',
                 (revision, source, key, at, body, sha, previous[0] if previous else None,
                  int(complete), str(raw_ref) if raw_ref else None, batch))
    pending = getattr(conn, 'pending_source_revisions', None)
    if pending is not None:
        pending.add(revision)
    return revision


def finalize_versions(conn, revisions, *, available_at=None):
    """Only call after the data commit. No unrelated transaction may be committed."""
    if conn.in_transaction:
        raise ValueError('Source availability requires an already committed data transaction')
    ensure(conn)
    at = available_at or now_iso()
    _utc(at)
    try:
        for revision in set(revisions):
            row = conn.execute('SELECT captured_at FROM source_observation_v1 WHERE revision_id=?', (revision,)).fetchone()
            if row is None:
                continue  # rolled-back observation
            if _utc(at) < _utc(row[0]):
                raise ValueError('Availability cannot precede capture')
            if not conn.execute('SELECT 1 FROM source_availability_v1 WHERE revision_id=?', (revision,)).fetchone():
                conn.execute('INSERT INTO source_availability_v1 VALUES (?,?)', (revision, at))
        sqlite3.Connection.commit(conn)
    except BaseException:
        sqlite3.Connection.rollback(conn)
        raise


def recover_committed_versions(conn):
    """Finalize committed orphan captures at recovery time, never their old time.

    An interrupted availability commit does not authorize an earlier decision.
    Calling this before an observed-model build makes the next real tick recover.
    """
    if conn.in_transaction:
        raise ValueError('Recovery cannot commit another operation')
    ensure(conn)
    rows = conn.execute('''SELECT o.revision_id FROM source_observation_v1 o
        LEFT JOIN source_availability_v1 a USING(revision_id) WHERE a.revision_id IS NULL''').fetchall()
    if rows:
        finalize_versions(conn, [r[0] for r in rows])
    return len(rows)


class ObservedConnection(sqlite3.Connection):
    """Projection writes retain immutable revisions through existing commit callers."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending_source_revisions = set()
        self.source_batch_id = uuid.uuid4().hex

    def commit(self):
        revisions = set(self.pending_source_revisions)
        super().commit()
        self.pending_source_revisions.clear()
        self.source_batch_id = uuid.uuid4().hex
        if revisions:
            finalize_versions(self, revisions)

    def rollback(self):
        super().rollback()
        self.pending_source_revisions.clear()
        self.source_batch_id = uuid.uuid4().hex


def _read_versions(conn, source, *, cutoff=None, entity_key=None):
    # A reader never ensures/migrates schema and never silently uses current tables.
    sql = '''SELECT o.revision_id,o.entity_key,o.payload,o.payload_hash,o.complete,a.available_at,o.source
        FROM source_observation_v1 o JOIN source_availability_v1 a USING(revision_id) WHERE o.source=?'''
    args = [source]
    if entity_key is not None:
        sql += ' AND o.entity_key=?'; args.append(canonical(entity_key))
    try:
        rows = conn.execute(sql + ' ORDER BY o.rowid', args).fetchall()
    except sqlite3.OperationalError as e:
        if 'no such table' in str(e):
            return []
        raise
    latest = {}
    for row in rows:
        if cutoff is not None and _utc(row[5]) > _utc(cutoff):
            continue
        if hashlib.sha256(row[2].encode()).hexdigest() != row[3]:
            raise ValueError('Source observation hash mismatch')
        latest[row[1]] = {'revision_id': row[0], 'entity_key': json.loads(row[1]),
                         'payload': json.loads(row[2]), 'complete': bool(row[4]),
                         'available_at': row[5], 'source': row[6]}
    return list(latest.values())


def versions_at(conn, source, cutoff):
    return _read_versions(conn, source, cutoff=cutoff)


def event_revision_at(conn, fixture_id, cutoff=None):
    rows = _read_versions(conn, 'fixture_event_set', cutoff=cutoff, entity_key=fixture_id)
    if not rows:
        return None
    row = rows[0]
    row['events'] = row.pop('payload')
    return row


def persist_quote_receipts(conn, receipts, *, raw_root=None, clock=now_iso):
    """Own both commits; transport availability is never trusted."""
    if conn.in_transaction:
        raise ValueError('Persist quote receipts outside the paper decision transaction')
    ensure(conn)
    ids = []
    try:
        for transport in receipts:
            receipt = dict(transport)
            receipt.pop('available_at', None)
            receipt.pop('persisted_available_at', None)
            receipt.pop('raw_ref', None)
            from prediction_market_soccer.util.quote_evidence import validate_receipt
            if not validate_receipt(receipt):
                raise ValueError('Invalid quote capture/hash/binding')
            rid = receipt.get('receipt_id')
            if not isinstance(rid, str) or not rid:
                raise ValueError('Receipt identity is required')
            body = canonical(receipt)
            sha = hashlib.sha256(body.encode()).hexdigest()
            raw_ref = write_content(receipt['raw'], root=raw_root)
            old = conn.execute('SELECT payload_hash FROM quote_receipt_v1 WHERE receipt_id=?', (rid,)).fetchone()
            if old and old[0] != sha:
                raise ValueError('A quote receipt identity cannot change content')
            if not old:
                conn.execute('INSERT INTO quote_receipt_v1 VALUES (?,?,?,?,?)', (rid, body, sha, str(raw_ref), clock()))
            ids.append(rid)
        sqlite3.Connection.commit(conn)
    except BaseException:
        sqlite3.Connection.rollback(conn)
        raise
    recover_committed_quote_receipts(conn, clock=clock, receipt_ids=ids)
    return [resolve_receipt(conn, rid) for rid in ids]


def recover_committed_quote_receipts(conn, *, clock=now_iso, receipt_ids=None):
    """Recover committed captures at NOW after verifying their immutable raw data."""
    if conn.in_transaction:
        raise ValueError('Quote recovery cannot commit another operation')
    ensure(conn)
    sql = '''SELECT q.receipt_id,q.payload,q.payload_hash,q.raw_ref,q.recorded_at
        FROM quote_receipt_v1 q LEFT JOIN quote_availability_v1 a USING(receipt_id)
        WHERE a.receipt_id IS NULL'''
    params = []
    if receipt_ids is not None:
        params = sorted(set(receipt_ids))
        if not params:
            return 0
        sql += ' AND q.receipt_id IN (' + ','.join('?' for _ in params) + ')'
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return 0
    at = clock()
    instant = _utc(at)
    try:
        from prediction_market_soccer.util.quote_evidence import validate_receipt
        for rid, body, sha, raw_ref, recorded_at in rows:
            receipt = json.loads(body)
            if hashlib.sha256(body.encode()).hexdigest() != sha or not validate_receipt(receipt):
                raise ValueError('Recovered quote receipt checksum or binding mismatch')
            if Path(raw_ref).read_bytes() != canonical(receipt['raw']).encode():
                raise ValueError('Recovered quote raw object is corrupt')
            if instant < max(_utc(receipt['received_at']), _utc(recorded_at)):
                raise ValueError('Quote availability cannot precede its capture or data commit')
            conn.execute('INSERT INTO quote_availability_v1 VALUES (?,?)', (rid, at))
        sqlite3.Connection.commit(conn)
    except BaseException:
        sqlite3.Connection.rollback(conn)
        raise
    return len(rows)


def resolve_receipt(conn, receipt_id, cutoff=None):
    try:
        row = conn.execute('''SELECT q.payload,q.payload_hash,q.raw_ref,a.available_at
            FROM quote_receipt_v1 q JOIN quote_availability_v1 a USING(receipt_id)
            WHERE q.receipt_id=?''', (receipt_id,)).fetchone()
    except sqlite3.OperationalError as e:
        if 'no such table' in str(e):
            return None
        raise
    if not row or (cutoff is not None and _utc(row[3]) > _utc(cutoff)):
        return None
    if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
        raise ValueError('Stored quote receipt has changed')
    resolver = getattr(conn, 'source_raw_path', None)
    raw_path = resolver(row[2]) if resolver is not None else Path(row[2])
    if not raw_path.is_file():
        raise ValueError('Stored quote receipt is missing its durable raw object')
    result = json.loads(row[0])
    raw = result['raw']
    if raw_path.read_bytes() != canonical(raw).encode():
        raise ValueError('Stored quote raw object has changed')
    result['available_at'] = row[3]
    result['raw_ref'] = row[2]
    return result


MODEL_TABLES = ('fixture', 'team', 'team_meta', 'club_registry', 'standing',
    'squad', 'player', 'player_stat', 'fixture_stats', 'fixture_player_stats',
    'lineup', 'match_odds', 'nt_recent', 'fc_player', 'tie', 'fixture_event')


def capture_current_sources(conn, *, tables=MODEL_TABLES, captured_at=None):
    """Observe today's projections NOW, never backdate them for historical research.

    Called by the data refresh before constructing derived priors. These complete
    baselines allow subsequent per-row revisions to retain untouched older rows.
    The caller owns commit; an ObservedConnection finalizes after it commits.
    """
    at = captured_at or now_iso()
    ids = []
    for table in tables:
        if table not in MODEL_TABLES:
            raise ValueError('Not a declared Soccer model source')
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            continue
        result = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
        cols = [x[0] for x in result.description]
        rows = [dict(zip(cols, row)) for row in result]
        ids.append(stage_version(conn, 'snapshot:' + table, 'all', rows, captured_at=at))
    return ids


def dependency_manifest(conn, cutoff):
    """Complete version references available at cutoff, independent of current data."""
    try:
        sources = [r[0] for r in conn.execute('SELECT DISTINCT source FROM source_observation_v1')]
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):
            return {'cutoff': cutoff, 'sources': [], 'manifest_id': digest([])}
        raise
    refs = []
    for source in sorted(sources):
        for version in _read_versions(conn, source, cutoff=cutoff):
            refs.append({k: version[k] for k in ('revision_id', 'source', 'entity_key', 'available_at', 'complete')})
    result = {'schema_version': 1, 'cutoff': cutoff, 'sources': refs}
    return {**result, 'manifest_id': digest(result)}


def project_asof(conn, cutoff, *, tables=MODEL_TABLES, required_tables=()):
    """Build a read-only model database from known versions; no current-value fallback.

    Only table definitions are borrowed from the source. A missing historical
    baseline stays missing. Row overlays after the selected complete baseline
    preserve late corrections without making them visible before availability.
    """
    _utc(cutoff)
    view = sqlite3.connect(':memory:')
    view.row_factory = sqlite3.Row
    refs = []
    missing = []
    try:
        for table in tables:
            if table not in MODEL_TABLES:
                raise ValueError('Unknown model projection table')
            schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if schema is None:
                missing.append(table)
                continue
            view.execute(schema[0])
            baseline = _read_versions(conn, 'snapshot:' + table, cutoff=cutoff, entity_key='all')
            rows = baseline[0]['payload'] if baseline and baseline[0]['complete'] else []
            baseline_at = baseline[0]['available_at'] if baseline else None
            versions = _read_versions(conn, 'table:' + table, cutoff=cutoff)
            if baseline:
                refs.append(baseline[0])
            # Individual rows cannot establish that the entire model table was
            # available (or empty). Require a complete baseline for required inputs.
            if not baseline or not baseline[0]['complete']:
                missing.append(table)
            for row in rows:
                if row:
                    cols = list(row)
                    view.execute(f'INSERT INTO "{table}" (' + ','.join('"' + c + '"' for c in cols) + ') VALUES (' +
                                 ','.join('?' for _ in cols) + ')', [row[c] for c in cols])
            for version in versions:
                if baseline_at is not None and _utc(version['available_at']) < _utc(baseline_at):
                    continue
                if not version['complete']:
                    missing.append(table)
                    continue
                row, key = version['payload'], version['entity_key']
                where = ' AND '.join('"' + k + '" IS ?' for k in key)
                view.execute(f'DELETE FROM "{table}" WHERE ' + where, list(key.values()))
                cols = list(row)
                view.execute(f'INSERT INTO "{table}" (' + ','.join('"' + c + '"' for c in cols) + ') VALUES (' +
                             ','.join('?' for _ in cols) + ')', [row[c] for c in cols])
                refs.append(version)
            if table == 'fixture_event':
                for version in _read_versions(conn, 'fixture_event_set', cutoff=cutoff):
                    if baseline_at and _utc(version['available_at']) < _utc(baseline_at):
                        continue
                    if not version['complete']:
                        missing.append(table)
                        continue
                    fid = version['entity_key']
                    view.execute('DELETE FROM fixture_event WHERE fixture_api_id=?', (fid,))
                    for row in version['payload']:
                        cols = list(row)
                        view.execute('INSERT INTO fixture_event (' + ','.join(cols) + ') VALUES (' +
                                     ','.join('?' for _ in cols) + ')', [row[c] for c in cols])
                    refs.append(version)
        unmet = sorted(set(required_tables) & set(missing))
        if unmet:
            raise ValueError('Historical source versions unavailable: ' + ', '.join(unmet))
        view.commit()
        view.execute('PRAGMA query_only=ON')
        manifest = {'schema_version': 1, 'cutoff': cutoff, 'missing_tables': sorted(set(missing)),
                    'source_versions': [{k: v[k] for k in ('revision_id', 'source', 'available_at', 'complete')} for v in refs]}
        return view, {**manifest, 'manifest_id': digest(manifest)}
    except BaseException:
        view.close()
        raise
