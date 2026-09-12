"""Routine bounded collection. Durable research inputs never overwrite frozen prices."""
from __future__ import annotations

from datetime import datetime, timezone
import uuid


def run(conn, *, collector='milestones', now=None, reader=None, root=None, limit=12):
    from prediction_market_soccer.config import CONFIG
    from prediction_market_soccer.util.collection_state import daily_scope, record_attempt
    from prediction_market_soccer.util.research_inputs import CandidateWriter
    from prediction_market_soccer.ops import backfill_milestones, backfill_price_ticks
    if collector not in ('milestones', 'ticks'):
        raise ValueError('Unknown daily collector')
    if conn.in_transaction:
        raise ValueError('Finish ingest transaction before daily collection')
    at = now or datetime.now(timezone.utc).isoformat()
    scope = daily_scope(conn, now=at, limit=limit, collector=collector)
    if not scope.fixture_ids:
        return {'complete': True, 'status': 'complete', 'fixtures': 0, 'rows': 0, 'items': []}
    root = (root or CONFIG.paths.data).resolve()
    run_id = collector + '-' + uuid.uuid4().hex
    writer = CandidateWriter(root / 'research_collection_v1' / (run_id + '.db'), root=root,
        run_id=run_id, input_manifest={**scope.manifest(), 'fixture_ids': list(scope.fixture_ids),
            'tracks': ['quotes'], 'provenance': 'daily_retrospective_reference',
            'collection_clock': at, 'require_observed_quotes': True})
    try:
        fn = backfill_milestones.backfill if collector == 'milestones' else backfill_price_ticks.backfill
        # Recollect every target of each due fixture into this one pinned run.
        # Reusing a prior task's complete flag would create an incomplete candidate.
        result = fn(conn, scope=scope, writer=writer, reader=reader)
        conclusions = []
        for fid in scope.fixture_ids:
            items = [r for r in result['items'] if r['fixture_id'] == fid]
            complete = ({r['target_id'] for r in items} == set(scope.target_ids)
                        and all(r['status'] == 'complete' for r in items))
            conclusions.append({'fixture_id': fid, 'track': 'quotes',
                'status': 'retrospective_reference_only' if complete else 'source_unavailable',
                'reason': None if complete else 'Some required targets remain retryable; see collection task state'})
        sealed = writer.finish(conclusions)
        # The task completion only becomes visible after its referenced candidate
        # has been sealed. A crash earlier leaves a safely retryable orphan run.
        for item in result['items']:
            record_attempt(conn, scope, collector, item['fixture_id'], item['target_id'],
                item['status'], item.get('reason'), item.get('observation_ids', ()))
        conn.commit()
        return {**result, 'run_id': run_id, 'candidate_path': str(writer.path),
                'input_manifest_hash': sealed['input_manifest_hash']}
    finally:
        writer.close()
