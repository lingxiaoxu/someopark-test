"""M1 acceptance: claims model PIT canary + strategy math + ledger. tmp db only."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model import claims
from prediction_market_macro.model.common import GaussianMix, grid_pmf
from prediction_market_macro.ops import ledger
from prediction_market_macro.strategy import devig
from prediction_market_macro.strategy.decision import decide
from prediction_market_macro.strategy.edge import (Struct, enumerate_structs,
                                                   quarter_kelly_usd, taker_fee)


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


def _seed_claims(conn, n_weeks=400, base=220_000):
    """Synthetic ICSA vintage rows: weekly first prints (+ a tiny revision next week)."""
    rng = np.random.default_rng(7)
    t0 = datetime(2018, 1, 4, tzinfo=timezone.utc)
    v = base
    for i in range(n_weeks):
        wk = t0 + timedelta(weeks=i)
        v = max(150_000, v * (1 + rng.normal(0, 0.02)))
        ev = wk.date().isoformat()
        rel = (wk + timedelta(days=6)).replace(hour=12, minute=30)
        conn.execute("INSERT OR IGNORE INTO fred_obs VALUES('ICSA',?,?,?,?,?)",
                     (ev, round(v / 250) * 250, rel.date().isoformat(), rel.isoformat(),
                      rel.isoformat()))
        rev = rel + timedelta(days=7)
        conn.execute("INSERT OR IGNORE INTO fred_obs VALUES('ICSA',?,?,?,?,?)",
                     (ev, round(v / 250) * 250 + 1000, rev.date().isoformat(),
                      rev.isoformat(), rev.isoformat()))
    conn.commit()
    return t0 + timedelta(weeks=n_weeks)


def test_claims_pit_canary(conn):
    """predict(asof=T) must be bit-identical after injecting FUTURE data (§5-bis.4-3)."""
    end = _seed_claims(conn)
    asof = end + timedelta(days=1)
    period = (end + timedelta(days=6)).date().isoformat()
    p1 = claims.predict(conn, asof, period)
    # inject a future vintage (knowledge_time AFTER asof)
    fut = (asof + timedelta(days=3))
    conn.execute("INSERT INTO fred_obs VALUES('ICSA',?,?,?,?,?)",
                 (end.date().isoformat(), 999_999, fut.date().isoformat(),
                  fut.isoformat(), fut.isoformat()))
    conn.commit()
    p2 = claims.predict(conn, asof, period)
    assert p1.dist.comps == p2.dist.comps
    assert p1.inputs == p2.inputs
    assert p1.data_horizon <= p1.asof


def test_claims_reasonable(conn):
    end = _seed_claims(conn)
    p = claims.predict(conn, end + timedelta(days=1),
                       (end + timedelta(days=6)).date().isoformat())
    mu = p.dist.comps[0][1]
    assert 150_000 < mu < 400_000
    pmf = claims.ladder(p)
    assert abs(sum(pmf.values()) - 1) < 1e-9


# ── devig ────────────────────────────────────────────────────────────────────
def test_isotonic_and_arb_detection():
    legs = [
        {"ticker": "A", "strike": 200_000, "yes_bid": 0.80, "yes_ask": 0.83},
        {"ticker": "B", "strike": 205_000, "yes_bid": 0.60, "yes_ask": 0.64},
        {"ticker": "C", "strike": 210_000, "yes_bid": 0.70, "yes_ask": 0.72},  # violates monotone
        {"ticker": "D", "strike": 215_000, "yes_bid": 0.20, "yes_ask": 0.24},
    ]
    out = devig.ladder_implied(legs)
    sv = out["survival"]
    assert all(sv[i] >= sv[i + 1] - 1e-9 for i in range(len(sv) - 1))
    assert out["violations"] and out["violations"][0]["buy"]["ticker"] == "B"  # ask .64 < bid .70
    assert abs(sum(out["pmf"].values()) - 1) < 1e-6


# ── fees / kelly / structures ────────────────────────────────────────────────
def test_fee_and_kelly():
    assert taker_fee(0.5, 100) == 1.75          # 0.07*100*0.25
    assert taker_fee(0.99, 100) == pytest.approx(0.07, abs=0.01)
    assert quarter_kelly_usd(0.60, 0.50, 1000, cap=1.0) == 1.0     # capped
    assert quarter_kelly_usd(0.40, 0.50, 1000) == 0.0              # no edge


def test_enumerate_structs_bucket_math():
    d = GaussianMix(((1.0, 210_000, 8_000),))
    pmf = grid_pmf(d, 250.0)
    legs = [{"ticker": "A", "strike": 205_000, "strike_type": "greater_or_equal",
             "yes_bid": 0.70, "yes_ask": 0.74, "bid_depth": 500, "ask_depth": 500},
            {"ticker": "B", "strike": 215_000, "strike_type": "greater_or_equal",
             "yes_bid": 0.25, "yes_ask": 0.29, "bid_depth": 500, "ask_depth": 500}]
    sts = enumerate_structs(legs, pmf, strict=False)
    kinds = {s.kind for s in sts}
    assert kinds == {"single", "bucket"}
    b = next(s for s in sts if s.kind == "bucket")
    assert b.cost == pytest.approx(0.74 + 0.75 - 1, abs=1e-9)      # eff bucket price 0.49
    assert 0.3 < b.fair < 0.6                                       # ~P(205k<=x<215k)


# ── decision gates + ledger ─────────────────────────────────────────────────
def _mk_struct(fair, cost, depth=500.0):
    from prediction_market_macro.strategy.edge import Leg
    return Struct("single", (Leg("T", "yes", cost, depth),), fair, cost, cost, "t")


def test_gates(conn):
    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    close = now + timedelta(hours=20)
    d = decide([_mk_struct(0.60, 0.50)], now=now, close_time=close, release_ts=None,
               market_implied=None, already_open=False, bankroll=1000)
    assert d.action == "open" and d.size_usd == 1.0
    # freeze window
    d2 = decide([_mk_struct(0.60, 0.50)], now=now, close_time=close,
                release_ts=now + timedelta(minutes=5), market_implied=None,
                already_open=False, bankroll=1000)
    assert d2.action == "pass" and "freeze_window" in d2.reasons
    # sanity gap
    d3 = decide([_mk_struct(0.90, 0.30)], now=now, close_time=close, release_ts=None,
                market_implied={}, already_open=False, bankroll=1000)
    assert d3.action == "pass"
    # depth
    d4 = decide([_mk_struct(0.60, 0.50, depth=10)], now=now, close_time=close,
                release_ts=None, market_implied=None, already_open=False, bankroll=1000)
    assert d4.action == "pass"


def test_ledger_append_only_and_has_open(conn):
    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    d = decide([_mk_struct(0.60, 0.50)], now=now, close_time=now + timedelta(hours=5),
               release_ts=None, market_implied=None, already_open=False, bankroll=1000)
    ledger.record(conn, series="KXJOBLESSCLAIMS", period="2026-07-30", decision=d,
                  pred_inputs={}, model_version="claims/0.1.0")
    assert ledger.has_open(conn, "KXJOBLESSCLAIMS", "2026-07-30")
    fills = conn.execute("SELECT * FROM fills").fetchall()
    # depth is $500 and the order is ~$1, so the taker is filled AT the touch. The old
    # flat +1c pad was applied after sizing, which is what broke the cap on cheap legs.
    assert len(fills) == 1 and fills[0]["price"] == pytest.approx(0.50)
    # no averaging down on second pass
    d2 = decide([_mk_struct(0.60, 0.50)], now=now, close_time=now + timedelta(hours=5),
                release_ts=None, market_implied=None,
                already_open=ledger.has_open(conn, "KXJOBLESSCLAIMS", "2026-07-30"),
                bankroll=1000)
    assert d2.action == "pass"


def test_fill_price_charges_a_tick_only_past_the_displayed_size():
    from prediction_market_macro.strategy.edge import fill_price
    assert fill_price(0.01, depth_usd=500.0, notional_usd=1.0) == 0.01   # fits: touch
    assert fill_price(0.01, depth_usd=0.5, notional_usd=1.0) == 0.02     # eats through
    assert fill_price(0.01, depth_usd=None, notional_usd=1.0) == 0.01    # unknown depth
    assert fill_price(0.99, depth_usd=0.1, notional_usd=9.9) == 0.99     # clamped at 0.99


def test_sizing_never_breaches_the_cap_on_cheap_legs():
    """Regression for the single most expensive bug found: KXAAAGASW #743.

    Sized 100 contracts off a 1c ask, then filled at 2c by a pad sizing never saw — $2.14
    of real downside against max_size_usd=$1.00, a 2.14x breach. 19 of 99 live positions
    breached by exactly (ask + 0.01) / ask, so the cheapest legs broke worst. Swept across
    the price grid because the multiplier grows without bound as the quote goes to zero.
    """
    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    # min_leg_price (added 2026-07-31, after these positions were opened) now bars legs
    # under 10c outright, so the 1c case can only be exercised at the fill_price level
    # above. Sweep the range that can still reach the ledger.
    d0 = decide([_mk_struct(0.21, 0.01)], now=now, close_time=now + timedelta(hours=5),
                release_ts=None, market_implied=None, already_open=False, bankroll=1000)
    assert d0.action == "pass" and any("penny_leg" in r for r in d0.reasons)
    opened = 0
    for cost in (0.10, 0.15, 0.20, 0.25, 0.35, 0.50, 0.65, 0.75):
        st = _mk_struct(min(cost + 0.20, 0.98), cost)   # +0.20 stays inside the gap gate
        d = decide([st], now=now, close_time=now + timedelta(hours=5), release_ts=None,
                   market_implied=None, already_open=False, bankroll=1000)
        if d.action != "open":
            continue
        opened += 1
        # a single risks its full premium, at the price actually paid
        worst = sum(st.fill_prices(d.count)) * d.count
        assert worst <= 1.0 + 1e-9, f"cost={cost}: worst={worst:.4f} breaches $1 cap"
        # and the ledger's recorded size must be that same number, not the budget
        assert d.size_usd == pytest.approx(worst, abs=0.01)
    assert opened >= 6, "sweep degenerated — gates rejected nearly everything"


def test_bucket_fill_cost_prices_the_dollar_that_always_comes_back():
    """A bucket's downside is sum(fills) - 1, not the cash outlay: one of the two legs
    pays in every branch. Sizing on the gross would under-size by ~4x on a 34c bucket."""
    from prediction_market_macro.strategy.edge import Leg
    st = Struct("bucket", (Leg("A", "yes", 0.98, 500.0), Leg("B", "no", 0.36, 500.0)),
                fair=0.55, cost=0.34, max_loss=0.34, desc="b")
    assert st.fill_cost(2) == pytest.approx(0.34)      # 0.98 + 0.36 - 1
    assert sum(st.fill_prices(2)) == pytest.approx(1.34)
