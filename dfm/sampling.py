"""
Sampling module for the Diffusion Factor Model.

This module implements sampling from a trained diffusion model to generate
synthetic asset returns.
"""

import torch
import numpy as np
from typing import Dict, List, Tuple, Optional, Union, Any, Callable
import logging
from tqdm import tqdm
import time

# Set up logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class DiffusionSampler:
    """Sampler for generating samples from a trained diffusion model."""
    
    def __init__(
        self,
        model: torch.nn.Module,
        diffusion_process: Any,
        device: torch.device = torch.device('cpu'),
        use_ema: bool = True,
        logger: Optional[logging.Logger] = None
    ):
        """
        Initialize the diffusion sampler.
        
        Args:
            model: Trained score network
            diffusion_process: Diffusion process
            device: Torch device
            use_ema: Whether to use EMA parameters for sampling
            logger: Logger
        """
        self.model = model
        self.diffusion = diffusion_process
        self.device = device
        self.use_ema = use_ema
        self.logger = logger or logging.getLogger(__name__)
        
        # Set model to evaluation mode
        self.model.eval()
    
    def sample(
        self,
        num_samples: int,
        data_shape: Union[Tuple[int, ...], List[int]],
        noise_steps: Optional[int] = None,
        return_trajectory: bool = False,
        batch_size: Optional[int] = None,
        discretization: str = 'euler-maruyama',
        callback: Optional[Callable[[torch.Tensor], None]] = None,
        final_denoise: bool = False
    ) -> Union[torch.Tensor, List[torch.Tensor]]:
        """
        Generate samples from the diffusion model.

        final_denoise: apply one Tweedie step at t0 after the reverse SDE,
        x0_hat = (x + h_t0 * s(x, t0)) / alpha_t0, removing the residual
        early-stopping noise (variance h_t0) from the returned samples.
        """
        # Set default batch size if not provided
        if batch_size is None:
            batch_size = min(num_samples, 64)  # Default batch size
        
        # Flatten data shape for diffusion process
        if isinstance(data_shape, (list, tuple)) and len(data_shape) > 1:
            flat_dim = np.prod(data_shape).astype(int)
        else:
            flat_dim = data_shape[0] if isinstance(data_shape, (list, tuple)) else data_shape
        
        # Initialize storage for all samples
        all_samples = []
        all_trajectories = [] if return_trajectory else None
        
        # Generate samples in batches
        num_batches = (num_samples + batch_size - 1) // batch_size
        
        self.logger.info(f"Generating {num_samples} samples in {num_batches} batches")
        start_time = time.time()
        
        # Define a proper score function with access to self
        def score_fn(x, t):
            # Convert t to tensor if it's a float
            if isinstance(t, float) or isinstance(t, int):
                t = torch.tensor([t], device=self.device)
            
            # Get alpha_t and h_t
            alpha_t, h_t = self.diffusion.get_alpha_h(t)
            
            # Return model prediction
            return self.model(x, t, alpha_t, h_t)
        
        with torch.no_grad():
            for i in range(num_batches):
                # Calculate batch size for this iteration
                current_batch_size = min(batch_size, num_samples - i * batch_size)
                
                # Generate samples for this batch
                if return_trajectory:
                    trajectory = self.diffusion.sample(
                        score_fn=score_fn,  # Use the function we defined above
                        num_samples=current_batch_size,
                        noise_steps=noise_steps,
                        return_trajectory=True,
                        discretization=discretization
                    )
                    
                    # Store the final samples
                    all_samples.append(trajectory[-1])
                    
                    # Transform and store the trajectory
                    if all_trajectories is not None:
                        batch_trajectory = [step.cpu() for step in trajectory]
                        all_trajectories.extend([
                            [step[j] for step in batch_trajectory]
                            for j in range(current_batch_size)
                        ])
                else:
                    # Generate samples without trajectory
                    samples = self.diffusion.sample(
                        score_fn=score_fn,  # Use the function we defined above
                        num_samples=current_batch_size,
                        noise_steps=noise_steps,
                        return_trajectory=False,
                        discretization=discretization
                    )

                    if final_denoise:
                        t0 = self.diffusion.t0
                        t0_tensor = torch.tensor([t0], device=self.device)
                        alpha0, h0 = self.diffusion.get_alpha_h(t0_tensor)
                        samples = (samples + h0 * score_fn(samples, t0)) / alpha0

                    # Store the samples
                    all_samples.append(samples)
                
                # Call callback if provided
                if callback is not None:
                    callback(all_samples[-1])
                
                # Log progress
                self.logger.info(f"Generated batch {i+1}/{num_batches}")
        
        # Concatenate all samples
        all_samples = torch.cat(all_samples, dim=0)
        
        # Reshape to original data shape if needed
        if isinstance(data_shape, (list, tuple)) and len(data_shape) > 1:
            all_samples = all_samples.reshape(num_samples, *data_shape)
        
        elapsed_time = time.time() - start_time
        self.logger.info(f"Sample generation completed in {elapsed_time:.2f} seconds")
        
        if return_trajectory:
            return all_trajectories
        else:
            return all_samples
    
    def denoise_samples(
        self,
        noisy_samples: torch.Tensor,
        t: Union[float, torch.Tensor],
        num_steps: Optional[int] = None
    ) -> torch.Tensor:
        """
        Denoise samples starting from a specific noise level t.
        
        Args:
            noisy_samples: Noisy samples of shape (batch_size, *data_shape)
            t: Noise level (time)
            num_steps: Number of denoising steps
            
        Returns:
            Denoised samples
        """
        # Set model to evaluation mode
        self.model.eval()
        
        # Default number of steps
        if num_steps is None:
            num_steps = 50

        # Define a proper score function with access to self
        def score_fn(x, t):
            # Convert t to tensor if it's a float
            if isinstance(t, float) or isinstance(t, int):
                t = torch.tensor([t], device=self.device)
            
            # Get alpha_t and h_t
            alpha_t, h_t = self.diffusion.get_alpha_h(t)
            
            # Return model prediction
            return self.model(x, t, alpha_t, h_t)
        
        # Calculate time steps from t to t0
        if isinstance(t, float):
            t = torch.tensor([t], device=self.device)
        
        # Create time steps for denoising
        t0 = self.diffusion.t0
        time_steps = torch.linspace(t.item(), t0, num_steps, device=self.device)
        
        # Denoising process
        x = noisy_samples.to(self.device)
        
        with torch.no_grad():
            for i in range(len(time_steps) - 1):
                t_cur = time_steps[i]
                t_next = time_steps[i + 1]
                dt = (t_cur - t_next).item()
                
                # Get current time embedding
                x = self.diffusion.reverse_process_step(
                    x, t_cur.item(), dt, 
                    score_fn=score_fn
                )
        
        return x

def generate_samples(
    model: torch.nn.Module,
    diffusion_process: Any,
    config: Dict,
    device: torch.device,
    true_beta: Optional[torch.Tensor] = None  # Add parameter for ground truth factor loadings
) -> Dict:
    """
    Generate samples from a trained diffusion model.
    
    Args:
        model: Trained score network
        diffusion_process: Diffusion process
        config: Sampling configuration
        device: Torch device
        
    Returns:
        Dictionary with generated samples and statistics
    """
    # Create sampler
    sampler = DiffusionSampler(
        model=model,
        diffusion_process=diffusion_process,
        device=device,
        use_ema=True
    )     
    
    # Generate samples
    start_time = time.time()
    samples = sampler.sample(
        num_samples=config['num_samples'],
        data_shape=(config.get('asset_dim', 512),),
        noise_steps=config.get('noise_steps', 180),
        batch_size=config.get('batch_size', 64),
        discretization=config.get('discretization', 'euler-maruyama')
    )
    elapsed_time = time.time() - start_time
    
    # Calculate statistics
    mean = samples.mean(dim=0)
    std = samples.std(dim=0)
    
    # Calculate sample covariance matrix
    centered = samples - mean.unsqueeze(0)
    cov = torch.matmul(centered.t(), centered) / (samples.shape[0] - 1)
    
    # Calculate eigenvalues of the covariance matrix
    eigenvalues, eigenvectors = torch.linalg.eigh(cov)
    # Sort in descending order
    idx = torch.argsort(eigenvalues, descending=True)
    eigenvalues = eigenvalues[idx]
    eigenvectors = eigenvectors[:, idx]

    # Evaluate subspace recovery if ground truth is provided
    subspace_metrics = None
    if true_beta is not None and hasattr(diffusion_process, 'factor_dim'):
        subspace_metrics = diffusion_process.evaluate_subspace_recovery(
            true_beta=true_beta,
            estimated_subspace=eigenvectors[:, :diffusion_process.factor_dim],
            true_eigenvalues=None,  # Add if available
            estimated_eigenvalues=eigenvalues[:diffusion_process.factor_dim]
        )
    
    # Return results
    results = {
        'samples': samples.cpu(),
        'mean': mean.cpu(),
        'std': std.cpu(),
        'cov': cov.cpu(),
        'eigenvalues': eigenvalues.cpu(),
        'eigenvectors': eigenvectors.cpu(),
        'sampling_time': elapsed_time
    }
    
    # Add subspace recovery metrics if available
    if subspace_metrics is not None:
        results['subspace_metrics'] = subspace_metrics
    
    return results

if __name__ == "__main__":
    # Test the sampling module
    from config import SAMPLING_CONFIG, DIFFUSION_CONFIG, SCORE_NETWORK_CONFIG, DEVICE
    from diffusion import DiffusionProcess
    from score_network import create_score_network
    
    # Create diffusion process
    diffusion = DiffusionProcess(
        T=DIFFUSION_CONFIG['T'],
        t0=DIFFUSION_CONFIG['t0'],
        eta=DIFFUSION_CONFIG['eta'],
        num_steps=DIFFUSION_CONFIG['num_steps'],
        device=DEVICE
    )
    
    # Create model
    asset_dim = 512
    factor_dim = 16
    model = create_score_network(SCORE_NETWORK_CONFIG, asset_dim, factor_dim, DEVICE)
    
    # Create dummy model (just for testing)
    def dummy_forward(x, t, *args, **kwargs):
        return -x  # Simple negative score for testing
    
    model.forward = dummy_forward
    
    # Create sampler
    sampler = DiffusionSampler(model, diffusion, DEVICE)
    
    # Test sampling
    samples = sampler.sample(
        num_samples=10,
        data_shape=(asset_dim,),
        noise_steps=10,
        return_trajectory=False
    )
    
    print(f"Generated samples shape: {samples.shape}")
