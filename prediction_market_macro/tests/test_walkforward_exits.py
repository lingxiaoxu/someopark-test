"""#142 — the backtest must run the exit rules production runs.

`ops/refresh.py` has always called `exits.run` every cycle, while `walkforward` jumped
from entry straight to the 0/1 settlement. So every headline this harness has ever
produced described a hold-to-settlement strategy nobody runs — the same class of
divergence as #109 (gates) and #128 (the displayed run was the gates-OFF one).

The danger in porting a rule rather than calling it is that the copy drifts. So the
load-bearing test here is `test_hold_edge_matches_what_exits_run_would_compute`: it
builds one position, feeds the SAME model and the SAME book to `ops.exits.run` and to
`walkforward._hold_edge`, and asserts they agree — including on the #141 sum-over-legs
aggregation, which is exactly where they last disagreed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops.exits import EXIT_EDGE, SLIP
from prediction_market_macro.research import walkforward as wf
from prediction_market_macro.strategy.edge import Leg, Struct, taker_fee

DEPTH = 1e9
ENTRY = datetime(2026, 6, 1, 16, tzinfo=timezone.utc)
CLOSE = datetime(2026, 6, 8, 16, tzinfo=timezone.utc)
TS = "2026-06-02T00:00:00+00:00"
# P(x > 0.1) = 0.60, P(x > 0.2) = 0.25
PMF = {0.05: 0.4, 0.15: 0.35, 0.25: 0.25}
META = {"T0.1": {"strike": 0.1, "cap_strike": None, "strike_type": "greater"},
        "T0.2": {"strike": 0.2, "cap_strike": None, "strike_type": "greater"}}


@pytest.fixture()
def conn(tmp_path):
    return init_db(str(tmp_path / "t.db"))


def _bucket(lo_px=0.55, hi_px=0.70):
    return Struct("bucket", (Leg("T0.1", "yes", lo_px, DEPTH),
                             Leg("T0.2", "no", hi_px, DEPTH)),
                  fair=0.35, cost=lo_px + hi_px - 1.0, max_loss=lo_px + hi_px - 1.0,
                  desc="BUCKET (0.1,0.2]")


def _q(bid, ask, **kw):
    return {"yes_bid": bid, "yes_ask": ask, **kw}


# ── the metric ───────────────────────────────────────────────────────────────────────

def test_hold_edge_sums_over_legs_and_is_the_structure_edge():
    """#141's identity, now on the backtest side of the fence."""
    quotes = {"T0.1": _q(0.78, 0.80, **META["T0.1"]),
              "T0.2": _q(0.48, 0.50, **META["T0.2"])}
    he = wf._hold_edge({"pmf": PMF}, _bucket(), quotes)
    #   yes leg: 0.60 - 0.79 = -0.19 ;  no leg: 0.75 - 0.51 = +0.24
    assert he == pytest.approx(0.05, abs=1e-9)


def test_a_wide_book_is_unmeasurable_not_a_reversal():
    """The 0.18/0.98 case that made `exits.py` refuse midpoints at all. Returning a
    number here would let the backtest liquidate into a bid nobody would pay."""
    quotes = {"T0.1": _q(0.18, 0.98, **META["T0.1"]),
              "T0.2": _q(0.48, 0.50, **META["T0.2"])}
    assert wf._hold_edge({"pmf": PMF}, _bucket(), quotes) is None


def test_a_missing_or_unpriceable_leg_is_unmeasurable():
    good = _q(0.48, 0.50, **META["T0.2"])
    assert wf._hold_edge({"pmf": PMF}, _bucket(), {"T0.2": good}) is None
    bad = _q(0.78, 0.80, strike=None, cap_strike=None, strike_type="greater")
    assert wf._hold_edge({"pmf": PMF}, _bucket(),
                         {"T0.1": bad, "T0.2": good}) is None
    assert wf._hold_edge(None, _bucket(), {"T0.1": _q(0.78, 0.80, **META["T0.1"]),
                                           "T0.2": good}) is None


def test_categorical_legs_price_off_probs_like_exits_does():
    st = Struct("single", (Leg("KXFED-26SEP-T4.00", "yes", 0.30, DEPTH),),
                fair=0.5, cost=0.30, max_loss=0.30, desc="YES cat")
    quotes = {"KXFED-26SEP-T4.00": _q(0.28, 0.32)}
    assert wf._hold_edge({"probs": {"T4.00": 0.50}}, st, quotes) == pytest.approx(0.20)


def test_hold_edge_matches_what_exits_run_would_compute(conn):
    """One position, one book, one model — through both code paths.

    This is the test that makes "the backtest runs the same strategy" a checkable claim
    rather than a comment. If `ops/exits.py` changes its metric and this copy does not,
    this fails.
    """
    from prediction_market_macro.ops import exits
    lo_px, hi_px = 0.55, 0.70
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair, ask,"
        " net_edge, size_usd, inputs_json, model_version, gate_snapshot, note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (TS, "KXPCECORE", "2026-09", "{}", "open", 0.35, 0.25, 0.1, 1.0, "{}",
         "pce/0.1.0", "{}", ""))
    for ticker, side, px in (("T0.1", "yes", lo_px), ("T0.2", "no", hi_px)):
        conn.execute(
            "INSERT INTO fills(decision_id, ts_utc, ticker, side, price, count, fee_usd,"
            " mode) VALUES(?,?,?,?,?,?,?, 'paper')",
            (cur.lastrowid, TS, ticker, side, px, 1, 0.01))
        conn.execute(
            "INSERT INTO contracts(ticker, event_ticker, series, period, floor_strike,"
            " strike_type, close_time, first_seen_ts) VALUES(?,?,?,?,?,?,?,?)",
            (ticker, "KXPCECORE-26SEP", "KXPCECORE", "2026-09", META[ticker]["strike"],
             "greater", "2026-09-30T00:00:00+00:00", TS))
    for ticker, bid, ask in (("T0.1", 0.78, 0.80), ("T0.2", 0.20, 0.22)):
        conn.execute(
            "INSERT INTO quotes(ts, ticker, yes_bid, yes_ask, bid_depth, ask_depth)"
            " VALUES(?,?,?,?,?,?)", (TS, ticker, bid, ask, 500.0, 500.0))
    conn.execute(
        "INSERT INTO preds(series, period, asof, ladder_json, dist_json, model_version,"
        " data_horizon, created_ts) VALUES(?,?,?,?,?,?,?,?)",
        ("KXPCECORE", "2026-09", TS,
         '{"0.05": 0.4, "0.15": 0.35, "0.25": 0.25}', "{}", "pce/0.1.0", TS, TS))
    conn.commit()

    class _S:
        pass

    assert exits.run(conn, _S()) == 1
    live = conn.execute("SELECT net_edge FROM decisions WHERE kind='exit'").fetchone()

    quotes = {"T0.1": _q(0.78, 0.80, **META["T0.1"]),
              "T0.2": _q(0.20, 0.22, **META["T0.2"])}
    sim = wf._hold_edge({"pmf": PMF}, _bucket(lo_px, hi_px), quotes)
    assert sim == pytest.approx(live["net_edge"], abs=1e-9), (
        f"backtest {sim} vs live {live['net_edge']} — the paths have drifted")


# ── the rules ────────────────────────────────────────────────────────────────────────

def _path(*edges, mtm=-0.5, exit_ok=True):
    return [{"day": f"2026-06-{i + 2:02d}", "mtm": mtm, "mtm_mid": mtm,
             "hold_edge": e, "exit_px_ok": exit_ok} for i, e in enumerate(edges)]


def test_edge_reversal_fires_on_the_first_day_past_the_threshold():
    ex = wf._first_exit(_bucket(), _path(0.10, -0.02, -0.20, -0.90), 0.10)
    assert ex["day"] == "2026-06-04" and ex["rule"] == "edge_reversal"


def test_the_threshold_is_strict_and_is_exits_own_constant():
    """`exits.run` holds at `worst >= EXIT_EDGE`, so exactly -0.06 must NOT exit."""
    assert wf._first_exit(_bucket(), _path(EXIT_EDGE), 0.10) is None
    assert wf._first_exit(_bucket(), _path(EXIT_EDGE - 1e-9), 0.10) is not None


def test_an_unmeasurable_day_is_skipped_not_treated_as_a_reversal():
    """None is `exits.run`'s hold — a wide book on day 2 must not close the position."""
    assert wf._first_exit(_bucket(), _path(None, None), 0.10) is None
    ex = wf._first_exit(_bucket(), _path(None, -0.9), 0.10)
    assert ex["day"] == "2026-06-03"


def test_regime_review_needs_a_penny_entry_and_a_recoverable_exit_price():
    penny = Struct("single", (Leg("T0.1", "yes", 0.05, DEPTH),), fair=0.6, cost=0.05,
                   max_loss=0.05, desc="YES penny")
    # -0.01 is well inside EXIT_EDGE, so only rule 3 can fire here
    ex = wf._first_exit(penny, _path(-0.01), 0.10)
    assert ex is not None and ex["rule"] == "regime_review"
    # not a penny entry -> rule 3 does not apply
    assert wf._first_exit(_bucket(), _path(-0.01), 0.10) is None
    # penny entry but nothing left to recover -> hold, as live
    assert wf._first_exit(penny, _path(-0.01, exit_ok=False), 0.10) is None


def test_no_exit_returns_none_so_the_trade_settles():
    assert wf._first_exit(_bucket(), _path(0.2, 0.1, 0.05), 0.10) is None


# ── the path carries what the rules need ─────────────────────────────────────────────

def _candle(conn, ticker, day, bid, ask):
    conn.execute(
        "INSERT INTO candles(ticker, end_ts, yes_bid_close, yes_ask_close)"
        " VALUES(?,?,?,?)", (ticker, int(day.timestamp()), bid, ask))
    conn.commit()


def test_path_carries_hold_edge_only_when_asked(conn):
    st = _bucket()
    d = ENTRY + timedelta(days=1)
    _candle(conn, "T0.1", d, 0.78, 0.80)
    _candle(conn, "T0.2", d, 0.48, 0.50)
    plain = wf._mtm_path(conn, st, 1, ENTRY, CLOSE)
    assert "hold_edge" not in plain[0], (
        "the §25.5 diagnostic path must stay unchanged when exits are off")
    withed = wf._mtm_path(conn, st, 1, ENTRY, CLOSE,
                          pmf_for=lambda _d: {"pmf": PMF}, leg_meta=META)
    assert withed[0]["hold_edge"] == pytest.approx(0.05, abs=1e-6)
    assert withed[0]["mtm"] == plain[0]["mtm"], "adding the edge must not move the mark"


def test_pmf_for_is_asked_per_day_not_once_at_entry(conn):
    """A model pinned to entry day would be stale, and the exit rule would be answering
    a question about a forecast production had already replaced.

    The path runs to `close_ts`, not to the last candle: `_candle_quote` forward-fills
    the most recent close at or before each day, so a book that stops printing keeps
    marking at its last quote. That is the pre-existing §25.5 behaviour and it is right —
    a stale quote is what live would see too. What #142 adds is that EACH of those days
    gets its own model call, once, in order.
    """
    seen = []
    for i in (1, 2, 3):
        d = ENTRY + timedelta(days=i)
        _candle(conn, "T0.1", d, 0.78, 0.80)
        _candle(conn, "T0.2", d, 0.48, 0.50)

    def pmf_for(d):
        seen.append(d.date().isoformat())
        return {"pmf": PMF}

    path = wf._mtm_path(conn, _bucket(), 1, ENTRY, CLOSE, pmf_for=pmf_for, leg_meta=META)
    assert seen == [r["day"] for r in path], "one call per marked day, in path order"
    assert len(set(seen)) == len(seen), "asked twice for the same day"
    assert seen[0] == (ENTRY + timedelta(days=1)).date().isoformat(), (
        "the path must start the day AFTER entry — entry day is the decision itself")
    assert ENTRY.date().isoformat() not in seen


# ── PR-7's inputs: the market's own probability, on the same scale as `cost` ─────────

def test_m_mid_is_the_markets_probability_for_the_bucket(conn):
    """A bucket YES(>lo)+NO(>hi) is one binary. `Struct.fill_cost` prices it as
    `sum(side prices) - 1` because one of the two legs pays in every branch, so the same
    subtraction on MIDS is the market's implied probability that the bucket pays.

    Here the market says P(x>0.1)=0.79 and P(x>0.2)=0.49, so P(0.1<x<=0.2) = 0.30.
    """
    d = ENTRY + timedelta(days=1)
    _candle(conn, "T0.1", d, 0.78, 0.80)          # mid 0.79
    _candle(conn, "T0.2", d, 0.48, 0.50)          # mid 0.49
    p = wf._mtm_path(conn, _bucket(), 1, ENTRY, CLOSE)[0]
    assert p["m_mid"] == pytest.approx(0.30, abs=1e-9)


def test_m_mid_is_the_plain_mid_for_a_single_leg(conn):
    """No $1 subtraction on a single — `fill_cost` does not make it either."""
    st = Struct("single", (Leg("T0.1", "yes", 0.55, DEPTH),), fair=0.6, cost=0.55,
                max_loss=0.55, desc="YES >0.1")
    d = ENTRY + timedelta(days=1)
    _candle(conn, "T0.1", d, 0.78, 0.80)
    assert wf._mtm_path(conn, st, 1, ENTRY, CLOSE)[0]["m_mid"] == pytest.approx(0.79)


def test_m_mid_uses_the_no_side_for_a_no_leg(conn):
    """A NO leg's price is 1 - yes mid, and `m_mid` must agree or the bucket identity
    above is an accident of the numbers rather than the arithmetic."""
    st = Struct("single", (Leg("T0.2", "no", 0.30, DEPTH),), fair=0.75, cost=0.30,
                max_loss=0.30, desc="NO >0.2")
    d = ENTRY + timedelta(days=1)
    _candle(conn, "T0.2", d, 0.48, 0.50)          # yes mid 0.49 -> no mid 0.51
    assert wf._mtm_path(conn, st, 1, ENTRY, CLOSE)[0]["m_mid"] == pytest.approx(0.51)


def test_a_one_sided_entry_book_has_no_mid_rather_than_a_touch():
    """`enumerate_structs` builds a YES leg off `yes_ask` alone and a NO leg off
    `yes_bid` alone, so a struct can exist with only one side quoted. Falling back to the
    touch would manufacture half a spread of drawdown on a position that never moved —
    which is precisely the effect PR-7 is testing for, so it must read as missing."""
    st = _bucket()
    assert wf._struct_mid(st, {"T0.1": _q(None, 0.80), "T0.2": _q(0.48, 0.50)}) is None
    assert wf._struct_mid(st, {"T0.1": _q(0.78, 0.80)}) is None, "absent leg"
    assert wf._struct_mid(st, {"T0.1": _q(0.78, 0.80),
                               "T0.2": _q(0.48, 0.50)}) == pytest.approx(0.30)


def test_m_mid_and_the_hold_edge_agree_on_what_the_market_thinks(conn):
    """The two are the same decomposition seen from opposite ends: our fair minus the
    market's price IS the structure's holding edge (#141). If these ever disagree, one of
    the two is summing the wrong sides."""
    st = _bucket()
    d = ENTRY + timedelta(days=1)
    _candle(conn, "T0.1", d, 0.78, 0.80)
    _candle(conn, "T0.2", d, 0.48, 0.50)
    p = wf._mtm_path(conn, st, 1, ENTRY, CLOSE, pmf_for=lambda _d: {"pmf": PMF},
                     leg_meta=META)[0]
    fair_bucket = 0.60 - 0.25                     # P(0.1 < x <= 0.2) under PMF
    assert p["hold_edge"] == pytest.approx(fair_bucket - p["m_mid"], abs=1e-6)


def test_exit_px_ok_mirrors_the_two_cent_floor(conn):
    st = Struct("single", (Leg("T0.1", "yes", 0.05, DEPTH),), fair=0.6, cost=0.05,
                max_loss=0.05, desc="YES penny")
    d = ENTRY + timedelta(days=1)
    _candle(conn, "T0.1", d, 0.01, 0.03)          # bid 0.01 - SLIP -> floored at 0.01
    p = wf._mtm_path(conn, st, 1, ENTRY, CLOSE, pmf_for=lambda _d: {"pmf": PMF},
                     leg_meta=META)[0]
    assert p["exit_px_ok"] is False
    conn.execute("DELETE FROM candles")
    _candle(conn, "T0.1", d, 0.10, 0.12)          # 0.10 - SLIP = 0.09 >= 0.02
    p = wf._mtm_path(conn, st, 1, ENTRY, CLOSE, pmf_for=lambda _d: {"pmf": PMF},
                     leg_meta=META)[0]
    assert p["exit_px_ok"] is True
    assert p["mtm"] == pytest.approx(
        (0.10 - SLIP - 0.05) - taker_fee(0.05, 1) - taker_fee(0.10 - SLIP, 1), abs=1e-6)
