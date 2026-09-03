"""EVENT-contract execution router (W5's venue) — Plan 00 layering applies.

All Kalshi trading functionality — demo or prod — lives HERE in the execution
layer, never inside strategy modules (design rule re-affirmed by the user
2026-08-25 after a first draft put the demo mirror inside w5_knockdown.py).
Strategy modules express INTENT (side, close hour, price zone, size); this
router owns venues, environments, mapping, and gates.

Two paths, same philosophy as the perps ``ExecutionRouter``:

  * ``submit(...)``       — prod path, HARD-GATED (env prod + ALLOW_LIVE_ORDERS
                            + dedicated key). Not armed until the user says so.
  * ``mirror_demo(...)``  — parallel demo rehearsal. Demo generates its own
                            strike ladder around its own index, so the prod
                            ticker never exists there; the mirror maps the
                            intent to demo's market for the SAME close hour
                            whose price sits nearest the prod entry, and sends
                            the SAME contract count as the paper loop (1:1
                            fidelity — a mirror that trades a different size is
                            not a mirror). Never raises into the caller's paper
                            loop.

Known demo limitation (probed 2026-08-25, crypto-dev/15 §demo): KXBTC event
markets live on a non-zero exchange instance where the demo user has no funded
account ("user_not_found" on auto-route), and the instance-transfer API is not
yet public. mirror_demo() therefore reports the venue's answer truthfully —
whether it is a fill or a structural refusal is itself the probe's data.
"""
from __future__ import annotations

import logging

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


def _demo_markets_cached(series_tuple: tuple = ("KXBTC",)) -> list[dict] | None:
    """Demo KXBTC market list — single attempt, short timeout, 5-min cache.

    Deliberately NOT the strips client: that one retries with backoff on 429
    (measured minutes-long loops), which would violate the isolation principle
    even inside the mirror thread by piling up threads. One try; on any
    failure serve the cache if fresh-ish, else skip this mirror entirely.
    """
    import time

    import requests

    from crypto_trading.crypto_common.kalshi.enums import rest_base
    now = time.time()
    ttl = (_MKT_CACHE_TTL_15M if any("15M" in x for x in series_tuple)
           else _MKT_CACHE_TTL)
    slot = _MKT_CACHE.setdefault("|".join(series_tuple), {"ts": 0.0, "markets": None})
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
                quoted += [m for m in r.json().get("markets", [])
                           if ask_cents(m, "yes") or ask_cents(m, "no")]
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
    except requests.RequestException:
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


class EventExecutionRouter:
    """Order router for Kalshi event contracts (binary settlement)."""

    def __init__(self, *, strategy: str):
        self.strategy = strategy

    # ── prod path: deliberately NOT armed ─────────────────────────────────
    def submit(self, **_) -> dict:
        """Prod events ordering is the ARMING step — a separate, explicit user
        decision (crypto-dev/15 §5.3). Refuses loudly until that lands."""
        raise NotImplementedError(
            "prod events ordering is not armed — see crypto-dev/15 §5.3")

    # ── demo mirror ───────────────────────────────────────────────────────
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
