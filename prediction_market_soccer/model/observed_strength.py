"""Strength from actually observed versions, with an immutable daily input cutoff.

Historical research with no observed source versions is unavailable. It cannot
fall back to today's FC table, priors, standings or a downloaded future cache.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
from pathlib import Path

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.util import source_history as history


class ObservedInputsUnavailable(ValueError):
    pass


def _model_identity():
    root = Path(__file__).resolve().parents[1]
    files = sorted([*root.glob('model/*.py'), *root.glob('config/*.py')])
    return history.digest({'files': [(str(f.relative_to(root)), hashlib.sha256(f.read_bytes()).hexdigest()) for f in files],
                           'config': asdict(CONFIG.model)})


def _prior(conn, comp, cutoff):
    rows = history._read_versions(conn, 'derived:prior', cutoff=cutoff, entity_key=comp)
    if not rows or not rows[0]['complete']:
        raise ObservedInputsUnavailable(f'No observed prior for {comp} before {cutoff}')
    version = rows[0]
    from prediction_market_soccer.ingest.club_prior import ClubPrior, ClubPriorSnapshot, _validate
    raw = version['payload']['prior']
    teams = tuple(ClubPrior(**{k: rec.get(k) for k in ClubPrior.__dataclass_fields__}) for rec in raw['clubs'])
    prior = ClubPriorSnapshot(prior_id=raw['prior_id'], source=raw['source'], as_of=raw['as_of'],
        is_stale=bool(raw.get('is_stale', True)), league=raw.get('league', comp), teams=teams)
    _validate(prior, validate_roster=False)
    return prior, version


def _restore(conn, record, available_at):
    from prediction_market_soccer.config.config import ModelConfig
    from prediction_market_soccer.model.strength import StrengthModel
    from prediction_market_soccer.model.altdata_adjust import TeamAdj
    frozen = record['strength']
    sm = StrengthModel(ratings=frozen['ratings'], sigma=frozen['sigma'],
        cfg=ModelConfig(**frozen['cfg']), adj={k: TeamAdj(**v) for k, v in (frozen['adj'] or {}).items()},
        comp=frozen['comp'], base_mu=frozen['base_mu'], home_adv=frozen['home_adv'])
    weights = record['altdata_weights']
    sm._altdata_w = lambda: dict(weights)
    view, view_manifest = history.project_asof(conn, record['input_manifest']['cutoff'],
        required_tables=history.MODEL_TABLES)
    if view_manifest['manifest_id'] != record['view_manifest_id']:
        view.close()
        raise ObservedInputsUnavailable('Frozen model inputs changed; refusing to silently rebuild the day')
    sm.observed_connection = view
    sm.input_manifest = {**record['input_manifest'], 'model_available_at': available_at}
    return sm


def build_observed_strength(conn, cutoff, comp):
    """First eligible observed model per UTC day and implementation/config identity.

    This explicit forward method freezes its real cutoff; it does not mislabel a
    midday capture as the midnight model. Existing financial entries are untouched.
    """
    if conn.in_transaction:
        raise ValueError('Build observed strength outside a decision write transaction')
    history.recover_committed_versions(conn)
    from prediction_market_soccer.util.forward_methods import active_epoch
    method = active_epoch(conn)
    key = {'day': history._utc(cutoff).date().isoformat(), 'comp': comp, 'model_identity': _model_identity(),
           'forward_epoch_id': method['epoch_id'] if method else None}
    existing = history._read_versions(conn, 'derived:daily_strength', cutoff=cutoff, entity_key=key)
    if existing:
        return _restore(conn, existing[0]['payload'], existing[0]['available_at'])
    # A model built after a historical cutoff cannot masquerade as already ready.
    all_existing = history._read_versions(conn, 'derived:daily_strength', entity_key=key)
    if all_existing:
        raise ObservedInputsUnavailable('Daily model was not available by this decision cutoff')
    prior, prior_version = _prior(conn, comp, cutoff)
    view, manifest = history.project_asof(conn, cutoff,
        required_tables=history.MODEL_TABLES)
    try:
        from prediction_market_soccer.model.squad_strength import build_strength_live
        sm = build_strength_live(view, prior, CONFIG.model, as_of=cutoff, xg_form=True,
                                 league=comp, strict_inputs=True)
        payload = {'strength': asdict(sm), 'altdata_weights': sm._altdata_w(),
                   'view_manifest_id': manifest['manifest_id'], 'input_manifest': {
                       **manifest, 'prior_revision_id': prior_version['revision_id'],
                       'prior_available_at': prior_version['available_at'], 'model_identity': key['model_identity'],
                       'forward_epoch_id': key['forward_epoch_id'],
                       'bucket_policy': 'first_eligible_observed_cutoff_per_utc_day'}}
        payload['strength']['host_ids'] = list(payload['strength']['host_ids'])
        # One winner establishes the day; a concurrent build must use that winner.
        conn.execute('BEGIN IMMEDIATE')
        current = history._read_versions(conn, 'derived:daily_strength', entity_key=key)
        if current:
            conn.rollback()
            view.close()
            if history._utc(current[0]['available_at']) > history._utc(cutoff):
                raise ObservedInputsUnavailable('Concurrent daily model was not available by this decision cutoff')
            return _restore(conn, current[0]['payload'], current[0]['available_at'])
        revision = history.stage_version(conn, 'derived:daily_strength', key, payload)
        conn.commit()
        history.finalize_versions(conn, [revision])
        saved = history._read_versions(conn, 'derived:daily_strength', entity_key=key)[0]
        if history._utc(saved['available_at']) > history._utc(cutoff):
            raise ObservedInputsUnavailable('Daily model prepared after this cutoff; retry with a fresh observed decision time')
        sm.observed_connection = view
        sm.input_manifest = {**payload['input_manifest'], 'model_available_at': saved['available_at']}
        weights = payload['altdata_weights']
        sm._altdata_w = lambda: dict(weights)
        return sm
    except BaseException:
        view.close()
        if conn.in_transaction:
            conn.rollback()
        raise
