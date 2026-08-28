"""synth/panel — the training corpus for the DFM generator (docs/PLAN_DFM_SYNTH.md §4).

Everything downstream of this file trusts three properties, so they are pinned here:
the condition vector describes the anchor and nothing after it, `integrate` is an exact
inverse of the increment transform, and a column reads the vintage its consuming model
reads. A panel that quietly used revised history, or leaked the forward path into the
condition, would still train and still generate — it would just be generating a world
that never existed, and nothing further downstream could detect that.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.research.synth import panel as P

UTC = timezone.utc


def _put(conn, sid, event_time, value, vintage, know):
    conn.execute(
        "INSERT OR REPLACE INTO fred_obs(sid, event_time, value, vintage_date,"
        " knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,?)",
        (sid, event_time, value, vintage, know, know))


def _seed(conn, sid, n=90, base=100.0, step=1.0, revise=0.0, start="2015-01-01"):
    """A monthly series printed 12 days after the month, optionally revised at +40 days.

    `revise` is the size of the revision, so a test can tell a first-print read from a
    latest-vintage read by making the two disagree by a known amount.
    """
    t0 = pd.Timestamp(start)
    for i in range(n):
        ev = (t0 + pd.DateOffset(months=i)).strftime("%Y-%m-%d")
        first = (pd.Timestamp(ev) + timedelta(days=12)).isoformat()
        _put(conn, sid, ev, base + step * i, first[:10], first)
        if revise:
            later = (pd.Timestamp(ev) + timedelta(days=40)).isoformat()
            _put(conn, sid, ev, base + step * i + revise, later[:10], later)
    conn.commit()
    return pd.Timestamp(t0 + pd.DateOffset(months=n - 1)) + timedelta(days=60)


def _seed_alfred(conn, sid, n=90, base=150_000.0, step=150.0, revise=-20.0,
                 start="2015-01-01"):
    """ALFRED-style: every vintage carries the new month AND a revised previous month.

    `_seed` writes one month per vintage, which no real ALFRED release looks like and which
    `payrolls.printed_changes` (correctly) refuses to difference — it needs both months
    inside the same vintage. It is also the shape that makes the printed change differ from
    the first-print chain difference by exactly `revise`, which is the thing under test.
    """
    t0 = pd.Timestamp(start)
    for i in range(n):
        ev = t0 + pd.DateOffset(months=i)
        vint = (ev + timedelta(days=12)).isoformat()
        _put(conn, sid, ev.strftime("%Y-%m-%d"), base + step * i, vint[:10], vint)
        if i:
            prev = t0 + pd.DateOffset(months=i - 1)
            _put(conn, sid, prev.strftime("%Y-%m-%d"),
                 base + step * (i - 1) + revise, vint[:10], vint)
    conn.commit()


def _spec(**kw) -> P.PanelSpec:
    cols = kw.pop("columns", (
        P.Column("a", "fred", "AAA", "first", "last", "diff", "pct"),
        P.Column("b", "fred", "BBB", "latest", "last", "dlog", "count"),
    ))
    base = dict(name="t", freq="MS", horizon=4, columns=cols, start="2015-01-01",
                level_lag=12)
    base.update(kw)
    return P.PanelSpec(**base)


@pytest.fixture()
def env(tmp_path):
    conn = init_db(tmp_path / "t.db")
    end = _seed(conn, "AAA", n=90, base=4.0, step=0.01)
    _seed(conn, "BBB", n=90, base=220_000.0, step=-300.0)
    return conn, datetime(end.year, end.month, end.day, tzinfo=UTC)


def _build(conn, spec, end):
    P.PANELS[spec.name] = spec
    try:
        return P.build(conn, spec.name, end)
    finally:
        P.PANELS.pop(spec.name, None)


def test_condition_uses_no_future(env):
    """The anchor state must be a function of the anchor and its past only.

    This is the property that makes the whole scheme legitimate: the generator learns
    p(forward path | state), and if the state already contained the forward path it would
    be learning p(path | path). Perturbing every row strictly after the anchor must leave
    the condition vector bit-identical.
    """
    conn, end = env
    spec = _spec()
    pdata = _build(conn, spec, end)
    t = pdata.anchors[len(pdata.anchors) // 2]
    before = P.condition_row(pdata.levels, pdata.inc, spec, t)

    lv, inc = pdata.levels.copy(), pdata.inc.copy()
    after = lv.index > t
    lv.loc[after] = lv.loc[after] * 3.0 + 17.0
    inc.loc[after] = inc.loc[after] * -5.0
    assert np.array_equal(before, P.condition_row(lv, inc, spec, t))


def test_condition_dim_matches_what_is_built(env):
    conn, end = env
    spec = _spec()
    pdata = _build(conn, spec, end)
    assert pdata.C.shape[1] == P.condition_dim(spec)


@pytest.mark.parametrize("transform, scale, anchor", [
    ("diff", 1.0, 4.25), ("diff", 1000.0, 158_984.0),
    ("dlog", 1.0, 63.5), ("pct100", 1.0, 332.5),
])
def test_integrate_inverts_the_transform(transform, scale, anchor):
    """`integrate` must undo `_to_increment` exactly, because a synthetic world is written
    in LEVELS (fred_obs rows) and scored on the increments the models recompute from them.
    A lossy inverse would put the generated number and the scored number a rounding error
    apart on every single event."""
    rng = np.random.default_rng(0)
    lv = pd.Series(anchor * np.exp(np.cumsum(rng.normal(0, 0.02, 40)))
                   if transform in ("dlog", "pct100")
                   else anchor + np.cumsum(rng.normal(0, 0.05, 40)),
                   index=pd.date_range("2020-01-01", periods=40, freq="MS"))
    col = P.Column("x", "fred", "X", "latest", "last", transform, "u", scale=scale)
    inc = P._to_increment(lv, transform, scale).dropna()
    spec = _spec(columns=(col,))
    # an increment is stamped on the period it MOVES TO, so the anchor for a segment is
    # the level one period before the segment's first stamp
    seg = inc.iloc[10:20]
    anchor_t = lv.index[lv.index.get_loc(seg.index[0]) - 1]
    got = P.integrate(pd.DataFrame({"x": seg}), pd.Series({"x": lv.loc[anchor_t]}), spec)
    np.testing.assert_allclose(got["x"].to_numpy(), lv.loc[seg.index].to_numpy(),
                               rtol=1e-12, atol=1e-9)


def _lattice_spec():
    """Three columns whose grids are known by construction, so discovery can be graded."""
    return _spec(columns=(
        P.Column("jobs", "fred", "J", "first", "last", "diff", "jobs", scale=1000.0),
        P.Column("tick", "fred", "T", "latest", "last", "dlog", "$"),
        P.Column("cont", "fred", "C", "latest", "last", "dlog", "$"),
    ))


def _lattice_levels(n=300):
    rng = np.random.default_rng(4)
    idx = pd.date_range("2000-01-01", periods=n, freq="MS")
    return pd.DataFrame({
        "jobs": np.round(150_000 + np.cumsum(rng.normal(150, 60, n))),      # grid 1
        "tick": np.round(60 + np.cumsum(rng.normal(0, 1.2, n)), 2),         # grid 0.01
        "cont": 60 * np.exp(np.cumsum(rng.normal(0, 0.02, n))),             # none
    }, index=idx)


def test_measure_lattice_discovers_the_grid_and_admits_when_there_is_none():
    """The grid must be READ off the data, not declared. `cont` is continuous by
    construction and must come back absent: a discovery that reported a grid for it would
    quantise a column that has none, and every downstream 'on-grid' number would be a
    tautology rather than a measurement."""
    spec = _lattice_spec()
    lat = P.measure_lattice(_lattice_levels(), spec)
    assert lat["jobs"] == {"step": 1.0, "dtype": "float64"}
    assert lat["tick"] == {"step": 0.01, "dtype": "float64"}
    assert "cont" not in lat


def test_measure_lattice_sees_through_float32_storage():
    """`fut_closes` hands back 71.41000366 for a settlement of 71.41, so an exactness-only
    test finds no grid on wti/natgas/rbob even though the CME tick is right there. The
    second pass must find it — and must NOT be reachable for a column that already failed
    on its own merits, which is what `cont` pins."""
    lv = _lattice_levels()
    lv["tick"] = lv["tick"].astype(np.float32).astype(float)
    lv["cont"] = lv["cont"].astype(np.float32).astype(float)
    lat = P.measure_lattice(lv, _lattice_spec())
    assert lat["tick"] == {"step": 0.01, "dtype": "float32"}
    assert "cont" not in lat, "a continuous column must not acquire a grid from the f32 pass"


def _mean_agg_levels(n=300, source_step=1000.0, seed=11):
    """A column built the way `agg="mean"` builds one: each period is the average of four OR
    five sub-period prints, each print a multiple of `source_step`.

    This is not a toy. It is the exact construction behind `labor_monthly`'s monthly `claims`
    (four-or-five weekly ICSA prints), `energy_weekly_wide`'s `dgs2`/`dgs10` (four-or-five
    daily yields) and `inflation_monthly`'s `gas_retail` (four-or-five weekly GASREGW prints).
    A mean of 4 lands on `g/4` and a mean of 5 on `g/5`, so a mix sits on `gcd(g/4, g/5)` =
    `g/20` and on nothing coarser.
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2000-01-01", periods=n, freq="MS")
    base = 300_000.0 + np.cumsum(rng.normal(0, 4_000, n))
    vals = []
    for i, b in enumerate(base):
        k = 4 + (i % 2)                       # alternating 4 and 5, like real month lengths
        prints = np.round((b + rng.normal(0, 8_000, k)) / source_step) * source_step
        vals.append(float(prints.mean()))
    return pd.DataFrame({"jobs": np.round(base), "tick": np.round(base / 5000, 2),
                         "cont": np.asarray(vals)}, index=idx)


def test_the_grid_of_a_mean_aggregated_column_is_the_source_grid_over_twenty():
    """#203/§4e-I. The hand-written `_LATTICE_STEPS` ladder contains `g/20` for `g = 1.0` and
    for nothing else that matters, so on four real columns it returned a grid FIVE TIMES
    FINER than the series occupies — and four of every five grid classes the generator then
    emitted were values the publication process cannot produce.

    The assertion is on the derived value, `1000 / 20 = 50`, not on whatever the code happens
    to return: 10 is what the ladder finds (a multiple of 50 passes the 10-grid trivially) and
    is the wrong answer; 250 and 200 are the two per-month grids and neither holds across the
    mix."""
    lv = _mean_agg_levels()
    v = lv["cont"].to_numpy()
    assert P.on_lattice(v, 50.0) == 1.0
    assert P.on_lattice(v, 10.0) == 1.0, "the ladder's answer is not wrong, it is not coarsest"
    assert P.on_lattice(v, 250.0) < 0.995 and P.on_lattice(v, 200.0) < 0.995
    lat = P.measure_lattice(lv, _lattice_spec())
    assert lat["cont"] == {"step": 50.0, "dtype": "float64"}


def test_the_exact_gcd_pass_only_ever_coarsens_and_one_rogue_row_makes_it_a_no_op():
    """The GCD is exact on 100% of rows, which is also its failure mode: a single row off the
    grid drags it to the resolution floor, where `_LATTICE_HIT`'s 0.995 would have shrugged.
    That is survivable only because the GCD may not make the answer finer. Corrupt one row of
    300 and the ladder's 10.0 must come back — not 50.0, and above all not a resolution-floor
    grid that would quantise the column to nothing."""
    lv = _mean_agg_levels()
    assert P.measure_lattice(lv, _lattice_spec())["cont"]["step"] == 50.0
    lv.iloc[7, lv.columns.get_loc("cont")] += 0.37       # off every grid in the ladder
    lat = P.measure_lattice(lv, _lattice_spec())
    assert P._exact_gcd_step(lv["cont"].to_numpy()) is None
    assert lat["cont"] == {"step": 10.0, "dtype": "float64"}


def test_the_exact_gcd_pass_declines_a_continuous_column_and_a_float32_one():
    """Two ways the GCD must stay silent. A continuous column has no grid and must not acquire
    one — `_exact_gcd_step` returns None because the GCD bottoms out at its own resolution.
    A float32 column has already been judged by the loose rule, and an exact GCD of values
    that are only APPROXIMATELY on their grid is meaningless, so pass 3 never runs on it."""
    lv = _lattice_levels()
    assert P._exact_gcd_step(lv["cont"].to_numpy()) is None
    lv["tick"] = lv["tick"].astype(np.float32).astype(float)
    lat = P.measure_lattice(lv, _lattice_spec())
    assert lat["tick"] == {"step": 0.01, "dtype": "float32"}
    assert "cont" not in lat


def test_the_exact_gcd_floor_rejects_a_grid_that_is_float64_bookkeeping():
    """`8 * eps32 * max|x|` is borrowed from the float32 pass and here means something weaker:
    below ~1e-7 of the series' own scale, "every value is a multiple of g" is a fact about
    float64 storage, not about a publication process. `gas_retail` is the tightest real case
    and clears it by ~10x, so the fence is load-bearing and is pinned in both directions."""
    v = np.arange(1, 400, dtype=float) * 5e-05 + 2.5          # gas_retail's real geometry
    assert P._exact_gcd_step(v) == pytest.approx(5e-05, rel=1e-9)
    big = v * 1e6                                              # same grid, 1e6 larger scale
    assert P._exact_gcd_step(big) == pytest.approx(50.0, rel=1e-9)
    # a grid 1e-9 of the series' own magnitude is below the floor and must be refused
    assert P._exact_gcd_step(np.arange(1, 400, dtype=float) * 1e-9 + 1000.0) is None


def test_quantisation_lands_on_the_grid_and_is_idempotent():
    """The property the C2ST reads: after quantisation the synthetic levels are on the same
    grid as the real ones, 100% of the time and not 99%."""
    spec = _lattice_spec()
    lv = _lattice_levels()
    lat = P.measure_lattice(lv, spec)
    rng = np.random.default_rng(1)
    paths = np.stack([lv[c].to_numpy()[:40] + rng.normal(0, 0.3, 40)
                      for c in spec.names], axis=-1)[None, ...]
    q = P.quantise_levels(paths, spec, lat)
    for j, name in enumerate(spec.names):
        e = lat.get(name)
        if e is None:
            np.testing.assert_array_equal(q[..., j], paths[..., j])
            continue
        assert P.on_lattice(paths[..., j], e["step"], dtype=e["dtype"]) < 0.5
        assert P.on_lattice(q[..., j], e["step"], dtype=e["dtype"]) == 1.0
    np.testing.assert_array_equal(q, P.quantise_levels(q, spec, lat))


def test_to_increments_inverts_integrate_paths():
    """`validate` scores INCREMENTS and `worlds.py` writes LEVELS. Without an exact round
    trip the quantisation is invisible to the test that exists to police it, which is
    precisely how the missing grid survived two rounds of validation."""
    spec = _lattice_spec()
    lv = _lattice_levels()
    anchor = lv.iloc[10]
    rng = np.random.default_rng(2)
    inc = np.stack([rng.normal(0, s, (5, 12)) for s in (2e5, 0.02, 0.02)], axis=-1)
    levels = P.integrate_paths(inc, anchor, spec, None)
    np.testing.assert_allclose(P.to_increments(levels, anchor, spec), inc,
                               rtol=1e-10, atol=1e-10)
    # and the quantised branch really is the unquantised one plus a rounding
    lat = P.measure_lattice(lv, spec)
    qq = P.integrate_paths(inc, anchor, spec, lat)
    np.testing.assert_array_equal(qq, P.quantise_levels(levels, spec, lat))


# ── PR-17 / #203: the grid a mean-aggregated column can actually reach ───────────────────
# The pooled grid `gcd(g/4, g/5) = g/20` DESCRIBES such a column correctly and is still the
# wrong thing to round onto: the union of {multiples of g/4} and {multiples of g/5} covers
# 40% of the g/20 lattice, so a quantiser handed the scalar emits an unprintable level about
# 60% of the time. Everything below exists to make that fix fail loudly instead of quietly —
# quietly is the whole danger, since a wrongly-quantised level is still on *a* measured grid
# and still passes every nesting check `build._check_settle_grid_nests` runs.

def _sub_spec(freq="MS", agg="mean", **kw):
    """One column aggregated from a faster source — the only shape PR-17 can touch."""
    return _spec(freq=freq, columns=(
        P.Column("avg", "fred", "WKY", "latest", agg, "dlog", "$"),), **kw)


def _weekly_source(n=800, dow=0, source_step=1000.0, seed=17, start="2006-01-02"):
    """A source that prints on ONE weekday, every print an exact multiple of `source_step`.

    ICSA to the day: weekly, one weekday, values on a 1000 grid. 800 prints is ~15.3 years,
    which is what lets a test put a hole OUTSIDE `_SUB_HOLE_FREE_YEARS` and mean it.
    """
    idx = pd.date_range(start, periods=n, freq="7D")
    assert idx[0].dayofweek == dow, "the fixture's start day is part of the fixture"
    rng = np.random.default_rng(seed)
    v = np.round((300_000 + np.cumsum(rng.normal(0, 6_000, n))) / source_step) * source_step
    return pd.Series(v, index=idx)


def _sub_levels(src, freq="MS"):
    """The panel column, built the way `_resample` builds it — mean THEN forward-fill, so a
    period with no print at all reaches the rule as a level rather than as a NaN."""
    return pd.DataFrame({"avg": src.resample(freq).mean().ffill()})


def _sub_lattice(src=None, freq="MS", agg="mean"):
    src = _weekly_source() if src is None else src
    spec = _sub_spec(freq=freq, agg=agg)
    return spec, src, P.measure_lattice(_sub_levels(src, freq), spec, {"avg": src})


def test_period_bounds_puts_the_bin_label_at_opposite_ends_for_a_month_and_a_week():
    """"MS" labels the FIRST day of its bin and "W-SAT" the LAST. A single generic answer
    written off the alias would be wrong for one of the two, and the failure would be a
    level quantised on the wrong month's grid — silent, and on-grid-looking."""
    assert P.period_bounds("MS", "2026-02-01") == (pd.Timestamp("2026-02-01"),
                                                   pd.Timestamp("2026-02-28"))
    assert P.period_bounds("W-SAT", "2026-02-07") == (pd.Timestamp("2026-02-01"),
                                                      pd.Timestamp("2026-02-07"))
    assert P.period_bounds("QS", "2026-01-01") is None, "an unknown convention must not guess"


def test_sub_period_count_is_arithmetic_on_a_period_no_data_exists_for():
    """The load-bearing property: `quantise_levels` runs on FUTURE paths, so `n` has to come
    from the calendar and not from a row count. September 2030 has five Mondays and October
    2030 has four; nothing in any database says so."""
    rule = {"kind": "weekly", "dayofweek": 0}
    assert P.sub_period_count(rule, "MS", "2030-09-01") == 5
    assert P.sub_period_count(rule, "MS", "2030-10-01") == 4
    assert P.sub_period_count(rule, "MS", "2026-02-01") == 4
    # a week contains exactly one of any weekday, whatever the label's own day is
    for dow in range(7):
        assert P.sub_period_count({"kind": "weekly", "dayofweek": dow},
                                  "W-SAT", "2030-09-07") == 1
    with pytest.raises(ValueError):
        P.sub_period_count(rule, "QS", "2030-01-01")
    with pytest.raises(ValueError):
        P.sub_period_count({"kind": "business_daily"}, "MS", "2030-09-01")


def test_weekly_print_dow_reads_through_a_hole_but_refuses_a_daily_index():
    """GASREGW is missing six consecutive Mondays in 1990-12 and is still a Monday series;
    demanding gaps of exactly 7 rejected it, and rejecting it costs `gas_retail` the fix for
    a 36-year-old hole. What must still be refused is a source whose print days are NOT a
    weekly calendar, because for those `n(future period)` is not arithmetic at all."""
    src = _weekly_source()
    assert P._weekly_print_dow(src.index) == 0
    holed = pd.concat([src.iloc[:40], src.iloc[46:]])          # six consecutive Mondays gone
    assert P._weekly_print_dow(holed.index) == 0, "a hole is still a weekly calendar"
    daily = pd.Series(1.0, index=pd.bdate_range("2006-01-02", periods=800))
    assert P._weekly_print_dow(daily.index) is None
    mixed = pd.concat([src, pd.Series(1.0, index=src.index[:60] + pd.Timedelta(days=2))])
    assert P._weekly_print_dow(mixed.sort_index().index) is None
    assert P._weekly_print_dow(src.index[:49]) is None, "49 prints is not a measured calendar"


def test_a_mean_column_gets_the_grid_of_its_own_period_and_a_last_column_never_does():
    """The entry is pinned whole, not field by field: an extra key would travel into every
    saved `scaler["lattice"]` and out again into artefacts this package cannot re-read.

    `step` stays on it because `_check_settle_grid_nests` compares Kalshi ladders against
    that number, and dropping it to "replace" the grid would silently disable the nesting
    check rather than tighten it."""
    spec, src, lat = _sub_lattice()
    assert lat["avg"] == {"step": 50.0, "dtype": "float64", "source_step": 1000.0,
                          "sub_period": {"kind": "weekly", "dayofweek": 0}}
    # 1000/lcm(4,5) is the pooled step: the conditional grids still nest inside it
    assert P.on_lattice(np.array([250.0, 200.0]), lat["avg"]["step"]) == 1.0
    _, _, last = _sub_lattice(src=src, agg="last")
    assert "sub_period" not in last["avg"], "an `agg=last` column is one print, not a mean"


def test_the_lattice_is_unchanged_when_no_sources_are_passed():
    """Backward compatibility is exact, not approximate. A Generator pickled before PR-17
    carries entries with no `source_step`, and `_grid`/`quantise_levels` must keep taking the
    scalar path for them — otherwise every artefact on disk becomes unloadable."""
    spec, src, _ = _sub_lattice()
    lv = _sub_levels(src)
    assert P.measure_lattice(lv, spec) == {"avg": {"step": 50.0, "dtype": "float64"}}
    assert P.measure_lattice(lv, spec, {}) == P.measure_lattice(lv, spec)
    old = {"avg": 50.0}                                    # the bare float an old one carries
    paths = np.full((2, 4, 1), 300_123.4)
    np.testing.assert_array_equal(P.quantise_levels(paths, spec, old),
                                  np.full((2, 4, 1), 300_100.0))


@pytest.mark.parametrize("case", ["daily_source", "source_off_its_own_grid", "level_off_g_n",
                                  "recent_hole", "one_n_group", "pooled_disagrees",
                                  "a_period_with_no_print"])
def test_every_refusal_path_leaves_the_column_on_the_pooled_scalar(case):
    """Seven ways the derivation can fail to hold, and one verdict for all of them: keep the
    scalar. That asymmetry is the design — a column that keeps the pooled step is exactly as
    wrong as it was yesterday, while a column handed a conditional grid the source does not
    obey is newly wrong in a way nothing downstream can see."""
    src, freq = _weekly_source(), "MS"
    if case == "pooled_disagrees":
        # the one condition `measure_lattice` cannot stage, because it measures the pooled
        # step itself: a `_best_step` the derivation's `g_src/lcm(n)` cannot reproduce means
        # one of the two measurements is wrong, and neither may be acted on.
        lv, spec = _sub_levels(src, freq), _sub_spec(freq=freq)
        assert P._sub_period_rule(lv["avg"], src, spec,
                                  {"step": 7.0, "dtype": "float64"}) is None
        return
    if case == "daily_source":
        src = pd.Series(np.round(np.linspace(300_000, 500_000, 800) / 1000) * 1000,
                        index=pd.bdate_range("2006-01-02", periods=800))
    elif case == "source_off_its_own_grid":
        src.iloc[123] += 0.37                              # `_exact_gcd_step` -> None
    elif case == "recent_hole":
        src = src.drop(src.index[776])                     # one 2020-11 print never published
    elif case == "one_n_group":
        freq = "W-SAT"                                     # every week holds exactly one print
    elif case == "a_period_with_no_print":
        src = pd.concat([src.iloc[:200], src.iloc[206:]])  # a whole month, forward-filled
    lv = _sub_levels(src, freq)
    if case == "level_off_g_n":
        lv.iloc[30, 0] += 1.0                              # falsifier (c), one period
    spec = _sub_spec(freq=freq)
    if case == "pooled_disagrees":
        pooled = {"step": 7.0, "dtype": "float64"}         # a step the derivation cannot make
        assert P._sub_period_rule(lv["avg"], src, spec, pooled) is None
    lat = P.measure_lattice(lv, spec, {"avg": src})
    assert "sub_period" not in lat.get("avg", {}), f"{case} must not carry a conditional grid"
    assert "source_step" not in lat.get("avg", {})


def test_a_truncated_edge_period_is_not_a_hole():
    """The counterpart to `recent_hole`, and the reason the two are distinguished at all.
    The panel's last period always runs past the PIT cut, so its observed print count is
    short — by arithmetic, not because the publisher skipped anything. Counting that as a
    hole would make the conditional grid appear and disappear with the DAY OF THE MONTH the
    training cut lands on, which is the least defensible kind of non-determinism."""
    full = _weekly_source()
    cut = full.iloc[:-2]                        # the cut lands mid-month, two prints missing
    lat = P.measure_lattice(_sub_levels(cut), _sub_spec(), {"avg": cut})
    assert lat["avg"]["sub_period"] == {"kind": "weekly", "dayofweek": 0}
    # an OLD hole is tolerated — GASREGW skipped six Mondays in 1990 and is fine today
    old = full.drop(full.index[19])                        # 2006-05-15, ~15 years back
    lat_old = P.measure_lattice(_sub_levels(old), _sub_spec(), {"avg": old})
    assert lat_old["avg"]["sub_period"] == {"kind": "weekly", "dayofweek": 0}
    # same count mismatch, opposite verdict: a RECENT skip says the next `n` may be wrong too
    recent = full.drop(full.index[776])                    # 2020-11-16
    lat_new = P.measure_lattice(_sub_levels(recent), _sub_spec(), {"avg": recent})
    assert lat_new["avg"] == {"step": 50.0, "dtype": "float64"}, "refused, and still measured"


def test_quantise_uses_each_period_own_grid_and_refuses_to_guess_the_period():
    """The fix itself, plus the omission that would undo it. `periods` is REQUIRED once an
    entry is conditional: falling back to the pooled step is the exact defect PR-17 removes
    and it fails silently, so it raises instead."""
    spec, src, lat = _sub_lattice()
    H = spec.horizon
    periods = P.forward_periods(spec, "2021-01-01", H)
    ns = [P.sub_period_count(lat["avg"]["sub_period"], spec.freq, t) for t in periods]
    assert set(ns) == {4, 5}, "a test that only saw one month length would prove nothing"
    rng = np.random.default_rng(203)
    paths = rng.uniform(300_000, 400_000, (500, H, 1))
    with pytest.raises(ValueError, match="period-conditional"):
        P.quantise_levels(paths, spec, lat)
    with pytest.raises(ValueError, match="period labels"):
        P.quantise_levels(paths, spec, lat, periods[:-1])
    q = P.quantise_levels(paths, spec, lat, periods)
    for h, n in enumerate(ns):
        assert P.on_lattice(q[:, h, 0], 1000.0 / n) == 1.0
        other = 1000.0 / (9 - n)                          # the OTHER month length's grid
        assert P.on_lattice(q[:, h, 0], other) < 1.0, "conditioning did nothing at step h"
    # and the pooled scalar — what the column had before — emits mostly unprintable levels
    scalar = {"avg": {k: v for k, v in lat["avg"].items()
                      if k not in ("source_step", "sub_period")}}
    b = P.quantise_levels(paths, spec, scalar)[..., 0]
    reachable = np.zeros(b.shape, dtype=bool)
    for n in (4, 5):
        d = b / (1000.0 / n)
        reachable |= np.abs(d - np.round(d)) < 1e-6
    assert reachable.mean() < 0.5, "the pooled grid was never 60% unreachable to begin with"


def test_integrate_paths_takes_the_period_labels_from_the_anchor_stamp():
    """One derivation of the forward stamps, shared with `build.build`. Two copies that
    drifted apart would quantise a level on one month's lattice and write it under another
    month's date — on-grid, correctly nested, and wrong."""
    spec, src, lat = _sub_lattice()
    lv = _sub_levels(src)
    anchor = lv.iloc[-30]
    assert isinstance(anchor.name, pd.Timestamp)
    # the pin against build.py's own `date_range(anchor, periods=H+1, freq=...)[1:]`
    pd.testing.assert_index_equal(
        P.forward_periods(spec, anchor.name, spec.horizon),
        pd.date_range(anchor.name, periods=spec.horizon + 1, freq=spec.freq)[1:])
    rng = np.random.default_rng(5)
    inc = rng.normal(0, 0.05, (7, spec.horizon, 1))
    raw = P.integrate_paths(inc, anchor, spec, None)
    got = P.integrate_paths(inc, anchor, spec, lat)
    np.testing.assert_array_equal(
        got, P.quantise_levels(raw, spec, lat,
                               P.forward_periods(spec, anchor.name, spec.horizon)))
    nameless = pd.Series(anchor.to_dict())
    with pytest.raises(ValueError, match="period-conditional"):
        P.integrate_paths(inc, nameless, spec, lat)


def test_integrate_dates_the_quantiser_off_the_increment_index():
    """`integrate` is the DataFrame twin and already knows the stamps — it must use them.
    Its index IS the period labels, so a path whose months alternate 4/5 prints must come
    back on two different grids."""
    spec, src, lat = _sub_lattice()
    lv = _sub_levels(src)
    idx = pd.date_range("2021-02-01", periods=4, freq="MS")
    inc = pd.DataFrame({"avg": [0.03, -0.02, 0.05, -0.01]}, index=idx)
    out = P.integrate(inc, lv.iloc[-30], spec, lat)
    for t, v in out["avg"].items():
        n = P.sub_period_count(lat["avg"]["sub_period"], spec.freq, t)
        assert P.on_lattice(np.array([v]), 1000.0 / n) == 1.0
    assert len({P.sub_period_count(lat["avg"]["sub_period"], spec.freq, t)
                for t in idx}) == 2


def _seed_weekly(conn, sid, n=800, start="2006-01-02", source_step=1000.0, seed=17):
    """The weekly source as ALFRED would hold it: printed five days after the week it dates."""
    src = _weekly_source(n=n, source_step=source_step, seed=seed, start=start)
    for ev, v in src.items():
        know = (ev + timedelta(days=5)).isoformat()
        _put(conn, sid, ev.strftime("%Y-%m-%d"), float(v), know[:10], know)
    conn.commit()
    return src


def test_build_conditions_the_lattice_on_the_print_calendar(tmp_path):
    """End to end, because the wiring is where this can be lost: `build` must hand
    `measure_lattice` the PRE-resample series. Reading them back separately would be a second
    PIT cut, and reading them not at all silently restores the pooled scalar."""
    conn = init_db(tmp_path / "w.db")
    src = _seed_weekly(conn, "WKY")
    end = datetime(*(src.index[-1] + timedelta(days=6)).timetuple()[:3], tzinfo=UTC)
    spec = _sub_spec(start="2007-01-01")
    pdata = _build(conn, spec, end)
    assert pdata.lattice["avg"]["sub_period"] == {"kind": "weekly", "dayofweek": 0}
    assert pdata.lattice["avg"]["source_step"] == 1000.0
    assert pdata.scaler["lattice"] == pdata.lattice
    # and the generated levels land where a mean of that month's prints could land
    rng = np.random.default_rng(9)
    inc = rng.normal(0, 0.04, (3, spec.horizon, 1))
    lv = P.integrate_paths(inc, pdata.levels.loc[pdata.anchors[-1]], spec, pdata.lattice)
    for h, t in enumerate(P.forward_periods(spec, pdata.anchors[-1], spec.horizon)):
        n = P.sub_period_count(pdata.lattice["avg"]["sub_period"], spec.freq, t)
        assert P.on_lattice(lv[:, h, 0], 1000.0 / n) == 1.0


def test_build_carries_the_lattice_into_the_scaler(env):
    """A saved Generator quantises from `scaler["lattice"]`, not from the panel it was
    fitted on — the panel is long gone by the time `build.py` samples."""
    conn, end = env
    pdata = _build(conn, _spec(), end)
    assert pdata.scaler["lattice"] == pdata.lattice
    assert set(pdata.lattice) <= set(_spec().names)


def test_a_first_print_column_ignores_the_revision(env):
    """`prints` has to match the consuming model or the generator fits a series nobody
    traded. Two columns over the SAME sid, one first-print and one latest-vintage, must
    disagree by exactly the revision."""
    conn, end = env
    _seed(conn, "CCC", n=90, base=4.0, step=0.01, revise=0.25)
    spec = _spec(columns=(
        P.Column("first", "fred", "CCC", "first", "last", "level", "pct"),
        P.Column("late", "fred", "CCC", "latest", "last", "level", "pct"),
    ))
    pdata = _build(conn, spec, end)
    gap = (pdata.levels["late"] - pdata.levels["first"]).to_numpy()
    # every month except the most recent one, whose revision is not yet published
    assert np.allclose(gap[:-1], 0.25), gap[-4:]


def test_dropped_spans_never_enter_a_forward_path(env):
    """An anchor is dropped when its path OVERLAPS the span, not merely when it starts
    inside it — a path beginning in 2019-06 still runs through the pandemic."""
    conn, end = env
    spec = _spec(drop_spans=(("2018-01-01", "2018-06-01"),))
    pdata = _build(conn, spec, end)
    lo, hi = pd.Timestamp("2018-01-01"), pd.Timestamp("2018-06-01")
    idx = list(pdata.inc.index)
    for t in pdata.anchors:
        k = idx.index(t)
        path = idx[k + 1:k + 1 + spec.horizon]
        assert not (path[0] <= hi and path[-1] >= lo), f"{t.date()} spans the drop"
    # and the dropped anchors really existed before the span was declared
    assert len(pdata.anchors) < len(_build(conn, _spec(), end).anchors)


def test_overlapping_rows_report_independent_draws_not_row_count(env):
    """Anchors step one period, so 379 windows of 12 months are ~32 independent draws.
    Downstream sizing reads `n_eff_hint`; if it ever returned the row count the sample
    gate would be handed a 12x overstated sample."""
    conn, end = env
    pdata = _build(conn, _spec(), end)
    assert pdata.n_eff_hint == pytest.approx(len(pdata.anchors) / 4.0)
    assert pdata.n_eff_hint < len(pdata.anchors)


def test_payems_column_is_the_printed_change_not_a_vintage_mix(env):
    """The NFP column must equal `payrolls.printed_changes` — level(t) minus level(t-1)
    from the SAME first vintage. Differencing the first-print CHAIN instead folds the
    revision to t-1 into the change, which is not what the contract settles on."""
    conn, end = env
    # PAYEMS with a revision: the chain difference and the printed change now differ.
    _seed_alfred(conn, "PAYEMS", n=90, base=150_000.0, step=150.0, revise=-20.0)
    spec = _spec(columns=(
        P.Column("payems", "fred", "PAYEMS", "first", "last", "diff", "jobs",
                 scale=1000.0, inc_fn="payems_printed"),
    ))
    pdata = _build(conn, spec, end)
    from prediction_market_macro.model.payrolls import printed_changes
    want = printed_changes(conn, end)
    for t, v in pdata.inc["payems"].items():
        assert v == pytest.approx(float(want.loc[t]))
    chain = pdata.levels["payems"].diff() * 1000.0
    assert not np.allclose(chain.dropna().to_numpy(),
                           pdata.inc["payems"].reindex(chain.dropna().index).to_numpy())
