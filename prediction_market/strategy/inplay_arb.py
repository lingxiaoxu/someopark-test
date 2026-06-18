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

from dataclasses import asdict, dataclass, field

from prediction_market.config import CONFIG
from prediction_market.model.inplay import LiveMatchProb, live_match_prob
from prediction_market.strategy.cross_venue import evaluate_lock
from prediction_market.strategy.edge import compute_edge
from prediction_market.strategy.inplay_tactics import (
    convergence_take_profit,
    draw_trade_signal,
    favourite_comeback,
    goal_overreaction_fade,
    knockout_late_draw,
    live_momentum_from_store,
    red_card_value,
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
    reason: str          # English fallback (numbers baked in)
    # i18n: stable template key + raw args so the frontend renders the localized
    # backbone with live numbers spliced in (5 languages). reason stays the fallback.
    reason_key: str = ""
    reason_args: dict = field(default_factory=dict)


def _is_knockout(round_name) -> bool:
    return bool(round_name) and "group" not in str(round_name).lower()


def _favourite_basis(conn, prior, sm, hi: str, ai: str) -> tuple[str, float, str]:
    """Pre-match favourite as an EXPLICIT, explainable combination of the two views the
    desk sees (Part 2): TEAM STRENGTH (the 球队强度 view — pure rating, no recent-form
    blend) z-scored, PLUS RECENT FORM (the 近期状态 view — the same form_z form.json
    ranks by). favourite = higher (z_strength + z_form). Recomputed every refresh, so a
    strong team in poor form can lose favourite status to an in-form one.

    Returns (fav_side, fav_win_prob, basis) where basis explains WHY:
      * ``aligned``           — strength and form agree (strong AND in form)
      * ``strong_poor_form``  — favourite is the stronger team but worse recent form
      * ``in_form_underdog``  — favourite is weaker by strength but in better form
    """
    from prediction_market.model.form_strength import form_index
    from prediction_market.model.match_pricing import price_match
    from prediction_market.model.strength import build_strength

    ratings = build_strength(prior).ratings        # pure team strength (no form blend)
    vals = list(ratings.values()) or [0.0]
    mu = sum(vals) / len(vals)
    sd = (sum((v - mu) ** 2 for v in vals) / len(vals)) ** 0.5 or 1.0
    z_str = {t: (ratings.get(t, mu) - mu) / sd for t in (hi, ai)}
    fi = form_index(conn)
    z_form = {t: (fi[t].form_z if t in fi else 0.0) for t in (hi, ai)}
    comb = {t: z_str[t] + z_form[t] for t in (hi, ai)}      # explicit equal-weight z-sum

    fav = hi if comb[hi] >= comb[ai] else ai
    fav_side = "home" if fav == hi else "away"
    mp = price_match(sm, hi, ai)                            # win prob from the live model
    prob = mp.p_home if fav_side == "home" else mp.p_away

    strength_fav = hi if z_str[hi] >= z_str[ai] else ai
    form_fav = hi if z_form[hi] >= z_form[ai] else ai
    if strength_fav == form_fav:
        basis = "aligned"
    elif fav == strength_fav:
        basis = "strong_poor_form"
    else:
        basis = "in_form_underdog"
    return fav_side, prob, basis


def _last_event(conn, fixture_row, etype: str, hi: str, ai: str, *, detail_like: str | None = None):
    """Most recent event of a type (Goal / Card) for a live fixture → (side, minute).
    Own goals are excluded for 'Goal'. Returns (None, None) if none."""
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    q = ("SELECT team_api_id, minute, detail FROM fixture_event "
         "WHERE fixture_api_id=? AND type=? ")
    params: list = [fixture_row["api_id"], etype]
    if detail_like:
        q += "AND detail LIKE ? "
        params.append(detail_like)
    if etype == "Goal":
        q += "AND (detail IS NULL OR detail != 'Own Goal') "
    q += "ORDER BY minute DESC, seq DESC LIMIT 1"
    r = conn.execute(q, params).fetchone()
    if not r:
        return (None, None)
    side = "home" if cmap.get(r["team_api_id"]) == hi else ("away" if cmap.get(r["team_api_id"]) == ai else None)
    return (side, r["minute"])


def _goal_on_board(conn, fixture_row, hi: str, ai: str, side: str, gh: int, ga: int) -> bool:
    """True if `side`'s latest goal event is actually reflected on the scoreboard.

    A goal can be disallowed (VAR / offside) or corrected by the feed AFTER its
    event row is written: the fixture_event row stays, but home_goals/away_goals
    revert. We count that side's (non-own-goal) goal events and require it to be
    ≤ the goals showing on the board — otherwise the latest event is a phantom
    goal and the goal-driven tactics must ignore it.
    """
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    target = hi if side == "home" else ai
    n_events = 0
    for r in conn.execute(
        "SELECT team_api_id, detail FROM fixture_event WHERE fixture_api_id=? AND type='Goal'",
        (fixture_row["api_id"],),
    ):
        if (r["detail"] or "") == "Own Goal":
            continue
        if cmap.get(r["team_api_id"]) == target:
            n_events += 1
    on_board = gh if side == "home" else ga
    return n_events <= on_board


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
    prior = load_prior()
    sm = sm or build_strength(prior)
    theta = CONFIG.risk.min_net_edge if theta is None else theta
    quote_sources = quote_sources or {}
    name = {t.team_id: t.name for t in prior.teams}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    live = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, elapsed, round "
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

        # Event-driven context: pre-match favourite (explicit strength+form basis, Part 2),
        # the latest goal + red card, KO flag.
        fav_side, fav_prob, fav_basis = _favourite_basis(conn, prior, sm, hi, ai)
        last_goal_side, last_goal_min = _last_event(conn, fx, "Goal", hi, ai)
        # Score-consistency guard: a goal can be chalked off (VAR / offside / feed
        # correction) AFTER its event row was written — the event lingers but the
        # scoreboard reverts. Only treat the latest goal as "recent" if the scoring
        # side's goal-event count matches the goals actually on the board; otherwise
        # the goal-driven tactics (overreaction fade) would fire on a disallowed goal.
        if last_goal_side and not _goal_on_board(conn, fx, hi, ai, last_goal_side, gh, ga):
            last_goal_side, last_goal_min = None, None
        carded_side, card_min = _last_event(conn, fx, "Card", hi, ai, detail_like="%Red%")
        knockout = _is_knockout(fx["round"])

        # (1) tactics — always available, no market quote needed.
        for sig in (draw_trade_signal(lp),
                    convergence_take_profit("home" if gh > ga else "away", 0.5, lp) if gh != ga else None,
                    live_momentum_from_store(conn, fx["api_id"], fx["home_api_id"], fx["away_api_id"], minute, gh, ga),
                    goal_overreaction_fade(lp, prematch_fav_side=fav_side, last_goal_side=last_goal_side,
                                           last_goal_minute=last_goal_min, fav_basis=fav_basis),
                    favourite_comeback(lp, prematch_fav_side=fav_side, prematch_fav_prob=fav_prob, fav_basis=fav_basis),
                    red_card_value(lp, carded_side=carded_side, card_minute=card_min),
                    knockout_late_draw(lp, knockout=knockout)):
            if sig and sig.act != "HOLD":
                opps.append(Opportunity(fx["api_id"], m, minute, score, "tactic", sig.side,
                                        "model", fair.get(sig.side), None, None, sig.act, sig.reason,
                                        reason_key=sig.reason_key, reason_args=sig.reason_args))

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
                    also_str = ", ".join(f"{v} {asks_by_v[v]:.2f}" for v in also)
                    reason = f"model {fair[side]:.2f} > {best_v} ask {best_ask:.2f}"
                    if also:
                        reason += f"; also {also_str}"
                    opps.append(Opportunity(fx["api_id"], m, minute, score, "relative_value", side, venue_lbl,
                                            round(fair[side], 3), round(best_ask, 3), round(e.net_edge, 3),
                                            "BUY", reason, reason_key="relative_value",
                                            reason_args={"fair": round(fair[side], 2), "venue": best_v,
                                                         "ask": round(best_ask, 2), "also": also_str}))
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
                        no_price, cost = 1 - bids[sell_v], asks[buy_v] + 1 - bids[sell_v]
                        opps.append(Opportunity(
                            fx["api_id"], m, minute, score, "lock_arb", side, f"{buy_v}+{sell_v}",
                            None, None, round(lock.net_lock, 3), "ARB",
                            f"buy {buy_v} YES {asks[buy_v]:.2f} + {sell_v} NO {no_price:.2f} "
                            f"(cost {cost:.2f}) → lock {lock.net_lock:+.2f}",
                            reason_key="lock_arb",
                            reason_args={"buy_v": buy_v, "ask": round(asks[buy_v], 2), "sell_v": sell_v,
                                         "no": round(no_price, 2), "cost": round(cost, 2),
                                         "lock": round(lock.net_lock, 2)}))
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
