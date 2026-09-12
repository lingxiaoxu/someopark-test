"""Pure soccer contract identities. No discovery, database access or fuzzy matching."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal


class MarketIdentityError(ValueError):
    pass


def canonical_json(value):
    def default(v):
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, datetime):
            return v.isoformat()
        raise TypeError(type(v).__name__)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False, default=default)


def content_hash(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def fixture_identity(fixture, home_id=None, away_id=None, comp=None):
    f = dict(fixture)
    if any(explicit is not None and f.get(key) is not None and explicit != f[key]
           for key,explicit in (('home_id',home_id),('away_id',away_id),('comp',comp))):
        raise MarketIdentityError('conflicting_fixture_identity')
    out = {'fixture_api_id': f.get('fixture_api_id', f.get('api_id', f.get('fixture_id'))),
           'comp': comp or f.get('comp', f.get('league')), 'season': f.get('season'),
           'home_api_id': f.get('home_api_id'), 'away_api_id': f.get('away_api_id'),
           'home_id': home_id or f.get('home_id'), 'away_id': away_id or f.get('away_id'),
           'kickoff_ts': f.get('kickoff_ts', f.get('kickoff')),
           'tie_id': f.get('tie_id'), 'leg': f.get('leg')}
    if (not out['fixture_api_id'] or not out['comp'] or not out['home_id'] or not out['away_id']
            or out['home_id'] == out['away_id'] or not out['home_api_id'] or not out['away_api_id']
            or out['home_api_id'] == out['away_api_id'] or not out['kickoff_ts']):
        raise MarketIdentityError('incomplete_fixture_identity')
    kickoff = datetime.fromisoformat(str(out['kickoff_ts']).replace('Z','+00:00'))
    if kickoff.tzinfo is None:
        raise MarketIdentityError('timezone_required')
    out['fixture_api_id'] = int(out['fixture_api_id'])
    return out


def make_binding(*, fixture, provider, environment, event_id, market_id, side,
                 token_id=None, outcome='yes', market_kind='match', settlement_scope=None,
                 period=None, line=None, identity_version='reviewed-v1',
                 rules_version='soccer-contract-v1', evidence=None):
    f = fixture_identity(fixture)
    scope = settlement_scope or ('advance' if market_kind == 'advance' else 'regulation')
    period = period or ('tie' if market_kind == 'advance' else 'regulation')
    sides = {'match': ('home','draw','away'), 'advance': ('home','away'),
             'totals': ('over','under'), 'corners': ('over','under')}
    if market_kind not in sides or side not in sides[market_kind]:
        raise MarketIdentityError('invalid_contract_side')
    if market_kind == 'advance' and scope != 'advance':
        raise MarketIdentityError('invalid_settlement_scope')
    if market_kind != 'advance' and scope != 'regulation':
        raise MarketIdentityError('invalid_settlement_scope')
    if market_kind in ('totals','corners') and line is None:
        raise MarketIdentityError('missing_contract_line')
    if not all((provider, environment, event_id, market_id, identity_version, rules_version)):
        raise MarketIdentityError('incomplete_market_identity')
    body = {**f, 'schema_version': 1, 'provider': provider, 'environment': environment,
            'event_id': str(event_id), 'market_id': str(market_id),
            'token_id': None if token_id is None else str(token_id), 'outcome': outcome,
            'side': side, 'market_kind': market_kind, 'settlement_scope': scope,
            'period': period, 'line': line, 'identity_version': identity_version,
            'rules_version': rules_version, 'identity_evidence': evidence or {}}
    return {**body, 'binding_id': content_hash(body)}


def validate_binding(binding, *, fixture_id=None, comp=None, side=None, market_kind=None,
                     settlement_scope=None, environment=None, line=None):
    if not isinstance(binding, dict):
        return False
    body = {k:v for k,v in binding.items() if k != 'binding_id'}
    try:
        if binding.get('binding_id') != content_hash(body):
            return False
        rebuilt = make_binding(fixture=binding, provider=binding['provider'],
            environment=binding['environment'], event_id=binding['event_id'],
            market_id=binding['market_id'], token_id=binding.get('token_id'),
            outcome=binding['outcome'], side=binding['side'], market_kind=binding['market_kind'],
            settlement_scope=binding['settlement_scope'], period=binding['period'],
            line=binding.get('line'), identity_version=binding['identity_version'],
            rules_version=binding['rules_version'], evidence=binding.get('identity_evidence'))
        if rebuilt != binding:
            return False
    except (ValueError, TypeError, KeyError):
        return False
    expected = {'fixture_api_id': fixture_id, 'comp': comp, 'side': side,
                'market_kind': market_kind, 'settlement_scope': settlement_scope,
                'environment': environment, 'line': line}
    return all(v is None or binding.get(k) == v for k,v in expected.items())


def equivalent_contract(a, b, *, same_venue=False):
    if not validate_binding(a) or not validate_binding(b):
        return False
    keys = ('fixture_api_id','comp','season','home_api_id','away_api_id','home_id','away_id',
            'side','market_kind','settlement_scope','period','line','rules_version','kickoff_ts')
    if a.get('market_kind') == 'advance':
        keys += ('tie_id','leg')
    if not all(a.get(k) == b.get(k) for k in keys):
        return False
    return not same_venue or all(a.get(k) == b.get(k) for k in
        ('provider','environment','event_id','market_id','token_id','outcome'))


def yes_token(market):
    """Confirm token/outcome array correspondence; never assume token zero is YES."""
    try:
        outcomes, tokens = market.get('outcomes'), market.get('clobTokenIds')
        if isinstance(outcomes, str): outcomes = json.loads(outcomes)
        if isinstance(tokens, str): tokens = json.loads(tokens)
        if not isinstance(outcomes, list) or not isinstance(tokens, list) or len(outcomes) != len(tokens):
            return None
        if len(set(map(str,tokens))) != len(tokens):
            return None
        pairs = [(str(o).strip().lower(), str(t)) for o,t in zip(outcomes,tokens)]
        yes = [t for o,t in pairs if o == 'yes']
        no = [t for o,t in pairs if o == 'no']
        return yes[0] if len(pairs) == 2 and len(yes) == len(no) == 1 else None
    except (ValueError, TypeError):
        return None


def unique_event(events, *, home_id, away_id, comp=None, dates=()):
    """Only an exact scoped event can bind; no nearest date or ordering preference."""
    matched = {}
    for event in events:
        if {event.get('home_id'), event.get('away_id')} != {home_id,away_id}:
            continue
        if comp is not None and event.get('comp') != comp:
            continue
        if dates and event.get('date') not in dates:
            continue
        key = event.get('event_id') or event.get('slug') or event.get('event')
        if not key:
            continue
        if key in matched and matched[key] != event:
            return None
        matched[key] = event
    return next(iter(matched.values())) if len(matched) == 1 else None
