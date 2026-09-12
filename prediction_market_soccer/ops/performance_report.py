"""Reports over the versioned immutable smart-timing strategy book.

Published history is stored as complete rendered records. Ordinary refreshes append
sealed forward-paper results and never reconstruct old entries, exits, or P&L.
Research pricing helpers remain available to explicit research callers only.
"""
from __future__ import annotations

from prediction_market_soccer.ops.maintenance_gate import writer

import json
from dataclasses import asdict, dataclass, field

import numpy as np

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.model.calibrate import brier_score, log_loss

_FINISHED = ("FT", "AET", "PEN")


@dataclass
class PerformanceReport:
    n_settled: int
    brier: float                  # RAW model Brier (pre-calibration)
    brier_uniform: float
    calibrated_brier: float | None  # post-calibration Brier (None if no calibration fit)
    trade_grade: bool             # gate verdict: calibrated Brier <= uniform
    log_loss: float
    favourite_hit_rate: float
    calibration_pnl: float        # paper, 1u on model pick at fair odds
    calibration_pnl_per_bet: float
    settled_signal_pnl: float     # realized P&L of recorded signals (finished matches)
    n_settled_signals: int
    # Production bet log: flat 1u on our model's best value side vs the closing book,
    # every match since the opener. Running record of what we predicted, bet, and won.
    bet_log: list                 # list of per-match dicts (date/match/pick/odds/result/pnl/cum)
    pnl_units: float              # cumulative P&L in units
    pnl_record: str               # e.g. "8W-8L"
    pnl_roi: float                # cumulative P&L / total staked
    bet_since: str                # date of the first match bet
    notes: list[str]
    notes_i18n: list = field(default_factory=list)   # {key, args} per note, for 5-language frontend
    # Per-contract ¢ view (plan 18 §2.7): real Kalshi/Poly contract economics —
    # buy 1 contract at entry ¢, settle 100¢ (won) / 0¢ (lost).
    pnl_cents_total: float = 0.0  # cumulative per-contract P&L in ¢
    avg_entry_cents: float = 0.0  # average price we entered at (¢)
    cents_capture_rate: float = 0.0  # captured ¢ / theoretically available ¢
    avg_clv_cents: float = 0.0    # mean closing-line value (T75 ¢ − entry ¢), result-independent
    # Decision model (plan 20): value/Kelly pick + confidence sizing.
    model_pred_accuracy: float = 0.0  # argmax prediction hit-rate (model-quality reference)
    n_decision_bets: int = 0          # matches the decision model actually bet
    n_skipped: int = 0                # settled matches skipped (no tradable edge)
    decision_staked_usd: float = 0.0  # total $ staked across the decision bets
    # Argmax 口径 (parallel reference, every settled match): bet the most-likely side.
    argmax_record: str = ""           # W-L over the PRICED matches (the ¢ P&L's own set)
    argmax_accuracy_record: str = ""  # W-L over every resolvable prediction (model quality)
    argmax_priced_n: int = 0          # how many of the settled matches carried an entry price
    argmax_pnl_cents_total: float = 0.0  # ¢ P&L betting argmax every match
    # REALISED 口径 — the actual strategy is decision + smart-exit cash-out (not hold-to-FT).
    realized_record: str = ""            # profitable-bets record with cash-out, e.g. "12-6"
    realized_pnl_cents_total: float = 0.0  # ¢ P&L with the smart-exit applied
    n_smart_sold: int = 0                # bets where the smart-exit cashed out the over-reaction
    hold_record: str = ""                # the hold-to-FT W-L (reference)
    hold_pnl_cents_total: float = 0.0    # hold-to-FT ¢ P&L (reference)
    # IN-PLAY ENTRY 口径 — the causal relative-value entry (frozen), a SECOND P&L stream that
    # trades matches the pre-match model skipped. Combined = smart-exit realised + in-play.
    inplay_record: str = ""              # in-play entry W-L
    inplay_pnl_cents_total: float = 0.0  # ¢ P&L from the in-play entries (flat $1)
    n_inplay: int = 0                    # matches with a tradable in-play entry
    combined_pnl_cents_total: float = 0.0  # smart-exit realised + in-play entry (the one cum)
    # PER-COMPETITION segmentation (C-20): one row per registry competition — the same
    # five tracks, plus that competition's §3.5 calibration-gate state. Every ENABLED
    # competition appears even with zero settled matches, so "we have no record here yet"
    # and "this competition is losing" can never look like the same thing.
    by_league: list = field(default_factory=list)
    as_of: str | None = None
    source_as_of: str | None = None
    data_status: dict = field(default_factory=dict)
    pnl_basis: str = "paper_replay_gross_position_before_fees"
    evidence_summary: dict = field(default_factory=dict)
    demo_execution: dict = field(default_factory=dict)
    strategy_ledger: dict = field(default_factory=dict)

    def __post_init__(self):
        from prediction_market_soccer.util.strategy_ledger import build_strategy_ledger,validate_strategy_ledger
        if self.strategy_ledger:
            validate_strategy_ledger(self.strategy_ledger)
            if self.strategy_ledger['records'] != self.bet_log:
                raise ValueError('The report and strategy ledger contain different records')
        elif self.as_of:
            self.strategy_ledger=build_strategy_ledger(self)


def _demo_coverage(conn, ledger):
    """Actual fills are an execution subset, never the denominator of model returns."""
    from collections import Counter
    model={}
    for row in ledger['records']:
        if row.get('bet'): model[(row['fixture_id'],'pre')]=row.get('pick')
        if row.get('inplay_side'): model[(row['fixture_id'],'inplay')]=row['inplay_side']
    try:
        rows=[dict(r) for r in conn.execute('SELECT fixture_api_id,track,side,status,fill_count FROM kalshi_mirror')]
    except Exception:
        return {'state':'unavailable','n_model_legs':len(model),'n_model_fixtures':len(ledger['records'])}
    filled=[r for r in rows if (r['fill_count'] or 0)>0]
    matches={(r['fixture_api_id'],r['track']) for r in filled
             if model.get((r['fixture_api_id'],r['track']))==r['side']}
    unmatched=[{'fixture_id':r['fixture_api_id'],'track':r['track'],'demo_side':r['side'],
                'model_side':model.get((r['fixture_api_id'],r['track'])),
                'reason':'side_mismatch' if (r['fixture_api_id'],r['track']) in model else 'no_model_leg'}
               for r in filled if model.get((r['fixture_api_id'],r['track']))!=r['side']]
    status_counts=dict.fromkeys(('open','pending','unfilled','error','skipped','settled','exited'),0)
    status_counts.update(Counter(r['status'] for r in rows))
    return {'state':'ok','n_model_legs':len(model),'n_model_fixtures':len(ledger['records']),
            'n_demo_rows':len(rows),'n_filled_entries':len(filled),
            'n_filled_model_legs':len(matches),'n_filled_unique_fixtures':len({r['fixture_api_id'] for r in filled}),
            'n_filled_model_unique_fixtures':len({fid for fid,track in matches}),
            'n_model_legs_without_demo_fill':len(model)-len(matches),
            'n_filled_outside_model_or_side_mismatch':len(unmatched),
            'matching_basis':'fixture_id_track_side','unmatched_filled_legs':unmatched,
            'fill_coverage_ratio':len(matches)/len(model) if model else None,
            'statuses':status_counts,
            'unfilled_counted_as_loss':False}


def _settled(conn, sm=None, issues=None):
    """RAW-model probs + outcomes for every settled match, used for the accuracy Brier.

    PIT + consistent with the bet log: each match is priced with its OWN point-in-time
    strength (``_pit_strength`` — host boost + xG-form + as_of-cut form, the SAME model
    the bets use). This is what makes the headline Brier reflect the model we actually
    trade, with no form leak.

    KNOCKOUT口径: the per-match market settles on the 90-MINUTE 3-way (home/draw/away by
    regulation score) for BOTH stages — a 1-1 knockout tie at 90' is a Tie payout, not an
    'advance' (the advance/penalty market is a SEPARATE per-team reach-round product). So
    the accuracy Brier MUST price every match with ``knockout=False`` — the same 90-min
    3-way the bet log / price-track / upcoming-card actually trade. Using ``knockout=True``
    here scaled λ down + dropped the host boost, inflating the draw mass and producing a
    Brier for a model we DON'T trade (and diverging from match_pick). Fixed → 90-min 3-way."""
    from prediction_market_soccer.model.match_pricing import is_knockout, price_match
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    from prediction_market_soccer.config.leagues import active as _active
    _comp_of = {c.api_football_id: c.key for c in _active()}
    _lids = tuple(_comp_of)
    rows = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, kickoff_ts, round, raw_json, "
        "league_id FROM fixture "
        "WHERE status_short IN ({}) AND home_goals IS NOT NULL "
        "AND league_id IN ({}) AND kickoff_ts >= datetime('now', '-60 days')".format(
            ",".join("?" * len(_FINISHED)), ",".join("?" * len(_lids))),
        (*_FINISHED, *_lids)).fetchall()
    from prediction_market_soccer.util.pricing import reg_score
    out = []
    _pit_day_cache: dict = {}   # PIT strength per (kickoff DATE, comp) — per-league models (C2)
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            if issues is not None:
                issues.append({"code": "missing_team_mapping", "fixture_id": r["api_id"]})
            continue
        try:
            _lg = _comp_of.get(r["league_id"])
            _k = ((r["kickoff_ts"] or "")[:10], _lg)
            if _k not in _pit_day_cache:
                _pit_day_cache[_k] = _pit_strength(conn, r["kickoff_ts"], _lg) if r["kickoff_ts"] else sm
            sm_pit = _pit_day_cache[_k]
            # 90-min 3-way for both stages (knockout=False) — matches the bet/MTM model;
            # host_neutral on a KO round drops the host's home-soil edge (neutral venue).
            mp = price_match(sm_pit, hi, ai, knockout=False, host_neutral=is_knockout(r["round"], _lg))
            # 90' regulation score (KO ET match settles the Tie market on the 90' result).
            gh90, ga90 = reg_score(r["raw_json"], r["home_goals"], r["away_goals"])
            outcome = 0 if gh90 > ga90 else (1 if gh90 == ga90 else 2)
            out.append(([mp.p_home, mp.p_draw, mp.p_away], outcome))
        except Exception as exc:
            if issues is not None:
                issues.append({"code": "pit_pricing_unavailable", "fixture_id": r["api_id"]})
            print(f"[performance] unavailable fixture={r['api_id']} ({type(exc).__name__})")
    return out


_SIDE_KEYS = ("home", "draw", "away")


def _headline(rep: "PerformanceReport") -> tuple[str, bool]:
    """State-aware headline string + whether it's a pass (for colour)."""
    from prediction_market_soccer.ops import system_overview as ov
    return ov.honest_headline(rep.trade_grade, rep.calibrated_brier, rep.brier_uniform), rep.trade_grade


def _advancer(raw_json: str | None, gh: int, ga: int) -> str | None:
    """Who went through in a knockout: by score, else the API-Football winner flag
    (covers level-after-extra-time decided on penalties). None if undeterminable."""
    if gh > ga:
        return "home"
    if ga > gh:
        return "away"
    if not raw_json:
        return None
    try:
        teams = json.loads(raw_json).get("teams", {})
        if teams.get("home", {}).get("winner") is True:
            return "home"
        if teams.get("away", {}).get("winner") is True:
            return "away"
    except Exception:
        pass
    return None


_prior_cache = None
_fifa_cache = None


def _fifa_ranks() -> dict:
    """Cached team_id → FIFA rank, for the motivation λ tilt."""
    global _prior_cache, _fifa_cache
    if _fifa_cache is None:
        if _prior_cache is None:
            from prediction_market_soccer.ingest.club_prior import load_prior
            _prior_cache = load_prior()
        _fifa_cache = {t.team_id: t.fifa_rank for t in _prior_cache.teams}
    return _fifa_cache


_pit_prior_cache: dict = {}
_pit_model_cache: dict = {}
_comp_by_lid: dict | None = None


def _comp_key(league_id) -> str | None:
    """fixture.league_id → registry comp key (None when unknown/not ours)."""
    global _comp_by_lid
    if _comp_by_lid is None:
        from prediction_market_soccer.config.leagues import active
        _comp_by_lid = {c.api_football_id: c.key for c in active()}
    return _comp_by_lid.get(league_id)


def _row_comp(row) -> str | None:
    """comp key from any row that MAY carry fixture.league_id (sqlite3.Row or dict)."""
    try:
        keys = row.keys()
    except AttributeError:
        return None
    if "league_id" not in keys:
        return None
    return _comp_key(row["league_id"])


def _pit_strength(conn, as_of: str, league: str | None = None):
    """PIT strength model for one match: ratings + alt-data computed only from data
    strictly before `as_of` (the kickoff). Mirrors param_sweep/decision_backtest so the
    record's pick matches the honest backtest. Returns the live-config strength model.

    ``league`` (comp key) builds the PER-LEAGUE model — the same construction every
    user-facing export prices with (per-comp fitted base_mu/home_adv + per-comp prior).
    Without it the merged-prior GLOBAL model is priced, which is NOT the model the
    display/bets use — pass the fixture's comp whenever it is known (C2)."""
    from dataclasses import replace
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.altdata_adjust import altdata_index
    from prediction_market_soccer.model.squad_strength import build_strength_live
    # Per-(kickoff DAY, league) memo shared by every consumer in this process —
    # settle_bets, the accuracy Brier, the frontend backtest and the price-track all
    # replay the same 60-day window, and going per-league multiplied the number of
    # fits. Same-day matches of one comp resolve to one build (137 in a 60-day window).
    from prediction_market_soccer.model.pit_strength import _db_identity, bucket_start
    as_of = bucket_start(as_of)
    from prediction_market_soccer.model.pit_strength import fc_input_fingerprint
    fc_version = fc_input_fingerprint(conn)
    ck = (_db_identity(conn), as_of, league, fc_version)
    if ck in _pit_model_cache:
        return _pit_model_cache[ck]
    # The prior must be the one that existed on the match's DATE, not tonight's file.
    # This cache was keyed on the league alone while the model memo above is keyed on
    # (date, league), so the model moved with the calendar and its anchor never did.
    _pk = (_db_identity(conn), as_of[:10], league, fc_version)
    if _pk not in _pit_prior_cache:
        from prediction_market_soccer.model.pit_strength import pit_prior
        _pit_prior_cache[_pk] = (pit_prior(conn, league, as_of[:10]) if league
                                 else load_prior())
    cfg = CONFIG.model
    sm = build_strength_live(conn, _pit_prior_cache[_pk], cfg, as_of=as_of,
                             xg_form=True, league=league)
    if cfg.oppadj_def_weight or cfg.oppadj_off_weight:
        sm = replace(sm, adj=altdata_index(conn, sm.ratings, as_of=as_of))
    if len(_pit_model_cache) < 400:      # bounded: one refresh replays ~137 keys
        _pit_model_cache[ck] = sm
    return sm


def match_pick(sm, cal, hi: str, ai: str, fx_row, book_row=None, *, conn=None,
               quotes=None, calib_confidence: float = 0.0, gate_open: bool = True,
               pit: bool = False) -> dict | None:
    """SINGLE SOURCE OF TRUTH for one settled match: which side we BET (the decision),
    the actual result, and whether we won — shared by the production bet log (this
    module) AND the price-track / mark-to-market (ops.milestone_export), so those views
    ALWAYS reconcile. Also reports the model's argmax pick as a prediction-accuracy
    reference (kept alongside, not the bet).

    BOTH stages settle on the 90-MINUTE 3-way (home/draw/away by regulation score) — a
    draw is a VALID outcome even in knockout (a 1-1 tie at 90' pays the Kalshi/Poly Tie
    contract; extra time + penalties then decide who ADVANCES, which is a SEPARATE
    per-team reach-round product, KXWCROUND, handled elsewhere). So:

      * group / knockout — 3-way incl. draw. The BET is the value/Kelly decision
                  (decision_model.decide on the PRE venue `quotes`): the most-underpriced
                  side, sized to [$0.2,$2]; `bet=False` when no side clears the edge bar.
                  Falls back to the model argmax as the bet when no `quotes` are given.
                  ``stage`` is reported ('group'/'knockout') for display only — it does NOT
                  switch the market to a 2-way advance bet.

    PIT: when ``conn`` is given and ``pit`` is True, the model + alt-data are recomputed
    with features cut at this match's kickoff (honest point-in-time). Both callers pass
    the same ``conn`` and the same PRE ``quotes`` row → identical pick → reconciliation.

    Returns None if the match can't be settled yet (knockout after ET, no winner flag).
    """
    from prediction_market_soccer.model.match_pricing import is_knockout, price_match_calibrated
    from prediction_market_soccer.util.pricing import reg_score
    gh, ga = fx_row["home_goals"], fx_row["away_goals"]
    if gh is None or ga is None:
        return None   # not settled (no final score) — nothing to settle a bet on
    # 90' regulation score for the 90' 3-way settlement (KO ET match → Tie on 1-1@90').
    try:
        _raw = fx_row["raw_json"]
    except (KeyError, IndexError):
        _raw = None
    gh90, ga90 = reg_score(_raw, gh, ga)
    knockout = is_knockout(fx_row["round"], _row_comp(fx_row))
    if pit and conn is not None and fx_row["kickoff_ts"]:
        sm = _pit_strength(conn, fx_row["kickoff_ts"], _row_comp(fx_row))
    # The per-match market (Kalshi KXWCGAME / Poly fwc) settles on the 90-MINUTE 3-way
    # result — a draw is VALID even in knockout (1-1 at 90' pays the "Tie" market; extra
    # time then decides who ADVANCES, which is a SEPARATE per-team reach-round market,
    # KXWCROUND). So the per-match bet is a 90-min 3-way for BOTH stages (knockout=False
    # → group-style draw calibration). gh/ga must be the 90-min (regulation) score.
    # Motivation tilt (group-progression psychology) — SAME logic as the live upcoming view,
    # PIT-correct (motivation_multipliers only counts games in rounds BEFORE this match, so
    # replaying game 2 sees game 1 only — no look-ahead). Applied to the BET decision so the
    # bet log + price-track reflect it; the accuracy Brier (_settled) stays on the clean model.
    lam_mult: tuple[float, float] | None = None
    motiv = None
    if conn is not None:
        from prediction_market_soccer.model.motivation import motivation_multipliers
        mh, ma, motiv = motivation_multipliers(conn, _fifa_ranks(), hi, ai, fx_row["round"], CONFIG.model)
        if (mh, ma) != (1.0, 1.0):
            lam_mult = (mh, ma)
    mp = price_match_calibrated(sm, hi, ai, knockout=False, cal=cal, lam_mult=lam_mult,
                                host_neutral=knockout)   # KO = neutral venue → drop host home-soil edge
    model = {"home": mp.p_home, "draw": mp.p_draw, "away": mp.p_away}
    base_stake = CONFIG.decision.base_stake_usd

    bd = book_row
    if bd and bd["bh"] is not None:
        s = (bd["bh"] or 0) + (bd["bd"] or 0) + (bd["ba"] or 0)
        price = {"home": bd["bh"] / s, "draw": bd["bd"] / s, "away": bd["ba"] / s} if s else model
    else:
        price = model  # no book → fair price
    edges = {k: model[k] - price[k] for k in _SIDE_KEYS}
    result = "home" if gh90 > ga90 else ("draw" if gh90 == ga90 else "away")
    model_pick = max(_SIDE_KEYS, key=lambda k: model[k])
    model_won = model_pick == result
    stage = "knockout" if knockout else "group"

    # ── Knockout WIN/LOSS standard: the 2-way "who advances" market (ET + penalties),
    # NOT the 90' 3-way. From the knockout stage on, the prediction is judged right/wrong
    # by the argmax pick-a-side (the team the model gives ≥50% to advance) vs who ACTUALLY
    # advanced (`_advancer`, ET-inclusive). Group stage keeps the 3-way result. The 90'
    # bet/settlement track above is unchanged (it trades the 3-way KXWCGAME/fwc contract).
    advance = None
    if knockout:
        mp_adv = price_match_calibrated(sm, hi, ai, knockout=True, cal=cal, lam_mult=lam_mult)
        pha = mp_adv.p_home_advance
        advancer = _advancer(_raw, gh, ga)
        if pha is not None:
            adv_pick = "home" if pha >= 0.5 else "away"
            advance = {"pick": adv_pick, "p_home_advance": round(pha, 4), "advancer": advancer,
                       "won": (adv_pick == advancer) if advancer else None}
    # Unified prediction口径: 2-way for knockout (when resolvable), else 3-way.
    if advance is not None:
        pred_pick, pred_result, pred_won = advance["pick"], advance["advancer"], advance["won"]
    else:
        pred_pick, pred_result, pred_won = model_pick, result, model_won
    # PIT recent-form for the decision's confidence sizing.
    form = None
    if pit and conn is not None:
        try:
            from prediction_market_soccer.model.form_strength import form_index
            fi = form_index(conn, as_of=fx_row["kickoff_ts"])
            form = {"home_z": fi[hi].form_z if hi in fi else None,
                    "away_z": fi[ai].form_z if ai in fi else None}
        except Exception:
            form = None
    if quotes:
        from prediction_market_soccer.strategy.decision_model import decide
        d = decide(model, quotes, calib_confidence=calib_confidence, form=form, gate_open=gate_open,
                   conviction_side=(motiv or {}).get("conviction_side"))
        if d.side is not None:
            # VALUE bet: a tradable edge → the most-underpriced side, confidence-sized [$0.2,$2].
            pick, bet, stake, conf_k, bet_kind = d.side, True, d.stake_usd, d.confidence_k, "value"
            cost = max((d.price_cents or 0.0) / 100.0, 1e-6)   # real entry ask we'd pay
            edge_val, model_prob = d.net_edge, round(model[pick], 4)
        else:
            # HYBRID (no tradable edge): instead of skipping, bet the model ARGMAX (most-likely
            # side) at the flat base stake ($1), sized through the SAME contract calc. Turns the
            # previously-skipped matches into argmax bets — bet where we have edge, else favourite.
            pick, bet, stake, conf_k, bet_kind = model_pick, True, base_stake, d.confidence_k, "argmax"
            cost, edge_val, model_prob = max(price[model_pick], 1e-6), edges[model_pick], round(model[model_pick], 4)
    else:
        # legacy (no venue quotes): bet the model argmax at the book price.
        pick, bet, stake, conf_k, bet_kind = model_pick, True, base_stake, None, "argmax"
        cost, edge_val, model_prob = max(price[pick], 1e-6), edges[pick], round(model[pick], 4)

    return {"stage": stage, "pick": pick, "model_pick": model_pick, "result": result,
            "won": (pick == result) if bet else None, "model_won": model_won,
            # 2-way advance judgment (knockout only; None for group) + the unified
            # prediction口径 the accuracy track uses (2-way for KO, 3-way for group).
            "advance": advance, "pred_pick": pred_pick, "pred_result": pred_result,
            "pred_won": pred_won,
            "bet": bet, "bet_kind": bet_kind, "stake_usd": round(stake, 2), "confidence_k": conf_k,
            "cost": cost, "model_prob": model_prob, "edge": round(edge_val, 4), "model": model,
            "motivation": motiv}


def _research_bet_log(conn) -> list[dict]:
    """Legacy reconstruction for explicit research only; never used by a published report."""
    from prediction_market_soccer.ingest.club_prior import load_prior

    prior = load_prior()
    name = {t.team_id: t.name for t in prior.teams}
    zh = {t.team_id: t.zh for t in prior.teams}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    # PRE-milestone venue entry quotes per fixture (real Kalshi/Poly price at kickoff) —
    # the executable asks + de-vig the decision model selects on, and the true per-contract
    # cost we'd have paid. SELECT * so quotes_from_milestone_row sees every ask/devig column.
    pre_px = {r["fixture_api_id"]: r for r in conn.execute(
        "SELECT * FROM milestone_snapshot WHERE milestone='PRE'")}
    try:
        legacy_render = {r['fixture_api_id']:json.loads(r['payload']) for r in conn.execute('SELECT fixture_api_id,payload FROM legacy_paper_render')}
    except Exception:
        legacy_render = {}

    # T75 (last in-play milestone before FT) per fixture, for closing-line value (CLV).
    t75_px = {r["fixture_api_id"]: r for r in conn.execute(
        "SELECT fixture_api_id, poly_home_ask, poly_draw_ask, poly_away_ask, "
        "kalshi_home_ask, kalshi_draw_ask, kalshi_away_ask "
        "FROM milestone_snapshot WHERE milestone='T75'")}

    # De-vig book (averaged bookmakers) per settled fixture.
    book = {r["api_id"]: r for r in conn.execute(
        "SELECT f.api_id, AVG(o.p_home) bh, AVG(o.p_draw) bd, AVG(o.p_away) ba "
        "FROM fixture f JOIN match_odds o ON o.fixture_api_id=f.api_id "
        "AND o.bookmaker <> 'live_consensus' "   # pre-match book only
        "WHERE f.status_short IN ({}) GROUP BY f.api_id".format(",".join("?" * len(_FINISHED))),
        _FINISHED).fetchall()}

    _comp_key(0)   # ensure the league_id→comp map is populated
    _lids = tuple(_comp_by_lid.keys())
    rows = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, kickoff_ts, round, "
        "raw_json, league_id "
        "FROM fixture WHERE status_short IN ({}) AND home_goals IS NOT NULL "
        "AND league_id IN ({}) "
        "ORDER BY kickoff_ts".format(",".join("?" * len(_FINISHED)), ",".join("?" * len(_lids))),
        (*_FINISHED, *_lids)).fetchall()

    from prediction_market_soccer.strategy.decision_model import quotes_from_milestone_row
    from prediction_market_soccer.ops.settle_bets import frozen_pick, frozen_inplay
    from prediction_market_soccer.util.pricing import to_cents, pnl_cents as _pnl_cents, sized_pnl_cents
    base_stake = CONFIG.decision.base_stake_usd     # flat $1 sizing for the in-play entry stream
    inplay_cum_c = 0.0; inplay_w = 0; inplay_n = 0  # in-play entry P&L stream
    combined_cum_c = 0.0                            # smart-exit realised + in-play entry (one cum)
    log: list[dict] = []
    cum = 0.0
    staked = 0.0
    wins = 0
    n_bets = 0           # matches the decision model actually bet
    skipped = 0          # settled matches the decision model declined (no edge)
    cum_c = 0.0          # cumulative per-contract ¢ P&L (decision bets)
    sum_entry_c = 0.0    # Σ entry ¢ (for the average)
    cents_avail = 0.0    # Σ theoretically-capturable ¢ (for capture rate)
    # argmax 口径 (model_pick): bet the most-likely side EVERY settled match (all 22) —
    # the parallel reference track shown alongside the decision model.
    am_cum_c = 0.0       # cumulative per-contract ¢ P&L (argmax, all matches)
    am_n = am_wins = 0
    am_priced_n = am_priced_wins = 0   # the subset the ¢ P&L is actually built from
    # REALISED 口径: the strategy is decision + smart-exit cash-out (we don't hold to FT).
    realized_cum_c = 0.0   # cumulative ¢ P&L with the model-aware cash-out applied
    realized_wins = 0      # bets that REALISED a profit (cash-out or settle)
    n_smart_sold = 0       # bets where the smart-exit actually fired (sold the overshoot)
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            continue
        # FROZEN DECISION (PIT strength + PIT calibration, computed once at settle) — read the
        # ledger, never recompute, so the track record never mutates as later matches settle.
        # The price-track reads the SAME frozen row → the two views reconcile by construction.
        pr = pre_px.get(r["api_id"])
        quotes = quotes_from_milestone_row(pr) if pr is not None else None
        mr = frozen_pick(conn, r, hi, ai, quotes, book.get(r["api_id"]), self_heal=False)
        if mr is None:
            continue   # knockout level after ET with no winner flag yet — can't settle
        knockout = mr["stage"] == "knockout"
        gh, ga = r["home_goals"], r["away_goals"]
        result = mr["result"]
        pr = pre_px.get(r["api_id"])
        side_label = {"home": name.get(hi, hi), "draw": "Draw", "away": name.get(ai, ai)}

        _pr_cols = set(pr.keys()) if (pr is not None and hasattr(pr, "keys")) else set()

        def _entry_c(side):
            """Real PRE venue ¢ for a side (Poly then Kalshi) → (cents, source); else None."""
            if pr is None:
                return None
            if pr[f"poly_{side}_ask"] is not None:
                return to_cents(pr[f"poly_{side}_ask"]), "poly"
            if pr[f"kalshi_{side}_ask"] is not None:
                return to_cents(pr[f"kalshi_{side}_ask"]), "kalshi"
            return None

        def _adv_entry_c(side):
            """Real PRE 2-way advance ¢ for a side (Poly then Kalshi) → (cents, source)."""
            if pr is None:
                return None
            for v in ("poly_adv", "kalshi_adv"):
                col = f"{v}_{side}_ask"
                if col in _pr_cols and pr[col] is not None:
                    return to_cents(pr[col]), ("poly" if v == "poly_adv" else "kalshi")
            return None

        def _clv_c(side, entry_c):
            t75 = t75_px.get(r["api_id"])
            if t75 is None or entry_c is None:
                return None
            s = t75[f"poly_{side}_ask"] if t75[f"poly_{side}_ask"] is not None else t75[f"kalshi_{side}_ask"]
            return round(to_cents(s) - entry_c, 1) if s is not None else None

        # ── argmax / prediction口径 — recorded for EVERY settled match ──
        # The win/loss STANDARD is 2-way (who advances) from the knockout stage on, 3-way
        # (90' result) for the group stage (mr["pred_*"] already encodes this). The ¢ PnL
        # marks the 2-way advance contract price for a knockout pick (PRE advance columns),
        # else the 90' 3-way price for a group pick. am_pick is the side predicted to advance.
        am_pick, am_won = mr["pred_pick"], mr["pred_won"]
        _ame = _adv_entry_c(am_pick) if mr.get("advance") is not None else _entry_c(am_pick)
        am_entry_c = _ame[0] if _ame else None
        # Position-sized like the decision bets, but with a FLAT $1 stake (argmax is the naive
        # "bet the most-likely side every match" benchmark). contracts = 1$ / (entry_c/100), so
        # a loss = −100¢ (the whole $1) and a cheap entry buys more contracts (bigger win).
        am_unit = _pnl_cents(am_entry_c, am_won)                    # per-contract ¢
        am_contracts = (1.0 / (am_entry_c / 100.0)) if am_entry_c else 0.0
        am_pnl_c = (round(am_contracts * am_unit, 1)
                    if (am_entry_c and am_unit is not None) else None)
        am_cum_c += (am_pnl_c or 0.0)
        if am_won is not None:        # only count resolvable predictions in the accuracy record
            am_n += 1
            am_wins += int(am_won)
            # The RECORD published beside the ¢ P&L must come from the same matches the
            # P&L does. It did not: the record counted every resolvable prediction (163)
            # while the P&L could only be marked where an entry price existed (101), and
            # the 62 unpriced matches happened to run 49W-13L — so the headline read 12.8
            # points better than the rows the money came from. Accuracy over everything is
            # still worth reporting; it is just a different number and now says so.
            if am_pnl_c is not None:
                am_priced_n += 1
                am_priced_wins += int(am_won)

        # ── decision 口径 (value pick) — populated only when the model actually bet ──
        if mr["bet"]:
            pick, won = mr["pick"], mr["won"]
            cost, edge_val, model_prob = mr["cost"], mr["edge"], mr["model_prob"]
            stake = mr["stake_usd"] or 1.0
            dec_odds = 1.0 / cost
            pnl = stake * (dec_odds - 1.0) if won else -stake
            cum += pnl
            staked += stake
            wins += int(won)
            n_bets += 1
            _de = ((mr['ledger_entry_c'],mr['ledger_venue']) if mr.get('ledger_entry_c') is not None else _entry_c(pick))
            entry_cents, entry_source = (_de[0], _de[1]) if _de else (to_cents(cost), "book_devig")
            # Position-sized ¢ P&L: the bet stakes `stake` $ at the entry price, so it buys
            # contracts = stake / (entry_c/100). The POSITION's ¢ P&L is the per-contract ¢
            # move × that contract count — NON-linear in entry price (a cheaper entry buys more
            # contracts, so the same ¢ move pays more). The earlier per-contract-only number
            # understated bets sized away from $1. (The argmax reference above deliberately stays
            # per-contract: it's a flat 1-contract benchmark, not a sized position.)
            c_pnl_unit = _pnl_cents(entry_cents, won)                       # per-contract ¢
            c_pnl = sized_pnl_cents(entry_cents, c_pnl_unit, stake)         # $-sized hold ¢ (shared fn)
            cum_c += (c_pnl or 0.0)
            sum_entry_c += (entry_cents or 0.0)
            cents_avail += ((100.0 - entry_cents) if won else entry_cents) if entry_cents is not None else 0.0
            clv_cents = _clv_c(pick, entry_cents)
            # REALISED cash-out: apply the validated smart-exit (sell the over-reaction)
            # to THIS bet — the realised ¢ is the cash-out PnL when it fired, else hold-to-FT.
            legacy_row = legacy_render.get(r['api_id'])
            smart_exit = mr.get('smart_exit') if legacy_row is None else legacy_row.get('smart_exit')
            try:
                from prediction_market_soccer.strategy.smart_exit import smart_exit_cashout
                if legacy_row is None and 'smart_exit' not in mr and conn is not None and r["kickoff_ts"]:
                    smart_exit = smart_exit_cashout(conn, _pit_strength(conn, r["kickoff_ts"], _row_comp(r)),
                                                    r["api_id"], pick, entry_cents, hi, ai, r["round"], won)
            except Exception as exc:
                raise ValueError('Research exit reconstruction is unavailable; explicit candidate required') from exc
            if isinstance(smart_exit, dict) and 'status' in smart_exit:
                if smart_exit['status'] == 'exited':
                    smart_exit = {key:value for key,value in smart_exit.items() if key != 'status'}
                elif smart_exit['status'] == 'held_no_trigger':
                    smart_exit = None
                else:
                    raise ValueError('Unknown research exit cannot be settled as hold: ' + str(smart_exit.get('reason')))
            # Realised = the SAME $-sizing (sized_pnl_cents) on the per-contract cash-out move
            # (sold_c − entry_c) or the held-to-FT per-contract number when no cash-out fired.
            realized_unit = smart_exit["pnl_c"] if smart_exit else c_pnl_unit
            realized_c = sized_pnl_cents(entry_cents, realized_unit, stake)   # $-sized realised ¢
            realized_cum_c += (realized_c or 0.0)
            realized_wins += int((realized_c or 0.0) > 0)
            n_smart_sold += int(bool(smart_exit))
            dec = {"pick": pick, "pick_team": side_label[pick], "won": won,
                   "bet_kind": mr.get("bet_kind", "value"),   # value (edge) | argmax (no-edge fallback)
                   "stake_usd": round(stake, 2), "model_prob": model_prob,
                   "price": round(cost, 4), "dec_odds": round(dec_odds, 3),
                   "edge": round(edge_val, 4), "confidence_k": mr.get("confidence_k"),
                   "pnl": round(pnl, 3), "cum_pnl": round(cum, 3),
                   "entry_cents": entry_cents, "entry_source": entry_source,
                   "settle_cents": (100.0 if won else 0.0), "pnl_cents": c_pnl,
                   "cum_pnl_cents": round(cum_c, 1), "clv_cents": clv_cents,
                   "smart_exit": smart_exit, "realized_pnl_cents": realized_c,
                   "realized_cum_pnl_cents": round(realized_cum_c, 1)}
        else:
            skipped += 1   # decision model found no tradable edge → no bet (but still listed)
            dec = {"pick": None, "pick_team": None, "won": None, "stake_usd": 0.0,
                   "model_prob": round(mr["model"][am_pick], 4), "price": None,
                   "dec_odds": None, "edge": None, "confidence_k": mr.get("confidence_k"),
                   "pnl": None, "cum_pnl": None, "entry_cents": None, "entry_source": None,
                   "settle_cents": None, "pnl_cents": None, "cum_pnl_cents": None, "clv_cents": None}

        # SECOND P&L stream: the frozen causal in-play entry (relative-value, PIT). The combined
        # cumulative = smart-exit realised (this bet, if any) + in-play entry, so the two streams
        # roll up into one column in the view.
        ip = frozen_inplay(conn, r["api_id"])
        inplay_c = None
        ip_dec: dict = {"inplay_side": None}
        if ip:
            # SAME $-sizing function (sized_pnl_cents) as the pre-match bet, but EDGE-WEIGHTED:
            # the frozen in-play entry carries its own stake_usd ($0.2–$2 by in-play edge, mirror
            # of the pre-match envelope). 盘中离场 = the frozen in-play smart-exit; realised =
            # cash-out or hold, per-contract, sized. Falls back to the flat base for old rows.
            ip_stake = ip.get("stake_usd") or base_stake
            inplay_c = sized_pnl_cents(ip["entry_cents"], ip["realized_pnl_cents"], ip_stake)
            inplay_hold_c = sized_pnl_cents(ip["entry_cents"], ip["hold_pnl_cents"], ip_stake)
            inplay_cum_c += (inplay_c or 0.0)
            inplay_n += 1
            inplay_w += int((inplay_c or 0.0) > 0)     # W = profitable realised (as pre-match realized)
            ip_dec = {"inplay_milestone": ip["milestone"], "inplay_side": ip["side"],
                      "inplay_side_team": side_label[ip["side"]], "inplay_entry_cents": ip["entry_cents"],
                      "inplay_won": ip["won"], "inplay_edge": ip["edge"], "inplay_exit": ip.get("exit"),
                      "inplay_stake_usd": round(ip_stake, 2),
                      "inplay_hold_cents": inplay_hold_c, "inplay_pnl_cents": inplay_c}
        realized_this = dec.get("realized_pnl_cents") or 0.0
        combined_this = realized_this + (inplay_c or 0.0)
        combined_cum_c += combined_this

        log.append({
            "fixture_id": r['api_id'],
            "evidence_level": (mr.get('evidence_level') or ('posthoc_candlestick_paper' if pr is not None and pr['price_source']=='candlestick' else 'legacy_live_marked_paper')),
            "pit_status": ('forward_recorded' if mr.get('evidence_level')=='forward_observed_paper' else ('posthoc_reconstruction' if pr is not None and pr['price_source']=='candlestick' else 'unverified_legacy')),
            "fee_status": "not_deducted",
            "stage": "knockout" if knockout else "group",
            # Competition key on EVERY row (C-20): the cross-competition roll-up above is
            # only honest if the reader can split it back apart, and the frontend BetLog
            # needs a filter key that is not the display name.
            "league": _row_comp(r),
            "date": (r["kickoff_ts"] or "")[:10],
            **ip_dec,
            "combined_pnl_cents": round(combined_this, 1),
            # three cumulatives: pre-match (smart-exit realised), in-play, and the combined total.
            "pre_cum_pnl_cents": round(realized_cum_c, 1),
            "inplay_cum_pnl_cents": round(inplay_cum_c, 1),
            "combined_cum_pnl_cents": round(combined_cum_c, 1),
            "home": name.get(hi, hi), "away": name.get(ai, ai),
            "home_id": hi, "away_id": ai,
            "home_zh": zh.get(hi, ""), "away_zh": zh.get(ai, ""),
            "score": f"{gh}-{ga}",
            "result": result,
            "bet": mr["bet"],
            **dec,
            # prediction口径 (argmax pick) — present on every row. For knockout this is the
            # 2-way "who advances" pick judged vs the actual advancer; group = 90' 3-way.
            "model_pick": am_pick, "model_pick_team": side_label[am_pick], "model_won": am_won,
            "argmax_entry_cents": am_entry_c,
            "argmax_settle_cents": (None if am_won is None else (100.0 if am_won else 0.0)),
            "argmax_pnl_cents": am_pnl_c, "argmax_cum_pnl_cents": round(am_cum_c, 1),
            # explicit 2-way advance block (knockout only; None for group) for the frontend.
            "advance": mr.get("advance"),
        })
    return log, {"skipped": skipped, "n_bets": n_bets, "model_n": am_n, "model_hits": am_wins,
                 "argmax_priced_n": am_priced_n, "argmax_priced_hits": am_priced_wins,
                 "staked_usd": round(staked, 2), "argmax_pnl_cents_total": round(am_cum_c, 1),
                 # Record over the PRICED matches — the ones argmax_pnl_cents_total sums.
                 "argmax_record": f"{am_priced_wins}W-{am_priced_n - am_priced_wins}L",
                 # Hit rate over every resolvable prediction, priced or not (model quality).
                 "argmax_accuracy_record": f"{am_wins}W-{am_n - am_wins}L",
                 # REALISED 口径 (decision + smart-exit cash-out — the actual strategy).
                 # W/L here = PROFITABLE / not (a 'W' can be a cash-out at a gain on a bet
                 # that would have lost at settlement). Same W-L format as the other modes.
                 "realized_pnl_cents_total": round(realized_cum_c, 1),
                 "realized_record": f"{realized_wins}W-{n_bets - realized_wins}L",
                 "n_smart_sold": n_smart_sold, "hold_pnl_cents_total": round(cum_c, 1),
                 "hold_record": f"{wins}W-{n_bets - wins}L",
                 # IN-PLAY ENTRY stream + the COMBINED cumulative (smart-exit + in-play).
                 "inplay_pnl_cents_total": round(inplay_cum_c, 1),
                 "inplay_record": f"{inplay_w}W-{inplay_n - inplay_w}L", "n_inplay": inplay_n,
                 "combined_pnl_cents_total": round(combined_cum_c, 1)}


def _gate_state(cal: dict | None, league: str) -> dict:
    """This competition's §3.5 calibration gate, phrased so a reader can act on it.

    ``gate_open_for`` answers the yes/no the executor asks; a report needs the WHY too,
    because "shut" has three different meanings — no history at all, a real history that
    is still short of PER_LEAGUE_MIN_N (cold start, prices with the pooled calibrator),
    and enough history whose calibrated Brier still loses to the uniform baseline.
    """
    from prediction_market_soccer.model.probability_calibration import (
        PER_LEAGUE_MIN_N, gate_open_for)

    per = ((cal or {}).get("per_league") or {}).get(league) or {}
    is_open = gate_open_for(cal, league)
    n = per.get("n")
    if not per:
        status = "NO-FIT (no settled history of its own)"
    elif is_open:
        status = "OPEN"
    elif per.get("cold_start"):
        status = f"COLD-START ({n}/{PER_LEAGUE_MIN_N})"
    else:
        status = "BLOCKED (calibrated Brier > uniform)"
    return {
        "gate_open": bool(is_open),
        "status": status,
        "n_calibration": n,
        "min_n": PER_LEAGUE_MIN_N,
        "cold_start": bool(per.get("cold_start")),
        # Which calibrator actually prices this competition: its own, or the pooled fit.
        "applies": per.get("applies") or ("pooled" if cal else None),
        "method": per.get("method"), "param": per.get("param"),
        "calibrated_brier": per.get("calibrated_brier"),
        "uniform_brier": per.get("uniform_brier") or (cal or {}).get("uniform_brier"),
    }


def _mean(xs: list) -> float:
    return round(sum(xs) / len(xs), 1) if xs else 0.0


def _by_league(log: list[dict], cal: dict | None) -> list[dict]:
    """Split the bet log by competition and attach each one's gate state (C-20).

    Every row field summed here is PER-ROW (``pnl_cents`` / ``realized_pnl_cents`` /
    ``inplay_pnl_cents`` / ``combined_pnl_cents``); the ``*_cum_*`` columns on a row are
    running totals across the whole log and would double-count if summed, so they are
    deliberately not touched.
    """
    from prediction_market_soccer.config.leagues import REGISTRY, active

    acc: dict[str, dict] = {}

    def _slot(lg: str) -> dict:
        return acc.setdefault(lg, {
            "n_settled": 0, "n_bets": 0, "n_skipped": 0, "wins": 0,
            "pnl_units": 0.0, "staked_usd": 0.0,
            "hold_cents": 0.0, "realized_cents": 0.0, "realized_wins": 0, "n_smart_sold": 0,
            "inplay_cents": 0.0, "n_inplay": 0, "inplay_wins": 0, "combined_cents": 0.0,
            "argmax_cents": 0.0, "model_n": 0, "model_hits": 0,
            "entry": [], "clv": [],
        })

    for b in log:
        d = _slot(b.get("league") or "_unmapped")
        d["n_settled"] += 1
        if b.get("bet"):
            d["n_bets"] += 1
            d["wins"] += int(bool(b.get("won")))
            d["pnl_units"] += (b.get("pnl") or 0.0)
            d["staked_usd"] += (b.get("stake_usd") or 0.0)
            d["hold_cents"] += (b.get("pnl_cents") or 0.0)
            rc = b.get("realized_pnl_cents") or 0.0
            d["realized_cents"] += rc
            d["realized_wins"] += int(rc > 0)
            d["n_smart_sold"] += int(bool(b.get("smart_exit")))
            if b.get("entry_cents") is not None:
                d["entry"].append(b["entry_cents"])
            if b.get("clv_cents") is not None:
                d["clv"].append(b["clv_cents"])
        else:
            d["n_skipped"] += 1
        if b.get("inplay_side"):
            ic = b.get("inplay_pnl_cents") or 0.0
            d["inplay_cents"] += ic
            d["n_inplay"] += 1
            d["inplay_wins"] += int(ic > 0)
        d["combined_cents"] += (b.get("combined_pnl_cents") or 0.0)
        d["argmax_cents"] += (b.get("argmax_pnl_cents") or 0.0)
        if b.get("model_won") is not None:
            d["model_n"] += 1
            d["model_hits"] += int(bool(b["model_won"]))

    # Registry order, then any competition that only shows up in the log (a fixture whose
    # league_id left the registry, or the unmapped bucket) so nothing is silently dropped.
    keys = [c.key for c in active()] + [k for k in acc if k not in REGISTRY]
    out: list[dict] = []
    for lg in keys:
        d = acc.get(lg)
        comp = REGISTRY.get(lg)
        if d is None and comp is None:
            continue
        d = d or _slot(lg)
        row = {
            "league": lg,
            "name": comp.name if comp else lg,
            "zh": comp.zh if comp else "",
            "kind": comp.kind if comp else "",
            "n_settled": d["n_settled"], "n_bets": d["n_bets"], "n_skipped": d["n_skipped"],
            "record": f'{d["wins"]}W-{d["n_bets"] - d["wins"]}L',
            "pnl_units": round(d["pnl_units"], 3),
            "staked_usd": round(d["staked_usd"], 2),
            "roi": round(d["pnl_units"] / d["staked_usd"], 4) if d["staked_usd"] else 0.0,
            "hold_pnl_cents": round(d["hold_cents"], 1),
            "realized_pnl_cents": round(d["realized_cents"], 1),
            "realized_record": f'{d["realized_wins"]}W-{d["n_bets"] - d["realized_wins"]}L',
            "n_smart_sold": d["n_smart_sold"],
            "inplay_pnl_cents": round(d["inplay_cents"], 1),
            "inplay_record": f'{d["inplay_wins"]}W-{d["n_inplay"] - d["inplay_wins"]}L',
            "n_inplay": d["n_inplay"],
            "combined_pnl_cents": round(d["combined_cents"], 1),
            "argmax_pnl_cents": round(d["argmax_cents"], 1),
            "argmax_record": f'{d["model_hits"]}W-{d["model_n"] - d["model_hits"]}L',
            "model_accuracy": round(d["model_hits"] / d["model_n"], 3) if d["model_n"] else 0.0,
            "avg_entry_cents": _mean(d["entry"]),
            "avg_clv_cents": _mean(d["clv"]),
            "n_clv": len(d["clv"]),
        }
        row["gate"] = _gate_state(cal, lg)
        out.append(row)
    return out


def _record_totals(records):
    """Compatibility aggregates from immutable rows; never price or choose a trade."""
    from decimal import Decimal
    def total(key):
        return float(sum((Decimal(str(row.get(key) or 0)) for row in records), Decimal(0)))

    pre = [row for row in records if row.get('bet')]
    inplay = [row for row in records if row.get('inplay_side')]
    model = [row for row in records if row.get('model_won') is not None]
    priced = [row for row in records if row.get('argmax_pnl_cents') is not None]
    def win_loss(rows, key):
        wins = sum(bool(row.get(key)) for row in rows)
        return f'{wins}W-{len(rows)-wins}L'
    def pnl_record(rows, key):
        wins = sum((row.get(key) or 0) > 0 for row in rows)
        losses = sum((row.get(key) or 0) < 0 for row in rows)
        flats = sum(row.get(key) == 0 for row in rows)
        return f'{wins}W-{losses}L-{flats}F'
    return {
        'n_decision_bets': len(pre), 'n_skipped': len(records)-len(pre),
        'decision_staked_usd': total('stake_usd'), 'n_inplay': len(inplay),
        'pnl_units': total('pnl'), 'pnl_record': win_loss(pre, 'won'),
        'pnl_roi': total('pnl')/total('stake_usd') if total('stake_usd') else 0.0,
        'bet_since': records[0].get('date', '') if records else '',
        'pnl_cents_total': total('pnl_cents'),
        'realized_pnl_cents_total': total('realized_pnl_cents'),
        'realized_record': pnl_record(pre, 'realized_pnl_cents'),
        'n_smart_sold': sum(bool(row.get('smart_exit')) for row in pre),
        'hold_record': win_loss(pre, 'won'), 'hold_pnl_cents_total': total('pnl_cents'),
        'inplay_record': pnl_record(inplay, 'inplay_pnl_cents'),
        'inplay_pnl_cents_total': total('inplay_pnl_cents'),
        'combined_pnl_cents_total': total('combined_pnl_cents'),
        'model_pred_accuracy': sum(bool(row.get('model_won')) for row in model)/len(model) if model else 0.0,
        'argmax_record': win_loss(priced, 'model_won'),
        'argmax_accuracy_record': win_loss(model, 'model_won'),
        'argmax_priced_n': len(priced), 'argmax_pnl_cents_total': total('argmax_pnl_cents'),
        'avg_entry_cents': _mean([row['entry_cents'] for row in pre if row.get('entry_cents') is not None]),
        'avg_clv_cents': _mean([row['clv_cents'] for row in pre if row.get('clv_cents') is not None]),
    }


def _bet_log(conn):
    """Read the complete frozen rendered book. Missing baseline is never a replay trigger."""
    from prediction_market_soccer.util.frozen_strategy_store import read_book
    book = read_book(conn)
    records = book['ledger']['records']
    return records, _record_totals(records)


def report_from_book(book, *, demo_execution=None, observed_at=None) -> PerformanceReport:
    """Pure projection of a validated frozen book, including an inactive candidate.

    No database, prices, calibration fitting, execution lookup, or publication. The
    optional demo summary is a separate explicitly supplied execution subset.
    """
    from copy import deepcopy
    from dataclasses import fields
    from prediction_market_soccer.util.strategy_ledger import validate_strategy_ledger
    ledger = deepcopy(validate_strategy_ledger(book['ledger']))
    version = book['version']
    if ledger.get('book_version', {}).get('version_id') != version['version_id']:
        raise ValueError('Report book identity does not match its ledger')
    records = ledger['records']
    accepted = {field.name for field in fields(PerformanceReport)}
    defaults = {'n_settled': 0, 'brier': None, 'brier_uniform': None, 'calibrated_brier': None,
        'trade_grade': False, 'log_loss': None, 'favourite_hit_rate': None,
        'calibration_pnl': None, 'calibration_pnl_per_bet': None, 'settled_signal_pnl': None,
        'n_settled_signals': 0, 'notes': []}
    data = {**defaults, **{key:deepcopy(value) for key,value in book['report_metadata'].items() if key in accepted}}
    data.update(_record_totals(records))
    data.update(bet_log=records, strategy_ledger=ledger, as_of=ledger['as_of'],
                source_as_of=ledger['as_of'], data_status={'state': 'ok', 'issues': []})
    data['evidence_summary'] = {**(data.get('evidence_summary') or {}),
        'book_version': ledger['book_version'], 'baseline_source_as_of': version['base_as_of'],
        'diagnostics_source_as_of': version['base_as_of'], 'historical_records_immutable': True,
        'forward_rows_appended': len(records)-version['baseline_count']}
    if observed_at is not None:
        data['evidence_summary']['refresh_observed_at'] = observed_at
    # Candidate/model returns never inherit unrelated demo P&L from old metadata.
    data['demo_execution'] = deepcopy(demo_execution) if demo_execution is not None else {'state':'not_requested'}
    original_gates = {row.get('league'):row.get('gate') for row in data.get('by_league', [])}
    data['by_league'] = _by_league(records, None)
    for row in data['by_league']:
        if row['league'] in original_gates:
            row['gate'] = original_gates[row['league']]
    return PerformanceReport(**data)


def build(conn=None, *, freeze=True) -> PerformanceReport:
    """Read frozen rows; only explicit/default freeze=True may append sealed paper.

    freeze=False is a read-only preview. It never settles, prices, consumes a new
    completion, initializes a schema, or changes the book's active version.
    """
    import sqlite3
    from datetime import datetime, timezone
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.util.frozen_strategy_store import consume_completed_paper, read_book
    from prediction_market_soccer.util.timing_provenance import demo_execution_summary
    own = conn is None
    if own:
        if freeze:
            conn = store.init_db()
        else:
            conn = sqlite3.connect(f"file:{CONFIG.paths.data / 'soccer.db'}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
    try:
        if freeze:
            consume_completed_paper(conn)
        book = read_book(conn)
        demo = demo_execution_summary(conn)
        demo['execution_scope'] = {'kind':'demo_execution_subset','pnl_covers':'closed_fills_only',
                                  'model_pnl_replaced':False,'unfilled_counted_as_loss':False}
        demo['coverage'] = _demo_coverage(conn, book['ledger'])
        return report_from_book(book, demo_execution=demo, observed_at=datetime.now(timezone.utc).isoformat())
    finally:
        if own:
            conn.close()


def preview_strategy_views(conn, book, destination, *, candidate_data=None):
    """Render a supplied candidate snapshot outside both publication directories.

    The caller provides a complete validated book; this never constructs a backtest,
    registers/activates a version, appends paper records, or promotes live files.
    """
    from pathlib import Path
    from prediction_market_soccer.ops import milestone_export
    from prediction_market_soccer.ops.run_status import atomic_bytes
    target = Path(destination).resolve()
    protected = (CONFIG.paths.output.resolve(), CONFIG.paths.frontend_data.resolve())
    if any(target == path or target.is_relative_to(path) or path.is_relative_to(target) for path in protected):
        raise ValueError('Candidate preview requires a separate directory outside live artifacts')
    explicit = book['version'].get('origin') == 'explicit_backtest'
    if explicit and candidate_data is None:
        raise ValueError('Candidate preview requires its explicit candidate price source')
    if candidate_data is not None:
        provenance = json.loads(book['version'].get('provenance_json') or '{}')
        if provenance.get('run_id') != candidate_data.run_id:
            raise ValueError('Candidate price run differs from the registered book')
    target.mkdir(parents=True, exist_ok=False)
    report = report_from_book(book)
    marks = (milestone_export.from_candidate(report.strategy_ledger, candidate_data) if candidate_data is not None
             else milestone_export.build(conn, freeze=False, ledger=report.strategy_ledger))
    atomic_bytes(target/'performance_report.json', json.dumps(asdict(report), ensure_ascii=False, allow_nan=False).encode())
    atomic_bytes(target/'milestone_marks.json', json.dumps(marks, ensure_ascii=False, allow_nan=False).encode())
    build_pdf(report, str(target/'performance_report.pdf'))
    validate_strategy_views(target, report.strategy_ledger)
    return {'report':report, 'path':str(target), 'ledger_id':report.strategy_ledger['ledger_id']}


def validate_strategy_views(directory, ledger):
    """Require the same full financial sequence in JSON, price-track and PDF metadata."""
    from pathlib import Path
    import shutil
    import subprocess
    from prediction_market_soccer.util.strategy_ledger import validate_strategy_ledger
    root = Path(directory)
    expected = validate_strategy_ledger(ledger)
    perf = json.loads((root/'performance_report.json').read_text())
    marks = json.loads((root/'milestone_marks.json').read_text())
    for artifact in (perf, marks):
        if validate_strategy_ledger(artifact.get('strategy_ledger')) != expected:
            raise ValueError('Strategy artifacts disagree on the full ledger')
    if perf.get('bet_log') != expected['records'] or marks.get('records') != expected['records'] or [row.get('strategy_record') for row in marks.get('matches', [])] != expected['records']:
        raise ValueError('Strategy artifact records/order/financial fields differ')
    pdfinfo, pdftotext = shutil.which('pdfinfo'), shutil.which('pdftotext')
    if not pdfinfo or not pdftotext:
        raise ValueError('Canonical PDF verification requires Poppler pdfinfo and pdftotext')
    info = subprocess.run([pdfinfo, str(root/'performance_report.pdf')], check=True, capture_output=True, text=True).stdout
    metadata = {line.split(':',1)[0].strip():line.split(':',1)[1].strip() for line in info.splitlines() if ':' in line}
    expected_subject = f"ledger_id={expected['ledger_id']}; as_of={expected['as_of']}"
    if not metadata or metadata.get('Subject') != expected_subject or metadata.get('Keywords') != 'book_version_id='+str(expected.get('book_version',{}).get('version_id','')):
        raise ValueError('PDF metadata belongs to a different strategy ledger')
    # The PDF carries the canonical records hash, including prices, stakes, exits,
    # all position amounts and three cumulatives; visible row IDs must be ordered.
    text = subprocess.run([pdftotext, '-layout', str(root/'performance_report.pdf'), '-'], check=True, capture_output=True, text=True).stdout
    import re
    chunks = re.split(r'\n\s*\n', text)
    cursor = 0
    def signed(cents):
        value=float(cents)/100
        return ('+' if value>=0 else '-')+f'${abs(value):,.3f}'
    for record in expected['records']:
        index = text.find(str(record['fixture_id']), cursor)
        if index < 0:
            raise ValueError('PDF fixture sequence is incomplete or reordered')
        cursor = index + len(str(record['fixture_id']))
        blocks = [chunk for chunk in chunks if re.search(r'(?<!\d)'+str(record['fixture_id'])+r'(?!\d)', chunk)]
        if len(blocks) != 1:
            raise ValueError('PDF fixture financial block is ambiguous')
        tokens = []
        for ip in (False,True):
            if not (record.get('inplay_side') if ip else record.get('bet')):
                continue
            tokens.append(f"${float(record['inplay_stake_usd' if ip else 'stake_usd']):,.3f}")
            entry = record['inplay_entry_cents' if ip else 'entry_cents']
            tokens.append(f'{float(entry):.1f}¢')
            exit_ = record.get('inplay_exit' if ip else 'smart_exit')
            won = record.get('inplay_won' if ip else 'won')
            terminal = (100.0 if won is True else 0.0 if won is False else None) if ip else record.get('settle_cents')
            terminal = exit_.get('sold_c') if exit_ else terminal
            if terminal is not None:
                tokens.append(f'{float(terminal):.1f}¢')
            pnl = record.get('inplay_pnl_cents' if ip else 'realized_pnl_cents')
            if pnl is not None:
                tokens.append(signed(pnl))
        tokens += [signed(record[key]) for key in ('pre_cum_pnl_cents','inplay_cum_pnl_cents','combined_cum_pnl_cents') if record.get(key) is not None]
        compact = re.sub(r'\s+','',blocks[0])
        if any(token not in compact for token in tokens):
            raise ValueError('PDF row financial values differ from the canonical record: '+str(record['fixture_id']))
    return {'ledger_id': expected['ledger_id'], 'as_of':expected['as_of'],
            'n_records':len(expected['records']), 'pdf_pages':int(metadata['Pages'])}


def build_pdf(rep: PerformanceReport, output_path: str, *, as_of: str = "") -> str:
    """Render only the shared smart-timing strategy snapshot; never recompute a trade."""
    from html import escape
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, Spacer
    from prediction_market_soccer.ops import pdf_style as ps
    from prediction_market_soccer.util.strategy_ledger import validate_strategy_ledger

    ledger=validate_strategy_ledger(rep.strategy_ledger)
    if ledger['records'] != rep.bet_log:
        raise ValueError('PDF records differ from the published strategy ledger')
    records,summary=ledger['records'],ledger['summary']
    story=[]
    ps.title_block(story,'足球智能择时 — 统一策略账本',
                   f"{summary['n_matches']} 场 · {summary['n_legs']} 笔策略腿 · 仓位毛收益 USD（未扣费用）")
    story.append(Paragraph('账本时间：'+escape(ledger['as_of']),ps.note_style))
    story.append(Paragraph('账本版本：'+ledger['ledger_id'],ps.S('ledger_version',fontSize=6.5,leading=9)))
    story.append(Spacer(1,8))
    story.append(Paragraph('准确度盈亏的智能择时视图、价格轨迹与本 PDF 读取同一份记录。报价单位为每张合约美分（¢，1 位小数）；下注金额、仓位收益和累计收益为美元（$，3 位小数）。',ps.note_style))

    def money(cents, signed=True):
        if cents is None: return '—'
        dollars=float(cents)/100
        return f"{'+' if signed and dollars>=0 else '-'}${abs(dollars):,.3f}" if signed else f'${dollars:,.3f}'

    def cash(value):
        return '—' if value is None else f'${float(value):,.3f}'

    def price(cents):
        return '—' if cents is None else f'{float(cents):.1f}¢'

    def colored(cents):
        if cents is None: return '—'
        color='#1a7a4a' if cents>0 else ('#c0392b' if cents<0 else '#555555')
        return f'<font color="{color}"><b>{money(cents)}</b></font>'

    ps.section(story,'策略结果（按每笔仓位的已实现毛收益判断盈利、亏损或持平）')
    totals=[[ps.H('策略'),ps.H('笔数'),ps.H('盈利 / 亏损 / 持平'),ps.H('未知'),ps.H('仓位毛收益 USD','RIGHT')]]
    for key,label in [('pre','赛前择时'),('inplay','赛中择时'),('combined','全部策略腿')]:
        v=summary[key]
        totals.append([ps.C(label),ps.C(str(v['n'])),ps.C(f"{v['profit']} / {v['loss']} / {v['flat']}"),
                       ps.C(str(v['unknown'])),ps.C(colored(v['pnl_cents']),'RIGHT')])
    story.append(ps.make_table(totals,[3*cm,1.3*cm,5*cm,1.3*cm,6.4*cm]))

    def leg_text(row, inplay=False):
        entered=bool(row.get('inplay_side')) if inplay else bool(row.get('bet'))
        if not entered: return '<font color="#777777">无已记录下注</font>'
        label=row.get('inplay_side_team') if inplay else row.get('pick_team')
        label=label or (row.get('inplay_side') if inplay else row.get('pick')) or '—'
        stake=row.get('inplay_stake_usd') if inplay else row.get('stake_usd')
        entry=row.get('inplay_entry_cents') if inplay else row.get('entry_cents')
        milestone=row.get('inplay_milestone') if inplay else 'PRE'
        exit_=row.get('inplay_exit') if inplay else row.get('smart_exit')
        won=row.get('inplay_won') if inplay else row.get('won')
        pnl=row.get('inplay_pnl_cents') if inplay else row.get('realized_pnl_cents')
        if exit_:
            ending=f"{escape(str(exit_.get('sold_min','—')))}′ 卖出 {price(exit_.get('sold_c'))}"
        else:
            ending='结算赢' if won is True else ('结算输' if won is False else '结算未知')
            settlement=(100.0 if won is True else (0.0 if won is False else None)) if inplay else row.get('settle_cents')
            ending+=' '+price(settlement)
        return (f"<b>{escape(str(label))}</b> · {escape(str(milestone or '—'))}<br/>"
                f"金额 {cash(stake)} · 入场 {price(entry)}<br/>{ending}<br/>毛收益 {colored(pnl)}")

    ps.section(story,'逐场记录（与智能择时及价格轨迹相同顺序）')
    table=[[ps.H('日期 / ID'),ps.H('比赛'),ps.H('赛前下注 / 退出 / 收益'),ps.H('赛中下注 / 退出 / 收益'),
            ps.H('赛前累计 USD','RIGHT'),ps.H('赛中累计 USD','RIGHT'),ps.H('合计累计 USD','RIGHT')]]
    for row in records:
        matchup=f"{escape(str(row.get('home','—')))}<br/>{escape(str(row.get('score','—')))}<br/>{escape(str(row.get('away','—')))}"
        table.append([ps.C(f"{escape(str(row.get('date',''))[5:])}<br/>{row['fixture_id']}"),ps.C(matchup),
                      ps.C(leg_text(row)),ps.C(leg_text(row,True)),
                      ps.C(colored(row.get('pre_cum_pnl_cents')),'RIGHT'),
                      ps.C(colored(row.get('inplay_cum_pnl_cents')),'RIGHT'),
                      ps.C(colored(row.get('combined_cum_pnl_cents')),'RIGHT')])
    if records:
        story.append(ps.make_table(table,[1.55*cm,3.0*cm,3.45*cm,3.45*cm,1.85*cm,1.85*cm,1.85*cm]))
    else:
        story.append(Paragraph('尚无已记录策略下注。',ps.note_style))
    doc=ps.new_doc(output_path)
    doc.title='足球智能择时 — 统一策略账本'
    doc.subject=f"ledger_id={ledger['ledger_id']}; as_of={ledger['as_of']}"
    doc.keywords='book_version_id='+str(ledger.get('book_version',{}).get('version_id',''))
    doc.build(story)
    return output_path


@writer
def publish_strategy_views(conn, *, report=None):
    """Publish one report/price-track/PDF group; caller holds the refresh_all lock.

    A supplied report is an existing snapshot and is never recalculated. This helper
    owns its ExportStage and must not be called from inside another staged refresh.
    """
    from prediction_market_soccer.ops.export_stage import ExportStage
    from prediction_market_soccer.ops.run_status import write_both,atomic_bytes
    from prediction_market_soccer.ops import milestone_export
    from prediction_market_soccer.util.strategy_ledger import validate_strategy_ledger
    with ExportStage() as stage:
        rep=report if report is not None else build(conn)
        ledger=validate_strategy_ledger(rep.strategy_ledger)
        from prediction_market_soccer.util.frozen_strategy_store import read_book
        if ledger != read_book(conn)['ledger']:
            raise ValueError('Only the active frozen book can be published; use the explicit version workflow to replace it')
        write_both('performance_report.json',asdict(rep))
        marks=milestone_export.build(conn,freeze=False,ledger=ledger)
        if validate_strategy_ledger(marks.get('strategy_ledger')) != ledger:
            raise ValueError('Price track does not contain this report strategy ledger')
        write_both('milestone_marks.json',marks)
        pdf=CONFIG.paths.output/'performance_report.pdf'
        build_pdf(rep,str(pdf))
        atomic_bytes(CONFIG.paths.frontend_data/pdf.name,pdf.read_bytes())
        for directory in (CONFIG.paths.output, CONFIG.paths.frontend_data):
            validate_strategy_views(directory, ledger)
        stage.promote()
    return rep


@writer
def main() -> None:
    import argparse
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ops.proc_lock import acquire,release
    ap=argparse.ArgumentParser(description='Publish the unified smart-timing report, price track and PDF')
    ap.add_argument('--pdf',action='store_true',help='Compatibility flag: the unified PDF is always published')
    ap.add_argument('--as-of',default='',help='Compatibility flag: all views use the actual ledger timestamp')
    ap.parse_args()
    if not acquire('refresh_all'):
        print('[performance] a full/report refresh is active — no artifacts published')
        return
    conn=None
    try:
        conn=store.init_db()
        rep=publish_strategy_views(conn)
        ledger=rep.strategy_ledger
        print('SMART-TIMING STRATEGY REPORT')
        print(f"  ledger: {ledger['ledger_id']}  as_of: {ledger['as_of']}")
        print(f"  matches: {ledger['summary']['n_matches']}  strategy legs: {ledger['summary']['n_legs']}")
        for key in ('pre','inplay','combined'):
            row=ledger['summary'][key]
            pnl=f"${row['pnl_usd']:+.3f}" if row['pnl_usd'] is not None else 'unavailable'
            print(f"  {key}: {row['profit']} profit / {row['loss']} loss / {row['flat']} flat; gross {pnl}")
        print('  Published performance_report.json, milestone_marks.json and performance_report.pdf to both directories')
    finally:
        if conn is not None: conn.close()
        release('refresh_all')


if __name__ == '__main__':
    main()
