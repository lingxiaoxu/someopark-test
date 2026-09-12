"""One published model-strategy snapshot for the timing table, price track and PDF.

Records and their order are copied exactly from bet_log. This module does not select
bets, price a match, resize a position, reconstruct an exit, or change a frozen row.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from decimal import Decimal, InvalidOperation
from copy import deepcopy


class StrategyLedgerUnavailable(ValueError):
    code = 'strategy_ledger_unavailable'


def _canonical(records):
    return json.dumps(records,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def _number(value):
    if value is None:
        return None
    try:
        n=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError) as exc:
        raise StrategyLedgerUnavailable('Invalid strategy-ledger financial value') from exc
    if not n.is_finite():
        raise StrategyLedgerUnavailable('Non-finite strategy-ledger financial value')
    return n


def _result(value, entered=True):
    if not entered: return 'no_entry'
    number=_number(value)
    if number is None: return 'unknown'
    return 'profit' if number>0 else ('loss' if number<0 else 'flat')


def _leg_summary(values):
    numbers=[_number(v) for v in values]
    known=[v for v in numbers if v is not None]
    total=sum(known,Decimal(0))
    return {'n':len(values),'profit':sum(v>0 for v in known),'loss':sum(v<0 for v in known),
            'flat':sum(v==0 for v in known),'unknown':len(values)-len(known),
            'pnl_cents':float(total) if len(known)==len(values) else None,
            'pnl_usd':float(total/100) if len(known)==len(values) else None,
            'known_pnl_cents':float(total),'known_pnl_usd':float(total/100),
            'profit_rate':float(sum(v>0 for v in known)/len(known)) if known else None}


def build_strategy_ledger(report) -> dict:
    """Wrap an existing report, retaining every original row/value in original order."""
    if is_dataclass(report):
        records=report.bet_log;at=report.as_of;basis=report.pnl_basis
    elif isinstance(report,dict):
        records=report.get('bet_log');at=report.get('as_of');basis=report.get('pnl_basis')
    else:
        raise StrategyLedgerUnavailable('A report snapshot is required')
    if not isinstance(records,list) or not isinstance(at,str) or not at:
        raise StrategyLedgerUnavailable('The report snapshot has no records or as_of')
    try:
        payload=_canonical(records)
        copied=deepcopy(records)
    except (ValueError,TypeError) as exc:
        raise StrategyLedgerUnavailable('The report records are not strict JSON') from exc
    ids=[r.get('fixture_id') for r in copied if isinstance(r,dict)]
    if len(ids)!=len(copied) or any(not isinstance(fid,int) for fid in ids) or len(set(ids))!=len(ids):
        raise StrategyLedgerUnavailable('Strategy records need unique fixture IDs')
    pre=[r.get('realized_pnl_cents') for r in copied if r.get('bet')]
    inplay=[r.get('inplay_pnl_cents') for r in copied if r.get('inplay_side')]
    return {'schema_version':1,'ledger_id':hashlib.sha256(payload).hexdigest(),'as_of':at,
            'pnl_basis':basis or 'paper_replay_gross_position_before_fees','strict_oos':False,
            'result_basis':'realized_position_pnl_sign','records':copied,
            'record_outcomes':[{'fixture_id':r['fixture_id'],
                 'pre':_result(r.get('realized_pnl_cents'),bool(r.get('bet'))),
                 'inplay':_result(r.get('inplay_pnl_cents'),bool(r.get('inplay_side')))} for r in copied],
            'summary':{'n_matches':len(copied),'n_pre':len(pre),'n_inplay':len(inplay),'n_legs':len(pre)+len(inplay),
                       'pre':_leg_summary(pre),'inplay':_leg_summary(inplay),'combined':_leg_summary(pre+inplay)}}


def validate_strategy_ledger(ledger) -> dict:
    """Validate a supplied snapshot without repairing or silently regenerating it."""
    if not isinstance(ledger,dict) or ledger.get('schema_version')!=1:
        raise StrategyLedgerUnavailable('Missing or unsupported strategy ledger')
    rebuilt=build_strategy_ledger({'bet_log':ledger.get('records'),'as_of':ledger.get('as_of'),
                                  'pnl_basis':ledger.get('pnl_basis')})
    if any(ledger.get(k)!=rebuilt[k] for k in ('ledger_id','summary','record_outcomes')):
        raise StrategyLedgerUnavailable('Strategy ledger hash or summary does not match its records')
    return ledger


def read_strategy_ledger(conn=None) -> dict:
    """Read the published snapshot only. A missing file is not permission to replay bets."""
    from prediction_market_soccer.config import CONFIG
    path=CONFIG.paths.output/'performance_report.json'
    try:
        report=json.loads(path.read_text(encoding='utf-8'))
        return validate_strategy_ledger(report.get('strategy_ledger'))
    except (OSError,ValueError,TypeError,AttributeError) as exc:
        if isinstance(exc,StrategyLedgerUnavailable): raise
        raise StrategyLedgerUnavailable('Published strategy ledger is unavailable') from exc
