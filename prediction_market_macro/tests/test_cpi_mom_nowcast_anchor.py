"""cpi/0.4.0 — the Cleveland MoM nowcast anchor on KXCPI, and the three lines it must
not cross (PR-10, 2026-08-27).

0.3.0 anchored the YoY pair. 0.4.0 anchors the HEADLINE MoM leg on the same feed's
'mom' vintages, which had been ingested since the feed landed and read by nothing. The
evidence and the explicit KXCPICORE rejection live in `model.cpi._nowcast`'s docstring;
what is pinned here is the SHAPE of the change, because the shape is what a later edit
would break without anyone noticing:

  1. KXCPI mu becomes the nowcast, sigma is untouched, and the internal mu survives in
     inputs as `mom_mu_model` — the anchor must stay observable, or the next bias study
     has nothing to measure against.
  2. KXCPICORE is NOT anchored. PR-10 rejected it on 47 events (DM p=0.325, median gain
     -0.0035, one event carrying 101.8% of the mean). A future "let's just make it
     uniform" edit fails here.
  3. The YoY channel and the PCE bridge are untouched. Both call `_predict_mom`, and the
     anchor deliberately sits in `predict_mom` instead, so that neither inherits a change
     nothing measured on them. This is an assertion about the call graph and therefore
     exactly the kind that rots silently.

Plus the boundaries 0.3.0 already established, re-asserted on the new branch because
they are per-call-site: PIT (a nowcast published after asof must not anchor), staleness,
missing table/row, and measure separation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest import cleveland_nowcast as cn
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model import cpi as m_cpi
from prediction_market_macro.model import pce as m_pce
from prediction_market_macro.tests.test_pit import _seed_gasregw, _seed_monthly

NC = 0.37          # the anchored MoM value; far from anything the internal chain emits


@pytest.fixture()
def env(tmp_path):
    """(conn, asof, ref_month) — the test_pit CPI world, nowcast table NOT yet created.

    PCEPILFE is seeded too (test_pit's _setup_pce world, same seed) so the PCE bridge
    test below runs for real instead of skipping; an extra FRED series is inert for
    every other test here.
    """
    conn = init_db(tmp_path / "t.db")
    kt = _seed_monthly(conn, "CPIAUCSL", seed=5)
    _seed_monthly(conn, "CPILFESL", seed=6)
    _seed_monthly(conn, "PCEPILFE", seed=8)
    _seed_gasregw(conn, end_year=kt.year)
    asof = kt + timedelta(days=3)
    ref_month = (kt + timedelta(days=40)).strftime("%Y-%m")
    return conn, asof, ref_month


def _put(conn, ref_month, day, val, measure="cpi", freq="mom"):
    cn.ensure_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO cleveland_nowcast VALUES(?,?,?,?,?,?,?)",
        (measure, freq, ref_month, day, val, cn._kt(day),
         datetime.now(timezone.utc).isoformat()))
    conn.commit()


def _mu_sigma(pred):
    (_, mu, sg), = pred.dist.comps
    return mu, sg


# ── 1. the anchor itself ────────────────────────────────────────────────────
def test_headline_mom_anchor_moves_mu_only(env):
    conn, asof, ref = env
    base = m_cpi.predict_mom(conn, asof, ref, core=False)     # no table yet -> internal
    mu0, sg0 = _mu_sigma(base)
    _put(conn, ref, (asof - timedelta(days=1)).date().isoformat(), NC)
    got = m_cpi.predict_mom(conn, asof, ref, core=False)
    mu, sg = _mu_sigma(got)
    assert mu == pytest.approx(NC)
    assert sg == pytest.approx(sg0)                            # sigma untouched
    assert got.inputs["mom_mu"] == pytest.approx(NC, abs=1e-3)
    assert got.inputs["mom_mu_model"] == pytest.approx(mu0, abs=1e-3)
    assert got.inputs["nowcast_date"] == (asof - timedelta(days=1)).date().isoformat()
    assert got.model_version == m_cpi.VERSION == "cpi/0.4.0"
    # the internal chain's own diagnostics must survive the rewrap, or the gasoline
    # study loses its inputs
    assert "gas_pp" in got.inputs and "core_mu" in got.inputs


def test_predict_dispatch_carries_the_anchor(env):
    """ops/predict_all reaches cpi through predict(), not predict_mom()."""
    conn, asof, ref = env
    _put(conn, ref, (asof - timedelta(days=1)).date().isoformat(), NC)
    mu, _ = _mu_sigma(m_cpi.predict(conn, asof, ref, series="KXCPI"))
    assert mu == pytest.approx(NC)


# ── 2. core is rejected, on purpose ─────────────────────────────────────────
def test_core_mom_is_not_anchored(env):
    """PR-10: 47 events, DM p=0.325, median gain -0.0035, one event = 101.8% of the mean.
    Rejected. Seed BOTH measures so this cannot pass merely because no row existed."""
    conn, asof, ref = env
    base = m_cpi.predict_mom(conn, asof, ref, core=True)
    day = (asof - timedelta(days=1)).date().isoformat()
    _put(conn, ref, day, NC, measure="cpi")
    _put(conn, ref, day, NC, measure="corecpi")
    got = m_cpi.predict_mom(conn, asof, ref, core=True)
    assert _mu_sigma(got) == _mu_sigma(base)
    assert "nowcast_date" not in got.inputs
    assert _mu_sigma(m_cpi.predict(conn, asof, ref, series="KXCPICORE")) == _mu_sigma(base)


# ── 3. blast radius: YoY and the PCE bridge must not inherit it ─────────────
def test_yoy_channel_is_untouched_by_the_mom_anchor(env):
    """predict_yoy calls _predict_mom, which the anchor deliberately does not wrap."""
    conn, asof, ref = env
    before = {c: _mu_sigma(m_cpi.predict_yoy(conn, asof, ref, core=c))
              for c in (False, True)}
    day = (asof - timedelta(days=1)).date().isoformat()
    _put(conn, ref, day, NC, measure="cpi", freq="mom")
    _put(conn, ref, day, NC, measure="corecpi", freq="mom")
    after = {c: _mu_sigma(m_cpi.predict_yoy(conn, asof, ref, core=c))
             for c in (False, True)}
    assert after == before


def test_pce_bridge_is_untouched_by_the_mom_anchor(env):
    """model/pce.py calls cpi.predict_mom(core=True); core is unanchored, so KXPCECORE
    must be bit-identical. If core is ever anchored, this test is the thing that says
    KXPCECORE moved too -- which no PCE validation has ever measured."""
    conn, asof, ref = env
    pred = m_pce.predict(conn, asof, ref, series="KXPCECORE")
    # Only the predicted-CPI branch calls into cpi.predict_mom at all. If the ref month's
    # core CPI has already printed, the bridge reads the actual and this test would pass
    # while touching none of the code it claims to cover.
    assert pred.inputs["mode"] == "bridge_on_predicted_cpi"
    before = _mu_sigma(pred)
    day = (asof - timedelta(days=1)).date().isoformat()
    _put(conn, ref, day, NC, measure="cpi")
    _put(conn, ref, day, NC, measure="corecpi")
    assert _mu_sigma(m_pce.predict(conn, asof, ref, series="KXPCECORE")) == before


# ── 4. the 0.3.0 boundaries, re-asserted on the new call site ───────────────
def test_pit_future_nowcast_does_not_anchor(env):
    conn, asof, ref = env
    _put(conn, ref, (asof + timedelta(days=1)).date().isoformat(), NC)
    got = m_cpi.predict_mom(conn, asof, ref, core=False)
    assert _mu_sigma(got)[0] != pytest.approx(NC)
    assert "nowcast_date" not in got.inputs


def test_missing_table_missing_row_and_stale_all_fall_back(env):
    conn, asof, ref = env
    no_table = _mu_sigma(m_cpi.predict_mom(conn, asof, ref, core=False))
    cn.ensure_schema(conn)
    assert _mu_sigma(m_cpi.predict_mom(conn, asof, ref, core=False)) == no_table
    stale = (asof - timedelta(days=m_cpi.NOWCAST_MAX_AGE_D + 1)).date().isoformat()
    _put(conn, ref, stale, NC)
    got = m_cpi.predict_mom(conn, asof, ref, core=False)
    assert _mu_sigma(got) == no_table
    assert "nowcast_date" not in got.inputs
    # ...and one day fresher anchors, so the boundary is the stated one and not "always"
    fresh = (asof - timedelta(days=m_cpi.NOWCAST_MAX_AGE_D)).date().isoformat()
    _put(conn, ref, fresh, NC)
    assert _mu_sigma(m_cpi.predict_mom(conn, asof, ref, core=False))[0] == pytest.approx(NC)


def test_measure_and_freq_separation(env):
    """Headline reads ('cpi', 'mom'). A corecpi row, or the yoy row for the same month,
    must not leak into it -- the yoy value for a month is ~20x the mom value, so this
    crosstalk would be a catastrophic mu, not a subtle one."""
    conn, asof, ref = env
    base = _mu_sigma(m_cpi.predict_mom(conn, asof, ref, core=False))
    day = (asof - timedelta(days=1)).date().isoformat()
    _put(conn, ref, day, NC, measure="corecpi", freq="mom")
    assert _mu_sigma(m_cpi.predict_mom(conn, asof, ref, core=False)) == base
    _put(conn, ref, day, 9.9, measure="cpi", freq="yoy")
    assert _mu_sigma(m_cpi.predict_mom(conn, asof, ref, core=False)) == base
