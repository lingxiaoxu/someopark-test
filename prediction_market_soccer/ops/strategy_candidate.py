"""Fixed-scope candidate redecision and fixed-leg price sensitivity.

No journal/book writes, no current model construction, no network or default paths.
All financial calculations consume the explicit completed CandidateMarketData run.
The manifest retains unavailable/no-edge opportunities in the declared denominator.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from prediction_market_soccer.util.research_inputs import CandidateMarketData, digest, epoch, _path


PRE_METHOD = {
    'name': 'paper_pre_hybrid', 'selection': 'decide.side_or_model_argmax',
    'stake': 'decide.stake_usd_or_declared_base_stake_usd',
    'quotes': 'available_selected_side_ask_no_devig', 'gate_open': True,
    'argmax_edge': 'model_minus_explicit_book_reference',
    'book_reference_fallback': 'explicit_model_fallback_no_observed_book',
    'fee_argument': 0.0, 'sigma_argument': None, 'alt_argument': None,
    'required_features': ['model', 'calibration', 'calib_confidence', 'form',
                          'gate_open', 'conviction_side', 'book_reference',
                          'book_reference_source'],
}


def _sized(stake, entry_c, per_contract_c):
    if not 0 < entry_c < 100 or stake <= 0:
        raise ValueError('invalid candidate position')
    from prediction_market_soccer.util.pricing import sized_pnl_cents
    return sized_pnl_cents(entry_c,per_contract_c,stake)


def _pre(candidate, fid, at, cutoff):
    from prediction_market_soccer.strategy.decision_model import SideQuote, decide
    from prediction_market_soccer.strategy.smart_exit import smart_exit_cashout
    sides = ('home', 'draw', 'away')
    features = candidate.features_at(fid, at)
    if features['status'] != 'ok':
        return {'status': features['status'], 'reason': features['reason']}
    f = features['data']
    model = f.get('model')
    if not isinstance(model, dict) or set(model) != set(sides) or any(not isinstance(v,(float,int)) or not 0 <= v <= 1 for v in model.values()) or abs(sum(model.values())-1)>1e-6:
        return {'status':'invalid','reason':'missing_selected_model_probabilities'}
    # Every confidence/fee assumption is explicit input, never silently today's
    # form/calibration table. Gross paper fee is separately labelled in output.
    for key in PRE_METHOD['required_features']:
        if key not in f:
            return {'status':'unavailable','reason':'missing_decision_input:'+key}
    reference = f['book_reference']
    if not isinstance(reference, dict) or set(reference) != set(sides) or any(not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in reference.values()) or abs(sum(reference.values()) - 1) > 1e-6:
        return {'status':'invalid','reason':'invalid_book_reference'}
    source = f['book_reference_source']
    if source not in ('observed_book', 'model_fallback_no_observed_book') or (source == 'model_fallback_no_observed_book' and reference != model):
        return {'status':'invalid','reason':'invalid_book_reference_source'}
    if f['gate_open'] is not True or f['conviction_side'] not in (*sides, None):
        return {'status':'invalid','reason':'pre_hybrid_method_contract_mismatch'}
    if f['calibration'] is not None and not isinstance(f['calibration'], dict):
        return {'status':'invalid','reason':'invalid_calibration_input'}
    if not isinstance(f['calib_confidence'], (int, float)) or not -1 <= f['calib_confidence'] <= 1 or (f['form'] is not None and not isinstance(f['form'], dict)):
        return {'status':'invalid','reason':'invalid_confidence_or_form'}
    # The live hybrid evaluates whichever sides have qualified quotes. It does
    # not require a complete three-way book or invent a price for a missing leg.
    quotes, availability, qs = {}, {}, {}
    for side in sides:
        found = candidate.quotes_at(fid, at, required_sides=[side], action='buy')
        availability[side] = {'status': found['status'], 'reason': found['reason']}
        if found['status'] == 'ok':
            quote = found['data'][side]
            price = quote.get('ask') if candidate.manifest.get('require_observed_quotes') else quote.get('price')
            if price is not None and 0 < price < 1:
                quotes[side] = quote
                qs[side] = SideQuote(ask=price, venue=quote.get('venue'))
                continue
            availability[side] = {'status':'invalid','reason':'invalid_selected_ask_or_reference'}
        qs[side] = SideQuote()
    cfg,risk=candidate.parameters()
    decision = decide(model,qs,cfg=cfg,risk=risk,calib_confidence=f['calib_confidence'],
                      form=f['form'],gate_open=True,conviction_side=f['conviction_side'])
    side = decision.side or max(sides, key=model.get)
    if side not in quotes:
        return {'status':availability[side]['status'], 'reason':availability[side]['reason'],
                'selected_side':side, 'quote_availability':availability}
    quote = quotes[side]
    entry_cents = round(qs[side].ask * 100, 1)
    stake = decision.stake_usd if decision.side else cfg.base_stake_usd
    kind = 'value' if decision.side else 'argmax'
    edge = decision.net_edge if decision.side else model[side] - reference[side]
    if decision.side and (quote.get('venue') != decision.venue or entry_cents != decision.price_cents):
        raise ValueError('PRE decision and selected candidate quote disagree')
    result = candidate.result_at(fid,cutoff)
    if result['status']!='ok' or result['data'].get('result') not in sides:
        return {'status':'unavailable','reason':result['reason'] or 'regulation_result_unknown'}
    won = side==result['data']['result']
    hold = 100-entry_cents if won else -entry_cents
    exit_result = smart_exit_cashout(None,None,fid,side,entry_cents,None,None,None,won,
                                    candidate=candidate,entry_at=at,until=cutoff)
    if exit_result['status'] not in ('exited','held_no_trigger'):
        return {'status':exit_result['status'],'reason':exit_result['reason'],'entry_observed':True}
    return {'status':'entered','side':side,'entry_at':at,'entry_cents':entry_cents,
            'stake_usd':round(float(stake),2),'bet_kind':kind,'net_edge':round(float(edge),4),
            'confidence_k':decision.confidence_k,'model_pick':max(sides,key=model.get),
            'decision_reason':decision.reason,'quote_availability':availability,
            'won':won,'result':result['data']['result'],
            'exit':exit_result if exit_result['status']=='exited' else None,'exit_status':exit_result['status'],
            'hold_pnl_cents':hold,'realized_pnl_cents':exit_result['pnl_c'] if exit_result['status']=='exited' else hold,
            'selected_quote':quote, 'features':f, 'fee_status':'unknown', 'net_pnl_cents':None}


def build_candidate(candidate, *, decision_scope, model_version, method_version, output_dir,
                    analysis_purpose='full_redecision'):
    """Build all declared fixture×track outcomes and a separate gross paper ledger.

    decision_scope is ordered [{fixture_id,pre_at,inplay_times,cutoff,metadata}].
    Model/state adapters must materialize inputs in the candidate before sealing;
    this function accepts no adapter that could read a current/default source.
    """
    if not isinstance(candidate,CandidateMarketData):
        raise TypeError('a validated completed CandidateMarketData is required')
    if analysis_purpose!='full_redecision' or not model_version or not method_version:
        raise ValueError('explicit candidate method and purpose required')
    scope=list(decision_scope)
    ids=[r['fixture_id'] for r in scope]
    if ids != candidate.manifest['fixture_ids']:
        raise ValueError('decision scope must exactly preserve the ordered fixed fixture scope')
    tracks=candidate.manifest.get('tracks',['pre','inplay'])
    if any(t not in ('pre','inplay') for t in tracks):
        raise ValueError('unsupported redecision track')
    for field,value in [('model_version',model_version),('method_version',method_version)]:
        if field in candidate.manifest and candidate.manifest[field]!=value:
            raise ValueError('candidate '+field+' mismatch')
    from prediction_market_soccer.ops.settle_bets import _inplay_entry
    from prediction_market_soccer.util.strategy_ledger import build_strategy_ledger
    candidate.assert_unchanged()
    candidate.verify_method_code()
    candidate.parameters()
    input_manifest={'schema_version':1,'analysis_purpose':analysis_purpose,
                    'candidate_run_id':candidate.run_id,'scope_id':candidate.scope_id,
                    'candidate_input_hash':candidate.completion['input_manifest_hash'],
                    'candidate_items_hash':candidate.completion['items_hash'],
                    'candidate_input':{'path':str(candidate.path),'root':str(candidate.root),'run_id':candidate.run_id,
                                       'scope_id':candidate.scope_id,'sha256':candidate.snapshot_sha256,
                                       **({'source_path':str(candidate.source.path),'source_root':str(candidate.root),
                                           'source_sha256':candidate.source.manifest['sha256']} if candidate.source is not None else {})},
                    'fixture_ids':ids,'tracks':tracks,'decision_scope':scope,
                    'model_version':model_version,'method_version':method_version,
                    'pre_method':PRE_METHOD,
                    'decision_parameters':candidate.manifest['decision_parameters'],'risk_parameters':candidate.manifest['risk_parameters'],
                    'code_hashes':candidate.manifest['code_hashes']}
    input_hash=digest(input_manifest)
    records, eligibility=[],[]
    pre_cum=ip_cum=0.0
    for item in scope:
        fid,cutoff=item['fixture_id'],item['cutoff']
        if any(epoch(at)>epoch(cutoff) for at in [item.get('pre_at'),*item.get('inplay_times',[])] if at is not None):
            raise ValueError('decision target beyond declared cutoff')
        outcomes={}
        for track in tracks:
            if track=='pre':
                out=_pre(candidate,fid,item['pre_at'],cutoff) if item.get('pre_at') is not None else {'status':'unavailable','reason':'missing_pre_target'}
            else:
                out=_inplay_entry(None,{'api_id':fid,'round':item.get('metadata',{}).get('round')},None,None,
                                  candidate=candidate,decision_times=item.get('inplay_times',[]),cutoff=cutoff)
            outcomes[track]=out
            eligibility.append({'fixture_id':fid,'track':track,'status':out['status'],'reason':out.get('reason'),
                                'entry_observed_but_unscored':out.get('entry_observed',False)})
        pre,ip=outcomes.get('pre',{}),outcomes.get('inplay',{})
        entered_pre,entered_ip=pre.get('status')=='entered',ip.get('status')=='entered'
        if not entered_pre and not entered_ip:
            continue
        metadata=item.get('metadata',{})
        row={k:metadata.get(k) for k in ('league','date','home','away','home_id','away_id','home_zh','away_zh','score')}
        row.update({'fixture_id':fid,'bet':entered_pre,'pick':pre.get('side') if entered_pre else None,
                    'stake_usd':pre.get('stake_usd',0) if entered_pre else 0,
                    'entry_cents':pre.get('entry_cents') if entered_pre else None,'won':pre.get('won') if entered_pre else None,
                    'result':pre.get('result',ip.get('result')),'smart_exit':pre.get('exit') if entered_pre else None,
                    'inplay_side':ip.get('side') if entered_ip else None,'inplay_stake_usd':ip.get('stake_usd',0) if entered_ip else 0,
                    'inplay_entry_cents':ip.get('entry_cents') if entered_ip else None,'inplay_won':ip.get('won') if entered_ip else None,
                    'inplay_milestone':ip.get('milestone') if entered_ip else None,'inplay_exit':ip.get('exit') if entered_ip else None,
                    'input_manifest_hash':input_hash,'model_version':model_version,'method_version':method_version,
                    'evidence_level':'candidate_research','strict_oos':False,'fee_status':'unknown','net_pnl_cents':None})
        pc=_sized(pre['stake_usd'],pre['entry_cents'],pre['realized_pnl_cents']) if entered_pre else None
        ic=_sized(ip['stake_usd'],ip['entry_cents'],ip['realized_pnl_cents']) if entered_ip else None
        pre_cum+=pc or 0;ip_cum+=ic or 0
        row.update(realized_pnl_cents=pc,inplay_pnl_cents=ic,pre_cum_pnl_cents=pre_cum,inplay_cum_pnl_cents=ip_cum,
                   realized_cum_pnl_cents=pre_cum,combined_pnl_cents=(pc or 0)+(ic or 0),combined_cum_pnl_cents=pre_cum+ip_cum)
        row['candidate_decisions']=outcomes
        records.append(row)
    as_of=datetime.now(timezone.utc).isoformat()
    report={'as_of':as_of,'bet_log':records,'pnl_basis':'candidate_paper_gross_before_unknown_fees',
            'input_manifest_hash':input_hash,'eligibility':eligibility,'strict_oos':False}
    ledger=build_strategy_ledger(report)
    report['strategy_ledger']=ledger
    allowed={'entered','no_edge','unavailable','invalid'}
    if any(e['status'] not in allowed for e in eligibility):
        raise ValueError('unknown candidate eligibility status')
    counts={status:sum(e['status']==status for e in eligibility) for status in ('entered','no_edge','unavailable','invalid')}
    manifest={'run_id':candidate.run_id,'status':'completed','completed_at':as_of,
              'model_version':model_version,'method_version':method_version,'ledger_id':ledger['ledger_id'],
              'ledger_hash':digest(ledger),'input_manifest_hash':input_hash,
              'scope_hash':digest(ids),'scope_total':len(ids)*len(tracks),'counts':counts,
              'eligibility':eligibility,'activation_eligible':False,
              'activation_blockers':['strict_pit_not_certified'] + (['no_usable_records'] if not records else []),
              'strict_pit_certified':False,'fee_status':'unknown','source_candidate_hash':candidate.completion['items_hash']}
    candidate.assert_unchanged()
    directory=_path(output_dir,candidate.root)
    directory.mkdir(parents=True,exist_ok=True)
    # Three independent files, single-direction hashes; no circular content SHA.
    for name,doc in [('input_manifest.json',input_manifest),('candidate_report.json',report),('completed_manifest.json',manifest)]:
        target=directory/name
        if target.exists():
            raise ValueError('candidate output already exists; choose a new run output directory')
    for name,doc in [('input_manifest.json',input_manifest),('candidate_report.json',report),('completed_manifest.json',manifest)]:
        with (directory/name).open('x',encoding='utf-8') as stream:
            stream.write(json.dumps(doc,ensure_ascii=False,indent=2,allow_nan=False))
    return {'report':report,'input_manifest':input_manifest,'completed_manifest':manifest}


def fixed_leg_sensitivity(candidate, original_legs, *, cutoff):
    """Preserve original side/stake/entry/exit timestamps; only reprice those inputs.

    Required leg fields: fixture_id,track,side,stake_usd,entry_at,entry_cents,
    exit_at (null means original hold), exit_cents, won. No inferred new timing.
    """
    if not isinstance(candidate, CandidateMarketData):
        raise TypeError('a validated completed CandidateMarketData is required')
    candidate.assert_unchanged()
    epoch(cutoff)
    legs=list(original_legs)
    keys=[(r['fixture_id'],r['track']) for r in legs]
    if len(set(keys))!=len(keys) or any(fid not in candidate.manifest['fixture_ids'] for fid,_ in keys):
        raise ValueError('fixed legs must be unique and inside candidate scope')
    manifest={'schema_version':1,'analysis_purpose':'fixed_leg_price_sensitivity',
              'candidate_input':{'path':str(candidate.path),'root':str(candidate.root),'run_id':candidate.run_id,
                                 'scope_id':candidate.scope_id,'sha256':candidate.snapshot_sha256},
              'candidate_input_hash':candidate.completion['input_manifest_hash'],
              'candidate_items_hash':candidate.completion['items_hash'],
              'cutoff':cutoff,'original_legs_hash':digest(legs),
              'scope':[{'fixture_id':r['fixture_id'],'track':r['track']} for r in legs],
              'scope_hash':digest(keys),'scope_total':len(legs),
              'required_inputs':'Original side/stake/entry time and price; original exit time and price, or observed regulation result for a hold',
              'pricing_code_hash':__import__('hashlib').sha256((Path(__file__).resolve().parents[1]/'util/pricing.py').read_bytes()).hexdigest(),
              'fee_status':'unknown','strict_pit_certified':False}
    rows=[]
    for old in legs:
        fid,side=old['fixture_id'],old['side']
        entry=candidate.quotes_at(fid,old['entry_at'],required_sides=[side],action='buy')
        row={'original':old,'status':'unavailable','reason':None,'new_gross_pnl_cents':None,'gross_delta_cents':None,'net_pnl_cents':None,'fee_status':'unknown'}
        if entry['status']!='ok':
            row['reason']=entry['reason'];rows.append(row);continue
        new_entry=100*entry['data'][side]['ask' if candidate.manifest.get('require_observed_quotes') else 'price']
        if not 0<new_entry<100:
            row['reason']='invalid_entry';rows.append(row);continue
        if old.get('exit_at') is not None:
            exit_quote=candidate.quotes_at(fid,old['exit_at'],required_sides=[side],action='sell')
            if exit_quote['status']!='ok':
                row['reason']=exit_quote['reason'];rows.append(row);continue
            new_exit=100*exit_quote['data'][side]['bid' if candidate.manifest.get('require_observed_quotes') else 'price']
            old_pc=old['exit_cents']-old['entry_cents'];new_pc=new_exit-new_entry
        else:
            result=candidate.result_at(fid,cutoff)
            if result['status']!='ok':
                row['reason']=result['reason'];rows.append(row);continue
            new_exit=None
            won=side==result['data']['result']
            if won != old['won']:
                row['reason']='result_conflict_not_price_sensitivity';rows.append(row);continue
            old_pc=100-old['entry_cents'] if old['won'] else -old['entry_cents']
            new_pc=100-new_entry if won else -new_entry
        old_pnl=_sized(old['stake_usd'],old['entry_cents'],old_pc)
        new_pnl=_sized(old['stake_usd'],new_entry,new_pc)
        row.update(status='repriced',new_entry_cents=new_entry,new_exit_cents=new_exit,old_gross_pnl_cents=old_pnl,
                   new_gross_pnl_cents=new_pnl,gross_delta_cents=new_pnl-old_pnl)
        rows.append(row)
    candidate.assert_unchanged()
    return {'analysis_purpose':'fixed_leg_price_sensitivity','original_legs_hash':digest(legs),
            'scope_total':len(legs),'repriced':sum(r['status']=='repriced' for r in rows),'rows':rows,
            'strict_pit_certified':False,'fee_status':'unknown','input_manifest':manifest,'input_manifest_hash':digest(manifest)}
