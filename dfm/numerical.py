"""
Numerical utilities for the Diffusion Factor Model.

This module provides helper functions for numerical stability and computations.
"""

import numpy as np
import torch
from typing import Tuple, Optional, Union, List


def ensure_positive_definite(matrix: Union[np.ndarray, torch.Tensor], 
                           epsilon: float = 1e-6) -> Union[np.ndarray, torch.Tensor]:
    """
    Ensure a matrix is positive definite by adding a small constant to the diagonal if needed.
    
    Args:
        matrix: Input matrix (covariance)
        epsilon: Small constant to add to diagonal if needed
        
    Returns:
        Positive definite matrix
    """
    if isinstance(matrix, torch.Tensor):
        # Compute smallest eigenvalue
        try:
            eigenvalues = torch.linalg.eigvalsh(matrix)
            min_eig = torch.min(eigenvalues)
            
            # If smallest eigenvalue is too small, add epsilon to diagonal
            if min_eig < epsilon:
                adjustment = epsilon - min_eig
                matrix = matrix + adjustment * torch.eye(matrix.shape[0], device=matrix.device)
                
        except torch.linalg.LinAlgError:
            # If eigendecomposition fails, just add epsilon to diagonal
            matrix = matrix + epsilon * torch.eye(matrix.shape[0], device=matrix.device)
            
    else:  # numpy array
        # Compute smallest eigenvalue
        try:
            eigenvalues = np.linalg.eigvalsh(matrix)
            min_eig = np.min(eigenvalues)
            
            # If smallest eigenvalue is too small, add epsilon to diagonal
            if min_eig < epsilon:
                adjustment = epsilon - min_eig
                matrix = matrix + adjustment * np.eye(matrix.shape[0])
                
        except np.linalg.LinAlgError:
            # If eigendecomposition fails, just add epsilon to diagonal
            matrix = matrix + epsilon * np.eye(matrix.shape[0])
            
    return matrix


def stable_correlation_matrix(covariance: Union[np.ndarray, torch.Tensor],
                            epsilon: float = 1e-6) -> Union[np.ndarray, torch.Tensor]:
    """
    Compute stable correlation matrix from covariance.
    
    Args:
        covariance: Covariance matrix
        epsilon: Small constant for numerical stability
        
    Returns:
        Correlation matrix
    """
    if isinstance(covariance, torch.Tensor):
        # Get standard deviations
        std = torch.sqrt(torch.diag(covariance) + epsilon)
        # Compute correlation matrix
        corr = covariance / torch.outer(std, std)
        # Ensure diagonal is exactly 1
        corr.fill_diagonal_(1.0)
        
    else:  # numpy array
        # Get standard deviations
        std = np.sqrt(np.diag(covariance) + epsilon)
        # Compute correlation matrix
        corr = covariance / np.outer(std, std)
        # Ensure diagonal is exactly 1
        np.fill_diagonal(corr, 1.0)
        
    return corr


def stable_inverse(matrix: Union[np.ndarray, torch.Tensor], 
                 epsilon: float = 1e-6) -> Union[np.ndarray, torch.Tensor]:
    """
    Compute stable matrix inverse with regularization.
    
    Args:
        matrix: Input matrix
        epsilon: Regularization parameter
        
    Returns:
        Inverse matrix
    """
    if isinstance(matrix, torch.Tensor):
        # First ensure matrix is positive definite
        matrix = ensure_positive_definite(matrix, epsilon)
        
        # Compute inverse
        try:
            inverse = torch.linalg.inv(matrix)
        except torch.linalg.LinAlgError:
            # If inversion fails, use pseudo-inverse
            inverse = torch.linalg.pinv(matrix)
            
    else:  # numpy array
        # First ensure matrix is positive definite
        matrix = ensure_positive_definite(matrix, epsilon)
        
        # Compute inverse
        try:
            inverse = np.linalg.inv(matrix)
        except np.linalg.LinAlgError:
            # If inversion fails, use pseudo-inverse
            inverse = np.linalg.pinv(matrix)
            
    return inverse


def orthogonalize_columns(matrix: Union[np.ndarray, torch.Tensor], 
                         normalize: bool = True) -> Union[np.ndarray, torch.Tensor]:
    """
    Orthogonalize columns of a matrix using QR decomposition.
    
    Args:
        matrix: Input matrix
        normalize: Whether to normalize columns
        
    Returns:
        Matrix with orthogonal columns
    """
    if isinstance(matrix, torch.Tensor):
        # Use QR decomposition
        q, r = torch.linalg.qr(matrix)
        
        if normalize:
            return q
        else:
            # Get signs from diagonal of R
            signs = torch.sign(torch.diag(r))
            # Apply signs to Q
            return q * signs.unsqueeze(0)
            
    else:  # numpy array
        # Use QR decomposition
        q, r = np.linalg.qr(matrix)
        
        if normalize:
            return q
        else:
            # Get signs from diagonal of R
            signs = np.sign(np.diag(r))
            # Apply signs to Q
            return q * signs[np.newaxis, :]


def stable_cholesky(covariance: Union[np.ndarray, torch.Tensor], 
                  epsilon: float = 1e-6) -> Union[np.ndarray, torch.Tensor]:
    """
    Compute stable Cholesky decomposition.
    
    Args:
        covariance: Covariance matrix
        epsilon: Small constant for numerical stability
        
    Returns:
        Cholesky factor (lower triangular)
    """
    if isinstance(covariance, torch.Tensor):
        # Ensure matrix is positive definite
        covariance = ensure_positive_definite(covariance, epsilon)
        
        # Compute Cholesky decomposition
        try:
            chol = torch.linalg.cholesky(covariance)
        except torch.linalg.LinAlgError:
            # If Cholesky fails, try with more regularization
            covariance = ensure_positive_definite(covariance, epsilon * 10)
            chol = torch.linalg.cholesky(covariance)
            
    else:  # numpy array
        # Ensure matrix is positive definite
        covariance = ensure_positive_definite(covariance, epsilon)
        
        # Compute Cholesky decomposition
        try:
            chol = np.linalg.cholesky(covariance)
        except np.linalg.LinAlgError:
            # If Cholesky fails, try with more regularization
            covariance = ensure_positive_definite(covariance, epsilon * 10)
            chol = np.linalg.cholesky(covariance)
            
    return chol


def trace_covariance_product(cov1: Union[np.ndarray, torch.Tensor],
                          cov2: Union[np.ndarray, torch.Tensor]) -> Union[float, torch.Tensor]:
    """
    Efficiently compute trace(cov1 @ cov2) for covariance matrices.
    
    Args:
        cov1: First covariance matrix
        cov2: Second covariance matrix
        
    Returns:
        Trace of matrix product
    """
    if isinstance(cov1, torch.Tensor) and isinstance(cov2, torch.Tensor):
        # Element-wise product and sum
        return torch.sum(cov1 * cov2)
    else:
        # Convert to numpy if needed
        if isinstance(cov1, torch.Tensor):
            cov1 = cov1.cpu().numpy()
        if isinstance(cov2, torch.Tensor):
            cov2 = cov2.cpu().numpy()
            
        # Element-wise product and sum
        return np.sum(cov1 * cov2)


def condition_number(matrix: Union[np.ndarray, torch.Tensor]) -> Union[float, torch.Tensor]:
    """
    Compute condition number of a matrix.
    
    Args:
        matrix: Input matrix
        
    Returns:
        Condition number
    """
    if isinstance(matrix, torch.Tensor):
        # Compute eigenvalues
        eigenvalues = torch.linalg.eigvalsh(matrix)
        
        # Compute condition number as ratio of largest to smallest eigenvalue
        return torch.max(eigenvalues) / torch.max(torch.tensor([torch.min(eigenvalues), 
                                                             torch.tensor(1e-10, device=eigenvalues.device)]))
    else:  # numpy array
        # Compute eigenvalues
        eigenvalues = np.linalg.eigvalsh(matrix)
        
        # Compute condition number as ratio of largest to smallest eigenvalue
        return np.max(eigenvalues) / max(np.min(eigenvalues), 1e-10)


def soft_thresholding(matrix: Union[np.ndarray, torch.Tensor],
                    threshold: float) -> Union[np.ndarray, torch.Tensor]:
    """
    Apply soft thresholding to a matrix.
    
    Args:
        matrix: Input matrix
        threshold: Threshold value
        
    Returns:
        Thresholded matrix
    """
    if isinstance(matrix, torch.Tensor):
        # Get sign of matrix elements
        sign = torch.sign(matrix)
        # Apply soft thresholding
        return sign * torch.maximum(torch.abs(matrix) - threshold, torch.tensor(0.0, device=matrix.device))
    else:  # numpy array
        # Get sign of matrix elements
        sign = np.sign(matrix)
        # Apply soft thresholding
        return sign * np.maximum(np.abs(matrix) - threshold, 0.0)


def shrink_covariance(sample_cov: Union[np.ndarray, torch.Tensor],
                    target: Optional[Union[np.ndarray, torch.Tensor]] = None,
                    alpha: Optional[float] = None) -> Tuple[Union[np.ndarray, torch.Tensor], float]:
    """
    Apply shrinkage to a sample covariance matrix.
    
    Args:
        sample_cov: Sample covariance matrix
        target: Target matrix for shrinkage (if None, use scaled identity)
        alpha: Shrinkage intensity (if None, estimate optimally)
        
    Returns:
        Tuple of (shrunk_covariance, shrinkage_intensity)
    """
    if isinstance(sample_cov, torch.Tensor):
        # Default target is scaled identity
        if target is None:
            target = torch.eye(sample_cov.shape[0], device=sample_cov.device) * torch.mean(torch.diag(sample_cov))
            
        # Estimate optimal shrinkage intensity if not provided
        if alpha is None:
            # Compute Frobenius norm of difference
            frob_norm_squared = torch.sum((sample_cov - target)**2)
            
            # Estimate optimal intensity (simplified)
            var_sample = torch.mean(torch.diag(sample_cov)**2)
            alpha = min(1.0, frob_norm_squared / (frob_norm_squared + var_sample))
            
        # Apply shrinkage
        shrunk_cov = (1 - alpha) * sample_cov + alpha * target
        
    else:  # numpy array
        # Default target is scaled identity
        if target is None:
            target = np.eye(sample_cov.shape[0]) * np.mean(np.diag(sample_cov))
            
        # Estimate optimal shrinkage intensity if not provided
        if alpha is None:
            # Compute Frobenius norm of difference
            frob_norm_squared = np.sum((sample_cov - target)**2)
            
            # Estimate optimal intensity (simplified)
            var_sample = np.mean(np.diag(sample_cov)**2)
            alpha = min(1.0, frob_norm_squared / (frob_norm_squared + var_sample))
            
        # Apply shrinkage
        shrunk_cov = (1 - alpha) * sample_cov + alpha * target
        
    return shrunk_cov, alpha

def prepare_data_for_diffusion(
    data: Union[np.ndarray, torch.Tensor], 
    expected_format: str = "vector"
) -> torch.Tensor:
    """
    Prepare input data for the diffusion model according to the paper's requirements.
    
    The Diffusion Factor Model paper works with asset returns as d-dimensional vectors,
    where d is the number of assets. The diffusion process operates on these vectors.
    
    Args:
        data: Input data as numpy array or torch tensor
        expected_format: Expected format, either "vector" for [batch_size, d] 
                         or "matrix" for [batch_size, d1, d2]
        
    Returns:
        Properly formatted torch tensor
    """
    # Convert to torch tensor if numpy array
    if isinstance(data, np.ndarray):
        data = torch.from_numpy(data).float()
    
    # Ensure data is float type
    if not torch.is_floating_point(data):
        data = data.float()
    
    # Check and adjust dimensionality
    if expected_format == "vector":
        # Paper requires [batch_size, d] format
        if len(data.shape) == 1:
            # Single sample, add batch dimension
            data = data.unsqueeze(0)
        elif len(data.shape) > 2:
            # Too many dimensions, flatten all except batch
            batch_size = data.shape[0]
            data = data.reshape(batch_size, -1)
            print(f"Flattened data from shape {data.shape} to {batch_size}×{data.shape[1]}")
    elif expected_format == "matrix":
        # If a matrix format is explicitly required
        if len(data.shape) == 2:
            # Need to reshape 2D to 3D
            # This is application-specific, so just warn
            print(f"Data has shape {data.shape} but matrix format requires 3D tensor.")
        elif len(data.shape) > 3:
            # Too many dimensions
            print(f"Data has {len(data.shape)} dimensions, expected 3D tensor for matrix format.")
    else:
        raise ValueError(f"Unknown expected_format: {expected_format}")
    
    return data

if __name__ == "__main__":
    # Test numerical utilities
    import numpy as np
    
    # Create a test matrix
    n = 5
    A = np.random.randn(n, n)
    A = A @ A.T  # Make it symmetric
    
    # Test ensure_positive_definite
    A_pd = ensure_positive_definite(A)
    print("Eigenvalues after ensure_positive_definite:", np.linalg.eigvalsh(A_pd))
    
    # Test stable_correlation_matrix
    corr = stable_correlation_matrix(A)
    print("Diagonal of correlation matrix:", np.diag(corr))
    
    # Test orthogonalize_columns
    B = np.random.randn(n, 3)
    B_orth = orthogonalize_columns(B)
    print("B_orth.T @ B_orth:", B_orth.T @ B_orth)
    
    # Test shrink_covariance
    shrunk_cov, alpha = shrink_covariance(A)
    print(f"Optimal shrinkage intensity: {alpha:.4f}")
