"""model/common.py — the pivot abstraction (PLAN §6, §18).

Every model outputs a Pred wrapping a Dist. The atomic representation for ladder markets
is the point-mass pmf ON THE SETTLEMENT GRID + its survival function; bucket probabilities
are derived. strict(>) vs >= is a first-order pricing input (CPI strikes sit ON the 0.1
grid; P(print == strike) is routinely 25-35%).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np


# ── distributions ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class GaussianMix:
    comps: tuple[tuple[float, float, float], ...]     # (weight, mu, sigma)

    def __post_init__(self):
        w = sum(c[0] for c in self.comps)
        assert abs(w - 1.0) < 1e-6, f"weights sum {w}"
        assert all(c[2] > 0 for c in self.comps), "sigma must be > 0"

    def cdf(self, x: float) -> float:
        return float(sum(w * 0.5 * (1 + math.erf((x - mu) / (s * math.sqrt(2))))
                         for w, mu, s in self.comps))

    def mean(self) -> float:
        return float(sum(w * mu for w, mu, _ in self.comps))

    def to_json(self) -> dict:
        return {"kind": "gmix", "comps": list(self.comps)}


_EMP_QUANTILES = 1001          # 0.1% resolution — a tenth of a Kalshi 1c tick.
                               # Was 201 (0.5%), which quantised a leg's fair value at
                               # half a tick and is too coarse to price a 1-2% wing.


@dataclass(frozen=True)
class Empirical:
    samples: tuple[float, ...]

    def __post_init__(self):
        # sorted once; cdf/quantile are searchsorted rather than a full scan. Energy
        # switched to bootstrap samples in energy/0.5.0, and grid_pmf calls cdf once
        # per settlement-grid point (thousands for a $0.01-grid WTI ladder) — the old
        # O(n) mean(a <= x) made that quadratic.
        object.__setattr__(self, "_sorted", np.sort(np.asarray(self.samples, dtype=float)))

    def cdf(self, x: float) -> float:
        a = self._sorted
        return float(np.searchsorted(a, x, side="right") / len(a))

    def quantile(self, q: float) -> float:
        return float(np.quantile(self._sorted, q))

    def mean(self) -> float:
        return float(np.mean(self._sorted))

    def to_json(self) -> dict:
        qs = np.quantile(self._sorted, np.linspace(0, 1, _EMP_QUANTILES)).tolist()
        return {"kind": "empirical", "quantiles": qs, "n": len(self.samples)}


@dataclass(frozen=True)
class Categorical:
    probs: dict[str, float]

    def __post_init__(self):
        s = sum(self.probs.values())
        assert abs(s - 1.0) < 1e-6, f"probs sum {s}"

    def to_json(self) -> dict:
        return {"kind": "categorical", "probs": self.probs}


Dist = GaussianMix | Empirical | Categorical


def dist_from_json(d: dict) -> Dist:
    if d["kind"] == "gmix":
        return GaussianMix(tuple(tuple(c) for c in d["comps"]))
    if d["kind"] == "empirical":
        return Empirical(tuple(d["quantiles"]))
    if d["kind"] == "categorical":
        return Categorical(d["probs"])
    raise ValueError(d["kind"])


@dataclass(frozen=True)
class Pred:
    series: str
    period: str
    dist: Dist
    asof: datetime
    model_version: str
    inputs: dict
    data_horizon: datetime            # max knowledge_time used (asserted <= asof on write)


# ── settlement-grid discretisation (§18.2-1) ────────────────────────────────
def grid_pmf(dist: Dist, grid_step: float, span_sigmas: float = 8.0,
             center: float | None = None, half_span: float | None = None) -> dict[float, float]:
    """Discretise a continuous dist onto the settlement grid: P(rounded print == g) =
    CDF(g + step/2) - CDF(g - step/2) (round-half-to-even edge effects are far below
    market tick size). Grid covers the distribution's effective support."""
    assert not isinstance(dist, Categorical), "grid_pmf is for continuous dists"
    if center is None:
        center = dist.mean()
    if half_span is None:
        if isinstance(dist, GaussianMix):
            spread = max(s for _, _, s in dist.comps) * span_sigmas + \
                     (max(m for _, m, _ in dist.comps) - min(m for _, m, _ in dist.comps))
        else:
            # quantile span, NOT max-min: bootstrap samples from a fat-tailed pool put
            # single draws far out, and min/max would blow the grid up to tens of
            # thousands of points at a $0.001 round_rule. 0.005%..99.995% keeps >0.9999
            # of the mass, comfortably above the assert below.
            lo_q, hi_q = dist.quantile(0.00005), dist.quantile(0.99995)
            spread = (hi_q - lo_q) + 4 * grid_step
        half_span = max(spread, 4 * grid_step)
    lo = math.floor((center - half_span) / grid_step) * grid_step
    hi = math.ceil((center + half_span) / grid_step) * grid_step
    pts = np.round(np.arange(lo, hi + grid_step / 2, grid_step), 10)
    pmf: dict[float, float] = {}
    prev = dist.cdf(float(pts[0]) - grid_step / 2)
    for g in pts:
        cur = dist.cdf(float(g) + grid_step / 2)
        p = max(cur - prev, 0.0)
        if p > 1e-12:
            pmf[float(g)] = p
        prev = cur
    tot = sum(pmf.values())
    assert tot > 0.999, f"grid span too narrow (mass {tot})"
    return {g: p / tot for g, p in pmf.items()}


def survival(pmf: dict[float, float], strike: float, strict: bool) -> float:
    """P(print > strike) if strict else P(print >= strike), on the settlement grid."""
    eps = 1e-9
    if strict:
        return sum(p for g, p in pmf.items() if g > strike + eps)
    return sum(p for g, p in pmf.items() if g >= strike - eps)


def leg_fair(pmf: dict[float, float], strike_type: str, floor_strike: float | None,
             cap_strike: float | None) -> float:
    """Fair YES probability of one contract leg from its strike metadata."""
    if strike_type == "greater":
        return survival(pmf, float(floor_strike), strict=True)
    if strike_type == "greater_or_equal":
        return survival(pmf, float(floor_strike), strict=False)
    if strike_type == "less":
        return 1.0 - survival(pmf, float(cap_strike), strict=False)
    if strike_type == "less_or_equal":
        return 1.0 - survival(pmf, float(cap_strike), strict=True)
    if strike_type == "between":
        return (survival(pmf, float(floor_strike), strict=False)
                - survival(pmf, float(cap_strike), strict=True))
    raise ValueError(f"unknown strike_type {strike_type}")


# ── scoring ─────────────────────────────────────────────────────────────────
def brier(probs: dict[str, float], outcome: str) -> float:
    return float(sum((p - (1.0 if k == outcome else 0.0)) ** 2 for k, p in probs.items()))


def logscore(probs: dict[str, float], outcome: str, floor: float = 1e-6) -> float:
    return -math.log(max(probs.get(outcome, 0.0), floor))


def crps_grid(pmf: dict[float, float], realized: float) -> float:
    """CRPS on the settlement grid (discrete integral of (CDF - 1{x>=realized})^2)."""
    gs = sorted(pmf)
    step = min(round(b - a, 10) for a, b in zip(gs, gs[1:])) if len(gs) > 1 else 1.0
    c = 0.0
    acc = 0.0
    for g in gs:
        acc += pmf[g]
        h = 1.0 if g >= realized - 1e-9 else 0.0
        c += (acc - h) ** 2 * step
    return float(c)


def pred_to_row(p: Pred, ladder: dict | None) -> tuple:
    assert p.data_horizon <= p.asof, \
        f"PIT violation: data_horizon {p.data_horizon} > asof {p.asof}"   # §5-bis.4-2 online guard
    return (p.series, p.period, p.asof.isoformat(), p.model_version,
            json.dumps(p.dist.to_json()), json.dumps(ladder) if ladder else None,
            json.dumps(p.inputs, ensure_ascii=False, default=str),
            p.data_horizon.isoformat(), datetime.utcnow().isoformat() + "+00:00")
