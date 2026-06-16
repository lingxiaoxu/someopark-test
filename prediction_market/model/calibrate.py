"""Calibration & scoring metrics (plan 03 §9, 05 §6).

The right success metrics for this project are **calibration + closing-line
value (CLV)**, NOT win rate (plan 03 §9 — guessing favourites in lopsided games
gives a high but meaningless win rate). These helpers are used by the OOS
evaluator (`oos_eval.py`) and by live monitoring.

Multiclass-first: a "prediction" is a probability vector over K outcomes (e.g.
3-way home/draw/away) and the realised outcome is an index in [0, K).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_EPS = 1e-12


def _as_arrays(probs, outcomes) -> tuple[np.ndarray, np.ndarray]:
    p = np.asarray(probs, dtype=float)
    y = np.asarray(outcomes, dtype=int)
    if p.ndim != 2:
        raise ValueError("probs must be (n_samples, n_outcomes)")
    if y.shape[0] != p.shape[0]:
        raise ValueError("probs and outcomes length mismatch")
    return p, y


def brier_score(probs, outcomes) -> float:
    """Multiclass Brier score = mean_i sum_k (p_ik - 1[y_i=k])^2. Lower is better.

    Range [0, 2]; a perfect prediction scores 0. For binary it reduces to the
    familiar mean squared error of the predicted probability.
    """
    p, y = _as_arrays(probs, outcomes)
    onehot = np.zeros_like(p)
    onehot[np.arange(len(y)), y] = 1.0
    return float(np.mean(np.sum((p - onehot) ** 2, axis=1)))


def log_loss(probs, outcomes) -> float:
    """Mean negative log-likelihood of the realised outcome. Lower is better."""
    p, y = _as_arrays(probs, outcomes)
    p_true = np.clip(p[np.arange(len(y)), y], _EPS, 1.0)
    return float(-np.mean(np.log(p_true)))


@dataclass(frozen=True)
class ReliabilityBin:
    lo: float
    hi: float
    n: int
    mean_predicted: float
    observed_freq: float


def reliability_curve(prob_event, occurred, n_bins: int = 10) -> list[ReliabilityBin]:
    """Binary reliability: bucket predicted probs, compare to observed frequency.

    A well-calibrated model has observed_freq ≈ mean_predicted in each bin (the
    "predict 30% → happens ~30%" check, plan 03 §2).
    """
    p = np.asarray(prob_event, dtype=float)
    occ = np.asarray(occurred, dtype=float)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    out: list[ReliabilityBin] = []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (p >= lo) & (p < hi if i < n_bins - 1 else p <= hi)
        if mask.sum() == 0:
            continue
        out.append(ReliabilityBin(
            lo=float(lo), hi=float(hi), n=int(mask.sum()),
            mean_predicted=float(p[mask].mean()), observed_freq=float(occ[mask].mean()),
        ))
    return out


def closing_line_value(entry_prob: float, closing_prob: float, *, side: str = "yes") -> float:
    """CLV: did we beat the closing line? (plan 03 §9, the core edge metric.)

    Buying YES at an implied prob (price) below the closing implied prob is
    positive CLV. For a NO/short position, the sign flips.
    """
    raw = closing_prob - entry_prob          # bought cheap vs close → positive
    return raw if side == "yes" else -raw


def bootstrap_ci(values, *, n_boot: int = 2000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float]:
    """Bootstrap CI for a metric's per-sample contributions (plan 03 §7 — small
    OOS samples need CIs, not point estimates)."""
    v = np.asarray(values, dtype=float)
    if len(v) == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = [v[rng.integers(0, len(v), len(v))].mean() for _ in range(n_boot)]
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)
