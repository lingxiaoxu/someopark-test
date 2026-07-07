"""
Main entry point for the Diffusion Factor Model.

This script runs the full pipeline for the Diffusion Factor Model, including
synthetic data generation, model training, and performance evaluation.
"""
import os
import argparse
import logging
import pandas as pd
from typing import Dict, Any

from config import (
    SYNTHETIC_CONFIG, DIFFUSION_CONFIG, 
    SCORE_NETWORK_CONFIG, TRAINING_CONFIG, 
    EVALUATION_CONFIG, DEVICE
)
from experiments import run_synthetic_experiment, run_portfolio_analysis

# Set up logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def main():
    """Main entry point for the Diffusion Factor Model."""
    parser = argparse.ArgumentParser(description='Diffusion Factor Model')
    
    # Experiment selection
    parser.add_argument('--experiment', type=str, choices=['synthetic', 'portfolio', 'both'], 
                        default='synthetic', help='Experiment to run')
    
    # General options
    parser.add_argument('--output_dir', type=str, default='results', 
                        help='Directory to save outputs')
    parser.add_argument('--checkpoint_path', type=str, default=None, 
                        help='Path to model checkpoint (if available)')
    
    # Synthetic experiment options
    parser.add_argument('--asset_dim', type=int, default=2048, 
                        help='Number of assets (d) for synthetic experiment')
    parser.add_argument('--factor_dim', type=int, default=16, 
                        help='Number of factors (k) for synthetic experiment')
    parser.add_argument('--num_samples', type=int, default=8192, 
                        help='Number of samples to generate')
    parser.add_argument('--no_training', action='store_true', 
                        help='Skip training and load model from checkpoint')
    parser.add_argument('--epochs', type=int, default=None,
                        help='Override number of training epochs')
    parser.add_argument('--sample_sizes', type=str, default=None,
                        help='Comma-separated training sample sizes (e.g. 128,256,512)')
    
    # Portfolio experiment options
    parser.add_argument('--data_path', type=str, default=None, 
                        help='Path to returns data CSV for portfolio experiment')
    parser.add_argument('--use_synthetic', action='store_true', 
                        help='Use synthetic data for portfolio experiment')
    parser.add_argument('--risk_aversion', type=float, default=3.0, 
                        help='Risk aversion parameter')
    parser.add_argument('--transaction_cost', type=float, default=0.002, 
                        help='Transaction cost (bps)')
    
    args = parser.parse_args()
    
    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Configure experiments based on arguments
    synthetic_config = SYNTHETIC_CONFIG.copy()
    synthetic_config['asset_dim'] = args.asset_dim
    synthetic_config['factor_dim'] = args.factor_dim
    synthetic_config['num_samples'] = args.num_samples

    # Wire the dependent knobs to the CLI dims — the config defaults assume d=2048:
    #  * reshape_dims must satisfy h*w == asset_dim for the 2D-UNet path; when it doesn't
    #    (or the grid is too small to downsample), fall back to the paper's core MLP
    #    encoder-decoder (FactorScoreNetwork) with flat (n, d) data.
    #  * sample_sizes can be overridden for quick runs.
    score_network_config = SCORE_NETWORK_CONFIG.copy()
    d = args.asset_dim
    rd = synthetic_config.get('reshape_dims')
    if rd is None or rd[0] * rd[1] != d:
        h = 1
        while (h * 2) * (h * 2) <= d:
            h *= 2
        while h > 1 and d % h != 0:
            h //= 2
        w = d // h if h > 1 else 0
        if h >= 16 and w >= 16 and h * w == d:     # UNet needs room for its down-blocks
            synthetic_config['reshape_dims'] = (h, w)
        else:
            synthetic_config['reshape_dims'] = None
            score_network_config['use_2d_unet'] = False
            logger.info(f"asset_dim={d}: using MLP FactorScoreNetwork (no 2D reshape)")
    training_config = TRAINING_CONFIG.copy()
    if args.epochs is not None:
        training_config['num_epochs'] = args.epochs
    if args.sample_sizes:
        synthetic_config['sample_sizes'] = [int(x) for x in args.sample_sizes.split(',')]
    
    evaluation_config = EVALUATION_CONFIG.copy()
    evaluation_config['risk_aversion'] = [args.risk_aversion]
    evaluation_config['transaction_cost'] = args.transaction_cost
    
    config_dict = {
        'synthetic_config': synthetic_config,
        'diffusion_config': DIFFUSION_CONFIG,
        'score_network_config': score_network_config,
        'training_config': training_config,
        'evaluation_config': evaluation_config
    }
    
    # Run selected experiments
    results = {}
    
    if args.experiment in ['synthetic', 'both']:
        logger.info("Running synthetic data experiment")
        synthetic_output_dir = os.path.join(args.output_dir, 'synthetic')
        
        results['synthetic'] = run_synthetic_experiment(
            config_dict=config_dict,
            output_dir=synthetic_output_dir,
            run_training=not args.no_training,
            checkpoint_path=args.checkpoint_path
        )
        
        logger.info(f"Synthetic experiment results saved to {synthetic_output_dir}")
    
    if args.experiment in ['portfolio', 'both']:
        logger.info("Running portfolio analysis experiment")
        portfolio_output_dir = os.path.join(args.output_dir, 'portfolio')
        
        # Load data if path provided
        if args.data_path and not args.use_synthetic:
            data = pd.read_csv(args.data_path, index_col=0, parse_dates=True)
            use_synthetic = False
        else:
            data = None
            use_synthetic = True
        
        results['portfolio'] = run_portfolio_analysis(
            data=data,
            config_dict=config_dict,
            output_dir=portfolio_output_dir,
            use_synthetic_data=use_synthetic,
            checkpoint_path=args.checkpoint_path
        )
        
        logger.info(f"Portfolio experiment results saved to {portfolio_output_dir}")
    
    return results


if __name__ == "__main__":
    main()
