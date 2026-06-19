"""Live in-play export for the frontend (the "In-Play Arbitrage" artifact data).

For EVERY currently-live fixture, emits the full intra-game view the desk needs:
  * live state-dependent model 3-way (minute + score + red cards + xG shading),
    fair draw, P(over 2.5), expected remaining goals;
  * the live score / minute / red cards / per-team xG;
  * ALL in-play opportunities for that match (from strategy/inplay_arb.find_opportunities):
    cross-venue lock-arb, relative value vs Kalshi/Poly US, and tactics
    (draw take-profit, convergence take-profit, xG momentum).

Writes data/output/inplay_live.json. Read-only (market data only; no orders).
Run per-minute during matches:  python -m prediction_market.ops.inplay_export --loop 30
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

from prediction_market.config import CONFIG

_LIVE = ("1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "SUSP")


def build(conn=None, *, with_venues: bool = True) -> dict:
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.inplay import live_match_prob
    from prediction_market.model.squad_strength import build_strength_live
    from prediction_market.strategy.inplay_arb import find_opportunities

    conn = conn or store.init_db()
    prior = load_prior()
    name = {t.team_id: t.name for t in prior.teams}
    zh = {t.team_id: t.zh for t in prior.teams}
    sm = build_strength_live(conn, prior, xg_form=True)   # live match → all data is prior (PIT)
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    live = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, elapsed, status_short "
        "FROM fixture WHERE status_short IN ({}) ORDER BY elapsed DESC".format(",".join("?" * len(_LIVE))),
        _LIVE).fetchall()

    # All opportunities once (grouped by fixture), with live venue quotes if available.
    quote_sources = {}
    if with_venues:
        try:
            from prediction_market.jobs.live_poller import _live_quote_sources
            quote_sources = _live_quote_sources(conn)
        except Exception:
            quote_sources = {}
    try:
        opps = find_opportunities(conn=conn, sm=sm, quote_sources=quote_sources)
    except Exception:
        opps = []
    # Per-contract ¢ on every opportunity (market/fair/edge → ¢), ADD ONLY.
    from prediction_market.util.pricing import to_cents, model_cents, quote_to_cents
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
        minute = fx["elapsed"] or 0
        gh, ga = fx["home_goals"] or 0, fx["away_goals"] or 0
        xg = {r["team_api_id"]: r["xg"] for r in conn.execute(
            "SELECT team_api_id, xg FROM fixture_stats WHERE fixture_api_id=?", (fx["api_id"],))}
        reds = {r["team_api_id"]: r["n"] for r in conn.execute(
            "SELECT team_api_id, COUNT(*) n FROM fixture_event WHERE fixture_api_id=? "
            "AND type='Card' AND detail LIKE '%Red%' GROUP BY team_api_id", (fx["api_id"],))}
        rh, ra = reds.get(fx["home_api_id"], 0), reds.get(fx["away_api_id"], 0)
        lam_h, lam_a = sm.pair_lambdas(hi, ai)
        lp = live_match_prob(lam_h, lam_a, minute, gh, ga, red_home=rh, red_away=ra,
                             xg_home=xg.get(fx["home_api_id"]), xg_away=xg.get(fx["away_api_id"]))
        # Live venue quotes (¢) per side for this fixture, alongside model-implied ¢.
        prices = {"model_c": model_cents({"home": lp.p_home, "draw": lp.p_draw, "away": lp.p_away})}
        for v, fn in quote_sources.items():
            try:
                q = fn(fx["api_id"])
            except Exception:
                q = None
            if q:
                prices[v] = quote_to_cents(q)
        matches.append({
            "fixture_id": fx["api_id"],
            "status": fx["status_short"],
            "minute": minute,
            "score": f"{gh}-{ga}",
            "reds": f"{rh}-{ra}",
            "home": {"id": hi, "name": name.get(hi, hi), "zh": zh.get(hi, "")},
            "away": {"id": ai, "name": name.get(ai, ai), "zh": zh.get(ai, "")},
            "model": {
                "home": round(lp.p_home, 4), "draw": round(lp.p_draw, 4), "away": round(lp.p_away, 4),
                "over_2_5": round(float(lp.p_over_total.get(2.5, 0.0)), 4),
                "exp_remaining_goals": round(lp.exp_remaining_goals, 3),
            },
            "xg": {"home": xg.get(fx["home_api_id"]), "away": xg.get(fx["away_api_id"])},
            "prices": prices,
            "opportunities": by_fixture.get(fx["api_id"], []),
        })

    return {"ts": datetime.now(timezone.utc).isoformat(), "n_live": len(matches), "matches": matches}


def main() -> None:
    ap = argparse.ArgumentParser(description="Export live in-play view for the frontend")
    ap.add_argument("--loop", type=int, default=0, metavar="SECONDS", help="re-export every N seconds (0 = once)")
    ap.add_argument("--no-venues", action="store_true", help="skip live venue quotes (model + tactics only)")
    args = ap.parse_args()

    def _go():
        doc = build(with_venues=not args.no_venues)
        CONFIG.paths.ensure()
        (CONFIG.paths.output / "inplay_live.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        n_opp = sum(len(m["opportunities"]) for m in doc["matches"])
        print(f"inplay_live.json: {doc['n_live']} live match(es), {n_opp} opportunities")
        for m in doc["matches"]:
            print(f"  {m['home']['name']} {m['score']} {m['away']['name']} @{m['minute']}'  "
                  f"model H{m['model']['home']:.2f}/D{m['model']['draw']:.2f}/A{m['model']['away']:.2f}  "
                  f"{len(m['opportunities'])} opps")
        return doc

    if args.loop:
        interval = max(args.loop, CONFIG.soccer.ttl_live)
        while True:
            try:
                doc = _go()
            except Exception as e:
                print(f"[warn] export failed: {e}")
                doc = {"n_live": 0}
            time.sleep(interval if doc["n_live"] else max(300, interval * 10))
    else:
        _go()


if __name__ == "__main__":
    main()
