"""
Configuration parameters for the Diffusion Factor Model.
"""

import os
import torch

# Device configuration
DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# Data configuration
SYNTHETIC_CONFIG = {
    'asset_dim': 2048,        # d = 2^11 = 2048 assets
    'factor_dim': 16,         # k = 16 factors
    'num_samples': 8192,      # 2^13 = 8192 samples
    'factor_mean_range': (0, 0.1),  # uniform range for factor means
    'factor_std_multiplier': 1.5,   # σ_Fi = 1.5 * μ_Fi
    'noise_std_range': (0, 0.4),    # uniform range for idiosyncratic noise
    'reshape_dims': (32, 64),       # (2^5, 2^6) for 2D UNet if needed
    'seed': 42,
    'sample_sizes': [512, 1024, 2048, 4096],  # Different training sample sizes
    'normalize': True,        # Apply normalization as in the paper
    'visualize': True,        # Generate visualizations
    'visualization_dir': 'visualizations',  # Directory to save visualizations
    'save_path': 'models/generator/synthetic_generator.pkl'  # Path to save the generator
}

# Diffusion process configuration
DIFFUSION_CONFIG = {
    'T': 1.0,                 # Terminal time
    't0': 0.01,               # Early stopping time
    'num_steps': 200,         # Number of discrete steps
    'eta': 1.0,               # η(t) = 1
}

# Score network configuration
SCORE_NETWORK_CONFIG = {
    'hidden_dim': 256,
    'encoder_layers': 4,      # Number of encoding layers
    'decoder_layers': 4,      # Number of decoding layers
    'dropout': 0.1,
    'use_2d_unet': True,      # Whether to use 2D UNet
    'unet_channels': [64, 128, 256, 512],  # UNet channel configuration
    'use_attention': True,    # Whether to use attention in UNet
}

# Training configuration
TRAINING_CONFIG = {
    'batch_size': 64,
    'num_epochs': 100,
    'learning_rate': 1e-4,
    'weight_decay': 1e-5,
    'lr_scheduler_step': 20,
    'lr_scheduler_gamma': 0.5,
    'gradient_clip_val': 1.0,
    'early_stopping_patience': 10,
    'checkpoint_dir': './checkpoints',
}

# Sampling configuration
SAMPLING_CONFIG = {
    'num_samples': 8192,      # Number of samples to generate
    'noise_steps': 180,       # Number of denoising steps (T' = 180)
    'discretization': 'euler-maruyama',  # Discretization method
}

# Evaluation configuration
EVALUATION_CONFIG = {
    'subspace_metrics': True,  # Evaluate subspace recovery metrics
    'distribution_metrics': True,  # Evaluate distribution metrics
    'portfolio_metrics': True,  # Evaluate portfolio performance
    'sample_sizes': [512, 1024, 2048, 4096],  # Various N for evaluation
    'risk_aversion': [3, 5],  # Risk aversion parameters
    'transaction_cost': 0.002,  # 20 basis points transaction cost
    'max_position': 0.05,     # Maximum position size
}

# Create necessary directories
os.makedirs(TRAINING_CONFIG['checkpoint_dir'], exist_ok=True)
