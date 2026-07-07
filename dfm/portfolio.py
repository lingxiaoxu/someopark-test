"""
Portfolio construction and evaluation for the Diffusion Factor Model.

This module implements portfolio construction and evaluation methods,
including mean-variance optimization and factor portfolios as described in
Section 7 of the paper.
"""

import torch
import numpy as np
from typing import Dict, Tuple, List, Optional, Union, Any, Callable
import pandas as pd
# cvxpy is only needed by the constrained-optimizer paths (mean_variance_portfolio /
# factor_portfolio). Import lazily so the rest of the package (synthetic experiment,
# metrics, factor recovery) works in environments without cvxpy installed.
try:
    import cvxpy as cp
except ImportError:  # pragma: no cover
    cp = None


def _require_cvxpy():
    if cp is None:
        raise ImportError("cvxpy is required for this optimizer path — "
                          "install it or use the closed-form estimators instead.")
import logging
import matplotlib.pyplot as plt
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler

# Set up logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def shrinkage_estimator(
    sample_cov: np.ndarray,
    method: str = 'ledoit-wolf',
    target: Optional[str] = 'identity'
) -> np.ndarray:
    """
    Compute shrinkage estimator for covariance matrix.
    
    Args:
        sample_cov: Sample covariance matrix
        method: Shrinkage method (ledoit-wolf, constant, oracle)
        target: Shrinkage target (identity, constant-correlation)
        
    Returns:
        Shrinkage estimator of the covariance matrix
    """
    from sklearn.covariance import ledoit_wolf, oas
    
    if method == 'ledoit-wolf':
        # Implementation follows Ledoit and Wolf (2004)
        if target == 'identity':
            shrinkage_cov, shrinkage_factor = ledoit_wolf(None, assume_centered=True, 
                                                        block_size=1000, return_results=True, 
                                                        store_precision=False)
            shrinkage_cov = shrinkage_cov @ sample_cov
        elif target == 'constant-correlation':
            n = sample_cov.shape[0]
            # Get sample variances (diagonal elements)
            sample_var = np.diag(sample_cov)
            # Get sample correlations
            corr = np.eye(n)
            for i in range(n):
                for j in range(i+1, n):
                    corr[i, j] = sample_cov[i, j] / np.sqrt(sample_var[i] * sample_var[j])
                    corr[j, i] = corr[i, j]
            
            # Compute average correlation
            avg_corr = (np.sum(corr) - n) / (n * (n - 1))
            
            # Construct target matrix (constant correlation)
            target_matrix = np.ones((n, n)) * avg_corr
            np.fill_diagonal(target_matrix, 1.0)
            
            # Apply shrinkage
            target_scaled = target_matrix * np.sqrt(np.outer(sample_var, sample_var))
            shrinkage_factor = ledoit_wolf_shrinkage_factor(sample_cov, target_scaled)
            shrinkage_cov = (1 - shrinkage_factor) * sample_cov + shrinkage_factor * target_scaled
        else:
            raise ValueError(f"Unknown target: {target}")
    
    elif method == 'oas':
        # Oracle Approximating Shrinkage
        shrinkage_cov, shrinkage_factor = oas(None, assume_centered=True)
        shrinkage_cov = shrinkage_cov @ sample_cov
    
    elif method == 'constant':
        # Simple constant shrinkage
        shrinkage_factor = 0.5  # Can be set differently
        if target == 'identity':
            target_matrix = np.eye(sample_cov.shape[0]) * np.mean(np.diag(sample_cov))
        elif target == 'constant-correlation':
            n = sample_cov.shape[0]
            sample_var = np.diag(sample_cov)
            corr = np.eye(n)
            for i in range(n):
                for j in range(i+1, n):
                    corr[i, j] = sample_cov[i, j] / np.sqrt(sample_var[i] * sample_var[j])
                    corr[j, i] = corr[i, j]
            
            avg_corr = (np.sum(corr) - n) / (n * (n - 1))
            target_matrix = np.ones((n, n)) * avg_corr
            np.fill_diagonal(target_matrix, 1.0)
            target_matrix = target_matrix * np.sqrt(np.outer(sample_var, sample_var))
        else:
            raise ValueError(f"Unknown target: {target}")
        
        shrinkage_cov = (1 - shrinkage_factor) * sample_cov + shrinkage_factor * target_matrix
    
    elif method == 'oracle':
        # Requires knowledge of true covariance matrix - just for simulation
        # Here, we'll just return the input as placeholder
        shrinkage_cov = sample_cov
    
    else:
        raise ValueError(f"Unknown method: {method}")
    
    return shrinkage_cov


def ledoit_wolf_shrinkage_factor(sample_cov: np.ndarray, target: np.ndarray) -> float:
    """
    Compute the Ledoit-Wolf shrinkage factor.
    
    Args:
        sample_cov: Sample covariance matrix
        target: Target matrix
        
    Returns:
        Optimal shrinkage intensity
    """
    n = sample_cov.shape[0]
    
    # Compute differences
    diff = sample_cov - target
    
    # Estimate variance of the difference
    var_diff = np.mean(diff**2)
    
    # Estimate the optimal intensity
    shrinkage_factor = min(1.0, var_diff / (np.mean(sample_cov**2) + 1e-8))
    
    return shrinkage_factor

def mean_variance_portfolio(
    returns: np.ndarray,
    risk_aversion: float = 3.0,
    mean_method: str = 'sample',
    cov_method: str = 'sample',
    max_weight: float = 0.05,
    min_weight: float = None,
    sector_constraints: Optional[Dict[str, List[int]]] = None,
    sector_max_weights: Optional[Dict[str, float]] = None
) -> np.ndarray:
    """
    Compute mean-variance optimal portfolio weights.
    
    Args:
        returns: Returns array of shape (n_samples, n_assets)
        risk_aversion: Risk aversion parameter
        mean_method: Method for estimating expected returns ('sample', 'zero', 'custom')
        cov_method: Method for estimating covariance ('sample', 'shrinkage')
        max_weight: Maximum weight for any single asset
        min_weight: Minimum weight for any single asset (can be negative)
        sector_constraints: Dictionary mapping sector names to lists of asset indices
        sector_max_weights: Dictionary mapping sector names to maximum sector weights
        
    Returns:
        Portfolio weights
    """
    _require_cvxpy()
    n, d = returns.shape
    
    # Estimate expected returns
    if mean_method == 'sample':
        mu = np.mean(returns, axis=0)
    elif mean_method == 'zero':
        mu = np.zeros(d)
    elif mean_method == 'custom':
        # This would be a customized expected return estimate
        raise NotImplementedError("Custom mean method not implemented")
    else:
        raise ValueError(f"Unknown mean method: {mean_method}")
    
    # Estimate covariance matrix
    if cov_method == 'sample':
        sigma = np.cov(returns, rowvar=False)
    elif cov_method == 'shrinkage':
        sample_cov = np.cov(returns, rowvar=False)
        sigma = shrinkage_estimator(sample_cov, method='ledoit-wolf', target='identity')
    else:
        raise ValueError(f"Unknown covariance method: {cov_method}")
    
    # Make sure covariance matrix is positive definite
    min_eig = np.min(np.linalg.eigvalsh(sigma))
    if min_eig < 1e-8:
        # Add small value to diagonal to ensure positive definiteness
        sigma = sigma + (1e-8 - min_eig) * np.eye(d)
    
    # Set up optimization problem
    w = cp.Variable(d)
    
    # Expected return
    expected_return = mu @ w
    
    # Risk (variance)
    risk = cp.quad_form(w, sigma)
    
    # Objective: maximize utility = expected_return - risk_aversion * risk / 2
    objective = cp.Maximize(expected_return - risk_aversion * risk / 2)
    
    # Constraints
    constraints = [cp.sum(w) == 1]  # Fully invested
    
    # Individual asset constraints
    if max_weight is not None:
        constraints.append(w <= max_weight)
    
    if min_weight is not None:
        constraints.append(w >= min_weight)
    else:
        constraints.append(w >= -max_weight)  # Default to symmetric short constraint
    
    # Sector constraints
    if sector_constraints is not None and sector_max_weights is not None:
        for sector, indices in sector_constraints.items():
            if sector in sector_max_weights:
                sector_weight = cp.sum(w[indices])
                constraints.append(sector_weight <= sector_max_weights[sector])
    
    # Solve the problem
    problem = cp.Problem(objective, constraints)
    try:
        problem.solve(solver=cp.OSQP)
        
        if problem.status not in ['optimal', 'optimal_inaccurate']:
            # If optimization fails, fall back to equal weights
            logger.warning(f"Optimization failed with status {problem.status}, falling back to equal weights")
            return np.ones(d) / d
        
        return w.value
    except Exception as e:
        logger.error(f"Optimization error: {e}")
        return np.ones(d) / d


def factor_portfolio(
    returns: np.ndarray,
    factors: np.ndarray,
    method: str = 'tangency',
    cov_method: str = 'sample',
    max_weight: float = 0.05
) -> np.ndarray:
    """
    Construct a portfolio based on extracted factors.
    
    Args:
        returns: Returns array of shape (n_samples, n_assets)
        factors: Factor matrix of shape (n_samples, n_factors)
        method: Portfolio construction method ('tangency', 'minimum-variance', 'equal-weight')
        cov_method: Method for estimating covariance ('sample', 'shrinkage')
        max_weight: Maximum weight for any single factor
        
    Returns:
        Portfolio weights for the factors
    """
    _require_cvxpy()
    n, k = factors.shape
    
    # Estimate factor means
    factor_means = np.mean(factors, axis=0)
    
    # Estimate factor covariance
    if cov_method == 'sample':
        factor_cov = np.cov(factors, rowvar=False)
    elif cov_method == 'shrinkage':
        sample_cov = np.cov(factors, rowvar=False)
        factor_cov = shrinkage_estimator(sample_cov, method='ledoit-wolf', target='identity')
    else:
        raise ValueError(f"Unknown covariance method: {cov_method}")
    
    # Make sure covariance matrix is positive definite
    min_eig = np.min(np.linalg.eigvalsh(factor_cov))
    if min_eig < 1e-8:
        # Add small value to diagonal to ensure positive definiteness
        factor_cov = factor_cov + (1e-8 - min_eig) * np.eye(k)
    
    # Construct portfolio based on method
    if method == 'tangency':
        # Tangency portfolio (maximum Sharpe ratio)
        # w ∝ Σ^{-1} μ
        factor_weights = np.linalg.solve(factor_cov, factor_means)
        # Normalize to sum to 1
        factor_weights = factor_weights / np.sum(factor_weights)
    
    elif method == 'minimum-variance':
        # Minimum variance portfolio
        # w ∝ Σ^{-1} 1
        ones = np.ones(k)
        factor_weights = np.linalg.solve(factor_cov, ones)
        # Normalize to sum to 1
        factor_weights = factor_weights / np.sum(factor_weights)
    
    elif method == 'equal-weight':
        # Equal-weight portfolio
        factor_weights = np.ones(k) / k
    
    else:
        raise ValueError(f"Unknown method: {method}")
    
    # Apply weight constraints if needed
    if max_weight is not None:
        # If any weight exceeds max_weight, solve a constrained problem
        if np.max(np.abs(factor_weights)) > max_weight:
            # Set up optimization problem
            w = cp.Variable(k)
            
            if method == 'tangency':
                # Maximize Sharpe ratio (approximate with a quadratic problem)
                objective = cp.Maximize(factor_means @ w - cp.quad_form(w, factor_cov) / 2)
            elif method == 'minimum-variance':
                # Minimize variance
                objective = cp.Minimize(cp.quad_form(w, factor_cov))
            
            # Constraints
            constraints = [cp.sum(w) == 1]  # Fully invested
            constraints.append(w <= max_weight)
            constraints.append(w >= -max_weight)
            
            # Solve the problem
            problem = cp.Problem(objective, constraints)
            try:
                problem.solve(solver=cp.OSQP)
                
                if problem.status not in ['optimal', 'optimal_inaccurate']:
                    # If optimization fails, fall back to proportional scaling
                    logger.warning(f"Optimization failed with status {problem.status}, falling back to scaled weights")
                    factor_weights = np.sign(factor_weights) * np.minimum(np.abs(factor_weights), max_weight)
                    factor_weights = factor_weights / np.sum(factor_weights)
                else:
                    factor_weights = w.value
            except Exception as e:
                logger.error(f"Optimization error: {e}")
                # Fall back to proportional scaling
                factor_weights = np.sign(factor_weights) * np.minimum(np.abs(factor_weights), max_weight)
                factor_weights = factor_weights / np.sum(factor_weights)
    
    return factor_weights


def convert_factor_weights_to_asset_weights(
    factor_weights: np.ndarray,
    factor_loadings: np.ndarray
) -> np.ndarray:
    """
    Convert factor portfolio weights to asset weights.
    
    Args:
        factor_weights: Factor portfolio weights of shape (n_factors,)
        factor_loadings: Factor loadings matrix of shape (n_assets, n_factors)
        
    Returns:
        Asset weights of shape (n_assets,)
    """
    # Compute asset weights
    asset_weights = factor_loadings @ factor_weights
    
    # Normalize to sum to 1
    asset_weights = asset_weights / np.sum(asset_weights)
    
    return asset_weights


def compute_portfolio_returns(
    weights: np.ndarray,
    returns: np.ndarray,
    rebalance_frequency: int = 1,
    transaction_cost: float = 0.002
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Compute portfolio returns based on weights and asset returns.
    
    Args:
        weights: Portfolio weights of shape (n_periods, n_assets)
        returns: Asset returns of shape (n_periods, n_assets)
        rebalance_frequency: Rebalancing frequency in terms of periods
        transaction_cost: Transaction cost as a fraction
        
    Returns:
        Tuple of (portfolio_returns, portfolio_weights_over_time)
    """
    n_periods, n_assets = returns.shape
    
    # If weights is 1D, repeat for all periods
    if len(weights.shape) == 1:
        weights = np.repeat(weights.reshape(1, -1), n_periods, axis=0)
    
    # Initialize arrays
    portfolio_returns = np.zeros(n_periods)
    portfolio_weights = np.zeros((n_periods, n_assets))
    
    # Initial portfolio weights
    current_weights = weights[0]
    portfolio_weights[0] = current_weights
    
    # Compute portfolio returns
    for t in range(n_periods):
        # Compute portfolio return for this period
        portfolio_returns[t] = np.sum(current_weights * returns[t])
        
        # Update weights due to price changes
        if t < n_periods - 1:
            new_weights = current_weights * (1 + returns[t])
            new_weights = new_weights / np.sum(new_weights)
            
            # Rebalance if necessary
            if (t + 1) % rebalance_frequency == 0:
                # Compute transaction costs
                if transaction_cost > 0:
                    turnover = np.sum(np.abs(new_weights - weights[t+1]))
                    cost = turnover * transaction_cost / 2  # Divide by 2 since turnover double-counts
                    portfolio_returns[t] -= cost
                
                # Update weights to target weights
                current_weights = weights[t+1]
            else:
                # Keep the drift weights
                current_weights = new_weights
            
            # Store updated weights
            portfolio_weights[t+1] = current_weights
    
    return portfolio_returns, portfolio_weights


def compute_portfolio_metrics(
    portfolio_returns: np.ndarray,
    risk_free_rate: float = 0.0
) -> Dict[str, float]:
    """
    Compute portfolio performance metrics.
    
    Args:
        portfolio_returns: Portfolio returns array
        risk_free_rate: Risk-free rate (annualized)
        
    Returns:
        Dictionary of performance metrics
    """
    # Convert to pandas Series for convenience
    returns = pd.Series(portfolio_returns)
    
    # Basic statistics
    mean_return = returns.mean()
    std_dev = returns.std()
    
    # Sharpe ratio
    excess_returns = returns - risk_free_rate / 252  # Daily risk-free rate
    sharpe_ratio = excess_returns.mean() / excess_returns.std() if excess_returns.std() > 0 else 0
    
    # Sortino ratio
    downside_returns = returns[returns < 0]
    sortino_ratio = mean_return / downside_returns.std() if len(downside_returns) > 0 else 0
    
    # Maximum drawdown
    cum_returns = (1 + returns).cumprod()
    max_drawdown = (cum_returns / cum_returns.cummax() - 1).min()
    
    # Compute CER (Certainty Equivalent Return)
    # For risk aversion parameter γ, CER = μ - γ/2 * σ²
    risk_aversion = 3.0  # Default value
    cer = mean_return - (risk_aversion / 2) * std_dev**2
    
    # Turnover
    # Note: Requires portfolio weights over time, not just returns
    # We'll skip this for now as it requires additional input
    
    # Create results dictionary
    metrics = {
        'mean': float(mean_return),
        'std': float(std_dev),
        'sharpe_ratio': float(sharpe_ratio),
        'sortino_ratio': float(sortino_ratio),
        'max_drawdown': float(max_drawdown),
        'cer': float(cer)
    }
    
    # Annualized metrics
    metrics['annualized_mean'] = float(mean_return * 252)
    metrics['annualized_std'] = float(std_dev * np.sqrt(252))
    metrics['annualized_sharpe'] = float(sharpe_ratio * np.sqrt(252))
    
    return metrics


def compare_portfolio_strategies(
    returns: np.ndarray,
    test_returns: np.ndarray,
    strategies: Dict[str, Callable[[np.ndarray], np.ndarray]],
    rebalance_frequency: int = 1,
    transaction_cost: float = 0.002,
    risk_free_rate: float = 0.0
) -> Dict[str, Dict[str, Union[float, np.ndarray]]]:
    """
    Compare multiple portfolio strategies.
    
    Args:
        returns: Training returns array of shape (n_train, n_assets)
        test_returns: Test returns array of shape (n_test, n_assets)
        strategies: Dictionary mapping strategy names to weight functions
        rebalance_frequency: Rebalancing frequency in terms of periods
        transaction_cost: Transaction cost as a fraction
        risk_free_rate: Risk-free rate (annualized)
        
    Returns:
        Dictionary of results for each strategy
    """
    results = {}
    
    for name, strategy_fn in strategies.items():
        # Compute portfolio weights
        weights = strategy_fn(returns)
        
        # Compute portfolio returns
        portfolio_returns, portfolio_weights = compute_portfolio_returns(
            weights, test_returns, rebalance_frequency, transaction_cost
        )
        
        # Compute performance metrics
        metrics = compute_portfolio_metrics(portfolio_returns, risk_free_rate)
        
        # Store results
        results[name] = {
            'weights': weights,
            'returns': portfolio_returns,
            'cum_returns': (1 + portfolio_returns).cumprod(),
            'metrics': metrics
        }
    
    return results


def plot_cumulative_returns(
    results: Dict[str, Dict[str, Union[float, np.ndarray]]],
    title: str = 'Cumulative Returns',
    figsize: Tuple[int, int] = (10, 6),
    log_scale: bool = True,
    save_path: Optional[str] = None
) -> plt.Figure:
    """
    Plot cumulative returns for multiple strategies.
    
    Args:
        results: Results dictionary from compare_portfolio_strategies
        title: Plot title
        figsize: Figure size
        log_scale: Whether to use log scale for y-axis
        save_path: Path to save the figure
        
    Returns:
        Matplotlib figure
    """
    fig, ax = plt.subplots(figsize=figsize)
    
    for name, result in results.items():
        ax.plot(result['cum_returns'], label=name)
    
    ax.set_xlabel('Time')
    ax.set_ylabel('Cumulative Return')
    ax.set_title(title)
    ax.legend()
    ax.grid(True, alpha=0.3)
    
    if log_scale:
        ax.set_yscale('log')
    
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
    
    return fig


def plot_weights_comparison(
    results: Dict[str, Dict[str, Union[float, np.ndarray]]],
    num_assets: int = 20,
    figsize: Tuple[int, int] = (12, 10),
    save_path: Optional[str] = None
) -> plt.Figure:
    """
    Plot comparison of weights for different strategies.
    
    Args:
        results: Results dictionary from compare_portfolio_strategies
        num_assets: Number of assets to show
        figsize: Figure size
        save_path: Path to save the figure
        
    Returns:
        Matplotlib figure
    """
    import seaborn as sns
    
    # Create figure
    fig, axes = plt.subplots(len(results), 1, figsize=figsize, sharex=True)
    if len(results) == 1:
        axes = [axes]
    
    # Plot weights for each strategy
    for i, (name, result) in enumerate(results.items()):
        weights = result['weights']
        if len(weights.shape) > 1:
            weights = weights[0]  # Use initial weights
        
        # Get top assets by absolute weight
        top_indices = np.argsort(np.abs(weights))[-num_assets:]
        top_weights = weights[top_indices]
        
        # Plot
        ax = axes[i]
        sns.barplot(x=top_indices, y=top_weights, ax=ax)
        ax.set_title(f"{name} Weights")
        ax.set_ylabel('Weight')
        
        # Add horizontal line at y=0
        ax.axhline(y=0, color='black', linestyle='-', alpha=0.3)
    
    # Set common x-label
    axes[-1].set_xlabel('Asset Index')
    
    plt.tight_layout()
    
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
    
    return fig


def create_mean_variance_strategies(
    methods: List[Dict[str, str]],
    risk_aversion: float = 3.0,
    max_weight: float = 0.05
) -> Dict[str, Callable[[np.ndarray], np.ndarray]]:
    """
    Create mean-variance optimization strategies with different methods.
    
    Args:
        methods: List of dictionaries with 'name', 'mean_method', and 'cov_method' keys
        risk_aversion: Risk aversion parameter
        max_weight: Maximum weight constraint
        
    Returns:
        Dictionary mapping strategy names to weight functions
    """
    strategies = {}
    
    for method in methods:
        name = method['name']
        mean_method = method['mean_method']
        cov_method = method['cov_method']
        
        strategies[name] = lambda r, mm=mean_method, cm=cov_method: mean_variance_portfolio(
            r, risk_aversion=risk_aversion, mean_method=mm, cov_method=cm, max_weight=max_weight
        )
    
    return strategies


def create_factor_portfolio_strategies(
    factor_extraction_methods: List[Dict[str, Any]],
    portfolio_methods: List[str] = ['tangency', 'minimum-variance'],
    max_weight: float = 0.05
) -> Dict[str, Callable[[np.ndarray], np.ndarray]]:
    """
    Create factor-based portfolio strategies.
    
    Args:
        factor_extraction_methods: List of dictionaries with factor extraction parameters
        portfolio_methods: List of portfolio construction methods
        max_weight: Maximum weight constraint
        
    Returns:
        Dictionary mapping strategy names to weight functions
    """
    strategies = {}
    
    for factor_method in factor_extraction_methods:
        method_name = factor_method['name']
        factor_fn = factor_method['function']
        
        for portfolio_method in portfolio_methods:
            name = f"{method_name}_{portfolio_method}"
            
            # Create strategy function
            def strategy_fn(r, fm=factor_fn, pm=portfolio_method, mw=max_weight):
                # Extract factors
                factors, loadings = fm(r)
                
                # Compute factor weights
                factor_weights = factor_portfolio(r, factors, method=pm, max_weight=mw)
                
                # Convert to asset weights
                asset_weights = convert_factor_weights_to_asset_weights(factor_weights, loadings)
                
                return asset_weights
            
            strategies[name] = strategy_fn
    
    return strategies


def evaluate_portfolios(
    train_returns: np.ndarray,
    test_returns: np.ndarray,
    risk_aversion: float = 3.0,
    max_weight: float = 0.05,
    transaction_cost: float = 0.002,
    rebalance_frequency: int = 1
) -> Dict[str, Dict[str, Union[float, np.ndarray]]]:
    """
    Evaluate different portfolio strategies including those from the paper.
    
    Args:
        train_returns: Training returns array
        test_returns: Test returns array
        risk_aversion: Risk aversion parameter
        max_weight: Maximum weight constraint
        transaction_cost: Transaction cost
        rebalance_frequency: Rebalancing frequency
        
    Returns:
        Dictionary of evaluation results
    """
    # Create basic strategies
    basic_strategies = {
        'EW': lambda r: np.ones(r.shape[1]) / r.shape[1],  # Equal-weight
        'VW': None,  # Value-weighted (would require market cap data)
        'Emp': lambda r: mean_variance_portfolio(
            r, risk_aversion=risk_aversion, mean_method='sample', cov_method='sample', max_weight=max_weight
        ),
        'Shr': lambda r: mean_variance_portfolio(
            r, risk_aversion=risk_aversion, mean_method='sample', cov_method='shrinkage', max_weight=max_weight
        )
    }
    
    # Create PCA-based factor strategies
    def pca_factors(r, k=5):
        # Center the data
        r_centered = r - np.mean(r, axis=0)
        
        # Compute PCA
        U, S, Vt = np.linalg.svd(r_centered, full_matrices=False)
        
        # Extract top k factors
        factors = U[:, :k] * S[:k]
        loadings = Vt[:k, :].T
        
        return factors, loadings
    
    def rpca_factors(r, k=5, gamma=1.0):
        # Center the data
        r_centered = r - np.mean(r, axis=0)
        
        # Compute mean
        r_mean = np.mean(r, axis=0)
        
        # Adjust covariance matrix with risk premium
        cov_matrix = np.cov(r, rowvar=False)
        cov_matrix = cov_matrix + gamma * np.outer(r_mean, r_mean)
        
        # Compute eigendecomposition
        eigenvalues, eigenvectors = np.linalg.eigh(cov_matrix)
        
        # Sort in descending order
        idx = np.argsort(eigenvalues)[::-1]
        eigenvalues = eigenvalues[idx]
        eigenvectors = eigenvectors[:, idx]
        
        # Extract top k factors
        loadings = eigenvectors[:, :k]
        
        # Project returns onto loadings to get factors
        factors = r_centered @ loadings
        
        return factors, loadings
    
    # Define poet (Principal Orthogonal complEment Thresholding) function
    def poet_factors(r, k=5, threshold=0.5):
        
        # Compute sample covariance
        cov_matrix = np.cov(r, rowvar=False)
        
        # Extract principal components
        pca = PCA(n_components=k)
        pca.fit(r)
        
        # Get low-rank component
        loadings = pca.components_.T
        explained_var = pca.explained_variance_
        low_rank = loadings @ np.diag(explained_var) @ loadings.T
        
        # Compute residual
        residual = cov_matrix - low_rank
        
        # Apply thresholding to residual
        threshold_value = threshold * np.sqrt(np.log(cov_matrix.shape[0]) / r.shape[0])
        residual_thresholded = np.where(np.abs(residual) > threshold_value, residual, 0)
        
        # Set diagonal elements to ensure positive definiteness
        np.fill_diagonal(residual_thresholded, np.diag(residual))
        
        # Compute factors
        factors = r @ loadings
        
        return factors, loadings
    
    # Create diffusion-based strategies from the paper
    # Note: This would require generating samples from the diffusion model
    def diffusion_factors(r, diffusion_samples, k=5):
        # Use diffusion-generated samples to estimate covariance
        cov_matrix = np.cov(diffusion_samples, rowvar=False)
        
        # Compute eigendecomposition
        eigenvalues, eigenvectors = np.linalg.eigh(cov_matrix)
        
        # Sort in descending order
        idx = np.argsort(eigenvalues)[::-1]
        eigenvalues = eigenvalues[idx]
        eigenvectors = eigenvectors[:, idx]
        
        # Extract top k factors
        loadings = eigenvectors[:, :k]
        
        # Project returns onto loadings to get factors
        factors = r @ loadings
        
        return factors, loadings
    
    factor_strategies = {
        'PCA': lambda r: pca_factors(r, k=5),
        'RPCA': lambda r: rpca_factors(r, k=5, gamma=1.0),
        'POET': lambda r: poet_factors(r, k=5, threshold=0.5)
    }
    
    # Combine basic and factor strategies
    all_strategies = {}
    all_strategies.update(basic_strategies)
    
    # Create factor portfolio strategies
    for name, factor_fn in factor_strategies.items():
        for portfolio_method in ['tangency']:  # Could add more methods
            strategy_name = f"{name}_{portfolio_method}"
            
            def strategy_fn(r, fm=factor_fn, pm=portfolio_method):
                # Extract factors
                factors, loadings = fm(r)
                
                # Compute factor weights
                factor_weights = factor_portfolio(r, factors, method=pm, max_weight=max_weight)
                
                # Convert to asset weights
                asset_weights = convert_factor_weights_to_asset_weights(factor_weights, loadings)
                
                return asset_weights
            
            all_strategies[strategy_name] = strategy_fn
    
    # Remove None strategies (like VW that requires additional data)
    all_strategies = {k: v for k, v in all_strategies.items() if v is not None}
    
    # Evaluate strategies
    results = compare_portfolio_strategies(
        train_returns, test_returns, 
        all_strategies,
        rebalance_frequency=rebalance_frequency,
        transaction_cost=transaction_cost
    )
    
    return results


def evaluate_diffusion_portfolios(
    train_returns: np.ndarray,
    test_returns: np.ndarray,
    diffusion_samples: np.ndarray,
    risk_aversion: float = 3.0,
    max_weight: float = 0.05,
    transaction_cost: float = 0.002,
    rebalance_frequency: int = 1
) -> Dict[str, Dict[str, Union[float, np.ndarray]]]:
    """
    Evaluate portfolios using diffusion-generated samples.
    
    Args:
        train_returns: Training returns array
        test_returns: Test returns array
        diffusion_samples: Samples generated from diffusion model
        risk_aversion: Risk aversion parameter
        max_weight: Maximum weight constraint
        transaction_cost: Transaction cost
        rebalance_frequency: Rebalancing frequency
        
    Returns:
        Dictionary of evaluation results
    """
    # Create standard strategies
    standard_strategies = {
        'EW': lambda r: np.ones(r.shape[1]) / r.shape[1],  # Equal-weight
        'Emp': lambda r: mean_variance_portfolio(
            r, risk_aversion=risk_aversion, mean_method='sample', cov_method='sample', max_weight=max_weight
        ),
        'Shr': lambda r: mean_variance_portfolio(
            r, risk_aversion=risk_aversion, mean_method='sample', cov_method='shrinkage', max_weight=max_weight
        )
    }
    
    # Create diffusion-based strategies
    diffusion_strategies = {
        'Diff+Emp': lambda r: mean_variance_portfolio(
            diffusion_samples, risk_aversion=risk_aversion, mean_method='sample', 
            cov_method='sample', max_weight=max_weight
        ),
        'Diff+Shr': lambda r: mean_variance_portfolio(
            diffusion_samples, risk_aversion=risk_aversion, mean_method='sample', 
            cov_method='shrinkage', max_weight=max_weight
        ),
        'E-Diff': lambda r: mean_variance_portfolio(
            diffusion_samples, risk_aversion=risk_aversion, mean_method='zero', 
            cov_method='sample', max_weight=max_weight
        )
    }
    
    # Create factor portfolio strategies
    k = 5  # Number of factors
    
    # PCA on train data
    def pca_factors(r, k=k):
        # Center the data
        r_centered = r - np.mean(r, axis=0)
        
        # Compute PCA
        U, S, Vt = np.linalg.svd(r_centered, full_matrices=False)
        
        # Extract top k factors
        factors = U[:, :k] * S[:k]
        loadings = Vt[:k, :].T
        
        return factors, loadings
    
    # PCA on diffusion data
    def diff_pca_factors(r, k=k):
        # Center the data
        diffusion_centered = diffusion_samples - np.mean(diffusion_samples, axis=0)
        
        # Compute PCA on diffusion samples
        U, S, Vt = np.linalg.svd(diffusion_centered, full_matrices=False)
        
        # Extract loadings
        loadings = Vt[:k, :].T
        
        # Project original returns onto loadings to get factors
        r_centered = r - np.mean(r, axis=0)
        factors = r_centered @ loadings
        
        return factors, loadings
    
    # POET on train data
    def poet_factors(r, k=k, threshold=0.5):
        # Compute sample covariance
        cov_matrix = np.cov(r, rowvar=False)
        
        # Extract principal components
        U, S, Vt = np.linalg.svd(r - np.mean(r, axis=0), full_matrices=False)
        
        # Get low-rank component
        loadings = Vt[:k, :].T
        factors = U[:, :k] * S[:k]
        
        return factors, loadings
    
    # POET on diffusion data
    def diff_poet_factors(r, k=k, threshold=0.5):
        # Compute sample covariance from diffusion samples
        cov_matrix = np.cov(diffusion_samples, rowvar=False)
        
        # Extract principal components
        U, S, Vt = np.linalg.svd(diffusion_samples - np.mean(diffusion_samples, axis=0), full_matrices=False)
        
        # Get loadings
        loadings = Vt[:k, :].T
        
        # Project original returns onto loadings
        r_centered = r - np.mean(r, axis=0)
        factors = r_centered @ loadings
        
        return factors, loadings
    
    # RPCA on train data
    def rpca_factors(r, k=k, gamma=1.0):
        # Compute mean and covariance
        r_mean = np.mean(r, axis=0)
        cov_matrix = np.cov(r, rowvar=False)
        
        # Adjust covariance with risk premium
        adjusted_cov = cov_matrix + gamma * np.outer(r_mean, r_mean)
        
        # Compute eigendecomposition
        eigenvalues, eigenvectors = np.linalg.eigh(adjusted_cov)
        
        # Sort in descending order
        idx = np.argsort(eigenvalues)[::-1]
        eigenvalues = eigenvalues[idx]
        eigenvectors = eigenvectors[:, idx]
        
        # Extract loadings
        loadings = eigenvectors[:, :k]
        
        # Project returns to get factors
        r_centered = r - r_mean
        factors = r_centered @ loadings
        
        return factors, loadings
    
    # RPCA on diffusion data
    def diff_rpca_factors(r, k=k, gamma=1.0):
        # Compute mean and covariance from diffusion samples
        diff_mean = np.mean(diffusion_samples, axis=0)
        diff_cov = np.cov(diffusion_samples, rowvar=False)
        
        # Adjust covariance with risk premium
        adjusted_cov = diff_cov + gamma * np.outer(diff_mean, diff_mean)
        
        # Compute eigendecomposition
        eigenvalues, eigenvectors = np.linalg.eigh(adjusted_cov)
        
        # Sort in descending order
        idx = np.argsort(eigenvalues)[::-1]
        eigenvalues = eigenvalues[idx]
        eigenvectors = eigenvectors[:, idx]
        
        # Extract loadings
        loadings = eigenvectors[:, :k]
        
        # Project original returns to get factors
        r_centered = r - np.mean(r, axis=0)
        factors = r_centered @ loadings
        
        return factors, loadings
    
    # Create factor-based strategies
    factor_strategies = {
        'FF': None,  # Would require Fama-French factors data
        'PCA': pca_factors,
        'POET': poet_factors,
        'RPPCA': rpca_factors,
        'Diff+PCA': diff_pca_factors,
        'Diff+POET': diff_poet_factors,
        'Diff+RPPCA': diff_rpca_factors
    }
    
    # Create portfolio strategies based on factor models
    for name, factor_fn in factor_strategies.items():
        if factor_fn is not None:
            factor_strategy_name = f"{name}"
            
            def strategy_fn(r, fm=factor_fn):
                # Extract factors and loadings
                factors, loadings = fm(r)
                
                # Compute factor expected returns and covariance
                factor_returns = factors
                factor_mean = np.mean(factor_returns, axis=0)
                factor_cov = np.cov(factor_returns, rowvar=False)
                
                # Invert factor covariance to get weights
                factor_weights = np.linalg.solve(factor_cov, factor_mean)
                
                # Normalize weights
                if np.sum(factor_weights) != 0:
                    factor_weights = factor_weights / np.sum(factor_weights)
                
                # Convert factor weights to asset weights
                asset_weights = loadings @ factor_weights
                
                # Apply constraints
                asset_weights = np.clip(asset_weights, -max_weight, max_weight)
                asset_weights = asset_weights / np.sum(np.abs(asset_weights))
                
                return asset_weights
            
            standard_strategies[factor_strategy_name] = strategy_fn
    
    # Combine all strategies
    all_strategies = {}
    all_strategies.update(standard_strategies)
    all_strategies.update(diffusion_strategies)
    
    # Evaluate strategies
    results = compare_portfolio_strategies(
        train_returns, test_returns, 
        all_strategies,
        rebalance_frequency=rebalance_frequency,
        transaction_cost=transaction_cost
    )
    
    return results


def plot_metric_comparison_table(
    results: Dict[str, Dict[str, Union[float, np.ndarray]]],
    metrics: List[str] = ['mean', 'std', 'sharpe_ratio', 'max_drawdown', 'cer'],
    title: str = 'Strategy Performance Comparison',
    figsize: Tuple[int, int] = (10, 6),
    save_path: Optional[str] = None
) -> plt.Figure:
    """
    Plot a comparison table of metrics for different strategies.
    
    Args:
        results: Results dictionary from compare_portfolio_strategies
        metrics: List of metrics to include in the table
        title: Plot title
        figsize: Figure size
        save_path: Path to save the figure
        
    Returns:
        Matplotlib figure
    """
    # Extract metric values for each strategy
    metric_values = {}
    strategies = list(results.keys())
    
    for metric in metrics:
        metric_values[metric] = [results[strategy]['metrics'][metric] for strategy in strategies]
    
    # Create DataFrame
    df = pd.DataFrame(metric_values, index=strategies)
    
    # Create figure
    fig, ax = plt.subplots(figsize=figsize)
    ax.axis('off')
    
    # Create table
    table = ax.table(
        cellText=np.round(df.values, 4),
        rowLabels=df.index,
        colLabels=df.columns,
        cellLoc='center',
        loc='center'
    )
    
    # Set table properties
    table.auto_set_font_size(False)
    table.set_fontsize(12)
    table.scale(1.2, 1.5)
    
    # Add title
    plt.title(title, fontsize=16, pad=20)
    
    # Save figure if path provided
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
    
    return fig


def plot_training_history(
    history: Dict[str, List[float]],
    figsize: Tuple[int, int] = (12, 6),
    save_path: Optional[str] = None
) -> plt.Figure:
    """
    Plot training history metrics.
    
    Args:
        history: Dictionary with training history
        figsize: Figure size
        save_path: Path to save the figure
        
    Returns:
        Matplotlib figure
    """
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=figsize)
    
    epochs = range(1, len(history['train_loss']) + 1)
    
    # Plot losses
    ax1.plot(epochs, history['train_loss'], 'b-', label='Training Loss')
    ax1.plot(epochs, history['val_loss'], 'r-', label='Validation Loss')
    ax1.set_title('Training and Validation Loss')
    ax1.set_xlabel('Epochs')
    ax1.set_ylabel('Loss')
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    
    # Plot learning rate
    ax2.plot(epochs, history['learning_rate'], 'g-')
    ax2.set_title('Learning Rate')
    ax2.set_xlabel('Epochs')
    ax2.set_ylabel('Learning Rate')
    ax2.grid(True, alpha=0.3)
    
    plt.tight_layout()
    
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
    
    return fig


if __name__ == "__main__":
    # Test portfolio construction and evaluation
    import numpy as np
    
    # Generate synthetic returns
    n_periods = 1000
    n_assets = 50
    np.random.seed(42)
    
    # Generate asset returns with some correlation structure
    cov_matrix = np.zeros((n_assets, n_assets))
    for i in range(n_assets):
        for j in range(n_assets):
            cov_matrix[i, j] = 0.6 ** abs(i - j) * 0.001
    
    mean_returns = np.linspace(0.0005, 0.002, n_assets)
    
    # Generate returns from a multivariate normal distribution
    returns = np.random.multivariate_normal(mean_returns, cov_matrix, n_periods)
    
    # Split into train and test
    train_returns = returns[:800]
    test_returns = returns[800:]
    
    # Test mean-variance optimization
    weights_mv = mean_variance_portfolio(train_returns, risk_aversion=3.0, max_weight=0.05)
    print("Mean-Variance Portfolio Weights:", weights_mv)
    
    # Test factor portfolio
    # Extract factors using PCA
    n_factors = 5
    pca = PCA(n_components=n_factors)
    factors = pca.fit_transform(train_returns)
    loadings = pca.components_.T
    
    weights_factor = factor_portfolio(train_returns, factors, method='tangency', max_weight=0.1)
    print("Factor Portfolio Weights:", weights_factor)
    
    asset_weights = convert_factor_weights_to_asset_weights(weights_factor, loadings)
    print("Asset Weights from Factor Portfolio:", asset_weights)
    
    # Test portfolio performance
    portfolio_returns, _ = compute_portfolio_returns(weights_mv, test_returns, rebalance_frequency=20, transaction_cost=0.002)
    metrics = compute_portfolio_metrics(portfolio_returns)
    print("\nPortfolio Performance Metrics:")
    for key, value in metrics.items():
        print(f"  {key}: {value}")
    
    # Test strategy comparison
    strategies = {
        'EW': lambda r: np.ones(r.shape[1]) / r.shape[1],
        'MV': lambda r: mean_variance_portfolio(r, risk_aversion=3.0, max_weight=0.05),
        'Factor': lambda r: asset_weights
    }
    
    results = compare_portfolio_strategies(train_returns, test_returns, strategies, rebalance_frequency=20, transaction_cost=0.002)
    
    print("\nStrategy Comparison:")
    for name, result in results.items():
        print(f"  {name} Sharpe Ratio: {result['metrics']['sharpe_ratio']:.4f}")