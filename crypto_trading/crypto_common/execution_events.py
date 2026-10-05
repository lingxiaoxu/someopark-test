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

import json
import logging
import math
import time
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


def _trim_table(decision: dict) -> dict:
    """Audit copy of a table decision for the order row (no bulky inputs)."""
    keep = ("action", "reason", "bin", "slot", "mult", "scenario", "slots", "table_run",
            "table_age_s", "cell_mean_c", "missing")
    out = {k: decision[k] for k in keep if k in decision}
    inp = decision.get("inputs") or {}
    out["inputs"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in inp.items()
                     if k in ("mkt_trend60", "mkt_vr2", "dev8h", "flow_1m", "mom_1m_bp", "flow_valid")}
    return out


def _fill_count(order_result: dict, default: float) -> float:
    """Contracts filled according to the venue response; `default` (the count sent)
    when the response cannot be read, so the window cap errs on the safe side."""
    try:
        resp = order_result.get("response")
        resp = json.loads(resp) if isinstance(resp, str) else (resp or {})
        return float(resp["fill_count"])
    except (KeyError, TypeError, ValueError):
        return float(default)


class EventExecutionRouter:
    """Order router for Kalshi event contracts (binary settlement)."""

    armed = False

    def __init__(self, *, strategy: str):
        self.strategy = strategy

    # ── prod path: implemented, ARMED ONLY BY THE USER ────────────────────
    MIN_SHARD2_USD = 30.0       # one 25-lot at the band ceiling ≈ $24.50 + fee

    # PROD-ONLY sizing override (user directives 2026-09-30): real orders are
    # resized here; the paper book stays at config `contracts` (25 — the
    # registered cell) and the demo mirror inherits the caller's size
    # unchanged. Keyed strategy→series so no other caller is affected. This
    # lives HERE and not in config.yaml / w7_noisefade.py because those files
    # are byte-hash-bound by the w9/w10/w9rnn paper observers — editing them
    # freezes their admissions (2026-09-29 incident).
    #
    # v2 (2026-09-30, user): sizes switch by UTC hour block. The 00-05 UTC
    # night band (US evening / Asia morning) ran -1.55c/contract vs +1.81c
    # for the other 18 hours (clustered t -2.28; negative in all 5 weeks,
    # and all four synchronized cross-coin flip events fell inside it), so
    # the night band trades small and the day band carries the size. The
    # 29-day counterfactual beats an equal-average flat allocation +47%
    # with a smaller drawdown; capacity per the 2026-09-29 ladder study.
    PROD_SIZING: dict = {"w7_noisefade": {
        "night_hours_utc": (0, 1, 2, 3, 4, 5),
        # v3 (2026-10-02, user): day sizes 60/40 -> 40/30 after the table-driven
        # entry went live (top1+top2 can both fire in one window).
        # v4 (2026-10-04, user): night 15/10 -> 20/20 (BTC and alts alike).
        "day":   {"KXBTC15M": 40, "default": 30},
        "night": {"KXBTC15M": 20, "default": 20},
        # no-after-dump tier (user directive 2026-09-30, armed directly): a
        # DAY-band NO order is cut to x0.25 when the coin's own index fell
        # more than 50bp over the prior 60 minutes — the "freshly-dumped
        # favourite" cell ran -3.4c/contract and was negative in all three
        # sample weeks (clustered t -1.52, threshold-robust 30..120bp; the
        # mirror cell, yes-after-dump, was the strongest positive at t +2.30).
        # YES orders, the night band, paper and demo are never touched, and a
        # missing/stale index reading fails OPEN to full size.
        "no_dump": {"threshold_bp": -50.0, "mult": 0.25, "lookback_s": 1800},
    }}

    # PROD-ONLY day-band flow gate (user directive 2026-09-30, applied at the
    # user's explicit instruction WITHOUT the pre-registration wait — the rule
    # was validated externally by the user; the in-house books read it as two
    # same-sign but individually insignificant measurements). During the DAY
    # band only, a prod order is SKIPPED when the frozen price_flow_reversal_v1
    # rule fires: last-60s momentum AND observed HL taker flow both against
    # the held side (aligned flow <= -0.5). The night band never consults it,
    # paper and demo are untouched, and the gate is FAIL-OPEN by construction:
    # missing data, thin prints, stale books or ANY exception send the order
    # normally — the gate may only ever skip on positive evidence, so a broken
    # recorder can never silently reduce fills. Disable = enabled False +
    # runner restart. Features/policy are imported from the downside_paper
    # package (the registered implementations), never re-derived here.
    FLOW_GATE: dict = {"w7_noisefade": {"enabled": True}}

    # PROD-ONLY table-driven entry (user directive 2026-10-02, go-live after
    # tests): W7 prod orders follow the 4-hourly scenario tables - for each
    # coin/window the scenario at the moment of each entry bin decides top1
    # (x1.5/x1/x0.5) and top2 (x1/x0.5), latest bin T-5.25. The T-8 call that
    # W7 itself makes is gated here; the other bins come from live_watch/
    # w7_table.py through this same submit, so every prod rule below still
    # applies. A missing/stale table or an unclassifiable scenario falls back
    # to the pre-2026-10-02 T-8 order (x1). Paper W7 and its demo mirror are
    # untouched. Disable = enabled False + runner restart.
    TABLE_LIVE: dict = {"w7_noisefade": {"enabled": True,
                                         # top1 + top2 bought on one coin-window may not exceed
                                         # cap_mult x the band's base size (user 2026-10-02):
                                         # day BTC 60 / others 45, night 22 / 15.
                                         "window_cap_mult": 1.5,
                                         # no single entry above the base size (user 2026-10-05:
                                         # "去掉 1.5 倍" for W7 prod, W11, W13); x0.5 still applies.
                                         "max_size_mult": 1.0}}
    # PROD-ONLY macro-release guard (user directive 2026-10-04, deployed the same day): for `after_s`
    # after a scheduled US macro release (CPI, NFP, PPI, GDP, PCE, retail sales, weekly claims, JOLTS,
    # ISM, consumer confidence, FOMC statement - crypto_common.macro_calendar, refreshed 4-hourly from
    # FRED + the Fed's calendar page) every W7 prod order is cut to `contracts` per coin-window: the
    # first leg takes it, the window cap is the same number so a second leg gets nothing. The paper
    # mirrors (W11/W13) inherit it through this router. A missing, stale or broken calendar fails
    # OPEN (full size) and is flagged in the audit row as macro_guard: stale|error.
    MACRO_GUARD: dict = {"w7_noisefade": {"enabled": True, "after_s": 45 * 60, "contracts": 5}}
    # PROD-ONLY entry band (user 2026-10-02): the paper book and its demo mirror
    # keep MAIN [0.78, 0.98]; real orders need the favourite priced >= 0.79.
    # Checked on the paper price BEFORE the +1c limit buffer.
    # v2 (2026-10-04, user): [0.79, 0.98] -> [0.80, 0.97] for W7 prod and its paper mirrors W11/W13.
    PROD_BAND: dict = {"w7_noisefade": (0.80, 0.97)}

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

    @staticmethod
    def _utc_hour() -> int:
        """Current UTC hour; a seam so tests can pin the sizing band."""
        return datetime.now(timezone.utc).hour

    @staticmethod
    def _tail_rows(path, max_bytes: int) -> list:
        """Parse the last ``max_bytes`` of a jsonl file; [] on any problem."""
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - max_bytes))
                lines = f.read().decode("utf-8", "ignore").split("\n")
        except OSError:
            return []
        if len(lines) > 1 and size > max_bytes:
            lines = lines[1:]                     # drop the torn first line
        out = []
        for ln in lines:
            if ln.strip():
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
        return out

    @classmethod
    def _trend_bp(cls, coin: str, lookback_s: float = 1800.0) -> float | None:
        """ln(index_now / index_lookback_ago) in bp from the live index recorder.

        2026-09-30 v2 (user): lookback 3600 -> 1800s — the 30-minute window
        separated the fresh-dump cell at -6.39c/contract (clustered t -2.14,
        negative in all three weeks) vs -2.97c for 60 minutes, and the
        hour-old-but-stabilised cohort the old window kept firing on was
        harmless (-0.90c). Tail-reads today's file only — the tier is
        day-band-only (06-23 UTC), so the lookback never crosses the UTC
        midnight boundary. None on any gap or failure (fail open, full size).
        """
        try:
            from crypto_trading.crypto_common.config import PRICE_DATA
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            rows = cls._tail_rows(
                PRICE_DATA / "index_proxy" / "live" / coin / f"{day}.jsonl",
                256_000)
            now = datetime.now(timezone.utc).timestamp()
            s_now = s_old = None
            for r in rows:
                ts = r.get("ts")
                if ts is None or r.get("stale"):
                    continue
                if ts <= now - lookback_s:
                    if s_old is None or ts > s_old[0]:
                        s_old = (ts, r["index"])
                if ts <= now and (s_now is None or ts > s_now[0]):
                    s_now = (ts, r["index"])
            if not s_now or not s_old:
                return None
            if now - s_now[0] > 90 or (now - lookback_s) - s_old[0] > 90:
                return None                        # stale anchors: fail open
            import math
            return math.log(s_now[1] / s_old[1]) * 1e4
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[%s] trend60 fail-open: %s", coin, str(e)[:80])
            return None

    def _flow_gate_decision(self, ticker: str, side: str) -> dict:
        """price_flow_reversal_v1 gate for DAY-band prod orders.

        Reuses the registered downside_paper feature/policy code verbatim on
        the live Hyperliquid recorder tails. ALWAYS returns an audit record —
        {"decision": off|night|accept|skip|unclassified|error, ...} — which
        the caller attaches to every order row, so a fail-open send is
        distinguishable from an evaluated accept in the logs (2026-09-30
        audit: 7% of day-band sends were thin-print unclassified and were
        previously indistinguishable). Only an explicit 'skip' blocks.
        """
        cfg = self.FLOW_GATE.get(self.strategy)
        if not cfg or not cfg.get("enabled"):
            return {"decision": "off"}
        sizing = self.PROD_SIZING.get(self.strategy) or {}
        if self._utc_hour() in sizing.get("night_hours_utc", ()):
            return {"decision": "night"}      # night band: gate off (directive)
        try:
            from crypto_trading.crypto_common.config import PRICE_DATA
            from crypto_trading.crypto_strategies.downside_paper.features import (
                compute_features)
            from crypto_trading.crypto_strategies.downside_paper.policy import (
                decide)
            coin = ticker.split("15M", 1)[0][2:]
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            base = PRICE_DATA / "hyperliquid"
            feats = compute_features(
                self._tail_rows(base / "context" / coin / f"{day}.jsonl", 96_000),
                self._tail_rows(base / "book" / coin / f"{day}.jsonl", 1_500_000),
                self._tail_rows(base / "trades" / coin / f"{day}.jsonl", 2_500_000),
                datetime.now(timezone.utc).timestamp())
            verdict = decide({"side": side}, feats, {})
            d = verdict.get("decision")
            if d in ("skip", "accept"):
                return {"decision": d,
                        "aligned_observed_flow_1m":
                            verdict.get("aligned_observed_flow_1m"),
                        "aligned_momentum_1m_bp":
                            verdict.get("aligned_momentum_1m_bp")}
            return {"decision": "unclassified",
                    "reason": str(verdict.get("reason"))[:80],
                    "flow_errors": [str(x)[:60] for x in
                                    (feats.get("flow_errors") or [])[:3]]}
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[%s] flow gate fail-open: %s",
                           self.strategy, str(e)[:120])
            return {"decision": "error", "reason": str(e)[:80]}

    def submit(self, *, ticker: str, side: str, entry_price: float,
               contracts: int, armed: bool = False, size_mult: float | None = None,
               entry_source: str | None = None, table_decision: dict | None = None) -> dict:
        """Submit one PROD events order (IOC taker, V2 single book).

        ``armed=False`` (the default, and the shipped configuration) records a
        full dry-run audit row of exactly what WOULD have been sent — so the
        moment the user arms the gate, nothing else about the pipeline
        changes. ``armed=True`` sends only if EVERY gate is open; a closed
        gate raises LiveOrderRefused rather than silently downgrading.
        """
        self.armed = armed
        sizing = self.PROD_SIZING.get(self.strategy)
        no_dump_audit = None
        if sizing:
            band = ("night" if self._utc_hour() in sizing["night_hours_utc"]
                    else "day")
            table = sizing[band]
            contracts = int(table.get(ticker.split("-", 1)[0],
                                      table["default"]))
            base_contracts = contracts           # the band's base size: the window cap is 1.5x THIS
            nd = sizing.get("no_dump")
            if nd and band == "day" and side == "no":
                lb = float(nd.get("lookback_s", 1800.0))
                tr = self._trend_bp(ticker.split("15M", 1)[0][2:], lb)
                if tr is not None and tr < nd["threshold_bp"]:
                    contracts = max(1, int(round(contracts * nd["mult"])))
                    no_dump_audit = {"trend_bp": round(tr, 1),
                                     "lookback_s": lb, "mult": nd["mult"]}
                else:
                    no_dump_audit = {"trend_bp":
                                     None if tr is None else round(tr, 1),
                                     "lookback_s": lb, "mult": 1.0}
        macro, macro_flag = None, None
        mg = self.MACRO_GUARD.get(self.strategy)
        if mg and mg.get("enabled"):
            try:
                from crypto_trading.crypto_common import macro_calendar
                ev = macro_calendar.active(time.time(), after_s=float(mg.get("after_s") or 2700))
                if ev is not None:
                    macro = {"event": ev["key"], "name": ev.get("name"), "release_utc": ev["t_utc"], "until": ev.get("until"),
                             "contracts": int(mg.get("contracts") or 5)}
                elif macro_calendar.freshness().get("stale"):
                    macro_flag = "stale"
            except Exception as e:                                    # noqa: BLE001
                logger.warning("[%s] macro guard unavailable (fail open): %s", self.strategy, str(e)[:120])
                macro_flag = "error"
        band_lohi = self.PROD_BAND.get(self.strategy)
        if band_lohi and not (band_lohi[0] <= float(entry_price) <= band_lohi[1]):
            logger.info("[%s] PROD-BAND SKIP %s %s @ %.4f (band %s)", self.strategy, ticker,
                        side, float(entry_price), band_lohi)
            return {"status": "skipped_by_prod_band", "strategy": self.strategy, "env": "prod",
                    "ticker": ticker, "side": side, "paper_price_dollars": round(float(entry_price), 4),
                    "prod_band": list(band_lohi), "entry_source": entry_source or "w7_t8"}
        if not sizing:
            base_contracts = contracts
        # Paper costs are depth-weighted walk averages and carry sub-cent
        # precision; prod rejected the first such price on 2026-09-28
        # (DOGE 0.8876 -> wire ask 0.1124 -> HTTP 400 invalid_price, while
        # 0.0960 passed: sub-cent ticks are only legal below $0.10). Round the
        # limit to the nearest whole cent - the IOC still fills at the book's
        # own levels, so the drift is bounded by half a cent and ~unbiased.
        entry_price = int(float(entry_price)*100+0.5)/100.0
        # +1c limit buffer (user directive 2026-09-30): the arrival-gap study
        # showed IOC orders dying because the ask moved ~1c past the second-old
        # paper price while the missed tickets were 98% winners (+5.6c/张), so
        # the limit is lifted ONE cent, capped at 0.99. Kalshi IOC fills at the
        # BOOK price, so the buffer never worsens a fill that was already
        # available at the paper price — it only converts near-miss zeros.
        # EXCLUDED by the user's design: a dump-quartered NO order (chasing
        # there would buy back into the -3.4c/张 fresh-dump cell). Scoped to
        # strategies with a PROD_SIZING entry (w7), never other callers.
        paper_price = entry_price
        buffer_c = 0
        if self.strategy in self.PROD_SIZING and not (
                no_dump_audit and no_dump_audit.get("mult") != 1.0):
            cents = int(round(entry_price * 100))
            if cents < 99:
                buffer_c = 1
                entry_price = (cents + 1) / 100.0
        intent = {"strategy": self.strategy, "env": "prod", "ticker": ticker,
                  "side": side, "price_dollars": round(entry_price, 4),
                  "paper_price_dollars": round(paper_price, 4),
                  "limit_buffer_c": buffer_c,
                  "contracts": contracts, "tif": "immediate_or_cancel"}
        if no_dump_audit is not None:
            intent["no_dump"] = no_dump_audit
        if macro is not None:
            intent["macro"] = macro
        if macro_flag is not None:
            intent["macro_guard"] = macro_flag
        table_cfg = self.TABLE_LIVE.get(self.strategy)
        reserved = 0                             # window-cap contracts reserved on the ledger (see below)
        if table_cfg and table_cfg.get("enabled"):
            if entry_source is None:
                # W7's own T-8 call: ask the table about the T-8.25 bin.
                try:
                    from crypto_trading.crypto_strategies.w7_scenarios import live_plan
                    table_decision = live_plan.decide(
                        ticker.split("15M", 1)[0][2:], live_plan.close_ts_from_ticker(ticker),
                        live_plan.LIVE_BIN, side, ticker=ticker)
                except Exception as e:                            # noqa: BLE001
                    table_decision = {"action": "fallback", "reason": f"error: {str(e)[:100]}"}
                if table_decision["action"] == "skip":
                    logger.info("[%s] TABLE SKIP %s %s at T-8.25 (%s)", self.strategy, ticker,
                                side, table_decision.get("scenario"))
                    return {"status": "skipped_by_table", **intent, "entry_source": "w7_t8",
                            "table": _trim_table(table_decision)}
                if table_decision["action"] == "trade":
                    size_mult = table_decision["mult"]
                    entry_source = f"table_{table_decision['slot']}_T-8.25"
                else:
                    entry_source = "t8_fallback"
            max_mult = float(table_cfg.get("max_size_mult") or 1.0)
            if size_mult is not None and float(size_mult) > max_mult:
                intent["size_mult_table"] = float(size_mult)              # audit: what the table asked for
                size_mult = max_mult
            if size_mult is not None and size_mult != 1.0:
                contracts = max(1, int(contracts * float(size_mult) + 0.5))   # same rounding as the backtest
                intent["contracts"] = contracts
            intent["entry_source"] = entry_source
            intent["size_mult"] = 1.0 if size_mult is None else float(size_mult)
            if macro is not None and contracts > macro["contracts"]:
                logger.info("[%s] MACRO GUARD %s %s: %d -> %d contracts (%s until %s)", self.strategy, ticker, side,
                            contracts, macro["contracts"], macro["event"], macro.get("until"))
                contracts = macro["contracts"]
                intent["contracts"] = contracts
            if table_decision is not None:
                intent["table"] = _trim_table(table_decision)
            cap_mult = float(table_cfg.get("window_cap_mult") or 0)
            if cap_mult > 0:
                try:
                    from crypto_trading.crypto_common.kalshi.rest_event import (
                        KalshiEventOrderClient)  # noqa: F401  (import failure = no send below either)
                    from crypto_trading.crypto_strategies.w7_scenarios import live_plan
                    cap = int(base_contracts * cap_mult + 1e-9)
                    if macro is not None:
                        cap = min(cap, macro["contracts"])
                    # Reserve atomically: two orders on the same ticker (top1 + top2,
                    # or threads racing) can never both pass a check-then-send. The
                    # reservation is replaced by the real fill after the send and
                    # released on every path that does not send.
                    granted, already = live_plan.reserve(ticker, contracts, cap)
                    intent["window_cap"] = {"cap": cap, "already": already}
                    if granted < 1:
                        logger.info("[%s] WINDOW-CAP SKIP %s %s: %.0f/%d already bought",
                                    self.strategy, ticker, side, already, cap)
                        return {"status": "skipped_by_window_cap", **intent}
                    if granted < contracts:
                        intent["window_cap"]["trimmed_from"] = contracts
                        contracts = granted
                        intent["contracts"] = contracts
                    reserved = granted
                except Exception as e:                            # noqa: BLE001
                    # the ledger is a safety net; a broken ledger must not stop the
                    # base-size order, so fall back to the base size at most
                    logger.warning("[%s] window cap unavailable (%s); capping at base size",
                                   self.strategy, str(e)[:100])
                    contracts = min(contracts, base_contracts)
                    intent["contracts"] = contracts
                    intent.pop("window_cap", None)
                    reserved = 0

        if macro is not None and not (table_cfg and table_cfg.get("enabled")) and contracts > macro["contracts"]:
            contracts = macro["contracts"]
            intent["contracts"] = contracts

        def _release() -> None:
            # give back a reservation on any path that does not send the order
            if reserved:
                try:
                    from crypto_trading.crypto_common.kalshi.rest_event import (
                        KalshiEventOrderClient)  # noqa: F401
                    from crypto_trading.crypto_strategies.w7_scenarios import live_plan
                    live_plan.settle(ticker, reserved, 0.0)
                except Exception as e:                            # noqa: BLE001
                    logger.warning("[%s] reservation release failed: %s", self.strategy, str(e)[:100])

        gate = self._flow_gate_decision(ticker, side)
        intent["flow_gate"] = gate
        if gate.get("decision") == "skip":
            _release()
            logger.info("[%s] FLOW-GATE SKIP %s %s x%d @ %.4f",
                        self.strategy, ticker, side, contracts, entry_price)
            skipped = {"status": "skipped_by_flow_gate", **intent,
                       "aligned_observed_flow_1m": gate.get("aligned_observed_flow_1m"),
                       "aligned_momentum_1m_bp": gate.get("aligned_momentum_1m_bp")}
            if armed:
                # The same skip applies to every mirrored user account; journal it
                # privately, only for users actually trading (allowlisted).
                self._journal_user_skips(skipped)
            return skipped
        if not armed:
            _release()
            logger.info("[%s] LIVE-DISARMED %s %s x%d @ %.4f (audit only)",
                        self.strategy, ticker, side, contracts, entry_price)
            return {"status": "live_disarmed", **intent}
        gate = self.gate_status()
        if not gate["live_open"]:
            _release()
            raise LiveOrderRefused(f"live events order refused — gate: {gate}")
        if (table_decision or {}).get("action") == "trade" and table_decision.get("slot"):
            # One top1 and one top2 per coin-window: claimed only once every gate
            # has passed and the order is about to go out (the backtest marks a
            # slot used only when it trades; an audit-only or refused order must
            # not consume it).
            try:
                from crypto_trading.crypto_strategies.w7_scenarios import live_plan
                if not live_plan.claim_slot(ticker, table_decision["slot"]):
                    _release()
                    return {"status": "skipped_by_table", **intent,
                            "slot_reason": f"{table_decision['slot']}_already_used_this_window"}
            except Exception as e:                                # noqa: BLE001
                logger.warning("[%s] slot claim failed (sending anyway): %s",
                               self.strategy, str(e)[:120])
        # From here a raised exception keeps the reservation: the order may have
        # reached the exchange, and over-counting exposure only ever buys less.
        r = self._send(ticker, side, int(contracts), float(entry_price))
        logger.warning("[%s] %s %s %s x%d @ %.4f -> HTTP %s",
                       self.strategy, "PAPER ORDER SIMULATED" if r.get("paper") else "LIVE ORDER SENT",
                       ticker, side, contracts, entry_price, r.get("status_code"))
        out = {"status": "live_sent", **intent, **r}
        if "window_cap" in intent:
            try:
                from crypto_trading.crypto_strategies.w7_scenarios import live_plan
                code = int(r.get("status_code") or 0)
                bought = 0.0 if 400 <= code < 500 else _fill_count(r, default=contracts)   # rejected: nothing bought
                out["window_cap"] = {**intent["window_cap"], "bought": bought,
                                     "total": live_plan.settle(ticker, reserved, bought)}
            except Exception as e:                                # noqa: BLE001
                logger.warning("[%s] exposure record failed: %s", self.strategy, str(e)[:100])
        # Per-user PROD accounts (2026-10-01): the IDENTICAL intent (same
        # ticker / side / size / limit, every rule already applied above) goes
        # to each allowlisted user account ONLY AFTER the owner's order has
        # returned - the owner's fill always has priority on the book. It runs
        # in its own daemon thread so the owner's row is returned (and logged)
        # immediately and NEVER carries user data; user results go to a
        # private journal outside the repo (~/.kalshi/journal).
        self._spawn_user_mirror(intent)
        return out

    def _send(self, ticker: str, side: str, contracts: int, price_dollars: float) -> dict:
        """The one venue call of submit(): a PROD IOC order. Every rule above it is
        shared with the paper mirror (w11_prod_mirror), which overrides only this."""
        from crypto_trading.crypto_common.kalshi.rest_event import (
            KalshiEventOrderClient)
        return KalshiEventOrderClient(env="prod").create_order(
            ticker=ticker, side=side, count=int(contracts),
            price_dollars=float(price_dollars), tif="immediate_or_cancel")

    # ── per-user PROD mirror (2026-10-01) ─────────────────────────────────
    _JOURNAL_LOCK = __import__("threading").Lock()

    @classmethod
    def _journal_user_rows(cls, rows: list[dict]) -> None:
        """Append rows to ~/.kalshi/journal/<UTC day>.jsonl (dir 0700, file 0600)."""
        if not rows:
            return
        import os
        from crypto_trading.crypto_common.config import USER_KEY_DIR
        d = USER_KEY_DIR / "journal"
        with cls._JOURNAL_LOCK:
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
            path = d / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as fh:
                for r in rows:
                    fh.write(json.dumps(r, default=str) + "\n")

    def _user_row(self, intent: dict, account: str, **extra) -> dict:
        return {"ts": datetime.now(timezone.utc).isoformat(), "strategy": self.strategy,
                "action": "live_order_result", "account": account,
                **{k: v for k, v in intent.items() if k != "status"}, **extra}

    def _journal_user_skips(self, skipped: dict) -> None:
        try:
            from crypto_trading.crypto_common.config import kalshi_user_accounts, trading_user_ids
            if self.strategy not in self.PROD_SIZING or not trading_user_ids():
                return
            self._journal_user_rows([self._user_row(skipped, a.user_id,
                                                    status="skipped_by_flow_gate")
                                     for a in kalshi_user_accounts()])
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[%s] user skip journal failed: %s", self.strategy, str(e)[:120])

    def _spawn_user_mirror(self, intent: dict) -> None:
        try:
            from crypto_trading.crypto_common.config import trading_user_ids
            if self.strategy not in self.PROD_SIZING or not trading_user_ids():
                return                       # nobody allowlisted: no thread, no I/O
            import threading
            threading.Thread(target=self._mirror_user_accounts, args=(dict(intent),),
                             name="w7-user-mirror", daemon=True).start()
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[%s] user mirror spawn failed: %s", self.strategy, str(e)[:120])

    def _mirror_user_accounts(self, intent: dict) -> list[dict]:
        """Send the owner's intent to every allowlisted user account, in parallel.

        Never raises. Same ticker / side / limit as the owner; the size is the
        owner's contracts x that user's owner-approved ratio, rounded DOWN and
        never above the owner's (standard enable flow, 2026-10-04); below one
        contract nothing is sent and a `skipped_below_one_contract` row is
        journaled. Each account gets a `pending_send` journal row BEFORE the
        request and a result row after, both keyed by mirror_id, so a runner
        restart mid-request leaves a visible unresolved order rather than a
        silent gap. A 401/403 disables only that account.
        """
        try:
            from crypto_trading.crypto_common.config import (
                disable_user_account, kalshi_user_accounts)
            accounts = kalshi_user_accounts()
            if not accounts:
                return []
            import uuid
            from concurrent.futures import ThreadPoolExecutor
            from crypto_trading.crypto_common.kalshi.rest_event import (
                KalshiEventOrderClient)
            ticker, side = intent["ticker"], intent["side"]
            contracts, price = int(intent["contracts"]), float(intent["price_dollars"])

            def one(acct):
                ratio = float(getattr(acct, "ratio", 1.0))
                n = min(contracts, int(math.floor(contracts * ratio + 1e-9)))
                size = {"contracts": n, "owner_contracts": contracts, "ratio": ratio}
                if n < 1:
                    row = self._user_row(intent, acct.user_id,
                                         status="skipped_below_one_contract", **size)
                    self._journal_user_rows([row])
                    return row
                mid = str(uuid.uuid4())
                pending = self._user_row(intent, acct.user_id, status="pending_send",
                                         mirror_id=mid, **size)
                self._journal_user_rows([pending])
                extra = {}
                try:
                    rr = KalshiEventOrderClient(env="prod", key=acct.key).create_order(
                        ticker=ticker, side=side, count=n,
                        price_dollars=price, tif="immediate_or_cancel")
                    extra.update(rr)
                    if rr.get("status_code") in (401, 403):
                        disable_user_account(acct.user_id, f"HTTP {rr.get('status_code')} on order")
                        extra["disabled"] = True
                except Exception as e:                            # noqa: BLE001
                    extra["error"] = f"{type(e).__name__}: {str(e)[:160]}"
                # keep the SEND time (the order's real time), not the response time
                extra.update(size, ts=pending["ts"])
                row = self._user_row(intent, acct.user_id, status="live_sent",
                                     mirror_id=mid, **extra)
                self._journal_user_rows([row])
                return row
            with ThreadPoolExecutor(max_workers=min(8, len(accounts))) as pool:
                results = list(pool.map(one, accounts))
            logger.warning("[%s] USER ORDERS %s %s owner x%d -> %s", self.strategy, ticker, side,
                           contracts, [(r.get("contracts"), r.get("status_code", r.get("error") or r.get("status")))
                                       for r in results])
            return results
        except Exception as e:                                    # noqa: BLE001
            logger.warning("[%s] user mirror failed: %s", self.strategy, str(e)[:120])
            return []

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
