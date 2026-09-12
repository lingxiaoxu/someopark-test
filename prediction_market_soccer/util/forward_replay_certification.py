"""Certify an exact replay of complete observed paper positions, not a new model.

This deliberately narrow interface cannot certify arbitrary research features,
historical reference prices, inferred no-edge windows, or absent paper legs.
Every decision is independently repriced with the recorded observed model and
current byte-identical implementation, then matched to the immutable source.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from prediction_market_soccer.util import forward_methods as fm, paper_store as ps, source_history as sh
from prediction_market_soccer.util.research_inputs import SourceSnapshot, _path, canonical, digest

KIND = 'observed_forward_exact_replay_v1'
NAMES = ('input_manifest.json', 'candidate_report.json', 'completed_manifest.json')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _equal(actual, expected, message):
    if canonical(actual) != canonical(expected):
        raise ValueError(message)


def _source_view(conn, at):
    return sh.project_asof(conn, at, required_tables=sh.MODEL_TABLES)


def _revision(source, rid, cutoff, root, raw_hashes):
    cache=getattr(source,'_cert_revision_cache',{})
    cached=cache.get((rid,cutoff))
    if cached is not None:
        result,raw=cached
        raw_hashes.update(raw)
        return result
    row=source.conn.execute('''SELECT o.*,a.available_at FROM source_observation_v1 o
        JOIN source_availability_v1 a USING(revision_id) WHERE revision_id=?''',(rid,)).fetchone()
    if not row or not row['complete'] or not sh._utc(row['captured_at']) <= sh._utc(row['available_at']) <= sh._utc(cutoff):
        raise ValueError('Source revision was not complete and durably available by cutoff')
    payload=json.loads(row['payload'])
    if digest(payload)!=row['payload_hash'] or digest([row['source'],row['entity_key'],row['captured_at'],row['payload_hash'],row['batch_id']])!=rid:
        raise ValueError('Source revision payload/identity hash mismatch')
    raw={}
    if row['raw_ref'] is not None:
        path=source.resolve_raw_path(row['raw_ref'])
        expected=payload['prior'] if row['source']=='derived:prior' else payload
        if path.read_bytes()!=canonical(expected).encode():
            raise ValueError('Original source raw object differs from its observed revision')
        raw[str(path)]=_sha(path);raw_hashes.update(raw)
    result={'revision_id':rid,'source':row['source'],'available_at':row['available_at'],'complete':bool(row['complete'])}
    cache[(rid,cutoff)]=(result,raw)
    return result


def _manifest_sources(source, manifest, root, raw_hashes):
    for ref in manifest['source_versions']:
        _equal(_revision(source,ref['revision_id'],manifest['cutoff'],root,raw_hashes),ref,'Source manifest reference differs from durable revision')


def _prior_dependencies(source, prior, root, raw_hashes):
    data=prior['payload'];dependencies=data.get('dependencies')
    if not isinstance(dependencies,dict) or not dependencies.get('source_versions'):
        raise ValueError('Prior has no observed builder input dependencies')
    if data.get('builder_sha256') != _sha(Path(__file__).resolve().parents[1]/'ingest/club_prior.py'):
        raise ValueError('Prior builder implementation differs')
    view,manifest=_source_view(source.conn,dependencies['cutoff'])
    view.close();_equal(manifest,dependencies,'Prior input source dependencies changed')
    _manifest_sources(source,manifest,root,raw_hashes)
    _revision(source,prior['revision_id'],prior['available_at'],root,raw_hashes)
    if sh._utc(dependencies['cutoff']) > sh._utc(prior['available_at']):
        raise ValueError('Prior source cutoff follows prior availability')
    refs=[]
    for name,kind in [('clubelo_revision','file:clubelo'),('champion_anchor_revision','derived:champion_anchor')]:
        rid=data.get(name)
        _revision(source,rid,prior['available_at'],root,raw_hashes)
        row=source.conn.execute('''SELECT o.*,a.available_at FROM source_observation_v1 o
            JOIN source_availability_v1 a USING(revision_id) WHERE revision_id=?''',(rid,)).fetchone()
        if not row or row['source']!=kind or not row['complete'] or sh._utc(row['available_at'])>sh._utc(prior['available_at']):
            raise ValueError('Prior anchor is missing or was not observed before prior availability')
        if digest(json.loads(row['payload']))!=row['payload_hash']:
            raise ValueError('Prior anchor content hash mismatch')
        path=source.resolve_raw_path(row['raw_ref'])
        if path.read_bytes()!=canonical(json.loads(row['payload'])).encode():
            raise ValueError('Prior anchor raw source differs')
        raw_hashes[str(path)]=_sha(path)
        refs.append({'revision_id':rid,'available_at':row['available_at'],'payload_hash':row['payload_hash']})
    return {'dependencies':dependencies,'anchors':refs,'builder_sha256':data['builder_sha256']}


def _quote_objects(source, root, observation, at, proofs):
    """Check paths before the resolver may read raw files; pin every decision book."""
    for quotes in observation.get('quotes', {}).values():
        if not isinstance(quotes, dict):
            continue
        for quote in quotes.values():
            if not isinstance(quote, dict) or not isinstance(quote.get('receipt'), dict):
                continue
            rid = quote['receipt'].get('receipt_id')
            row = source.conn.execute('SELECT raw_ref FROM quote_receipt_v1 WHERE receipt_id=?', (rid,)).fetchone()
            if row is None:
                raise ValueError('Decision quote has no immutable source receipt')
            path = source.resolve_raw_path(row[0])
            durable = sh.resolve_receipt(source.conn, rid, at)
            if durable is None:
                raise ValueError('Decision quote was not available at its recorded cutoff')
            captured = {k: v for k, v in durable.items() if k not in ('available_at', 'raw_ref')}
            transport={k:v for k,v in quote['receipt'].items() if k not in ('available_at','raw_ref','persisted_available_at')}
            _equal(captured, transport, 'Observation receipt differs from immutable capture')
            proofs[str(path)] = _sha(path)


def _observation(source, root, payload, fid, *, pre, raw_hashes):
    from prediction_market_soccer.ops.paper_trading import _fresh,_metadata
    observation, at = payload['input_snapshot'], payload['decision_at']
    saved = source.conn.execute('SELECT payload FROM paper_observation WHERE fixture_api_id=? AND observed_at=?',
                                (fid, observation['observed_at'])).fetchone()
    if saved is None:
        raise ValueError('Missing original paper observation')
    _equal(json.loads(saved[0]), observation, 'Paper observation does not match decision')
    for key, value in [('state', observation['state']), ('snapshot_observed_at', observation['observed_at']), ('snapshot_ts', observation['ts'])]:
        _equal(payload[key], value, 'Decision state/capture identity mismatch')
    _quote_objects(source, root, observation, at, raw_hashes)
    view, manifest = _source_view(source.conn, at)
    try:
        _manifest_sources(source,manifest,root,raw_hashes)
        fx = view.execute('SELECT * FROM fixture WHERE api_id=?', (fid,)).fetchone()
        if fx is None or not _fresh(observation, fx, at, pre=pre, conn=view):
            raise ValueError('No contemporaneous source fixture/state for this decision')
        if payload.get('fixture_input') is not None:
            _equal(dict(fx), payload['fixture_input'], 'Decision fixture differs from its observed source revision')
            identities={r['api_id']:r['canonical_team_id'] for r in view.execute(
                'SELECT api_id,canonical_team_id FROM team_meta WHERE api_id IN (?,?)',(fx['home_api_id'],fx['away_api_id']))}
            for key,api in [('home_id',fx['home_api_id']),('away_id',fx['away_api_id'])]:
                if not identities.get(api) or payload.get(key)!=identities[api]:
                    raise ValueError('Canonical decision team differs from its observed API-team mapping')
            generated = _metadata(view,fx,payload['home_id'],payload['away_id'],observation,at)
            for key,value in generated.items():
                _equal(payload.get(key),value,'Recorded strategy metadata differs from actual method/source: '+key)
        selected=payload.get('selected_quote') or {}
        binding=(selected.get('receipt') or {}).get('binding') or {}
        expected_environment={'kalshi':'public','poly_us':'us'}.get(binding.get('provider'))
        if expected_environment is None:
            raise ValueError('v1 requires an identified public live book provider')
        for key,value in [('fixture_api_id',fid),('home_api_id',fx['home_api_id']),('away_api_id',fx['away_api_id']),
                          ('settlement_scope','regulation'),('market_kind','match'),('environment',expected_environment)]:
            if binding.get(key)!=value:
                raise ValueError('Selected contract identity/scope differs from source fixture')
        if sh._utc(binding['kickoff_ts'])!=sh._utc(fx['kickoff_ts']):
            raise ValueError('Selected contract kickoff differs from source fixture')
        if payload.get('fixture_input') is not None and any(binding.get(k)!=payload.get(k) for k in ('home_id','away_id','comp')):
            raise ValueError('Selected contract canonical identity differs from decision')
        if pre:
            if payload['milestone']!='PRE' or not 0 < (sh._utc(fx['kickoff_ts'])-sh._utc(at)).total_seconds() <= 25*60:
                raise ValueError('Recorded PRE decision lies outside its actual window')
        else:
            from prediction_market_soccer.ops.paper_trading import _MILESTONES
            minimum=dict(_MILESTONES).get(payload['milestone']);minute=observation['state']['elapsed']
            if minimum is None or (fx['status_short']!='HT' if payload['milestone']=='HT' else not minimum<=minute<=minimum+8):
                raise ValueError('Recorded live decision lies outside its milestone window')
        if not pre:
            # Require the actual state revision to predate the request, not just
            # the transport document's self-reported source_as_of.
            state_refs = [r for r in manifest['source_versions'] if r['source'] in ('table:fixture', 'snapshot:fixture', 'fixture_event_set', 'snapshot:fixture_event', 'table:fixture_event')]
            # A later unchanged baseline may conservatively reject a replay; it
            # must never manufacture an earlier state availability.
            if not state_refs:
                raise ValueError('State availability has no source revision')
            boundary=max((sh._utc(r['available_at']) for r in state_refs))
            for quotes in observation.get('quotes',{}).values():
                for quote in quotes.values():
                    receipt=quote.get('receipt') or {}
                    if sh._utc(receipt['request_started_at']) < boundary or (receipt.get('provider_quote_at') and sh._utc(receipt['provider_quote_at']) < boundary):
                        raise ValueError('Quote predates the independently observed match state')
        return dict(fx), manifest
    finally:
        view.close()


class _RecordedStrength:
    def __init__(self, source, entry, root, raw_hashes):
        from prediction_market_soccer.model.observed_strength import _restore, _prior, _model_identity
        from prediction_market_soccer.model.squad_strength import build_strength_live
        from prediction_market_soccer.config import CONFIG
        from dataclasses import asdict
        from prediction_market_soccer.ops.paper_trading import _strength_inputs
        self.models = {}
        manifests = []
        at = entry['decision_at']
        for name, saved in entry['model_inputs'].items():
            if not isinstance(saved, dict) or not isinstance(saved.get('input_manifest'), dict):
                continue
            manifest = saved['input_manifest']
            rows = sh.versions_at(source.conn, 'derived:daily_strength', at)
            matching = [r for r in rows if {**r['payload']['input_manifest'], 'model_available_at': r['available_at']} == manifest]
            if len(matching) != 1 or not matching[0]['complete']:
                raise ValueError('Model input manifest has no unique previously available daily model')
            model = matching[0]
            identity=_model_identity()
            expected_key={'day':sh._utc(manifest['cutoff']).date().isoformat(),'comp':entry['comp'],
                'model_identity':identity,'forward_epoch_id':entry['forward_epoch_id']}
            if manifest.get('model_identity')!=identity or model['entity_key']!=expected_key:
                raise ValueError('Recorded daily model key/implementation belongs to another method or cutoff')
            _equal(model['payload']['strength']['cfg'],asdict(CONFIG.model),'Derived model configuration differs from the activated runtime')
            _revision(source,model['revision_id'],at,root,raw_hashes)
            _manifest_sources(source,manifest,root,raw_hashes)
            if manifest['forward_epoch_id'] != entry['forward_epoch_id'] or sh._utc(manifest['cutoff']) > sh._utc(at):
                raise ValueError('Wrong model epoch or future model cutoff')
            prior = [r for r in sh.versions_at(source.conn, 'derived:prior', manifest['cutoff']) if r['revision_id'] == manifest['prior_revision_id']]
            if len(prior) != 1 or not prior[0]['complete'] or prior[0]['available_at'] != manifest['prior_available_at']:
                raise ValueError('Model prior revision was not observed by its input cutoff')
            prior_proof=_prior_dependencies(source,prior[0],root,raw_hashes)
            sm = _restore(source.conn, model['payload'], model['available_at'])
            try:
                key=(model['revision_id'],manifest['cutoff'],manifest['forward_epoch_id'])
                if key not in source._cert_model_proofs:
                    original_prior,_ = _prior(source.conn,sm.comp,manifest['cutoff'])
                    rebuilt=build_strength_live(sm.observed_connection,original_prior,CONFIG.model,as_of=manifest['cutoff'],xg_form=True,league=sm.comp,strict_inputs=True)
                    frozen=asdict(rebuilt);frozen['host_ids']=list(frozen['host_ids'])
                    _equal(frozen,model['payload']['strength'],'Derived daily model cannot be rebuilt from observed source revisions')
                    _equal(rebuilt._altdata_w(),model['payload']['altdata_weights'],'Derived model used different loaded league weights')
                    source._cert_model_proofs.add(key)
                _equal(_strength_inputs(sm, entry['home_id'], entry['away_id']), saved, 'Recorded strength differs from immutable model output')
            except BaseException:
                sm.observed_connection.close()
                raise
            self.models[name] = sm
            manifests.append({'revision_id': model['revision_id'], 'available_at': model['available_at'], 'input_manifest': manifest,'prior_proof':prior_proof})
        if 'strength' not in self.models:
            raise ValueError('No complete recorded strength')
        self.proofs = manifests

    def get(self, at, comp):
        sm = self.models['strength']
        if sm.comp != comp or sh._utc(sm.input_manifest['model_available_at']) > sh._utc(at):
            raise ValueError('Recorded model was not available for this decision')
        return sm

    def close(self):
        for sm in self.models.values():
            sm.observed_connection.close()


def _calibration(source, entry):
    from prediction_market_soccer.ops.paper_trading import _calibration_records
    epoch = fm.get_epoch(source.conn, entry['forward_epoch_id'])
    all_records = _calibration_records(source.conn, epoch)
    eligible = [r for r in all_records if sh._utc(r['result_available_at']) <= sh._utc(entry['decision_at'])]
    recorded = entry['model_inputs'].get('calibration_records', [])
    for record in recorded:
        if record not in eligible or record['fid'] == entry['input_snapshot']['fixture_api_id']:
            raise ValueError('Calibration reference has no earlier observed source prediction/result')
    # Source positions preserve original insertion order, but compare the full
    # fixed pool by identity to avoid accepting a selected profitable subset.
    _equal(sorted(recorded, key=lambda r:r['fid']), sorted(eligible, key=lambda r:r['fid']), 'Calibration pool differs from available source records')
    return recorded


def _add_cumulatives(records):
    records=deepcopy(records)
    pre=ip=Decimal(0)
    for record in records:
        pre += Decimal(str(record['realized_pnl_cents'])); ip += Decimal(str(record['inplay_pnl_cents']))
        record.update(realized_cum_pnl_cents=float(pre),pre_cum_pnl_cents=float(pre),inplay_cum_pnl_cents=float(ip),
            combined_pnl_cents=float(Decimal(str(record['realized_pnl_cents']))+Decimal(str(record['inplay_pnl_cents']))),combined_cum_pnl_cents=float(pre+ip))
    return records


def _audit(source, root, fixture_ids, tracks):
    from prediction_market_soccer.ops import paper_trading as pt
    from prediction_market_soccer.util.pricing import reg_score
    if not isinstance(source, SourceSnapshot):
        raise TypeError('A fixed read-only SourceSnapshot is required')
    source.assert_unchanged()
    # Proof reuse is confined to this scan of this fixed snapshot. No live model
    # connection is cached; every caller owns/closes its own projected view.
    source._cert_revision_cache={}
    source._cert_model_proofs=set()
    if not fixture_ids or len(set(fixture_ids)) != len(fixture_ids) or any(type(i) is not int for i in fixture_ids):
        raise ValueError('Exact nonempty ordered fixture scope required')
    if tuple(tracks) != ('pre', 'inplay'):
        raise ValueError('v1 certifies complete PRE and INPLAY pairs only; missing/no-edge tracks are unsupported')
    records, proofs, raw_hashes, model_versions, method_ids = [], [], {}, set(), set()
    positions = ps.positions(source.conn)
    for fid in fixture_ids:
        rows = [p for p in positions if p['fixture_api_id'] == fid]
        if len(rows) != 2 or {p['track'] for p in rows} != set(tracks) or any(p['status'] != 'settled' for p in rows):
            raise ValueError('Every declared track requires an actual entry and sealed completion; no inferred no-edge')
        completion = rows[0]['settlement']
        if not completion.get('sealed') or completion.get('origin') != 'paper_forward':
            raise ValueError('Not a sealed observed paper completion')
        _equal(rows[1]['settlement'], completion, 'Tracks disagree on final completion')
        legs = {}
        for pos in rows:
            entry, exit_ = pos['entry'], pos['exit']
            if not isinstance(entry.get('fixture_input'),dict):
                raise ValueError('Entry has no original fixture input; metadata cannot be certified')
            track = pos['track']; at = entry['decision_at']
            epoch = fm.get_epoch(source.conn, entry['forward_epoch_id'])
            fm.validate_manifest(epoch['manifest'], check_runtime=True)
            _equal(entry['method_manifest'], epoch['manifest'], 'Entry method differs from durable epoch')
            if any(entry[k] != epoch[k] for k in ('model_version','method_version')):
                raise ValueError('Entry model/method was relabeled')
            activation = source.conn.execute('SELECT * FROM forward_method_activation WHERE activated_at<=? ORDER BY sequence DESC LIMIT 1',(at,)).fetchone()
            if not activation or activation['epoch_id'] != entry['forward_epoch_id'] or activation['book_version_id'] != entry['book_version_id']:
                raise ValueError('Entry did not belong to the activated book/epoch at decision time')
            if entry['decision_id'] != hashlib.sha256(f"{entry['book_version_id']}:{fid}:{track}:entry".encode()).hexdigest():
                raise ValueError('Paper entry identity mismatch')
            if sh._utc(entry['snapshot_observed_at']) < sh._utc(activation['activated_at']):
                raise ValueError('Observation predates activation')
            fx, state_manifest = _observation(source, root, entry, fid, pre=track=='pre', raw_hashes=raw_hashes)
            strength = _RecordedStrength(source, entry, root, raw_hashes)
            try:
                decision = (pt.pre_decision(source.conn, fx, entry['home_id'], entry['away_id'], entry['input_snapshot'],
                    _calibration(source, entry), strength, decision_at=at) if track=='pre' else
                    pt._inplay_decision(source.conn, fx, entry['home_id'], entry['away_id'], entry['input_snapshot'], strength, at))
                if not decision or decision.get('no_entry'):
                    raise ValueError('Recorded entry is not reproducible from its observed inputs')
                for key, value in decision.items():
                    _equal(entry.get(key), value, 'Recorded decision differs from exact replay: '+key)
                model_proofs = deepcopy(strength.proofs)
            finally:
                strength.close()
            if exit_:
                _observation(source, root, exit_, fid, pre=False, raw_hashes=raw_hashes)
                if exit_['entry_id'] != entry['decision_id'] or sh._utc(exit_['decision_at']) < sh._utc(at):
                    raise ValueError('Exit identity/time mismatch')
                if not max(1,entry['entry_min'])<=exit_['state']['elapsed']<=95 or exit_['sold_min']!=exit_['state']['elapsed']:
                    raise ValueError('Exit minute differs from actual execution window')
                for key in ('forward_epoch_id','method_manifest','model_version','method_version','side'):
                    _equal(exit_[key],entry[key],'Exit changed the original leg method or identity')
                choices = ps.available_quotes(source.conn, exit_['input_snapshot'], exit_['decision_at'], action='sell', side=entry['side'], entry=entry)
                quote = ps.choose_quote(choices, entry['side'], action='sell')
                if quote is None:
                    raise ValueError('Exit has no same-contract observed bid')
                _equal(quote, exit_['selected_quote'], 'Exit selected quote mismatch')
                fair = pt._live_model(entry, exit_['state'], for_exit=True, conn=source.conn)[entry['side']]
                trigger = min(entry['exit_rule']['margin'], entry['exit_rule']['headroom_frac']*max(0.,1-fair))
                if quote['price'] < fair+trigger or exit_['sold_c'] != round(quote['price']*100,1) or exit_['fair_c'] != round(fair*100,1) or exit_['trigger_c'] != round(trigger*100,1):
                    raise ValueError('Exit price/trigger not reproducible')
            model_versions.add(entry['model_version']);method_ids.add(entry['method_manifest']['manifest_id'])
            legs[track] = pos
            proofs.append({'fixture_id':fid,'track':track,'entry_id':entry['decision_id'],'entry_hash':digest(entry),
                'exit_id':exit_['decision_id'] if exit_ else None,'exit_hash':digest(exit_) if exit_ else None,
                'epoch_id':entry['forward_epoch_id'],'decision_at':at,'state_manifest':state_manifest,'model_proofs':model_proofs})
        result_input = completion['result_input']; end = completion['settled_at']
        if result_input.get('status_short') not in ('FT','AET','PEN') or result_input.get('home_goals') is None or result_input.get('away_goals') is None:
            raise ValueError('Replay requires an observed terminal fixture, not a claimed sealed flag')
        result_raw=json.loads(result_input['raw_json'] or '{}')
        if result_input['status_short'] in ('AET','PEN'):
            fulltime=(result_raw.get('score') or {}).get('fulltime') or {}
            if fulltime.get('home') is None or fulltime.get('away') is None:
                raise ValueError('Extra-time result lacks observed regulation fulltime goals')
        gh,ga = reg_score(result_raw, result_input['home_goals'],result_input['away_goals'])
        observed = source.conn.execute('SELECT * FROM fixture_result_observation WHERE fixture_api_id=? AND observed_at=?',(fid,end)).fetchone()
        if not observed or tuple(observed[k] for k in ('home_goals','away_goals')) != (gh,ga):
            raise ValueError('Completion lacks the actual observed regulation result')
        if any(sh._utc(end) < sh._utc((p['exit'] or p['entry'])['decision_at']) for p in rows):
            raise ValueError('Result was available before the alleged paper decision')
        result_view, result_manifest = _source_view(source.conn,end)
        try:
            _manifest_sources(source,result_manifest,root,raw_hashes)
            r = result_view.execute('SELECT * FROM fixture WHERE api_id=?',(fid,)).fetchone()
            if not r:
                raise ValueError('Missing observed final fixture source')
            _equal(dict(r), result_input, 'Completion result differs from final source revision')
        finally:
            result_view.close()
        result = 'home' if gh>ga else 'draw' if gh==ga else 'away'
        _equal(ps._render(result_input,legs,result,f'{gh}-{ga}',end),completion['record'],'Completion financial projection differs from its original decisions')
        records.append(deepcopy(completion['record']))
        proofs.append({'fixture_id':fid,'completion_id':completion['source_id'],'completion_hash':digest(completion),'result_manifest':result_manifest})
    if len(model_versions) != 1:
        raise ValueError('v1 refuses a replay scope with multiple source model versions')
    records=_add_cumulatives(records)
    for path, sha in raw_hashes.items():
        if _sha(path) != sha:
            raise ValueError('Raw source changed during certification')
    source.assert_unchanged()
    return {'records':records,'proofs':proofs,'raw_hashes':raw_hashes,'model_version':next(iter(model_versions)),
        'method_version':KIND+':'+digest(sorted(method_ids))[:16],
        'as_of':max((r['settlement_observed_at'] for r in records),key=sh._utc)}


def _documents(audit, reference, fixture_ids, tracks, completed_at):
    from prediction_market_soccer.util.strategy_ledger import build_strategy_ledger
    inputs={'schema_version':1,'analysis_purpose':KIND,'candidate_input':reference,'fixture_ids':fixture_ids,'tracks':list(tracks),
        'model_version':audit['model_version'],'method_version':audit['method_version'],
        'scope_id':reference['scope_id'],'proof_hash':digest(audit['proofs'])}
    eligibility=[{'fixture_id':fid,'track':track,'status':'entered','reason':'verified_original_forward_entry'} for fid in fixture_ids for track in tracks]
    report={'as_of':audit['as_of'],'bet_log':audit['records'],'pnl_basis':'observed_paper_gross_exact_replay_before_fees',
        'strict_oos':False,'input_manifest_hash':digest(inputs),'eligibility':eligibility}
    report['strategy_ledger']=build_strategy_ledger(report)
    completed={'status':'completed','run_id':reference['run_id'],'completed_at':completed_at,
        'model_version':audit['model_version'],'method_version':audit['method_version'],'certification_kind':KIND,
        'ledger_id':report['strategy_ledger']['ledger_id'],'ledger_hash':digest(report['strategy_ledger']),
        'input_manifest_hash':digest(inputs),'scope_hash':digest(fixture_ids),'scope_total':len(eligibility),'eligibility':eligibility,
        'activation_eligible':True,'strict_pit_certified':True,'certification_scope':'exact_recorded_forward_inputs_and_decisions_only',
        'new_model_redecision':False,'historical_missing_data_repaired':False,'fee_status':'not_deducted'}
    return dict(zip(NAMES,(inputs,report,completed)))


def build_forward_replay_bundle(source, *, root, fixture_ids, run_id, output_dir, tracks=('pre','inplay'), model_version=None, method_version=None):
    """Create a new four-file bundle; never update source, candidate, or active book."""
    directory=Path(output_dir)
    root=Path(root).resolve(strict=True);directory=_path(directory,root)
    _path(source.path,root,exists=True)
    if not isinstance(run_id,str) or not run_id:
        raise ValueError('Explicit replay run identity required')
    audit=_audit(source,root,list(fixture_ids),tracks)
    if model_version not in (None,audit['model_version']) or method_version not in (None,audit['method_version']):
        raise ValueError('Exact replay cannot relabel the recorded model or invent a method name')
    reference={'kind':KIND,'path':str(source.path),'root':str(root),'sha256':source.manifest['sha256'],
        'run_id':run_id,'fixture_ids':list(fixture_ids),'scope_id':digest({'fixture_ids':list(fixture_ids),'tracks':list(tracks)})}
    if source.raw_archive is not None:
        reference['raw_archive'] = source.raw_archive
    docs=_documents(audit,reference,list(fixture_ids),tracks,datetime.now(timezone.utc).isoformat())
    encoded={name:json.dumps(doc,ensure_ascii=False,indent=2,allow_nan=False).encode() for name,doc in docs.items()}
    certification={'schema_version':1,'kind':KIND,'source':reference,'proofs':audit['proofs'],'raw_hashes':audit['raw_hashes'],
        'documents':{name:hashlib.sha256(body).hexdigest() for name,body in encoded.items()},'runtime_hash':digest(fm.runtime_snapshot())}
    certification['certification_id']=digest(certification)
    source.assert_unchanged()
    directory.mkdir(parents=True,exist_ok=False)
    for name,body in encoded.items():
        with (directory/name).open('xb') as stream:stream.write(body)
    with (directory/'certification.json').open('x',encoding='utf-8') as stream:
        json.dump(certification,stream,ensure_ascii=False,indent=2,allow_nan=False)
    # A changed raw object or input after the first scan leaves a rejected bundle,
    # never an activation. Validators always repeat this scan.
    validate_forward_replay_bundle(directory,root=root)
    return docs


class ForwardReplayPriceData:
    """Read only the pinned original observations after certification/registration.

    Rendering never rebuilds a decision or requires the old runtime to be loaded;
    the source checksum and complete baseline rows remain the registered ones.
    """
    def __init__(self, reference):
        self.reference=deepcopy(reference)
        if reference.get('kind')!=KIND:
            raise ValueError('Explicit forward replay price source required')
        self.source=SourceSnapshot(Path(reference['path']),root=reference['root'],raw_archive=reference.get('raw_archive'))
        self.run_id=reference['run_id']
        try:
            if self.source.manifest['sha256']!=reference['sha256']:
                raise ValueError('Registered replay source checksum changed')
            records=[]
            for fid in reference['fixture_ids']:
                matches=[r for r in ps.completed_records(self.source.conn) if r['fixture_id']==fid]
                if len(matches)!=1:
                    raise ValueError('Registered replay source lacks a unique completion')
                records.append(matches[0]['record'])
            self.records=_add_cumulatives(records)
        except BaseException:
            self.source.close();raise

    def close(self):
        self.source.close()


def validate_forward_replay_bundle(bundle, *, root):
    """Recompute evidence and financial projections; claimed PIT flags confer nothing."""
    directory=_path(Path(bundle),root,exists=True)
    docs={name:json.loads((directory/name).read_text()) for name in NAMES}
    cert=json.loads((directory/'certification.json').read_text())
    body={k:v for k,v in cert.items() if k!='certification_id'}
    if cert.get('kind')!=KIND or cert.get('certification_id')!=digest(body):
        raise ValueError('Certification identity mismatch')
    for name in NAMES:
        if cert.get('documents',{}).get(name)!=_sha(directory/name):
            raise ValueError('Certified bundle document changed')
    ref=docs[NAMES[0]]['candidate_input']
    _equal(ref,cert['source'],'Certification source reference differs')
    if ref.get('kind')!=KIND or ref.get('root')!=str(Path(root).resolve(strict=True)):
        raise ValueError('Wrong replay kind or isolated source root')
    inputs=docs[NAMES[0]]
    if not isinstance(ref.get('run_id'),str) or not ref['run_id'].strip() or ref.get('fixture_ids')!=inputs.get('fixture_ids') or ref.get('scope_id')!=digest({'fixture_ids':inputs['fixture_ids'],'tracks':inputs['tracks']}):
        raise ValueError('Replay run/scope identity is not derived from its actual fixed inputs')
    with SourceSnapshot(Path(ref['path']),root=root,raw_archive=ref.get('raw_archive')) as source:
        if source.manifest['sha256']!=ref['sha256']:
            raise ValueError('Certified source snapshot changed')
        inputs=docs[NAMES[0]]
        audit=_audit(source,root,inputs['fixture_ids'],inputs['tracks'])
        expected=_documents(audit,ref,inputs['fixture_ids'],inputs['tracks'],docs[NAMES[2]]['completed_at'])
        _equal(docs,expected,'Candidate differs from the actual source forward replay')
        _equal(cert['proofs'],audit['proofs'],'Certification proof chain differs')
        _equal(cert['raw_hashes'],audit['raw_hashes'],'Certification raw objects differ')
        if cert.get('runtime_hash')!=digest(fm.runtime_snapshot()):
            raise ValueError('Certification implementation or actual parameters changed')
    return docs
