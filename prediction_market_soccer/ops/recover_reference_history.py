"""Recover reference-only candidates from sealed local captures; never fetch or trade.

All paths, source hashes and original task keys are explicit. No collection state,
source snapshot, original candidate or financial table is written. The returned
acknowledgements identify exact new sealed items; an operator may later append
collection attempts after independently validating those references.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from prediction_market_soccer.util.price_history import _hash, normalize_series, series_receipt, sample_price
from prediction_market_soccer.util.research_inputs import (
    CandidateMarketData, CandidateWriter, SourceSnapshot, _path, _ro, digest, epoch,
)
from prediction_market_soccer.util.match_timeline import milestone_target

_TASK_KEY = ('scope_id', 'collector', 'collector_version', 'fixture_id', 'target_id')
_FINISHED = ('FT', 'AET', 'PEN')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_candidates(candidates, root):
    """Verify original seals, inner hashes and captured response before selecting."""
    sources, series = [], defaultdict(list)
    for supplied in candidates:
        path = _path(Path(supplied['path']), root, exists=True)
        sha = _sha(path)
        if sha != supplied['sha256']:
            raise ValueError('original_candidate_hash_mismatch')
        c = _ro(path)
        try:
            runs = [dict(r) for r in c.execute('SELECT run_id,scope_id FROM research_run')]
        finally:
            c.close()
        for run in runs:
            md = CandidateMarketData(path, root=root, run_id=run['run_id'], scope_id=run['scope_id'])
            try:
                for item in md.items:
                    if item['kind'] != 'series' or item['market_kind'] != 'match':
                        continue
                    r = item['data']; raw = r.get('raw'); window = r.get('request_window') or {}
                    if r.get('price_kind') != 'historical_reference' or r.get('executable') is not False:
                        raise ValueError('non_reference_series')
                    if r.get('series_receipt_id') != _hash({k:v for k,v in r.items() if k != 'series_receipt_id'}):
                        raise ValueError('original_series_hash_mismatch')
                    if not isinstance(raw, dict) or not isinstance(raw.get('history'), list) or r.get('raw_hash') != _hash(raw):
                        raise ValueError('missing_or_invalid_raw_series')
                    if r.get('points_hash') != _hash(r.get('points')):
                        raise ValueError('original_points_hash_mismatch')
                    token = (r.get('identity') or {}).get('token_id')
                    request = r.get('provider_request_window') or {}
                    if (r.get('identity') or {}).get('provider') != 'poly_global' or not token or str(request.get('market')) != str(token):
                        raise ValueError('history_token_mismatch')
                    if any(request.get(a) != window.get(b) for a,b in (('startTs','start_ts'),('endTs','end_ts'),('fidelity','fidelity'))):
                        raise ValueError('history_request_window_mismatch')
                    if epoch(r['request_started_at']) > epoch(r['received_at']):
                        raise ValueError('invalid_history_capture_clock')
                    # Invalid captures remain explicit unavailable results. Recovery never
                    # discards malformed out-of-window points to make a series pass.
                    try:
                        raw_points = normalize_series(raw['history'])
                        original_points = list(r['points']) + list((r.get('window_projection') or {}).get('excluded_points', []))
                        if raw_points != normalize_series(original_points):
                            raise ValueError('raw_projection_mismatch')
                    except (TypeError, AttributeError) as exc:
                        raise ValueError('invalid_raw_projection') from exc
                    rec = series_receipt(raw['history'], identity=r['identity'],
                        start_ts=window['start_ts'], end_ts=window['end_ts'], fidelity=window['fidelity'],
                        request_started_at=r['request_started_at'], received_at=r['received_at'],
                        raw=raw, raw_hash=r['raw_hash'], provider_request_window=request)
                    ref = {'path':str(path), 'sha256':sha, 'run_id':run['run_id'],
                           'scope_id':run['scope_id'], 'item_hash':item['payload_hash'],
                           'original_series_receipt_id':r['series_receipt_id']}
                    key = (item['fixture_id'], item['side'], item['market_kind'])
                    series[key].append({'receipt':rec,'reference':ref,'kickoff':item['target_at'],
                                        'manifest':md.manifest})
            finally:
                md.conn.close()
        sources.append({'path':str(path),'sha256':sha})
    return sources, series


def recover(*, root, source_path, source_sha256, candidates, tasks, output_path, run_id):
    """Create a new sealed research candidate and exact per-task recovery receipts."""
    root = Path(root).resolve(strict=True)
    output = _path(Path(output_path), root)
    if output.exists():
        raise ValueError('output_already_exists')
    if not tasks or len({tuple(t[k] for k in _TASK_KEY) for t in tasks}) != len(tasks):
        raise ValueError('explicit_unique_task_scope_required')
    if len({t['collector'] for t in tasks}) != 1:
        raise ValueError('one_collector_per_candidate_required')
    ids = sorted({t['fixture_id'] for t in tasks})
    source = SourceSnapshot(Path(source_path), root=root, fixture_scope=ids)
    writer = None
    try:
        if source.manifest['sha256'] != source_sha256:
            raise ValueError('source_snapshot_hash_mismatch')
        for task in tasks:
            if set(task) != set(_TASK_KEY):
                raise ValueError('only_exact_original_task_keys_allowed')
            row = source.conn.execute('SELECT * FROM collection_task_state_v1 WHERE ' +
                ' AND '.join(k+'=?' for k in _TASK_KEY), tuple(task[k] for k in _TASK_KEY)).fetchone()
            if row is None or row['status'] == 'complete':
                raise ValueError('task_not_in_original_incomplete_scope')
            kind, side, market = task['target_id'].split(':')
            if side not in ('home','draw','away') or market != 'match' or (
                    task['collector'] == 'ticks' and kind != 'tick') or (
                    task['collector'] == 'milestones' and kind not in ('PRE','T15','T30','HT','T60','T75')) or task['collector'] not in ('ticks','milestones'):
                raise ValueError('unsupported_recovery_target')
        sources, series = _load_candidates(candidates, root)
        if output == source.path or any(output == Path(s['path']) for s in sources):
            raise ValueError('output_aliases_input')
        # A token assigned to more than one requested direction is an identity
        # conflict, even if one response is newer or the prices happen to agree.
        tokens_by_fixture = defaultdict(dict)
        chosen = {}
        for task in tasks:
            _, side, market = task['target_id'].split(':'); fid=task['fixture_id']
            values=series.get((fid,side,market), [])
            tokens={v['receipt']['identity']['token_id'] for v in values}
            if len(tokens)>1:
                raise ValueError('conflicting_original_token_assignments')
            if tokens:
                token=next(iter(tokens)); old=tokens_by_fixture[fid].get(token)
                if old is not None and old != side:
                    raise ValueError('duplicate_token_across_sides')
                tokens_by_fixture[fid][token]=side
                chosen[(fid,side,market)]=max(values,key=lambda v:(epoch(v['receipt']['received_at']),v['reference']['sha256']))
        created=datetime.now(timezone.utc).isoformat()
        scope_id='reference-recovery-'+digest(tasks)
        manifest={'scope_id':scope_id,'fixture_ids':ids,'tracks':['quotes'],'market_kinds':['match'],
            'analysis_purpose':'retrospective_reference_recovery_v1','require_observed_quotes':False,
            'pit_certified':False,'strict_oos':False,'created_at':created,'source_snapshot':source.manifest,
            'original_candidates':sources,'original_tasks':tasks,
            'identity_basis':'sealed_candidate_fixture_side_assignment_not_independently_reverified',
            'method_hash':_sha(Path(__file__)),
            'price_history_hash':_sha(Path(__file__).parents[1]/'util/price_history.py')}
        # Reserve a new file exclusively: never attach another run to an existing DB.
        output.parent.mkdir(parents=True, exist_ok=True)
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
        writer=CandidateWriter(output,root=root,run_id=run_id,input_manifest=manifest,source=source)
        outcomes=[]; written_states=set()
        for task in tasks:
            fid=task['fixture_id']; milestone,side,market=task['target_id'].split(':')
            base={**task,'candidate_path':str(output),'candidate_run_id':run_id,'candidate_scope_id':scope_id,
                  'status':'unavailable','reason':'saved_series_unavailable','observation_ids':[]}
            value=chosen.get((fid,side,market))
            if not value:
                outcomes.append(base);continue
            fx=source.conn.execute('SELECT * FROM fixture WHERE api_id=?',(fid,)).fetchone()
            ko=epoch(fx['kickoff_ts']) if fx and fx['kickoff_ts'] else None
            rec=value['receipt']; window=rec['request_window']; original=value['manifest']
            if (not fx or fx['status_short'] not in _FINISHED or ko != value['kickoff'] or
                fid not in original['fixture_ids'] or not epoch(original['window_start']) <= ko <= epoch(original['window_end']) or
                window['start_ts'] != ko-1800 or window['end_ts'] != ko+170*60):
                outcomes.append({**base,'reason':'original_fixture_scope_mismatch'});continue
            series_ref=writer.add('series',fid,ko,rec,side=side,market_kind=market,
                                 status='invalid' if rec['quality']=='invalid' else 'ok')
            refs=[]
            provenance={'source_candidate':value['reference'],'series_item_hash':series_ref,
                        'recovered_at':created,'identity_basis':manifest['identity_basis']}
            if rec['quality'] != 'available':
                outcomes.append({**base,'reason':rec.get('reason') or 'empty_series','series_item_hash':series_ref});continue
            if milestone=='tick':
                points=[(p['ts'],None) for p in rec['points']]
            else:
                clock=milestone_target(ko,milestone);points=[(clock['target_at'],clock)]
            samples=[]
            for target,clock in points:
                sample=sample_price(rec,target);samples.append(sample)
                payload={**sample,'token_id':rec['identity']['token_id'],'venue':'poly_global','side':side,
                         'market_kind':market,'target_id':task['target_id'],'received_at':rec['received_at'],
                         'recovery':provenance}
                if clock:payload['clock']=clock
                else:payload['relative_wall_seconds']=target-ko
                refs.append(writer.add('quote',fid,target,payload,status=sample['status'],side=side,market_kind=market))
                # Price series cannot reconstruct a contemporaneously known score,
                # red card, model input or executable bid/ask observation.
                if clock and (fid,target) not in written_states:
                    writer.add('state',fid,target,{'status':'unavailable','reason':'not_recovered_from_price_series',
                        'target_at':target,'certainty':'unknown'},status='unavailable')
                    written_states.add((fid,target))
            coverage=rec['coverage']
            complete=all(s['status']=='ok' for s in samples)
            reason=None if complete else next(s['reason'] for s in samples if s['status']!='ok')
            if milestone=='tick':
                complete=bool(refs) and coverage['max_gap_s'] is not None and coverage['max_gap_s']<=180 and coverage['start_ts']<=ko-300 and coverage['end_ts']>=ko+5400
                reason=None if complete else 'history_gap_exceeds_tolerance'
            outcomes.append({**base,'status':'complete' if complete else 'partial','reason':reason,
                'observation_ids':refs,'series_item_hash':series_ref,'source_candidate':value['reference'],
                'reference_only':True,'executable':False,'received_at':rec['received_at']})
        conclusions=[{'fixture_id':fid,'track':'quotes',
            'status':'retrospective_reference_only' if all(x['status']=='complete' for x in outcomes if x['fixture_id']==fid) else 'source_unavailable'} for fid in ids]
        completion=writer.finish(conclusions);writer.close();writer=None
        for original in sources:
            if _sha(original['path'])!=original['sha256']:raise ValueError('original_candidate_changed')
        source.assert_unchanged()
        return {'schema_version':1,'analysis_purpose':manifest['analysis_purpose'],'network_requests':0,
                'candidate_path':str(output),'candidate_sha256':_sha(output),'candidate_run_id':run_id,
                'candidate_scope_id':scope_id,'completion_hash':digest(completion),'source_sha256':source_sha256,
                'original_candidates':sources,'tasks':outcomes,'n_requested':len(tasks),
                'n_complete':sum(x['status']=='complete' for x in outcomes),
                'financial_tables_written':False,'collection_state_written':False,
                'historical_pit_certified':False,'executable':False}
    finally:
        if writer is not None:writer.close()
        source.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plan',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    args=p.parse_args();plan=json.loads(args.plan.read_text())
    report=_path(args.report,plan['root'])
    if report.exists():raise ValueError('report_already_exists')
    result=recover(**plan)
    with report.open('x',encoding='utf-8') as f:
        json.dump(result,f,indent=2,ensure_ascii=False,allow_nan=False);f.write('\n')
    print(json.dumps({'n_requested':result['n_requested'],'n_complete':result['n_complete'],
                      'network_requests':0,'report':str(report)}))


if __name__=='__main__':
    main()
