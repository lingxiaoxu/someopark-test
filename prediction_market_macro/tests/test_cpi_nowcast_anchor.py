"""cpi/0.3.0 — the Cleveland nowcast anchor on the YoY pair, and its exact boundaries.

What 0.3.0 changed (module docstring carries the evidence and its asymmetry): both
YoY mus are REPLACED by the PIT-visible Cleveland nowcast — headline on the decisive
45-event replay, core by explicit user decision over a wash — sigma and every other
moving part stay. The four boundaries these tests pin, each a real failure mode:

  1. anchor applied: dist mu == nowcast value, sigma == the un-anchored sigma, and
     inputs keep the internal mu as `yoy_mu_model` (observability for the bias study).
  2. PIT: a nowcast whose knowledge_time is after asof must NOT anchor — the 45-event
     validation was leak-free only because latest() filters on knowledge_time.
  3. fallback: no table (bare DBs), no row, or a stale feed (> NOWCAST_MAX_AGE_D)
     degrades to 0.2.0 behaviour, never to an exception.
  4. measure separation: headline reads 'cpi', core reads 'corecpi' — crosstalk would
     silently feed core a number no validation ever measured.

0.4.0 later anchored the headline MoM leg off the same feed's 'mom' vintages. That is a
DIFFERENT call site (predict_mom, not _predict_mom) and is pinned separately in
tests/test_cpi_mom_nowcast_anchor.py — which also asserts that the YoY channel here
stays bit-identical across that change.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest import cleveland_nowcast as cn
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model import cpi as m_cpi
from prediction_market_macro.tests.test_pit import (_seed_gasregw, _seed_monthly)


@pytest.fixture()
def env(tmp_path):
    """(conn, asof, ref_month) — the test_pit CPI world, nowcast table NOT yet created."""
    conn = init_db(tmp_path / "t.db")
    kt = _seed_monthly(conn, "CPIAUCSL", seed=5)
    _seed_monthly(conn, "CPILFESL", seed=6)
    _seed_gasregw(conn, end_year=kt.year)
    asof = kt + timedelta(days=3)
    ref_month = (kt + timedelta(days=40)).strftime("%Y-%m")
    return conn, asof, ref_month


def _put(conn, ref_month, day, val, measure="cpi"):
    cn.ensure_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO cleveland_nowcast VALUES(?,?,?,?,?,?,?)",
        (measure, "yoy", ref_month, day, val, cn._kt(day),
         datetime.now(timezone.utc).isoformat()))
    conn.commit()


def test_headline_anchor_moves_mu_only(env):
    conn, asof, ref = env
    base = m_cpi.predict_yoy(conn, asof, ref, core=False)   # no table yet -> internal
    _put(conn, ref, (asof - timedelta(days=1)).date().isoformat(), 9.9)
    anchored = m_cpi.predict_yoy(conn, asof, ref, core=False)
    (_, mu, sg), = anchored.dist.comps
    (_, mu0, sg0), = base.dist.comps
    assert mu == pytest.approx(9.9)
    assert sg == pytest.approx(sg0)                          # sigma untouched
    assert anchored.inputs["yoy_mu"] == pytest.approx(9.9, abs=1e-3)
    assert anchored.inputs["yoy_mu_model"] == pytest.approx(mu0, abs=1e-3)
    assert anchored.inputs["nowcast_date"] == (asof - timedelta(days=1)).date().isoformat()
    # the *current* version, not a literal: the literal pin lives once, in
    # test_cpi_mom_nowcast_anchor.py. Two files pinning it means every bump edits both.
    assert anchored.model_version == m_cpi.VERSION


def test_pit_future_nowcast_does_not_anchor(env):
    conn, asof, ref = env
    _put(conn, ref, (asof + timedelta(days=1)).date().isoformat(), 9.9)
    p = m_cpi.predict_yoy(conn, asof, ref, core=False)
    (_, mu, _), = p.dist.comps
    assert mu != pytest.approx(9.9)
    assert "nowcast_date" not in p.inputs


def test_stale_and_missing_fall_back_to_internal(env):
    conn, asof, ref = env
    no_table = m_cpi.predict_yoy(conn, asof, ref, core=False)          # table absent
    cn.ensure_schema(conn)
    no_row = m_cpi.predict_yoy(conn, asof, ref, core=False)            # table empty
    stale_day = (asof - timedelta(days=m_cpi.NOWCAST_MAX_AGE_D + 1)).date().isoformat()
    _put(conn, ref, stale_day, 9.9)
    stale = m_cpi.predict_yoy(conn, asof, ref, core=False)             # feed died
    for p in (no_table, no_row, stale):
        (_, mu, _), = p.dist.comps
        assert mu != pytest.approx(9.9)
        assert "nowcast_date" not in p.inputs and "yoy_mu_model" not in p.inputs
        assert p.inputs["yoy_mu"] == pytest.approx(mu, abs=1e-3)


def test_measures_do_not_crosstalk(env):
    """Core anchors on 'corecpi', headline on 'cpi' — each ignores the other's rows."""
    conn, asof, ref = env
    day = (asof - timedelta(days=1)).date().isoformat()
    _put(conn, ref, day, 9.9, measure="cpi")
    _put(conn, ref, day, 8.8, measure="corecpi")
    (_, mu_h, _), = m_cpi.predict_yoy(conn, asof, ref, core=False).dist.comps
    (_, mu_c, _), = m_cpi.predict_yoy(conn, asof, ref, core=True).dist.comps
    assert mu_h == pytest.approx(9.9)
    assert mu_c == pytest.approx(8.8)


def test_core_with_only_headline_rows_falls_back(env):
    """A partially-populated table (headline row present, corecpi absent) must leave
    core on its internal chain, not borrow the headline number."""
    conn, asof, ref = env
    base = m_cpi.predict_yoy(conn, asof, ref, core=True)
    _put(conn, ref, (asof - timedelta(days=1)).date().isoformat(), 9.9, measure="cpi")
    p = m_cpi.predict_yoy(conn, asof, ref, core=True)
    assert p.dist.to_json() == base.dist.to_json()
    assert "nowcast_date" not in p.inputs
