"""Backtest metrics (plan 05 §6).

Thin layer over ``model/calibrate.py`` (the single source of scoring truth) plus
walk-forward-specific aggregations (rolling Brier).
"""
from __future__ import annotations

import numpy as np

from prediction_market.model.calibrate import (  # re-export
    bootstrap_ci,
    brier_score,
    closing_line_value,
    log_loss,
    reliability_curve,
)

__all__ = ["brier_score", "log_loss", "reliability_curve", "closing_line_value",
           "bootstrap_ci", "rolling_brier"]


def rolling_brier(probs, outcomes, window: int = 8) -> list[float]:
    """Rolling-window Brier over a chronological sequence (drift detection)."""
    p = np.asarray(probs, dtype=float)
    y = np.asarray(outcomes, dtype=int)
    onehot = np.zeros_like(p)
    onehot[np.arange(len(y)), y] = 1.0
    per = np.sum((p - onehot) ** 2, axis=1)
    return [float(per[max(0, i - window + 1): i + 1].mean()) for i in range(len(per))]
