"""Independent observed-input research, never a journal of historical trades.

The fixed v1 method evaluates every durably saved paper observation exactly once.
It does not certify unrecorded continuous opportunities. Missing input or terminal
coverage is an audited unavailable result, never a no-edge or a zero-return leg.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

from prediction_market_soccer.util import forward_methods as fm, paper_store as ps, source_history as sh
from prediction_market_soccer.util import forward_replay_certification as exact
from prediction_market_soccer.util.research_inputs import SourceSnapshot, _path, canonical, digest

KIND = 'soccer_observed_input_redecision_v1'
NAMES = ('input_manifest.json', 'candidate_report.json', 'completed_manifest.json')
SIDES = ('home', 'draw', 'away')
RULES = {
    'model_registry': 'soccer_strength_pre_hybrid_ip_overshoot_v1',
    'opportunities': 'every_persisted_paper_observation_once_in_durable_available_order',
    'cutoff': 'max_original_observation_quote_availability_and_first_source_daily_model_ready_with_original_freshness',
    'pre': 'first_qualified_hybrid_value_or_argmax_within_25_minutes_before_kickoff',
    'inplay': 'first_qualified_postevent_observation_in_original_milestone_windows',
    'exit': 'first_qualified_same_contract_bid_trigger_at_observed_regulation_minute_1_to_95',
    'closure': 'source_terminal_fixture_and_matching_observed_regulation_result',
    'calibration': 'all_compatible_available_source_predictions_independently_exact_verified',
    'strength': 'rebuild_current_registered_algorithm_from_source_daily_input_cutoff_and_prior',
    'parameters': 'must_equal_parameters_recorded_in_source_epoch_before_opportunity',
    'quote_universe': 'all_three_outcomes_have_captured_contract_receipts_empty_books_remain_empty',
    'max_observation_age_seconds': 120,
    'sizing': 'fractional_paper_usd_rounded_0.1_cent', 'fees': 'paper_gross_not_deducted',
    'missing': 'unavailable_no_imputed_decision_or_profit',
    'continuous_window_certified': False, 'strategy_preregistered': False,
}


class Unavailable(ValueError):
    """Missing historical evidence; the audit must retain the opportunity."""


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_method_manifest():
    runtime = fm.runtime_snapshot()
    body = {'schema_version': 1, 'kind': KIND, 'rules': RULES, 'runtime': runtime,
            'strict_oos': False, 'model_version': 'soccer_strength_redecision:' + digest(runtime)[:20]}
    body['method_version'] = KIND + ':' + digest(body)[:20]
    return {**deepcopy(body), 'manifest_id': digest(body)}


def _method(manifest):
    if canonical(manifest) != canonical(build_method_manifest()):
        raise ValueError('Unsupported method, changed implementation/parameters, or self-reported rules')
    return manifest


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _view(source, at, raw_hashes):
    view, manifest = sh.project_asof(source.conn, at, required_tables=sh.MODEL_TABLES)
    try:
        exact._manifest_sources(source, manifest, source.root_for_redecision, raw_hashes)
        return view, manifest
    except BaseException:
        view.close()
        raise


def _epoch(source, at, method):
    row = source.conn.execute('SELECT * FROM forward_method_activation WHERE activated_at<=? ORDER BY sequence DESC LIMIT 1', (at,)).fetchone()
    if not row:
        raise Unavailable('source_epoch_not_available')
    epoch = fm.get_epoch(source.conn, row['epoch_id'])
    fm.validate_manifest(epoch['manifest'])
    if sh._utc(epoch['created_at']) > sh._utc(at):
        raise ValueError('Source epoch was not created by opportunity cutoff')
    if canonical(epoch['manifest']['runtime']['parameters']) != canonical(method['runtime']['parameters']):
        raise Unavailable('source_fixed_parameters_do_not_match_supported_method')
    return {**epoch, 'activation': dict(row)}


class _Strength:
    def __init__(self, source, epoch, at, raw_hashes):
        self.source, self.epoch, self.at, self.raw_hashes = source, epoch, at, raw_hashes
        self.models, self.proofs = {}, []

    def get(self, at, comp):
        if at != self.at:
            raise ValueError('A redecision strength cannot move its input cutoff')
        if comp in self.models:
            return self.models[comp]
        from prediction_market_soccer.config import CONFIG
        from prediction_market_soccer.model.observed_strength import _prior
        from prediction_market_soccer.model.squad_strength import build_strength_live
        # The recorded daily model supplies only its actual fixed input boundary.
        # Its stored probabilities/ratings are never candidate features.
        models = [r for r in sh.versions_at(self.source.conn, 'derived:daily_strength', at)
                  if r['complete'] and isinstance(r['entity_key'], dict)
                  and r['entity_key'].get('day') == sh._utc(at).date().isoformat()
                  and r['entity_key'].get('comp') == comp
                  and r['entity_key'].get('forward_epoch_id') == self.epoch['epoch_id']]
        if len(models) != 1:
            raise Unavailable('unique_observed_daily_input_boundary_unavailable')
        model = models[0]
        exact._revision(self.source, model['revision_id'], at, self.source.root_for_redecision, self.raw_hashes)
        saved = model['payload']['input_manifest']
        if saved['forward_epoch_id'] != self.epoch['epoch_id'] or sh._utc(saved['cutoff']) > sh._utc(at):
            raise ValueError('Daily source input boundary belongs to another epoch/time')
        if canonical(model['payload']['strength']['cfg']) != canonical(asdict(CONFIG.model)):
            raise ValueError('Daily source parameters differ from their recorded epoch')
        view, manifest = _view(self.source, saved['cutoff'], self.raw_hashes)
        try:
            if manifest['manifest_id'] != model['payload']['view_manifest_id']:
                raise ValueError('Daily source input projection changed')
            prior, prior_revision = _prior(self.source.conn, comp, saved['cutoff'])
            if (prior_revision['revision_id'] != saved['prior_revision_id']
                    or prior_revision['available_at'] != saved['prior_available_at']):
                raise ValueError('Daily input prior differs from source dependency')
            prior_proof = exact._prior_dependencies(self.source, prior_revision, self.source.root_for_redecision, self.raw_hashes)
            sm = build_strength_live(view, prior, CONFIG.model, as_of=saved['cutoff'], xg_form=True,
                                     league=comp, strict_inputs=True)
            weights = deepcopy(sm._altdata_w())
            sm._altdata_w = lambda: deepcopy(weights)
            sm.observed_connection = view
            sm.input_manifest = {**manifest, 'prior_revision_id': prior_revision['revision_id'],
                'prior_available_at': prior_revision['available_at'], 'source_daily_revision_id': model['revision_id'],
                'source_daily_available_at': model['available_at'], 'forward_epoch_id': self.epoch['epoch_id'],
                'research_rebuilt': True, 'simulated_at': at}
            self.models[comp] = sm
            frozen_strength = asdict(sm)
            frozen_strength['host_ids'] = sorted(frozen_strength['host_ids'])
            self.proofs.append({'comp': comp, 'source_daily_revision_id': model['revision_id'],
                'input_manifest': deepcopy(sm.input_manifest), 'prior_proof': prior_proof,
                'rebuilt_strength_hash': digest(frozen_strength), 'altdata_weights': weights})
            return sm
        except BaseException:
            view.close()
            raise

    def close(self):
        for sm in self.models.values():
            sm.observed_connection.close()


def _terminal(source, fid, raw_hashes):
    if not _table(source.conn, 'fixture_result_observation'):
        raise Unavailable('observed_terminal_result_missing')
    from prediction_market_soccer.util.pricing import reg_score
    for r in source.conn.execute('SELECT * FROM fixture_result_observation WHERE fixture_api_id=? ORDER BY observed_at', (fid,)):
        view, manifest = _view(source, r['observed_at'], raw_hashes)
        try:
            fx = view.execute('SELECT * FROM fixture WHERE api_id=?', (fid,)).fetchone()
            if fx is None or fx['status_short'] not in ('FT', 'AET', 'PEN'):
                continue
            raw = json.loads(fx['raw_json'] or '{}')
            if fx['status_short'] in ('AET', 'PEN') and any((raw.get('score', {}).get('fulltime') or {}).get(s) is None for s in ('home', 'away')):
                raise Unavailable('regulation_result_missing')
            gh, ga = reg_score(raw, fx['home_goals'], fx['away_goals'])
            if gh is None or ga is None or (gh, ga) != (r['home_goals'], r['away_goals']):
                raise Unavailable('observed_result_differs_from_terminal_source')
            return {'fixture': dict(fx), 'at': r['observed_at'], 'result': 'home' if gh > ga else 'draw' if gh == ga else 'away',
                    'score': f'{gh}-{ga}', 'source_manifest': manifest, 'result_observation': dict(r)}
        finally:
            view.close()
    raise Unavailable('observed_terminal_source_missing')


def _target_state_boundary(source, fid, at, view, raw_hashes):
    """Prove when this fixture's current state became observable.

    Complete baselines establish an empty event set too. Later full-table
    captures or another fixture's updates cannot make an unchanged state new.
    All relevant earlier originals remain part of the proof.
    """
    kinds = ('snapshot:fixture', 'table:fixture', 'snapshot:fixture_event',
             'table:fixture_event', 'fixture_event_set')
    rows = source.conn.execute('''SELECT o.rowid AS ordinal,o.*,a.available_at
        FROM source_observation_v1 o JOIN source_availability_v1 a USING(revision_id)
        WHERE o.source IN (?,?,?,?,?)''', kinds).fetchall()
    rows = sorted((r for r in rows if sh._utc(r['available_at']) <= sh._utc(at)),
                  key=lambda r: (sh._utc(r['available_at']), r['ordinal']))
    state_keys = ('status_short', 'elapsed', 'home_goals', 'away_goals',
                  'home_api_id', 'away_api_id', 'kickoff_ts')
    fixture, events, known_fixture, known_events, boundary, refs = None, [], False, False, None, []
    def signature():
        return canonical([known_fixture, known_events,
            {k: fixture.get(k) for k in state_keys} if fixture else None,
            sorted(events, key=canonical)])
    for row in rows:
        payload, key, kind = json.loads(row['payload']), json.loads(row['entity_key']), row['source']
        relevant = kind.startswith('snapshot:')
        if kind == 'table:fixture':
            relevant = payload.get('api_id') == fid or key.get('api_id') == fid
        elif kind == 'table:fixture_event':
            relevant = payload.get('fixture_api_id') == fid or key.get('fixture_api_id') == fid
        elif kind == 'fixture_event_set':
            relevant = key == fid
        if not relevant:
            continue
        proof = exact._revision(source, row['revision_id'], at, source.root_for_redecision, raw_hashes)
        before = signature()
        if kind == 'snapshot:fixture':
            selected = [r for r in payload if r.get('api_id') == fid]
            if len(selected) > 1:
                raise ValueError('Duplicate fixture identity in source baseline')
            fixture, known_fixture = (selected[0] if selected else None), True
        elif kind == 'table:fixture':
            fixture, known_fixture = payload, True
        elif kind == 'snapshot:fixture_event':
            events, known_events = [r for r in payload if r.get('fixture_api_id') == fid], True
        elif kind == 'table:fixture_event':
            events = [r for r in events if not all(r.get(k) == v for k, v in key.items())] + [payload]
        else:
            events, known_events = payload, True
        if before != signature():
            boundary = row['available_at']
        refs.append(proof)
    expected = view.execute('SELECT * FROM fixture WHERE api_id=?', (fid,)).fetchone()
    expected_events = [dict(r) for r in view.execute('SELECT * FROM fixture_event WHERE fixture_api_id=?', (fid,))]
    if (not known_fixture or not known_events or not fixture or not expected or not boundary
            or {k: fixture.get(k) for k in state_keys} != {k: expected[k] for k in state_keys}
            or canonical(sorted(events, key=canonical)) != canonical(sorted(expected_events, key=canonical))):
        raise Unavailable('target_state_history_does_not_reproduce_asof_projection')
    return {'available_at': boundary, 'source_versions': refs}


def _outside_pre_envelope(source, row, fid, raw_hashes):
    """Exclude a provably non-opportunity before requiring market/model input.

    A PRE more than 27 minutes early cannot enter the 25-minute window within
    the original 120-second freshness limit. Verify the source clock at both
    ends; a changed/unknown kickoff keeps the case unavailable instead.
    """
    observation = json.loads(row['payload'])
    if observation.get('milestone') != 'PRE':
        return None
    if observation.get('fixture_api_id') != fid or observation.get('observed_at') != row['observed_at']:
        return None
    start = sh._utc(row['observed_at'])
    end = start + timedelta(seconds=RULES['max_observation_age_seconds'])
    clocks, fixtures = [], []
    try:
        for cutoff in (start.isoformat(), end.isoformat()):
            view, manifest = sh.project_asof(source.conn, cutoff, tables=('fixture',), required_tables=('fixture',))
            try:
                exact._manifest_sources(source, manifest, source.root_for_redecision, raw_hashes)
                fx = view.execute('SELECT * FROM fixture WHERE api_id=?', (fid,)).fetchone()
                if not fx or fx['status_short'] != 'NS' or (fx['elapsed'] or 0):
                    return None
                fixtures.append(dict(fx)); clocks.append(manifest)
            finally:
                view.close()
        if fixtures[0]['kickoff_ts'] != fixtures[1]['kickoff_ts']:
            return None
        clock = {k: fixtures[0][k] for k in ('kickoff_ts', 'status_short', 'elapsed')}
        # Equal endpoints do not exclude an intervening kickoff/status revision
        # that was later reverted. Every actual target clock revision matters.
        changes = source.conn.execute('''SELECT o.*,a.available_at FROM source_observation_v1 o
            JOIN source_availability_v1 a USING(revision_id)
            WHERE o.source IN ('snapshot:fixture','table:fixture')''').fetchall()
        for change in changes:
            if not start < sh._utc(change['available_at']) <= end:
                continue
            payload, key = json.loads(change['payload']), json.loads(change['entity_key'])
            if change['source'] == 'snapshot:fixture':
                selected = [r for r in payload if r.get('api_id') == fid]
                if len(selected) != 1:
                    return None
                candidate = selected[0]
            elif payload.get('api_id') == fid or key.get('api_id') == fid:
                candidate = payload
            else:
                continue
            exact._revision(source, change['revision_id'], end.isoformat(), source.root_for_redecision, raw_hashes)
            if {k: candidate.get(k) for k in clock} != clock:
                return None
        if (sh._utc(fixtures[0]['kickoff_ts']) - end).total_seconds() > 25 * 60:
            return {'observed_at': row['observed_at'], 'simulated_at': None,
                'observation_hash': digest(observation), 'decision': 'outside_pre_window',
                'reason': 'outside_entire_original_freshness_envelope', 'source_clock_manifests': clocks}
    except (ValueError, KeyError, TypeError, sqlite3.Error, OSError):
        return None  # no trustworthy clock proof: ordinary unavailable handling
    return None


def _observation(source, row, fid, terminal, raw_hashes, method):
    observation = json.loads(row['payload'])
    if observation['fixture_api_id'] != fid or observation['observed_at'] != row['observed_at']:
        raise ValueError('Original paper observation identity differs from its database key')
    if not observation.get('quotes'):
        raise Unavailable('observation_has_no_original_contract_receipts')
    times, receipts, seen = [observation['observed_at']], [], set()
    for venue, values in observation['quotes'].items():
        if not isinstance(values, dict):
            raise ValueError('Malformed original quote universe')
        for side, quote in values.items():
            if side not in SIDES:
                continue
            receipt = quote.get('receipt') if isinstance(quote, dict) else None
            if not isinstance(receipt, dict):
                raise Unavailable('original_outcome_receipt_missing:' + side)
            record = source.conn.execute('''SELECT r.*,a.available_at FROM quote_receipt_v1 r
                JOIN quote_availability_v1 a USING(receipt_id) WHERE receipt_id=?''', (receipt.get('receipt_id'),)).fetchone()
            if not record:
                raise Unavailable('receipt_durable_availability_missing')
            path = source.resolve_raw_path(record['raw_ref'])
            durable = sh.resolve_receipt(source.conn, record['receipt_id'], terminal['at'])
            if durable is None:
                raise Unavailable('receipt_not_available_before_terminal')
            expected = {k: v for k, v in durable.items() if k not in ('raw_ref', 'available_at')}
            actual = {k: v for k, v in receipt.items() if k not in ('raw_ref', 'available_at', 'persisted_available_at')}
            exact._equal(expected, actual, 'Observation differs from its original durable receipt')
            binding = durable['binding']
            if binding['side'] != side or ps._venue(durable['provider']) != ps._venue(venue):
                raise ValueError('Receipt provider/side differs from source quote universe')
            raw_hashes[str(path)] = _sha(path)
            times.append(durable['available_at']); receipts.append(durable); seen.add(side)
    if seen != set(SIDES):
        raise Unavailable('complete_observed_outcome_universe_missing')
    at = max(times, key=sh._utc)
    initial_epoch = _epoch(source, at, method)
    comps = {r['binding']['comp'] for r in receipts}
    if len(comps) != 1:
        raise ValueError('Observation mixes competition identities')
    daily = [r for r in sh._read_versions(source.conn, 'derived:daily_strength')
             if r['complete'] and isinstance(r['entity_key'], dict)
             and r['entity_key'].get('comp') == next(iter(comps))
             and r['entity_key'].get('day') == sh._utc(observation['observed_at']).date().isoformat()
             and r['entity_key'].get('forward_epoch_id') == initial_epoch['epoch_id']]
    if len(daily) != 1:
        raise Unavailable('first_source_daily_model_ready_missing')
    at = max((at, daily[0]['available_at']), key=sh._utc)
    if _epoch(source, at, method)['epoch_id'] != initial_epoch['epoch_id']:
        raise Unavailable('observation_crossed_source_method_activation')
    if sh._utc(at) >= sh._utc(terminal['at']):
        raise Unavailable('opportunity_not_before_terminal')
    view, manifest = _view(source, at, raw_hashes)
    try:
        from prediction_market_soccer.ops.paper_trading import _fresh, _metadata
        fx = view.execute('SELECT * FROM fixture WHERE api_id=?', (fid,)).fetchone()
        pre = observation.get('milestone') == 'PRE'
        if not fx or not _fresh(observation, fx, at, pre=pre, conn=view):
            raise Unavailable('observation_not_contemporaneous_with_source_fixture')
        identities = {r['api_id']: r['canonical_team_id'] for r in view.execute(
            'SELECT api_id,canonical_team_id FROM team_meta WHERE api_id IN (?,?)', (fx['home_api_id'], fx['away_api_id']))}
        hi, ai = identities.get(fx['home_api_id']), identities.get(fx['away_api_id'])
        if not hi or not ai or hi == ai:
            raise Unavailable('source_canonical_identity_missing_or_conflicting')
        metadata = _metadata(view, fx, hi, ai, observation, at)
        state_boundary = None if pre else _target_state_boundary(source, fid, at, view, raw_hashes)
        for receipt in receipts:
            b = receipt['binding']
            expected = {'fixture_api_id': fid, 'home_api_id': fx['home_api_id'], 'away_api_id': fx['away_api_id'],
                        'home_id': hi, 'away_id': ai, 'comp': metadata['comp'], 'market_kind': 'match', 'settlement_scope': 'regulation'}
            if any(b.get(k) != v for k, v in expected.items()) or sh._utc(b['kickoff_ts']) != sh._utc(fx['kickoff_ts']):
                raise ValueError('Source receipt does not bind the independently observed fixture')
            if b.get('environment') != {'kalshi': 'public', 'poly_us': 'us'}.get(receipt['provider']):
                raise Unavailable('unsupported_observed_provider_environment')
            if not pre and (sh._utc(receipt['request_started_at']) < sh._utc(state_boundary['available_at']) or
                    receipt.get('provider_quote_at') and sh._utc(receipt['provider_quote_at']) < sh._utc(state_boundary['available_at'])):
                raise Unavailable('quote_precedes_independent_state_revision')
        return observation, dict(fx), metadata, {'observed_at': row['observed_at'], 'simulated_at': at,
            'observation_hash': digest(observation), 'source_manifest': manifest,
            'state_boundary': state_boundary,
            'receipt_ids': [r['receipt_id'] for r in receipts]}
    finally:
        view.close()


def _calibration(source, epoch, at, fid, raw_hashes):
    from prediction_market_soccer.ops.paper_trading import _calibration_records
    pool = [r for r in _calibration_records(source.conn, epoch) if sh._utc(r['result_available_at']) <= sh._utc(at)]
    for record in pool:
        if record['fid'] == fid:
            raise ValueError('Current fixture result entered its own calibration pool')
        # v1 supports source calibration only when its full exact proof exists.
        # Never silently substitute an empty pool when source predictions exist.
        proof = exact._audit(source, source.root_for_redecision, [record['fid']], ('pre', 'inplay'))
        raw_hashes.update(proof['raw_hashes'])
    return pool


def _render(records):
    pre = ip = Decimal(0)
    for row in records:
        pre += Decimal(str(row.get('realized_pnl_cents') or 0))
        ip += Decimal(str(row.get('inplay_pnl_cents') or 0))
        row.update(pre_cum_pnl_cents=float(pre), realized_cum_pnl_cents=float(pre),
                   inplay_cum_pnl_cents=float(ip), combined_cum_pnl_cents=float(pre + ip),
                   combined_pnl_cents=float(Decimal(str(row.get('realized_pnl_cents') or 0)) + Decimal(str(row.get('inplay_pnl_cents') or 0))))
    return records


def _audit(source, root, fixture_ids, tracks, method):
    from prediction_market_soccer.ops import paper_trading as pt
    if not isinstance(source, SourceSnapshot):
        raise TypeError('An explicit immutable SourceSnapshot is required')
    if not fixture_ids or len(set(fixture_ids)) != len(fixture_ids) or any(type(fid) is not int for fid in fixture_ids):
        raise ValueError('A nonempty unique ordered fixture scope is required')
    if not tracks or len(set(tracks)) != len(tracks) or any(t not in ('pre', 'inplay') for t in tracks):
        raise ValueError('Explicit unique PRE/INPLAY tracks required')
    _method(method); source.assert_unchanged()
    source.root_for_redecision = root
    source._cert_revision_cache = {}; source._cert_model_proofs = set()
    has_quotes = (_table(source.conn, 'quote_receipt_v1') and _table(source.conn, 'quote_availability_v1')
        and source.conn.execute('''SELECT 1 FROM quote_receipt_v1
            JOIN quote_availability_v1 USING(receipt_id) LIMIT 1''').fetchone() is not None)
    records, eligibility, proofs, raw_hashes, marks = [], [], [], {}, {}
    for fid in fixture_ids:
        legs = {}; outcomes = {t: {'fixture_id': fid, 'track': t, 'status': 'unavailable', 'valuation_status': 'unavailable',
                                  'reason': 'no_persisted_opportunity', 'observations': []} for t in tracks}
        marks[str(fid)] = []
        try:
            if not has_quotes:
                raise Unavailable('missing_durable_quote_receipts')
            terminal = _terminal(source, fid, raw_hashes)
            rows = list(source.conn.execute('SELECT * FROM paper_observation WHERE fixture_api_id=? AND observed_at<? ORDER BY observed_at', (fid, terminal['at'])))
            opportunities, bad = [], []
            saved_observations = {r['observed_at']: json.loads(r['payload']) for r in rows}
            # The fixed observation protocol cannot silently omit an observation
            # whose existence is independently recorded by an original decision.
            for position in ps.positions(source.conn):
                if position['fixture_api_id'] != fid:
                    continue
                for label, original in [('entry', position['entry']), ('exit', position['exit'])]:
                    if not original:
                        continue
                    snapshot = original.get('input_snapshot') or {}
                    stamp = snapshot.get('observed_at')
                    if stamp and sh._utc(stamp) < sh._utc(terminal['at']) and saved_observations.get(stamp) != snapshot:
                        bad.append({'observed_at': stamp, 'pre': label == 'entry' and position['track'] == 'pre',
                                    'reason': 'source_decision_references_missing_or_conflicting_observation'})
            for row in rows:
                try:
                    outside = _outside_pre_envelope(source, row, fid, raw_hashes)
                    if outside is not None:
                        if 'pre' in outcomes:
                            outcomes['pre']['observations'].append(outside)
                        continue
                    opportunities.append(_observation(source, row, fid, terminal, raw_hashes, method))
                except (Unavailable, ValueError, KeyError, TypeError, sqlite3.Error, OSError) as exc:
                    raw = json.loads(row['payload'])
                    bad.append({'observed_at': row['observed_at'], 'pre': raw.get('milestone') == 'PRE',
                                'reason': type(exc).__name__ + ': ' + str(exc)})
            opportunities.sort(key=lambda item: (sh._utc(item[3]['simulated_at']), item[3]['observation_hash']))
            states = {t: {'seen': 0, 'edge_evaluations': 0, 'missing': [], 'last': None} for t in tracks}
            for observation, fx, metadata, proof in opportunities:
                at = proof['simulated_at']; pre = observation.get('milestone') == 'PRE'
                eligible_marks = ps.available_quotes(source.conn, observation, at)
                minute = observation['state']['elapsed']
                rendered = {'milestone': 'PRE' if pre else 'HT' if fx['status_short'] == 'HT' else 'T' + str(minute),
                    'minute': minute, 'score': f"{observation['state']['home_goals']}-{observation['state']['away_goals']}",
                    'target_at': at, 'poly_c': {}, 'kalshi_c': {}, 'devig': None, 'model': None,
                    'provenance': {'source': KIND, 'observed_at': observation['observed_at'], 'simulated_at': at,
                                   'observation_hash': proof['observation_hash'], 'receipt_ids': proof['receipt_ids']}}
                for venue, side_quotes in eligible_marks.items():
                    field = 'kalshi_c' if venue == 'kalshi' else 'poly_c'
                    for side, selected in side_quotes.items():
                        rendered[field][side] = round(selected['price'] * 100, 1)
                mark = {'observed_at': observation['observed_at'], 'target_at': at, 'state': observation['state'],
                        'observation_hash': proof['observation_hash'], 'receipt_ids': proof['receipt_ids'],
                        'quotes': deepcopy(observation['quotes']), 'source_manifest': proof['source_manifest'], 'rendered': rendered}
                marks[str(fid)].append(mark)
                epoch = None
                for t in tracks:
                    state = states[t]
                    if t in legs:
                        pos = legs[t]
                        if pre or pos['exit'] or sh._utc(at) <= sh._utc(pos['entry']['decision_at']):
                            continue
                        minute = observation['state']['elapsed']
                        if not max(1, pos['entry']['entry_min']) <= minute <= 95:
                            continue
                        try:
                            choices = ps.available_quotes(source.conn, observation, at, action='sell', side=pos['entry']['side'], entry=pos['entry'])
                            quote = ps.choose_quote(choices, pos['entry']['side'], action='sell')
                            if not quote:
                                raise Unavailable('same_contract_exit_bid_unavailable')
                            fair = pt._live_model(pos['entry'], observation['state'], for_exit=True)[pos['entry']['side']]
                            trigger = min(pos['entry']['exit_rule']['margin'], pos['entry']['exit_rule']['headroom_frac'] * max(0., 1 - fair))
                            event = {**proof, 'decision': 'hold', 'selected_quote': quote, 'fair': fair, 'trigger': trigger}
                            if quote['price'] >= fair + trigger:
                                pos['exit'] = {**metadata, 'entry_id': pos['entry']['decision_id'], 'side': pos['entry']['side'],
                                    'decision_id': digest([KIND, pos['entry']['decision_id'], at, 'exit']),
                                    'sold_min': minute, 'sold_c': round(100 * quote['price'], 1), 'fair_c': round(100 * fair, 1),
                                    'trigger_c': round(100 * trigger, 1), 'selected_quote': quote, 'research_redecision': True}
                                event['decision'] = 'exit'
                            outcomes[t]['observations'].append(event)
                            state['last'] = at
                        except (Unavailable, ValueError, KeyError, TypeError) as exc:
                            state['missing'].append({'simulated_at': at, 'reason': str(exc), 'after_entry': True})
                        continue
                    if pre != (t == 'pre'):
                        continue
                    if pre and not 0 < (sh._utc(fx['kickoff_ts']) - sh._utc(at)).total_seconds() <= 25 * 60:
                        outcomes[t]['observations'].append({**proof, 'decision': 'outside_pre_window'})
                        continue
                    codes = ['PRE'] if pre else [code for code, minimum in pt._MILESTONES
                        if (fx['status_short'] == 'HT' if code == 'HT' else minimum <= observation['state']['elapsed'] <= minimum + 8)]
                    if not codes:
                        continue
                    state['seen'] += 1
                    strength = None
                    try:
                        epoch = epoch or _epoch(source, at, method)
                        strength = _Strength(source, epoch, at, raw_hashes)
                        if pre:
                            decision = pt.pre_decision(source.conn, fx, metadata['home_id'], metadata['away_id'], observation,
                                _calibration(source, epoch, at, fid, raw_hashes), strength, decision_at=at)
                        else:
                            decision = pt._inplay_decision(source.conn, fx, metadata['home_id'], metadata['away_id'], observation, strength, at)
                        event = {**proof, 'model_proofs': deepcopy(strength.proofs), 'decision': 'condition_not_triggered'}
                        if decision and not decision.get('no_entry'):
                            entry = {**decision, **metadata, 'milestone': codes[0], 'book_version_id': KIND,
                                'model_version': method['model_version'], 'method_version': method['method_version'],
                                'forward_epoch_id': None, 'source_epoch_id': epoch['epoch_id'],
                                'method_manifest': method, 'decision_id': digest([KIND, fid, t, at]), 'research_redecision': True}
                            legs[t] = {'entry': entry, 'exit': None}
                            event.update(decision='entered', selected_quote=entry['selected_quote'], model=entry['model'])
                        elif decision and decision.get('model_evaluation_complete'):
                            choices = ps.available_quotes(source.conn, observation, at)
                            if not all(ps.choose_quote(choices, s) for s in SIDES):
                                raise Unavailable('incomplete_quotes_cannot_prove_no_edge')
                            state['edge_evaluations'] += 1
                            event.update(decision='no_edge', model=decision['model'])
                        elif pre:
                            raise Unavailable('pre_selected_offer_or_model_missing')
                        outcomes[t]['observations'].append(event)
                    except (Unavailable, ValueError, KeyError, TypeError, sqlite3.Error, OSError) as exc:
                        state['missing'].append({'simulated_at': at, 'reason': type(exc).__name__ + ': ' + str(exc)})
                    finally:
                        if strength:
                            strength.close()
            complete_legs = {}
            for t in tracks:
                state, out = states[t], outcomes[t]
                if t in legs:
                    pos = legs[t]
                    # Unusable live observations before the first exit can hide a
                    # different exit. Data after a proven exit cannot affect it.
                    until = (pos['exit'] or {}).get('decision_at', terminal['at'])
                    for failure in bad:
                        before_entry = sh._utc(failure['observed_at']) <= sh._utc(pos['entry']['decision_at'])
                        affects_entry = before_entry and failure['pre'] == (t == 'pre')
                        affects_exit = not failure['pre'] and not before_entry and sh._utc(failure['observed_at']) < sh._utc(until)
                        if affects_entry or affects_exit:
                            state['missing'].append({**failure, 'after_entry': True})
                    if not pos['exit'] and state['last'] is None:
                        state['missing'].append({'reason': 'no_observed_exit_path_after_entry'})
                    out.update(status='entered', entry=pos['entry'], exit=pos['exit'])
                else:
                    state['missing'].extend(f for f in bad if f['pre'] == (t == 'pre'))
                if state['missing']:
                    out.update(valuation_status='unavailable', reason='incomplete_source_opportunities', missing=state['missing'])
                elif t in legs:
                    out.update(valuation_status='complete', reason=None); complete_legs[t] = legs[t]
                elif state['seen']:
                    out.update(status='no_edge' if state['edge_evaluations'] else 'condition_not_triggered',
                               valuation_status='not_applicable', reason=None)
                out['result_proof'] = terminal
            if complete_legs:
                record = ps._render(terminal['fixture'], complete_legs, terminal['result'], terminal['score'], terminal['at'])
                record.update(evidence_level='observed_input_hypothetical_redecision', pit_status='observed_opportunities_only',
                              research_redecision=True, strict_oos=False)
                records.append(record)
        except (Unavailable, ValueError, KeyError, TypeError, sqlite3.Error, OSError) as exc:
            for out in outcomes.values():
                out.update(reason=type(exc).__name__ + ': ' + str(exc))
        for out in outcomes.values():
            eligibility.append({k: out[k] for k in ('fixture_id', 'track', 'status', 'valuation_status', 'reason')})
            proofs.append(out)
    for path, sha in raw_hashes.items():
        if _sha(path) != sha:
            raise ValueError('Original raw object changed during redecision')
    source.assert_unchanged()
    return {'records': _render(records), 'eligibility': eligibility, 'tracks': proofs, 'raw_hashes': raw_hashes, 'marks': marks}


def _documents(audit, reference, method, completed_at):
    from prediction_market_soccer.util.strategy_ledger import build_strategy_ledger
    inputs = {'schema_version': 1, 'analysis_purpose': KIND, 'candidate_input': reference,
              'fixture_ids': reference['fixture_ids'], 'tracks': reference['tracks'], 'scope_id': reference['scope_id'],
              'method_manifest': method, 'model_version': method['model_version'], 'method_version': method['method_version']}
    causal = all(r['valuation_status'] in ('complete', 'not_applicable') for r in audit['eligibility'])
    as_of = max((r['settlement_observed_at'] for r in audit['records']), default=None, key=sh._utc) or completed_at
    report = {'as_of': as_of, 'bet_log': audit['records'], 'eligibility': audit['eligibility'],
              'redecision_marks': audit['marks'], 'pnl_basis': 'observed_input_hypothetical_paper_gross',
              'strict_oos': False, 'computed_at': completed_at, 'input_manifest_hash': digest(inputs)}
    report['strategy_ledger'] = build_strategy_ledger(report)
    counts = {k: sum(r['status'] == k for r in audit['eligibility']) for k in ('entered', 'no_edge', 'condition_not_triggered', 'unavailable', 'invalid')}
    financial = causal and all(r['status'] in ('entered', 'no_edge') for r in audit['eligibility'])
    manifest = {'status': 'completed', 'certification_kind': KIND, 'run_id': reference['run_id'], 'completed_at': completed_at,
        'model_version': method['model_version'], 'method_version': method['method_version'], 'scope_hash': digest(reference['fixture_ids']),
        'scope_total': len(audit['eligibility']), 'eligibility': audit['eligibility'], 'counts': counts,
        'audit_complete': True, 'input_causal_complete': causal, 'financial_complete': financial,
        'activation_eligible': financial and bool(audit['records']), 'strict_pit_certified': causal, 'strict_oos': False,
        'certification_scope': 'persisted_observed_opportunities_only_not_continuous_windows',
        'historical_execution_certified': False, 'new_model_redecision': True,
        'fee_status': 'not_deducted', 'ledger_id': report['strategy_ledger']['ledger_id'],
        'ledger_hash': digest(report['strategy_ledger']), 'input_manifest_hash': digest(inputs)}
    return dict(zip(NAMES, (inputs, report, manifest)))


def build_redecision_bundle(source, *, root, fixture_ids, run_id, method_manifest, output_dir, tracks=('pre', 'inplay')):
    root = Path(root).resolve(strict=True); directory = _path(Path(output_dir), root)
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError('Explicit nonempty run_id required')
    fixture_ids, tracks = list(fixture_ids), list(tracks)
    audit = _audit(source, root, fixture_ids, tracks, method_manifest)
    ref = {'kind': KIND, 'path': str(source.path), 'root': str(root), 'sha256': source.manifest['sha256'],
           'run_id': run_id, 'fixture_ids': fixture_ids, 'tracks': tracks, 'raw_archive': source.raw_archive,
           'scope_id': digest({'fixture_ids': fixture_ids, 'tracks': tracks})}
    docs = _documents(audit, ref, method_manifest, datetime.now(timezone.utc).isoformat())
    encoded = {name: json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False).encode() for name, doc in docs.items()}
    cert = {'schema_version': 1, 'kind': KIND, 'source': ref, 'method_manifest': method_manifest,
            'proofs': audit['tracks'], 'raw_hashes': audit['raw_hashes'],
            'documents': {name: hashlib.sha256(body).hexdigest() for name, body in encoded.items()}}
    cert['certification_id'] = digest(cert)
    directory.mkdir(parents=True, exist_ok=False)
    for name, body in encoded.items():
        with (directory / name).open('xb') as stream:
            stream.write(body)
    with (directory / 'certification.json').open('x', encoding='utf-8') as stream:
        json.dump(cert, stream, ensure_ascii=False, indent=2, allow_nan=False)
    validate_redecision_bundle(directory, root=root)
    return docs


def validate_redecision_bundle(bundle, *, root):
    root = Path(root).resolve(strict=True); directory = _path(Path(bundle), root, exists=True)
    docs = {name: json.loads((directory / name).read_text()) for name in NAMES}
    cert = json.loads((directory / 'certification.json').read_text())
    if cert.get('kind') != KIND or cert.get('certification_id') != digest({k: v for k, v in cert.items() if k != 'certification_id'}):
        raise ValueError('Certification checksum/kind mismatch')
    if any(cert['documents'].get(name) != _sha(directory / name) for name in NAMES):
        raise ValueError('Certified documents changed')
    inputs = docs[NAMES[0]]; ref = inputs['candidate_input']; method = _method(inputs['method_manifest'])
    if (ref != cert['source'] or ref.get('kind') != KIND or ref.get('root') != str(root)
            or ref.get('fixture_ids') != inputs['fixture_ids'] or ref.get('tracks') != inputs['tracks']
            or not isinstance(ref.get('run_id'), str) or not ref['run_id'].strip()
            or ref['scope_id'] != digest({'fixture_ids': inputs['fixture_ids'], 'tracks': inputs['tracks']})):
        raise ValueError('Actual source, scope or run identity mismatch')
    with SourceSnapshot(Path(ref['path']), root=root, raw_archive=ref.get('raw_archive')) as source:
        if source.manifest['sha256'] != ref['sha256']:
            raise ValueError('Certified source snapshot changed')
        audit = _audit(source, root, inputs['fixture_ids'], inputs['tracks'], method)
        expected = _documents(audit, ref, method, docs[NAMES[2]]['completed_at'])
        exact._equal(docs, expected, 'Candidate differs from independently rebuilt source opportunities')
        exact._equal(cert['proofs'], audit['tracks'], 'Rebuilt opportunity proof differs')
        exact._equal(cert['raw_hashes'], audit['raw_hashes'], 'Original raw proof differs')
        exact._equal(cert['method_manifest'], method, 'Certificate method differs')
    return docs
