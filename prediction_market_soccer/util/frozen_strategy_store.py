"""Versioned, append-only rendered strategy books.

Ordinary refreshes never reconstruct historical rows. A release explicitly seeds
the published book; subsequent rows can only come from the independent forward
paper journal. Replacing a book requires a completed backtest for a deliberately
changed model or trading method. Every previous version remains readable.
"""
from __future__ import annotations

from prediction_market_soccer.ops.maintenance_gate import writer

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import sqlite3

from prediction_market_soccer.util.strategy_ledger import (
    StrategyLedgerUnavailable, build_strategy_ledger, validate_strategy_ledger,
)


class FrozenBookConflict(ValueError):
    """A caller attempted to change a frozen record or the wrong active version."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value):
    if not isinstance(value, str):
        raise FrozenBookConflict('A timestamp is required')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise FrozenBookConflict('Book timestamps must include a timezone')
    return parsed


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def ensure(conn):
    """Idempotent additive schema only; never resets existing tables or commits work."""
    statements = (
        '''CREATE TABLE IF NOT EXISTS strategy_book_version (
            version_id TEXT PRIMARY KEY, model_version TEXT NOT NULL,
            method_version TEXT NOT NULL, origin TEXT NOT NULL,
            created_at TEXT NOT NULL, base_as_of TEXT NOT NULL,
            baseline_ledger_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
            baseline_count INTEGER NOT NULL, metadata_json TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            UNIQUE(model_version, method_version))''',
        '''CREATE TABLE IF NOT EXISTS strategy_book_record (
            version_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            fixture_id INTEGER NOT NULL, record_json TEXT NOT NULL,
            record_sha256 TEXT NOT NULL, source_id TEXT NOT NULL,
            source_sha256 TEXT NOT NULL, recorded_at TEXT NOT NULL,
            settled_at TEXT NOT NULL, origin TEXT NOT NULL,
            PRIMARY KEY(version_id, ordinal), UNIQUE(version_id, fixture_id),
            UNIQUE(version_id, source_id))''',
        '''CREATE TABLE IF NOT EXISTS strategy_book_activation (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, version_id TEXT NOT NULL,
            previous_version_id TEXT, activated_at TEXT NOT NULL,
            operation TEXT NOT NULL, reason TEXT NOT NULL)''',
    )
    for statement in statements:
        conn.execute(statement)
    for table in ('strategy_book_version', 'strategy_book_record', 'strategy_book_activation'):
        for operation in ('UPDATE', 'DELETE'):
            conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_deny_{operation.lower()}
                BEFORE {operation} ON {table} BEGIN
                SELECT RAISE(ABORT, 'Frozen strategy book is append-only'); END''')
    # SQLite REPLACE can bypass DELETE triggers unless recursive triggers are enabled.
    # Guard duplicate inserts explicitly so neither REPLACE nor an upsert can rewrite history.
    for table, predicate in (
        ('strategy_book_version', 'version_id=NEW.version_id OR (model_version=NEW.model_version AND method_version=NEW.method_version)'),
        ('strategy_book_record', 'version_id=NEW.version_id AND (ordinal=NEW.ordinal OR fixture_id=NEW.fixture_id OR source_id=NEW.source_id)'),
        ('strategy_book_activation', 'sequence=NEW.sequence'),
    ):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_deny_replace
            BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {predicate})
            BEGIN SELECT RAISE(ABORT, 'Frozen strategy book cannot replace existing rows'); END''')


@contextmanager
def _transaction(conn):
    # SAVEPOINT preserves a caller's existing transaction; no executescript auto-commit.
    conn.execute('SAVEPOINT frozen_strategy_store')
    try:
        yield
        conn.execute('RELEASE SAVEPOINT frozen_strategy_store')
    except BaseException:
        conn.execute('ROLLBACK TO SAVEPOINT frozen_strategy_store')
        conn.execute('RELEASE SAVEPOINT frozen_strategy_store')
        raise


def _dicts(cursor):
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def active_version(conn):
    """Read the explicitly activated version; never infer it from current code/config."""
    try:
        rows = _dicts(conn.execute('''SELECT v.*, a.activated_at, a.operation
            FROM strategy_book_activation a JOIN strategy_book_version v
            ON v.version_id=a.version_id ORDER BY a.sequence DESC LIMIT 1'''))
    except sqlite3.OperationalError as exc:
        raise StrategyLedgerUnavailable('No frozen strategy book has been initialized') from exc
    if not rows:
        raise StrategyLedgerUnavailable('No frozen strategy book has been initialized')
    return rows[0]


def _report_dict(report):
    if is_dataclass(report):
        report = asdict(report)
    if not isinstance(report, dict):
        raise FrozenBookConflict('An existing report snapshot is required')
    report = deepcopy(report)
    ledger = report.get('strategy_ledger') or build_strategy_ledger(report)
    validate_strategy_ledger(ledger)
    if report.get('bet_log') != ledger['records'] or report.get('as_of') != ledger['as_of']:
        raise FrozenBookConflict('The report and strategy ledger do not match')
    _timestamp(ledger['as_of'])
    _validate_cumulatives(ledger['records'])
    report['strategy_ledger'] = ledger
    return report


def _validate_cumulatives(records):
    """Validate supplied cumulative fields; never repair a historical value."""
    pre = inplay = Decimal(0)
    for record in records:
        p = _number(record.get('realized_pnl_cents') or 0) if record.get('bet') else Decimal(0)
        i = _number(record.get('inplay_pnl_cents') or 0) if record.get('inplay_side') else Decimal(0)
        pre += p
        inplay += i
        for key, expected in (('pre_cum_pnl_cents', pre), ('inplay_cum_pnl_cents', inplay),
                              ('combined_cum_pnl_cents', pre+inplay), ('combined_pnl_cents', p+i)):
            if _number(record.get(key)) != expected:
                raise FrozenBookConflict('A supplied cumulative field does not match the frozen record sequence')


def _version_id(model_version, method_version, ledger_id):
    for value in (model_version, method_version):
        if not isinstance(value, str) or not value.strip():
            raise FrozenBookConflict('Explicit model and method versions are required')
    return _sha(_json([model_version, method_version, ledger_id]))


def _create_version(conn, report, *, model_version, method_version, origin,
                    source_sha256, provenance, created_at):
    ledger = report['strategy_ledger']
    version_id = _version_id(model_version, method_version, ledger['ledger_id'])
    existing = conn.execute('SELECT version_id, source_sha256 FROM strategy_book_version WHERE model_version=? AND method_version=?',
                            (model_version, method_version)).fetchone()
    if existing:
        if tuple(existing) != (version_id, source_sha256):
            raise FrozenBookConflict('This model/method version already has another frozen baseline')
        return version_id
    metadata = {key: value for key, value in report.items() if key not in ('bet_log', 'strategy_ledger')}
    conn.execute('''INSERT INTO strategy_book_version
        (version_id,model_version,method_version,origin,created_at,base_as_of,
         baseline_ledger_id,source_sha256,baseline_count,metadata_json,provenance_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (version_id, model_version, method_version, origin, created_at, ledger['as_of'],
         ledger['ledger_id'], source_sha256, len(ledger['records']), _json(metadata), _json(provenance)))
    for ordinal, record in enumerate(ledger['records']):
        payload = _json(record)
        conn.execute('''INSERT INTO strategy_book_record
            (version_id,ordinal,fixture_id,record_json,record_sha256,source_id,
             source_sha256,recorded_at,settled_at,origin) VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (version_id, ordinal, record['fixture_id'], payload, _sha(payload),
             f'baseline:{ordinal}', source_sha256, created_at, ledger['as_of'], origin))
    return version_id


def seed_published_report(conn, report, *, model_version, method_version,
                          artifact_sha256=None, observed_at=None):
    """Explicit one-time release migration. Never called by an ordinary refresh.

Pass the saved pre-install published artifact, not a reconstruction from fixtures.
An identical repeat is safe; an initialized book cannot be replaced by this path.
"""
    report = _report_dict(report)
    now = observed_at or _now()
    _timestamp(now)
    source_sha256 = artifact_sha256 or _sha(_json(report))
    with _transaction(conn):
        ensure(conn)
        try:
            active = active_version(conn)
        except StrategyLedgerUnavailable:
            active = None
        if active:
            wanted = _version_id(model_version, method_version, report['strategy_ledger']['ledger_id'])
            if active['version_id'] != wanted or active['source_sha256'] != source_sha256:
                raise FrozenBookConflict('An active book exists; use the explicit version workflow')
            return read_book(conn)
        version_id = _create_version(conn, report, model_version=model_version, method_version=method_version,
            origin='published_baseline', source_sha256=source_sha256,
            provenance={'artifact_as_of': report['as_of'], 'artifact_sha256': source_sha256}, created_at=now)
        conn.execute('''INSERT INTO strategy_book_activation
            (version_id,previous_version_id,activated_at,operation,reason) VALUES (?,NULL,?,?,?)''',
            (version_id, now, 'seed', 'Explicit migration of the existing published strategy book'))
    return read_book(conn)


def seed_published_artifact(conn, artifact_path, *, model_version, method_version, observed_at=None):
    """Release convenience: preserve the exact bytes' hash and the already-published rows."""
    from pathlib import Path
    payload = Path(artifact_path).read_bytes()
    return seed_published_report(conn, json.loads(payload), model_version=model_version,
        method_version=method_version, artifact_sha256=hashlib.sha256(payload).hexdigest(), observed_at=observed_at)


def register_backtest_version(conn, report, *, model_version, method_version,
                              backtest_manifest, confirmed_replacement=False,
                              candidate_bundle=None, isolated_root=None):
    """Stage a deliberately rerun model/method version; does not activate it.

The caller must supply the completed run's manifest and explicit authorization.
No daily pipeline calls this operation. A new label by itself is insufficient.
"""
    if confirmed_replacement is not True:
        raise FrozenBookConflict('An explicit replacement request is required')
    report = _report_dict(report)
    from prediction_market_soccer.ops.version_workflow import validate_backtest_bundle
    if candidate_bundle is None or isolated_root is None:
        raise FrozenBookConflict('Registration requires the completed isolated candidate evidence bundle')
    verified = validate_backtest_bundle(candidate_bundle, root=isolated_root)
    if verified['candidate_report.json'] != report or verified['completed_manifest.json'] != backtest_manifest:
        raise FrozenBookConflict('Supplied report/manifest differ from the verified candidate files')
    current = active_version(conn)
    if (model_version, method_version) == (current['model_version'], current['method_version']):
        raise FrozenBookConflict('Replacement requires a changed model or trading method version')
    manifest = dict(backtest_manifest or {})
    required = {'status': 'completed', 'model_version': model_version, 'method_version': method_version,
                'ledger_id': report['strategy_ledger']['ledger_id']}
    if any(manifest.get(key) != value for key, value in required.items()) or not manifest.get('run_id'):
        raise FrozenBookConflict('A matching completed backtest manifest is required')
    _timestamp(manifest.get('completed_at'))
    candidate_input = deepcopy(verified['input_manifest.json']['candidate_input'])
    from prediction_market_soccer.util.redecision_certification import KIND as REDECISION_KIND
    if candidate_input.get('kind') == REDECISION_KIND:
        from pathlib import Path
        from prediction_market_soccer.util.research_inputs import _path
        directory = _path(Path(candidate_bundle), isolated_root, exists=True)
        # Pin the independently verified rendering inputs at registration. Daily
        # exports use these bytes and never rerun the historical model.
        pinned = {}
        certificate = json.loads((directory / 'certification.json').read_text())
        for name in ('input_manifest.json', 'candidate_report.json', 'completed_manifest.json', 'certification.json'):
            path = _path(directory / name, isolated_root, exists=True)
            body = path.read_bytes()
            if name in verified and json.loads(body) != verified[name]:
                raise FrozenBookConflict('Verified candidate changed before registration')
            sha = hashlib.sha256(body).hexdigest()
            if name != 'certification.json' and certificate['documents'].get(name) != sha:
                raise FrozenBookConflict('Candidate document changed after certification')
            pinned[name] = {'path': str(path), 'sha256': sha}
        candidate_input['validated_render_files'] = pinned
    with _transaction(conn):
        ensure(conn)
        return _create_version(conn, report, model_version=model_version, method_version=method_version,
            origin='explicit_backtest', source_sha256=_sha(_json(report)),
            provenance={**manifest, 'candidate_input': candidate_input}, created_at=_now())


def activate_backtest_version(conn, version_id, *, expected_active_version, confirmed_replacement=False):
    from prediction_market_soccer.ops.maintenance_gate import maintenance_active
    if not maintenance_active():
        raise FrozenBookConflict('Book activation requires the exclusive Soccer maintenance coordinator')
    if confirmed_replacement is not True:
        raise FrozenBookConflict('An explicit replacement request is required')
    with _transaction(conn):
        current = active_version(conn)
        if current['version_id'] != expected_active_version:
            raise FrozenBookConflict('The active book changed before replacement')
        _require_no_open_positions(conn)
        candidates = _dicts(conn.execute('SELECT * FROM strategy_book_version WHERE version_id=?', (version_id,)))
        if not candidates or candidates[0]['origin'] != 'explicit_backtest':
            raise FrozenBookConflict('Only a completed explicitly rerun backtest can replace the active book')
        candidate = candidates[0]
        if (candidate['model_version'], candidate['method_version']) == (current['model_version'], current['method_version']):
            raise FrozenBookConflict('Replacement requires a changed model or trading method version')
        read_book(conn, version_id=version_id)  # Verify every frozen row before switching.
        conn.execute('''INSERT INTO strategy_book_activation
            (version_id,previous_version_id,activated_at,operation,reason) VALUES (?,?,?,?,?)''',
            (version_id, current['version_id'], _now(), 'replace', 'Explicit completed-backtest replacement'))
    return read_book(conn)


def restore_version(conn, version_id, *, expected_active_version, confirmed_restore=False):
    """Explicitly restore a previously active version without changing any of its rows."""
    from prediction_market_soccer.ops.maintenance_gate import maintenance_active
    if not maintenance_active():
        raise FrozenBookConflict('Book restoration requires the exclusive Soccer maintenance coordinator')
    if confirmed_restore is not True:
        raise FrozenBookConflict('An explicit restore request is required')
    with _transaction(conn):
        current = active_version(conn)
        if current['version_id'] != expected_active_version:
            raise FrozenBookConflict('The active book changed before restore')
        _require_no_open_positions(conn)
        if not conn.execute('SELECT 1 FROM strategy_book_activation WHERE version_id=?', (version_id,)).fetchone():
            raise FrozenBookConflict('The requested version was never active')
        read_book(conn, version_id=version_id)
        conn.execute('''INSERT INTO strategy_book_activation
            (version_id,previous_version_id,activated_at,operation,reason) VALUES (?,?,?,?,?)''',
            (version_id, current['version_id'], _now(), 'restore', 'Explicit restoration of a preserved book'))
    return read_book(conn)


def _require_no_open_positions(conn):
    """A version switch cannot strand positions that depend on the original model."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # The durable intent is authoritative even if a process died before updating
    # kalshi_mirror. In particular, unfilled/0 in that projection is not proof that
    # a committed POST intent never reached the venue.
    if 'demo_forward_intent' in tables:
        if 'demo_forward_event' not in tables:
            if conn.execute('SELECT 1 FROM demo_forward_intent LIMIT 1').fetchone():
                raise FrozenBookConflict('Demo intents have no outcome journal')
        else:
            attempts = _dicts(conn.execute('''SELECT i.*, e.status AS outcome_status,
                e.payload AS outcome_payload FROM demo_forward_intent i
                LEFT JOIN demo_forward_event e ON e.id=(SELECT MAX(id)
                    FROM demo_forward_event WHERE client_order_id=i.client_order_id)'''))
            held = {}
            for attempt in attempts:
                state = attempt['outcome_status']
                if state not in ('filled', 'unfilled', 'rejected', 'absent'):
                    raise FrozenBookConflict('Resolve every durable pending/unknown demo intent before switching')
                if state != 'filled':
                    continue
                try:
                    receipt = json.loads(attempt['outcome_payload'])
                    count = _number(receipt['count'])
                    if count < 0 or count > _number(attempt['count']) or attempt['action'] not in ('buy', 'sell'):
                        raise ValueError('Invalid fill count/action')
                except (KeyError, TypeError, ValueError) as exc:
                    raise FrozenBookConflict('Demo execution receipts need reconciliation') from exc
                key = attempt['paper_entry_id']
                held[key] = held.get(key, Decimal(0)) + count * (1 if attempt['action'] == 'buy' else -1)
            for entry_id, quantity in held.items():
                if quantity < 0:
                    raise FrozenBookConflict('Demo sell receipts exceed verified buys')
                if not quantity:
                    continue
                terminal = None
                if 'kalshi_mirror' in tables:
                    from prediction_market_soccer.util.demo_settlement import terminal_binary_settlement
                    rows = conn.execute('''SELECT ticker,raw_json FROM kalshi_mirror WHERE status='settled'
                        AND json_extract(raw_json,'$.paper_entry_id')=?''', (entry_id,)).fetchall()
                    terminal = any(terminal_binary_settlement(json.loads(row[1] or '{}').get('demo_settlement'),
                                      ticker=row[0]) is not None for row in rows)
                if not terminal:
                    raise FrozenBookConflict('Verified demo fills still have an unsettled remainder')
    if 'paper_entry' in tables:
        open_paper = conn.execute('''SELECT 1 FROM paper_entry e
            LEFT JOIN paper_exit x ON x.entry_id=e.decision_id
            LEFT JOIN paper_completion c ON c.book_version_id=e.book_version_id AND c.fixture_api_id=e.fixture_api_id
            WHERE x.entry_id IS NULL AND c.fixture_api_id IS NULL LIMIT 1''').fetchone()
        if open_paper:
            raise FrozenBookConflict('Close outstanding paper positions before changing model/method versions')
    if 'kalshi_mirror' in tables:
        open_demo = conn.execute('''SELECT 1 FROM kalshi_mirror
            WHERE status NOT IN ('settled','exited') AND
            (COALESCE(fill_count,0)>COALESCE(exit_fill_count,0) OR status='pending') LIMIT 1''').fetchone()
        if open_demo:
            raise FrozenBookConflict('Resolve outstanding demo positions/orders before changing model/method versions')


def read_book(conn, *, version_id=None):
    """Return exact stored rows in insertion order, with no fixture/model/price reads."""
    if version_id is None:
        version = active_version(conn)
    else:
        try:
            rows = _dicts(conn.execute('SELECT * FROM strategy_book_version WHERE version_id=?', (version_id,)))
        except sqlite3.OperationalError as exc:
            raise StrategyLedgerUnavailable('No frozen strategy book has been initialized') from exc
        if not rows:
            raise StrategyLedgerUnavailable('Unknown frozen strategy book version')
        version = rows[0]
    stored = _dicts(conn.execute('SELECT * FROM strategy_book_record WHERE version_id=? ORDER BY ordinal', (version['version_id'],)))
    records = []
    for ordinal, row in enumerate(stored):
        if row['ordinal'] != ordinal or _sha(row['record_json']) != row['record_sha256']:
            raise FrozenBookConflict('Frozen record sequence or checksum is invalid')
        record = json.loads(row['record_json'])
        if record['fixture_id'] != row['fixture_id']:
            raise FrozenBookConflict('Frozen record fixture identity is invalid')
        records.append(record)
    if len(records) < version['baseline_count']:
        raise FrozenBookConflict('The frozen baseline is incomplete')
    _validate_cumulatives(records)
    basis = json.loads(version['metadata_json']).get('pnl_basis')
    baseline = build_strategy_ledger({'bet_log': records[:version['baseline_count']], 'as_of': version['base_as_of'], 'pnl_basis': basis})
    if baseline['ledger_id'] != version['baseline_ledger_id']:
        raise FrozenBookConflict('Frozen baseline checksum mismatch')
    at = stored[-1]['settled_at'] if len(records) > version['baseline_count'] else version['base_as_of']
    ledger = build_strategy_ledger({'bet_log': records, 'as_of': at, 'pnl_basis': basis})
    ledger['book_version'] = {'version_id': version['version_id'], 'model_version': version['model_version'],
        'method_version': version['method_version'], 'baseline_ledger_id': version['baseline_ledger_id'],
        'baseline_count': version['baseline_count'], 'created_at': version['created_at']}
    return {'version': version, 'ledger': ledger, 'report_metadata': json.loads(version['metadata_json'])}


def _number(value, *, positive=False):
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise FrozenBookConflict('A finite financial value is required') from exc
    if not number.is_finite() or (positive and number <= 0):
        raise FrozenBookConflict('A finite positive financial value is required')
    return number


def _append_completed_row(conn, completion, version):
    """Internal: consume a sealed completion returned by the forward paper store."""
    data = deepcopy(completion)
    if data.get('book_version_id') != version['version_id'] or data.get('origin') != 'paper_forward' or data.get('sealed') is not True:
        raise FrozenBookConflict('Only sealed forward-paper completions for this book can append')
    source_id = data.get('source_id')
    if not isinstance(source_id, str) or not source_id:
        raise FrozenBookConflict('A durable paper completion ID is required')
    source_sha = _sha(_json(data))
    duplicate = conn.execute('SELECT source_sha256 FROM strategy_book_record WHERE version_id=? AND source_id=?',
                             (version['version_id'], source_id)).fetchone()
    if duplicate:
        if duplicate[0] != source_sha:
            raise FrozenBookConflict('A completed paper source changed after freezing')
        return False
    settled = _timestamp(data.get('settled_at'))
    earliest = _timestamp(data.get('entry_decision_at_min'))
    latest = _timestamp(data.get('entry_decision_at_max'))
    if not _timestamp(version['created_at']) <= earliest <= latest <= settled:
        raise FrozenBookConflict('Paper decisions were not recorded during this book version')
    record = data.get('record')
    if not isinstance(record, dict) or record.get('fixture_id') != data.get('fixture_id'):
        raise FrozenBookConflict('Paper completion fixture identity mismatch')
    if not isinstance(record['fixture_id'], int) or isinstance(record['fixture_id'], bool):
        raise FrozenBookConflict('An integer fixture identity is required')
    if conn.execute('SELECT 1 FROM strategy_book_record WHERE version_id=? AND fixture_id=?',
                    (version['version_id'], record['fixture_id'])).fetchone():
        raise FrozenBookConflict('A frozen fixture cannot acquire another or changed strategy row')
    if not record.get('bet') and not record.get('inplay_side'):
        raise FrozenBookConflict('A completion must contain an actual forward paper entry')
    if record.get('result') not in ('home', 'draw', 'away') or not record.get('score'):
        raise FrozenBookConflict('A forward completion requires a sealed result and score')
    # Each newly journaled leg owns its method identity. A PRE/INPLAY pair may
    # straddle epochs after PRE has closed; never replace either with the book label.
    if record.get('leg_methods'):
        from prediction_market_soccer.util.forward_methods import get_epoch
        for track, identity in record['leg_methods'].items():
            entry_id = record.get(track + '_decision_id')
            durable = conn.execute('SELECT payload FROM paper_entry WHERE decision_id=?', (entry_id,)).fetchone()
            if not durable:
                raise FrozenBookConflict('Completed leg is missing its durable paper entry')
            payload = json.loads(durable[0])
            expected = {key: payload.get(key) for key in ('forward_epoch_id','model_version','method_version','method_manifest')}
            if identity != expected or any(record.get(track + '_' + key) != value for key,value in expected.items()):
                raise FrozenBookConflict('Completed leg method identity differs from its entry')
            if payload.get('forward_epoch_id'):
                epoch = get_epoch(conn, payload['forward_epoch_id'])
                if epoch['manifest'] != payload['method_manifest']:
                    raise FrozenBookConflict('Completed leg epoch manifest changed')
    pre = inplay = Decimal(0)
    for entered, side, entry, stake, pnl in (
        (bool(record.get('bet')), 'pick', 'entry_cents', 'stake_usd', 'realized_pnl_cents'),
        (bool(record.get('inplay_side')), 'inplay_side', 'inplay_entry_cents', 'inplay_stake_usd', 'inplay_pnl_cents'),
    ):
        if not entered:
            if record.get(pnl) not in (None, 0):
                raise FrozenBookConflict('A non-entered paper track cannot carry realized P&L')
            continue
        if record.get(side) not in ('home', 'draw', 'away') or not 0 < _number(record.get(entry)) <= 100:
            raise FrozenBookConflict('Forward entry side/price is invalid')
        _number(record.get(stake), positive=True)
        value = _number(record.get(pnl))
        if pnl == 'realized_pnl_cents':
            pre = value
        else:
            inplay = value
    last = conn.execute('SELECT ordinal,record_json,settled_at FROM strategy_book_record WHERE version_id=? ORDER BY ordinal DESC LIMIT 1',
                         (version['version_id'],)).fetchone()
    previous = json.loads(last[1]) if last else {}
    ordinal = last[0] + 1 if last else 0
    # Only a new row receives newly calculated cumulatives. Historical prefixes stay byte-identical.
    for key, value in (('pre_cum_pnl_cents', pre), ('inplay_cum_pnl_cents', inplay), ('combined_cum_pnl_cents', pre+inplay)):
        record[key] = float(_number(previous.get(key, 0)) + value)
    record['combined_pnl_cents'] = float(pre+inplay)
    record['realized_cum_pnl_cents'] = record['pre_cum_pnl_cents']
    payload = _json(record)
    # Publication time must not run backward for delayed completions.
    publication_at = max((data['settled_at'], last[2] if last else version['base_as_of']), key=_timestamp)
    conn.execute('''INSERT INTO strategy_book_record
        (version_id,ordinal,fixture_id,record_json,record_sha256,source_id,source_sha256,
         recorded_at,settled_at,origin) VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (version['version_id'], ordinal, record['fixture_id'], payload, _sha(payload), source_id,
         source_sha, _now(), publication_at, 'paper_forward'))
    return True


@writer
def consume_completed_paper(conn):
    """Append only the independent paper lifecycle's sealed results, never demo fills."""
    version = active_version(conn)
    from prediction_market_soccer.util import paper_store
    completed = list(paper_store.completed_records(conn, version_id=version['version_id']))
    completed.sort(key=lambda row: (_timestamp(row['settled_at']), row['source_id']))
    added = 0
    with _transaction(conn):
        if active_version(conn)['version_id'] != version['version_id']:
            raise FrozenBookConflict('The active book changed while loading paper completions')
        for row in completed:
            added += int(_append_completed_row(conn, row, version))
    return added
