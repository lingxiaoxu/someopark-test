"""EVENT-contract execution router (W5's venue) — Plan 00 layering applies.

All Kalshi trading functionality — demo or prod — lives HERE in the execution
layer, never inside strategy modules (design rule re-affirmed by the user
2026-08-25 after a first draft put the demo mirror inside w5_knockdown.py).
Strategy modules express INTENT (side, close hour, price zone, size); this
router owns venues, environments, mapping, and gates.

Two paths, same philosophy as the perps ``ExecutionRouter``:

  * ``submit(...)``       — prod path, HARD-GATED (env prod + ALLOW_LIVE_ORDERS
                            + dedicated key). Not armed until the user says so.
  * ``mirror_demo(...)``  — parallel demo rehearsal at the paper contract
                            count. W7 uses the same coin and 15M close, reads
                            a fresh demo book, and enforces its MAIN entry
                            band. W5 retains its legacy nearest-price mapping
                            across demo's separate strike ladder. Never raises
                            into the caller's paper loop.

Known demo limitation (probed 2026-08-25, crypto-dev/15 §demo): KXBTC event
markets live on a non-zero exchange instance where the demo user has no funded
account ("user_not_found" on auto-route), and the instance-transfer API is not
yet public. mirror_demo() therefore reports the venue's answer truthfully —
whether it is a fill or a structural refusal is itself the probe's data.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)


def ask_cents(m: dict, side: str) -> int | None:
    """Ask on our side in integer cents, tolerant of BOTH wire schemas.

    The batch /markets endpoint serves `yes_ask_dollars` STRINGS on demo
    (measured 2026-08-25); other environments/endpoints use integer-cent
    `yes_ask`. Reading only the integer field made every demo market look
    unquoted — the mirror starved silently until an end-to-end test caught it.
    Returns None when unquoted (missing, 0, or 100 = empty-book sentinel).
    """
    key = "yes_ask" if side == "yes" else "no_ask"
    v = m.get(key)
    if v is None:
        vd = m.get(key + "_dollars")
        if vd is None:
            return None
        try:
            v = round(float(vd) * 100)
        except (TypeError, ValueError):
            return None
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if 1 <= v <= 99 else None


def _summary_ask_dollars(m: dict, side: str) -> float | None:
    """Read the dollar field first, including 0/1 empty-book sentinels.

    Unlike the legacy hourly selector, the 15M execution path must retain
    decimal cents. This value is evidence only; the live book prices orders.
    """
    key = f"{side}_ask"
    raw = m.get(key + "_dollars")
    dollars = raw is not None
    if not dollars:
        raw = m.get(key)
    try:
        value = Decimal(str(raw)) / (1 if dollars else 100)
        if value.is_finite() and 0 <= value <= 1:
            return float(value)
    except (InvalidOperation, TypeError, ValueError):
        pass
    return None


def demo_executable_ask(ticker: str, side: str, *, timeout: float = 8.0) -> dict:
    """Read one fresh DEMO book; no retries, strategy gates, or order calls.

    Buying YES consumes NO bids, and buying NO consumes YES bids. Prices
    retain the venue's four dollar decimals instead of rounding to cents.
    ``ready`` includes the executable ask and available top/total quantity;
    ``empty_side`` is a valid empty opposite ladder; malformed or failed reads
    are ``orderbook_unavailable`` and must never be interpreted as empty.
    This helper is shared by demo execution consumers, including W8 exits.
    """
    import requests
    import time

    from crypto_trading.crypto_common.kalshi.enums import rest_base

    started = time.monotonic()
    result = {"status": "orderbook_unavailable", "demo_ticker": ticker,
              "side": side, "book_ask_dollars": None,
              "book_best_opposite_bid_dollars": None,
              "book_top_quantity": 0.0, "book_side_quantity": 0.0,
              "book_observed_at": None, "book_http_status": None,
              "book_request_started_at": datetime.now(timezone.utc).isoformat(),
              "book_read_duration_ms": None}
    if side not in ("yes", "no") or not ticker:
        return {**result, "book_error": "invalid_ticker_or_side"}
    try:
        response = requests.get(
            rest_base("demo") + f"/markets/{ticker}/orderbook", timeout=timeout,
            headers={"User-Agent": "someopark-crypto/0.1"})
        result["book_observed_at"] = datetime.now(timezone.utc).isoformat()
        result["book_read_duration_ms"] = round((time.monotonic()-started)*1000, 3)
        result["book_http_status"] = response.status_code
        if response.status_code != 200:
            return {**result, "book_error": f"http_{response.status_code}"}
        payload = response.json()
        book = payload.get("orderbook_fp") if isinstance(payload, dict) else None
        ladder_key = "no_dollars" if side == "yes" else "yes_dollars"
        ladder = book.get(ladder_key) if isinstance(book, dict) else None
        if not isinstance(ladder, list):
            return {**result, "book_error": "malformed_orderbook"}
        levels = []
        for level in ladder:
            if not isinstance(level, (list, tuple)) or len(level) != 2:
                return {**result, "book_error": "malformed_level"}
            bid, quantity = (Decimal(str(value)) for value in level)
            if (not bid.is_finite() or not quantity.is_finite()
                    or not 0 <= bid <= 1 or quantity < 0
                    or bid != bid.quantize(Decimal("0.0001"))):
                return {**result, "book_error": "malformed_level"}
            if 0 < bid < 1 and quantity > 0:
                levels.append((bid, quantity))
        if not levels:
            return {**result, "status": "empty_side", "book_levels": 0}
        best_bid = max(bid for bid, _ in levels)
        return {**result, "status": "ready", "book_levels": len(levels),
                "book_ask_dollars": float(Decimal(1) - best_bid),
                "book_best_opposite_bid_dollars": float(best_bid),
                "book_top_quantity": float(sum(q for p, q in levels if p == best_bid)),
                "book_side_quantity": float(sum(q for _, q in levels))}
    except (requests.RequestException, ValueError, TypeError, InvalidOperation) as exc:
        return {**result, "book_observed_at": datetime.now(timezone.utc).isoformat(),
                "book_error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def choose_demo_market(markets: list[dict], close_time: str, side: str,
                       entry_price: float, *, now_iso: str | None = None,
                       exact_only: bool = False) -> tuple[str, int, str] | None:
    """Pure selection with graceful degradation.

    Preferred: a QUOTED demo market for the same close hour, ask nearest the
    prod entry. Measured reality (2026-08-25): demo only quotes the daily
    events — hourly ladders sit unquoted — so a strict same-hour rule fires
    ~never. Fallback: nearest-price quoted market at the EARLIEST future
    close. The deviation is returned (mapped close_time) so every mirror log
    shows exactly how far the rehearsal drifted from the prod intent.
    Returns (ticker, ask_cents, mapped_close) or None if nothing is quoted.

    ``now_iso``: candidates whose close_time is not strictly in the future
    are dropped — the cached list can hold windows that closed since the
    fetch (measured 2026-09-01: 117 mirror orders → 409 market_closed).
    ``exact_only``: no fallback at all — for 15-minute windows a different
    close is a different bet, so the only honest answer is no_demo_market.
    """
    def cands(require_close):
        out = []
        for m in markets:
            ct_m = m.get("close_time") or ""
            if require_close and ct_m != close_time:
                continue
            if now_iso and ct_m <= now_iso:
                continue                            # already closed
            ask_c = ask_cents(m, side)
            if ask_c is None:
                continue
            out.append((abs(ask_c / 100.0 - entry_price),
                        m.get("close_time") or "", m["ticker"], ask_c))
        return out
    exact = cands(True)
    if exact:
        _, ct, tkr, ask_c = min(exact)
        return tkr, ask_c, ct
    if exact_only:
        return None
    any_q = cands(False)
    if not any_q:
        return None
    # nearest price first, then earliest close among quoted markets
    _, ct, tkr, ask_c = min(any_q, key=lambda x: (x[0], x[1]))
    return tkr, ask_c, ct


class _Stale(list):
    """A market list served from an expired cache after a failed refetch.
    Still a list (callers can use it), but carries the age so the mirror log
    can say "we could not ask" instead of "the venue had nothing"."""

    def __init__(self, markets, age_s):
        super().__init__(markets)
        self.age_s = age_s


_MKT_CACHE: dict = {}
_MKT_CACHE_TTL = 300.0
_MKT_CACHE_TTL_15M = 90.0      # 15M windows are born every 15 min — one tape cycle


def _demo_markets_cached(series_tuple: tuple = ("KXBTC",), *,
                         include_unquoted: bool = False) -> list[dict] | None:
    """Demo market discovery, cached for 90s (15M) or 5 minutes (legacy).

    Deliberately NOT the strips client: that one retries with backoff on 429
    (measured minutes-long loops), which would violate the isolation principle
    even inside the mirror thread by piling up threads. One try; on any
    failure serve the cache if fresh-ish, else skip this mirror entirely.
    ``include_unquoted`` gives W7 an independent identity-only cache. Legacy
    callers retain their quoted-market filtering and existing cache behavior.
    """
    import time

    import requests

    from crypto_trading.crypto_common.kalshi.enums import rest_base
    now = time.time()
    ttl = (_MKT_CACHE_TTL_15M if any("15M" in x for x in series_tuple)
           else _MKT_CACHE_TTL)
    cache_key = "|".join(series_tuple) + ("|all_markets" if include_unquoted else "")
    slot = _MKT_CACHE.setdefault(cache_key, {"ts": 0.0, "markets": None})
    if slot["markets"] is not None and now - slot["ts"] < ttl:
        return slot["markets"]
    try:
        # Quoted markets hide behind a wall of ~5,000 unquoted hourly strikes
        # (page 5 of 6 — measured), and an 8-page sweep both crawls and courts
        # 429s. min_close_ts skips the wall in ONE request: demo only quotes
        # the far-dated flagship events anyway. A plain near-page is added as
        # a fallback so nearer quotes (if demo ever adds them) are not missed.
        quoted = []
        ok_any = False                       # at least one 200 → list is REAL
        for series in series_tuple:
            # 15M windows live only ~an hour ahead: the +12h probe can never
            # hit them and just spends a request (5 threads at once → 429).
            probes = ({},) if "15M" in series else ({"min_close_ts": int(now + 12 * 3600)}, {})
            for extra in probes:
                r = requests.get(rest_base("demo") + "/markets",
                                 params={"series_ticker": series,
                                         "status": "open", "limit": 1000, **extra},
                                 timeout=8,
                                 headers={"User-Agent": "someopark-crypto/0.1"})
                if r.status_code != 200:
                    continue
                ok_any = True
                # 15M discovery is about market identity, not a cached quote.
                # An empty/lagging summary (or a 99.6c ask) must not remove a
                # real market before the execution path can inspect its book.
                quoted += [m for m in r.json().get("markets", [])
                           if include_unquoted
                           or ask_cents(m, "yes") or ask_cents(m, "no")]
                if quoted:
                    break
            if quoted:
                break
        if quoted or ok_any:
            # an EMPTY but successful answer is a real answer ("demo quotes
            # nothing here right now") and must map to no_demo_market, not
            # to "list unavailable" (2026-09-01: 24 mislabelled skips once the
            # flagship fallback — which always had far-dated quotes — was
            # removed for 15M)
            slot.update(ts=now, markets=quoted)
            return quoted
    except (requests.RequestException, ValueError, TypeError):
        pass
    # A stale cache is fine for a link rehearsal, but it must not LAUNDER a
    # failure into "demo quotes nothing here": the mirror ledger's only job is
    # to answer, before prod arming, whether the venue had our window or we
    # failed to ask. 15M dispatches are 900s apart so the 90s TTL is always
    # expired — this branch is the normal path, not a corner case. Signal the
    # staleness to the caller instead of hiding it (2026-09-02 audit).
    if slot["markets"] is not None and now - slot["ts"] < 1800:
        return _Stale(slot["markets"], round(now - slot["ts"]))
    return None


class LiveOrderRefused(RuntimeError):
    """A live intent hit a closed gate. Never downgraded silently."""


class EventExecutionRouter:
    """Order router for Kalshi event contracts (binary settlement)."""

    armed = False

    def __init__(self, *, strategy: str):
        self.strategy = strategy

    # ── prod path: implemented, ARMED ONLY BY THE USER ────────────────────
    MIN_SHARD2_USD = 30.0       # one 25-lot at the band ceiling ≈ $24.50 + fee

    def gate_status(self) -> dict:
        """Every condition between this code and real money; never raises.

        Mirrors the perps router's design (execution.py): a live order goes
        out only when ALL gates are open, and the caller may never silently
        downgrade a live intent. The gates, and who controls them:
          env_allows   ALLOW_LIVE_ORDERS=1 in the environment       — the user
          cfg_armed    live_orders: true under the strategy config  — the user
          dedicated_key a non-borrowed prod events key exists       — setup
          shard2_funded prod exchange-instance 2 holds enough cash  — the user
                        (crypto events live on shard 2; funding is UI-only,
                        crypto-dev/15 §2.5 — an unfunded shard means every
                        order dies with user_not_found)
        """
        from crypto_trading.crypto_common.config import (allow_live_orders,
                                                         kalshi_key)
        status = {"env_allows": allow_live_orders(), "cfg_armed": self.armed,
                  "dedicated_key": False, "shard2_funded": False}
        try:
            status["dedicated_key"] = not kalshi_key(
                "margin", borrowed_ok=False).borrowed
        except RuntimeError as e:
            status["key_error"] = str(e)[:120]
        if status["dedicated_key"]:
            try:
                from crypto_trading.crypto_common.kalshi.rest_event import (
                    KalshiEventOrderClient)
                b = KalshiEventOrderClient(env="prod")._authed(
                    "GET", "/portfolio/balance").json()
                shard2 = next((float(x["balance"]) for x in
                               b.get("balance_breakdown", [])
                               if x.get("exchange_index") == 2), 0.0)
                status["shard2_usd"] = round(shard2, 2)
                status["shard2_funded"] = shard2 >= self.MIN_SHARD2_USD
            except Exception as e:                          # noqa: BLE001
                status["shard2_error"] = str(e)[:120]
        status["live_open"] = all(status.get(k) for k in
                                  ("env_allows", "cfg_armed",
                                   "dedicated_key", "shard2_funded"))
        return status

    def submit(self, *, ticker: str, side: str, entry_price: float,
               contracts: int, armed: bool = False) -> dict:
        """Submit one PROD events order (IOC taker, V2 single book).

        ``armed=False`` (the default, and the shipped configuration) records a
        full dry-run audit row of exactly what WOULD have been sent — so the
        moment the user arms the gate, nothing else about the pipeline
        changes. ``armed=True`` sends only if EVERY gate is open; a closed
        gate raises LiveOrderRefused rather than silently downgrading.
        """
        self.armed = armed
        intent = {"strategy": self.strategy, "env": "prod", "ticker": ticker,
                  "side": side, "price_dollars": round(entry_price, 4),
                  "contracts": contracts, "tif": "immediate_or_cancel"}
        if not armed:
            logger.info("[%s] LIVE-DISARMED %s %s x%d @ %.4f (audit only)",
                        self.strategy, ticker, side, contracts, entry_price)
            return {"status": "live_disarmed", **intent}
        gate = self.gate_status()
        if not gate["live_open"]:
            raise LiveOrderRefused(f"live events order refused — gate: {gate}")
        from crypto_trading.crypto_common.kalshi.rest_event import (
            KalshiEventOrderClient)
        r = KalshiEventOrderClient(env="prod").create_order(
            ticker=ticker, side=side, count=int(contracts),
            price_dollars=float(entry_price), tif="immediate_or_cancel")
        logger.warning("[%s] LIVE ORDER SENT %s %s x%d @ %.4f -> HTTP %s",
                       self.strategy, ticker, side, contracts, entry_price,
                       r.get("status_code"))
        return {"status": "live_sent", **intent, **r}

    # ── demo mirror ───────────────────────────────────────────────────────
    def _mirror_w7_demo(self, *, side: str, close_time: str, entry_price: float,
                        contracts: int, series: str) -> dict:
        """W7's same-coin/window MAIN execution, isolated from W5 behavior."""
        from crypto_trading.crypto_common.kalshi.rest_event import (
            KalshiEventOrderClient)

        main_lo, main_hi = 0.78, 0.98
        evidence = {"env": "demo", "execution_version": "w7_demo_book_v1",
                    "series": series, "side": side, "contracts": contracts,
                    "demo_ticker": None, "close": close_time,
                    "prod_close": close_time, "mapped_close": None,
                    "entry_price_dollars": entry_price,
                    "entry_in_main": main_lo <= entry_price <= main_hi,
                    "main_price_bounds": [main_lo, main_hi],
                    "demo_ask_in_main": None, "summary_ask_dollars": None,
                    "summary_ask_raw": None, "summary_book_delta_cents": None}
        markets = _demo_markets_cached((series,), include_unquoted=True)
        if markets is None:
            return {**evidence, "status": "skipped_market_list_unavailable"}
        stale_age = getattr(markets, "age_s", None)
        if stale_age is not None:
            evidence["market_list_stale_s"] = stale_age
        now = datetime.now(timezone.utc)
        try:
            target_close = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            if target_close.tzinfo is None:
                raise ValueError("close_time must have a timezone")
        except (TypeError, ValueError, AttributeError):
            return {**evidence, "status": "invalid_close_time"}
        if target_close <= now:
            return {**evidence, "status": "market_closed"}
        exact = []
        for market in markets:
            ticker = market.get("ticker", "")
            if not ticker.startswith(series + "-"):
                continue
            if market.get("status") not in (None, "active", "open"):
                continue
            try:
                market_close = datetime.fromisoformat(
                    market.get("close_time", "").replace("Z", "+00:00"))
            except (TypeError, ValueError, AttributeError):
                continue
            if market_close == target_close:
                exact.append(market)
        if not exact:
            return {**evidence,
                    "status": ("skipped_market_list_unavailable" if stale_age is not None
                               else "missing_market")}
        # A 15M series has one contract per close. Refuse ambiguous identity
        # instead of choosing a different strike or coin by approximate price.
        by_ticker = {market["ticker"]: market for market in exact}
        if len(by_ticker) != 1:
            return {**evidence, "status": "ambiguous_market",
                    "candidate_tickers": sorted(by_ticker)}
        market = next(iter(by_ticker.values()))
        ticker = market["ticker"]
        summary = _summary_ask_dollars(market, side)
        evidence.update(demo_ticker=ticker, mapped_close=market["close_time"],
                        summary_ask_dollars=summary,
                        summary_ask_raw={key: market.get(key) for key in
                                         (f"{side}_ask", f"{side}_ask_dollars")})
        quote = demo_executable_ask(ticker, side)
        evidence.update({key: value for key, value in quote.items() if key != "status"})
        if quote["status"] != "ready":
            return {**evidence, "status": quote["status"]}
        if quote.get("book_read_duration_ms", 0) > 2000:
            return {**evidence, "status": "orderbook_stale"}
        ask = quote["book_ask_dollars"]
        evidence["demo_ask_in_main"] = main_lo <= ask <= main_hi
        if summary is not None and 0 < summary < 1:
            evidence["summary_book_delta_cents"] = round((ask - summary) * 100, 4)
        if not evidence["entry_in_main"] or not evidence["demo_ask_in_main"]:
            return {**evidence, "status": "price_outside_main"}
        # Recheck the close after the read. Never send after a slow response
        # has consumed the remaining window, and never retry an IOC blindly.
        if target_close <= datetime.now(timezone.utc):
            return {**evidence, "status": "market_closed"}
        try:
            response = KalshiEventOrderClient(env="demo").create_order(
                ticker=ticker, side=side, count=contracts, price_dollars=ask,
                tif="immediate_or_cancel")
        except Exception as exc:                          # noqa: BLE001
            return {**evidence, "status": "error", "error": str(exc)[:200],
                    "price_dollars": ask, "price_cents": round(ask * 100, 4)}
        # Keep price_cents for existing audit readers, now fractional when
        # required. The wire is priced with four-decimal dollar precision.
        return {**evidence, "status": "sent", "price_dollars": ask,
                "price_cents": round(ask * 100, 4), **response}

    def mirror_demo(self, *, side: str, close_time: str, entry_price: float,
                    contracts: int,
                    series: tuple = ("KXBTC",)) -> dict:
        """Mirror one signal into the demo venue at PAPER-IDENTICAL size.
        ``series`` = preference order; demo quoting is sparse (dailies only),
        so callers pass their own series first and the flagship as fallback."""
        try:
            from crypto_trading.crypto_common.kalshi.rest_event import (
                KalshiEventOrderClient)
            # 15-minute windows: same-window ONLY, no flagship fallback — a
            # different close is a different bet, and the flagship fallback
            # produced garbage fills (2026-09-01: 409 market_closed x117 and
            # a stale April market). Hourly/daily keep the graceful path.
            strict = any("15M" in x for x in series)
            if strict:
                series = tuple(x for x in series if "15M" in x)
            if strict and self.strategy == "w7_noisefade":
                return self._mirror_w7_demo(
                    side=side, close_time=close_time, entry_price=entry_price,
                    contracts=contracts, series=series[0])
            mkts = _demo_markets_cached(series)
            if mkts is None:
                return {"status": "skipped_market_list_unavailable"}
            import datetime as _dt
            now_iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            pick = choose_demo_market(mkts, close_time, side, entry_price,
                                      now_iso=now_iso, exact_only=strict)
            if pick is None:
                stale = getattr(mkts, "age_s", None)
                if stale is not None:
                    return {"status": "skipped_market_list_unavailable",
                            "close": close_time, "stale_list_age_s": stale}
                return {"status": "no_demo_market", "close": close_time}
            tkr, ask_c, mapped_close = pick
            r = KalshiEventOrderClient(env="demo").create_order(
                ticker=tkr, side=side, count=contracts, price_cents=ask_c)
            rec = {"status": "sent", "demo_ticker": tkr,
                   "price_cents": ask_c, "contracts": contracts,
                   "prod_close": close_time, "mapped_close": mapped_close, **r}
            logger.info("[%s] EVENTS DEMO-MIRROR %s %s ×%d @ %dc → %s",
                        self.strategy, tkr, side, contracts, ask_c,
                        r.get("status_code"))
            return rec
        except Exception as e:                              # noqa: BLE001
            logger.warning("[%s] events demo mirror failed: %s", self.strategy, e)
            return {"status": "error", "error": str(e)[:200]}
