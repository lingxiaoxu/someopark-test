"""The composition layer: which column goes where, what clock an event closes on, and the
weekly-to-daily expansion the energy models need.

The expensive half of `build` — panel, generator, worlds, book — is covered by the tests of
those modules. What is tested here is the glue, which is where the mistakes that survive
review live: a column generated and never written, a weekly stamp landing on the wrong
weekday, a futures path handed to a model that measures daily volatility.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.research.synth import build as BD
from prediction_market_macro.research.synth import panel as P
from prediction_market_macro.util.periods import kalshi_period_to_key

UTC = timezone.utc


# ── the column -> table map ──────────────────────────────────────────────────
def test_every_generated_column_of_every_mapped_panel_has_somewhere_to_go():
    """A column the generator pays to produce and nobody writes is a stretch of history
    that silently stops at the splice for every model reading it."""
    for name in BD.SINKS:
        got = BD._sinks(name)
        assert set(got) == {c.name for c in P.PANELS[name].gen_columns}


def test_a_panel_with_an_unmapped_generated_column_is_refused_not_partially_written():
    BD.SINKS["_probe"] = {}
    P.PANELS["_probe"] = P.CLAIMS_WEEKLY
    try:
        with pytest.raises(ValueError, match="nowhere to write"):
            BD._sinks("_probe")
    finally:
        del BD.SINKS["_probe"], P.PANELS["_probe"]


def test_every_settling_series_names_a_column_its_panel_actually_generates():
    for series, st in BD.SETTLES.items():
        assert st.how in BD._HOW, f"{series}: unknown transform {st.how!r}"
        assert st.column in {c.name for c in P.PANELS[st.panel].gen_columns}, \
            f"{series} settles on {st.column!r}, which {st.panel} does not generate"


# ── the settlement transform ────────────────────────────────────────────────
def test_outcome_path_reproduces_each_transform_from_a_known_level_series():
    lv = pd.Series([100.0, 101.0, 102.01],
                   index=pd.date_range("2026-01-01", periods=3, freq="MS"))
    assert BD.outcome_path(lv, BD.Settle("p", "c", "level")).iloc[-1] == pytest.approx(102.01)
    assert BD.outcome_path(lv, BD.Settle("p", "c", "diff", scale=1000.0)).iloc[-1] == \
        pytest.approx(1010.0)
    assert BD.outcome_path(lv, BD.Settle("p", "c", "pct100")).iloc[-1] == \
        pytest.approx(1.0, abs=1e-9)


def test_outcome_path_needs_the_lookback_and_says_nothing_rather_than_guessing():
    """A transform that reads backwards has no answer for the first period it is given. It
    must come back NaN there so `build` skips the event, not 0.0 — a fabricated -100% MoM
    would settle every leg NO and look like an ordinary bad month."""
    lv = pd.Series([100.0, 101.0], index=pd.date_range("2026-01-01", periods=2, freq="MS"))
    for how in ("diff", "pct100", "yoy100"):
        assert np.isnan(BD.outcome_path(lv, BD.Settle("p", "c", how)).iloc[0])


def test_outcome_path_refuses_a_transform_it_does_not_implement():
    with pytest.raises(ValueError, match="unknown transform"):
        BD.outcome_path(pd.Series([1.0]), BD.Settle("p", "c", "log_change"))


def test_level_step_aligns_the_grid_only_where_a_grid_exists():
    """Rounding PAYEMS' thousands to 1.0 puts the settlement on the 1000-job ladder exactly.
    Rounding a CPI index of ~320 to 0.1 would move the MoM it implies by ~0.03pp — a third
    of a bucket of noise injected into the answer, so that one is left alone."""
    assert BD.SETTLES["KXWTIW"].level_step(0.01) == pytest.approx(0.01)
    assert BD.SETTLES["KXPAYROLLS"].level_step(1000.0) == pytest.approx(1.0)
    assert BD.SETTLES["KXCPI"].level_step(0.1) is None
    assert BD.SETTLES["KXCPIYOY"].level_step(0.1) is None


def test_aaa_is_not_generatable_and_says_so_rather_than_inventing_an_outcome():
    """KXAAAGASW settles on the AAA national average: 21 observations, all after
    2026-07-31. Setting AAA equal to the generated GASREGW would hand its drift regression
    a target that is identically zero; resampling the gap independently would destroy the
    dependence the model exists to exploit. Both fabricate the answer."""
    assert "KXAAAGASW" not in BD.SETTLES
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ValueError, match="no generated settlement column"):
        BD.build(conn, "KXAAAGASW", datetime(2026, 5, 30, tzinfo=UTC),
                 donors=[], out_dir="/tmp/never-created")


# ── clocks measured from the db ──────────────────────────────────────────────
def _contract(conn, series, ticker, close):
    conn.execute("INSERT INTO contracts(ticker, series, event_ticker, period, close_time,"
                 " first_seen_ts) VALUES(?,?,?,?,?,'x')",
                 (ticker, series, series, "P", close))


def test_fred_weekday_reads_each_series_own_convention(tmp_path):
    """ICSA dates its weeks on the Saturday, GASREGW on the Monday. A weekly panel stamps
    W-SAT for both, and writing GASREGW on Saturday would date it five days early — then
    `publication_lag`, measured on Monday-dated prints, stamps its knowledge time five days
    early too. A point-in-time leak assembled out of two correct pieces."""
    conn = init_db(tmp_path / "w.db")
    rows = []
    for i in range(6):
        rows.append(("ICSA", f"2026-06-{6+7*i:02d}", 220000.0))       # Saturdays
        rows.append(("GASREGW", f"2026-06-{1+7*i:02d}", 3.10))        # Mondays
    for sid, ev, v in rows:
        conn.execute("INSERT INTO fred_obs(sid, event_time, value, vintage_date,"
                     " knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,'x')",
                     (sid, ev, v, ev, ev + "T13:00:00+00:00"))
    conn.commit()
    assert BD._fred_weekday(conn, "ICSA") == 5
    assert BD._fred_weekday(conn, "GASREGW") == 0


def test_on_weekday_lands_inside_the_bucket_it_was_given():
    """The W-SAT bucket is [d-6, d], so its Monday is d-5 and its Saturday is d itself —
    never the next week's."""
    sat = pd.DatetimeIndex(["2026-06-06", "2026-06-13"])              # Saturdays
    assert list(BD._on_weekday(sat, 5)) == list(sat)
    mon = BD._on_weekday(sat, 0)
    assert [t.weekday() for t in mon] == [0, 0]
    assert list(mon) == list(pd.DatetimeIndex(["2026-06-01", "2026-06-08"]))
    for a, b in zip(mon, sat):
        assert b - pd.Timedelta(days=6) <= a <= b


def test_knowable_at_reproduces_the_real_claims_release_from_the_bucket_it_dates():
    """ICSA dates the week ending Saturday 25 July and DoL releases it Thursday 30 July at
    12:30 UTC. That is the whole clock: bucket end -> its own weekday -> plus the measured
    lag -> at the measured hour."""
    ck = BD.Clock("fred", 5, 12, 5)
    got = BD.knowable_at(P.CLAIMS_WEEKLY, ck, pd.Timestamp("2026-07-25"))
    assert (got.year, got.month, got.day, got.hour) == (2026, 7, 30, 12)
    assert got.weekday() == 3 and got.tzinfo is UTC


def test_knowable_at_puts_a_futures_settle_on_the_friday_of_its_w_sat_week():
    """KXWTIW settles on a Friday session; the W-SAT bucket that session belongs to ends the
    next day. Dating the bar on the Saturday would put a close on a day the market is shut
    and hand `_gbm_futures` a bar that never traded."""
    ck = BD.Clock("fut", 0, 20, None)
    got = BD.knowable_at(P.ENERGY_WEEKLY, ck, pd.Timestamp("2026-05-30"))
    assert (got.year, got.month, got.day, got.hour) == (2026, 5, 29, 20)
    assert got.weekday() == 4


def test_knowable_at_leaves_a_monthly_bucket_where_fred_already_dates_it():
    """FRED dates July CPI 2026-07-01 and BLS releases it 12 August. A monthly panel labels
    its buckets the same way, so the weekday shift a weekly panel needs would be a bug here
    — and `Clock.weekday` is None precisely so it cannot be applied by accident."""
    ck = BD.Clock("fred", 42, 12, None)
    got = BD.knowable_at(P.INFLATION_MONTHLY, ck, pd.Timestamp("2026-07-01"))
    assert (got.year, got.month, got.day, got.hour) == (2026, 8, 12, 12)


def test_the_close_is_before_the_answer_exists_by_construction():
    """The one invariant the whole clock layer exists to guarantee. In the first build this
    held on the weekly series only because a tabulated 12:25 close happened to fall before a
    measured 12:00 release SEVEN DAYS LATER; on the monthly series the same arithmetic put
    the close 25 minutes AFTER the release, i.e. the model would have read the settlement
    value before quoting it."""
    for spec, ck, per in (
            (P.CLAIMS_WEEKLY, BD.Clock("fred", 5, 12, 5), pd.Timestamp("2026-07-25")),
            (P.ENERGY_WEEKLY, BD.Clock("fut", 0, 20, None), pd.Timestamp("2026-05-30")),
            (P.INFLATION_MONTHLY, BD.Clock("fred", 42, 12, None),
             pd.Timestamp("2026-07-01")),
            (P.LABOR_MONTHLY, BD.Clock("fred", 37, 12, None), pd.Timestamp("2026-07-01"))):
        k = BD.knowable_at(spec, ck, per)
        assert k - BD.CLOSE_LEAD < k
        assert BD.CLOSE_LEAD.total_seconds() > 0


def test_token_follows_each_series_own_naming_convention():
    """Not cosmetic: `claims.predict` recovers its target week as `period - 5 days` and
    `cpi.predict` reads `period` as a reference month. A token in the wrong convention is a
    model forecasting a different period than the one that settles."""
    close = datetime(2026, 7, 30, 11, 55, tzinfo=UTC)
    assert BD._token(P.CLAIMS_WEEKLY, close, pd.Timestamp("2026-07-25")) == "26JUL30"
    assert BD._token(P.INFLATION_MONTHLY, datetime(2026, 8, 12, 11, 55, tzinfo=UTC),
                     pd.Timestamp("2026-07-01")) == "26JUL"


def test_claims_token_hands_the_model_back_the_week_it_settles_on():
    """The round trip that matters: bucket -> close -> token -> the model's own target-week
    arithmetic must land on the bucket again."""
    per = pd.Timestamp("2026-07-25")
    ck = BD.Clock("fred", 5, 12, 5)
    close = BD.knowable_at(P.CLAIMS_WEEKLY, ck, per) - BD.CLOSE_LEAD
    key = kalshi_period_to_key(BD._token(P.CLAIMS_WEEKLY, close, per))
    assert pd.Timestamp(key) - pd.Timedelta(days=5) == per


# ── the settlement checker's own logic ───────────────────────────────────────
# `verify_settle` is checked against the production db, not here: its claim is about real
# vintages and real ladders and no fixture can stand in for those. What IS tested here is
# the checker's own arithmetic, because both bugs it produced on first run were its own —
# it reported 20+ correct WTI settlements as inconsistent, and a $4 discrepancy that did
# not exist. A checker that cries wolf is worse than no checker.
def test_verify_settle_refuses_a_series_it_has_no_transform_for():
    with pytest.raises(ValueError, match="not a generated series"):
        BD.verify_settle(sqlite3.connect(":memory:"), "KXFED",
                         datetime(2026, 8, 21, tzinfo=UTC))


def test_verify_settle_refuses_a_scale_that_disagrees_with_the_panel():
    """For a `diff` series the panel increment IS the settlement value — `labor_monthly`
    carries PAYEMS at scale 1000 precisely so that `inc` is in jobs. If `Settle` claimed a
    different conversion the two would silently differ by a thousand and every event would
    settle NO, which reads like a bad month rather than like a bug."""
    BD.SETTLES["_probe"] = BD.Settle("labor_monthly", "payems", "diff", scale=1.0)
    try:
        with pytest.raises(ValueError, match="same conversion"):
            BD.verify_settle(sqlite3.connect(":memory:"), "_probe",
                             datetime(2026, 8, 21, tzinfo=UTC))
    finally:
        del BD.SETTLES["_probe"]


def test_panel_period_reads_a_monthly_token_as_its_reference_month():
    assert BD._panel_period(P.INFLATION_MONTHLY, None, pd.DatetimeIndex([]),
                            "2026-07-01", None) == pd.Timestamp("2026-07-01")


def test_panel_period_finds_the_week_a_real_close_belongs_to():
    idx = pd.date_range("2026-07-04", periods=6, freq="W-SAT")
    ck = BD.Clock("fred", 5, 12, 5)
    per = pd.Timestamp("2026-07-25")
    close = BD.knowable_at(P.CLAIMS_WEEKLY, ck, per) - BD.CLOSE_LEAD
    assert BD._panel_period(P.CLAIMS_WEEKLY, ck, idx, "2026-07-30", close) == per


def test_panel_period_says_nothing_when_the_bucket_is_simply_absent():
    """A panel built today ends where its SHORTEST column ends, so the bucket a recent event
    settles on may not be in the index at all. Snapping to the nearest one instead put a
    26AUG14 WTI event on the 08-08 week and reported a $4 discrepancy that was entirely the
    checker's doing."""
    idx = pd.date_range("2026-07-04", periods=3, freq="W-SAT")       # ends 2026-07-18
    ck = BD.Clock("fred", 5, 12, 5)
    close = BD.knowable_at(P.CLAIMS_WEEKLY, ck,
                           pd.Timestamp("2026-08-01")) - BD.CLOSE_LEAD
    assert BD._panel_period(P.CLAIMS_WEEKLY, ck, idx, "2026-08-06", close) is None


# ── the weekly -> daily bridge ───────────────────────────────────────────────
def _weekly(vals, start="2026-06-05"):
    return pd.Series(vals, index=pd.date_range(start, periods=len(vals), freq="7D"))


def test_bridge_pins_every_weekly_close_exactly_because_that_is_what_settles():
    """The Friday close IS the settlement value for KXWTIW and KXNATGASW. If the bridge
    moved it even by a float epsilon, the number the world holds and the number the event
    settles on would be different objects."""
    wk = _weekly([80.0, 82.5, 79.25, 85.0])
    got = BD._daily_bridge(wk, 0.02, np.random.default_rng(0))
    for ts, v in wk.items():
        assert got[ts] == pytest.approx(v, abs=0.0), f"{ts} moved"


def test_bridge_fills_business_days_and_skips_weekends():
    wk = _weekly([80.0, 82.0])
    got = BD._daily_bridge(wk, 0.02, np.random.default_rng(0))
    assert len(got) > 2, "a bridge that adds no days is a straight line by another name"
    assert all(t.weekday() < 5 for t in got.index if t not in set(wk.index))


def test_bridge_delivers_the_daily_volatility_a_straight_line_would_destroy():
    """`energy._gbm_futures` takes sigma from the MAD of DAILY log returns. On a linearly
    interpolated path those are constant within a week, the MAD collapses toward zero, and
    the model quotes a market it is certain about — so the synthetic world looks wildly
    profitable for reasons that are entirely an artifact of interpolation."""
    wk = _weekly([80.0] * 14)                    # flat weekly: all the motion is the bridge
    sigma = 0.02
    got = BD._daily_bridge(wk, sigma, np.random.default_rng(1))
    r = np.diff(np.log(got.values))
    assert r.std() > 0.5 * sigma, f"bridge returns sd {r.std():.4f} against sigma {sigma}"
    flat = wk.reindex(pd.bdate_range(wk.index[0], wk.index[-1])).interpolate()
    assert np.diff(np.log(flat.values)).std() < 0.1 * r.std()


def test_bridge_scales_with_sigma_so_a_calm_root_stays_calm():
    wk = _weekly([80.0] * 10)
    calm = np.diff(np.log(BD._daily_bridge(wk, 0.005, np.random.default_rng(2)).values))
    wild = np.diff(np.log(BD._daily_bridge(wk, 0.05, np.random.default_rng(2)).values))
    assert wild.std() > 4 * calm.std()


def test_bridge_refuses_a_series_with_nothing_to_bridge_to():
    with pytest.raises(ValueError, match="at least an anchor"):
        BD._daily_bridge(_weekly([80.0]), 0.02, np.random.default_rng(0))


def test_sigma_daily_refuses_a_root_it_cannot_measure(tmp_path):
    """Better to stop than to bridge on a made-up volatility: the sigma chosen here is the
    daily motion every energy model will read back out of the world."""
    conn = init_db(tmp_path / "w.db")
    with pytest.raises(ValueError, match="cannot scale a bridge"):
        BD._sigma_daily(conn, "CL", datetime(2026, 6, 1, tzinfo=UTC))


def test_sigma_daily_is_point_in_time(tmp_path):
    """Bars stamped knowable after the asof are the synthetic future — using them to scale
    the bridge would let the world's own volatility inform its own path."""
    conn = init_db(tmp_path / "w.db")
    rng = np.random.default_rng(3)
    px = 80.0
    for i, d in enumerate(pd.bdate_range("2026-01-01", periods=120)):
        px *= float(np.exp(rng.standard_normal() * (0.005 if i < 60 else 0.05)))
        conn.execute("INSERT INTO fut_daily(root, event_time, open, high, low, close,"
                     " knowledge_time, first_seen_ts) VALUES('CL',?,?,?,?,?,?,'x')",
                     (d.date().isoformat(), px, px, px, px,
                      d.replace(hour=20).isoformat()))
    conn.commit()
    early = BD._sigma_daily(conn, "CL", datetime(2026, 3, 25, tzinfo=UTC))
    late = BD._sigma_daily(conn, "CL", datetime(2026, 7, 1, tzinfo=UTC))
    assert late > 3 * early, "the calm first half must not know about the wild second half"


# ── the scoring contract ─────────────────────────────────────────────────────
def test_score_matrix_returns_no_partial_rows(tmp_path):
    """`pnl_score.score_matrix`'s keep rule, restated here because S5 and S6 compare the two
    matrices directly: an event scored on some candidates but not others compares them on
    different samples, which is the bias the paired test exists to remove."""
    ev = BD.SynthEvent(series="KXWTIW", path=0, period="26JUN05", key="2026-06-05",
                       close=datetime(2026, 6, 5, 18, 30, tzinfo=UTC), outcome=80.0,
                       z_y=0.0, donor="x/y", world=str(init_db(tmp_path / "empty.db")
                                                       and tmp_path / "empty.db"))
    kept, mat = BD.score_matrix([ev], [{}, {"fut_vol_window": 10}])
    assert kept == [] and mat == []
