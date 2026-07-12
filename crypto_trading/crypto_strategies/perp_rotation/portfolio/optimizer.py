"""Portfolio Optimizer — crypto perps (Plan 05 §5).

COPIED from qlib-main/sector_rotation/portfolio/optimizer.py (read-only
template). The template guarded qlib imports and fell back to self-contained
implementations; in crypto_trading there is NO qlib (isolation invariant 4),
so the guarded qlib blocks are removed and the availability flags are hard
False — the template's OWN fallbacks (inv_vol / Spinu risk-parity / GMV via
scipy SLSQP / sklearn LedoitWolf) become the only, fully-tested paths. Every
public interface, parameter, and constraint step is preserved, including
methods that now degrade exactly as the template degraded without qlib
(structured_pca / structured_fa / poet → sample cov; enhanced_indexing → GMV;
mvo → custom fallback path which the template routed to equal-weight warning).

Adaptations (plan "Change" column): 24/7 covariance — annualization 252 → 365,
default cov_lookback_days 252 → 365.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TRADING_DAYS = 365  # crypto adaptation (template annualized with 252)

# Template flags — hard False here (no qlib in someopark_run; provenance note).
_QLIB_COV_AVAILABLE = False
_QLIB_OPT_AVAILABLE = False
_QLIB_EI_AVAILABLE = False


# ---------------------------------------------------------------------------
# Covariance estimation — sklearn LW primary (template fallback promoted)
# ---------------------------------------------------------------------------

def _compute_cov_ledoit_wolf_sklearn(returns: pd.DataFrame) -> pd.DataFrame:
    """Ledoit-Wolf via sklearn — the template's qlib-less path (verbatim)."""
    from sklearn.covariance import LedoitWolf
    lw = LedoitWolf()
    lw.fit(returns.values)
    return pd.DataFrame(lw.covariance_, index=returns.columns, columns=returns.columns)


def _compute_cov_oas_sklearn(returns: pd.DataFrame) -> pd.DataFrame:
    """OAS via sklearn (template routed 'oas' to qlib; sklearn.OAS is the
    faithful qlib-less equivalent — ADAPTED, flagged)."""
    from sklearn.covariance import OAS
    oas = OAS()
    oas.fit(returns.values)
    return pd.DataFrame(oas.covariance_, index=returns.columns, columns=returns.columns)


def _compute_cov_sample(returns: pd.DataFrame) -> pd.DataFrame:
    """Plain sample covariance matrix (template verbatim)."""
    return returns.cov()


def compute_cov(
    returns: pd.DataFrame,
    method: str = "ledoit_wolf",
    min_periods: int = 63,
    num_factors: int = 3,
    poet_thresh: float = 1.0,
    poet_thresh_method: str = "soft",
) -> Optional[pd.DataFrame]:
    """Covariance matrix (template interface preserved).

    "ledoit_wolf*" → sklearn LW; "oas" → sklearn OAS; "sample" → sample;
    "structured_pca"/"structured_fa"/"poet" required qlib → fall back to
    sample cov with the template's own warning semantics.
    """
    valid = returns.dropna(how="all")
    if len(valid) < min_periods:
        logger.warning(
            f"Insufficient data for cov ({len(valid)} < {min_periods}). Returning None."
        )
        return None

    if method == "sample":
        return _compute_cov_sample(valid)
    elif method in ("ledoit_wolf", "ledoit_wolf_const_corr", "ledoit_wolf_single_factor"):
        try:
            return _compute_cov_ledoit_wolf_sklearn(valid)
        except Exception as e:
            logger.warning(f"sklearn LedoitWolf failed ({e}). Using sample cov.")
            return _compute_cov_sample(valid)
    elif method == "oas":
        try:
            return _compute_cov_oas_sklearn(valid)
        except Exception as e:
            logger.warning(f"sklearn OAS failed ({e}). Using sample cov.")
            return _compute_cov_sample(valid)
    elif method in ("structured_pca", "structured_fa", "poet"):
        logger.warning(
            f"Method '{method}' requires qlib (not present in crypto_trading). "
            "Falling back to sample cov."
        )
        return _compute_cov_sample(valid)
    else:
        raise ValueError(
            f"Unknown cov method: '{method}'. "
            "Use 'ledoit_wolf', 'ledoit_wolf_const_corr', 'ledoit_wolf_single_factor', "
            "'oas', 'structured_pca', 'structured_fa', 'poet', or 'sample'."
        )


# ---------------------------------------------------------------------------
# Weight implementations (template verbatim — these were the fallbacks)
# ---------------------------------------------------------------------------

def _inv_vol_weights(cov: np.ndarray) -> np.ndarray:
    """Inverse volatility weights: w_i = (1/σ_i) / Σ(1/σ_j)."""
    vols = np.sqrt(np.diag(cov))
    vols = np.where(vols < 1e-10, 1e-10, vols)
    inv_vols = 1.0 / vols
    return inv_vols / inv_vols.sum()


def _risk_parity_weights(
    cov: np.ndarray,
    max_iter: int = 500,
    tol: float = 1e-8,
) -> np.ndarray:
    """Equal Risk Contribution — Spinu (2013) Newton-Raphson variant (verbatim)."""
    n = cov.shape[0]
    w = _inv_vol_weights(cov)

    for _ in range(max_iter):
        sigma_w = cov @ w
        port_var = w @ sigma_w
        if port_var <= 0:
            break
        target_rc = 1.0 / n
        grad = sigma_w / port_var - target_rc / w
        h = 1.0 / (sigma_w / port_var)
        delta = -grad * h
        step = 0.01 / max(abs(delta))
        w_new = w + step * delta
        w_new = np.maximum(w_new, 1e-8)
        w_new /= w_new.sum()
        if np.max(np.abs(w_new - w)) < tol:
            w = w_new
            break
        w = w_new

    return w / w.sum()


def _gmv_weights(cov: np.ndarray, w_min: float = 0.0, w_max: float = 1.0) -> np.ndarray:
    """Global Minimum Variance — analytical + scipy SLSQP constrained (verbatim)."""
    n = cov.shape[0]

    if w_min == 0.0 and w_max == 1.0:
        try:
            cov_inv = np.linalg.inv(cov + np.eye(n) * 1e-8)
            ones = np.ones(n)
            w = cov_inv @ ones
            w = w / w.sum()
            return np.clip(w, 0, 1)
        except np.linalg.LinAlgError:
            logger.warning("GMV matrix inversion failed. Falling back to inv_vol.")
            return _inv_vol_weights(cov)
    else:
        try:
            from scipy.optimize import minimize

            def portfolio_var(w):
                return w @ cov @ w

            def portfolio_var_grad(w):
                return 2 * cov @ w

            constraints = [{"type": "eq", "fun": lambda w: w.sum() - 1}]
            bounds = [(w_min, w_max)] * n
            w0 = np.ones(n) / n
            result = minimize(
                portfolio_var, w0,
                jac=portfolio_var_grad,
                method="SLSQP",
                bounds=bounds,
                constraints=constraints,
                options={"ftol": 1e-9, "maxiter": 500},
            )
            if result.success:
                w = result.x
                w = np.maximum(w, 0)
                return w / w.sum()
            else:
                logger.warning(f"GMV optimization failed: {result.message}. Falling back to inv_vol.")
                return _inv_vol_weights(cov)
        except ImportError:
            logger.warning("scipy not available for constrained GMV. Using unconstrained.")
            return _gmv_weights(cov, w_min=0.0, w_max=1.0)


def _mvo_weights(
    cov: np.ndarray,
    r_vec: np.ndarray,
    lamb: float = 1.0,
    w_max: float = 1.0,
) -> np.ndarray:
    """Mean-variance weights max r·w − λ·wᵀΣw (long-only, sum=1) via SLSQP.

    ADAPTED: the template routed 'mvo' to qlib's PortfolioOptimizer and had NO
    custom fallback (fell to equal-weight with a warning). A dead 'mvo' would
    silently degrade the config surface, so this faithful qlib-less equivalent
    is added and flagged.
    """
    try:
        from scipy.optimize import minimize
        n = cov.shape[0]

        def neg_util(w):
            return -(r_vec @ w - lamb * (w @ cov @ w))

        constraints = [{"type": "eq", "fun": lambda w: w.sum() - 1}]
        bounds = [(0.0, w_max)] * n
        result = minimize(neg_util, np.ones(n) / n, method="SLSQP",
                          bounds=bounds, constraints=constraints,
                          options={"ftol": 1e-9, "maxiter": 500})
        if result.success:
            w = np.maximum(result.x, 0)
            return w / w.sum()
    except Exception as e:
        logger.warning(f"MVO optimization failed ({e}).")
    return _inv_vol_weights(cov)


def _equal_weight(n: int) -> np.ndarray:
    """Equal weight across n perps (verbatim)."""
    return np.ones(n) / n


# ---------------------------------------------------------------------------
# Main optimizer — template interface preserved
# ---------------------------------------------------------------------------

_METHOD_SET = {"inv_vol", "risk_parity", "gmv", "mvo", "equal_weight", "enhanced_indexing"}


def optimize_weights(
    scores: pd.Series,
    returns: pd.DataFrame,
    method: str = "inv_vol",
    cov_method: str = "ledoit_wolf",
    cov_lookback_days: int = 365,
    min_periods: int = 63,
    max_weight: float = 0.45,
    min_weight: float = 0.00,
    top_n: int = 4,
    min_score: float = -0.5,
    mvo_lambda: float = 1.0,
    mvo_scale_return: bool = True,
    cov_num_factors: int = 3,
    current_weights: Optional[pd.Series] = None,
    benchmark_weights: Optional[pd.Series] = None,
    ei_lamb: float = 1.0,
    ei_delta: Optional[float] = 0.4,
    ei_b_dev: Optional[float] = 0.30,
) -> pd.Series:
    """Portfolio weights from composite z-scores + historical returns.

    Template algorithm preserved:
    1. top-N filter with min_score (long-only) → 2. covariance →
    3. optimize → 4. box constraints via iterative water-filling →
    5. normalize to 1. 'enhanced_indexing' degrades to GMV (its template
    behavior whenever qlib/cvxpy was missing).
    """
    all_tickers = list(scores.index)
    weights_out = pd.Series(0.0, index=all_tickers)

    # Step 1: Select top N with score >= min_score
    valid_scores = scores[scores >= min_score].dropna()
    if valid_scores.empty:
        logger.warning("No valid scores above min_score. Returning equal weight across all.")
        weights_out[:] = 1.0 / len(all_tickers)
        return weights_out

    selected = valid_scores.nlargest(top_n).index.tolist()
    if not selected:
        logger.warning("No perps selected. Returning equal weight.")
        weights_out[:] = 1.0 / len(all_tickers)
        return weights_out

    # Step 2: Covariance
    sel_returns = returns[selected].dropna(how="all")
    if len(sel_returns) > cov_lookback_days:
        sel_returns = sel_returns.iloc[-cov_lookback_days:]

    cov_df = compute_cov(sel_returns, method=cov_method, min_periods=min_periods,
                         num_factors=cov_num_factors)
    n = len(selected)

    # Step 3: Optimize
    if cov_df is None or method == "equal_weight":
        raw_w = _equal_weight(n)
    else:
        cov_mat_annual = cov_df.values * TRADING_DAYS   # 24/7 annualization

        if method == "enhanced_indexing":
            logger.warning(
                "enhanced_indexing requires qlib/cvxpy (not present). "
                "Falling back to GMV (template degradation path).")
            raw_w = _gmv_weights(cov_mat_annual, w_min=min_weight, w_max=max_weight)
        elif method == "mvo":
            r_vec = np.array([valid_scores[t] if t in valid_scores.index else 0.0
                              for t in selected])
            raw_w = _mvo_weights(cov_mat_annual, r_vec, lamb=mvo_lambda,
                                 w_max=max_weight)
        elif method == "inv_vol":
            raw_w = _inv_vol_weights(cov_mat_annual)
        elif method == "risk_parity":
            raw_w = _risk_parity_weights(cov_mat_annual)
        elif method == "gmv":
            raw_w = _gmv_weights(cov_mat_annual, w_min=min_weight, w_max=max_weight)
        else:
            raise ValueError(
                f"Unknown optimizer method: '{method}'. Use one of: {sorted(_METHOD_SET)}."
            )

    # Step 4: Box constraints (template's iterative water-filling, verbatim)
    if n > 0 and n * max_weight < 1.0 - 1e-9:
        logger.warning(
            f"max_weight={max_weight} infeasible with {n} perp(s) "
            f"(max possible sum={n * max_weight:.2f} < 1.0). Using equal weight; "
            "risk.py will add concentration cash buffer."
        )
        raw_w = np.ones(n) / n
    else:
        for _ in range(100):
            over = raw_w > max_weight + 1e-9
            if not over.any():
                break
            raw_w = np.clip(raw_w, min_weight, max_weight)
            s = raw_w.sum()
            if s > 0:
                raw_w = raw_w / s
            else:
                raw_w = np.ones(n) / n
                break

    # Step 5: Assign
    for i, ticker in enumerate(selected):
        weights_out[ticker] = raw_w[i]

    return weights_out


def apply_constraints(
    weights: pd.Series,
    max_weight: float = 0.45,
    min_weight: float = 0.00,
    beta_target: Optional[float] = None,
    beta_range: Tuple[float, float] = (0.85, 1.15),
    sector_betas: Optional[pd.Series] = None,
) -> pd.Series:
    """Post-optimization constraints (template verbatim; betas vs KXBTCPERP)."""
    w = weights.copy()

    w = w.clip(lower=min_weight, upper=max_weight)
    total = w.sum()
    if total > 0:
        w = w / total

    if beta_target is not None and sector_betas is not None:
        port_beta = (w * sector_betas).sum()
        max_iter = 10
        for _ in range(max_iter):
            if beta_range[0] <= port_beta <= beta_range[1]:
                break
            scale = beta_target / port_beta if port_beta > 0 else 1.0
            w = w * scale
            w = w.clip(0, max_weight)
            w = w / w.sum()
            port_beta = (w * sector_betas).sum()
        logger.debug(f"Portfolio beta after constraint: {port_beta:.3f}")

    return w
