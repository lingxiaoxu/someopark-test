"""
Synthetic data experiment for the Diffusion Factor Model.

This module implements the synthetic data experiment described in Section 6 of the paper,
which evaluates the model's ability to recover the underlying factor structure.
"""

import os
import numpy as np
import torch
import matplotlib.pyplot as plt
from typing import Dict, Tuple, List, Optional, Union, Any
import logging
import time
import argparse
from tqdm import tqdm
import pandas as pd

# Import local modules
import sys
sys.path.append('.')
from config import SYNTHETIC_CONFIG, DIFFUSION_CONFIG, SCORE_NETWORK_CONFIG, TRAINING_CONFIG, DEVICE
from data import create_synthetic_dataset, prepare_data_loader
from model import (
    DiffusionProcess, create_score_network, 
    create_trainer, train_diffusion_model,
    DiffusionSampler, generate_samples
)
from evaluation import (
    compute_all_metrics, evaluate_factor_recovery,
    FactorRecoveryEvaluator
)
from utils import (
    plot_distribution_comparison, plot_eigenvalue_scree, 
    plot_covariance_heatmap, plot_subspace_recovery,
    plot_training_history, plot_relative_improvement_by_sample_size
)

# Set up logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def run_synthetic_experiment(
    config_dict: Dict[str, Dict],
    output_dir: str = 'results/synthetic',
    run_training: bool = True,
    evaluation_only: bool = False,
    checkpoint_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Run synthetic data experiment.
    
    Args:
        config_dict: Dictionary with all configuration settings
        output_dir: Directory to save outputs
        run_training: Whether to train the model or load from checkpoint
        evaluation_only: Whether to only run evaluation on existing model
        checkpoint_path: Path to model checkpoint (if not running training)
        
    Returns:
        Dictionary with experiment results
    """
    logger.info("Starting synthetic data experiment")
    
    # Create output directory
    os.makedirs(output_dir, exist_ok=True)
    
    # Extract configurations
    synthetic_config = config_dict.get('synthetic_config', SYNTHETIC_CONFIG)
    diffusion_config = config_dict.get('diffusion_config', DIFFUSION_CONFIG)
    network_config = config_dict.get('score_network_config', SCORE_NETWORK_CONFIG)
    training_config = config_dict.get('training_config', TRAINING_CONFIG)
    
    # Step 1: Generate synthetic data
    logger.info("Generating synthetic data")
    data_dict = create_synthetic_dataset(synthetic_config)
    generator = data_dict['generator']
    
    # Save true data information for evaluation
    true_data = {
        'true_factor_subspace': generator.true_factor_subspace,
        'true_cov': generator.true_cov,
        'true_eigenvalues': generator.true_eigenvalues,
        'sample_size_datasets': data_dict['sample_size_datasets']
    }
    
    # Step 2: Create diffusion process
    logger.info("Creating diffusion process")
    diffusion = DiffusionProcess(
        data_dim=synthetic_config['asset_dim'],     # default is 100 — must match the data,
        factor_dim=synthetic_config['factor_dim'],  # else sampling draws wrong-dim noise
        T=diffusion_config['T'],
        t0=diffusion_config['t0'],
        eta=diffusion_config['eta'],
        num_steps=diffusion_config['num_steps'],
        device=DEVICE
    )
    
    # Step 3: Create or load the model
    logger.info("Setting up the model")
    model = create_score_network(
        network_config,
        asset_dim=synthetic_config['asset_dim'],
        factor_dim=synthetic_config['factor_dim'],
        device=DEVICE
    )
    
    # Step 4: Train or load the model
    if not evaluation_only and run_training:
        logger.info("Preparing data loaders")
        # Get train and validation datasets
        train_data = data_dict['full_dataset']['train']
        val_data = data_dict['full_dataset']['val']
        
        # Create data loaders
        train_loader = prepare_data_loader(
            train_data, 
            batch_size=training_config['batch_size'],
            shuffle=True
        )
        val_loader = prepare_data_loader(
            val_data, 
            batch_size=training_config['batch_size'],
            shuffle=False
        )
        
        # Train the model
        logger.info("Training the model")
        model, history = train_diffusion_model(
            model=model,
            diffusion_process=diffusion,
            train_loader=train_loader,
            val_loader=val_loader,
            config=training_config,
            device=DEVICE
        )
        
        # Plot and save training history
        fig = plot_training_history(history)
        plt.savefig(os.path.join(output_dir, 'training_history.png'), dpi=300, bbox_inches='tight')
        plt.close(fig)
        
    elif checkpoint_path:
        # Load model from checkpoint
        logger.info(f"Loading model from checkpoint: {checkpoint_path}")
        checkpoint = torch.load(checkpoint_path, map_location=DEVICE)
        model.load_state_dict(checkpoint['model_state_dict'])
    else:
        logger.warning("No training or checkpoint loading specified")
    
    # Step 5: Generate samples from the trained model
    logger.info("Generating samples from the model")
    sampler = DiffusionSampler(
        model=model,
        diffusion_process=diffusion,
        device=DEVICE
    )
    
    # Generate samples
    samples = sampler.sample(
        num_samples=synthetic_config['num_samples'],
        data_shape=(synthetic_config['asset_dim'],),
        noise_steps=180,  # Following the paper's setting
        batch_size=64
    )
    
    # Step 6: Compute and save sample statistics
    logger.info("Computing sample statistics")
    sample_mean = samples.mean(dim=0)
    sample_cov = torch.matmul(
        (samples - sample_mean).t(),
        (samples - sample_mean)
    ) / (samples.shape[0] - 1)
    
    # Compute eigendecomposition
    eigenvalues, eigenvectors = torch.linalg.eigh(sample_cov)
    # Sort in descending order
    idx = torch.argsort(eigenvalues, descending=True)
    eigenvalues = eigenvalues[idx]
    eigenvectors = eigenvectors[:, idx]
    
    # Store generated data info
    generated_data = {
        'samples': samples.cpu().numpy(),
        'mean': sample_mean.cpu().numpy(),
        'cov': sample_cov.cpu().numpy(),
        'eigenvalues': eigenvalues.cpu().numpy(),
        'eigenvectors': eigenvectors.cpu().numpy()
    }
    
    # Step 7: Evaluate the results
    logger.info("Evaluating model performance")

    if 'full_dataset' in data_dict and 'test' in data_dict['full_dataset']:
        # Add reference samples from test set
        true_data['samples'] = data_dict['full_dataset']['test'].cpu().numpy()
        print(f"Added test data as reference samples with shape {true_data['samples'].shape}")
    
    # Compute all metrics
    all_metrics = compute_all_metrics(generated_data, true_data, k=synthetic_config['factor_dim'])
    
    # Evaluate factor recovery
    factor_results = evaluate_factor_recovery(
        generated_data=generated_data,
        true_data=true_data,
        factor_dim=synthetic_config['factor_dim'],
        sample_sizes=synthetic_config.get('sample_sizes', [512, 1024, 2048, 4096])
    )
    
    # Step 8: Generate and save visualizations
    logger.info("Generating visualizations")
    
    # Plot 1: Distribution comparison
    fig1 = plot_distribution_comparison(
        generated_samples=generated_data['samples'],
        reference_samples=data_dict['full_dataset']['test'].cpu().numpy(),
        title="Distribution Comparison: Generated vs. True"
    )
    plt.savefig(os.path.join(output_dir, 'distribution_comparison.png'), dpi=300, bbox_inches='tight')
    plt.close(fig1)
    
    # Plot 2: Eigenvalue scree plot
    fig2 = plot_eigenvalue_scree(
        generated_eigenvalues=generated_data['eigenvalues'],
        true_eigenvalues=true_data['true_eigenvalues'],
        factor_dim=synthetic_config['factor_dim'],
        title="Eigenvalue Scree Plot"
    )
    plt.savefig(os.path.join(output_dir, 'eigenvalue_scree.png'), dpi=300, bbox_inches='tight')
    plt.close(fig2)
    
    # Plot 3: Cumulative variance
    fig3 = plot_eigenvalue_scree(
        generated_eigenvalues=generated_data['eigenvalues'],
        true_eigenvalues=true_data['true_eigenvalues'],
        factor_dim=synthetic_config['factor_dim'],
        title="Cumulative Explained Variance",
        cumulative=True
    )
    plt.savefig(os.path.join(output_dir, 'cumulative_variance.png'), dpi=300, bbox_inches='tight')
    plt.close(fig3)
    
    # Plot 4: Covariance heatmap
    fig4 = plot_covariance_heatmap(
        estimated_cov=generated_data['cov'],
        true_cov=true_data['true_cov'],
        title="Covariance Matrix Comparison"
    )
    plt.savefig(os.path.join(output_dir, 'covariance_heatmap.png'), dpi=300, bbox_inches='tight')
    plt.close(fig4)
    
    # Plot 5: Subspace recovery
    fig5 = plot_subspace_recovery(
        recovered_eigenvectors=generated_data['eigenvectors'],
        true_eigenvectors=true_data['true_factor_subspace'],
        k=synthetic_config['factor_dim'],
        title="Subspace Recovery"
    )
    plt.savefig(os.path.join(output_dir, 'subspace_recovery.png'), dpi=300, bbox_inches='tight')
    plt.close(fig5)
    
    # Plot 6: Sample size effect on subspace recovery
    if factor_results['sample_size_results'] is not None:
        sample_sizes = synthetic_config.get('sample_sizes', [512, 1024, 2048, 4096])
        results = factor_results['sample_size_results']
        
        if 'projection_error' in results:
            fig6 = plot_relative_improvement_by_sample_size(
                sample_sizes=sample_sizes,
                diff_errors=[results['projection_error']['diffusion'].get(n, 0) for n in sample_sizes],
                emp_errors=[results['projection_error']['empirical'].get(n, 0) for n in sample_sizes],
                title="Effect of Sample Size on Subspace Recovery",
                ylabel="Projection Error"
            )
            plt.savefig(os.path.join(output_dir, 'sample_size_effect.png'), dpi=300, bbox_inches='tight')
            plt.close(fig6)
    
    # Step 9: Save results
    logger.info("Saving results")
    
    # Save metrics as CSV
    metrics_df = {}
    for category, metrics in all_metrics.items():
        for name, value in metrics.items():
            metrics_df[f"{category}_{name}"] = [value]
    
    pd.DataFrame(metrics_df).to_csv(os.path.join(output_dir, 'metrics.csv'), index=False)
    
    # Save some sample data
    np.save(os.path.join(output_dir, 'generated_samples.npy'), generated_data['samples'][:1000])
    
    # Combine all results
    results = {
        'metrics': all_metrics,
        'factor_recovery': factor_results,
        'generated_data': generated_data,
        'true_data': true_data,
        'configuration': {
            'synthetic_config': synthetic_config,
            'diffusion_config': diffusion_config,
            'network_config': network_config,
            'training_config': training_config
        }
    }
    
    logger.info("Synthetic data experiment completed successfully")
    
    return results


def main():
    """Main function to run the synthetic experiment with command line arguments."""
    parser = argparse.ArgumentParser(description='Run synthetic data experiment for Diffusion Factor Model')
    parser.add_argument('--output_dir', type=str, default='results/synthetic', 
                        help='Directory to save outputs')
    parser.add_argument('--no_training', action='store_true', 
                        help='Skip training and load model from checkpoint')
    parser.add_argument('--evaluation_only', action='store_true', 
                        help='Only run evaluation on existing model')
    parser.add_argument('--checkpoint_path', type=str, default=None, 
                        help='Path to model checkpoint')
    parser.add_argument('--asset_dim', type=int, default=2048, 
                        help='Number of assets (d)')
    parser.add_argument('--factor_dim', type=int, default=16, 
                        help='Number of factors (k)')
    parser.add_argument('--num_samples', type=int, default=8192, 
                        help='Number of samples to generate')
    
    args = parser.parse_args()
    
    # Update configuration based on arguments
    synthetic_config = SYNTHETIC_CONFIG.copy()
    synthetic_config['asset_dim'] = args.asset_dim
    synthetic_config['factor_dim'] = args.factor_dim
    synthetic_config['num_samples'] = args.num_samples
    
    # Run the experiment
    run_synthetic_experiment(
        config_dict={
            'synthetic_config': synthetic_config,
            'diffusion_config': DIFFUSION_CONFIG,
            'score_network_config': SCORE_NETWORK_CONFIG,
            'training_config': TRAINING_CONFIG
        },
        output_dir=args.output_dir,
        run_training=not args.no_training,
        evaluation_only=args.evaluation_only,
        checkpoint_path=args.checkpoint_path
    )


if __name__ == "__main__":
    main()
