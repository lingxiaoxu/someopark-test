"""Durable forward paper positions, independent of every broker and fill.

Entries, exits, observations and terminal records are append-only. Historical
settled_bet / mirror rows are never used to invent a position and are never changed.
"""
from __future__ import annotations

from prediction_market_soccer.ops.maintenance_gate import writer

import hashlib
import json
import math

from prediction_market_soccer.util import forward_methods as fm
from datetime import datetime, timezone


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def dt(value):
    value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Paper timestamps require an explicit timezone')
    return value.astimezone(timezone.utc)


def _json_keys(value):
    """Use JSON's string-key representation before deterministic sorting.

    Live corner/total lines may have float keys alongside named fields. Normal
    JSON encoding accepts these, but sorting the Python keys compares float to
    str. Reject collisions rather than silently discarding an observed value.
    """
    if isinstance(value, dict):
        normalized = {}
        for key, child in value.items():
            if isinstance(key, str):
                text = key
            elif key is None or isinstance(key, (bool, int, float)):
                text = json.dumps(key, allow_nan=False)
            else:
                raise TypeError('Unsupported paper observation object key')
            if text in normalized:
                raise ValueError('Paper observation keys collide after JSON normalization: ' + text)
            normalized[text] = _json_keys(child)
        return normalized
    if isinstance(value, (tuple, list)):
        return [_json_keys(child) for child in value]
    return value


def _json(value):
    return json.dumps(_json_keys(value), sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False, default=str)


def ensure(conn):
    for sql in (
        "CREATE TABLE IF NOT EXISTS paper_entry (decision_id TEXT PRIMARY KEY, book_version_id TEXT NOT NULL, fixture_api_id INTEGER NOT NULL, track TEXT NOT NULL CHECK(track IN ('pre','inplay')), decision_at TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(fixture_api_id,track))",
        "CREATE TABLE IF NOT EXISTS paper_exit (decision_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL UNIQUE, decision_at TEXT NOT NULL, payload TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS paper_observation (fixture_api_id INTEGER NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(fixture_api_id,observed_at))",
        "CREATE TABLE IF NOT EXISTS paper_evaluation (book_version_id TEXT NOT NULL, fixture_api_id INTEGER NOT NULL, track TEXT NOT NULL, milestone TEXT NOT NULL, observed_at TEXT NOT NULL, verdict TEXT NOT NULL, PRIMARY KEY(book_version_id,fixture_api_id,track,milestone))",
        "CREATE TABLE IF NOT EXISTS paper_evaluation_v2 (epoch_id TEXT NOT NULL, fixture_api_id INTEGER NOT NULL, track TEXT NOT NULL, milestone TEXT NOT NULL, observed_at TEXT NOT NULL, verdict TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(epoch_id,fixture_api_id,track,milestone))",
        "CREATE TABLE IF NOT EXISTS paper_completion (source_id TEXT PRIMARY KEY, book_version_id TEXT NOT NULL, fixture_api_id INTEGER NOT NULL, settled_at TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(book_version_id,fixture_api_id))",
        "CREATE TABLE IF NOT EXISTS paper_data_state_event (event_id TEXT PRIMARY KEY, book_version_id TEXT NOT NULL, epoch_id TEXT NOT NULL, fixture_api_id INTEGER NOT NULL, track TEXT NOT NULL, milestone TEXT NOT NULL, observed_at TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL)",
    ):
        conn.execute(sql)
    keys = {'paper_entry': '(decision_id=NEW.decision_id OR (fixture_api_id=NEW.fixture_api_id AND track=NEW.track))',
            'paper_exit': '(decision_id=NEW.decision_id OR entry_id=NEW.entry_id)',
            'paper_observation': '(fixture_api_id=NEW.fixture_api_id AND observed_at=NEW.observed_at)',
            'paper_completion': '(source_id=NEW.source_id OR (book_version_id=NEW.book_version_id AND fixture_api_id=NEW.fixture_api_id))'}
    for table, key in keys.items():
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_conflicting_insert BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {key} AND payload<>NEW.payload) BEGIN SELECT RAISE(ABORT, 'Conflicting immutable paper record'); END")
    columns = {'paper_entry': ('decision_id','book_version_id','fixture_api_id','track','decision_at','payload'),
               'paper_exit': ('decision_id','entry_id','decision_at','payload'),
               'paper_observation': ('fixture_api_id','observed_at','payload'),
               'paper_completion': ('source_id','book_version_id','fixture_api_id','settled_at','payload')}
    for table, names in columns.items():
        changed = ' OR '.join(f'{name} IS NOT NEW.{name}' for name in names)
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_conflicting_insert_v2 BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {keys[table]} AND ({changed})) BEGIN SELECT RAISE(ABORT, 'Conflicting immutable paper record'); END")
    conn.execute("CREATE TRIGGER IF NOT EXISTS paper_evaluation_no_conflicting_insert BEFORE INSERT ON paper_evaluation WHEN EXISTS(SELECT 1 FROM paper_evaluation WHERE book_version_id=NEW.book_version_id AND fixture_api_id=NEW.fixture_api_id AND track=NEW.track AND milestone=NEW.milestone AND (observed_at<>NEW.observed_at OR verdict<>NEW.verdict)) BEGIN SELECT RAISE(ABORT, 'Conflicting immutable paper evaluation'); END")
    conn.execute("CREATE TRIGGER IF NOT EXISTS paper_evaluation_v2_no_conflicting_insert BEFORE INSERT ON paper_evaluation_v2 WHEN EXISTS(SELECT 1 FROM paper_evaluation_v2 WHERE epoch_id=NEW.epoch_id AND fixture_api_id=NEW.fixture_api_id AND track=NEW.track AND milestone=NEW.milestone AND payload<>NEW.payload) BEGIN SELECT RAISE(ABORT, 'Conflicting immutable paper evaluation'); END")
    conn.execute("CREATE TRIGGER IF NOT EXISTS paper_data_state_no_replace BEFORE INSERT ON paper_data_state_event WHEN EXISTS(SELECT 1 FROM paper_data_state_event WHERE event_id=NEW.event_id) BEGIN SELECT RAISE(ABORT,'Paper data state cannot be replaced'); END")
    for table in ('paper_entry', 'paper_exit', 'paper_observation', 'paper_evaluation', 'paper_evaluation_v2', 'paper_completion','paper_data_state_event'):
        for op in ('UPDATE', 'DELETE'):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT, 'Paper observations and decisions are immutable'); END")


def data_states(conn, epoch_id):
    """Latest diagnostic state per opportunity; never an eligibility/freeze gate."""
    if not _table_exists(conn,'paper_data_state_event'):
        return []
    rows=conn.execute('''SELECT e.* FROM paper_data_state_event e WHERE epoch_id=? AND rowid=(
        SELECT MAX(x.rowid) FROM paper_data_state_event x WHERE x.epoch_id=e.epoch_id
        AND x.fixture_api_id=e.fixture_api_id AND x.track=e.track AND x.milestone=e.milestone)''',(epoch_id,))
    return [dict(row) for row in rows]


@writer
def record_data_state(conn, version, epoch, fixture_id, track, milestone, at, state, reason):
    """Append-only operational diagnostics, separate from frozen evaluations/P&L."""
    if state not in ('waiting_data','missed_data','decision_recorded','no_edge'):
        raise ValueError('Unknown paper data state')
    payload={'book_version_id':version['version_id'],'epoch_id':epoch['epoch_id'],
        'fixture_api_id':fixture_id,'track':track,'milestone':milestone,'observed_at':at,
        'state':state,'reason':reason,'retryable':state=='waiting_data'}
    with fm.transaction(conn):
        ensure(conn)
        old=conn.execute('''SELECT state,reason FROM paper_data_state_event WHERE epoch_id=?
            AND fixture_api_id=? AND track=? AND milestone=? ORDER BY rowid DESC LIMIT 1''',
            (epoch['epoch_id'],fixture_id,track,milestone)).fetchone()
        if old and tuple(old)==(state,reason):
            return False
        conn.execute('INSERT INTO paper_data_state_event VALUES (?,?,?,?,?,?,?,?,?,?)',
            (fm.digest(payload),version['version_id'],epoch['epoch_id'],fixture_id,track,milestone,at,state,reason,_json(payload)))
    return True


def _table_exists(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _venue(name):
    return 'poly' if name in ('poly', 'poly_us') else name


@writer
def observe(conn, fixture_id, payload, *, clock=now_iso):
    """Persist actual raw receipts before the observation; no transaction stealing."""
    from prediction_market_soccer.util.source_history import persist_quote_receipts
    receipts = list(payload.get('quote_receipts') or [])
    for quotes in (payload.get('quotes') or {}).values():
        if isinstance(quotes, dict):
            for quote in quotes.values():
                receipt = quote.get('receipt') if isinstance(quote, dict) else None
                if receipt:
                    receipts.append(receipt)
    if receipts:
        receipts = list({receipt['receipt_id']:receipt for receipt in receipts}.values())
        persist_quote_receipts(conn, receipts, clock=clock)
    with fm.transaction(conn):
        ensure(conn)
        conn.execute('INSERT OR IGNORE INTO paper_observation VALUES (?,?,?)',
                     (fixture_id, payload['observed_at'], _json(payload)))


def observe_pre(conn, fixture_id, at, kalshi_q, poly_q):
    """Append retrieved receipts; never replace a historical PRE mark."""
    row = {'fixture_api_id': fixture_id, 'milestone': 'PRE', 'observed_at': at, 'ts': at,
           'source_as_of': at, 'status_short': 'NS', 'quotes': {'kalshi': kalshi_q or {}, 'poly': poly_q or {}},
           'state': {'elapsed': 0, 'home_goals': 0, 'away_goals': 0, 'reds_home': 0, 'reds_away': 0}}
    for venue, quotes in (('kalshi', kalshi_q), ('poly', poly_q)):
        for side in ('home', 'draw', 'away'):
            for field in ('ask', 'bid'):
                row[f'{venue}_{side}_{field}'] = ((quotes or {}).get(side) or {}).get(field)
    observe(conn, fixture_id, row)


def observations(conn, fixture_id):
    if not _table_exists(conn, 'paper_observation'):
        return []
    return [json.loads(r[0]) for r in conn.execute('SELECT payload FROM paper_observation WHERE fixture_api_id=? ORDER BY observed_at', (fixture_id,))]


def available_quotes(conn, observation, at, *, action='buy', side=None, entry=None):
    """Return only complete, durable, timely contract receipts; never raw floats."""
    from prediction_market_soccer.util.source_history import resolve_receipt
    from prediction_market_soccer.util.quote_evidence import qualify_quote, quote_from_receipt
    from prediction_market_soccer.util.market_identity import equivalent_contract
    out = {}
    for venue, quotes in (observation.get('quotes') or {}).items():
        if not isinstance(quotes, dict):
            continue
        for candidate_side in ('home', 'draw', 'away'):
            if side is not None and candidate_side != side:
                continue
            quote = quotes.get(candidate_side)
            if not isinstance(quote, dict) or not isinstance(quote.get('receipt'), dict):
                continue
            original = quote['receipt']
            receipt = resolve_receipt(conn, original.get('receipt_id'), cutoff=at)
            if not receipt:
                continue
            if observation.get('milestone') != 'PRE':
                # The match state must be known before requesting this orderbook.
                # A 120-second TTL alone permits a pre-goal book with a post-goal
                # model. PRE's source_as_of is stash completion, so it is distinct.
                try:
                    state_at=max(dt(value) for value in (observation['source_as_of'],observation.get('state_available_at')) if value)
                    if dt(receipt['request_started_at']) < state_at or (
                        receipt.get('provider_quote_at') and dt(receipt['provider_quote_at']) < state_at):
                        continue
                except (ValueError,TypeError,KeyError):
                    continue
            qualified = qualify_quote(quote, action, fixture_id=observation['fixture_api_id'],
                side=candidate_side, now=at, require_available=True,
                persisted_available_at=receipt['available_at'])
            if not qualified['eligible']:
                continue
            selected = qualified['selected']
            if _venue(selected['venue']) != _venue(venue):
                continue
            if entry:
                previous = (entry.get('selected_quote') or {}).get('receipt', {}).get('binding')
                if previous:
                    if not equivalent_contract(previous, receipt['binding'], same_venue=True):
                        continue
                elif _venue(entry.get('ledger_venue')) != _venue(selected['venue']):
                    continue  # legacy entries keep their recorded venue; never invent a binding
            selected['persisted_available_at'] = receipt['available_at']
            out.setdefault(_venue(venue), {})[candidate_side] = selected
    return out


def choose_quote(quotes, side, *, action='buy', priority=False):
    options = [quotes[v][side] for v in ('poly', 'kalshi') if side in quotes.get(v, {})]
    if not options:
        return None
    return options[0] if priority else (min(options, key=lambda q:q['price']) if action == 'buy' else max(options, key=lambda q:q['price']))


def evaluated(conn, version_id, fixture_id, track, milestone, *, epoch_id=None):
    table = 'paper_evaluation_v2' if epoch_id else 'paper_evaluation'
    if not _table_exists(conn, table):
        return False
    column = 'epoch_id' if epoch_id else 'book_version_id'
    return bool(conn.execute(f'SELECT 1 FROM {table} WHERE {column}=? AND fixture_api_id=? AND track=? AND milestone=?',
                            (epoch_id or version_id, fixture_id, track, milestone)).fetchone())


def mark_evaluated(conn, version_id, fixture_id, track, milestone, at, verdict, *, epoch_id=None, evidence=None):
    """Low-level append; caller must hold the same transaction as its decision."""
    if not conn.in_transaction:
        raise ValueError('Paper evaluation must share its checked decision transaction')
    if not epoch_id:
        conn.execute('INSERT OR IGNORE INTO paper_evaluation VALUES (?,?,?,?,?,?)',
                     (version_id, fixture_id, track, milestone, at, verdict))
    else:
        conn.execute('INSERT OR IGNORE INTO paper_evaluation_v2 VALUES (?,?,?,?,?,?,?)',
                     (epoch_id, fixture_id, track, milestone, at, verdict, _json(evidence or {})))


def _check_observation(conn, fixture_id, payload, *, pre=False):
    at = dt(payload['decision_at'])
    snapshot = payload['input_snapshot']
    row = conn.execute('SELECT payload FROM paper_observation WHERE fixture_api_id=? AND observed_at=?',
                       (fixture_id, snapshot['observed_at'])).fetchone()
    if not row or row[0] != _json(snapshot):
        raise ValueError('Decision requires the exact durably recorded observation')
    if payload.get('state') != snapshot.get('state') or payload.get('snapshot_observed_at') != snapshot['observed_at'] or payload.get('snapshot_ts') != snapshot['ts']:
        raise ValueError('Decision and observation state disagree')
    fx = conn.execute('SELECT * FROM fixture WHERE api_id=?', (fixture_id,)).fetchone()
    from prediction_market_soccer.ops.paper_trading import _fresh
    if not fx or not _fresh(snapshot, fx, at.isoformat(), pre=pre, conn=conn):
        raise ValueError('Decision requires the current observed regulation state')
    return fx


def _check_selected(conn, fixture_id, payload, *, action, entry=None):
    side = entry['side'] if entry else payload['side']
    selected = payload.get('selected_quote')
    if not isinstance(selected, dict):
        raise ValueError('Decision requires a selected durable BBO receipt')
    choices = available_quotes(conn, payload['input_snapshot'], payload['decision_at'], action=action, side=side, entry=entry)
    actual = next((q for sides in choices.values() for q in sides.values() if q['receipt_id'] == selected.get('receipt_id')), None)
    if not actual or any(actual.get(k) != selected.get(k) for k in ('receipt_id', 'binding_id', 'side', 'action', 'venue', 'price', 'receipt')):
        raise ValueError('Selected quote changed or is not durably available')
    binding = actual['receipt']['binding']
    fx = conn.execute('SELECT * FROM fixture WHERE api_id=?', (fixture_id,)).fetchone()
    if (binding['home_api_id'] != fx['home_api_id'] or binding['away_api_id'] != fx['away_api_id'] or dt(binding['kickoff_ts']) != dt(fx['kickoff_ts'])):
        raise ValueError('Contract does not bind the current fixture identity')
    identity = entry or payload
    if any(binding.get(key) != identity.get(key) for key in ('comp','home_id','away_id')):
        raise ValueError('Contract canonical team or competition identity changed')
    cents = payload['entry_cents'] if action == 'buy' else payload['sold_c']
    if float(cents) != round(actual['price'] * 100, 1):
        raise ValueError('Decision price differs from its selected quote')
    if action == 'buy' and _venue(payload['ledger_venue']) != _venue(actual['venue']):
        raise ValueError('Decision venue differs from its selected quote')
    return actual


def _check_entry_epoch(conn, version, payload):
    from prediction_market_soccer.util.frozen_strategy_store import active_version
    current = active_version(conn)
    if current['version_id'] != version['version_id']:
        raise ValueError('The active paper book changed before commit')
    data = {**payload, 'book_version_id': version['version_id']}
    epoch = fm.validate_entry_epoch(conn, data, require_active=True)
    manifests = [value['input_manifest'] for value in (payload.get('model_inputs') or {}).values() if isinstance(value, dict) and isinstance(value.get('input_manifest'), dict)]
    if not manifests or any(manifest.get('forward_epoch_id') != epoch['epoch_id'] or not manifest.get('model_available_at') or dt(manifest['model_available_at']) > dt(payload['decision_at']) for manifest in manifests):
        raise ValueError('Decision precedes durable model input availability')
    if dt(payload['snapshot_observed_at']) < max(dt(version['activated_at']), dt(fm.active_epoch(conn)['activated_at'])):
        raise ValueError('Cannot replay an observation from before activation')
    return epoch


@writer
def record_entry(conn, version, fixture_id, track, payload):
    if payload.get('market_kind') != 'match' or track not in ('pre', 'inplay') or payload['side'] not in ('home', 'draw', 'away'):
        raise ValueError('Invalid regulation paper side or track')
    for key in ('stake_usd', 'entry_cents', 'lambda_home', 'lambda_away'):
        value = float(payload[key])
        if not math.isfinite(value) or value <= 0 or (key == 'entry_cents' and value >= 100):
            raise ValueError('Invalid paper financial/model input: ' + key)
    with fm.transaction(conn):
        ensure(conn)
        _check_entry_epoch(conn, version, payload)
        if conn.execute('SELECT 1 FROM paper_completion WHERE fixture_api_id=?', (fixture_id,)).fetchone():
            raise ValueError('A terminal paper fixture cannot receive a late entry')
        _check_observation(conn, fixture_id, payload, pre=track == 'pre')
        _check_selected(conn, fixture_id, payload, action='buy')
        decision_id = hashlib.sha256(f"{version['version_id']}:{fixture_id}:{track}:entry".encode()).hexdigest()
        data = {**payload, 'decision_id': decision_id, 'book_version_id': version['version_id']}
        cur = conn.execute('INSERT OR IGNORE INTO paper_entry VALUES (?,?,?,?,?,?)',
                           (decision_id, version['version_id'], fixture_id, track, data['decision_at'], _json(data)))
        if cur.rowcount:
            mark_evaluated(conn, version['version_id'], fixture_id, track, payload['milestone'], payload['decision_at'],
                           'entry', epoch_id=payload['forward_epoch_id'], evidence={'decision_id': decision_id})
        return data if cur.rowcount else None


def _current_position(conn, entry_id):
    return next((p for p in positions(conn) if p['entry']['decision_id'] == entry_id), None)


@writer
def record_exit(conn, position, payload):
    entry = position['entry']
    with fm.transaction(conn):
        ensure(conn)
        current = _current_position(conn, entry['decision_id'])
        if not current or current['status'] != 'open':
            return None
        if current['entry'] != entry:
            raise ValueError('Exit position differs from its durable entry')
        if entry.get('forward_epoch_id'):
            fm.validate_entry_epoch(conn, entry)
        if dt(payload['decision_at']) < dt(entry['decision_at']) or dt(payload['snapshot_observed_at']) < dt(entry['decision_at']):
            raise ValueError('Exit or observation precedes paper entry')
        _check_observation(conn, position['fixture_api_id'], payload)
        _check_selected(conn, position['fixture_api_id'], payload, action='sell', entry=entry)
        decision_id = hashlib.sha256((entry['decision_id'] + ':exit').encode()).hexdigest()
        data = {**payload, 'decision_id': decision_id, 'entry_id': entry['decision_id'], 'side': entry['side'],
                **{k:entry.get(k) for k in ('forward_epoch_id','method_manifest','model_version','method_version')}}
        cur = conn.execute('INSERT OR IGNORE INTO paper_exit VALUES (?,?,?,?)',
                           (decision_id, entry['decision_id'], data['decision_at'], _json(data)))
        if cur.rowcount:
            mark_evaluated(conn, position['book_version_id'], position['fixture_api_id'], 'exit:' + position['track'],
                           payload['milestone'], payload['decision_at'], 'exit', epoch_id=entry.get('forward_epoch_id'), evidence={'decision_id': decision_id})
        return data if cur.rowcount else None


@writer
def record_evaluation(conn, version, fixture_id, track, milestone, payload, *, position=None):
    """Seal a successful no-edge/hold evaluation, never unavailable inputs."""
    with fm.transaction(conn):
        ensure(conn)
        if position:
            current = _current_position(conn, position['entry']['decision_id'])
            if not current or current['status'] != 'open':
                return False
            entry = current['entry']
            if entry.get('forward_epoch_id'):
                fm.validate_entry_epoch(conn, entry)
            _check_observation(conn, fixture_id, payload)
            _check_selected(conn, fixture_id, payload, action='sell', entry=entry)
            epoch_id, verdict = entry.get('forward_epoch_id'), 'hold'
        else:
            _check_entry_epoch(conn, version, payload)
            _check_observation(conn, fixture_id, payload, pre=track == 'pre')
            if conn.execute('SELECT 1 FROM paper_entry WHERE fixture_api_id=? AND track=?', (fixture_id,track)).fetchone():
                return False
            choices = available_quotes(conn, payload['input_snapshot'], payload['decision_at'])
            if not all(choose_quote(choices, side) for side in ('home','draw','away')) or payload.get('model_evaluation_complete') is not True:
                raise ValueError('Unavailable input is not a completed no-edge evaluation')
            epoch_id, verdict = payload['forward_epoch_id'], 'no_observed_edge'
        mark_evaluated(conn, version['version_id'], fixture_id, track, milestone, payload['decision_at'], verdict,
                       epoch_id=epoch_id, evidence=payload)
        return True


def positions(conn, version_id=None):
    if not _table_exists(conn, 'paper_entry'):
        return []
    rows = conn.execute('SELECT e.*,x.payload exit_payload,c.payload completion_payload FROM paper_entry e LEFT JOIN paper_exit x ON x.entry_id=e.decision_id LEFT JOIN paper_completion c ON c.book_version_id=e.book_version_id AND c.fixture_api_id=e.fixture_api_id ' +
                        ('WHERE e.book_version_id=? ' if version_id else '') + 'ORDER BY e.decision_at,e.decision_id',
                        (version_id,) if version_id else ()).fetchall()
    return [{'book_version_id': r['book_version_id'], 'fixture_api_id': r['fixture_api_id'], 'track': r['track'],
             'entry': json.loads(r['payload']), 'exit': json.loads(r['exit_payload']) if r['exit_payload'] else None,
             'settlement': json.loads(r['completion_payload']) if r['completion_payload'] else None,
             'status': 'settled' if r['completion_payload'] else ('exited' if r['exit_payload'] else 'open')} for r in rows]


def _render(fixture, legs, result, score, closed_at):
    """Project only recorded financial inputs; no model/market/history lookup."""
    from prediction_market_soccer.util.pricing import pnl_cents, sized_pnl_cents
    base = next(iter(legs.values()))['entry']
    labels = {'home': base.get('home', base['home_id']), 'draw': 'Draw', 'away': base.get('away', base['away_id'])}
    row = {'fixture_id': fixture['api_id'], 'league': base['comp'], 'date': base['kickoff_ts'][:10],
           **{key: base.get(key, '') for key in ('home', 'away', 'home_id', 'away_id', 'home_zh', 'away_zh')},
           'score': score, 'result': result, 'stage': base.get('stage', 'group'), 'bet': 'pre' in legs,
           'evidence_level': 'forward_observed_paper', 'pit_status': 'forward_recorded_inputs', 'strict_oos': False,
           'fee_status': 'not_deducted', 'book_version_id': base['book_version_id'],
           'model_version': base['model_version'], 'method_version': base['method_version'],
           'settlement_observed_at': closed_at, 'advance': None,
           'no_entry': {track: 'no_observed_decision_before_final_result' for track in ('pre', 'inplay') if track not in legs},
           'pick': None, 'pick_team': None, 'entry_cents': None, 'stake_usd': 0.0, 'won': None,
           'settle_cents': None, 'smart_exit': None, 'realized_pnl_cents': None,
           'inplay_side': None, 'inplay_side_team': None, 'inplay_entry_cents': None, 'inplay_stake_usd': 0.0,
           'inplay_won': None, 'inplay_exit': None, 'inplay_pnl_cents': None,
           'model_pick': None, 'model_pick_team': None, 'model_won': None, 'argmax_entry_cents': None,
           'argmax_settle_cents': None, 'argmax_pnl_cents': None, 'argmax_cum_pnl_cents': None}
    for track, pos in legs.items():
        entry, exit_ = pos['entry'], pos['exit']
        side, cents, stake = entry['side'], entry['entry_cents'], entry['stake_usd']
        won = side == result
        unit = pnl_cents(cents, won)
        sx = ({**exit_, 'pnl_c': round(exit_['sold_c'] - cents, 1),
               'vs_hold': round(exit_['sold_c'] - (100.0 if won else 0), 1)} if exit_ else None)
        realized = sized_pnl_cents(cents, sx['pnl_c'] if sx else unit, stake)
        hold = sized_pnl_cents(cents, unit, stake)
        row[track + '_decision_id'] = entry['decision_id']
        row[track + '_decision_at'] = entry['decision_at']
        for key in ('forward_epoch_id', 'model_version', 'method_version', 'method_manifest'):
            row[track + '_' + key] = entry.get(key)
        row.setdefault('leg_methods', {})[track] = {key: entry.get(key) for key in ('forward_epoch_id', 'model_version', 'method_version', 'method_manifest')}
        if track == 'pre':
            row.update(pick=side, pick_team=labels[side], bet_kind=entry['bet_kind'], entry_cents=cents,
                       entry_source=entry['ledger_venue'], stake_usd=stake, won=won,
                       settle_cents=100.0 if won else 0.0, smart_exit=sx, realized_pnl_cents=realized,
                       pnl_cents=hold, pnl=round(hold / 100, 3), price=cents / 100,
                       dec_odds=round(100 / cents, 3), edge=entry.get('net_edge'), model_prob=entry['model'][side],
                       confidence_k=entry.get('confidence_k'), clv_cents=None,
                       model_pick=entry.get('model_pick'), model_pick_team=labels.get(entry.get('model_pick')),
                       model_won=entry.get('model_pick') == result)
        else:
            row.update(inplay_side=side, inplay_side_team=labels[side], inplay_milestone=entry['milestone'],
                       inplay_entry_cents=cents, inplay_stake_usd=stake, inplay_won=won, inplay_exit=sx,
                       inplay_pnl_cents=realized, inplay_hold_cents=hold, inplay_edge=entry.get('net_edge'))
    row['forward_epoch_ids'] = sorted({p['entry']['forward_epoch_id'] for p in legs.values() if p['entry'].get('forward_epoch_id')})
    for key in ('model_version', 'method_version'):
        values = {p['entry'].get(key) for p in legs.values()}
        row[key] = next(iter(values)) if len(values) == 1 else None
    return row


@writer
def settle(conn, fixture_ids=None, now=None):
    """Seal only existing paper positions after an observed regulation result."""
    from prediction_market_soccer.util.pricing import reg_score
    from prediction_market_soccer.util.timing_provenance import result_availability
    with fm.transaction(conn):
        ensure(conn)
        grouped = {}
        for position in positions(conn):
            if position['settlement'] or (fixture_ids is not None and position['fixture_api_id'] not in fixture_ids):
                continue
            grouped.setdefault((position['book_version_id'], position['fixture_api_id']), {})[position['track']] = position
        count = 0
        clock = dt(now or now_iso())
        for (version_id, fid), legs in grouped.items():
            fx = conn.execute('SELECT * FROM fixture WHERE api_id=?', (fid,)).fetchone()
            if not fx or fx['status_short'] not in ('FT', 'AET', 'PEN') or fx['home_goals'] is None or fx['away_goals'] is None:
                continue
            # ET-inclusive goals alone cannot settle a regulation contract.
            raw = json.loads(fx['raw_json'] or '{}')
            fulltime = (raw.get('score') or {}).get('fulltime') or {}
            if fx['status_short'] in ('AET', 'PEN') and (fulltime.get('home') is None or fulltime.get('away') is None):
                continue
            gh, ga = reg_score(raw, fx['home_goals'], fx['away_goals'])
            available = result_availability(conn, fid, gh, ga)
            times = [p['entry']['decision_at'] for p in legs.values()]
            exit_times = [p['exit']['decision_at'] for p in legs.values() if p['exit']]
            if not available or dt(available) > clock or dt(available) < max(map(dt, times + exit_times)):
                continue
            result = 'home' if gh > ga else ('draw' if gh == ga else 'away')
            source_id = hashlib.sha256(f'{version_id}:{fid}:settled'.encode()).hexdigest()
            payload = {'source_id': source_id, 'book_version_id': version_id, 'origin': 'paper_forward',
                       'fixture_id': fid, 'settled_at': available, 'sealed': True, 'result_input': dict(fx),
                       'entry_decision_at_min': min(times, key=dt), 'entry_decision_at_max': max(times, key=dt),
                       'entry_ids': [p['entry']['decision_id'] for p in legs.values()],
                       'exit_ids': [p['exit']['decision_id'] for p in legs.values() if p['exit']],
                       'record': _render(fx, legs, result, f'{gh}-{ga}', available)}
            cur = conn.execute('INSERT OR IGNORE INTO paper_completion VALUES (?,?,?,?,?)',
                               (source_id, version_id, fid, available, _json(payload)))
            count += bool(cur.rowcount)
        return count


def completed_records(conn, version_id=None):
    if not _table_exists(conn, 'paper_completion'):
        return []
    return [json.loads(r[0]) for r in conn.execute('SELECT payload FROM paper_completion ' +
            ('WHERE book_version_id=? ' if version_id else '') + 'ORDER BY settled_at,source_id', (version_id,) if version_id else ())]
