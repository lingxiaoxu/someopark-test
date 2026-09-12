"""ops/leak_corrected_book.py — rebuild the frozen strategy book on leak-corrected prices.

WHAT THIS IS
    The frozen book's legacy rows (origin 'published_baseline') were priced off milestone
    rows reconstructed after the match by a backfill that sampled the venue series on the
    WALL clock while scoring the row on the MATCH clock, fetched 10-minute bars and took the
    nearest bar (which could sit in the future). The owner authorised, on 2026-09-11, lifting
    the freeze, correcting exactly that, and freezing the result as a NEW book version.

    This module holds the legacy rows' DECISIONS fixed — pick, bet kind, stake, in-play rule,
    exit rule, model — and re-prices only what the leak touched:
      * the pre-match entry price (the corrected PRE quote of the same venue column),
      * the smart-exit leg (the legacy 5-milestone rule, evaluated on corrected quotes),
      * the in-play leg (the legacy causal entry rule + its exit, on corrected quotes),
    then recomputes the three cumulative series over the whole book in order. Forward rows
    (origin 'paper_forward', evidence 'forward_observed_paper') keep every leg value; only
    their cumulative fields move, because the rows before them changed.

WHERE THE CORRECTED PRICES COME FROM
    A sealed price candidate produced by the repair's own collector
    (ops/rederive_milestone_prices → util/research_inputs.CandidateWriter): one 'quote' item
    per (fixture, milestone, side) sampled at the corrected wall time (T60 → kickoff+75,
    T75 → kickoff+90; util/match_timeline.milestone_target) from a 1-minute series, causally.
    Original milestone_snapshot rows are never updated. Kalshi columns and live-captured
    rows are read from the database unchanged: the leak lived only in reconstructed Poly
    columns.

WHAT THIS IS NOT
    Not strict point-in-time: the half-time clock is the +15-minute approximation and the
    August inputs carry no availability evidence. Every corrected record says so
    (evidence_level 'leak_corrected_approx_pit_paper', pit_status
    'approximate_reconstruction_leak_corrected') and carries a `leak_correction` block with
    the before/after of each leg and the lambdas used, so a third party can re-run it.
    Not fee-adjusted (the owner keeps fees out of the ledger; they are observed on demo fills).

The legacy rules below are ported verbatim from git f2fe563 (strategy/smart_exit.py,
ops/settle_bets.py) — the code that priced the frozen book — so that the only thing that
differs between the old and new book is the price data.
"""
from __future__ import annotations

import json
import math
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG

EVIDENCE_LEVEL = "leak_corrected_approx_pit_paper"
PIT_STATUS = "approximate_reconstruction_leak_corrected"
RULE_VERSION = "legacy-f2fe563-5-milestone-v1"

# ── legacy constants (f2fe563) ────────────────────────────────────────────────────────────
_SIDES = ("home", "draw", "away")
_MILESTONE_MIN = {"T15": 15, "T30": 30, "HT": 45, "T60": 60, "T75": 75}
_MIN_MILESTONE_POINTS = 3
_HALFTIME_WALL_MIN = 15
_REG_MAX_MATCH_MIN = 95
_SCAN_MAX_RELMIN = 170
_MAX_ENTRY_MIN = 85


def _match_minute(rel_min: int) -> int:
    if rel_min <= 45:
        return rel_min
    if rel_min <= 45 + _HALFTIME_WALL_MIN:
        return 45
    return rel_min - _HALFTIME_WALL_MIN


# ── corrected quotes ──────────────────────────────────────────────────────────────────────
def load_candidate_quotes(candidate_db, run_id: str) -> dict:
    """{(fixture_id, milestone): {'home': p|None, 'draw': ..., 'away': ..., 'ts': iso,
    'status': {side: ok|unavailable}}} from the sealed candidate's 'quote' items."""
    c = sqlite3.connect(f"file:{candidate_db}?mode=ro&immutable=1", uri=True)
    c.row_factory = sqlite3.Row
    out: dict = {}
    try:
        for r in c.execute("SELECT fixture_id, target_at, side, status, payload FROM research_item "
                           "WHERE run_id=? AND kind='quote' AND market_kind='match'", (run_id,)):
            p = json.loads(r["payload"])
            code = str(p.get("target_id", "")).split(":")[0]
            if code not in _MILESTONE_MIN and code != "PRE":
                continue
            q = out.setdefault((int(r["fixture_id"]), code), {"home": None, "draw": None, "away": None,
                                                                "ts": None, "status": {}})
            q["status"][r["side"]] = r["status"]
            if r["status"] == "ok" and p.get("price") is not None:
                q[r["side"]] = float(p["price"])
            q["ts"] = datetime.fromtimestamp(float(r["target_at"]), timezone.utc).isoformat()
    finally:
        c.close()
    return out


def _milestone_rows(conn, fid: int) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM milestone_snapshot WHERE fixture_api_id=?", (fid,))]


def _poly(row: dict, quotes: dict, side: str):
    """The Poly quote for one side of one milestone row: the corrected candidate value for a
    reconstructed row, the stored value for a live-captured one. A reconstructed row whose
    corrected quote is unavailable yields None — the leaky number never survives."""
    if row.get("price_source") == "candlestick":
        q = quotes.get((row["fixture_api_id"], row["milestone"]))
        return None if q is None else q.get(side)
    return row.get(f"poly_{side}_ask")


def _ticks(conn, fid: int, pick: str, entry_min: int, quotes: dict) -> list[tuple[int, float]]:
    """Legacy _milestone_ticks: [(match_minute, price)] with the legacy venue precedence
    Kalshi bid → Kalshi ask → Poly (bid==ask on reconstructed rows)."""
    out = []
    for r in _milestone_rows(conn, fid):
        mn = _MILESTONE_MIN.get(r["milestone"])
        if mn is None or mn < max(1, entry_min):
            continue
        polys = ((_poly(r, quotes, pick),) if r.get("price_source") == "candlestick"
                 else (r.get(f"poly_{pick}_bid"), r.get(f"poly_{pick}_ask")))
        px = next((v for v in (r.get(f"kalshi_{pick}_bid"), r.get(f"kalshi_{pick}_ask"), *polys) if v is not None), None)
        if px is not None:
            out.append((mn, float(px)))
    return sorted(out)


def _event_timelines(conn, fid, home_api):
    goals, reds = [], []
    gh = ga = rh = ra = 0
    for e in conn.execute("SELECT minute, team_api_id, type, detail FROM fixture_event "
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


def _state_at(timeline, mn):
    h = a = 0
    for row in timeline:
        if row[0] <= mn:
            h, a = row[1], row[2]
        else:
            break
    return h, a


# ── legacy smart exit (f2fe563 smart_exit_cashout, milestone branch) ──────────────────────
def legacy_smart_exit(conn, sm, fid, pick, entry_c, hi, ai, round_name, won, quotes, *, entry_min=0):
    from prediction_market_soccer.model.inplay import live_match_prob
    from prediction_market_soccer.model.inplay_constants import OVERSHOOT_MARGIN, overshoot_trigger
    from prediction_market_soccer.model.match_pricing import is_knockout
    if entry_c is None or pick not in _SIDES:
        return None, []
    raw = conn.execute("SELECT rel_min, price FROM price_tick WHERE fixture_api_id=? AND side=? AND rel_min BETWEEN 1 AND ? "
                       "ORDER BY ts", (fid, pick, _SCAN_MAX_RELMIN)).fetchall()
    ticks = [(_match_minute(r["rel_min"]), r["price"]) for r in raw]
    ticks = [(mn, px) for (mn, px) in ticks if max(1, entry_min) <= mn <= _REG_MAX_MATCH_MIN]
    source = "price_tick"
    if len(ticks) < 10:
        ticks = _ticks(conn, fid, pick, entry_min, quotes)
        source = "milestones"
        if len(ticks) < _MIN_MILESTONE_POINTS:
            return None, []
    lam_h, lam_a = sm.pair_lambdas(hi, ai, knockout=is_knockout(round_name))
    home_row = conn.execute("SELECT home_api_id FROM fixture WHERE api_id=?", (fid,)).fetchone()
    goals, _reds = _event_timelines(conn, fid, home_row[0] if home_row else None)
    trail = []
    for mn, price in ticks:
        sh, sa = _state_at(goals, mn)
        lp = live_match_prob(lam_h, lam_a, mn, sh, sa)
        fair = {"home": lp.p_home, "draw": lp.p_draw, "away": lp.p_away}[pick] * 100.0
        mkt = price * 100.0
        trig = min(OVERSHOOT_MARGIN, overshoot_trigger(fair / 100.0))
        trail.append({"min": int(mn), "price_c": round(mkt, 1), "fair_c": round(fair, 1),
                      "trigger_c": round(trig * 100.0, 1), "score": f"{sh}-{sa}", "source": source})
        if mkt >= fair + trig * 100.0:
            hold = (100.0 - entry_c) if won else -entry_c
            return {"sold_min": int(mn), "sold_c": round(mkt, 1), "pnl_c": round(mkt - entry_c, 1),
                    "vs_hold_c": round((mkt - entry_c) - hold, 1)}, trail
    return None, trail


# ── legacy in-play entry (f2fe563 settle_bets._inplay_entry) ──────────────────────────────
def legacy_inplay_entry(conn, fx_row, hi, ai, sm_pit, quotes):
    from prediction_market_soccer.model.inplay import live_match_prob
    from prediction_market_soccer.strategy.decision_model import _clip, _kelly_fraction
    from prediction_market_soccer.util.pricing import pnl_cents, reg_score
    if not fx_row["kickoff_ts"]:
        return None
    cfg, risk = CONFIG.decision, CONFIG.risk
    thresh = risk.min_net_edge
    lam_h, lam_a = sm_pit.pair_lambdas(hi, ai)
    gh90, ga90 = reg_score(fx_row["raw_json"], fx_row["home_goals"], fx_row["away_goals"])
    result = "home" if gh90 > ga90 else ("draw" if gh90 == ga90 else "away")
    fid = fx_row["api_id"]
    goals, reds = _event_timelines(conn, fid, fx_row["home_api_id"])
    event_mins = sorted({g[0] for g in goals} | {x[0] for x in reds})
    by_min: dict = {}
    for r in conn.execute("SELECT rel_min, side, price FROM price_tick WHERE fixture_api_id=? AND rel_min "
                          "BETWEEN 1 AND ? ORDER BY rel_min", (fid, _SCAN_MAX_RELMIN)):
        by_min.setdefault(_match_minute(r["rel_min"]), {})[r["side"]] = r["price"]
    source = "price_tick" if by_min else "milestones"
    if not by_min:
        for r in _milestone_rows(conn, fid):
            mn = _MILESTONE_MIN.get(r["milestone"])
            if mn is None:
                continue
            px = {}
            for s in _SIDES:
                poly = _poly(r, quotes, s)
                px[s] = poly if poly is not None else r.get(f"kalshi_{s}_ask")
            if all(v is not None for v in px.values()):
                by_min[mn] = px
    entry = None
    for mn in sorted(by_min):
        if mn < 1 or mn > _MAX_ENTRY_MIN:
            continue
        if not any(e <= mn for e in event_mins):
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
            break
    if not entry:
        return None
    edge, s, ask, mn, fair = entry
    won = (s == result)
    entry_c = round(ask * 100, 1)
    hold_pc = pnl_cents(entry_c, won)
    f_kelly = _kelly_fraction(fair, ask, risk.kelly_fraction)
    edge_comp = math.tanh(f_kelly / max(cfg.kelly_ref, 1e-6))
    k = _clip(cfg.conf_w_edge * edge_comp, cfg.k_min, cfg.k_max)
    stake = _clip(cfg.base_stake_usd * (1.0 + k - getattr(cfg, "conf_k_ref", 0.0)),
                  cfg.min_stake_usd, cfg.max_stake_usd)
    sx, trail = legacy_smart_exit(conn, sm_pit, fid, s, entry_c, hi, ai, fx_row["round"], won, quotes, entry_min=mn)
    realized_pc = sx["pnl_c"] if sx else hold_pc
    return {"milestone": f"{mn}'", "entry_min": mn, "side": s, "entry_cents": entry_c, "source": "poly",
            "edge": round(edge, 4), "result": result, "won": won, "stake_usd": round(stake, 2),
            "hold_pnl_cents": hold_pc, "exit": sx, "realized_pnl_cents": realized_pc,
            "_price_source": source, "_lambdas": [round(lam_h, 6), round(lam_a, 6)], "_exit_trail": trail}


def _side_quote_c(rows: list[dict], quotes: dict, milestone: str, side: str):
    """PRE/T75 ¢ for a side, f2fe563 precedence (Poly ask then Kalshi ask), corrected on
    reconstructed rows. Returns None when no quote exists."""
    from prediction_market_soccer.util.pricing import to_cents
    row = next((x for x in rows if x.get("milestone") == milestone), None)
    if row is None or side not in _SIDES:
        return None
    poly = _poly(row, quotes, side)
    v = poly if poly is not None else row.get(f"kalshi_{side}_ask")
    return to_cents(v) if v is not None else None


def _accumulate(r: dict, cums: dict) -> None:
    """Every running total the legacy report carried, recomputed in book order."""
    p = float(r.get("realized_pnl_cents") or 0) if r.get("bet") else 0.0
    i = float(r.get("inplay_pnl_cents") or 0) if r.get("inplay_side") else 0.0
    cums["pre"] += p
    cums["ip"] += i
    r["pre_cum_pnl_cents"] = round(cums["pre"], 1)
    r["inplay_cum_pnl_cents"] = round(cums["ip"], 1)
    r["combined_cum_pnl_cents"] = round(cums["pre"] + cums["ip"], 1)
    r["combined_pnl_cents"] = round(p + i, 1)
    for k in ("usd", "hold", "real"):
        cums.setdefault(k, 0.0)
    cums["real"] += p
    # The realized cumulative is the pre-track running total and exists on every row,
    # including the in-play-only rows that carry bet=False; writing it only under the
    # bet guard leaves those rows holding the pre-correction value.
    r["realized_cum_pnl_cents"] = round(cums["real"], 1)
    if r.get("bet"):
        cums["usd"] += float(r.get("pnl") or 0.0)
        cums["hold"] += float(r.get("pnl_cents") or 0.0)
        r["cum_pnl"] = round(cums["usd"], 3)
        r["cum_pnl_cents"] = round(cums["hold"], 1)


# ── the rebuild ───────────────────────────────────────────────────────────────────────────
def _is_forward(record: dict) -> bool:
    return record.get("evidence_level") == "forward_observed_paper" or bool(record.get("book_version_id"))


def _corrected_entry(record: dict, quotes: dict, rows: list[dict]) -> tuple:
    """(entry_cents, note). Only a Poly-sourced entry on a RECONSTRUCTED PRE row moves; a
    PRE row the live loop captured itself was a real, executable ask and is never replaced
    by a historical reference sample (review 2026-09-11: 36 such entries would have moved)."""
    fid, pick = record["fixture_id"], record.get("pick")
    pre = next((x for x in rows if x.get("milestone") == "PRE"), None)
    if pre is None or pre.get("price_source") != "candlestick":
        return record.get("entry_cents"), "kept:pre_row_live"
    q = quotes.get((fid, "PRE"))
    if record.get("entry_source") != "poly" or q is None or pick not in _SIDES:
        return record.get("entry_cents"), "kept:" + str(record.get("entry_source"))
    if q.get(pick) is None:
        return record.get("entry_cents"), "kept:pre_quote_unavailable"
    return round(float(q[pick]) * 100.0, 1), "corrected"


def rebuild(conn, records: list[dict], quotes: dict, *, run_id: str) -> tuple[list[dict], dict]:
    """Return (new_records, summary). ``records`` is the active book in order."""
    from prediction_market_soccer.ops.performance_report import _pit_strength, _row_comp
    from prediction_market_soccer.util.pricing import pnl_cents, sized_pnl_cents
    from prediction_market_soccer.ingest.club_prior import load_prior

    prior = load_prior()
    zh = {t.team_id: t.zh for t in prior.teams}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    new_records: list[dict] = []
    cums = {"pre": 0.0, "ip": 0.0, "usd": 0.0, "hold": 0.0, "real": 0.0}
    summary = {"run_id": run_id, "rule_version": RULE_VERSION, "legacy": 0, "forward": 0,
               "entry_corrected": 0, "entry_kept": 0, "exit_changed": 0, "inplay_changed": 0,
               "inplay_dropped": 0, "inplay_added": 0, "pre_before_c": 0.0, "pre_after_c": 0.0,
               "inplay_before_c": 0.0, "inplay_after_c": 0.0, "candlestick_rows": 0, "live_rows": 0,
               "unresolved_fixtures": []}
    sm_cache: dict = {}

    for rec in records:
        r = deepcopy(rec)
        fid = r["fixture_id"]
        if _is_forward(r):
            summary["forward"] += 1
        else:
            summary["legacy"] += 1
            fx = conn.execute("SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, kickoff_ts, "
                              "round, raw_json, league_id, status_short FROM fixture WHERE api_id=?", (fid,)).fetchone()
            if fx is None:
                summary["unresolved_fixtures"].append(fid)
                r["leak_correction"] = {"run_id": run_id, "rule_version": RULE_VERSION, "status": "unresolved",
                                        "reason": "fixture row missing", "milestone_source": None}
                _accumulate(r, cums)
                new_records.append(r)      # cannot re-price; keep, but never silently
                continue
            hi, ai = cmap.get(fx["home_api_id"]), cmap.get(fx["away_api_id"])
            comp = _row_comp(fx)
            key = (fx["kickoff_ts"][:10], comp)
            if key not in sm_cache:
                sm_cache[key] = _pit_strength(conn, fx["kickoff_ts"], comp)
            sm = sm_cache[key]
            rows = _milestone_rows(conn, fid)
            # FT rows carry the settlement value, never a quote: a fixture whose only
            # reconstructed row is FT has nothing to correct and is a LIVE fixture here.
            recon_rows = [x for x in rows if x.get("price_source") == "candlestick" and x["milestone"] != "FT"]
            reconstructed = bool(recon_rows)
            summary["candlestick_rows" if reconstructed else "live_rows"] += 1
            if not reconstructed:
                # Nothing that priced this fixture changed. Its frozen legs stay verbatim;
                # the legacy rules are re-run only as a DIAGNOSTIC of the port's fidelity.
                diag = None
                try:
                    if r.get("bet"):
                        sx, _t = legacy_smart_exit(conn, sm, fid, r["pick"], r.get("entry_cents"), hi, ai, fx["round"], bool(r["won"]), quotes)
                        diag = {"exit_reproduced": ((sx or None) == (r.get("smart_exit") or None))}
                    ip = legacy_inplay_entry(conn, fx, hi, ai, sm, quotes)
                    diag = {**(diag or {}), "inplay_reproduced": ((ip["side"], ip["milestone"], ip["entry_cents"]) if ip else None) ==
                            ((r.get("inplay_side"), r.get("inplay_milestone"), r.get("inplay_entry_cents")) if r.get("inplay_side") else None)}
                except Exception as exc:  # noqa: BLE001 — a diagnostic must never block the book
                    diag = {"error": str(exc)[:120]}
                summary.setdefault("live_fidelity", {"exit_same": 0, "exit_n": 0, "inplay_same": 0, "inplay_n": 0})
                if diag and "exit_reproduced" in diag:
                    summary["live_fidelity"]["exit_n"] += 1; summary["live_fidelity"]["exit_same"] += int(diag["exit_reproduced"])
                if diag and "inplay_reproduced" in diag:
                    summary["live_fidelity"]["inplay_n"] += 1; summary["live_fidelity"]["inplay_same"] += int(diag["inplay_reproduced"])
                r["leak_correction"] = {"run_id": run_id, "rule_version": RULE_VERSION, "status": "unchanged",
                                        "milestone_source": "live", "diagnostic": diag}
                _accumulate(r, cums)
                p = float(r.get("realized_pnl_cents") or 0) if r.get("bet") else 0.0
                i = float(r.get("inplay_pnl_cents") or 0) if r.get("inplay_side") else 0.0
                summary["pre_before_c"] += p; summary["pre_after_c"] += p
                summary["inplay_before_c"] += i; summary["inplay_after_c"] += i
                new_records.append(r)
                continue
            if not any((fid, x["milestone"]) in quotes and any(quotes[(fid, x["milestone"])].get(s) is not None for s in _SIDES)
                       for x in recon_rows):
                # The collector could not re-identify this fixture (e.g. identity_conflict), so
                # there is no corrected quote for any of its reconstructed rows. Keep every legacy
                # leg exactly as frozen and say so — dropping legs here would be a data gap
                # masquerading as a correction. The old evidence tier stays on the record.
                summary["unresolved_fixtures"].append(fid)
                r["leak_correction"] = {"run_id": run_id, "rule_version": RULE_VERSION, "status": "unresolved",
                                        "reason": "no corrected quotes for any reconstructed milestone",
                                        "milestone_source": "candlestick"}
                _accumulate(r, cums)
                p = float(r.get("realized_pnl_cents") or 0) if r.get("bet") else 0.0
                i = float(r.get("inplay_pnl_cents") or 0) if r.get("inplay_side") else 0.0
                summary["pre_before_c"] += p; summary["pre_after_c"] += p
                summary["inplay_before_c"] += i; summary["inplay_after_c"] += i
                new_records.append(r)
                continue
            before = {"entry_cents": r.get("entry_cents"), "pnl_cents": r.get("pnl_cents"),
                      "smart_exit": r.get("smart_exit"), "realized_pnl_cents": r.get("realized_pnl_cents"),
                      "inplay": {k: r.get(k) for k in ("inplay_side", "inplay_milestone", "inplay_entry_cents",
                                                      "inplay_edge", "inplay_exit", "inplay_stake_usd",
                                                      "inplay_hold_cents", "inplay_pnl_cents", "inplay_won")}}
            lambdas = None
            # ── pre-match leg: decision fixed, price corrected ──
            if r.get("bet"):
                pick, won, stake = r["pick"], bool(r["won"]), float(r.get("stake_usd") or 1.0)
                entry_c, note = _corrected_entry(r, quotes, rows)
                summary["entry_corrected" if note == "corrected" else "entry_kept"] += 1
                unit = pnl_cents(entry_c, won) if entry_c is not None else None
                hold_c = sized_pnl_cents(entry_c, unit, stake) if entry_c is not None else None
                sx, trail = (legacy_smart_exit(conn, sm, fid, pick, entry_c, hi, ai, fx["round"], won, quotes)
                             if entry_c is not None else (None, []))
                realized_unit = sx["pnl_c"] if sx else unit
                realized_c = sized_pnl_cents(entry_c, realized_unit, stake) if entry_c is not None else None
                r.update(entry_cents=entry_c, pnl_cents=hold_c, smart_exit=sx, realized_pnl_cents=realized_c,
                         settle_cents=(100.0 if won else 0.0))
                if entry_c:
                    r["price"] = round(entry_c / 100.0, 4)
                    r["dec_odds"] = round(100.0 / entry_c, 3)
                    r["pnl"] = round(stake * (100.0 / entry_c - 1.0), 3) if won else round(-stake, 3)
                if (sx or None) != (before["smart_exit"] or None):
                    summary["exit_changed"] += 1
                lambdas = [round(x, 6) for x in sm.pair_lambdas(hi, ai)]
                exit_trail = trail
            else:
                exit_trail = []
            # ── argmax benchmark and CLV: same f2fe563 formulas on the corrected PRE/T75 quotes ──
            am_pick, am_won = r.get("model_pick"), r.get("model_won")
            am_e = _side_quote_c(rows, quotes, "PRE", am_pick) if am_pick in _SIDES else None
            am_unit = pnl_cents(am_e, bool(am_won)) if (am_e and am_won is not None) else None
            r["argmax_entry_cents"] = am_e
            r["argmax_pnl_cents"] = (round((1.0 / (am_e / 100.0)) * am_unit, 1) if (am_e and am_unit is not None) else None)
            if r.get("bet") and r.get("entry_cents") is not None:
                t75 = _side_quote_c(rows, quotes, "T75", r["pick"])
                r["clv_cents"] = round(t75 - r["entry_cents"], 1) if t75 is not None else None
            # ── in-play leg: legacy causal rule on corrected quotes ──
            ip = legacy_inplay_entry(conn, fx, hi, ai, sm, quotes)
            had = bool(before["inplay"]["inplay_side"])
            if ip and had and (ip["side"], ip["milestone"], ip["entry_cents"]) == (before["inplay"]["inplay_side"], before["inplay"]["inplay_milestone"], before["inplay"]["inplay_entry_cents"]) \
                    and (ip["exit"] or None) == (before["inplay"]["inplay_exit"] or None):
                # Same entry, same exit: only the PRICE was authorised to move and it did not,
                # so the frozen leg (edge, stake, P&L — all fitted then) stays verbatim.
                lambdas = lambdas or ip["_lambdas"]
                ip_trail, ip_price_source = ip["_exit_trail"], ip["_price_source"]
                ip = None
                keep_inplay = True
            else:
                keep_inplay = False
            if keep_inplay:
                pass
            elif ip:
                team = {"home": fx["home_api_id"], "away": fx["away_api_id"]}.get(ip["side"])
                r.update(inplay_side=ip["side"], inplay_side_team=(r.get("home") if ip["side"] == "home" else r.get("away") if ip["side"] == "away" else "Draw"),
                         inplay_milestone=ip["milestone"], inplay_entry_cents=ip["entry_cents"],
                         inplay_edge=ip["edge"], inplay_exit=ip["exit"], inplay_stake_usd=ip["stake_usd"],
                         inplay_hold_cents=sized_pnl_cents(ip["entry_cents"], ip["hold_pnl_cents"], ip["stake_usd"]),
                         inplay_pnl_cents=sized_pnl_cents(ip["entry_cents"], ip["realized_pnl_cents"], ip["stake_usd"]),
                         inplay_won=ip["won"])
                lambdas = lambdas or ip["_lambdas"]
                if not had:
                    summary["inplay_added"] += 1
                elif (ip["side"], ip["milestone"], ip["entry_cents"]) != (before["inplay"]["inplay_side"], before["inplay"]["inplay_milestone"], before["inplay"]["inplay_entry_cents"]):
                    summary["inplay_changed"] += 1
                ip_trail = ip["_exit_trail"]
                ip_price_source = ip["_price_source"]
            else:
                for k in ("inplay_side", "inplay_side_team", "inplay_milestone", "inplay_entry_cents", "inplay_edge",
                          "inplay_exit", "inplay_stake_usd", "inplay_hold_cents", "inplay_pnl_cents", "inplay_won"):
                    r[k] = None
                if had:
                    summary["inplay_dropped"] += 1
                ip_trail, ip_price_source = [], None
            if keep_inplay:
                summary["inplay_unchanged"] = summary.get("inplay_unchanged", 0) + 1
            # ── labels and provenance ──
            r["evidence_level"] = EVIDENCE_LEVEL if reconstructed else r.get("evidence_level")
            r["pit_status"] = PIT_STATUS if reconstructed else r.get("pit_status")
            r["leak_correction"] = {"run_id": run_id, "rule_version": RULE_VERSION,
                                    "milestone_source": "candlestick" if reconstructed else "live",
                                    "entry_note": (note if r.get("bet") else None),
                                    "lambdas": lambdas, "before": before,
                                    "exit_trail": exit_trail, "inplay_price_source": ip_price_source,
                                    "inplay_exit_trail": ip_trail}
            summary["pre_before_c"] += float(before["realized_pnl_cents"] or 0) if rec.get("bet") else 0.0
            summary["pre_after_c"] += float(r.get("realized_pnl_cents") or 0) if r.get("bet") else 0.0
            summary["inplay_before_c"] += float(before["inplay"]["inplay_pnl_cents"] or 0) if had else 0.0
            summary["inplay_after_c"] += float(r.get("inplay_pnl_cents") or 0) if r.get("inplay_side") else 0.0
        # ── cumulatives, recomputed for every row in order ──
        _accumulate(r, cums)
        new_records.append(r)
    am_cum = 0.0
    for r in new_records:
        am_cum += float(r.get("argmax_pnl_cents") or 0.0)
        r["argmax_cum_pnl_cents"] = round(am_cum, 1)
    for k in ("pre_before_c", "pre_after_c", "inplay_before_c", "inplay_after_c"):
        summary[k] = round(summary[k], 1)
    return new_records, summary


def main() -> None:
    """Dry run against a clone: rebuild in memory and print the summary. Writes nothing."""
    import argparse
    import pathlib
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.util.frozen_strategy_store import read_book
    ap = argparse.ArgumentParser(description="Rebuild the frozen book on leak-corrected prices (dry run)")
    ap.add_argument("--db", required=True, help="a CLONE of soccer.db; never production")
    ap.add_argument("--candidate-db", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", default=None, help="write new records + summary JSON here")
    a = ap.parse_args()
    store.DB_PATH = pathlib.Path(a.db)
    conn = store.init_db()
    assert str(a.db) in str(next(r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"))
    book = read_book(conn)
    quotes = load_candidate_quotes(a.candidate_db, a.run_id)
    recs, summary = rebuild(conn, book["ledger"]["records"], quotes, run_id=a.run_id)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if a.out:
        pathlib.Path(a.out).write_text(json.dumps({"summary": summary, "records": recs}, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()


# ═════════════════════════════════════════════════════════════════════════════════════════
# Orchestration: report → bundle → register → render → switch. Every step is explicit and
# rehearsed on an isolated clone before it is allowed near production.
# ═════════════════════════════════════════════════════════════════════════════════════════
MODEL_VERSION = "club-soccer-v1"


def method_version_for(run_id: str, attempt: str) -> str:
    """One label per registration attempt: strategy_book_version is UNIQUE on
    (model, method) and append-only, so a re-registration after a failed attempt needs a
    new label rather than a silent collision."""
    return f"hybrid-smart-timing-v1+leakfix-approxpit-{run_id}-{attempt}"


def assemble_report(prev_book: dict, records: list[dict], *, run_id: str, summary: dict) -> dict:
    """A report dict in the exact shape frozen_strategy_store expects, built from the
    previous version's metadata and the corrected records; totals are recomputed from the
    records by report_from_book at read time."""
    from prediction_market_soccer.util.strategy_ledger import build_strategy_ledger
    meta = deepcopy(prev_book["report_metadata"])
    as_of = prev_book["ledger"]["as_of"]
    basis = meta.get("pnl_basis") or prev_book["ledger"].get("pnl_basis")
    ledger = build_strategy_ledger({"bet_log": records, "as_of": as_of, "pnl_basis": basis})
    ev = dict(meta.get("evidence_summary") or {})
    # Never carry the previous book's derived tier breakdown into a corrected book's
    # frozen metadata; report_from_book recomputes it from the records at read time.
    ev.pop("tiers", None)
    ev["leak_correction"] = {"run_id": run_id, "rule_version": RULE_VERSION, "pit_status": PIT_STATUS,
                             "strict_pit_certified": False, "owner_authorized": True,
                             "previous_version_id": prev_book["version"]["version_id"],
                             "summary": {k: v for k, v in summary.items() if k != "unresolved_fixtures"},
                             "unresolved_fixtures": summary.get("unresolved_fixtures", [])}
    meta["evidence_summary"] = ev
    meta["model_version_pit"] = ("approximate: legacy decisions held fixed; prices re-derived at the corrected "
                                 "clock from a sealed reference candidate; not strict PIT")
    report = {**{k: v for k, v in meta.items() if k not in ("bet_log", "strategy_ledger")},
              "as_of": as_of, "pnl_basis": ledger["pnl_basis"], "bet_log": records, "strategy_ledger": ledger}
    return report


def candidate_input_for(root, candidate_db, source_db, run_id: str, scope_id: str) -> dict:
    import hashlib
    from prediction_market_soccer.util.research_inputs import CandidateMarketData
    root = str(pathlib_resolve(root))
    data = CandidateMarketData(candidate_db, root=root, run_id=run_id, scope_id=scope_id)
    try:
        completion = dict(data.completion)
    finally:
        data.close()
    return {"path": str(pathlib_resolve(candidate_db)), "root": root, "run_id": run_id, "scope_id": scope_id,
            "sha256": hashlib.sha256(open(candidate_db, "rb").read()).hexdigest(),
            "source_path": str(pathlib_resolve(source_db)), "source_root": root,
            "source_sha256": hashlib.sha256(open(source_db, "rb").read()).hexdigest(),
            "input_manifest_hash": completion["input_manifest_hash"], "items_hash": completion["items_hash"],
            "require_observed_quotes": False}


def pathlib_resolve(p):
    from pathlib import Path
    return Path(p).resolve(strict=True)


def make_bundle(*, root, output_dir, report: dict, prev_book: dict, summary: dict, candidate_input: dict,
                run_id: str, authorization: dict, attempt: str) -> dict:
    from prediction_market_soccer.ops import owner_authorized_correction as O
    prev = {"version_id": prev_book["version"]["version_id"], "ledger_id": prev_book["ledger"]["ledger_id"],
            "records": prev_book["ledger"]["records"]}
    return O.build_bundle(output_dir=output_dir, root=root, candidate_input=candidate_input, report=report,
                          summary={k: v for k, v in summary.items() if k != "unresolved_fixtures"},
                          previous_book=prev, authorization=authorization, model_version=MODEL_VERSION,
                          method_version=method_version_for(run_id, attempt), run_id=run_id, rule_version=RULE_VERSION)


def register(conn, *, root, bundle_dir) -> str:
    """Register the corrected book as an explicit backtest version (not yet active)."""
    from prediction_market_soccer.util.frozen_strategy_store import register_backtest_version
    docs = {n: json.loads((pathlib_resolve(bundle_dir) / n).read_text()) for n in
            ("candidate_report.json", "completed_manifest.json")}
    return register_backtest_version(conn, docs["candidate_report.json"], model_version=docs["completed_manifest.json"]["model_version"],
                                     method_version=docs["completed_manifest.json"]["method_version"],
                                     backtest_manifest=docs["completed_manifest.json"],
                                     confirmed_replacement=True, candidate_bundle=str(bundle_dir), isolated_root=str(root))


def make_render(conn, *, root, candidate_input: dict):
    """render(book, [dir0, dir1]) for version_workflow.switch: the three financial files from
    the target book, prices from the sealed candidate, forward marks from paper observations."""
    from pathlib import Path
    from dataclasses import asdict
    from prediction_market_soccer.ops import milestone_export
    from prediction_market_soccer.ops.performance_report import build_pdf, report_from_book, validate_strategy_views
    from prediction_market_soccer.ops.run_status import atomic_bytes

    def render(book, directories):
        report = report_from_book(book)
        # One code path for every render of this version, now and in the daily refresh.
        marks = milestone_export.marks_for_book(conn, report.strategy_ledger, book["version"])
        # Render ONCE and copy the bytes: the workflow requires the two artifact directories
        # to be byte-identical, and a PDF rendered twice carries two creation timestamps.
        first = Path(directories[0])
        atomic_bytes(first / "performance_report.json", json.dumps(asdict(report), ensure_ascii=False, allow_nan=False).encode())
        atomic_bytes(first / "milestone_marks.json", json.dumps(marks, ensure_ascii=False, allow_nan=False).encode())
        build_pdf(report, str(first / "performance_report.pdf"))
        validate_strategy_views(first, report.strategy_ledger)
        for d in directories[1:]:
            for name in ("performance_report.json", "milestone_marks.json", "performance_report.pdf"):
                atomic_bytes(Path(d) / name, (first / name).read_bytes())
            validate_strategy_views(Path(d), report.strategy_ledger)
    return render


def unsealed_paper_entries(conn) -> int:
    """Entries whose completion has not been sealed yet. Such a completion would later be
    sealed under the OLD book id, so a switch must wait for zero."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"paper_entry", "paper_completion"} <= tables:
        return 0
    return conn.execute("SELECT COUNT(*) FROM paper_entry e LEFT JOIN paper_completion c "
                        "ON c.book_version_id=e.book_version_id AND c.fixture_api_id=e.fixture_api_id "
                        "WHERE c.fixture_api_id IS NULL").fetchone()[0]


def switch_book(conn, *, root, directories, recovery_dir, operation_id, version_id, bundle_dir, candidate_input):
    """Activate the registered corrected version, then RE-BIND the active forward epoch to
    it. Without the re-bind every later paper cycle fails 'forward_method_not_activated'
    (review 2026-09-11). Both steps journal into their own recovery directories."""
    from pathlib import Path
    from prediction_market_soccer.ops import version_workflow as VW
    from prediction_market_soccer.ops import owner_authorized_correction as O
    from prediction_market_soccer.util import forward_methods as FM
    from prediction_market_soccer.util.frozen_strategy_store import active_version, read_book
    cur = read_book(conn)
    inputs = json.loads((Path(bundle_dir) / "input_manifest.json").read_text())
    prev = inputs["previous_book"]
    if prev["version_id"] != cur["version"]["version_id"] or prev["ledger_id"] != cur["ledger"]["ledger_id"]:
        raise ValueError("The bundle was built against a different book than the one now active; rebuild")
    if prev["forward_signature"] != O.forward_signature(cur["ledger"]["records"]):
        raise ValueError("Forward rows changed since the bundle was built; rebuild")
    if unsealed_paper_entries(conn):
        raise ValueError("Paper entries with unsealed completions exist; wait until they are sealed")
    expected = VW.head(conn, directories)
    render = make_render(conn, root=root, candidate_input=candidate_input)
    head = VW.switch(conn, root=root, directories=directories, recovery_dir=recovery_dir, operation_id=operation_id,
                     expected_head=expected, version_id=version_id, render=render, backtest_bundle=str(bundle_dir))
    epoch = FM.active_epoch(conn)
    if epoch and epoch["book_version_id"] != active_version(conn)["version_id"]:
        rdir = Path(recovery_dir).parent / (Path(recovery_dir).name + "_epoch")
        VW.activate_forward_method(conn, root=root, directories=directories, recovery_dir=rdir,
                                   operation_id=operation_id + "-epoch", expected_head=VW.head(conn, directories),
                                   manifest=epoch["manifest"])
    epoch = FM.active_epoch(conn)
    if not epoch or epoch["book_version_id"] != active_version(conn)["version_id"]:
        raise RuntimeError("Forward epoch is not bound to the newly active book")
    return head


def rebind_epoch_after_restore(conn, *, root, directories, recovery_dir, operation_id):
    """After switch(restoring=True) the epoch still points at the replaced version."""
    from pathlib import Path
    from prediction_market_soccer.ops import version_workflow as VW
    from prediction_market_soccer.util import forward_methods as FM
    from prediction_market_soccer.util.frozen_strategy_store import active_version
    epoch = FM.active_epoch(conn)
    if epoch and epoch["book_version_id"] != active_version(conn)["version_id"]:
        VW.activate_forward_method(conn, root=root, directories=directories, recovery_dir=Path(recovery_dir),
                                   operation_id=operation_id, expected_head=VW.head(conn, directories), manifest=epoch["manifest"])
    epoch = FM.active_epoch(conn)
    assert epoch and epoch["book_version_id"] == active_version(conn)["version_id"]
