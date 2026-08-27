"""Tests for the strategy math layer (plan 04)."""
from __future__ import annotations

import numpy as np

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.strategy.devig import devig, multiplicative, power, shin
from prediction_market_soccer.strategy.edge import compute_edge, shrink
from prediction_market_soccer.strategy.sizing import kelly_fraction, size_position


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
def test_xv_liquidity_guard_rejects_an_empty_book():
    """An untouched club market quotes ~3¢ bid / ~81¢ ask on all three sides; de-vigging
    that returns 33/33/33, which then reads as a huge 'divergence' against any real model
    and tops the board. Only a genuinely tight book counts as a reference price."""
    from prediction_market_soccer.strategy.xv_monitor import _MAX_SPREAD, _liquid_devig

    def book(spread: float) -> dict:
        side = {"ask": 0.40 + spread / 2, "bid": 0.40 - spread / 2}
        return {"home": dict(side), "draw": dict(side), "away": dict(side),
                "devig": {"home": 0.4, "draw": 0.3, "away": 0.3}}

    assert _liquid_devig(book(0.02)) == {"home": 0.4, "draw": 0.3, "away": 0.3}
    assert _liquid_devig(book(_MAX_SPREAD + 0.01)) is None      # empty book, not a price
    # A missing side is also not a price — never half a book.
    half = book(0.02)
    half["away"] = {"ask": None, "bid": None}
    assert _liquid_devig(half) is None
    assert _liquid_devig(None) is None
    assert _liquid_devig({"home": {"ask": 0.4, "bid": 0.39}}) is None   # no devig payload


def test_xv_champion_shin_devig_needs_a_real_field():
    """Champion ¢ are de-vigged N-way with Shin (longshot-aware). A field too small to be
    an exclusive market must return nothing rather than a fabricated distribution."""
    from prediction_market_soccer.strategy.xv_monitor import _shin_devig_cents
    p = _shin_devig_cents({"arsenal": 40.0, "manchester_city": 30.0, "liverpool": 25.0,
                           "chelsea": 15.0})
    assert abs(sum(p.values()) - 1.0) < 1e-6
    assert p["arsenal"] > p["chelsea"]
    # zero/None marks are dropped, and under three real quotes there is no field to de-vig
    assert _shin_devig_cents({"arsenal": 40.0, "chelsea": 0.0, "ipswich": None}) == {}
    assert _shin_devig_cents({}) == {}
