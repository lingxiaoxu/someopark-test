"""De-vig: convert tradable ask prices into market-implied probabilities
(plan 04 §1).

Use the genuinely tradable ASK (not last/mid). For a mutually-exclusive event
(3-way match, champion, golden boot) the asks sum to > 1; the excess is the
overround (vig). We strip it. Long-tail markets (golden boot) suffer
favourite-longshot bias, so ``power`` and ``shin`` methods are provided in
addition to plain ``multiplicative`` (plan 04 §1, 08 §8).

All math uses float; callers convert venue prices (Decimal) to float at the
boundary.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import brentq


def multiplicative(asks: list[float] | np.ndarray) -> np.ndarray:
    """q_i = ask_i / sum(ask_j). Simple proportional normalisation."""
    a = np.asarray(asks, dtype=float)
    return a / a.sum()


def power(asks: list[float] | np.ndarray) -> np.ndarray:
    """Power de-vig: find k s.t. sum(ask_i ** k) = 1, return ask_i ** k.

    k > 1 deflates favourites less and longshots more, countering the
    longshot bias better than multiplicative on skewed books.
    """
    a = np.asarray(asks, dtype=float)

    def f(k: float) -> float:
        return float(np.sum(a**k) - 1.0)

    # sum(ask)>1 so f(1)>0; large k drives sum below 1 → root in (1, 50].
    k = brentq(f, 1.0, 50.0)
    return a**k


def shin(asks: list[float] | np.ndarray) -> np.ndarray:
    """Shin (1992) de-vig: backs out an insider-trading proportion z.

    Recovers probabilities p_i from normalised prices under Shin's model;
    well-suited to longshot-heavy books (golden boot). Falls back to
    multiplicative if the solve fails.
    """
    a = np.asarray(asks, dtype=float)
    pi = a / a.sum()  # normalised prices
    booksum = a.sum()

    def p_of_z(z: float) -> np.ndarray:
        # Shin closed form for the implied probabilities.
        root = np.sqrt(z**2 + 4.0 * (1.0 - z) * pi**2 * booksum)
        return (root - z) / (2.0 * (1.0 - z))

    def g(z: float) -> float:
        return float(p_of_z(z).sum() - 1.0)

    try:
        z = brentq(g, 1e-9, 0.5)
        p = p_of_z(z)
        return p / p.sum()
    except (ValueError, RuntimeError):
        return multiplicative(a)


def devig(asks: list[float] | np.ndarray, method: str = "multiplicative") -> np.ndarray:
    """Dispatch to a de-vig method by name."""
    methods = {"multiplicative": multiplicative, "power": power, "shin": shin}
    if method not in methods:
        raise ValueError(f"unknown devig method {method!r}; choose from {list(methods)}")
    return methods[method](asks)
