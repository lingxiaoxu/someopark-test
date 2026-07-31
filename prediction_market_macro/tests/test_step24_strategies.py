"""tests/test_step24_strategies.py — §24 A(arb)/B(snipe)/D(favorite path) units.
tmp db only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.strategy import arb
from prediction_market_macro.strategy.decision import decide
from prediction_market_macro.strategy.edge import Leg, Struct

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


# ── A. arb execution ─────────────────────────────────────────────────────────

def _legs_meta():
    return [
        {"ticker": "S-190", "strike": 190.0, "yes_bid": 0.60, "yes_ask": 0.62,
         "bid_depth": 200.0, "ask_depth": 200.0},
        {"ticker": "S-195", "strike": 195.0, "yes_bid": 0.70, "yes_ask": 0.74,
         "bid_depth": 200.0, "ask_depth": 200.0},
    ]


def test_arb_monotone_executes(conn):
    legs = _legs_meta()
    # violation: ask(190)=0.62 < bid(195)=0.70 → buy YES(190)+NO(195), cost 0.92
    v = [{"buy": legs[0], "sell": legs[1], "gross": 0.08}]
    n = arb.execute(conn, "KXT", "2026-07", legs, v)
    assert n == 1
    d = conn.execute("SELECT * FROM decisions WHERE kind='arb'").fetchone()
    assert d is not None and d["net_edge"] > 0
    fills = conn.execute("SELECT side, price FROM fills WHERE decision_id=?",
                         (d["id"],)).fetchall()
    assert {(f["side"], f["price"]) for f in fills} == {("yes", 0.62), ("no", 0.30)}
    # idempotent: same open arb blocks a second
    assert arb.execute(conn, "KXT", "2026-07", legs, v) == 0


def test_arb_fee_gate_blocks_thin_gross(conn):
    legs = _legs_meta()
    legs[0]["yes_ask"] = 0.695                      # gross 0.005 — fees eat it
    v = [{"buy": legs[0], "sell": legs[1], "gross": 0.005}]
    assert arb.execute(conn, "KXT2", "2026-07", legs, v) == 0


def test_arb_partition_buy_all_yes(conn):
    legs = [{"ticker": f"B-{i}", "strike": float(i), "yes_bid": 0.20,
             "yes_ask": 0.30, "bid_depth": 100.0, "ask_depth": 100.0}
            for i in range(3)]                      # sum asks 0.90 < 1
    v = [{"kind": "buy_all_yes", "gross": 0.10}]
    assert arb.execute(conn, "KXB", "2026-07", legs, v) == 1
    d = conn.execute("SELECT * FROM decisions WHERE kind='arb'").fetchone()
    assert conn.execute("SELECT COUNT(*) c FROM fills WHERE decision_id=?",
                        (d["id"],)).fetchone()["c"] == 3


# ── B. snipe boundary + certainty logic (via health leg semantics) ───────────

def test_snipe_requires_ingested_print(conn):
    from prediction_market_macro.strategy import snipe
    # KXJOBLESSCLAIMS with no fred data and no contracts → 0, never guesses
    assert snipe.run_for(conn, "KXJOBLESSCLAIMS", "2026-07-30") == 0


def test_snipe_executes_on_certain_leg(conn):
    from prediction_market_macro.strategy import snipe
    kt = datetime(2026, 7, 30, 12, 30, tzinfo=timezone.utc)
    conn.execute("INSERT INTO fred_obs VALUES('ICSA','2026-07-25',197000,?,?,?)",
                 (kt.date().isoformat(), kt.isoformat(), kt.isoformat()))
    for strike, bid, ask in ((190000, 0.80, 0.85), (200000, 0.10, 0.15)):
        tok = "26JUL30"
        conn.execute(
            "INSERT INTO contracts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"KXJOBLESSCLAIMS-{tok}-{strike}", "KXJOBLESSCLAIMS", "EV", tok, "",
             "greater_or_equal", float(strike), None,
             "2026-07-30T14:00:00Z", "active", kt.isoformat()))
        conn.execute("INSERT INTO quotes VALUES(?,?,?,?,?,?)",
                     (kt.isoformat(), f"KXJOBLESSCLAIMS-{tok}-{strike}",
                      bid, ask, 300.0, 300.0))
    conn.commit()
    n = snipe.run_for(conn, "KXJOBLESSCLAIMS", "2026-07-30")
    # print 197000: ≥190000 YES certain (ask .85) and ≥200000 NO certain (.90);
    # the $2 per-period cap admits the first (2 contracts ≈ $1.70) and correctly
    # stops before the second — total stake bounded by MAX_SNIPE_USD
    assert n >= 1
    staked = conn.execute(
        "SELECT SUM(size_usd) s FROM decisions WHERE kind='snipe'").fetchone()["s"]
    assert staked <= snipe.MAX_SNIPE_USD + 1e-9


def test_snipe_boundary_guard(conn):
    from prediction_market_macro.strategy import snipe
    kt = datetime(2026, 7, 30, 12, 30, tzinfo=timezone.utc)
    conn.execute("INSERT INTO fred_obs VALUES('ICSA','2026-07-25',195100,?,?,?)",
                 (kt.date().isoformat(), kt.isoformat(), kt.isoformat()))
    tok = "26JUL30"
    conn.execute(
        "INSERT INTO contracts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (f"KXJOBLESSCLAIMS-{tok}-195000", "KXJOBLESSCLAIMS", "EV", tok, "",
         "greater_or_equal", 195000.0, None, "2026-07-30T14:00:00Z", "active",
         kt.isoformat()))
    conn.execute("INSERT INTO quotes VALUES(?,?,?,?,?,?)",
                 (kt.isoformat(), f"KXJOBLESSCLAIMS-{tok}-195000",
                  0.50, 0.55, 300.0, 300.0))
    conn.commit()
    # print 195100 is within 0.5*round_rule(250)=125 of strike 195000 → skip
    assert snipe.run_for(conn, "KXJOBLESSCLAIMS", "2026-07-30") == 0


# ── D. favorite path ─────────────────────────────────────────────────────────

def _st(fair, cost, depth=500.0):
    return Struct("single", (Leg("T", "yes", cost, depth),), fair=fair, cost=cost,
                  max_loss=cost, desc=f"YES T @{cost:.2f}")


def test_favorite_path_admits_short_dated_near_certainty():
    st = _st(0.97, 0.95)                            # ne ≈ 0.02−fee < 0.04 main gate
    close = NOW + timedelta(days=1)
    d = decide([st], now=NOW, close_time=close, release_ts=None,
               market_implied=None, already_open=False, bankroll=100.0)
    assert d.action == "open"
    assert any("favorite_path" in r for r in d.reasons)


def test_favorite_path_rejects_long_dated():
    st = _st(0.97, 0.95)
    close = NOW + timedelta(days=30)                # epd ≈ 0.0005 < 0.008
    d = decide([st], now=NOW, close_time=close, release_ts=None,
               market_implied=None, already_open=False, bankroll=100.0)
    assert d.action == "pass"


def test_favorite_path_rejects_low_fair():
    st = _st(0.60, 0.58)                            # small edge but not near-certain
    close = NOW + timedelta(days=1)
    d = decide([st], now=NOW, close_time=close, release_ts=None,
               market_implied=None, already_open=False, bankroll=100.0)
    assert d.action == "pass"
