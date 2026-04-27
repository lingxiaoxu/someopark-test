"""
Portfolio Optimizer
===================
Weight optimization for sector ETF portfolio.

qlib Integration (primary)
--------------------------
- `PortfolioOptimizer` (qlib.contrib.strategy.optimizer.optimizer)
    methods: "inv" (inv_vol), "gmv", "rp" (risk_parity), "mvo" (mean-variance)
- `ShrinkCovEstimator` (qlib.model.riskmodel.shrink)
    alpha: "lw" (Ledoit-Wolf), "oas"; target: "const_var", "const_corr", "single_factor"
- `StructuredCovEstimator` (qlib.model.riskmodel.structured)
    factor_model: "pca" | "fa"; num_factors: int
- `POETCovEstimator` (qlib.model.riskmodel.poet)
    num_factors, thresh, thresh_method: "soft"|"hard"|"scad"

All qlib components fall back to custom/sklearn implementations when qlib is unavailable.

Supported optimizer methods (public interface)
----------------------------------------------
- "inv_vol"     : Inverse volatility weighting          → qlib method="inv"
- "risk_parity" : Equal risk contribution               → qlib method="rp"
- "gmv"         : Global minimum variance               → qlib method="gmv"
- "mvo"         : Mean-variance optimization            → qlib method="mvo"
- "equal_weight": Equal weight across selected sectors  → manual (1/N)

Supported covariance methods
----------------------------
- "ledoit_wolf"               : LW shrinkage, const_var target  (default)
- "ledoit_wolf_const_corr"    : LW shrinkage, const_corr target
- "ledoit_wolf_single_factor" : LW shrinkage, single_factor target
- "oas"                       : OAS shrinkage, const_var target
- "structured_pca"            : PCA factor model (qlib StructuredCovEstimator)
- "structured_fa"             : FA factor model  (qlib StructuredCovEstimator)
- "poet"                      : POET thresholded estimator
- "sample"                    : Plain sample covariance (no shrinkage)

Design
------
Input : composite z-scores (from signals/composite.py)
         → determines WHICH sectors to hold and their RELATIVE attractiveness
Output: allocation weights (dict {ticker: float})

Weight constraints (max_weight) are applied post-optimization via clip+renorm
(qlib PortfolioOptimizer enforces 0 ≤ w ≤ 1 only; further capping done here).
"""

from __future__ import annotations

import logging
import sys
import io
import warnings
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# qlib imports with graceful fallback
# ---------------------------------------------------------------------------

# Suppress qlib init-time stderr noise during import
_qlib_import_stderr = sys.stderr
sys.stderr = io.StringIO()

try:
    from qlib.model.riskmodel import (
        ShrinkCovEstimator,
        StructuredCovEstimator,
        POETCovEstimator,
    )
    _QLIB_COV_AVAILABLE = True
    logger.debug("qlib covariance estimators (Shrink/Structured/POET) loaded.")
except Exception:
    _QLIB_COV_AVAILABLE = False
    logger.warning(
        "qlib covariance estimators not available. "
        "Using sklearn LedoitWolf fallback for Ledoit-Wolf; "
        "structured/POET methods unavailable."
    )

try:
    from qlib.contrib.strategy.optimizer.optimizer import (
        PortfolioOptimizer as QlibPortfolioOptimizer,
    )
    _QLIB_OPT_AVAILABLE = True
    logger.debug("qlib PortfolioOptimizer loaded.")
except Exception:
    _QLIB_OPT_AVAILABLE = False
    logger.warning(
        "qlib PortfolioOptimizer not available. "
        "Using custom fallback (inv_vol / risk_parity / gmv)."
    )

try:
    from qlib.contrib.strategy.optimizer.enhanced_indexing import (
        EnhancedIndexingOptimizer as QlibEnhancedIndexingOptimizer,
    )
    _QLIB_EI_AVAILABLE = True
    logger.debug("qlib EnhancedIndexingOptimizer loaded (requires cvxpy).")
except Exception:
    _QLIB_EI_AVAILABLE = False
    logger.warning(
        "qlib EnhancedIndexingOptimizer not available (cvxpy missing?). "
        "'enhanced_indexing' method will fall back to 'gmv'."
    )

sys.stderr = _qlib_import_stderr


# ---------------------------------------------------------------------------
# Covariance estimation — qlib primary, sklearn fallback
# ---------------------------------------------------------------------------

def _compute_cov_qlib_shrink(
    returns: pd.DataFrame,
    alpha: str = "lw",
    target: str = "const_var",
) -> pd.DataFrame:
    """
    Shrinkage covariance via qlib ShrinkCovEstimator.

    Parameters
    ----------
    returns : pd.DataFrame
        Daily returns (rows = dates, columns = tickers). Already pct-change.
    alpha : str
        "lw" (Ledoit-Wolf) or "oas".
    target : str
        "const_var" | "const_corr" | "single_factor".

    Notes
    -----
    - ``is_price=False``: returns are passed directly (no pct_change inside qlib).
    - ``scale_return=False``: keep covariance in decimal-return units (not ×100²).
    - ``nan_option="fill"``: NaN cells are zero-filled (conservative for ETFs).
    """
    estimator = ShrinkCovEstimator(
        alpha=alpha,
        target=target,
        nan_option="fill",
        scale_return=False,
    )
    cov = estimator.predict(returns, is_price=False)
    if isinstance(cov, pd.DataFrame):
        return cov
    return pd.DataFrame(cov, index=returns.columns, columns=returns.columns)


def _compute_cov_qlib_structured(
    returns: pd.DataFrame,
    factor_model: str = "pca",
    num_factors: int = 3,
) -> pd.DataFrame:
    """
    Structured (factor) covariance via qlib StructuredCovEstimator.

    Uses PCA or FA to decompose the covariance into systematic + idiosyncratic.
    ``num_factors`` defaults to 3, which is reasonable for 11 GICS ETFs.
    """
    estimator = StructuredCovEstimator(
        factor_model=factor_model,
        num_factors=num_factors,
        scale_return=False,
    )
    cov = estimator.predict(returns, is_price=False)
    if isinstance(cov, pd.DataFrame):
        return cov
    return pd.DataFrame(cov, index=returns.columns, columns=returns.columns)


def _compute_cov_qlib_poet(
    returns: pd.DataFrame,
    num_factors: int = 3,
    thresh: float = 1.0,
    thresh_method: str = "soft",
) -> pd.DataFrame:
    """
    POET covariance via qlib POETCovEstimator.

    Principal Orthogonal Complement Thresholding (Fan et al. 2013).
    ``num_factors`` defaults to 3 for GICS ETF universe.
    """
    estimator = POETCovEstimator(
        num_factors=num_factors,
        thresh=thresh,
        thresh_method=thresh_method,
        scale_return=False,
    )
    cov = estimator.predict(returns, is_price=False)
    if isinstance(cov, pd.DataFrame):
        return cov
    return pd.DataFrame(cov, index=returns.columns, columns=returns.columns)


def _compute_cov_ledoit_wolf_sklearn(returns: pd.DataFrame) -> pd.DataFrame:
    """
    Ledoit-Wolf via sklearn — used only when qlib is unavailable.
    """
    from sklearn.covariance import LedoitWolf
    lw = LedoitWolf()
    lw.fit(returns.values)
    return pd.DataFrame(lw.covariance_, index=returns.columns, columns=returns.columns)


def _compute_cov_sample(returns: pd.DataFrame) -> pd.DataFrame:
    """Plain sample covariance matrix."""
    return returns.cov()


def compute_cov(
    returns: pd.DataFrame,
    method: str = "ledoit_wolf",
    min_periods: int = 63,
    num_factors: int = 3,
    poet_thresh: float = 1.0,
    poet_thresh_method: str = "soft",
) -> Optional[pd.DataFrame]:
    """
    Compute covariance matrix for a return DataFrame.

    Parameters
    ----------
    returns : pd.DataFrame
        Daily returns (rows = dates, columns = tickers).
    method : str
        Covariance estimation method. Choices:
        - "ledoit_wolf"               LW shrinkage, const_var target  (default)
        - "ledoit_wolf_const_corr"    LW shrinkage, const_corr target
        - "ledoit_wolf_single_factor" LW shrinkage, single_factor target
        - "oas"                       OAS shrinkage, const_var target
        - "structured_pca"            PCA factor model
        - "structured_fa"             FA factor model
        - "poet"                      POET thresholded estimator
        - "sample"                    Plain sample covariance (no shrinkage)
    min_periods : int
        Minimum return observations required. Returns None if insufficient.
    num_factors : int
        Number of factors for "structured_*" and "poet" methods.
    poet_thresh : float
        Threshold constant for POET.
    poet_thresh_method : str
        POET thresholding: "soft" | "hard" | "scad".

    Returns
    -------
    pd.DataFrame or None
    """
    valid = returns.dropna(how="all")
    if len(valid) < min_periods:
        logger.warning(
            f"Insufficient data for cov ({len(valid)} < {min_periods}). Returning None."
        )
        return None

    # --- qlib-based methods ---
    if _QLIB_COV_AVAILABLE:
        try:
            if method == "ledoit_wolf":
                return _compute_cov_qlib_shrink(valid, alpha="lw", target="const_var")
            elif method == "ledoit_wolf_const_corr":
                return _compute_cov_qlib_shrink(valid, alpha="lw", target="const_corr")
            elif method == "ledoit_wolf_single_factor":
                return _compute_cov_qlib_shrink(valid, alpha="lw", target="single_factor")
            elif method == "oas":
                return _compute_cov_qlib_shrink(valid, alpha="oas", target="const_var")
            elif method == "structured_pca":
                return _compute_cov_qlib_structured(valid, factor_model="pca", num_factors=num_factors)
            elif method == "structured_fa":
                return _compute_cov_qlib_structured(valid, factor_model="fa", num_factors=num_factors)
            elif method == "poet":
                return _compute_cov_qlib_poet(
                    valid, num_factors=num_factors,
                    thresh=poet_thresh, thresh_method=poet_thresh_method
                )
        except Exception as e:
            logger.warning(f"qlib cov estimator failed for method='{method}': {e}. Falling back.")

    # --- fallback or "sample" ---
    if method == "sample":
        return _compute_cov_sample(valid)
    elif method in ("ledoit_wolf", "ledoit_wolf_const_corr", "ledoit_wolf_single_factor", "oas"):
        # qlib unavailable → sklearn LW as best available fallback
        try:
            return _compute_cov_ledoit_wolf_sklearn(valid)
        except Exception as e:
            logger.warning(f"sklearn LedoitWolf failed ({e}). Using sample cov.")
            return _compute_cov_sample(valid)
    elif method in ("structured_pca", "structured_fa", "poet"):
        logger.warning(
            f"Method '{method}' requires qlib (unavailable). Falling back to sample cov."
        )
        return _compute_cov_sample(valid)
    else:
        raise ValueError(
            f"Unknown cov method: '{method}'. "
            "Use 'ledoit_wolf', 'ledoit_wolf_const_corr', 'ledoit_wolf_single_factor', "
            "'oas', 'structured_pca', 'structured_fa', 'poet', or 'sample'."
        )


# ---------------------------------------------------------------------------
# Custom weight implementations (fallback when qlib PortfolioOptimizer unavailable)
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
    """
    Equal Risk Contribution (Risk Parity) weights.

    Spinu (2013) Newton-Raphson variant.
    Used as fallback when qlib PortfolioOptimizer is unavailable.
    """
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
    """
    Global Minimum Variance weights.

    Analytical unconstrained + scipy constrained fallback.
    Used as fallback when qlib PortfolioOptimizer is unavailable.
    """
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


def _equal_weight(n: int) -> np.ndarray:
    """Equal weight across n sectors."""
    return np.ones(n) / n


# ---------------------------------------------------------------------------
# Main optimizer — qlib PortfolioOptimizer primary, custom fallback
# ---------------------------------------------------------------------------

# Methods handled by qlib PortfolioOptimizer (standard)
_QLIB_METHOD_MAP = {
    "inv_vol": "inv",
    "risk_parity": "rp",
    "gmv": "gmv",
    "mvo": "mvo",
}

# Methods handled by qlib EnhancedIndexingOptimizer (benchmark-relative, requires cvxpy)
_QLIB_EI_METHODS = {"enhanced_indexing"}


def optimize_weights(
    scores: pd.Series,
    returns: pd.DataFrame,
    method: str = "inv_vol",
    cov_method: str = "ledoit_wolf",
    cov_lookback_days: int = 252,
    min_periods: int = 63,
    max_weight: float = 0.40,
    min_weight: float = 0.00,
    top_n: int = 4,
    min_score: float = -0.5,
    mvo_lambda: float = 1.0,
    mvo_scale_return: bool = True,
    cov_num_factors: int = 3,
    # Enhanced-indexing specific parameters
    current_weights: Optional[pd.Series] = None,
    benchmark_weights: Optional[pd.Series] = None,
    ei_lamb: float = 1.0,
    ei_delta: Optional[float] = 0.4,
    ei_b_dev: Optional[float] = 0.30,
) -> pd.Series:
    """
    Compute portfolio weights from composite z-scores and historical returns.

    Algorithm
    ---------
    1. Filter to top N sectors with score >= min_score (long-only).
    2. Estimate covariance from cov_lookback_days of daily returns.
    3. Optimize weights using qlib PortfolioOptimizer (primary) or custom (fallback).
    4. Apply box constraints (max_weight, min_weight) via clip + renorm.
    5. Normalize to sum to 1.0.

    Parameters
    ----------
    scores : pd.Series
        Composite z-scores for all tickers (index = tickers).
    returns : pd.DataFrame
        Historical daily returns (rows = dates, columns = all tickers).
    method : str
        "inv_vol" | "risk_parity" | "gmv" | "mvo" | "equal_weight" | "enhanced_indexing".
        "inv_vol"/"risk_parity"/"gmv"/"mvo" → qlib PortfolioOptimizer.
        "enhanced_indexing" → qlib EnhancedIndexingOptimizer (benchmark-relative,
        requires cvxpy; uses StructuredCovEstimator for factor decomposition).
    cov_method : str
        Covariance estimation method; see ``compute_cov()`` for options.
    cov_lookback_days : int
        Rolling window for covariance estimation.
    min_periods : int
        Minimum return observations required.
    max_weight : float
        Maximum single-sector weight (default 0.40).
    min_weight : float
        Minimum single-sector weight (default 0.00).
    top_n : int
        Number of top-ranked sectors to hold (default 4).
    min_score : float
        Minimum z-score for inclusion (default -0.5).
    mvo_lambda : float
        Risk aversion parameter for "mvo" method (larger = more focus on return).
    mvo_scale_return : bool
        Whether to scale expected returns in MVO (qlib default = True).
    cov_num_factors : int
        Number of factors for structured/POET covariance estimators.
    current_weights : pd.Series, optional
        Current portfolio weights (for turnover control in enhanced_indexing).
    benchmark_weights : pd.Series, optional
        Benchmark sector weights (for enhanced_indexing). Defaults to equal-weight.
    ei_lamb : float
        Risk aversion for enhanced_indexing (larger = more tracking error tolerance).
    ei_delta : float, optional
        Total turnover limit for enhanced_indexing.
    ei_b_dev : float, optional
        Max deviation from benchmark per sector (enhanced_indexing).

    Returns
    -------
    pd.Series
        Portfolio weights (index = tickers, sum = 1.0).
        Non-selected tickers have weight 0.0.
    """
    all_tickers = list(scores.index)
    weights_out = pd.Series(0.0, index=all_tickers)

    # Step 1: Select top N sectors with score >= min_score
    valid_scores = scores[scores >= min_score].dropna()
    if valid_scores.empty:
        logger.warning("No valid scores above min_score. Returning equal weight across all.")
        weights_out[:] = 1.0 / len(all_tickers)
        return weights_out

    selected = valid_scores.nlargest(top_n).index.tolist()
    if not selected:
        logger.warning("No sectors selected. Returning equal weight.")
        weights_out[:] = 1.0 / len(all_tickers)
        return weights_out

    # Step 2: Covariance estimation
    sel_returns = returns[selected].dropna(how="all")
    if len(sel_returns) > cov_lookback_days:
        sel_returns = sel_returns.iloc[-cov_lookback_days:]

    cov_df = compute_cov(
        sel_returns,
        method=cov_method,
        min_periods=min_periods,
        num_factors=cov_num_factors,
    )
    n = len(selected)

    # Step 3: Optimize using qlib PortfolioOptimizer (primary) or custom (fallback)
    if cov_df is None or method == "equal_weight":
        raw_w = _equal_weight(n)
    else:
        cov_mat = cov_df.values
        # Annualize for numeric stability (relative weights unchanged by scale)
        cov_mat_annual = cov_mat * 252

        if method in _QLIB_EI_METHODS:
            # --- Enhanced Indexing: benchmark-relative, factor-model risk ---
            # Uses qlib StructuredCovEstimator (decomposed) + EnhancedIndexingOptimizer
            raw_w = _optimize_enhanced_indexing(
                scores=valid_scores,
                selected=selected,
                sel_returns=sel_returns,
                cov_mat_annual=cov_mat_annual,
                cov_num_factors=cov_num_factors,
                current_weights=current_weights,
                benchmark_weights=benchmark_weights,
                ei_lamb=ei_lamb,
                ei_delta=ei_delta,
                ei_b_dev=ei_b_dev,
                min_weight=min_weight,
                max_weight=max_weight,
            )
        elif _QLIB_OPT_AVAILABLE and method in _QLIB_METHOD_MAP:
            qlib_method = _QLIB_METHOD_MAP[method]
            try:
                r_vec = None
                if method == "mvo":
                    # Use z-scores as expected return proxy
                    r_vec = np.array([valid_scores[t] if t in valid_scores.index else 0.0
                                      for t in selected])

                opt = QlibPortfolioOptimizer(
                    method=qlib_method,
                    lamb=mvo_lambda if method == "mvo" else 0,
                    scale_return=mvo_scale_return,
                )
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    raw_w = np.asarray(opt(S=cov_mat_annual, r=r_vec))
                logger.debug(f"qlib PortfolioOptimizer ({qlib_method}) succeeded.")
            except Exception as e:
                logger.warning(
                    f"qlib PortfolioOptimizer failed ({e}). Using custom fallback for {method}."
                )
                raw_w = _custom_optimize(method, cov_mat_annual, min_weight, max_weight)
        elif method in _QLIB_METHOD_MAP:
            # qlib unavailable → custom fallback
            raw_w = _custom_optimize(method, cov_mat_annual, min_weight, max_weight)
        else:
            raise ValueError(
                f"Unknown optimizer method: '{method}'. "
                f"Use one of: {list(_QLIB_METHOD_MAP.keys()) + list(_QLIB_EI_METHODS) + ['equal_weight']}."
            )

    # Step 4: Apply box constraints (iterative water-filling)
    # When n * max_weight < 1.0 the constraint is infeasible (too few sectors);
    # fall back to equal weight and let risk.py add a concentration cash buffer.
    if n > 0 and n * max_weight < 1.0 - 1e-9:
        logger.warning(
            f"max_weight={max_weight} infeasible with {n} sector(s) "
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

    # Step 5: Assign to output
    for i, ticker in enumerate(selected):
        weights_out[ticker] = raw_w[i]

    return weights_out


def _optimize_enhanced_indexing(
    scores: pd.Series,
    selected: List[str],
    sel_returns: pd.DataFrame,
    cov_mat_annual: np.ndarray,
    cov_num_factors: int,
    current_weights: Optional[pd.Series],
    benchmark_weights: Optional[pd.Series],
    ei_lamb: float,
    ei_delta: Optional[float],
    ei_b_dev: Optional[float],
    min_weight: float,
    max_weight: float,
) -> np.ndarray:
    """
    Benchmark-relative optimization via qlib EnhancedIndexingOptimizer.

    Pipeline
    --------
    1. Get factor decomposition (F, cov_b, var_u) from qlib StructuredCovEstimator.
    2. Use z-scores as expected return proxy r.
    3. Call EnhancedIndexingOptimizer(r, F, cov_b, var_u, w0, wb).
    4. Fall back to GMV if either qlib component is unavailable or fails.

    Notes
    -----
    - ``benchmark_weights``: defaults to equal-weight if not provided.
    - ``current_weights``: defaults to equal-weight if not provided (no turnover history).
    - ``ei_delta``: total turnover budget for the rebalance (in weight units, e.g. 0.4 = 40%).
    - ``ei_b_dev``: max deviation from benchmark per sector (e.g. 0.30 = ±30pp).
    """
    n = len(selected)

    # Default benchmark and current weights (equal-weight)
    wb = np.array([
        float(benchmark_weights[t]) if benchmark_weights is not None and t in benchmark_weights.index
        else 1.0 / n
        for t in selected
    ])
    wb = np.clip(wb, 0, 1)
    if wb.sum() > 0:
        wb /= wb.sum()
    else:
        wb = np.ones(n) / n

    w0 = np.array([
        float(current_weights[t]) if current_weights is not None and t in current_weights.index
        else 1.0 / n
        for t in selected
    ])
    w0 = np.clip(w0, 0, 1)
    if w0.sum() > 0:
        w0 /= w0.sum()
    else:
        w0 = np.ones(n) / n

    # Expected returns: z-scores as proxy
    r_vec = np.array([
        float(scores[t]) if t in scores.index else 0.0
        for t in selected
    ])

    if not (_QLIB_COV_AVAILABLE and _QLIB_EI_AVAILABLE):
        logger.warning(
            "EnhancedIndexingOptimizer or StructuredCovEstimator unavailable. "
            "Falling back to GMV."
        )
        return _gmv_weights(cov_mat_annual, w_min=min_weight, w_max=max_weight)

    try:
        # Step 1: Get factor decomposition from qlib StructuredCovEstimator
        n_factors = min(cov_num_factors, n - 1)  # factors < n
        struct_est = StructuredCovEstimator(
            factor_model="pca",
            num_factors=n_factors,
            scale_return=False,
        )
        F, cov_b, var_u = struct_est.predict(
            sel_returns, is_price=False, return_decomposed_components=True
        )
        # F: (n_assets, n_factors), cov_b: (n_factors, n_factors), var_u: (n_assets,)
        F = np.asarray(F)
        cov_b = np.asarray(cov_b)
        var_u = np.asarray(var_u)

        # Step 2: Call qlib EnhancedIndexingOptimizer
        ei_opt = QlibEnhancedIndexingOptimizer(
            lamb=ei_lamb,
            delta=ei_delta if ei_delta is not None else 1.0,
            b_dev=ei_b_dev,
            scale_return=True,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            raw_w = np.asarray(ei_opt(r=r_vec, F=F, cov_b=cov_b, var_u=var_u, w0=w0, wb=wb))
        logger.debug("qlib EnhancedIndexingOptimizer succeeded.")
        return raw_w
    except Exception as e:
        logger.warning(
            f"EnhancedIndexingOptimizer failed ({e}). Falling back to GMV."
        )
        return _gmv_weights(cov_mat_annual, w_min=min_weight, w_max=max_weight)


def _custom_optimize(
    method: str,
    cov_mat_annual: np.ndarray,
    min_weight: float,
    max_weight: float,
) -> np.ndarray:
    """
    Fallback custom optimizer used when qlib PortfolioOptimizer is unavailable.
    Supports: "inv_vol", "risk_parity", "gmv".
    """
    if method == "inv_vol":
        return _inv_vol_weights(cov_mat_annual)
    elif method == "risk_parity":
        return _risk_parity_weights(cov_mat_annual)
    elif method == "gmv":
        return _gmv_weights(cov_mat_annual, w_min=min_weight, w_max=max_weight)
    else:
        logger.warning(f"No custom fallback for method '{method}'. Using equal weight.")
        n = cov_mat_annual.shape[0]
        return _equal_weight(n)


# ---------------------------------------------------------------------------
# Constraint application (post-optimization)
# ---------------------------------------------------------------------------

def apply_constraints(
    weights: pd.Series,
    max_weight: float = 0.40,
    min_weight: float = 0.00,
    beta_target: Optional[float] = None,
    beta_range: Tuple[float, float] = (0.85, 1.15),
    sector_betas: Optional[pd.Series] = None,
) -> pd.Series:
    """
    Apply post-optimization constraints.

    Handles:
    - Box constraints (max/min weight per sector).
    - Optional beta targeting via iterative reweighting.

    Parameters
    ----------
    weights : pd.Series
        Raw portfolio weights (sum = 1).
    max_weight : float
        Maximum single-sector weight.
    min_weight : float
        Minimum non-zero weight (zero is always allowed).
    beta_target : float, optional
        Target portfolio beta vs benchmark. If None, beta not adjusted.
    beta_range : tuple
        (min_beta, max_beta) acceptable range.
    sector_betas : pd.Series, optional
        Estimated betas for each sector vs benchmark.

    Returns
    -------
    pd.Series
        Constrained weights, sum = 1.
    """
    w = weights.copy()

    # Box constraints
    w = w.clip(lower=min_weight, upper=max_weight)
    total = w.sum()
    if total > 0:
        w = w / total

    # Beta constraint (iterative scaling)
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s: %(message)s")

    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))

    from sector_rotation.data.loader import load_all, load_returns
    from sector_rotation.data.universe import get_tickers
    from sector_rotation.signals.composite import compute_composite_signals

    prices, macro = load_all()
    etf_tickers = get_tickers(include_benchmark=False)
    etf_prices = prices[[t for t in etf_tickers if t in prices.columns]]
    daily_returns = load_returns(etf_prices)

    composite, regime_monthly, _ = compute_composite_signals(etf_prices, macro)
    latest_scores = composite.iloc[-1].dropna()

    print(f"\nLatest scores:\n{latest_scores.sort_values(ascending=False).round(3)}")
    print(f"\nqlib PortfolioOptimizer available: {_QLIB_OPT_AVAILABLE}")
    print(f"qlib Covariance Estimators available: {_QLIB_COV_AVAILABLE}")

    for method in ["inv_vol", "risk_parity", "gmv", "mvo", "equal_weight"]:
        w = optimize_weights(
            latest_scores, daily_returns, method=method, top_n=4
        )
        print(f"\n=== {method} weights ===")
        print(w[w > 0].sort_values(ascending=False).round(4))

    # Test all covariance methods
    sel = daily_returns.iloc[-252:]
    print("\n=== Covariance method comparison ===")
    for cov_m in ["ledoit_wolf", "ledoit_wolf_const_corr", "oas", "structured_pca", "sample"]:
        cov = compute_cov(sel, method=cov_m)
        status = "OK" if cov is not None else "FAILED"
        print(f"  {cov_m:<30}: {status}")
