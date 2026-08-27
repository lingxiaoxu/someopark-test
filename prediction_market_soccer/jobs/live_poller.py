"""Match-day in-play poller (plan 05 §1 live_poller, 04 §4c).

During matches, polls live data every ~15-30s and turns the live model
(model/inplay.py) + tactics (strategy/inplay_tactics.py) into in-play trade
signals: draw time-value / level-late take-profit, convergence take-profit, and
xG momentum. Writes data/output/inplay_signals.json each poll.

Cadence (plan 02 §3.2): API-Football live data refreshes ~every 15s; poll floor
is CONFIG.soccer.ttl_live (30s) to stay frugal. Idle (no live match) → long sleep.
Nothing here places an order; signals flow to the gated executor.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.model.inplay import live_match_prob
from prediction_market_soccer.strategy.inplay_tactics import (
    convergence_take_profit,
    draw_trade_signal,
    live_momentum_from_store,
)

log = logging.getLogger("live_poller")
_LIVE = ("1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "SUSP")


def _red_counts(conn, fixture_id: int, home_api: int, away_api: int) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT team_api_id, COUNT(*) n FROM fixture_event "
        "WHERE fixture_api_id=? AND type='Card' AND detail LIKE '%Red%' GROUP BY team_api_id",
        (fixture_id,)).fetchall()
    d = {r["team_api_id"]: r["n"] for r in rows}
    return d.get(home_api, 0), d.get(away_api, 0)


def generate_inplay_signals(conn=None, sm=None) -> list[dict]:
    """In-play signals for every currently-live fixture in the store."""
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.strength import build_strength

    conn = conn or store.init_db()
    sm = sm or build_strength(load_prior())
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    live = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, elapsed "
        "FROM fixture WHERE status_short IN ({})".format(",".join("?" * len(_LIVE))), _LIVE).fetchall()
    out: list[dict] = []
    for fx in live:
        hi, ai = cmap.get(fx["home_api_id"]), cmap.get(fx["away_api_id"])
        if not (hi and ai):
            continue
        minute = fx["elapsed"] or 0
        gh, ga = fx["home_goals"] or 0, fx["away_goals"] or 0
        rh, ra = _red_counts(conn, fx["api_id"], fx["home_api_id"], fx["away_api_id"])
        lam_h, lam_a = sm.pair_lambdas(hi, ai)
        lp = live_match_prob(lam_h, lam_a, minute, gh, ga, red_home=rh, red_away=ra)

        actions = [draw_trade_signal(lp)]
        # Convergence take-profit on whichever side is winning.
        if gh != ga:
            actions.append(convergence_take_profit("home" if gh > ga else "away", 0.5, lp))
        actions.append(live_momentum_from_store(conn, fx["api_id"], fx["home_api_id"],
                                                fx["away_api_id"], minute, gh, ga))
        for a in actions:
            if a.act != "HOLD":
                out.append({"fixture_id": fx["api_id"], "home": hi, "away": ai,
                            "minute": minute, "score": f"{gh}-{ga}", "reds": f"{rh}-{ra}",
                            **asdict(a)})
    return out


def poll_once(conn=None, *, ingest: bool = True) -> dict:
    """One in-play poll: refresh live data (+xG), generate signals, persist."""
    from prediction_market_soccer.ingest import store

    conn = conn or store.init_db()
    n_live = 0
    if ingest:
        try:
            from prediction_market_soccer.ingest.api_football import ApiFootball
            from prediction_market_soccer.ingest.soccer_ingest import sync_live
            n_live = sync_live(ApiFootball(conn), conn)
        except Exception as e:
            log.warning("live ingest failed: %s", e)
    signals = generate_inplay_signals(conn)
    # Full opportunity finder: live fair (xG-shaded) vs market → relative value +
    # cross-venue lock arb + tactics. quote_sources plug in when single-match
    # markets are live (Kalshi per-match / Polymarket US / Global).
    opportunities = []
    try:
        from prediction_market_soccer.strategy.inplay_arb import find_opportunities
        opportunities = find_opportunities(conn=conn, quote_sources=_live_quote_sources(conn))
    except Exception as e:
        log.warning("opportunity finder skipped: %s", e)

    CONFIG.paths.ensure()
    ts = datetime.now(timezone.utc).isoformat()
    (CONFIG.paths.output / "inplay_signals.json").write_text(
        json.dumps({"ts": ts, "n_live": n_live, "signals": signals}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    (CONFIG.paths.output / "inplay_opportunities.json").write_text(
        json.dumps({"ts": ts, "n_live": n_live, "opportunities": opportunities}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return {"n_live": n_live, "n_signals": len(signals), "signals": signals,
            "n_opportunities": len(opportunities), "opportunities": opportunities}


def _live_quote_sources(conn) -> dict:
    """Per-match live market-quote functions per venue.

    Kalshi single-match (KXWCGAME) 3-way is live and wired. Polymarket US/Global
    single-match plug in here when their markets populate.
    """
    sources: dict = {}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    try:
        # CLUB EDITION: one KalshiDiscovery PER COMPETITION with live/imminent fixtures
        # (series tickers are per-league, §2.2); accessors route by the fixture's league_id.
        from prediction_market_soccer.config.leagues import active
        from prediction_market_soccer.venues.kalshi.discovery import KalshiDiscovery
        lid_to_key = {c.api_football_id: c.key for c in active()}
        live_lids = {r["league_id"] for r in conn.execute(
            "SELECT DISTINCT league_id FROM fixture "
            "WHERE kickoff_ts >= datetime('now','-6 hours') AND kickoff_ts <= datetime('now','+6 hours')")}
        ds: dict[int, KalshiDiscovery] = {}
        for lid in live_lids:
            key = lid_to_key.get(lid)
            if not key:
                continue
            try:
                d = KalshiDiscovery(key)
                d.match_index()    # warm the 3-way event→market cache once per poll
                d.totals_index()
                d.advance_index()  # advance cache (caps.advance fixtures only have entries)
                ds[lid] = d
            except Exception as e:  # noqa: BLE001 — one comp's venue outage ≠ all comps
                log.warning("Kalshi discovery %s unavailable: %s", key, e)

        def _resolve(fixture_id: int):
            fx = conn.execute("SELECT league_id, home_api_id, away_api_id FROM fixture WHERE api_id=?",
                              (fixture_id,)).fetchone()
            if not fx:
                return None, None, None
            d = ds.get(fx["league_id"])
            hi, ai = cmap.get(fx["home_api_id"]), cmap.get(fx["away_api_id"])
            return d, hi, ai

        def kalshi_q(fixture_id: int) -> dict:
            d, hi, ai = _resolve(fixture_id)
            return (d.match_quotes(hi, ai) or {}) if (d and hi and ai) else {}

        def kalshi_totals_q(fixture_id: int) -> dict:
            d, hi, ai = _resolve(fixture_id)
            return (d.totals_quotes(hi, ai) or {}) if (d and hi and ai) else {}

        def kalshi_advance_q(fixture_id: int) -> dict:
            d, hi, ai = _resolve(fixture_id)
            return (d.advance_quotes(hi, ai) or {}) if (d and hi and ai) else {}

        def kalshi_corners_q(fixture_id: int) -> dict:
            d, hi, ai = _resolve(fixture_id)
            return (d.corners_quotes(hi, ai) or {}) if (d and hi and ai) else {}

        if ds:
            sources["kalshi"] = kalshi_q
            sources["kalshi_totals"] = kalshi_totals_q
            sources["kalshi_advance"] = kalshi_advance_q
            sources["kalshi_corners"] = kalshi_corners_q
    except Exception as e:
        log.warning("Kalshi match quotes unavailable: %s", e)

    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from prediction_market_soccer.venues.polymarket_us.discovery import PolymarketUSDiscovery
        ud = PolymarketUSDiscovery()
        ud.code_map()   # warm once

        def _et_date(fixture_id: int):
            fx = conn.execute("SELECT home_api_id, away_api_id, kickoff_ts FROM fixture WHERE api_id=?",
                              (fixture_id,)).fetchone()
            hi, ai = cmap.get(fx["home_api_id"]), cmap.get(fx["away_api_id"])
            if not (hi and ai and fx["kickoff_ts"]):
                return None, None, None
            et = datetime.fromisoformat(fx["kickoff_ts"]).astimezone(ZoneInfo("America/New_York")).date().isoformat()
            return hi, ai, et

        def us_q(fixture_id: int) -> dict:
            hi, ai, et = _et_date(fixture_id)
            return (ud.match_quotes(hi, ai, et) or {}) if et else {}

        def us_totals_q(fixture_id: int) -> dict:
            hi, ai, et = _et_date(fixture_id)
            return (ud.totals_quotes(hi, ai, et) or {}) if et else {}

        def us_advance_q(fixture_id: int) -> dict:
            hi, ai, et = _et_date(fixture_id)
            return (ud.advance_quotes(hi, ai, et) or {}) if et else {}

        sources["poly_us"] = us_q
        sources["poly_us_totals"] = us_totals_q
        sources["poly_us_advance"] = us_advance_q
    except Exception as e:
        log.warning("Polymarket US match quotes unavailable: %s", e)
    return sources


def main() -> None:
    ap = argparse.ArgumentParser(description="Club-football in-play poller (all enabled competitions)")
    ap.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                    help="poll every N seconds (>= ttl_live); 0 = once")
    ap.add_argument("--no-ingest", action="store_true", help="use stored live data only")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    def _go():
        res = poll_once(ingest=not args.no_ingest)
        log.info("poll: %d live, %d signals, %d opportunities", res["n_live"],
                 res["n_signals"], res.get("n_opportunities", 0))
        for o in res.get("opportunities", [])[:10]:
            log.info("  [%s] %s %s %s @%s' %s → %s %s edge=%s: %s", o["kind"], o["match"],
                     o["score"], "", o["minute"], o["venue"], o["action"], o["side"], o["edge"],
                     o["reason"][:46])
        return res

    if args.loop:
        interval = max(args.loop, CONFIG.soccer.ttl_live)
        while True:
            try:
                res = _go()
            except Exception as e:
                log.exception("poll failed: %s", e)
                res = {"n_live": 0}
            time.sleep(interval if res["n_live"] else max(300, interval * 10))  # idle → back off
    else:
        _go()


if __name__ == "__main__":
    main()
