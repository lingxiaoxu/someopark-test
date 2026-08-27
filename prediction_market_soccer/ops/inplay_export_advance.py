"""Live in-play export — 2-WAY "ADVANCE" FORK (plan 24 §6) of inplay_export.py.

The knockout who-advances twin of ops/inplay_export.py. For every live KNOCKOUT fixture it
emits the live 2-way advance view: model home/away advance (incl. ET+penalties, reg/et/pens
split), live advance venue prices, the 2-way opportunities (strategy/inplay_arb_advance), and
the 2-way hedge suggestion (strategy/inplay_hedge_advance). Writes
data/output/inplay_live_advance.json — SEPARATE from the 3-way inplay_live.json, which is
UNCHANGED and runs in parallel. Read-only (market data only; no orders).
Run:  python -m prediction_market_soccer.ops.inplay_export_advance --loop 30
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG

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
    from prediction_market_soccer.util.pricing import mid_cents, to_cents
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
        from prediction_market_soccer.jobs.live_poller import _live_quote_sources
        src = _live_quote_sources(conn)
    except Exception:
        return {}
    out = {}
    if "kalshi_advance" in src:
        out["kalshi"] = src["kalshi_advance"]
    if "poly_us_advance" in src:
        out["poly_us"] = src["poly_us_advance"]
    return out


def _match_hedge_advance(conn, fx, pick_name, hi, ai, sm, gh, ga, minute, lap, prices, *, period="reg"):
    """2-way hedge suggestion (plan 24 §4): protect OUR directional advance position by buying
    the opponent's advance leg. Our pick = the positive-value side vs the PRE advance market;
    entry from the PRE advance ask. None when not applicable.

    Stays live through the penalty shootout: the break-even / payoff math is period-agnostic
    (it works off the two advance legs), so we only skip on minute<=0 in normal play — during
    pens `elapsed` is None (minute 0) but the position still needs protecting, so we don't gate."""
    from prediction_market_soccer.strategy import inplay_hedge_advance as ih
    from prediction_market_soccer.util.pricing import to_cents
    if minute <= 0 and period != "pens":
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
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.inplay_advance import live_advance_prob
    from prediction_market_soccer.model.penalties import shootout_win_prob_detailed
    from prediction_market_soccer.model.squad_strength import build_strength_live
    from prediction_market_soccer.strategy.inplay_arb_advance import find_opportunities_advance
    from prediction_market_soccer.util.pricing import model_cents, to_cents

    conn = conn or store.init_db()
    name, zh = {}, {}
    for r in conn.execute("SELECT DISTINCT club_id, name, zh FROM club_registry"):
        name[r["club_id"]] = r["name"]
        zh[r["club_id"]] = r["zh"] or ""
    from prediction_market_soccer.config.leagues import active, by_api_id, caps_for
    from prediction_market_soccer.ingest.soccer_ingest import carry_of, leg_of
    from prediction_market_soccer.model.strength_cache import composite_live_strength
    sm = composite_live_strength(conn)
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    _lids = tuple(c.api_football_id for c in active())
    live = conn.execute(
        "SELECT league_id, api_id, home_api_id, away_api_id, home_goals, away_goals, elapsed, status_short, round "
        "FROM fixture WHERE status_short IN ({}) AND league_id IN ({}) "
        "AND kickoff_ts >= datetime('now', '-10 hours') "
        "ORDER BY elapsed DESC".format(",".join("?" * len(_LIVE)), ",".join("?" * len(_lids))),
        (*_LIVE, *_lids)).fetchall()

    quote_sources = _adv_sources(conn) if with_venues else {}
    scan_error = None
    try:
        opps = find_opportunities_advance(conn=conn, sm=sm, quote_sources=quote_sources)
    except Exception as e:   # noqa: BLE001 — a scanner crash must not kill the export
        # ...but it must not look like a quiet market either. An empty list with no
        # trace is indistinguishable from "nothing to trade", and that is how a crash
        # inside the scanner could run for days unnoticed.
        opps = []
        scan_error = f"{type(e).__name__}: {e}"
        import traceback
        print(f"[inplay] opportunity scan FAILED — no signals this cycle: {scan_error}")
        traceback.print_exc()
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
        comp = by_api_id(fx["league_id"])
        leg, agg = leg_of(conn, fx["api_id"])          # running aggregate — display only
        _, carry = carry_of(conn, fx["api_id"])        # first leg only — what the model carries
        cp = caps_for(comp.key, fx["round"], leg=leg) if comp else None
        if not (cp and cp.advance):
            continue   # §3.0: the advance path exists ⇔ caps.advance (C1 done right)
        minute = fx["elapsed"] or 0
        gh, ga = fx["home_goals"] or 0, fx["away_goals"] or 0
        # C5: the deciding leg of a two-legged tie carries the leg-1 AGGREGATE in — the
        # live advance state is the aggregate, so shift the score fed to the model.
        # (tie table stores agg for team_a = leg-1 home = TODAY'S AWAY side on leg 2.)
        carry_h = carry_a = 0
        # Carry the FIRST LEG only. `agg` already folds in leg 2 the moment it kicks
        # off, so adding it to the live leg-2 score counted those goals twice — a side
        # 1-0 up on the night read as 2-0 up on aggregate from its own single goal.
        if cp.two_leg and leg == 2 and carry:
            try:
                a_a, a_b = (int(x) for x in carry.split("-"))
                carry_h, carry_a = a_b, a_a
            except ValueError:
                pass
        gh_eff, ga_eff = gh + carry_h, ga + carry_a
        period = _period_for(fx["status_short"])
        xg = {r["team_api_id"]: r["xg"] for r in conn.execute(
            "SELECT team_api_id, xg FROM fixture_stats WHERE fixture_api_id=?", (fx["api_id"],))}
        reds = {r["team_api_id"]: r["n"] for r in conn.execute(
            "SELECT team_api_id, COUNT(*) n FROM fixture_event WHERE fixture_api_id=? "
            "AND type='Card' AND detail LIKE '%Red%' GROUP BY team_api_id", (fx["api_id"],))}
        rh, ra = reds.get(fx["home_api_id"], 0), reds.get(fx["away_api_id"], 0)
        lam_h, lam_a = sm.pair_lambdas(hi, ai, knockout=True, neutral=bool(cp.neutral))
        # Live penalty-shootout tally (kicks already taken/scored per side) from the stored
        # shootout events (comments='Penalty Shootout'; detail 'Penalty'=scored, 'Missed
        # Penalty'=miss). Feeds the shootout DP so the advance prob updates PER KICK during pens
        # instead of sitting on the static pre-shootout strength prior. Zero outside pens.
        so_taken = {"home": 0, "away": 0}
        so_scored = {"home": 0, "away": 0}
        if period == "pens":
            for r in conn.execute(
                "SELECT team_api_id, detail, COUNT(*) n FROM fixture_event WHERE fixture_api_id=? "
                "AND comments='Penalty Shootout' GROUP BY team_api_id, detail", (fx["api_id"],)):
                side = ("home" if r["team_api_id"] == fx["home_api_id"]
                        else "away" if r["team_api_id"] == fx["away_api_id"] else None)
                if side is None:
                    continue
                so_taken[side] += r["n"]
                if r["detail"] == "Penalty":
                    so_scored[side] += r["n"]
        shootout_home = shootout_win_prob_detailed(
            sm, hi, ai, taken_a=so_taken["home"], scored_a=so_scored["home"],
            taken_b=so_taken["away"], scored_b=so_scored["away"])
        lap = live_advance_prob(lam_h, lam_a, minute, gh_eff, ga_eff, period=period,
                                et_then_pens=bool(cp.et_then_pens),
                                shootout_home=shootout_home,
                                red_home=rh, red_away=ra, xg_home=xg.get(fx["home_api_id"]),
                                xg_away=xg.get(fx["away_api_id"]),
                                et_home_goals=gh_eff, et_away_goals=ga_eff)
        prices = {"model_c": model_cents({"home": lap.p_home_advance, "away": lap.p_away_advance})}
        for v, fn in quote_sources.items():
            try:
                q = fn(fx["api_id"])
            except Exception:
                q = None
            if q:
                prices[v] = _q2c(q)
        fixture_opps = by_fixture.get(fx["api_id"], [])
        # During the shootout there is NO open play, so open-play tactics (momentum / xG-chase /
        # possession / fade / red-card / comeback) are meaningless — keep only model-vs-market
        # value (relative_value / lock_arb, now driven by the LIVE shootout model) and any
        # held-position "manage" exits. The 90'+ET reads are already settled.
        if period == "pens":
            fixture_opps = [o for o in fixture_opps
                            if o.get("reason_key") in ("relative_value", "lock_arb")
                            or o.get("intent") == "manage"]
        # Confidence tier + staking gate on every advance opportunity (same validated rules as
        # the 3-way view; the advance signals are all home/away so the tiering applies directly).
        # Without this the advance opportunities showed an empty 置信 column.
        try:
            from prediction_market_soccer.strategy import inplay_confidence as ic
            ctx = ic.match_context(hi, ai, name.get(hi, hi), name.get(ai, ai), gh, ga, minute,
                                   model={"home": lap.p_home_advance, "away": lap.p_away_advance},
                                   knockout=True)
            for _o in fixture_opps:
                ic.annotate(_o, ctx)
        except Exception:
            pass
        try:
            hedge = _match_hedge_advance(conn, fx, None, hi, ai, sm, gh, ga, minute, lap, prices, period=period)
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
            # Live shootout tally (scored) for the UI, None outside pens.
            "shootout": ({"home": so_scored["home"], "away": so_scored["away"]} if period == "pens" else None),
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

    return {"ts": datetime.now(timezone.utc).isoformat(), "n_live": len(matches),
            **({"scan_error": scan_error} if scan_error else {}),
            "matches": matches}


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
