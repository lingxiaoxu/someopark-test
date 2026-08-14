"""Trade-level significance + purged cross-validation (crypto strategies).

Why this exists: a coarse 2-fold daily walk-forward is a bad estimator for a
high-frequency strategy. Basis makes ~5,500 P&L events in 52 days; the daily WF
collapsed that into 2 noisy folds and returned a garbage OOS Sharpe (−8.4) while
the realized per-trade edge was strongly positive. The right tools operate on
the TRADE series:

  * Newey-West (HAC) t-stat — corrects the naive t for autocorrelation between
    consecutive trades (mean-reversion trades are serially correlated, so the
    naive t overstates significance).
  * Effective sample size — how many *independent* trades the n trades are worth.
  * Deflated / Probabilistic Sharpe (Bailey & López de Prado) — reused verbatim
    from walk_forward.py so the DSR math never diverges.
  * Purged K-fold CV (López de Prado) — contiguous OOS test blocks with the
    training set purged around each block; here the "model" is a FIXED strategy,
    so this measures whether the realized edge GENERALIZES across held-out
    blocks (block-wise stability), not parameter fitting.

Pure functions, numpy/pandas/scipy only. No I/O.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import scipy.stats

# reuse the exact DSR math from the WF module — do not diverge
from crypto_trading.crypto_common.walk_forward import (deflated_sharpe_ratio,
                                                       expected_max_sharpe)


def _clean(pnl: pd.Series | np.ndarray) -> np.ndarray:
    a = np.asarray(pd.Series(pnl).dropna(), dtype=float)
    return a


def _auto_lags(n: int) -> int:
    """Newey-West rule-of-thumb bandwidth: floor(4*(n/100)^(2/9))."""
    if n <= 1:
        return 0
    return int(np.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))


def newey_west_tstat(pnl: pd.Series, lags: int | None = None) -> dict:
    """Mean per-trade P&L and its HAC (Newey-West) t-statistic.

    NW long-run variance of the mean: S = γ0 + 2·Σ_{l=1}^{L} (1 − l/(L+1))·γ_l,
    with Bartlett weights; Var(mean)=S/n. When trades are positively
    autocorrelated S > γ0, so t_nw ≤ t_naive — the honest deflation.
    """
    a = _clean(pnl)
    n = a.size
    out = {"mean": float(np.mean(a)) if n else 0.0, "n": int(n),
           "t_naive": 0.0, "t_nw": 0.0, "lags": 0, "se_nw": float("nan")}
    if n < 3 or np.std(a, ddof=1) == 0:
        return out
    mean = a.mean()
    dev = a - mean
    gamma0 = float(np.dot(dev, dev) / n)               # biased autocov at lag 0
    L = _auto_lags(n) if lags is None else int(lags)
    L = max(0, min(L, n - 1))
    s_lr = gamma0
    for l in range(1, L + 1):
        w = 1.0 - l / (L + 1.0)                         # Bartlett kernel
        gamma_l = float(np.dot(dev[l:], dev[:-l]) / n)
        s_lr += 2.0 * w * gamma_l
    s_lr = max(s_lr, 1e-300)
    se_nw = np.sqrt(s_lr / n)
    se_naive = np.std(a, ddof=1) / np.sqrt(n)
    out.update({"t_naive": float(mean / se_naive), "t_nw": float(mean / se_nw),
                "lags": int(L), "se_nw": float(se_nw)})
    return out


def effective_sample_size(pnl: pd.Series) -> float:
    """n adjusted for lag-1 autocorrelation ρ: n_eff = n·(1−ρ)/(1+ρ).

    ρ>0 (persistent) ⇒ n_eff<n; ρ<0 (mean-reverting trade P&L) ⇒ n_eff>n.
    """
    a = _clean(pnl)
    n = a.size
    if n < 3 or np.std(a) == 0:
        return float(n)
    dev = a - a.mean()
    rho = float(np.dot(dev[1:], dev[:-1]) / np.dot(dev, dev))
    rho = min(0.999, max(-0.999, rho))
    return float(n * (1.0 - rho) / (1.0 + rho))


def deflated_sharpe(returns: pd.Series, n_trials: int = 1) -> dict:
    """PSR (prob SR>0) and DSR (n-trial-deflated) for a return/P&L series.

    Uses walk_forward.deflated_sharpe_ratio / expected_max_sharpe. The trial
    benchmark var_sharpes is the sampling variance of the SR estimator,
    V[SR] ≈ (1 − γ3·SR + (γ4−1)/4·SR²)/(T−1) (Lo 2002 / Bailey-LdP), so a single
    fixed strategy still gets a defensible multiple-testing deflation.
    """
    a = _clean(returns)
    T = a.size
    out = {"sharpe": 0.0, "psr": 0.0, "dsr": 0.0, "n_trials": int(n_trials), "T": int(T)}
    if T < 3 or np.std(a, ddof=1) == 0:
        return out
    sr = float(a.mean() / a.std(ddof=1))
    skew = float(scipy.stats.skew(a))
    kurt = float(scipy.stats.kurtosis(a, fisher=False))  # non-excess (normal=3)
    psr = deflated_sharpe_ratio(sr, 0.0, T, skew, kurt)  # prob SR>0
    if n_trials > 1:
        var_sr = max((1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr ** 2) / (T - 1), 1e-12)
        sr0 = expected_max_sharpe(n_trials, var_sr)
        dsr = deflated_sharpe_ratio(sr, sr0, T, skew, kurt)
    else:
        dsr = psr
    out.update({"sharpe": sr, "psr": float(psr), "dsr": float(dsr)})
    return out


def purged_kfold_indices(n: int, k: int, embargo: int = 0) -> list[tuple[np.ndarray, np.ndarray]]:
    """López de Prado purged K-fold: contiguous test blocks; training purged of
    any index within `embargo` of a test block (removes leakage from serial
    correlation across the train/test boundary).
    """
    if k < 2 or n < k:
        raise ValueError(f"need k>=2 and n>=k (got n={n}, k={k})")
    bounds = np.linspace(0, n, k + 1, dtype=int)
    folds = []
    for i in range(k):
        lo, hi = bounds[i], bounds[i + 1]
        test = np.arange(lo, hi)
        purge_lo, purge_hi = lo - embargo, hi + embargo
        train = np.array([j for j in range(n) if j < purge_lo or j >= purge_hi], dtype=int)
        folds.append((train, test))
    return folds


def purged_cv_evaluate(pnl: pd.Series, k: int = 5, embargo: int = 0) -> dict:
    """Purged K-fold over a fixed strategy's per-trade P&L: block-wise OOS edge.

    The strategy is fixed (no fitting), so test folds partition all trades and
    the useful signal is STABILITY: does each held-out block independently show
    a positive mean? Reports per-fold mean/t, fraction of positive folds, the
    worst fold, and a pooled Newey-West t over the full (all-OOS) series.
    """
    a = _clean(pnl)
    n = a.size
    if n < k or k < 2:
        return {"k": int(k), "n": int(n), "folds": [], "frac_positive": 0.0,
                "pooled_oos_t": 0.0, "worst_fold_mean": 0.0, "insufficient": True}
    per_fold = []
    for train, test in purged_kfold_indices(n, k, embargo):
        seg = a[test]
        m = float(seg.mean())
        t = float(m / (seg.std(ddof=1) / np.sqrt(seg.size))) if seg.size > 2 and seg.std(ddof=1) > 0 else 0.0
        per_fold.append({"mean": m, "t": t, "n": int(seg.size)})
    means = [f["mean"] for f in per_fold]
    frac_pos = float(np.mean([m > 0 for m in means]))
    pooled = newey_west_tstat(pd.Series(a))["t_nw"]   # all trades are OOS (fixed strategy)
    return {"k": int(k), "n": int(n), "folds": per_fold,
            "frac_positive": frac_pos, "pooled_oos_t": float(pooled),
            "worst_fold_mean": float(min(means)), "insufficient": False}


def trade_significance_report(pnl: pd.Series, *, k: int = 5, embargo: int = 0,
                              n_trials: int = 1) -> dict:
    """One-call verdict combining NW-t, effective-N, DSR, purged-CV.

    ``significant`` requires: NW t ≥ 2.0 AND purged-CV frac_positive ≥ 0.6 AND
    (DSR ≥ 0.9 when n_trials>1, else the single-strategy PSR isn't gated on).
    """
    nw = newey_west_tstat(pnl)
    n_eff = effective_sample_size(pnl)
    dsr = deflated_sharpe(pnl, n_trials=n_trials)
    cv = purged_cv_evaluate(pnl, k=k, embargo=embargo)

    dsr_ok = (dsr["dsr"] >= 0.9) if n_trials > 1 else True
    significant = bool(nw["t_nw"] >= 2.0 and cv["frac_positive"] >= 0.6
                       and dsr_ok and not cv.get("insufficient"))
    return {
        "mean": nw["mean"], "n": nw["n"], "n_eff": float(n_eff),
        "t_naive": nw["t_naive"], "t_nw": nw["t_nw"], "nw_lags": nw["lags"],
        "sharpe": dsr["sharpe"], "psr": dsr["psr"], "dsr": dsr["dsr"],
        "n_trials": int(n_trials),
        "purged_cv": {"k": cv["k"], "frac_positive": cv["frac_positive"],
                      "pooled_oos_t": cv["pooled_oos_t"],
                      "worst_fold_mean": cv["worst_fold_mean"]},
        "significant": significant,
    }
