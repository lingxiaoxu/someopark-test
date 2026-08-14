"""Plan 05 LONG-SHORT mode — the structural fix for the long-only OOS failure.

research_alpha.py proved ALL factors go negative OOS because the long-only book
picks WHICH perps to hold long, never WHETHER: in a down/chop market every
selection bleeds. A dollar-neutral long-short book monetizes the cross-sectional
factor spread irrespective of market direction, and makes carry structurally
stronger: a SHORT in a positive-funding perp COLLECTS that funding (the holder
funding convention is P&L = −rate·w, so w<0 with rate>0 is positive carry —
matches crypto_common.costs.funding_payment).

Config-gated: ``portfolio.long_short: true`` (default false → the engine's
long-only path is byte-identical). Design choices, baked-in lessons:
  * Equal-weight per leg (gross/2 long, gross/2 short), per-name |w| capped —
    keeps the book dollar-neutral by construction (Σw≈0, Σ|w|=gross).
  * RANK-BAND HYSTERESIS (the XS-carry lesson: churn fees killed it): an
    incumbent long is kept while its rank stays within top_k + band; an
    incumbent short while within bottom_k + band. Only decisive rank moves
    trade. band=0 recovers plain top/bottom-k.
  * Risk scaling reuses the CALIBRATED levers (absolute vol-target, DD
    halve/flat tiers, rvol emergency) as a SCALAR on gross exposure — the
    long-only pipeline's concentration/beta steps don't apply to a
    beta-neutral book (documented divergence, not hidden).
  * cap_turnover / zscore filter from the long-only path are NOT used here
    (both assume w≥0; hysteresis is the turnover control).
  * stop_loss tracker is long-only machinery → ignored in LS mode (DD tiers
    are the drawdown control).
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from crypto_trading.crypto_strategies.perp_rotation.portfolio.risk import (
    RiskFlags, compute_historical_vol, compute_realized_vol, vol_scaling_factor)

logger = logging.getLogger(__name__)


def build_long_short_weights(
    scores: pd.Series,
    prev_weights: Optional[pd.Series] = None,
    *,
    k: int = 3,
    gross: float = 1.0,
    band: int = 0,
    max_weight: float = 0.45,
) -> pd.Series:
    """Cross-sectionally demeaned scores → dollar-neutral ±equal-weight legs.

    Hysteresis: incumbents (from ``prev_weights`` sign) are retained while
    their rank is within ``band`` of the entry cutoff; vacant slots fill with
    the best-ranked non-incumbents. Σw ≈ 0, Σ|w| = gross (subject to |w| cap).
    """
    s = scores.dropna()
    out = pd.Series(0.0, index=scores.index)
    n = len(s)
    kk = min(k, n // 2)
    if kk < 1:
        return out
    demeaned = s - s.mean()
    rank = demeaned.rank(ascending=False, method="first")   # 1 = best score

    prev = prev_weights.reindex(s.index).fillna(0.0) if prev_weights is not None \
        else pd.Series(0.0, index=s.index)
    prev_longs = set(prev[prev > 0].index)
    prev_shorts = set(prev[prev < 0].index)

    def _pick(side: str) -> list:
        if side == "long":
            cutoff, keep_zone, incumbents = kk, kk + band, prev_longs
            in_zone = lambda t: rank[t] <= keep_zone
            order = rank.sort_values().index                # best first
        else:
            cutoff, keep_zone, incumbents = kk, kk + band, prev_shorts
            rev_rank = n + 1 - rank                          # 1 = worst score
            in_zone = lambda t: rev_rank[t] <= keep_zone
            order = rank.sort_values(ascending=False).index  # worst first
        kept = [t for t in order if t in incumbents and in_zone(t)][:cutoff]
        for t in order:
            if len(kept) >= cutoff:
                break
            if t not in kept:
                kept.append(t)
        return kept

    longs = _pick("long")
    shorts = [t for t in _pick("short") if t not in longs]
    if not longs or not shorts:
        return out

    w_leg = gross / 2.0
    out[longs] = min(w_leg / len(longs), max_weight)
    out[shorts] = -min(w_leg / len(shorts), max_weight)
    return out


def ls_risk_scale(
    portfolio_returns: pd.Series,
    regime_inputs: pd.DataFrame,
    equity_curve: Optional[pd.Series],
    *,
    vol_target: float = 0.20,
    vol_target_mode: str = "absolute",
    rvol_emergency_threshold: float = 60.0,
    emergency_cash_pct: float = 0.50,
    dd_halve_threshold: float = -0.12,
    dd_flat_threshold: Optional[float] = -0.25,
) -> Tuple[float, RiskFlags]:
    """Scalar gross-exposure multiplier from the calibrated risk levers.

    Mirrors apply_risk_controls' ordering (emergency → DD tiers → vol target)
    but returns one multiplicative scale for a dollar-neutral book instead of
    reshaping long-only weights.
    """
    date = regime_inputs.index[-1] if len(regime_inputs) > 0 else pd.Timestamp.now(tz="UTC")
    flags = RiskFlags(date=date)
    scale = 1.0

    if "btc_rvol" in regime_inputs.columns and len(regime_inputs) > 0:
        rv = regime_inputs["btc_rvol"].dropna()
        if len(rv) > 0:
            current = float(rv.iloc[-1])
            flags.current_rvol = current
            if current > rvol_emergency_threshold:
                scale *= (1.0 - emergency_cash_pct)
                flags.emergency_triggered = True
                flags.notes.append(f"rvol emergency: {current:.1f}")

    if equity_curve is not None and len(equity_curve) > 0:
        peak = equity_curve.expanding().max()
        dd = float((equity_curve / peak - 1.0).iloc[-1])
        flags.current_dd_pct = dd
        if dd_flat_threshold is not None and dd < dd_flat_threshold:
            scale *= 0.10
            flags.dd_circuit_triggered = True
            flags.notes.append(f"DD flat tier: {dd:.2%}")
        elif dd < dd_halve_threshold:
            scale *= 0.50
            flags.dd_circuit_triggered = True
            flags.notes.append(f"DD circuit: {dd:.2%}")

    if len(portfolio_returns) >= 20:
        realized = compute_realized_vol(portfolio_returns, window=20)
        hist = compute_historical_vol(portfolio_returns, window=365)
        flags.realized_vol_annual = realized
        flags.historical_vol_annual = hist
        v = vol_scaling_factor(realized, hist, target_vol=vol_target,
                               mode=vol_target_mode)
        if v < 1.0:
            scale *= v
            flags.vol_scaling_triggered = True
            flags.notes.append(f"vol target: realized {realized:.2%}, scale {v:.3f}")

    flags.cash_pct = 1.0 - scale
    return scale, flags
