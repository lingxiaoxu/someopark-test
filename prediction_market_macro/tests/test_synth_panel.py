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
