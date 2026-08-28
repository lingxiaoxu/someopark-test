"""ops/settle_bets.py — the FROZEN bet ledger.

A settled match's bet must reflect ONLY information available before its kickoff. The
strength model is already point-in-time (``_pit_strength``), but the probability calibration
the reports applied was GLOBAL — refit daily over ALL settled matches, including matches that
occur AFTER a given one. Applying that calibration to a past match's pick is look-ahead, and
it makes the historical bet log (and its cumulative P&L) drift from day to day.

This module freezes each settled match's ``match_pick()`` output ONCE — priced with its own
point-in-time strength AND a calibration fit only on matches BEFORE its kickoff — into the
``settled_bet`` table. ``freeze_settled_bets()`` is append-only + idempotent: it writes only
matches not already frozen, so running it every refresh never rewrites history. The reports
read the frozen payload via ``frozen_pick()`` instead of recomputing, so the three views
(Accuracy/PnL, PriceTrack, PnL report) are stable — "a bet, once placed, never changes".

    python -m prediction_market_soccer.ops.settle_bets   →  freeze any newly-settled matches
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

_FINISHED = ("FT", "AET", "PEN")


def _pit_py(conn, cmap):
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
        _k = (r["kickoff_ts"][:10], _comp_of.get(r["league_id"]))
        if _k not in _day_cache:
            _day_cache[_k] = _pit_strength(conn, r["kickoff_ts"], _k[1])
        mp = price_match(_day_cache[_k], hi, ai)
        gh, ga = reg_score(r["raw_json"], r["home_goals"], r["away_goals"])
        out.append({"fid": r["api_id"], "kickoff": r["kickoff_ts"],
                    "P": [mp.p_home, mp.p_draw, mp.p_away],
                    "Y": 0 if gh > ga else (1 if gh == ga else 2)})
    return out


def _pit_cal(records, as_of):
    """Calibration fit ONLY on matches with kickoff strictly before ``as_of`` (needs >=3);
    None otherwise (earliest matches → the raw, uncalibrated model — the honest state when
    there was no calibration data yet)."""
    from prediction_market_soccer.model.probability_calibration import fit_calibration
    P = [r["P"] for r in records if r["kickoff"] < as_of]
    Y = [r["Y"] for r in records if r["kickoff"] < as_of]
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


def _event_timelines(conn, fid, home_api):
    """Cumulative (minute → score) and (minute → red count) timelines from fixture_event, so
    the live model can be priced at ANY match minute (not just the 5 fixed milestones). Own
    goals credit the other side; missed penalties ignored — SAME goal accounting as smart_exit."""
    goals, reds = [], []
    gh = ga = rh = ra = 0
    for e in conn.execute(
            "SELECT minute, team_api_id, type, detail FROM fixture_event "
            "WHERE fixture_api_id=? AND type IN ('Goal','Card') ORDER BY minute, seq", (fid,)):
        mn = e["minute"] or 0
        if e["type"] == "Goal":
            if (e["detail"] or "") == "Missed Penalty":
                continue
            if (e["team_api_id"] == home_api) ^ ((e["detail"] or "") == "Own Goal"):
                gh += 1
            else:
                ga += 1
            goals.append((mn, gh, ga))
        elif (e["detail"] or "") == "Red Card":
            if e["team_api_id"] == home_api:
                rh += 1
            else:
                ra += 1
            reds.append((mn, rh, ra))
    return goals, reds


def _state_at(timeline, mn, dims=2):
    """The last cumulative (h, a) tuple in ``timeline`` at or before match minute ``mn`` (0,0
    if none yet)."""
    h = a = 0
    for row in timeline:
        if row[0] <= mn:
            h, a = row[1], row[2]
        else:
            break
    return h, a


def _inplay_entry(conn, fx_row, hi, ai):
    """Causal (non-hindsight) in-play relative-value entry, frozen alongside the pre-match bet.

    Scans EVERY in-game price point in match-minute order — the Polymarket ``price_tick``
    series (~13/match) where it exists, else our own 5 milestone snapshots, which for club
    football is nearly always the live source (Polymarket Global lists 1 of these fixtures
    against 164 in milestone_snapshot). At each, prices the model's LIVE fair 3-way from
    ONLY the then-known state (minute, score reconstructed from fixture_event, red cards) with
    the PIT pre-match lambdas, and takes the FIRST side whose live fair beats the then market
    price by the in-play edge threshold (relative value — the market over-reacted). This is the
    causal optimal entry (first tradable in time; you can't wait for a better price you don't
    yet know is coming). Stake is EDGE-WEIGHTED via the SAME ¼-Kelly→envelope as the pre-match
    ``decide()`` (so bigger in-play edges stake more, $0.2–$2). ``exit`` is the 盘中离场 smart-
    exit from the entry minute. Returns the entry dict or None (no tradable in-play edge)."""
    import math

    from prediction_market_soccer.config import CONFIG
    from prediction_market_soccer.model.inplay import live_match_prob
    from prediction_market_soccer.ops.performance_report import _pit_strength
    from prediction_market_soccer.strategy.decision_model import _clip, _kelly_fraction
    from prediction_market_soccer.strategy.smart_exit import (
        _MILESTONE_MIN, _match_minute, smart_exit_cashout)
    from prediction_market_soccer.util.pricing import pnl_cents, reg_score

    if not fx_row["kickoff_ts"]:
        return None
    cfg, risk = CONFIG.decision, CONFIG.risk
    thresh = risk.min_net_edge                 # in-play edge bar (0.03; stricter than pre-match)
    from prediction_market_soccer.ops.performance_report import _row_comp
    sm_pit = _pit_strength(conn, fx_row["kickoff_ts"], _row_comp(fx_row))
    lam_h, lam_a = sm_pit.pair_lambdas(hi, ai)
    gh90, ga90 = reg_score(fx_row["raw_json"], fx_row["home_goals"], fx_row["away_goals"])
    result = "home" if gh90 > ga90 else ("draw" if gh90 == ga90 else "away")
    fid = fx_row["api_id"]
    goals, reds = _event_timelines(conn, fid, fx_row["home_api_id"])
    # POST-EVENT gate: only ENTER after a goal or red card has occurred — that is when the market
    # OVER-REACTS (the classic in-play thesis), and it makes the entry genuinely independent of
    # the pre-match bet. Entering on the opening ticks instead just re-bets the pre-match model-
    # vs-market disagreement (validated: earliest→52%/+$8, min30→54%/+$23, postevent→65%/+$29).
    event_mins = sorted({g[0] for g in goals} | {x[0] for x in reds})

    # In-game price points keyed by MATCH minute → {side: price}. The 3 sides are captured
    # together, so each minute carries a full 3-way quote.
    by_min: dict = {}
    for r in conn.execute(
            "SELECT rel_min, side, price FROM price_tick WHERE fixture_api_id=? AND rel_min "
            "BETWEEN 1 AND ? ORDER BY rel_min", (fid, _SCAN_MAX_RELMIN)):
        by_min.setdefault(_match_minute(r["rel_min"]), {})[r["side"]] = r["price"]
    if not by_min:
        # CLUB EDITION fallback, mirroring smart_exit._milestone_ticks: price_tick comes
        # from the Polymarket global history, which does not list club fixtures — it holds
        # 1 match against 164 in milestone_snapshot. Without this the in-play track could
        # never freeze an entry on any club match, which is exactly what the empty
        # 0W-0L in-play record was: not "no edge appeared", but "no prices to look at".
        # Coarser (5 points instead of ~13), so the entry lands on a milestone minute.
        for r in conn.execute(
                "SELECT milestone, poly_home_ask ph, poly_draw_ask pd, poly_away_ask pa, "
                "       kalshi_home_ask kh, kalshi_draw_ask kd, kalshi_away_ask ka "
                "FROM milestone_snapshot WHERE fixture_api_id=?", (fid,)):
            mn = _MILESTONE_MIN.get(r["milestone"])
            if mn is None:
                continue
            px = {s: (r[f"p{s[0]}"] if r[f"p{s[0]}"] is not None else r[f"k{s[0]}"])
                  for s in _SIDES}
            if all(v is not None for v in px.values()):
                by_min[mn] = px

    entry = None
    for mn in sorted(by_min):                  # match-minute order → first tradable is causal
        if mn < 1 or mn > _MAX_ENTRY_MIN:
            continue
        if not any(e <= mn for e in event_mins):   # no goal/red yet → not an in-play over-reaction
            continue
        sh, sa = _state_at(goals, mn)
        rh, ra = _state_at(reds, mn)
        lp = live_match_prob(lam_h, lam_a, mn, sh, sa, red_home=rh, red_away=ra)
        lpp = {"home": lp.p_home, "draw": lp.p_draw, "away": lp.p_away}
        px = by_min[mn]
        best = None
        for s in _SIDES:
            ask = px.get(s)
            if ask is None:
                continue
            edge = lpp[s] - ask
            if edge >= thresh and (best is None or edge > best[0]):
                best = (edge, s, ask, mn, lpp[s])
        if best:
            entry = best
            break                              # first tradable in time = causal entry

    if not entry:
        return None
    edge, s, ask, mn, fair = entry
    won = (s == result)
    entry_c = round(ask * 100, 1)
    hold_pc = pnl_cents(entry_c, won)                  # per-contract hold-to-FT
    # EDGE-WEIGHTED stake — SAME ¼-Kelly→envelope as the pre-match decide(), edge component only
    # (in-play has no pre-match calibration/form confidence). Bigger relative-value edge → bigger
    # stake, clipped to [$0.2, $2]; contracts then come from the shared sized_pnl_cents().
    f_kelly = _kelly_fraction(fair, ask, risk.kelly_fraction)   # same ¼-Kelly cap as decide()
    edge_comp = math.tanh(f_kelly / max(cfg.kelly_ref, 1e-6))
    k = _clip(cfg.conf_w_edge * edge_comp, cfg.k_min, cfg.k_max)
    stake = _clip(cfg.base_stake_usd * (1.0 + k - getattr(cfg, "conf_k_ref", 0.0)),
                  cfg.min_stake_usd, cfg.max_stake_usd)
    # 盘中离场: smart-exit from the entry minute onward (same cash-out logic as the pre-match bet).
    sx = smart_exit_cashout(conn, sm_pit, fid, s, entry_c, hi, ai, fx_row["round"], won, entry_min=mn)
    realized_pc = sx["pnl_c"] if sx else hold_pc       # per-contract realised (exit or hold)
    return {"milestone": f"{mn}'", "entry_min": mn, "side": s, "entry_cents": entry_c,
            "source": "poly", "edge": round(edge, 4), "result": result, "won": won,
            "stake_usd": round(stake, 2), "hold_pnl_cents": hold_pc, "exit": sx,
            "realized_pnl_cents": realized_pc}


def freeze_settled_bets(conn=None) -> int:
    """Freeze every settled match not yet in ``settled_bet`` (append-only, idempotent).
    Returns the number newly frozen. Safe to call every refresh — a no-op when nothing new
    has settled."""
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.squad_strength import build_strength_live
    from prediction_market_soccer.ops.performance_report import match_pick
    from prediction_market_soccer.strategy.decision_model import quotes_from_milestone_row

    conn = conn or store.init_db()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    have = {r["fixture_api_id"] for r in conn.execute("SELECT fixture_api_id FROM settled_bet")}
    # CLUB SCOPE GUARD (4th of the family): the frozen ledger records OUR live track
    # record from launch (plan R8 — no retro-claiming a historical season). Without
    # this, first run scanned 5,215 historical fixtures through PIT fits.
    from prediction_market_soccer.config.leagues import active as _active
    _lids = tuple(c.api_football_id for c in _active())
    rows = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, kickoff_ts, round, "
        "raw_json, league_id "
        "FROM fixture WHERE status_short IN ({}) AND home_goals IS NOT NULL "
        "AND league_id IN ({}) AND kickoff_ts >= datetime('now', '-14 days') "
        "ORDER BY kickoff_ts".format(",".join("?" * len(_FINISHED)), ",".join("?" * len(_lids))),
        (*_FINISHED, *_lids)).fetchall()
    todo = [r for r in rows if r["api_id"] not in have]
    if not todo:
        return 0
    pre_px = {r["fixture_api_id"]: r for r in conn.execute(
        "SELECT * FROM milestone_snapshot WHERE milestone='PRE'")}
    # Cold-start short-circuit: a settled match with NO pre-match entry quotes has no
    # bet to freeze (PRE stashes start with the live loop, ≤20min pre-kickoff). Without
    # this, the expensive per-day PIT machinery ran for hundreds of historical rows
    # that could never freeze anything (the 5th unscoped-history pin).
    todo = [r for r in todo if r["api_id"] in pre_px]
    if not todo:
        return 0

    sm = build_strength_live(conn, load_prior())         # overridden per-match by pit=True
    records = _pit_py(conn, cmap)                         # PIT (P,Y) once, for the as-of fits
    book = {r["api_id"]: r for r in conn.execute(
        "SELECT f.api_id, AVG(o.p_home) bh, AVG(o.p_draw) bd, AVG(o.p_away) ba "
        "FROM fixture f JOIN match_odds o ON o.fixture_api_id=f.api_id "
        "AND o.bookmaker <> 'live_consensus' "   # pre-match book only
        "WHERE f.status_short IN ({}) GROUP BY f.api_id".format(",".join("?" * len(_FINISHED))),
        _FINISHED).fetchall()}
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for r in todo:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            continue
        cal = _pit_cal(records, r["kickoff_ts"]) if r["kickoff_ts"] else None
        pr = pre_px.get(r["api_id"])
        quotes = quotes_from_milestone_row(pr) if pr is not None else None
        mr = match_pick(sm, cal, hi, ai, r, book.get(r["api_id"]), conn=conn,
                        quotes=quotes, calib_confidence=_conf(cal), gate_open=True, pit=True)
        if mr is None:
            continue   # knockout after ET with no winner flag yet — not settleable
        inplay = _inplay_entry(conn, r, hi, ai)     # causal in-play relative-value entry (or None)
        conn.execute(
            "INSERT OR IGNORE INTO settled_bet "
            "(fixture_api_id, payload, inplay_json, cal_method, cal_param, cal_n, settled_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (r["api_id"], json.dumps(mr, ensure_ascii=False),
             json.dumps(inplay, ensure_ascii=False) if inplay else None,
             (cal or {}).get("method"), (cal or {}).get("param"), (cal or {}).get("n"), now))
        n += 1
    conn.commit()
    return n


def frozen_pick(conn, fx_row, hi, ai, quotes=None, book_row=None):
    """The FROZEN ``match_pick()`` decision for a settled fixture — never recomputed. Self-heals
    if a just-settled match hasn't been frozen yet. Returns None for an unsettled fixture (the
    caller falls back to a live argmax display)."""
    def _read():
        row = conn.execute("SELECT payload FROM settled_bet WHERE fixture_api_id=?",
                           (fx_row["api_id"],)).fetchone()
        return json.loads(row["payload"]) if row is not None else None

    mr = _read()
    if mr is not None:
        return mr
    freeze_settled_bets(conn)     # settled since the last freeze → freeze now, then read
    return _read()


def frozen_inplay(conn, fixture_api_id):
    """The FROZEN causal in-play entry for a settled fixture (or None if no tradable in-play
    edge / not yet frozen). Shares the settled_bet row with the pre-match bet."""
    row = conn.execute("SELECT inplay_json FROM settled_bet WHERE fixture_api_id=?",
                       (fixture_api_id,)).fetchone()
    if row is None or row["inplay_json"] is None:
        return None
    return json.loads(row["inplay_json"])


def backfill_inplay(conn=None, *, dry_run: bool = True) -> dict:
    """Fill ``inplay_json`` on rows frozen while the in-play scan had no prices to read.

    ADDITIVE ONLY — touches rows where inplay_json IS NULL and never rewrites a placed
    pre-match bet, so "a bet, once placed, never changes" still holds for every decision
    already on the ledger. The NULLs are not "no edge was found": ``_inplay_entry`` scanned
    only the Polymarket per-minute series, which lists 1 of these club fixtures against 164
    in milestone_snapshot, so on nearly every match it had nothing to look at. The entries
    written here are still causal — each prices the live fair from ONLY the then-known
    minute/score/reds with that match's point-in-time lambdas, and takes the first side
    that cleared the edge bar in time.

    Returns {n_rows, n_filled, n_no_edge}; dry_run reports without writing.
    """
    from prediction_market_soccer.ingest import store

    conn = conn or store.init_db()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    rows = conn.execute(
        "SELECT f.* FROM fixture f JOIN settled_bet s ON s.fixture_api_id = f.api_id "
        "WHERE s.inplay_json IS NULL ORDER BY f.kickoff_ts").fetchall()
    filled = no_edge = 0
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            no_edge += 1
            continue
        try:
            entry = _inplay_entry(conn, r, hi, ai)
        except Exception as e:  # noqa: BLE001 — one bad match must not stop the backfill
            print(f"  [skip] fixture {r['api_id']}: {type(e).__name__}: {e}")
            no_edge += 1
            continue
        if entry is None:
            no_edge += 1
            continue
        filled += 1
        if not dry_run:
            conn.execute("UPDATE settled_bet SET inplay_json=? WHERE fixture_api_id=? "
                         "AND inplay_json IS NULL",
                         (json.dumps(entry, ensure_ascii=False), r["api_id"]))
    if not dry_run:
        conn.commit()
    return {"n_rows": len(rows), "n_filled": filled, "n_no_edge": no_edge}


def main() -> None:
    import argparse

    from prediction_market_soccer.ingest import store
    ap = argparse.ArgumentParser(description="freeze newly-settled bets; optionally backfill in-play")
    ap.add_argument("--backfill-inplay", action="store_true",
                    help="fill inplay_json on already-frozen rows that were frozen with no price series")
    ap.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    args = ap.parse_args()
    conn = store.init_db()
    if args.backfill_inplay:
        res = backfill_inplay(conn, dry_run=not args.apply)
        mode = "WROTE" if args.apply else "dry run"
        print(f"in-play backfill ({mode}): {res['n_rows']} rows with no in-play entry → "
              f"{res['n_filled']} filled, {res['n_no_edge']} genuinely had no tradable entry")
        return
    n = freeze_settled_bets(conn)
    total = conn.execute("SELECT COUNT(*) FROM settled_bet").fetchone()[0]
    print(f"settled_bet: froze {n} new match(es); {total} total frozen")


if __name__ == "__main__":
    main()
