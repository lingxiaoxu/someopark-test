"""ops/decision_backtest.py — PIT backtest of the pre-match DECISION model (plan 20).

Answers, on the REAL settled WC-2026 matches and REAL recorded venue prices, the
question "what pre-match decision rule makes the most money / has the best CLV?":

  * Strategy A — argmax  (the CURRENT rule): bet the most-likely side, $1 flat.
  * Strategy B — value   : bet the best net-edge side (decision_model.decide), $1 flat.
  * Strategy C — value + confidence sizing: same side, stake scaled to [$0.2, $2].

Everything is strictly point-in-time: the model probabilities are recomputed for
each match with features cut at its kickoff (same engine as param_sweep), and the
prices come from milestone_snapshot (PRE = entry, T15..T75 / FT = exits).

For the VALUE pick it also reports the PnL of each EXIT timing (hold-to-FT vs sell
at T15/T30/HT/T60/T75), so we can see whether holding to settlement or taking an
in-play exit pays better.

Run:  python -m prediction_market.ops.decision_backtest
"""
from __future__ import annotations

from dataclasses import replace

from prediction_market.config.config import CONFIG
from prediction_market.strategy.decision_model import SideQuote, decide

_SIDES = ("home", "draw", "away")
_EXITS = ("T15", "T30", "HT", "T60", "T75", "FT")
_FINISHED = ("FT", "AET", "PEN")


# ── PnL accounting for a YES contract bought at entry_c ¢ (pays 100¢ if it wins) ──
def _pnl_hold(stake: float, entry_c: float, won: bool) -> float:
    """Hold to settlement: each contract → 100¢ (win) or 0¢ (loss)."""
    if entry_c <= 0:
        return 0.0
    return stake * ((100.0 - entry_c) / entry_c) if won else -stake


def _pnl_exit(stake: float, entry_c: float, exit_c: float) -> float:
    """Sell before settlement at exit_c ¢ (the pick side's price at that milestone)."""
    if entry_c <= 0 or exit_c is None:
        return 0.0
    return stake * ((exit_c - entry_c) / entry_c)


def _quotes_from_row(row) -> dict:
    """Best (cheapest) executable ask per side across the two venues + its de-vig."""
    q = {}
    for s in _SIDES:
        asks = []
        ka, pa = row[f"kalshi_{s}_ask"], row[f"poly_{s}_ask"]
        if ka is not None:
            asks.append((ka, "kalshi"))
        if pa is not None:
            asks.append((pa, "poly_us"))
        if not asks:
            q[s] = SideQuote()
            continue
        ask, ven = min(asks, key=lambda t: t[0])
        q[s] = SideQuote(ask=ask, devig=row[f"devig_{s}"], venue=ven)
    return q


# venue label (as used by the decision model) → milestone_snapshot column prefix
_COL = {"kalshi": "kalshi", "poly_us": "poly", "poly": "poly"}


def _exit_cents(row, side: str, venue: str | None):
    """Price you'd sell the pick side at this milestone — the bid (selling hits the
    bid), falling back to ask, on the entry venue (else either venue)."""
    if row is None:
        return None
    pref = [_COL.get(venue)] if venue else []
    pref += [p for p in ("kalshi", "poly") if p != _COL.get(venue)]
    for p in pref:
        bid = row[f"{p}_{side}_bid"]
        if bid is not None:
            return bid * 100.0
    for p in pref:
        ask = row[f"{p}_{side}_ask"]
        if ask is not None:
            return ask * 100.0
    return None


def backtest(conn=None, *, calib_confidence: float = 0.25) -> dict:
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.altdata_adjust import altdata_index
    from prediction_market.model.form_strength import form_index
    from prediction_market.model.match_pricing import price_match, is_knockout
    from prediction_market.model.probability_calibration import load_calibration, apply_calibration
    from prediction_market.model.squad_strength import build_strength_live

    conn = conn or store.init_db()
    prior = load_prior()
    cal = load_calibration()
    cfg_model = CONFIG.model
    name_of = {t.team_id: t.name for t in prior.teams}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    fixtures = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, round, kickoff_ts "
        "FROM fixture WHERE status_short IN ({}) AND home_goals IS NOT NULL "
        "ORDER BY kickoff_ts".format(",".join("?" * len(_FINISHED))), _FINISHED).fetchall()

    # milestone rows indexed by (fixture, milestone)
    ms = {}
    for r in conn.execute("SELECT * FROM milestone_snapshot"):
        ms[(r["fixture_api_id"], r["milestone"])] = r

    strategies = {"A_argmax": [], "B_value_flat": [], "C_value_sized": []}
    exit_pnl = {e: [] for e in (*_EXITS,)}   # value pick, $1 flat, PnL by exit timing
    clv_list, detail = [], []

    for f in fixtures:
        hi, ai = cmap.get(f["home_api_id"]), cmap.get(f["away_api_id"])
        if not (hi and ai):
            continue
        pre = ms.get((f["api_id"], "PRE"))
        if pre is None:
            continue
        ko = is_knockout(f["round"])

        # PIT calibrated model probs (features cut at kickoff).
        sm = build_strength_live(conn, prior, cfg_model)
        if cfg_model.oppadj_def_weight or cfg_model.oppadj_off_weight:
            sm = replace(sm, adj=altdata_index(conn, sm.ratings, as_of=f["kickoff_ts"]))
        mp = price_match(sm, hi, ai, knockout=ko)
        p = apply_calibration([mp.p_home, mp.p_draw, mp.p_away], cal, knockout=ko)
        model = {"home": p[0], "draw": p[1], "away": p[2]}

        result = "home" if f["home_goals"] > f["away_goals"] else ("draw" if f["home_goals"] == f["away_goals"] else "away")

        # PIT recent-form for the confidence blend.
        try:
            fi = form_index(conn, as_of=f["kickoff_ts"])
            form = {"home_z": fi[hi].form_z if hi in fi else None,
                    "away_z": fi[ai].form_z if ai in fi else None}
        except Exception:
            form = None

        quotes = _quotes_from_row(pre)

        # ── Strategy A: argmax, $1 flat ──
        a_side = max(_SIDES, key=lambda s: model[s])
        a_q = quotes.get(a_side)
        if a_q and a_q.ask is not None:
            a_entry = a_q.ask * 100.0
            strategies["A_argmax"].append({"side": a_side, "won": a_side == result,
                                           "pnl": _pnl_hold(1.0, a_entry, a_side == result)})

        # ── Strategies B/C: value pick via decision_model ──
        d = decide(model, quotes, calib_confidence=calib_confidence, form=form, gate_open=True)
        if d.side is not None and d.price_cents:
            entry = d.price_cents
            won = d.side == result
            strategies["B_value_flat"].append({"side": d.side, "won": won, "pnl": _pnl_hold(1.0, entry, won)})
            strategies["C_value_sized"].append({"side": d.side, "won": won, "stake": d.stake_usd,
                                                 "pnl": _pnl_hold(d.stake_usd, entry, won)})
            # exit-timing PnL for the value pick ($1 flat for apples-to-apples)
            for e in _EXITS:
                if e == "FT":
                    exit_pnl["FT"].append(_pnl_hold(1.0, entry, won))
                else:
                    ec = _exit_cents(ms.get((f["api_id"], e)), d.side, d.venue)
                    if ec is not None:
                        exit_pnl[e].append(_pnl_exit(1.0, entry, ec))
            # CLV ¢ (pick side T75 − entry); positive ⇒ market drifted our way
            t75c = _exit_cents(ms.get((f["api_id"], "T75")), d.side, d.venue)
            if t75c is not None:
                clv_list.append(t75c - entry)
            detail.append({"date": (f["kickoff_ts"] or "")[:10],
                           "match": f"{name_of.get(hi, hi)} v {name_of.get(ai, ai)}",
                           "argmax": a_side, "value": d.side, "result": result,
                           "entry_c": round(entry, 1), "stake": d.stake_usd,
                           "edge": d.net_edge, "k": d.confidence_k})

    def _summ(rows, money_key="pnl"):
        n = len(rows)
        if not n:
            return {"n": 0}
        wins = sum(1 for r in rows if r.get("won"))
        pnl = sum(r[money_key] for r in rows)
        staked = sum(r.get("stake", 1.0) for r in rows)
        return {"n": n, "wins": wins, "win_rate": round(wins / n, 3),
                "pnl": round(pnl, 3), "staked": round(staked, 2),
                "roi": round(pnl / staked, 4) if staked else None}

    out = {"strategies": {k: _summ(v) for k, v in strategies.items()},
           "exit_timing": {e: {"n": len(v), "pnl": round(sum(v), 3),
                               "roi": round(sum(v) / len(v), 4) if v else None} for e, v in exit_pnl.items()},
           "clv": {"n": len(clv_list), "avg_cents": round(sum(clv_list) / len(clv_list), 2) if clv_list else None,
                   "pct_positive": round(sum(1 for c in clv_list if c > 0) / len(clv_list), 3) if clv_list else None},
           "detail": detail, "calib_confidence": calib_confidence}
    return out


def _fmt(d: dict) -> str:
    L = []
    L.append("=" * 68)
    L.append("PRE-MATCH DECISION BACKTEST  (PIT, real settled matches + real prices)")
    L.append("=" * 68)
    L.append(f"{'strategy':<16}{'n':>4}{'win%':>8}{'PnL($)':>10}{'staked':>9}{'ROI':>9}")
    for k, s in d["strategies"].items():
        if s.get("n"):
            L.append(f"{k:<16}{s['n']:>4}{s['win_rate']*100:>7.1f}%{s['pnl']:>10.2f}{s['staked']:>9.2f}{(s['roi'] or 0)*100:>8.1f}%")
    L.append("")
    L.append("EXIT TIMING for the VALUE pick ($1 flat) — hold-to-FT vs sell early:")
    L.append(f"{'exit':<8}{'n':>4}{'PnL($)':>10}{'avg ROI':>10}")
    for e in _EXITS:
        s = d["exit_timing"][e]
        L.append(f"{e:<8}{s['n']:>4}{s['pnl']:>10.2f}{(s['roi'] or 0)*100:>9.1f}%")
    c = d["clv"]
    L.append("")
    L.append(f"CLV (value pick, T75−entry): avg {c['avg_cents']}¢  positive on {(c['pct_positive'] or 0)*100:.0f}% of {c['n']} bets")
    L.append(f"(calib_confidence used for sizing = {d['calib_confidence']:+.2f})")
    return "\n".join(L)


def main():
    d = backtest()
    print(_fmt(d))


if __name__ == "__main__":
    main()
