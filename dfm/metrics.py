"""
Evaluation metrics for the Diffusion Factor Model.

This module implements metrics for evaluating the performance of the diffusion model,
including distribution metrics and subspace recovery evaluation.
"""

import torch
import numpy as np
from typing import Dict, Tuple, List, Optional, Union, Any
from scipy import stats
import logging

# Set up logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def distribution_metrics(
    generated_samples: torch.Tensor,
    reference_samples: torch.Tensor,
    bins: int = 50
) -> Dict[str, float]:
    """
    Compute metrics for comparing generated and reference distributions.
    
    Args:
        generated_samples: Tensor of generated samples (batch_size, dim)
        reference_samples: Tensor of reference samples (batch_size, dim)
        bins: Number of bins for histogram-based metrics
        
    Returns:
        Dictionary of distribution metrics
    """
    # Ensure tensors are on CPU and convert to numpy
    if isinstance(generated_samples, torch.Tensor):
        generated_samples = generated_samples.cpu().numpy()
    if isinstance(reference_samples, torch.Tensor):
        reference_samples = reference_samples.cpu().numpy()
    
    # Get dimensions
    n_gen, dim_gen = generated_samples.shape
    n_ref, dim_ref = reference_samples.shape
    
    assert dim_gen == dim_ref, "Generated and reference samples must have the same dimension"
    
    # Initialize results dictionary
    metrics = {}
    
    # 1. Mean squared error of means
    mean_gen = np.mean(generated_samples, axis=0)
    mean_ref = np.mean(reference_samples, axis=0)
    mean_mse = np.mean((mean_gen - mean_ref) ** 2)
    metrics['mean_mse'] = float(mean_mse)
    
    # 2. Mean squared error of standard deviations
    std_gen = np.std(generated_samples, axis=0)
    std_ref = np.std(reference_samples, axis=0)
    std_mse = np.mean((std_gen - std_ref) ** 2)
    metrics['std_mse'] = float(std_mse)
    
    # 3. Trace of covariance matrix error (Frobenius norm)
    cov_gen = np.cov(generated_samples, rowvar=False)
    cov_ref = np.cov(reference_samples, rowvar=False)
    cov_error = np.linalg.norm(cov_gen - cov_ref, 'fro')
    metrics['cov_error'] = float(cov_error)
    
    # 4. Operator norm of covariance matrix error
    try:
        cov_error_op = np.linalg.norm(cov_gen - cov_ref, 2)
        metrics['cov_error_op'] = float(cov_error_op)
    except np.linalg.LinAlgError:
        metrics['cov_error_op'] = float('nan')
    
    # 5. Compute KL divergence approximation for a subset of dimensions (if dim is large)
    if dim_gen <= 10:
        # Use all dimensions
        dims_to_check = list(range(dim_gen))
    else:
        # Use a subset of dimensions for computational efficiency
        # Include some with highest and lowest variance, and some random ones
        var_ref = np.var(reference_samples, axis=0)
        high_var_idx = np.argsort(var_ref)[-5:]
        low_var_idx = np.argsort(var_ref)[:5]
        random_idx = np.random.choice(
            np.setdiff1d(np.arange(dim_gen), np.concatenate([high_var_idx, low_var_idx])),
            size=min(10, dim_gen - 10),
            replace=False
        )
        dims_to_check = np.concatenate([high_var_idx, low_var_idx, random_idx])
    
    kl_divs = []
    for dim_idx in dims_to_check:
        # Get samples for this dimension
        gen_dim = generated_samples[:, dim_idx]
        ref_dim = reference_samples[:, dim_idx]
        
        # Create histograms
        hist_gen, bin_edges = np.histogram(gen_dim, bins=bins, density=True)
        hist_ref, _ = np.histogram(ref_dim, bins=bin_edges, density=True)
        
        # Add small epsilon to avoid division by zero
        hist_gen = np.maximum(hist_gen, 1e-10)
        hist_ref = np.maximum(hist_ref, 1e-10)
        
        # Compute KL divergence
        kl_div = np.sum(hist_ref * np.log(hist_ref / hist_gen)) * (bin_edges[1] - bin_edges[0])
        kl_divs.append(kl_div)
    
    # Average KL divergence across dimensions
    metrics['kl_divergence_approx'] = float(np.mean(kl_divs))
    
    # 6. Wasserstein distance approximation
    wasserstein_dists = []
    for dim_idx in dims_to_check:
        # Get samples for this dimension
        gen_dim = generated_samples[:, dim_idx]
        ref_dim = reference_samples[:, dim_idx]
        
        # Compute Wasserstein distance
        wasserstein_dist = stats.wasserstein_distance(gen_dim, ref_dim)
        wasserstein_dists.append(wasserstein_dist)
    
    # Average Wasserstein distance across dimensions
    metrics['wasserstein_distance'] = float(np.mean(wasserstein_dists))
    
    # 7. Eigenvalues comparison
    eigenvalues_gen, _ = np.linalg.eigh(cov_gen)
    eigenvalues_ref, _ = np.linalg.eigh(cov_ref)
    
    # Sort eigenvalues in descending order
    eigenvalues_gen = eigenvalues_gen[::-1]
    eigenvalues_ref = eigenvalues_ref[::-1]
    
    # Relative error of top eigenvalues
    top_k = min(10, dim_gen)
    rel_eigenvalue_error = np.mean(
        np.abs(eigenvalues_gen[:top_k] - eigenvalues_ref[:top_k]) / 
        np.maximum(np.abs(eigenvalues_ref[:top_k]), 1e-10)
    )
    metrics['top_eigenvalues_rel_error'] = float(rel_eigenvalue_error)
    
    return metrics


def subspace_recovery_metrics(
    recovered_subspace: torch.Tensor,
    true_subspace: torch.Tensor,
    k: Optional[int] = None
) -> Dict[str, float]:
    """
    Compute metrics for evaluating the recovery of the factor subspace.
    """
    # Ensure tensors are on CPU and convert to numpy
    if isinstance(recovered_subspace, torch.Tensor):
        recovered_subspace = recovered_subspace.cpu().numpy()
    if isinstance(true_subspace, torch.Tensor):
        true_subspace = true_subspace.cpu().numpy()
    
    # Determine k if not provided
    if k is None:
        k = true_subspace.shape[1]
    
    # Ensure k is an integer
    k = int(k) if k is not None else true_subspace.shape[1]
    
    # Ensure recovered_subspace has at least k columns
    if recovered_subspace.shape[1] < k:
        raise ValueError(f"Recovered subspace has {recovered_subspace.shape[1]} columns, but k={k}")
    
    # Extract top k columns if needed
    recovered_subspace = recovered_subspace[:, :k]
    true_subspace = true_subspace[:, :k]
    
    # Initialize results dictionary
    metrics = {}
    
    # 1. Frobenius norm of subspace projection error
    true_projection = true_subspace @ true_subspace.T
    recovered_projection = recovered_subspace @ recovered_subspace.T
    
    projection_error_f = np.linalg.norm(true_projection - recovered_projection, 'fro')
    metrics['projection_error_frobenius'] = float(projection_error_f)
    
    # 2. Normalized projection error
    projection_error_normalized = projection_error_f / np.linalg.norm(true_projection, 'fro')
    metrics['projection_error_normalized'] = float(projection_error_normalized)
    
    # 3. Principal angles
    # Compute the singular values of true_subspace.T @ recovered_subspace
    singular_values = np.linalg.svd(true_subspace.T @ recovered_subspace, compute_uv=False)
    # Clip to [-1, 1] to handle numerical issues
    singular_values = np.clip(singular_values, -1.0, 1.0)
    principal_angles = np.arccos(singular_values)
    
    metrics['principal_angles_mean'] = float(np.mean(principal_angles))
    metrics['principal_angles_max'] = float(np.max(principal_angles))
    
    # 4. Canonical correlation (average of cosines of principal angles)
    canonical_correlation = np.mean(np.cos(principal_angles))
    metrics['canonical_correlation'] = float(canonical_correlation)
    
    # 5. Subspace inclusion metric
    # This measures how much of the true subspace is contained in the recovered subspace
    if k > 1:
        # Add small regularization term
        reg_term = 1e-8 * np.eye(recovered_subspace.shape[1])
        inclusion_metric = np.linalg.norm(true_subspace.T @ recovered_subspace @ 
                                        np.linalg.inv(recovered_subspace.T @ recovered_subspace + reg_term), 'fro')
        metrics['subspace_inclusion'] = float(inclusion_metric / np.sqrt(k))
    else:
        # Simplified case for k=1
        inclusion_metric = np.abs(true_subspace.T @ recovered_subspace)
        metrics['subspace_inclusion'] = float(inclusion_metric[0, 0])
    
    # 6. Davis-Kahan sin(θ) metrics (as in the paper)
    # Compute sin(θ) values from the principal angles
    sin_theta_values = np.sin(principal_angles)
    
    # Frobenius norm of sin(Θ)
    sin_theta_f = np.linalg.norm(sin_theta_values)
    metrics['davis_kahan_sin_theta_f'] = float(sin_theta_f)
    
    # Normalized sin(θ) metric (related to the paper's formula)
    metrics['davis_kahan_sin_theta_normalized'] = float(sin_theta_f / np.sqrt(k))
    
    # 7. Top eigenvalue preservation
    # Get eigenvalues from projections
    true_eigenvalues, _ = np.linalg.eigh(true_projection)
    recovered_eigenvalues, _ = np.linalg.eigh(recovered_projection)
    
    # Sort in descending order
    true_eigenvalues = true_eigenvalues[::-1][:k]
    recovered_eigenvalues = recovered_eigenvalues[::-1][:k]
    
    # Relative error of eigenvalues
    eigenvalue_rel_error = np.mean(
        np.abs(recovered_eigenvalues - true_eigenvalues) / 
        np.maximum(np.abs(true_eigenvalues), 1e-10)
    )
    metrics['eigenvalue_relative_error'] = float(eigenvalue_rel_error)
    
    return metrics


def eigenvalue_ratio_metrics(
    generated_eigenvalues: Union[torch.Tensor, np.ndarray],
    true_eigenvalues: Union[torch.Tensor, np.ndarray],
    k: Optional[int] = None
) -> Dict[str, float]:
    """
    Compute metrics related to eigenvalue ratios to evaluate factor structure recovery.
    
    Args:
        generated_eigenvalues: Eigenvalues of the generated samples covariance matrix
        true_eigenvalues: Eigenvalues of the true covariance matrix
        k: Number of factors to consider
        
    Returns:
        Dictionary of eigenvalue ratio metrics
    """
    # Ensure tensors are on CPU and convert to numpy
    if isinstance(generated_eigenvalues, torch.Tensor):
        generated_eigenvalues = generated_eigenvalues.cpu().numpy()
    if isinstance(true_eigenvalues, torch.Tensor):
        true_eigenvalues = true_eigenvalues.cpu().numpy()
    
    # Determine k if not provided
    if k is None:
        # Heuristic: use the "elbow" in the scree plot of true eigenvalues
        diffs = np.diff(true_eigenvalues)
        k = np.argmin(diffs) + 1
        # Ensure k is at least 2 and at most 20% of the dimension
        k = max(2, min(k, len(true_eigenvalues) // 5))
    
    # Initialize results dictionary
    metrics = {}
    
    # Ensure eigenvalues are sorted in descending order
    generated_eigenvalues = np.sort(generated_eigenvalues)[::-1]
    true_eigenvalues = np.sort(true_eigenvalues)[::-1]
    
    # 1. Top-k eigenvalue ratio: sum(λ_1:k) / sum(λ)
    gen_ratio = np.sum(generated_eigenvalues[:k]) / np.sum(generated_eigenvalues)
    true_ratio = np.sum(true_eigenvalues[:k]) / np.sum(true_eigenvalues)
    
    metrics['top_k_eigenvalue_ratio_gen'] = float(gen_ratio)
    metrics['top_k_eigenvalue_ratio_true'] = float(true_ratio)
    metrics['top_k_eigenvalue_ratio_error'] = float(np.abs(gen_ratio - true_ratio))
    
    # 2. Individual eigenvalue ratios: λ_i / λ_{i+1} for i = 1 to min(k+5, n-1)
    max_idx = min(k + 5, len(generated_eigenvalues) - 1, len(true_eigenvalues) - 1)
    
    for i in range(max_idx):
        gen_ratio_i = generated_eigenvalues[i] / max(generated_eigenvalues[i+1], 1e-10)
        true_ratio_i = true_eigenvalues[i] / max(true_eigenvalues[i+1], 1e-10)
        
        metrics[f'eigenvalue_ratio_{i+1}to{i+2}_gen'] = float(gen_ratio_i)
        metrics[f'eigenvalue_ratio_{i+1}to{i+2}_true'] = float(true_ratio_i)
        metrics[f'eigenvalue_ratio_{i+1}to{i+2}_error'] = float(np.abs(gen_ratio_i - true_ratio_i))
    
    # 3. Largest gap in eigenvalue ratios (to find the "elbow")
    gen_ratios = generated_eigenvalues[:-1] / np.maximum(generated_eigenvalues[1:], 1e-10)
    true_ratios = true_eigenvalues[:-1] / np.maximum(true_eigenvalues[1:], 1e-10)
    
    gen_gap_idx = np.argmax(gen_ratios)
    true_gap_idx = np.argmax(true_ratios)
    
    metrics['largest_eigenvalue_gap_idx_gen'] = int(gen_gap_idx)
    metrics['largest_eigenvalue_gap_idx_true'] = int(true_gap_idx)
    metrics['eigenvalue_gap_index_difference'] = int(abs(gen_gap_idx - true_gap_idx))
    
    return metrics


def covariance_estimation_metrics(
    estimated_cov: Union[torch.Tensor, np.ndarray],
    true_cov: Union[torch.Tensor, np.ndarray]
) -> Dict[str, float]:
    """
    Compute metrics for evaluating covariance matrix estimation.
    
    Args:
        estimated_cov: Estimated covariance matrix
        true_cov: True covariance matrix
        
    Returns:
        Dictionary of covariance estimation metrics
    """
    # Ensure tensors are on CPU and convert to numpy
    if isinstance(estimated_cov, torch.Tensor):
        estimated_cov = estimated_cov.cpu().numpy()
    if isinstance(true_cov, torch.Tensor):
        true_cov = true_cov.cpu().numpy()
    
    # Initialize results dictionary
    metrics = {}
    
    # 1. Frobenius norm of the difference
    frob_norm = np.linalg.norm(estimated_cov - true_cov, 'fro')
    metrics['cov_frobenius_error'] = float(frob_norm)
    
    # 2. Normalized Frobenius norm
    true_frob = np.linalg.norm(true_cov, 'fro')
    metrics['cov_frobenius_error_normalized'] = float(frob_norm / true_frob)
    
    # 3. Operator norm (spectral norm) of the difference
    try:
        op_norm = np.linalg.norm(estimated_cov - true_cov, 2)
        metrics['cov_operator_error'] = float(op_norm)
        
        # 4. Normalized operator norm
        true_op = np.linalg.norm(true_cov, 2)
        metrics['cov_operator_error_normalized'] = float(op_norm / true_op)
    except np.linalg.LinAlgError:
        metrics['cov_operator_error'] = float('nan')
        metrics['cov_operator_error_normalized'] = float('nan')
    
    # 5. Log-determinant divergence
    try:
        # Ensure matrices are positive definite
        eps = 1e-8 * np.eye(estimated_cov.shape[0])
        est_cov_pd = estimated_cov + eps
        true_cov_pd = true_cov + eps
        
        # Compute log-determinant
        log_det_est = np.log(np.linalg.det(est_cov_pd))
        log_det_true = np.log(np.linalg.det(true_cov_pd))
        
        metrics['log_determinant_error'] = float(np.abs(log_det_est - log_det_true))
    except np.linalg.LinAlgError:
        metrics['log_determinant_error'] = float('nan')
    
    # 6. Condition number comparison
    try:
        cond_est = np.linalg.cond(estimated_cov)
        cond_true = np.linalg.cond(true_cov)
        
        metrics['condition_number_est'] = float(cond_est)
        metrics['condition_number_true'] = float(cond_true)
        metrics['condition_number_ratio'] = float(cond_est / cond_true)
    except np.linalg.LinAlgError:
        metrics['condition_number_est'] = float('nan')
        metrics['condition_number_true'] = float('nan')
        metrics['condition_number_ratio'] = float('nan')
    
    # 7. Correlation matrix differences (to isolate correlation structure from variances)
    def cov_to_corr(cov):
        std = np.sqrt(np.diag(cov))
        corr = cov / np.outer(std, std)
        return corr
    
    corr_est = cov_to_corr(estimated_cov)
    corr_true = cov_to_corr(true_cov)
    
    corr_frob_error = np.linalg.norm(corr_est - corr_true, 'fro')
    metrics['correlation_frobenius_error'] = float(corr_frob_error)
    metrics['correlation_frobenius_error_normalized'] = float(
        corr_frob_error / np.linalg.norm(corr_true, 'fro')
    )
    
    return metrics

def total_variation_distance(
    generated_samples: Union[torch.Tensor, np.ndarray],
    true_samples: Union[torch.Tensor, np.ndarray],
    bins: int = 50,
    max_dims: int = 100
) -> Dict[str, float]:
    """
    Approximate total variation distance between distributions.
    
    Args:
        generated_samples: Generated samples
        true_samples: True samples
        bins: Number of bins for histogram approximation
        max_dims: Maximum number of dimensions to evaluate
        
    Returns:
        Dictionary with total variation distance metrics
    """
    # Ensure tensors are on CPU and convert to numpy
    if isinstance(generated_samples, torch.Tensor):
        generated_samples = generated_samples.cpu().numpy()
    if isinstance(true_samples, torch.Tensor):
        true_samples = true_samples.cpu().numpy()
    
    # Number of dimensions to evaluate
    d = min(generated_samples.shape[1], true_samples.shape[1], max_dims)
    
    # Compute total variation distance for individual dimensions
    tv_distances = []
    for dim_idx in range(d):
        # Get samples for this dimension
        gen_dim = generated_samples[:, dim_idx]
        true_dim = true_samples[:, dim_idx]
        
        # Create histograms with same bins
        hist_range = (min(gen_dim.min(), true_dim.min()), max(gen_dim.max(), true_dim.max()))
        hist_gen, bin_edges = np.histogram(gen_dim, bins=bins, density=True, range=hist_range)
        hist_true, _ = np.histogram(true_dim, bins=bin_edges, density=True)
        
        # Compute TV distance: 0.5 * ∫|p-q|dx
        bin_width = bin_edges[1] - bin_edges[0]
        tv_dim = 0.5 * np.sum(np.abs(hist_gen - hist_true)) * bin_width
        tv_distances.append(tv_dim)
    
    # Compute metrics
    metrics = {
        'tv_distance_mean': float(np.mean(tv_distances)),
        'tv_distance_max': float(np.max(tv_distances)),
        'tv_distance_median': float(np.median(tv_distances))
    }
    
    # Also compute joint TV distance approximation for pairs of dimensions
    if d >= 2:
        # Sample a subset of dimension pairs
        num_pairs = min(20, d * (d - 1) // 2)
        pairs = []
        np.random.seed(42)  # For reproducibility
        
        for _ in range(num_pairs):
            i, j = np.random.choice(d, size=2, replace=False)
            pairs.append((i, j))
        
        # Compute 2D TV distances
        tv_2d_distances = []
        for i, j in pairs:
            # Get samples for this pair of dimensions
            gen_i, gen_j = generated_samples[:, i], generated_samples[:, j]
            true_i, true_j = true_samples[:, i], true_samples[:, j]
            
            # Create 2D histograms
            range_i = (min(gen_i.min(), true_i.min()), max(gen_i.max(), true_i.max()))
            range_j = (min(gen_j.min(), true_j.min()), max(gen_j.max(), true_j.max()))
            
            hist_gen, x_edges, y_edges = np.histogram2d(
                gen_i, gen_j, bins=bins, density=True, range=[range_i, range_j]
            )
            hist_true, _, _ = np.histogram2d(
                true_i, true_j, bins=[x_edges, y_edges], density=True
            )
            
            # Compute TV distance for 2D histogram
            bin_area = (x_edges[1] - x_edges[0]) * (y_edges[1] - y_edges[0])
            tv_2d = 0.5 * np.sum(np.abs(hist_gen - hist_true)) * bin_area
            tv_2d_distances.append(tv_2d)
        
        metrics['tv_distance_2d_mean'] = float(np.mean(tv_2d_distances))
    
    return metrics

def compute_all_metrics(
    generated_data: Dict[str, Any],
    true_data: Dict[str, Any],
    k: Optional[int] = None,
    skip_distribution_metrics: bool = False
) -> Dict[str, Dict[str, float]]:
    """
    Compute all evaluation metrics for generated data.
    
    Args:
        generated_data: Dictionary with generated data
        true_data: Dictionary with true data
        k: Number of factors to consider
        skip_distribution_metrics: Whether to skip distribution-based metrics
        
    Returns:
        Dictionary of all metrics
    """
    all_metrics = {}
    
    # Debug available keys
    print(f"Generated data keys: {list(generated_data.keys())}")
    print(f"True data keys: {list(true_data.keys())}")
    # (debug key dump removed — its loop variable shadowed the `k` parameter, turning it
    #  into a dict-key STRING and silently breaking the subspace/eigenvalue metrics below)
    
    # 1. Distribution metrics (only if reference samples are available)
    reference_samples = true_data.get('samples', true_data.get('data', None))
    
    if not skip_distribution_metrics and reference_samples is not None and 'samples' in generated_data:
        try:
            distribution_metrics_result = distribution_metrics(
                generated_samples=generated_data['samples'],
                reference_samples=reference_samples
            )
            all_metrics['distribution'] = distribution_metrics_result

            # Add total variation distance metrics
            tv_metrics = total_variation_distance(
                generated_samples=generated_data['samples'],
                true_samples=reference_samples
            )
            all_metrics['total_variation'] = tv_metrics
        except Exception as e:
            print(f"Warning: Could not compute distribution metrics: {e}")
    else:
        print("Skipping distribution metrics (reference samples not available)")
    
    # 3. Subspace recovery metrics
    if 'eigenvectors' in generated_data and 'true_factor_subspace' in true_data:
        try:
            # Determine k if not provided
            k_effective = k if k is not None else true_data['true_factor_subspace'].shape[1]
            
            subspace_metrics_result = subspace_recovery_metrics(
                recovered_subspace=generated_data['eigenvectors'][:, :k_effective],
                true_subspace=true_data['true_factor_subspace'],
                k=k_effective
            )
            all_metrics['subspace'] = subspace_metrics_result
        except Exception as e:
            print(f"Warning: Could not compute subspace metrics: {e}")
    
    # 4. Eigenvalue ratio metrics
    if 'eigenvalues' in generated_data and 'true_eigenvalues' in true_data:
        try:
            eigenvalue_metrics_result = eigenvalue_ratio_metrics(
                generated_eigenvalues=generated_data['eigenvalues'],
                true_eigenvalues=true_data['true_eigenvalues'],
                k=k
            )
            all_metrics['eigenvalues'] = eigenvalue_metrics_result
        except Exception as e:
            print(f"Warning: Could not compute eigenvalue metrics: {e}")
    
    # 5. Covariance estimation metrics
    if 'cov' in generated_data and 'true_cov' in true_data:
        try:
            covariance_metrics_result = covariance_estimation_metrics(
                estimated_cov=generated_data['cov'],
                true_cov=true_data['true_cov']
            )
            all_metrics['covariance'] = covariance_metrics_result
        except Exception as e:
            print(f"Warning: Could not compute covariance metrics: {e}")
    
    return all_metrics

if __name__ == "__main__":
    # Test the metrics with synthetic data
    import torch
    
    # Create synthetic data with known factor structure
    d = 100  # Dimension
    k = 5    # Number of factors
    n = 1000  # Number of samples
    
    # Create true factor loadings
    true_loadings = torch.randn(d, k)
    true_loadings, _ = torch.linalg.qr(true_loadings)
    
    # Create true factor covariance
    true_factor_cov = torch.diag(torch.linspace(10, 1, k))
    
    # Create true noise covariance
    true_noise_var = torch.linspace(0.5, 0.1, d)
    true_noise_cov = torch.diag(true_noise_var)
    
    # Create true covariance
    true_cov = true_loadings @ true_factor_cov @ true_loadings.T + true_noise_cov
    
    # Generate true data from the covariance
    true_data = torch.randn(n, d) @ torch.linalg.cholesky(true_cov).T
    
    # Generate some data with a slightly perturbed covariance
    perturbation = 0.2 * torch.randn(d, d)
    perturbation = perturbation @ perturbation.T  # Make it symmetric
    perturbed_cov = true_cov + 0.1 * perturbation
    generated_data = torch.randn(n, d) @ torch.linalg.cholesky(perturbed_cov).T
    
    # Compute sample statistics
    true_mean = true_data.mean(dim=0)
    true_sample_cov = (true_data - true_mean).T @ (true_data - true_mean) / (n - 1)
    
    generated_mean = generated_data.mean(dim=0)
    generated_sample_cov = (generated_data - generated_mean).T @ (generated_data - generated_mean) / (n - 1)
    
    # Compute eigendecomposition
    true_eigenvalues, true_eigenvectors = torch.linalg.eigh(true_sample_cov)
    # Sort in descending order
    true_sort_idx = torch.argsort(true_eigenvalues, descending=True)
    true_eigenvalues = true_eigenvalues[true_sort_idx]
    true_eigenvectors = true_eigenvectors[:, true_sort_idx]
    
    generated_eigenvalues, generated_eigenvectors = torch.linalg.eigh(generated_sample_cov)
    # Sort in descending order
    generated_sort_idx = torch.argsort(generated_eigenvalues, descending=True)
    generated_eigenvalues = generated_eigenvalues[generated_sort_idx]
    generated_eigenvectors = generated_eigenvectors[:, generated_sort_idx]
    
    # Prepare data dictionaries
    true_data_dict = {
        'samples': true_data,
        'true_eigenvalues': true_eigenvalues,
        'true_factor_subspace': true_loadings,
        'true_cov': true_cov
    }
    
    generated_data_dict = {
        'samples': generated_data,
        'eigenvectors': generated_eigenvectors,
        'eigenvalues': generated_eigenvalues,
        'cov': generated_sample_cov
    }
    
    # Compute all metrics
    metrics = compute_all_metrics(generated_data_dict, true_data_dict, k=k)
    
    # Print metrics
    for category, category_metrics in metrics.items():
        print(f"\n{category.upper()} METRICS:")
        for name, value in category_metrics.items():
            print(f"  {name}: {value}")
