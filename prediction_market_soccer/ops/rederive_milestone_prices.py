"""Build an isolated, reviewable price candidate; never UPDATE original rows.

Stored tokens are comparison evidence only. Every new collection revalidates the
fixture/competition/outcome identity. Failed or missing sides stay unavailable in
the candidate and never borrow retained old values. FT is not a quote target.
"""
from __future__ import annotations

import json
from pathlib import Path

from prediction_market_soccer.util.research_inputs import digest, epoch


def _fixture_rows(conn, fixture_ids):
    if not fixture_ids:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM milestone_snapshot WHERE fixture_api_id IN (" +
        ",".join("?" for _ in fixture_ids) + ") ORDER BY fixture_api_id,milestone", tuple(fixture_ids))]


def run(conn=None, *, scope=None, fixture_ids=None, writer=None, reader=None, state_conn=None,
        dry_run=True, limit=None, verbose=True, output_dir=None):
    from prediction_market_soccer.ops.backfill_milestones import collect, _validate_collection
    source, ids = _validate_collection(conn, writer, scope, fixture_ids, limit)
    if writer.source is None or source is not writer.source.conn or source.execute('PRAGMA query_only').fetchone()[0] != 1:
        raise ValueError('rederive requires a bound read-only SourceSnapshot')
    writer.source.assert_unchanged()
    before = _fixture_rows(source, ids)
    before_hash = digest(before)
    collection = collect(source, scope=scope, writer=writer, reader=reader, state_conn=state_conn)
    # A failed side still has an explicit unavailable candidate, never a retained
    # old price masquerading as corrected. Preserve full target diffs.
    from prediction_market_soccer.util.match_timeline import milestone_target
    for item in collection['items']:
        if item['status']=='complete':
            continue
        code,side,kind=item['target_id'].split(':')
        fx=source.execute('SELECT kickoff_ts FROM fixture WHERE api_id=?',(item['fixture_id'],)).fetchone()
        if fx is None or code=='tick':
            continue
        target=milestone_target(fx[0],code)['target_at']
        found=writer.conn.execute("SELECT 1 FROM research_item WHERE run_id=? AND kind='quote' AND fixture_id=? AND target_at=? AND side=? AND market_kind=?",
                                  (writer.run_id,item['fixture_id'],target,side,kind)).fetchone()
        if not found:
            writer.add('quote',item['fixture_id'],target,{'target_id':item['target_id'],'price':None,'sample_ts':None,
                       'reason':item['reason'],'price_kind':'historical_reference','executable':False},
                       status='unavailable',side=side,market_kind=kind)
    # Only this run's new candidate rows are read. No fallback to old price values.
    rows = [dict(r) for r in writer.conn.execute("SELECT * FROM research_item WHERE run_id=? AND kind='quote' ORDER BY fixture_id,target_at,side", (writer.run_id,))]
    old_index = {(r['fixture_api_id'], r['milestone']): r for r in before}
    moves = []
    for row in rows:
        payload = json.loads(row['payload'])
        milestone = payload['target_id'].split(':')[0]
        old = old_index.get((row['fixture_id'], milestone))
        old_values = {'ts': old.get('ts'), 'price': old.get('poly_' + row['side'] + '_ask'),
                      'price_source': old.get('price_source')} if old else None
        new_values = {'target_ts': row['target_at'], 'sample_ts': payload.get('sample_ts'),
                      'price': payload.get('price'), 'clock': payload.get('clock'),
                      'status': row['status'], 'reason': payload.get('reason'),
                      'series_receipt_id': payload.get('series_receipt_id'),
                      'price_source': 'candidate_historical_reference',
                      'price_kind': payload.get('price_kind'), 'price_unit': payload.get('price_unit'),
                      'token_id': payload.get('token_id'), 'venue': payload.get('venue'),
                      'binding': payload.get('binding'), 'received_at': payload.get('received_at'),
                      'age_seconds': payload.get('age_seconds'), 'executable': payload.get('executable'),
                      'devig': payload.get('devig')}
        # Old snapshot target time is not a provider sample time. Missing old
        # evidence stays unknown instead of being inferred from its price label.
        comparable_old = {key: None for key in new_values}
        if old:
            comparable_old.update(target_ts=epoch(old_values['ts']) if old_values['ts'] else None,
                                  price=old_values['price'], price_source=old_values['price_source'])
        changed_fields = {key: {'old': comparable_old[key], 'new': value}
                          for key, value in new_values.items() if comparable_old[key] != value}
        changed = old is None or bool(changed_fields)
        change = {'fixture_id': row['fixture_id'], 'milestone': milestone, 'side': row['side'],
                  'old': old_values, 'new': new_values, 'old_hash': digest(old_values),
                  'new_hash': digest(new_values), 'changed': changed,
                  'changed_fields': changed_fields,
                  'old_snapshot_hash': digest(old), 'new_payload_hash': digest(payload)}
        writer.add('diff', row['fixture_id'], row['target_at'], change, side=row['side'], market_kind=row['market_kind'])
        moves.append(change)
    after_hash = digest(_fixture_rows(source, ids))
    writer.source.assert_unchanged()
    if before_hash != after_hash:
        raise ValueError('source rows changed during candidate computation')
    report = {'run_id': writer.run_id, 'scope_id': scope.scope_id, 'source_rows_before_hash': before_hash,
              'source_rows_after_hash': after_hash, 'source_preserved': True, 'in_place_writes': 0,
              'dry_run': bool(dry_run), 'candidate_only': True, 'input_manifest_hash': digest(writer.manifest),
              'collection': collection, 'changes': moves,
              'rows_changed': sum(m['changed'] and m['new']['status']=='ok' for m in moves),
              'rows_unchanged': sum(not m['changed'] and m['new']['status']=='ok' for m in moves),
              'rows_unavailable': sum(m['new']['status']!='ok' for m in moves)}
    if output_dir is not None:
        from prediction_market_soccer.util.research_inputs import _path
        # Writer's root is explicit and revalidated, never CONFIG.paths.output.
        root = writer.root
        directory = _path(output_dir, root)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'rederive_milestone_prices.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    import argparse
    from prediction_market_soccer.util.collection_state import CollectionScope
    from prediction_market_soccer.util.research_inputs import SourceSnapshot, CandidateWriter, _path
    p = argparse.ArgumentParser(description='Produce an isolated historical price candidate; source is read-only')
    p.add_argument('--source-db', required=True)
    p.add_argument('--candidate-db', required=True)
    p.add_argument('--output-dir', required=True)
    p.add_argument('--fixture-manifest', required=True)
    p.add_argument('--isolated-root', required=True)
    args = p.parse_args()
    manifest_path = _path(args.fixture_manifest, args.isolated_root, exists=True)
    manifest = json.loads(manifest_path.read_text())
    scope = CollectionScope(**manifest['collection_scope'])
    with SourceSnapshot(args.source_db, root=args.isolated_root, fixed_as_of=scope.now, fixture_scope=scope.fixture_ids) as source:
        inputs = {**manifest['input_manifest'], 'source_snapshot': source.manifest,
                  'scope_id': scope.scope_id, 'fixture_ids': list(scope.fixture_ids)}
        writer = CandidateWriter(args.candidate_db, root=args.isolated_root,
                                 run_id=manifest['run_id'], input_manifest=inputs, source=source)
        try:
            result = run(source.conn, scope=scope, writer=writer, output_dir=args.output_dir)
            # This command produces price observations only; it cannot certify
            # event/model/history availability or claim a completed strategy run.
            print(json.dumps({'run_id': writer.run_id, 'status': result['collection']['status'], 'candidate_only': True}))
        finally:
            writer.close()


if __name__ == '__main__':
    main()
