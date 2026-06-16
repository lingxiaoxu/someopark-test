"""Tests for the strategy math layer (plan 04)."""
from __future__ import annotations

import numpy as np

from prediction_market.config import CONFIG
from prediction_market.strategy.devig import devig, multiplicative, power, shin
from prediction_market.strategy.edge import compute_edge, shrink
from prediction_market.strategy.sizing import kelly_fraction, size_position


# ── De-vig ───────────────────────────────────────────────────────────────────
def test_devig_methods_normalise():
    asks = [0.55, 0.30, 0.20]  # 3-way with overround (sum 1.05)
    for method in ("multiplicative", "power", "shin"):
        q = devig(asks, method)
        assert abs(q.sum() - 1.0) < 1e-6, method
        assert (q > 0).all()


def test_power_devig_corrects_favourite_longshot_bias():
    asks = [0.80, 0.15, 0.10]  # heavy favourite + longshots, overround
    q_mult = multiplicative(asks)
    q_pow = power(asks)
    # Favourite-longshot bias: longshots are overbet. The power method (k>1)
    # pushes the longshots' implied prob DOWN and the favourite's UP vs plain
    # multiplicative de-vig.
    assert q_pow[0] >= q_mult[0] - 1e-9      # favourite weight not reduced
    assert q_pow[-1] <= q_mult[-1] + 1e-9    # longest shot weight not increased


def test_shin_runs_on_longshot_book():
    asks = [0.92, 0.05, 0.04, 0.03]  # extreme favourite-longshot
    q = shin(asks)
    assert abs(q.sum() - 1.0) < 1e-6


# ── Edge ─────────────────────────────────────────────────────────────────────
def test_shrink_is_conservative_and_clipped():
    assert shrink(0.50, 0.04, 1.0) == 0.46
    assert shrink(0.01, 0.10, 1.0) == 0.0  # clipped at 0


def test_compute_edge_gating():
    # Model 60%, ask 50%, small costs → tradable above theta=0.03.
    e = compute_edge(0.60, 0.50, sigma_p=0.02, k=1.0, fee=0.01, slippage=0.01, theta=0.03)
    assert abs(e.gross_edge - 0.10) < 1e-9
    # p_eff = 0.58; net = 0.58 - 0.50 - 0.01 - 0.01 = 0.06 >= 0.03 → trade
    assert abs(e.net_edge - 0.06) < 1e-9
    assert e.tradable

    # No edge → not tradable.
    e2 = compute_edge(0.50, 0.51, theta=0.03)
    assert not e2.tradable


# ── Sizing ───────────────────────────────────────────────────────────────────
def test_kelly_fraction():
    assert kelly_fraction(0.60, 0.50) == (0.60 - 0.50) / (1 - 0.50)
    assert kelly_fraction(0.40, 0.50) == 0.0  # negative edge → no bet


def test_size_position_respects_market_cap():
    risk = CONFIG.risk
    bankroll = 100_000.0
    # Huge edge would want a big Kelly stake; single-market cap binds.
    res = size_position(0.90, 0.10, bankroll, risk=risk)
    assert res.stake <= bankroll * risk.max_single_market_frac + 1e-6
    assert res.capped_by in ("market", "kelly")


def test_size_position_theme_room_binds():
    res = size_position(0.70, 0.50, 100_000.0, theme_room=500.0)
    assert res.stake <= 500.0 + 1e-9


# ── cross-venue monitor (pure parts, no network) ─────────────────────────────
def test_xv_team_extraction_and_consensus():
    from prediction_market.strategy.xv_monitor import Quote, _team_from_market, devig_event
    assert _team_from_market({"groupItemTitle": "Spain"}) == "spain"
    assert _team_from_market({"question": "Will Brazil win the 2026 FIFA World Cup"}) == "brazil"
    assert _team_from_market({"question": "unrelated"}) is None
    # consensus = mid when two-sided, else the present side.
    assert abs(Quote("v", 0.40, 0.42).consensus - 0.41) < 1e-9
    assert Quote("v", None, 0.42).consensus == 0.42
    assert Quote("v", 0.40, None).consensus == 0.40
    # de-vig a 3-team exclusive event → sums to 1.
    q = {"a": Quote("v", 0.50, 0.52), "b": Quote("v", 0.30, 0.32), "c": Quote("v", 0.18, 0.20)}
    p = devig_event(q, method="multiplicative")
    assert abs(sum(p.values()) - 1.0) < 1e-9 and set(p) == {"a", "b", "c"}
