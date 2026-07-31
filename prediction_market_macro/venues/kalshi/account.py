"""venues/kalshi/account.py — authenticated demo-account access (balance as bankroll).

The paper bankroll is NOT a made-up number: it is read live from the Kalshi DEMO
account (the user's paper-money account), via the same RSA-PSS signing the WC system
uses in production. The signing helpers are imported READ-ONLY from the mother template
(the one sanctioned reference module, PLAN §15); demo vs prod selection follows
KALSHI_ENV exactly like the WC system.

Fallback discipline: if the balance fetch fails, use the LAST KNOWN balance from the db;
only if none has ever been stored does the static seed (=1000, flagged "seed") apply.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone

DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"


def _auth_headers(method: str, path: str) -> dict:
    import os
    from prediction_market.venues.kalshi.auth import auth_headers, load_private_key
    env = (os.environ.get("KALSHI_ENV") or "demo").lower()
    if env == "prod":
        key_id = os.environ.get("KALSHI_PROD_API_KEY_ID")
        key_path = os.environ.get("KALSHI_PROD_PRIVATE_KEY_PATH")
    else:
        key_id = os.environ.get("KALSHI_API_KEY_ID")
        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    assert key_id and key_path, "Kalshi API credentials missing from env"
    pk = load_private_key(key_path)
    return auth_headers(pk, key_id, method, path)


def _base() -> str:
    import os
    return PROD_BASE if (os.environ.get("KALSHI_ENV") or "demo").lower() == "prod" \
        else DEMO_BASE


def fetch_balance_usd() -> float:
    """GET /portfolio/balance on the env-selected account. Returns dollars."""
    path = "/trade-api/v2/portfolio/balance"
    url = _base() + "/portfolio/balance"
    req = urllib.request.Request(url, headers={
        "User-Agent": "someopark-macro", **_auth_headers("GET", path)})
    with urllib.request.urlopen(req, timeout=20) as r:
        doc = json.load(r)
    return float(doc["balance"]) / 100.0          # API returns cents


def refresh_bankroll(conn) -> dict:
    """Fetch + persist the live demo balance; returns {bankroll_usd, source, ts}."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        bal = fetch_balance_usd()
        conn.execute(
            "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
            " metrics_json, created_ts) VALUES('bankroll','latest','*','live',?,?)",
            (json.dumps({"bankroll_usd": bal, "source": "kalshi_demo"}), now))
        conn.commit()
        return {"bankroll_usd": bal, "source": "kalshi_demo", "ts": now}
    except Exception as e:                                       # noqa: BLE001
        r = conn.execute("SELECT metrics_json FROM experiments WHERE name='bankroll'"
                         " AND config_hash='latest'").fetchone()
        if r:
            d = json.loads(r["metrics_json"])
            d.update({"source": d.get("source", "cache") + "(cached)", "error": str(e)})
            return d
        return {"bankroll_usd": 1000.0, "source": "seed_fallback", "error": str(e)}


def current_bankroll(conn) -> float:
    r = conn.execute("SELECT metrics_json FROM experiments WHERE name='bankroll'"
                     " AND config_hash='latest'").fetchone()
    if r:
        return float(json.loads(r["metrics_json"])["bankroll_usd"])
    return 1000.0
