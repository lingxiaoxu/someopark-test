"""fed/0.3.0 — the move at ONE meeting, priced at the right horizon.

Regression for the two archived losers that were genuine model error rather than execution
cost: KXFEDDECISION 2027-03 (-$0.92) and 2026-12 (-$0.75). The 2027-03 open was made
against H0 = 0.0137 — a 1.4% chance the Fed holds at a meeting 20 months out, against a
measured unconditional hold rate of 0.780 over 141 meetings. Three defects stacked:

  1. the market prior classified the LEVEL in period P against today's target, so
     P(no change at that meeting) was really P(20 months of drift nets to exactly zero);
  2. the FF-futures source recorded the NEXT meeting's move and returned it for every
     period, which is why 2027-06 through 2027-12 printed byte-identical distributions;
  3. nothing widened with horizon.

Each test below pins one of those, plus the two numerical traps found while fixing them:
the day-weighted solve levering late-month meetings, and Kalshi changing strike sets
between periods.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest.calendars import CALENDARS
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model import fed
from prediction_market_macro.model.features import FeatureStore

ASOF = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
MID = 3.625                      # DFEDTARU 3.75 upper bound → corridor midpoint


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


def _meeting(period: str) -> datetime:
    return next(e.scheduled_ts for e in CALENDARS["FOMC"] if e.period == period)


def _ins(conn, sid, event, value, kt):
    conn.execute("INSERT OR IGNORE INTO fred_obs VALUES(?,?,?,?,?,?)",
                 (sid, event, value, kt.date().isoformat(), kt.isoformat(),
                  kt.isoformat()))


def _seed_rates(conn, years: int = 9):
    """Daily DFEDTARU + DGS2 back from ASOF, with a handful of real target moves.

    Long enough for _base_rates (260 rows) and _dgs2_path (756 overlapping days, 500 with
    a 1y-ahead realisation).
    """
    import math as _m
    d0 = ASOF - timedelta(days=365 * years)
    ub = 2.25                                            # + 6 x 25bp lands on 3.75 today
    for i in range(365 * years):
        day = d0 + timedelta(days=i)
        kt = day + timedelta(hours=20)
        if i in (400, 800, 1200, 1600, 2000, 2400):      # 6 moves over the span
            ub = round(ub + 0.25, 2)
        _ins(conn, "DFEDTARU", day.date().isoformat(), ub, kt)
        # the 2y slope has to actually VARY or the drift regression is singular
        _ins(conn, "DGS2", day.date().isoformat(),
             round(ub + 0.30 + 0.45 * _m.sin(i / 180.0), 3), kt)
    conn.commit()


def _seed_macro(conn):
    """UNRATE + CPILFESL monthly history for _rule_probs."""
    d = datetime(2016, 1, 1, tzinfo=timezone.utc)
    lvl = 260.0
    while d < ASOF:
        kt = d + timedelta(days=45)
        if kt <= ASOF:
            _ins(conn, "UNRATE", d.date().isoformat(), 4.0, kt)
            _ins(conn, "CPILFESL", d.date().isoformat(), round(lvl, 3), kt)
        lvl *= 1.0018
        d = (d.replace(day=28) + timedelta(days=8)).replace(day=1)
    conn.commit()


def _zq(conn, y: int, m: int, implied: float):
    day = datetime(2026, 7, 31, tzinfo=timezone.utc)
    conn.execute("INSERT OR IGNORE INTO fut_daily VALUES(?,?,?,?,?,?,?,?,?)",
                 (fed._zq_root(y, m), day.date().isoformat(), 0.0, 0.0, 0.0,
                  round(100.0 - implied, 4), 1.0, day.isoformat(), day.isoformat()))
    conn.commit()


def _flat_strip(conn, rate: float = MID):
    for y, m in ((2026, 8), (2026, 9), (2026, 10), (2026, 11)):
        _zq(conn, y, m, rate)


# ── 1. the FF chain must price the meeting it was asked about ────────────────

def test_ff_path_prices_the_requested_meeting_not_the_next_one(conn):
    """v0.2 recorded exp_move only for meetings[0] and returned it for every period.

    A strip that is flat through November and steps +25bp at the December meeting must
    put that 25bp on December and nothing on September or October.
    """
    _seed_rates(conn)
    _flat_strip(conn)
    # Dec 2026: meeting on the 9th, so 29% of the month is pre-meeting at MID and 71%
    # post-meeting at MID+0.25 — the levered solve has to recover exactly +25bp.
    w_pre = 9 / 31
    _zq(conn, 2026, 12, w_pre * MID + (1 - w_pre) * (MID + 0.25))
    fs = FeatureStore(conn)
    moves = {p: fed._ff_path(fs, ASOF, _meeting(p))[1]
             for p in ("2026-09", "2026-10", "2026-12")}
    assert moves["2026-09"] == pytest.approx(0.0, abs=1e-6)
    assert moves["2026-10"] == pytest.approx(0.0, abs=1e-6)
    # 1e-3: the fixture quantises the ZQ close to 4dp and the Dec solve
    # divides that by 0.71
    assert moves["2026-12"] == pytest.approx(0.25, abs=1e-3)


def test_ff_path_reads_a_late_month_meeting_off_the_following_contract(conn):
    """The Oct-2026 meeting is on the 28th: 9.7% of the month is post-meeting, so the
    day-weighted solve divides by 0.097 and levers every upstream error ~10x. That
    compounding is what let the chain price a +50bp hike at Mar-2027 off a strip that
    only rises 46bp in total. November has no meeting, so its contract quotes the
    post-October rate outright — use it.
    """
    _seed_rates(conn)
    _flat_strip(conn)
    # October's own contract is deliberately absurd; November says the rate is unchanged.
    conn.execute("UPDATE fut_daily SET close=? WHERE root=?",
                 (100.0 - 9.99, fed._zq_root(2026, 10)))
    conn.commit()
    pre, move, _ = fed._ff_path(FeatureStore(conn), ASOF, _meeting("2026-10"))
    assert pre == pytest.approx(MID, abs=1e-6)
    assert move == pytest.approx(0.0, abs=1e-6), "fell back to the levered solve"


def test_ff_path_refuses_to_price_what_the_strip_cannot_reach(conn):
    """No ZQ contract past the target ⇒ None, never a stale copy of a nearer meeting.

    Without this the DGS2 fallback never engages and every 2027-H2 meeting inherits
    September's number.
    """
    _seed_rates(conn)
    _flat_strip(conn)
    fs = FeatureStore(conn)
    assert fed._ff_path(fs, ASOF, _meeting("2027-06"))[1] is None
    # and with November removed, October's levered solve is refused rather than run
    conn.execute("DELETE FROM fut_daily WHERE root=?", (fed._zq_root(2026, 11),))
    conn.commit()
    assert fed._ff_path(FeatureStore(conn), ASOF, _meeting("2026-10"))[1] is None


# ── 2. the market prior must difference two ladders, not compare to today ────

def _ladder(conn, tok: str, strikes, survival):
    """A KXFED ladder whose devigged survival curve is exactly `survival`."""
    for k, s in zip(strikes, survival):
        tk = f"KXFED-{tok}-T{k}"
        conn.execute(
            "INSERT OR IGNORE INTO contracts(ticker, event_ticker, series, period,"
            " floor_strike, strike_type, close_time, first_seen_ts)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (tk, f"KXFED-{tok}", "KXFED", tok, k, "greater_or_equal",
             ASOF.isoformat(), ASOF.isoformat()))
        conn.execute("INSERT OR REPLACE INTO quotes(ts, ticker, yes_bid, yes_ask,"
                     " bid_depth, ask_depth) VALUES(?,?,?,?,?,?)",
                     ((ASOF - timedelta(hours=1)).isoformat(), tk,
                      round(max(s - 0.005, 0.01), 4), round(min(s + 0.005, 0.99), 4),
                      500.0, 500.0))
    conn.commit()


def test_market_move_differences_consecutive_meetings(conn):
    """Two identical ladders on consecutive meetings mean NO move at the later one.

    v0.2 read the later ladder's level against today's target instead, so an identical
    pair still produced whatever cumulative drift the market had priced in between.
    """
    _seed_rates(conn)
    ks = [3.25, 3.50, 3.75, 4.00, 4.25]
    sv = [0.97, 0.85, 0.50, 0.15, 0.03]
    _ladder(conn, "26OCT", ks, sv)
    _ladder(conn, "26DEC", ks, sv)
    mv, _ = fed._market_move(FeatureStore(conn), ASOF, "2026-12", _meeting("2026-12"))
    assert mv == pytest.approx(0.0, abs=1e-9)


def test_mismatched_strike_sets_do_not_manufacture_a_cut(conn):
    """The live trap: on 2026-08-04 Kalshi quoted the 2026 KXFED ladders over 2.75..5.25
    and the 2027 ones over 0.00..4.25. Differencing the raw means across that boundary
    invented a -41bp cut at the Jan-2027 meeting purely from the change in coverage.

    Here both periods carry the SAME distribution over the overlap; only the strike
    coverage differs. The answer must be ~0, and the pre-fix raw-mean difference (which
    the second assert reproduces) must not be what comes out.
    """
    _seed_rates(conn)
    # identical over the overlap; only the quoted COVERAGE differs, exactly as live
    common_k = [3.25, 3.50, 3.75, 4.00, 4.25]
    common_s = [0.97, 0.85, 0.60, 0.35, 0.18]
    _ladder(conn, "26DEC", common_k + [4.50, 4.75, 5.00, 5.25],
            common_s + [0.13, 0.09, 0.05, 0.02])
    _ladder(conn, "27JAN", [2.50, 2.75, 3.00] + common_k,
            [0.999, 0.995, 0.99] + common_s)
    fs = FeatureStore(conn)
    mv, _ = fed._market_move(fs, ASOF, "2027-01", _meeting("2027-01"))
    assert mv is not None
    assert abs(mv) < 0.05, f"strike coverage leaked into the move: {mv:+.4f}"
    # the raw (unwindowed) means really do disagree — this is the bug being suppressed
    p_jan, _ = fed._level_pmf(fs, ASOF, "2027-01")
    p_dec, _ = fed._level_pmf(fs, ASOF, "2026-12")
    raw = (fed._conditional_mean(p_jan, -9e9, 9e9)[0]
           - fed._conditional_mean(p_dec, -9e9, 9e9)[0])
    assert abs(raw) > 0.05, "test no longer exercises the coverage mismatch"


def test_market_move_is_none_when_the_prior_meeting_is_unpriced(conn):
    """No predecessor ladder ⇒ no derivable move. Guessing one is how 2027-03 came to be
    quoted at P(hold)=0.045 alongside a 24% double-cut AND a 36.5% double-hike."""
    _seed_rates(conn)
    _ladder(conn, "27MAR", [3.25, 3.50, 3.75, 4.00], [0.97, 0.85, 0.50, 0.15])
    mv, _ = fed._market_move(FeatureStore(conn), ASOF, "2027-03", _meeting("2027-03"))
    assert mv is None


# ── 3. horizon shrink: the floor under H0 ────────────────────────────────────

def test_base_rate_matches_the_measured_hold_frequency(conn):
    """Holds are the unobserved bulk — DFEDTARU only records moves — so the meeting count
    is reconstructed at 8/year. On the live store that is 31 changes over ~141 meetings,
    H0 = 0.780. The fixture seeds 6 moves over 9 years (~72 meetings)."""
    _seed_rates(conn)
    base, _ = fed._base_rates(FeatureStore(conn), ASOF)
    assert sum(base.values()) == pytest.approx(1.0)
    assert base["H0"] == pytest.approx(1 - 6 / 72, abs=0.02)
    assert base["H25"] == pytest.approx(6 / 72, abs=0.02)


def test_h0_never_collapses_at_a_long_horizon(conn):
    """The bug that cost -$0.92 and -$0.75, stated as an invariant.

    With no market and no futures reaching them, the far meetings are pooled from the rule
    alone and shrunk toward the base rate; H0 must stay in the neighbourhood of the
    unconditional hold frequency instead of falling to 0.0137.
    """
    _seed_rates(conn)
    _seed_macro(conn)
    base, _ = fed._base_rates(FeatureStore(conn), ASOF)
    for period in ("2027-06", "2027-09", "2027-12"):
        p = fed.predict(conn, ASOF, period)
        h0 = p.dist.probs["H0"]
        assert h0 > 0.5, f"{period}: H0={h0:.4f} collapsed"
        # the shrink alone guarantees at least lambda * base
        assert h0 >= p.inputs["shrink_lambda"] * base["H0"] - 1e-6


def test_far_meetings_are_not_byte_identical(conn):
    """2027-06 through 2027-12 used to print the same distribution to the last digit,
    because all three inherited the next meeting's futures number. Distinct meetings that
    are 6 months apart must differ."""
    _seed_rates(conn)
    _seed_macro(conn)
    probs = [tuple(round(fed.predict(conn, ASOF, p).dist.probs[k], 6) for k in fed.CATS)
             for p in ("2027-06", "2027-09", "2027-12")]
    assert len(set(probs)) == 3, probs


def test_shrink_is_monotone_in_horizon(conn):
    lams = [fed._shrink_lambda(ASOF, _meeting(p))
            for p in ("2026-09", "2026-12", "2027-03", "2027-06", "2027-12")]
    assert lams == sorted(lams)
    assert 0.0 < lams[0] < 0.25 and lams[-1] > 0.6


def test_kxfed_ladder_is_anchored_on_the_pre_meeting_rate(conn):
    """The level product must sit on the rate going INTO the meeting, not on today's.

    Anchoring a 2027 KXFED strike on today's target is the same level/move confusion
    running backwards — it would price every far strike as if no move had happened in
    between. Here the strip steps +25bp at the December meeting, so the December ladder
    must sit a step above the September one.
    """
    _seed_rates(conn)
    _seed_macro(conn)
    _flat_strip(conn)
    w_pre = 9 / 31
    _zq(conn, 2026, 12, w_pre * MID + (1 - w_pre) * (MID + 0.25))
    _zq(conn, 2027, 1, MID + 0.25)
    _zq(conn, 2027, 2, MID + 0.25)          # Feb has no meeting: it reads post-Jan direct
    sep = fed.predict_kxfed(conn, ASOF, "2026-09")
    dec = fed.predict_kxfed(conn, ASOF, "2027-01")
    assert dec.inputs["anchor_ub"] > sep.inputs["anchor_ub"] + 0.2
    assert sep.inputs["current_ub"] == dec.inputs["current_ub"] == 3.75


def test_predict_is_pit_clean_across_all_new_sources(conn):
    """Canary over the v0.3 sources specifically: the DGS2 regression and the base-rate
    count both walk long histories, and both must be blind to vintages after asof."""
    _seed_rates(conn)
    _seed_macro(conn)
    _flat_strip(conn)
    before = fed.predict(conn, ASOF, "2027-06")
    fut = ASOF + timedelta(days=5)
    _ins(conn, "DFEDTARU", (ASOF + timedelta(days=1)).date().isoformat(), 9.99, fut)
    _ins(conn, "DGS2", (ASOF + timedelta(days=1)).date().isoformat(), 9.99, fut)
    conn.execute("INSERT OR IGNORE INTO fut_daily VALUES(?,?,?,?,?,?,?,?,?)",
                 (fed._zq_root(2026, 9), (ASOF + timedelta(days=1)).date().isoformat(),
                  0.0, 0.0, 0.0, 50.0, 1.0, fut.isoformat(), fut.isoformat()))
    conn.commit()
    after = fed.predict(conn, ASOF, "2027-06")
    assert before.dist.probs == after.dist.probs
    assert before.data_horizon <= before.asof
