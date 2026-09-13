"""Forward paper strategy tick. This module never constructs or calls a broker.

The deployed PRE hybrid, post-event in-play entry and milestone overshoot exit
rules are evaluated when their inputs are observed, before demo execution. A
missed decision remains missed; settlement never re-runs these calculations.
"""
from __future__ import annotations

from prediction_market_soccer.ops.maintenance_gate import writer

import hashlib
import json
import math
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.util import paper_store as ps
from prediction_market_soccer.util import forward_methods as fm

_SIDES = ('home', 'draw', 'away')
_MILESTONES = (('T15', 15), ('T30', 30), ('HT', 45), ('T60', 60), ('T75', 75))
_LIVE = ('1H', '2H', 'HT', 'LIVE')  # regulation market: no new ET/penalty decisions


def _now():
    return datetime.now(timezone.utc)


def _plain(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    return value


def _implementation_hash():
    root = Path(__file__).resolve().parents[1]
    return hashlib.sha256(b''.join((root / p).read_bytes() for p in
                                  ('model/inplay.py', 'model/dixon_coles.py'))).hexdigest()


def _exit_rule():
    from prediction_market_soccer.model.inplay_constants import OVERSHOOT_MARGIN, OVERSHOOT_HEADROOM_FRAC
    return {'margin': OVERSHOOT_MARGIN, 'headroom_frac': OVERSHOOT_HEADROOM_FRAC}


def _model_options():
    from prediction_market_soccer.model import inplay as m
    # These are the existing mirror model defaults, now made explicit and frozen.
    return {'injury_time': 0.0, 'xg_home': None, 'xg_away': None, 'total_lines': [1.5, 2.5, 3.5],
            'kmax': 8, 'use_hazard': True, 'xg_full_trust_min': m.XG_FULL_TRUST_MIN,
            'residual_rho': m.RESIDUAL_DC_RHO, 'state_scaling': False}


class Strength:
    def __init__(self, conn):
        self.conn, self.cache = conn, {}

    def get(self, at, comp):
        from prediction_market_soccer.model.observed_strength import build_observed_strength, _model_identity
        epoch = fm.active_epoch(self.conn)
        key = (ps.dt(at).date().isoformat(), comp, (epoch or {}).get('epoch_id'), _model_identity())
        if key not in self.cache:
            self.cache[key] = build_observed_strength(self.conn, at, comp)
        return self.cache[key]


def _strength_inputs(sm, hi, ai):
    if not getattr(sm, 'input_manifest', None):
        raise ValueError('Forward strength requires its observed input manifest')
    return {'ratings': {hi: sm.ratings[hi], ai: sm.ratings[ai]},
            'sigma': {hi: sm.sigma.get(hi), ai: sm.sigma.get(ai)},
            'cfg': _plain(sm.cfg), 'base_mu': sm.base_mu, 'home_adv': sm.home_adv,
            'adj': {key: _plain((sm.adj or {}).get(key)) for key in (hi, ai)},
            'altdata_weights': _plain(sm._altdata_w()), 'input_manifest': _plain(getattr(sm, 'input_manifest', None))}


def _calibration_records(conn, epoch=None):
    """Only genuinely observed PRE predictions with genuinely observed outcomes."""
    out = []
    for p in ps.positions(conn):
        if p['track'] != 'pre' or not p['settlement']:
            continue
        e, terminal = p['entry'], p['settlement']
        compatible = {epoch['epoch_id'], *epoch['manifest']['compatible_epoch_ids']} if epoch else set()
        if not e.get('forward_epoch_id') or e['forward_epoch_id'] not in compatible:
            continue
        result = terminal['record']['result']
        out.append({'fid': p['fixture_api_id'], 'kickoff': e['kickoff_ts'],
                    'prediction_observed_at': e['decision_at'], 'result_available_at': terminal['settled_at'],
                    'forward_epoch_id': e['forward_epoch_id'], 'method_manifest_id': e['method_manifest']['manifest_id'],
                    'P': [e['raw_model'][s] for s in _SIDES], 'Y': _SIDES.index(result)})
    return out


def pre_decision(conn, fx, hi, ai, pre_row, records, strength, *, decision_at):
    """Same hybrid value/argmax and sizing; every actual input is retained."""
    from prediction_market_soccer.model.form_strength import form_index
    from prediction_market_soccer.model.match_pricing import is_knockout, price_match_calibrated, price_match
    from prediction_market_soccer.model.motivation import motivation_multipliers
    from prediction_market_soccer.ops.performance_report import _row_comp
    from prediction_market_soccer.ops.settle_bets import _conf, _pit_cal
    from prediction_market_soccer.strategy.decision_model import decide, SideQuote
    comp = _row_comp(fx)
    sm = strength.get(decision_at, comp)
    if not (hi in sm.ratings and ai in sm.ratings):
        return None
    view = getattr(sm, 'observed_connection', None)
    if view is None or not getattr(sm, 'input_manifest', None):
        raise ValueError('Forward pricing requires an observed model input projection')
    knockout = is_knockout(fx['round'], comp)
    cal = _pit_cal(records, decision_at)
    conf = _conf(cal)
    mh, ma, motiv = motivation_multipliers(view, {}, hi, ai, fx['round'], CONFIG.model)
    lam_mult = (mh, ma) if (mh, ma) != (1.0, 1.0) else None
    # {} explicitly disables the load-today-calibration fallback when PIT has no fit.
    mp = price_match_calibrated(sm, hi, ai, knockout=False, cal=cal or {}, lam_mult=lam_mult, host_neutral=knockout)
    raw_mp = price_match(sm, hi, ai, knockout=False, lam_mult=lam_mult, host_neutral=knockout)
    model = dict(zip(_SIDES, (mp.p_home, mp.p_draw, mp.p_away)))
    bd = view.execute("SELECT AVG(p_home) h,AVG(p_draw) d,AVG(p_away) a FROM match_odds WHERE fixture_api_id=? AND bookmaker<>'live_consensus' AND fetched_at<=?", (fx['api_id'], decision_at)).fetchone()
    total = sum(float(x or 0) for x in bd) if bd else 0
    reference = dict(zip(_SIDES, (float(x or 0) / total for x in bd))) if total else model
    model_pick = max(_SIDES, key=model.get)
    fi = form_index(view, as_of=decision_at)
    form = {'home_z': fi[hi].form_z if hi in fi else None, 'away_z': fi[ai].form_z if ai in fi else None}
    available = ps.available_quotes(conn, pre_row, decision_at)
    selected = {side: ps.choose_quote(available, side) for side in _SIDES}
    decision_quotes = {side: SideQuote(ask=q['price'], venue=q['venue']) if q else SideQuote() for side, q in selected.items()}
    d = decide(model, decision_quotes, calib_confidence=conf, form=form,
               gate_open=True, conviction_side=(motiv or {}).get('conviction_side'))
    side = d.side or model_pick
    stake = d.stake_usd if d.side else CONFIG.decision.base_stake_usd
    kind = 'value' if d.side else 'argmax'
    edge = d.net_edge if d.side else model[side] - reference[side]
    quote = selected[side]
    if not quote:
        return None
    if d.side and (quote['venue'] != d.venue or round(quote['price'] * 100, 1) != d.price_cents):
        raise ValueError('PRE decision and selected quote disagree')
    # Existing smart exit uses the kickoff PIT, un-tilted live-model lambdas.
    exit_sm = strength.get(decision_at, comp)
    lh, la = exit_sm.pair_lambdas(hi, ai, knockout=is_knockout(fx['round']))
    return {'side': side, 'stake_usd': round(float(stake), 2), 'bet_kind': kind,
            'entry_cents': round(float(quote['price']) * 100, 1), 'ledger_entry_c': round(float(quote['price']) * 100, 1),
            'ledger_venue': ps._venue(quote['venue']), 'selected_quote': quote, 'net_edge': round(float(edge), 4), 'confidence_k': d.confidence_k,
            'model': {s: round(model[s], 4) for s in _SIDES}, 'fair': model[side], 'model_pick': model_pick,
            'raw_model': dict(zip(_SIDES, (raw_mp.p_home, raw_mp.p_draw, raw_mp.p_away))),
            'entry_min': 0, 'milestone': 'PRE', 'lambda_home': lh, 'lambda_away': la,
            'model_inputs': {'strength': _strength_inputs(sm, hi, ai), 'exit_strength': _strength_inputs(exit_sm, hi, ai),
                             'pricing_lambdas': [raw_mp.lam_home, raw_mp.lam_away], 'calibration': cal,
                             'calibration_records': records, 'form': form, 'motivation': motiv,
                             'motivation_lambdas': [mh, ma], 'book_reference': reference,
                             'book_reference_source': 'observed_book' if total else 'model_fallback_no_observed_book',
                             'decision_config': _plain(CONFIG.decision), 'risk_config': _plain(CONFIG.risk)},
            'cal': cal or {}, 'stage': 'knockout' if knockout else 'group'}


def _fresh(observation, fx, at, *, pre=False, conn=None):
    try:
        for key in ('observed_at', 'ts', 'source_as_of'):
            if not 0 <= (ps.dt(at) - ps.dt(observation[key])).total_seconds() <= 120:
                return False
        if pre:
            return fx['status_short'] == 'NS' and ps.dt(at) < ps.dt(fx['kickoff_ts']) and not (fx['elapsed'] or 0)
        state = observation['state']
        if fx['status_short'] not in _LIVE or state['elapsed'] is None or ps.dt(at) < ps.dt(fx['kickoff_ts']):
            return False
        if any(state.get(k) is None for k in ('home_goals', 'away_goals', 'reds_home', 'reds_away')):
            return False
        if conn is None:
            return False
        from prediction_market_soccer.util.timing_provenance import current_state_matches
        verified = {**state, 'observed_at': observation['observed_at']}
        if not current_state_matches(conn, fx, verified, at):
            return False
        return (fx['home_goals'] == state['home_goals'] and fx['away_goals'] == state['away_goals']
                and fx['elapsed'] is not None and 0 <= fx['elapsed'] - state['elapsed'] <= 2)
    except (KeyError, TypeError, ValueError):
        return False


def _snapshot_from_live(m, doc, at):
    try:
        goals = [int(x) for x in str(m['score']).split('-')]
        reds = [int(x) for x in str(m['reds']).split('-')]
        if len(goals) != 2 or len(reds) != 2 or min(goals + reds) < 0:
            return None
        row = {'fixture_api_id': int(m['fixture_id']), 'observed_at': at, 'ts': doc['ts'],
               'source_as_of': m['source_as_of'], 'status_short': m['status'],
               'state': dict(zip(('elapsed', 'home_goals', 'away_goals', 'reds_home', 'reds_away'), [m['minute'], *goals, *reds])),
               'display_model': m.get('model'), 'quote_status': m.get('quote_status'), 'quotes': m.get('prices') or {}, 'quote_receipts':m.get('quote_receipts') or []}
        for venue, source in (('kalshi', 'kalshi'), ('poly', 'poly_us')):
            for side in _SIDES:
                for field in ('ask', 'bid'):
                    row[f'{venue}_{side}_{field}'] = (((m.get('prices') or {}).get(source) or {}).get(side) or {}).get(field)
        return row
    except (KeyError, TypeError, ValueError):
        return None


def _live_model(entry, state, *, for_exit=False, conn=None):
    from prediction_market_soccer.model.inplay import live_match_prob
    if entry.get('forward_epoch_id'):
        if conn is None:
            raise ValueError('An epoch model requires its durable manifest')
        fm.validate_entry_epoch(conn, entry)
    if entry['model_implementation_hash'] != _implementation_hash():
        raise ValueError('The running live model differs from the recorded paper model')
    lh = entry.get('exit_lambda_home', entry['lambda_home']) if for_exit else entry['lambda_home']
    la = entry.get('exit_lambda_away', entry['lambda_away']) if for_exit else entry['lambda_away']
    lp = live_match_prob(lh, la, state['elapsed'],
                         state['home_goals'], state['away_goals'], red_home=state['reds_home'],
                         red_away=state['reds_away'], **entry['model_options'])
    return dict(zip(_SIDES, (lp.p_home, lp.p_draw, lp.p_away)))


def execution_context(conn, position, now=None):
    """Latest observed state for the same immutable side, for demo safety checks."""
    at = (now.isoformat() if isinstance(now, datetime) else now) or _now().isoformat()
    fx = conn.execute('SELECT * FROM fixture WHERE api_id=?', (position['fixture_api_id'],)).fetchone()
    if not fx or position['status'] == 'settled':
        return None
    entry = position['entry']
    if entry.get('forward_epoch_id'):
        fm.validate_entry_epoch(conn, entry)
    for observation in reversed(ps.observations(conn, position['fixture_api_id'])):
        pre = position['track'] == 'pre' and not position['exit'] and fx['status_short'] == 'NS'
        if _fresh(observation, fx, at, pre=pre, conn=conn):
            if position['track'] == 'pre' and not position['exit'] and not pre:
                return None  # never place an unfilled PRE after kickoff
            fair = entry['fair'] if pre else _live_model(entry, observation['state'], for_exit=bool(position['exit']), conn=conn)[entry['side']]
            return {'fair': fair, 'state': observation['state'], 'snapshot_observed_at': observation['observed_at'],
                    'snapshot_ts': observation['ts'], 'observation': observation}
    return None


def _metadata(conn, fx, hi, ai, observation, at):
    from prediction_market_soccer.ops.performance_report import _row_comp
    names = {r['api_id']: dict(r) for r in conn.execute('SELECT * FROM team_meta WHERE api_id IN (?,?)', (fx['home_api_id'], fx['away_api_id']))}
    def label(api, cid):
        return names.get(api, {}).get('name') or cid
    return {'decision_at': at, 'kickoff_ts': fx['kickoff_ts'], 'comp': _row_comp(fx), 'market_kind': 'match',
            'home_id': hi, 'away_id': ai, 'home': label(fx['home_api_id'], hi), 'away': label(fx['away_api_id'], ai),
            'home_zh': '', 'away_zh': '', 'snapshot_observed_at': observation['observed_at'],
            'snapshot_ts': observation['ts'], 'state': observation['state'], 'input_snapshot': observation,
            'fixture_input': dict(fx), 'exit_rule': _exit_rule(), 'model_options': _model_options(), 'model_implementation_hash': _implementation_hash(),
            'evidence_level': 'forward_observed_paper'}


def _inplay_decision(conn, fx, hi, ai, observation, strength, at):
    from prediction_market_soccer.ops.performance_report import _row_comp
    from prediction_market_soccer.strategy.decision_model import _clip, _kelly_fraction
    cfg, risk = CONFIG.decision, CONFIG.risk
    state = observation['state']
    if not 1 <= state['elapsed'] <= 85 or sum(state[k] for k in ('home_goals', 'away_goals', 'reds_home', 'reds_away')) <= 0:
        return None
    sm = strength.get(at, _row_comp(fx))
    lh, la = sm.pair_lambdas(hi, ai)
    from prediction_market_soccer.model.match_pricing import is_knockout
    exit_lh, exit_la = sm.pair_lambdas(hi, ai, knockout=is_knockout(fx['round']))
    base = {'lambda_home': lh, 'lambda_away': la, 'exit_lambda_home': exit_lh, 'exit_lambda_away': exit_la, 'model_options': _model_options(), 'model_implementation_hash': _implementation_hash()}
    model = _live_model(base, state)
    available = ps.available_quotes(conn, observation, at)
    best = None
    for side in _SIDES:
        quote = ps.choose_quote(available, side, priority=True)
        if not quote:
            continue
        price, venue = quote['price'], ps._venue(quote['venue'])
        edge = model[side] - price
        if edge >= risk.min_net_edge and (best is None or edge > best['net_edge']):
            best = {'side': side, 'entry_cents': round(price * 100, 1), 'ledger_entry_c': round(price * 100, 1),
                    'ledger_venue': venue, 'selected_quote': quote, 'net_edge': edge, 'fair': model[side]}
    if best is None:
        return {'no_entry': True, 'model_evaluation_complete': True, 'model': model,
                'model_inputs': {'strength': _strength_inputs(sm, hi, ai), 'decision_config': _plain(cfg), 'risk_config': _plain(risk)}}
    kelly = _kelly_fraction(best['fair'], best['entry_cents'] / 100, risk.kelly_fraction)
    conf = _clip(cfg.conf_w_edge * math.tanh(kelly / max(cfg.kelly_ref, 1e-6)), cfg.k_min, cfg.k_max)
    stake = _clip(cfg.base_stake_usd * (1 + conf - cfg.conf_k_ref), cfg.min_stake_usd, cfg.max_stake_usd)
    return {**base, **best, 'net_edge': round(best['net_edge'], 4), 'stake_usd': round(stake, 2),
            'bet_kind': 'relative_value', 'model': model, 'raw_model': model, 'entry_min': state['elapsed'],
            'model_inputs': {'strength': _strength_inputs(sm, hi, ai), 'decision_config': _plain(cfg), 'risk_config': _plain(risk)}}


def _epoch_metadata(epoch):
    return {'forward_epoch_id': epoch['epoch_id'], 'method_manifest': epoch['manifest'],
            'model_version': epoch['model_version'], 'method_version': epoch['method_version']}


@writer
def run_cycle(conn, inplay_doc=None, *, now=None, strength=None):
    """Capture → decide → paper-exit → settle, regardless of demo configuration."""
    from prediction_market_soccer.util.frozen_strategy_store import active_version
    from prediction_market_soccer.util.strategy_ledger import StrategyLedgerUnavailable
    from prediction_market_soccer.util.timing_provenance import live_snapshots
    try:
        version = active_version(conn)
    except StrategyLedgerUnavailable:
        return {'state': 'unavailable', 'reason': 'paper_book_not_activated', 'entries': 0, 'exits': 0, 'settled': 0}
    ps.ensure(conn)
    def clock():
        value = now() if callable(now) else now
        return (value if isinstance(value, datetime) else ps.dt(value)) if value else _now()
    at = clock().isoformat()
    strength = strength or Strength(conn)
    out = {'state': 'ok', 'entries': 0, 'exits': 0, 'settled': 0, 'errors': []}
    epoch = fm.active_epoch(conn)
    try:
        if not epoch or epoch['book_version_id'] != version['version_id']:
            raise ValueError('forward_method_not_activated')
        fm.validate_manifest(epoch['manifest'], check_runtime=True)
    except ValueError as exc:
        epoch = None
        out['errors'].append('entry_method: ' + str(exc))
    def data_state(fid,track,code,state,reason):
        if epoch:
            ps.record_data_state(conn,version,epoch,fid,track,code,clock().isoformat(),state,reason)
    cmap = {r['api_id']: r['canonical_team_id'] for r in conn.execute('SELECT api_id,canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL')}
    observations = {}
    for m in (inplay_doc or {}).get('matches', []):
        observation = _snapshot_from_live(m, inplay_doc, at)
        if observation:
            observations[m['fixture_id']] = observation
    for fx in conn.execute("SELECT * FROM fixture WHERE status_short='NS' AND kickoff_ts>? AND kickoff_ts<=?", (at, (clock() + timedelta(minutes=25)).isoformat())).fetchall():
        candidates = ps.observations(conn, fx['api_id']) + live_snapshots(conn, fx['api_id'], at)
        for row in sorted(candidates, key=lambda item: ps.dt(item['observed_at']), reverse=True):
            if row.get('milestone') == 'PRE':
                observations[fx['api_id']] = {**row, 'source_as_of': row.get('source_as_of', row['observed_at']),
                    'state': {'elapsed': 0, 'home_goals': 0, 'away_goals': 0, 'reds_home': 0, 'reds_away': 0}}
                break
    persisted_observations = {}
    # ps.positions() json-parses every paper_entry payload (48 rows / 158 MB → 1.5 s measured).
    # It was called once per (fixture, milestone) pair inside the loop below, ~83 s on an
    # 11-match evening — time charged straight against the 120 s observation-freshness rule.
    # Loop-invariant except for entries this cycle commits, which are added as they happen.
    open_tracks = {(position['fixture_api_id'], position['track']) for position in ps.positions(conn)}
    # A PRE observation is staged seconds before this call and dies at 120 s, so it must not
    # queue behind the live fixtures: on 2026-09-12 the PRE leg's freshness check ran at
    # +123..190 s and every pre leg of three kickoff waves was lost. sorted() is stable, so
    # ordering within each group — and the precedence established above — is unchanged.
    for fid, observation in sorted(observations.items(),
                                   key=lambda item: 0 if item[1].get('milestone') == 'PRE' else 1):
        fx = conn.execute('SELECT * FROM fixture WHERE api_id=?', (fid,)).fetchone()
        pre = observation.get('milestone') == 'PRE'
        if not fx or not _fresh(observation, fx, clock().isoformat(), pre=pre, conn=conn):
            continue
        try:
            ps.observe(conn, fid, observation, clock=lambda: clock().isoformat())
        except Exception as exc:
            out['errors'].append(f'{fid}:observation: {type(exc).__name__}: {str(exc)[:120]}')
            continue
        # This is after both raw/receipt and availability commits, never tick-start time.
        decision_at = clock().isoformat()
        persisted_observations[fid] = observation
        if not epoch or ps.dt(observation['observed_at']) < ps.dt(epoch['activated_at']):
            continue
        hi, ai = cmap.get(fx['home_api_id']), cmap.get(fx['away_api_id'])
        if not hi or not ai:
            out['errors'].append(f'{fid}: missing_team_mapping')
            continue
        codes = ['PRE'] if pre else [code for code, minimum in _MILESTONES
            if (fx['status_short'] == 'HT' if code == 'HT' else minimum <= observation['state']['elapsed'] <= minimum + 8)]
        for code in codes:
            track = 'pre' if pre else 'inplay'
            if ps.evaluated(conn, version['version_id'], fid, track, code, epoch_id=epoch['epoch_id']) or (fid, track) in open_tracks:
                continue
            try:
                available = ps.available_quotes(conn, observation, decision_at)
                # Incomplete outcomes cannot certify a completed no-edge evaluation.
                complete = all(ps.choose_quote(available, side) for side in _SIDES)
                dec = (pre_decision(conn, fx, hi, ai, observation, _calibration_records(conn, epoch), strength, decision_at=decision_at) if pre
                       else _inplay_decision(conn, fx, hi, ai, observation, strength, decision_at))
                decided_at = clock().isoformat()
                metadata = {**_metadata(conn, fx, hi, ai, observation, decided_at), **_epoch_metadata(epoch), 'milestone': code}
                if dec and not dec.get('no_entry'):
                    out['entries'] += bool(ps.record_entry(conn, version, fid, track, {**dec, **metadata}))
                    open_tracks.add((fid, track))   # same-cycle duplicate-entry guard, preserved
                    data_state(fid,track,code,'decision_recorded','entry_committed')
                elif not pre and complete and dec and dec.get('model_evaluation_complete'):
                    # Pure calculator returned normally with complete inputs. Missing models throw.
                    ps.record_evaluation(conn, version, fid, track, code, {**dec, **metadata, 'model_evaluation_complete': True})
                    data_state(fid,track,code,'no_edge','complete_model_and_quotes')
                else:
                    data_state(fid,track,code,'waiting_data','incomplete_quotes' if not complete else 'decision_inputs_unavailable')
            except Exception as exc:
                out['errors'].append(f'{fid}:{track}: {type(exc).__name__}: {str(exc)[:120]}')
                data_state(fid,track,code,'waiting_data',type(exc).__name__+': '+str(exc)[:120])
    for position in ps.positions(conn):
        if position['status'] != 'open':
            continue
        fid, entry = position['fixture_api_id'], position['entry']
        observation = persisted_observations.get(fid)
        fx = conn.execute('SELECT * FROM fixture WHERE api_id=?', (fid,)).fetchone()
        decided_at = clock().isoformat()
        if not observation or not fx or not _fresh(observation, fx, decided_at, conn=conn):
            continue
        minute = observation['state']['elapsed']
        if not max(1, entry['entry_min']) <= minute <= 95:
            continue
        for code, minimum in _MILESTONES:
            reached = fx['status_short'] == 'HT' if code == 'HT' else minimum <= minute <= minimum + 8
            track = 'exit:' + position['track']
            if not reached or ps.evaluated(conn, position['book_version_id'], fid, track, code, epoch_id=entry.get('forward_epoch_id')):
                continue
            try:
                choices = ps.available_quotes(conn, observation, decided_at, action='sell', side=entry['side'], entry=entry)
                quote = ps.choose_quote(choices, entry['side'], action='sell')
                if not quote:
                    continue
                fair = _live_model(entry, observation['state'], for_exit=True, conn=conn)[entry['side']]
                trigger = min(entry['exit_rule']['margin'], entry['exit_rule']['headroom_frac'] * max(0.0, 1 - fair))
                fired = quote['price'] >= fair + trigger
                payload = {'decision_at': decided_at, 'sold_min': minute, 'sold_c': round(quote['price'] * 100, 1),
                    'fair_c': round(fair * 100, 1), 'trigger_c': round(trigger * 100, 1), 'state': observation['state'],
                    'snapshot_observed_at': observation['observed_at'], 'snapshot_ts': observation['ts'],
                    'input_snapshot': observation, 'selected_quote': quote, 'milestone': code}
                if fired:
                    out['exits'] += bool(ps.record_exit(conn, position, payload))
                    break
                ps.record_evaluation(conn, {'version_id':position['book_version_id']}, fid, track, code, payload, position=position)
            except Exception as exc:
                out['errors'].append(f'{fid}:exit:{position["track"]}: {type(exc).__name__}: {str(exc)[:120]}')
    if epoch:
        # Missing observations need a retryable diagnostic even when no decision
        # function could be called. This table never marks an evaluation complete.
        current_at=clock();positions=ps.positions(conn)
        known={(r['fixture_api_id'],r['track'],r['milestone']):r for r in ps.data_states(conn,epoch['epoch_id'])}
        for fx in conn.execute("SELECT * FROM fixture WHERE status_short IN ('NS','1H','HT','2H','LIVE')").fetchall():
            fid=fx['api_id'];pre=fx['status_short']=='NS'
            if pre:
                codes=['PRE'] if current_at < ps.dt(fx['kickoff_ts']) <= current_at+timedelta(minutes=25) else []
            else:
                minute=fx['elapsed']
                codes=[] if minute is None else [code for code,minimum in _MILESTONES
                    if (fx['status_short']=='HT' if code=='HT' else minimum<=minute<=minimum+8)]
            track='pre' if pre else 'inplay'
            for code in codes:
                if any(p['fixture_api_id']==fid and p['track']==track for p in positions) or ps.evaluated(conn,version['version_id'],fid,track,code,epoch_id=epoch['epoch_id']):
                    continue
                if (fid,track,code) not in known:
                    data_state(fid,track,code,'waiting_data','missing_fresh_observation')
        for item in ps.data_states(conn,epoch['epoch_id']):
            if item['state']!='waiting_data':
                continue
            fid,track,code=item['fixture_api_id'],item['track'],item['milestone']
            fx=conn.execute('SELECT * FROM fixture WHERE api_id=?',(fid,)).fetchone()
            if not fx:
                continue
            entered=any(p['fixture_api_id']==fid and p['track']==track for p in positions)
            if entered:
                data_state(fid,track,code,'decision_recorded','track_entry_committed')
                continue
            minimum=dict(_MILESTONES).get(code)
            expired=(current_at>=ps.dt(fx['kickoff_ts']) or fx['status_short']!='NS') if track=='pre' else (
                fx['status_short'] in ('FT','AET','PEN') or
                (code=='HT' and fx['status_short']=='2H') or
                (minimum is not None and fx['elapsed'] is not None and fx['elapsed']>minimum+8))
            if expired:
                data_state(fid,track,code,'missed_data','window_expired')
        out['data_states']=[{k:r[k] for k in ('fixture_api_id','track','milestone','state','reason')} for r in ps.data_states(conn,epoch['epoch_id'])]
    out['settled'] = ps.settle(conn, now=clock().isoformat())
    if out['errors']:
        out['state'] = 'degraded'
    return out
