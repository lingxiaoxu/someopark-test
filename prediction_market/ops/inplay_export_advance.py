"""Live in-play export — 2-WAY "ADVANCE" FORK (plan 24 §6) of inplay_export.py.

The knockout who-advances twin of ops/inplay_export.py. For every live KNOCKOUT fixture it
emits the live 2-way advance view: model home/away advance (incl. ET+penalties, reg/et/pens
split), live advance venue prices, the 2-way opportunities (strategy/inplay_arb_advance), and
the 2-way hedge suggestion (strategy/inplay_hedge_advance). Writes
data/output/inplay_live_advance.json — SEPARATE from the 3-way inplay_live.json, which is
UNCHANGED and runs in parallel. Read-only (market data only; no orders).
Run:  python -m prediction_market.ops.inplay_export_advance --loop 30
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

from prediction_market.config import CONFIG

_LIVE = ("1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "SUSP")
_ET_STATUS = ("ET", "BT")
_PENS_STATUS = ("P", "PEN")


def _period_for(status: str) -> str:
    if status in _PENS_STATUS:
        return "pens"
    if status in _ET_STATUS:
        return "et"
    return "reg"


def _q2c(q: dict | None) -> dict | None:
    """{home:{ask,bid},away:{...}} → adds ask_c/bid_c/mid_c per side (2-way; ADD ONLY)."""
    if not q:
        return None
    from prediction_market.util.pricing import mid_cents, to_cents
    out: dict = {}
    for side in ("home", "away"):
        s = q.get(side)
        if not s:
            out[side] = s
            continue
        ask, bid = s.get("ask"), s.get("bid")
        out[side] = {**s, "ask_c": to_cents(ask), "bid_c": to_cents(bid), "mid_c": mid_cents(ask, bid)}
    return out


def _adv_sources(conn):
    """Build {venue: fn} returning 2-way ADVANCE quotes, with plain venue names (kalshi /
    poly_us) so the advance arb + lock-arb see executable-venue names. Remaps the live
    poller's *_advance sources."""
    try:
        from prediction_market.jobs.live_poller import _live_quote_sources
        src = _live_quote_sources(conn)
    except Exception:
        return {}
    out = {}
    if "kalshi_advance" in src:
        out["kalshi"] = src["kalshi_advance"]
    if "poly_us_advance" in src:
        out["poly_us"] = src["poly_us_advance"]
    return out


def _match_hedge_advance(conn, fx, pick_name, hi, ai, sm, gh, ga, minute, lap, prices):
    """2-way hedge suggestion (plan 24 §4): protect OUR directional advance position by buying
    the opponent's advance leg. Our pick = the positive-value side vs the PRE advance market;
    entry from the PRE advance ask. None when not applicable."""
    from prediction_market.strategy import inplay_hedge_advance as ih
    from prediction_market.util.pricing import to_cents
    if minute <= 0:
        return None
    pre_row = conn.execute(
        "SELECT * FROM milestone_snapshot WHERE fixture_api_id=? AND milestone='PRE'",
        (fx["api_id"],)).fetchone()
    model_adv = {"home": lap.p_home_advance, "away": lap.p_away_advance}
    our_pick, entry_c = None, None
    if pre_row is not None:
        keys = set(pre_row.keys())
        ask = {}
        for s in ("home", "away"):
            for v in ("poly_adv", "kalshi_adv"):
                col = f"{v}_{s}_ask"
                if col in keys and pre_row[col] is not None:
                    ask[s] = pre_row[col]
                    break
        tot = sum(ask.values())
        if tot:
            edges = {s: model_adv[s] - ask[s] / tot for s in ask}   # model − de-vig market
            cand = max(edges, key=edges.get)
            if edges[cand] > 0:
                our_pick, entry_c = cand, to_cents(ask[cand])
    if our_pick not in ("home", "away"):
        return None
    pick = our_pick
    other = "away" if pick == "home" else "home"
    # opponent (hedge leg) live advance price ¢ from the venue blocks, else model.
    hedge_c = None
    for v in ("kalshi", "poly_us"):
        blk = (prices or {}).get(v)
        if blk and blk.get(other) and blk[other].get("mid_c") is not None:
            hedge_c = blk[other]["mid_c"]
            break
    if hedge_c is None:
        hedge_c = to_cents(model_adv[other])
    if entry_c is None or hedge_c is None or hedge_c >= 100.0:
        return None
    SHARES = 10.0
    pos = ih.Position(shares=SHARES, entry_c=float(entry_c), side=pick)
    quotes = ih.Quotes(home_ask=(float(entry_c) if pick == "home" else float(hedge_c)),
                       away_ask=(float(hedge_c) if pick == "home" else float(entry_c)),
                       minute=minute, score=f"{gh}-{ga}")
    be = ih.break_even_b(pos, quotes, hedge_side=other)
    if be.b is None:
        return None
    full = ih.full_hedge_b(pos, quotes, hedge_side=other)
    bs = sorted({0.0, round(be.b, 2)} | ({round(full, 2)} if full is not None else set()))
    matrix = [r.as_dict() for r in ih.payoff_matrix(pos, quotes, other, bs=bs)]
    be_row = be.payoff.as_dict() if be.payoff else None
    lead_state = ("leading" if (pick == "home" and gh > ga) or (pick == "away" and ga > gh)
                  else ("level" if gh == ga else "behind"))
    return {
        "held_side": pick, "held_team": pick_name, "hedge_side": other,
        "shares_ref": SHARES, "entry_c": round(float(entry_c), 1),
        "away_adv_c": round(float(hedge_c), 1),    # opponent-advance hedge leg price
        "break_even_b": round(be.b, 2),
        "full_hedge_b": (round(full, 2) if full is not None else None),
        "profit_if_win_c": (round(be_row[pick], 1) if be_row else None),
        "payoff": matrix, "lead_state": lead_state, "note_key": "hedge.protectAdvance",
    }


def build(conn=None, *, with_venues: bool = True) -> dict:
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.inplay_advance import live_advance_prob
    from prediction_market.model.penalties import shootout_win_prob_detailed
    from prediction_market.model.squad_strength import build_strength_live
    from prediction_market.strategy.inplay_arb_advance import find_opportunities_advance
    from prediction_market.util.pricing import model_cents, to_cents

    conn = conn or store.init_db()
    prior = load_prior()
    name = {t.team_id: t.name for t in prior.teams}
    zh = {t.team_id: t.zh for t in prior.teams}
    sm = build_strength_live(conn, prior, xg_form=True)
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    live = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, elapsed, status_short, round "
        "FROM fixture WHERE status_short IN ({}) ORDER BY elapsed DESC".format(",".join("?" * len(_LIVE))),
        _LIVE).fetchall()

    quote_sources = _adv_sources(conn) if with_venues else {}
    try:
        opps = find_opportunities_advance(conn=conn, sm=sm, quote_sources=quote_sources)
    except Exception:
        opps = []
    by_fixture: dict[int, list] = {}
    for o in opps:
        o = {**o, "market_c": to_cents(o.get("market")), "fair_c": to_cents(o.get("fair")),
             "edge_c": to_cents(o.get("edge"))}
        by_fixture.setdefault(o["fixture_id"], []).append(o)

    matches = []
    for fx in live:
        hi, ai = cmap.get(fx["home_api_id"]), cmap.get(fx["away_api_id"])
        if not (hi and ai):
            continue
        if not (bool(fx["round"]) and "group" not in str(fx["round"]).lower()):
            continue   # advance market exists only for knockout ties
        minute = fx["elapsed"] or 0
        gh, ga = fx["home_goals"] or 0, fx["away_goals"] or 0
        period = _period_for(fx["status_short"])
        xg = {r["team_api_id"]: r["xg"] for r in conn.execute(
            "SELECT team_api_id, xg FROM fixture_stats WHERE fixture_api_id=?", (fx["api_id"],))}
        reds = {r["team_api_id"]: r["n"] for r in conn.execute(
            "SELECT team_api_id, COUNT(*) n FROM fixture_event WHERE fixture_api_id=? "
            "AND type='Card' AND detail LIKE '%Red%' GROUP BY team_api_id", (fx["api_id"],))}
        rh, ra = reds.get(fx["home_api_id"], 0), reds.get(fx["away_api_id"], 0)
        lam_h, lam_a = sm.pair_lambdas(hi, ai, knockout=True, host_neutral=True)
        shootout_home = shootout_win_prob_detailed(sm, hi, ai)
        lap = live_advance_prob(lam_h, lam_a, minute, gh, ga, period=period, shootout_home=shootout_home,
                                red_home=rh, red_away=ra, xg_home=xg.get(fx["home_api_id"]),
                                xg_away=xg.get(fx["away_api_id"]), et_home_goals=gh, et_away_goals=ga)
        prices = {"model_c": model_cents({"home": lap.p_home_advance, "away": lap.p_away_advance})}
        for v, fn in quote_sources.items():
            try:
                q = fn(fx["api_id"])
            except Exception:
                q = None
            if q:
                prices[v] = _q2c(q)
        fixture_opps = by_fixture.get(fx["api_id"], [])
        try:
            hedge = _match_hedge_advance(conn, fx, None, hi, ai, sm, gh, ga, minute, lap, prices)
            if hedge is not None:
                hedge["held_team"] = name.get(hi, hi) if hedge["held_side"] == "home" else name.get(ai, ai)
        except Exception:
            hedge = None
        matches.append({
            "fixture_id": fx["api_id"],
            "status": fx["status_short"],
            "minute": minute,
            "period": period,
            "score": f"{gh}-{ga}",
            "reds": f"{rh}-{ra}",
            "home": {"id": hi, "name": name.get(hi, hi), "zh": zh.get(hi, "")},
            "away": {"id": ai, "name": name.get(ai, ai), "zh": zh.get(ai, "")},
            "model": {
                "home": round(lap.p_home_advance, 4), "away": round(lap.p_away_advance, 4),
                "p_reg_decides": round(lap.p_reg_decides, 4), "p_et_decides": round(lap.p_et_decides, 4),
                "p_pens_decides": round(lap.p_pens_decides, 4),
            },
            "xg": {"home": xg.get(fx["home_api_id"]), "away": xg.get(fx["away_api_id"])},
            "prices": prices,
            "opportunities": fixture_opps,
            "hedge_advance": hedge,
        })

    return {"ts": datetime.now(timezone.utc).isoformat(), "n_live": len(matches), "matches": matches}


def main() -> None:
    ap = argparse.ArgumentParser(description="Export live 2-way ADVANCE in-play view")
    ap.add_argument("--loop", type=int, default=0, metavar="SECONDS", help="re-export every N seconds (0 = once)")
    ap.add_argument("--no-venues", action="store_true", help="skip live venue quotes (model + tactics only)")
    args = ap.parse_args()

    def _go():
        doc = build(with_venues=not args.no_venues)
        CONFIG.paths.ensure()
        (CONFIG.paths.output / "inplay_live_advance.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        n_opp = sum(len(m["opportunities"]) for m in doc["matches"])
        print(f"inplay_live_advance.json: {doc['n_live']} live knockout match(es), {n_opp} opportunities")
        for m in doc["matches"]:
            print(f"  {m['home']['name']} {m['score']} {m['away']['name']} @{m['minute']}' [{m['period']}]  "
                  f"adv H{m['model']['home']:.2f}/A{m['model']['away']:.2f}  {len(m['opportunities'])} opps")
        return doc

    if args.loop:
        interval = max(args.loop, CONFIG.soccer.ttl_live)
        while True:
            try:
                doc = _go()
            except Exception as e:
                print(f"[warn] advance export failed: {e}")
                doc = {"n_live": 0}
            time.sleep(interval if doc["n_live"] else max(300, interval * 10))
    else:
        _go()


if __name__ == "__main__":
    main()
