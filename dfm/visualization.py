"""
Visualization utilities for the Diffusion Factor Model.

This module provides functions for visualizing the model results,
including distribution plots, eigenvalue comparisons, and factor recovery.
"""
import torch
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from typing import Dict, Tuple, List, Optional, Union, Any
import pandas as pd
from matplotlib.ticker import MaxNLocator
import os


def plot_distribution_comparison(
    generated_samples: np.ndarray,
    reference_samples: np.ndarray,
    asset_indices: Optional[List[int]] = None,
    num_assets: int = 4,
    figsize: Tuple[int, int] = (15, 10),
    title: str = 'Distribution Comparison',
    bins: int = 50,
    save_path: Optional[str] = None
) -> plt.Figure:
    """
    Plot comparison of distributions for selected assets.
    """
    # Ensure arrays are numpy arrays
    if not isinstance(generated_samples, np.ndarray):
        generated_samples = np.array(generated_samples)
    if not isinstance(reference_samples, np.ndarray):
        reference_samples = np.array(reference_samples)
    
    # Fix for different dimensionality - ensure both are 2D with samples as rows
    if len(generated_samples.shape) > 2:
        # More than 2D, flatten all but the first dimension
        generated_samples = generated_samples.reshape(generated_samples.shape[0], -1)
        print(f"Reshaped generated samples to {generated_samples.shape}")
    
    if len(reference_samples.shape) > 2:
        # More than 2D, flatten all but the first dimension
        reference_samples = reference_samples.reshape(reference_samples.shape[0], -1)
        print(f"Reshaped reference samples to {reference_samples.shape}")
    
    # If reference_samples has a different dimensionality, reshape to match generated
    if len(reference_samples.shape) == 2 and len(generated_samples.shape) == 1:
        # Generated is 1D, but reference is 2D
        generated_samples = generated_samples.reshape(1, -1)
        print(f"Reshaped 1D generated samples to {generated_samples.shape}")
    elif len(generated_samples.shape) == 2 and len(reference_samples.shape) == 1:
        # Reference is 1D, but generated is 2D
        reference_samples = reference_samples.reshape(1, -1)
        print(f"Reshaped 1D reference samples to {reference_samples.shape}")
    
    # Key fix: Handle the reshaping for 2D matrices that need to be compared
    if len(generated_samples.shape) == 2 and len(reference_samples.shape) == 2:
        if generated_samples.shape[1] == 1 and reference_samples.shape[1] != 1:
            # Generated is a column vector, reshape reference to match
            total_elements = reference_samples.shape[0] * reference_samples.shape[1]
            reference_samples = reference_samples.reshape(total_elements, 1)
            print(f"Reshaped 2D reference samples to {reference_samples.shape}")
        elif reference_samples.shape[1] == 1 and generated_samples.shape[1] != 1:
            # Reference is a column vector, reshape generated to match
            total_elements = generated_samples.shape[0] * generated_samples.shape[1]
            generated_samples = generated_samples.reshape(total_elements, 1)
            print(f"Reshaped 2D generated samples to {generated_samples.shape}")
        elif generated_samples.shape[1] != reference_samples.shape[1]:
            # Different shapes - reshape both to 1D arrays for comparing individual elements
            gen_total = generated_samples.shape[0] * generated_samples.shape[1]
            ref_total = reference_samples.shape[0] * reference_samples.shape[1]
            
            # If total elements match, reshape to match
            if gen_total == ref_total:
                generated_samples = generated_samples.reshape(-1)
                reference_samples = reference_samples.reshape(-1)
                # Then reshape both to 2D with same shape
                new_shape = (gen_total, 1)
                generated_samples = generated_samples.reshape(new_shape)
                reference_samples = reference_samples.reshape(new_shape)
                print(f"Reshaped both samples to {new_shape}")
            else:
                print(f"Warning: Cannot compare samples with different total elements: {gen_total} vs {ref_total}")
    
    # Get number of assets for each dataset
    if len(generated_samples.shape) == 1:
        n_assets_gen = 1
    else:
        n_assets_gen = generated_samples.shape[1]
    
    if len(reference_samples.shape) == 1:
        n_assets_ref = 1
    else:
        n_assets_ref = reference_samples.shape[1]
    
    print(f"Generated shape: {generated_samples.shape}, Reference shape: {reference_samples.shape}")
    print(f"Assets in generated: {n_assets_gen}, Assets in reference: {n_assets_ref}")
    
    # Check if shapes are compatible for comparison
    if n_assets_gen != n_assets_ref:
        print(f"Warning: Different number of assets in generated ({n_assets_gen}) vs reference ({n_assets_ref})")
        # Try one more reshape attempt - if total elements match
        gen_total = np.prod(generated_samples.shape)
        ref_total = np.prod(reference_samples.shape)
        
        # Check if total elements match and both are 2D
        if gen_total == ref_total and len(generated_samples.shape) <= 2 and len(reference_samples.shape) <= 2:
            # Reshape both to have the same number of columns
            n_cols = min(n_assets_gen, n_assets_ref)
            n_rows_gen = gen_total // n_cols
            n_rows_ref = ref_total // n_cols
            
            # Reshape
            generated_samples = generated_samples.reshape(n_rows_gen, n_cols)
            reference_samples = reference_samples.reshape(n_rows_ref, n_cols)
            
            print(f"Reshaped to match: Generated {generated_samples.shape}, Reference {reference_samples.shape}")
            
            # Update asset counts
            n_assets_gen = n_cols
            n_assets_ref = n_cols
        else:
            # Fall back to comparing only common assets
            common_assets = min(n_assets_gen, n_assets_ref)
            if len(generated_samples.shape) > 1:
                generated_samples = generated_samples[:, :common_assets]
            if len(reference_samples.shape) > 1:
                reference_samples = reference_samples[:, :common_assets]
            print(f"Comparing only common assets: {common_assets}")
            n_assets_gen = common_assets
            n_assets_ref = common_assets
    
    # Select assets to plot
    if asset_indices is None:
        # Calculate variances
        if len(generated_samples.shape) == 1:
            var_gen = np.var(generated_samples)
            var_ref = np.var(reference_samples)
            # For 1D case, just use the same index
            asset_indices = [0] * num_assets
        else:
            var_gen = np.var(generated_samples, axis=0)
            var_ref = np.var(reference_samples, axis=0)
            
            # Ensure var_gen and var_ref have the same shape
            if var_gen.shape != var_ref.shape:
                print(f"Warning: Variance shapes don't match: {var_gen.shape} vs {var_ref.shape}")
                # Use common length
                common_len = min(len(var_gen), len(var_ref))
                var_gen = var_gen[:common_len]
                var_ref = var_ref[:common_len]
            
            # Compute combined variance
            combined_var = var_gen + var_ref
            
            # Select assets with highest and lowest variance
            if len(combined_var) >= num_assets:
                high_var_idx = np.argsort(combined_var)[-num_assets//2:]  # Highest variance
                low_var_idx = np.argsort(combined_var)[:num_assets//2]    # Lowest variance
                asset_indices = np.concatenate([high_var_idx, low_var_idx])
            else:
                # Not enough assets, use all available
                asset_indices = np.arange(len(combined_var))
    
    # Limit to specified number of assets and available assets
    if len(asset_indices) > num_assets:
        asset_indices = asset_indices[:num_assets]
    
    # Make sure indices don't exceed available assets
    max_index = min(n_assets_gen, n_assets_ref) - 1
    asset_indices = [min(idx, max_index) for idx in asset_indices]
    
    # Create figure
    n_plots = len(asset_indices)
    if n_plots <= 2:
        fig, axes = plt.subplots(1, n_plots, figsize=figsize)
    else:
        fig, axes = plt.subplots(2, (n_plots + 1) // 2, figsize=figsize)
    
    # Convert to array and flatten
    axes = np.array(axes).flatten()
    
    # Set color palette
    colors = ['#3498db', '#2ecc71']  # Blue for reference, green for generated
    
    # Plot distributions
    for i, idx in enumerate(asset_indices):
        if i >= len(axes):
            break  # Skip if we have more indices than axes
            
        ax = axes[i]
        
        # Get data for this asset
        if len(generated_samples.shape) == 1:
            gen_data = generated_samples
            ref_data = reference_samples
        else:
            gen_data = generated_samples[:, idx]
            ref_data = reference_samples[:, idx]
        
        # Plot histograms
        sns.histplot(ref_data, ax=ax, color=colors[0], 
                    alpha=0.5, label='Reference', bins=bins, stat='density')
        sns.histplot(gen_data, ax=ax, color=colors[1], 
                    alpha=0.5, label='Generated', bins=bins, stat='density')
        
        # Add kernel density estimate
        sns.kdeplot(ref_data, ax=ax, color=colors[0], linewidth=2)
        sns.kdeplot(gen_data, ax=ax, color=colors[1], linewidth=2)
        
        # Add title and labels
        ax.set_title(f'Asset {idx}')
        ax.set_xlabel('Return')
        ax.set_ylabel('Density')
        ax.legend()
        
        # Add grid
        ax.grid(True, alpha=0.3)
    
    # Hide unused axes
    for i in range(len(asset_indices), len(axes)):
        axes[i].set_visible(False)
    
    # Add overall title
    fig.suptitle(title, fontsize=16)
    plt.tight_layout(rect=[0, 0, 1, 0.96])
    
    # Save figure if requested
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
    
    return fig


def plot_eigenvalue_scree(
    generated_eigenvalues: np.ndarray,
    true_eigenvalues: Optional[np.ndarray] = None,
    num_components: int = 20,
    figsize: Tuple[int, int] = (10, 6),
    title: str = 'Eigenvalue Scree Plot',
    log_scale: bool = True,
    cumulative: bool = False,
    factor_dim: Optional[int] = None,
    save_path: Optional[str] = None
) -> plt.Figure:
    """
    Plot eigenvalue scree plot to visualize factor structure.
    
    Args:
        generated_eigenvalues: Eigenvalues of generated covariance matrix
        true_eigenvalues: Eigenvalues of true covariance matrix (optional)
        num_components: Number of components to plot
        figsize: Figure size
        title: Plot title
        log_scale: Whether to use log scale for y-axis
        cumulative: Whether to plot cumulative explained variance
        factor_dim: True factor dimension (for reference line)
        save_path: Path to save the figure
        
    Returns:
        Matplotlib figure
    """
    # Create figure
    fig, ax = plt.subplots(figsize=figsize)
    
    # Ensure eigenvalues are sorted in descending order
    generated_eigenvalues = np.sort(generated_eigenvalues)[::-1]
    
    if true_eigenvalues is not None:
        true_eigenvalues = np.sort(true_eigenvalues)[::-1]
    
    # Limit to specified number of components
    num_plot = min(num_components, len(generated_eigenvalues))
    
    # Create x-axis
    x = np.arange(1, num_plot + 1)
    
    if cumulative:
        # Plot cumulative explained variance ratio
        gen_total = np.sum(generated_eigenvalues)
        gen_ratio = np.cumsum(generated_eigenvalues[:num_plot]) / gen_total
        
        ax.plot(x, gen_ratio, 'o-', label='Generated', color='#3498db')
        
        if true_eigenvalues is not None:
            true_total = np.sum(true_eigenvalues)
            true_ratio = np.cumsum(true_eigenvalues[:num_plot]) / true_total
            
            ax.plot(x, true_ratio, 's--', label='True', color='#2ecc71')
        
        # Add horizontal lines at key thresholds
        for threshold in [0.7, 0.8, 0.9, 0.95]:
            ax.axhline(y=threshold, color='gray', linestyle=':', alpha=0.5)
            ax.text(x[-1] + 0.5, threshold, f'{threshold:.0%}', va='center')
        
        # Set y-label and limits
        ax.set_ylabel('Cumulative Explained Variance Ratio')
        ax.set_ylim(0, 1.05)
    else:
        # Plot raw eigenvalues
        ax.plot(x, generated_eigenvalues[:num_plot], 'o-', label='Generated', color='#3498db')
        
        if true_eigenvalues is not None:
            ax.plot(x, true_eigenvalues[:num_plot], 's--', label='True', color='#2ecc71')
        
        # Set y-label
        ax.set_ylabel('Eigenvalue')
        
        # Set log scale if requested
        if log_scale:
            ax.set_yscale('log')
    
    # Add vertical line at factor dimension
    if factor_dim is not None:
        ax.axvline(x=factor_dim, color='red', linestyle='--', 
                  alpha=0.7, label=f'k={factor_dim}')
    
    # Add labels, title, and legend
    ax.set_xlabel('Component')
    ax.set_title(title)
    ax.legend()
    
    # Set integer x-axis
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    
    # Add grid
    ax.grid(True, alpha=0.3)
    
    # Save figure if requested
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
    
    return fig


def plot_covariance_heatmap(
    estimated_cov: np.ndarray,
    true_cov: Optional[np.ndarray] = None,
    max_assets: int = 50,
    figsize: Tuple[int, int] = (16, 7),
    title: str = 'Covariance Matrix Comparison',
    show_difference: bool = True,
    save_path: Optional[str] = None
) -> plt.Figure:
    """
    Plot heatmap of covariance matrices.
    
    Args:
        estimated_cov: Estimated covariance matrix
        true_cov: True covariance matrix (optional)
        max_assets: Maximum number of assets to show
        figsize: Figure size
        title: Plot title
        show_difference: Whether to show difference matrix
        save_path: Path to save the figure
        
    Returns:
        Matplotlib figure
    """
    # Determine number of plots
    n_plots = 3 if (true_cov is not None and show_difference) else 2 if true_cov is not None else 1
    
    # Create figure
    fig, axes = plt.subplots(1, n_plots, figsize=figsize)
    if n_plots == 1:
        axes = [axes]
    
    # Limit to max_assets
    d = estimated_cov.shape[0]
    if d > max_assets:
        # Select assets with highest variance
        var = np.diag(estimated_cov)
        indices = np.argsort(var)[-max_assets:]
        
        # Extract submatrices
        estimated_cov = estimated_cov[np.ix_(indices, indices)]
        if true_cov is not None:
            true_cov = true_cov[np.ix_(indices, indices)]
    
    # Determine common scale
    if true_cov is not None:
        vmin = min(np.min(estimated_cov), np.min(true_cov))
        vmax = max(np.max(estimated_cov), np.max(true_cov))
    else:
        vmin = np.min(estimated_cov)
        vmax = np.max(estimated_cov)
    
    # Plot estimated covariance
    im0 = sns.heatmap(estimated_cov, ax=axes[0], cmap='viridis', vmin=vmin, vmax=vmax)
    axes[0].set_title('Estimated Covariance')
    
    # Plot true covariance if provided
    if true_cov is not None:
        im1 = sns.heatmap(true_cov, ax=axes[1], cmap='viridis', vmin=vmin, vmax=vmax)
        axes[1].set_title('True Covariance')
        
        # Plot difference if requested
        if show_difference:
            diff = estimated_cov - true_cov
            abs_max = max(abs(np.min(diff)), abs(np.max(diff)))
            im2 = sns.heatmap(diff, ax=axes[2], cmap='coolwarm', vmin=-abs_max, vmax=abs_max, center=0)
            axes[2].set_title('Difference')
    
    # Add overall title
    fig.suptitle(title, fontsize=16)
    plt.tight_layout(rect=[0, 0, 1, 0.96])
    
    # Save figure if requested
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
    
    return fig


def plot_subspace_recovery(
    recovered_eigenvectors: np.ndarray,
    true_eigenvectors: np.ndarray,
    k: int,
    figsize: Tuple[int, int] = (10, 8),
    title: str = 'Subspace Recovery',
    save_path: Optional[str] = None
) -> plt.Figure:
    """
    Visualize subspace recovery quality.
    
    Args:
        recovered_eigenvectors: Recovered eigenvectors
        true_eigenvectors: True eigenvectors
        k: Number of factors
        figsize: Figure size
        title: Plot title
        save_path: Path to save the figure
        
    Returns:
        Matplotlib figure
    """
    # Compute projections
    recovered_projection = recovered_eigenvectors[:, :k] @ recovered_eigenvectors[:, :k].T
    true_projection = true_eigenvectors[:, :k] @ true_eigenvectors[:, :k].T
    
    # Create figure
    fig, axes = plt.subplots(1, 3, figsize=figsize)
    
    # Determine common scale
    vmin = min(np.min(recovered_projection), np.min(true_projection))
    vmax = max(np.max(recovered_projection), np.max(true_projection))
    
    # Plot projections
    im0 = sns.heatmap(recovered_projection, ax=axes[0], cmap='viridis', vmin=vmin, vmax=vmax)
    axes[0].set_title('Recovered Projection')
    
    im1 = sns.heatmap(true_projection, ax=axes[1], cmap='viridis', vmin=vmin, vmax=vmax)
    axes[1].set_title('True Projection')
    
    # Plot difference
    diff = recovered_projection - true_projection
    abs_max = max(abs(np.min(diff)), abs(np.max(diff)))
    im2 = sns.heatmap(diff, ax=axes[2], cmap='coolwarm', vmin=-abs_max, vmax=abs_max, center=0)
    axes[2].set_title('Difference')
    
    # Add overall title
    fig.suptitle(title, fontsize=16)
    plt.tight_layout(rect=[0, 0, 1, 0.96])
    
    # Save figure if requested
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
    
    return fig


def plot_training_history(
    history: Dict[str, List[float]],
    figsize: Tuple[int, int] = (12, 8),
    save_path: Optional[str] = None
) -> plt.Figure:
    """
    Plot training history.
    
    Args:
        history: Dictionary of training metrics
        figsize: Figure size
        save_path: Path to save the figure
        
    Returns:
        Matplotlib figure
    """
    # Create figure
    fig, axes = plt.subplots(2, 1, figsize=figsize, sharex=True)
    
    # Plot training and validation loss
    if 'train_loss' in history:
        axes[0].plot(history['train_loss'], label='Train Loss')
    if 'val_loss' in history:
        axes[0].plot(history['val_loss'], label='Validation Loss')
    
    axes[0].set_ylabel('Loss')
    axes[0].set_title('Training and Validation Loss')
    axes[0].legend()
    axes[0].grid(True, alpha=0.3)
    
    # Plot learning rate
    if 'learning_rate' in history:
        axes[1].plot(history['learning_rate'], label='Learning Rate')
        axes[1].set_ylabel('Learning Rate')
        axes[1].set_yscale('log')
    
    # Set x-axis label
    axes[1].set_xlabel('Epoch')
    axes[1].grid(True, alpha=0.3)
    
    plt.tight_layout()
    
    # Save figure if requested
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
    
    return fig


def plot_relative_improvement_by_sample_size(
    sample_sizes: List[int],
    diff_errors: List[float],
    emp_errors: List[float],
    figsize: Tuple[int, int] = (10, 6),
    title: str = 'Effect of Sample Size on Subspace Recovery',
    ylabel: str = 'Projection Error',
    log_scale: bool = True,
    save_path: Optional[str] = None
) -> plt.Figure:
    """
    Plot effect of sample size on subspace recovery error.
    
    Args:
        sample_sizes: List of sample sizes
        diff_errors: List of diffusion model errors
        emp_errors: List of empirical errors
        figsize: Figure size
        title: Plot title
        ylabel: Y-axis label
        log_scale: Whether to use log scale for errors
        save_path: Path to save the figure
        
    Returns:
        Matplotlib figure
    """
    # Create figure
    fig, ax = plt.subplots(figsize=figsize)
    
    # Plot errors
    ax.plot(sample_sizes, emp_errors, 'o-', label='Empirical', color='#3498db')
    ax.plot(sample_sizes, diff_errors, 's--', label='Diffusion', color='#2ecc71')
    
    # Calculate relative improvement
    rel_improvements = [(emp - diff) / emp * 100 for emp, diff in zip(emp_errors, diff_errors)]
    
    # Add relative improvement as text
    for i, n in enumerate(sample_sizes):
        if rel_improvements[i] > 0:
            ax.annotate(f"+{rel_improvements[i]:.1f}%", 
                      xy=(n, (emp_errors[i] + diff_errors[i]) / 2),
                      xytext=(0, 10),
                      textcoords="offset points", 
                      ha='center',
                      fontsize=9,
                      color='green')
    
    # Set labels and title
    ax.set_xlabel('Sample Size (N)')
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    
    # Set x-axis as log scale
    ax.set_xscale('log', base=2)
    
    # Set y-axis as log scale if requested
    if log_scale:
        ax.set_yscale('log')
    
    # Add legend
    ax.legend()
    
    # Add grid
    ax.grid(True, alpha=0.3)
    
    # Save figure if requested
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
    
    return fig


def plot_sample_trajectories(
    trajectories: List[torch.Tensor],
    asset_indices: List[int],
    num_steps: int = 10,
    figsize: Tuple[int, int] = (15, 10),
    title: str = 'Diffusion Sampling Trajectories',
    save_path: Optional[str] = None
) -> plt.Figure:
    """
    Plot evolution of asset returns during the reverse diffusion process.
    
    Args:
        trajectories: List of tensors representing sampling trajectory
        asset_indices: Indices of assets to plot
        num_steps: Number of time steps to include (evenly spaced)
        figsize: Figure size
        title: Plot title
        save_path: Path to save the figure
        
    Returns:
        Matplotlib figure
    """
    # Create figure
    fig, axes = plt.subplots(len(asset_indices), 1, figsize=figsize, sharex=True)
    if len(asset_indices) == 1:
        axes = [axes]
    
    # Convert trajectories to numpy if they are torch tensors
    if isinstance(trajectories[0], torch.Tensor):
        trajectories = [t.cpu().numpy() for t in trajectories]
    
    # Select time steps
    total_steps = len(trajectories)
    step_indices = np.linspace(0, total_steps - 1, num_steps).astype(int)
    
    # Create color map
    cmap = plt.cm.viridis
    colors = [cmap(i / (num_steps - 1)) for i in range(num_steps)]
    
    # Plot trajectories
    for i, asset_idx in enumerate(asset_indices):
        ax = axes[i]
        
        # Plot each time step as a distribution
        for j, step_idx in enumerate(step_indices):
            step_data = trajectories[step_idx][:, asset_idx]
            sns.kdeplot(step_data, ax=ax, color=colors[j], 
                       label=f'T-t={total_steps - step_idx}' if i == 0 else None)
        
        # Add title and labels
        ax.set_title(f'Asset {asset_idx}')
        ax.set_ylabel('Density')
        ax.grid(True, alpha=0.3)
    
    # Add x-label to bottom plot
    axes[-1].set_xlabel('Return')
    
    # Add legend to first plot only
    if len(axes) > 0:
        axes[0].legend(title="Diffusion Step")
    
    # Add overall title
    fig.suptitle(title, fontsize=16)
    plt.tight_layout(rect=[0, 0, 1, 0.96])
    
    # Save figure if requested
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
    
    return fig


def plot_metric_comparison_table(
    results: Dict[str, Dict[str, float]],
    metrics: List[str],
    names: Optional[List[str]] = None,
    figsize: Tuple[int, int] = (10, 6),
    title: str = 'Strategy Performance Comparison',
    cmap: str = 'RdYlGn',
    highlight_best: bool = True,
    save_path: Optional[str] = None
) -> plt.Figure:
    """
    Create a comparison table of performance metrics.
    
    Args:
        results: Dictionary of strategy results
        metrics: List of metrics to include
        names: List of strategy names (if None, use keys from results)
        figsize: Figure size
        title: Table title
        cmap: Colormap for highlighting
        highlight_best: Whether to highlight the best value in each row
        save_path: Path to save the figure
        
    Returns:
        Matplotlib figure
    """
    if names is None:
        names = list(results.keys())
    
    # Create data table
    data = []
    for metric in metrics:
        row = []
        for name in names:
            if name in results and 'metrics' in results[name] and metric in results[name]['metrics']:
                row.append(results[name]['metrics'][metric])
            else:
                row.append(np.nan)
        data.append(row)
    
    # Convert to pandas DataFrame
    df = pd.DataFrame(data, index=metrics, columns=names)
    
    # Highlight best values
    if highlight_best:
        # Determine direction of comparison (higher or lower is better)
        higher_better = {
            'mean': True, 'annualized_mean': True, 
            'sharpe_ratio': True, 'annualized_sharpe': True,
            'sortino_ratio': True, 'cer': True
        }
        
        lower_better = {
            'std': False, 'annualized_std': False,
            'max_drawdown': False
        }
        
        # Create formatter
        def formatter(val, name):
            if pd.isna(val):
                return 'N/A'
            
            if name in ['sharpe_ratio', 'annualized_sharpe', 'sortino_ratio']:
                return f'{val:.2f}'
            elif name in ['max_drawdown']:
                return f'{val:.2%}'
            elif name in ['mean', 'annualized_mean', 'std', 'annualized_std', 'cer']:
                return f'{val:.4f}'
            else:
                return f'{val:.4g}'
        
        # Format the DataFrame
        formatted_df = df.copy()
        for idx, row in df.iterrows():
            for col in row.index:
                formatted_df.loc[idx, col] = formatter(row[col], idx)
        
        # Create figure
        fig, ax = plt.subplots(figsize=figsize)
        ax.axis('tight')
        ax.axis('off')
        
        # Create table
        table = ax.table(
            cellText=formatted_df.values,
            rowLabels=formatted_df.index,
            colLabels=formatted_df.columns,
            cellLoc='center',
            loc='center'
        )
        
        # Style table
        table.auto_set_font_size(False)
        table.set_fontsize(10)
        table.scale(1.2, 1.5)
        
        # Highlight best values
        for i, metric in enumerate(metrics):
            if metric in higher_better:
                best_idx = df.iloc[i].idxmax()
                best_col = df.columns.get_loc(best_idx)
                table[(i + 1, best_col)].set_facecolor('#c8e6c9')  # Light green
            elif metric in lower_better:
                best_idx = df.iloc[i].idxmin()
                best_col = df.columns.get_loc(best_idx)
                table[(i + 1, best_col)].set_facecolor('#c8e6c9')  # Light green
    else:
        # Create figure and table without highlighting
        fig, ax = plt.subplots(figsize=figsize)
        ax.axis('tight')
        ax.axis('off')
        
        # Format the DataFrame
        formatted_df = df.copy()
        for idx, row in df.iterrows():
            for col in row.index:
                val = row[col]
                if pd.isna(val):
                    formatted_df.loc[idx, col] = 'N/A'
                elif idx in ['sharpe_ratio', 'annualized_sharpe', 'sortino_ratio']:
                    formatted_df.loc[idx, col] = f'{val:.2f}'
                elif idx in ['max_drawdown']:
                    formatted_df.loc[idx, col] = f'{val:.2%}'
                elif idx in ['mean', 'annualized_mean', 'std', 'annualized_std', 'cer']:
                    formatted_df.loc[idx, col] = f'{val:.4f}'
                else:
                    formatted_df.loc[idx, col] = f'{val:.4g}'
        
        # Create table
        table = ax.table(
            cellText=formatted_df.values,
            rowLabels=formatted_df.index,
            colLabels=formatted_df.columns,
            cellLoc='center',
            loc='center'
        )
        
        # Style table
        table.auto_set_font_size(False)
        table.set_fontsize(10)
        table.scale(1.2, 1.5)
    
    # Add title
    ax.set_title(title, fontsize=14, pad=20)
    
    # Save figure if requested
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
    
    return fig


if __name__ == "__main__":
    # Test visualization functions with simulated data
    import numpy as np
    
    # Generate synthetic data
    np.random.seed(42)
    n_samples = 1000
    n_assets = 10
    factor_dim = 2
    
    # Create factor loadings and factors
    factor_loadings = np.random.randn(n_assets, factor_dim)
    factors = np.random.randn(n_samples, factor_dim)
    
    # Generate true data
    noise = 0.1 * np.random.randn(n_samples, n_assets)
    true_data = factors @ factor_loadings.T + noise
    
    # Add some distortion to create "generated" data
    extra_noise = 0.05 * np.random.randn(n_samples, n_assets)
    generated_data = true_data + extra_noise
    
    # Create covariance matrices
    true_cov = np.cov(true_data, rowvar=False)
    gen_cov = np.cov(generated_data, rowvar=False)
    
    # Plot distribution comparison
    fig1 = plot_distribution_comparison(
        generated_data, true_data,
        asset_indices=[0, 1, 2, 3],
        title='Test Distribution Comparison'
    )
    
    # Compute eigendecomposition
    true_eigenvalues, true_eigenvectors = np.linalg.eigh(true_cov)
    gen_eigenvalues, gen_eigenvectors = np.linalg.eigh(gen_cov)
    
    # Sort in descending order
    true_idx = np.argsort(true_eigenvalues)[::-1]
    true_eigenvalues = true_eigenvalues[true_idx]
    true_eigenvectors = true_eigenvectors[:, true_idx]
    
    gen_idx = np.argsort(gen_eigenvalues)[::-1]
    gen_eigenvalues = gen_eigenvalues[gen_idx]
    gen_eigenvectors = gen_eigenvectors[:, gen_idx]
    
    # Plot eigenvalue scree plot
    fig2 = plot_eigenvalue_scree(
        gen_eigenvalues, true_eigenvalues,
        num_components=10,
        title='Test Eigenvalue Scree Plot',
        factor_dim=factor_dim
    )
    
    # Plot covariance heatmap
    fig3 = plot_covariance_heatmap(
        gen_cov, true_cov,
        title='Test Covariance Comparison'
    )
    
    # Plot subspace recovery
    fig4 = plot_subspace_recovery(
        gen_eigenvectors, true_eigenvectors,
        k=factor_dim,
        title='Test Subspace Recovery'
    )
    
    # Plot sample size effect
    sample_sizes = [32, 64, 128, 256, 512]
    emp_errors = [0.5, 0.4, 0.3, 0.2, 0.15]
    diff_errors = [0.45, 0.32, 0.21, 0.12, 0.08]
    
    fig5 = plot_relative_improvement_by_sample_size(
        sample_sizes, diff_errors, emp_errors,
        title='Test Sample Size Effect',
        ylabel='Projection Error'
    )
    
    plt.show()

                      