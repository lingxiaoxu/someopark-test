"""Plan 02 `signals/dislocation.py` — the §6 dislocation FACTORS.

Builds on signals/implied_dist.py (which turns a captured strike strip into an
ImpliedDist with mean/sd/skew + arb detectors). This module formalises the four
Plan 02 §6 factors, each z-scored vs its OWN history, plus a weighted composite.
Pure computation over per-snapshot context — the backtest/live loader supplies
the perp/index/funding series; nothing here fetches data.

The four factors (orientation: POSITIVE ⇒ perp is cheap vs the implied
distribution ⇒ expect the perp to rise toward it — so the fair-value factor's
IC against forward perp convergence is positive when predictive):

  1. fair_value_gap  = (implied_mean_carry_adj − perp_spot) / perp_spot
       The directional dislocation — the only one the preliminary backtest found
       predictive (IC +0.13 BTC / +0.20 ETH on 4 days). Carry adjustment shifts
       the implied mean by the funding carry to the horizon (small at short T).
  2. vol_gap         = implied_CoV − perp_realized_vol
       Event-implied dispersion vs perp realized vol. NOT price-directional — it
       drives a vol trade, so it is NOT expected to IC against perp convergence;
       reported for completeness / future vol leg.
  3. skew_gap        = implied_skew − k·sign(funding)
       Event-implied skew vs the funding-sign prior (crowded funding ⇒ expected
       skew). A dislocation is disagreement. Also not directly price-directional.
  4. arb_violation   = best fee-positive static-arb net credit (pairwise + tile)
       The near-riskless standalone factor (magnitude, not z-scored — a direct
       expectancy signal). On captured Kalshi strips this is ~0 (no free money).

``composite`` z-scores factors 1-3 within-history and weight-sums them with the
arb magnitude (plan §9 weights: fair 0.35, vol 0.25, skew 0.15, arb 0.25).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from crypto_trading.crypto_strategies.event_perp.signals.implied_dist import (
    ImpliedDist, event_fee, find_violations, tile_arb)

DEFAULT_WEIGHTS = {"fair_value": 0.35, "vol": 0.25, "skew": 0.15, "arb": 0.25}


@dataclass(frozen=True)
class DislocationParams:
    """WF sweep surface (plan §9)."""
    zwin: int = 60                    # z-score window (snapshots)
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    skew_funding_k: float = 1.0       # funding-sign prior weight in skew_gap
    fee_rate: float = 0.07


# ── raw factors (pure, per-snapshot) ─────────────────────────────────────────

def fair_value_gap_raw(implied_mean: float, perp_spot: float,
                       funding_carry: float = 0.0) -> float | None:
    """(carry-adjusted implied mean − perp) / perp. + ⇒ perp cheap ⇒ rise."""
    if perp_spot is None or perp_spot <= 0 or implied_mean is None:
        return None
    adj_mean = implied_mean * (1.0 + funding_carry)     # carry to horizon
    return (adj_mean - perp_spot) / perp_spot


def vol_gap_raw(implied_sd: float, implied_mean: float,
                realized_vol: float | None) -> float | None:
    """implied coefficient-of-variation − perp realized vol. NOT price-directional.

    Absolute scale is irrelevant (the factor is z-scored); it measures whether
    the event book implies MORE or LESS dispersion than the perp realized."""
    if implied_sd is None or implied_mean is None or implied_mean <= 0:
        return None
    if realized_vol is None or not np.isfinite(realized_vol):
        return None
    return implied_sd / implied_mean - realized_vol


def skew_gap_raw(implied_skew: float, funding_rate: float | None,
                 k: float = 1.0) -> float | None:
    """implied skew − k·sign(funding). Crowded (positive) funding primes a
    negative-skew expectation; disagreement is the dislocation."""
    if implied_skew is None or not np.isfinite(implied_skew):
        return None
    fsign = 0.0 if funding_rate is None else float(np.sign(funding_rate))
    return implied_skew - k * fsign


def arb_violation_raw(surv_quotes, bins, *, fee_rate: float = 0.07,
                      tail_lo=None, tail_hi=None) -> float:
    """Best fee-positive static-arb net credit (pairwise + complete tile). ≥0.

    Direct expectancy (near-riskless) — a MAGNITUDE, not z-scored. ~0 on the
    captured Kalshi strips (confirmed: no free money survives fees)."""
    best = 0.0
    for v in find_violations(surv_quotes, fee_rate=fee_rate, min_net_credit=0.0):
        best = max(best, v.net_credit)
    ta = tile_arb(bins, tail_lo=tail_lo, tail_hi=tail_hi, fee_rate=fee_rate) if bins else None
    if ta is not None and ta.coverage_complete:
        best = max(best, ta.buy_credit_net, ta.sell_credit_net)
    return max(0.0, best)


# ── z-scoring (PIT, within-horizon) ──────────────────────────────────────────

def rolling_z(s: pd.Series, win: int) -> pd.Series:
    """Trailing z of each point vs its own past window (no look-ahead)."""
    mp = max(10, win // 3)
    mu = s.rolling(win, min_periods=mp).mean()
    sd = s.rolling(win, min_periods=mp).std(ddof=0)
    return (s - mu) / sd.replace(0.0, np.nan)


# ── composite over a factor frame ────────────────────────────────────────────

def composite(frame: pd.DataFrame, params: DislocationParams = DislocationParams()
              ) -> pd.Series:
    """Weighted composite of z-scored fair_value/vol/skew + raw arb magnitude.

    ``frame`` must carry raw columns fair_value_gap, vol_gap, skew_gap,
    arb_violation, and a ``close_time`` grouping key. Z-scoring is done WITHIN
    each event horizon (interleaved horizons otherwise fabricate signal — the
    2026-07-10 bug). arb is a magnitude (already an expectancy), not z-scored.
    """
    w = params.weights
    out = pd.Series(0.0, index=frame.index)
    z = {}
    for name, col in (("fair_value", "fair_value_gap"), ("vol", "vol_gap"),
                      ("skew", "skew_gap")):
        if col not in frame:
            continue
        zc = frame.groupby("close_time", sort=False)[col].transform(
            lambda s: rolling_z(s, params.zwin))
        z[name] = zc
        out = out.add(w.get(name, 0.0) * zc.fillna(0.0), fill_value=0.0)
    if "arb_violation" in frame:
        # arb magnitude → its own z so the weight is comparable; but keep sign
        # (always ≥0). On real data this is ~0 so contributes ~nothing.
        za = frame.groupby("close_time", sort=False)["arb_violation"].transform(
            lambda s: rolling_z(s, params.zwin))
        out = out.add(w.get("arb", 0.0) * za.fillna(0.0), fill_value=0.0)
    for name, zc in z.items():
        frame[f"z_{name}"] = zc
    return out


# ── per-snapshot extraction (glue to implied_dist) ──────────────────────────

def snapshot_factors(dist: ImpliedDist | None, *, perp_spot: float | None,
                     realized_vol: float | None, funding_rate: float | None,
                     surv_quotes=None, bins=None, tail_lo=None, tail_hi=None,
                     params: DislocationParams = DislocationParams()) -> dict:
    """All four raw factors for one snapshot (None where inputs are missing)."""
    if dist is None:
        return {"fair_value_gap": None, "vol_gap": None, "skew_gap": None,
                "arb_violation": 0.0}
    return {
        "fair_value_gap": fair_value_gap_raw(dist.mean, perp_spot),
        "vol_gap": vol_gap_raw(dist.sd, dist.mean, realized_vol),
        "skew_gap": skew_gap_raw(dist.skew, funding_rate, k=params.skew_funding_k),
        "arb_violation": arb_violation_raw(surv_quotes or [], bins or [],
                                           fee_rate=params.fee_rate,
                                           tail_lo=tail_lo, tail_hi=tail_hi),
    }
