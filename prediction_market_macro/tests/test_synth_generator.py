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


# ── the OTHER factor ceiling: the data dimension (#183) ──────────────────────
# Rows-per-factor says how many factors the SAMPLE can identify. It says nothing about how
# many the SPACE has room for, and `arch='factor'` needs a non-empty residual subspace
# because `sigma0 = (Z - (Z@beta0)@beta0.T).var(0) + 1e-4` is the diagonal its score uses
# outside span(V). Both failures above k = d - 1 are silent-ish and neither is dfm's to fix:
# at k > d the warm start truncates V to (d, d) while the MLP is sized on the k that was
# asked for, and at k = d the residual is exactly zero so sigma0 is the 1e-4 floor for every
# dimension and d_t reaches 1e4. gdp_quarterly (d_flat = 5) is the first panel to reach it.
def test_factor_dim_is_clamped_to_leave_a_residual_subspace():
    """k = d is not a tight fit, it is an unidentified one, and it does not raise — it
    generates. Measured on gdp_quarterly at k = d = 5: levels went to sd 11.90 with a draw at
    -172.9% annualised GDP growth, against sd 2.76 and min -10.9 at k = 4. The clamp is
    asserted through `fit` rather than on the expression so that a future refactor moving it
    into dfm's caller chain still has to keep the guarantee."""
    pdata = _toy_panel(n=400, H=4, d=1)                  # d_flat = 4
    g = G.Generator.fit(pdata, G.GenConfig(panel="toy", factor_dim=8, epochs=1))
    assert g.meta["data_dim"] == 4
    assert g.meta["factor_dim"] == 3                     # d - 1, not d, and not 8
    assert g.meta["factor_dim_requested"] == 8           # what was asked is still reported


def test_the_factor_clamp_changes_nothing_for_any_panel_that_predates_it():
    """The safety claim of #183's generator edit, stated as arithmetic rather than as a
    promise: every panel that existed before gdp_quarterly has d_flat = horizon * columns
    comfortably above the default factor_dim, so the clamp is inert on all of them and the
    four fitted artefacts stay bit-identical. gdp_quarterly is the first panel it binds on
    (5 - 1 = 4 < 8), which is why it is asserted here as the exception rather than skipped."""
    default = G.GenConfig(panel="x").factor_dim
    for name, spec in P.PANELS.items():
        d_flat = spec.horizon * len(spec.gen_columns)
        binds = d_flat - 1 < default
        assert binds is (name == "gdp_quarterly"), f"{name}: d_flat={d_flat}"


def test_load_sizes_the_net_from_what_was_fitted_not_what_was_asked(tmp_path):
    """`save` stores the whole config, including the UNCLAMPED factor_dim. Rebuilding the
    module from `cfg` would size the MLP for 8 factors and `load_state_dict` would fail on a
    tensor shape — after the artefact was written, which is the worst place to find out."""
    pdata = _toy_panel(n=400, H=4, d=1)
    g = G.Generator.fit(pdata, G.GenConfig(panel="toy", factor_dim=8, epochs=1))
    back = G.Generator.load(g.save(tmp_path / "g.pt"))
    assert back.cfg.factor_dim == 8 and back.meta["factor_dim"] == 3


# ── the whitened basis (#207 / PR-15) ────────────────────────────────────────
# `dfm`'s `arch='factor'` approximates the residual covariance by a DIAGONAL in whatever
# coordinates it is handed, and the residual is not diagonal in the panel's. §4e-D measured
# the consequence and its own falsifier in one run: whitening (`Z @ U / sqrt(lambda)`) put
# `tail` inside [0.80, 1.25] and `var/train` above 0.90 on 4/4 panels, while the plain
# eigenbasis (`Z @ U`, the `rot` arm) collapsed `sigma0` to its 1e-4 floor exactly as
# predicted and blew the dispersion out to 1.21-1.75. So `whiten` is a bool and there is no
# `rot` and no swept exponent — the arms that would need tuning are the ones that failed.
#
# NOTHING HERE ASSERTS THAT WHITENING IS BETTER. That is PR-15's question, it is answered
# end-to-end on real panels, and a toy panel cannot speak to it. What these tests pin is the
# part that must be true whatever the answer: the map is exactly invertible, it is inert when
# off, it survives a save/load round trip, and it cannot be half-loaded.
def test_whitening_round_trips_exactly_and_produces_an_identity_covariance():
    rng = np.random.default_rng(0)
    A = rng.normal(size=(12, 12))
    Z = rng.normal(size=(300, 12)) @ A.T + np.arange(12.0)
    wh = G._whiten_basis(Z)
    Zw = (Z - wh["mu"]) @ wh["fwd"]
    assert wh["rank"] == 12 and wh["dropped"] == 0
    assert np.abs(np.cov(Zw, rowvar=False) - np.eye(12)).max() < 1e-9
    assert np.abs(Zw @ wh["inv"] + wh["mu"] - Z).max() < 1e-9


def test_whitening_drops_directions_the_training_rows_never_moved_in():
    """`fit_local` exists to use 120 rows and `core_monthly`'s d_flat is 144, so a singular
    covariance is the normal case, not the pathological one. `1/sqrt(lambda)` on a direction
    with no measured variance is not a large number, it is a division by an estimate of
    nothing — those directions come out, and the round trip still has to be exact on the rows
    that defined the span, because they live in it."""
    rng = np.random.default_rng(1)
    Z = rng.normal(size=(10, 20))                     # 10 rows, 20 dims -> rank <= 9
    wh = G._whiten_basis(Z)
    assert wh["rank"] == 9 and wh["dropped"] == 11
    assert wh["fwd"].shape == (20, 9) and wh["inv"].shape == (9, 20)
    Zw = (Z - wh["mu"]) @ wh["fwd"]
    assert np.abs(Zw @ wh["inv"] + wh["mu"] - Z).max() < 1e-8
    with pytest.raises(ValueError, match="no variance in any direction"):
        G._whiten_basis(np.ones((8, 4)))


def test_whitening_is_off_by_default_and_leaves_the_fit_in_the_panel_basis():
    """The inertness claim that lets #207 land without re-validating everything that came
    before it: `whiten` defaults False, and with it False the generator carries no basis and
    works in H*d dimensions exactly as it did."""
    assert G.GenConfig(panel="toy").whiten is False
    pdata = _toy_panel(n=400, H=4, d=1)
    g = G.Generator.fit(pdata, G.GenConfig(panel="toy", factor_dim=2, epochs=1))
    assert g._whiten is None
    assert g._dim == 4 and g.meta["data_dim"] == 4
    assert "whiten_rank" not in g.meta


def test_a_whitened_fit_trains_and_samples_in_the_panel_shape():
    """The net works in the whitened space and the caller must never see it. Shape and
    finiteness only — whether the paths are BETTER is PR-15's measurement, not a unit test's."""
    pdata = _toy_panel(n=400, H=4, d=1)
    cfg = G.GenConfig(panel="toy", factor_dim=2, epochs=1, noise_steps=8, whiten=True)
    g = G.Generator.fit(pdata, cfg)
    assert g._whiten is not None
    assert g.meta["whiten_rank"] == 4 and g.meta["whiten_dropped"] == 0
    out = g.sample(np.array([1.0, 0.0]), 16, seed=3)
    assert out.shape == (16, 4, 1) and np.isfinite(out).all()


def test_a_whitened_generator_survives_save_and_load_bit_for_bit(tmp_path):
    """A basis that did not travel with the weights would produce paths wrong by a linear
    map, and they would still look like macro data — the exact failure `save`'s docstring
    exists to prevent, applied to the one thing #207 adds."""
    pdata = _toy_panel(n=400, H=4, d=1)
    cfg = G.GenConfig(panel="toy", factor_dim=2, epochs=1, noise_steps=8, whiten=True)
    g = G.Generator.fit(pdata, cfg)
    before = g.sample(np.array([1.0, 0.0]), 8, seed=11)
    back = G.Generator.load(g.save(tmp_path / "w.pt"))
    assert back._whiten is not None and back.meta["whiten_rank"] == 4
    assert np.array_equal(before, back.sample(np.array([1.0, 0.0]), 8, seed=11))


def test_load_refuses_a_whitening_artefact_whose_basis_did_not_travel(tmp_path):
    """Loading it and sampling anyway would apply the panel scaler to whitened coordinates
    and return a number for every strike without one of them looking odd. Refusing is the
    only honest option: `U` and `lambda` came from training rows the artefact does not carry,
    so there is nothing to reconstruct them from."""
    import torch
    pdata = _toy_panel(n=400, H=4, d=1)
    cfg = G.GenConfig(panel="toy", factor_dim=2, epochs=1, noise_steps=8, whiten=True)
    p = G.Generator.fit(pdata, cfg).save(tmp_path / "w.pt")
    blob = torch.load(p, map_location="cpu", weights_only=False)
    blob.pop("whiten")
    torch.save(blob, p)
    with pytest.raises(ValueError, match="carries no basis"):
        G.Generator.load(p)


def _toy_panel_end_to_end(n=200, H=4, seed=0):
    """`_toy_panel` with a condition vector `panel.condition_row` would ACTUALLY produce.

    `_toy_panel` hands `validate` a two-column condition it invented, and `validate` rebuilds
    the condition from `levels`/`inc` through `condition_row` — 3*d+2 = 5 dims — so the two
    disagree the moment `sample` checks its own scaler. That mismatch is why nothing in this
    file has ever called `validate`; it is cheap to remove and worth removing, because the
    control PR-15 rests on is a property of `validate` and cannot be tested one layer down.

    Built the way a real panel is: a regime-driven random walk in `levels`, `inc` its first
    difference, `Z` the H forward increments at each anchor, `C` `condition_row` evaluated at
    that anchor, both standardized on the panel exactly as `build` standardizes them.
    """
    rng = np.random.default_rng(seed)
    spec = P.PanelSpec(
        name="toy2", freq="MS", horizon=H, start="2000-01-01", level_lag=6,
        columns=(P.Column("x", "fred", "X", "latest", "last", "diff", "u"),))
    idx = pd.date_range("2000-01-01", periods=n + H, freq="MS")
    regime = rng.choice([-1.0, 1.0], size=n + H)
    levels = pd.DataFrame({"x": 100.0 + np.cumsum(rng.normal(regime, 0.25))}, index=idx)
    inc = pd.DataFrame({"x": levels["x"].diff().fillna(0.0)}, index=idx)
    anchors = list(idx[:n])
    step = inc["x"].to_numpy()
    Zr = np.array([step[i + 1:i + 1 + H] for i in range(n)])
    Cr = np.array([P.condition_row(levels, inc, spec, t) for t in anchors])
    mu, sd = Zr.mean(0), Zr.std(0)
    cmu, csd = Cr.mean(0), Cr.std(0)
    return P.PanelData(
        spec=spec, levels=levels, inc=inc, anchors=anchors,
        Z=(Zr - mu) / sd, C=(Cr - cmu) / csd, end=idx[-1].to_pydatetime(),
        scaler={"mu": mu, "sd": sd, "cmu": cmu, "csd": csd,
                "names": ["x"], "horizon": H, "transforms": ["diff"]})


def test_validate_local_k_fits_per_anchor_and_leaves_the_floors_bit_identical():
    """PR-15's CONTROL, asserted in the suite rather than only in the run it guards.

    The whole comparison in PR-15 is `whi` against `raw` on shared floors, and the same
    requirement applies to `fit` against `fit_local`: `boot` and `knn` are drawn from
    `seed=sd` and never touch the generator, so if any of their numbers move between two
    passes then something other than the estimator moved and the run is void. Asserting it
    here means that failure surfaces as a red test instead of as a plausible table.

    Sized at 24 held-out anchors on purpose, which is BELOW `_auc_2sample`'s 25-row refusal,
    so the C2ST leg declines on both passes and the whole test costs two seconds instead of
    two minutes. What is pinned here is the control, not the classifier: 97% of
    `_separability`'s runtime is 3000 boosted trees, and its floors and refusals already have
    their own tests above. `mem` and `dup_frac` do not decline at this size and are the legs
    that carry the claim.
    """
    pdata = _toy_panel_end_to_end(n=80, H=4)
    cfg = G.GenConfig(panel="toy2", factor_dim=2, epochs=1, noise_steps=4)
    kw = dict(holdout=0.3, folds=1, n_samples=16, seed=2, printed=False)
    glob = G.validate(pdata, cfg, **kw)
    loc = G.validate(pdata, cfg, local_k=40, **kw)
    assert glob["local_k"] is None and loc["local_k"] == 40
    assert glob["n_holdout"] == 24                      # below the C2ST's own refusal
    for tag in ("boot", "knn"):
        a, b = glob["arms"][tag], loc["arms"][tag]
        for k in ("cover50", "cover80", "ks", "moments_inside", "crps_ratio"):
            assert a[k] == b[k], f"{tag}.{k} moved between the two passes"
        for f_a, f_b in zip(glob["separability"]["folds"], loc["separability"]["folds"]):
            for k in ("mem", "dup_frac", "auc"):
                assert f_a["arms"][tag][k] == f_b["arms"][tag][k], f"{tag}.{k} moved"
    # And the thing the pass was for: the DFM arm IS a different estimator, so it is not
    # required to match — a test asserting it did would be asserting `local_k` is inert.
    # Read on CRPS and `mem` rather than on coverage: coverage is a rank frequency over 24
    # anchors, so it moves in steps of 1/24 and two genuinely different arms can tie on it.
    assert loc["arms"]["dfm"]["crps_ratio"] != glob["arms"]["dfm"]["crps_ratio"]
    assert (loc["separability"]["folds"][0]["arms"]["dfm"]["mem"]
            != glob["separability"]["folds"][0]["arms"]["dfm"]["mem"])


def test_report_names_the_estimator_and_the_basis():
    """Two runs of `validate` can describe different estimators under identical column
    headers. `fit` vs `fit_local` was the first way (#211 re-ran every floor because of it)
    and raw vs whitened is the second, so both go on the header line."""
    v = {"panel": "toy", "config_key": "k", "folds": 1, "n_train": [10], "n_holdout": 9,
         "n_samples": 4, "arms": {}}
    assert "est=fit basis=raw" in G.report(v)
    v2 = dict(v, local_k=120, cfg={"whiten": True})
    assert "est=fit_local(k=120) basis=whiten" in G.report(v2)


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


# ── the memorization anchor (#208 / PR-14) ───────────────────────────────────
# `mem` divides by the HELD-OUT block's distance to training, and #208 established on four
# production panels that this denominator is not 1.0-worth on an honest generator: draws
# from N(mu_tr, Sigma_tr), which cannot have copied anything, scored 1.166 on labor_monthly
# and 0.876 on energy_weekly — production's own two failing verdicts, in production's own
# two opposite directions, on exactly the two panels production failed. These pin that fact
# as an executable statement, in both directions, and pin that the fix did not cost the veto
# its power against verbatim plagiarism.
#
# WHY NOTHING HERE ASSERTS A THRESHOLD. There is no adopted cut on `mem_pos` — PR-14 proposed
# 0.90 and its own out-of-sample run falsified it (`MEM_POS_CUT`, §4e-G) — and there was never
# a toy-panel case for one either. `mem_pos`'s numerator is ONE draw's nn-median and that
# carries real sampling noise: measured at 40 re-draws on this very panel, an honest arm's
# `mem_pos` spans 95% [0.76, 1.38] at a 100-row pool, [0.89, 1.11] at 512, and [0.91, 1.06] at
# 1024, so even at production's `N_DRAW = 1024` the spread is as wide as the 0.10 PR-14 wanted
# to adjudicate. That measurement is why these tests assert the RATIO — `mem_pos` is closer to
# 1 than `mem` is — which is the claim that survived and the claim the code makes.
def _gauss_like(Ztr, n, seed):
    """An honest generator: independent draws matched to `Ztr`'s first two moments.

    It cannot have memorized a training row — given mu and Sigma, every draw is independent
    of every row that produced them. Whatever a memorization metric says about this pool is
    the metric's own behaviour, not the pool's.
    """
    mu = Ztr.mean(0)
    w, V = np.linalg.eigh(np.cov(Ztr, rowvar=False))
    L = V @ np.diag(np.sqrt(np.clip(w, 0.0, None)))
    return mu + np.random.default_rng(seed).standard_normal((n, Ztr.shape[1])) @ L.T


def test_mem_gauss_returns_k_undivided_medians_and_is_reproducible():
    """The anchor is `k` raw `_nn_median` values, NOT a ratio. `_separability` divides them by
    the same `base_nn` the arms divide by, and that shared denominator is the entire point —
    it cancels in `mem_pos`. Returning a pre-divided number here would silently make the
    cancellation approximate.
    """
    rng = np.random.default_rng(31)
    Ztr = rng.normal(size=(120, 4))
    g = G._mem_gauss(Ztr, 64, seed=5, k=3)
    assert len(g) == 3
    assert g == G._mem_gauss(Ztr, 64, seed=5, k=3)          # seeded, not wall-clock
    assert len(set(g)) == 3                                 # k DRAWS, not one repeated
    assert all(x > 0 for x in g)


def test_mem_gauss_declines_rather_than_inventing_an_anchor():
    """A fold too short to estimate a covariance gets `[]`, which becomes `nan` upstream and
    prints as `n/a`. A fabricated anchor would put a `mem_pos` next to every arm that reads
    like a measurement and is not one."""
    assert G._mem_gauss(np.zeros((1, 4)), 64, seed=5) == []
    assert G._mem_gauss(np.zeros((40, 4)), 0, seed=5) == []


def test_mem_falsely_calls_an_honest_generator_a_copier_when_the_holdout_drifts():
    """#208's COPY direction, reproduced small.

    The held-out block sits 1.2 away from training, so `base_nn` — `mem`'s denominator — is
    inflated by a regime break that has nothing to do with copying. An honest N(mu, Sigma)
    pool then reads far below 1 and the old band would have vetoed it. `mem_pos` divides that
    same inflation out of both sides and lands at 1.
    """
    pdata = _toy_panel(n=400, H=4, d=1, seed=41)
    pdata.Z[280:] += 1.2                                    # regime break, both sides real
    tr, te = np.arange(0, 280), np.arange(280, 400)
    rng = np.random.default_rng(42)
    pools = {"dfm": _gauss_like(pdata.Z[tr], 512, 43),      # cannot have memorized
             "boot": pdata.Z[rng.choice(tr, len(te))],
             "knn": pdata.Z[rng.choice(tr, len(te))]}
    s = G._separability(pdata, tr, te, pools, seed=44)
    a = s["arms"]["dfm"]
    assert a["mem"] < 0.8, a["mem"]                                        # the false verdict
    assert abs(a["mem_pos"] - 1.0) < 0.2, a["mem_pos"]
    assert abs(a["mem_pos"] - 1.0) < abs(a["mem"] - 1.0) / 2, (a["mem"], a["mem_pos"])


def test_mem_falsely_calls_an_honest_generator_over_dispersed_when_the_holdout_is_close():
    """#208's WIDE direction, the same defect with the sign flipped.

    Here the held-out rows are near-duplicates of training rows, so `base_nn` collapses and
    every arm's `mem` explodes. Production saw this on labor_monthly, where an honest draw
    read 1.166 and was called over-dispersed. `mem_pos` is unmoved.
    """
    pdata = _toy_panel(n=400, H=4, d=1, seed=45)
    rng = np.random.default_rng(46)
    pdata.Z[280:400] = pdata.Z[60:180] + 0.02 * rng.normal(size=(120, 4))
    tr, te = np.arange(0, 280), np.arange(280, 400)
    pools = {"dfm": _gauss_like(pdata.Z[tr], 512, 47),
             "boot": pdata.Z[rng.choice(tr, len(te))],
             "knn": pdata.Z[rng.choice(tr, len(te))]}
    s = G._separability(pdata, tr, te, pools, seed=48)
    a = s["arms"]["dfm"]
    assert a["mem"] > 1.5, a["mem"]                                        # the false verdict
    assert abs(a["mem_pos"] - 1.0) < 0.2, a["mem_pos"]
    assert abs(a["mem_pos"] - 1.0) < abs(a["mem"] - 1.0) / 2, (a["mem"], a["mem_pos"])


def test_mem_pos_still_catches_verbatim_plagiarism():
    """The whole reason the column exists. A recalibration that stopped firing on literal
    training rows would have cured the false positives by removing the test."""
    pdata = _toy_panel(n=400, H=4, d=1, seed=49)
    tr, te = np.arange(0, 280), np.arange(280, 400)
    rng = np.random.default_rng(50)
    pools = {"dfm": _gauss_like(pdata.Z[tr], 512, 51),
             "boot": pdata.Z[rng.choice(tr, len(te))],      # literal training rows
             "knn": pdata.Z[rng.choice(tr, len(te))]}
    s = G._separability(pdata, tr, te, pools, seed=52)
    assert s["arms"]["boot"]["mem"] == 0.0                  # literal rows, distance 0
    assert s["arms"]["boot"]["mem_pos"] == 0.0
    # ...and the honest arm is nowhere near it. The gap is what the column has to preserve;
    # the exact 0.0 is what makes plagiarism detectable WITHOUT a threshold, which is the only
    # memorization test left standing after PR-14 (see `MEM_POS_CUT`).
    assert s["arms"]["dfm"]["mem_pos"] > 0.5, s["arms"]["dfm"]["mem_pos"]


def test_the_anchor_changes_no_arm_number_it_sits_beside():
    """`mem` is what every number in `PLAN_DFM_SYNTH.md` was measured as. The anchor is
    ADDITIVE: it must not consume shared RNG state, reorder a pool, or shift a denominator.
    Recomputed here from the raw arrays, byte for byte.
    """
    pdata = _toy_panel(n=300, H=4, d=1, seed=53)
    tr, te = np.arange(0, 200), np.arange(200, 300)
    rng = np.random.default_rng(54)
    pools = {"dfm": _gauss_like(pdata.Z[tr], len(te), 55),
             "boot": pdata.Z[rng.choice(tr, len(te))],
             "knn": pdata.Z[rng.choice(tr, len(te))]}
    s = G._separability(pdata, tr, te, {k: p.copy() for k, p in pools.items()}, seed=56)
    Ztr = pdata.Z[tr]
    base = G._nn_median(pdata.Z[te], Ztr)
    for tag, pool in pools.items():
        uniq = pool[G._unique_rows(pool)]
        assert s["arms"][tag]["mem"] == G._nn_median(uniq, Ztr) / base
        assert s["arms"][tag]["mem_pos"] == pytest.approx(
            s["arms"][tag]["mem"] / s["mem_gauss"], rel=1e-12)
    # The anchor shares the arms' denominator exactly — that shared `base_nn` is what cancels
    # in every ratio above, and a different one would make `mem_pos` a different statistic.
    assert s["mem_gauss"] == pytest.approx(
        float(np.median(G._mem_gauss(Ztr, max(len(p[G._unique_rows(p)]) for p in pools.values()),
                                     56 + 7))) / base, rel=1e-12)
    lo, hi = s["mem_gauss_range"]
    assert lo <= s["mem_gauss"] <= hi


def test_report_prints_the_position_beside_the_raw_mem_and_refuses_to_name_a_cut():
    """Both numbers, and NO threshold.

    #208 happened because a bare `mem` column looked self-explanatory, so `mem_pos` has to be
    in the text. PR-14's 0.90 then failed out-of-sample, so the text must also say that there
    is no cut — a report that prints a number and stays silent about how to read it is how the
    next reader invents one.
    """
    v = {"panel": "toy", "config_key": "k", "folds": 1, "n_train": [10],
         "n_holdout": 5, "n_samples": 7, "arms": {}, "separability": {
             "floor_train": 0.79, "floor_boot": 0.86, "folds": [], "mem_gauss": 1.17,
             "arms": {"dfm": {"auc": 0.88, "mem": 1.06, "mem_pos": 0.91,
                              "excess_over_boot": 0.02}}}}
    text = G.report(v)
    assert "mem_pos" in text and "1.060" in text and "0.910" in text
    assert "NO CUT ON `mem_pos`" in text
    assert G.MEM_POS_CUT is None
    assert not hasattr(G, "MEM_POS_MIN")                    # the dead threshold stays dead


def test_report_survives_a_separability_block_written_before_the_anchor_existed():
    """Every `validate` JSON on disk predates PR-14 and has no `mem_pos`. Reading one has to
    print `n/a` in that column, not crash and not invent a position."""
    v = {"panel": "toy", "config_key": "k", "folds": 1, "n_train": [10],
         "n_holdout": 5, "n_samples": 7, "arms": {}, "separability": {
             "floor_train": 0.79, "floor_boot": 0.86, "folds": [],
             "arms": {"dfm": {"auc": 0.88, "mem": 1.02, "excess_over_boot": 0.02}}}}
    assert "n/a" in G.report(v)


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


# ── sample_coupled (#214, PR-20) ─────────────────────────────────────────────
def _toy_pair(seed=0):
    """A single-column hub panel and a two-column panel on the same toy clock."""
    a = _toy_panel(n=400, H=4, d=1, seed=seed)
    rng = np.random.default_rng(seed + 50)
    regime = rng.choice([-1.0, 1.0], size=400)
    Z = rng.normal(regime[:, None], 0.25, size=(400, 8))
    C = np.column_stack([regime, rng.normal(0, 1, size=400)])
    spec = P.PanelSpec(
        name="toy2", freq="MS", horizon=4, start="2000-01-01", level_lag=1,
        columns=(P.Column("p", "fred", "PP", "latest", "last", "diff", "u"),
                 P.Column("q", "fred", "QQ", "latest", "last", "diff", "u")))
    idx = pd.date_range("2000-01-01", periods=400, freq="MS")
    b = P.PanelData(
        spec=spec, levels=pd.DataFrame({"p": np.zeros(400), "q": np.zeros(400)}, index=idx),
        inc=pd.DataFrame({"p": np.zeros(400), "q": np.zeros(400)}, index=idx),
        anchors=list(idx), Z=Z, C=C, end=idx[-1].to_pydatetime(),
        scaler={"mu": np.zeros(8), "sd": np.ones(8), "cmu": np.zeros(2),
                "csd": np.ones(2), "names": ["p", "q"], "horizon": 4,
                "transforms": ["diff", "diff"]})
    cfg = G.GenConfig(panel="toy", factor_dim=2, epochs=1, noise_steps=8)
    ga = G.Generator.fit(a, cfg)
    gb = G.Generator.fit(b, G.GenConfig(panel="toy2", factor_dim=2, epochs=1,
                                        noise_steps=8))
    c = np.array([1.0, 0.0])
    return ga, gb, c


def test_sample_coupled_is_the_identity_at_rho_zero():
    """PR-20's falsifier (b), held as a unit test: with no coupling both panels must
    reproduce `sample` BIT FOR BIT — the joint loop is a copy, and this is the tripwire
    that a future edit to `_reverse` or `sample` desynchronising the copy fails loudly."""
    ga, gb, c = _toy_pair()
    ra, rb = G.sample_coupled(ga, gb, c, c, 16, rho={}, seed=9)
    assert np.array_equal(ra, ga.sample(c, 16, seed=9))
    assert np.array_equal(rb, gb.sample(c, 16, seed=9))


def test_sample_coupled_never_touches_the_hub_panels_stream():
    """Panel A's noise is read, never written, so its draw is bit-identical at EVERY rho.
    This is the structural half of PR-20's marginal-invariance criterion (B1)."""
    ga, gb, c = _toy_pair()
    ra, _ = G.sample_coupled(ga, gb, c, c, 16, rho={"p": -0.6}, seed=9)
    assert np.array_equal(ra, ga.sample(c, 16, seed=9))


def test_sample_coupled_induces_correlation_of_the_requested_sign():
    """The point of the mechanism: coupled noise -> correlated same-week increments.
    Sign only — the magnitude is the calibrated, registered quantity, not a unit test's."""
    ga, gb, c = _toy_pair()
    ra, rb = G.sample_coupled(ga, gb, c, c, 128, rho={"p": -0.9}, seed=9)
    r = np.mean([np.corrcoef(ra[:, w, 0], rb[:, w, 0])[0, 1] for w in range(4)])
    assert r < -0.15
    _, rb0 = G.sample_coupled(ga, gb, c, c, 128, rho={}, seed=9)
    r0 = np.mean([np.corrcoef(ra[:, w, 0], rb0[:, w, 0])[0, 1] for w in range(4)])
    assert abs(r0) < abs(r)


def test_sample_coupled_refuses_a_non_psd_coupling():
    """PR-20 (c): sum(rho^2) > 1 has no joint Gaussian. Refusal, not silent rescale —
    the rescale is a registered, reported act, never an automatic one."""
    ga, gb, c = _toy_pair()
    with pytest.raises(ValueError, match="PSD"):
        G.sample_coupled(ga, gb, c, c, 8, rho={"p": 0.8, "q": 0.8}, seed=9)


def test_sample_coupled_refuses_a_multi_column_hub_and_mismatched_horizons():
    """The judged mechanism has claims_weekly (d=1) as the hub and week-w meaning the
    same week on both sides; anything else is an unjudged generalisation and must not
    run silently."""
    ga, gb, c = _toy_pair()
    with pytest.raises(ValueError, match="single-column"):
        G.sample_coupled(gb, ga, c, c, 8, rho={}, seed=9)
    with pytest.raises(ValueError, match="not in panel B"):
        G.sample_coupled(ga, gb, c, c, 8, rho={"nope": -0.5}, seed=9)


# ── ar_phi (#205, §4e-P) ─────────────────────────────────────────────────────
def test_ar_phi_none_is_the_default_and_changes_nothing():
    """Every config written before the field must sample bit-identically — the same
    guarantee `whiten=False` carries, held the same way."""
    pdata = _toy_panel(n=400, H=4, d=1)
    cfg = G.GenConfig(panel="toy", factor_dim=2, epochs=1, noise_steps=8)
    g = G.Generator.fit(pdata, cfg)
    assert g._noise_mix() is None
    a = g.sample(np.array([1.0, 0.0]), 16, seed=3)
    import dataclasses
    g.cfg = dataclasses.replace(cfg, ar_phi=(0.0,))
    assert np.array_equal(a, g.sample(np.array([1.0, 0.0]), 16, seed=3))


def test_ar_phi_raises_the_paths_acf1_and_zero_leaves_it():
    """The mechanism: AR(1) noise -> more persistent increments. Sign only; the magnitude
    is §4e-P's calibrated quantity, not a unit test's."""
    import dataclasses
    pdata = _toy_panel(n=400, H=4, d=1)
    cfg = G.GenConfig(panel="toy", factor_dim=2, epochs=1, noise_steps=8)
    g = G.Generator.fit(pdata, cfg)
    a0 = G.path_stats(g.sample(np.array([1.0, 0.0]), 128, seed=3))["acf1"].mean()
    g.cfg = dataclasses.replace(cfg, ar_phi=(0.6,))
    a1 = G.path_stats(g.sample(np.array([1.0, 0.0]), 128, seed=3))["acf1"].mean()
    assert a1 > a0 + 0.05


def test_ar_phi_refuses_misalignment_and_out_of_range():
    import dataclasses
    pdata = _toy_panel(n=400, H=4, d=1)
    cfg = G.GenConfig(panel="toy", factor_dim=2, epochs=1, noise_steps=8)
    g = G.Generator.fit(pdata, cfg)
    g.cfg = dataclasses.replace(cfg, ar_phi=(0.2, 0.2))
    with pytest.raises(ValueError, match="misaligned|entries"):
        g.sample(np.array([1.0, 0.0]), 8, seed=3)
    g.cfg = dataclasses.replace(cfg, ar_phi=(1.0,))
    with pytest.raises(ValueError, match="inside"):
        g.sample(np.array([1.0, 0.0]), 8, seed=3)
