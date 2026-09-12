"""Execute immutable forward paper positions on demo; never choose a paper trade.

Intents and their observations are append-only. A lost POST outcome blocks another
order until a complete venue listing and receipts resolve it. Empty books/listings
are temporary; retries continue within the paper decision's valid entry window.
"""
from __future__ import annotations

from prediction_market_soccer.ops.maintenance_gate import writer

import json
import math
import re
import uuid
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.util.timing_provenance import _dt
from prediction_market_soccer.util.demo_settlement import terminal_binary_settlement

_SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_forward_intent (
 client_order_id TEXT PRIMARY KEY, paper_entry_id TEXT NOT NULL,
 decision_id TEXT NOT NULL, fixture_api_id INTEGER NOT NULL, track TEXT NOT NULL,
 action TEXT NOT NULL, ticker TEXT NOT NULL, count INTEGER NOT NULL,
 limit_price REAL NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_forward_event (
 id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT NOT NULL,
 observed_at TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS demo_forward_event_client ON demo_forward_event(client_order_id,id);
CREATE TABLE IF NOT EXISTS demo_legacy_attempt_archive (
 mirror_id INTEGER NOT NULL, sha256 TEXT NOT NULL, archived_at TEXT NOT NULL,
 paper_entry_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(mirror_id,sha256)
);
"""


def ensure_schema(conn):
    for statement in _SCHEMA.split(';'):
        if statement.strip():
            conn.execute(statement)
    for table, same in (
        ('demo_forward_intent', 'client_order_id=NEW.client_order_id'),
        ('demo_forward_event', 'id=NEW.id'),
        ('demo_legacy_attempt_archive', 'mirror_id=NEW.mirror_id AND sha256=NEW.sha256'),
    ):
        for op in ('UPDATE', 'DELETE'):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{op.lower()}_guard BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'Demo intent history is immutable'); END")
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_replace_guard BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {same}) BEGIN SELECT RAISE(ABORT,'Demo intent history cannot be replaced'); END")


def _release_empty_legacy(conn, broker, row, entry, now):
    """Archive a proven terminal zero-fill old attempt before rebinding its slot."""
    import hashlib
    old = dict(row)
    raw = json.loads(old['raw_json'] or '{}')
    if (old['status'] not in ('skipped', 'unfilled', 'error') or float(old['fill_count'] or 0)
            or float(old['exit_fill_count'] or 0) or raw.get('exit_outcome_unknown') or raw.get('paper_entry_id')):
        return False
    coid = old.get('client_order_id')
    if coid:
        orders = [o for o in broker.orders_for(old['ticker']) if o.get('client_order_id') == coid]
        if len(orders) > 1:
            return False
        if orders:
            o = orders[0]
            count = o.get('fill_count_fp', o.get('fill_count'))
            if o.get('status') not in ('executed', 'canceled', 'cancelled') or count is None or float(count) != 0:
                return False
        elif not old.get('submitted_at') or (now - _dt(old['submitted_at'])).total_seconds() <= 120:
            return False
    elif old.get('order_id') or old.get('submitted_at'):
        return False
    payload = _json(old)
    sha = hashlib.sha256(payload.encode()).hexdigest()
    conn.commit()
    conn.execute('BEGIN IMMEDIATE')
    current = conn.execute('SELECT * FROM kalshi_mirror WHERE id=?', (old['id'],)).fetchone()
    if current is None or dict(current) != old:
        conn.rollback()
        return False
    conn.execute('INSERT INTO demo_legacy_attempt_archive VALUES(?,?,?,?,?)',
                 (old['id'], sha, now.isoformat(), entry['decision_id'], payload))
    conn.execute('DELETE FROM kalshi_mirror WHERE id=?', (old['id'],))
    conn.commit()
    return True


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)


def _now():
    return datetime.now(timezone.utc)


def _reconcile_legacy(conn, broker, now, *, reconcile_pending=True):
    """No new legacy orders or paper decisions. Reconcile and observe settlement.

    Release must verify no legacy open/unknown rows. If an unexpected one appears,
    report it explicitly instead of silently assigning it a new paper lifecycle.
    """
    from prediction_market_soccer.exec.kalshi_mirror import _reconcile_pending
    if reconcile_pending:
        _reconcile_pending(conn, broker)
    warnings = []
    for row in conn.execute("SELECT * FROM kalshi_mirror WHERE json_extract(raw_json,'$.paper_entry_id') IS NULL AND status IN ('open','pending')").fetchall():
        if row['status'] == 'pending' or json.loads(row['raw_json'] or '{}').get('exit_outcome_unknown'):
            warnings.append(f"legacy {row['fixture_api_id']}/{row['track']} requires reconciliation")
            continue
        try:
            market = broker.market(row['ticker'])
            n, sold = float(row['fill_count']), float(row['exit_fill_count'] or 0)
            result = terminal_binary_settlement(market, ticker=row['ticker'], observed_at=now)
            if result is None or not n or row['avg_fill_c'] is None:
                warnings.append(f"legacy {row['fixture_api_id']}/{row['track']} open: release requires no legacy positions")
                continue
            won = result == 'yes'
            pnl = (sold * float(row['exit_avg_c'] or 0) + (n-sold) * 100 * won) / n - float(row['avg_fill_c'])
            raw = json.loads(row['raw_json'] or '{}'); raw['demo_settlement'] = market
            conn.execute("UPDATE kalshi_mirror SET status='settled',exit_reason='settled',won=?,pnl_c=?,exited_at=?,raw_json=? WHERE id=?", (int(won), pnl, now.isoformat(), _json(raw), row['id']))
        except Exception:
            warnings.append(f"legacy {row['fixture_api_id']}/{row['track']} settlement unavailable")
    conn.commit()
    return warnings


def _event(conn, intent, status, payload, now):
    conn.execute('INSERT INTO demo_forward_event(client_order_id,observed_at,status,payload) VALUES (?,?,?,?)',
                 (intent['client_order_id'], now.isoformat(), status, _json(payload)))


def _attempts(conn, entry_id=None):
    sql = '''SELECT i.*,CASE WHEN e.status IN ('filled','unfilled','rejected','absent') THEN e.status ELSE 'pending' END status,COALESCE(e.payload,'{}') event_payload,e.observed_at
      FROM demo_forward_intent i LEFT JOIN demo_forward_event e ON e.id=(
       SELECT MAX(id) FROM demo_forward_event WHERE client_order_id=i.client_order_id)'''
    args = ()
    if entry_id is not None:
        sql += ' WHERE i.paper_entry_id=?'
        args = (entry_id,)
    sql += ' ORDER BY i.created_at,i.rowid'
    return [dict(r) for r in conn.execute(sql, args)]


class DemoTickers:
    """Only demo's complete event listing; exact club identity and fixture date.

    A single requested leg is sufficient when the event title identifies both
    teams. Ambiguous event dates/pairs/duplicate target legs fail closed.
    """
    def __init__(self, conn, broker):
        self.conn, self.broker, self.cache = conn, broker, {}
        self.bindings = {}

    def for_position(self, position, fx):
        from prediction_market_soccer.config.leagues import get
        from prediction_market_soccer.venues.kalshi.discovery import _load_aliases
        from prediction_market_soccer.util.club_identity import venue_identity_index
        from zoneinfo import ZoneInfo
        entry = position['entry']
        comp = entry['comp']
        if comp not in self.cache:
            series = get(comp).kalshi.get('game')
            if not series:
                return None
            # No public endpoint and no cache that treats failed/partial pages as absence.
            self.cache[comp] = self.broker.events(series)
        records = [dict(r) for r in self.conn.execute('SELECT club_id,api_team_id,name FROM club_registry')]
        pool = {r['club_id'] for r in self.conn.execute('SELECT club_id FROM club_registry WHERE comp=?', (comp,))}
        cmap = {r['api_id']: r['canonical_team_id'] for r in self.conn.execute('SELECT * FROM team_meta')}
        hi, ai = cmap.get(fx['home_api_id']), cmap.get(fx['away_api_id'])
        if not hi or not ai or hi == ai:
            return None
        idx = venue_identity_index(allowed_ids=pool | {hi, ai}, records=records, aliases=_load_aliases(comp))
        wanted = 'draw' if entry['side'] == 'draw' else hi if entry['side'] == 'home' else ai
        kickoff = _dt(fx['kickoff_ts'])
        dates = {kickoff.date(), kickoff.astimezone(ZoneInfo('America/New_York')).date()}
        candidates = []
        identity_evidence = {}
        for ev in self.cache[comp]:
            event = ev.get('event_ticker', '')
            stamp = re.match(r'^[^-]+-(\d{2}[A-Z]{3}\d{2})', event)
            try:
                if not stamp or datetime.strptime(stamp[1], '%y%b%d').date() not in dates:
                    continue
            except ValueError:
                continue
            pair, targets = set(), []
            head = (ev.get('title') or '').split(':')[0]
            parts = re.split(r'\s+vs\.?\s+', head, flags=re.I)
            if len(parts) == 2:
                pair.update(x for x in (idx.resolve(p.strip()) for p in parts) if x)
            market_pairs, market_identities = set(), {}
            for market in ev.get('markets') or []:
                ticker = market.get('ticker', '')
                if not ticker.startswith(event + '-') or market.get('status') not in (None, 'open', 'active'):
                    continue
                label = (market.get('yes_sub_title') or '').strip()
                if label.lower().startswith('reg time:'):
                    label = label.split(':', 1)[1].strip()
                identity = 'draw' if label.lower() in ('draw', 'tie') else idx.resolve(label)
                market_identities.setdefault(ticker, set()).add(identity)
                if identity and identity != 'draw':
                    market_pairs.add(identity)
                if identity == wanted:
                    targets.append(ticker)
            # One venue contract cannot denote both teams (or a team and draw).
            # A correct event title does not resolve contradictory market labels.
            if any(len(identities) != 1 for identities in market_identities.values()):
                continue
            if market_pairs and not market_pairs <= {hi, ai}:
                continue
            if pair and pair != {hi, ai}:
                continue
            if (pair == {hi, ai} or market_pairs == {hi, ai}) and len(set(targets)) == 1:
                candidates.append(targets[0])
                identity_evidence[targets[0]] = {'method':'exact_demo_event_pair_date_and_yes_label', 'event':ev,
                    'target_markets':[m for m in (ev.get('markets') or []) if m.get('ticker')==targets[0]]}
        if len(candidates) != 1:
            if not candidates:
                return self._official_schedule_ticker(comp, fx, hi, ai, entry['side'])
            return None
        ticker = candidates[0]
        from prediction_market_soccer.util.market_identity import make_binding
        from prediction_market_soccer.util.club_identity import identity_manifest_id
        self.bindings[ticker] = make_binding(fixture={**dict(fx), 'comp':comp,'home_id':hi,'away_id':ai},
            provider='kalshi',environment='demo',event_id=ticker.rsplit('-',1)[0],market_id=ticker,
            side=entry['side'],identity_version=identity_manifest_id(),evidence=identity_evidence[ticker])
        return ticker


    def _official_schedule_ticker(self, comp, fx, home_id, away_id, side):
        """A date-miss fallback using only this broker's complete Demo catalog.

        The shared discovery verifies a fresh, exact-event official milestone and
        all three regulation contracts. No public proof, order or book is used.
        """
        from copy import deepcopy
        from prediction_market_soccer.venues.kalshi.discovery import KalshiDiscovery
        from prediction_market_soccer.util.market_identity import fixture_identity, make_binding
        base = getattr(getattr(self.broker, 'c', None), 'base', None)
        if base not in ('https://external-api.demo.kalshi.co/trade-api/v2',
                        'https://demo-api.kalshi.co/trade-api/v2'):
            return None
        discovery = KalshiDiscovery(comp, base_url=base, conn=self.conn)
        # broker.events either returns its complete listing or raises. Reuse that
        # same listing; do not fetch a second catalog or cross environments.
        discovery.md.last_discovery_status = {'complete': True, 'state': 'ok', 'issues': []}
        discovery.md.list_events = lambda series, status='open': deepcopy(self.cache[comp])
        event = discovery._entry('match', home_id, away_id, dict(fx))
        if not event or not event.get('schedule_binding'):
            return None
        ticker = event['tie'] if side == 'draw' else event['teams'].get(home_id if side == 'home' else away_id)
        if not ticker:
            return None
        self.bindings[ticker] = make_binding(
            fixture=fixture_identity(dict(fx), home_id, away_id, comp),
            provider='kalshi', environment='demo', event_id=event['event'], market_id=ticker,
            side=side, identity_version=discovery.identity_version,
            evidence={'method': 'exact_demo_official_current_milestone', 'event': event['raw'],
                      'schedule_binding': event['schedule_binding'],
                      'milestone_receipt': event['milestone_receipt']})
        return ticker


def _receipt_from_response(res, intent):
    """Unknown/malformed success is pending, never assumed zero-filled."""
    if not res.get('ok') or res.get('outcome_known') is not True:
        return None
    try:
        n = float(res['fill_count'])
        p = float(res['avg_fill']) if res.get('avg_fill') is not None else None
        if not math.isfinite(n) or not 0 <= n <= intent['count'] or float(res['remaining_count']) != 0:
            return None
        if n and (p is None or not math.isfinite(p) or not 0 <= p <= 1):
            return None
        if n and ((intent['action'] == 'buy' and p > intent['limit_price'] + 1e-8)
                  or (intent['action'] == 'sell' and p < intent['limit_price'] - 1e-8)):
            return None
        raw = json.loads(res['raw']) if isinstance(res.get('raw'), str) else res.get('raw', {})
        fee = raw.get('average_fee_paid')
        fee = float(fee) * n if fee is not None else None
        if fee is not None and (not math.isfinite(fee) or fee < 0):
            fee = None
        return {'count': n, 'price': p, 'fee_usd': fee, 'order_id': res.get('order_id'),
                'response': raw, 'cash_usd': n * p if n else 0.0}
    except (KeyError, TypeError, ValueError):
        return None


def _reconcile(conn, broker, now, *, allow_absent=True):
    from prediction_market_soccer.util.timing_provenance import fills_cash, record_fills
    warnings = []
    for intent in _attempts(conn):
        if intent['status'] != 'pending':
            continue
        try:
            found = [o for o in broker.orders_for(intent['ticker'])
                     if o.get('client_order_id') == intent['client_order_id']]
            if len(found) > 1:
                continue
            if not found:
                if allow_absent and (now - _dt(intent['created_at'])).total_seconds() > 120:
                    _event(conn, intent, 'absent', {'reason': 'complete_demo_listing_has_no_client_id'}, now)
                continue
            order = found[0]
            if order.get('status') not in ('executed', 'canceled', 'cancelled'):
                continue
            n = float(order['fill_count_fp'] if order.get('fill_count_fp') is not None else order['fill_count'])
            if not math.isfinite(n) or not 0 <= n <= intent['count']:
                continue
            fills = broker.fills_for_order(intent['ticker'], order['order_id']) if n else []
            if any(f.get('action') != intent['action'] or f.get('order_id') != order['order_id']
                   or f.get('ticker', f.get('market_ticker')) != intent['ticker'] for f in fills):
                continue
            cash = fills_cash(fills)
            if cash is None or abs(cash['count'] - n) > 1e-8:
                continue
            receipt = {'count': n, 'price': cash['cash_usd'] / n if n else None,
                       'cash_usd': cash['cash_usd'], 'fee_usd': cash['fee_usd'],
                       'order_id': order['order_id'], 'response': {'source': 'verified_fills_recovery'}}
            if n:
                record_fills(conn, fills)
            _event(conn, intent, 'filled' if n else 'unfilled', receipt, now)
        except Exception as exc:
            # Read failure/partial receipts keep the durable intent unknown.
            warnings.append(f"{intent['fixture_api_id']}/{intent['track']}: reconciliation {type(exc).__name__}")
            continue
    conn.commit()
    return warnings


def _totals(conn, entry_id):
    intents = _attempts(conn, entry_id)
    out = {'buy': {'count': 0.0, 'cash': 0.0, 'fees': 0.0, 'ids': [], 'parts': []},
           'sell': {'count': 0.0, 'cash': 0.0, 'fees': 0.0, 'ids': [], 'parts': []},
           'pending': [i for i in intents if i['status'] == 'pending'], 'intents': intents}
    for i in intents:
        if i['status'] != 'filled':
            continue
        receipt = json.loads(i['event_payload'])
        part = out[i['action']]
        part['count'] += receipt['count']
        part['cash'] += receipt['cash_usd']
        part['fees'] = (part['fees'] + receipt['fee_usd']) if part['fees'] is not None and receipt['fee_usd'] is not None else None
        part['ids'].append(receipt['order_id'])
        part['parts'].append(receipt)
    return out


def _sync_row(conn, position, now):
    entry = position['entry']
    row = conn.execute('SELECT * FROM kalshi_mirror WHERE fixture_api_id=? AND track=?',
                       (position['fixture_api_id'], position['track'])).fetchone()
    if row is None:
        return
    raw = json.loads(row['raw_json'] or '{}')
    if raw.get('paper_entry_id') != entry['decision_id']:
        return  # historical demo rows are never repurposed
    totals = _totals(conn, entry['decision_id'])
    buy, sell = totals['buy'], totals['sell']
    if not totals['intents']:
        return
    if sell['count'] > buy['count'] + 1e-8:
        raise ValueError('Demo sold more than recorded fills')
    avg = buy['cash'] / buy['count'] * 100 if buy['count'] else None
    exit_avg = sell['cash'] / sell['count'] * 100 if sell['count'] else None
    done = buy['count'] > 0 and sell['count'] >= buy['count'] - 1e-8
    pending = totals['pending']
    status = 'pending' if any(i['action'] == 'buy' for i in pending) else 'exited' if done else 'open' if buy['count'] else 'unfilled'
    last_buy = next((i for i in reversed(totals['intents']) if i['action'] == 'buy'), None)
    last_sell = next((i for i in reversed(totals['intents']) if i['action'] == 'sell'), None)
    raw.update(entry_order_ids=buy['ids'], exit_order_ids=sell['ids'],
               entry_response={'fill_count': buy['count'], 'average_fill_price': avg / 100 if avg is not None else None,
                               'average_fee_paid': buy['fees'] / buy['count'] if buy['count'] and buy['fees'] is not None else None},
               exit_fills=[{'fill_count': p['count'], 'average_fill_price': p['price'],
                            'average_fee_paid': p['fee_usd'] / p['count'] if p['fee_usd'] is not None else None}
                           for p in sell['parts']],
               exit_outcome_unknown=next((i['client_order_id'] for i in pending if i['action'] == 'sell'), None))
    if row['status'] in ('settled', 'exited'):
        return
    conn.execute('''UPDATE kalshi_mirror SET status=?,fill_count=?,avg_fill_c=?,exit_fill_count=?,exit_avg_c=?,
      order_id=?,exit_order_id=?,client_order_id=?,exit_client_order_id=?,attempts=?,filled_at=?,
      exited_at=?,exit_reason=?,pnl_c=?,raw_json=? WHERE id=?''',
      (status, buy['count'], avg, sell['count'], exit_avg,
       buy['ids'][-1] if buy['ids'] else None, sell['ids'][-1] if sell['ids'] else None,
       last_buy['client_order_id'] if last_buy else None, last_sell['client_order_id'] if last_sell else None,
       sum(i['action'] == 'buy' for i in totals['intents']), row['filled_at'] or (now.isoformat() if buy['count'] else None),
       now.isoformat() if done else None, 'smart_exit' if done else None,
       (sell['cash'] - buy['cash']) * 100 / buy['count'] if done else None, _json(raw), row['id']))


def _reconciliation_needed(conn):
    """Local read-only preflight; an idle, empty account never constructs a broker."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {'demo_forward_intent', 'demo_forward_event'} <= tables:
        attempts = _attempts(conn)
        if any(intent['status'] == 'pending' for intent in attempts):
            return True
        net = {}
        for intent in attempts:
            if intent['status'] == 'filled':
                n = float(json.loads(intent['event_payload'])['count'])
                if not math.isfinite(n) or n < 0:
                    raise ValueError('Invalid durable Demo fill count')
                entry_id = intent['paper_entry_id']
                net[entry_id] = net.get(entry_id, 0) + (n if intent['action'] == 'buy' else -n)
        closed = {r[0] for r in conn.execute("""SELECT json_extract(raw_json,'$.paper_entry_id')
                    FROM kalshi_mirror WHERE status IN ('settled','exited')""")} if 'kalshi_mirror' in tables else set()
        # A projection is replaceable; durable fills are exposure even after a
        # lost mirror row. Aggregate once, without one query per historical bet.
        if any(n > 1e-8 and entry_id not in closed for entry_id, n in net.items()):
            return True
    if 'kalshi_mirror' not in tables:
        return False
    return bool(conn.execute("""SELECT 1 FROM kalshi_mirror
        WHERE status NOT IN ('settled','exited') AND
        (status='pending' OR json_extract(raw_json,'$.exit_outcome_unknown') IS NOT NULL
         OR COALESCE(fill_count,0)>COALESCE(exit_fill_count,0)) LIMIT 1""").fetchone())


def _restore_forward_projection(conn, position):
    """Recover a missing display row from existing intents, never paper-only bets."""
    entry = position['entry']
    if conn.execute('SELECT 1 FROM kalshi_mirror WHERE fixture_api_id=? AND track=?',
                    (position['fixture_api_id'], position['track'])).fetchone():
        return
    totals = _totals(conn, entry['decision_id'])
    attempts = totals['intents']
    if not attempts or (not totals['pending'] and totals['buy']['count'] - totals['sell']['count'] <= 1e-8):
        return
    buys = [intent for intent in attempts if intent['action'] == 'buy']
    if (not buys or len({intent['ticker'] for intent in attempts}) != 1
            or any(intent['fixture_api_id'] != position['fixture_api_id'] or intent['track'] != position['track']
                   for intent in attempts)):
        raise ValueError('Cannot restore an ambiguous Demo intent projection')
    # INSERT OR IGNORE cannot repurpose a concurrent or legacy row. _sync_row
    # subsequently verifies its exact paper_entry_id before writing fill totals.
    conn.execute('''INSERT OR IGNORE INTO kalshi_mirror(
        fixture_api_id,track,comp,side,ticker,bet_kind,entry_min,ledger_entry_c,
        ledger_venue,ledger_stake_usd,ledger_edge,count,status,attempts,raw_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (position['fixture_api_id'], position['track'], entry['comp'], entry['side'], buys[0]['ticker'],
         entry.get('bet_kind'), entry.get('entry_min', 0), entry['entry_cents'], entry.get('ledger_venue'),
         entry['stake_usd'], entry.get('net_edge'), buys[-1]['count'], 'pending', len(buys),
         _json({'paper_entry_id': entry['decision_id'], 'paper_entry': entry,
                'projection_recovered_from_durable_intents': True})))


def _settle_forward_position(conn, broker, position, row, totals, now):
    held = totals['buy']['count'] - totals['sell']['count']
    if held <= 0 or totals['pending'] or position.get('status') != 'settled':
        return False
    market = broker.market(row['ticker'])
    result = terminal_binary_settlement(market, ticker=row['ticker'], observed_at=now)
    if result is None:
        return False
    won = result == 'yes'
    pnl = (totals['sell']['cash'] + held * won - totals['buy']['cash']) * 100 / totals['buy']['count']
    raw = json.loads(row['raw_json'] or '{}')
    raw.update(demo_settlement=market, demo_settlement_observed_at=now.isoformat())
    conn.execute("UPDATE kalshi_mirror SET status='settled',exit_reason='settled',won=?,pnl_c=?,exited_at=?,raw_json=? WHERE id=?",
                 (int(won), pnl, now.isoformat(), _json(raw), row['id']))
    return True


class _ReconciliationReads:
    """Only the three GET capabilities needed by idle bookkeeping."""
    def __init__(self, broker):
        self._broker = broker

    def orders_for(self, ticker):
        return self._broker.orders_for(ticker)

    def fills_for_order(self, ticker, order_id):
        return self._broker.fills_for_order(ticker, order_id)

    def market(self, ticker):
        return self._broker.market(ticker)


@writer
def reconcile_only(conn, *, broker_factory=None, now=None):
    """Observe known Demo orders and terminal payouts outside match windows.

    Never submit/retry/cancel an order. An absent order remains unknown here;
    only actual terminal orders plus matching fill receipts can resolve it.
    """
    from prediction_market_soccer.exec.kalshi_mirror import DemoBroker, enabled
    from prediction_market_soccer.util.paper_store import positions
    if conn.in_transaction:
        raise ValueError('Demo reconciliation cannot commit caller-owned work')
    out = {'enabled': enabled(), 'needed': False, 'settled': 0, 'pending': 0, 'errors': []}
    if not out['enabled']:
        return out
    if CONFIG.venue.kalshi_env != 'demo':
        raise ValueError('Idle reconciliation requires the Demo environment')
    if not _reconciliation_needed(conn):
        return out
    out['needed'] = True
    ensure_schema(conn)
    broker = _ReconciliationReads((broker_factory or DemoBroker)())
    now = now or _now()
    out['errors'].extend(_reconcile(conn, broker, now, allow_absent=False))
    # Legacy unknown outcomes must never be released to an order retry by idle
    # bookkeeping. Known old holdings can still receive their actual payout.
    out['errors'].extend(_reconcile_legacy(conn, broker, now, reconcile_pending=False))
    for position in positions(conn):
        try:
            _restore_forward_projection(conn, position)
            _sync_row(conn, position, now)
            row = conn.execute('SELECT * FROM kalshi_mirror WHERE fixture_api_id=? AND track=?',
                               (position['fixture_api_id'], position['track'])).fetchone()
            if not row or row['status'] in ('settled', 'exited'):
                continue
            raw = json.loads(row['raw_json'] or '{}')
            if raw.get('paper_entry_id') != position['entry']['decision_id']:
                continue
            totals = _totals(conn, position['entry']['decision_id'])
            out['settled'] += bool(_settle_forward_position(conn, broker, position, row, totals, now))
        except Exception as exc:
            out['errors'].append(f"{position['fixture_api_id']}/{position['track']}: idle reconciliation {type(exc).__name__}")
        finally:
            conn.commit()
    out['pending'] = sum(intent['status'] == 'pending' for intent in _attempts(conn))
    return out


def _buy_still_valid(conn, position, quoted, now):
    """Recheck paper lifecycle and current entry state while holding the write lock."""
    from prediction_market_soccer.ops.paper_trading import execution_context
    entry = position['entry']
    current = conn.execute('SELECT * FROM paper_entry WHERE decision_id=?', (entry['decision_id'],)).fetchone()
    from prediction_market_soccer.util.forward_methods import validate_entry_epoch
    validate_entry_epoch(conn, entry)
    if (not current or current['fixture_api_id'] != position['fixture_api_id']
            or current['track'] != position['track'] or current['book_version_id'] != position['book_version_id']
            or json.loads(current['payload']) != entry
            or conn.execute('SELECT 1 FROM paper_exit WHERE entry_id=?', (entry['decision_id'],)).fetchone()
            or conn.execute('SELECT 1 FROM paper_completion WHERE fixture_api_id=?', (position['fixture_api_id'],)).fetchone()):
        return False
    fx = conn.execute('SELECT * FROM fixture WHERE api_id=?', (position['fixture_api_id'],)).fetchone()
    if not fx or not 0 <= (now - _dt(quoted['quote_at'])).total_seconds() <= 30:
        return False
    if position['track'] == 'pre':
        if fx['status_short'] != 'NS' or (fx['elapsed'] or 0) != 0 or now >= _dt(fx['kickoff_ts']):
            return False
    elif (fx['status_short'] not in ('1H', 'HT', '2H', 'LIVE') or fx['elapsed'] is None
          or not 1 <= fx['elapsed'] <= 85 or now < _dt(fx['kickoff_ts'])):
        return False
    fresh = execution_context(conn, position, now=now)
    old = quoted['context']
    if (not fresh or fresh['state'] != old['state']
            or not math.isfinite(float(fresh['fair'])) or abs(float(fresh['fair']) - float(old['fair'])) > 1e-8):
        return False
    if position['track'] == 'inplay' and not 1 <= fresh['state']['elapsed'] <= 85:
        return False
    return True


def _execution_quote(conn, broker, ticker, binding, action):
    from prediction_market_soccer.util.source_history import persist_quote_receipts
    from prediction_market_soccer.util.quote_evidence import qualify_quote
    if conn.in_transaction:
        raise ValueError('Quote observation must not commit caller-owned work')
    quote = broker.book_observation(ticker, binding)
    durable = persist_quote_receipts(conn, [quote['receipt']], clock=lambda: _now().isoformat())[0]
    now = _now()
    checked = qualify_quote(quote, action, fixture_id=binding['fixture_api_id'], side=binding['side'],
        comp=binding['comp'], environment='demo', now=now.isoformat(), max_age_seconds=30,
        require_available=True, persisted_available_at=durable['available_at'])
    if not checked['eligible']:
        return None
    return checked['selected']


def _intent_binding(conn, position, ticker):
    for intent in _attempts(conn, position['entry']['decision_id']):
        if intent['ticker'] == ticker and intent['action'] == 'buy':
            receipt = (json.loads(intent['payload']).get('selected_quote') or {}).get('receipt')
            if receipt:
                return receipt['binding']
    return None


def _check_execution_receipt(conn, position, ticker, action, price, context, now):
    from prediction_market_soccer.util.source_history import resolve_receipt
    from prediction_market_soccer.util.quote_evidence import qualify_quote
    from prediction_market_soccer.util.market_identity import equivalent_contract
    from prediction_market_soccer.util.forward_methods import validate_entry_epoch
    selected = context.get('selected_quote') or {}
    receipt = resolve_receipt(conn, selected.get('receipt_id'), cutoff=now.isoformat())
    if not receipt:
        return False
    quote = {'receipt': selected.get('receipt'), 'ask': receipt['ask'], 'bid': receipt['bid']}
    checked = qualify_quote(quote, action, fixture_id=position['fixture_api_id'], side=position['entry']['side'],
        comp=position['entry']['comp'], environment='demo', now=now.isoformat(), max_age_seconds=30,
        require_available=True,persisted_available_at=receipt['available_at'])
    actual = checked['selected']
    if not checked['eligible'] or actual['receipt']['binding']['market_id'] != ticker or actual['price'] != price or any(actual.get(k) != selected.get(k) for k in ('receipt_id','binding_id','side','action','venue','price','receipt')):
        return False
    entry = position['entry']
    if entry.get('forward_epoch_id'):
        validate_entry_epoch(conn, entry, require_active=action == 'buy')
    elif action == 'buy':
        return False  # no new executions from unversioned historical paper inputs
    binding = receipt['binding']
    original = (entry.get('selected_quote') or {}).get('receipt',{}).get('binding')
    if original and not equivalent_contract(original,binding):
        return False
    fx=conn.execute('SELECT * FROM fixture WHERE api_id=?',(position['fixture_api_id'],)).fetchone()
    if not fx or binding['home_api_id'] != fx['home_api_id'] or binding['away_api_id'] != fx['away_api_id'] or _dt(binding['kickoff_ts']) != _dt(fx['kickoff_ts']):
        return False
    if action == 'sell':
        current=conn.execute('SELECT payload FROM paper_exit WHERE entry_id=?',(entry['decision_id'],)).fetchone()
        if not current or json.loads(current[0]) != position.get('exit') or conn.execute('SELECT 1 FROM paper_completion WHERE fixture_api_id=?',(position['fixture_api_id'],)).fetchone():
            return False
        owned = _intent_binding(conn,position,ticker)
        if owned and not equivalent_contract(owned,binding,same_venue=True):
            return False
    return True


@writer
def _send(conn, broker, position, ticker, action, count, price, context, now):
    from prediction_market_soccer.exec.kalshi_mirror import MAX_ORDERS_PER_DAY, MAX_OPEN_POSITIONS, MAX_ORDER_USD
    entry = position['entry']
    if isinstance(count, bool) or not isinstance(count, int) or count < 1 or not math.isfinite(price) or not 0 < price < 1:
        raise ValueError('Invalid demo order amount')
    if conn.in_transaction:
        raise ValueError('Demo send requires ownership of its durable intent transaction')
    conn.execute('BEGIN IMMEDIATE')
    try:
        now = _now()  # acquiring the lock may have waited behind a paper update
        if not 0 <= (now - _dt(context['quote_at'])).total_seconds() <= 30:
            conn.rollback()
            return None  # retry the same paper intent after fetching another quote
        if not _check_execution_receipt(conn, position, ticker, action, price, context, now):
            conn.rollback()
            return None
        if action == 'buy' and not _buy_still_valid(conn, position, context, now):
            conn.rollback()
            return None
        totals = _totals(conn, entry['decision_id'])
        if totals['pending'] or any(i['status'] == 'pending' and i['ticker'] == ticker for i in _attempts(conn)):
            conn.rollback()
            return None
        if totals['intents'] and (now - _dt(totals['intents'][-1]['created_at'])).total_seconds() < 30:
            conn.rollback()
            return None
        if action == 'buy':
            today = now.date().isoformat()
            attempts = conn.execute("SELECT COUNT(*) FROM demo_forward_intent WHERE action='buy' AND created_at>=?", (today,)).fetchone()[0]
            legacy = conn.execute("SELECT COUNT(*) FROM kalshi_mirror WHERE submitted_at>=? AND json_extract(raw_json,'$.paper_entry_id') IS NULL", (today,)).fetchone()[0]
            legacy += conn.execute("SELECT COUNT(*) FROM demo_legacy_attempt_archive WHERE json_extract(payload,'$.submitted_at')>=?", (today,)).fetchone()[0]
            open_n = conn.execute("SELECT COUNT(*) FROM kalshi_mirror WHERE status IN ('open','pending')").fetchone()[0]
            cap = min(MAX_ORDER_USD, float(entry['stake_usd']))
            if attempts + legacy >= MAX_ORDERS_PER_DAY or (not totals['buy']['count'] and open_n >= MAX_OPEN_POSITIONS) or totals['buy']['cash'] + count * price > cap + 1e-8:
                conn.rollback()
                return None
        else:
            if count > totals['buy']['count'] - totals['sell']['count'] + 1e-8:
                conn.rollback()
                return None
        coid = f"paper-demo-{action}-{uuid.uuid4().hex}"
        decision = entry if action == 'buy' else position['exit']
        intent = dict(client_order_id=coid, paper_entry_id=entry['decision_id'], decision_id=decision['decision_id'],
                      fixture_api_id=position['fixture_api_id'], track=position['track'], action=action,
                      ticker=ticker, count=count, limit_price=price, created_at=now.isoformat(), payload=_json({**context,
                      'forward_epoch_id':entry.get('forward_epoch_id'),'method_manifest_id':(entry.get('method_manifest') or {}).get('manifest_id'),
                      'book_version_id':position['book_version_id']}))
        columns = list(intent)
        conn.execute(f"INSERT INTO demo_forward_intent ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(intent.values()))
        _event(conn, intent, 'pending', {'reason': 'durable_before_http'}, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    try:
        result = (broker.buy_yes if action == 'buy' else broker.sell_yes)(ticker, count, price, coid)
        receipt = _receipt_from_response(result, intent)
        if receipt is not None:
            _event(conn, intent, 'filled' if receipt['count'] else 'unfilled', receipt, now)
            if receipt['count'] and receipt['order_id']:
                try:
                    from prediction_market_soccer.util.timing_provenance import record_fills
                    record_fills(conn, broker.fills_for_order(ticker, receipt['order_id']))
                except Exception:
                    pass
        elif not result.get('ok') and int(result.get('status_code') or 500) in (400, 401, 403, 404, 422, 429):
            _event(conn, intent, 'rejected', {'status_code': result['status_code']}, now)
        # 5xx, malformed success, conflict and unknown statuses retain pending.
    except Exception:
        pass
    _sync_row(conn, position, now)
    conn.commit()
    return {'action': 'entry' if action == 'buy' else 'smart_exit', 'fixture': position['fixture_api_id'],
            'track': position['track'], 'side': entry['side'], 'paper_decision_id': decision['decision_id'],
            'ticker': ticker, 'requested': count, 'client_order_id': coid}


@writer
def run(conn, broker, *, now=None):
    from prediction_market_soccer.exec.kalshi_mirror import MAX_ORDER_USD
    from prediction_market_soccer.util.paper_store import positions
    from prediction_market_soccer.ops.paper_trading import execution_context
    from prediction_market_soccer.strategy.edge import compute_edge
    if conn.in_transaction:
        raise ValueError('Demo cycle cannot commit caller-owned work')
    ensure_schema(conn)
    now = now or _now()
    legacy_warnings = _reconcile_legacy(conn, broker, now)
    _reconcile(conn, broker, now)
    ticker_index = DemoTickers(conn, broker)
    out = {'actions': [], 'errors': legacy_warnings}
    venue_positions = None
    for position in positions(conn):
        entry = position['entry']
        fid, track = position['fixture_api_id'], position['track']
        try:
            if entry.get('market_kind') != 'match' or track not in ('pre', 'inplay') or entry.get('side') not in ('home', 'draw', 'away'):
                continue
            _sync_row(conn, position, now)
            row = conn.execute('SELECT * FROM kalshi_mirror WHERE fixture_api_id=? AND track=?', (fid, track)).fetchone()
            legacy = None
            if row and json.loads(row['raw_json'] or '{}').get('paper_entry_id') != entry['decision_id']:
                if row['status'] not in ('skipped', 'unfilled', 'error') or float(row['fill_count'] or 0) or float(row['exit_fill_count'] or 0):
                    continue
                legacy, row = row, None
            if row and row['status'] in ('exited', 'settled'):
                continue
            totals = _totals(conn, entry['decision_id'])
            if totals['pending']:
                continue
            fx = conn.execute('SELECT * FROM fixture WHERE api_id=?', (fid,)).fetchone()
            if not fx:
                continue
            held = totals['buy']['count'] - totals['sell']['count']
            # Venue settlement is a fact of the demo contract, not an inferred fill
            # or payoff from a fixture's provisional FT/AWD/WO status.
            if held and position.get('status') == 'settled':
                _settle_forward_position(conn, broker, position, row, totals, now)
                continue
            if position.get('exit'):
                if held < 1 or not row:
                    continue
                ticker = row['ticker']
                if any(i['status'] == 'pending' and i['ticker'] == ticker for i in _attempts(conn)):
                    continue
                binding = _intent_binding(conn,position,ticker)
                if not binding:
                    resolved = ticker_index.for_position(position,fx)
                    if resolved != ticker:
                        continue
                    binding = ticker_index.bindings[ticker]
                if venue_positions is None:
                    venue_positions = broker.positions()
                count = min(math.floor(held + 1e-8), math.floor(venue_positions.get(ticker, 0)))
                if count < 1:
                    continue
                conn.commit()  # cycle-owned projection writes; observation owns its commits
                selected = _execution_quote(conn,broker,ticker,binding,'sell')
                if not selected:
                    continue
                bid = selected['price']
                before = _dt(selected['receipt']['request_started_at'])
                exit_ = position['exit']
                conn.execute('UPDATE kalshi_mirror SET exit_min=?,exit_bid_c=?,exit_fair_c=?,raw_json=json_patch(raw_json,?) WHERE id=?',
                             (exit_.get('sold_min'), exit_.get('sold_c'), exit_.get('fair_c'), _json({'paper_exit_id': exit_['decision_id']}), row['id']))
                conn.commit()
                act = _send(conn, broker, position, ticker, 'sell', count, bid,
                            {'paper_exit': exit_, 'demo_bid': bid, 'quote_at': before.isoformat(), 'selected_quote': selected}, _now())
                if act:
                    # Re-read before another position sharing this ticker: unknown
                    # execution also reserves these contracts for this cycle.
                    venue_positions[ticker] = max(0, venue_positions.get(ticker, 0) - count)
                    out['actions'].append(act)
                continue
            if position.get('status') != 'open':
                continue
            context = execution_context(conn, position, now=now)
            if not context:
                continue
            if legacy and not _release_empty_legacy(conn, broker, legacy, entry, now):
                continue
            ticker = row['ticker'] if row and row['ticker'] else ticker_index.for_position(position, fx)
            if not ticker:
                continue
            binding = _intent_binding(conn,position,ticker)
            if not binding:
                resolved = ticker_index.for_position(position,fx)
                if resolved != ticker:
                    continue
                binding = ticker_index.bindings[ticker]
            conn.commit()
            selected = _execution_quote(conn,broker,ticker,binding,'buy')
            if not selected:
                continue
            ask = selected['price']
            before = _dt(selected['receipt']['request_started_at'])
            cap = min(MAX_ORDER_USD, float(entry['stake_usd']))
            count = math.floor((cap - totals['buy']['cash'] + 1e-9) / ask)
            if count < 1:
                continue
            fair = float(context['fair'])
            if not math.isfinite(fair) or not 0 <= fair <= 1:
                continue
            fee = broker.estimate_taker_fee(ticker, count, ask)
            fee_p = float(fee['fee_per_contract'])
            if not math.isfinite(fee_p) or fee_p < 0:
                continue
            threshold = CONFIG.risk.min_net_edge if track == 'inplay' else CONFIG.decision.min_net_edge
            if track == 'pre' and ask * 100 < CONFIG.decision.longshot_cents:
                threshold += CONFIG.decision.longshot_extra_theta
            if track == 'pre' and entry['side'] == 'draw':
                threshold += getattr(CONFIG.decision, 'draw_extra_theta', 0.0)
            edge = compute_edge(fair, ask, sigma_p=float((entry.get('sigma') or {}).get(entry['side'], 0)),
                                k=CONFIG.risk.shrink_k, fee=fee_p, theta=threshold)
            if entry.get('bet_kind') in ('value', 'relative_value') and not edge.tradable:
                continue
            # Model/context and kickoff may have expired while the venue reads ran.
            actual_now = _now()
            if (actual_now - before).total_seconds() > 30:
                continue
            if track == 'pre' and actual_now >= _dt(fx['kickoff_ts']):
                continue
            fresh = execution_context(conn, position, now=actual_now)
            if not fresh or fresh['state'] != context['state'] or abs(float(fresh['fair'])-fair) > 1e-8:
                continue
            if row is None:
                conn.execute('''INSERT INTO kalshi_mirror(fixture_api_id,track,comp,side,ticker,bet_kind,entry_min,
                 ledger_entry_c,ledger_venue,ledger_stake_usd,ledger_edge,count,status,attempts,raw_json)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (fid, track, entry['comp'], entry['side'], ticker, entry.get('bet_kind'), entry.get('entry_min', 0),
                   entry['entry_cents'], entry.get('ledger_venue'), entry['stake_usd'], entry.get('net_edge'),
                   0, 'unfilled', 0, _json({'paper_entry_id': entry['decision_id'], 'paper_entry': entry})))
            conn.execute('UPDATE kalshi_mirror SET ask_c=?,count=?,submitted_at=COALESCE(submitted_at,?) WHERE fixture_api_id=? AND track=?',
                         (ask * 100, count, now.isoformat(), fid, track))
            conn.commit()
            act = _send(conn, broker, position, ticker, 'buy', count, ask,
                        {'context': fresh, 'demo_ask': ask, 'quote_at': before.isoformat(), 'fee': fee, 'selected_quote': selected,
                         'execution_net_edge':edge.net_edge if hasattr(edge,'net_edge') else None}, actual_now)
            if act:
                out['actions'].append(act)
        except Exception as exc:
            out['errors'].append(f'{fid}/{track}: {type(exc).__name__}: {str(exc)[:120]}')
        finally:
            conn.commit()
    return out
