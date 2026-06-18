"""venues/champion_prices.py — real per-contract ¢ for the tournament-winner market
(plan 18 §2.8).

Pulls each team's WORLD-CUP-WINNER YES price from both venues and maps it to our
canonical team_id, so the champion-odds view can show the executable contract price
(¢) next to the model probability:

  * Kalshi  — `KXMENWORLDCUP-26` event; each market carries an inline `yes_ask_dollars`
    (one list_events call, no per-team orderbook hits).
  * Polymarket Global — the public `world-cup-winner` event; each team market carries
    `outcomePrices` ([YES, NO]) inline (one Gamma call, no auth).

Both are READ-ONLY reference prices (0–1) → ¢ = price × 100. A venue that has no
market / is unreachable simply contributes nothing (the field stays absent → '—').
"""
from __future__ import annotations

from prediction_market.ingest.prior_ingest import canonical_team_name, team_id
from prediction_market.util.pricing import to_cents


def _kalshi_champ_cents() -> dict[str, float]:
    """{canonical_team_id: champion ¢} from the Kalshi WC-winner event.

    Use the LAST-TRADED price (`previous_price_dollars`) as the contract value, not the
    raw `yes_ask`: a no-hoper team has NO YES sellers, so Kalshi reports yes_ask = $1.00
    (the cap) — which is not its value (~1–10¢). Last-traded matches Poly's current-price
    semantics. Fall back to the bid / a sane mid when there's no last price.
    """
    def _num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    out: dict[str, float] = {}
    from prediction_market.venues.kalshi.discovery import CHAMPION_SERIES
    from prediction_market.venues.kalshi.market_data import KalshiMarketData
    md = KalshiMarketData()
    for ev in md.list_events(CHAMPION_SERIES, status="open"):
        for m in ev.get("markets", []):
            tid = team_id(canonical_team_name(m.get("yes_sub_title", "") or ""))
            if not tid:
                continue
            ask = _num(m.get("yes_ask_dollars"))
            bid = _num(m.get("yes_bid_dollars"))
            last = _num(m.get("previous_price_dollars"))
            # prefer last-traded; else the bid; else the ask only if it's a real offer (<$1).
            price = last
            if price is None:
                price = bid if bid else (ask if (ask is not None and ask < 0.99) else None)
            if price is not None:
                out[tid] = to_cents(price)
    return out


# Kalshi reach-round ("qualify for round X") markets → our model's per-team round prob.
# These are PER-TEAM 2-way Yes/No markets (NOT the per-match 90-min 3-way) — the genuine
# no-draw "advance" product. The PARENT SERIES is "KXWCROUND"; each round is an EVENT
# under it (event_ticker → round key).
REACH_ROUND_PARENT = "KXWCROUND"
REACH_ROUND_EVENTS = {
    "KXWCROUND-26RO16": "r16",     # reach Round of 16   → model p_r16
    "KXWCROUND-26QUAR": "qf",      # reach Quarterfinals → model p_qf
    "KXWCROUND-26SEMI": "sf",      # reach Semifinals    → model p_sf
    "KXWCROUND-26FINAL": "final",  # reach Final         → model p_final
}
# round key → event ticker (for display/reference in the export).
REACH_ROUND_SERIES = {v: k for k, v in REACH_ROUND_EVENTS.items()}


def _real_price(ask, bid, last):
    """The executable BUY price for a reach-round contract = the YES **ask** (what you'd
    pay), when it's a real offer below the $1.00 no-seller cap. The reach-round books are
    thin: the last-traded price is usually a STALE single fill at an absurd value (e.g.
    France→RO16 last-traded 5¢ while the real ask is 80¢) and the bid is often a lone
    lowball — so neither is the price. ``bid``/``last`` are accepted for signature
    compatibility but ignored. Returns None ('—') when there's no real ask (capped)."""
    def _num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    ask = _num(ask)
    if ask is not None and 0.0 < ask < 1.0:
        return ask
    return None


def _kalshi_reach_round_cents() -> dict[str, dict[str, float]]:
    """{round_key: {team_id: ¢}} — query the PARENT series once, dispatch by event."""
    out: dict[str, dict[str, float]] = {rk: {} for rk in REACH_ROUND_EVENTS.values()}
    from prediction_market.venues.kalshi.market_data import KalshiMarketData
    md = KalshiMarketData()
    for ev in md.list_events(REACH_ROUND_PARENT, status="open"):
        rk = REACH_ROUND_EVENTS.get(ev.get("event_ticker"))
        if not rk:
            continue
        for m in ev.get("markets", []):
            tid = team_id(canonical_team_name(m.get("yes_sub_title", "") or ""))
            if not tid:
                continue
            price = _real_price(m.get("yes_ask_dollars"), m.get("yes_bid_dollars"),
                                m.get("previous_price_dollars"))
            if price is not None:
                out[rk][tid] = to_cents(price)
    return out


# Polymarket Global per-round qualifier events (read-only). These ARE listed and liquid
# for all 48 teams — the "Nation To Reach <round>" markets (YES = reach that round).
POLY_REACH_ROUND_SLUGS = {
    "advance": "world-cup-team-to-advance-to-knockout-stages",  # reach R32 (advance from group)
    "r16": "world-cup-nation-to-reach-round-of-16",
    "qf": "world-cup-nation-to-reach-quarterfinals",
    "sf": "world-cup-nation-to-reach-semifinals",
    "final": "world-cup-nation-to-reach-final",
}

# All round keys (Kalshi has no 'advance' market — only Poly + the model do).
REACH_ROUND_KEYS = ("advance", "r16", "qf", "sf", "final")


def _poly_reach_round_cents() -> dict[str, dict[str, float]]:
    """{round_key: {team_id: ¢}} from Polymarket Global's per-round "Nation To Reach
    <round>" markets (YES price × 100). Liquid for all 48 teams."""
    out: dict[str, dict[str, float]] = {rk: {} for rk in REACH_ROUND_KEYS}
    import json as _json
    from prediction_market.venues.polymarket_global.reader import PolymarketGlobalReader
    r = PolymarketGlobalReader()
    for rk, slug in POLY_REACH_ROUND_SLUGS.items():
        try:
            evs = r.list_events(slug=slug) or []
        except Exception as e:
            print(f"[champion_prices] poly reach_round {rk} skipped: {e}")
            continue
        if not evs:
            continue
        for m in evs[0].get("markets", []) or []:
            tid = team_id(canonical_team_name(m.get("groupItemTitle", "") or ""))
            op = m.get("outcomePrices")
            if isinstance(op, str):
                try:
                    op = _json.loads(op)
                except Exception:
                    op = None
            if tid and op:
                try:
                    yes = float(op[0])
                    if 0.0 < yes < 1.0:
                        out[rk][tid] = to_cents(yes)
                except (TypeError, ValueError, IndexError):
                    pass
    return out


def reach_round_cents() -> dict[str, dict[str, dict[str, float]]]:
    """{round_key: {'kalshi': {team: ¢}, 'poly': {team: ¢}}} for advance/r16/qf/sf/final —
    failure-tolerant per venue. ('advance' = reach R32 from the group; Kalshi has no such
    market, only Poly + the model.)"""
    kal = {rk: {} for rk in REACH_ROUND_KEYS}
    poly = {rk: {} for rk in REACH_ROUND_KEYS}
    try:
        kal.update(_kalshi_reach_round_cents())
    except Exception as e:
        print(f"[champion_prices] kalshi reach_round skipped: {e}")
    try:
        poly.update(_poly_reach_round_cents())
    except Exception as e:
        print(f"[champion_prices] poly reach_round skipped: {e}")
    return {rk: {"kalshi": kal.get(rk, {}), "poly": poly.get(rk, {})}
            for rk in REACH_ROUND_KEYS}


def _poly_champ_cents() -> dict[str, float]:
    """{canonical_team_id: YES ¢} from the Poly Global world-cup-winner event."""
    out: dict[str, float] = {}
    from prediction_market.venues.polymarket_global.reader import PolymarketGlobalReader
    r = PolymarketGlobalReader()
    evs = r.list_events(slug="world-cup-winner") or []
    if not evs:
        return out
    import json as _json
    for m in evs[0].get("markets", []) or []:
        tid = team_id(canonical_team_name(m.get("groupItemTitle", "") or ""))
        op = m.get("outcomePrices")
        if isinstance(op, str):
            try:
                op = _json.loads(op)
            except Exception:
                op = None
        if tid and op:
            try:
                out[tid] = to_cents(float(op[0]))
            except (TypeError, ValueError, IndexError):
                pass
    return out


def champion_cents() -> dict[str, dict]:
    """{canonical_team_id: {"kalshi_c": ¢|None, "poly_c": ¢|None}} for all teams that
    either venue prices. Each venue is fetched independently and failure-tolerant —
    a down/empty venue just yields no entries (never raises)."""
    out: dict[str, dict] = {}
    for fn, key in ((_kalshi_champ_cents, "kalshi_c"), (_poly_champ_cents, "poly_c")):
        try:
            for tid, c in fn().items():
                out.setdefault(tid, {})[key] = c
        except Exception as e:  # venue unreachable / shape change — skip, never break sim
            print(f"[champion_prices] {key} skipped: {e}")
    return out
