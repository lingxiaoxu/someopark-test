"""cpi/0.3.0 — the Cleveland nowcast anchor on HEADLINE YoY, and its exact boundaries.

What 0.3.0 changed (module docstring carries the evidence): KXCPIYOY's yoy_mu is
REPLACED by the PIT-visible Cleveland nowcast; sigma and every other moving part stay.
The four boundaries these tests pin, each a real failure mode:

  1. anchor applied: dist mu == nowcast value, sigma == the un-anchored sigma, and
     inputs keep the internal mu as `yoy_mu_model` (observability for the bias study).
  2. PIT: a nowcast whose knowledge_time is after asof must NOT anchor — the 45-event
     validation was leak-free only because latest() filters on knowledge_time.
  3. fallback: no table (bare DBs), no row, or a stale feed (> NOWCAST_MAX_AGE_D)
     degrades to 0.2.0 behaviour, never to an exception.
  4. core untouched: KXCPICOREYOY ignores the table entirely (44-event replay was a
     wash — anchoring core was judged AGAINST, not merely omitted).
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
    assert anchored.model_version == "cpi/0.3.0"


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


def test_core_yoy_never_anchors(env):
    conn, asof, ref = env
    base = m_cpi.predict_yoy(conn, asof, ref, core=True)
    _put(conn, ref, (asof - timedelta(days=1)).date().isoformat(), 9.9, measure="cpi")
    _put(conn, ref, (asof - timedelta(days=1)).date().isoformat(), 8.8, measure="corecpi")
    p = m_cpi.predict_yoy(conn, asof, ref, core=True)
    assert p.dist.to_json() == base.dist.to_json()
    assert "nowcast_date" not in p.inputs
