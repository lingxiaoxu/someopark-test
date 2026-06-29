"""Polymarket Global — READ-ONLY reference reader (plan 07 §2, 08).

Polymarket Global (`clob.polymarket.com` + Gamma + Data) is the deeper, often
price-leading pool. From the US it is **readable but NOT tradable** (order
geoblock). We use it purely as a third reference price for cross-venue value
(plan 08): when Polymarket US diverges from Global's de-vigged price, that is a
signal the thinner US pool may be mispriced.

NO credentials required — all endpoints are public. This class deliberately does
NOT implement ExecutionVenue; `venue_guard` additionally blocks any order routed
to `poly_global`.

Three public APIs (plan 07 §2):
  * Gamma  (`gamma-api.polymarket.com`)  — market/event discovery + metadata
  * CLOB   (`clob.polymarket.com`)       — order book / price / midpoint reads
  * Data   (`data-api.polymarket.com`)   — public trades / positions / OI

Identifiers: condition_id (market) / token_id (a YES or NO outcome). Prices are
0–1 and read directly as implied probabilities.
"""
from __future__ import annotations

from decimal import Decimal

import requests

from prediction_market.config import CONFIG
from prediction_market.venues.base import OrderBook

_VENUE = "poly_global"

# Per-team "reach round X" events (YES = that team reaches the round). Polymarket Global
# has NO dedicated per-match advance market (unlike Kalshi KXWCADVANCE / Poly US aadc-…);
# the 2-way advance for a knockout match is the two teams' reach-NEXT-round YES prices
# (they sum to ~1 since exactly one advances). Keyed by our round codes.
REACH_ROUND_SLUGS = {
    "advance": "world-cup-team-to-advance-to-knockout-stages",  # reach R32 (qualify from group)
    "r16": "world-cup-nation-to-reach-round-of-16",
    "qf": "world-cup-nation-to-reach-quarterfinals",
    "sf": "world-cup-nation-to-reach-semifinals",
    "final": "world-cup-nation-to-reach-final",
}


def parse_clob_book(payload: dict, token_id: str) -> OrderBook:
    """Normalise a CLOB `/book` response into our two-sided OrderBook.

    Unlike Kalshi (bids only), Polymarket returns both `bids` and `asks` for a
    token directly. For the YES token: yes_bid = best (max) bid, yes_ask = best
    (min) ask; the NO side is the binary complement (1 − yes price).
    """
    bids = payload.get("bids") or []
    asks = payload.get("asks") or []
    # Polymarket returns levels as {"price": "0.42", "size": "100"}; best bid is
    # the highest price, best ask the lowest.
    best_bid = max((Decimal(str(b["price"])) for b in bids), default=None)
    best_ask = min((Decimal(str(a["price"])) for a in asks), default=None)
    bid_depth = next((Decimal(str(b["size"])) for b in bids
                      if Decimal(str(b["price"])) == best_bid), None) if best_bid is not None else None
    ask_depth = next((Decimal(str(a["size"])) for a in asks
                      if Decimal(str(a["price"])) == best_ask), None) if best_ask is not None else None

    one = Decimal("1")
    no_bid = (one - best_ask) if best_ask is not None else None
    no_ask = (one - best_bid) if best_bid is not None else None
    return OrderBook(
        venue=_VENUE, market_key=token_id,
        yes_bid=best_bid, yes_ask=best_ask, no_bid=no_bid, no_ask=no_ask,
        yes_depth=bid_depth, no_depth=ask_depth,
    )


class PolymarketGlobalReader:
    """Read-only client for Polymarket Global. Never places orders."""

    name = _VENUE
    executable = False

    def __init__(self, *, timeout: float = 15.0):
        self.cfg = CONFIG.venue
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers.update({"User-Agent": "someopark-prediction-market/1.0"})

    def _get(self, base: str, path: str, params: dict | None = None):
        r = self._s.get(f"{base}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ── Gamma: discovery ─────────────────────────────────────────────────────
    def list_events(self, **params) -> list[dict]:
        return self._get(self.cfg.pmglobal_gamma, "/events", params)

    def list_markets(self, **params) -> list[dict]:
        return self._get(self.cfg.pmglobal_gamma, "/markets", params)

    def search_events(self, query: str, *, limit: int = 200) -> list[dict]:
        """Client-side keyword filter over Gamma events (slug/title contains query)."""
        q = query.lower()
        events = self.list_events(limit=limit, closed="false")
        return [e for e in events
                if q in (e.get("slug", "") + " " + e.get("title", "")).lower()]

    # ── CLOB: order book / price ─────────────────────────────────────────────
    def get_book(self, token_id: str) -> OrderBook:
        payload = self._get(self.cfg.pmglobal_clob, "/book", {"token_id": token_id})
        return parse_clob_book(payload, token_id)

    def get_price(self, token_id: str, side: str = "buy") -> Decimal | None:
        payload = self._get(self.cfg.pmglobal_clob, "/price", {"token_id": token_id, "side": side})
        p = payload.get("price")
        return Decimal(str(p)) if p is not None else None

    def get_midpoint(self, token_id: str) -> Decimal | None:
        payload = self._get(self.cfg.pmglobal_clob, "/midpoint", {"token_id": token_id})
        m = payload.get("mid")
        return Decimal(str(m)) if m is not None else None

    # ── Data: public trades ──────────────────────────────────────────────────
    def recent_trades(self, **params) -> list[dict]:
        return self._get(self.cfg.pmglobal_data, "/trades", params)

    # ── CLOB: historical price series (plan 18 §2.4b) ────────────────────────
    def prices_history(self, token_id: str, *, start_ts: int | None = None,
                       end_ts: int | None = None, fidelity: int = 1,
                       interval: str | None = None) -> list[dict]:
        """Per-minute (or coarser) historical price series for a token.

        Public CLOB endpoint, no auth. Returns [{"ts": <unix>, "price": <0..1 float>}]
        sorted ascending. `fidelity` is the bar size in minutes (1 = per-minute).
        Pass an explicit start_ts/end_ts window, or `interval` (e.g. 'max','1w','1d').
        """
        params: dict = {"market": token_id, "fidelity": fidelity}
        if start_ts is not None:
            params["startTs"] = int(start_ts)
        if end_ts is not None:
            params["endTs"] = int(end_ts)
        if interval is not None:
            params["interval"] = interval
        elif start_ts is None and end_ts is None:
            params["interval"] = "max"
        payload = self._get(self.cfg.pmglobal_clob, "/prices-history", params)
        out = []
        for p in payload.get("history") or []:
            t, pr = p.get("t"), p.get("p")
            if t is not None and pr is not None:
                out.append({"ts": int(t), "price": float(pr)})
        return out

    # ── Gamma: World-Cup single-match discovery (closed/archived too) ─────────
    def list_wc_match_events(self, *, soccer_tag: str = "100350",
                             max_pages: int = 16, page: int = 100,
                             end_date_min: str | None = None,
                             end_date_max: str | None = None) -> list[dict]:
        """All World-Cup single-match BASE events (slug `fifwc-{h}-{a}-{date}`),
        INCLUDING closed/archived ones (so already-played matches are reachable —
        the live `search_events` only sees open events).

        Pass an `end_date_min`/`end_date_max` ISO window (recommended) to filter the
        Gamma query to the match dates — far more reliable than paging blindly through
        thousands of unrelated soccer events. Each returned dict:
        {slug, date, teams: {name: token_id}, raw}; `teams` maps each 3-way outcome's
        groupItemTitle (real team name / 'Draw …') to its YES clob token.
        """
        import json as _json
        import re as _re
        base_re = _re.compile(r"^fifwc-([a-z]{2,4})-([a-z]{2,4})-(\d{4}-\d{2}-\d{2})$")
        win: dict = {}
        if end_date_min:
            win["end_date_min"] = end_date_min
        if end_date_max:
            win["end_date_max"] = end_date_max
        out: list[dict] = []
        seen: set[str] = set()
        # Query BOTH closed and still-open events: a match that JUST finished can
        # linger as closed=false for a while before Polymarket archives it, but its
        # price history is already complete — so we must see open events too, else a
        # just-ended match can't be backfilled until Poly gets around to closing it.
        for closed in ("true", "false"):
            for offset in range(0, max_pages * page, page):
                try:
                    evs = self.list_events(limit=page, closed=closed, tag_id=soccer_tag,
                                           archived="true", offset=offset,
                                           order="endDate", ascending="false", **win) or []
                except Exception:
                    break
                if not evs:
                    break
                for e in evs:
                    slug = e.get("slug", "") or ""
                    if slug in seen:
                        continue
                    seen.add(slug)
                    m = base_re.match(slug)
                    if not m:
                        continue
                    teams: dict[str, str] = {}
                    for mk in e.get("markets") or []:
                        git = (mk.get("groupItemTitle") or mk.get("question") or "").strip()
                        toks = mk.get("clobTokenIds")
                        if isinstance(toks, str):
                            try:
                                toks = _json.loads(toks)
                            except Exception:
                                toks = None
                        if git and toks:
                            teams[git] = toks[0]
                    out.append({"slug": slug, "date": m.group(3), "teams": teams, "raw": e})
        return out

    def find_match_event(self, slug: str) -> dict | None:
        """Fetch a single event by exact slug (e.g. 'fifwc-fra-sen-2026-06-16')."""
        evs = self.list_events(slug=slug)
        return evs[0] if evs else None

    # ── Gamma: per-team reach-round (the Global 2-way "advance") ──────────────
    def reach_round_index(self, round_key: str) -> dict[str, str]:
        """{canonical_team_id: YES clob token_id} for the per-team "reach <round>" event.

        round_key ∈ REACH_ROUND_SLUGS (advance/r16/qf/sf/final). Cached per (instance,round)."""
        import json as _json
        from prediction_market.ingest.prior_ingest import canonical_team_name, team_id
        cache = getattr(self, "_rri", None)
        if cache is None:
            cache = self._rri = {}
        if round_key in cache:
            return cache[round_key]
        slug = REACH_ROUND_SLUGS.get(round_key)
        idx: dict[str, str] = {}
        if slug:
            evs = self.list_events(slug=slug) or []
            for mk in (evs[0].get("markets", []) if evs else []):
                tid = team_id(canonical_team_name(mk.get("groupItemTitle", "") or ""))
                toks = mk.get("clobTokenIds")
                if isinstance(toks, str):
                    try:
                        toks = _json.loads(toks)
                    except Exception:
                        toks = None
                if tid and toks:
                    idx[tid] = toks[0]
        cache[round_key] = idx
        return idx

    def advance_quotes(self, home_id: str, away_id: str, round_key: str) -> dict[str, dict] | None:
        """{home/away: {'ask','bid'}} 2-way advance for a knockout match (None if not found).

        Derived from the two teams' "reach <round_key>" YES books (no dedicated per-match
        market on Global). round_key is the match's NEXT round (R32 match → 'r16', R16 → 'qf',
        QF → 'sf', SF → 'final'). Mirrors the {home/away} shape of the Kalshi/Poly-US advance."""
        idx = self.reach_round_index(round_key)
        th, ta = idx.get(home_id), idx.get(away_id)
        if not (th and ta):
            return None

        def q(token):
            ob = self.get_book(token)
            return {"ask": float(ob.yes_ask) if ob.yes_ask is not None else None,
                    "bid": float(ob.yes_bid) if ob.yes_bid is not None else None}

        return {"home": q(th), "away": q(ta)}


if __name__ == "__main__":
    r = PolymarketGlobalReader()
    wc = r.search_events("world-cup", limit=300) or r.search_events("world cup", limit=300)
    print(f"Gamma: found {len(wc)} open event(s) matching 'world cup'")
    for e in wc[:5]:
        print(f"  - {e.get('slug')}  ({len(e.get('markets', []))} markets)")
    if not wc:
        print("  (none open right now — Gamma discovery still reachable)")
