"""
Factor subspace recovery evaluation for the Diffusion Factor Model.

This module implements methods for evaluating the factor subspace recovery,
including comparison of eigendecompositions and factor loadings.
"""

import torch
import numpy as np
from typing import Dict, Tuple, List, Optional, Union, Any
import logging
from sklearn.decomposition import PCA
from scipy import stats
import matplotlib.pyplot as plt

from metrics import subspace_recovery_metrics, eigenvalue_ratio_metrics

# Set up logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class FactorRecoveryEvaluator:
    """Evaluator for factor recovery from generated samples."""
    
    def __init__(
        self,
        true_data: Dict[str, Any],
        factor_dim: int,
        logger: Optional[logging.Logger] = None
    ):
        """
        Initialize the factor recovery evaluator.
        
        Args:
            true_data: Dictionary with true data and factor information
            factor_dim: Number of factors (k)
            logger: Logger
        """
        self.factor_dim = factor_dim
        self.true_data = true_data
        self.logger = logger or logging.getLogger(__name__)
        
        # Extract true factor information
        self.true_cov = true_data.get('true_cov', None)
        self.true_factor_loadings = true_data.get('true_factor_loadings', None)
        self.true_factor_subspace = true_data.get('true_factor_subspace', None)
        self.true_eigenvalues = true_data.get('true_eigenvalues', None)
        
        # Calculate true factor subspace if not provided
        if self.true_factor_subspace is None and self.true_factor_loadings is not None:
            # Extract factor subspace from loadings (orthogonalize if needed)
            Q, R = np.linalg.qr(self.true_factor_loadings)
            self.true_factor_subspace = Q[:, :self.factor_dim]
        
        # Calculate true eigendecomposition if not provided
        if self.true_eigenvalues is None and self.true_cov is not None:
            # Compute eigendecomposition of true covariance
            eigenvalues, eigenvectors = np.linalg.eigh(self.true_cov)
            # Sort in descending order
            idx = np.argsort(eigenvalues)[::-1]
            self.true_eigenvalues = eigenvalues[idx]
            self.true_eigenvectors = eigenvectors[:, idx]

    def compute_optimal_t0(num_samples: int, factor_dim: int) -> float:
        """
        Compute optimal early stopping time t0 as per Theorem 3.
        
        Args:
            num_samples: Number of samples
            factor_dim: Number of factors (k)
            
        Returns:
            Optimal early stopping time t0
        """
        # Calculate δ(n) as defined in Theorem 3
        delta_n = (factor_dim + 10) * np.log(np.log(num_samples)) / (2 * np.log(num_samples))
        
        # Compute t0 = n^(-(1-δ(n))/(k+5))
        t0 = num_samples ** (-(1 - delta_n) / (factor_dim + 5))
        
        # Apply reasonable bounds for numerical stability
        return max(0.01, min(0.3, t0))


    def compute_optimal_T(num_samples: int, factor_dim: int) -> float:
        """
        Compute optimal terminal time T as per Theorem 3.
        
        Args:
            num_samples: Number of samples
            factor_dim: Number of factors (k)
            
        Returns:
            Optimal terminal time T
        """
        # Calculate δ(n) as defined in Theorem 3
        delta_n = (factor_dim + 10) * np.log(np.log(num_samples)) / (2 * np.log(num_samples))
        
        # Lipschitz parameter approximation (from paper's Theorem 1)
        gamma_1 = 20 * factor_dim
        
        # T = ((4·γ₁+2)·(1-δ(n)))/(k+5)·log(n)
        T = ((4 * gamma_1 + 2) * (1 - delta_n)) / (factor_dim + 5) * np.log(num_samples)
        
        # Apply reasonable minimum
        return max(1.0, T)
    
    def evaluate_pca_recovery(
        self,
        generated_samples: Union[torch.Tensor, np.ndarray],
        num_factors: Optional[int] = None,
        comparison_methods: Optional[List[str]] = None
    ) -> Dict[str, Dict[str, float]]:
        """
        Evaluate recovery of factor structure using PCA.
        
        Args:
            generated_samples: Generated samples
            num_factors: Number of factors to recover (default: self.factor_dim)
            comparison_methods: List of methods to compare with
            
        Returns:
            Dictionary of evaluation metrics
        """
        # Set default number of factors
        if num_factors is None:
            num_factors = self.factor_dim
        
        # Set default comparison methods
        if comparison_methods is None:
            comparison_methods = ['PCA']
        
        # Convert to numpy if torch tensor
        if isinstance(generated_samples, torch.Tensor):
            generated_samples = generated_samples.cpu().numpy()
        
        # Initialize results
        results = {}
        
        # Center the data
        centered_samples = generated_samples - np.mean(generated_samples, axis=0)
        
        # Compute sample covariance
        sample_cov = (centered_samples.T @ centered_samples) / (len(centered_samples) - 1)
        
        # PCA-based factor recovery
        if 'PCA' in comparison_methods:
            # Apply PCA
            pca = PCA(n_components=num_factors)
            pca.fit(generated_samples)
            
            # Get recovered factor loadings (eigenvectors)
            recovered_loadings = pca.components_.T
            
            # Get explained variance ratio
            explained_variance_ratio = pca.explained_variance_ratio_
            
            # Compute metrics compared to true factor subspace
            if self.true_factor_subspace is not None:
                subspace_metrics = subspace_recovery_metrics(
                    recovered_subspace=recovered_loadings,
                    true_subspace=self.true_factor_subspace,
                    k=num_factors
                )
                results['PCA_subspace_metrics'] = subspace_metrics
            
            # Compute eigenvalue metrics
            if self.true_eigenvalues is not None:
                eigenvalue_metrics = eigenvalue_ratio_metrics(
                    generated_eigenvalues=pca.explained_variance_,
                    true_eigenvalues=self.true_eigenvalues,
                    k=num_factors
                )
                results['PCA_eigenvalue_metrics'] = eigenvalue_metrics
            
            # Store additional information
            results['PCA_explained_variance_ratio'] = explained_variance_ratio
            results['PCA_recovered_loadings'] = recovered_loadings
        
        # Implement other factor recovery methods
        # For example, RPCA, POET, etc. mentioned in the paper
        if 'RPCA' in comparison_methods:
            # Risk Premium PCA as in Lettau and Pelger (2020b)
            mean_returns = np.mean(generated_samples, axis=0)
            gamma_rpca = 1.0  # Risk premium adjustment parameter
            
            # Adjust covariance matrix with risk premium term
            adjusted_cov = sample_cov + gamma_rpca * np.outer(mean_returns, mean_returns)
            
            # Perform eigendecomposition
            eigenvalues, eigenvectors = np.linalg.eigh(adjusted_cov)
            idx = np.argsort(eigenvalues)[::-1]
            eigenvalues = eigenvalues[idx]
            eigenvectors = eigenvectors[:, idx]
            
            # Extract top k loadings
            rpca_loadings = eigenvectors[:, :num_factors]
            
            # Compute metrics
            if self.true_factor_subspace is not None:
                subspace_metrics = subspace_recovery_metrics(
                    recovered_subspace=rpca_loadings,
                    true_subspace=self.true_factor_subspace,
                    k=num_factors
                )
                results['RPCA_subspace_metrics'] = subspace_metrics
            
            # Store results
            results['RPCA_recovered_loadings'] = rpca_loadings
        
        if 'POET' in comparison_methods:
            # Principal Orthogonal complEment Thresholding (POET)
            # As in Fan, Liao, and Mincheva (2013)
            
            # Perform eigendecomposition of sample covariance
            eigenvalues, eigenvectors = np.linalg.eigh(sample_cov)
            idx = np.argsort(eigenvalues)[::-1]
            eigenvalues = eigenvalues[idx]
            eigenvectors = eigenvectors[:, idx]
            
            # Compute low-rank component
            low_rank = eigenvectors[:, :num_factors] @ np.diag(eigenvalues[:num_factors]) @ eigenvectors[:, :num_factors].T
            
            # Compute residual
            residual = sample_cov - low_rank
            
            # Apply thresholding to residual
            threshold = 0.5 * np.sqrt(np.log(generated_samples.shape[1]) / generated_samples.shape[0])
            residual_thresholded = np.where(np.abs(residual) > threshold, residual, 0)
            
            # Keep diagonal of residual unchanged
            np.fill_diagonal(residual_thresholded, np.diag(residual))
            
            # Compute POET covariance estimator
            poet_cov = low_rank + residual_thresholded
            
            # Perform eigendecomposition of POET estimator
            poet_eigenvalues, poet_eigenvectors = np.linalg.eigh(poet_cov)
            idx = np.argsort(poet_eigenvalues)[::-1]
            poet_eigenvalues = poet_eigenvalues[idx]
            poet_eigenvectors = poet_eigenvectors[:, idx]
            
            # Extract top k loadings
            poet_loadings = poet_eigenvectors[:, :num_factors]
            
            # Compute metrics
            if self.true_factor_subspace is not None:
                subspace_metrics = subspace_recovery_metrics(
                    recovered_subspace=poet_loadings,
                    true_subspace=self.true_factor_subspace,
                    k=num_factors
                )
                results['POET_subspace_metrics'] = subspace_metrics
            
            # Store results
            results['POET_recovered_loadings'] = poet_loadings
        
        return results
    
    def compute_svd_recovery(
        self,
        generated_samples: Union[torch.Tensor, np.ndarray],
        num_factors: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Compute SVD-based recovery of factor structure.
        
        Args:
            generated_samples: Generated samples
            num_factors: Number of factors to recover (default: self.factor_dim)
            
        Returns:
            Dictionary with recovered factor information
        """
        # Set default number of factors
        if num_factors is None:
            num_factors = self.factor_dim
        
        # Convert to numpy if torch tensor
        if isinstance(generated_samples, torch.Tensor):
            generated_samples = generated_samples.cpu().numpy()
        
        # Center the data
        centered_samples = generated_samples - np.mean(generated_samples, axis=0)
        
        # Compute SVD
        U, S, Vt = np.linalg.svd(centered_samples, full_matrices=False)
        
        # Compute factor loadings (scaled eigenvectors)
        loadings = (S[:num_factors] / np.sqrt(len(centered_samples) - 1)) * U[:, :num_factors]
        
        # Compute explained variance
        total_var = np.sum(S**2) / (len(centered_samples) - 1)
        explained_var = (S[:num_factors]**2) / (len(centered_samples) - 1)
        explained_var_ratio = explained_var / total_var
        
        # Compute eigenvalues and eigenvectors of sample covariance
        def compute_large_covariance(
            data: Union[torch.Tensor, np.ndarray],
            batch_size: int = 1000
        ) -> np.ndarray:
            """
            Compute covariance matrix in batches for memory efficiency.
            
            Args:
                data: Input data of shape (n_samples, n_features)
                batch_size: Batch size for memory efficiency
                
            Returns:
                Covariance matrix
            """
            # Convert to numpy if needed
            if isinstance(data, torch.Tensor):
                data = data.cpu().numpy()
            
            # Get dimensions
            n, d = data.shape
            
            # Compute mean
            mean = np.mean(data, axis=0)
            
            # Initialize covariance matrix
            cov = np.zeros((d, d))
            
            # Process in batches
            for i in range(0, n, batch_size):
                # Get batch and center it
                batch = data[i:min(i+batch_size, n)] - mean
                
                # Update covariance matrix
                cov += batch.T @ batch
            
            # Normalize
            cov /= (n - 1)
            
            return cov
    
        if centered_samples.shape[1] > 1000 or centered_samples.shape[0] > 10000:
            sample_cov = compute_large_covariance(centered_samples, batch_size=1000)
        else:
            sample_cov = (centered_samples.T @ centered_samples) / (len(centered_samples) - 1)
        
        eigenvalues, eigenvectors = np.linalg.eigh(sample_cov)
        # Sort in descending order
        idx = np.argsort(eigenvalues)[::-1]
        eigenvalues = eigenvalues[idx]
        eigenvectors = eigenvectors[:, idx]
        
        return {
            'loadings': loadings,
            'eigenvalues': eigenvalues,
            'eigenvectors': eigenvectors[:, :num_factors],
            'singular_values': S[:num_factors],
            'explained_variance': explained_var,
            'explained_variance_ratio': explained_var_ratio,
            'sample_covariance': sample_cov
        }
    
    def compare_with_true_factor_structure(
        self,
        generated_results: Dict[str, Any]
    ) -> Dict[str, Dict[str, float]]:
        """
        Compare generated factor structure with true factor structure.
        
        Args:
            generated_results: Dictionary with generated factor information
            
        Returns:
            Dictionary of comparison metrics
        """
        results = {}
        
        # Extract generated factor information
        loadings = generated_results.get('loadings', None)
        eigenvectors = generated_results.get('eigenvectors', None)
        eigenvalues = generated_results.get('eigenvalues', None)
        sample_cov = generated_results.get('sample_covariance', None)
        
        # Compare subspaces
        if self.true_factor_subspace is not None and eigenvectors is not None:
            subspace_metrics = subspace_recovery_metrics(
                recovered_subspace=eigenvectors,
                true_subspace=self.true_factor_subspace,
                k=self.factor_dim
            )
            results['subspace_metrics'] = subspace_metrics
        
        # Compare eigenvalues
        if self.true_eigenvalues is not None and eigenvalues is not None:
            eigenvalue_metrics = eigenvalue_ratio_metrics(
                generated_eigenvalues=eigenvalues,
                true_eigenvalues=self.true_eigenvalues,
                k=self.factor_dim
            )
            results['eigenvalue_metrics'] = eigenvalue_metrics
        
        # Compare covariance matrices
        if self.true_cov is not None and sample_cov is not None:
            # Ensure true_cov is a numpy array for consistent comparison
            true_cov_np = self.true_cov.cpu().numpy() if isinstance(self.true_cov, torch.Tensor) else self.true_cov
            
            # Ensure sample_cov is a numpy array
            sample_cov_np = sample_cov.cpu().numpy() if isinstance(sample_cov, torch.Tensor) else sample_cov
            
            # Compute Frobenius norm of difference
            frob_diff = np.linalg.norm(sample_cov_np - true_cov_np, 'fro')
            frob_norm_true = np.linalg.norm(true_cov_np, 'fro')
            rel_frob_diff = frob_diff / frob_norm_true
            
            # Compute operator norm of difference
            op_diff = np.linalg.norm(sample_cov_np - true_cov_np, 2)
            op_norm_true = np.linalg.norm(true_cov_np, 2)
            rel_op_diff = op_diff / op_norm_true

            # Store covariance metrics
            results['covariance_metrics'] = {
                'frobenius_diff': float(frob_diff),
                'relative_frobenius_diff': float(rel_frob_diff),
                'operator_diff': float(op_diff),
                'relative_operator_diff': float(rel_op_diff)
            }
        
        return results
    
    def plot_eigenvalue_comparison(
        self,
        generated_eigenvalues: Union[torch.Tensor, np.ndarray],
        true_eigenvalues: Optional[Union[torch.Tensor, np.ndarray]] = None,
        num_eigenvalues: int = 20,
        title: str = 'Eigenvalue Comparison',
        figsize: Tuple[int, int] = (10, 6),
        save_path: Optional[str] = None
    ) -> plt.Figure:
        """
        Plot comparison of eigenvalues between generated and true data.
        
        Args:
            generated_eigenvalues: Eigenvalues of generated data
            true_eigenvalues: Eigenvalues of true data (if None, use self.true_eigenvalues)
            num_eigenvalues: Number of eigenvalues to plot
            title: Plot title
            figsize: Figure size
            save_path: Path to save the figure
            
        Returns:
            Matplotlib figure
        """
        # Convert to numpy if needed
        if isinstance(generated_eigenvalues, torch.Tensor):
            generated_eigenvalues = generated_eigenvalues.cpu().numpy()
        
        if true_eigenvalues is None:
            true_eigenvalues = self.true_eigenvalues
        elif isinstance(true_eigenvalues, torch.Tensor):
            true_eigenvalues = true_eigenvalues.cpu().numpy()
        
        # Sort in descending order if needed
        if true_eigenvalues is not None:
            true_eigenvalues = np.sort(true_eigenvalues)[::-1]
        
        generated_eigenvalues = np.sort(generated_eigenvalues)[::-1]
        
        # Create figure
        fig, ax = plt.subplots(figsize=figsize)
        
        # Plot eigenvalues
        x = np.arange(1, min(num_eigenvalues + 1, len(generated_eigenvalues) + 1))
        
        ax.plot(x, generated_eigenvalues[:num_eigenvalues], 'o-', label='Generated')
        
        if true_eigenvalues is not None:
            ax.plot(x, true_eigenvalues[:num_eigenvalues], 's--', label='True')
        
        # Add vertical line at factor_dim
        ax.axvline(x=self.factor_dim, color='red', linestyle='--', alpha=0.7, label=f'k={self.factor_dim}')
        
        # Add labels and title
        ax.set_xlabel('Eigenvalue Index')
        ax.set_ylabel('Eigenvalue')
        ax.set_title(title)
        ax.legend()
        ax.grid(True, alpha=0.3)
        
        # Set y-scale to log if there's large variance
        if (generated_eigenvalues[0] / generated_eigenvalues[min(num_eigenvalues, len(generated_eigenvalues))-1]) > 100:
            ax.set_yscale('log')
        
        # Save figure if path provided
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
        
        return fig
    
    def plot_cumulative_variance(
        self,
        generated_eigenvalues: Union[torch.Tensor, np.ndarray],
        true_eigenvalues: Optional[Union[torch.Tensor, np.ndarray]] = None,
        num_eigenvalues: int = 20,
        title: str = 'Cumulative Explained Variance',
        figsize: Tuple[int, int] = (10, 6),
        save_path: Optional[str] = None
    ) -> plt.Figure:
        """
        Plot cumulative explained variance.
        
        Args:
            generated_eigenvalues: Eigenvalues of generated data
            true_eigenvalues: Eigenvalues of true data (if None, use self.true_eigenvalues)
            num_eigenvalues: Number of eigenvalues to include
            title: Plot title
            figsize: Figure size
            save_path: Path to save the figure
            
        Returns:
            Matplotlib figure
        """
        # Convert to numpy if needed
        if isinstance(generated_eigenvalues, torch.Tensor):
            generated_eigenvalues = generated_eigenvalues.cpu().numpy()
        
        if true_eigenvalues is None:
            true_eigenvalues = self.true_eigenvalues
        elif isinstance(true_eigenvalues, torch.Tensor):
            true_eigenvalues = true_eigenvalues.cpu().numpy()
        
        # Sort in descending order if needed
        if true_eigenvalues is not None:
            true_eigenvalues = np.sort(true_eigenvalues)[::-1]
        
        generated_eigenvalues = np.sort(generated_eigenvalues)[::-1]
        
        # Calculate cumulative explained variance
        cum_gen_var = np.cumsum(generated_eigenvalues[:num_eigenvalues]) / np.sum(generated_eigenvalues)
        
        if true_eigenvalues is not None:
            cum_true_var = np.cumsum(true_eigenvalues[:num_eigenvalues]) / np.sum(true_eigenvalues)
        
        # Create figure
        fig, ax = plt.subplots(figsize=figsize)
        
        # Plot cumulative variance
        x = np.arange(1, min(num_eigenvalues + 1, len(generated_eigenvalues) + 1))
        
        ax.plot(x, cum_gen_var[:num_eigenvalues], 'o-', label='Generated')
        
        if true_eigenvalues is not None:
            ax.plot(x, cum_true_var[:num_eigenvalues], 's--', label='True')
        
        # Add vertical line at factor_dim
        ax.axvline(x=self.factor_dim, color='red', linestyle='--', alpha=0.7, label=f'k={self.factor_dim}')
        
        # Add horizontal lines at key thresholds
        for threshold in [0.5, 0.8, 0.9, 0.95]:
            ax.axhline(y=threshold, color='gray', linestyle=':', alpha=0.5)
            ax.text(x[-1] + 0.5, threshold, f'{threshold:.0%}', va='center')
        
        # Add labels and title
        ax.set_xlabel('Number of Components')
        ax.set_ylabel('Cumulative Explained Variance')
        ax.set_title(title)
        ax.legend()
        ax.grid(True, alpha=0.3)
        
        # Set axis limits
        ax.set_xlim(0.5, num_eigenvalues + 0.5)
        ax.set_ylim(0, 1.05)
        
        # Save figure if path provided
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
        
        return fig
    
    def plot_factor_loadings_heatmap(
        self,
        generated_loadings: Union[torch.Tensor, np.ndarray],
        true_loadings: Optional[Union[torch.Tensor, np.ndarray]] = None,
        num_assets: int = 20,
        title: str = 'Factor Loadings Heatmap',
        figsize: Tuple[int, int] = (12, 10),
        save_path: Optional[str] = None
    ) -> plt.Figure:
        """
        Plot heatmap of factor loadings.
        
        Args:
            generated_loadings: Generated factor loadings
            true_loadings: True factor loadings (if None, use self.true_factor_loadings)
            num_assets: Number of assets to include
            title: Plot title
            figsize: Figure size
            save_path: Path to save the figure
            
        Returns:
            Matplotlib figure
        """
        import seaborn as sns
        
        # Convert to numpy if needed
        if isinstance(generated_loadings, torch.Tensor):
            generated_loadings = generated_loadings.cpu().numpy()
        
        if true_loadings is None:
            true_loadings = self.true_factor_loadings
        elif isinstance(true_loadings, torch.Tensor):
            true_loadings = true_loadings.cpu().numpy()
        
        # Create figure
        fig, axes = plt.subplots(1, 2, figsize=figsize, sharey=true_loadings is not None)
        
        # Determine vmin and vmax for colormap
        vmin = -max(abs(generated_loadings[:num_assets, :].min()), abs(generated_loadings[:num_assets, :].max()))
        vmax = -vmin
        
        # Plot generated loadings
        sns.heatmap(
            generated_loadings[:num_assets, :],
            ax=axes[0],
            cmap='coolwarm',
            vmin=vmin,
            vmax=vmax,
            center=0,
            annot=False,
            cbar_kws={'label': 'Loading Value'}
        )
        axes[0].set_title('Generated Loadings')
        axes[0].set_xlabel('Factor')
        axes[0].set_ylabel('Asset')
        
        # Plot true loadings if available
        if true_loadings is not None:
            true_vmin = -max(abs(true_loadings[:num_assets, :].min()), abs(true_loadings[:num_assets, :].max()))
            true_vmax = -true_vmin
            
            sns.heatmap(
                true_loadings[:num_assets, :],
                ax=axes[1],
                cmap='coolwarm',
                vmin=true_vmin,
                vmax=true_vmax,
                center=0,
                annot=False,
                cbar_kws={'label': 'Loading Value'}
            )
            axes[1].set_title('True Loadings')
            axes[1].set_xlabel('Factor')
        else:
            axes[1].axis('off')
        
        # Add title
        fig.suptitle(title, fontsize=16)
        plt.tight_layout(rect=[0, 0, 1, 0.96])
        
        # Save figure if path provided
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
        
        return fig
    
    def compare_empirical_with_diffusion(
        self,
        empirical_samples: Union[torch.Tensor, np.ndarray],
        diffusion_samples: Union[torch.Tensor, np.ndarray],
        num_factors: Optional[int] = None
    ) -> Dict[str, Dict[str, Union[float, np.ndarray]]]:
        """
        Compare empirical and diffusion-generated samples in terms of factor structure.
        
        Args:
            empirical_samples: Empirical samples
            diffusion_samples: Diffusion-generated samples
            num_factors: Number of factors to consider
            
        Returns:
            Dictionary of comparison metrics
        """
        # Set default number of factors
        if num_factors is None:
            num_factors = self.factor_dim
        
        # Convert to numpy if needed
        if isinstance(empirical_samples, torch.Tensor):
            empirical_samples = empirical_samples.cpu().numpy()
        if isinstance(diffusion_samples, torch.Tensor):
            diffusion_samples = diffusion_samples.cpu().numpy()
        
        # Compute eigendecomposition for both datasets
        emp_results = self.compute_svd_recovery(empirical_samples, num_factors)
        diff_results = self.compute_svd_recovery(diffusion_samples, num_factors)
        
        # Compare with true factor structure
        emp_vs_true = self.compare_with_true_factor_structure(emp_results)
        diff_vs_true = self.compare_with_true_factor_structure(diff_results)
        
        # Compute direct comparison between empirical and diffusion
        emp_vs_diff = {
            'subspace_metrics': subspace_recovery_metrics(
                recovered_subspace=diff_results['eigenvectors'],
                true_subspace=emp_results['eigenvectors'],
                k=num_factors
            ),
            'eigenvalue_metrics': eigenvalue_ratio_metrics(
                generated_eigenvalues=diff_results['eigenvalues'],
                true_eigenvalues=emp_results['eigenvalues'],
                k=num_factors
            ),
            'cov_frob_diff': float(np.linalg.norm(
                diff_results['sample_covariance'] - emp_results['sample_covariance'], 'fro'
            )),
            'cov_op_diff': float(np.linalg.norm(
                diff_results['sample_covariance'] - emp_results['sample_covariance'], 2
            ))
        }
        
        # For each sample size, compute relative improvement
        if 'subspace_metrics' in emp_vs_true and 'subspace_metrics' in diff_vs_true:
            emp_error = emp_vs_true['subspace_metrics']['projection_error_normalized']
            diff_error = diff_vs_true['subspace_metrics']['projection_error_normalized']
            
            relative_improvement = (emp_error - diff_error) / emp_error
            
            emp_vs_diff['relative_improvement'] = {
                'projection_error': float(relative_improvement)
            }
        
        # Return all results
        return {
            'empirical_vs_true': emp_vs_true,
            'diffusion_vs_true': diff_vs_true,
            'empirical_vs_diffusion': emp_vs_diff,
            'empirical_results': emp_results,
            'diffusion_results': diff_results
        }
    
    def evaluate_sample_size_effect(
        self,
        sample_sizes: List[int],
        sample_generators: Dict[int, Dict[str, Union[torch.Tensor, np.ndarray]]],
        num_factors: Optional[int] = None
    ) -> Dict[str, Dict[int, Dict[str, float]]]:
        """
        Evaluate the effect of sample size on factor recovery.
        
        Args:
            sample_sizes: List of sample sizes
            sample_generators: Dictionary mapping sample sizes to generators (each with 'empirical' and 'diffusion' keys)
            num_factors: Number of factors to consider
            
        Returns:
            Dictionary of metrics across sample sizes
        """
        # Set default number of factors
        if num_factors is None:
            num_factors = self.factor_dim
        
        results = {}
        
        # Initialize metric dictionaries
        for metric_type in ['projection_error', 'canonical_correlation', 'eigenvalue_ratio_error']:
            results[metric_type] = {
                'empirical': {},
                'diffusion': {},
                'relative_improvement': {}
            }
        
        # Evaluate for each sample size
        for n in sample_sizes:
            # Get samples for this size
            empirical = sample_generators[n]['empirical']
            diffusion = sample_generators[n]['diffusion']
            
            # Compare
            comparison = self.compare_empirical_with_diffusion(empirical, diffusion, num_factors)
            
            # Extract metrics
            emp_vs_true = comparison['empirical_vs_true']
            diff_vs_true = comparison['diffusion_vs_true']
            
            # Store projection error
            if 'subspace_metrics' in emp_vs_true and 'subspace_metrics' in diff_vs_true:
                emp_error = emp_vs_true['subspace_metrics']['projection_error_normalized']
                diff_error = diff_vs_true['subspace_metrics']['projection_error_normalized']
                
                results['projection_error']['empirical'][n] = emp_error
                results['projection_error']['diffusion'][n] = diff_error
                results['projection_error']['relative_improvement'][n] = (emp_error - diff_error) / emp_error
            
            # Store canonical correlation
            if 'subspace_metrics' in emp_vs_true and 'subspace_metrics' in diff_vs_true:
                emp_corr = emp_vs_true['subspace_metrics']['canonical_correlation']
                diff_corr = diff_vs_true['subspace_metrics']['canonical_correlation']
                
                results['canonical_correlation']['empirical'][n] = emp_corr
                results['canonical_correlation']['diffusion'][n] = diff_corr
                results['canonical_correlation']['relative_improvement'][n] = (diff_corr - emp_corr) / (1 - emp_corr) if emp_corr < 1 else 0
            
            # Store eigenvalue ratio error
            if 'eigenvalue_metrics' in emp_vs_true and 'eigenvalue_metrics' in diff_vs_true:
                emp_error = emp_vs_true['eigenvalue_metrics']['top_k_eigenvalue_ratio_error']
                diff_error = diff_vs_true['eigenvalue_metrics']['top_k_eigenvalue_ratio_error']
                
                results['eigenvalue_ratio_error']['empirical'][n] = emp_error
                results['eigenvalue_ratio_error']['diffusion'][n] = diff_error
                results['eigenvalue_ratio_error']['relative_improvement'][n] = (emp_error - diff_error) / emp_error if emp_error > 0 else 0
        
        return results
    
    def plot_sample_size_effect(
        self,
        results: Dict[str, Dict[str, Dict[int, float]]],
        metric_name: str = 'projection_error',
        title: Optional[str] = None,
        figsize: Tuple[int, int] = (10, 6),
        save_path: Optional[str] = None
    ) -> plt.Figure:
        """
        Plot the effect of sample size on a given metric.
        
        Args:
            results: Results from evaluate_sample_size_effect
            metric_name: Name of the metric to plot
            title: Plot title
            figsize: Figure size
            save_path: Path to save the figure
            
        Returns:
            Matplotlib figure
        """
        if metric_name not in results:
            raise ValueError(f"Metric '{metric_name}' not found in results")
        
        # Extract sample sizes and metrics
        sample_sizes = sorted(results[metric_name]['empirical'].keys())
        emp_values = [results[metric_name]['empirical'][n] for n in sample_sizes]
        diff_values = [results[metric_name]['diffusion'][n] for n in sample_sizes]
        
        # Create figure
        fig, ax = plt.subplots(figsize=figsize)
        
        # Plot metrics
        ax.plot(sample_sizes, emp_values, 'o-', label='Empirical')
        ax.plot(sample_sizes, diff_values, 's--', label='Diffusion')
        
        # Add labels and title
        ax.set_xlabel('Sample Size (N)')
        ax.set_ylabel(metric_name.replace('_', ' ').title())
        
        if title is None:
            title = f'Effect of Sample Size on {metric_name.replace("_", " ").title()}'
        ax.set_title(title)
        
        ax.legend()
        ax.grid(True, alpha=0.3)
        
        # Set x-scale to log
        ax.set_xscale('log', base=2)
        
        # Save figure if path provided
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
        
        return fig
    
    def plot_relative_improvement(
        self,
        results: Dict[str, Dict[str, Dict[int, float]]],
        metric_names: Optional[List[str]] = None,
        title: str = 'Relative Improvement with Diffusion',
        figsize: Tuple[int, int] = (10, 6),
        save_path: Optional[str] = None
    ) -> plt.Figure:
        """
        Plot the relative improvement with diffusion compared to empirical.
        
        Args:
            results: Results from evaluate_sample_size_effect
            metric_names: List of metrics to plot
            title: Plot title
            figsize: Figure size
            save_path: Path to save the figure
            
        Returns:
            Matplotlib figure
        """
        if metric_names is None:
            metric_names = [k for k in results.keys() if 'relative_improvement' in results[k]]
        
        # Create figure
        fig, ax = plt.subplots(figsize=figsize)
        
        # Plot relative improvement for each metric
        for metric_name in metric_names:
            if metric_name not in results:
                self.logger.warning(f"Metric '{metric_name}' not found in results")
                continue
                
            if 'relative_improvement' not in results[metric_name]:
                self.logger.warning(f"No relative improvement data for '{metric_name}'")
                continue
                
            # Extract sample sizes and improvements
            sample_sizes = sorted(results[metric_name]['relative_improvement'].keys())
            improvements = [results[metric_name]['relative_improvement'][n] for n in sample_sizes]
            
            # Plot
            ax.plot(sample_sizes, improvements, 'o-', label=metric_name.replace('_', ' ').title())
        
        # Add horizontal line at y=0
        ax.axhline(y=0, color='black', linestyle='-', alpha=0.3)
        
        # Add labels and title
        ax.set_xlabel('Sample Size (N)')
        ax.set_ylabel('Relative Improvement')
        ax.set_title(title)
        ax.legend()
        ax.grid(True, alpha=0.3)
        
        # Set x-scale to log
        ax.set_xscale('log', base=2)
        
        # Save figure if path provided
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
        
        return fig


def evaluate_factor_recovery(
    generated_data: Dict[str, Any],
    true_data: Dict[str, Any],
    factor_dim: int,
    sample_sizes: Optional[List[int]] = None
) -> Dict[str, Any]:
    """
    Evaluate factor recovery from generated samples.
    
    Args:
        generated_data: Dictionary with generated data
        true_data: Dictionary with true data
        factor_dim: Number of factors
        sample_sizes: List of sample sizes to evaluate
        
    Returns:
        Dictionary of evaluation results
    """
    # Create evaluator
    evaluator = FactorRecoveryEvaluator(true_data, factor_dim)
    
    # Evaluate PCA recovery
    pca_results = evaluator.evaluate_pca_recovery(generated_data['samples'])
    
    # Compute SVD recovery
    svd_results = evaluator.compute_svd_recovery(generated_data['samples'])
    
    # Compare with true factor structure
    comparison_results = evaluator.compare_with_true_factor_structure(svd_results)
    
    # If sample sizes provided, evaluate sample size effect
    sample_size_results = None
    if sample_sizes and 'sample_size_datasets' in true_data:
        # Create dictionary of sample generators
        sample_generators = {}
        for n in sample_sizes:
            if n in true_data['sample_size_datasets']:
                sample_generators[n] = {
                    'empirical': true_data['sample_size_datasets'][n]['train_flat'],
                    'diffusion': generated_data['samples'][:n]  # Use first n samples
                }
        
        # Evaluate sample size effect
        sample_size_results = evaluator.evaluate_sample_size_effect(
            sample_sizes, sample_generators
        )
    
    return {
        'pca_results': pca_results,
        'svd_results': svd_results,
        'comparison_results': comparison_results,
        'sample_size_results': sample_size_results,
        'evaluator': evaluator
    }


if __name__ == "__main__":
    # Test the factor recovery evaluation with synthetic data
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
    
    # Create dictionaries
    true_data_dict = {
        'samples': true_data,
        'true_cov': true_cov,
        'true_factor_loadings': true_loadings,
        'true_eigenvalues': torch.linalg.eigvalsh(true_cov).flip(0)  # Sorted in descending order
    }
    
    generated_data_dict = {
        'samples': generated_data
    }
    
    # Evaluate factor recovery
    results = evaluate_factor_recovery(generated_data_dict, true_data_dict, k)
    
    # Print results
    print("\nPCA RESULTS:")
    for key, value in results['pca_results'].items():
        if not isinstance(value, np.ndarray):
            print(f"  {key}: {value}")
    
    print("\nCOMPARISON RESULTS:")
    for category, metrics in results['comparison_results'].items():
        print(f"  {category}:")
        for name, value in metrics.items():
            print(f"    {name}: {value}")
