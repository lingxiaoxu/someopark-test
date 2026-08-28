"""The trade gate must be able to FAIL.

`trade_grade` was `calibrated_brier <= 2/3`, which is a tautology: the shrinkage grid
runs to lambda = 1.0 and `apply_shrinkage(p, 1.0)` IS the uniform forecast, whose Brier
is exactly 2/3. The search family therefore contains the baseline, so its best member
can never be worse than the baseline. A model backing the WRONG outcome at 95% every
match (raw Brier 1.85) was graded trade-grade.

The gate now asks whether the MODEL has skill: the winning calibrator must not be the
degenerate "throw it away" one, and the calibrated Brier must beat uniform by more than
one standard error AND by an absolute floor (the s.e. alone collapses toward zero for a
flat forecast, and float summation error then clears it).
"""
from __future__ import annotations

import random

from prediction_market_soccer.model.probability_calibration import fit_calibration


def _outcomes(n=400, seed=1):
    rng = random.Random(seed)
    return [rng.randrange(3) for _ in range(n)]


def test_a_model_that_backs_the_wrong_side_is_not_trade_grade():
    """The original counterexample: 95% on the losing outcome, every match."""
    Y = _outcomes()
    P = []
    for y in Y:
        p = [0.025] * 3
        p[(y + 1) % 3] = 0.95
        P.append(p)
    fit = fit_calibration(P, Y)
    assert fit["raw_brier"] > 2 / 3, "the input must really be worse than the baseline"
    assert fit["gate_degenerate"] is True, "shrinking it away is the fit discarding the model"
    assert fit["trade_grade"] is False


def test_an_uninformative_model_is_not_trade_grade():
    """Constant 1/3 IS the baseline; tying it is not skill. Its per-match Brier has no
    spread, so the standard error collapses and only the absolute floor catches it."""
    Y = _outcomes()
    fit = fit_calibration([[1 / 3, 1 / 3, 1 / 3]] * len(Y), Y)
    assert fit["trade_grade"] is False


def test_a_genuinely_skilled_model_is_trade_grade():
    Y = _outcomes()
    P = []
    for y in Y:
        p = [0.15] * 3
        p[y] = 0.70
        P.append(p)
    fit = fit_calibration(P, Y)
    assert fit["trade_grade"] is True
    assert fit["gate_margin"] > 0.01


def test_a_margin_thinner_than_the_floor_does_not_pass():
    """A model a hair better than uniform is not tradable — the floor is what stopped
    the competition carrying the most live paper bets (margin 0.0020)."""
    Y = _outcomes()
    P = []
    for y in Y:
        p = [1 / 3 - 0.002, 1 / 3 - 0.002, 1 / 3 - 0.002]
        p[y] += 0.006
        P.append(p)
    fit = fit_calibration(P, Y)
    assert fit["gate_margin"] < 0.01
    assert fit["trade_grade"] is False


def test_the_gate_reports_why():
    """An operator has to be able to see WHICH condition closed it."""
    Y = _outcomes()
    fit = fit_calibration([[1 / 3, 1 / 3, 1 / 3]] * len(Y), Y)
    for k in ("gate_margin", "gate_margin_se", "gate_degenerate"):
        assert k in fit


def test_an_empty_sample_is_not_trade_grade():
    assert fit_calibration([], [])["trade_grade"] is False
