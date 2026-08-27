"""Idempotent order router with the DEMO-FIRST HARD GATE (Plan 00 §3.8, §5).

THE GATE (all must hold before a single live order leaves this process):
  1. ``KALSHI_ENV == "prod"``            — explicit operator env flip
  2. ``ALLOW_LIVE_ORDERS == 1``          — explicit operator flag
  3. ``/margin/enabled`` returns true    — account-level margin opt-in done
  4. the key is DEDICATED (not the borrowed prediction_market key)
Anything else ⇒ orders are DRY-RUN: fully constructed, logged to
    trading_signals/orders_dryrun/<strategy>/<date>.jsonl
and NOT sent. There is deliberately NO override path in code.

Wire format VERIFIED against docs.kalshi.com/margin-rest/orders/create-order +
a real prod fill (2026-07-12) — the order body matches the official spec exactly:
  POST /trade-api/v2/margin/orders
  side  = "bid" (long) | "ask" (short)          NOT buy/sell (Kalshi uses bid/ask;
          the real short fill returned side="ask")
  count = fixed-point decimal string, 2 dp, min granularity 0.01 ("1.00")
  price = fixed-point dollars string, up to 6 dp (tick_size governs; "6.4015")
  time_in_force = fill_or_kill | good_till_canceled | immediate_or_cancel
  self_trade_prevention_type = taker_at_cross | maker
  subaccount = integer (default 0) — MUST match where the margin balance lives
               (the real account traded on subaccount 64, not 0)
Cancel: DELETE /trade-api/v2/margin/orders/{order_id}. Success = HTTP 201.
Sequential orders only (no batch on margin — Plan 00 §3.4).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field

import requests

from crypto_trading.crypto_common.config import (SIGNALS_DIR, allow_live_orders,
                                                 kalshi_env, kalshi_key)
from crypto_trading.crypto_common.io_jsonl import DailyJsonlWriter
from crypto_trading.crypto_common.kalshi.auth import auth_headers, load_private_key
from crypto_trading.crypto_common.kalshi.enums import API_ROOT, rest_base
from crypto_trading.crypto_common.kalshi.ratelimit import KalshiRateLimiter

logger = logging.getLogger(__name__)

# Verified against docs.kalshi.com/margin-rest/orders (2026-07-12):
# POST /margin/orders (create, 201), DELETE /margin/orders/{order_id} (cancel).
ORDERS_PATH = "/margin/orders"
ORDER_PATH = "/margin/orders/{order_id}"


class LiveOrderRefused(RuntimeError):
    """Raised when a live send is requested but the hard gate is not fully open."""


_SIDES = ("bid", "ask")          # Kalshi: bid = long, ask = short (verified via fill)
_TIFS = ("fill_or_kill", "good_till_canceled", "immediate_or_cancel")
_STPS = ("taker_at_cross", "maker")


@dataclass(frozen=True)
class Order:
    ticker: str
    side: str                    # "bid" (long) | "ask" (short) — Kalshi enum, NOT buy/sell
    count: float                 # contracts; fixed-point 2 dp on the wire, min/step 0.01
    price: float                 # decimal dollars (up to 6 dp; snapped to tick_size if given)
    tif: str = "good_till_canceled"
    post_only: bool = False
    reduce_only: bool = False
    subaccount: int = 0          # MUST match the trading subaccount (real acct = 64)
    stp: str = "taker_at_cross"  # taker_at_cross | maker
    tick_size: float | None = None   # if set, price is snapped to a multiple before sending
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self):
        if self.side not in _SIDES:
            raise ValueError(f"side must be 'bid'(long) or 'ask'(short), got {self.side!r}")
        if self.tif not in _TIFS:
            raise ValueError(f"time_in_force must be one of {_TIFS}, got {self.tif!r}")
        if self.stp not in _STPS:
            raise ValueError(f"self_trade_prevention_type must be one of {_STPS}")
        if self.count < 0.01:
            raise ValueError(f"count {self.count} below min granularity 0.01")
        # count must be a whole multiple of 0.01 (silent rounding is a quantity mismatch)
        if abs(round(self.count / 0.01) * 0.01 - self.count) > 1e-9:
            raise ValueError(f"count {self.count} must be a multiple of 0.01")
        if self.reduce_only and self.tif == "good_till_canceled":
            # official: reduce_only is rejected unless IOC/FOK
            raise ValueError("reduce_only requires IOC or FOK time_in_force")

    @classmethod
    def from_signed(cls, ticker: str, signed_contracts: float, price: float, **kw):
        """Convenience: +contracts → long (bid), −contracts → short (ask)."""
        side = "bid" if signed_contracts > 0 else "ask"
        return cls(ticker, side, abs(signed_contracts), price, **kw)

    def wire_price(self) -> float:
        """Price snapped to tick_size (if known). Kalshi rejects off-tick prices.

        Snaps CONSERVATIVELY: a bid rounds DOWN (never pay more than intended), an
        ask rounds UP (never sell cheaper than intended)."""
        if not self.tick_size or self.tick_size <= 0:
            return self.price
        import math
        n = self.price / self.tick_size
        snapped = (math.floor(n) if self.side == "bid" else math.ceil(n)) * self.tick_size
        return round(snapped, 6)

    def _price_decimals(self) -> int:
        """Decimals to format price at: from tick_size (up to 6), else 4."""
        if not self.tick_size or self.tick_size <= 0:
            return 4
        s = f"{self.tick_size:.6f}".rstrip("0")
        return min(6, max(4, len(s.split(".")[1]) if "." in s else 0))

    def to_demo(self) -> "Order":
        """Demo-environment twin: demo perp tickers carry a ``1`` suffix
        (KXBTCPERP → KXBTCPERP1, probed 2026-08-23) and the demo account has a
        single subaccount 0 (prod trades on 64). A fresh client_order_id keeps
        the mirror out of the prod idempotency set."""
        import dataclasses
        import uuid as _uuid
        tkr = self.ticker if self.ticker.endswith("1") else self.ticker + "1"
        return dataclasses.replace(self, ticker=tkr, subaccount=0,
                                   client_order_id="demo-" + str(_uuid.uuid4()))

    def body(self) -> dict:
        # format to the tick's precision (up to 6dp) so tick-snapping isn't
        # truncated away for perps with a finer tick than 0.0001
        b = {"ticker": self.ticker, "side": self.side,
             "count": f"{self.count:.2f}",              # "1.00", 2-dp fixed point
             "price": f"{self.wire_price():.{self._price_decimals()}f}",
             "time_in_force": self.tif,
             "client_order_id": self.client_order_id,
             "self_trade_prevention_type": self.stp,
             "subaccount": int(self.subaccount)}
        if self.post_only:
            b["post_only"] = True
        if self.reduce_only:
            b["reduce_only"] = True
        return b


    # ── demo mirror plumbing ───────────────────────────────────────────────

DEMO_GTC_EXPIRE_S = 900          # resting mirror orders self-destruct in 15min


def demo_order_body(order: "Order", *, now_ts: float,
                    expire_s: int = DEMO_GTC_EXPIRE_S) -> tuple[dict, "Order"]:
    """Wire body for the demo twin of ``order``.

    GTC orders get an ``expiration_ts``: the paper loop cancels its pending
    entries implicitly (fill-verify timeout), but a mirrored GTC order on the
    demo venue would rest FOREVER — zombie orders accumulating margin. The
    self-destruct keeps demo lifecycle in sync with the paper loop without
    the mirror thread having to track order ids (fire-and-forget stays true).
    IOC/FOK orders need nothing — they die at the matching engine.
    """
    d = order.to_demo()
    b = d.body()
    if d.tif == "good_till_canceled":
        b["expiration_ts"] = int(now_ts + expire_s)
    return b, d


class ExecutionRouter:
    def __init__(self, strategy: str, *, env: str | None = None,
                 margin_client=None):
        self.strategy = strategy
        self.env = env or kalshi_env()
        self._margin = margin_client        # KalshiMarginClient, for /margin/enabled
        self.limiter = KalshiRateLimiter()
        self._dry_writer = DailyJsonlWriter(SIGNALS_DIR / "orders_dryrun" / strategy)
        self._sent_ids: set[str] = set()    # idempotency dedup (per process)
        self._s = requests.Session()
        self._s.headers["User-Agent"] = "someopark-crypto/0.1"
        self._key = None
        self._demo_key = None
        self._demo_pk = None
        self._pk = None

    # ── the gate ───────────────────────────────────────────────────────────
    def gate_status(self) -> dict:
        """Evaluate every gate condition; never raises."""
        status = {"env_prod": self.env == "prod",
                  "allow_live_orders": allow_live_orders(),
                  "dedicated_key": False, "margin_enabled": False}
        try:
            key = kalshi_key("margin", borrowed_ok=True)
            status["dedicated_key"] = not key.borrowed
        except RuntimeError:
            pass
        if status["env_prod"] and status["dedicated_key"] and self._margin is not None:
            try:
                status["margin_enabled"] = bool(self._margin.enabled())
            except Exception as e:
                status["margin_enabled_error"] = str(e)[:120]
        status["live_open"] = all(status.get(k) for k in
                                  ("env_prod", "allow_live_orders",
                                   "dedicated_key", "margin_enabled"))
        return status

    # ── order flow ─────────────────────────────────────────────────────────
    def submit(self, order: Order, *, live: bool = False) -> dict:
        """Submit one order. ``live=False`` (default) always dry-runs.

        ``live=True`` only sends if the ENTIRE gate is open; otherwise raises
        LiveOrderRefused (caller must not silently downgrade a live intent).
        """
        if order.client_order_id in self._sent_ids:
            return {"status": "duplicate", "client_order_id": order.client_order_id}
        record = {"ts": time.time(), "strategy": self.strategy, "env": self.env,
                  "order": asdict(order)}
        if not live:
            record["mode"] = "dry_run"
            self._dry_writer.write(".", record)
            self._sent_ids.add(order.client_order_id)
            logger.info("[%s] DRY-RUN %s %.2f %s @ %.4f", self.strategy, order.side,
                        order.count, order.ticker, order.price)
            return {"status": "dry_run", **record}

        gate = self.gate_status()
        if not gate["live_open"]:
            raise LiveOrderRefused(f"live order refused — gate: {gate}")

        if self._key is None:
            self._key = kalshi_key("margin", borrowed_ok=False)   # dedicated only
            self._pk = load_private_key(self._key.expanded_path())
        self.limiter.acquire_write()
        path = ORDERS_PATH
        headers = auth_headers(self._pk, self._key.key_id, "POST", f"{API_ROOT}{path}")
        r = self._s.post(rest_base(self.env) + path, json=order.body(),
                         headers=headers, timeout=20)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"create_order failed {r.status_code}: {r.text[:300]}")
        self._sent_ids.add(order.client_order_id)
        record["mode"] = "live"
        record["response"] = r.json()
        self._dry_writer.write(".", record)      # live orders logged too
        return record

    def submit_demo(self, order: Order) -> dict:
        """Mirror an order into the DEMO environment (parallel link rehearsal).

        Runs OUTSIDE the prod gate on purpose: demo is the sandbox the gate
        exists to protect us into. Its own safety instead: the URL is asserted
        to be the demo host, the borrowed demo key is allowed, and the order is
        translated via Order.to_demo() (ticker suffix + subaccount 0). Never
        raises into the caller's paper loop — the mirror must not be able to
        break the probe.
        """
        try:
            base = rest_base("demo")
            if "demo" not in base:
                return {"status": "refused", "error": f"not a demo base: {base}"}
            body, d = demo_order_body(order, now_ts=time.time())
            if self._demo_key is None:
                self._demo_key = kalshi_key("margin", borrowed_ok=True)
                self._demo_pk = load_private_key(self._demo_key.expanded_path())
            self.limiter.acquire_write()
            headers = auth_headers(self._demo_pk, self._demo_key.key_id,
                                   "POST", f"{API_ROOT}{ORDERS_PATH}")
            r = self._s.post(base + ORDERS_PATH, json=body,
                             headers=headers, timeout=20)
            rec = {"ts": time.time(), "mode": "demo_mirror",
                   "strategy": self.strategy, "order": asdict(d),
                   "status_code": r.status_code, "response": r.text[:400]}
            self._dry_writer.write(".", rec)
            logger.info("[%s] DEMO-MIRROR %s %s %.2f @ %.4f → %s",
                        self.strategy, d.ticker, d.side, d.count, d.price,
                        r.status_code)
            return rec
        except Exception as e:                              # noqa: BLE001
            logger.warning("[%s] demo mirror failed: %s", self.strategy, e)
            return {"status": "error", "error": str(e)[:200]}

    def cancel_demo(self, order_id: str) -> dict:
        """Cancel a demo-mirror order (no prod gates — demo host asserted)."""
        try:
            base = rest_base("demo")
            if "demo" not in base:
                return {"status": "refused", "error": f"not a demo base: {base}"}
            if self._demo_key is None:
                self._demo_key = kalshi_key("margin", borrowed_ok=True)
                self._demo_pk = load_private_key(self._demo_key.expanded_path())
            path = ORDER_PATH.format(order_id=order_id)
            headers = auth_headers(self._demo_pk, self._demo_key.key_id,
                                   "DELETE", f"{API_ROOT}{path}")
            self.limiter.acquire_cancel()
            r = self._s.delete(base + path, headers=headers, timeout=15)
            return {"status_code": r.status_code, "response": r.text[:200]}
        except Exception as e:                              # noqa: BLE001
            return {"status": "error", "error": str(e)[:200]}

    def cancel(self, order_id: str, *, live: bool = False) -> dict:
        if not live:
            rec = {"ts": time.time(), "mode": "dry_run", "cancel": order_id}
            self._dry_writer.write(".", rec)
            return rec
        gate = self.gate_status()
        if not gate["live_open"]:
            raise LiveOrderRefused(f"live cancel refused — gate: {gate}")
        path = ORDER_PATH.format(order_id=order_id)
        headers = auth_headers(self._pk, self._key.key_id, "DELETE", f"{API_ROOT}{path}")
        self.limiter.acquire_cancel()
        r = self._s.delete(rest_base(self.env) + path, headers=headers, timeout=20)
        if r.status_code not in (200, 204):
            raise RuntimeError(f"cancel failed {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {"order_id": order_id, "status": "canceled"}

    # ── reconciliation (Plan 00 §5: desync → halt+alert) ──────────────────
    def reconcile(self, inventory_positions: dict[str, float], *,
                  subaccount: int | None = None, tol: float = 1e-9) -> dict:
        """Compare our inventory vs the venue's actual positions.

        Uses the verified /margin/positions schema (market_ticker + signed
        decimal 'position', '-1.00' = short 1) — and is **subaccount-aware**: the
        real account holds the same ticker across subaccounts (e.g. #64 and #0),
        so positions are NETTED per ticker across the relevant subaccounts, not
        last-wins. Pass ``subaccount`` to restrict to one; None = net all.
        """
        if self._margin is None:
            return {"ok": False, "error": "no margin client", "breaks": None}
        try:
            raw = self._margin.positions()
        except Exception as e:
            return {"ok": False, "error": f"positions unavailable: {str(e)[:120]}",
                    "breaks": None}
        venue: dict[str, float] = {}
        for p in raw:
            t = p.get("market_ticker")
            if t is None:
                continue
            if subaccount is not None and int(p.get("subaccount", 0)) != subaccount:
                continue
            venue[t] = venue.get(t, 0.0) + float(p["position"])   # NET, don't overwrite
        tickers = set(inventory_positions) | set(venue)
        breaks = {t: {"inventory": inventory_positions.get(t, 0.0),
                      "venue": venue.get(t, 0.0)}
                  for t in tickers
                  if abs(inventory_positions.get(t, 0.0) - venue.get(t, 0.0)) > tol}
        return {"ok": not breaks, "breaks": breaks, "venue_positions": venue}

    def close(self) -> None:
        self._dry_writer.close()
