"""Kalshi PUBLIC market data (plan 01 §3-§4). No auth required.

Two layers:
  * ``best_prices`` — PURE parser turning a raw orderbook payload into the
    derived two-sided best bid/ask (binary identity: YES ask = 1 - best NO bid).
    Unit-tested without any network.
  * ``KalshiMarketData`` — thin REST client (markets / orderbook discovery).
    Hits the live (demo or prod) public endpoints; used by the ingest layer.

Prices use Decimal throughout (plan 01 §4.3 — never float for money).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal

import requests

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.util.market_identity import content_hash
from prediction_market_soccer.venues.base import OrderBook


def best_prices(orderbook_payload: dict, *, venue: str = "kalshi", market_key: str = "") -> OrderBook:
    """Parse a Kalshi orderbook payload into a normalised OrderBook.

    Expects ``{"orderbook_fp": {"yes_dollars": [[price, size], ...],
    "no_dollars": [...]}}`` with levels ascending by price (best bid last,
    plan 01 §4.2). Missing sides yield None.
    """
    ob = orderbook_payload.get("orderbook_fp", orderbook_payload)
    yes = ob.get("yes_dollars") or []
    no = ob.get("no_dollars") or []

    yes_bid = Decimal(str(yes[-1][0])) if yes else None
    no_bid = Decimal(str(no[-1][0])) if no else None
    yes_depth = Decimal(str(yes[-1][1])) if yes else None
    no_depth = Decimal(str(no[-1][1])) if no else None

    one = Decimal("1")
    yes_ask = (one - no_bid) if no_bid is not None else None  # buy-YES fill price
    no_ask = (one - yes_bid) if yes_bid is not None else None

    return OrderBook(
        venue=venue, market_key=market_key,
        yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
        yes_depth=yes_depth, no_depth=no_depth,
    )


class KalshiMarketData:
    """Read-only public REST client (no auth). Demo or prod per KALSHI_ENV."""

    def __init__(self, base_url: str | None = None, *, timeout: float = 10.0):
        self.base = base_url or CONFIG.venue.kalshi_rest
        self.timeout = timeout
        self._session = requests.Session()

    # Public-API 429 handling (club edition): 12 competitions × 3-leg orderbooks per
    # poll trips Kalshi's public rate limit where the single WC league never did —
    # gentle pacing + exponential backoff instead of surfacing 429s to every caller.
    _MIN_INTERVAL_S = 0.18

    def _get(self, path: str, params: dict | None = None) -> dict:
        import time
        last = getattr(self, "_last_req_ts", 0.0)
        wait = self._MIN_INTERVAL_S - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        backoff = 1.0
        for attempt in range(5):
            resp = self._session.get(f"{self.base}{path}", params=params, timeout=self.timeout)
            self._last_req_ts = time.time()
            if resp.status_code == 429:
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def list_series(self, category: str = "Sports") -> list[dict]:
        return self._get("/series", {"category": category}).get("series", [])

    # One series' open events are the same answer for every fixture in that
    # competition, but the exporters ask per fixture: the live loop was re-fetching
    # the identical list ~150 times a cycle, which at the 0.18s pace plus 429 backoff
    # is where a "60 second" loop spent 188 seconds. Cached per process — each live
    # cycle is a fresh process, so a hit can never outlive the cycle that made it,
    # whatever the TTL says. The TTL is therefore sized to COVER a whole cycle (a
    # measured 92s at the 12-hour horizon) rather than expire inside one: at 45s the
    # back half of a cycle re-fetched every series it had already read.
    _EVENTS_TTL_S = 180.0
    _events_cache: dict = {}
    #: series tickers whose discovery was rate-limited this process, and why. Read by
    #: the exporters so an empty board can say WHICH markets it could not see — an
    #: unreported empty list is indistinguishable from "this series has no markets".
    unavailable: dict = {}

    def list_events(self, series_ticker: str, status: str = "open") -> list[dict]:
        key=(self.base,series_ticker,status)
        now=time.time()
        hit=self._events_cache.get(key)
        if hit and now-hit[0] < (self._EVENTS_TTL_S if hit[2]['complete'] else 15):
            self.last_discovery_status=dict(hit[2])
            return list(hit[1])
        out,seen,cursors={},set(),set()
        cursor=None; complete=False; issues=[]; pages=0
        for page_no in range(32):
            params={'series_ticker':series_ticker,'status':status,'with_nested_markets':'true','limit':200}
            if cursor: params['cursor']=cursor
            try:
                result=self._get('/events',params)
                pages+=1
            except Exception as exc:
                issues.append({'code':'request_failed','page':page_no,'error':type(exc).__name__})
                break
            for event in result.get('events') or []:
                eid=event.get('event_ticker')
                if not eid: continue
                if eid in out and out[eid] != event:
                    issues.append({'code':'conflicting_event','event_id':eid})
                    out.pop(eid,None); seen.add(eid)
                elif eid not in seen: out[eid]=event
            cursor=result.get('cursor')
            if not cursor:
                complete=True
                break
            if cursor in cursors:
                issues.append({'code':'repeated_cursor','page':page_no})
                break
            cursors.add(cursor)
        if not complete and not issues: issues.append({'code':'truncated_listing'})
        complete=complete and not issues
        at=datetime.now(timezone.utc).isoformat()
        info={'complete':complete,'state':'ok' if complete else 'partial','pages':pages,'issues':issues,
              'last_attempt_at':at,'last_complete_at':at if complete else (hit[2].get('last_complete_at') if hit else None)}
        if not complete:
            self.unavailable[series_ticker]='incomplete event listing'
            old={e['event_ticker']:e for e in (hit[1] if hit else []) if e['event_ticker'] not in seen}
            old.update(out); out=old
        else:
            self.unavailable.pop(series_ticker,None)
        events=list(out.values())
        self._events_cache[key]=(now,events,info)
        self.last_discovery_status=info
        return events

    def list_markets(self, **filters) -> list[dict]:
        """Bounded cursor listing; incomplete pages never masquerade as complete."""
        out, cursors, cursor = [], set(), None
        for _ in range(100):
            params = dict(filters, limit=1000)
            if cursor:
                params['cursor'] = cursor
            page = self._get('/markets', params)
            out.extend(page.get('markets') or [])
            cursor = page.get('cursor')
            if not cursor:
                return out
            if cursor in cursors:
                raise ValueError('repeated_market_cursor')
            cursors.add(cursor)
        raise ValueError('truncated_market_listing')

    def get_orderbook_capture(self, ticker: str):
        started=datetime.now(timezone.utc).isoformat()
        payload=self._get(f'/markets/{ticker}/orderbook')
        received=datetime.now(timezone.utc).isoformat()
        return best_prices(payload,market_key=ticker),payload,started,received

    def get_event_milestones_capture(self, event_ticker: str):
        """One public GET for an exact event; no pagination or automatic retry.

        This fallback is only used for an otherwise exact pair with a stale ticker
        date. A cursor or malformed result remains incomplete. Successful captures
        are reused for at most 60 seconds, retaining their real capture clocks.
        """
        cache = self.__dict__.setdefault('_milestone_captures', {})
        now = time.time()
        prior = cache.get(event_ticker)
        if prior and 0 <= now - prior[0] <= 60:
            self.last_milestone_requests = 0
            return prior[1]
        params = {'related_event_ticker': event_ticker, 'limit': 50}
        started = datetime.now(timezone.utc).isoformat()
        self.last_milestone_requests = 1
        response = self._session.get(self.base + '/milestones', params=params,
                                     timeout=self.timeout, allow_redirects=False)
        received = datetime.now(timezone.utc).isoformat()
        if (300 <= response.status_code < 400 or response.history
                or not isinstance(response.url, str)
                or response.url.split('?', 1)[0] != self.base + '/milestones'):
            raise ValueError('milestone_redirect_refused')
        response.raise_for_status()
        raw = response.json()
        complete = (isinstance(raw, dict) and isinstance(raw.get('milestones'), list)
                    and raw.get('cursor') in ('', None))
        capture = {'endpoint': self.base + '/milestones', 'params': params,
                   'request_started_at': started, 'received_at': received,
                   'raw': raw, 'raw_hash': content_hash(raw),
                   'complete': complete, 'http_requests': 1}
        if complete:
            cache[event_ticker] = (now, capture)
        return capture

    def get_orderbook(self, ticker: str) -> OrderBook:
        return self.get_orderbook_capture(ticker)[0]

    def candlesticks(self, series_ticker: str, ticker: str, start_ts: int, end_ts: int,
                     period_interval: int = 1) -> list[dict]:
        """Historical OHLC price bars for a market (plan 18 §2.4b). Public, no auth.

        period_interval is the bar size in minutes (1 / 60 / 1440). Returns
        [{"ts","ask","bid","last","vol"}] (prices 0–1), ascending by time — usable
        to reconstruct any past minute's contract price even after the event closed
        (the live event index only lists OPEN markets, but candlesticks persist).
        """
        path = f"/series/{series_ticker}/markets/{ticker}/candlesticks"
        payload = self._get(path, {"start_ts": int(start_ts), "end_ts": int(end_ts),
                                   "period_interval": period_interval})

        def _d(node, *keys):
            for k in keys:
                if isinstance(node, dict) and k in node and node[k] is not None:
                    try:
                        return float(node[k])
                    except (TypeError, ValueError):
                        return None
            return None

        out = []
        for c in payload.get("candlesticks") or []:
            ts = c.get("end_period_ts") or c.get("ts")
            ask = _d(c.get("yes_ask") or {}, "close_dollars", "close")
            bid = _d(c.get("yes_bid") or {}, "close_dollars", "close")
            last = _d(c.get("price") or {}, "close_dollars", "previous_dollars", "mean_dollars")
            vol = _d(c, "volume_fp", "volume")
            if ts is not None:
                out.append({"ts": int(ts), "ask": ask, "bid": bid, "last": last, "vol": vol})
        return out
