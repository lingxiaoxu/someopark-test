"""Ensemble (plan 10 §5.3) — variant dispersion → real uncertainty for sizing.

A single model is one opinion. We derive a panel of variants by perturbing the
modelling parameters (Dixon-Coles rho, rating→goal sensitivity beta, host
advantage, rank-strength decay, penalty edge, knockout lambda scale), run each
through the tournament simulation, and take, per market, the **probability mean
+ dispersion** across variants.

The dispersion is the missing piece the rest of the system needs: it replaces
the placeholder ``sigma`` (plan 03 §1 output) so the sizing layer can discount
positions where the model panel disagrees (plan 04 §2/§8, 10 §5.3 — high
dispersion → smaller stake / higher edge threshold).

Cost-aware: variants reuse the fast calibration + vectorised sim, so a dozen
variants at quick-look N run in seconds.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

import numpy as np

from prediction_market.config import CONFIG, ModelConfig
from prediction_market.ingest.prior_ingest import PriorSnapshot, load_prior
from prediction_market.model.strength import build_strength
from prediction_market.model.tournament import simulate

# Perturbation ranges per axis (plan 10 §5.3 dimensions).
_AXES = {
    "dc_rho": (-0.12, 0.0),
    "beta": (0.32, 0.48),
    "home_adv": (0.15, 0.35),
    "rank_strength_decay": (0.010, 0.015),
    "penalty_favorite_edge": (0.51, 0.55),
    "knockout_lambda_scale": (0.80, 0.92),
}


@dataclass
class EnsembleResult:
    team_ids: list[str]
    n_variants: int
    n_sims: int
    p_champion_mean: dict[str, float]
    p_champion_sigma: dict[str, float]    # cross-variant std → feeds sizing
    p_advance_mean: dict[str, float]
    p_advance_sigma: dict[str, float]
    variant_fingerprints: list[str]


def generate_variants(base: ModelConfig, n_variants: int = 16, *, seed: int = 0) -> list[ModelConfig]:
    """Latin-hypercube-ish sample of variants across the parameter axes."""
    rng = np.random.default_rng(seed)
    variants = [base]  # always include the base config
    for _ in range(max(0, n_variants - 1)):
        overrides = {k: float(rng.uniform(lo, hi)) for k, (lo, hi) in _AXES.items()}
        variants.append(replace(base, **overrides))
    return variants


def _fingerprint(cfg: ModelConfig) -> str:
    blob = "|".join(f"{k}={getattr(cfg, k):.5f}" for k in _AXES)
    return hashlib.sha1(blob.encode()).hexdigest()[:10]


def run_ensemble(
    prior: PriorSnapshot | None = None,
    *,
    n_variants: int = 16,
    n_sims: int | None = None,
    seed: int | None = None,
) -> EnsembleResult:
    prior = prior or load_prior()
    base = CONFIG.model
    n_sims = n_sims or 20_000
    seed = seed if seed is not None else base.random_seed
    variants = generate_variants(base, n_variants, seed=seed)

    team_ids = [t.team_id for t in prior.teams]
    champ = np.zeros((len(variants), len(team_ids)))
    adv = np.zeros((len(variants), len(team_ids)))
    fps = []
    for vi, vcfg in enumerate(variants):
        sm = build_strength(prior, vcfg)
        res = simulate(prior, sm, n_sims=n_sims, seed=seed + vi)
        champ[vi] = [res.p_champion[t] for t in team_ids]
        adv[vi] = [res.p_advance[t] for t in team_ids]
        fps.append(_fingerprint(vcfg))

    return EnsembleResult(
        team_ids=team_ids, n_variants=len(variants), n_sims=n_sims,
        p_champion_mean={t: float(champ[:, i].mean()) for i, t in enumerate(team_ids)},
        p_champion_sigma={t: float(champ[:, i].std()) for i, t in enumerate(team_ids)},
        p_advance_mean={t: float(adv[:, i].mean()) for i, t in enumerate(team_ids)},
        p_advance_sigma={t: float(adv[:, i].std()) for i, t in enumerate(team_ids)},
        variant_fingerprints=fps,
    )


if __name__ == "__main__":
    prior = load_prior()
    ens = run_ensemble(prior, n_variants=10, n_sims=15_000)
    print(f"Ensemble: {ens.n_variants} variants × N={ens.n_sims}")
    print(f"{'team':<14}{'champ mean':>12}{'champ sigma':>13}{'dispersion%':>13}")
    for t in sorted(ens.team_ids, key=lambda x: -ens.p_champion_mean[x])[:8]:
        mean, sig = ens.p_champion_mean[t], ens.p_champion_sigma[t]
        name = next(x.name for x in prior.teams if x.team_id == t)
        disp = (sig / mean * 100) if mean > 0 else 0.0
        print(f"{name:<14}{mean:>12.4f}{sig:>13.4f}{disp:>12.1f}%")
