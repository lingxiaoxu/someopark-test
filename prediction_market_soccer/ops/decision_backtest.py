"""Explicit candidate-only decision comparison, without legacy price/model fallback.

This is a research calculation, not a paper journal or a published financial book.
Each fixture keeps its eligibility result; unavailable exits are never hold P&L.
"""
from __future__ import annotations

from prediction_market_soccer.strategy.decision_model import SideQuote, decide

_SIDES = ("home", "draw", "away")
_EXITS = ("T15", "T30", "HT", "T60", "T75", "FT")


def _pnl_hold(stake, entry_c, won):
    return stake * ((100 - entry_c) / entry_c) if won else -stake


def _pnl_exit(stake, entry_c, exit_c):
    return stake * (exit_c - entry_c) / entry_c


def _quotes_from_row(row):
    raise ValueError("legacy snapshot prices are not candidate inputs")


def _exit_cents(row, side, venue):
    """Legacy inspection adapter: same venue bid only, never ask or other venue."""
    if row is None:
        return None
    prefix = {"kalshi": "kalshi", "poly_us": "poly", "poly": "poly"}.get(venue)
    value = row[f"{prefix}_{side}_bid"] if prefix else None
    return value * 100 if value is not None else None


def backtest(conn=None, *, calib_confidence=0.25, candidate=None, decision_scope=None):
    """decision_scope: ordered [{fixture_id,pre_at,cutoff,exit_targets:{label:at}}].

    Candidate features contain an explicitly selected ``model`` probability map,
    form/calibration inputs and frozen lambdas. This function never builds today's
    prior or reads a final event table on behalf of an old decision.
    """
    if candidate is None or decision_scope is None:
        return {"status": "unavailable", "reason": "explicit_candidate_and_decision_scope_required",
                "strategies": {}, "detail": [], "eligibility": []}
    from prediction_market_soccer.strategy.smart_exit import smart_exit_cashout
    candidate.verify_method_code()
    cfg,risk=candidate.parameters()
    strategies = {k: [] for k in ("A_argmax", "B_value_flat", "C_value_sized", "D_value_smart_exit")}
    details, eligibility = [], []
    scope_ids = [r['fixture_id'] for r in decision_scope]
    if scope_ids != candidate.manifest['fixture_ids']:
        raise ValueError('decision scope must preserve the complete candidate fixture order')
    for item in decision_scope:
        fid, at, cutoff = item['fixture_id'], item['pre_at'], item['cutoff']
        feat, qr, result = candidate.features_at(fid, at), candidate.quotes_at(fid, at), candidate.result_at(fid, cutoff)
        bad = next((r for r in (feat, qr, result) if r['status'] != 'ok'), None)
        if bad:
            eligibility.append({'fixture_id': fid, 'status': bad['status'], 'reason': bad['reason']})
            continue
        v, quotes = feat['data'], qr['data']
        model = v.get('model')
        if not model or any(s not in model for s in _SIDES) or abs(sum(model.values()) - 1) > 1e-6:
            eligibility.append({'fixture_id': fid, 'status': 'invalid', 'reason': 'missing_candidate_model'})
            continue
        prices = {s: quotes[s].get('ask') if candidate.manifest.get('require_observed_quotes') else quotes[s].get('price') for s in _SIDES}
        if any(p is None or not 0 < p < 1 for p in prices.values()):
            eligibility.append({'fixture_id': fid, 'status': 'unavailable', 'reason': 'missing_candidate_ask_or_reference'})
            continue
        outcome = result['data'].get('result')
        if outcome not in _SIDES:
            eligibility.append({'fixture_id': fid, 'status': 'invalid', 'reason': 'invalid_regulation_result'})
            continue
        a_side = max(_SIDES, key=lambda s: model[s])
        a_entry = 100 * prices[a_side]
        strategies['A_argmax'].append({'fixture_id': fid, 'side': a_side, 'won': a_side == outcome,
                                      'pnl': _pnl_hold(1, a_entry, a_side == outcome), 'stake': 1})
        total = sum(prices.values())
        qs = {s: SideQuote(ask=prices[s], devig=prices[s] / total, venue=quotes[s].get('venue')) for s in _SIDES}
        d = decide(model, qs, cfg=cfg, risk=risk, calib_confidence=v.get('calib_confidence', calib_confidence), form=v.get('form'), gate_open=v.get('gate_open', True))
        if d.side is None:
            eligibility.append({'fixture_id': fid, 'status': 'no_edge', 'reason': d.reason})
            continue
        won = d.side == outcome
        for label, stake in [('B_value_flat', 1), ('C_value_sized', d.stake_usd)]:
            strategies[label].append({'fixture_id': fid, 'side': d.side, 'won': won, 'stake': stake,
                                      'pnl': _pnl_hold(stake, d.price_cents, won)})
        se = smart_exit_cashout(None, None, fid, d.side, d.price_cents, None, None, None, won,
                               candidate=candidate, entry_at=at, until=cutoff)
        if se['status'] == 'exited':
            pnl = se['pnl_c'] / d.price_cents
        elif se['status'] == 'held_no_trigger':
            pnl = _pnl_hold(1, d.price_cents, won)
        else:
            pnl = None
        if pnl is not None:
            strategies['D_value_smart_exit'].append({'fixture_id': fid, 'side': d.side, 'won': won, 'pnl': pnl,
                                                     'stake': 1, 'exited': se['status'] == 'exited'})
        eligibility.append({'fixture_id': fid, 'status': 'evaluated', 'exit_status': se['status'], 'exit_reason': se.get('reason')})
        details.append({'fixture_id': fid, 'side': d.side, 'entry_c': d.price_cents, 'stake': d.stake_usd,
                        'selected_quote': quotes[d.side], 'smart_exit': se})
    summary = {}
    for name, rows in strategies.items():
        n, stake = len(rows), sum(r['stake'] for r in rows)
        pnl = sum(r['pnl'] for r in rows)
        summary[name] = {'n': n, 'wins': sum(r['won'] for r in rows), 'pnl': pnl, 'staked': stake,
                         'win_rate': sum(r['won'] for r in rows) / n if n else None, 'roi': pnl / stake if stake else None}
    return {'status': 'evaluated', 'run_id': candidate.run_id, 'scope_id': candidate.scope_id,
            'strategies': summary, 'records': strategies, 'detail': details, 'eligibility': eligibility,
            'evidence': 'candidate_research_only'}


def _fmt(doc):
    import json
    return json.dumps(doc, ensure_ascii=False, indent=2)


def main():
    raise SystemExit('Use CandidateMarketData and a fixed decision scope; no default historical replay.')


if __name__ == '__main__':
    main()
