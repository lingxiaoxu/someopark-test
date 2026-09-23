"""W9 only: consume published forecasts and atomic W7 paper snapshots.

No model training, network client, order routing, parent writes or W8 imports.
The legacy joint W9/W10 observer continues independently. Entry timing is the
parent's declared paper timestamp; actual observation delay is also recorded.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
from pathlib import Path
import os
import time

from ..downside_paper.features import compute_features
from ..downside_paper.sources import make_parent_binding, w7_candidates_from_state, w7_outcomes_from_state
from ..downside_paper.tape import ExistingTape, JsonlTail
from . import policy

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'trading_signals/live_watch/w7_noisefade_state.json'
RUNTIME = ROOT / 'trading_signals/w9_rnn_paper'
PARAMETERS = Path(__file__).with_name('parameters.json')
PARENT_FILES = [ROOT/'crypto_strategies/live_watch'/n for n in ['w7_noisefade.py','common.py','config.yaml']]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',',':'), allow_nan=False).encode()).hexdigest()


def file_hashes(paths):
    return {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def own_files():
    own = list(Path(__file__).parent.glob('*.py')) + [PARAMETERS]
    shared = Path(__file__).parent.parent/'downside_paper'
    return sorted(own+[shared/n for n in ['__init__.py','features.py','sources.py','tape.py']])


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)


def number(value):
    if isinstance(value,bool):
        return None
    try:
        value=float(value)
        return value if math.isfinite(value) else None
    except (TypeError,ValueError):
        return None


def select_prediction(rows, asset, entry_ts):
    """Only forecasts that were already durably published at the entry clock."""
    eligible=[]
    for row in rows:
        if row.get('asset') != asset:
            continue
        p,g,s,a=[number(row.get(k)) for k in ['prediction_ts','generated_at','persisted_at','model_available_ts']]
        if any(v is None for v in [p,g,s,a]):
            continue
        if not (0 <= entry_ts-p <= 60 and a <= g <= s <= entry_ts):
            continue
        if not row.get('strict_recorded_pit_eligible') or not row.get('risk_available'):
            continue
        receipt=number(row.get('feature_last_recorded_receipt_ts'))
        if receipt is None or receipt > p:
            continue
        if row.get('origin') in ['retrospective_bootstrap','historical_replay','training_only']:
            continue
        if number(row.get('pred_abs_return_augmented')) is None or number(row.get('prior_calibration_risk_q90_augmented')) is None:
            continue
        eligible.append(row)
    return max(eligible,key=lambda r:(r['prediction_ts'],r['persisted_at'])) if eligible else None


class ForecastTape:
    def __init__(self,path):
        self.path=Path(path)
        self.tail=JsonlTail(initial_bytes=2_000_000)
        self.rows=[]

    def update(self,now):
        self.rows.extend(self.tail.read(self.path))
        self.rows=[r for r in self.rows if (number(r.get('prediction_ts')) or 0) >= now-300]


def sync_journal(path, records):
    """State is authoritative; repair a torn final line and append missing IDs.

    A crash between atomic state replacement and journaling is recoverable.
    Repeated restarts never duplicate logical event IDs.
    """
    existing=set()
    if path.exists():
        good=0
        with path.open('rb+') as stream:
            while line:=stream.readline():
                if not line.endswith(b'\n'):
                    stream.truncate(good); break
                event=json.loads(line)
                existing.add(event['event_id']); good=stream.tell()
    additions=[r for r in records if r['event_id'] not in existing]
    if additions:
        with path.open('a') as stream:
            for row in additions:
                stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
            stream.flush(); os.fsync(stream.fileno())


class Observer:
    def __init__(self, output=RUNTIME, *, clock=time.time):
        self.clock=clock
        self.output=Path(output)
        if self.output.resolve()==SOURCE.parent.resolve() or SOURCE.parent.resolve() in self.output.resolve().parents:
            raise ValueError('W9 output must be separate from parent files')
        self.output.mkdir(parents=True,exist_ok=True)
        self.lock=(self.output/'observer.lock').open('a')
        fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.parameters=json.loads(PARAMETERS.read_text())
        self.tape=ExistingTape(ROOT/'price_data/hyperliquid')
        self.forecasts=ForecastTape(self.output/'live_predictions.jsonl')
        self.state_path=self.output/'state.json'
        self.parent_signature=None
        self.parent=None
        self.last_hash_check=0
        self.hash_checks={}
        if self.state_path.exists():
            self.state=json.loads(self.state_path.read_text())
            if self.state['parameters_sha256'] != digest(self.parameters):
                raise ValueError('New parameters require a new W9 registration, never overwrite old PNL')
        else:
            parent=self.load_parent()
            binding=make_parent_binding('w7',parent)
            now=self.clock()
            self.state={
                'version':self.parameters['policy'], 'strategy':'w9',
                'mode':self.parameters['mode'], 'registered_at':now,
                'registered_utc':datetime.fromtimestamp(now,timezone.utc).isoformat(),
                'parameters':self.parameters, 'parameters_sha256':digest(self.parameters),
                'parent_binding':binding, 'parent_source_sha256':file_hashes(PARENT_FILES),
                'own_source_sha256':file_hashes(own_files()),
                'book':policy.new_state(now,binding), 'missed_candidates':{},
                'decision_events':[], 'settlement_events':[], 'errors':[],
                'cycles':0, 'last_tick':None,
                'legacy_control_path':str(ROOT/'trading_signals/w9_w10_paper'),
                'historical_research_pnl_included':False,
                'scope':'Prospective inherited W7 MAIN paper execution, independently sized W9; no Demo/Prod orders.'
            }
            atomic_json(self.output/'registration.json',self.state)
            atomic_json(self.state_path,self.state)

    def load_parent(self):
        stat=SOURCE.stat()
        sig=(stat.st_ino,stat.st_mtime_ns,stat.st_size)
        if sig != self.parent_signature:
            # The producer atomically replaces this file only after a full scan.
            self.parent=json.loads(SOURCE.read_text())
            self.parent_signature=sig
        return self.parent

    def check_sources(self,parent,now):
        if now-self.last_hash_check >= 30 or not self.hash_checks:
            self.hash_checks={
                'parent_disk_unchanged':file_hashes(PARENT_FILES)==self.state['parent_source_sha256'],
                'own_and_adapter_disk_unchanged':file_hashes(own_files())==self.state['own_source_sha256']}
            self.last_hash_check=now
        self.state['source_checks']={**self.hash_checks,'parent_binding_matches':make_parent_binding('w7',parent)==self.state['parent_binding']}
        self.state['new_admissions_allowed']=all(self.state['source_checks'].values())

    def cycle(self):
        now=self.clock()
        parent=self.load_parent()
        self.check_sources(parent,now)
        self.tape.update(now)
        self.forecasts.update(now)
        decisions=[]
        if self.state['new_admissions_allowed']:
            candidates=w7_candidates_from_state(parent,self.state['parent_binding'],since_ts=self.state['registered_at'],until_ts=now)
            existing=self.state['book']['episodes']
            # Identity deliberately omits mutable entry fields. Check them
            # before the idempotent-ID filter can hide a source revision.
            for candidate in candidates:
                previous=existing.get(candidate['id'])
                if previous is not None and previous['candidate'] != candidate:
                    self.state['new_admissions_allowed']=False
                    self.state['source_checks']['parent_entries_unchanged']=False
                    self.state.setdefault('source_entry_revisions',{})[candidate['id']]={
                        'observed_at':now, 'original_candidate':previous['candidate'],
                        'observed_candidate':candidate}
                    raise policy.SourceRevisionError('previously recorded W7 entry changed')
            self.state['source_checks']['parent_entries_unchanged']=True
            candidates=[c for c in candidates if c['id'] not in existing and c['id'] not in self.state['missed_candidates']]
            timely=[]
            for candidate in candidates:
                if now-candidate['decision_ts'] > self.parameters['source_discovery_max_delay_seconds'] or now >= candidate['expires_ts']:
                    self.state['missed_candidates'][candidate['id']]={'candidate':candidate,'observed_at':now,'reason':'source_discovered_too_late_no_retroactive_allocation'}
                else:
                    timely.append(candidate)
            if timely:
                # One atomic snapshot contains every same-opened-time member.
                features={c['id']:compute_features(*self.tape.for_asset(c['asset']),c['decision_ts']) for c in timely}
                predictions={c['id']:select_prediction(self.forecasts.rows,c['asset'],c['decision_ts']) for c in timely}
                self.state['book'],decisions=policy.apply_batch(self.state['book'],timely,features,predictions,observed_at=now)
                self.state['decision_events'].extend({'event_id':'decision:'+d['candidate']['id'],**d} for d in decisions)
        settlements=[]
        if self.state['source_checks']['parent_binding_matches']:
            outcomes=w7_outcomes_from_state(parent,self.state['parent_binding'],until_ts=now)
            self.state['book'],settlements=policy.settle(self.state['book'],outcomes)
            self.state['settlement_events'].extend({'event_id':'settlement:'+s['id'],**s} for s in settlements)
            for outcome in outcomes:
                missed=self.state['missed_candidates'].get(outcome['id'])
                if missed:
                    missed['parent_outcome']=outcome
        self.state['summary']=policy.summarize(self.state['book'])
        self.state['summary']['source_missed_count']=len(self.state['missed_candidates'])
        self.state['last_tick']=now
        self.state['cycles']+=1
        self.state['live_prediction_health']={
            'latest_persisted_at':max((number(r.get('persisted_at')) or 0 for r in self.forecasts.rows),default=None),
            'recent_prediction_rows':len(self.forecasts.rows),
            'parse_errors':self.forecasts.tail.errors,
            'flow_parse_errors':self.tape.tail.errors}
        self.state['errors']=self.state['errors'][-100:]
        atomic_json(self.state_path,self.state)
        sync_journal(self.output/'decisions.jsonl',self.state['decision_events'])
        sync_journal(self.output/'settlements.jsonl',self.state['settlement_events'])
        return {'at':now,'new_decisions':len(decisions),'new_settlements':len(settlements),
                'new_admissions_allowed':self.state['new_admissions_allowed'],'summary':self.state['summary']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--loop',type=float,default=0)
    args=parser.parse_args()
    observer=Observer()
    while True:
        start=time.time()
        try:
            result=observer.cycle()
            if result['new_decisions'] or result['new_settlements'] or observer.state['cycles']%30==1:
                print(json.dumps(result,ensure_ascii=False),flush=True)
        except Exception as error:
            message={'at':time.time(),'type':type(error).__name__,'detail':str(error)}
            observer.state['errors'].append(message)
            observer.state['last_error']=message
            atomic_json(observer.state_path,observer.state)
            print(json.dumps({'error':message}),flush=True)
            if not args.loop:
                raise
        if not args.loop:
            return
        time.sleep(max(.1,args.loop-(time.time()-start)))


if __name__=='__main__':
    main()
