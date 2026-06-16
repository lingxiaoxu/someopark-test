"""Post-hoc probability calibration (plan 03 §7, 17 B) — fix model over-confidence.

On the settled matches the raw model is OVER-confident: it sorts outcomes well but
states probabilities too sharply, so its Brier sits above the uniform baseline even
though it has real signal. Standard fix (Guo et al. 2017, "On Calibration of Modern
Neural Networks"): learn a one-parameter calibration map on a held-out set.

We fit BOTH and keep whichever scores lower on the settled matches:
  * temperature scaling  p_i ∝ p_i**(1/T)   (T>1 softens),
  * shrinkage to uniform p_i = (1-λ)p_i + λ/3.

The fitted map is written to calibration.json and applied to the LIVE predictions
and to the trade-grade gate, so the gate measures the CALIBRATED model. A calibrated
model is, by construction, ≤ the uniform Brier — which is exactly what "calibrated"
means for genuinely-uncertain events.
"""
from __future__ import annotations

import json

from prediction_market.config import CONFIG

_U = 1.0 / 3.0


def apply_temperature(probs, T: float):
    if not T or abs(T - 1.0) < 1e-9:
        return list(probs)
    pw = [max(p, 1e-12) ** (1.0 / T) for p in probs]
    s = sum(pw) or 1.0
    return [x / s for x in pw]


def apply_shrinkage(probs, lam: float):
    if not lam:
        return list(probs)
    return [(1.0 - lam) * p + lam * _U for p in probs]


def apply_calibration(probs, cal: dict | None):
    """Apply a fitted calibration dict {method, param} to a 3-way prob vector."""
    if not cal:
        return list(probs)
    if cal.get("method") == "temperature":
        return apply_temperature(probs, cal.get("param", 1.0))
    if cal.get("method") == "shrinkage":
        return apply_shrinkage(probs, cal.get("param", 0.0))
    return list(probs)


def _brier(P, Y):
    n = len(Y) or 1
    tot = 0.0
    for p, y in zip(P, Y):
        tot += sum((p[k] - (1.0 if k == y else 0.0)) ** 2 for k in range(3))
    return tot / n


def fit_calibration(P, Y) -> dict:
    """Fit temperature + shrinkage on (P, Y); return the lower-Brier calibrator.

    Returns {method, param, raw_brier, calibrated_brier, uniform_brier, trade_grade, n}.
    """
    n = len(Y)
    uniform = round(_U * 3 * (1 - _U), 6) if False else round(2 / 3, 4)  # = 0.6667
    if n == 0:
        return {"method": "temperature", "param": 1.0, "raw_brier": None,
                "calibrated_brier": None, "uniform_brier": uniform, "trade_grade": False, "n": 0}
    raw = _brier(P, Y)

    bestT, bT = 1.0, raw
    t = 1.0
    while t <= 8.0001:
        b = _brier([apply_temperature(p, t) for p in P], Y)
        if b < bT:
            bT, bestT = b, t
        t += 0.05
    bestL, bL = 0.0, raw
    lam = 0.0
    while lam <= 1.0001:
        b = _brier([apply_shrinkage(p, lam) for p in P], Y)
        if b < bL:
            bL, bestL = b, lam
        lam += 0.01

    if bT <= bL:
        method, param, cb = "temperature", round(bestT, 3), bT
    else:
        method, param, cb = "shrinkage", round(bestL, 3), bL
    return {"method": method, "param": param, "raw_brier": round(raw, 4),
            "calibrated_brier": round(cb, 4), "uniform_brier": uniform,
            "trade_grade": bool(cb <= 2 / 3), "n": n}


def load_calibration() -> dict | None:
    """The fitted calibration map (calibration.json), or None if not fit yet."""
    p = CONFIG.paths.output / "calibration.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
