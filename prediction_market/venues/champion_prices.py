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


def _sub_team(m: dict) -> str:
    """Team name from a Kalshi market's yes_sub_title, defensively stripping the
    knockout-round "Reg Time: " prefix (mirrors venues/kalshi/discovery.match_index).

    Champion (KXMENWORLDCUP) and reach-round (KXWCROUND / KXWCGROUPQUAL) markets are
    tournament-level, so they carry the bare team name today. This strip is a no-op
    there, but keeps the parse correct if Kalshi ever prefixes these like the per-match
    3-way books — so a knockout format change can never blank these columns."""
    sub = (m.get("yes_sub_title", "") or "").strip()
    if sub.lower().startswith("reg time:"):
        sub = sub.split(":", 1)[1].strip()
    return sub


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
            tid = team_id(canonical_team_name(_sub_team(m)))
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

# 'advance' (reach the Round of 32 = qualify from the group) is a SEPARATE Kalshi series:
# "KXWCGROUPQUAL", with one EVENT per group (KXWCGROUPQUAL-26A…26L) and a per-team Yes/No
# market inside each. (KXWCROUND only covers R16→Final.) These books are thin/illiquid early
# in the tournament — most sit at ask $1 / no bid — so the column fills in as they trade.
REACH_ROUND_ADVANCE_SERIES = "KXWCGROUPQUAL"


def _real_price(ask, bid, last):
    """Sane executable mark for a thin reach-round contract, from the YES ask/bid/last (dollars).

    The reach-round books are thin and frequently CROSSED: a stale lone SELL order can sit at
    1¢ while the live bid is 60¢ and the last trade 63¢ (real case: Argentina→QF ask $0.01,
    bid $0.60, last $0.63). So the raw ask alone is NOT the price — using it makes a strong
    team look like a 1¢ free-arb. Rule:
      * well-formed book — the ask is a real offer AT/ABOVE the bid (0<ask<1) → use the ask
        (what you'd actually pay), and
      * crossed / no real ask (ask < bid, or ask capped at $1 with no sellers) → fall back to
        the live best BID (a real resting order), else the last trade.
    Returns None ('—') when none of the three is a real in-range price (e.g. a settled market).
    Verified: this recovers the QF/SF/Final marks to within a few ¢ of liquid Polymarket."""
    def _num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    def _ok(x):
        return x is not None and 0.0 < x < 1.0

    a, b, l = _num(ask), _num(bid), _num(last)
    if _ok(a) and (b is None or a >= b):   # well-formed book → executable ask
        return a
    if _ok(b):                              # crossed / capped ask → live best bid
        return b
    if _ok(l):                              # last resort → last trade
        return l
    return None


def _reach_market_cents(m: dict) -> float | None:
    """¢ mark for one Kalshi reach-round per-team market.

    SETTLEMENT-AWARE FIRST — this is the crux. Kalshi keeps FINALIZED per-team markets nested
    under the still-open round event, so ``list_events(status='open')`` returns them too, with an
    empty live book (ask $1.00 / bid $0.00) but a STALE last-trade. Reading ask/bid/last there
    falls back to that stale last (e.g. Morocco RO16 finalized-YES but last=$0.37 → a fake 37¢
    and a bogus +63% edge, while the contract is actually worth 100¢). A decided market is worth
    its RESULT (100¢ yes / 0¢ no), not its last trade. Only an ACTIVE market uses _real_price.
    Returns None when there's no reliable mark (settled-but-unresulted, or a dead book)."""
    status = str(m.get("status") or "").lower()
    if status in ("finalized", "settled", "determined"):
        res = str(m.get("result") or "").lower()
        if res == "yes":
            return 100.0
        if res == "no":
            return 0.0
        return None                    # settled without a result yet → no reliable mark
    price = _real_price(m.get("yes_ask_dollars"), m.get("yes_bid_dollars"),
                        m.get("last_price_dollars"))
    return to_cents(price) if price is not None else None


def _kalshi_reach_round_cents() -> dict[str, dict[str, float]]:
    """{round_key: {team_id: ¢}} for advance/r16/qf/sf/final, from the Kalshi reach-round books.

    R16→Final live under the KXWCROUND parent (one event per round); 'advance' (reach R32 =
    qualify from group) lives under KXWCGROUPQUAL (one event per group). A SETTLED market is
    worth its result (100¢ yes / 0¢ no); an active one uses _real_price (ask on a well-formed
    book, else bid/last on a crossed/thin one)."""
    out: dict[str, dict[str, float]] = {"advance": {}, "r16": {}, "qf": {}, "sf": {}, "final": {}}
    from prediction_market.venues.kalshi.market_data import KalshiMarketData
    md = KalshiMarketData()

    def _fill(round_key: str, ev: dict) -> None:
        for m in ev.get("markets", []):
            tid = team_id(canonical_team_name(_sub_team(m)))
            if not tid:
                continue
            c = _reach_market_cents(m)
            if c is not None:
                out[round_key][tid] = c

    # R16 / QF / SF / Final — KXWCROUND, dispatch each event to its round.
    for ev in md.list_events(REACH_ROUND_PARENT, status="open"):
        rk = REACH_ROUND_EVENTS.get(ev.get("event_ticker"))
        if rk:
            _fill(rk, ev)
    # Advance (reach R32 / qualify from group) — KXWCGROUPQUAL, one event per group, all → 'advance'.
    try:
        for ev in md.list_events(REACH_ROUND_ADVANCE_SERIES, status="open"):
            _fill("advance", ev)
    except Exception as e:
        print(f"[champion_prices] kalshi advance ({REACH_ROUND_ADVANCE_SERIES}) skipped: {e}")
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
