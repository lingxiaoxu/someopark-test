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
    assert len(fills) == 1 and fills[0]["price"] == pytest.approx(0.51)   # slippage 1c
    # no averaging down on second pass
    d2 = decide([_mk_struct(0.60, 0.50)], now=now, close_time=now + timedelta(hours=5),
                release_ts=None, market_implied=None,
                already_open=ledger.has_open(conn, "KXJOBLESSCLAIMS", "2026-07-30"),
                bankroll=1000)
    assert d2.action == "pass"
