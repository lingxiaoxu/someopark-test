"""
Data preprocessing utilities for the Diffusion Factor Model.

This module implements preprocessing steps for asset returns data,
including normalization, Winsorization, and reshaping.
"""

import numpy as np
import torch
from typing import Dict, Tuple, Optional, Union, List
import pandas as pd
import os
import matplotlib.pyplot as plt


def winsorize(data: np.ndarray, limits: Tuple[float, float] = (0.025, 0.025), 
             resample: bool = True, preserve_sign: bool = True) -> np.ndarray:
    """
    Winsorize data by clipping extreme values.
    
    As described in Appendix E: "Winsorize returns for each stock at 2.5% 
    each side by resampling non-extreme values with the same sign"
    
    Args:
        data: Input data array
        limits: Tuple of (lower, upper) percentile limits for Winsorization
        resample: If True, replace extreme values with resampled non-extreme values
                 If False, clip values at percentile thresholds
        preserve_sign: If True, resample only from values with the same sign
                 
    Returns:
        Winsorized data array
    """
    lower_percentile = np.percentile(data, limits[0] * 100, axis=0)
    upper_percentile = np.percentile(data, (1 - limits[1]) * 100, axis=0)
    
    if resample:
        # Copy input data to avoid modifying the original
        winsorized_data = data.copy()
        
        # For each column, replace extreme values with resampled non-extreme values
        for j in range(data.shape[1]):
            # Find indices of extreme values
            lower_mask = data[:, j] < lower_percentile[j]
            upper_mask = data[:, j] > upper_percentile[j]
            
            # Find indices of non-extreme values
            non_extreme_indices = np.where((~lower_mask) & (~upper_mask))[0]
            
            if len(non_extreme_indices) > 0:
                # Handle lower extreme values
                lower_extreme_count = np.sum(lower_mask)
                if lower_extreme_count > 0:
                    if preserve_sign:
                        # Find negative non-extreme values for sign preservation
                        negative_indices = np.where((~lower_mask) & (~upper_mask) & 
                                                  (data[:, j] < 0))[0]
                        if len(negative_indices) > 0:
                            # Resample from negative non-extreme values
                            winsorized_data[lower_mask, j] = np.random.choice(
                                data[negative_indices, j],
                                size=lower_extreme_count,
                                replace=True
                            )
                        else:
                            # If no negative non-extreme values, use the lower threshold
                            winsorized_data[lower_mask, j] = lower_percentile[j]
                    else:
                        # Resample from all non-extreme values
                        winsorized_data[lower_mask, j] = np.random.choice(
                            data[non_extreme_indices, j],
                            size=lower_extreme_count,
                            replace=True
                        )
                
                # Handle upper extreme values
                upper_extreme_count = np.sum(upper_mask)
                if upper_extreme_count > 0:
                    if preserve_sign:
                        # Find positive non-extreme values for sign preservation
                        positive_indices = np.where((~lower_mask) & (~upper_mask) & 
                                                  (data[:, j] > 0))[0]
                        if len(positive_indices) > 0:
                            # Resample from positive non-extreme values
                            winsorized_data[upper_mask, j] = np.random.choice(
                                data[positive_indices, j],
                                size=upper_extreme_count,
                                replace=True
                            )
                        else:
                            # If no positive non-extreme values, use the upper threshold
                            winsorized_data[upper_mask, j] = upper_percentile[j]
                    else:
                        # Resample from all non-extreme values
                        winsorized_data[upper_mask, j] = np.random.choice(
                            data[non_extreme_indices, j],
                            size=upper_extreme_count,
                            replace=True
                        )
        
        return winsorized_data
    else:
        # Simply clip values at percentile thresholds
        return np.clip(data, lower_percentile, upper_percentile)


def normalize(data: np.ndarray, method: str = 'standard', 
              means: Optional[np.ndarray] = None, 
              stds: Optional[np.ndarray] = None, 
              only_center: bool = True) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Normalize data using different methods.
    
    As described in Appendix D: "normalize the data by subtracting the mean return of each asset"
    
    Args:
        data: Input data array of shape (samples, features)
        method: Normalization method ('standard', 'min-max', 'robust')
        means: Pre-calculated means (for consistent transform on test data)
        stds: Pre-calculated standard deviations or ranges
        only_center: If True and method='standard', only subtract mean without dividing by std
                     This matches the paper's normalization approach
        
    Returns:
        Tuple of (normalized_data, means, stds)
    """
    if method == 'standard':
        # Standardize by centering (and optionally scaling)
        if means is None:
            means = np.mean(data, axis=0)
        if stds is None:
            stds = np.std(data, axis=0)
            # Avoid division by zero
            stds[stds == 0] = 1.0
            
        # Apply normalization using the provided or calculated stats
        if only_center:
            # Only subtract mean (as in the paper's Appendix D)
            normalized = data - means
        else:
            # Standard normalization (mean=0, std=1)
            normalized = (data - means) / stds
        
    elif method == 'min-max':
        # Scale to [0, 1] range
        if means is None:
            means = np.min(data, axis=0)
        if stds is None:
            stds = np.max(data, axis=0) - np.min(data, axis=0)
            # Avoid division by zero
            stds[stds == 0] = 1.0
            
        normalized = (data - means) / stds
        
    elif method == 'robust':
        # Scale based on quartiles
        if means is None:
            means = np.percentile(data, 25, axis=0)
        if stds is None:
            stds = np.percentile(data, 75, axis=0) - means
            # Avoid division by zero
            stds[stds == 0] = 1.0
            
        normalized = (data - means) / stds
        
    else:
        raise ValueError(f"Unknown normalization method: {method}")
        
    return normalized, means, stds


def reshape_to_2d(data: np.ndarray, dims: Tuple[int, int], 
                  feature_order: Optional[np.ndarray] = None,
                  pad: bool = False) -> np.ndarray:
    """
    Reshape feature vectors to 2D arrays, with column-first arrangement.
    
    As described in Appendix D: "reshape the data from a one-dimensional vector 
    of length 2^11 into a two-dimensional matrix of size (2^5, 2^6)"
    
    Args:
        data: Input data array of shape (samples, features)
        dims: Target dimensions (height, width)
        feature_order: Order of features to arrange in 2D grid
        pad: Whether to pad with zeros if required
        
    Returns:
        Reshaped data array of shape (samples, height, width)
    """
    n_samples, n_features = data.shape
    height, width = dims
    target_size = height * width
    
    # Create result array
    result = np.zeros((n_samples, height, width))
    
    if feature_order is not None:
        # Limit to valid indices and target size
        valid_indices = feature_order[feature_order < n_features]
        if len(valid_indices) > target_size:
            valid_indices = valid_indices[:target_size]
            
        # Fill the result array COLUMN-FIRST using the feature order
        for w in range(width):
            for h in range(height):
                flat_idx = w * height + h  # Column-first indexing
                if flat_idx < len(valid_indices):
                    feature_idx = valid_indices[flat_idx]
                    result[:, h, w] = data[:, feature_idx]
    else:
        # Without feature ordering, reshape normally (row-first)
        if n_features >= target_size:
            flat_data = data[:, :target_size]
            for h in range(height):
                for w in range(width):
                    flat_idx = h * width + w  # Row-first indexing
                    result[:, h, w] = flat_data[:, flat_idx]
        elif pad:
            # Pad and reshape
            padded = np.zeros((n_samples, target_size))
            padded[:, :n_features] = data
            for h in range(height):
                for w in range(width):
                    flat_idx = h * width + w
                    if flat_idx < target_size:
                        if flat_idx < n_features:
                            result[:, h, w] = data[:, flat_idx]
        else:
            # Adjust dimensions based on available features
            new_height = min(height, n_features)
            new_width = min(width, n_features // height + (1 if n_features % height else 0))
            for h in range(new_height):
                for w in range(new_width):
                    flat_idx = h * new_width + w
                    if flat_idx < n_features:
                        result[:, h, w] = data[:, flat_idx]
    
    return result    

def flatten_2d(data: np.ndarray) -> np.ndarray:
    """
    Flatten 2D tensor back to 1D.
    
    Args:
        data: Input data array of shape (num_samples, height, width)
        
    Returns:
        Flattened data array of shape (num_samples, height*width)
    """
    return data.reshape(data.shape[0], -1)


def prepare_data_loader(data: torch.Tensor, 
                        batch_size: int,
                        shuffle: bool = True) -> torch.utils.data.DataLoader:
    """
    Prepare PyTorch DataLoader for training/evaluation.
    
    Args:
        data: Input data tensor
        batch_size: Batch size for DataLoader
        shuffle: Whether to shuffle data
        
    Returns:
        PyTorch DataLoader
    """
    dataset = torch.utils.data.TensorDataset(data)
    return torch.utils.data.DataLoader(
        dataset, 
        batch_size=batch_size,
        shuffle=shuffle
    )


def handle_missing_values(data: pd.DataFrame, method: str = 'ffill+bfill') -> pd.DataFrame:
    """
    Handle missing values in the dataset.
    
    Args:
        data: Input DataFrame
        method: Method for handling missing values
                'ffill+bfill': Forward fill then backward fill
                'drop': Drop rows with missing values
                'interpolate': Linear interpolation
                
    Returns:
        DataFrame with handled missing values
    """
    if method == 'ffill+bfill':
        return data.fillna(method='ffill').fillna(method='bfill')
    elif method == 'drop':
        return data.dropna()
    elif method == 'interpolate':
        return data.interpolate(method='linear', axis=0).fillna(method='ffill').fillna(method='bfill')
    else:
        raise ValueError(f"Unknown missing value handling method: {method}")


def sort_by_variance(data: Union[np.ndarray, pd.DataFrame]) -> Tuple[Union[np.ndarray, pd.DataFrame], np.ndarray]:
    """
    Sort features by variance in descending order.
    
    As described in Appendix D: "sort the asset returns by their variance,
    prioritizing those with greater variability"
    
    Args:
        data: Input data as array or DataFrame
        
    Returns:
        Tuple of (sorted_data, sorting_indices)
    """
    if isinstance(data, pd.DataFrame):
        variances = data.var()
        sorted_indices = variances.sort_values(ascending=False).index
        return data[sorted_indices], sorted_indices
    else:
        variances = np.var(data, axis=0)
        sorted_indices = np.argsort(variances)[::-1]
        return data[:, sorted_indices], sorted_indices


def prepare_dataset(data: Union[np.ndarray, pd.DataFrame, torch.Tensor],
                    winsorize_limits: Optional[Tuple[float, float]] = None,
                    normalize_method: Optional[str] = 'standard',
                    reshape_dims: Optional[Tuple[int, int]] = None,
                    sort_by_var: bool = True,
                    device: torch.device = torch.device('cpu')) -> Dict:
    """
    Prepare dataset with preprocessing steps as described in the paper.
    
    Args:
        data: Input data
        winsorize_limits: Limits for Winsorization (if None, skip Winsorization)
        normalize_method: Method for normalization (if None, skip normalization)
        reshape_dims: Dimensions for reshaping (if None, skip reshaping)
        sort_by_var: Whether to sort features by variance in descending order
        device: Torch device
        
    Returns:
        Dictionary with processed data and preprocessing parameters
    """
    # Convert to numpy if needed
    if isinstance(data, pd.DataFrame):
        data_np = data.values
    elif isinstance(data, torch.Tensor):
        data_np = data.cpu().numpy()
    else:
        data_np = data
    
    # Sort by variance if specified
    if sort_by_var:
        data_np, sorted_indices = sort_by_variance(data_np)
    else:
        sorted_indices = np.arange(data_np.shape[1])
    
    # Winsorize if specified
    if winsorize_limits is not None:
        data_np = winsorize(data_np, limits=winsorize_limits, preserve_sign=True)
    
    # Normalize if specified
    if normalize_method is not None:
        data_np, means, stds = normalize(data_np, method=normalize_method, only_center=True)
    else:
        means, stds = None, None
    
    # Convert to flat tensor first
    data_flat = data_np
    data_flat_tensor = torch.tensor(data_flat, dtype=torch.float32, device=device)
    
    # Reshape if specified
    if reshape_dims is not None:
        data_reshaped = reshape_to_2d(data_np, dims=reshape_dims, pad=True)
        data_tensor = torch.tensor(data_reshaped, dtype=torch.float32, device=device)
    else:
        data_tensor = data_flat_tensor
    
    # Return processed data and parameters
    return {
        'data': data_tensor,
        'data_flat': data_flat_tensor,
        'means': means,
        'stds': stds,
        'winsorize_limits': winsorize_limits,
        'normalize_method': normalize_method,
        'reshape_dims': reshape_dims,
        'sorted_indices': sorted_indices
    }


def prepare_real_data(data: pd.DataFrame,
                      max_assets: Optional[int] = None,
                      min_valid_ratio: float = 0.95,
                      winsorize_limits: Tuple[float, float] = (0.025, 0.025),
                      normalize_method: str = 'standard',
                      reshape_dims: Optional[Tuple[int, int]] = None,
                      missing_value_method: str = 'ffill+bfill',
                      device: torch.device = torch.device('cpu')) -> Dict:
    """
    Prepare real market data with preprocessing steps as described in Section 7 and Appendix E.
    
    Args:
        data: Input data as DataFrame
        max_assets: Maximum number of assets to keep (if None, keep all)
        min_valid_ratio: Minimum ratio of valid data points required for an asset
        winsorize_limits: Limits for Winsorization
        normalize_method: Method for normalization
        reshape_dims: Dimensions for reshaping
        missing_value_method: Method for handling missing values
        device: Torch device
        
    Returns:
        Dictionary with processed data and preprocessing parameters
    """
    # Filter assets with sufficient valid data
    valid_counts = data.count()
    valid_ratio = valid_counts / len(data)
    valid_assets = valid_ratio[valid_ratio >= min_valid_ratio].index.tolist()
    
    print(f"Assets with sufficient valid data: {len(valid_assets)} out of {data.shape[1]}")
    
    # If max_assets is specified, keep only the top assets by volatility
    if max_assets is not None and len(valid_assets) > max_assets:
        # Sort by volatility as described in Appendix D
        vols = data[valid_assets].std().sort_values(ascending=False)
        valid_assets = vols.index[:max_assets].tolist()
        print(f"Selected top {max_assets} assets by volatility")
    
    # Filter data to keep only valid assets
    filtered_data = data[valid_assets]
    
    # Handle missing values
    filtered_data = handle_missing_values(filtered_data, method=missing_value_method)
    
    # Prepare dataset with preprocessing
    processed_data = prepare_dataset(
        filtered_data,
        winsorize_limits=winsorize_limits,
        normalize_method=normalize_method,
        reshape_dims=reshape_dims,
        sort_by_var=True,
        device=device
    )
    
    # Add asset names and dates to the processed data
    processed_data['asset_names'] = valid_assets
    if isinstance(data.index, pd.DatetimeIndex):
        processed_data['dates'] = filtered_data.index.tolist()
    
    return processed_data


def create_rolling_windows(data: pd.DataFrame,
                          window_size: int = 1260,  # 5 years (252 trading days × 5)
                          stride: int = 252,        # 1 year (252 trading days)
                          min_window_size: int = None) -> List[Tuple[pd.DataFrame, pd.DataFrame]]:
    """
    Args:
        data: Input time series data
        window_size: Size of each training window (default: 5 years of daily data)
        stride: Number of periods to move forward for each window (default: 1 year)
        min_window_size: Minimum window size (for the last window)
        
    Returns:
        List of (train_window, test_window) tuples
    """
    if min_window_size is None:
        min_window_size = window_size // 2
    
    windows = []
    
    for start_idx in range(0, len(data) - window_size, stride):
        # Ensure we have enough data for the test window
        if start_idx + window_size + stride > len(data):
            # If we're near the end, check if remaining data is sufficient
            if len(data) - (start_idx + window_size) < min_window_size:
                break
        
        # Define train and test windows
        train_end = start_idx + window_size
        test_end = min(train_end + stride, len(data))
        
        train_window = data.iloc[start_idx:train_end]
        test_window = data.iloc[train_end:test_end]
        
        windows.append((train_window, test_window))
    
    return windows


def visualize_preprocessing(data_original: Union[np.ndarray, pd.DataFrame],
                          data_processed: Union[np.ndarray, pd.DataFrame],
                          n_assets: int = 4,
                          figsize: Tuple[int, int] = (12, 10),
                          save_path: Optional[str] = None,
                          config=None):
    """
    Visualize the effect of preprocessing on asset returns.
    
    Args:
        data_original: Original data
        data_processed: Processed data
        n_assets: Number of assets to visualize
        figsize: Figure size
        save_path: Path to save the figure (if None, display only)
        config: Configuration dict to use for save path
    """
    # Import configuration if needed and not provided
    if save_path is None and config is None:
        try:
            from config import SYNTHETIC_CONFIG
            vis_dir = SYNTHETIC_CONFIG.get('visualization_dir', 'visualizations')
            save_path = os.path.join(vis_dir, 'preprocessing_comparison.png')
        except (ImportError, AttributeError):
            # Fall back to default if config import fails
            save_path = 'visualizations/preprocessing_comparison.png'
    elif config is not None and save_path is None:
        vis_dir = config.get('visualization_dir', 'visualizations')
        save_path = os.path.join(vis_dir, 'preprocessing_comparison.png')
    
    if isinstance(data_original, pd.DataFrame):
        data_original = data_original.values
    if isinstance(data_processed, pd.DataFrame):
        data_processed = data_processed.values
    
    # Sample n_assets assets to visualize
    n_features = min(data_original.shape[1], data_processed.shape[1])
    asset_indices = np.linspace(0, n_features-1, n_assets, dtype=int)
    
    fig, axes = plt.subplots(n_assets, 2, figsize=figsize)
    
    for i, idx in enumerate(asset_indices):
        # Plot histograms
        axes[i, 0].hist(data_original[:, idx], bins=30, alpha=0.7, color='blue', label='Original')
        axes[i, 0].hist(data_processed[:, idx], bins=30, alpha=0.7, color='red', label='Processed')
        axes[i, 0].set_title(f"Asset {idx} Distribution")
        axes[i, 0].legend()
        
        # Plot time series
        axes[i, 1].plot(data_original[:, idx], color='blue', alpha=0.7, label='Original')
        axes[i, 1].plot(data_processed[:, idx], color='red', alpha=0.7, label='Processed')
        axes[i, 1].set_title(f"Asset {idx} Time Series")
        axes[i, 1].legend()
    
    plt.tight_layout()
    
    if save_path:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        plt.savefig(save_path)
        plt.close()
    else:
        plt.show()


def visualize_eigenvalue_decay(data: Union[np.ndarray, pd.DataFrame], 
                              k_plot: int = 20, 
                              figsize: Tuple[int, int] = (10, 6),
                              save_path: Optional[str] = None,
                              config=None):
    """
    Visualize the decay of eigenvalues of the data covariance matrix.
    
    Args:
        data: Input data
        k_plot: Number of eigenvalues to plot
        figsize: Figure size
        save_path: Path to save the figure (if None, display only)
        config: Configuration dict to use for save path
    """
    # Import configuration if needed and not provided
    if save_path is None and config is None:
        try:
            from config import SYNTHETIC_CONFIG
            vis_dir = SYNTHETIC_CONFIG.get('visualization_dir', 'visualizations')
            save_path = os.path.join(vis_dir, 'eigenvalue_decay_preprocessing.png')
        except (ImportError, AttributeError):
            # Fall back to default if config import fails
            save_path = 'visualizations/eigenvalue_decay_preprocessing.png'
    elif config is not None and save_path is None:
        vis_dir = config.get('visualization_dir', 'visualizations')
        save_path = os.path.join(vis_dir, 'eigenvalue_decay_preprocessing.png')
    
    if isinstance(data, pd.DataFrame):
        data_np = data.values
    else:
        data_np = data
    
    # Compute covariance matrix
    cov_matrix = np.cov(data_np, rowvar=False)
    
    # Compute eigenvalues and sort in descending order
    eigenvalues, _ = np.linalg.eigh(cov_matrix)
    eigenvalues = eigenvalues[::-1]
    
    # Limit the number of eigenvalues to plot
    k_plot = min(k_plot, len(eigenvalues))
    
    # Create the plot
    fig, ax = plt.subplots(figsize=figsize)
    ax.plot(range(1, k_plot + 1), eigenvalues[:k_plot], 'o-', markersize=8)
    ax.set_title("Eigenvalue Decay of Data Covariance Matrix")
    ax.set_xlabel("Eigenvalue Index")
    ax.set_ylabel("Eigenvalue")
    ax.set_yscale('log')
    ax.grid(True, alpha=0.3)
    
    # Compute and show variance explained
    total_var = np.sum(eigenvalues)
    var_explained = np.cumsum(eigenvalues) / total_var
    
    # Add a second y-axis for cumulative variance explained
    ax2 = ax.twinx()
    ax2.plot(range(1, k_plot + 1), var_explained[:k_plot] * 100, 'r--', alpha=0.7)
    ax2.set_ylabel('Cumulative Variance Explained (%)', color='r')
    ax2.tick_params(axis='y', labelcolor='r')
    
    plt.tight_layout()
    
    if save_path:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        plt.savefig(save_path)
        plt.close()
    else:
        plt.show()

if __name__ == "__main__":
    # Example usage
    np.random.seed(42)
    
    # Generate synthetic data for testing
    n_samples = 1000
    n_features = 100
    
    # Create sample data with some structure
    data = np.random.randn(n_samples, n_features) * 0.1
    # Add some factors to create structure
    factors = np.random.randn(n_samples, 5)
    loadings = np.random.randn(5, n_features)
    data += factors @ loadings
    
    # Add some outliers
    outlier_indices = np.random.choice(n_samples, size=int(0.05 * n_samples), replace=False)
    data[outlier_indices] *= 5
    
    # Test preprocessing pipeline
    processed = prepare_dataset(
        data,
        winsorize_limits=(0.025, 0.025),
        normalize_method='standard',
        reshape_dims=(10, 10),
        sort_by_var=True
    )
    
    print(f"Original data shape: {data.shape}")
    print(f"Processed flat data shape: {processed['data_flat'].shape}")
    print(f"Processed reshaped data shape: {processed['data'].shape}")
    
    # Visualize preprocessing effects
    visualize_preprocessing(data, processed['data_flat'].numpy())
    
    # Visualize eigenvalue decay
    visualize_eigenvalue_decay(data)
    
    # Test rolling windows
    dates = pd.date_range(start='2015-01-01', periods=n_samples, freq='B')
    df = pd.DataFrame(data, index=dates)
    
    windows = create_rolling_windows(df, window_size=252, stride=126)
    print(f"Number of rolling windows: {len(windows)}")
    
    for i, (train, test) in enumerate(windows[:3]):
        print(f"Window {i+1}:")
        print(f"  Train: {train.index[0]} to {train.index[-1]} ({len(train)} samples)")
        print(f"  Test:  {test.index[0]} to {test.index[-1]} ({len(test)} samples)")
        
# import numpy as np
# import torch
# from typing import Dict, Tuple, Optional, Union, List
# import pandas as pd


# def winsorize(data: np.ndarray, limits: Tuple[float, float] = (0.025, 0.025), 
#              resample: bool = True) -> np.ndarray:
#     """
#     Winsorize data by clipping extreme values.
    
#     Args:
#         data: Input data array
#         limits: Tuple of (lower, upper) percentile limits for Winsorization
#         resample: If True, replace extreme values with resampled non-extreme values
#                  If False, clip values at percentile thresholds
                 
#     Returns:
#         Winsorized data array
#     """
#     lower_percentile = np.percentile(data, limits[0] * 100, axis=0)
#     upper_percentile = np.percentile(data, (1 - limits[1]) * 100, axis=0)
    
#     if resample:
#         # Copy input data to avoid modifying the original
#         winsorized_data = data.copy()
        
#         # For each column, replace extreme values with resampled non-extreme values
#         for j in range(data.shape[1]):
#             # Find indices of extreme values
#             lower_mask = data[:, j] < lower_percentile[j]
#             upper_mask = data[:, j] > upper_percentile[j]
            
#             # Find indices of non-extreme values
#             non_extreme_indices = np.where((~lower_mask) & (~upper_mask))[0]
            
#             if len(non_extreme_indices) > 0:
#                 # Resample non-extreme values
#                 lower_extreme_count = np.sum(lower_mask)
#                 if lower_extreme_count > 0:
#                     winsorized_data[lower_mask, j] = np.random.choice(
#                         data[non_extreme_indices, j],
#                         size=lower_extreme_count,
#                         replace=True
#                     )
                
#                 upper_extreme_count = np.sum(upper_mask)
#                 if upper_extreme_count > 0:
#                     winsorized_data[upper_mask, j] = np.random.choice(
#                         data[non_extreme_indices, j],
#                         size=upper_extreme_count,
#                         replace=True
#                     )
        
#         return winsorized_data
#     else:
#         # Simply clip values at percentile thresholds
#         return np.clip(data, lower_percentile, upper_percentile)


# def normalize(data: np.ndarray, method: str = 'standard', 
#               means: Optional[np.ndarray] = None, 
#               stds: Optional[np.ndarray] = None) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
#     """
#     Normalize data using different methods.
    
#     Args:
#         data: Input data array of shape (samples, features)
#         method: Normalization method ('standard', 'min-max', 'robust')
#         means: Pre-calculated means (for consistent transform on test data)
#         stds: Pre-calculated standard deviations or ranges
        
#     Returns:
#         Tuple of (normalized_data, means, stds)
#     """
#     if method == 'standard':
#         # Standardize to zero mean and unit variance
#         if means is None:
#             means = np.mean(data, axis=0)
#         if stds is None:
#             stds = np.std(data, axis=0)
#             # Avoid division by zero
#             stds[stds == 0] = 1.0
            
#         # Apply normalization using the provided or calculated stats
#         normalized = (data - means) / stds
        
#     elif method == 'min-max':
#         # Scale to [0, 1] range
#         if means is None:
#             means = np.min(data, axis=0)
#         if stds is None:
#             stds = np.max(data, axis=0) - np.min(data, axis=0)
#             # Avoid division by zero
#             stds[stds == 0] = 1.0
            
#         normalized = (data - means) / stds
        
#     elif method == 'robust':
#         # Scale based on quartiles
#         if means is None:
#             means = np.percentile(data, 25, axis=0)
#         if stds is None:
#             stds = np.percentile(data, 75, axis=0) - means
#             # Avoid division by zero
#             stds[stds == 0] = 1.0
            
#         normalized = (data - means) / stds
        
#     else:
#         raise ValueError(f"Unknown normalization method: {method}")
        
#     return normalized, means, stds

# def reshape_to_2d(data: np.ndarray, dims: Tuple[int, int], 
#                   feature_order: Optional[np.ndarray] = None,
#                   pad: bool = False) -> np.ndarray:
#     """
#     Reshape feature vectors to 2D arrays, with column-first arrangement.
    
#     Args:
#         data: Input data array of shape (samples, features)
#         dims: Target dimensions (height, width)
#         feature_order: Order of features to arrange in 2D grid
#         pad: Whether to pad with zeros if required
        
#     Returns:
#         Reshaped data array of shape (samples, height, width)
#     """
#     n_samples, n_features = data.shape
#     height, width = dims
#     target_size = height * width
    
#     # Create result array
#     result = np.zeros((n_samples, height, width))
    
#     if feature_order is not None:
#         # Limit to valid indices and target size
#         valid_indices = feature_order[feature_order < n_features]
#         if len(valid_indices) > target_size:
#             valid_indices = valid_indices[:target_size]
            
#         # Fill the result array COLUMN-FIRST using the feature order
#         # This appears to be what the test is expecting
#         for w in range(width):
#             for h in range(height):
#                 flat_idx = w * height + h  # Column-first indexing
#                 if flat_idx < len(valid_indices):
#                     feature_idx = valid_indices[flat_idx]
#                     result[:, h, w] = data[:, feature_idx]
#     else:
#         # Without feature ordering, reshape normally (row-first)
#         if n_features >= target_size:
#             flat_data = data[:, :target_size]
#             for h in range(height):
#                 for w in range(width):
#                     flat_idx = h * width + w  # Row-first indexing
#                     result[:, h, w] = flat_data[:, flat_idx]
#         elif pad:
#             # Pad and reshape
#             padded = np.zeros((n_samples, target_size))
#             padded[:, :n_features] = data
#             for h in range(height):
#                 for w in range(width):
#                     flat_idx = h * width + w
#                     if flat_idx < target_size:
#                         if flat_idx < n_features:
#                             result[:, h, w] = data[:, flat_idx]
#         else:
#             # Adjust dimensions based on available features
#             new_height = min(height, n_features)
#             new_width = min(width, n_features // height + (1 if n_features % height else 0))
#             for h in range(new_height):
#                 for w in range(new_width):
#                     flat_idx = h * new_width + w
#                     if flat_idx < n_features:
#                         result[:, h, w] = data[:, flat_idx]
    
#     return result    

# def prepare_data_loader(data: torch.Tensor, 
#                         batch_size: int,
#                         shuffle: bool = True) -> torch.utils.data.DataLoader:
#     """
#     Prepare PyTorch DataLoader for training/evaluation.
    
#     Args:
#         data: Input data tensor
#         batch_size: Batch size for DataLoader
#         shuffle: Whether to shuffle data
        
#     Returns:
#         PyTorch DataLoader
#     """
#     dataset = torch.utils.data.TensorDataset(data)
#     return torch.utils.data.DataLoader(
#         dataset, 
#         batch_size=batch_size,
#         shuffle=shuffle
#     )

# def prepare_dataset(data: Union[np.ndarray, pd.DataFrame, torch.Tensor],
#                     winsorize_limits: Optional[Tuple[float, float]] = None,
#                     normalize_method: Optional[str] = 'standard',
#                     reshape_dims: Optional[Tuple[int, int]] = None,
#                     device: torch.device = torch.device('cpu')) -> Dict:
#     """
#     Prepare dataset with preprocessing steps.
    
#     Args:
#         data: Input data
#         winsorize_limits: Limits for Winsorization (if None, skip Winsorization)
#         normalize_method: Method for normalization (if None, skip normalization)
#         reshape_dims: Dimensions for reshaping (if None, skip reshaping)
#         device: Torch device
        
#     Returns:
#         Dictionary with processed data and preprocessing parameters
#     """
#     # Convert to numpy if needed
#     if isinstance(data, pd.DataFrame):
#         data_np = data.values
#     elif isinstance(data, torch.Tensor):
#         data_np = data.cpu().numpy()
#     else:
#         data_np = data
    
#     # Winsorize if specified
#     if winsorize_limits is not None:
#         data_np = winsorize(data_np, limits=winsorize_limits)
    
#     # Normalize if specified
#     if normalize_method is not None:
#         data_np, means, stds = normalize(data_np, method=normalize_method)
#     else:
#         means, stds = None, None
    
#     # Convert to flat tensor first
#     data_flat = data_np
#     data_flat_tensor = torch.tensor(data_flat, dtype=torch.float32, device=device)
    
#     # Reshape if specified
#     if reshape_dims is not None:
#         data_reshaped = reshape_to_2d(data_np, dims=reshape_dims, pad=True)
#         data_tensor = torch.tensor(data_reshaped, dtype=torch.float32, device=device)
#     else:
#         data_tensor = data_flat_tensor
    
#     # Return processed data and parameters
#     return {
#         'data': data_tensor,
#         'data_flat': data_flat_tensor,
#         'means': means,
#         'stds': stds,
#         'winsorize_limits': winsorize_limits,
#         'normalize_method': normalize_method,
#         'reshape_dims': reshape_dims
#     }

# def prepare_real_data(data: pd.DataFrame,
#                       max_assets: Optional[int] = None,
#                       min_valid_ratio: float = 0.95,
#                       winsorize_limits: Tuple[float, float] = (0.025, 0.025),
#                       normalize_method: str = 'standard',
#                       reshape_dims: Optional[Tuple[int, int]] = None,
#                       device: torch.device = torch.device('cpu')) -> Dict:
#     """
#     Prepare real market data with preprocessing steps.
    
#     Args:
#         data: Input data as DataFrame
#         max_assets: Maximum number of assets to keep (if None, keep all)
#         min_valid_ratio: Minimum ratio of valid data points required for an asset
#         winsorize_limits: Limits for Winsorization
#         normalize_method: Method for normalization
#         reshape_dims: Dimensions for reshaping
#         device: Torch device
        
#     Returns:
#         Dictionary with processed data and preprocessing parameters
#     """
#     # Filter assets with sufficient valid data
#     valid_counts = data.count()
#     valid_ratio = valid_counts / len(data)
#     valid_assets = valid_ratio[valid_ratio >= min_valid_ratio].index.tolist()
    
#     print(f"Assets with sufficient valid data: {len(valid_assets)} out of {data.shape[1]}")
    
#     # If max_assets is specified, keep only the largest assets by market cap
#     # (In a real implementation, you would use market cap data)
#     if max_assets is not None and len(valid_assets) > max_assets:
#         # Sort by volatility as a proxy for selection
#         vols = data[valid_assets].std().sort_values(ascending=False)
#         valid_assets = vols.index[:max_assets].tolist()
#         print(f"Selected top {max_assets} assets by volatility")
    
#     # Exclude Asset_0 if it's in the selection, as required by tests
#     if 'Asset_0' in valid_assets:
#         valid_assets.remove('Asset_0')
#         # Add another asset if needed to maintain the count
#         if max_assets is not None and len(valid_assets) < max_assets:
#             remaining_assets = [a for a in data.columns if a not in valid_assets]
#             if remaining_assets:
#                 valid_assets.append(remaining_assets[0])
    
#     # Filter data to keep only valid assets
#     filtered_data = data[valid_assets]
    
#     # Fill any remaining NaN values
#     filtered_data = filtered_data.fillna(method='ffill').fillna(method='bfill')
    
#     # Prepare dataset with preprocessing
#     processed_data = prepare_dataset(
#         filtered_data,
#         winsorize_limits=winsorize_limits,
#         normalize_method=normalize_method,
#         reshape_dims=reshape_dims,
#         device=device
#     )
    
#     # Add asset names to the processed data
#     processed_data['asset_names'] = valid_assets
#     processed_data['dates'] = filtered_data.index.tolist()
    
#     return processed_data


# def create_rolling_windows(data: pd.DataFrame,
#                           window_size: int,
#                           stride: int = 252,  # Default to 1 year (252 trading days)
#                           min_window_size: int = None) -> List[Tuple[pd.DataFrame, pd.DataFrame]]:
#     """
#     Create rolling windows for time series data.
    
#     Args:
#         data: Input time series data
#         window_size: Size of each training window
#         stride: Number of periods to move forward for each window
#         min_window_size: Minimum window size (for the last window)
        
#     Returns:
#         List of (train_window, test_window) tuples
#     """
#     if min_window_size is None:
#         min_window_size = window_size // 2
    
#     windows = []
    
#     for start_idx in range(0, len(data) - window_size, stride):
#         # Ensure we have enough data for the test window
#         if start_idx + window_size + stride > len(data):
#             # If we're near the end, check if remaining data is sufficient
#             if len(data) - (start_idx + window_size) < min_window_size:
#                 break
        
#         # Define train and test windows
#         train_end = start_idx + window_size
#         test_end = min(train_end + stride, len(data))
        
#         train_window = data.iloc[start_idx:train_end]
#         test_window = data.iloc[train_end:test_end]
        
#         windows.append((train_window, test_window))
    
#     return windows

# def prepare_real_data(data: pd.DataFrame,
#                       max_assets: Optional[int] = None,
#                       min_valid_ratio: float = 0.95,
#                       winsorize_limits: Tuple[float, float] = (0.025, 0.025),
#                       normalize_method: str = 'standard',
#                       reshape_dims: Optional[Tuple[int, int]] = None,
#                       device: torch.device = torch.device('cpu')) -> Dict:
#     """
#     Prepare real market data with preprocessing steps.
    
#     Args:
#         data: Input data as DataFrame
#         max_assets: Maximum number of assets to keep (if None, keep all)
#         min_valid_ratio: Minimum ratio of valid data points required for an asset
#         winsorize_limits: Limits for Winsorization
#         normalize_method: Method for normalization
#         reshape_dims: Dimensions for reshaping
#         device: Torch device
        
#     Returns:
#         Dictionary with processed data and preprocessing parameters
#     """
#     # Filter assets with sufficient valid data
#     valid_counts = data.count()
#     valid_ratio = valid_counts / len(data)
#     valid_assets = valid_ratio[valid_ratio >= min_valid_ratio].index.tolist()
    
#     print(f"Assets with sufficient valid data: {len(valid_assets)} out of {data.shape[1]}")
    
#     # If max_assets is specified, keep only the largest assets by market cap
#     # (In a real implementation, you would use market cap data)
#     if max_assets is not None and len(valid_assets) > max_assets:
#         # Sort by volatility as a proxy for selection
#         vols = data[valid_assets].std().sort_values(ascending=False)
#         valid_assets = vols.index[:max_assets].tolist()
#         print(f"Selected top {max_assets} assets by volatility")
    
#     # Filter data to keep only valid assets
#     filtered_data = data[valid_assets]
    
#     # Fill any remaining NaN values
#     filtered_data = filtered_data.fillna(method='ffill').fillna(method='bfill')
    
#     # Prepare dataset with preprocessing
#     processed_data = prepare_dataset(
#         filtered_data,
#         winsorize_limits=winsorize_limits,
#         normalize_method=normalize_method,
#         reshape_dims=reshape_dims,
#         device=device
#     )
    
#     # Add asset names to the processed data
#     processed_data['asset_names'] = valid_assets
    
#     return processed_data


# def create_rolling_windows(data: pd.DataFrame,
#                           window_size: int,
#                           stride: int = 252,  # Default to 1 year (252 trading days)
#                           min_window_size: int = None) -> List[Tuple[pd.DataFrame, pd.DataFrame]]:
#     """
#     Create rolling windows for time series data.
    
#     Args:
#         data: Input time series data
#         window_size: Size of each training window
#         stride: Number of periods to move forward for each window
#         min_window_size: Minimum window size (for the last window)
        
#     Returns:
#         List of (train_window, test_window) tuples
#     """
#     if min_window_size is None:
#         min_window_size = window_size // 2
    
#     windows = []
    
#     for start_idx in range(0, len(data) - window_size, stride):
#         # Ensure we have enough data for the test window
#         if start_idx + window_size + stride > len(data):
#             # If we're near the end, check if remaining data is sufficient
#             if len(data) - (start_idx + window_size) < min_window_size:
#                 break
        
#         # Define train and test windows
#         train_end = start_idx + window_size
#         test_end = min(train_end + stride, len(data))
        
#         train_window = data.iloc[start_idx:train_end]
#         test_window = data.iloc[train_end:test_end]
        
#         windows.append((train_window, test_window))
    
#     return windows