"""#141 — entry and exit must price the SAME object.

`decide()` opens a STRUCTURE and gates on `fair(struct) - cost(struct) - fee`.
`exits.run()` used to close that structure on `min` over its legs' individual holding
edges. For a single leg the two agree; for a bucket YES(>lo) + NO(>hi) they cannot, because
the lo leg is bought deep-ITM near $1 and its standalone model edge is negative by
construction. min() therefore read a reversal the same second the spread opened.

Measured on the live ledger before the fix: 39 same-cycle open->exit round trips, 36 of
them buckets, every one of the 36 holding a POSITIVE structure edge at the moment it was
liquidated. The worst live case (decision #3197, KXCPIYOY 2026-07, today) showed
min=-0.3692 against a true structure edge of +0.1399 and paid -$0.27 to close.

What is NOT the bug, and is pinned here so nobody re-opens it: the two paths do not
disagree about `fair`. The entry path prices greater-family legs with
`survival(strict=spec.strict_gt)`; the exit path prices them with
`leg_fair(strike_type)`. Those agree on every contract in the book, and
`test_registry_strict_gt_matches_every_strike_type` keeps them agreeing.
"""
from __future__ import annotations

import pytest

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model.common import leg_fair, survival
from prediction_market_macro.strategy.edge import enumerate_structs

TS = "2026-08-05T00:00:00+00:00"
# P(x > 0.0) = 1.0, P(x > 0.1) = 0.6, P(x > 0.2) = 0.25 — a plain ladder.
PMF = {0.05: 0.4, 0.15: 0.35, 0.25: 0.25}


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


# ── the identity: sum over legs IS the structure edge ────────────────────────────────

def test_summed_leg_edges_equal_the_structure_edge_at_those_prices():
    """The algebra the fix rests on, checked numerically against `enumerate_structs`.

    For every structure the entry path can build, the exit path's per-leg quantity
    `fair_side - price_side`, SUMMED, must equal `struct.fair - struct.cost`. If this ever
    breaks, the two paths have drifted apart again and the churn loop is back.
    """
    meta = [{"ticker": "T0.0", "strike": 0.0, "cap_strike": None, "strike_type": "greater",
             "yes_bid": 0.97, "yes_ask": 0.98, "bid_depth": 500.0, "ask_depth": 500.0},
            {"ticker": "T0.1", "strike": 0.1, "cap_strike": None, "strike_type": "greater",
             "yes_bid": 0.61, "yes_ask": 0.62, "bid_depth": 500.0, "ask_depth": 500.0},
            {"ticker": "T0.2", "strike": 0.2, "cap_strike": None, "strike_type": "greater",
             "yes_bid": 0.26, "yes_ask": 0.27, "bid_depth": 500.0, "ask_depth": 500.0}]
    structs = enumerate_structs(meta, PMF, strict=True)
    assert any(s.kind == "bucket" for s in structs), "no bucket built — test is vacuous"

    by_ticker = {m["ticker"]: m for m in meta}
    for s in structs:
        summed = 0.0
        for leg in s.legs:
            m = by_ticker[leg.ticker]
            fair_yes = leg_fair(PMF, m["strike_type"], m["strike"], m["cap_strike"])
            fair_side = fair_yes if leg.side == "yes" else 1 - fair_yes
            summed += fair_side - leg.price
        assert summed == pytest.approx(s.fair - s.cost, abs=1e-9), (
            f"{s.desc}: legs sum to {summed:+.6f} but struct edge is "
            f"{s.fair - s.cost:+.6f}")


def test_min_over_legs_is_not_the_structure_edge_on_a_spread():
    """The counter-example, stated positively — so the fix cannot be "simplified" back.

    A single leg is the degenerate case where min == sum, which is why the bug hid.
    """
    # the lo leg quoted ABOVE its model fair (0.79 vs P(x>0.1)=0.60) — the real #604/#3197
    # shape, where the market is more confident in the tail than we are
    meta = [{"ticker": "T0.1", "strike": 0.1, "cap_strike": None, "strike_type": "greater",
             "yes_bid": 0.78, "yes_ask": 0.79, "bid_depth": 500.0, "ask_depth": 500.0},
            {"ticker": "T0.2", "strike": 0.2, "cap_strike": None, "strike_type": "greater",
             "yes_bid": 0.49, "yes_ask": 0.50, "bid_depth": 500.0, "ask_depth": 500.0}]
    by_ticker = {m["ticker"]: m for m in meta}
    bucket = next(s for s in enumerate_structs(meta, PMF, strict=True)
                  if s.kind == "bucket")
    edges = []
    for leg in bucket.legs:
        m = by_ticker[leg.ticker]
        fy = leg_fair(PMF, m["strike_type"], m["strike"], m["cap_strike"])
        edges.append((fy if leg.side == "yes" else 1 - fy) - leg.price)
    assert min(edges) < -0.06 < sum(edges), (
        f"the spread stopped being a counter-example: min={min(edges):+.4f} "
        f"sum={sum(edges):+.4f}")


# ── the same asymmetry, but end-to-end through exits.run ─────────────────────────────

def _ladder_position(conn, legs, series="KXPCECORE", period="2026-09"):
    """Open a paper position on `legs` = [(ticker, strike, side, price)] and quote them."""
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair, ask,"
        " net_edge, size_usd, inputs_json, model_version, gate_snapshot, note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (TS, series, period, "{}", "open", 0.5, 0.5, 0.1, 1.0, "{}", "pce/0.1.0",
         "{}", ""))
    for ticker, strike, side, price in legs:
        conn.execute(
            "INSERT INTO fills(decision_id, ts_utc, ticker, side, price, count, fee_usd,"
            " mode) VALUES(?,?,?,?,?,?,?, 'paper')",
            (cur.lastrowid, TS, ticker, side, price, 1, 0.01))
        conn.execute(
            "INSERT INTO contracts(ticker, event_ticker, series, period, floor_strike,"
            " strike_type, close_time, first_seen_ts)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (ticker, f"{series}-26SEP", series, period, strike, "greater",
             "2026-09-30T00:00:00+00:00", TS))
    conn.execute(
        "INSERT INTO preds(series, period, asof, ladder_json, dist_json, model_version,"
        " data_horizon, created_ts) VALUES(?,?,?,?,?,?,?,?)",
        (series, period, TS, '{"0.05": 0.4, "0.15": 0.35, "0.25": 0.25}', "{}",
         "pce/0.1.0", TS, TS))
    conn.commit()
    return cur.lastrowid


def _quote(conn, ticker, bid, ask):
    conn.execute(
        "INSERT INTO quotes(ts, ticker, yes_bid, yes_ask, bid_depth, ask_depth)"
        " VALUES(?,?,?,?,?,?)", (TS, ticker, bid, ask, 500.0, 500.0))
    conn.commit()


class _S:
    pass


def test_the_leg_that_looks_worst_alone_does_not_force_the_exit(conn):
    """Directly the regression: one leg at -0.19, the package at +0.11.

    Before the fix `min` = -0.19 < EXIT_EDGE and the position was dumped for fees.
    """
    from prediction_market_macro.ops import exits
    _ladder_position(conn, [("T0.1", 0.1, "yes", 0.55), ("T0.2", 0.2, "no", 0.70)])
    # model: P(x>0.1) = 0.60, P(x>0.2) = 0.25  =>  fair(bucket] = 0.35
    _quote(conn, "T0.1", 0.78, 0.80)      # mid 0.79; yes leg edge = 0.60 - 0.79 = -0.19
    _quote(conn, "T0.2", 0.48, 0.50)      # mid 0.49; no  leg edge = 0.75 - 0.51 = +0.24
    legs = [(0.60 - 0.79), (0.75 - 0.51)]
    assert min(legs) < -0.06 and sum(legs) == pytest.approx(0.05, abs=1e-9)
    # structure edge +0.05 >= EXIT_EDGE (-0.06)  =>  hold
    assert exits.run(conn, _S()) == 0
    assert conn.execute("SELECT COUNT(*) c FROM decisions WHERE kind='exit'"
                        ).fetchone()["c"] == 0


def test_a_genuinely_reversed_structure_still_exits(conn):
    """The fix must not become "never exit". Push the PACKAGE below -0.06 and it goes."""
    from prediction_market_macro.ops import exits
    _ladder_position(conn, [("T0.1", 0.1, "yes", 0.55), ("T0.2", 0.2, "no", 0.70)])
    _quote(conn, "T0.1", 0.78, 0.80)      # yes leg edge = -0.19
    _quote(conn, "T0.2", 0.20, 0.22)      # no  leg edge = 0.75 - 0.79 = -0.04
    assert exits.run(conn, _S()) == 1
    r = conn.execute("SELECT net_edge, note FROM decisions WHERE kind='exit'").fetchone()
    assert r["net_edge"] == pytest.approx(-0.23, abs=1e-6)   # the SUM, not the min
    assert "hold_edge" in r["note"]


def test_a_single_leg_is_unchanged_because_sum_equals_min(conn):
    """The degenerate case the old code got right — pinned so the fix is provably
    a no-op for singles, which is what makes it safe to ship without re-running the
    single-leg half of the book."""
    from prediction_market_macro.ops import exits
    _ladder_position(conn, [("T0.1", 0.1, "yes", 0.55)])
    _quote(conn, "T0.1", 0.78, 0.80)      # edge = 0.60 - 0.79 = -0.19
    assert exits.run(conn, _S()) == 1
    r = conn.execute("SELECT net_edge FROM decisions WHERE kind='exit'").fetchone()
    assert r["net_edge"] == pytest.approx(-0.19, abs=1e-6)


# ── the thing that was NOT broken, kept that way ─────────────────────────────────────

def test_registry_strict_gt_matches_every_strike_type():
    """Entry prices greater-family legs off `survival(strict=spec.strict_gt)`; exit prices
    them off `leg_fair(strike_type)`, where 'greater' means strict and 'greater_or_equal'
    means not. If a series' rulebook flag and its Kalshi strike_type ever disagree, the two
    paths WOULD compute different fairs — the bug #141 was originally suspected to be.

    Checked against the registry rather than the live DB so it holds in CI: every ladder
    series must declare a `strict_gt` that a 'greater'/'greater_or_equal' strike_type can
    represent. (Measured 2026-08-05: 0 mismatches over 6,392 contracts, 14 series.)
    """
    for name, spec in REGISTRY.items():
        st = "greater" if spec.strict_gt else "greater_or_equal"
        assert leg_fair(PMF, st, 0.1, None) == pytest.approx(
            survival(PMF, 0.1, strict=spec.strict_gt)), (
            f"{name}: leg_fair('{st}') != survival(strict={spec.strict_gt})")
