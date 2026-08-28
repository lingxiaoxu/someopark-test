"""synth/generator.py — the conditional diffusion factor model, CALLED not modified.

`dfm/` is a research repo, not a library: its modules import each other by bare top-level
name (`from diffusion import DiffusionProcess`) and `dfm/football/` has no `__init__.py`,
so neither is importable as a package from here. `_dfm()` loads the two files this project
needs by path, with `dfm/football` ahead of `dfm` on `sys.path` for the duration — that
ordering matters, because `generate.py` does `from model import ...` and BOTH directories
contain a `model.py`. The football one is the intended target; resolving to `dfm/model.py`
would fail with a confusing AttributeError instead of an ImportError.

Three symbols are borrowed and nothing else:

* `train_conditional(Z, C, ...)` — h-weighted denoising score matching over the Lemma-1
  factor score with the condition vector concatenated into g_zeta.
* `reverse_sample(model, c_z, n, d, ...)` — Euler-Maruyama reverse SDE + Tweedie denoise.
* `make_regressed_guidance(...)` — optional ridge guidance, see `GenConfig.guidance`.

They are generic in exactly the way we need: nothing in them knows what a football match
is. The football-specific machinery (`cond_vector`, `transform`, `realize_events`, the CFA
blocks) is untouched and unused; `arch='factor'` is the exploratory PCA-warm-started
variant, which needs neither `channels` nor `n_seg`.

**What is being learned.** p(z | c), where c is the macro state at an anchor date and z is
the next H periods of increments (`panel.py`). Generation conditions on *today's* state and
re-integrates from *today's* levels, which is the user's requirement that a synthetic
sample resemble the environment the bet is actually placed in.

**What can go wrong, and what checks it.** With n ~ 400 rows the football fork measured a
real failure mode: the score net's conditional mean shrinks toward the corpus average, so
every generated path looks like the unconditional history. Here that would be fatal in a
quiet way — the samples would still be plausible macro paths, just not paths for *this*
environment, and the argmin would select a parameter set for an average decade. `validate`
therefore does not only ask "do the samples look like macro data"; it compares against an
unconditional block bootstrap of the same history, and if conditioning has bought nothing
the two score the same and that is the reported result.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sys
from dataclasses import asdict, dataclass, replace as _replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from prediction_market_macro.research.synth import panel as P

REPO = Path(__file__).resolve().parents[3]
DFM = REPO / "dfm"
FOOTBALL = DFM / "football"

_DFM_CACHE: dict[str, Any] = {}


def _dfm() -> dict[str, Any]:
    """Load `dfm/football/{model,generate}.py` by path. Cached; loads at most once."""
    if _DFM_CACHE:
        return _DFM_CACHE
    for p in (FOOTBALL / "model.py", FOOTBALL / "generate.py"):
        if not p.exists():
            raise FileNotFoundError(f"dfm not found at {p} — synth needs the dfm repo")
    saved = list(sys.path)
    sys.path[:0] = [str(FOOTBALL), str(DFM)]
    try:
        mods = {}
        for name in ("model", "generate"):
            spec = importlib.util.spec_from_file_location(name, FOOTBALL / f"{name}.py")
            mod = importlib.util.module_from_spec(spec)
            # `generate` resolves `from model import ...` through sys.modules, so the
            # football model has to be registered under its bare name before it loads.
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            mods[name] = mod
        _DFM_CACHE.update({
            "train_conditional": mods["model"].train_conditional,
            "CondFactorScoreNet": mods["model"].CondFactorScoreNet,
            "DEVICE": mods["model"].DEVICE,
            "reverse_sample": mods["generate"].reverse_sample,
            "make_regressed_guidance": mods["generate"].make_regressed_guidance,
            # read, never assumed: `_start_root` needs the diffusion horizon, and a
            # hardcoded 1.0 here would silently go wrong the day dfm retunes its schedule.
            "DIFFUSION_CONFIG": dict(mods["model"].DIFFUSION_CONFIG),
        })
    finally:
        # Leave dfm's own `model`/`generate` out of the global namespace: `dfm/model.py`
        # is a different file with the same name, and a stale entry would shadow it for
        # anything else in the process. The function objects above keep working.
        for name in ("model", "generate"):
            sys.modules.pop(name, None)
        sys.path[:] = saved
    return _DFM_CACHE


# ── configuration ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class GenConfig:
    """Everything that changes the weights. `key()` hashes it, and the hash is what the
    stored artefacts and the `synth_runs` bookkeeping are filed under — a config that has
    not been validated cannot be silently confused with one that has.

    `factor_dim` is the number of latent drivers. The panel's d is 10-12 columns over
    12-13 periods (120-156 dims) from ~400 rows, so the factor structure is doing real
    work: an unconstrained score net at that ratio memorizes. 8 is above the number of
    macro factors anyone claims to find (2-5) and well below the rank the data supports.

    `guidance='ridge'` adds the football fork's regressed guidance, which pulls each sample
    toward the ridge-predicted conditional mean for its own condition vector. It exists
    because conditional-mean shrinkage is the known failure at this sample size; whether it
    helps HERE is measured in `validate`, not assumed.

    `cond_pcs` is the number of principal components the condition vector is compressed to
    before it reaches the net, and it is the single most consequential knob here. The raw
    condition is 3*d+2 = 32-38 dims against ~300 training rows; at that ratio the net keys
    the forward path off the anchor's exact state and reproduces it, which measures as a
    strong in-sample conditional fit and a held-out calibration WORSE than resampling
    history at random (measured 2026-08-20: cover80 0.49 vs 0.77 for the block bootstrap).
    Compressing the condition is the regularizer. `cond_pcs=0` means unconditional — kept
    reachable because it is the control arm that separates "conditioning is overfitting"
    from "the sampler is broken".
    """
    panel: str
    factor_dim: int = 8
    cond_pcs: int = 4
    epochs: int = 6000
    lr: float = 1e-3
    batch: int = 64
    seed: int = 0
    noise_steps: int = 240
    guidance: str = "none"          # "none" | "ridge"
    guidance_weight: float = 0.4
    guidance_lam: float = 3.0

    def key(self) -> str:
        blob = json.dumps(asdict(self), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


# ── the generator ────────────────────────────────────────────────────────────
class Generator:
    """A trained p(z | c) plus the scaler that makes z mean something.

    The scaler is carried with the weights and never recomputed: a model trained on one
    standardization and sampled through another produces paths that are wrong by a factor
    nobody would notice, because they still look like macro data.
    """

    def __init__(self, cfg: GenConfig, net, scaler: dict, meta: dict, proj=None,
                 start_root=None):
        self.cfg, self.net, self.scaler, self.meta = cfg, net, scaler, meta
        self._ridge = None
        self._proj = proj          # (mean, loadings, scales) from `_fit_proj`
        self._start = start_root   # (d, d) root of S_T from `_start_root` (#181B)
        self.H = int(scaler["horizon"])
        self.names = list(scaler["names"])
        self.d = len(self.names)

    # -- construction ---------------------------------------------------------
    @classmethod
    def fit(cls, pdata: P.PanelData, cfg: GenConfig, rows: np.ndarray | None = None,
            verbose: bool = False) -> "Generator":
        """Train on `pdata` (optionally on a subset of its rows, for held-out validation).

        `rows` is a boolean or index array over anchors. It exists so `validate` can fit on
        an early span and score on a late one WITHOUT rebuilding the panel: rebuilding with
        an earlier `end` would also change the standardization, and then the held-out
        comparison would be measuring two different things at once.
        """
        if cfg.panel != pdata.spec.name:
            raise ValueError(f"config is for panel {cfg.panel!r}, got {pdata.spec.name!r}")
        Z, C = pdata.Z, pdata.C
        if rows is not None:
            Z, C = Z[rows], C[rows]
        # Rows per factor, not a magic constant: the previous hardcoded floor of 50 was
        # really this rule evaluated at the default factor_dim=8, and it blocked
        # `fit_local` from using a neighbourhood tighter than 50 — which is the one thing
        # local fitting exists to do. Six rows per factor is the same bar, stated so that
        # a caller who shrinks factor_dim may legitimately shrink the sample with it.
        if len(Z) < 6 * cfg.factor_dim:
            raise ValueError(f"{len(Z)} training rows is too few to fit {cfg.factor_dim} "
                             f"factors (need {6 * cfg.factor_dim}) — widen the panel, "
                             "shorten the horizon, or lower factor_dim")
        # `arch='factor'` needs a NON-EMPTY residual subspace, and dfm does not say so. Two
        # separate failures sit above k = d - 1, and only the first one announces itself:
        #
        #   k > d   the warm start is `beta0 = evecs[:, :k]` off a (d, d) eigenbasis, so `V`
        #           comes out (d, d) while `CondFactorScoreNet` sizes its MLP on the k that
        #           was ASKED for. The forward pass dies inside torch on a shape no traceback
        #           connects to a config.
        #   k = d   the factors span everything, `resid = Z - (Z@beta0)@beta0.T` is exactly
        #           zero, and `sigma0 = resid.var(0) + 1e-4` becomes the 1e-4 FLOOR for every
        #           dimension rather than a measurement. `d_t = 1/(h + alpha^2*exp(log_c))`
        #           then reaches 1e4 near t0 and the sampler blows up quietly. Measured on
        #           gdp_quarterly (d=5, k=120 neighbourhood): generated levels go from sd 2.76
        #           at k=4 to sd 11.90 at k=5, with a draw at -172.9% annualised GDP growth.
        #           Nothing raises. This is the same collapse §4e-D measured on the `rot` arm.
        #
        # dfm is call-only, so the clamp belongs here — and k >= d is the CALLER's error
        # either way: it is not a tight fit, it is an unidentified one. Bit-identical for the
        # four panels that existed before this line (d_flat 13/36/36/13 against factor_dim 8);
        # it binds for the first time on gdp_quarterly, whose d_flat is H*d = 5*1 = 5.
        fdim = max(1, min(int(cfg.factor_dim), int(Z.shape[1]) - 1))
        proj = _fit_proj(C, cfg.cond_pcs)
        Cp = _apply_proj(C, proj)
        d = _dfm()
        net = d["train_conditional"](Z, Cp, factor_dim=fdim, epochs=cfg.epochs,
                                     lr=cfg.lr, batch=cfg.batch, seed=cfg.seed,
                                     verbose=verbose, arch="factor")
        meta = {"n_train": int(len(Z)), "cond_dim": int(Cp.shape[1]),
                "cond_raw_dim": int(C.shape[1]), "factor_dim": fdim,
                "factor_dim_requested": int(cfg.factor_dim),
                "data_dim": int(Z.shape[1]), "panel_end": pdata.end.isoformat(),
                "anchor_first": str(pdata.anchors[0].date()),
                "anchor_last": str(pdata.anchors[-1].date())}
        # From the fit's OWN rows, which is what makes this self-consistent rather than a
        # peek: `fit_local` passes the k-neighbourhood, so its start is the conditional
        # marginal of that neighbourhood, and `validate` fitting on an early span gets a
        # start estimated on the early span and is still graded on the late one.
        g = cls(cfg, net, pdata.scaler, meta, proj=proj, start_root=_start_root(Z))
        g.meta["start"] = "marginal"
        if cfg.guidance == "ridge":
            g._ridge = _fit_ridge(Z, Cp, cfg.guidance_lam)
        return g

    @classmethod
    def fit_local(cls, pdata: P.PanelData, cfg: GenConfig, c_raw: np.ndarray,
                  rows: np.ndarray | None = None, k: int = 120,
                  verbose: bool = False) -> "Generator":
        """Fit UNCONDITIONALLY on the `k` training rows most similar to `c_raw`.

        This is the estimator the measurements pointed at, and the reasoning is worth
        keeping because it inverts the original design.

        `fit` asks one network to learn p(z | c) over all of history from ~27 independent
        draws, and it demonstrably cannot: measured on a purged 5-fold it is beaten by an
        unconditional bootstrap at every conditioning width including zero, and its CRPS
        against that bootstrap is 1.03 (t=+3.3) — conditioning made it actively worse. But
        `knn_bootstrap`, which is conditional and non-parametric, BEATS the same bootstrap
        by 5-10% CRPS on every panel, and the gain survives forcing every neighbour to be
        five years away in time. So the information is there; the global score net was the
        wrong way to extract it.

        Local fitting moves the conditioning out of the network and into the sample: pick
        the neighbourhood by similarity, then let the diffusion model do the one thing it
        is genuinely good at — smoothing a small empirical cloud into a continuous density
        that can emit paths which never literally happened. That last property is the whole
        reason not to simply ship `knn_bootstrap`: k neighbours are k distinct worlds, and
        a parameter search wants thousands.

        The cost is that the fit is per-condition, so this is expensive to VALIDATE (one
        fit per held-out anchor) and cheap to DEPLOY (one fit per run, on today's state).
        That asymmetry is the right way round.
        """
        idx = np.arange(len(pdata.Z)) if rows is None else np.asarray(rows)
        c_z = (np.asarray(c_raw, dtype=float) - pdata.scaler["cmu"]) / pdata.scaler["csd"]
        dist = np.linalg.norm(pdata.C[idx] - c_z[None, :], axis=1)
        take = min(k, len(idx))
        near = idx[np.argsort(dist)[:take]]
        # Factors are budgeted out of the neighbourhood, not inherited from a config sized
        # for the whole panel: `fit`'s six-rows-per-factor bar has to be met by k, and a
        # tighter neighbourhood buys its sharpness by supporting fewer factors. The data
        # dimension is the OTHER ceiling and it is not the same one — `take // 6` says how
        # many factors the sample can identify, `d - 1` says how many the space has room for
        # while leaving the residual the score's diagonal is measured on. `fit` enforces the
        # second regardless; it is repeated here only so `local_factor_dim` reports the
        # number that was actually fitted.
        fdim = max(1, min(cfg.factor_dim, take // 6, int(pdata.Z.shape[1]) - 1))
        local = _replace(cfg, cond_pcs=0, factor_dim=fdim)  # the neighbourhood IS the cond
        g = cls.fit(pdata, local, rows=near, verbose=verbose)
        g.meta["local_k"] = int(take)
        g.meta["local_factor_dim"] = int(fdim)
        g.meta["local_radius"] = float(np.sort(dist)[take - 1])
        return g

    # -- sampling -------------------------------------------------------------
    def _reverse(self, c_z: np.ndarray, n: int, seed: int, guidance, start: str):
        """`dfm.generate.reverse_sample` with the INITIAL DRAW exposed — see `_start_root`.

        dfm is call-only, and `reverse_sample` hardcodes `x = randn(n, d)`, so there is no
        way to correct the start through it. The Euler-Maruyama loop below is therefore a
        copy, and a copy is a liability: two implementations of the same integrator drift.
        The guard is `test_reverse_matches_dfm_with_the_identity_start`, which asserts this
        returns dfm's array BIT FOR BIT at `start="identity"`, so any future divergence in
        dfm fails the suite rather than quietly changing the sample.

        The correction reuses the SAME standard-normal draw and maps it, `x = base @ R.T`
        with `R R' = S_T`. Two consequences worth stating: `R = I` makes it literally the
        production draw (which is what makes the bit-for-bit test possible at all), and the
        two starts consume the identical random stream, so an A/B differs by exactly the
        linear map and by nothing else.
        """
        import torch
        d = _dfm()
        dev, dc = d["DEVICE"], d["DIFFUSION_CONFIG"]
        t0, T = float(dc["t0"]), float(dc["T"])
        ns = int(self.cfg.noise_steps)
        dim = self.d * self.H
        g = torch.Generator(device="cpu").manual_seed(int(seed))
        base = torch.randn(n, dim, generator=g)
        if start == "marginal":
            if self._start is None:
                raise ValueError(
                    "this generator has no start root: it was saved before #181B and its "
                    "training covariance was never stored, so the corrected reverse-SDE "
                    "start cannot be reconstructed. Refit it — sampling it with "
                    "start='identity' reproduces the under-dispersion bug (variance ratio "
                    "0.61-0.90 of the training marginal) and must be a deliberate choice.")
            base = base @ torch.as_tensor(self._start, dtype=torch.float32).T
        elif start != "identity":
            raise ValueError(f"start must be 'marginal' or 'identity', got {start!r}")
        x = base.to(dev)
        c = torch.tensor(np.asarray(c_z), dtype=torch.float32, device=dev)
        if c.dim() == 1:
            c = c.expand(n, -1)
        times = torch.linspace(T, t0, ns)
        dt = (T - t0) / (ns - 1)
        with torch.no_grad():
            for t in times[:-1]:
                tt = t.expand(n).to(dev)
                score = self.net(x, tt, c)
                if guidance is not None:
                    score = score + float(torch.exp(-0.5 * t)) * guidance(x)
                x = x + (0.5 * x + score) * dt + np.sqrt(dt) * torch.randn(
                    x.shape, generator=g).to(dev)
            tt = times[-1].expand(n).to(dev)
            a0 = float(np.exp(-0.5 * t0))
            score = self.net(x, tt, c)
            if guidance is not None:
                score = score + guidance(x)
            x = (x + (1 - a0 ** 2) * score) / a0
        return x.cpu().numpy()

    def sample(self, c_raw: np.ndarray, n: int, seed: int = 0,
               start: str = "marginal") -> np.ndarray:
        """(cond_dim,) raw condition -> (n, H, d) increments in natural units.

        `c_raw` comes from `panel.condition_row` and is standardized here with the training
        scaler, so callers never have to know the standardization exists.

        `start="identity"` reproduces dfm's `reverse_sample` exactly, which is the
        pre-#181B behaviour and is under-dispersed by construction. It is kept reachable
        because the A/B that established that is worth being able to re-run, and it is NOT
        the default: an under-dispersed sample makes every candidate parameter set look
        more skilful than it is, which is the one failure `validate`'s docstring calls
        disqualifying.
        """
        c_raw = np.asarray(c_raw, dtype=float)
        if c_raw.shape != (len(self.scaler["cmu"]),):
            raise ValueError(f"condition has shape {c_raw.shape}, expected "
                             f"{(len(self.scaler['cmu']),)}")
        c_z = (c_raw - self.scaler["cmu"]) / self.scaler["csd"]
        c_z = _apply_proj(c_z[None, :], self._proj)[0]
        guidance = None
        if self.cfg.guidance == "ridge":
            guidance = self._ridge_guidance(c_z, n)
        z = self._reverse(c_z, n, seed, guidance, start)
        raw = np.asarray(z, dtype=float) * self.scaler["sd"] + self.scaler["mu"]
        return raw.reshape(n, self.H, self.d)

    def level_paths(self, c_raw: np.ndarray, anchor_levels: pd.Series, n: int,
                    seed: int = 0, quantise: bool = True,
                    start: str = "marginal") -> np.ndarray:
        """(n, H, d) LEVELS in natural units, integrated forward from `anchor_levels`.

        This is what a synthetic world is written from. Integrating from the real anchor is
        what makes the sample "close to the current environment" in the only sense that
        matters to a strike ladder: the path starts at today's WTI print, not at the
        1990-2026 average.

        The result is then rounded onto the measured publication grid
        (`panel.quantise_levels`), because a synthetic world is read by settlement code:
        the level written here is compared to a Kalshi strike, and a continuous level makes
        strike boundaries that the real print can never land on. This is also the single
        choke point that fixes the validity side — `sample_printed` re-differences the same
        quantised object, so the C2ST scores what production writes rather than a smoother
        intermediate it never sees. `quantise=False` exists to reproduce the pre-fix
        behaviour for A/B measurement and for generators saved before the grid was
        measured (their scaler has no `lattice`, which is also handled: no entry means
        continuous, and nothing is rounded).
        """
        inc = self.sample(c_raw, n, seed=seed, start=start)
        spec = P.PANELS[self.cfg.panel]
        return P.integrate_paths(inc, anchor_levels, spec,
                                 self.lattice if quantise else None)

    @property
    def lattice(self) -> dict:
        """The publication grid measured on the training panel, carried in the scaler.

        `.get` rather than `[...]`: a generator pickled before #181's fix has no entry, and
        the honest behaviour there is "no grid known, quantise nothing" rather than an
        AttributeError on load of an artefact that was valid when written."""
        return dict(self.scaler.get("lattice") or {})

    def sample_printed(self, c_raw: np.ndarray, anchor_levels: pd.Series, n: int,
                       seed: int = 0, start: str = "marginal") -> np.ndarray:
        """(n, H, d) INCREMENTS implied by the quantised levels — the printed increments.

        `sample` returns the generator's raw output; this returns the increments a consumer
        would compute from the world that was actually written. They differ by exactly the
        rounding, and that difference is the whole of defect A: on `labor_monthly` the real
        increments are 100% on a grid and `sample`'s are 0%, which is separable with one
        threshold and is why the C2ST read 1.000.
        """
        lv = self.level_paths(c_raw, anchor_levels, n, seed=seed, quantise=True,
                              start=start)
        return P.to_increments(lv, anchor_levels, P.PANELS[self.cfg.panel])

    def _ridge_guidance(self, c_z: np.ndarray, n: int):
        W, v = self._ridge
        m = np.tile(np.asarray(c_z, dtype=float) @ W, (n, 1))
        import torch
        dev = _dfm()["DEVICE"]
        m_t = torch.tensor(m, dtype=torch.float32, device=dev)
        v_t = torch.tensor(v, dtype=torch.float32, device=dev)
        w = float(self.cfg.guidance_weight)

        def guidance(x):
            return -w * (x - m_t) / v_t
        return guidance

    # -- persistence ----------------------------------------------------------
    def save(self, path: Path) -> Path:
        """State dict + scaler, never a pickled module. `dfm/football/model.py` is not
        importable under a stable module name from here (see `_dfm`), so a whole-object
        `torch.save` would produce a file that only reloads if sys.path is arranged the
        same way — a trap for whoever runs this in six months."""
        import torch
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = {"cfg": asdict(self.cfg), "meta": self.meta,
                "state": self.net.state_dict(),
                "proj": {"mean": self._proj[0], "load": self._proj[1],
                         "scale": self._proj[2]},
                "scaler": {k: (np.asarray(v) if isinstance(v, np.ndarray) else v)
                           for k, v in self.scaler.items()}}
        if self._start is not None:
            blob["start_root"] = np.asarray(self._start, dtype=float)
        if self.cfg.guidance == "ridge":
            blob["ridge"] = {"W": self._ridge[0], "v": self._ridge[1]}
        torch.save(blob, path)
        return path

    @classmethod
    def load(cls, path: Path) -> "Generator":
        import torch
        blob = torch.load(Path(path), map_location="cpu", weights_only=False)
        cfg = GenConfig(**blob["cfg"])
        meta, scaler = blob["meta"], blob["scaler"]
        d = _dfm()
        # `meta["factor_dim"]` is what was FITTED; `cfg.factor_dim` is what was asked for, and
        # `fit` clamps the first to the data dimension. Rebuilding from the config would size
        # the MLP wrong for any panel where the clamp bit and fail in `load_state_dict`. The
        # fallback is for artefacts written before the clamp existed, where the two agreed by
        # construction because every panel then had d_flat > factor_dim.
        net = d["CondFactorScoreNet"](meta["data_dim"], meta["cond_dim"],
                                      factor_dim=int(meta.get("factor_dim",
                                                              cfg.factor_dim))).to(d["DEVICE"])
        net.load_state_dict(blob["state"])
        net.eval()
        p = blob["proj"]
        # A file written before #181B has no start root and cannot be given one — the
        # training rows are gone. It loads, because refusing to load an artefact that was
        # valid when written helps nobody, and `sample` raises the moment it is asked for
        # the corrected start. Silently falling back to the identity would reintroduce the
        # under-dispersion into a lane whose whole job is to compare parameter sets.
        g = cls(cfg, net, scaler, meta, proj=(p["mean"], p["load"], p["scale"]),
                start_root=blob.get("start_root"))
        if "ridge" in blob:
            g._ridge = (blob["ridge"]["W"], blob["ridge"]["v"])
        return g


def _fit_proj(C: np.ndarray, k: int):
    """PCA of the condition, fitted on TRAINING rows only -> (mean, loadings, scales).

    `k=0` returns a projection onto nothing, which `_apply_proj` turns into a single
    constant column: the net then has no conditioning signal at all. That arm is not a
    curiosity — it is the control that says whether the conditional model is beating its
    own unconditional self, and if it is not, no downstream number from this package
    should be believed.
    """
    cm = C.mean(0)
    X = C - cm
    if k <= 0:
        return cm, np.zeros((C.shape[1], 0)), np.ones(0)
    k = min(int(k), X.shape[1], max(1, X.shape[0] - 1))
    _, _, Vt = np.linalg.svd(X, full_matrices=False)
    load = Vt[:k].T
    sc = (X @ load).std(0) + 1e-12
    return cm, load, sc


def _apply_proj(C: np.ndarray, proj) -> np.ndarray:
    cm, load, sc = proj
    if load.shape[1] == 0:
        return np.zeros((len(C), 1), dtype=float)
    return ((np.asarray(C, dtype=float) - cm) @ load) / sc


def _fit_ridge(Z: np.ndarray, C: np.ndarray, lam: float):
    """Ridge of the forward path on the condition — the guidance target. Fit on TRAINING
    rows only; a ridge fitted on the held-out rows would launder the answer into the
    guidance and make the validation vacuous."""
    W = np.linalg.solve(C.T @ C + lam * np.eye(C.shape[1]), C.T @ Z)
    v = (Z - C @ W).var(0) + 0.25          # same variance floor the football fork uses
    return W, v


def _start_root(Z: np.ndarray) -> np.ndarray:
    """Matrix root of the marginal the reverse SDE is supposed to BEGIN at (#181B).

    `dfm.generate.reverse_sample` starts the reverse diffusion at `x ~ N(0, I)`. That is
    correct only once the forward process has reached its prior, and this fork's has not:
    it runs beta = 1 over T = 1.0, so `a_T = exp(-T/2) = 0.607` and the true marginal at the
    top of the diffusion is

        S_T = a_T^2 * Sigma + h_T * I = 0.368 * Sigma + 0.632 * I

    with 37% of the signal variance still present. (A standard VP schedule ramps beta 0.1
    -> 20 so that the integral is ~10 and a_T ~ 0.007; here the integral is 1.)

    Why this survived review: `Z` is standardized, so `diag(Sigma) = 1` and therefore
    `diag(S_T) = 1` — the identity start has the right marginal variance in every
    COORDINATE and is wrong in every DIRECTION that is not an eigenvector of eigenvalue 1.
    A direction of variance L must start at `0.368*L + 0.632` and starts at 1 instead, so
    the dominant factors are born too tight and the tail too wide; the reverse SDE is
    contracting, so the dominant factors never recover. Measured with the EXACT analytic
    score (the network perfect by construction), the production sampler returns 0.630 of a
    variance-4 direction and 0.539 of a realistic panel, flat across a 16x increase in
    `noise_steps` — i.e. this is not a discretization error and cannot be integrated away.
    The same integrator started here returns 0.991.

    Eigendecomposition rather than Cholesky: `Sigma` is estimated from the fit's own rows
    and can be singular when a panel has fewer rows than `H*d`, and clipping a negative
    eigenvalue to zero is the honest handling of a direction the training data never moved
    in — a Cholesky would simply raise.
    """
    a2 = float(np.exp(-float(_dfm()["DIFFUSION_CONFIG"]["T"])))
    S = a2 * np.cov(np.asarray(Z, dtype=float), rowvar=False) + (1.0 - a2) * np.eye(
        np.shape(Z)[1])
    w, V = np.linalg.eigh(S)
    return V @ np.diag(np.sqrt(np.clip(w, 0.0, None)))


# ── validation (the S2 gate) ─────────────────────────────────────────────────
def path_stats(paths: np.ndarray) -> dict[str, np.ndarray]:
    """(n, H, d) increments -> four per-path summaries, each (n, d).

    `cum` is the one that decides money: a monthly contract settles on where the level
    ends up, which is the sum of the increments. `sd` and `acf1` are what make the path
    between here and there realistic, and `acf1` in particular is the property a naive iid
    generator gets wrong — macro increments are persistent and a generator that forgets
    that produces paths that mean-revert to the anchor and understate how far things drift.
    """
    m = paths.mean(axis=1)
    s = paths.std(axis=1, ddof=0)
    cum = paths.sum(axis=1)
    a = paths - m[:, None, :]
    num = (a[:, :-1, :] * a[:, 1:, :]).sum(axis=1)
    den = (a ** 2).sum(axis=1)
    acf1 = np.divide(num, den, out=np.zeros_like(num), where=den > 1e-18)
    return {"mean": m, "sd": s, "cum": cum, "acf1": acf1}


def _boot_ci(x: np.ndarray, seed: int, reps: int = 2000, lo: float = 5.0,
             hi: float = 95.0) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), size=(reps, len(x)))
    means = x[idx].mean(axis=1)
    return float(np.percentile(means, lo)), float(np.percentile(means, hi))


def block_bootstrap(pdata: P.PanelData, rows: np.ndarray, n: int,
                    seed: int = 0) -> np.ndarray:
    """The honest baseline: draw whole historical H-blocks at random, ignoring today.

    This is what "use the history" means without any model at all. It preserves every
    within-path property exactly (autocorrelation, cross-correlation, tails) and knows
    nothing about the current environment. If the DFM cannot beat it on conditional
    calibration, the DFM is an expensive way to resample history and the honest conclusion
    is that the conditioning bought nothing.
    """
    rng = np.random.default_rng(seed)
    Z = pdata.Z[rows]
    pick = rng.integers(0, len(Z), size=n)
    raw = Z[pick] * pdata.scaler["sd"] + pdata.scaler["mu"]
    return raw.reshape(n, pdata.spec.horizon, pdata.spec.d)


def knn_bootstrap(pdata: P.PanelData, rows: np.ndarray, c_raw: np.ndarray, n: int,
                  k: int = 40, seed: int = 0) -> np.ndarray:
    """The ANALOG baseline: resample H-blocks only from the k historically most similar
    environments. A non-parametric conditional generator, and the reason it exists is to
    make the project's verdict independent of the estimator.

    `block_bootstrap` and the DFM differ in two ways at once — parametric vs not, and
    conditional vs not — so "the DFM loses to the bootstrap" cannot by itself distinguish
    "conditioning carries no information" from "the DFM is the wrong way to extract it".
    This arm is conditional and non-parametric, so it separates them:

    * knn beats boot  -> conditioning carries information; the DFM is the wrong estimator
      and the honest next step is to use the analog draw, not a neural score model.
    * knn ties boot   -> the macro state at T says nothing about the next H periods that
      the unconditional history does not already say, at this sample size. Then no
      estimator rescues it and lambda is zero on the evidence, not on one model's failure.

    Similarity is Euclidean distance in the standardized condition space, which is the same
    space the DFM conditions on — deliberately, so the two arms are given identical
    information and differ only in what they do with it. `k` trades bias against variance
    the usual way; 40 of ~300-700 rows is roughly a decile.
    """
    if k < 5:
        raise ValueError(f"knn_bootstrap: k={k} is too small to resample from")
    rng = np.random.default_rng(seed)
    c_z = (np.asarray(c_raw, dtype=float) - pdata.scaler["cmu"]) / pdata.scaler["csd"]
    Z = pdata.Z[rows]
    dist = np.linalg.norm(pdata.C[rows] - c_z[None, :], axis=1)
    near = np.argsort(dist)[:min(k, len(Z))]
    pick = near[rng.integers(0, len(near), size=n)]
    raw = Z[pick] * pdata.scaler["sd"] + pdata.scaler["mu"]
    return raw.reshape(n, pdata.spec.horizon, pdata.spec.d)


def _crps(draws: np.ndarray, y: float) -> float:
    """CRPS of an empirical predictive distribution, lower is better.

    `E|X - y| - 0.5 E|X - X'|`, the standard kernel form. This is the metric that decides
    whether conditioning is worth anything, and it is here because the first build's gate
    could not have detected it if it were: KS uniformity rewards CALIBRATION and is blind
    to SHARPNESS. A conditional generator that is calibrated and narrow is strictly more
    informative than an unconditional one that is calibrated and wide, and both score KS
    ~ 0. CRPS is proper — it is minimised by the true conditional law — so it prefers the
    narrow one exactly when the narrowness is earned.

    The second term is computed on a sorted array in O(n log n) via the identity
    `E|X - X'| = (2 / n^2) * sum_i (2i - n + 1) x_(i)`, because the naive pairwise form is
    n^2 and this is called once per held-out anchor per column per arm.
    """
    x = np.sort(np.asarray(draws, dtype=float))
    n = len(x)
    i = np.arange(n)
    spread = (2.0 / (n * n)) * np.sum((2 * i - n + 1) * x)
    return float(np.abs(x - y).mean() - 0.5 * spread)


def splits(n_rows: int, horizon: int, holdout: float = 0.25, folds: int = 1):
    """(train_idx, test_idx) pairs. `folds=1` is the out-of-time tail split; `folds>1` is
    purged blocked k-fold over the whole span.

    Both exist because they answer different questions and the first build conflated them.
    The tail split asks "does this work on a future that does not resemble the past" — on
    the monthly panel the tail is 2017-2025, which contains the entire post-2021 inflation
    regime and nothing in the training span looks like it. Failing that split is compatible
    with a perfectly good conditional model. The k-fold asks "does conditioning carry
    information at all, when the regime is represented in training" — which is the question
    that decides whether this package has a reason to exist, and it is the honest one to ask
    of a model that will be DEPLOYED trained on all history including the recent regime.

    Either way the training rows within `horizon` of a test block are purged: anchors step
    one period, so neighbouring rows share up to H-1 periods of forward path and an
    unpurged split is partly scoring the model on paths it memorized.
    """
    idx = np.arange(n_rows)
    if folds <= 1:
        cut = int(round(n_rows * (1.0 - holdout)))
        yield idx[:max(0, cut - horizon)], idx[cut:]
        return
    for b in np.array_split(idx, folds):
        lo, hi = b[0], b[-1]
        train = idx[(idx < lo - horizon) | (idx > hi + horizon)]
        yield train, b


def validate(pdata: P.PanelData, cfg: GenConfig, holdout: float = 0.25, folds: int = 1,
             n_samples: int = 400, seed: int = 0, verbose: bool = False,
             knn_k: int = 40, printed: bool = True, start: str = "marginal") -> dict:
    """Fit out-of-sample, score in-sample-free. Returns a report, never raises on a bad
    result — a generator that fails its gate is a finding, not an exception.

    The two questions asked, in order of how much they matter:

    1. **Calibration.** For each held-out anchor, where does the REAL forward path fall in
       the generated distribution for that anchor's condition? Averaged over anchors those
       ranks must be uniform. Under-dispersion (the classic diffusion-at-small-n failure)
       shows up as ranks piling at 0 and 1 and coverage far below nominal, and it is the
       failure that would make a synthetic sample actively harmful: a too-narrow generator
       makes every candidate parameter set look more skilful than it is.
    2. **Moments.** Do the generated paths have the drift, volatility and persistence of
       the real held-out paths? Judged against a bootstrap CI of the real held-out mean,
       so "inside" means indistinguishable at this sample size, not equal.

    Both are reported for the DFM and for `block_bootstrap`. The baseline is expected to
    win on moments (it IS the history) and to lose on calibration if conditioning works.

    `printed=True` scores the DFM arm on the increments implied by the QUANTISED levels
    (`Generator.sample_printed`) rather than the sampler's raw output. That is not a
    cosmetic choice about which array to grab: `worlds.py` writes levels, settlement reads
    levels, and scoring the un-quantised intermediate meant every validity number here
    described an object no consumer ever sees. `boot`/`knn` are deliberately NOT quantised,
    and the reason first given for that — "they resample real rows, so they are on the grid
    by construction" — is measurably **false**, so it is corrected here rather than left to
    mislead the next reader. Those arms splice a real increment block onto a DIFFERENT
    anchor. For an additive (`diff`) column that stays on the grid, because a grid-spaced
    anchor plus grid-spaced increments is grid-spaced — measured at 100% for `payems` and
    `unrate`. For a multiplicative (`dlog`/`pct100`) column it does not, because
    `anchor * exp(some other period's dlog)` has no reason to land on a publication grid —
    measured at **0.5%** for labor_monthly's `claims` (`/tmp/dfm_verify/fixA_score.py`).
    They stay un-quantised anyway, for the reason that actually holds: they are the CONTROL,
    and a control given the same treatment as the arm can no longer isolate what the
    treatment did.

    `start` selects the reverse-SDE initialization (`Generator._reverse`). The default is
    the corrected one; `start="identity"` reproduces dfm's `N(0, I)` and therefore the
    pre-#181B under-dispersion, and exists so that the A/B which established the fix can be
    re-run on demand rather than trusted from a doc.

    3. **Separability**, added in #181 — `out["separability"]`. Calibration and moments are
       both *marginal* checks: a generator can pass every one of them and still be trivially
       recognisable from a property no moment names. A classifier two-sample test asks the
       only question that subsumes them — can anything tell the two samples apart. Two design
       constraints, and both of them are scar tissue:

       * **Never pooled.** #185's C2ST scored `n_samples` draws per anchor against the real
         rows, so near-duplicate draws from one anchor landed on both sides of the
         classifier's own split and the AUC measured that leakage. Here each held-out anchor
         contributes exactly ONE draw, folds are scored separately and never concatenated,
         and the per-fold values are returned alongside the mean so a fold that disagrees
         cannot hide inside an average.
       * **Never without its floor.** #185 also read the AUC against an implicit 0.5. Real
         data does not score 0.5 against real data at these sample sizes: the measured
         `floor_train` (real training rows) and `floor_boot` (a real resampled block) run
         **0.44–0.86** across the four panels, and the whole DFM-vs-real verdict changes sign
         on `claims_weekly` depending on which baseline is used. So both floors are computed
         from the same rows with the same classifier on every call, and `excess_over_boot`
         — the DFM's AUC minus the resampled-history AUC — is the number to read. Zero means
         "as hard to distinguish from real as real history is". It is negative on
         `claims_weekly`.

       `mem` is the memorization guard that makes the rest of it meaningful: median
       nearest-neighbour distance from the generated pool to the training rows, over the same
       distance for the real held-out rows. Near 1 is honest; well below 1 means the sample is
       winning by copying, which improves every other number in this report at once. The band
       around 1 is measured, not assumed — see `docs/PLAN_DFM_SYNTH.md` §4e-B — and it is
       TWO-sided: above the band is over-dispersion, which is a different defect, not a pass.

       `dep_within`/`dep_cross`, the third leg, close a gap the first two leave open by
       construction. `moments` scores each coordinate alone and the C2ST says only whether
       ANYTHING separates the samples, never what. A generator can match every marginal and
       still get the joint law wrong, and for this codebase that is the consumed quantity: a
       synthetic world is read as a ladder of contracts on one event and as several series
       moving together, so the correlation structure is most of what `param_argmin` sees.
       Split in two because they fail differently — `within` is persistence resolved over
       every lag rather than acf1's lag 1, `cross` is the co-movement a per-column generator
       would lose entirely — and both carry `..._excess`, against `boot`, for the same reason
       the AUC does.
    """
    n_rows = len(pdata.anchors)
    H, d = pdata.spec.horizon, pdata.spec.d
    arms = ("dfm", "boot", "knn")
    ranks = {a: [] for a in arms}
    syn_stats = {a: [] for a in arms}
    crps = {a: [] for a in arms}
    real_all: list[np.ndarray] = []
    n_train: list[int] = []
    sep_folds: list[dict] = []
    for f, (tr, te) in enumerate(splits(n_rows, H, holdout, folds)):
        if len(te) < 10 or len(tr) < 50:
            raise ValueError(f"fold {f}: {len(tr)} train / {len(te)} test is too small "
                             "to say anything")
        n_train.append(int(len(tr)))
        gen = Generator.fit(pdata, cfg, rows=tr, verbose=verbose)
        real = (pdata.Z[te] * pdata.scaler["sd"] + pdata.scaler["mu"]).reshape(len(te), H, d)
        rs = path_stats(real)
        real_all.append(real)
        # One draw per held-out anchor, kept in the standardized flat space, for the
        # separability battery below. Collected inside the loop because it must come from
        # the SAME draws the calibration numbers are computed on — a second sampling pass
        # would be a different generator state and the two reports could disagree.
        sep_pool: dict[str, list[np.ndarray]] = {a: [] for a in arms}
        sep_rng = np.random.default_rng(seed + 6151 * (f + 1))
        for i, k in enumerate(te):
            c_raw = P.condition_row(pdata.levels, pdata.inc, pdata.spec, pdata.anchors[k])
            sd = seed + 977 * f + i
            anchor_lv = pdata.levels.loc[pdata.anchors[k]]
            dfm_draw = (gen.sample_printed(c_raw, anchor_lv, n_samples, seed=sd,
                                           start=start)
                        if printed else gen.sample(c_raw, n_samples, seed=sd, start=start))
            draws = {"dfm": dfm_draw,
                     "boot": block_bootstrap(pdata, tr, n_samples, seed=sd),
                     "knn": knn_bootstrap(pdata, tr, c_raw, n_samples, k=knn_k, seed=sd)}
            for tag, s in draws.items():
                st = path_stats(s)
                syn_stats[tag].append({k2: v.mean(axis=0) for k2, v in st.items()})
                # rank of the real cumulative move among the draws, per column
                ranks[tag].append((st["cum"] < rs["cum"][i]).mean(axis=0))
                crps[tag].append([_crps(st["cum"][:, j], rs["cum"][i, j])
                                  for j in range(d)])
                sep_pool[tag].append(np.asarray(s, dtype=float)[
                    sep_rng.integers(len(s))].reshape(-1))

        sep_folds.append(_separability(
            pdata, tr, te,
            {t: (np.asarray(v) - pdata.scaler["mu"]) / pdata.scaler["sd"]
             for t, v in sep_pool.items()},
            seed=seed + 6151 * (f + 1)))

    real_stats = path_stats(np.concatenate(real_all))
    out = {"panel": pdata.spec.name, "cfg": asdict(cfg), "config_key": cfg.key(),
           "printed": bool(printed), "start": str(start),
           "lattice": dict(pdata.lattice or {}),
           "folds": int(folds), "n_train": n_train, "knn_k": int(knn_k),
           "n_holdout": int(len(ranks["dfm"])),
           "n_samples": n_samples, "columns": pdata.spec.names, "arms": {}}
    for tag in arms:
        u = np.asarray(ranks[tag])                      # (n_holdout, d)
        cr = np.asarray(crps[tag])                      # (n_holdout, d)
        syn = {k: np.asarray([s[k] for s in syn_stats[tag]]) for k in real_stats}
        cols = {}
        for j, name in enumerate(pdata.spec.names):
            moments = {}
            for stat in ("mean", "sd", "cum", "acf1"):
                r = real_stats[stat][:, j]
                lo, hi = _boot_ci(r, seed=seed + j)
                s_mean = float(syn[stat][:, j].mean())
                moments[stat] = {"real": float(r.mean()), "synth": s_mean,
                                 "ci": [lo, hi], "inside": bool(lo <= s_mean <= hi)}
            uj = u[:, j]
            # CRPS is scale-bound, so it is reported RELATIVE to the unconditional
            # bootstrap on the same column. 1.0 means "conditioning bought nothing";
            # below 1.0 means the conditional arm is sharper where it counts.
            cols[name] = {
                "moments": moments,
                "cover80": float(((uj > 0.10) & (uj < 0.90)).mean()),
                "cover50": float(((uj > 0.25) & (uj < 0.75)).mean()),
                "ks": _ks_uniform(uj),
                "crps": float(cr[:, j].mean()),
                "crps_paired_sd": float(cr[:, j].std(ddof=1) / math.sqrt(len(cr))),
            }
        inside = sum(m["inside"] for c in cols.values() for m in c["moments"].values())
        out["arms"][tag] = {
            "columns": cols,
            "moments_inside": int(inside),
            "moments_total": int(4 * pdata.spec.d),
            "cover80": float(np.mean([c["cover80"] for c in cols.values()])),
            "cover50": float(np.mean([c["cover50"] for c in cols.values()])),
            "ks": float(np.mean([c["ks"] for c in cols.values()])),
            "crps_raw": np.asarray(crps[tag]),
        }
    # Paired CRPS against `boot`, per column and per held-out anchor. Paired because the
    # arms are scored on the SAME anchors, so the anchor-to-anchor variance — which is far
    # larger than the difference between arms — cancels. An unpaired comparison of these
    # means would be swamped by it and would call every arm a tie.
    base = out["arms"]["boot"]["crps_raw"]
    for tag in arms:
        raw = out["arms"][tag].pop("crps_raw")
        ratio, tstat = [], []
        for j, name in enumerate(pdata.spec.names):
            dj = raw[:, j] - base[:, j]
            out["arms"][tag]["columns"][name]["crps_ratio"] = float(
                raw[:, j].mean() / base[:, j].mean())
            se = dj.std(ddof=1) / math.sqrt(len(dj)) if len(dj) > 1 else float("inf")
            t = float(dj.mean() / se) if se > 0 else 0.0
            out["arms"][tag]["columns"][name]["crps_t_vs_boot"] = t
            ratio.append(raw[:, j].mean() / base[:, j].mean())
            tstat.append(t)
        out["arms"][tag]["crps_ratio"] = float(np.mean(ratio))
        out["arms"][tag]["crps_t_vs_boot"] = float(np.mean(tstat))

    # Folds are averaged for the headline and kept individually underneath. `_mean_or_none`
    # rather than `np.mean` because a short fold legitimately returns None, and a mean that
    # silently treated that as 0.0 would report a perfectly indistinguishable generator.
    def _mean_or_none(vals):
        got = [v for v in vals if v is not None]
        return float(np.mean(got)) if got else None

    def _dep_mean(tag, field, key):
        """Mean of one dependence entry across folds. Non-finite entries are dropped rather
        than averaged, because `cross` is legitimately `nan` on a single-column panel — there
        are no cross-column pairs — and `np.mean` would turn that honest non-measurement into
        a `nan` headline indistinguishable from a broken one. Empty means None, which the
        report prints as `n/a`."""
        got = [s["arms"][tag][field][key] for s in sep_folds
               if s["arms"][tag][field] is not None]
        got = [v for v in got if v is not None and np.isfinite(v)]
        return float(np.mean(got)) if got else None

    out["separability"] = {
        "folds": sep_folds,
        "floor_train": _mean_or_none([s["floor_train"] for s in sep_folds]),
        "floor_boot": _mean_or_none([s["floor_boot"] for s in sep_folds]),
        "arms": {tag: {
            "auc": _mean_or_none([s["arms"][tag]["auc"] for s in sep_folds]),
            "dup_frac": _mean_or_none([s["arms"][tag]["dup_frac"] for s in sep_folds]),
            "mem": _mean_or_none([s["arms"][tag]["mem"] for s in sep_folds]),
            "excess_over_boot": _mean_or_none(
                [s["arms"][tag]["excess_over_boot"] for s in sep_folds]),
            "dep_within": _dep_mean(tag, "dep", "within"),
            "dep_cross": _dep_mean(tag, "dep", "cross"),
            "dep_within_excess": _dep_mean(tag, "dep_excess_over_boot", "within"),
            "dep_cross_excess": _dep_mean(tag, "dep_excess_over_boot", "cross"),
        } for tag in arms},
    }
    return out


def _auc_2sample(real: np.ndarray, synth: np.ndarray, seed: int) -> float | None:
    """Cross-validated classifier AUC separating `real` from `synth`. `None` when there are
    too few rows for the answer to mean anything, which is a real outcome on short folds and
    is reported as such rather than filled with a number."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import StratifiedKFold

    n = min(len(real), len(synth))
    if n < 25:
        return None
    rng = np.random.default_rng(seed)
    X = np.vstack([np.asarray(real)[rng.choice(len(real), n, replace=False)],
                   np.asarray(synth)[rng.choice(len(synth), n, replace=False)]])
    y = np.r_[np.ones(n), np.zeros(n)]
    aucs = []
    for a, b in StratifiedKFold(5, shuffle=True, random_state=seed % (2 ** 31)).split(X, y):
        clf = HistGradientBoostingClassifier(max_iter=200, min_samples_leaf=5,
                                             random_state=seed % (2 ** 31))
        clf.fit(X[a], y[a])
        aucs.append(roc_auc_score(y[b], clf.predict_proba(X[b])[:, 1]))
    return float(np.mean(aucs))


def _nn_median(A: np.ndarray, B: np.ndarray, chunk: int = 256) -> float:
    """Median over `A` of the distance to the nearest row of `B`. Chunked because the panels
    are small in rows but the pairwise block is d_flat-wide and there is no reason to
    materialize it whole."""
    out = np.empty(len(A))
    for i in range(0, len(A), chunk):
        blk = A[i:i + chunk]
        out[i:i + chunk] = np.sqrt(((blk[:, None, :] - B[None, :, :]) ** 2).sum(-1).min(1))
    return float(np.median(out))


def _unique_rows(A: np.ndarray) -> np.ndarray:
    """Indices of the first occurrence of each distinct row, in order.

    Rows are keyed on their bytes after rounding to 1e-9, so two rows differing only by float
    noise still count as one row — which is what is wanted here, because the thing being
    removed is a VERBATIM copy, not a near neighbour.

    Why this has to run before any classifier sees the pool (#209). `block_bootstrap` copies
    whole rows, so two held-out anchors that draw the same training row put the same vector in
    the pool twice; `knn_bootstrap` draws from a 40-neighbour candidate set and collides far
    harder. Measured on the real fold structure, `boot` pools are 19-24% duplicates and `knn`
    pools 34-49%. A cross-validated classifier then meets one copy in training and its twin in
    test, memorizes it, and scores the twin confidently — which INFLATES the AUC by 0.08-0.25
    points on the arms that resample real history.

    That is not a small bookkeeping issue, because `boot` IS the floor every other arm is read
    against: an inflated floor makes every `excess_over_boot` too negative and flatters the
    DFM. It is the #185 error with the sign reversed — that one read the AUC against a floor
    that was too low and condemned the generator.

    §4d diagnosed the same corruption in its cross-class form (a row held out in one fold is a
    training row in the next, so it carries both labels) and concluded that a per-fold C2ST
    that never pools was the cure. It is not: per-fold scoring removes the label straddling —
    `prod_dupes.py` confirms zero cross-class copies — and leaves the within-class duplicates
    untouched. `boot_twin.py` isolates that half with real rows and no model at all: two
    disjoint halves of real history, zero shared rows, score 0.712 against each other once one
    side is bootstrap-resampled.

    Duplicates carry no information about whether two DISTRIBUTIONS differ, which is the only
    question a C2ST asks, so dropping them costs nothing and is not a fudge.
    """
    seen: set[bytes] = set()
    keep = []
    for i, r in enumerate(np.asarray(A, dtype=float)):
        k = r.round(9).tobytes()
        if k not in seen:
            seen.add(k)
            keep.append(i)
    return np.asarray(keep, dtype=int)


def _dependence(Zte: np.ndarray, pool: np.ndarray, H: int, d: int) -> dict:
    """Mean absolute correlation error, split into WITHIN-column and CROSS-column entries.

    The third leg of #181, and the one the moment tests cannot see. `moments` scores each
    coordinate's mean/sd/skew/kurtosis on its own, and `separability` answers whether
    ANYTHING distinguishes the samples but not WHAT. A generator can match every marginal and
    still get the joint law wrong, which for this codebase is not an abstract concern: a
    synthetic world is consumed as a LADDER of contracts on the same event and as several
    series moving together, so the dependence structure is most of what the parameter argmin
    is actually reading.

    Two numbers rather than one because they fail for different reasons and admit different
    fixes:

    * `within` — pairs of horizon steps inside ONE column. This is the persistence structure,
      the same thing `acf1` measures at lag 1, resolved over every lag at once.
    * `cross` — pairs spanning two different columns. This is the co-movement the panel exists
      to capture, and it is the one a per-column generator would lose entirely.

    `nan` for `cross` on a single-column panel (claims_weekly) is the honest answer, not a
    missing measurement — there are no cross-column pairs to score.

    The reference is the HELD-OUT correlation matrix. Correlations estimated on tens of rows
    are noisy, so the absolute level of these numbers means little on its own and they must be
    read against the `boot` arm's value, exactly like the C2ST is read against its floor.
    `validate` reports `excess_over_boot` for that reason.
    """
    iu = np.triu_indices(H * d, k=1)
    # `same[i, j]` is True when flat coordinates i and j belong to the same column. Z is laid
    # out (H, d) row-major, so the column index of flat coordinate k is k % d.
    col = np.arange(H * d) % d
    within = (col[:, None] == col[None, :])[iu]

    err = np.abs(np.corrcoef(pool, rowvar=False)[iu]
                 - np.corrcoef(Zte, rowvar=False)[iu])
    return {"within": float(err[within].mean()) if within.any() else float("nan"),
            "cross": float(err[~within].mean()) if (~within).any() else float("nan")}


def _separability(pdata: P.PanelData, tr: np.ndarray, te: np.ndarray,
                  pools: dict[str, np.ndarray], seed: int) -> dict:
    """One fold of the #181 separability battery. See `validate`'s docstring for why this is
    never pooled across folds and never reported without its floors."""
    Ztr, Zte = pdata.Z[tr], pdata.Z[te]
    base_nn = _nn_median(Zte, Ztr)

    # De-duplicate ONCE, here, so all three legs below score the same pool. `dup_frac` is kept
    # and reported: a `knn` pool that was half copies is a thinner sample than its row count
    # claims, and the reader has to be able to see that rather than infer it. See
    # `_unique_rows` for why this is a correctness fix and not a convenience.
    dup_frac = {tag: 1.0 - len(_unique_rows(p)) / len(p) if len(p) else float("nan")
                for tag, p in pools.items()}
    pools = {tag: np.asarray(p, dtype=float)[_unique_rows(p)] for tag, p in pools.items()}

    # The floors, measured with the identical classifier on the identical real rows. Held-out
    # real against a same-sized sample of real TRAINING rows is the strictest honest floor:
    # both sides are real, so whatever the classifier finds is the train/test regime
    # difference and nothing a generator could remove.
    rng = np.random.default_rng(seed + 1)
    idx = rng.choice(len(Ztr), min(len(Ztr), len(Zte)), replace=False)

    # Every arm is scored at the SAME seed. `floor_boot` is not a separate measurement of the
    # `boot` arm, it IS the `boot` arm's AUC — scoring it twice under two seeds made the boot
    # row report a 0.022 excess over itself, which is a number that cannot exist.
    aucs = {tag: _auc_2sample(Zte, pool, seed + 4) for tag, pool in pools.items()}
    floor_boot = aucs.get("boot")

    # The joint-dependence leg, scored against the SAME held-out rows as everything above and
    # referenced to the SAME `boot` arm. It gets its own excess because a raw correlation
    # error has no more meaning against an implicit 0 than an AUC has against an implicit 0.5
    # — the reference is what a real resampled block scores, and that is panel-specific.
    H, d = pdata.spec.horizon, pdata.spec.d
    deps = {tag: _dependence(Zte, pool, H, d) for tag, pool in pools.items()}
    dep_boot = deps.get("boot")

    out = {"n_real": int(len(Zte)),
           "floor_train": _auc_2sample(Zte, Ztr[idx], seed + 4),
           "floor_boot": floor_boot,
           "dep_boot": dep_boot,
           "arms": {}}
    for tag, pool in pools.items():
        auc = aucs[tag]
        out["arms"][tag] = {
            "auc": auc,
            "dup_frac": dup_frac[tag],
            "mem": float(_nn_median(pool, Ztr) / base_nn) if base_nn > 0 else float("nan"),
            "excess_over_boot": (None if auc is None or floor_boot is None
                                 else float(auc - floor_boot)),
            "dep": deps[tag],
            # Subtraction, not a ratio: the boot value can be near zero on a well-behaved
            # panel and a ratio would explode there. `boot` differences itself to exactly
            # 0.0, which is the identity check the AUC leg already earns by construction.
            "dep_excess_over_boot": (None if dep_boot is None else
                                     {k: float(deps[tag][k] - dep_boot[k])
                                      for k in ("within", "cross")}),
        }
    return out


def _ks_uniform(u: np.ndarray) -> float:
    """One-sample KS distance of the rank sequence from Uniform(0,1). Small is calibrated;
    a systematically under-dispersed generator drives this toward 0.5."""
    x = np.sort(np.asarray(u, dtype=float))
    n = len(x)
    i = np.arange(1, n + 1)
    return float(max(np.max(i / n - x), np.max(x - (i - 1) / n)))


def report(v: dict) -> str:
    """The validation report as the line a human reads before trusting anything."""
    lines = [f"panel={v['panel']} key={v['config_key']} folds={v['folds']} "
             f"train={min(v['n_train'])}-{max(v['n_train'])} "
             f"scored={v['n_holdout']} draws={v['n_samples']}",
             f"{'arm':<6} {'moments in CI':>14} {'cover50':>9} {'cover80':>9} {'KS':>7} "
             f"{'CRPS/boot':>10} {'t':>7}"]
    for tag, a in v["arms"].items():
        lines.append(f"{tag:<6} {a['moments_inside']:>7}/{a['moments_total']:<6} "
                     f"{a['cover50']:>9.3f} {a['cover80']:>9.3f} {a['ks']:>7.3f} "
                     f"{a['crps_ratio']:>10.3f} {a['crps_t_vs_boot']:>7.2f}")
    lines += [
        "  nominal cover50=0.50 cover80=0.80; KS is distance from uniform.",
        "  CRPS/boot < 1 means the arm is SHARPER than the unconditional bootstrap where",
        "  it counts; t is the paired t-stat of that difference (negative = better).",
        "  `boot` scoring 100% on moments is near-tautological — it IS the history — so",
        "  that column judges the other arms and says nothing about the baseline.",
    ]
    sep = v.get("separability")
    if sep:
        def _f(x, w=8):
            return f"{'  n/a':>{w}}" if x is None else f"{x:>{w}.3f}"

        lines += ["", f"{'separability':<6} {'C2ST':>8} {'vs boot':>8} {'mem':>8} "
                      f"{'dup':>8} {'dep in':>8} {'vs boot':>8} {'dep x':>8} {'vs boot':>8}"]
        for tag, a in sep["arms"].items():
            lines.append(f"{tag:<6}       {_f(a['auc'])} {_f(a['excess_over_boot'])} "
                         f"{_f(a['mem'])} {_f(a.get('dup_frac'))} {_f(a.get('dep_within'))} "
                         f"{_f(a.get('dep_within_excess'))} {_f(a.get('dep_cross'))} "
                         f"{_f(a.get('dep_cross_excess'))}")
        lines += [
            f"  floors on this panel: real train rows {_f(sep['floor_train'], 0)}, "
            f"real resampled blocks {_f(sep['floor_boot'], 0)}.",
            "  READ `vs boot`, NOT the raw C2ST. Real data does not score 0.5 against real",
            "  data at these sample sizes, so an AUC of 0.86 can be at the floor and an AUC",
            "  of 0.56 can be above it — it depends on the panel and the floor is measured",
            "  here for exactly that reason. 0 means as hard to tell from real as real",
            "  history is; negative means harder.",
            "  `mem` well below 1 VOIDS the row: the sample sits closer to the training rows",
            "  than a genuine held-out observation does, which is copying, and copying",
            "  improves every other number in this report at the same time. `boot`/`knn` sit",
            "  at ~0 by construction — they ARE training rows — which is why they are the",
            "  baseline to beat and never a candidate. The veto applies to `dfm`.",
            "  `boot`'s own excess is 0 by definition; it is printed as the identity check.",
            "  `dup` is the fraction of the arm's pool that was a VERBATIM copy of another row",
            "  in the same pool, before scoring. Those copies are dropped (#209), so the pool",
            "  actually scored is smaller than `draws` claims by this fraction — a `knn` arm at",
            "  0.40 is drawing 60 distinct worlds where the header says 100. It is reported",
            "  rather than silently fixed because a high value is a fact about the GENERATOR,",
            "  not about the test: it means that arm's effective sample is thin.",
            "  `dep in`/`dep x` are the mean absolute correlation error against the held-out",
            "  rows, WITHIN one column (persistence over every lag, not just acf1's lag 1)",
            "  and ACROSS columns (the co-movement a per-column generator would lose). They",
            "  are read against boot for the same reason the C2ST is: correlations from tens",
            "  of rows are noisy and the noise floor is panel-specific. `dep x` is n/a on a",
            "  single-column panel, where there are no cross-column pairs to score.",
        ]
    return "\n".join(lines)
