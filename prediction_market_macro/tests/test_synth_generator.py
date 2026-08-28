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


# ── separability (#181) ──────────────────────────────────────────────────────
# These pin the three properties that #185 got wrong, each of which produced a confident
# published verdict that later measurement reversed. They are cheap because they exercise
# `_separability` on arrays directly rather than training a score net.
def test_auc_refuses_a_sample_too_small_to_mean_anything():
    """`None`, not a number. A short fold is a real outcome and the aggregate has to be able
    to see that it happened; a filled-in 0.5 would read as 'indistinguishable' and would be
    the most flattering possible reading of no evidence."""
    rng = np.random.default_rng(0)
    assert G._auc_2sample(rng.normal(size=(20, 4)), rng.normal(size=(20, 4)), 0) is None
    assert G._auc_2sample(rng.normal(size=(60, 4)), rng.normal(size=(60, 4)), 0) is not None


def test_boot_is_never_reported_as_separable_from_itself():
    """`floor_boot` IS the `boot` arm, not a second measurement of it.

    Scoring the same pool twice under two seeds made the boot row print an excess of +0.022
    over itself — an identity violation, and the kind that quietly adds a floor's worth of
    noise to every other arm's excess.
    """
    pdata = _toy_panel(n=120, H=4, d=1, seed=1)
    tr, te = np.arange(0, 80), np.arange(80, 120)
    rng = np.random.default_rng(3)
    pools = {"dfm": rng.normal(size=(len(te), 4)),
             "boot": pdata.Z[rng.choice(tr, len(te))],
             "knn": pdata.Z[rng.choice(tr, len(te))]}
    s = G._separability(pdata, tr, te, pools, seed=5)
    assert s["floor_boot"] == s["arms"]["boot"]["auc"]
    assert s["arms"]["boot"]["excess_over_boot"] == 0.0


def test_mem_separates_a_copier_from_an_honest_sample():
    """The veto has to actually fire. A pool built from literal training rows must land far
    below 1; a pool of genuine held-out rows must land near it. Without this column every
    other number in the report can be won by memorizing."""
    pdata = _toy_panel(n=300, H=4, d=1, seed=2)
    tr, te = np.arange(0, 200), np.arange(200, 300)
    rng = np.random.default_rng(4)
    pools = {"dfm": pdata.Z[te],                          # honest: real held-out rows
             "boot": pdata.Z[rng.choice(tr, len(te))],    # copier: literal training rows
             "knn": pdata.Z[rng.choice(tr, len(te))]}
    s = G._separability(pdata, tr, te, pools, seed=6)
    assert s["arms"]["boot"]["mem"] < 0.05, s["arms"]["boot"]["mem"]
    assert s["arms"]["dfm"]["mem"] == pytest.approx(1.0, abs=0.15), s["arms"]["dfm"]["mem"]


def test_the_floor_is_not_half_when_train_and_test_differ():
    """The #185 correction, as an executable statement.

    Held-out rows drawn from a SHIFTED distribution are separable from training rows even
    though both are 'real', so `floor_train` sits far above 0.5. Any verdict that reads a
    generator's AUC against 0.5 is therefore reading it against a baseline the data does not
    support — which is exactly what the closed #185 did.
    """
    pdata = _toy_panel(n=300, H=4, d=1, seed=7)
    pdata.Z[200:] += 1.2                                  # a regime break, both sides real
    tr, te = np.arange(0, 200), np.arange(200, 300)
    rng = np.random.default_rng(8)
    pools = {"dfm": pdata.Z[te], "boot": pdata.Z[rng.choice(tr, len(te))],
             "knn": pdata.Z[rng.choice(tr, len(te))]}
    s = G._separability(pdata, tr, te, pools, seed=9)
    assert s["floor_train"] > 0.8, s["floor_train"]


def test_report_never_prints_a_c2st_without_its_floor():
    """The number and the only baseline that makes it readable travel together, in the text
    a human actually sees. Separating them is how the misreading happened the first time."""
    v = {"panel": "toy", "config_key": "k", "folds": 1, "n_train": [10],
         "n_holdout": 5, "n_samples": 7, "arms": {}, "separability": {
             "floor_train": 0.79, "floor_boot": 0.86, "folds": [],
             "arms": {"dfm": {"auc": 0.88, "mem": 1.02, "excess_over_boot": 0.02}}}}
    text = G.report(v)
    assert "0.880" in text and "floors on this panel" in text
    assert "0.860" in text and "0.790" in text


# ── joint dependence (#181, third leg) ───────────────────────────────────────
# The moment tests score each coordinate alone and the C2ST says only THAT something
# separates the samples. These pin the leg that says WHAT: a generator can match every
# marginal and still get the joint law wrong, and the joint law is what a ladder of
# contracts on one event, and several series moving together, actually consume.
def test_dependence_sees_a_broken_joint_law_that_every_marginal_test_passes():
    """Identical marginals, identical within-column persistence, destroyed co-movement.

    The broken pool permutes each column's rows independently, which leaves every coordinate's
    distribution and every within-column correlation exactly intact and removes only the
    cross-column structure. `moments` cannot see this and neither can `acf1`. If `cross` does
    not fire here the metric is measuring nothing the other legs do not.

    Scored against an HONEST pool — the same rows resampled whole, so the joint law survives —
    rather than against a threshold, which is the discipline the metric itself is built on and
    which this test failed to follow on its first draft. A hand-picked `> 0.5` looked obviously
    safe and was wrong: only 4 of the 16 cross-column pairs here are same-horizon-step pairs
    carrying real correlation, so a completely destroyed structure can only move the MEAN
    absolute error to about 4/16, and it measured 0.293. The honest reference makes the
    assertion say what is actually meant — the broken pool is far worse than a real resample —
    without my having to predict the arithmetic in advance.
    """
    rng = np.random.default_rng(11)
    n, H, d = 400, 4, 2
    a = rng.normal(size=(n, H))
    b = a + 0.05 * rng.normal(size=(n, H))        # column 1 tracks column 0 almost exactly
    Zte = np.empty((n, H * d))
    Zte[:, 0::d], Zte[:, 1::d] = a, b             # flat layout is (H, d) row-major

    broken = np.empty_like(Zte)
    broken[:, 0::d] = a[rng.permutation(n)]       # each column shuffled on its own
    broken[:, 1::d] = b[rng.permutation(n)]
    honest = Zte[rng.integers(n, size=n)]         # whole rows: the joint law survives

    d_broken, d_honest = (G._dependence(Zte, p, H, d) for p in (broken, honest))
    assert d_broken["cross"] > 5 * d_honest["cross"], (d_broken, d_honest)
    # The within-column structure is untouched by the shuffle, so this leg must NOT fire —
    # otherwise `cross` firing would prove nothing about which half of the joint law broke.
    assert d_broken["within"] <= d_honest["within"], (d_broken, d_honest)


def test_dependence_says_n_a_rather_than_zero_on_a_single_column_panel():
    """`nan`, not 0.0. A one-column panel has no cross-column pairs, so there is nothing to
    score; reporting 0.0 would read as PERFECT co-movement on the one panel where the
    question is undefined, and claims_weekly is exactly that panel."""
    rng = np.random.default_rng(12)
    dep = G._dependence(rng.normal(size=(200, 5)), rng.normal(size=(200, 5)), 5, 1)
    assert math.isnan(dep["cross"]), dep
    assert dep["within"] > 0.0


def test_boot_differences_itself_to_zero_on_dependence_too():
    """The same identity the AUC leg earns, on the new leg. `dep_boot` IS the boot arm's
    dependence, not a second measurement of it — the #185 mistake was cheap to repeat and
    cost a fold's worth of noise on every other arm's excess when it was."""
    pdata = _toy_panel(n=200, H=4, d=1, seed=13)
    tr, te = np.arange(0, 140), np.arange(140, 200)
    rng = np.random.default_rng(14)
    pools = {"dfm": rng.normal(size=(len(te), 4)),
             "boot": pdata.Z[rng.choice(tr, len(te))],
             "knn": pdata.Z[rng.choice(tr, len(te))]}
    s = G._separability(pdata, tr, te, pools, seed=15)
    assert s["dep_boot"] == s["arms"]["boot"]["dep"]
    assert s["arms"]["boot"]["dep_excess_over_boot"]["within"] == 0.0
    # `cross` is nan on this single-column panel, so its excess is nan and not 0.0. That is
    # the honest propagation of an undefined quantity, and the aggregate drops it.
    assert math.isnan(s["arms"]["boot"]["dep_excess_over_boot"]["cross"])


def test_report_never_prints_a_dependence_number_without_its_boot_reference():
    """Same discipline as the C2ST, in the text a human reads. A raw correlation error has no
    more meaning against an implicit 0 than an AUC has against an implicit 0.5: correlations
    estimated on tens of held-out rows are noisy and the noise floor is panel-specific."""
    v = {"panel": "toy", "config_key": "k", "folds": 1, "n_train": [10],
         "n_holdout": 5, "n_samples": 7, "arms": {}, "separability": {
             "floor_train": 0.79, "floor_boot": 0.86, "folds": [],
             "arms": {"dfm": {"auc": 0.88, "mem": 1.02, "excess_over_boot": 0.02,
                              "dep_within": 0.311, "dep_cross": 0.422,
                              "dep_within_excess": 0.041, "dep_cross_excess": 0.055}}}}
    text = G.report(v)
    assert "0.311" in text and "0.041" in text
    assert "0.422" in text and "0.055" in text
    assert "read against boot" in text


# ── duplicate rows in the pool (#209) ────────────────────────────────────────
def test_unique_rows_drops_verbatim_copies_and_keeps_genuine_neighbours():
    """The distinction the whole fix turns on. A VERBATIM copy carries no information about
    whether two distributions differ, so dropping it costs nothing; a near neighbour does,
    and dropping it would be thinning the sample to flatter the score. The 1e-9 rounding
    exists only so a row that survived a float round-trip still counts as the same row."""
    a = np.array([1.0, 2.0, 3.0])
    A = np.vstack([a, a, a + 1e-12, a + 1e-4, -a, a])
    keep = G._unique_rows(A)
    # rows 0/1/2/5 are one row; row 3 is 1e-4 away and is its own row; row 4 is different.
    assert keep.tolist() == [0, 3, 4]


def test_a_duplicate_heavy_pool_no_longer_scores_above_a_clean_one():
    """#209 reproduced small, and the reason `_separability` de-duplicates before scoring.

    Both pools here are drawn from the SAME real training rows, so they are drawn from one
    distribution and the honest answer for both is the panel's floor. The only difference is
    that one pool repeats 40 rows three times, which is what `block_bootstrap` and
    `knn_bootstrap` do on real folds (measured: 19-24% and 34-49% duplicates). A
    cross-validated classifier meets one copy in training and its twin in test, memorizes it,
    and scores the twin confidently.

    Asserted against the CLEAN arm rather than against a number, for the same reason every
    other metric in this file is read against `boot`: the floor is panel-specific and never
    0.5. Post-dedup the two arms must be indistinguishable; pre-dedup the gap is enormous.
    """
    pdata = _toy_panel(n=400, H=4, d=1, seed=17)
    tr, te = np.arange(0, 280), np.arange(280, 400)
    rng = np.random.default_rng(21)
    dup_heavy = np.repeat(pdata.Z[rng.choice(tr, 40, replace=False)], 3, axis=0)
    clean = pdata.Z[rng.choice(tr, len(dup_heavy), replace=False)]

    s = G._separability(pdata, tr, te, {"boot": clean, "knn": dup_heavy}, seed=21)
    assert s["arms"]["knn"]["dup_frac"] == pytest.approx(1.0 - 40 / 120)
    assert s["arms"]["boot"]["dup_frac"] == 0.0

    # `_separability` scores arms with `seed + 4`, so this is the same classifier on the same
    # rows — the ONLY difference is that production now drops the copies first.
    raw = G._auc_2sample(pdata.Z[te], dup_heavy, 21 + 4)
    assert raw - s["arms"]["boot"]["auc"] > 0.30, (raw, s["arms"])
    assert s["arms"]["knn"]["auc"] - s["arms"]["boot"]["auc"] < 0.10, s["arms"]


def test_report_shows_how_much_of_each_pool_was_copies():
    """A `knn` arm at dup 0.40 is drawing 60 distinct worlds where the header says 100. That
    is a fact about the GENERATOR, not about the test, so it is printed rather than silently
    absorbed by the de-duplication."""
    v = {"panel": "toy", "config_key": "k", "folds": 1, "n_train": [10],
         "n_holdout": 5, "n_samples": 7, "arms": {}, "separability": {
             "floor_train": 0.79, "floor_boot": 0.86, "folds": [],
             "arms": {"knn": {"auc": 0.88, "mem": 1.02, "excess_over_boot": 0.02,
                              "dup_frac": 0.417, "dep_within": 0.311, "dep_cross": 0.422,
                              "dep_within_excess": 0.041, "dep_cross_excess": 0.055}}}}
    text = G.report(v)
    assert "0.417" in text
    assert "VERBATIM copy" in text
