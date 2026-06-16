"""In-play opportunity finder (plan 04 §4c, 08, 15) — per-minute, intra-game stats.

For every LIVE match, each poll:
  1. compute the **live fair price** from intra-game stats — state-dependent
     Poisson on (minute, score, red cards) shaded by live **xG** (model/inplay.py);
  2. compare to live market prices on each venue (pluggable quote sources):
     * **relative value** — fair vs a venue's de-vigged price (net of fee);
     * **cross-venue lock arb** — buy cheap-venue YES + expensive-venue NO when
       two TRADABLE venues quote the same outcome (cross_venue.evaluate_lock);
  3. emit the in-play **tactics** (draw take-profit, convergence, momentum).

Quote sources are functions ``venue -> {outcome: ask}`` for a given match; they
populate when the single-match markets go live (Kalshi per-match, Polymarket US
fwc-*, Global match events). Until then the finder still outputs fair prices +
tactics so the desk sees the model's live view.

Read-only: opportunities flow to the gated executor + the hard $1 test cap.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from prediction_market.config import CONFIG
from prediction_market.model.inplay import LiveMatchProb, live_match_prob
from prediction_market.strategy.cross_venue import evaluate_lock
from prediction_market.strategy.edge import compute_edge
from prediction_market.strategy.inplay_tactics import (
    convergence_take_profit,
    draw_trade_signal,
    live_momentum_from_store,
)

_LIVE = ("1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "SUSP")


def _ask(q) -> float | None:
    """Ask from a quote: {'ask','bid'} dict, or a plain float treated as the ask."""
    if isinstance(q, dict):
        return q.get("ask")
    return q


def _bid(q) -> float | None:
    if isinstance(q, dict):
        return q.get("bid", q.get("ask"))
    return q


@dataclass
class Opportunity:
    fixture_id: int
    match: str
    minute: int
    score: str
    kind: str            # "relative_value" | "lock_arb" | "tactic"
    side: str
    venue: str
    fair: float | None
    market: float | None
    edge: float | None
    action: str
    reason: str


def live_fair(conn, sm, fixture_row) -> tuple[LiveMatchProb, dict]:
    """xG-shaded live fair prices for one fixture (intra-game stats → price)."""
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    hi, ai = cmap.get(fixture_row["home_api_id"]), cmap.get(fixture_row["away_api_id"])
    lam_h, lam_a = sm.pair_lambdas(hi, ai)
    xg = {r["team_api_id"]: r["xg"] for r in conn.execute(
        "SELECT team_api_id, xg FROM fixture_stats WHERE fixture_api_id=?", (fixture_row["api_id"],))}
    reds = {r["team_api_id"]: r["n"] for r in conn.execute(
        "SELECT team_api_id, COUNT(*) n FROM fixture_event WHERE fixture_api_id=? "
        "AND type='Card' AND detail LIKE '%Red%' GROUP BY team_api_id", (fixture_row["api_id"],))}
    minute = fixture_row["elapsed"] or 0
    gh, ga = fixture_row["home_goals"] or 0, fixture_row["away_goals"] or 0
    lp = live_match_prob(
        lam_h, lam_a, minute, gh, ga,
        red_home=reds.get(fixture_row["home_api_id"], 0), red_away=reds.get(fixture_row["away_api_id"], 0),
        xg_home=xg.get(fixture_row["home_api_id"]), xg_away=xg.get(fixture_row["away_api_id"]),
    )
    fair = {"home": lp.p_home, "draw": lp.p_draw, "away": lp.p_away}
    return lp, fair


def find_opportunities(conn=None, sm=None, *, quote_sources: dict | None = None,
                       fee: float = 0.01, theta: float | None = None) -> list[dict]:
    """All in-play opportunities across live matches (relative value + arb + tactics)."""
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.strength import build_strength

    conn = conn or store.init_db()
    sm = sm or build_strength(load_prior())
    theta = CONFIG.risk.min_net_edge if theta is None else theta
    quote_sources = quote_sources or {}
    name = {t.team_id: t.name for t in load_prior().teams}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    live = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, elapsed "
        "FROM fixture WHERE status_short IN ({})".format(",".join("?" * len(_LIVE))), _LIVE).fetchall()
    opps: list[Opportunity] = []
    for fx in live:
        hi, ai = cmap.get(fx["home_api_id"]), cmap.get(fx["away_api_id"])
        if not (hi and ai):
            continue
        m = f"{name.get(hi, hi)} vs {name.get(ai, ai)}"
        minute = fx["elapsed"] or 0
        gh, ga = fx["home_goals"] or 0, fx["away_goals"] or 0
        score = f"{gh}-{ga}"
        lp, fair = live_fair(conn, sm, fx)

        # (1) tactics — always available, no market quote needed.
        for sig in (draw_trade_signal(lp),
                    convergence_take_profit("home" if gh > ga else "away", 0.5, lp) if gh != ga else None,
                    live_momentum_from_store(conn, fx["api_id"], fx["home_api_id"], fx["away_api_id"], minute, gh, ga)):
            if sig and sig.act != "HOLD":
                opps.append(Opportunity(fx["api_id"], m, minute, score, "tactic", sig.side,
                                        "model", fair.get(sig.side), None, None, sig.act, sig.reason))

        # (2) market-dependent: relative value + cross-venue lock arb.
        # Quote per outcome is {'ask','bid'} (or a plain float = ask==bid).
        quotes = {v: fn(fx["api_id"]) for v, fn in quote_sources.items()}
        for side in ("home", "draw", "away"):
            present = {v: q.get(side) for v, q in quotes.items() if q and q.get(side) is not None}
            # One row per side: back the CHEAPEST ask (best edge); name the other
            # venues that also qualify, instead of a near-duplicate row per venue.
            asks_by_v = {v: _ask(qv) for v, qv in present.items() if _ask(qv) is not None}
            if asks_by_v:
                best_v = min(asks_by_v, key=asks_by_v.get)
                best_ask = asks_by_v[best_v]
                e = compute_edge(fair[side], best_ask, fee=fee, theta=theta)
                if e.tradable:
                    also = [v for v, a in asks_by_v.items()
                            if v != best_v and compute_edge(fair[side], a, fee=fee, theta=theta).tradable]
                    venue_lbl = best_v + (f" (+{', '.join(also)})" if also else "")
                    reason = f"model {fair[side]:.2f} > {best_v} ask {best_ask:.2f}"
                    if also:
                        reason += f"; also {', '.join(f'{v} {asks_by_v[v]:.2f}' for v in also)}"
                    opps.append(Opportunity(fx["api_id"], m, minute, score, "relative_value", side, venue_lbl,
                                            round(fair[side], 3), round(best_ask, 3), round(e.net_edge, 3),
                                            "BUY", reason))
            # Cross-venue lock arb: BUY YES at the cheapest ASK, SELL YES (=buy NO)
            # at the highest BID, on two EXECUTABLE venues. Lock = sell_bid − buy_ask
            # − fees. Using bid for the sell leg avoids false positives.
            ex = {v: qv for v, qv in present.items() if v in CONFIG.venue.executable_venues}
            asks = {v: _ask(qv) for v, qv in ex.items() if _ask(qv) is not None}
            bids = {v: _bid(qv) for v, qv in ex.items() if _bid(qv) is not None}
            if len(asks) >= 1 and len(bids) >= 1:
                buy_v = min(asks, key=asks.get)
                sell_v = max(bids, key=bids.get)
                if buy_v != sell_v:
                    lock = evaluate_lock(asks[buy_v], bids[sell_v], equiv_verified=True,
                                         fee_cheap=fee, fee_expensive=fee)
                    if lock.tradable:
                        opps.append(Opportunity(
                            fx["api_id"], m, minute, score, "lock_arb", side, f"{buy_v}+{sell_v}",
                            None, None, round(lock.net_lock, 3), "ARB",
                            f"buy {buy_v} YES {asks[buy_v]:.2f} + {sell_v} NO {1-bids[sell_v]:.2f} "
                            f"(cost {asks[buy_v]+1-bids[sell_v]:.2f}) → lock {lock.net_lock:+.2f}"))
    # Rank: lock arb > relative value > tactic; by |edge|.
    rank = {"lock_arb": 0, "relative_value": 1, "tactic": 2}
    opps.sort(key=lambda o: (rank[o.kind], -(abs(o.edge) if o.edge is not None else 0)))
    return [asdict(o) for o in opps]


if __name__ == "__main__":
    import sqlite3

    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.strength import build_strength

    # Demo with a synthetic live match + a synthetic two-venue mispricing.
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; store.init_db(c)
    for api, cid in ((10, "france"), (20, "senegal")):
        store.upsert(c, "team_meta", {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "status_short": "2H",
        "home_api_id": 10, "away_api_id": 20, "home_goals": 0, "away_goals": 0, "elapsed": 78,
        "updated_at": store.utcnow()}, pk=["api_id"])
    for tid, xg in ((10, 1.9), (20, 0.3)):   # France dominating xG but 0:0
        store.upsert(c, "fixture_stats", {"fixture_api_id": 1, "team_api_id": tid, "xg": xg, "fetched_at": store.utcnow()}, pk=["fixture_api_id", "team_api_id"])
    sm = build_strength(load_prior())
    # Synthetic quote sources: Kalshi cheap on France, Global expensive (cross-venue gap).
    qs = {"kalshi": lambda fid: {"home": 0.30, "draw": 0.45, "away": 0.10},
          "poly_global": lambda fid: {"home": 0.42, "draw": 0.40, "away": 0.12}}
    opps = find_opportunities(conn=c, sm=sm, quote_sources=qs)
    print(f"{len(opps)} in-play opportunities (France 0:0 Senegal @78', France xG 1.9):")
    for o in opps[:8]:
        print(f"  [{o['kind']:<14}] {o['action']:<4} {o['side']:<5} {o['venue']:<14} edge={o['edge']} — {o['reason'][:46]}")
