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
    "seasonal_estimator": ("median", "seasonal"),
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


# ── #197 / PR-11: the seasonal centre ────────────────────────────────────────
#
# claims/0.1.0 averaged a 10-year window that contains March 2020, when ICSA went from
# ~230k to 3.3M. Measured on the live store: ISO week 12's mean reads +0.2560 where its
# screened mean reads -0.0109, week 13 +0.2550 against -0.0152. Both clip at
# seasonal_clip and still multiply mu by 1.284; week 14 lands at +0.1652, inside the clip,
# and applies x1.18 leaving no trace. The two worst misses in the whole scored history are
# exactly those two weeks, at z = -3.72 and -3.41.
#
# So these tests are not about a tuning knob. They are about a data-quality screen, and
# what they have to pin is that it screens the regime break and NOTHING else.

_COVID_WEEK_12 = [0.0685, -0.0381, -0.0619, 2.6579, -0.0111,
                  -0.0399, 0.0111, -0.0129, -0.0103, -0.0034]   # live store, ISO week 12
_CLEAN = [v for v in _COVID_WEEK_12 if v < 1.0]
_GLOBAL_DEV = [0.0, 0.04, -0.04, 0.08, -0.08, 0.02, -0.02, 0.06, -0.06, 2.66]


def _centre(hist, spec, dev=None, k=None):
    p = {"seasonal_estimator": spec}
    return claims._seasonal_centre(hist, dev if dev is not None else _GLOBAL_DEV, p)


def test_the_registered_default_is_the_mechanism_derived_threshold():
    """claims/0.2.0's default is `mad_screen:10`, and 10 is NOT the arm that scored best
    on the 45 events — `mad_screen:6` was, by +0.069 nats/event. #192's rule is that a
    constant may not be chosen by a scan, and 10 comes from the measured MAD gap instead
    (largest non-COVID deviation 7.64, clean gap only above 16.9). A later edit that
    quietly promotes the scan winner would be reversing that reasoning without saying so,
    so the version and the value are pinned together here."""
    assert claims.DEFAULT_PARAMS["seasonal_estimator"] == "mad_screen:10"
    assert claims.VERSION == "claims/0.2.0", \
        "the default path changed; the version must move with it (health replay canary)"


def test_mean_is_still_reachable_and_is_still_the_plain_mean():
    """0.1.0's centre has to stay available, not just as history: `param_space` lists
    `mean` among the candidates so the DSR walk-forward can revert this decision on
    evidence, and a revert that did not reproduce 0.1.0 exactly would not be a revert."""
    import numpy as np
    assert "mean" in dict(claims.__dict__)["_ESTIMATORS"]
    assert _centre(_COVID_WEEK_12, "mean") == pytest.approx(float(np.mean(_COVID_WEEK_12)))


def test_the_screen_recovers_the_clean_centre_from_a_contaminated_window():
    import numpy as np
    got = _centre(_COVID_WEEK_12, "mad_screen:10")
    assert got == pytest.approx(float(np.mean(_CLEAN)), abs=1e-9), \
        "the screen must drop 2020 and average the other nine, not shrink toward zero"
    assert abs(got) < 0.02 < 0.25 == claims.DEFAULT_PARAMS["seasonal_clip"], \
        "and the result must be nowhere near the clip that used to contain the damage"


def test_the_screen_costs_nothing_on_a_clean_week():
    """This is the property that makes a screen preferable to a robust centre: on 48 of
    53 ISO weeks there is nothing to screen, and it must then BE the mean — not the
    median, which throws away efficiency on every clean week to pay for five bad ones."""
    import numpy as np
    # deliberately skewed: on a symmetric fixture the median equals the mean by
    # construction and the second assertion would pass without proving anything
    clean = [0.01, -0.02, 0.03, 0.00, -0.01, 0.02, -0.03, 0.09, 0.00, -0.01]
    assert _centre(clean, "mad_screen:10") == pytest.approx(float(np.mean(clean)))
    assert _centre(clean, "median") != pytest.approx(float(np.mean(clean)))


def test_the_scale_is_global_so_a_tight_week_does_not_screen_ordinary_variation():
    """The threshold must not be computed from this week's own ten points. A +0.15
    deviation is unremarkable — real ones reach 0.33 — but inside a week whose nine other
    years cluster within +-0.002 it is 68 of that week's own sigmas, and a within-week
    scale would discard it. Against the 3,107-observation scale it is 3.4 sigma and stays."""
    import numpy as np
    tight = [0.001, 0.002, 0.000, -0.001, 0.001, -0.002, 0.000, 0.001, -0.001, 0.15]
    wide_scale = [0.0, 0.04, -0.04, 0.08, -0.08, 0.02, -0.02, 0.06, -0.06, 0.03]
    assert _centre(tight, "mad_screen:10", dev=wide_scale) == \
        pytest.approx(float(np.mean(tight))), "0.15 is ordinary; it must survive"


def test_a_screen_that_rejects_most_of_the_window_falls_back_to_the_median():
    """Fewer than three survivors means the window is not a clean sample with one bad
    point, and averaging whatever is left would be a two-observation mean."""
    import numpy as np
    allbad = [2.0, 2.1, -2.2, 2.3, 2.4, -2.5, 2.6, 2.7, 2.8, 2.9]
    assert _centre(allbad, "mad_screen:10") == pytest.approx(float(np.median(allbad)))


def test_a_degenerate_scale_does_not_screen_everything():
    """MAD == 0 (a constant deviation series) would make every threshold zero-width and
    reject the entire window. Falling back to the mean keeps it a no-op instead."""
    import numpy as np
    assert _centre(_COVID_WEEK_12, "mad_screen:10", dev=[0.05] * 40) == \
        pytest.approx(float(np.mean(_COVID_WEEK_12)))


def test_trimmed_keeps_eight_of_ten_and_survives_one_2020():
    import numpy as np
    got = _centre(_COVID_WEEK_12, "trimmed")
    assert got == pytest.approx(float(np.mean(np.sort(_COVID_WEEK_12)[1:-1])))
    assert abs(got) < 0.05, "one 2020 in the window must not reach the output"


def test_the_threshold_has_exactly_one_spelling():
    """`mad_screen` alone and `median:5` are both rejected. Two ways to spell the same
    configuration is how a grid ends up searching one point twice and calling it two."""
    for bad in ("mad_screen", "median:5", "mean:10", "huber", "mad_screen:"):
        with pytest.raises(ValueError):
            _centre(_COVID_WEEK_12, bad)


def test_the_screen_is_in_the_default_and_the_search_is_in_the_dsr_lane_only():
    """The two-lane decision, pinned. Production predicts with `param_select.current()`
    layered on DEFAULT_PARAMS, and BOTH selection lanes score a `{}` row that means "the
    default" — so putting the screen in the default is what makes it live, and neither
    lane has to adopt anything for it to take effect.

    Searching AWAY from it belongs to the DSR-deflated lane (52 events, deflated) and not
    to the daily raw argmin (~10 events of realised dollars, no deflation), where the key
    would also evict `vol_window` and `seasonal_clip` from a cap-100 grid. If a later edit
    moves it into `param_argmin.SPACES`, that is a real decision about production and it
    should fail here rather than arrive as a diff nobody read."""
    from prediction_market_macro.research import param_argmin, param_space
    assert "seasonal_estimator" in param_space.CANDIDATES["claims"]
    assert "seasonal_estimator" not in param_argmin.SPACES["claims"]
    assert "mean" in param_space.CANDIDATES["claims"]["seasonal_estimator"][1], \
        "the DSR lane must be able to revert 0.2.0, not only move further away from it"
    assert claims.DEFAULT_PARAMS["seasonal_estimator"] in \
        param_space.CANDIDATES["claims"]["seasonal_estimator"][1], \
        "the grid must be able to return the current model as its own winner"


def test_the_estimator_never_changes_the_shape_of_Pred_inputs(seeded):
    """#196 identifies a branch by the SET of input keys. If an estimator added or
    dropped one, every prediction written after this commit would read as a different
    branch from the rows already in `preds`, and the parity veto would block adoption
    until they aged out. The values may move; the keys may not."""
    base = set(_pred(seeded).inputs)
    for spec in ("median", "trimmed", "mad_screen:10", "mad_screen:6"):
        assert set(_pred(seeded, seasonal_estimator=spec).inputs) == base
