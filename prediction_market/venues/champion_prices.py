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
