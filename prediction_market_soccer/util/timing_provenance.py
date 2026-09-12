"""Append-only observations for forward decisions; old market timestamps are not observations."""
from __future__ import annotations

import json
import sqlite3
import math
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def _dt(value):
    d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def record_fixture_result(conn, row, *, observed_at=None):
    """Record the time THIS process observed a final result, never its kickoff time."""
    if row.get('status_short') not in ('FT', 'AET', 'PEN') or row.get('home_goals') is None or row.get('away_goals') is None:
        return
    from prediction_market_soccer.util.pricing import reg_score
    gh, ga = reg_score(row.get('raw_json'), row['home_goals'], row['away_goals'])
    last = conn.execute('SELECT home_goals,away_goals FROM fixture_result_observation WHERE fixture_api_id=? ORDER BY observed_at DESC LIMIT 1', (row['api_id'],)).fetchone()
    if last and tuple(last) == (gh, ga):
        return
    conn.execute('INSERT OR IGNORE INTO fixture_result_observation (fixture_api_id,observed_at,home_goals,away_goals,status_short) VALUES (?,?,?,?,?)',
                 (row['api_id'], observed_at or utcnow(), gh, ga, row['status_short']))


def result_availability(conn, fid, gh, ga):
    """The latest observed version must match the current result; unknown stays unknown."""
    try:
        row = conn.execute('SELECT observed_at,home_goals,away_goals FROM fixture_result_observation WHERE fixture_api_id=? ORDER BY observed_at DESC LIMIT 1', (fid,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row and (row[1], row[2]) == (gh, ga) else None


def record_live_milestone(conn, fid, milestone, *, observed_at=None):
    """Record a legacy projection, without certifying its floats as orderbook BBO.

    Actual executable captures live in quote_receipt_v1 via paper_store.observe.
    This compatibility table has no original response or market binding.
    """
    row = conn.execute('SELECT * FROM milestone_snapshot WHERE fixture_api_id=? AND milestone=?', (fid, milestone)).fetchone()
    if row is None or row['price_source'] != 'live':
        return
    at = observed_at or utcnow()
    data = dict(row)
    legs = {k: {'source': 'legacy_live_projection', 'observed_at': at, 'market_ts': data['ts'],
                'quote_kind':'legacy_unverified','quote_time_basis':'legacy_snapshot_timestamp','execution_verified':False}
            for k, v in data.items() if v is not None and k.startswith(('kalshi_', 'poly_')) and k.endswith(('_ask', '_bid'))}
    conn.execute('INSERT OR IGNORE INTO milestone_observation (fixture_api_id,milestone,source,observed_at,snapshot_json,provenance_json) VALUES (?,?,?,?,?,?)',
                 (fid, milestone, 'live', at, json.dumps(data), json.dumps(legs)))


def record_history_milestone(conn, fid, milestone, provenance, *, observed_at=None):
    row = conn.execute('SELECT * FROM milestone_snapshot WHERE fixture_api_id=? AND milestone=?', (fid, milestone)).fetchone()
    if row is None or row['price_source'] != 'candlestick':
        return
    conn.execute('INSERT OR IGNORE INTO milestone_observation (fixture_api_id,milestone,source,observed_at,snapshot_json,provenance_json) VALUES (?,?,?,?,?,?)',
                 (fid, milestone, 'candlestick', observed_at or utcnow(), json.dumps(dict(row)), json.dumps(provenance)))


def live_snapshots(conn, fid, decision_at):
    """Read legacy snapshots with their original provenance and explicit limits.

    Time visibility alone never establishes executable BBO. The old table/rows
    are preserved; new paper decisions still require independently durable receipts.
    """
    try:
        rows = conn.execute("SELECT * FROM milestone_observation WHERE fixture_api_id=? AND source='live'", (fid,)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for row in rows:
        data = json.loads(row['snapshot_json'])
        if _dt(row['observed_at']) <= _dt(decision_at) and _dt(data['ts']) <= _dt(decision_at):
            data['observed_at'] = row['observed_at']
            data['provenance'] = json.loads(row['provenance_json'])
            data['provenance_status'] = 'legacy_unverified'
            data['execution_verified'] = False
            out.append(data)
    return sorted(out, key=lambda r: (_dt(r['ts']), r.get('elapsed') or 0))


def current_state_matches(conn, fx, snapshot, decision_at):
    """Reject a stale snapshot after an observed goal/red/clock change; never reconstruct FT."""
    if fx['status_short'] not in ('1H','2H','HT','ET','BT','P','LIVE'):
        return False
    try:
        age = (_dt(decision_at)-_dt(snapshot['observed_at'])).total_seconds()
        if not 0 <= age <= 120 or not 0 <= float(fx['elapsed'])-float(snapshot['elapsed']) <= 2:
            return False
        if any(fx[k] != snapshot[k] for k in ('home_goals','away_goals')):
            return False
        # These are the currently observed live events; minute-based settled replay is forbidden.
        reds = dict(conn.execute("SELECT team_api_id,COUNT(*) FROM fixture_event WHERE fixture_api_id=? AND type='Card' AND detail LIKE '%Red%' GROUP BY team_api_id",(fx['api_id'],)))
        return (snapshot['reds_home'],snapshot['reds_away']) == (reds.get(fx['home_api_id'],0),reds.get(fx['away_api_id'],0))
    except (KeyError, TypeError, ValueError):
        return False


def record_decision(conn, fid, track, decision, *, decision_at=None):
    at = decision_at or utcnow()
    import hashlib
    from pathlib import Path
    from dataclasses import asdict
    from prediction_market_soccer.config import CONFIG
    data = dict(decision)
    paths = [*CONFIG.paths.root.glob('model/*.py'),*CONFIG.paths.root.glob('strategy/*.py'),
             CONFIG.paths.root/'config/config.py',CONFIG.paths.root/'exec/kalshi_mirror.py',
             CONFIG.paths.root/'ops/settle_bets.py',CONFIG.paths.root/'util/timing_provenance.py',
             *CONFIG.paths.priors.glob('league_*.json')]
    paths.extend(p for p in CONFIG.paths.priors.glob('clubs_*.json') if '_pit_' not in p.name or p.name.endswith(f'_pit_{at[:10]}.json'))
    paths.extend((CONFIG.paths.root/'data/raw').glob('*fc*'))
    manifest = {str(p.relative_to(CONFIG.paths.root)) if p.is_relative_to(CONFIG.paths.root) else str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}
    times = {}
    for table,column in [('fixture','updated_at'),('fixture_stats','fetched_at'),('fixture_player_stats','fetched_at'),('match_odds','fetched_at')]:
        try: times[table] = conn.execute(f'SELECT MAX({column}) FROM {table}').fetchone()[0]
        except sqlite3.OperationalError: times[table] = None
    data.update(decision_at=at, model_parameter_snapshot=asdict(CONFIG.model), decision_parameter_snapshot=asdict(CONFIG.decision),
                version_hashes=manifest, feature_observation_times=times, feature_observation_times_complete=False)
    from prediction_market_soccer.model.pit_strength import fc_input_fingerprint
    data['fc_current_inputs_sha256'] = fc_input_fingerprint(conn)
    conn.execute('INSERT OR IGNORE INTO timing_decision_observation (fixture_api_id,track,decision_at,payload) VALUES (?,?,?,?)',
                 (fid, track, at, json.dumps(data, default=str, allow_nan=False)))


def archive_legacy_paper_render(conn, artifact_path):
    """Explicit release step: preserve published replay, never invent a historic decision."""
    import hashlib
    from pathlib import Path
    raw = Path(artifact_path).read_bytes()
    doc = json.loads(raw)
    artifact_at = doc.get('as_of')
    if not artifact_at:
        raise ValueError('Legacy artifact must have its original publication timestamp')
    rows = conn.execute('''SELECT s.fixture_api_id,f.kickoff_ts,h.canonical_team_id hi,a.canonical_team_id ai
       FROM settled_bet s JOIN fixture f ON f.api_id=s.fixture_api_id
       JOIN team_meta h ON h.api_id=f.home_api_id JOIN team_meta a ON a.api_id=f.away_api_id
       WHERE json_extract(s.payload,'$.evidence_level') IS NULL''').fetchall()
    identities = {}
    for r in rows:
        key = (r['kickoff_ts'][:10],r['hi'],r['ai'])
        if key in identities: raise ValueError(f'Ambiguous legacy fixture identity: {key}')
        identities[key] = r['fixture_api_id']
    matched = {}
    for row in doc.get('bet_log', []):
        key = (row.get('date'),row.get('home_id'),row.get('away_id'))
        fid = identities.get(key)
        if fid is not None:
            if fid in matched: raise ValueError(f'Duplicate legacy artifact fixture: {fid}')
            matched[fid] = (key,row)
    if set(matched) != set(identities.values()):
        raise ValueError('Legacy artifact does not cover every existing legacy frozen fixture')
    now, sha = utcnow(), hashlib.sha256(raw).hexdigest()
    for fid,(key,row) in matched.items():
        conn.execute('INSERT OR IGNORE INTO legacy_paper_render (fixture_api_id,identity_json,artifact_as_of,archived_at,artifact_sha256,payload) VALUES (?,?,?,?,?,?)',
                     (fid,json.dumps(key),artifact_at,now,sha,json.dumps(row)))
    return {'covered':len(matched),'artifact_as_of':artifact_at,'artifact_sha256':sha,'archived_at':now}


def response_cash(raw, *, count=None):
    """Venue response average price and average fee are dollars per filled contract."""
    if isinstance(raw, str):
        try: raw = json.loads(raw)
        except (ValueError, TypeError): return None
    if not isinstance(raw, dict):
        return None
    try:
        n = float(raw.get('fill_count') if raw.get('fill_count') is not None else (raw.get('fill_count_fp') if raw.get('fill_count_fp') is not None else (count or 0)))
    except (TypeError, ValueError): return None
    price, fee = raw.get('average_fill_price'), raw.get('average_fee_paid')
    try:
        price = float(price) if price is not None else None
        fee = float(fee) if fee is not None else None
    except (TypeError, ValueError): return None
    if not math.isfinite(n) or n < 0 or (price is not None and (not math.isfinite(price) or not 0 <= price <= 1)) or (fee is not None and (not math.isfinite(fee) or fee < 0)):
        return None
    return {'count': n, 'cash_usd': n * price if price is not None else None,
            'fee_usd': n * fee if fee is not None else None}


def fills_cash(fills):
    from decimal import Decimal, InvalidOperation
    count = cash = fees = Decimal('0')
    try:
        for fill in fills:
            n, p, f = (Decimal(str(fill[k])) for k in ('count_fp','yes_price_dollars','fee_cost'))
            if not all(x.is_finite() for x in (n,p,f)) or n < 0 or not 0 <= p <= 1 or f < 0:
                return None
            count += n; cash += n*p; fees += f
    except (KeyError, TypeError, InvalidOperation, ValueError):
        return None
    return {'count':float(count),'cash_usd':float(cash),'fee_usd':float(fees)}


def record_fills(conn, fills, *, observed_at=None):
    at = observed_at or utcnow()
    for fill in fills:
        if not fill.get('fill_id') or not fill.get('order_id') or fills_cash([fill]) is None:
            raise ValueError('Incomplete or invalid venue fill receipt')
    for fill in fills:
        existing = conn.execute('SELECT payload FROM venue_fill_observation WHERE fill_id=?',(fill['fill_id'],)).fetchone()
        if existing and json.loads(existing[0]) != fill:
            raise ValueError('Conflicting receipt for immutable venue fill')
        conn.execute('INSERT OR IGNORE INTO venue_fill_observation(fill_id,order_id,observed_at,payload) VALUES (?,?,?,?)',
                     (fill['fill_id'],fill['order_id'],at,json.dumps(fill,sort_keys=True)))
    return len(fills)


def verified_order_cash(conn, order_id, expected_count):
    if not order_id: return None
    try:
        rows = conn.execute('SELECT payload FROM venue_fill_observation WHERE order_id=?',(order_id,)).fetchall()
    except sqlite3.OperationalError:
        return None
    got = fills_cash([json.loads(r[0]) for r in rows]) if rows else None
    return got if got and abs(got['count']-expected_count)<1e-8 else None


def demo_execution_summary(conn, track=None):
    """Reconcile recorded filled responses without editing historical mirror rows."""
    rows = [dict(r) for r in conn.execute('SELECT * FROM kalshi_mirror WHERE fill_count>0')]
    if track is not None:
        rows = [r for r in rows if r['track'] == track]
    closed = [r for r in rows if r['status'] in ('settled', 'exited')]
    gross = fees = 0.0
    missing = 0
    estimated = 0
    missing_prices = 0
    details = []
    for r in closed:
        try: raw = json.loads(r.get('raw_json') or '{}')
        except (ValueError, TypeError): raw = {}
        if not isinstance(raw,dict): raw = {}
        entry = response_cash(raw.get('entry_response'), count=r['fill_count'])
        exit_ = response_cash(raw.get('exit_response'), count=r.get('exit_fill_count'))
        n = float(r['fill_count'] or 0)
        sold = float(r.get('exit_fill_count') or 0)
        exact_entry = verified_order_cash(conn, r.get('order_id'), n)
        entry_orders = raw.get('entry_order_ids')
        if isinstance(entry_orders, list) and entry_orders:
            exact_entry = None
            try:
                receipts = [json.loads(x[0]) for oid in set(entry_orders) for x in conn.execute(
                    'SELECT payload FROM venue_fill_observation WHERE order_id=?', (oid,))]
                candidate = fills_cash(receipts) if receipts else None
                if candidate and abs(candidate['count'] - n) < 1e-8:
                    exact_entry = candidate
            except sqlite3.OperationalError:
                pass
        exit_orders = raw.get('exit_order_ids') or ([r['exit_order_id']] if r.get('exit_order_id') else [])
        exact_exit = None
        if sold and exit_orders:
            try:
                receipts = [json.loads(x[0]) for oid in set(exit_orders) for x in conn.execute('SELECT payload FROM venue_fill_observation WHERE order_id=?',(oid,))]
                candidate = fills_cash(receipts) if receipts else None
                if candidate and abs(candidate['count']-sold)<1e-8: exact_exit = candidate
            except sqlite3.OperationalError: pass
        # New responses keep every partial fill; legacy rows only prove the single response.
        fills = raw.get('exit_fills')
        if isinstance(fills, list):
            parts = [response_cash(x) for x in fills]
            exit_ = None if any(x is None for x in parts) else {'count': sum(x['count'] for x in parts),
                     'cash_usd': sum(x['cash_usd'] for x in parts) if all(x['cash_usd'] is not None for x in parts) else None,
                     'fee_usd': sum(x['fee_usd'] for x in parts) if all(x['fee_usd'] is not None for x in parts) else None}
        if exact_entry: entry = exact_entry
        if exact_exit: exit_ = exact_exit
        if not exact_entry or (sold and not exact_exit): estimated += 1
        valid = entry and abs(entry['count']-n)<1e-8 and 0 <= sold <= n and entry['cash_usd'] is not None and (not sold or (exit_ and exit_['cash_usd'] is not None and abs(exit_['count'] - sold) < 1e-8))
        if not valid:
            details.append({'id': r['id'], 'gross_usd': None, 'fees_usd': None, 'net_usd': None})
            missing += 1
            missing_prices += 1
            continue
        proceeds = (exit_['cash_usd'] if sold else 0) + (max(0, n-sold) * int(bool(r.get('won'))) if r['status'] == 'settled' else 0)
        g = proceeds - entry['cash_usd']
        f = None if entry['fee_usd'] is None or (sold and exit_['fee_usd'] is None) else entry['fee_usd'] + (exit_['fee_usd'] if sold else 0)
        gross += g
        if f is None: missing += 1
        else: fees += f
        details.append({'id': r['id'], 'gross_usd': round(g, 6), 'fees_usd': round(f, 6) if f is not None else None,
                        'net_usd': round(g-f, 6) if f is not None else None})
    out = {'n_filled': len(rows), 'n_closed': len(closed), 'gross_pnl_usd': round(gross, 6) if not missing_prices else None,
            'fees_usd': round(fees, 6) if not missing else None,
            'net_pnl_usd': round(gross-fees, 6) if not missing else None,
            'fee_status': 'complete' if not missing else 'partial', 'n_missing_fees': missing,
            'fee_precision':'rounded_response_average' if estimated else 'venue_fills', 'net_is_estimate':bool(estimated),
            'source': 'recorded_venue_responses', 'rows': details}
    out['wins'] = sum(r['net_usd'] is not None and r['net_usd'] > 0 for r in details)
    out['losses'] = sum(r['net_usd'] is not None and r['net_usd'] < 0 for r in details)
    out['breakeven'] = sum(r['net_usd'] == 0 for r in details)
    if track is None:
        out['by_track'] = {t: demo_execution_summary(conn, t) for t in ('pre', 'inplay')}
    return out
