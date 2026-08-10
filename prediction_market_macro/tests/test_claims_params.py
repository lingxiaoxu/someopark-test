"""Every key in DEFAULT_PARAMS must actually reach the arithmetic.

Found 2026-08-04: claims.py declared five parameters and read exactly one. `predict()`
hardcoded the seasonal window (10), the seasonal clip (0.25), the vol window (27 levels)
and the sigma floor (0.02), so `params=` silently ignored four of them. The damage was
downstream: research/param_grid.py builds 3 weight schemes x 2 seasonal windows x 2 vol
windows x 2 sigma floors and calls that 24 sets, but on the live db those 24 collapsed to
**3 distinct predictions**, each duplicated 8 times. Walk-forward selection was picking
argmin over 24 columns of which 21 were copies — it could never have found anything the
level weights alone did not already give, and the search looked 8x wider than it was.

Nothing raised, nothing logged, and each individual number stayed plausible. The only
thing that could have caught it is this file: assert a parameter MOVES the output.

So the rule for every model that gets a params interface: one test per declared key,
asserting a perturbation changes the prediction. A key that cannot move the output does
not belong in DEFAULT_PARAMS, because the grid will spend its width on it regardless.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model import claims
from prediction_market_macro.tests.test_m1_claims import _seed_claims


@pytest.fixture()
def seeded(tmp_path):
    conn = init_db(tmp_path / "p.db")
    end = _seed_claims(conn)
    return conn, end + timedelta(days=1), (end + timedelta(days=6)).date().isoformat()


def _pred(seeded, **over):
    conn, asof, period = seeded
    return claims.predict(conn, asof, period, params=over or None)


# perturbation per key -> the input field it must move
PERTURB = {
    "level_weights": ((0.0, 0.0, 0.0, 1.0), "base_log"),
    "seasonal_years": (3, "seasonal"),
    "seasonal_clip": (0.0, "seasonal"),
    "vol_window": (8, "sigma_log"),
    "sigma_floor": (0.50, "sigma_log"),
}


def test_every_declared_param_is_actually_read(seeded):
    """The regression itself. Parametrised over DEFAULT_PARAMS' own keys, so adding a
    key without wiring it fails here rather than in six weeks of wasted grid search."""
    assert set(PERTURB) == set(claims.DEFAULT_PARAMS), \
        "a param was added or removed without a perturbation case"
    base = _pred(seeded)
    for key, (value, field) in PERTURB.items():
        got = _pred(seeded, **{key: value})
        assert got.inputs[field] != base.inputs[field], \
            f"{key}={value!r} did not move inputs[{field!r}] — the param is dead"


def test_the_seasonal_term_is_live_before_the_clip_test_means_anything(seeded):
    """seasonal_clip=0 only proves something if seasonal was nonzero to begin with."""
    base = _pred(seeded)
    assert base.inputs["seasonal"] != 0.0
    assert base.inputs["n_hist_weeks"] >= 3


def test_defaults_are_the_registered_behaviour(seeded):
    """params=None, params={} and params=DEFAULT_PARAMS are the same model. The health
    replay canary and claims/0.1.0's registration both depend on this path not drifting."""
    conn, asof, period = seeded
    a = claims.predict(conn, asof, period)
    b = claims.predict(conn, asof, period, params={})
    c = claims.predict(conn, asof, period, params=dict(claims.DEFAULT_PARAMS))
    assert a.inputs == b.inputs == c.inputs
    assert a.dist.comps == b.dist.comps == c.dist.comps


def test_vol_window_counts_differences_not_levels(seeded):
    """n differences needs n+1 levels. Off by one here shifts sigma on every set in the
    grid by one observation's worth, in the same direction, which is invisible."""
    conn, asof, period = seeded
    import numpy as np
    from prediction_market_macro.model.features import FeatureStore
    first, _ = FeatureStore(conn).fred_first_prints("ICSA", asof)
    for vw in (8, 13, 26):
        d = np.diff(np.log(first.tail(vw + 1).values))
        assert len(d) == vw
        expect = max(1.4826 * float(np.median(np.abs(d - np.median(d)))), 0.02)
        got = claims.predict(conn, asof, period, params={"vol_window": vw})
        assert got.inputs["sigma_log"] == pytest.approx(round(expect, 5))
