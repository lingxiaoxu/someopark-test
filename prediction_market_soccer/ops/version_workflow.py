"""Rehearse a version switch and crash recovery in an explicit isolated directory.

SQLite and six files do not have a joint commit. Admission stays closed after any
interruption until real database/file state is reconciled. This workflow does not
deploy a website, change source code, stop services, or place/cancel orders.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from prediction_market_soccer.ops.maintenance_gate import maintenance_gate, finish_maintenance
from prediction_market_soccer.ops.run_status import atomic_bytes, atomic_json
from prediction_market_soccer.util.research_inputs import _path, digest


NAMES = ('performance_report.json', 'milestone_marks.json', 'performance_report.pdf')


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _require_runtime(recovery_dir, state):
    """Recovery restores DB/files only; a code or parameter change needs reconciliation."""
    from prediction_market_soccer.util.forward_methods import runtime_snapshot
    saved = state.get('runtime')
    if not saved or runtime_snapshot() != saved:
        raise ValueError('Running code or parameters changed; restore requires explicit runtime reconciliation')
    for name, expected in saved['code'].items():
        path = _path(Path(recovery_dir) / 'code' / name, recovery_dir, exists=True)
        if _sha(path) != expected:
            raise ValueError('Recovery code evidence checksum mismatch')


def _execution_hash(conn, *, include_methods=True):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = {}
    for name in ('paper_entry', 'paper_exit', 'paper_completion', 'demo_forward_intent',
                 'demo_forward_event', 'kalshi_mirror', 'forward_method_activation'):
        if name == 'forward_method_activation' and not include_methods:
            continue
        rows[name] = sorted([list(r) for r in conn.execute('SELECT * FROM ' + name)], key=str) if name in tables else []
    return digest(rows)


def head(conn, directories):
    from prediction_market_soccer.util.frozen_strategy_store import read_book
    book = read_book(conn)
    ledger = book['ledger']
    return {'version_id': book['version']['version_id'], 'ledger_id': ledger['ledger_id'],
            'as_of': ledger['as_of'], 'records_hash': digest(ledger['records']),
            'fixture_ids': [r['fixture_id'] for r in ledger['records']],
            'execution_hash': _execution_hash(conn),
            'files': {str(i) + '/' + name: _sha(Path(directory) / name)
                      for i, directory in enumerate(directories) for name in NAMES}}


def validate_backtest_bundle(bundle, *, root):
    directory = _path(bundle, root, exists=True)
    docs = {name: json.loads((directory / name).read_text()) for name in
            ('input_manifest.json', 'candidate_report.json', 'completed_manifest.json')}
    inputs, report, manifest = (docs[n] for n in ('input_manifest.json', 'candidate_report.json', 'completed_manifest.json'))
    if inputs.get('analysis_purpose') == 'observed_forward_exact_replay_v1':
        # A narrow, independently verified replay of complete observed decisions.
        # Generic historical research cannot opt in by changing a PIT boolean.
        from prediction_market_soccer.util.forward_replay_certification import validate_forward_replay_bundle
        return validate_forward_replay_bundle(directory, root=root)
    from prediction_market_soccer.ops.owner_authorized_correction import KIND as OWNER_KIND
    if inputs.get('analysis_purpose') == OWNER_KIND:
        # The owner-ordered leak correction of 2026-09-11: a recorded authorisation, prices
        # from the repair's own collector at the corrected clock, decisions/rules/model held
        # fixed, strict_pit_certified kept False, forward rows proven unchanged. Validated by
        # its own module; it cannot be reached by relabelling generic research.
        from prediction_market_soccer.ops.owner_authorized_correction import validate_bundle
        return validate_bundle(directory, root=root)
    from prediction_market_soccer.util.redecision_certification import KIND as REDECISION_KIND
    if inputs.get('analysis_purpose') == REDECISION_KIND:
        from prediction_market_soccer.util.redecision_certification import validate_redecision_bundle
        verified = validate_redecision_bundle(directory, root=root)
        completed = verified['completed_manifest.json']
        for flag in ('audit_complete', 'input_causal_complete', 'financial_complete',
                     'strict_pit_certified', 'activation_eligible'):
            if completed.get(flag) is not True:
                raise ValueError('Observed-input redecision is incomplete and cannot replace frozen history: ' + flag)
        if (not verified['candidate_report.json']['bet_log']
                or any(row['status'] not in ('entered', 'no_edge') for row in completed['eligibility'])):
            raise ValueError('Replacement requires complete scored opportunities and nonempty financial records')
        return verified
    reference = inputs.get('candidate_input') or {}
    from prediction_market_soccer.util.research_inputs import CandidateMarketData, SourceSnapshot
    path = _path(Path(reference.get('path', '')), root, exists=True)
    if reference.get('root') != str(Path(root).resolve()) or _sha(path) != reference.get('sha256'):
        raise ValueError('Source candidate path/hash differs from the pinned backtest input')
    source = None
    if reference.get('source_path'):
        if reference.get('source_root', str(root)) != str(Path(root).resolve()):
            raise ValueError('Source snapshot escaped the isolated root')
        source = SourceSnapshot(Path(reference['source_path']), root=root, raw_archive=reference.get('raw_archive'))
        if source.manifest['sha256'] != reference.get('source_sha256'):
            source.close()
            raise ValueError('Backtest source snapshot hash mismatch')
    data = CandidateMarketData(path, root=root, run_id=reference['run_id'], scope_id=reference['scope_id'], source=source)
    try:
        data.verify_method_code()
        data.parameters()
        if (data.completion['input_manifest_hash'] != inputs.get('candidate_input_hash')
                or data.completion['items_hash'] != inputs.get('candidate_items_hash')
                or data.manifest['fixture_ids'] != inputs.get('fixture_ids')):
            raise ValueError('Completed backtest references another candidate input run')
    finally:
        data.close()
        if source is not None:
            source.close()
    from prediction_market_soccer.util.strategy_ledger import validate_strategy_ledger
    ledger = validate_strategy_ledger(report.get('strategy_ledger'))
    if (report.get('bet_log') != ledger['records'] or manifest.get('ledger_hash') != digest(ledger)
            or manifest.get('ledger_id') != ledger['ledger_id']
            or report.get('input_manifest_hash') != digest(inputs)
            or manifest.get('input_manifest_hash') != digest(inputs)):
        raise ValueError('Backtest input/report/ledger evidence chain mismatch')
    ids, tracks = inputs['fixture_ids'], inputs['tracks']
    expected = {(fid, track) for fid in ids for track in tracks}
    eligibility = manifest.get('eligibility', [])
    actual = [(r['fixture_id'], r['track']) for r in eligibility]
    if (len(ids) != len(set(ids)) or len(actual) != len(set(actual)) or set(actual) != expected
            or manifest.get('scope_total') != len(expected) or manifest.get('scope_hash') != digest(ids)):
        raise ValueError('Backtest scope does not cover every declared fixture and track')
    if report.get('eligibility') != eligibility:
        raise ValueError('Backtest report eligibility differs from its completion manifest')
    if any(r['status'] not in ('entered', 'no_edge') for r in eligibility):
        raise ValueError('A partially unscored backtest cannot replace the frozen book')
    if not ledger['records'] or manifest.get('activation_eligible') is not True:
        raise ValueError('Backtest is not eligible for historical replacement')
    if manifest.get('strict_pit_certified') is not True:
        raise ValueError('Reference-only history cannot replace the book as repaired PIT results')
    if manifest.get('status') != 'completed' or not manifest.get('run_id'):
        raise ValueError('Completed backtest identity required')
    for key in ('model_version', 'method_version'):
        if not inputs.get(key) or inputs[key] != manifest.get(key):
            raise ValueError('Backtest method identity mismatch')
    # Generic research has no independent certification protocol. Even a locally
    # rewritten, internally consistent set of booleans/hashes cannot authorize
    # replacing history. Only the explicitly dispatched observed replay above can.
    raise ValueError('Generic historical research has no independent PIT certification; activation is forbidden')


def _validate_files(directories, ledger):
    from prediction_market_soccer.util.strategy_ledger import validate_strategy_ledger
    hashes = []
    for directory in directories:
        directory = Path(directory)
        report = json.loads((directory / NAMES[0]).read_text())
        marks = json.loads((directory / NAMES[1]).read_text())
        if (validate_strategy_ledger(report.get('strategy_ledger')) != ledger
                or report.get('bet_log') != ledger['records']
                or validate_strategy_ledger(marks.get('strategy_ledger')) != ledger):
            raise ValueError('Financial artifacts disagree with the intended complete book')
        pdf = (directory / NAMES[2]).read_bytes()
        if not pdf.startswith(b'%PDF-'):
            raise ValueError('Missing rendered PnL PDF')
        from prediction_market_soccer.ops.performance_report import validate_strategy_views
        validate_strategy_views(directory, ledger)
        hashes.append({name: _sha(directory / name) for name in NAMES})
    if len(hashes) != 2 or hashes[0] != hashes[1]:
        raise ValueError('The two artifact directories differ')
    return hashes[0]


def _isolated(conn, root, directories, recovery_dir):
    root = Path(root).resolve(strict=True)
    if (root / '.soccer-isolated').read_text().strip() != 'soccer-isolated-v1':
        raise ValueError('Version rehearsals require an explicit isolated workspace')
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    _path(Path(database), root, exists=True)
    if conn.in_transaction:
        raise ValueError('Version workflow requires its own transactions')
    directories = [_path(Path(p), root, exists=True) for p in directories]
    if len(directories) != 2 or directories[0] == directories[1]:
        raise ValueError('Two distinct isolated artifact directories required')
    recovery_dir = _path(Path(recovery_dir), root)
    return root, directories, recovery_dir


def _pending_completion(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'paper_completion' in tables:
        row = conn.execute('''SELECT 1 FROM paper_completion c LEFT JOIN strategy_book_record r
            ON r.source_id=c.source_id AND r.version_id=c.book_version_id WHERE r.source_id IS NULL LIMIT 1''').fetchone()
        if row:
            raise ValueError('Append and publish newly completed paper records before preparing a replacement')


def switch(conn, *, root, directories, recovery_dir, operation_id, expected_head,
           version_id, render, backtest_bundle=None, restoring=False, fault=None):
    """render(book, [stage_output, stage_frontend]) is a pure explicit book renderer."""
    from prediction_market_soccer.util import frozen_strategy_store as books
    from prediction_market_soccer.config import CONFIG
    root, directories, recovery_dir = _isolated(conn, root, directories, recovery_dir)
    gate_root = _path(CONFIG.paths.data, root, exists=True)
    if head(conn, directories) != expected_head:
        raise ValueError('Book, appended rows, executions or artifacts changed since preparation')
    with maintenance_gate(operation_id, root=gate_root):
        journal = recovery_dir / 'journal.json'
        if journal.exists():
            raise ValueError('Existing recovery journal requires resume/recover, not another switch')
        try:
            if head(conn, directories) != expected_head:
                raise ValueError('State changed before the exclusive gate; take a fresh final recovery snapshot')
            books._require_no_open_positions(conn)
            _pending_completion(conn)
            target = books.read_book(conn, version_id=version_id)
            if not restoring:
                bundle = validate_backtest_bundle(backtest_bundle, root=root)
                scope = set(bundle['input_manifest.json']['fixture_ids'])
                if not set(expected_head['fixture_ids']) <= scope:
                    raise ValueError('Candidate omits existing or newly appended financial fixtures')
                if target['ledger']['records'] != bundle['candidate_report.json']['bet_log']:
                    raise ValueError('Registered candidate differs from validated backtest')
            recovery_dir.mkdir(parents=True, exist_ok=False)
            for i, directory in enumerate(directories):
                for name in NAMES:
                    path = directory / name
                    if path.exists():
                        atomic_bytes(recovery_dir / 'previous' / str(i) / name, path.read_bytes())
            # A consistent backup is supplementary recovery evidence. Recovery
            # preserves append-only journals; it never copies this over the live DB.
            backup = sqlite3.connect(recovery_dir / 'database-before.db')
            try:
                conn.backup(backup)
            finally:
                backup.close()
            from prediction_market_soccer.util.forward_methods import runtime_snapshot
            runtime = runtime_snapshot()
            module_root = Path(__file__).resolve().parents[1]
            for name in runtime['code']:
                atomic_bytes(recovery_dir / 'code' / name, (module_root / name).read_bytes())
            state = {'operation_id': operation_id, 'phase': 'prepared', 'before': expected_head,
                     'target_version': version_id, 'runtime': runtime, 'directories': [str(p) for p in directories],
                     'database_backup_sha256': _sha(recovery_dir / 'database-before.db')}
            _require_runtime(recovery_dir, state)
            atomic_json(journal, state)
        except BaseException:
            if not journal.exists():
                finish_maintenance(operation_id, root=gate_root)
            raise
        def checkpoint(phase):
            state['phase'] = phase
            atomic_json(journal, state)
            if fault:
                fault(phase)
        staged = [recovery_dir / 'stage' / str(i) for i in range(2)]
        for path in staged:
            path.mkdir(parents=True)
        render(target, staged)
        state['target_file_hashes'] = _validate_files(staged, target['ledger'])
        checkpoint('staged_verified')
        _require_runtime(recovery_dir, state)
        if restoring:
            books.restore_version(conn, version_id, expected_active_version=expected_head['version_id'], confirmed_restore=True)
        else:
            books.activate_backtest_version(conn, version_id, expected_active_version=expected_head['version_id'], confirmed_replacement=True)
        conn.commit()
        checkpoint('database_activated')
        for i, directory in enumerate(directories):
            for name in NAMES:
                atomic_bytes(directory / name, (staged[i] / name).read_bytes())
                checkpoint('promoted_' + str(i) + '_' + name)
        _validate_files(directories, books.read_book(conn)['ledger'])
        _require_runtime(recovery_dir, state)
        checkpoint('verified')
        finish_maintenance(operation_id, root=gate_root)
        return head(conn, directories)


def recover(conn, *, root, recovery_dir, operation_id):
    """Restore the saved book/files only if no new paper or execution fact appeared."""
    from prediction_market_soccer.util import frozen_strategy_store as books
    from prediction_market_soccer.config import CONFIG
    recovery_dir = _path(Path(recovery_dir), root, exists=True)
    journal = recovery_dir / 'journal.json'
    state = json.loads(journal.read_text())
    if state['operation_id'] != operation_id:
        raise ValueError('Recovery operation identity mismatch')
    root, directories, _ = _isolated(conn, root, state['directories'], recovery_dir)
    gate_root = _path(CONFIG.paths.data, root, exists=True)
    with maintenance_gate(operation_id, root=gate_root):
        _require_runtime(recovery_dir, state)
        books._require_no_open_positions(conn)
        if _execution_hash(conn) != state['before']['execution_hash']:
            raise ValueError('New decision/execution facts appeared; automatic restore is unsafe')
        current = books.read_book(conn)
        if current['version']['version_id'] not in (state['before']['version_id'], state['target_version']):
            raise ValueError('Unexpected active version; explicit reconciliation required')
        for i, directory in enumerate(directories):
            for name in NAMES:
                saved = recovery_dir / 'previous' / str(i) / name
                if _sha(saved) != state['before']['files'][str(i) + '/' + name]:
                    raise ValueError('Recovery artifact checksum mismatch')
        if current['version']['version_id'] != state['before']['version_id']:
            books.restore_version(conn, state['before']['version_id'],
                expected_active_version=current['version']['version_id'], confirmed_restore=True)
            conn.commit()
        for i, directory in enumerate(directories):
            for name in NAMES:
                saved = recovery_dir / 'previous' / str(i) / name
                if saved.exists():
                    atomic_bytes(directory / name, saved.read_bytes())
                else:
                    (directory / name).unlink(missing_ok=True)
        if head(conn, directories) != state['before']:
            raise ValueError('Restored DB/files do not match the saved head')
        state['phase'] = 'restored_verified'
        atomic_json(journal, state)
        finish_maintenance(operation_id, root=gate_root)
        return state['before']


def activate_forward_method(conn, *, root, directories, recovery_dir, operation_id,
                            expected_head, manifest, fault=None):
    """Initial or changed method activation; no history replacement or forced close.

    Retrying the same journal verifies actual state and can finish an interrupted
    activation. It cannot undo outside trades or infer an unknown intent failed.
    """
    from prediction_market_soccer.config import CONFIG
    from prediction_market_soccer.util import forward_methods as methods
    from prediction_market_soccer.util import frozen_strategy_store as books
    root, directories, recovery_dir = _isolated(conn, root, directories, recovery_dir)
    gate_root = _path(CONFIG.paths.data, root, exists=True)
    methods.validate_manifest(manifest, check_runtime=True)
    journal = recovery_dir / 'method-journal.json'
    epoch_id = methods.digest({'manifest_id': manifest['manifest_id']})
    with maintenance_gate(operation_id, root=gate_root):
        if journal.exists():
            state = json.loads(journal.read_text())
            if state['operation_id'] != operation_id or state['manifest'] != manifest or state['before'] != expected_head:
                raise ValueError('Interrupted method activation identity differs; reconcile its existing journal')
            current = head(conn, directories)
            if ({k:v for k,v in current.items() if k != 'execution_hash'} !=
                    {k:v for k,v in expected_head.items() if k != 'execution_hash'}
                    or _execution_hash(conn, include_methods=False) != state['financial_hash']):
                raise ValueError('Financial state changed during interrupted method activation')
        else:
            try:
                if head(conn, directories) != expected_head:
                    raise ValueError('Method activation requires a fresh book/artifact recovery snapshot')
                books._require_no_open_positions(conn)
                _pending_completion(conn)
                current = methods.active_epoch(conn)
                recovery_dir.mkdir(parents=True, exist_ok=False)
                backup = sqlite3.connect(recovery_dir / 'database-before.db')
                try:
                    conn.backup(backup)
                finally:
                    backup.close()
                state = {'operation_id':operation_id, 'manifest':manifest, 'before':expected_head,
                         'financial_hash':_execution_hash(conn, include_methods=False),
                         'previous_epoch_id':(current or {}).get('epoch_id'), 'epoch_id':epoch_id,
                         'phase':'prepared', 'database_backup_sha256':_sha(recovery_dir/'database-before.db')}
                atomic_json(journal, state)
            except BaseException:
                if not journal.exists():
                    finish_maintenance(operation_id, root=gate_root)
                raise
        books._require_no_open_positions(conn)
        methods.register_epoch(conn, manifest, confirmed=True)
        current = methods.active_epoch(conn)
        if not current or current['epoch_id'] != epoch_id or current['book_version_id'] != expected_head['version_id']:
            methods.activate_epoch(conn, epoch_id, expected_book_version=expected_head['version_id'],
                expected_epoch_id=state['previous_epoch_id'], confirmed=True)
        state['phase'] = 'activated'
        atomic_json(journal, state)
        if fault:
            fault('activated')
        current = methods.active_epoch(conn)
        methods.validate_manifest(current['manifest'], check_runtime=True)
        if current['epoch_id'] != epoch_id or current['book_version_id'] != expected_head['version_id']:
            raise ValueError('Actual method activation differs from intended identity')
        state['phase'] = 'verified'
        atomic_json(journal, state)
        finish_maintenance(operation_id, root=gate_root)
        return current
