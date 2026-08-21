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
