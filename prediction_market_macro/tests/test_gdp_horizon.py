"""gdp/0.2.0 — the off-quarter branch must depend on HOW FAR OFF the quarter is.

The defect 0.2.0 fixes, as reported from the live board: KXGDP's 2027-01-28 / 04-29 /
07-29 contracts carried byte-identical distributions. `predict()` did read `period`, but
whenever GDPNow had no vintage for the target quarter it fell back to the CURRENT
quarter's reading as mu and widened sigma by a flat 0.5pp in quadrature — the same number
one quarter out and four. So the board anchored 2027 on a 4.03pp nowcast of 2026-Q3.

What the data says (ALFRED first prints + GDPNow vintages, PIT, ex-2020): scoring a GDPNow
reading against the advance print of Q+k gives RMSE 0.99pp at k=0 but 2.54/2.63/2.18/2.76
at k=1..4 — a step at k=1, because quarter-to-quarter growth barely persists. Shrinking mu
toward the long-run mean at phi^k cuts OOS RMSE 2.533 -> 1.57 (anchor-clustered bootstrap
+0.772pp, 90% CI [+0.395, +1.146], P=100%).

These tests pin the SHAPE of that fix rather than its fitted constants: on the live db phi
and m move with every new print, but "far out is wider and closer to the mean" must hold
for any history the estimator can be handed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model import gdp

_HOT = 6.0          # the final nowcast, deliberately far above the long-run mean


def _seed(conn, n_quarters=120, hot=_HOT):
    """Quarterly advance prints (AR(1) around 2.5) + a GDPNow vintage per quarter that
    lands BEFORE that quarter's print, so _nowcast_error_sigma has real pairs."""
    rng = np.random.default_rng(4)
    v, last_kt, last_q = 2.5, None, None
    for i in range(n_quarters):
        y, q = 1996 + i // 4, i % 4
        ev = datetime(y, q * 3 + 1, 1, tzinfo=timezone.utc)
        v = 2.5 + 0.3 * (v - 2.5) + rng.normal(0, 1.8)
        qkey = f"{y}-Q{q + 1}"
        nc_kt = ev + timedelta(days=100)                 # nowcast lands in the quarter
        lab_kt = ev + timedelta(days=120)                # advance print ~1 month later
        nc = hot if i == n_quarters - 1 else v + rng.normal(0, 1.2)
        conn.execute("INSERT OR IGNORE INTO nowcast_vintages VALUES"
                     "('GDPNow','KXGDP',?,?,?,?)",
                     (qkey, round(float(nc), 2), nc_kt.isoformat(), nc_kt.isoformat()))
        conn.execute("INSERT OR IGNORE INTO fred_obs VALUES(?,?,?,?,?,?)",
                     (gdp._LABEL_SID, ev.date().isoformat(), round(float(v), 1),
                      lab_kt.date().isoformat(), lab_kt.isoformat(), lab_kt.isoformat()))
        last_kt, last_q = nc_kt, qkey
    conn.commit()
    return last_kt, last_q


@pytest.fixture()
def seeded(tmp_path):
    conn = init_db(tmp_path / "g.db")
    nc_kt, last_q = _seed(conn)
    # asof sits after the last nowcast but BEFORE its advance print, which is exactly the
    # live situation: the current quarter is nowcast, everything after it is off-quarter.
    return conn, nc_kt + timedelta(days=1), last_q


def _periods(last_q):
    """Release-date period keys for the anchor quarter and the three quarters after it.
    _quarter_period() reads a date key as the RELEASE date and shifts back one quarter."""
    y, q = int(last_q[:4]), int(last_q[-1])
    out = []
    for k in range(4):
        ry, rq = y + (q + k) // 4, (q + k) % 4          # release falls in the NEXT quarter
        out.append(f"{ry}-{rq * 3 + 1:02d}-28")
    return out


def test_the_anchor_quarter_is_untouched(seeded):
    conn, asof, last_q = seeded
    p = gdp.predict(conn, asof, _periods(last_q)[0])
    assert p.inputs["mode"] == "gdpnow_anchor" and p.inputs["k_quarters"] == 0
    assert p.inputs["ref_quarter"] == last_q
    assert p.dist.comps[0][1] == pytest.approx(_HOT)     # k=0 still IS the nowcast
    assert p.dist.comps[0][2] == pytest.approx(
        gdp._nowcast_error_sigma(conn, asof), rel=1e-9)


def test_far_quarters_are_no_longer_byte_identical(seeded):
    """The user-visible symptom. Three contracts, three distributions."""
    conn, asof, last_q = seeded
    off = [gdp.predict(conn, asof, per) for per in _periods(last_q)[1:]]
    assert [p.inputs["k_quarters"] for p in off] == [1, 2, 3]
    assert len({p.dist.comps for p in off}) == 3, "off-quarter preds are still identical"
    assert all(p.inputs["mode"] == "gdpnow_offquarter" for p in off)


def test_mu_shrinks_toward_the_long_run_mean_with_distance(seeded):
    conn, asof, last_q = seeded
    off = [gdp.predict(conn, asof, per) for per in _periods(last_q)[1:]]
    m = off[0].inputs["ar_mean"]
    gaps = [abs(p.dist.comps[0][1] - m) for p in off]
    assert gaps == sorted(gaps, reverse=True), f"mu did not shrink monotonically: {gaps}"
    assert gaps[0] < abs(_HOT - m), "k=1 mu is no closer to the mean than the raw nowcast"
    assert 0.0 <= off[0].inputs["ar_phi"] < 0.95


def test_sigma_grows_with_distance_and_exceeds_the_anchor(seeded):
    conn, asof, last_q = seeded
    preds = [gdp.predict(conn, asof, per) for per in _periods(last_q)]
    sig = [p.dist.comps[0][2] for p in preds]
    assert sig[1] > sig[0], "an off-quarter contract must be wider than the anchor"
    assert sig[1:] == sorted(sig[1:]), f"sigma is not non-decreasing in k: {sig}"


def test_retired_offquarter_widen_is_recorded_and_inert(seeded):
    """It was an additive fudge standing in for exactly the horizon uncertainty the AR(1)
    now measures, so honouring it would double-count. Recorded, never silently dropped."""
    conn, asof, last_q = seeded
    per = _periods(last_q)[1]
    base = gdp.predict(conn, asof, per)
    got = gdp.predict(conn, asof, per, params={"offquarter_widen": 5.0})
    assert got.dist.comps == base.dist.comps
    assert got.inputs["offquarter_widen_retired"] == 5.0
    assert "offquarter_widen_retired" not in base.inputs


def test_a_short_history_shrinks_all_the_way_rather_than_fitting_noise(tmp_path):
    """Below ar_min_obs there is nothing to identify persistence with; phi goes to 0 and
    mu becomes the window mean. Defined behaviour, and the conservative direction."""
    conn = init_db(tmp_path / "short.db")
    nc_kt, last_q = _seed(conn, n_quarters=20)
    asof = nc_kt + timedelta(days=1)
    p = gdp.predict(conn, asof, _periods(last_q)[1])
    assert p.inputs["ar_phi"] == 0.0
    assert p.dist.comps[0][1] == pytest.approx(p.inputs["ar_mean"], abs=5e-4)


def test_defaults_are_the_registered_behaviour(seeded):
    conn, asof, last_q = seeded
    per = _periods(last_q)[2]
    a = gdp.predict(conn, asof, per)
    b = gdp.predict(conn, asof, per, params={})
    c = gdp.predict(conn, asof, per, params=dict(gdp.DEFAULT_PARAMS))
    assert a.inputs == b.inputs == c.inputs
    assert a.dist.comps == b.dist.comps == c.dist.comps
