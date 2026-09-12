"""Pure QuoteReceiptV1 contract shared by soccer collectors and consumers.

Capture IDs exclude transport availability projections. Durable availability must
be resolved from source_history; a payload cannot certify its own persistence.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from prediction_market_soccer.util.market_identity import content_hash, validate_binding

_HASH_EXCLUDED = frozenset(('receipt_id','payload_hash','available_at','raw_ref'))


def _dt(value):
    d = datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if d.tzinfo is None:
        raise ValueError('timezone_required')
    return d.astimezone(timezone.utc)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
        return f if math.isfinite(f) and 0 < f < 1 else None
    except (ValueError, TypeError, OverflowError):
        return None


def receipt_hash(receipt):
    return content_hash({k:v for k,v in receipt.items() if k not in _HASH_EXCLUDED})


def make_receipt(binding, *, ask=None, bid=None, reference_price=None, reference_kind=None,
                 request_started_at, received_at, provider_quote_at=None, raw=None,
                 ask_size=None, bid_size=None, status='active', available_at=None,
                 capture_id=None, derivation=None):
    if not validate_binding(binding):
        raise ValueError('invalid_market_binding')
    if _dt(received_at) < _dt(request_started_at):
        raise ValueError('invalid_capture_clock')
    a,b = _number(ask), _number(bid)
    def state(value, valid):
        if status not in ('active','open'): return 'suspended'
        return 'available' if valid is not None else 'empty' if value is None else 'invalid'
    crossed = a is not None and b is not None and b > a
    raw_hash = content_hash(raw) if raw is not None else None
    body = {'schema_version': 1, 'binding': binding, 'binding_id': binding['binding_id'],
            'provider': binding['provider'], 'environment': binding['environment'],
            'fixture_api_id': binding['fixture_api_id'], 'comp': binding['comp'],
            'side': binding['side'], 'market_kind': binding['market_kind'],
            'settlement_scope': binding['settlement_scope'], 'period': binding['period'],
            'line': binding['line'], 'ask': a, 'bid': b,
            'ask_state': 'invalid' if crossed else state(ask,a), 'bid_state': 'invalid' if crossed else state(bid,b),
            'ask_size': ask_size, 'bid_size': bid_size, 'reference_price': _number(reference_price),
            'reference_kind': reference_kind, 'quote_kind': 'observed_bbo',
            'request_started_at': request_started_at, 'received_at': received_at,
            'provider_quote_at': provider_quote_at, 'quote_time_basis': 'provider' if provider_quote_at else 'local_request',
            'raw': raw, 'raw_hash': raw_hash, 'raw_ref': None,
            'capture_id': capture_id or raw_hash, 'derivation': derivation}
    sha = receipt_hash(body)
    return {**body, 'receipt_id': sha, 'payload_hash': sha, 'available_at': None}


def validate_receipt(receipt):
    if not isinstance(receipt, dict):
        return False
    try:
        sha = receipt_hash(receipt)
        b = receipt['binding']
        return (receipt.get('schema_version') == 1 and receipt.get('receipt_id') == sha
            and receipt.get('payload_hash') == sha and validate_binding(b)
            and receipt.get('binding_id') == b['binding_id']
            and all(receipt.get(k) == b.get(k) for k in
                ('provider','environment','fixture_api_id','comp','side','market_kind','settlement_scope','period','line'))
            and receipt.get('raw_hash') is not None and receipt['raw_hash'] == content_hash(receipt.get('raw')))
    except (TypeError, ValueError, KeyError):
        return False


def quote_from_receipt(receipt):
    if not validate_receipt(receipt):
        raise ValueError('invalid_quote_receipt')
    return {'ask': receipt['ask'], 'bid': receipt['bid'], 'receipt': receipt,
            'reference_price': receipt.get('reference_price')}


def qualify_quote(quote, action, *, fixture_id=None, comp=None, market_kind='match',
                  settlement_scope='regulation', side=None, now=None, max_age_seconds=120,
                  require_available=False, persisted_available_at=None, environment=None, line=None):
    def reject(reason): return {'eligible': False, 'reason': reason, 'selected': None}
    if action not in ('buy','sell'):
        return reject('invalid_action')
    r = quote.get('receipt') if isinstance(quote,dict) else None
    if not validate_receipt(r):
        return reject('missing_or_invalid_receipt')
    if not validate_binding(r['binding'], fixture_id=fixture_id, comp=comp, side=side,
            market_kind=market_kind, settlement_scope=settlement_scope, environment=environment, line=line):
        return reject('contract_mismatch')
    if r.get('ask') is not None and r.get('bid') is not None and r['bid'] > r['ask']:
        return reject('crossed_book')
    field = 'ask' if action == 'buy' else 'bid'
    price = _number(r.get(field))
    if r.get('quote_kind') != 'observed_bbo' or r.get(field + '_state') != 'available' or price is None:
        return reject('no_executable_' + field)
    size = r.get(field + '_size')
    if size is not None:
        try:
            if not math.isfinite(float(size)) or float(size) <= 0:
                return reject('no_executable_depth')
        except (TypeError, ValueError, OverflowError):
            return reject('invalid_quote_depth')
    if quote.get(field) != price:
        return reject('quote_projection_mismatch')
    try:
        at = _dt(now or datetime.now(timezone.utc).isoformat())
        start,received = _dt(r['request_started_at']),_dt(r['received_at'])
        if not start <= received <= at or (at-start).total_seconds() > max_age_seconds:
            return reject('stale_or_future_quote')
        if r.get('provider_quote_at'):
            provider = _dt(r['provider_quote_at'])
            if provider > received or (at-provider).total_seconds() > max_age_seconds:
                return reject('stale_or_future_provider_quote')
        if require_available and persisted_available_at is None:
            return reject('receipt_not_persisted')
        if persisted_available_at is not None and not received <= _dt(persisted_available_at) <= at:
            return reject('receipt_not_available_at_decision')
    except (ValueError, TypeError, KeyError):
        return reject('invalid_quote_clock')
    selected = {'receipt_id': r['receipt_id'], 'binding_id': r['binding_id'], 'side': r['side'],
                'action': action, 'venue': r['provider'], 'price': price, 'receipt': r}
    return {'eligible': True, 'reason': None, 'selected': selected}


def select_quote(quotes_by_venue, side, action, *, venue_order=None, selection='best', **kwargs):
    if selection not in ('best','priority'):
        raise ValueError('invalid_selection_rule')
    choices = []
    for venue in venue_order or quotes_by_venue:
        source = quotes_by_venue.get(venue) or {}
        q = source.get(side)
        result = qualify_quote(q, action, side=side, **kwargs)
        if result['eligible']:
            choices.append(result['selected'])
            if selection == 'priority':
                return choices[0]
    if not choices:
        return None
    return (min if action == 'buy' else max)(choices, key=lambda s:s['price'])


def qualified_quotes(quotes, *, market_kind='match', settlement_scope='regulation', **kwargs):
    """Keep independently eligible sides/directions and all evidence; never invent sides."""
    result = {}
    for side,q in (quotes or {}).items():
        if not isinstance(q,dict) or 'receipt' not in q:
            continue
        checks = {a: qualify_quote(q,a,side=side,market_kind=market_kind,
                    settlement_scope=settlement_scope,**kwargs) for a in ('buy','sell')}
        if any(v['eligible'] for v in checks.values()):
            result[side] = {**q, 'ask': q.get('ask') if checks['buy']['eligible'] else None,
                           'bid': q.get('bid') if checks['sell']['eligible'] else None}
    return result


def executable_price(quote, action):
    """Direction only after the caller has selected its expected contract family."""
    r = quote.get('receipt') if isinstance(quote, dict) else None
    if not isinstance(r, dict):
        return None
    check = qualify_quote(quote, action, market_kind=r.get('market_kind'),
                          settlement_scope=r.get('settlement_scope'), side=r.get('side'),
                          line=r.get('line'))
    return check['selected']['price'] if check['eligible'] else None


def qualify_source_block(quotes, *, market_kind, fixture_id=None, comp=None, **kwargs):
    """Validate normal side blocks and the legacy corner ladder without changing scope."""
    scope = 'advance' if market_kind == 'advance' else 'regulation'
    if market_kind != 'corners':
        return qualified_quotes(quotes, market_kind=market_kind, settlement_scope=scope,
                                fixture_id=fixture_id, comp=comp,
                                **({'line': 2.5} if market_kind == 'totals' else {}), **kwargs)
    out = {}
    for line, q in (quotes or {}).items():
        if not isinstance(q, dict):
            continue
        try:
            value = float(line)
        except (TypeError, ValueError):
            continue
        under = {'ask': q.get('under_ask'), 'bid': q.get('under_bid'), 'receipt': q.get('under_receipt')}
        checked = qualified_quotes({'over': q, 'under': under}, market_kind='corners',
                                   settlement_scope=scope, line=value, fixture_id=fixture_id,
                                   comp=comp, **kwargs)
        if checked:
            over = checked.get('over') or {}
            un = checked.get('under') or {}
            out[line] = {**q, 'ask': over.get('ask'), 'bid': over.get('bid'),
                         'under_ask': un.get('ask'), 'under_bid': un.get('bid')}
    return out


def source_kind(name, source=None, default='match'):
    return getattr(source, 'market_kind', None) or next(
        (kind for kind in ('advance','totals','corners') if name.endswith('_' + kind)), default)


def collect_qualified_sources(sources, fixture_id, *, allowed_kinds=('match', 'totals', 'corners'),
                              default_kind='match', comp=None):
    """Skip unrelated products before GET, isolate failures, and preserve verified sides."""
    result = {}
    for name, source in sources.items():
        kind = source_kind(name, source, default_kind)
        if kind not in allowed_kinds:
            continue
        try:
            quotes = source(fixture_id)
            result[name] = qualify_source_block(quotes, market_kind=kind,
                                                fixture_id=fixture_id, comp=comp)
        except Exception as exc:
            print(f'[quotes] fixture={fixture_id} source={name} unavailable ({type(exc).__name__})')
            result[name] = {}
    return result


def collect_receipts(value):
    """Capture every received book, including empty or invalid BBO, once by stable ID."""
    out = {}
    def visit(x):
        if isinstance(x, dict):
            if validate_receipt(x):
                out[x['receipt_id']] = x
                return
            for key, child in x.items():
                if key not in ('raw', 'binding', 'identity_evidence'):
                    visit(child)
        elif isinstance(x, (list, tuple)):
            for child in x:
                visit(child)
    visit(value)
    return list(out.values())


def diagnose_quotes(value, *, wide_spread=0.20, three_way_sum=1.20):
    """Backend evidence annotations, never a basket-based execution gate.

    Independent YES markets can legitimately have a high ask sum or all quote
    0.5. Keep their observed BBO unchanged. A crossed single book is separately
    invalid under qualify_quote. References are never counted as asks here.
    """
    receipts=collect_receipts(value); issues=[]; evidence=[]; groups={}
    for r in receipts:
        a,b=r.get('ask'),r.get('bid')
        kind='observed_bbo' if a is not None or b is not None else 'reference_only' if r.get('reference_price') is not None else 'empty'
        evidence.append({'receipt_id':r['receipt_id'],'side':r['side'],'price_evidence':kind,
                         'reference_kind':r.get('reference_kind'),'ask_state':r['ask_state'],'bid_state':r['bid_state']})
        base={'receipt_ids':[r['receipt_id']],'side':r['side'],'price_evidence':kind}
        if a is not None and b is not None:
            if b>a:
                issues.append({**base,'code':'crossed_book','level':'invalid','ask':a,'bid':b})
            elif a-b>=wide_spread-1e-12:
                issues.append({**base,'code':'wide_spread','level':'advisory','spread':round(a-b,8)})
        elif kind=='reference_only':
            issues.append({**base,'code':'reference_without_bbo','level':'unavailable',
                           'reference_price':r['reference_price'],'reference_kind':r.get('reference_kind')})
        binding=r['binding']
        key=tuple(binding.get(k) for k in ('provider','environment','fixture_api_id','comp','market_kind','settlement_scope','period','line','event_id'))
        groups.setdefault(key,{}).setdefault(r['side'],[]).append(r)
    for key,sides in groups.items():
        if key[4]!='match' or any(len(sides.get(s,[]))!=1 for s in ('home','draw','away')):
            continue  # Never choose arbitrarily among different captures of a side.
        rows=[sides[s][0] for s in ('home','draw','away')]
        if not all(r.get('ask') is not None and r['ask_state']=='available' for r in rows):
            continue
        asks={r['side']:r['ask'] for r in rows}; total=sum(asks.values())
        times=[_dt(r['request_started_at']) for r in rows]
        base={'receipt_ids':[r['receipt_id'] for r in rows],'price_evidence':'observed_bbo',
              'asks':asks,'ask_sum':round(total,8),'capture_span_seconds':(max(times)-min(times)).total_seconds(),
              'basket_type':'independently_observed_sides','level':'advisory'}
        if total>three_way_sum+1e-12:
            issues.append({**base,'code':'high_three_way_ask_sum'})
        if all(abs(p-0.5)<1e-12 for p in asks.values()):
            issues.append({**base,'code':'uniform_half_asks','interpretation':'review_source_not_proof_of_fabrication'})
    return {'schema_version':1,'diagnostic_only':True,'receipts':evidence,'issues':issues}
