"""Tests for risk / cross-venue math / order translation (plan 04 §6, 08, 09)."""
from __future__ import annotations

import pytest

from prediction_market_soccer.config import RiskConfig
from prediction_market_soccer.exec.order_translation import (
    Order,
    OrderTranslationError,
    assert_pretrade_ok,
    pretrade_checks,
    to_orders,
)
from prediction_market_soccer.strategy.cross_venue import evaluate_lock, scan_yes_basket
from prediction_market_soccer.strategy.risk import RiskManager
from prediction_market_soccer.venues.guard import VenueGuardError


# ── risk ─────────────────────────────────────────────────────────────────────
def test_market_and_theme_caps():
    rm = RiskManager(bankroll=100_000.0, risk=RiskConfig())
    # max_single_market_frac 0.05 → $5000 per market; theme 0.10 → $10000.
    allowed, reason = rm.approve(8000.0, "BRAZIL-WIN", "brazil")
    assert allowed == 5000.0 and "cap" in reason
    rm.register(5000.0, "BRAZIL-WIN", "brazil")
    # Second market in same theme: theme has $5000 room left.
    allowed2, _ = rm.approve(8000.0, "BRAZIL-GBOOT", "brazil")
    assert allowed2 == 5000.0


def test_kill_switch_blocks():
    rm = RiskManager(bankroll=100_000.0, risk=RiskConfig())
    rm.record_pnl(-9000.0)  # > 8% of 100k
    assert rm.killed
    allowed, reason = rm.approve(100.0, "X", "x")
    assert allowed == 0.0 and "kill" in reason


# ── cross-venue ──────────────────────────────────────────────────────────────
def test_lock_arb_requires_equiv_and_edge():
    # Dembele example (plan 08 §1): cheap 0.032 vs expensive 0.067.
    lock = evaluate_lock(0.032, 0.067, equiv_verified=True, fee_cheap=0.005, fee_expensive=0.005)
    assert lock.gross_lock == pytest.approx(0.035)
    assert lock.net_lock == pytest.approx(0.025)
    assert lock.tradable  # net 0.025 >= theta_arb 0.02
    # Same edge but settlement NOT verified → never tradable.
    unverified = evaluate_lock(0.032, 0.067, equiv_verified=False)
    assert not unverified.tradable


def test_yes_basket_free_money():
    arb = scan_yes_basket([0.30, 0.30, 0.30], fees=[0.005, 0.005, 0.005])
    # cost 0.915 < 1 → profit ~0.085
    assert arb.profit == pytest.approx(0.085)
    assert arb.tradable
    assert not scan_yes_basket([0.40, 0.35, 0.30]).tradable  # sums > 1


# ── order translation ────────────────────────────────────────────────────────
def test_kalshi_target_net():
    assert to_orders("kalshi", "M", target_net=5, current_pos=0) == [Order("kalshi", "M", "buy", "yes", 5)]
    # Reverse intent → buy NO (netting), never both sides.
    assert to_orders("kalshi", "M", target_net=-3, current_pos=0) == [Order("kalshi", "M", "buy", "no", 3)]
    assert to_orders("kalshi", "M", target_net=2, current_pos=2) == []  # already there


def test_polyus_intents():
    [o] = to_orders("poly_us", "slug", target_net=10, current_pos=0)
    assert o.intent == "ORDER_INTENT_BUY_LONG" and o.tif == "GTC"
    [o2] = to_orders("poly_us", "slug", target_net=4, current_pos=10)  # reduce long
    assert o2.intent == "ORDER_INTENT_SELL_LONG" and o2.count == 6


def test_guard_blocks_global_orders():
    with pytest.raises(VenueGuardError):
        to_orders("poly_global", "x", target_net=1)


def test_pretrade_checks_catch_both_sides():
    bad = [Order("kalshi", "M", "buy", "yes", 1), Order("kalshi", "M", "buy", "no", 1)]
    results = dict((n, ok) for n, ok, _ in pretrade_checks(bad))
    assert results["kalshi_single_side"] is False
    with pytest.raises(OrderTranslationError):
        assert_pretrade_ok(bad)


def test_pretrade_checks_pass_clean():
    good = to_orders("kalshi", "M", target_net=5) + to_orders("poly_us", "s", target_net=3)
    assert_pretrade_ok(good)  # no raise
    assert all(ok for _, ok, _ in pretrade_checks(good))


def test_executor_dollar_cap_count():
    from prediction_market_soccer.exec.executor import cap_count
    assert cap_count(0.17) == 5            # 5 × 0.17 = 0.85 <= $1; 6 would be 1.02
    assert cap_count(0.50) == 2            # 2 × 0.50 = $1.00
    assert cap_count(0.99) == 1
    assert cap_count(0.0001) >= 1


def test_executor_calibration_gate_blocks(tmp_path):
    import json
    from prediction_market_soccer.exec import executor
    bad = tmp_path / "oos_bad.json"; bad.write_text(json.dumps({"brier": 0.72, "n_matches": 15}))
    ok, msg = executor._calibration_ok("champion", path=bad)
    assert ok is False and "uncalibrated" in msg.lower()
    good = tmp_path / "oos_good.json"; good.write_text(json.dumps({"brier": 0.55, "n_matches": 15}))
    ok2, _ = executor._calibration_ok("champion", path=good)
    assert ok2 is True
