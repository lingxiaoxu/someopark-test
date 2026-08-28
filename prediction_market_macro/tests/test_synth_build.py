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


# ── the sub-monthly expansion (2026-08-21) ──────────────────────────────────
# `claims` enters labor_monthly and `gas_retail` enters inflation_monthly as `agg="mean"`
# of a WEEKLY FRED series. The generator produces one number per month for each; writing
# that one number as one observation would leave `payrolls`' icsa.rolling(4).mean() and
# `cpi._gas_effect`'s obs_frac = min(len(cur)/4.3, 1) reading a series that had gone
# quiet — a data outage, not a macro path. These pin the expansion that avoids it.
def _monthly(vals, start="2026-01-01"):
    return pd.Series([float(v) for v in vals],
                     index=pd.date_range(start, periods=len(vals), freq="MS"))


def test_sub_monthly_prints_average_back_to_the_generated_month():
    """The pin. The generator learned how claims co-moves with payems; a world whose ICSA
    does not aggregate to the generated `claims` has broken exactly that co-movement."""
    m = _monthly([220.0, 240.0, 210.0])
    wk = BD._sub_monthly(m, 5, 0.05, np.random.default_rng(0))
    got = wk.groupby(wk.index.to_period("M")).mean()
    for per, want in zip(m.index, m.values):
        assert got[per.to_period("M")] == pytest.approx(want, rel=1e-9)


def test_sub_monthly_lands_on_the_series_own_weekday_and_covers_every_week():
    m = _monthly([220.0, 240.0])
    wk = BD._sub_monthly(m, 5, 0.03, np.random.default_rng(1))
    assert {d.weekday() for d in wk.index} == {5}
    jan = pd.date_range("2026-01-01", "2026-01-31", freq="D")
    assert len(wk[:"2026-01-31"]) == sum(1 for d in jan if d.weekday() == 5)


def test_sub_monthly_actually_varies_within_the_month():
    """The failure mode of guessing sigma_within is a flat path, which makes every model
    reading the weekly series more certain than it has any right to be."""
    m = _monthly([220.0] * 3)
    flat = BD._sub_monthly(m, 5, 0.0, np.random.default_rng(2))
    wig = BD._sub_monthly(m, 5, 0.06, np.random.default_rng(2))
    assert flat.std() == pytest.approx(0.0, abs=1e-9)
    assert wig.std() > 2.0, "a 6% within-month sd on ~220 must show up as several units"


def test_sub_monthly_holds_real_prints_and_moves_the_residual_onto_the_free_weeks():
    """The straddling first month: weeks knowable before the splice are already in the
    world and rewriting them would be a PIT violation inside the synthetic history."""
    m = _monthly([220.0])
    days = [d for d in pd.date_range("2026-01-01", "2026-01-31", freq="D")
            if d.weekday() == 5]
    real = pd.Series([300.0], index=[pd.Timestamp(days[0])])
    wk = BD._sub_monthly(m, 5, 0.04, np.random.default_rng(3), fixed=real)
    assert pd.Timestamp(days[0]) not in wk.index, "a held week must not be rewritten"
    assert (wk.sum() + 300.0) / len(days) == pytest.approx(220.0, rel=1e-9)
    assert (wk < 220.0).all(), "the residual after a high held print must land low"


def test_sub_monthly_drops_the_pin_rather_than_spiking_one_week():
    """With fewer than two free weeks the pin puts the whole monthly residual on a single
    print. A visible unpinned month is better than an invented spike."""
    said = []
    m = _monthly([220.0])
    days = [d for d in pd.date_range("2026-01-01", "2026-01-31", freq="D")
            if d.weekday() == 5]
    real = pd.Series([300.0] * (len(days) - 1), index=[pd.Timestamp(d) for d in days[:-1]])
    wk = BD._sub_monthly(m, 5, 0.02, np.random.default_rng(4), fixed=real,
                         log=said.append)
    assert len(wk) == 1
    assert wk.iloc[0] == pytest.approx(220.0, rel=0.2), "left on the backbone, not spiked"
    assert said and "unpinned" in said[0]


def test_sub_monthly_slopes_toward_the_next_month_instead_of_stepping():
    """`payrolls` reads a 4-week rolling mean straight across the boundary, so two flat
    months are a fake claims shock on the first week of the second one. With sigma 0 the
    backbone IS the path, so the tilt that avoids it is exactly visible."""
    m = _monthly([200.0, 250.0, 300.0])
    wk = BD._sub_monthly(m, 5, 0.0, np.random.default_rng(5))
    jan, feb = wk[:"2026-01-31"], wk["2026-02-01":"2026-02-28"]
    assert jan.is_monotonic_increasing and feb.is_monotonic_increasing
    assert jan.iloc[-1] > 200.0 > jan.iloc[0], "January must lean toward February"
    assert feb.iloc[0] < 250.0 < feb.iloc[-1], "and February must lean back"
    gap = feb.iloc[0] - jan.iloc[-1]
    assert 0 < gap < 50.0, f"boundary step {gap:.1f} is no better than the flat 50"


def test_sigma_within_measures_deviation_from_the_month_not_week_to_week(tmp_path):
    """The week-to-week sd contains the monthly movement the generator already produces;
    double-counting it would inflate every synthetic claims path."""
    conn = init_db(tmp_path / "s.db")
    lvl = 200.0
    rng = np.random.default_rng(7)
    for d in pd.date_range("2021-01-02", "2026-01-02", freq="7D"):
        if d.day <= 7:
            lvl *= 1.05                                   # big MONTHLY steps
        v = lvl * float(np.exp(rng.standard_normal() * 0.01))   # small within-month
        conn.execute("INSERT INTO fred_obs(sid, event_time, value, vintage_date,"
                     " knowledge_time, first_seen_ts) VALUES('ICSA',?,?,?,?,'x')",
                     (d.date().isoformat(), v, d.date().isoformat(),
                      (d + pd.Timedelta(days=5)).isoformat()))
    conn.commit()
    got = BD._sigma_within(conn, "ICSA", datetime(2026, 1, 1, tzinfo=UTC))
    assert 0.002 < got < 0.03, f"within-month sd {got:.4f} picked up the monthly steps"


def test_sigma_within_refuses_rather_than_guessing_on_a_short_history(tmp_path):
    conn = init_db(tmp_path / "s.db")
    for d in pd.date_range("2025-12-06", periods=5, freq="7D"):
        conn.execute("INSERT INTO fred_obs(sid, event_time, value, vintage_date,"
                     " knowledge_time, first_seen_ts) VALUES('ICSA',?,220.0,?,?,'x')",
                     (d.date().isoformat(), d.date().isoformat(), d.isoformat()))
    conn.commit()
    with pytest.raises(ValueError, match="cannot measure"):
        BD._sigma_within(conn, "ICSA", datetime(2026, 1, 1, tzinfo=UTC))


def test_every_monthly_panel_maps_its_sub_monthly_columns_to_a_fred_sid():
    """The bug this closes: inflation_monthly generated four columns with no SINKS entry,
    so `build` raised 'nowhere to write them' for every monthly market."""
    for st in BD.SETTLES.values():
        panel = P.PANELS[st.panel]
        if panel.freq != "MS":
            continue
        sinks = BD._sinks(st.panel)
        for c in panel.gen_columns:
            if c.agg == "mean":
                assert sinks[c.name].kind == "fred", \
                    f"{st.panel}.{c.name} is a monthly mean of a sub-monthly source"


# ── #212: the four axes `_weekly` used to carry as one bit ──────────────────
def test_cadence_reproduces_exactly_what_the_weekly_boolean_decided():
    """The refactor's whole safety claim. Ten live series build through these five call
    sites, so every axis must return, for every panel that exists today, precisely what
    `spec.freq.upper().startswith("W")` used to hand that site. Asserted per panel rather
    than in aggregate so a failure names the panel it broke.

    `gdp_quarterly` is excluded BY NAME, not by a `freq != "QS"` filter, because the exclusion
    is a claim about history rather than about frequency: the boolean never saw this panel, so
    there is no prior answer for it to reproduce. Written this way so that a second weekly or
    monthly panel added later is still caught here instead of slipping through a frequency
    filter; the quarterly axes are pinned in the next test."""
    for name, spec in P.PANELS.items():
        if name == "gdp_quarterly":       # post-dates the boolean — see the docstring
            continue
        was_weekly = spec.freq.upper().startswith("W")
        cad = BD.cadence(spec)
        assert (cad.token == "close_date") is was_weekly, name       # _token, _panel_period
        assert cad.dates_within is was_weekly, name                  # clock's weekday
        assert (cad.expander == "sub_monthly") is (not was_weekly), name  # sub-monthly sinks
        assert cad.registry_cadence == ("weekly" if was_weekly else "monthly"), name


def test_cadence_separates_frequency_from_token_convention():
    """The reason the boolean had to go, stated as an assertion rather than a comment.
    KXGDP is quarterly *and* named for its release date (KXGDP-27JAN28), so it takes the
    weekly answer on the token axis and the monthly answer on the other two. No single bit
    can produce that combination, which is what makes the four axes independent rather than
    a renaming."""
    q = BD.CADENCES["QS"]
    w, m = BD.CADENCES["W"], BD.CADENCES["MS"]
    assert q.token == w.token == "close_date"
    assert q.dates_within is m.dates_within is False
    assert q.expander is w.expander is None
    assert q.registry_cadence == "quarterly"


def test_cadence_refuses_a_frequency_nobody_has_decided_the_axes_for():
    """A new frequency must not fall through to a default. Falling through is exactly how
    the old boolean would have handled quarterly — silently, as 'not weekly' — and it would
    have been wrong on the one axis that matters for KXGDP."""
    from dataclasses import replace
    with pytest.raises(ValueError, match="not one of"):
        BD.cadence(replace(P.PANELS["labor_monthly"], freq="B"))


def test_panel_period_search_window_comes_from_the_index_not_a_hardcoded_week():
    """`_panel_period`'s close-date branch searched +/-14 days at a half-week tolerance.
    Those are 'two periods' and 'half a period' written out for a weekly panel; on a
    quarterly index they would find no candidate at all. Read off the index, a weekly panel
    must still resolve exactly as before -- that is the bit-identity claim -- and a
    quarterly one must resolve at all."""
    import inspect
    src = inspect.getsource(BD._panel_period)
    assert "asi8" in src and "0.5 * step" in src, "the spacing generalisation was reverted"
    assert "Timedelta(days=14)" not in src


def test_build_refuses_a_series_whose_cadence_disagrees_with_its_panel():
    """`_token` names every generated event off the panel's frequency. A weekly SeriesSpec
    on a monthly panel would name each event wrong and settle it against the wrong period,
    silently — this raises instead."""
    import inspect
    src = inspect.getsource(BD.build)
    assert "settles off panel" in src, "the cadence guard was removed"


# ── #183: the generated nowcast, which is an INPUT rather than a print ──────
# KXGDP is the only series whose model does not read the generated column to price the
# event: `gdp.predict` reads a GDPNow vintage and treats A191RL1Q225SBEA only as the answer.
# So a world needs a forecast OF its own generated truth, and it has to be a forecast that
# behaves like GDPNow — a path that tightens toward the release, not a single number.
def test_the_nowcast_key_agrees_with_the_ingest_module_that_writes_the_real_rows(tmp_path):
    """`nowcast_vintages.event_time` holds a quarter STRING, and the only reason it does is
    that `ingest/nowcast._quarter_of` put it there. The generated rows sit in the same table
    and are read by the same query, so a disagreement here is not a formatting difference —
    the model would simply find no vintage for the quarter it was asked about, report "no
    vintage visible", and the build would score zero events as a modelling failure.

    Pinned by comparing the two functions on real quarter boundaries rather than by having
    one call the other: `build` must not import an ingest module to write a world, and the
    duplication is deliberate. This is the test that makes it safe."""
    from prediction_market_macro.ingest.nowcast import _quarter_of
    nc = BD.NOWCASTS["gdp_quarterly"]
    for ts in ("2026-01-01", "2026-03-31", "2026-04-01", "2026-12-31", "2027-07-01"):
        assert nc.key(pd.Timestamp(ts)) == _quarter_of(ts), ts


def test_every_nowcast_forecasts_a_column_its_own_panel_writes_to_fred():
    """The error blocks are measured as `vintage - print`, and the print comes from the FRED
    series the column is written to. A nowcast of a column with no sink has nothing to be
    measured against, and the transplant would silently be a transplant of noise."""
    for panel, nc in BD.NOWCASTS.items():
        sinks = BD._sinks(panel)
        assert nc.column in sinks, f"{panel}: nowcast of {nc.column!r}, sinks {sorted(sinks)}"
        assert sinks[nc.column].kind == "fred"


def _donor_book():
    """Two real error blocks with distinguishable magnitudes, at distinguishable leads."""
    return [(1.0, [(-60.0, +0.5), (-30.0, +0.3), (-2.0, +0.1)]),
            (8.0, [(-55.0, -4.0), (-25.0, -3.0), (-3.0, -2.0)])]


def _truths():
    idx = pd.to_datetime(["2027-01-01", "2027-04-01", "2027-07-01"])
    return pd.Series([2.0, 1.5, 3.0], index=idx), {
        idx[0]: datetime(2027, 4, 29, 12, 30, tzinfo=UTC),
        idx[1]: datetime(2027, 7, 29, 12, 30, tzinfo=UTC),
        idx[2]: datetime(2027, 10, 28, 12, 30, tzinfo=UTC)}


def test_synth_nowcast_is_the_generated_truth_plus_a_real_error_block():
    """The whole construction in one assertion. §4f measured the final vintage at
    b = 0.95 +/- 0.03 against the print, which licenses `nowcast = truth + eps` with eps
    transplanted rather than modelled — but only if the transplant lands on the TRUTH THE
    WORLD HOLDS. Reconstructing the errors from the output and matching them to a donor
    block is what catches a transplant onto the unrounded draw, or onto the wrong period."""
    nc = BD.NOWCASTS["gdp_quarterly"]
    truths, rel = _truths()
    got = BD.synth_nowcast(_donor_book(), truths, rel, np.random.default_rng(0), nc,
                           floor=datetime(2026, 12, 1, tzinfo=UTC))
    by_key = {nc.key(t): float(y) for t, y in truths.items()}
    assert set(got) == set(by_key)                        # every quarter got a path
    blocks = {tuple(e for _, e in seq) for _, seq in _donor_book()}
    for period, seq in got.items():
        assert len(seq) == 3
        assert tuple(round(v - by_key[period], 9) for _, v in seq) in blocks, period


def test_synth_nowcast_draws_its_donor_on_the_size_of_the_truth_not_uniformly():
    """§4f licensed a uniform draw off the FINAL vintage, where corr(|err|, |truth|) is
    +0.331 over all 41 donors and -0.222 ex-2020 — sign-flipping, so not measurable. Measured
    over the whole block instead, at 45 days out it is +0.624 (ex-2020 +0.161), and that is
    the lead the model actually prices at. Uniformly drawn, 2020Q3's block lands on a +2.5%
    quarter and writes a +23% nowcast into a world — a number no GDPNow vintage has ever
    printed, handed to the model as its anchor."""
    truths = pd.Series([8.2], index=pd.to_datetime(["2027-01-01"]))
    rel = {pd.Timestamp("2027-01-01"): datetime(2027, 4, 29, 12, 30, tzinfo=UTC)}
    nc = BD.NOWCASTS["gdp_quarterly"]
    from dataclasses import replace
    got = BD.synth_nowcast(_donor_book(), truths, rel, np.random.default_rng(0),
                           replace(nc, k_donor=1), floor=datetime(2026, 1, 1, tzinfo=UTC))
    errs = [round(v - 8.2, 9) for _, v in got["2027-Q1"]]
    assert errs == [-4.0, -3.0, -2.0], "the |truth|=8 block is the near neighbour of 8.2"


def test_synth_nowcast_starts_each_path_after_the_previous_quarters_release():
    """How GDPNow is actually produced — the 2025-Q1 window opens on 2025-01-31, two days
    after the 2024-Q4 advance print — and, as a by-product, the reason two generated periods
    can never collide on a knowledge_time. The by-product is not the justification: a path
    that ran from 60 days before its own release would overlap the previous quarter's tail
    and `write_nowcast` would raise, which is a correct refusal of an incorrect path."""
    truths, rel = _truths()
    got = BD.synth_nowcast(_donor_book(), truths, rel, np.random.default_rng(0),
                           BD.NOWCASTS["gdp_quarterly"],
                           floor=datetime(2026, 12, 1, tzinfo=UTC))
    spans = {p: (min(k for k, _ in s), max(k for k, _ in s)) for p, s in got.items()}
    order = sorted(spans, key=lambda p: spans[p][0])
    for a, b in zip(order, order[1:]):
        assert spans[a][1] < spans[b][0], f"{a} overlaps {b}"
    for p, (k0, k1) in spans.items():
        r = rel[[t for t in truths.index if BD.NOWCASTS["gdp_quarterly"].key(t) == p][0]]
        assert k1 < r - BD.CLOSE_LEAD, f"{p}: a vintage at or after its own close"
        assert k0 > datetime(2026, 12, 1, tzinfo=UTC), f"{p}: a vintage before the splice"


def test_synth_nowcast_refuses_an_empty_donor_book_rather_than_writing_no_vintages():
    """The failure this converts into an error is the expensive one: an empty nowcast table
    makes every event raise the model's own 'no vintage visible', which the build reports as
    zero events generated — indistinguishable, at the log line, from a generator that failed
    to produce a usable path."""
    truths, rel = _truths()
    with pytest.raises(ValueError, match="no real GDPNow/KXGDP error blocks"):
        BD.synth_nowcast([], truths, rel, np.random.default_rng(0),
                         BD.NOWCASTS["gdp_quarterly"],
                         floor=datetime(2026, 12, 1, tzinfo=UTC))


def test_nowcast_donors_reads_neither_a_vintage_nor_a_print_from_after_the_splice(tmp_path):
    """Two separate cutoffs and both matter. The vintages are the donor's SHAPE and the
    prints are what makes them errors rather than levels, so a print visible early would
    label a block with an answer the world has not published yet — and the resulting error
    block would be, quietly, a better forecast than GDPNow has ever managed."""
    conn = init_db(tmp_path / "d.db")
    for q, y, kt in (("2026-01-01", 2.0, "2026-04-29T12:30:00+00:00"),
                     ("2026-04-01", 3.0, "2026-07-29T12:30:00+00:00")):
        conn.execute("INSERT INTO fred_obs(sid, event_time, value, vintage_date,"
                     " knowledge_time, first_seen_ts) VALUES('A191RL1Q225SBEA',?,?,?,?,"
                     "'real')", (q, y, kt[:10], kt))
    for q, kt, v in (("2026-Q1", "2026-03-01T14:30:00+00:00", 2.4),
                     ("2026-Q1", "2026-04-01T14:30:00+00:00", 2.1),
                     ("2026-Q2", "2026-06-01T14:30:00+00:00", 3.9)):
        conn.execute("INSERT INTO nowcast_vintages VALUES('GDPNow','KXGDP',?,?,?,'real')",
                     (q, v, kt))
    conn.commit()
    nc = BD.NOWCASTS["gdp_quarterly"]
    got = BD.nowcast_donors(conn, nc, "A191RL1Q225SBEA", datetime(2026, 6, 15, tzinfo=UTC))
    assert [(y, [round(e, 9) for _, e in s]) for y, s in got] == [(2.0, [0.4, 0.1])]
    later = BD.nowcast_donors(conn, nc, "A191RL1Q225SBEA", datetime(2026, 8, 1, tzinfo=UTC))
    assert [y for y, _ in later] == [2.0, 3.0]


def test_nowcast_donors_keeps_each_quarter_as_a_block_not_a_bag_of_errors():
    """`synth_nowcast` transplants a WHOLE path, because the thing being reproduced is how a
    nowcast tightens toward its release. A flat pool of errors would let a 60-day-out error
    land two days before the print, which is the one shape GDPNow never has."""
    import inspect
    src = inspect.getsource(BD.nowcast_donors)
    assert "out.append((y, seq))" in src, "the per-period block structure was flattened"


# ── parallel scoring (2026-08-21) ───────────────────────────────────────────
# Scoring is the weekly job's whole cost: `event_pnl` re-runs the forecasting model for
# every (event, candidate) pair, so it cannot be hoisted across candidates, and at 215 ms
# a pair the seven monthly markets came to ~6.3 h of one pinned core. Worlds are
# independent SQLite files, so the fix is a pool -- but ONLY if the pool cannot reorder
# the rows, because `mat[i]` is paired to `kept[i]` and both halves of S5 are paired tests.
def test_parallel_scoring_reassembles_in_sorted_world_order(tmp_path, monkeypatch):
    """`as_completed` yields in COMPLETION order, not submission order. Slotting by index
    is what keeps the pool from silently permuting the sample; this drives a pool whose
    futures complete backwards, which is the permutation that would do the damage."""
    worlds = {}
    for name in ("c_world", "a_world", "b_world"):
        p = tmp_path / f"{name}.db"
        p.write_text("")
        worlds[str(p)] = name
    evs = [BD.SynthEvent(series="KXWTIW", path=i, period="26JUN05", key="2026-06-05",
                         close=datetime(2026, 6, 5, 18, 30, tzinfo=UTC), outcome=80.0,
                         z_y=0.0, donor="x/y", world=w)
           for i, w in enumerate(sorted(worlds))]

    def fake_score_world(wp, es, grid):
        return list(es), [[float(worlds[wp].startswith("a")) + len(worlds[wp])] for _ in es]

    class RevFuture:
        def __init__(self, v): self._v = v
        def result(self): return self._v

    class FakePool:
        def __init__(self, max_workers=None): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def submit(self, fn, *a): return RevFuture(fn(*a))

    monkeypatch.setattr(BD, "_score_world", fake_score_world)
    monkeypatch.setattr(BD.cf, "ProcessPoolExecutor", FakePool)
    monkeypatch.setattr(BD.cf, "as_completed", lambda fs: list(reversed(list(fs))))

    kept, mat = BD.score_matrix(evs, [{}], workers=3)
    serial_kept, serial_mat = BD.score_matrix(evs, [{}], workers=1)
    assert [e.world for e in kept] == sorted(worlds), "the pool permuted the sample"
    assert [e.world for e in kept] == [e.world for e in serial_kept]
    assert mat == serial_mat, "parallel and serial must agree row for row"


def test_parallel_scoring_keeps_each_row_paired_to_its_event(tmp_path, monkeypatch):
    """The failure that survives an order check: right events, rows off by one world. Each
    world here returns a row that names itself, so a mis-slot is visible in the values."""
    paths = []
    for name in ("w2", "w0", "w1"):
        p = tmp_path / f"{name}.db"
        p.write_text("")
        paths.append(str(p))
    evs = [BD.SynthEvent(series="KXWTIW", path=i, period="26JUN05", key="2026-06-05",
                         close=datetime(2026, 6, 5, 18, 30, tzinfo=UTC), outcome=80.0,
                         z_y=0.0, donor="x/y", world=w) for i, w in enumerate(paths)]
    tag = {w: float(i) for i, w in enumerate(sorted(paths))}
    monkeypatch.setattr(BD, "_score_world",
                        lambda wp, es, grid: (list(es), [[tag[wp]] for _ in es]))

    class F:
        def __init__(self, v): self._v = v
        def result(self): return self._v

    class FakePool:
        def __init__(self, max_workers=None): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def submit(self, fn, *a): return F(fn(*a))

    monkeypatch.setattr(BD.cf, "ProcessPoolExecutor", FakePool)
    monkeypatch.setattr(BD.cf, "as_completed", lambda fs: list(reversed(list(fs))))
    kept, mat = BD.score_matrix(evs, [{}], workers=3)
    for e, row in zip(kept, mat):
        assert row == [tag[e.world]], f"{e.world} got another world's row"


def test_scoring_stays_serial_for_a_single_world(tmp_path, monkeypatch):
    """A pool costs seconds of process startup. Most unit tests and `calibrate`'s smaller
    builds have one world, where that is pure loss -- and a subprocess would also drop any
    monkeypatch the caller installed, turning a fast fake into a real model run."""
    monkeypatch.setattr(BD.cf, "ProcessPoolExecutor",
                        lambda **k: pytest.fail("spawned a pool for one world"))
    p = tmp_path / "only.db"
    p.write_text("")
    ev = BD.SynthEvent(series="KXWTIW", path=0, period="26JUN05", key="2026-06-05",
                       close=datetime(2026, 6, 5, 18, 30, tzinfo=UTC), outcome=80.0,
                       z_y=0.0, donor="x/y", world=str(p))
    monkeypatch.setattr(BD, "_score_world", lambda wp, es, grid: (list(es), [[1.0]]))
    kept, mat = BD.score_matrix([ev], [{}])
    assert mat == [[1.0]]
