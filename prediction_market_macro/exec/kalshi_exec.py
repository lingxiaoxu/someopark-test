"""exec/kalshi_exec.py — Kalshi order client, demo-first (PLAN §13).

Three-layer switch, ALL must be green before a single real order leaves this module:
  1. settings.trading_enabled (config default False) AND env KALSHI_TRADING_ENABLED=1
  2. per-series gate: research/eval must have flipped the series to 'real'
     (experiments row name='series_gate', metrics_json {"real": true}) — no gate row,
     no trading, whatever the env says
  3. circuit breaker clear: no alerts(source='circuit_breaker') in the last 7 days

Anything short of that → place_order() returns a REFUSED record and the decision stays
paper (ledger paper fills, the default production mode today). Auth reuses the same
READ-ONLY imported RSA-PSS helpers as venues/kalshi/account.py; KALSHI_ENV selects
demo (default) vs prod exactly like the WC system.

Order type: limit only (post_only maker where possible — fee plan §11); crossing the
book is a deliberate taker choice by the caller, never a default.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from prediction_market_macro.venues.kalshi.account import DEMO_BASE, PROD_BASE, _auth_headers


@dataclass(frozen=True)
class OrderResult:
    status: str            # "refused" | "accepted" | "error"
    reason: str
    order_id: str | None = None
    raw: dict | None = None


def _base() -> str:
    return PROD_BASE if (os.environ.get("KALSHI_ENV") or "demo").lower() == "prod" \
        else DEMO_BASE


def trading_allowed(conn, settings, series: str) -> tuple[bool, str]:
    if not getattr(settings, "trading_enabled", False):
        return False, "settings.trading_enabled=False"
    if os.environ.get("KALSHI_TRADING_ENABLED") != "1":
        return False, "env KALSHI_TRADING_ENABLED!=1"
    row = conn.execute(
        "SELECT metrics_json FROM experiments WHERE name='series_gate' AND series=?"
        " ORDER BY created_ts DESC LIMIT 1", (series,)).fetchone()
    if row is None or not json.loads(row["metrics_json"] or "{}").get("real"):
        return False, f"series_gate not passed for {series}"
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    cb = conn.execute(
        "SELECT COUNT(*) c FROM alerts WHERE source='circuit_breaker' AND ts>=?",
        (week_ago,)).fetchone()
    if cb["c"] > 0:
        return False, "circuit_breaker tripped within 7d"
    return True, "all_gates_green"


def place_limit_order(conn, settings, *, ticker: str, side: str, action: str,
                      count: int, price_cents: int, post_only: bool = True,
                      client_order_id: str | None = None) -> OrderResult:
    """side: 'yes'|'no'; action: 'buy'|'sell'; price in CENTS (1-99). Refuses unless
    every gate is green. Demo account unless KALSHI_ENV=prod."""
    series = ticker.split("-", 1)[0]
    ok, why = trading_allowed(conn, settings, series)
    if not ok:
        conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                     (datetime.now(timezone.utc).isoformat(), "info", "exec",
                      f"order REFUSED {ticker} {action} {side} x{count}@{price_cents}c: {why}"))
        conn.commit()
        return OrderResult("refused", why)
    assert side in ("yes", "no") and action in ("buy", "sell")
    assert 1 <= price_cents <= 99 and count >= 1
    body = {"ticker": ticker, "side": side, "action": action, "count": count,
            "type": "limit",
            ("yes_price" if side == "yes" else "no_price"): price_cents,
            "post_only": post_only,
            "client_order_id": client_order_id or
            f"spm-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"}
    path = "/trade-api/v2/portfolio/orders"
    req = urllib.request.Request(
        _base() + "/portfolio/orders", data=json.dumps(body).encode(),
        headers={"User-Agent": "someopark-macro", "Content-Type": "application/json",
                 **_auth_headers("POST", path)}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            doc = json.load(r)
        oid = (doc.get("order") or {}).get("order_id")
        conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                     (datetime.now(timezone.utc).isoformat(), "info", "exec",
                      f"order ACCEPTED {ticker} {action} {side} x{count}@{price_cents}c"
                      f" id={oid}"))
        conn.commit()
        return OrderResult("accepted", "ok", order_id=oid, raw=doc)
    except Exception as e:                                       # noqa: BLE001
        conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                     (datetime.now(timezone.utc).isoformat(), "error", "exec",
                      f"order ERROR {ticker}: {e}"))
        conn.commit()
        return OrderResult("error", str(e))


def cancel_order(conn, order_id: str) -> OrderResult:
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    req = urllib.request.Request(
        _base() + f"/portfolio/orders/{order_id}",
        headers={"User-Agent": "someopark-macro", **_auth_headers("DELETE", path)},
        method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            doc = json.load(r)
        return OrderResult("accepted", "cancelled", order_id=order_id, raw=doc)
    except Exception as e:                                       # noqa: BLE001
        return OrderResult("error", str(e))
