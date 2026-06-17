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
    """A REAL 0<p<1 mark: reach-round books are thin in the group stage, so 0.00
    (placeholder) and 1.00 (no-seller cap) are NOT prices. Prefer last-traded, then the
    bid (what buyers will pay), then a genuine ask (<0.99)."""
    def _num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    ask, bid, last = _num(ask), _num(bid), _num(last)
    if last is not None and 0.0 < last < 1.0:
        return last
    if bid is not None and 0.0 < bid < 1.0:
        return bid
    if ask is not None and 0.0 < ask < 0.99:
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


def _poly_reach_round_cents() -> dict[str, dict[str, float]]:
    """{round_key: {team_id: ¢}} from Polymarket, IF it lists per-round qualifier markets.
    As of the group stage Poly Global only lists the champion ("world-cup-winner") market
    and NO reach-round markets — so this returns empty until/unless Poly lists them."""
    out: dict[str, dict[str, float]] = {rk: {} for rk in REACH_ROUND_EVENTS.values()}
    # Candidate Poly slugs per round (Poly hasn't listed these for WC-26; ready if it does).
    slugs = {"final": ("world-cup-finalist", "world-cup-finalists", "world-cup-to-reach-the-final"),
             "sf": ("world-cup-semifinalist", "world-cup-semifinalists", "world-cup-final-four"),
             "qf": ("world-cup-quarterfinalist", "world-cup-quarterfinalists"),
             "r16": ("world-cup-round-of-16", "world-cup-to-reach-round-of-16")}
    try:
        import json as _json
        from prediction_market.venues.polymarket_global.reader import PolymarketGlobalReader
        r = PolymarketGlobalReader()
        for rk, cand in slugs.items():
            for slug in cand:
                evs = r.list_events(slug=slug) or []
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
                            out[rk][tid] = to_cents(float(op[0]))
                        except (TypeError, ValueError, IndexError):
                            pass
                break  # first slug that resolves wins for this round
    except Exception as e:
        print(f"[champion_prices] poly reach_round skipped: {e}")
    return out


def reach_round_cents() -> dict[str, dict[str, dict[str, float]]]:
    """{round_key: {'kalshi': {team: ¢}, 'poly': {team: ¢}}} — failure-tolerant per venue."""
    kal = {rk: {} for rk in REACH_ROUND_EVENTS.values()}
    poly = {rk: {} for rk in REACH_ROUND_EVENTS.values()}
    try:
        kal = _kalshi_reach_round_cents()
    except Exception as e:
        print(f"[champion_prices] kalshi reach_round skipped: {e}")
    try:
        poly = _poly_reach_round_cents()
    except Exception as e:
        print(f"[champion_prices] poly reach_round skipped: {e}")
    return {rk: {"kalshi": kal.get(rk, {}), "poly": poly.get(rk, {})}
            for rk in REACH_ROUND_EVENTS.values()}


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
