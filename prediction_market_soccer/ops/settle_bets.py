"""Read legacy frozen picks and settle independent forward paper positions.

Historical settled_bet financial rows are preserved. The production settlement
entry point only consumes paper entries/exits saved before the observed result;
it never re-runs pricing, selection, sizing or smart-exit reconstruction.

The historical pricing helpers below remain available to explicit research code,
but are not called by settlement or the published immutable strategy book.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

_FINISHED = ("FT", "AET", "PEN")


def _pit_py(conn, cmap, issues=None):
    """Per settled match: {fid, kickoff, P=[p_home,p_draw,p_away], Y} priced by each match's
    OWN point-in-time strength. Computed ONCE and reused for every as-of calibration fit, so
    freezing N matches is O(N) strength builds, not O(N^2)."""
    from prediction_market_soccer.model.match_pricing import price_match
    from prediction_market_soccer.ops.performance_report import _pit_strength
    from prediction_market_soccer.util.pricing import reg_score

    from prediction_market_soccer.config.leagues import active as _active
    _comp_of = {c.api_football_id: c.key for c in _active()}
    _lids = tuple(_comp_of)
    rows = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, kickoff_ts, raw_json, "
        "league_id "
        "FROM fixture WHERE status_short IN ({}) AND home_goals IS NOT NULL "
        "AND league_id IN ({}) AND kickoff_ts >= datetime('now', '-60 days') "
        "ORDER BY kickoff_ts".format(",".join("?" * len(_FINISHED)), ",".join("?" * len(_lids))),
        (*_FINISHED, *_lids)).fetchall()
    out = []
    _day_cache: dict = {}   # PIT strength per (kickoff DATE, comp) — per-league models (C2)
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai) or not r["kickoff_ts"]:
            continue
        try:
            _k = (r["kickoff_ts"][:10], _comp_of.get(r["league_id"]))
            if _k not in _day_cache:
                _day_cache[_k] = _pit_strength(conn, r["kickoff_ts"], _k[1])
            mp = price_match(_day_cache[_k], hi, ai)
            gh, ga = reg_score(r["raw_json"], r["home_goals"], r["away_goals"])
            from prediction_market_soccer.util.timing_provenance import result_availability
            available = result_availability(conn, r["api_id"], gh, ga)
            prediction_at = None
            recorded_p = None
            try:
                journal = conn.execute("SELECT decision_at,payload FROM timing_decision_observation WHERE fixture_api_id=? AND track='pre' ORDER BY decision_at LIMIT 1", (r["api_id"],)).fetchone()
                if journal:
                    recorded_p = json.loads(journal["payload"]).get("raw_model")
                    prediction_at = journal["decision_at"] if recorded_p else None
            except Exception:
                pass
            out.append({"fid": r["api_id"], "kickoff": r["kickoff_ts"],
                        "result_available_at": available,
                        "prediction_observed_at": prediction_at,
                        "availability_source": "observed_result" if available else "unverified_legacy",
                        "P": ([recorded_p[s] for s in _SIDES] if recorded_p else [mp.p_home, mp.p_draw, mp.p_away]),
                        "Y": 0 if gh > ga else (1 if gh == ga else 2)})
        except Exception as exc:
            if issues is not None:
                issues.append({"code": "pit_pricing_unavailable", "fixture_id": r["api_id"]})
            print(f"[settle_bets] PIT unavailable fixture={r['api_id']} ({type(exc).__name__})")
    return out


def _pit_cal(records, as_of):
    """Forward calibration accepts only results actually observed before the decision.

    An earlier kickoff does not prove that a match finished, or that its final score
    was available. Legacy caches lacking observations are excluded, not guessed.
    """
    from prediction_market_soccer.model.probability_calibration import fit_calibration
    from prediction_market_soccer.util.timing_provenance import _dt
    eligible = [r for r in records if r.get("result_available_at") and r.get("prediction_observed_at")
                and _dt(r["prediction_observed_at"]) < _dt(r["kickoff"])
                and _dt(r["kickoff"]) < _dt(as_of)
                and _dt(r["result_available_at"]) <= _dt(as_of)]
    P = [r["P"] for r in eligible]
    Y = [r["Y"] for r in eligible]
    return fit_calibration(P, Y) if len(P) >= 3 else None


def _conf(cal):
    """Calibrated-Brier-vs-uniform confidence (drives the stake) from a PIT calibration."""
    if not cal:
        return 0.0
    ub = cal.get("uniform_brier") or (2.0 / 3.0)
    cb = cal.get("calibrated_brier")
    return max(-1.0, min(1.0, (ub - cb) / ub)) if (cb is not None and ub) else 0.0


_SIDES = ("home", "draw", "away")
_SCAN_MAX_RELMIN = 170     # wall-clock ceiling for in-game ticks (covers ET; match-clock gates)
_MAX_ENTRY_MIN = 85        # don't ENTER with < ~5' runway (the 90' 3-way settles at 90')


def _event_timelines(conn, fid, home_api, *, away_api=None, event_revision=None):
    """Reference adapter requiring an explicit complete, versioned event set."""
    from prediction_market_soccer.util.match_timeline import normalize_events
    if event_revision is None or not event_revision.get("complete") or away_api is None:
        raise ValueError("complete event revision and both team identities required")
    return normalize_events(event_revision["events"], home_api, away_api)


def _state_at(timeline, mn, dims=2):
    raise ValueError("use match_timeline.timeline_state with an explicit target/cutoff")


def _forward_inplay_entry(conn, fx_row, hi, ai, decision_at):
    """Actual observed clock/score/reds and quotes; never read post-match event history."""
    import math
    from prediction_market_soccer.config import CONFIG
    from prediction_market_soccer.model.inplay import live_match_prob
    from prediction_market_soccer.ops.performance_report import _pit_strength, _row_comp
    from prediction_market_soccer.strategy.decision_model import _clip, _kelly_fraction
    from prediction_market_soccer.util.timing_provenance import live_snapshots, current_state_matches, _dt
    cfg, risk = CONFIG.decision, CONFIG.risk
    sm = _pit_strength(conn, fx_row['kickoff_ts'], _row_comp(fx_row))
    lh, la = sm.pair_lambdas(hi, ai)
    for row in live_snapshots(conn, fx_row['api_id'], decision_at):
        mn = row.get('elapsed')
        if row['milestone'] == 'PRE' or mn is None or not (1 <= mn <= _MAX_ENTRY_MIN):
            continue
        if not current_state_matches(conn,fx_row,row,decision_at):
            continue
        sh, sa, rh, ra = (row.get(k) for k in ('home_goals','away_goals','reds_home','reds_away'))
        if sh is None or sa is None or rh is None or ra is None:
            continue
        if not (sh + sa + rh + ra):
            continue
        lp = live_match_prob(lh, la, mn, sh, sa, red_home=rh, red_away=ra)
        fair = dict(zip(_SIDES, (lp.p_home, lp.p_draw, lp.p_away)))
        best = None
        for side in _SIDES:
            venue = 'poly' if row.get(f'poly_{side}_ask') is not None else 'kalshi'
            ask = row.get(f'{venue}_{side}_ask')
            if ask is None or not 0 < ask < 1:
                continue
            edge = fair[side] - ask
            if edge >= risk.min_net_edge and (best is None or edge > best[0]):
                best = (edge, side, ask, venue)
        if best:
            edge, side, ask, venue = best
            k = _clip(cfg.conf_w_edge * math.tanh(_kelly_fraction(fair[side], ask, risk.kelly_fraction) / max(cfg.kelly_ref, 1e-6)), cfg.k_min, cfg.k_max)
            stake = _clip(cfg.base_stake_usd * (1+k-getattr(cfg,'conf_k_ref',0)), cfg.min_stake_usd, cfg.max_stake_usd)
            return {'milestone': row['milestone'], 'entry_min': mn, 'side': side,
                    'entry_cents': round(ask*100, 1), 'source': venue, 'edge': round(edge,4),
                    'fair': fair[side], 'model':fair,'stake_usd': round(stake,2), 'decision_at': decision_at,
                    'snapshot_observed_at': row['observed_at'], 'snapshot_ts': row['ts'],
                    'state': {'elapsed': mn, 'home_goals': sh, 'away_goals': sa, 'reds_home': rh, 'reds_away': ra},
                    'evidence_level': 'forward_observed_paper'}
    return None


def _inplay_entry(conn, fx_row, hi, ai, *, decision_at=None, candidate=None,
                  decision_times=None, cutoff=None):
    """Forward compatibility or explicit candidate research; no historical replay fallback.

    Research returns status entered/no_edge/unavailable/invalid. A missing model,
    quote, result or exit path is not a zero-return/hold financial record.
    """
    if decision_at is not None:
        return _forward_inplay_entry(conn, fx_row, hi, ai, decision_at)
    if candidate is None or decision_times is None or cutoff is None:
        return {"status": "unavailable", "reason": "explicit_candidate_and_decision_scope_required"}
    import math
    from prediction_market_soccer.config import CONFIG
    from prediction_market_soccer.model.inplay import live_match_prob
    from prediction_market_soccer.strategy.decision_model import _clip, _kelly_fraction
    from prediction_market_soccer.strategy.smart_exit import smart_exit_cashout
    from prediction_market_soccer.util.research_inputs import epoch
    fid = fx_row["api_id"]
    times = list(decision_times)
    if times != sorted(times, key=epoch) or len(set(map(epoch, times))) != len(times) or any(epoch(t) > epoch(cutoff) for t in times):
        return {"status": "invalid", "reason": "invalid_decision_scope"}
    if not times:
        return {"status": "unavailable", "reason": "empty_decision_scope"}
    try:
        cfg, risk = candidate.parameters()
    except ValueError as exc:
        return {"status":"unavailable","reason":str(exc)}
    for at in times:
        state = candidate.state_at(fid, at)
        features = candidate.features_at(fid, at)
        quotes = candidate.quotes_at(fid, at)
        for item in (state, features, quotes):
            if item["status"] != "ok":
                return {"status": item["status"], "reason": item["reason"], "target_at": at}
        st, model = state["data"], features["data"]
        from prediction_market_soccer.util.match_timeline import quote_state_consistent
        if not all(quote_state_consistent(q, st, at) for q in quotes["data"].values()):
            return {"status":"unavailable","reason":"quote_precedes_state_change"}
        mn = st.get("elapsed")
        if st.get("period") not in ("1H", "HT", "2H") or mn is None or not 1 <= mn <= _MAX_ENTRY_MIN:
            continue
        if not sum(st[k] for k in ("home_goals", "away_goals", "reds_home", "reds_away")):
            continue
        lambdas = model.get("base_lambdas", [model.get("lambda_home"), model.get("lambda_away")])
        if any(v is None or v <= 0 for v in lambdas):
            return {"status": "invalid", "reason": "missing_model_lambdas"}
        lp = live_match_prob(*lambdas, mn, st["home_goals"], st["away_goals"],
                             red_home=st["reds_home"], red_away=st["reds_away"])
        fair = dict(zip(_SIDES, (lp.p_home, lp.p_draw, lp.p_away)))
        best = None
        for side in _SIDES:
            q = quotes["data"][side]
            ask = q.get("ask") if candidate.manifest.get("require_observed_quotes") else q.get("price")
            if ask is None or not 0 < ask < 1:
                return {"status": "unavailable", "reason": "missing_entry_ask_or_reference"}
            edge = fair[side] - ask
            if edge >= risk.min_net_edge and (best is None or edge > best[0]):
                best = (edge, side, ask)
        if best is None:
            continue
        edge, side, ask = best
        result = candidate.result_at(fid, cutoff)
        if result["status"] != "ok" or result["data"].get("result") not in _SIDES:
            return {"status": "unavailable", "reason": result["reason"] or "result_unknown"}
        won = result["data"]["result"] == side
        entry_c = round(ask * 100, 1)
        hold = 100 - entry_c if won else -entry_c
        k = _clip(cfg.conf_w_edge * math.tanh(_kelly_fraction(fair[side], ask, risk.kelly_fraction) / max(cfg.kelly_ref, 1e-6)), cfg.k_min, cfg.k_max)
        stake = _clip(cfg.base_stake_usd * (1 + k - getattr(cfg, "conf_k_ref", 0)), cfg.min_stake_usd, cfg.max_stake_usd)
        exit_result = smart_exit_cashout(None, None, fid, side, entry_c, hi, ai, fx_row["round"], won,
                                          candidate=candidate, entry_at=at, until=cutoff, entry_min=mn)
        if exit_result["status"] not in ("exited", "held_no_trigger"):
            return {"status": exit_result["status"], "reason": exit_result["reason"], "entry_observed": True}
        sx = exit_result if exit_result["status"] == "exited" else None
        return {"status": "entered", "milestone": f"{mn}'", "entry_at": at, "entry_min": mn,
                "side": side, "entry_cents": entry_c, "source": quotes["data"][side].get("venue"),
                "selected_quote": quotes["data"][side], "edge": round(edge, 4), "won": won,
                "result": result["data"]["result"], "stake_usd": round(stake, 2),
                "hold_pnl_cents": hold, "exit": sx, "exit_status": exit_result["status"],
                "realized_pnl_cents": sx["pnl_c"] if sx else hold, "provenance": quotes["provenance"]}
    return {"status": "no_edge", "reason": None, "evaluated_targets": times}


from prediction_market_soccer.ops.maintenance_gate import writer


@writer
def freeze_settled_bets(conn=None, *, fixture_ids=None, issues=None) -> int:
    """Settle only durable forward paper entries; never reconstruct a past trade.

    The historical settled_bet table is a preserved compatibility source. New
    completions live in paper_completion and are consumed by the immutable book.
    """
    if conn is None:
        from prediction_market_soccer.ingest import store
        conn = store.init_db()
    from prediction_market_soccer.util.paper_store import settle
    return settle(conn, fixture_ids=fixture_ids)


def frozen_pick(conn, fx_row, hi, ai, quotes=None, book_row=None, *, self_heal=True):
    """Read a preserved historical pick. The old self_heal argument is ignored."""
    row = conn.execute("SELECT payload FROM settled_bet WHERE fixture_api_id=?",
                       (fx_row['api_id'],)).fetchone()
    return json.loads(row['payload']) if row is not None else None


def frozen_inplay(conn, fixture_api_id):
    """The FROZEN causal in-play entry for a settled fixture (or None if no tradable in-play
    edge / not yet frozen). Shares the settled_bet row with the pre-match bet."""
    row = conn.execute("SELECT inplay_json FROM settled_bet WHERE fixture_api_id=?",
                       (fixture_api_id,)).fetchone()
    if row is None or row["inplay_json"] is None:
        return None
    return json.loads(row["inplay_json"])


def backfill_inplay(conn=None, *, dry_run: bool = True) -> dict:
    """Legacy diagnostic only. Historical financial backfill is permanently disabled."""
    if not dry_run:
        raise ValueError('Historical paper trades are frozen; in-play backfill cannot be applied')
    if conn is None:
        from prediction_market_soccer.ingest import store
        conn = store.init_db()
    n = conn.execute("SELECT COUNT(*) FROM settled_bet WHERE inplay_json IS NULL").fetchone()[0]
    return {'missing': n, 'updated': 0, 'dry_run': True, 'frozen': True}


@writer
def main() -> None:
    import argparse

    from prediction_market_soccer.ingest import store
    ap = argparse.ArgumentParser(description="settle recorded forward paper positions")
    ap.add_argument("--backfill-inplay", action="store_true",
                    help="diagnose preserved historical rows; does not create trades")
    ap.add_argument("--apply", action="store_true", help="unsupported for frozen historical trades")
    args = ap.parse_args()
    if args.backfill_inplay and args.apply:
        ap.error("Historical paper trades are frozen; --apply is not allowed")
    conn = store.init_db()
    if args.backfill_inplay:
        res = backfill_inplay(conn, dry_run=not args.apply)
        print(f"Preserved historical rows missing in-play data: {res['missing']}; no financial rows changed")
        return
    n = freeze_settled_bets(conn)
    total = conn.execute("SELECT COUNT(*) FROM paper_completion").fetchone()[0]
    print(f"paper_completion: settled {n} newly observed match(es); {total} terminal paper fixtures")


if __name__ == "__main__":
    main()
