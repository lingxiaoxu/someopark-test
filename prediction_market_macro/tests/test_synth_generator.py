"""Tests for the parts of `synth/generator.py` that decide the project's verdict.

Deliberately scoped to the arithmetic, not the neural fit: `_crps` and `knn_bootstrap` are
what the S2 conclusion in `docs/PLAN_DFM_SYNTH.md` §4b rests on, and a sign or scale error
in either would flip that conclusion silently and plausibly. Training the score net is
covered by running `validate`, which is minutes not milliseconds and belongs in research
scripts rather than the suite.
"""
import math

import numpy as np
import pandas as pd
import pytest

from prediction_market_macro.research.synth import generator as G
from prediction_market_macro.research.synth import panel as P


# ── CRPS ─────────────────────────────────────────────────────────────────────
def test_crps_matches_the_closed_form_for_a_normal():
    """CRPS(N(mu, sigma), y) = sigma * [z(2*Phi(z) - 1) + 2*phi(z) - 1/sqrt(pi)].

    Pinning the empirical estimator against the analytic value is what catches a wrong
    constant in the E|X - X'| term — the failure that would make every arm's CRPS wrong by
    the same multiplicative factor and therefore invisible in the RATIOS the plan reports.
    """
    rng = np.random.default_rng(0)
    for sigma, y in ((1.0, 0.0), (2.5, 1.0), (0.4, -0.7)):
        draws = rng.normal(0.0, sigma, size=400_000)
        z = y / sigma
        phi = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
        Phi = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
        want = sigma * (z * (2 * Phi - 1) + 2 * phi - 1 / math.sqrt(math.pi))
        assert G._crps(draws, y) == pytest.approx(want, abs=0.01)


def test_crps_is_proper_so_a_sharper_wrong_forecast_loses():
    """The property the whole S2 gate depends on: CRPS is minimised by the TRUE law.

    KS uniformity is not — it is indifferent to sharpness, which is why a generator that
    was calibrated but uselessly wide could pass the original gate. Here a forecast that is
    correctly centred but too narrow, and one that is correctly wide but off-centre, must
    BOTH score worse than the truth, on average over draws from the truth.
    """
    rng = np.random.default_rng(1)
    ys = rng.normal(0.0, 1.0, size=4000)
    truth = rng.normal(0.0, 1.0, size=20_000)
    narrow = rng.normal(0.0, 0.3, size=20_000)
    shifted = rng.normal(0.8, 1.0, size=20_000)
    s_true = np.mean([G._crps(truth, y) for y in ys])
    assert s_true < np.mean([G._crps(narrow, y) for y in ys])
    assert s_true < np.mean([G._crps(shifted, y) for y in ys])


def test_crps_of_a_point_forecast_is_the_absolute_error():
    """Degenerate predictive: the spread term vanishes and CRPS collapses to |x - y|."""
    assert G._crps(np.full(500, 3.0), 5.0) == pytest.approx(2.0)


def test_ks_uniform_is_small_on_uniform_ranks_and_large_on_piled_ones():
    u = (np.arange(200) + 0.5) / 200.0
    assert G._ks_uniform(u) < 0.01
    assert G._ks_uniform(np.full(200, 0.5)) > 0.45


# ── the analog arm ───────────────────────────────────────────────────────────
def _toy_panel(n=400, H=4, d=1, seed=0):
    """A panel whose forward path is DETERMINED by its condition, up to noise.

    Condition column 0 is a regime label in {-1, +1}; the forward increments have mean
    equal to that label. An unconditional bootstrap of the whole history therefore has to
    straddle both regimes, while an analog draw can find the right one. That is the exact
    contrast `knn_bootstrap` exists to exploit, so it is the contrast worth pinning.
    """
    rng = np.random.default_rng(seed)
    regime = rng.choice([-1.0, 1.0], size=n)
    Z = rng.normal(regime[:, None], 0.25, size=(n, H * d))
    C = np.column_stack([regime, rng.normal(0, 1, size=n)])
    spec = P.PanelSpec(
        name="toy", freq="MS", horizon=H, start="2000-01-01", level_lag=1,
        columns=(P.Column("x", "fred", "X", "latest", "last", "diff", "u"),) * d)
    idx = pd.date_range("2000-01-01", periods=n, freq="MS")
    return P.PanelData(
        spec=spec, levels=pd.DataFrame({"x": np.zeros(n)}, index=idx),
        inc=pd.DataFrame({"x": np.zeros(n)}, index=idx), anchors=list(idx),
        Z=Z, C=C, end=idx[-1].to_pydatetime(),
        scaler={"mu": np.zeros(H * d), "sd": np.ones(H * d),
                "cmu": np.zeros(2), "csd": np.ones(2),
                "names": ["x"], "horizon": H, "transforms": ["diff"]})


def test_knn_draws_only_from_the_rows_it_was_given():
    """The purging guarantee. If `knn_bootstrap` could reach outside `rows` it would be
    sampling held-out paths during validation and every CRPS number would be a leak."""
    pdata = _toy_panel()
    rows = np.arange(0, 200)
    got = G.knn_bootstrap(pdata, rows, np.array([1.0, 0.0]), 300, k=20, seed=3)
    allowed = {tuple(np.round(r, 12)) for r in pdata.Z[rows]}
    for path in got.reshape(300, -1):
        assert tuple(np.round(path, 12)) in allowed


def test_knn_finds_the_regime_the_unconditional_bootstrap_averages_away():
    """The S2 finding in miniature: on data where the condition genuinely carries the
    answer, the analog draw beats the unconditional resample under CRPS."""
    pdata = _toy_panel()
    rows = np.arange(len(pdata.Z))
    c = np.array([1.0, 0.0])                       # ask for the +1 regime
    knn = G.knn_bootstrap(pdata, rows, c, 600, k=40, seed=5)
    boot = G.block_bootstrap(pdata, rows, 600, seed=5)
    y = float(pdata.spec.horizon * 1.0)            # a +1-regime path's cumulative move
    assert knn.sum(axis=1).mean() == pytest.approx(1.0 * pdata.spec.horizon, abs=0.25)
    assert abs(boot.sum(axis=1).mean()) < 0.5      # straddles both regimes
    assert G._crps(knn.sum(axis=1)[:, 0], y) < G._crps(boot.sum(axis=1)[:, 0], y)


def test_knn_refuses_a_neighbourhood_too_small_to_resample():
    pdata = _toy_panel()
    with pytest.raises(ValueError, match="too small"):
        G.knn_bootstrap(pdata, np.arange(100), np.array([1.0, 0.0]), 10, k=2)


# ── local fitting budgets factors out of the neighbourhood ───────────────────
def test_fit_refuses_fewer_than_six_rows_per_factor():
    """The bar `fit_local` has to respect when it shrinks k. Stated as rows-per-factor
    rather than a constant so that shrinking factor_dim legitimately shrinks the floor."""
    pdata = _toy_panel(n=60)
    cfg = G.GenConfig(panel="toy", factor_dim=8, epochs=1)
    with pytest.raises(ValueError, match="too few to fit"):
        G.Generator.fit(pdata, cfg, rows=np.arange(40))
