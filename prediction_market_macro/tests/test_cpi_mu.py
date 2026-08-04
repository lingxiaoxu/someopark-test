"""cpi/0.2.0 — the second lag is gone, and these pin why it may not come back.

_core_mu_sigma used to read mu = 0.5*L1 + 0.3*L2 + 0.2*M12. The 0.3 on the second lag
was the whole problem: MoM's lag-2 autocorrelation (+0.657 core) runs almost entirely
THROUGH lag-1, so conditional on L1 the second lag carries no incremental signal — just
the sampling noise of one print, in a slot where the trailing-12 mean estimates the same
persistent level from twelve observations.

Measured before changing anything (walk-forward, strictly prior months only, DM with
HAC lag 1). Rerouting L2's weight into M12 at an UNCHANGED L1 weight of 0.5, so the gain
is attributable to that single move:

    core     1995  n=353  RMSE .1121 -> .1057  DM p=.033
    core     2010  n=173  RMSE .1296 -> .1183  DM p=.027
    headline 1995  n=353  RMSE .2915 -> .2661  DM p=.003
    headline 2010  n=173  RMSE .2570 -> .2348  DM p=.003

and end-to-end on settled contracts (model-only Brier per leg, all four series improve):
KXCPI .12047->.11573, KXCPICORE .06693->.06152, KXCPIYOY .09358->.09100,
KXCPICOREYOY .05309->.05021.

The momentum term itself was NOT the issue and must not be "fixed" next: the RMSE grid
over w_last is flat across 0.3..0.5 (core-1995 .1055/.1051/.1057), i.e. 0.5 was already
about right. test_the_momentum_weight_is_deliberate pins that.

What led me here was KXCPICORE putting 31-35% of its mass on exactly 0.0, and the first
comparison I reached for was WRONG: that is a CONDITIONAL forecast (the previous print
was ~0) and I held it against a 36-month UNCONDITIONAL frequency of 2.8%. Full-history
unconditional is 10.2% (85 of 832 months at MoM <= 0), and with lag-1 autocorr +0.66 the
conditional rate belongs above it. The mass is a symptom worth chasing, never a pass/fail
bar — the two tests that decided this were walk-forward RMSE and settled-contract Brier.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from prediction_market_macro.model.cpi import _core_mu_sigma


def _mom(vals) -> pd.Series:
    idx = pd.date_range("2020-01-01", periods=len(vals), freq="MS")
    return pd.Series(list(vals), index=idx, dtype=float)


def test_the_second_lag_carries_no_weight():
    """Two histories identical everywhere except m[-2] must give the SAME mu, up to the
    0.5/12 the trailing-12 mean legitimately gives that observation."""
    base = [0.2] * 30
    a = _mom(base)
    b = _mom(base[:-2] + [2.0, base[-1]])          # m[-2] blown out to +2.0
    mu_a, _ = _core_mu_sigma(a)
    mu_b, _ = _core_mu_sigma(b)
    # under the old 0.3 weight this gap was 0.3*1.8 = 0.54
    assert abs(mu_b - mu_a) == pytest.approx(0.5 * 1.8 / 12, abs=1e-9)
    assert abs(mu_b - mu_a) < 0.08


def test_the_momentum_weight_is_deliberate():
    """m[-1] still moves mu by half its deviation — the grid said 0.3..0.5 is flat and
    0.5 sits inside it. A future 'the model chases the last print' patch has to beat
    the numbers in this module's docstring first."""
    base = [0.2] * 30
    a = _mom(base)
    b = _mom(base[:-1] + [1.2])                    # m[-1] +1.0 above the rest
    mu_a, _ = _core_mu_sigma(a)
    mu_b, _ = _core_mu_sigma(b)
    assert mu_b - mu_a == pytest.approx(0.5 * 1.0 + 0.5 * 1.0 / 12, abs=1e-9)


def test_mu_is_a_convex_combination_of_the_history():
    """Weights sum to 1: a flat history must predict itself, at any level. Guards the
    arithmetic slip where dropping a term leaves the rest summing to 0.7."""
    for lvl in (-0.3, 0.0, 0.25, 1.5):
        mu, _ = _core_mu_sigma(_mom([lvl] * 30))
        assert mu == pytest.approx(lvl, abs=1e-12)


def test_sigma_is_untouched_by_the_mu_change_and_keeps_its_floor():
    """0.2.0 changed the location only. sigma is still the MAD of the last 18 about
    their own mean, floored at 0.06 — a flat history must not produce sigma 0."""
    mu, sg = _core_mu_sigma(_mom([0.2] * 30))
    assert sg == pytest.approx(0.06)
    noisy = _mom(list(0.2 + 0.5 * np.sin(np.arange(30))))
    _mu2, sg2 = _core_mu_sigma(noisy)
    assert sg2 > 0.06


def test_short_history_is_refused_rather_than_guessed():
    with pytest.raises(AssertionError):
        _core_mu_sigma(_mom([0.2] * 12))


def test_nans_do_not_silently_shift_the_window():
    """dropna runs first, so a NaN must not make m[-1] read a stale month."""
    v = [0.2] * 30 + [float("nan")]
    mu_with, _ = _core_mu_sigma(_mom(v))
    mu_without, _ = _core_mu_sigma(_mom(v[:-1]))
    assert mu_with == pytest.approx(mu_without, abs=1e-12)
