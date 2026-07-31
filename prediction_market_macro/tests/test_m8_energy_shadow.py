"""Tests for the plan-gap batch: energy periods/buckets, shadow guard, registry cards.

Pure-function + tmp-sqlite only — never touches the production db (dev boundary).
"""
from __future__ import annotations

import math

from prediction_market_macro.model.common import leg_fair
from prediction_market_macro.strategy.devig import bucket_implied
from prediction_market_macro.strategy.edge import enumerate_structs
from prediction_market_macro.util.periods import kalshi_period_to_key


def test_energy_period_tokens():
    # KXWTIW events carry a 4-digit day+seq suffix ('26JUL3114' = Jul 31, seq 14)
    assert kalshi_period_to_key("26JUL3114") == "2026-07-31"
    assert kalshi_period_to_key("26AUG03") == "2026-08-03"
    assert kalshi_period_to_key("26SEP") == "2026-09"
    assert kalshi_period_to_key("garbage") is None


def test_leg_fair_between():
    pmf = {77.10: 0.25, 77.50: 0.25, 78.10: 0.5}
    # between [77.00, 77.99]: mass at 77.10 + 77.50
    assert math.isclose(leg_fair(pmf, "between", 77.00, 77.99), 0.5)
    assert math.isclose(leg_fair(pmf, "less", None, 77.99), 0.5)
    assert math.isclose(leg_fair(pmf, "greater", 77.99, None), 0.5)


def test_bucket_implied_partition_arb():
    legs = [
        {"ticker": "A", "strike": None, "strike_type": "less",
         "yes_bid": 0.05, "yes_ask": 0.07},
        {"ticker": "B", "strike": 77.0, "strike_type": "between",
         "yes_bid": 0.40, "yes_ask": 0.42},
        {"ticker": "C", "strike": 78.0, "strike_type": "between",
         "yes_bid": 0.30, "yes_ask": 0.32},
    ]
    out = bucket_implied(legs)
    assert abs(sum(out["pmf"].values()) - 1.0) < 1e-9
    # asks sum to 0.81 < 1 → buy-every-YES free money flagged
    assert any(v["kind"] == "buy_all_yes" for v in out["violations"])
    # the 'less' tail keys at -inf, buckets at their lower edge
    assert float("-inf") in out["pmf"] and 77.0 in out["pmf"]


def test_enumerate_structs_between_legs():
    pmf = {77.10: 0.2, 77.50: 0.3, 78.10: 0.5}
    legs = [
        {"ticker": "B1", "strike": 77.0, "cap_strike": 77.99, "strike_type": "between",
         "yes_bid": 0.30, "yes_ask": 0.35, "bid_depth": 100, "ask_depth": 100},
        {"ticker": "T", "strike": None, "cap_strike": 77.0, "strike_type": "less",
         "yes_bid": 0.01, "yes_ask": 0.03, "bid_depth": 100, "ask_depth": 100},
    ]
    structs = enumerate_structs(legs, pmf, strict=True)
    yes_b1 = next(s for s in structs if s.desc.startswith("YES B1"))
    assert math.isclose(yes_b1.fair, 0.5)          # mass in [77.00, 77.99]
    no_b1 = next(s for s in structs if s.desc.startswith("NO B1"))
    assert math.isclose(no_b1.fair, 0.5)
    yes_t = next(s for s in structs if s.desc.startswith("YES T"))
    assert math.isclose(yes_t.fair, 0.0, abs_tol=1e-12)   # nothing below 77.00


def test_registry_cards_complete():
    import sqlite3
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.ingest.store import SCHEMA
    from prediction_market_macro.model.registry import ensure_registered
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    n = ensure_registered(conn)
    assert n == len(REGISTRY)                      # one card per registered series
    for r in conn.execute("SELECT series, card_md FROM models"):
        assert r["card_md"] and len(r["card_md"]) > 20, r["series"]
    assert ensure_registered(conn) == 0            # idempotent


def test_shadow_guard_query_shape():
    """The decide_all pred query must exclude shadow model_versions by prefix."""
    import inspect
    from prediction_market_macro.ops import decide_all
    src = inspect.getsource(decide_all.run)
    assert "model_version LIKE" in src
