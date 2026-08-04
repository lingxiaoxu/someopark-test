"""
Diffusion process implementation for the Diffusion Factor Model.

This module implements the Ornstein-Uhlenbeck (O-U) forward and reverse processes
as described in the paper, including time-discretization methods and factor model specifics.
"""

import numpy as np
import torch
from typing import Tuple, Callable, Dict, Optional, List, Union, Any
from tqdm import tqdm


class DiffusionProcess:
    """
    Implementation of the Ornstein-Uhlenbeck (O-U) diffusion process with factor structure.
    
    The forward process is:
    dR_t = -0.5 * η(t) * R_t * dt + sqrt(η(t)) * dW_t, R_0 ~ P_data, t ∈ [0, T]
    
    The reverse process is:
    dR_t^← = (0.5 * R_t^← + ∇log p_{T-t}(R_t^←)) * dt + dW_t^←, R_0^← ~ P_T, t ∈ [0, T]
    
    The factor model structure is:
    R = β F + ε, where β is the factor loading matrix, F are factors, and ε is residual noise.
    """
    
    def __init__(
        self,
        data_dim: int = 100,
        factor_dim: int = 5,
        T: float = 1.0,
        t0: Optional[float] = None,
        eta: Union[float, Callable[[float], float]] = 1.0,
        num_steps: int = 200,
        sigma_diag: Optional[torch.Tensor] = None,
        beta: Optional[torch.Tensor] = None,
        num_samples: Optional[int] = None,
        device: torch.device = torch.device('cpu'),
        infer_dims_from_data: bool = True
    ):
        """
        Initialize the diffusion factor model.
        
        Args:
            data_dim: Dimension of the asset returns
            factor_dim: Number of latent factors (k)
            T: Terminal time
            t0: Early stopping time (if None, computed based on num_samples)
            eta: Noise schedule function η(t) or constant
            num_steps: Number of discretized steps
            sigma_diag: Diagonal of residual noise covariance
            beta: Factor loading matrix
            num_samples: Number of training samples (used to set t0)
            device: Torch device
        """
        self.data_dim = data_dim
        self.factor_dim = factor_dim
        self.device = device
        self.num_steps = num_steps
        self.infer_dims_from_data = infer_dims_from_data
        
        # Set t0 based on paper recommendation if not provided
        if t0 is None and num_samples is not None:
            # Calculate t0 according to Theorem 3
            delta_n = (factor_dim + 10) * np.log(np.log(num_samples)) / (2 * np.log(num_samples))
            # t0 = n^(-(1-δ(n))/(k+5))
            self.t0 = num_samples ** (-(1 - delta_n) / (factor_dim + 5))
            # Apply reasonable bounds for numerical stability
            self.t0 = min(0.3, max(0.01, self.t0))
        else:
            self.t0 = t0 or 0.01
        
        # Set T based on paper recommendation
        if num_samples is not None:
            delta_n = (factor_dim + 10) * np.log(np.log(num_samples)) / (2 * np.log(num_samples))
            gamma_1 = 20 * factor_dim  # Using approximate value from Theorem 1
            computed_T = ((4 * gamma_1 + 2) * (1 - delta_n)) / (factor_dim + 5) * np.log(num_samples)
            self.T = max(1.0, computed_T) * (1 + 0.1 * np.log10(num_samples))
        else:
            self.T = T
            
        # Define η(t) function
        if isinstance(eta, float):
            self.eta = lambda t: eta
        else:
            self.eta = eta

        # Factor model specific parameters - will be initialized or updated when first data batch is processed
        self._init_factor_model(sigma_diag, beta)
        
        # Initialize time steps
        self._init_time_steps()
        
    def _init_factor_model(self, sigma_diag=None, beta=None):
        """Initialize factor model parameters"""
        if sigma_diag is None:
            # Default to homogeneous noise
            self.sigma_diag = torch.ones(self.data_dim, device=self.device)
        else:
            self.sigma_diag = sigma_diag.to(self.device)
        
        if beta is None:
            # Initialize with orthonormal columns if not provided
            self.beta = torch.randn(self.data_dim, self.factor_dim, device=self.device)
            q, r = torch.linalg.qr(self.beta)
            self.beta = q[:, :self.factor_dim]
        else:
            self.beta = beta.to(self.device)

    def _init_time_steps(self):
        """Initialize discretized time steps"""
        # Discretize time
        self.time_steps = torch.linspace(self.t0, self.T, self.num_steps, device=self.device)
        
        # Precompute coefficients for the forward SDE
        self.alpha_t = torch.empty(self.num_steps, device=self.device)
        self.h_t = torch.empty(self.num_steps, device=self.device)
        
        for i, t in enumerate(self.time_steps):
            # α_t = exp(-∫_0^t 0.5 * η(s) ds)
            # Simplified for η(t) = constant: α_t = exp(-0.5 * η * t)
            self.alpha_t[i] = torch.exp(-0.5 * self.eta(t.item()) * t)
            
            # h_t = 1 - α_t^2
            self.h_t[i] = 1 - self.alpha_t[i]**2

    def update_dims_from_data(self, data):
        """
        Update dimensions based on the first batch of data if needed.
        
        Args:
            data: Input data tensor [batch_size, ...]
        """
        if not self.infer_dims_from_data:
            return
        
        # Get actual data dimension
        if len(data.shape) > 2:
            # For 3D+ tensors, flatten all except batch dimension
            batch_size = data.shape[0]
            actual_dim = np.prod(data.shape[1:])
            # print(f"Input data has shape {data.shape}, flattening to [batch_size, {actual_dim}] for diffusion process.")
        else:
            actual_dim = data.shape[1]
        
        # Update data_dim if necessary
        if actual_dim != self.data_dim:
            old_dim = self.data_dim
            self.data_dim = int(actual_dim)
            print(f"Updated data_dim from {old_dim} to {self.data_dim} based on input data.")
            
            # Re-initialize factor model parameters
            self._init_factor_model()
    
    def compute_lambda_t(self, t: Union[float, torch.Tensor]) -> torch.Tensor:
        """
        Compute Λ_t as defined in equation (10).
        
        Args:
            t: Time point(s)
            
        Returns:
            Λ_t diagonal matrix elements
        """
        if isinstance(t, float):
            t = torch.tensor([t], device=self.device)
        
        alpha_t, h_t = self.get_alpha_h(t)
        
        # Λ_t := diag{h_t + σ_1^2 * α_t^2, h_t + σ_2^2 * α_t^2, ..., h_t + σ_d^2 * α_t^2}
        return h_t + (alpha_t**2) * self.sigma_diag
    
    def compute_projection_t(self, t: Union[float, torch.Tensor]) -> torch.Tensor:
        """
        Compute T_t as defined in equation (11).
        
        Args:
            t: Time point(s)
            
        Returns:
            T_t projection matrix
        """
        lambda_t = self.compute_lambda_t(t)
        lambda_sqrt_inv = 1.0 / torch.sqrt(lambda_t)
        
        # Γ_t := (β^T Λ_t^{-1} β)^{-1}
        beta_lambda_inv = self.beta.T @ torch.diag(1.0 / lambda_t)
        gamma_t = torch.inverse(beta_lambda_inv @ self.beta)
        
        # T_t := Λ_t^{-1/2} β Γ_t β^T Λ_t^{-1/2}
        return torch.diag(lambda_sqrt_inv) @ self.beta @ gamma_t @ self.beta.T @ torch.diag(lambda_sqrt_inv)
    
    def score_decomposition(
        self, 
        r: torch.Tensor, 
        t: Union[float, torch.Tensor],
        score_fn: Callable[[torch.Tensor, float], torch.Tensor]
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Decompose the score function as in Lemma 1 (equation 12).
        
        Args:
            r: Input data of shape (batch_size, data_dim)
            t: Time point
            score_fn: Score function
            
        Returns:
            Tuple of (subspace_score, complement_score)
        """
        if isinstance(t, float):
            t = torch.tensor([t], device=self.device)
        
        lambda_t = self.compute_lambda_t(t)
        lambda_sqrt = torch.sqrt(lambda_t)
        lambda_sqrt_inv = 1.0 / lambda_sqrt
        
        T_t = self.compute_projection_t(t)
        
        # Vectorized implementation
        batch_size = r.shape[0]
        
        # Create projection matrix (I - T_t)
        I_minus_T = torch.eye(self.data_dim, device=self.device) - T_t
        
        # Precompute the transformation matrix
        transform_matrix = -torch.diag(lambda_sqrt_inv) @ I_minus_T @ torch.diag(lambda_sqrt_inv)
        
        # Apply transformation to each sample in batch
        # reshape r to (batch_size, data_dim, 1)
        r_reshaped = r.unsqueeze(2)
        
        # Calculate complement score for all samples at once
        # transform_matrix: (data_dim, data_dim)
        # r_reshaped: (batch_size, data_dim, 1)
        complement_score = torch.matmul(transform_matrix.unsqueeze(0), r_reshaped)
        complement_score = complement_score.squeeze(2)  # Shape: (batch_size, data_dim)
        
        # Calculate subspace score
        full_score = score_fn(r, t)
        subspace_score = full_score - complement_score
        
        return subspace_score, complement_score
    
    def forward_process(
        self, 
        x0: torch.Tensor, 
        t: Union[float, torch.Tensor],
        return_noise: bool = False
    ) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        """
        Sample from the forward process at time t.
        """
        # print(f"DEBUG forward_process: x0.shape={x0.shape}, t.shape={t.shape if torch.is_tensor(t) else 'scalar'}")

        # Handle input data dimensionality
        original_shape = x0.shape
        batch_size = x0.shape[0]
        
        # Reshape data if necessary
        if len(x0.shape) > 2:
            # Flatten all dimensions except batch dimension
            x0_flat = x0.reshape(batch_size, -1)
            print(f"Reshaped data from {original_shape} to {x0_flat.shape}")
            x0 = x0_flat
            
            # Update dimensions based on data if needed
            if self.infer_dims_from_data:
                self.update_dims_from_data(x0)
        
        # Handle time tensor
        if isinstance(t, float):
            t = torch.tensor([t], device=self.device)
        
        # Prepare alpha_t and h_t
        if t.dim() == 1:
            if t.numel() == 1:  # Single time value
                t_scalar = t.item()
                eta_value = self.eta(t_scalar)
                alpha_scalar = torch.exp(-0.5 * eta_value * t_scalar)
                # Create batch-sized tensor for broadcasting
                alpha_t = torch.full((batch_size, 1), alpha_scalar, device=self.device)
            else:  # Batch of time values
                # Match batch sizes
                if t.shape[0] != batch_size:
                    if t.shape[0] == 1:
                        t = t.expand(batch_size)
                    else:
                        raise ValueError(f"Batch size mismatch: t has {t.shape[0]} samples, x0 has {batch_size} samples")
                
                mean_t = t.mean().item()
                eta_value = self.eta(mean_t)
                t_expanded = t.view(batch_size, 1)
                alpha_t = torch.exp(-0.5 * eta_value * t_expanded)
        else:
            # Scalar tensor
            t_scalar = t.item()
            eta_value = self.eta(t_scalar)
            alpha_scalar = torch.exp(-0.5 * eta_value * t_scalar)
            alpha_t = torch.full((batch_size, 1), alpha_scalar, device=self.device)
        
        # Calculate h_t = 1 - α_t^2
        h_t = 1 - alpha_t**2
        
        # Sample noise
        noise = torch.randn_like(x0)
        
        # Generate forward sample: x_t = α_t * x_0 + sqrt(h_t) * ε
        x_t = alpha_t * x0 + torch.sqrt(h_t) * noise
        
        # Reshape output back to original dimensions if needed
        if len(original_shape) > 2 and x_t.shape != original_shape:
            x_t_reshaped = x_t.view(original_shape)
            if return_noise:
                noise_reshaped = noise.view(original_shape)
                # print(f"DEBUG forward_process: alpha_t.shape={alpha_t.shape}, h_t.shape={h_t.shape}")
                # print(f"DEBUG forward_process: returning x_t.shape={x_t_reshaped.shape}, noise.shape={noise_reshaped.shape}")
                return x_t_reshaped, noise_reshaped
            else:
                # print(f"DEBUG forward_process: alpha_t.shape={alpha_t.shape}, h_t.shape={h_t.shape}")
                # print(f"DEBUG forward_process: returning x_t.shape={x_t_reshaped.shape}")
                return x_t_reshaped
        
        # Return without reshaping
        # print(f"DEBUG forward_process: alpha_t.shape={alpha_t.shape}, h_t.shape={h_t.shape}")
        if return_noise:
            # print(f"DEBUG forward_process: returning x_t.shape={x_t.shape}, noise.shape={noise.shape}")
            return x_t, noise
        else:
            # print(f"DEBUG forward_process: returning x_t.shape={x_t.shape}")
            return x_t
    
    def get_alpha_h(self, t: Union[float, torch.Tensor]) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Calculate α_t and h_t for given time t.
        """
        # Convert input to tensor if it's a float
        if isinstance(t, float) or isinstance(t, int):
            t = torch.tensor([t], device=self.device)
        elif not isinstance(t, torch.Tensor):
            raise TypeError(f"Expected t to be float or torch.Tensor, got {type(t)}")
        
        # Move tensor to the correct device
        t = t.to(self.device)
        
        # Get batch size
        batch_size = t.shape[0] if t.dim() == 1 and t.numel() > 1 else 1
        
        if t.dim() == 1:
            if t.numel() == 1:  # Single time value
                # Get scalar value for eta calculation
                t_scalar = t.item()
                eta_value = self.eta(t_scalar)
                
                # Calculate alpha using tensors all the way
                alpha_t_value = torch.exp(torch.tensor(-0.5 * eta_value * t_scalar, device=self.device))
                alpha_t = alpha_t_value.expand(batch_size, 1)
            else:  # Batch of time values
                # Calculate mean for eta if needed
                mean_t = t.mean().item()
                eta_value = self.eta(mean_t)
                
                # Ensure correct shape for broadcasting
                t_expanded = t.view(batch_size, 1)
                alpha_t = torch.exp(-0.5 * eta_value * t_expanded)
        else:
            # Scalar tensor
            t_scalar = t.item()
            eta_value = self.eta(t_scalar)
            
            # Calculate alpha using tensors
            alpha_t_value = torch.exp(torch.tensor(-0.5 * eta_value * t_scalar, device=self.device))
            alpha_t = alpha_t_value.expand(batch_size, 1)
        
        # Calculate h_t = 1 - α_t^2
        h_t = 1 - alpha_t**2
        
        return alpha_t, h_t
    
    def get_score_matching_target(
        self, 
        x0: torch.Tensor,
        t: Union[float, torch.Tensor]
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Generate noised sample and score matching target for training.
        
        Args:
            x0: Initial data points
            t: Time point(s)
            
        Returns:
            Tuple of (noised_sample, score_target)
        """
        # Get x_t and the noise used
        x_t, noise = self.forward_process(x0, t, return_noise=True)
        
        # Calculate α_t and h_t
        alpha_t, h_t = self.get_alpha_h(t)
        
        # Reshape alpha_t and h_t for proper broadcasting with x0
        input_dims = len(x0.shape)
        batch_size = x0.shape[0]
        
        if input_dims > 2:
            # Add necessary dimensions for broadcasting
            reshape_dims = [batch_size] + [1] * (input_dims - 1)
            alpha_t = alpha_t.view(*reshape_dims)
            h_t = h_t.view(*reshape_dims)
        
        # The score matching target is -(x_t - α_t * x0) / h_t = -noise / sqrt(h_t)
        score_target = -(x_t - alpha_t * x0) / h_t
        
        return x_t, score_target
    
    def apply_diagonal_multiplication(self, diagonal: torch.Tensor, x: torch.Tensor) -> torch.Tensor:
        """
        Apply diagonal matrix multiplication to each sample in a batch.
        This is a workaround for the issue with torch.diag creating a 2D matrix.
        
        Args:
            diagonal: Diagonal elements of shape (data_dim,)
            x: Input data of shape (batch_size, data_dim)
            
        Returns:
            Result of diagonal matrix multiplication of shape (batch_size, data_dim)
        """
        # Element-wise multiplication with broadcasting
        return diagonal.unsqueeze(0) * x
    
    def reverse_process_step(
        self, 
        xt: torch.Tensor, 
        t: float, 
        dt: float,
        score_fn: Callable[[torch.Tensor, float], torch.Tensor],
        discretization: str = 'euler-maruyama'
    ) -> torch.Tensor:
        """
        Perform a single step of the reverse process.
        
        Args:
            xt: Current state
            t: Current time
            dt: Time step
            score_fn: Score function (neural network)
            discretization: Method for discretization
            
        Returns:
            Next state
        """
        if discretization == 'euler-maruyama':
            # Basic Euler-Maruyama discretization
            drift = 0.5 * xt + score_fn(xt, t)
            diffusion = torch.sqrt(torch.tensor(dt, device=self.device)) * torch.randn_like(xt)
            
            return xt + drift * dt + diffusion
        
        elif discretization == 'improved-euler':
            # Improved Euler method
            drift = 0.5 * xt + score_fn(xt, t)
            diffusion = torch.sqrt(torch.tensor(dt, device=self.device)) * torch.randn_like(xt)
            
            # Predict mid-point
            x_mid = xt + 0.5 * drift * dt + 0.5 * diffusion
            
            # Evaluate drift at mid-point
            drift_mid = 0.5 * x_mid + score_fn(x_mid, t + 0.5 * dt)
            
            return xt + drift_mid * dt + diffusion
        
        else:
            raise ValueError(f"Unknown discretization method: {discretization}")
    
    def sample(
        self, 
        score_fn: Callable[[torch.Tensor, float], torch.Tensor],
        num_samples: int, 
        noise_steps: Optional[int] = None,
        return_trajectory: bool = False,
        discretization: str = 'euler-maruyama'
    ) -> Union[torch.Tensor, List[torch.Tensor]]:
        """
        Sample from the reverse process.
        
        Args:
            score_fn: Score function (neural network)
            num_samples: Number of samples to generate
            noise_steps: Number of steps in the reverse process (default: self.num_steps)
            return_trajectory: Whether to return the full trajectory
            discretization: Method for discretization
            
        Returns:
            Generated samples, optionally with the full trajectory
        """
        if noise_steps is None:
            noise_steps = self.num_steps
        
        # Start from Gaussian noise
        x = torch.randn(num_samples, self.data_dim, device=self.device)
        
        # Time steps for reverse process
        times = torch.linspace(self.T, self.t0, noise_steps, device=self.device)
        time_pairs = list(zip(times[:-1], times[1:]))
        dt = (self.T - self.t0) / (noise_steps - 1)
        
        # Store trajectory if requested
        trajectory = [x.clone()] if return_trajectory else None
        
        # Perform reverse process
        with torch.no_grad():
            for t_cur, t_next in tqdm(time_pairs, desc="Sampling", disable=not return_trajectory):
                x = self.reverse_process_step(
                    x, t_cur.item(), dt, score_fn, discretization=discretization
                )
                
                if return_trajectory:
                    trajectory.append(x.clone())
        
        if return_trajectory:
            return trajectory
        else:
            return x
    
    def sample_and_recover_subspace(
        self, 
        score_fn: Callable[[torch.Tensor, float], torch.Tensor],
        num_samples: int,
        noise_steps: Optional[int] = None,
        discretization: str = 'euler-maruyama'
    ) -> Dict[str, Any]:
        """
        Sample and recover the latent factor subspace as in Algorithm 1.
        
        Args:
            score_fn: Score function (neural network)
            num_samples: Number of samples to generate
            noise_steps: Number of steps in the reverse process
            discretization: Method for discretization
            
        Returns:
            Dictionary with generated samples, estimated covariance, 
            eigenvalues, and recovered subspace
        """
        # Generate samples
        samples = self.sample(score_fn, num_samples, noise_steps, discretization=discretization)
        
        # Compute sample covariance
        mean = torch.mean(samples, dim=0, keepdim=True)
        centered = samples - mean
        cov = (centered.T @ centered) / (num_samples - 1)
        
        # Perform SVD to recover latent subspace
        eigenvalues, eigenvectors = torch.linalg.eigh(cov)
        # Sort in descending order
        idx = torch.argsort(eigenvalues, descending=True)
        eigenvalues = eigenvalues[idx]
        eigenvectors = eigenvectors[:, idx]
        
        # Extract top-k eigenspace
        top_k_eigenvalues = eigenvalues[:self.factor_dim]
        top_k_eigenspace = eigenvectors[:, :self.factor_dim]
        
        return {
            "samples": samples,
            "mean": mean.squeeze(0),
            "covariance": cov,
            "eigenvalues": top_k_eigenvalues,
            "subspace": top_k_eigenspace
        }

    def evaluate_subspace_recovery(
        true_beta: torch.Tensor,
        estimated_subspace: torch.Tensor,
        true_eigenvalues: Optional[torch.Tensor] = None,
        estimated_eigenvalues: Optional[torch.Tensor] = None
    ) -> Dict[str, float]:
        """
        Evaluate the accuracy of subspace recovery as described in Theorem 3.
        
        Args:
            true_beta: True factor loading matrix with orthonormal columns
            estimated_subspace: Estimated factor subspace (e.g., from SVD)
            true_eigenvalues: True eigenvalues (optional)
            estimated_eigenvalues: Estimated eigenvalues (optional)
            
        Returns:
            Dictionary with evaluation metrics
        """
        # Ensure both matrices have compatible shapes
        assert true_beta.shape[1] == estimated_subspace.shape[1], "Dimension mismatch in subspace matrices"
        
        # Calculate relative Frobenius norm error for eigenspace recovery
        # ||Û·Û^T - U·U^T||_F / ||U·U^T||_F
        true_proj = true_beta @ true_beta.t()
        est_proj = estimated_subspace @ estimated_subspace.t()
        
        frob_error = torch.norm(est_proj - true_proj, p='fro') / torch.norm(true_proj, p='fro')
        
        # Calculate principal angles between subspaces
        # SVD of true_beta^T · estimated_subspace
        U, S, Vh = torch.linalg.svd(true_beta.t() @ estimated_subspace)
        principal_angles = torch.acos(torch.clamp(S, -1.0, 1.0))
        
        results = {
            "frobenius_error": frob_error.item(),
            "max_principal_angle": principal_angles.max().item(),
            "mean_principal_angle": principal_angles.mean().item(),
        }
        
        # Add eigenvalue error metrics if provided
        if true_eigenvalues is not None and estimated_eigenvalues is not None:
            k = min(len(true_eigenvalues), len(estimated_eigenvalues))
            rel_eigenvalue_error = torch.abs(estimated_eigenvalues[:k] / true_eigenvalues[:k] - 1.0)
            results["max_eigenvalue_rel_error"] = rel_eigenvalue_error.max().item()
            results["mean_eigenvalue_rel_error"] = rel_eigenvalue_error.mean().item()
        
        return results
    
    def get_denoising_target(
        self, 
        x0: torch.Tensor, 
        t: Union[float, torch.Tensor]
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Get denoising target for training with Vincent's denoising score matching.
        
        Args:
            x0: Initial data points
            t: Time point(s)
            
        Returns:
            Tuple of (noised_sample, denoising_target)
        """
        # Get x_t and the noise used
        x_t, noise = self.forward_process(x0, t, return_noise=True)
        
        # Calculate α_t and h_t
        alpha_t, h_t = self.get_alpha_h(t)
        
        # Reshape alpha_t and h_t for proper broadcasting with x0
        input_dims = len(x0.shape)
        batch_size = x0.shape[0]
        
        if input_dims > 2:
            # Add necessary dimensions for broadcasting
            reshape_dims = [batch_size] + [1] * (input_dims - 1)
            alpha_t = alpha_t.view(*reshape_dims)
            h_t = h_t.view(*reshape_dims)
        
        # The denoising target is (x_t - α_t * x0) / h_t
        denoising_target = (x_t - alpha_t * x0) / h_t
        
        return x_t, denoising_target
    
    def sample_time(self, batch_size: int, device=None) -> torch.Tensor:
        """Sample random time points for training."""
        # Sample uniformly from [t0, T]
        times = torch.rand(batch_size, device=self.device) * (self.T - self.t0) + self.t0
        # print(f"DEBUG sample_time: returning tensor of shape {times.shape}")
        return times

if __name__ == "__main__":
    # Test the diffusion process
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    
    # Define dimensions
    data_dim = 100  # Asset returns dimension
    factor_dim = 5  # Number of latent factors
    
    # Create model with factor structure
    diffusion = DiffusionProcess(
        data_dim=data_dim,
        factor_dim=factor_dim,
        T=1.0, 
        num_samples=1000,  # Use to compute optimal t0 and T
        device=device
    )
    
    # Create dummy data
    x0 = torch.randn(10, data_dim, device=device)
    
    # Test forward process
    x_t = diffusion.forward_process(x0, t=0.5)
    print(f"Forward process output shape: {x_t.shape}")
    
    # Test denoising target
    x_t, target = diffusion.get_denoising_target(x0, t=0.5)
    print(f"Denoising target shape: {target.shape}")
    
    # Test score decomposition
    def dummy_score_fn(x, t):
        return -x  # Dummy score function
        
    x_t = diffusion.forward_process(x0, t=0.5)
    subspace_score, complement_score = diffusion.score_decomposition(x_t, 0.5, dummy_score_fn)
    print(f"Subspace score shape: {subspace_score.shape}")
    print(f"Complement score shape: {complement_score.shape}")
    
    # Test the sampling and subspace recovery with a small number of samples
    results = diffusion.sample_and_recover_subspace(
        dummy_score_fn, 
        num_samples=20, 
        noise_steps=10
    )
    
    print(f"Generated samples shape: {results['samples'].shape}")
    print(f"Estimated covariance shape: {results['covariance'].shape}")
    print(f"Top-k eigenvalues: {results['eigenvalues']}")
    print(f"Recovered subspace shape: {results['subspace'].shape}")
