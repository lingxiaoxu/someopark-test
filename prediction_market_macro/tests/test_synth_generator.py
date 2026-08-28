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


# ── the reverse-SDE start (#181B) ────────────────────────────────────────────
# `dfm.generate.reverse_sample` starts the reverse diffusion at N(0, I), which is right only
# once the forward process has reached its prior. This fork's has not: beta = 1 over T = 1.0
# leaves a_T = exp(-1/2) = 0.607, so the true marginal at the top is 0.368*Sigma + 0.632*I
# and 37% of the signal variance is still there. Because Z is standardized, diag(Sigma) = 1
# and the identity start has the right variance in every COORDINATE while being wrong in
# every DIRECTION whose eigenvalue is not 1 — which is why it survived review, and why the
# tests below are written in the eigen-basis rather than on coordinates.
class _ExactGaussianScore:
    """The exact score of N(0, diag(var)) under the forward VP marginal.

    Injected in place of a trained net so these tests have a ground truth. With the score
    exact by construction, every deviation the sampler produces belongs to the INTEGRATOR,
    which is the only thing being claimed here — no training, no seeds to tune, and a
    failure means the sampler changed rather than that a fit came out unlucky.
    """

    def __init__(self, var):
        import torch
        self.var = torch.tensor(np.asarray(var, dtype=float), dtype=torch.float32)

    def __call__(self, x, t, c):
        import torch
        a2 = torch.exp(-t).reshape(-1, 1)
        return -x / (a2 * self.var + (1.0 - a2))


def _gaussian_generator(var, noise_steps=240):
    var = np.asarray(var, dtype=float)
    d = len(var)
    cfg = G.GenConfig(panel="toy", noise_steps=noise_steps)
    scaler = {"mu": np.zeros(d), "sd": np.ones(d), "cmu": np.zeros(2), "csd": np.ones(2),
              "names": ["x"], "horizon": d, "transforms": ["diff"]}
    proj = (np.zeros(2), np.zeros((2, 0)), np.ones(0))
    # S_T is diagonal here, so its root is too — written out rather than routed through
    # `_start_root` so the test does not verify the fix against itself.
    a2 = float(np.exp(-1.0))
    root = np.diag(np.sqrt(a2 * var + (1.0 - a2)))
    return G.Generator(cfg, _ExactGaussianScore(var), scaler, {}, proj=proj,
                       start_root=root)


def test_reverse_matches_dfm_with_the_identity_start():
    """`_reverse` is a COPY of dfm's integrator, and a copy is a liability: two
    implementations of the same loop drift apart and the divergence shows up as a quiet
    change in the sample. This is the guard — identical seed, identical array, so a future
    edit to either side fails here instead of silently moving the numbers."""
    gen = _gaussian_generator([4.0, 1.0, 0.25, 0.05])
    c_z = np.zeros(1)
    mine = gen._reverse(c_z, 32, seed=5, guidance=None, start="identity")
    theirs = G._dfm()["reverse_sample"](gen.net, c_z, 32, 4, guidance=None,
                                        noise_steps=gen.cfg.noise_steps, seed=5)
    assert np.array_equal(mine, np.asarray(theirs))


_VAR4 = [4.0, 1.0, 0.5, 0.05]
# 0.5 rather than 0.25 on purpose: the identity start's error CHANGES SIGN with the
# eigenvalue, and 0.5 is where "born too wide" is unambiguous (analytic 1.034). At 0.05 the
# error has flipped back — starting at 1.0 for a direction that should start at 0.650 is
# still too wide, but by then the reverse drift is stiff enough to over-contract it. A fix
# motivated by "the sampler is uniformly too narrow" would aim at the wrong thing.


def test_identity_start_loses_the_variance_of_the_dominant_direction():
    """The defect, with the score exact so nothing can be blamed on estimation.

    A direction of variance 4 must start at 0.368*4 + 0.632 = 2.10 and starts at 1.0, and
    the reverse SDE contracts, so it never recovers: the analytic prediction is 0.63. The
    assertions below walk the whole sign change — short, right, wide, short again — because
    the shape of the error is the evidence for the diagnosis, not the magnitude alone."""
    gen = _gaussian_generator(_VAR4)
    z = gen.sample(np.zeros(2), 6000, seed=1, start="identity").reshape(6000, 4)
    got = z.var(0) / np.array(_VAR4)
    assert got[0] == pytest.approx(0.63, abs=0.06), got   # dominant: born too tight
    assert got[1] == pytest.approx(0.99, abs=0.06), got   # eigenvalue 1: the one it gets right
    assert got[2] > 1.00, got                             # L=0.5: born too WIDE
    assert got[3] < 0.95, got                             # far tail: over-contracts anyway


def test_marginal_start_recovers_it_except_in_the_far_tail():
    """Same integrator, same seed, same exact score — only the initial draw differs.

    The residual in the last coordinate is pinned rather than tolerated away. Started at the
    exactly-correct marginal, the analytic recursion still returns 0.862 for L=0.05 and 0.554
    for L=0.01: that is a SECOND, much smaller defect, and unlike the start it really is
    discretization — the reverse drift near t0 is about -1/h(t0) = -100 and production's
    dt = 0.0041 makes |coef|*dt = 0.41, which Euler tracks with a bias. It is the one place
    where `noise_steps` would buy something. Kept visible so that a later step-size change is
    graded against a number instead of a feeling."""
    gen = _gaussian_generator(_VAR4)
    z = gen.sample(np.zeros(2), 6000, seed=1, start="marginal").reshape(6000, 4)
    got = z.var(0) / np.array(_VAR4)
    assert np.allclose(got[:3], 1.0, atol=0.06), got
    assert 0.84 < got[3] < 0.96, got


def test_more_noise_steps_do_not_fix_it():
    """`noise_steps` was the obvious suspect and it is not the mechanism: the loss is in the
    initial condition, not the discretization, so a 16x finer grid buys nothing. Pinned so
    that nobody re-derives the wrong fix and pays for it in sampling time."""
    coarse = _gaussian_generator([4.0, 1.0], noise_steps=240)
    fine = _gaussian_generator([4.0, 1.0], noise_steps=3840)
    vc = coarse.sample(np.zeros(2), 4000, seed=2, start="identity").reshape(4000, 2).var(0)
    vf = fine.sample(np.zeros(2), 4000, seed=2, start="identity").reshape(4000, 2).var(0)
    assert vc[0] / 4.0 < 0.75 and vf[0] / 4.0 < 0.75
    assert abs(vc[0] - vf[0]) < 0.25


def test_start_root_is_the_marginal_at_the_top_of_the_diffusion():
    """R R' must be a_T^2 * Sigma + h_T * I, with a_T read from dfm's own config rather
    than hardcoded — the day dfm retunes its schedule this has to move with it."""
    rng = np.random.default_rng(0)
    L = rng.normal(size=(6, 6))
    Z = rng.normal(size=(500, 6)) @ L.T
    R = G._start_root(Z)
    a2 = float(np.exp(-float(G._dfm()["DIFFUSION_CONFIG"]["T"])))
    want = a2 * np.cov(Z, rowvar=False) + (1 - a2) * np.eye(6)
    assert np.allclose(R @ R.T, want, atol=1e-8)


def test_sample_refuses_the_corrected_start_when_the_artefact_predates_it():
    """A generator pickled before #181B has no training covariance and cannot be given one.
    It must say so rather than fall back to the identity, because the fallback is the bug
    and the caller is a lane that compares parameter sets on the width of this sample."""
    gen = _gaussian_generator([1.0, 1.0])
    gen._start = None
    with pytest.raises(ValueError, match="saved before #181B"):
        gen.sample(np.zeros(2), 8, seed=0)
