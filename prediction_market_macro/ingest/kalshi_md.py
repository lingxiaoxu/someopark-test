"""ingest/kalshi_md.py — Kalshi public market data (no auth; PLAN §5).

Lessons baked in (measured, this repo):
  * /markets list endpoints return NO prices → per-ticker /orderbook is mandatory
  * orderbook payload is {"orderbook_fp": {"yes_dollars": [[price, $depth]...],
    "no_dollars": [[...]]}} — yes_ask is derived from the best NO bid (1 - no_bid)
  * rate limit: ~10 req/s public → 0.18s spacing + retry. _pace() is PER-INSTANCE and
    therefore per-process: macrotick overlapping the refresh is what has
    produced every 429 in the alert log (2026-08-03 shows two series failing in the SAME
    second — impossible inside one serial loop). Retry rides that out; it does not
    prevent it. A cross-process pacer is the real cure if 429s ever stop being rare.
"""
from __future__ import annotations

import json
import random as _rand
import time as _time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

BASE = "https://api.elections.kalshi.com/trade-api/v2"


@dataclass(frozen=True)
class OB:
    yes_bid: float | None
    yes_ask: float | None
    bid_depth: float          # $ resting at best yes bid
    ask_depth: float          # $ resting at best no bid (what a YES taker lifts)


def _get(path: str, params: dict | None = None, tries: int = 9, timeout: float = 30) -> dict:
    q = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = f"{BASE}{path}" + (f"?{q}" if q else "")
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "someopark-macro"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if i == tries - 1:
                raise
            if e.code == 429:                     # rate limited: honor Retry-After, long backoff
                ra = e.headers.get("Retry-After")
                if ra:
                    try:
                        _time.sleep(float(ra))
                        continue
                    except ValueError:            # HTTP-date form — fall through to our own
                        pass
                # 6 tries of 3+2i rode out only ~35s, which was short of the window that
                # actually bites here: the limit is breached when a second PROCESS
                # (macrotick) overlaps the refresh, and _pace() is per-instance
                # so it cannot see that. Every 429 in the log burned all 6 tries. Capped
                # exponential + jitter rides out ~2.5min instead; jitter matters precisely
                # BECAUSE two processes collide — identical backoff makes them retry in
                # lockstep and collide again.
                _time.sleep(min(30.0, 3.0 * 1.7 ** i) * (0.75 + 0.5 * _rand.random()))
                continue
            if e.code == 404:                      # definitive: retrying never helps
                raise
            _time.sleep(1.2 * (i + 1))
        except Exception as e:                                    # noqa: BLE001
            last = e
            if i == tries - 1:
                raise
            _time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"kalshi GET failed {url}: {last}")


_HELD_TRIES, _HELD_TIMEOUT = 2, 8


class KalshiMD:
    def __init__(self, conn=None, spacing: float = 0.18):
        self._conn = conn
        self._spacing = spacing
        self._t_last = 0.0
        self.on_progress = None  # executor may service cheap clock-only tasks

    def _pace(self):
        if self.on_progress is not None:
            self.on_progress()
        dt = _time.monotonic() - self._t_last
        if dt < self._spacing:
            _time.sleep(self._spacing - dt)
        self._t_last = _time.monotonic()

    # ── discovery ─────────────────────────────────────────────────────────
    def events(self, series: str, status: str = "open") -> list[dict]:
        out, cursor = [], ""
        while True:
            self._pace()          # INSIDE the loop: pages 2..N used to go out back-to-back
            params = {"series_ticker": series, "status": status, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            d = _get("/events", params)
            out += d.get("events", [])
            cursor = d.get("cursor") or ""
            if not cursor:
                return out

    def markets(self, event_ticker: str) -> list[dict]:
        self._pace()
        return _get("/markets", {"event_ticker": event_ticker, "limit": 100}).get("markets", [])

    def settled_markets(self, series: str, limit: int = 1000) -> list[dict]:
        out, cursor = [], ""
        while len(out) < limit:
            self._pace()          # INSIDE the loop, matching historical_markets()
            params = {"series_ticker": series, "status": "settled", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            d = _get("/markets", params)
            out += d.get("markets", [])
            cursor = d.get("cursor") or ""
            if not cursor:
                break
        return out[:limit]

    def historical_markets(self, series: str, limit: int = 5000) -> list[dict]:
        """Settled markets OLDER than the live cutoff (~3 months): /historical/markets
        (measured 2026-07-28: cursor-paginated, same market shape, reaches back to
        series launch). Live /markets prunes these — this is the ONLY public source."""
        out, cursor = [], ""
        while len(out) < limit:
            self._pace()
            params = {"series_ticker": series, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            d = _get("/historical/markets", params)
            out += d.get("markets", [])
            cursor = d.get("cursor") or ""
            if not cursor:
                break
        return out[:limit]

    # ── prices ────────────────────────────────────────────────────────────
    def market(self, ticker: str, *, tries: int = 9, timeout: float = 30) -> dict:
        self._pace()
        market = _get(f"/markets/{ticker}", tries=tries, timeout=timeout).get("market")
        if not isinstance(market, dict) or market.get("ticker") != ticker:
            raise ValueError(f"invalid market metadata for {ticker}")
        return market

    def orderbook(self, ticker: str, depth: int = 8, *, tries: int = 9,
                  timeout: float = 30) -> OB:
        self._pace()
        d = _get(f"/markets/{ticker}/orderbook", {"depth": depth},
                 tries=tries, timeout=timeout)
        fp = d.get("orderbook_fp") or {}
        yes = [(float(p), float(s)) for p, s in (fp.get("yes_dollars") or [])]
        no = [(float(p), float(s)) for p, s in (fp.get("no_dollars") or [])]
        yes_bid = max(yes, key=lambda x: x[0]) if yes else None
        no_bid = max(no, key=lambda x: x[0]) if no else None
        return OB(
            yes_bid=yes_bid[0] if yes_bid else None,
            yes_ask=round(1.0 - no_bid[0], 4) if no_bid else None,
            bid_depth=yes_bid[1] if yes_bid else 0.0,
            ask_depth=no_bid[1] if no_bid else 0.0,
        )

    # 2026-09-14: tries=1/timeout=5 meant one network hiccup dropped a held leg for a
    # whole round, and with the tick firing every 900s against a 1200s freshness bar a
    # single miss made the leg stale and the WHOLE portfolio unpriceable on the panel.
    # These are the marks the user sees; one cheap retry is worth more than the 5s saved.
    def snapshot_tickers(self, tickers, *, before_each=None) -> dict:
        """Refresh held market status and quotes, with per-leg failure isolation.

        Unlike discovery this runs throughout the day. Never stamp an unsuccessful
        request as fresh; marks/exits will reject the retained old quote on age.
        """
        assert self._conn is not None, "snapshot requires a db connection"
        saved, failed = [], {}
        for ticker in sorted(set(tickers)):
            if before_each is not None:
                before_each()
            try:
                market = self.market(ticker, tries=_HELD_TRIES, timeout=_HELD_TIMEOUT)
                # An exchange can close early. Persist its latest state before
                # asking for an orderbook, including when that request then fails.
                self._conn.execute(
                    "UPDATE contracts SET status=?,close_time=? WHERE ticker=?",
                    (market.get("status"), market.get("close_time"), ticker))
                self._conn.commit()
                close = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
                if market.get("status") != "active" or close <= datetime.now(timezone.utc):
                    raise ValueError("held market is not active or has closed")
                if before_each is not None:
                    before_each()
                ob = self.orderbook(ticker, tries=_HELD_TRIES, timeout=_HELD_TIMEOUT)
                ts = datetime.now(timezone.utc).isoformat()
                self._conn.execute(
                    "INSERT OR REPLACE INTO quotes(ts,ticker,yes_bid,yes_ask,bid_depth,ask_depth)"
                    " VALUES(?,?,?,?,?,?)",
                    (ts, ticker, ob.yes_bid, ob.yes_ask, ob.bid_depth, ob.ask_depth))
                self._conn.commit()
                saved.append(ticker)
            except Exception as exc:
                failed[ticker] = str(exc)[:200]
        return {"refreshed": saved, "failed": failed}

    # ── snapshot into db (contracts + quotes) ─────────────────────────────
    def snapshot_series(self, series: str) -> int:
        assert self._conn is not None, "snapshot requires a db connection"
        now = datetime.now(timezone.utc).isoformat()
        n = 0
        for ev in self.events(series, "open"):
            evt = ev["event_ticker"]
            period = evt.split("-", 1)[1] if "-" in evt else evt
            for m in self.markets(evt):
                t = m["ticker"]
                self._conn.execute(
                    "INSERT OR REPLACE INTO contracts(ticker, series, event_ticker, period,"
                    " sub_title, strike_type, floor_strike, cap_strike, close_time, status,"
                    " first_seen_ts) VALUES(?,?,?,?,?,?,?,?,?,?,"
                    " COALESCE((SELECT first_seen_ts FROM contracts WHERE ticker=?), ?))",
                    (t, series, evt, period, m.get("yes_sub_title"), m.get("strike_type"),
                     m.get("floor_strike"), m.get("cap_strike"), m.get("close_time"),
                     m.get("status"), t, now))
                ob = self.orderbook(t)
                self._conn.execute(
                    "INSERT OR REPLACE INTO quotes(ts, ticker, yes_bid, yes_ask, bid_depth,"
                    " ask_depth) VALUES(?,?,?,?,?,?)",
                    (now, t, ob.yes_bid, ob.yes_ask, ob.bid_depth, ob.ask_depth))
                n += 1
        self._conn.commit()
        return n

    def sync_settlements(self, series: str, deep: bool = False) -> int:
        """deep=True additionally walks /historical/markets back to series launch —
        run once per series (idempotent) to seed the backtest universe; the daily
        refresh keeps using the cheap live pass."""
        assert self._conn is not None
        now = datetime.now(timezone.utc).isoformat()
        n = 0
        markets = self.settled_markets(series)
        if deep:
            markets = markets + self.historical_markets(series)
        for m in markets:
            evt = m.get("event_ticker", "")
            period = evt.split("-", 1)[1] if "-" in evt else evt
            self._conn.execute(
                "INSERT OR IGNORE INTO settlements(ticker, series, period, result, settled_ts,"
                " first_seen_ts) VALUES(?,?,?,?,?,?)",
                (m["ticker"], series, period,
                 m.get("result"), m.get("settlement_time") or m.get("close_time"), now))
            # strikes of settled markets feed the backtest ladder reconstruction
            self._conn.execute(
                "INSERT OR IGNORE INTO contracts(ticker, series, event_ticker, period,"
                " sub_title, strike_type, floor_strike, cap_strike, close_time, status,"
                " first_seen_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (m["ticker"], series, evt, period, m.get("yes_sub_title"),
                 m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike"),
                 m.get("close_time"), "settled", now))
            # OR IGNORE leaves a pre-existing 'active' row untouched — Kalshi's
            # listing stops returning settled markets, so without this the board
            # keeps showing decided markets forever (KXFED 26JUL bug)
            if m.get("result") in ("yes", "no"):
                self._conn.execute(
                    "UPDATE contracts SET status='settled' WHERE ticker=?"
                    " AND status='active'", (m["ticker"],))
            n += 1
        self._conn.commit()
        return n

    def candles(self, series: str, ticker: str, start_ts: int, end_ts: int,
                interval_min: int = 1440) -> int:
        """Daily candlesticks for one market into the candles table. A 404 means Kalshi
        never generated candlesticks for this ticker (illiquid/never-traded leg) — that's
        permanent, not transient, so we record a NULL-price sentinel row to stop future
        backfill passes from retrying it forever."""
        assert self._conn is not None
        self._pace()
        try:
            d = _get(f"/series/{series}/markets/{ticker}/candlesticks",
                     {"period_interval": interval_min, "start_ts": start_ts, "end_ts": end_ts})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self._conn.execute(
                    "INSERT OR IGNORE INTO candles(ticker, end_ts, yes_bid_close,"
                    " yes_ask_close, price_close, volume) VALUES(?,0,NULL,NULL,NULL,NULL)",
                    (ticker,))
                self._conn.commit()
                return 0
            raise
        n = 0
        for c in d.get("candlesticks") or []:
            yb = (c.get("yes_bid") or {}).get("close_dollars")
            ya = (c.get("yes_ask") or {}).get("close_dollars")
            pc = (c.get("price") or {}).get("close_dollars")
            self._conn.execute(
                "INSERT OR REPLACE INTO candles(ticker, end_ts, yes_bid_close, yes_ask_close,"
                " price_close, volume) VALUES(?,?,?,?,?,?)",
                (ticker, c["end_period_ts"],
                 float(yb) if yb else None, float(ya) if ya else None,
                 float(pc) if pc else None, float(c.get("volume_fp") or 0)))
            n += 1
        self._conn.commit()
        return n
