"""
Synthetic data generation module for the Diffusion Factor Model.

This module implements the synthetic data generation process described in
Section 6 and Appendix D of the paper. It generates asset returns based on
a factor model with idiosyncratic noise.

References:
- Factor model structure: Equation (8)
- Data preprocessing: Appendix D "Data Preprocessing"
- Orthogonal factor loadings: Assumption 1(i)
"""

import numpy as np
import torch
from typing import Tuple, Dict, Optional, Union, List
import pandas as pd
from tqdm import tqdm
import scipy.stats
import matplotlib.pyplot as plt
import os
import pickle
import json


class SyntheticDataGenerator:
    """
    Generates synthetic asset returns data based on a factor model.
    
    The factor model is R = βF + ε, where:
    - R is the d-dimensional asset return vector
    - β is the d×k factor loading matrix
    - F is the k-dimensional factor vector
    - ε is the d-dimensional idiosyncratic noise vector
    
    As described in Section 6 and Appendix D of the paper.
    """
    
    def __init__(
        self,
        asset_dim: int,
        factor_dim: int,
        factor_mean_range: Tuple[float, float] = (0, 0.1),
        factor_std_multiplier: float = 1.5,
        noise_std_range: Tuple[float, float] = (0, 0.4),
        device: torch.device = torch.device('cpu'),
        seed: Optional[int] = None
    ):
        """
        Initialize the synthetic data generator.
        
        Args:
            asset_dim: Number of assets (d)
            factor_dim: Number of factors (k)
            factor_mean_range: Range for uniform sampling of factor means
            factor_std_multiplier: Multiplier for factor standard deviations (σ_Fi = factor_std_multiplier * μ_Fi)
            noise_std_range: Range for uniform sampling of idiosyncratic noise standard deviations
            device: Torch device
            seed: Random seed for reproducibility
        """
        self.asset_dim = asset_dim
        self.factor_dim = factor_dim
        self.factor_mean_range = factor_mean_range
        self.factor_std_multiplier = factor_std_multiplier
        self.noise_std_range = noise_std_range
        self.device = device
        
        # Set random seed if provided
        if seed is not None:
            np.random.seed(seed)
            torch.manual_seed(seed)
            
        # Generate factor means and standard deviations (Section 6)
        self.factor_means = np.random.uniform(
            factor_mean_range[0], 
            factor_mean_range[1], 
            size=factor_dim
        )

        # Set factor standard deviations proportional to means as per paper
        self.factor_stds = self.factor_means * factor_std_multiplier
        
        # Generate factor loadings matrix (β) with orthogonal columns (Assumption 1(i))
        beta = np.random.normal(0, 1, size=(asset_dim, factor_dim))
        q, _ = np.linalg.qr(beta)
        self.beta = q[:, :factor_dim]  # Ensure correct dimensions if asset_dim > factor_dim
        
        # Generate noise standard deviations (Appendix D)
        # Use uniform distribution as described in the paper but ensure decreasing values
        # for numerical stability
        raw_noise_stds = np.random.uniform(
            noise_std_range[0], 
            noise_std_range[1], 
            size=asset_dim
        )
        # Sort in descending order for better conditioning
        self.noise_stds = np.sort(raw_noise_stds)[::-1].copy()
        
        # Create factor covariance matrix (diagonal)
        self.factor_cov = np.diag(self.factor_stds ** 2)
        
        # Create noise covariance matrix (diagonal)
        self.noise_cov = np.diag(self.noise_stds ** 2)
        
        # Compute true asset return covariance matrix
        self.true_cov = self.beta @ self.factor_cov @ self.beta.T + self.noise_cov
        
        # Compute true asset return mean
        self.true_mean = self.beta @ self.factor_means
        
        # Calculate R-squared (variance explained by factors) (Section 6)
        self.r_squared = np.trace(self.beta @ self.factor_cov @ self.beta.T) / np.trace(self.true_cov)
        
        # Convert arrays to torch tensors
        self.to_torch()
        
        # Save ground truth eigen-decomposition of covariance matrix
        self._compute_eigen_decomposition()
        
    def to_torch(self):
        """Convert numpy arrays to torch tensors."""
        self.beta_torch = torch.tensor(np.copy(self.beta), dtype=torch.float32, device=self.device)
        self.factor_means_torch = torch.tensor(np.copy(self.factor_means), dtype=torch.float32, device=self.device)
        self.factor_cov_torch = torch.tensor(np.copy(self.factor_cov), dtype=torch.float32, device=self.device)
        self.noise_stds_torch = torch.tensor(np.copy(self.noise_stds), dtype=torch.float32, device=self.device)
        self.true_cov_torch = torch.tensor(np.copy(self.true_cov), dtype=torch.float32, device=self.device)
        self.true_mean_torch = torch.tensor(np.copy(self.true_mean), dtype=torch.float32, device=self.device)
        
    def _compute_eigen_decomposition(self):
        """Compute eigenvalues and eigenvectors of the true covariance matrix."""
        eigenvalues, eigenvectors = np.linalg.eigh(self.true_cov)
        # Sort in descending order
        idx = np.argsort(eigenvalues)[::-1]
        self.true_eigenvalues = eigenvalues[idx]
        self.true_eigenvectors = eigenvectors[:, idx]
        
        # Top k eigenvectors form the factor subspace
        self.true_factor_subspace = self.true_eigenvectors[:, :self.factor_dim]
        self.true_factor_projection = self.true_factor_subspace @ self.true_factor_subspace.T
        
        # Compute eigenvalue gap (Section 5, Theorem 3)
        self.eigen_gap = self.true_eigenvalues[self.factor_dim-1] - self.true_eigenvalues[self.factor_dim]
        
        # Convert to torch
        self.true_eigenvalues_torch = torch.tensor(self.true_eigenvalues, dtype=torch.float32, device=self.device)
        self.true_eigenvectors_torch = torch.tensor(self.true_eigenvectors, dtype=torch.float32, device=self.device)
        self.true_factor_subspace_torch = torch.tensor(self.true_factor_subspace, dtype=torch.float32, device=self.device)
        self.true_factor_projection_torch = torch.tensor(self.true_factor_projection, dtype=torch.float32, device=self.device)
        
    def generate_factors(self, num_samples: int) -> np.ndarray:
        """
        Generate factor vectors from multivariate normal distribution.
        
        Args:
            num_samples: Number of samples to generate
            
        Returns:
            Array of shape (num_samples, factor_dim)
        """
        return np.random.multivariate_normal(
            self.factor_means, 
            self.factor_cov, 
            size=num_samples
        )
    
    def generate_noise(self, num_samples: int) -> np.ndarray:
        """
        Generate idiosyncratic noise vectors from multivariate normal distribution.
        
        Args:
            num_samples: Number of samples to generate
            
        Returns:
            Array of shape (num_samples, asset_dim)
        """
        # Generate standard normal noise
        noise = np.random.multivariate_normal(
            np.zeros(self.asset_dim),  # Zero mean noise
            self.noise_cov,            # Use the exact noise covariance
            size=num_samples
        )
        return noise
    
    def generate_returns(self, num_samples: int, return_components: bool = False) -> Union[np.ndarray, Tuple[np.ndarray, np.ndarray, np.ndarray]]:
        """
        Generate asset returns based on the factor model.
        
        Args:
            num_samples: Number of samples to generate
            return_components: Whether to also return factors and noise components
            
        Returns:
            If return_components is False:
                Array of shape (num_samples, asset_dim) containing returns
            If return_components is True:
                Tuple of (returns, factors, noise), each with shape (num_samples, *)
        """
        # Generate factors and noise
        factors = self.generate_factors(num_samples)
        noise = self.generate_noise(num_samples)
        
        # Compute returns
        returns = factors @ self.beta.T + noise
        
        if return_components:
            return returns, factors, noise
        else:
            return returns
    
    def generate_torch_returns(self, num_samples: int, return_components: bool = False) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor, torch.Tensor]]:
        """
        Generate asset returns as torch tensors.
        
        Args:
            num_samples: Number of samples to generate
            return_components: Whether to also return factors and noise components
            
        Returns:
            Torch tensor(s) of returns (and optionally factors and noise)
        """
        if return_components:
            returns, factors, noise = self.generate_returns(num_samples, return_components=True)
            return (
                torch.tensor(returns, dtype=torch.float32, device=self.device),
                torch.tensor(factors, dtype=torch.float32, device=self.device),
                torch.tensor(noise, dtype=torch.float32, device=self.device)
            )
        else:
            returns = self.generate_returns(num_samples)
            return torch.tensor(returns, dtype=torch.float32, device=self.device)
    
    def generate_dataset(
        self, 
        num_samples: int, 
        train_ratio: float = 0.7,
        val_ratio: float = 0.1,
        test_ratio: float = 0.2,
        reshape_2d: Optional[Tuple[int, int]] = None,
        normalize: bool = True
    ) -> Dict[str, torch.Tensor]:
        """
        Generate a dataset with train/validation/test splits.
        
        Args:
            num_samples: Total number of samples to generate
            train_ratio: Fraction of samples for training
            val_ratio: Fraction of samples for validation
            test_ratio: Fraction of samples for testing
            reshape_2d: If provided, reshape returns to 2D tensors with these dimensions
            normalize: Whether to normalize the data by subtracting the mean (Appendix D)
            
        Returns:
            Dictionary with keys 'train', 'val', 'test', each containing tensor of returns
        """
        assert np.isclose(train_ratio + val_ratio + test_ratio, 1.0), "Ratios must sum to 1"
        
        # Generate returns
        returns = self.generate_torch_returns(num_samples)
        
        # Preprocessing - sort by variance if needed (Appendix D)
        return_vars = torch.var(returns, dim=0)
        sorted_indices = torch.argsort(return_vars, descending=True)
        returns = returns[:, sorted_indices]
        
        # Normalize if requested (Appendix D)
        if normalize:
            return_means = torch.mean(returns, dim=0, keepdim=True)
            returns = returns - return_means
        
        # Compute split indices
        train_size = int(num_samples * train_ratio)
        val_size = int(num_samples * val_ratio)
        
        # Split the data
        train_data = returns[:train_size]
        val_data = returns[train_size:train_size + val_size]
        test_data = returns[train_size + val_size:]
        
        # Reshape if needed
        if reshape_2d is not None:
            if train_data.shape[1] != reshape_2d[0] * reshape_2d[1]:
                raise ValueError(f"Cannot reshape data of shape {train_data.shape[1]} to {reshape_2d}")
            
            train_data = train_data.reshape(-1, reshape_2d[0], reshape_2d[1])
            val_data = val_data.reshape(-1, reshape_2d[0], reshape_2d[1])
            test_data = test_data.reshape(-1, reshape_2d[0], reshape_2d[1])
        
        return {
            'train': train_data,
            'val': val_data,
            'test': test_data
        }
    
    def generate_dataset_by_sample_sizes(
        self, 
        sample_sizes: List[int], 
        reshape_2d: Optional[Tuple[int, int]] = None,
        normalize: bool = True
    ) -> Dict[int, Dict[str, torch.Tensor]]:
        """
        Generate multiple datasets with different training sample sizes.
        
        Args:
            sample_sizes: List of training sample sizes to generate
            reshape_2d: If provided, reshape returns to 2D tensors with these dimensions
            normalize: Whether to normalize the data by subtracting the mean
            
        Returns:
            Dictionary mapping sample sizes to datasets
        """
        # Find maximum sample size needed
        max_samples = max(sample_sizes) + 1000  # Add buffer for val/test
        
        # Generate a large dataset
        all_returns = self.generate_torch_returns(max_samples)
        
        # Preprocessing - sort by variance if needed (Appendix D)
        return_vars = torch.var(all_returns, dim=0)
        sorted_indices = torch.argsort(return_vars, descending=True)
        all_returns = all_returns[:, sorted_indices]
        
        # Normalize if requested (Appendix D)
        if normalize:
            return_means = torch.mean(all_returns, dim=0, keepdim=True)
            all_returns = all_returns - return_means
        
        # Define test set as last 1000 samples
        test_data = all_returns[-1000:]
        
        # Create a dictionary to store datasets
        datasets = {}
        
        for n in sample_sizes:
            # Use exactly n samples for training
            train_data = all_returns[:n]
            
            # Define validation set as samples between train and test
            val_data = all_returns[n:-1000]
            
            # Reshape if needed
            if reshape_2d is not None:
                if train_data.shape[1] != reshape_2d[0] * reshape_2d[1]:
                    raise ValueError(f"Cannot reshape data of shape {train_data.shape[1]} to {reshape_2d}")
                
                train_data_reshaped = train_data.reshape(-1, reshape_2d[0], reshape_2d[1])
                val_data_reshaped = val_data.reshape(-1, reshape_2d[0], reshape_2d[1])
                test_data_reshaped = test_data.reshape(-1, reshape_2d[0], reshape_2d[1])
                
                datasets[n] = {
                    'train': train_data_reshaped, 
                    'val': val_data_reshaped, 
                    'test': test_data_reshaped,
                    'train_flat': train_data,
                    'val_flat': val_data,
                    'test_flat': test_data
                }
            else:
                datasets[n] = {
                    'train': train_data, 
                    'val': val_data, 
                    'test': test_data,
                    # flat aliases — downstream (factor_recovery) always reads *_flat
                    'train_flat': train_data,
                    'val_flat': val_data,
                    'test_flat': test_data
                }
        
        return datasets

    def get_summary_statistics(self) -> Dict[str, Dict[str, float]]:
        """
        Compute summary statistics for the synthetic data.
        
        Returns:
            Dictionary with summary statistics
        """
        # Generate a sample dataset to compute statistics
        returns = self.generate_returns(1000)
        
        # Compute statistics for returns
        return_means = np.mean(returns, axis=0)
        return_stds = np.std(returns, axis=0)
        
        # Compute summary stats for the distributions
        return_stats = {
            'mean': np.mean(return_means),
            'std': np.mean(return_stds),
            'min': np.min(return_means),
            '25%': np.percentile(return_means, 25),
            '50%': np.percentile(return_means, 50),
            '75%': np.percentile(return_means, 75),
            'max': np.max(return_means)
        }
        
        return_std_stats = {
            'mean': np.mean(return_stds),
            'std': np.std(return_stds),
            'min': np.min(return_stds),
            '25%': np.percentile(return_stds, 25),
            '50%': np.percentile(return_stds, 50),
            '75%': np.percentile(return_stds, 75),
            'max': np.max(return_stds)
        }
        
        # Return all statistics
        return {
            'return_means': return_stats,
            'return_stds': return_std_stats,
            'r_squared': self.r_squared,
            'eigenvalue_decay': self.true_eigenvalues[:10].tolist(),  # First 10 eigenvalues
            'eigen_gap': float(self.eigen_gap)
        }
    
    def save(self, path: str):
        """
        Save the generator object for reproducibility.
        
        Args:
            path: Path to save the generator
        """
        os.makedirs(os.path.dirname(path), exist_ok=True)
        
        # Save numpy and basic attributes
        save_dict = {
            'asset_dim': self.asset_dim,
            'factor_dim': self.factor_dim,
            'factor_mean_range': self.factor_mean_range,
            'factor_std_multiplier': self.factor_std_multiplier,
            'noise_std_range': self.noise_std_range,
            'factor_means': self.factor_means,
            'factor_stds': self.factor_stds,
            'beta': self.beta,
            'noise_stds': self.noise_stds,
            'factor_cov': self.factor_cov,
            'noise_cov': self.noise_cov,
            'true_cov': self.true_cov,
            'true_mean': self.true_mean,
            'r_squared': self.r_squared,
            'true_eigenvalues': self.true_eigenvalues,
            'true_eigenvectors': self.true_eigenvectors,
            'true_factor_subspace': self.true_factor_subspace,
            'true_factor_projection': self.true_factor_projection,
            'eigen_gap': self.eigen_gap
        }
        
        with open(path, 'wb') as f:
            pickle.dump(save_dict, f)
            
        # Save summary statistics as JSON for easier inspection
        stats = self.get_summary_statistics()
        with open(f"{os.path.splitext(path)[0]}_stats.json", 'w') as f:
            json.dump(stats, f, indent=2)
    
    @classmethod
    def load(cls, path: str, device: torch.device = torch.device('cpu')):
        """
        Load a generator from disk.
        
        Args:
            path: Path to load the generator from
            device: Torch device
            
        Returns:
            SyntheticDataGenerator instance
        """
        with open(path, 'rb') as f:
            load_dict = pickle.load(f)
        
        # Create instance with basic parameters
        generator = cls(
            asset_dim=load_dict['asset_dim'],
            factor_dim=load_dict['factor_dim'],
            factor_mean_range=load_dict['factor_mean_range'],
            factor_std_multiplier=load_dict['factor_std_multiplier'],
            noise_std_range=load_dict['noise_std_range'],
            device=device
        )
        
        # Override with saved values
        for key, value in load_dict.items():
            setattr(generator, key, value)
        
        # Convert to torch tensors
        generator.to_torch()
        
        return generator
    
    def visualize_return_distributions(self, num_samples: int = 1000, num_assets: int = 4, figsize: Tuple[int, int] = (12, 10)):
        """
        Visualize the distribution of returns for selected assets.
        
        Similar to Figure 1 in the paper.
        
        Args:
            num_samples: Number of samples to generate
            num_assets: Number of assets to plot
            figsize: Figure size
        """
        # Generate returns
        returns = self.generate_returns(num_samples)
        
        # Select representative assets
        var_indices = np.argsort(np.var(returns, axis=0))
        mean_indices = np.argsort(np.mean(returns, axis=0))
        
        indices = [
            var_indices[-1],  # Asset with largest variance
            var_indices[0],   # Asset with smallest variance
            mean_indices[-1], # Asset with largest mean
            mean_indices[0]   # Asset with smallest mean
        ]
        
        titles = [
            "Asset with largest variance",
            "Asset with smallest variance",
            "Asset with largest mean",
            "Asset with smallest mean"
        ]
        
        # Create plot
        fig, axes = plt.subplots(2, 2, figsize=figsize)
        axes = axes.flatten()
        
        for i, (idx, title) in enumerate(zip(indices, titles)):
            axes[i].hist(returns[:, idx], bins=30, alpha=0.7, density=True)
            axes[i].set_title(title)
            axes[i].set_xlabel("Return")
            axes[i].set_ylabel("Density")
            axes[i].grid(alpha=0.3)
        
        plt.tight_layout()
        return fig
    
    def visualize_eigenvalue_decay(self, k_plot: int = 20, figsize: Tuple[int, int] = (10, 6)):
        """
        Visualize the decay of eigenvalues of the covariance matrix.
        
        Args:
            k_plot: Number of eigenvalues to plot
            figsize: Figure size
        """
        k_plot = min(k_plot, self.asset_dim)
        
        fig, ax = plt.subplots(figsize=figsize)
        ax.plot(range(1, k_plot + 1), self.true_eigenvalues[:k_plot], 'o-', markersize=8)
        ax.axvline(x=self.factor_dim + 0.5, color='r', linestyle='--', 
                   label=f'Factor dimension (k={self.factor_dim})')
        ax.set_title("Eigenvalue Decay of Covariance Matrix")
        ax.set_xlabel("Eigenvalue Index")
        ax.set_ylabel("Eigenvalue")
        ax.set_yscale('log')
        ax.grid(True, alpha=0.3)
        ax.legend()
        plt.tight_layout()
        return fig
    
    def visualize_factor_loadings(self, num_assets_plot: int = 20, figsize: Tuple[int, int] = (12, 8)):
        """
        Visualize factor loadings (β) for selected assets.
        
        Args:
            num_assets_plot: Number of assets to include in the plot
            figsize: Figure size
        """
        # Select a subset of assets for clarity
        num_assets_plot = min(num_assets_plot, self.asset_dim)
        
        # Compute asset importance based on squared loadings
        asset_importance = np.sum(self.beta**2, axis=1)
        top_asset_indices = np.argsort(asset_importance)[-num_assets_plot:]
        
        # Create plot
        fig, ax = plt.subplots(figsize=figsize)
        im = ax.imshow(self.beta[top_asset_indices, :], cmap='coolwarm', aspect='auto')
        
        # Add colorbar
        cbar = fig.colorbar(im, ax=ax)
        cbar.set_label('Loading Value')
        
        # Labels
        ax.set_title(f"Factor Loadings for Top {num_assets_plot} Assets")
        ax.set_xlabel("Factor Index")
        ax.set_ylabel("Asset Index")
        
        plt.tight_layout()
        return fig


def create_synthetic_dataset(config: Dict) -> Dict:
    """
    Create a synthetic dataset according to the configuration.
    
    Args:
        config: Configuration dictionary with data parameters
        
    Returns:
        Dictionary with dataset and generator
    """
    print("Generating synthetic data based on factor model...")
    
    # Create data generator
    generator = SyntheticDataGenerator(
        asset_dim=config['asset_dim'],
        factor_dim=config['factor_dim'],
        factor_mean_range=config['factor_mean_range'],
        factor_std_multiplier=config['factor_std_multiplier'],
        noise_std_range=config['noise_std_range'],
        seed=config.get('seed', None)
    )
    
    # Print summary statistics
    stats = generator.get_summary_statistics()
    print(f"R-squared (variance explained by factors): {stats['r_squared']:.4f}")
    print(f"Return means statistics: {stats['return_means']}")
    print(f"Return std statistics: {stats['return_stds']}")
    print(f"Eigenvalue gap: {stats['eigen_gap']:.4f}")
    
    # Generate datasets for different sample sizes
    sample_sizes = config.get('sample_sizes', [512, 1024, 2048, 4096])
    datasets = generator.generate_dataset_by_sample_sizes(
        sample_sizes=sample_sizes,
        reshape_2d=config.get('reshape_dims', None),
        normalize=config.get('normalize', True)
    )
    
    # Also generate a full dataset with all samples
    full_dataset = generator.generate_dataset(
        num_samples=config['num_samples'],
        reshape_2d=config.get('reshape_dims', None),
        normalize=config.get('normalize', True)
    )
    
    # Save generator if path is provided
    if 'save_path' in config:
        os.makedirs(os.path.dirname(config['save_path']), exist_ok=True)
        generator.save(config['save_path'])
        print(f"Generator saved to {config['save_path']}")
    
    # Visualize if requested
    if config.get('visualize', False):
        # Create output directory if needed
        vis_dir = config.get('visualization_dir', 'visualizations')
        os.makedirs(vis_dir, exist_ok=True)
        
        # Generate visualizations
        fig1 = generator.visualize_return_distributions()
        fig1.savefig(os.path.join(vis_dir, 'return_distributions.png'))
        
        fig2 = generator.visualize_eigenvalue_decay()
        fig2.savefig(os.path.join(vis_dir, 'eigenvalue_decay.png'))
        
        fig3 = generator.visualize_factor_loadings()
        fig3.savefig(os.path.join(vis_dir, 'factor_loadings.png'))
        
        print(f"Visualizations saved to {vis_dir}")
    
    return {
        'generator': generator,
        'full_dataset': full_dataset,
        'sample_size_datasets': datasets,
        'stats': stats
    }


if __name__ == "__main__":
    # Test the synthetic data generation
    from config import SYNTHETIC_CONFIG, DEVICE
    
    # Create synthetic dataset
    data_dict = create_synthetic_dataset(SYNTHETIC_CONFIG)
    
    # Print some info
    generator = data_dict['generator']
    print(f"True covariance matrix shape: {generator.true_cov.shape}")
    print(f"Top 5 eigenvalues: {generator.true_eigenvalues[:5]}")
    
    # Print dataset shapes
    for split, data in data_dict['full_dataset'].items():
        print(f"{split} dataset shape: {data.shape}")
    
    # Print sample size dataset shapes
    for n, dataset in data_dict['sample_size_datasets'].items():
        print(f"N={n} train dataset shape: {dataset['train'].shape}")
    
    # # Visualize
    # generator.visualize_return_distributions()
    # plt.show()
    
    # generator.visualize_eigenvalue_decay()
    # plt.show()
    
    # generator.visualize_factor_loadings()
    # plt.show()