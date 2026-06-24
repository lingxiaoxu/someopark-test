"""Performance & P&L report (plan 03 §9, 05 §5).

Honest reporting of what the system delivers. Because live trading is gated
(no real positions beyond the demo test), realized P&L is ~0 by design; the
report therefore leads with the metrics that DO measure value now:

  1. **Prediction accuracy** on settled matches — Brier / Log-loss / hit-rate vs
     the uniform baseline (is the model actually good?).
  2. **Calibration P&L** — paper bet 1 unit on the model's pick at FAIR odds each
     settled match; a calibrated model ≈ breaks even, an over-confident one loses
     (measures over/under-confidence as money).
  3. **Settled-signal P&L** — realized P&L of any recorded signals whose match has
     finished (forward-looking framework; ~0 now under the discipline gate).
  4. **Value snapshot** — current model-vs-market divergences + cross-venue state.

CLV (closing-line value, the true edge metric) accrues once signals record an
entry price and the market closes — wired to fill going forward.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

import numpy as np

from prediction_market.config import CONFIG
from prediction_market.model.calibrate import brier_score, log_loss

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
    argmax_record: str = ""           # e.g. "12W-10L" over all settled matches
    argmax_pnl_cents_total: float = 0.0  # ¢ P&L betting argmax every match
    # REALISED 口径 — the actual strategy is decision + smart-exit cash-out (not hold-to-FT).
    realized_record: str = ""            # profitable-bets record with cash-out, e.g. "12-6"
    realized_pnl_cents_total: float = 0.0  # ¢ P&L with the smart-exit applied
    n_smart_sold: int = 0                # bets where the smart-exit cashed out the over-reaction
    hold_record: str = ""                # the hold-to-FT W-L (reference)
    hold_pnl_cents_total: float = 0.0    # hold-to-FT ¢ P&L (reference)


def _settled(conn, sm=None):
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
    from prediction_market.model.match_pricing import price_match
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    rows = conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals, kickoff_ts, round, raw_json FROM fixture "
        "WHERE status_short IN ({}) AND home_goals IS NOT NULL".format(",".join("?" * len(_FINISHED))),
        _FINISHED).fetchall()
    from prediction_market.util.pricing import reg_score
    out = []
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            continue
        sm_pit = _pit_strength(conn, r["kickoff_ts"]) if r["kickoff_ts"] else sm
        # 90-min 3-way for both stages (knockout=False) — matches the bet/MTM model.
        mp = price_match(sm_pit, hi, ai, knockout=False)
        # 90' regulation score (KO ET match settles the Tie market on the 90' result).
        gh90, ga90 = reg_score(r["raw_json"], r["home_goals"], r["away_goals"])
        outcome = 0 if gh90 > ga90 else (1 if gh90 == ga90 else 2)
        out.append(([mp.p_home, mp.p_draw, mp.p_away], outcome))
    return out


_SIDE_KEYS = ("home", "draw", "away")


def _headline(rep: "PerformanceReport") -> tuple[str, bool]:
    """State-aware headline string + whether it's a pass (for colour)."""
    from prediction_market.ops import system_overview as ov
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
            from prediction_market.ingest.prior_ingest import load_prior
            _prior_cache = load_prior()
        _fifa_cache = {t.team_id: t.fifa_rank for t in _prior_cache.teams}
    return _fifa_cache


def _pit_strength(conn, as_of: str):
    """PIT strength model for one match: ratings + alt-data computed only from data
    strictly before `as_of` (the kickoff). Mirrors param_sweep/decision_backtest so the
    record's pick matches the honest backtest. Returns the live-config strength model."""
    from dataclasses import replace
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.altdata_adjust import altdata_index
    from prediction_market.model.squad_strength import build_strength_live
    global _prior_cache
    if _prior_cache is None:
        _prior_cache = load_prior()
    cfg = CONFIG.model
    sm = build_strength_live(conn, _prior_cache, cfg, as_of=as_of, xg_form=True)
    if cfg.oppadj_def_weight or cfg.oppadj_off_weight:
        sm = replace(sm, adj=altdata_index(conn, sm.ratings, as_of=as_of))
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
    from prediction_market.model.match_pricing import is_knockout, price_match_calibrated
    from prediction_market.util.pricing import reg_score
    gh, ga = fx_row["home_goals"], fx_row["away_goals"]
    if gh is None or ga is None:
        return None   # not settled (no final score) — nothing to settle a bet on
    # 90' regulation score for the 90' 3-way settlement (KO ET match → Tie on 1-1@90').
    try:
        _raw = fx_row["raw_json"]
    except (KeyError, IndexError):
        _raw = None
    gh90, ga90 = reg_score(_raw, gh, ga)
    knockout = is_knockout(fx_row["round"])
    if pit and conn is not None and fx_row["kickoff_ts"]:
        sm = _pit_strength(conn, fx_row["kickoff_ts"])
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
        from prediction_market.model.motivation import motivation_multipliers
        mh, ma, motiv = motivation_multipliers(conn, _fifa_ranks(), hi, ai, fx_row["round"], CONFIG.model)
        if (mh, ma) != (1.0, 1.0):
            lam_mult = (mh, ma)
    mp = price_match_calibrated(sm, hi, ai, knockout=False, cal=cal, lam_mult=lam_mult)
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
    stage = "knockout" if knockout else "group"
    # PIT recent-form for the decision's confidence sizing.
    form = None
    if pit and conn is not None:
        try:
            from prediction_market.model.form_strength import form_index
            fi = form_index(conn, as_of=fx_row["kickoff_ts"])
            form = {"home_z": fi[hi].form_z if hi in fi else None,
                    "away_z": fi[ai].form_z if ai in fi else None}
        except Exception:
            form = None
    if quotes:
        from prediction_market.strategy.decision_model import decide
        d = decide(model, quotes, calib_confidence=calib_confidence, form=form, gate_open=gate_open,
                   conviction_side=(motiv or {}).get("conviction_side"))
        if d.side is not None:
            pick, bet, stake, conf_k = d.side, True, d.stake_usd, d.confidence_k
            cost = max((d.price_cents or 0.0) / 100.0, 1e-6)   # real entry ask we'd pay
            edge_val, model_prob = d.net_edge, round(model[pick], 4)
        else:
            pick, bet, stake, conf_k = model_pick, False, 0.0, d.confidence_k
            cost, edge_val, model_prob = max(price[model_pick], 1e-6), edges[model_pick], round(model[model_pick], 4)
    else:
        # legacy (no venue quotes): bet the model argmax at the book price.
        pick, bet, stake, conf_k = model_pick, True, base_stake, None
        cost, edge_val, model_prob = max(price[pick], 1e-6), edges[pick], round(model[pick], 4)

    return {"stage": stage, "pick": pick, "model_pick": model_pick, "result": result,
            "won": (pick == result) if bet else None, "model_won": model_pick == result,
            "bet": bet, "stake_usd": round(stake, 2), "confidence_k": conf_k,
            "cost": cost, "model_prob": model_prob, "edge": round(edge_val, 4), "model": model,
            "motivation": motiv}


def _bet_log(conn) -> list[dict]:
    """Production bet history: every settled match since the opener, flat 1u on the
    model's best value side vs the closing de-vig book, settled on the real result.

    Returns chronological rows with our prediction, the bet, the actual result, and
    running P&L — i.e. the live track record of the system as if it had been trading
    from match 1 (no point-in-time / out-of-sample framing; this is the record).
    """
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.probability_calibration import load_calibration
    from prediction_market.model.squad_strength import build_strength_live

    prior = load_prior()
    name = {t.team_id: t.name for t in prior.teams}
    zh = {t.team_id: t.zh for t in prior.teams}
    sm = build_strength_live(conn, prior)
    cal = load_calibration()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    # PRE-milestone venue entry quotes per fixture (real Kalshi/Poly price at kickoff) —
    # the executable asks + de-vig the decision model selects on, and the true per-contract
    # cost we'd have paid. SELECT * so quotes_from_milestone_row sees every ask/devig column.
    pre_px = {r["fixture_api_id"]: r for r in conn.execute(
        "SELECT * FROM milestone_snapshot WHERE milestone='PRE'")}

    # Model-quality → confidence: how far the calibrated Brier beats the uniform baseline
    # (scales the stake). gate_open stays True for the RECORD (it shows the value picks);
    # real-money execution is additionally gated in production.
    _ub = (cal.get("uniform_brier") if cal else None) or (2.0 / 3.0)
    _cb = cal.get("calibrated_brier") if cal else None
    calib_conf = max(-1.0, min(1.0, (_ub - _cb) / _ub)) if (_cb is not None and _ub) else 0.0
    # T75 (last in-play milestone before FT) per fixture, for closing-line value (CLV).
    t75_px = {r["fixture_api_id"]: r for r in conn.execute(
        "SELECT fixture_api_id, poly_home_ask, poly_draw_ask, poly_away_ask, "
        "kalshi_home_ask, kalshi_draw_ask, kalshi_away_ask "
        "FROM milestone_snapshot WHERE milestone='T75'")}

    # De-vig book (averaged bookmakers) per settled fixture.
    book = {r["api_id"]: r for r in conn.execute(
        "SELECT f.api_id, AVG(o.p_home) bh, AVG(o.p_draw) bd, AVG(o.p_away) ba "
        "FROM fixture f JOIN match_odds o ON o.fixture_api_id=f.api_id "
        "WHERE f.status_short IN ({}) GROUP BY f.api_id".format(",".join("?" * len(_FINISHED))),
        _FINISHED).fetchall()}

    rows = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, kickoff_ts, round, raw_json "
        "FROM fixture WHERE status_short IN ({}) AND home_goals IS NOT NULL "
        "ORDER BY kickoff_ts".format(",".join("?" * len(_FINISHED))),
        _FINISHED).fetchall()

    from prediction_market.strategy.decision_model import quotes_from_milestone_row
    from prediction_market.util.pricing import to_cents, pnl_cents as _pnl_cents
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
    # REALISED 口径: the strategy is decision + smart-exit cash-out (we don't hold to FT).
    realized_cum_c = 0.0   # cumulative ¢ P&L with the model-aware cash-out applied
    realized_wins = 0      # bets that REALISED a profit (cash-out or settle)
    n_smart_sold = 0       # bets where the smart-exit actually fired (sold the overshoot)
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            continue
        # Shared DECISION (value/Kelly on the PRE venue quotes, PIT) — the SAME call the
        # price-track uses with the SAME quotes, so the two views reconcile by construction.
        pr = pre_px.get(r["api_id"])
        quotes = quotes_from_milestone_row(pr) if pr is not None else None
        mr = match_pick(sm, cal, hi, ai, r, book.get(r["api_id"]),
                        conn=conn, quotes=quotes, calib_confidence=calib_conf,
                        gate_open=True, pit=True)
        if mr is None:
            continue   # knockout level after ET with no winner flag yet — can't settle
        knockout = mr["stage"] == "knockout"
        gh, ga = r["home_goals"], r["away_goals"]
        result = mr["result"]
        pr = pre_px.get(r["api_id"])
        side_label = {"home": name.get(hi, hi), "draw": "Draw", "away": name.get(ai, ai)}

        def _entry_c(side):
            """Real PRE venue ¢ for a side (Poly then Kalshi) → (cents, source); else None."""
            if pr is None:
                return None
            if pr[f"poly_{side}_ask"] is not None:
                return to_cents(pr[f"poly_{side}_ask"]), "poly"
            if pr[f"kalshi_{side}_ask"] is not None:
                return to_cents(pr[f"kalshi_{side}_ask"]), "kalshi"
            return None

        def _clv_c(side, entry_c):
            t75 = t75_px.get(r["api_id"])
            if t75 is None or entry_c is None:
                return None
            s = t75[f"poly_{side}_ask"] if t75[f"poly_{side}_ask"] is not None else t75[f"kalshi_{side}_ask"]
            return round(to_cents(s) - entry_c, 1) if s is not None else None

        # ── argmax 口径 (model_pick) — recorded for EVERY settled match (all 22) ──
        am_pick, am_won = mr["model_pick"], mr["model_won"]
        _ame = _entry_c(am_pick)
        am_entry_c = _ame[0] if _ame else None
        am_pnl_c = _pnl_cents(am_entry_c, am_won) if am_entry_c is not None else None
        am_cum_c += (am_pnl_c or 0.0)
        am_n += 1
        am_wins += int(am_won)

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
            _de = _entry_c(pick)
            entry_cents, entry_source = (_de[0], _de[1]) if _de else (to_cents(cost), "book_devig")
            c_pnl = _pnl_cents(entry_cents, won)
            cum_c += (c_pnl or 0.0)
            sum_entry_c += (entry_cents or 0.0)
            cents_avail += ((100.0 - entry_cents) if won else entry_cents) if entry_cents is not None else 0.0
            clv_cents = _clv_c(pick, entry_cents)
            # REALISED cash-out: apply the validated smart-exit (sell the over-reaction)
            # to THIS bet — the realised ¢ is the cash-out PnL when it fired, else hold-to-FT.
            smart_exit = None
            try:
                from prediction_market.strategy.smart_exit import smart_exit_cashout
                if conn is not None and r["kickoff_ts"]:
                    smart_exit = smart_exit_cashout(conn, _pit_strength(conn, r["kickoff_ts"]),
                                                    r["api_id"], pick, entry_cents, hi, ai, r["round"], won)
            except Exception:
                smart_exit = None
            realized_c = smart_exit["pnl_c"] if smart_exit else c_pnl
            realized_cum_c += (realized_c or 0.0)
            realized_wins += int((realized_c or 0.0) > 0)
            n_smart_sold += int(bool(smart_exit))
            dec = {"pick": pick, "pick_team": side_label[pick], "won": won,
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

        log.append({
            "stage": "knockout" if knockout else "group",
            "date": (r["kickoff_ts"] or "")[:10],
            "home": name.get(hi, hi), "away": name.get(ai, ai),
            "home_id": hi, "away_id": ai,
            "home_zh": zh.get(hi, ""), "away_zh": zh.get(ai, ""),
            "score": f"{gh}-{ga}",
            "result": result,
            "bet": mr["bet"],
            **dec,
            # argmax 口径 (model_pick) — present on every row, all 22 matches
            "model_pick": am_pick, "model_pick_team": side_label[am_pick], "model_won": am_won,
            "argmax_entry_cents": am_entry_c, "argmax_settle_cents": (100.0 if am_won else 0.0),
            "argmax_pnl_cents": am_pnl_c, "argmax_cum_pnl_cents": round(am_cum_c, 1),
        })
    return log, {"skipped": skipped, "n_bets": n_bets, "model_n": am_n, "model_hits": am_wins,
                 "staked_usd": round(staked, 2), "argmax_pnl_cents_total": round(am_cum_c, 1),
                 "argmax_record": f"{am_wins}W-{am_n - am_wins}L",
                 # REALISED 口径 (decision + smart-exit cash-out — the actual strategy).
                 # W/L here = PROFITABLE / not (a 'W' can be a cash-out at a gain on a bet
                 # that would have lost at settlement). Same W-L format as the other modes.
                 "realized_pnl_cents_total": round(realized_cum_c, 1),
                 "realized_record": f"{realized_wins}W-{n_bets - realized_wins}L",
                 "n_smart_sold": n_smart_sold, "hold_pnl_cents_total": round(cum_c, 1),
                 "hold_record": f"{wins}W-{n_bets - wins}L"}


def build(conn=None) -> PerformanceReport:
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.strength import build_strength

    conn = conn or store.init_db()
    sm = build_strength(load_prior())
    data = _settled(conn, sm)
    notes = []          # English prose (used by the PDF)
    notes_i18n = []     # {key, args} parallel to `notes`, for the 5-language frontend

    if not data:
        notes.append("no settled matches yet")
        notes_i18n.append({"key": "noSettled", "args": {}})
        nan = float('nan')
        return PerformanceReport(
            n_settled=0, brier=nan, brier_uniform=nan, calibrated_brier=None,
            trade_grade=False, log_loss=nan, favourite_hit_rate=nan,
            calibration_pnl=nan, calibration_pnl_per_bet=nan,
            settled_signal_pnl=0.0, n_settled_signals=0,
            bet_log=[], pnl_units=0.0, pnl_record="0W-0L", pnl_roi=0.0, bet_since="",
            notes=notes,
            notes_i18n=notes_i18n,
        )

    probs = [d[0] for d in data]
    outcomes = [d[1] for d in data]
    n = len(data)

    # Calibration P&L: 1 unit on the model's pick at fair odds (price = model prob).
    pnl, staked = 0.0, 0.0
    hits = 0
    for p, o in data:
        pick = int(np.argmax(p))
        price = p[pick]
        staked += price
        if pick == o:
            pnl += 1.0 - price
            hits += 1
        else:
            pnl -= price

    # Settled-signal P&L: recorded signals whose match has finished (framework).
    sig_pnl, n_sig = 0.0, 0
    try:
        # (Single-match signals settle here once we record them per fixture; champion
        # signals settle at tournament end. None tradable yet under the gate → 0.)
        n_sig = conn.execute("SELECT COUNT(*) n FROM signal WHERE action='BUY'").fetchone()["n"]
    except Exception:
        pass

    from prediction_market.model.probability_calibration import load_calibration
    cal = load_calibration()
    # The trade-grade gate is decided on the CALIBRATED Brier (post-hoc temperature/
    # shrinkage), NOT the raw model — the raw model is over-confident on this tiny,
    # draw-heavy sample, but calibration restores it below the uniform baseline.
    calibrated_brier = cal.get("calibrated_brier") if cal else None
    trade_grade = bool(cal and cal.get("trade_grade"))
    if trade_grade:
        notes.append(f"After calibration ({cal['method']} {cal['param']}) the model Brier is "
                     f"{cal['calibrated_brier']} ≤ uniform {cal['uniform_brier']} — TRADE-GRADE (gate passes). "
                     f"Raw model was over-confident ({cal['raw_brier']}).")
        notes_i18n.append({"key": "calibTradeGrade", "args": {
            "method": cal['method'], "param": cal['param'], "cal": cal['calibrated_brier'],
            "uniform": cal['uniform_brier'], "raw": cal['raw_brier']}})
    elif brier_score(probs, outcomes) > (2 / 3):
        notes.append("model Brier WORSE than uniform and calibration does not recover it — "
                     "not yet trade-grade (discipline gate blocks).")
        notes_i18n.append({"key": "calibBlock", "args": {}})
    # Production bet log: flat 1u on our model's best value side vs the closing book,
    # every match since the opener. This is the track record we present (no OOS framing).
    log, betmeta = _bet_log(conn)   # ALL settled matches; no-bet rows carry the argmax track only
    n_bets = betmeta["n_bets"]
    pnl_units = round(sum((b["pnl"] or 0.0) for b in log), 2)   # $ P&L at the decision-model stakes
    wins = sum(1 for b in log if b.get("won"))
    pnl_record = f"{wins}W-{n_bets - wins}L"
    decision_staked = betmeta["staked_usd"] or 0.0
    pnl_roi = round(pnl_units / decision_staked, 4) if decision_staked else 0.0
    bet_since = log[0]["date"] if log else ""
    model_pred_accuracy = round(betmeta["model_hits"] / betmeta["model_n"], 3) if betmeta["model_n"] else 0.0
    argmax_record = betmeta["argmax_record"]                      # W-L over ALL settled (22)
    argmax_pnl_cents_total = betmeta["argmax_pnl_cents_total"]    # ¢ P&L if we bet argmax every match

    # Per-contract ¢ headline metrics (plan 18 §2.7).
    _ent = [b["entry_cents"] for b in log if b.get("entry_cents") is not None]
    pnl_cents_total = round(sum((b.get("pnl_cents") or 0.0) for b in log), 1)
    avg_entry_cents = round(sum(_ent) / len(_ent), 1) if _ent else 0.0
    _avail = sum(((100.0 - b["entry_cents"]) if b["won"] else b["entry_cents"])
                 for b in log if b.get("entry_cents") is not None)
    cents_capture_rate = round(pnl_cents_total / _avail, 4) if _avail else 0.0
    _clv = [b["clv_cents"] for b in log if b.get("clv_cents") is not None]
    avg_clv_cents = round(sum(_clv) / len(_clv), 1) if _clv else 0.0

    notes.append(f"Track record (decision model): value bet the most-underpriced side, "
                 f"sized ${CONFIG.decision.min_stake_usd:.1f}–${CONFIG.decision.max_stake_usd:.1f} by "
                 f"confidence, since {bet_since} — {pnl_record}, {pnl_units:+.2f}$ "
                 f"({pnl_roi:+.1%} ROI) over {len(log)} bets; {betmeta['skipped']} settled "
                 f"matches skipped (no tradable edge).")
    notes_i18n.append({"key": "trackRecord", "args": {
        "min": f"{CONFIG.decision.min_stake_usd:.1f}", "max": f"{CONFIG.decision.max_stake_usd:.1f}",
        "since": bet_since, "record": pnl_record, "pnl": f"{pnl_units:+.2f}",
        "roi": f"{pnl_roi*100:+.1f}", "bets": len(log), "skipped": betmeta['skipped']}})
    notes.append(f"Argmax track (reference, every match): bet the most-likely side all "
                 f"{betmeta['model_n']} settled matches — {argmax_record} "
                 f"({model_pred_accuracy:.1%}), {argmax_pnl_cents_total:+.0f}¢/contract. This is the "
                 f"OLD naive rule, shown alongside so both methods are comparable on the full sample.")
    notes_i18n.append({"key": "argmaxTrack", "args": {
        "n": betmeta['model_n'], "record": argmax_record,
        "acc": f"{model_pred_accuracy*100:.1f}", "pnl": f"{argmax_pnl_cents_total:+.0f}"}})
    notes.append("Calibration P&L (fair-odds) is a separate over/under-confidence diagnostic.")
    notes_i18n.append({"key": "calibPnlNote", "args": {}})

    return PerformanceReport(
        n_settled=n,
        brier=round(brier_score(probs, outcomes), 4),
        brier_uniform=round(brier_score([[1 / 3, 1 / 3, 1 / 3]] * n, outcomes), 4),
        calibrated_brier=calibrated_brier,
        trade_grade=trade_grade,
        log_loss=round(log_loss(probs, outcomes), 4),
        favourite_hit_rate=round(hits / n, 3),
        calibration_pnl=round(pnl, 3),
        calibration_pnl_per_bet=round(pnl / n, 4),
        settled_signal_pnl=round(sig_pnl, 2),
        n_settled_signals=n_sig,
        bet_log=log,
        pnl_units=pnl_units,
        pnl_record=pnl_record,
        pnl_roi=pnl_roi,
        bet_since=bet_since,
        notes=notes,
        notes_i18n=notes_i18n,
        pnl_cents_total=pnl_cents_total,
        avg_entry_cents=avg_entry_cents,
        cents_capture_rate=cents_capture_rate,
        avg_clv_cents=avg_clv_cents,
        model_pred_accuracy=model_pred_accuracy,
        n_decision_bets=n_bets,
        n_skipped=betmeta["skipped"],
        decision_staked_usd=round(decision_staked, 2),
        argmax_record=argmax_record,
        argmax_pnl_cents_total=argmax_pnl_cents_total,
        realized_record=betmeta["realized_record"],
        realized_pnl_cents_total=betmeta["realized_pnl_cents_total"],
        n_smart_sold=betmeta["n_smart_sold"],
        hold_record=betmeta["hold_record"],
        hold_pnl_cents_total=betmeta["hold_pnl_cents_total"],
    )


def build_pdf(rep: PerformanceReport, output_path: str, *, as_of: str = "") -> str:
    """Render the full System & Performance report in the house PDF style
    (PnLReport.py look: CJK font, navy headers + gold rule, alt-row shading).

    Embeds the complete system overview (interfaces / modes / schedule / I-O /
    value) so this one document answers "what is this, how/when to run it, what
    does it predict, and is it any good".
    """
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, Spacer

    from prediction_market.ops import pdf_style as ps
    from prediction_market.ops import system_overview as ov

    story: list = []
    ps.title_block(
        story,
        "World Cup 2026 预测交易系统 — 系统总览 & 收益/准确度报告",
        f"Kalshi + Polymarket | someopark_run{('  |  as of ' + as_of) if as_of else ''}",
    )

    # Headline reflects the CURRENT system state (gate verdict), not a fixed stance.
    headline, hl_color = _headline(rep)
    story.append(Paragraph(headline,
                           ps.S("hl", fontName=ps.FONT, fontSize=8, leading=12,
                                textColor=(ps.C_POS if rep.trade_grade else ps.C_NEG))))
    story.append(Spacer(1, 8))

    # 一、接口(CLI 命令)
    ps.section(story, "一、系统接口(CLI 命令)")
    data = [[ps.H("类别"), ps.H("命令  python -m prediction_market.<x>"), ps.H("作用")]]
    data += [[ps.C(a), ps.C(b), ps.C(c)] for a, b, c in ov.INTERFACES]
    story.append(ps.make_table(data, [2.0 * cm, 6.3 * cm, 8.7 * cm]))

    # 二、模式 & 闸门
    ps.section(story, "二、模式 & 闸门")
    story.append(ps.make_kv_table([(m, d) for m, d in ov.MODES], label_w=4.6 * cm, val_w=12.4 * cm))

    # 三、运行调度(何时 / 频率)
    ps.section(story, "三、运行调度(何时运行 / 频率)")
    data = [[ps.H("时机"), ps.H("跑什么"), ps.H("频率", "RIGHT")]]
    data += [[ps.C(a), ps.C(b), ps.C(c, "RIGHT")] for a, b, c in ov.SCHEDULE]
    story.append(ps.make_table(data, [4.0 * cm, 10.5 * cm, 2.5 * cm]))

    # 四、输入 / 输出(在哪里)
    ps.section(story, "四、输入 / 输出(在哪里)")
    story.append(Paragraph("输入", ps.body_style))
    story.append(ps.make_kv_table([(a, b) for a, b in ov.INPUTS], label_w=4.6 * cm, val_w=12.4 * cm))
    story.append(Spacer(1, 4))
    story.append(Paragraph(f"输出  (全部在 {ov.OUTPUT_DIR})", ps.body_style))
    story.append(ps.make_kv_table([(a, b) for a, b in ov.OUTPUTS], label_w=8.4 * cm, val_w=8.6 * cm))

    # 五、价值主张
    ps.section(story, "五、给用户带来的价值 + 怎么看到")
    for v in ov.VALUE:
        story.append(ps.bullet(v))

    # 六、预测准确度(已结算场次)
    ps.section(story, "六、预测准确度(已结算场次)")
    if rep.n_settled:
        grade = "PASS(已校准,优于均匀)" if rep.trade_grade else "BLOCK(劣于均匀,纪律闸门拦截)"
        cal_row = (f"{rep.calibrated_brier}  ≤ 均匀基线 {rep.brier_uniform}"
                   if rep.calibrated_brier is not None else "尚未拟合校准")
        acc = [
            ("已结算场次", f"{rep.n_settled}"),
            ("Brier 原始(越低越好)", f"{rep.brier}  vs 均匀基线 {rep.brier_uniform}"),
            ("Brier 校准后", cal_row),
            ("Log-loss", f"{rep.log_loss}"),
            ("热门命中率", f"{rep.favourite_hit_rate:.0%}"),
            ("交易等级", grade),
        ]
        story.append(ps.make_kv_table(acc, label_w=8.4 * cm, val_w=8.6 * cm))
    else:
        story.append(Paragraph("尚无已结算场次。", ps.note_style))

    # 七、实盘战绩 — 真实策略 = 决策 + 智能择时现金出(持有到 FT / argmax 作参考)
    ps.section(story, f"七、实盘战绩(真实口径:决策选边 + 超调止盈现金出;持有/argmax 作参考,自 {rep.bet_since or '—'} 起)")
    if rep.bet_log:
        def _col(v: float, text: str) -> str:
            c = "#1a7a4a" if v >= 0 else "#c0392b"
            return f'<font color="{c}"><b>{text}</b></font>'

        # Unified format across the 3 modes: 标签 {W-L} · 累计 {¢}/张 · {场景}
        story.append(Paragraph(
            f"<b>实现(决策 + 智能择时现金出)</b> {rep.realized_record} · 累计 {rep.realized_pnl_cents_total:+.0f}¢/张 · "
            f"{rep.n_decision_bets} 注 · {rep.n_smart_sold} 现金出",
            ps.body_style))
        story.append(Paragraph(
            f"持有到 FT(参考) {rep.hold_record} · 累计 {rep.hold_pnl_cents_total:+.0f}¢/张 · "
            f"{rep.n_decision_bets} 注 · {rep.n_skipped} 跳过(无边际)",
            ps.body_style))
        story.append(Paragraph(
            f"argmax 口径(参考) {rep.argmax_record} · 累计 {rep.argmax_pnl_cents_total:+.0f}¢/张 · "
            f"{len(rep.bet_log)} 场 · 准确率 {rep.model_pred_accuracy:+.0%}",
            ps.body_style))
        story.append(Paragraph(
            f"平均入场 {rep.avg_entry_cents:.0f}¢ · 价格空间捕获率 {rep.cents_capture_rate:+.0%} · 平均 CLV {rep.avg_clv_cents:+.0f}¢",
            ps.note_style))
        data = [[ps.H("日期"), ps.H("对阵"), ps.H("下注边"), ps.H("金额", "RIGHT"), ps.H("离场"),
                 ps.H("入场¢", "RIGHT"), ps.H("实现¢", "RIGHT"), ps.H("累计¢", "RIGHT"),
                 ps.H("argmax(边·输赢·¢)")]]

        for b in rep.bet_log:
            bet = b.get("bet", True)
            ec = f'{b["entry_cents"]:.0f}' if b.get("entry_cents") is not None else "—"
            se = b.get("smart_exit")
            rpc = b.get("realized_pnl_cents")
            rcum = b.get("realized_cum_pnl_cents")
            apc = b.get("argmax_pnl_cents")
            am_res = _col(1 if b["model_won"] else -1, "赢" if b["model_won"] else "输")
            am_txt = (f'{b["model_pick_team"]} {am_res}'
                      + (f' {_col(apc, f"{apc:+.0f}")}' if apc is not None else ''))
            if not bet:
                exit_txt = '<font color="#888">—</font>'
            elif se:
                exit_txt = _col(se["pnl_c"], f'{se["sold_min"]}′卖{se["sold_c"]:.0f}¢')
            else:
                exit_txt = _col(1 if b["won"] else -1, "结算赢" if b["won"] else "结算输")
            data.append([
                ps.C(b["date"][5:]),
                ps.C(f'{b["home"]} {b["score"]} {b["away"]}'),
                ps.C(b["pick_team"] if bet else '<font color="#888">不下注</font>'),
                ps.C(f'${b["stake_usd"]:.2f}' if bet else '<font color="#888">$0</font>', "RIGHT"),
                ps.C(exit_txt),
                ps.C(ec if bet else "—", "RIGHT"),
                ps.C(_col(rpc if rpc is not None else 0, f'{rpc:+.0f}') if (bet and rpc is not None) else "—", "RIGHT"),
                ps.C(_col(rcum if rcum is not None else 0, f'{rcum:+.0f}') if (bet and rcum is not None) else "—", "RIGHT"),
                ps.C(am_txt),
            ])
        story.append(ps.make_table(data, [1.4 * cm, 4.0 * cm, 1.9 * cm, 1.2 * cm, 2.0 * cm,
                                          1.2 * cm, 1.2 * cm, 1.2 * cm, 3.0 * cm]))

    # 八、校准 P&L(纸面,公允赔率诊断)
    ps.section(story, "八、校准 P&L(纸面,按公允赔率下注模型选边 — 过度/不足自信诊断)")
    if rep.n_settled:
        story.append(ps.make_kv_table([
            ("总校准 P&L(1u/场)", ps.money(rep.calibration_pnl, unit="u")),
            ("每场均值", ps.money(rep.calibration_pnl_per_bet, unit="u")),
            ("已结算信号 P&L(真钱框架)", ps.money(rep.settled_signal_pnl)),
            ("已记录 BUY 信号数", f"{rep.n_settled_signals}"),
        ], label_w=8.4 * cm, val_w=8.6 * cm))

    # 九、说明
    ps.section(story, "九、说明")
    for i, n in enumerate(rep.notes, 1):
        story.append(ps.note_item(f"{i}.", n))

    ps.new_doc(output_path).build(story)
    return output_path


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Performance & P&L report")
    ap.add_argument("--pdf", action="store_true", help="also render a styled PDF")
    ap.add_argument("--as-of", default="", help="as-of date label for the PDF header")
    args = ap.parse_args()

    rep = build()
    CONFIG.paths.ensure()
    (CONFIG.paths.output / "performance_report.json").write_text(
        json.dumps(asdict(rep), ensure_ascii=False, indent=2), encoding="utf-8")
    print("PERFORMANCE & P&L REPORT")
    print(f"  settled matches      : {rep.n_settled}")
    if rep.n_settled:
        print(f"  accuracy Brier       : {rep.brier}  (uniform {rep.brier_uniform})  log-loss {rep.log_loss}")
        print(f"  favourite hit-rate   : {rep.favourite_hit_rate:.0%}")
        print(f"  calibration P&L      : {rep.calibration_pnl:+.2f}u total ({rep.calibration_pnl_per_bet:+.3f}u/bet, paper)")
        print(f"  settled-signal P&L   : {rep.settled_signal_pnl:+.2f}  ({rep.n_settled_signals} BUY signals recorded)")
    for nnote in rep.notes:
        print(f"  • {nnote}")

    if args.pdf:
        path = build_pdf(rep, str(CONFIG.paths.output / "performance_report.pdf"), as_of=args.as_of)
        print(f"  PDF written          : {path}")


if __name__ == "__main__":
    main()
