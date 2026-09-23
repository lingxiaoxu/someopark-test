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
    from prediction_market_soccer.model.strength_cache import composite_live_strength, model_for_fixture
    sm = sm or composite_live_strength(conn)
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    live = conn.execute(
        "SELECT api_id, league_id, home_api_id, away_api_id, home_goals, away_goals, elapsed "
        "FROM fixture WHERE status_short IN ({})".format(",".join("?" * len(_LIVE))), _LIVE).fetchall()
    out: list[dict] = []
    for fx in live:
        hi, ai = cmap.get(fx["home_api_id"]), cmap.get(fx["away_api_id"])
        if not (hi and ai):
            log.warning("fixture %s: missing_team_mapping", fx["api_id"])
            continue
        fixture_sm = model_for_fixture(sm, fx, hi, ai)
        if fixture_sm is None:
            log.warning("fixture %s: missing_strength", fx["api_id"])
            continue
        minute = fx["elapsed"] or 0
        gh, ga = fx["home_goals"] or 0, fx["away_goals"] or 0
        rh, ra = _red_counts(conn, fx["api_id"], fx["home_api_id"], fx["away_api_id"])
        lam_h, lam_a = fixture_sm.pair_lambdas(hi, ai)
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
    n_live = conn.execute(
        "SELECT COUNT(*) FROM fixture WHERE status_short IN ({})".format(",".join("?" * len(_LIVE))), _LIVE).fetchone()[0]
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
    """One capture per fixture/source/cycle, retaining explicit contract scope."""
    from prediction_market_soccer.config.leagues import active, caps_for
    from prediction_market_soccer.ingest.soccer_ingest import leg_of
    from prediction_market_soccer.util.market_identity import fixture_identity
    from prediction_market_soccer.venues.kalshi.discovery import KalshiDiscovery
    from prediction_market_soccer.venues.polymarket_us.discovery import PolymarketUSDiscovery
    from zoneinfo import ZoneInfo
    comps={c.api_football_id:c for c in active()}
    cmap={r['api_id']:r['canonical_team_id'] for r in conn.execute('SELECT api_id,canonical_team_id FROM team_meta')}
    kd={}
    # In-play quotes can reach a paper decision, so this caller outranks the
    # upcoming sweep on the one shared Polymarket US read budget.
    try: pd=PolymarketUSDiscovery(conn=conn, priority='live')
    except Exception as exc:
        log.warning('Polymarket US discovery unavailable: %s',type(exc).__name__)
        pd=None
    sources={}
    def create(venue,kind):
        cache={}; statuses={}
        def fetch(fid):
            if fid in cache: return cache[fid]
            fx=conn.execute('SELECT * FROM fixture WHERE api_id=?',(fid,)).fetchone()
            result={}
            try:
                if fx is None or fx['league_id'] not in comps:
                    statuses[fid]={'state':'unavailable','reason':'unknown_fixture'}
                    return {}
                comp=comps[fx['league_id']]
                hi,ai=cmap.get(fx['home_api_id']),cmap.get(fx['away_api_id'])
                leg,_=leg_of(conn,fid)
                tie=conn.execute('SELECT tie_key FROM tie WHERE leg1_fixture_id=? OR leg2_fixture_id=?',(fid,fid)).fetchone()
                f=fixture_identity({**dict(fx),'leg':leg,'tie_id':tie['tie_key'] if tie else None},hi,ai,comp.key)
                # Preserve the observed phase for schedule validation in every source.
                f['status_short'] = fx['status_short']
                if kind=='advance':
                    leg,_=leg_of(conn,fid)
                    if not caps_for(comp.key,fx['round'],leg=leg).advance:
                        statuses[fid]={'state':'not_requested','reason':'unsupported_market'}
                        cache[fid]={}; return {}
                if venue=='kalshi':
                    if comp.key not in kd: kd[comp.key]=KalshiDiscovery(comp.key,conn=conn)
                    d=kd[comp.key]
                    result=getattr(d,kind+'_quotes')(hi,ai,fixture=f) or {}
                    source_status=d.discovery_status
                else:
                    if pd is None: return {}
                    et=datetime.fromisoformat(f['kickoff_ts']).astimezone(ZoneInfo('America/New_York')).date().isoformat()
                    result=getattr(pd,kind+'_quotes')(hi,ai,et,fixture=f,comp_key=comp.key) or {}
                    source_status=pd.discovery_status
                statuses[fid]={'state':'ok' if result else 'unavailable','discovery':source_status}
                if venue=='poly_us' and kind=='advance':
                    statuses[fid]={'state':'not_requested','reason':'unsupported_market'}
            except Exception as exc:
                statuses[fid]={'state':'unavailable','reason':'request_failed','error':type(exc).__name__}
                log.warning('quote %s/%s fixture %s: %s',venue,kind,fid,type(exc).__name__)
            cache[fid]=result
            return result
        fetch.market_kind=kind
        fetch.settlement_scope='advance' if kind=='advance' else 'regulation'
        fetch.status=lambda fid: statuses.get(fid,{'state':'not_requested'})
        return fetch
    for venue in ('kalshi','poly_us'):
        for kind in ('match','totals','advance','corners'):
            if venue=='poly_us' and kind=='corners': continue
            name=venue+('' if kind=='match' else '_'+kind)
            sources[name]=create(venue,kind)
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
