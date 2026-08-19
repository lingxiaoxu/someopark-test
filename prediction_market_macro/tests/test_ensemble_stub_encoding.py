"""The Empirical stub must represent the ladder it was built from.

`shadow_run` stores two views of the same pooled forecast: `ladder_json` (authoritative)
and a `dist_json` Empirical stub. Everything downstream that takes a Dist rather than a
ladder — grid_pmf, leg_fair, research/eval, shadow_gate — reads the stub, so a stub that
disagrees with its ladder is a forecast that silently means two different things.

The encoder these tests pin replaced one that replicated each grid point `round(p*2000)`
times (floored at 1) and truncated at `vals[:4000]`. Because `sorted()` emits low grid
points first, any ladder with >4000 points kept only its bottom tail. On the 2026-08-18
KXPAYROLLS pred that turned a ladder mean of +84,376 jobs into a stub mean of −1,886,973
and priced every "payrolls > 0" leg at exactly 0.0 against a market at 0.75.
"""
from __future__ import annotations

import numpy as np
import pytest

from prediction_market_macro.model.ensemble import N_STUB_SAMPLES, ladder_samples


def _pmf(keys, probs):
    return dict(zip(keys, np.asarray(probs, dtype=float) / np.sum(probs)))


def test_mean_survives_a_grid_larger_than_the_sample_budget():
    """The regression. 8739 grid points is what KXPAYROLLS actually carries."""
    ks = np.arange(-3_884_000, 4_855_000, 1000, dtype=float)
    w = np.exp(-0.5 * ((ks - 84_000) / 60_000) ** 2) + 1e-12   # tiny floor everywhere,
    pmf = _pmf(ks, w)                                          # as the real pool has
    assert len(pmf) > N_STUB_SAMPLES, "fixture must exceed the sample budget"
    ladder_mean = sum(k * v for k, v in pmf.items())
    s = np.asarray(ladder_samples(pmf))
    assert abs(s.mean() - ladder_mean) < 2000, (
        f"stub mean {s.mean():,.0f} vs ladder mean {ladder_mean:,.0f}")


def test_upper_tail_is_not_discarded():
    """WTI lost [98, 115] the same way payrolls lost everything above -8k."""
    ks = np.arange(50.0, 115.01, 0.01)
    pmf = _pmf(ks, np.exp(-0.5 * ((ks - 84.7) / 6.0) ** 2) + 1e-12)
    s = np.asarray(ladder_samples(pmf))
    assert s.max() > 100, "upper wing truncated"
    assert s.min() < 70, "lower wing truncated"


def test_exact_sample_count_regardless_of_grid_size():
    for n_pts in (3, 500, 20_000):
        ks = np.linspace(0.0, 1.0, n_pts)
        s = ladder_samples(_pmf(ks, np.ones(n_pts)))
        assert len(s) == N_STUB_SAMPLES, f"{n_pts} grid points -> {len(s)} samples"


def test_support_is_a_subset_of_the_ladder_grid():
    """Settlement values are grid points; the stub must not invent between them."""
    ks = [2.5, 2.6, 2.7, 2.8]
    pmf = _pmf(ks, [0.1, 0.4, 0.4, 0.1])
    assert set(ladder_samples(pmf)) <= set(ks)


def test_zero_mass_points_are_not_sampled():
    """The old floor-at-1 gave a p=0 grid point a sample; that is what let thousands of
    empty points crowd out the real mass."""
    pmf = {1.0: 0.0, 2.0: 0.5, 3.0: 0.5, 4.0: 0.0}
    assert set(ladder_samples(pmf)) == {2.0, 3.0}


def test_is_deterministic():
    pmf = _pmf(np.linspace(0, 10, 5000), np.random.default_rng(0).random(5000))
    assert ladder_samples(pmf) == ladder_samples(pmf)


def test_empty_pmf_raises_rather_than_returning_a_point_mass():
    with pytest.raises(ValueError):
        ladder_samples({1.0: 0.0, 2.0: 0.0})
