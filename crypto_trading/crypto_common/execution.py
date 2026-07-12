"""Idempotent order router with the DEMO-FIRST HARD GATE (Plan 00 §3.8, §5).

THE GATE (all must hold before a single live order leaves this process):
  1. ``KALSHI_ENV == "prod"``            — explicit operator env flip
  2. ``ALLOW_LIVE_ORDERS == 1``          — explicit operator flag
  3. ``/margin/enabled`` returns true    — account-level margin opt-in done
  4. the key is DEDICATED (not the borrowed prediction_market key)
Anything else ⇒ orders are DRY-RUN: fully constructed, logged to
    trading_signals/orders_dryrun/<strategy>/<date>.jsonl
and NOT sent. There is deliberately NO override path in code.

Wire notes: margin order endpoints mirror the event API under /margin/*
(order body: decimal-dollar `price`, string `count`, `side` buy/sell,
`client_order_id` idempotency). Exact paths carry VERIFY tags — the account
isn't margin-enabled yet, so they are unexercised; verify against
docs.kalshi.com/margin on first enabled use. Sequential orders only (no batch
on margin — Plan 00 §3.4).
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

# VERIFY on first margin-enabled use (mirror of event /portfolio/events/orders)
ORDERS_PATH = "/margin/orders"
ORDER_PATH = "/margin/orders/{order_id}"


class LiveOrderRefused(RuntimeError):
    """Raised when a live send is requested but the hard gate is not fully open."""


@dataclass(frozen=True)
class Order:
    ticker: str
    side: str                    # "buy" | "sell"
    count: int                   # integer contracts (fractional trading off)
    price: float                 # decimal dollars, ≤4 dp
    tif: str = "good_till_canceled"
    post_only: bool = False
    reduce_only: bool = False
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def body(self) -> dict:
        b = {"ticker": self.ticker, "side": self.side, "count": str(int(self.count)),
             "price": f"{self.price:.4f}", "time_in_force": self.tif,
             "client_order_id": self.client_order_id,
             "self_trade_prevention_type": "taker_at_cross"}
        if self.post_only:
            b["post_only"] = True
        if self.reduce_only:
            b["reduce_only"] = True
        return b


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
            logger.info("[%s] DRY-RUN %s %d %s @ %.4f", self.strategy, order.side,
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
    def reconcile(self, inventory_positions: dict[str, float]) -> dict:
        """Compare inventory vs venue positions. Returns {'breaks': {...}}.

        Positions come via the margin client (authed read). While the account
        is not margin-enabled this returns all-breaks=unknown gracefully.
        """
        try:
            bal = self._margin.balance() if self._margin else None
        except Exception as e:
            return {"ok": False, "error": f"positions unavailable: {str(e)[:120]}",
                    "breaks": None}
        venue = {}   # VERIFY: extract positions once /margin/balance|positions is enabled
        breaks = {t: {"inventory": c, "venue": venue.get(t)}
                  for t, c in inventory_positions.items()
                  if venue.get(t) != c}
        return {"ok": not breaks, "breaks": breaks, "venue_raw": bal}

    def close(self) -> None:
        self._dry_writer.close()
