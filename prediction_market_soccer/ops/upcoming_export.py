"""Per-match cross-venue quote export for the frontend "Upcoming Matches" panel.

For each of the next N not-started fixtures this produces a fully-enriched row:

  * **model** 3-way + O2.5 + BTTS (Dixon-Coles via ``price_match``);
  * **book_devig** — sharp bookmaker de-vig (validation reference);
  * **kalshi** — REAL single-match 3-way quotes (public market data, no auth),
    via ``KalshiDiscovery.match_quotes`` → {home/draw/away: {ask, bid}} + de-vig;
  * **poly_us** — REAL single-match 3-way quotes via ``PolymarketUSDiscovery.
    match_quotes(home, away, et_date)`` → {home/draw/away: {ask, bid}} + de-vig;
  * **edge** — model vs each venue's de-vigged price + the best tradable buy edge
    (``compute_edge``, fee-aware, theta-gated);
  * **lock_arb** — best cross-venue locked arbitrage per side (``evaluate_lock``,
    buy cheapest ask + sell highest bid across the two TRADABLE venues).

Read-only: market data only, NEVER places an order. Kalshi market data is public;
Polymarket US uses the PMUS read credentials. A venue that has not yet listed a
given match returns ``None`` for that match — the fetch is REAL, not stubbed; the
field is absent only when the venue genuinely has no market yet.

    python -m prediction_market_soccer.ops.upcoming_export [--limit 6] [--no-venues]
    → data/output/upcoming.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.util.pricing import quote_to_cents

ET = ZoneInfo("America/New_York")
_FEE = 0.01            # per-contract execution fee estimate (matches inplay_arb)
_FINISHED = ("FT", "AET", "PEN")
_UPCOMING = ("NS", "TBD", "PST")   # not-started statuses
_FINISHED = ("FT", "AET", "PEN")   # finished statuses


# C1: stage/caps come from the league registry (leagues.caps_for), resolved per
# fixture with the tie-layer leg — no round-name substring guessing anywhere.


def _tentative_pairing(raw_json) -> tuple[str, str] | None:
    """Placeholder home/away labels for an undecided knockout tie, from the
    API-Football fixture (it labels the undetermined side, e.g. "Winner Group A" /
    "Runner-up Group B" / "1A" / "2B"). Returns None if no usable labels."""
    try:
        teams = json.loads(raw_json or "{}").get("teams", {})
        h = (teams.get("home", {}) or {}).get("name")
        a = (teams.get("away", {}) or {}).get("name")
        if h and a:
            return (str(h), str(a))
    except Exception:
        pass
    return None


def recent_finished(conn, hours: float = 0.75) -> list[dict]:
    """Matches that finished in the last `hours` (default 45 min) — shown briefly in
    the top region marked FT + final score so a just-ended live match isn't simply
    dropped, then rolls off into the next upcoming fixtures. Data straight from
    API-Football's finished-status fixtures. Club edition: names/zh from
    club_registry; every row carries its ``league`` key."""
    from datetime import timedelta
    from prediction_market_soccer.config.leagues import by_api_id
    name, zh = {}, {}
    for r in conn.execute("SELECT DISTINCT club_id, name, zh FROM club_registry"):
        name[r["club_id"]] = r["name"]
        zh[r["club_id"]] = r["zh"] or ""
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    # The fixture stores kickoff, not the final whistle, so approximate finish as
    # kickoff + ~3h (covers 90' + stoppage + half-time + any extra time/penalties +
    # post-match settling). Show while now is within `hours` (45 min) of that
    # approximate finish, i.e. kickoff in the last (3h + hours).
    _MATCH_DURATION_H = 3.0
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=_MATCH_DURATION_H + hours)).isoformat()
    lids = {c.api_football_id: c for c in __import__(
        "prediction_market_soccer.config.leagues", fromlist=["active"]).active()}
    out = []
    for r in conn.execute(
        "SELECT league_id, home_api_id, away_api_id, home_goals, away_goals, kickoff_ts, "
        "status_short, round, raw_json "
        "FROM fixture WHERE status_short IN ({}) AND home_goals IS NOT NULL AND kickoff_ts >= ? "
        "AND league_id IN ({}) ORDER BY kickoff_ts DESC".format(
            ",".join("?" * len(_FINISHED)), ",".join("?" * len(lids))),
        (*_FINISHED, cutoff, *lids)).fetchall():
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            continue
        comp = lids.get(r["league_id"])
        from prediction_market_soccer.util.pricing import reg_score
        fgh, fga = r["home_goals"], r["away_goals"]                       # final (incl ET)
        gh, ga = reg_score(r["raw_json"], fgh, fga)                       # 90' regulation
        score = f"{gh}-{ga}" + (f" (AET {fgh}-{fga})" if (gh, ga) != (fgh, fga) else "")
        out.append({
            "league": comp.key if comp else None,
            "league_zh": comp.zh if comp else "",
            "home": {"id": hi, "name": name.get(hi, hi), "zh": zh.get(hi, "")},
            "away": {"id": ai, "name": name.get(ai, ai), "zh": zh.get(ai, "")},
            "score": score, "status": r["status_short"], "finished": True,
            "result": "home" if gh > ga else ("draw" if gh == ga else "away"),
            "kickoff": r["kickoff_ts"], "et": _et_human(r["kickoff_ts"]), "round": r["round"] or "",
        })
    return out


def _et_date(kickoff_ts: str) -> str | None:
    try:
        return datetime.fromisoformat(kickoff_ts).astimezone(ET).date().isoformat()
    except Exception:
        return None


def _et_human(kickoff_ts: str) -> str | None:
    try:
        return datetime.fromisoformat(kickoff_ts).astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    except Exception:
        return None


def _venue_devig(q: dict | None) -> dict | None:
    """De-vig a venue's 3-way ask quote into fair probs (multiplicative)."""
    if not q:
        return None
    from prediction_market_soccer.strategy.devig import devig
    asks = [(q.get(s) or {}).get("ask") for s in ("home", "draw", "away")]
    if any(a is None for a in asks):
        return None
    p = devig(asks, method="multiplicative")
    return {"home": round(float(p[0]), 4), "draw": round(float(p[1]), 4), "away": round(float(p[2]), 4)}


def _best_buy_edge(model: dict, venue_q: dict | None, venue: str, theta: float) -> dict | None:
    """Best tradable BUY edge (model vs a venue's raw ask) across the 3 sides."""
    if not venue_q:
        return None
    from prediction_market_soccer.strategy.edge import compute_edge
    best = None
    for side in ("home", "draw", "away"):
        ask = (venue_q.get(side) or {}).get("ask")
        if ask is None:
            continue
        e = compute_edge(model[side], float(ask), fee=_FEE, theta=theta)
        if best is None or e.net_edge > best["net_edge"]:
            best = {"side": side, "venue": venue, "ask": round(float(ask), 4),
                    "net_edge": round(e.net_edge, 4), "tradable": bool(e.tradable)}
    return best


def _decision_quotes(kalshi_q, poly_q, k_devig, p_devig):
    """{side: SideQuote} for decision_model.decide — cheapest executable ask per side
    across venues + that side's de-vig (same selection the bet log uses on PRE rows)."""
    from prediction_market_soccer.strategy.decision_model import SideQuote
    q = {}
    for s in ("home", "draw", "away"):
        cands = []
        ka = (kalshi_q.get(s) or {}).get("ask") if kalshi_q else None
        pa = (poly_q.get(s) or {}).get("ask") if poly_q else None
        if ka is not None:
            cands.append((ka, "kalshi", (k_devig or {}).get(s)))
        if pa is not None:
            cands.append((pa, "poly_us", (p_devig or {}).get(s)))
        if not cands:
            q[s] = SideQuote()
            continue
        ask, ven, dv = min(cands, key=lambda t: t[0])
        q[s] = SideQuote(ask=ask, devig=dv, venue=ven)
    return q


def _cap_count(ask: float) -> int:
    """Largest contract count whose notional stays within the $1 hard test cap."""
    cap = CONFIG.risk.max_test_order_usd
    cnt = max(1, int(cap / max(ask, 1e-3)))
    while cnt > 1 and cnt * ask > cap + 1e-9:
        cnt -= 1
    return cnt


def _decision_for(model, kalshi_q, poly_q, k_devig, p_devig, form_row, calib_conf, gate_open, knockout,
                  conviction_side=None):
    """The production pre-match DECISION for one match — the SAME decision the bet log +
    match_signals use, so the card's '怎么操作' matches.

    The per-match market (KXWCGAME / fwc) settles on the 90-MIN 3-way for BOTH stages — a
    draw is a valid outcome even in knockout (the match can be level at 90'). So this is a
    3-way value/Kelly decision regardless of stage. ('Advancing' a tie is a SEPARATE
    per-team reach-round product, KXWCROUND — handled elsewhere.)"""
    from prediction_market_soccer.strategy.decision_model import decide
    dq = _decision_quotes(kalshi_q, poly_q, k_devig, p_devig)
    d = decide(model, dq, calib_confidence=calib_conf, form=form_row, gate_open=gate_open,
               conviction_side=conviction_side)
    if d.side is None:
        return {"bet": False, "side": None, "stake_usd": 0.0,
                "net_edge": d.net_edge, "confidence_k": d.confidence_k, "knockout": knockout}
    ask = dq[d.side].ask
    cnt = _cap_count(ask)
    return {"bet": True, "side": d.side, "venue": d.venue, "price_cents": d.price_cents,
            "model_prob": d.model_prob, "net_edge": d.net_edge, "stake_usd": d.stake_usd,
            "count": cnt, "capped_notional_usd": round(cnt * ask, 2),
            "confidence_k": d.confidence_k, "knockout": knockout}


def _lock_arb(kalshi_q: dict | None, poly_q: dict | None) -> dict | None:
    """Best cross-venue locked arb per side: buy cheapest ask + sell highest bid."""
    if not (kalshi_q and poly_q):
        return None
    from prediction_market_soccer.strategy.cross_venue import evaluate_lock
    best = None
    for side in ("home", "draw", "away"):
        legs = {"kalshi": kalshi_q.get(side), "poly_us": poly_q.get(side)}
        asks = {v: (q or {}).get("ask") for v, q in legs.items() if (q or {}).get("ask") is not None}
        bids = {v: (q or {}).get("bid") for v, q in legs.items() if (q or {}).get("bid") is not None}
        if not asks or not bids:
            continue
        buy_v = min(asks, key=asks.get)
        sell_v = max(bids, key=bids.get)
        if buy_v == sell_v:
            continue
        lock = evaluate_lock(asks[buy_v], bids[sell_v], equiv_verified=True,
                             fee_cheap=_FEE, fee_expensive=_FEE)
        if best is None or lock.net_lock > best["net_lock"]:
            best = {"side": side, "buy_venue": buy_v, "sell_venue": sell_v,
                    "buy_ask": round(asks[buy_v], 4), "sell_bid": round(bids[sell_v], 4),
                    "net_lock": round(lock.net_lock, 4), "tradable": bool(lock.tradable)}
    return best


# ─────────────────────────────────────────────────────────────────────────────
# 2-WAY "ADVANCE" (knockout who-advances) — parallel block to the 3-way above.
# The knockout tie has a SEPARATE 2-way market on every venue (Kalshi KXWCADVANCE,
# Poly US aadc-…-to-advance, Poly Global reach-round): home advances / away advances,
# NO draw (one team always goes through, incl. ET + penalties). These helpers mirror
# the 3-way ones over the two sides so the frontend's "Advances" view switches the
# whole card (model / quotes / edge / decision) to this product. Group-stage matches
# have no advance market → the whole block is None (the card stays on regulation time).
# ─────────────────────────────────────────────────────────────────────────────
_ADV_SIDES = ("home", "away")


def _quote_to_cents_2way(q: dict | None) -> dict | None:
    """{home:{ask,bid},away:{...}} → adds ask_c/bid_c/mid_c per side (ADD ONLY)."""
    if not q:
        return None
    from prediction_market_soccer.util.pricing import mid_cents, to_cents
    out: dict = {}
    for side in _ADV_SIDES:
        s = q.get(side)
        if not s:
            out[side] = s
            continue
        ask, bid = s.get("ask"), s.get("bid")
        out[side] = {**s, "ask_c": to_cents(ask), "bid_c": to_cents(bid), "mid_c": mid_cents(ask, bid)}
    return out


def _venue_devig_2way(q: dict | None) -> dict | None:
    """De-vig a venue's 2-way ask quote into fair probs (multiplicative, 2 outcomes)."""
    if not q:
        return None
    from prediction_market_soccer.strategy.devig import devig
    asks = [(q.get(s) or {}).get("ask") for s in _ADV_SIDES]
    if any(a is None for a in asks):
        return None
    p = devig(asks, method="multiplicative")
    return {"home": round(float(p[0]), 4), "away": round(float(p[1]), 4)}


def _best_buy_edge_2way(model: dict, venue_q: dict | None, venue: str, theta: float) -> dict | None:
    """Best tradable BUY edge (model vs a venue's raw ask) across the 2 advance sides."""
    if not venue_q:
        return None
    from prediction_market_soccer.strategy.edge import compute_edge
    best = None
    for side in _ADV_SIDES:
        ask = (venue_q.get(side) or {}).get("ask")
        if ask is None:
            continue
        e = compute_edge(model[side], float(ask), fee=_FEE, theta=theta)
        if best is None or e.net_edge > best["net_edge"]:
            best = {"side": side, "venue": venue, "ask": round(float(ask), 4),
                    "net_edge": round(e.net_edge, 4), "tradable": bool(e.tradable)}
    return best


def _decision_for_2way(model_adv, kalshi_a, poly_a, k_devig_a, p_devig_a, form_row,
                       calib_conf, gate_open) -> dict | None:
    """Full 2-way advance DECISION (value side + ¼-Kelly stake + $1-cap), reusing the
    same decision_model.decide() as the 3-way so the "Advances" trade plan is built by
    identical logic. Draw is passed as an empty quote (no draw leg) so decide() only
    considers home/away. Returns None when no venue advance quote exists."""
    if not (kalshi_a or poly_a):
        return None
    from prediction_market_soccer.strategy.decision_model import SideQuote, decide
    dq = {"draw": SideQuote()}
    for s in _ADV_SIDES:
        cands = []
        ka = (kalshi_a.get(s) or {}).get("ask") if kalshi_a else None
        pa = (poly_a.get(s) or {}).get("ask") if poly_a else None
        if ka is not None:
            cands.append((ka, "kalshi", (k_devig_a or {}).get(s)))
        if pa is not None:
            cands.append((pa, "poly_us", (p_devig_a or {}).get(s)))
        if not cands:
            dq[s] = SideQuote()
            continue
        ask, ven, dv = min(cands, key=lambda t: t[0])
        dq[s] = SideQuote(ask=ask, devig=dv, venue=ven)
    # model needs a (zero) draw so decide()'s argmax helpers never KeyError.
    model = {"home": model_adv["home"], "away": model_adv["away"], "draw": 0.0}
    d = decide(model, dq, calib_confidence=calib_conf, form=form_row, gate_open=gate_open)
    if d.side is None:
        return {"bet": False, "side": None, "stake_usd": 0.0, "net_edge": d.net_edge,
                "confidence_k": d.confidence_k, "knockout": True, "advance": True}
    ask = dq[d.side].ask
    cnt = _cap_count(ask)
    return {"bet": True, "side": d.side, "venue": d.venue, "price_cents": d.price_cents,
            "model_prob": d.model_prob, "net_edge": d.net_edge, "stake_usd": d.stake_usd,
            "count": cnt, "capped_notional_usd": round(cnt * ask, 2),
            "confidence_k": d.confidence_k, "knockout": True, "advance": True}


def _lock_arb_2way(kalshi_a: dict | None, poly_a: dict | None) -> dict | None:
    """Best cross-venue locked arb on the 2-way advance: buy cheapest ask + sell highest bid."""
    if not (kalshi_a and poly_a):
        return None
    from prediction_market_soccer.strategy.cross_venue import evaluate_lock
    best = None
    for side in _ADV_SIDES:
        legs = {"kalshi": kalshi_a.get(side), "poly_us": poly_a.get(side)}
        asks = {v: (q or {}).get("ask") for v, q in legs.items() if (q or {}).get("ask") is not None}
        bids = {v: (q or {}).get("bid") for v, q in legs.items() if (q or {}).get("bid") is not None}
        if not asks or not bids:
            continue
        buy_v = min(asks, key=asks.get)
        sell_v = max(bids, key=bids.get)
        if buy_v == sell_v:
            continue
        lock = evaluate_lock(asks[buy_v], bids[sell_v], equiv_verified=True,
                             fee_cheap=_FEE, fee_expensive=_FEE)
        if best is None or lock.net_lock > best["net_lock"]:
            best = {"side": side, "buy_venue": buy_v, "sell_venue": sell_v,
                    "buy_ask": round(asks[buy_v], 4), "sell_bid": round(bids[sell_v], 4),
                    "net_lock": round(lock.net_lock, 4), "tradable": bool(lock.tradable)}
    return best


def _stash_pre(conn, fixture_id, kickoff_ts, kalshi_q, poly_q, kalshi_a=None, poly_a=None) -> None:
    """Write a milestone='PRE' snapshot when the match is ≤20 min from kickoff (and
    not already stored). Captures the live Kalshi/Poly 3-way ask/bid AND the 2-way
    advance ask/bid (knockout) as the pre-match entry — so the price-track can mark the
    knockout 2-way entry ¢."""
    if not kickoff_ts:
        return
    from datetime import timedelta
    try:
        ko = datetime.fromisoformat(kickoff_ts)
    except Exception:
        return
    now = datetime.now(timezone.utc)
    if not (now <= ko <= now + timedelta(minutes=20)):
        return
    if conn.execute("SELECT 1 FROM milestone_snapshot WHERE fixture_api_id=? AND milestone='PRE'",
                    (fixture_id,)).fetchone():
        return

    def ab(q, side):
        s = ((q or {}).get(side) or {})
        return s.get("ask"), s.get("bid")

    kh, khb = ab(kalshi_q, "home"); kd, kdb = ab(kalshi_q, "draw"); ka, kab = ab(kalshi_q, "away")
    ph, phb = ab(poly_q, "home"); pd_, pdb = ab(poly_q, "draw"); pa, pab = ab(poly_q, "away")
    akh, akhb = ab(kalshi_a, "home"); aka, akab = ab(kalshi_a, "away")   # advance (no draw)
    aph, aphb = ab(poly_a, "home"); apa, apab = ab(poly_a, "away")
    conn.execute(
        # PRE is captured pre-kickoff (status NS) → the score is 0-0 by definition.
        # Store it explicitly so the price-track view shows "0-0", not "?-?" (null score):
        # backfill won't heal it once a live/settlement FT row trips its incremental skip.
        "INSERT OR IGNORE INTO milestone_snapshot "
        "(fixture_api_id, milestone, ts, elapsed, status_short, home_goals, away_goals, "
        " kalshi_home_ask, kalshi_home_bid, kalshi_draw_ask, kalshi_draw_bid, kalshi_away_ask, kalshi_away_bid, "
        " poly_home_ask, poly_home_bid, poly_draw_ask, poly_draw_bid, poly_away_ask, poly_away_bid, "
        " kalshi_adv_home_ask, kalshi_adv_home_bid, kalshi_adv_away_ask, kalshi_adv_away_bid, "
        " poly_adv_home_ask, poly_adv_home_bid, poly_adv_away_ask, poly_adv_away_bid, "
        " price_source) VALUES (?,?,?,?,?,?,?, ?,?,?,?,?,?, ?,?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?)",
        (fixture_id, "PRE", now.isoformat(), 0, "NS", 0, 0,
         kh, khb, kd, kdb, ka, kab, ph, phb, pd_, pdb, pa, pab,
         akh, akhb, aka, akab, aph, aphb, apa, apab, "live"))
    conn.commit()


def build(*, limit: int = 6, conn=None, with_venues: bool = True,
          per_league_limit: int | None = None) -> list[dict]:
    """Upcoming fixtures across every enabled competition, fully enriched.

    Club edition: iterates the LEAGUE REGISTRY; each row carries ``league`` +
    ``stage`` + ``caps`` (§3.0 — the frontend renders advance blocks/badges from
    caps, never from names). ``limit`` = per-competition row cap (kept the WC
    param name so copied callers behave sensibly)."""
    from prediction_market_soccer.config.leagues import active, caps_dict, caps_for, stage_of
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.soccer_ingest import leg_of
    from prediction_market_soccer.model.match_pricing import price_match_calibrated as price_match
    from prediction_market_soccer.model.strength_cache import cached_strength

    conn = conn or store.init_db()
    per_league_limit = per_league_limit or limit
    name_of, zh_of = {}, {}
    for r in conn.execute("SELECT DISTINCT club_id, name, zh FROM club_registry"):
        name_of[r["club_id"]] = r["name"]
        zh_of[r["club_id"]] = r["zh"] or ""
    try:
        from prediction_market_soccer.model.form_strength import form_index
        fidx = form_index(conn)
    except Exception as e:
        print(f"[warn] form_index unavailable: {e}")
        fidx = {}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    theta = CONFIG.risk.min_net_edge

    from prediction_market_soccer.model.probability_calibration import (
        gate_open_for, load_calibration)
    _cal = load_calibration()
    # Gate is PER COMPETITION (§3.5): a league nine matches into its season has no
    # verdict of its own, so it stays shut while a competition with 30+ settled
    # matches trades on its own calibrator. `_gate_open` remains the pooled value
    # for the summary line at the end of the export.
    _gate_open = bool(_cal and _cal.get("trade_grade"))
    _ub = (_cal.get("uniform_brier") if _cal else None) or (2.0 / 3.0)
    _cb = _cal.get("calibrated_brier") if _cal else None
    _calib_conf = max(-1.0, min(1.0, (_ub - _cb) / _ub)) if (_cb is not None and _ub) else 0.0

    book = {r["api_id"]: r for r in conn.execute(
        "SELECT f.api_id, AVG(o.p_home) bh, AVG(o.p_draw) bd, AVG(o.p_away) ba "
        "FROM fixture f JOIN match_odds o ON o.fixture_api_id=f.api_id "
        "AND o.bookmaker <> 'live_consensus' "   # pre-match book only
        "WHERE f.status_short IN ({}) GROUP BY f.api_id".format(",".join("?" * len(_UPCOMING))),
        _UPCOMING).fetchall()}

    out: list[dict] = []
    for comp in active():
        fixtures = conn.execute(
            "SELECT api_id, home_api_id, away_api_id, kickoff_ts, round, status_short, raw_json "
            "FROM fixture WHERE league_id=? AND season=? AND status_short IN ({}) "
            "AND kickoff_ts IS NOT NULL AND kickoff_ts >= datetime('now', '-3 hours') "
            "ORDER BY kickoff_ts LIMIT ?".format(",".join("?" * len(_UPCOMING))),
            (comp.api_football_id, comp.season, *_UPCOMING, per_league_limit)).fetchall()
        if not fixtures:
            continue
        try:
            sm = cached_strength(conn, comp.key)
        except Exception as e:  # noqa: BLE001 — one comp's model failing must not blank the rest
            print(f"[warn] strength model {comp.key}: {e}")
            continue

        kd = pd_ = None
        if with_venues:
            try:
                from prediction_market_soccer.venues.kalshi.discovery import KalshiDiscovery
                kd = KalshiDiscovery(comp.key)
            except Exception as e:
                print(f"[warn] Kalshi discovery {comp.key}: {e}")
            try:
                from prediction_market_soccer.venues.polymarket_us.discovery import PolymarketUSDiscovery
                pd_ = PolymarketUSDiscovery()
            except Exception as e:
                print(f"[warn] Polymarket US discovery unavailable: {e}")

        out.extend(_build_comp_rows(
            conn, comp, fixtures, sm, kd, pd_, cmap, name_of, zh_of, fidx, book,
            theta, _calib_conf, gate_open_for(_cal, comp.key), caps_for, caps_dict,
            stage_of, leg_of, price_match))
    out.sort(key=lambda r: r.get("kickoff") or "")
    return out


def _build_comp_rows(conn, comp, fixtures, sm, kd, pd_, cmap, name_of, zh_of, fidx,
                     book, theta, _calib_conf, _gate_open, caps_for, caps_dict,
                     stage_of, leg_of, price_match) -> list[dict]:
    out: list[dict] = []
    for f in fixtures:
        hi, ai = cmap.get(f["home_api_id"]), cmap.get(f["away_api_id"])
        stage = stage_of(comp.key, f["round"])
        if not (hi in sm.ratings and ai in sm.ratings):
            # Undetermined KO slot ("Winner of tie X") → tentative placeholder row.
            tent = _tentative_pairing(f["raw_json"]) if stage.value != "league" else None
            if tent:
                out.append({
                    "fixture_id": f["api_id"], "league": comp.key, "league_zh": comp.zh,
                    "kickoff": f["kickoff_ts"], "et": _et_human(f["kickoff_ts"]),
                    "et_date": _et_date(f["kickoff_ts"]), "round": f["round"] or "",
                    "status": f["status_short"] or "", "tentative": True,
                    "home": {"id": None, "name": tent[0], "zh": tent[0]},
                    "away": {"id": None, "name": tent[1], "zh": tent[1]},
                    "caps": caps_dict(caps_for(comp.key, f["round"]), stage),
                    "model": None, "book_devig": None, "kalshi": None, "poly_us": None,
                    "edge": {"vs_book": None, "vs_kalshi": None, "vs_poly_us": None, "best": None},
                    "lock_arb": None, "advance": None,
                })
            continue
        from prediction_market_soccer.util.pricing import model_cents
        leg, agg = leg_of(conn, f["api_id"])
        cp = caps_for(comp.key, f["round"], leg=leg)
        ko = cp.advance   # §3.0: advance path exists ⇔ caps say so (C1 done right)
        mp = price_match(sm, hi, ai, host_neutral=cp.neutral)
        model = {"home": round(mp.p_home, 4), "draw": round(mp.p_draw, 4), "away": round(mp.p_away, 4),
                 "over_2_5": round(mp.p_over_2_5, 4), "btts": round(mp.p_btts, 4),
                 # per-contract ¢ view of the model's fair (= prob×100); ADD ONLY.
                 "cents": model_cents({"home": mp.p_home, "draw": mp.p_draw, "away": mp.p_away})}
        et_date = _et_date(f["kickoff_ts"])

        bd = book.get(f["api_id"])
        book_devig = ({"home": round(bd["bh"], 4), "draw": round(bd["bd"], 4), "away": round(bd["ba"], 4)}
                      if bd else None)

        kalshi_q = poly_q = None
        if kd is not None:
            try:
                kalshi_q = kd.match_quotes(hi, ai)
            except Exception as e:
                print(f"[warn] Kalshi quote {hi} vs {ai}: {e}")
        if pd_ is not None and et_date:
            try:
                poly_q = pd_.match_quotes(hi, ai, et_date)
            except Exception as e:
                print(f"[warn] PolyUS quote {hi} vs {ai}: {e}")

        k_devig, p_devig = _venue_devig(kalshi_q), _venue_devig(poly_q)
        # Knockout 2-way advance quotes (fetched once here so the PRE stash can store them
        # AND the advance block below can reuse them — no double fetch).
        kalshi_a = poly_a = None
        if ko:
            if kd is not None:
                try:
                    kalshi_a = kd.advance_quotes(hi, ai)
                except Exception as e:
                    print(f"[warn] Kalshi advance {hi} vs {ai}: {e}")
            if pd_ is not None and et_date:
                try:
                    poly_a = pd_.advance_quotes(hi, ai, et_date)
                except Exception as e:
                    print(f"[warn] PolyUS advance {hi} vs {ai}: {e}")
        # Stash a PRE milestone snapshot once the match is within ~20 min of kickoff,
        # so an in-progress match has a real pre-match entry ¢ before it settles (the
        # backfill later refines PRE from venue history). INSERT OR IGNORE → first
        # near-kickoff write wins; harmless no-op for far-out fixtures.
        _stash_pre(conn, f["api_id"], f["kickoff_ts"], kalshi_q, poly_q, kalshi_a, poly_a)

        def _edge_vs(devig_probs):
            if not devig_probs:
                return None
            return {s: round(model[s] - devig_probs[s], 4) for s in ("home", "draw", "away")}

        buy_edges = [e for e in (_best_buy_edge(model, kalshi_q, "kalshi", theta),
                                 _best_buy_edge(model, poly_q, "poly_us", theta)) if e]
        best_edge = max(buy_edges, key=lambda e: e["net_edge"]) if buy_edges else None

        form_row = {"home_z": (fidx[hi].form_z if hi in fidx else None),
                    "away_z": (fidx[ai].form_z if ai in fidx else None)}
        # motivation conviction: club version ships weight-0/off (plan §2.2 motivation row)
        decision = _decision_for(model, kalshi_q, poly_q, k_devig, p_devig,
                                 form_row, _calib_conf, _gate_open, ko,
                                 conviction_side=None)

        # ── 2-way ADVANCE block (knockout only) — the SEPARATE who-advances product ──
        # (ET + penalties; Kalshi KXWCADVANCE / Poly US aadc- / Poly Global reach-round).
        # The frontend's "Advances" selector renders this whole block in place of the
        # 3-way one. Group matches have no advance market → adv stays None.
        adv = None
        if ko:
            from prediction_market_soccer.util.pricing import to_cents
            mp_adv = price_match(sm, hi, ai, knockout=True)  # → p_home_advance (single-leg path)
            pha = mp_adv.p_home_advance
            if cp.two_leg and leg == 2 and agg:
                # deciding leg: advance carries the leg-1 aggregate in (C5); the tie
                # table stores agg for team_a = LEG-1 home = today's AWAY side.
                try:
                    from prediction_market_soccer.model.dixon_coles import two_leg_advance_prob
                    a_h, a_a = (int(x) for x in agg.split("-"))
                    pha = two_leg_advance_prob(
                        mp_adv.lam_home, mp_adv.lam_away, a_a, a_h,
                        rho=sm.cfg.dc_rho, kmax=sm.cfg.score_matrix_kmax,
                        et_fraction=sm.cfg.extra_time_fraction,
                        penalty_home_edge=0.5, et_then_pens=cp.et_then_pens)
                except (ValueError, AttributeError):
                    pass
            if pha is not None:
                model_adv = {"home": round(pha, 4), "away": round(1.0 - pha, 4),
                             "cents": {"home": to_cents(pha), "away": to_cents(1.0 - pha)}}
                # kalshi_a / poly_a already fetched above (reused for the PRE stash).
                ka_devig, pa_devig = _venue_devig_2way(kalshi_a), _venue_devig_2way(poly_a)

                def _edge_adv(dv):
                    if not dv:
                        return None
                    return {s: round(model_adv[s] - dv[s], 4) for s in _ADV_SIDES}

                buy_adv = [e for e in (_best_buy_edge_2way(model_adv, kalshi_a, "kalshi", theta),
                                       _best_buy_edge_2way(model_adv, poly_a, "poly_us", theta)) if e]
                best_adv = max(buy_adv, key=lambda e: e["net_edge"]) if buy_adv else None
                decision_adv = _decision_for_2way(model_adv, kalshi_a, poly_a, ka_devig, pa_devig,
                                                  form_row, _calib_conf, _gate_open)
                adv = {
                    "model": model_adv,
                    "kalshi": {**_quote_to_cents_2way(kalshi_a), "devig": ka_devig} if kalshi_a else None,
                    "poly_us": {**_quote_to_cents_2way(poly_a), "devig": pa_devig} if poly_a else None,
                    "edge": {"vs_kalshi": _edge_adv(ka_devig), "vs_poly_us": _edge_adv(pa_devig),
                             "best": best_adv},
                    "decision": decision_adv,
                    "lock_arb": _lock_arb_2way(kalshi_a, poly_a),
                }

        out.append({
            "fixture_id": f["api_id"],
            "league": comp.key, "league_zh": comp.zh,
            "kickoff": f["kickoff_ts"],
            "et": _et_human(f["kickoff_ts"]),
            "et_date": et_date,
            "round": f["round"] or "",
            "status": f["status_short"] or "",
            "home": {"id": hi, "name": name_of.get(hi, hi), "zh": zh_of.get(hi, "")},
            "away": {"id": ai, "name": name_of.get(ai, ai), "zh": zh_of.get(ai, "")},
            "model": model,
            "knockout": ko,
            "caps": caps_dict(cp, stage, agg=agg),   # §3.0 frontend contract
            "motivation": None,   # club motivation ships weight-0 (plan §2.2)
            "form": {"home": (round(fidx[hi].form_z, 2) if hi in fidx else None),
                     "away": (round(fidx[ai].form_z, 2) if ai in fidx else None)},
            "book_devig": book_devig,
            # quote_to_cents adds ask_c/bid_c/mid_c per side (0–1 ask/bid preserved).
            "kalshi": {**quote_to_cents(kalshi_q), "devig": k_devig} if kalshi_q else None,
            "poly_us": {**quote_to_cents(poly_q), "devig": p_devig} if poly_q else None,
            "edge": {
                "vs_book": _edge_vs(book_devig),
                "vs_kalshi": _edge_vs(k_devig),
                "vs_poly_us": _edge_vs(p_devig),
                "best": best_edge,
            },
            "decision": decision,   # production decide(): value side + confidence stake + $1 cap
            "lock_arb": _lock_arb(kalshi_q, poly_q),
            # 2-way knockout "advances" product (null for group stage / no advance market).
            # Same shape as the 3-way fields above but over {home, away} only — the frontend's
            # "Advances" selector swaps the card to render from here.
            "advance": adv,
        })
    return out


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Export per-match cross-venue quotes for the frontend")
    ap.add_argument("--limit", type=int, default=6, help="max upcoming matches")
    ap.add_argument("--no-venues", action="store_true", help="skip live venue quotes (model+book only)")
    args = ap.parse_args()

    rows = build(limit=args.limit, with_venues=not args.no_venues)
    CONFIG.paths.ensure()
    payload = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "n": len(rows),
        "note_key": "notes.upcoming",
        "note": "Real Kalshi (public) + Polymarket US (read creds) single-match quotes; "
                "venue=null only when that venue has not listed the match yet. "
                "Read-only — no orders. Edge/lock are fee-aware (fee=0.01) and theta-gated.",
        "matches": rows,
    }
    out = CONFIG.paths.output / "upcoming.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"upcoming.json written → {out}  ({len(rows)} matches)")
    for m in rows:
        kq = "Kalshi✓" if m["kalshi"] else "Kalshi—"
        pq = "Poly✓" if m["poly_us"] else "Poly—"
        be = m["edge"]["best"]
        edge = f"  best {be['venue']}/{be['side']} {be['net_edge']:+.3f}{'*' if be['tradable'] else ''}" if be else ""
        print(f"  {m['et']}  {m['home']['name']} vs {m['away']['name']}  [{kq} {pq}]{edge}")


if __name__ == "__main__":
    main()
