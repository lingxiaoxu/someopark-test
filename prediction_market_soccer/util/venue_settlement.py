"""Venue-declared final settlement for paper legs whose match left its scheduled slot.

A paper leg is a venue contract, so when its match is postponed or cancelled the
contract's own rules decide the payout — Polymarket US settles to the last fair
price unless the match is rescheduled within two weeks, Kalshi to a fair price
beyond 48 hours — and the venue closes the contract long before any rescheduled
fixture is played. Waiting for that later regulation result would book a payout
the contract never had (Levante–Athletic, 2026-09-16 → 10-21: Polymarket US 0.28).

Only legs of a displaced fixture are ever queried, and only once: the venue's own
closed-market response is stored verbatim with its hash and never rewritten.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

from prediction_market_soccer.ops.maintenance_gate import writer

DISPLACED_STATUS = ("PST", "CANC", "ABD", "SUSP", "INT")
DISPLACEMENT = timedelta(hours=48)


def _utc(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_venue_settlement (
        fixture_api_id INTEGER NOT NULL, track TEXT NOT NULL, entry_id TEXT NOT NULL,
        provider TEXT NOT NULL, market_id TEXT NOT NULL, outcome TEXT NOT NULL,
        settle_price REAL NOT NULL, observed_at TEXT NOT NULL, raw TEXT NOT NULL,
        raw_sha256 TEXT NOT NULL, PRIMARY KEY(fixture_api_id, track))""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS paper_venue_settlement_immutable
        BEFORE UPDATE ON paper_venue_settlement
        BEGIN SELECT RAISE(ABORT, 'Venue settlement evidence is immutable'); END""")


def binding_for(conn, entry):
    receipt_id = (entry.get("selected_quote") or {}).get("receipt_id")
    row = conn.execute("SELECT payload FROM quote_receipt_v1 WHERE receipt_id=?", (receipt_id,)).fetchone()
    if not row:
        return None
    return json.loads(row[0]).get("binding")


def displaced(fixture, binding) -> bool:
    """The match is no longer in the slot the contract was written for."""
    if fixture is None or not binding or not binding.get("kickoff_ts"):
        return False
    if fixture["status_short"] in DISPLACED_STATUS:
        return True
    if not fixture["kickoff_ts"]:
        return False
    return _utc(fixture["kickoff_ts"]) - _utc(binding["kickoff_ts"]) > DISPLACEMENT


def _price(value):
    price = float(value)
    if not math.isfinite(price) or not 0.0 <= price <= 1.0:
        raise ValueError("Venue settlement price outside [0, 1]")
    return price


def _poly_us_final(market_id):
    """(yes price, raw) once Polymarket US has closed and settled the market, else None."""
    import os
    from polymarket_us import PolymarketUS
    from prediction_market_soccer.venues import ratelimit
    client = PolymarketUS(key_id=os.environ["PMUS_KEY_ID"], secret_key=os.environ["PMUS_SECRET"])
    limiter = ratelimit.polymarket_us_limiter()
    market = limiter.run(lambda: client.markets.retrieve_by_slug(market_id), bulk=True, timeout=30)["market"]
    if market.get("slug") != market_id:
        raise ValueError("market_response_identity_mismatch")
    if market.get("closed") is not True:
        return None
    settlement = limiter.run(lambda: client.markets.settlement(market_id), bulk=True, timeout=30)
    if settlement.get("slug") != market_id or settlement.get("settlement") is None:
        return None
    return _price(settlement["settlement"]), {"market": market, "settlement": settlement}


def _kalshi_final(ticker):
    import requests
    from prediction_market_soccer.venues.kalshi.discovery import PROD_PUBLIC
    response = requests.get(f"{PROD_PUBLIC}/markets/{ticker}", timeout=30)
    response.raise_for_status()
    market = response.json()["market"]
    if market.get("ticker") != ticker:
        raise ValueError("market_response_identity_mismatch")
    if market.get("status") not in ("settled", "finalized"):
        return None
    if market.get("result") in ("yes", "no"):
        return (1.0 if market["result"] == "yes" else 0.0), {"market": market}
    for key, scale in (("settlement_value_dollars", 1.0), ("settlement_value", 100.0)):
        if market.get(key) is not None:
            return _price(float(market[key]) / scale), {"market": market}
    return None


FETCHERS = {"poly_us": _poly_us_final, "kalshi": _kalshi_final}


@writer
def observe(conn, *, now=None, fetchers=None):
    """Record the venue's final settlement for every open leg of a displaced fixture."""
    from prediction_market_soccer.util.paper_store import positions
    fetchers = fetchers or FETCHERS
    now = (now or datetime.now(timezone.utc)).isoformat()
    ensure(conn)
    conn.commit()
    out = {"displaced_legs": 0, "recorded": 0, "pending": 0, "errors": []}
    for position in positions(conn):
        if position["settlement"]:
            continue
        fid, track, entry = position["fixture_api_id"], position["track"], position["entry"]
        if conn.execute("SELECT 1 FROM paper_venue_settlement WHERE fixture_api_id=? AND track=?",
                        (fid, track)).fetchone():
            continue
        fixture = conn.execute("SELECT * FROM fixture WHERE api_id=?", (fid,)).fetchone()
        binding = binding_for(conn, entry)
        if not displaced(fixture, binding):
            continue
        out["displaced_legs"] += 1
        fetch = fetchers.get(binding.get("provider"))
        if fetch is None:
            out["errors"].append(f"{fid}/{track}: no settlement reader for {binding.get('provider')}")
            continue
        try:
            final = fetch(binding["market_id"])
        except Exception as exc:                                # noqa: BLE001
            out["errors"].append(f"{fid}/{track}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        if final is None:
            out["pending"] += 1
            continue
        yes_price, raw = final
        outcome = binding.get("outcome") or "yes"
        if outcome not in ("yes", "no"):
            out["errors"].append(f"{fid}/{track}: unsupported contract outcome {outcome!r}")
            continue
        price = yes_price if outcome == "yes" else round(1.0 - yes_price, 6)
        text = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
        conn.execute("INSERT OR IGNORE INTO paper_venue_settlement VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (fid, track, entry["decision_id"], binding["provider"], binding["market_id"], outcome,
                      price, now, text, hashlib.sha256(text.encode()).hexdigest()))
        conn.commit()
        out["recorded"] += 1
    return out


def evidence(conn, fixture_id, legs):
    """{track: evidence row} when EVERY leg carries recorded venue settlement, else None."""
    ensure(conn)
    rows = {}
    for track, position in legs.items():
        row = conn.execute("SELECT * FROM paper_venue_settlement WHERE fixture_api_id=? AND track=?",
                           (fixture_id, track)).fetchone()
        if not row or row["entry_id"] != position["entry"]["decision_id"]:
            return None
        if hashlib.sha256(row["raw"].encode()).hexdigest() != row["raw_sha256"]:
            raise ValueError("Venue settlement evidence changed after it was recorded")
        rows[track] = dict(row)
    return rows
