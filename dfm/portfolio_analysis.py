"""
Portfolio analysis experiment for the Diffusion Factor Model.

This module implements the portfolio analysis experiment described in Section 7 of the paper,
which evaluates the model's ability to improve portfolio construction.
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
from config import SYNTHETIC_CONFIG, DIFFUSION_CONFIG, SCORE_NETWORK_CONFIG, TRAINING_CONFIG, EVALUATION_CONFIG, DEVICE
from data import prepare_dataset, create_rolling_windows
from model import (
    DiffusionProcess, create_score_network, 
    create_trainer, train_diffusion_model,
    DiffusionSampler, generate_samples
)
from evaluation import (
    evaluate_portfolios, evaluate_diffusion_portfolios,
    plot_cumulative_returns, plot_weights_comparison
)
from utils import (
    plot_distribution_comparison, plot_eigenvalue_scree, 
    plot_metric_comparison_table
)

# Set up logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def run_portfolio_analysis(
    data: pd.DataFrame,
    config_dict: Dict[str, Dict],
    output_dir: str = 'results/portfolio',
    use_synthetic_data: bool = True,
    checkpoint_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Run portfolio analysis experiment.
    
    Args:
        data: DataFrame with asset returns
        config_dict: Dictionary with all configuration settings
        output_dir: Directory to save outputs
        use_synthetic_data: Whether to use synthetic data for initial tests
        checkpoint_path: Path to model checkpoint (if available)
        
    Returns:
        Dictionary with experiment results
    """
    logger.info("Starting portfolio analysis experiment")
    
    # Create output directory
    os.makedirs(output_dir, exist_ok=True)
    
    # Extract configurations
    diffusion_config = config_dict.get('diffusion_config', DIFFUSION_CONFIG)
    network_config = config_dict.get('score_network_config', SCORE_NETWORK_CONFIG)
    training_config = config_dict.get('training_config', TRAINING_CONFIG)
    evaluation_config = config_dict.get('evaluation_config', EVALUATION_CONFIG)
    
    # Step 1: Prepare data
    logger.info("Preparing data")
    
    if use_synthetic_data:
        # For testing, create synthetic data with known properties
        logger.info("Creating synthetic data for testing")
        asset_dim = 50
        num_periods = 2000
        
        # Generate synthetic data with factor structure
        np.random.seed(42)
        factor_dim = 5
        factor_loadings = np.random.randn(asset_dim, factor_dim)
        factor_loadings, _ = np.linalg.qr(factor_loadings)
        
        # Generate factor returns
        factor_means = np.linspace(0.0005, 0.002, factor_dim)
        factor_cov = np.diag(np.linspace(0.001, 0.005, factor_dim))
        factors = np.random.multivariate_normal(factor_means, factor_cov, num_periods)
        
        # Generate idiosyncratic returns
        noise_std = np.linspace(0.002, 0.01, asset_dim)
        noise = np.random.normal(0, 1, (num_periods, asset_dim)) * noise_std
        
        # Combine to get asset returns
        returns = factors @ factor_loadings.T + noise
        
        # Convert to DataFrame
        dates = pd.date_range(start='2000-01-01', periods=num_periods, freq='B')
        asset_names = [f'Asset_{i}' for i in range(asset_dim)]
        data = pd.DataFrame(returns, index=dates, columns=asset_names)
        
        # Split into train and test
        train_data = data.iloc[:1600]
        test_data = data.iloc[1600:]
        
        # Create a simple window for testing
        windows = [(train_data, test_data)]
        
    else:
        # If using real data, create rolling windows
        windows = create_rolling_windows(
            data, 
            window_size=5*252,  # 5 years of daily data
            stride=252  # 1 year stride
        )
    
    # Step 2: Run portfolio analysis for each window
    results_all_windows = []
    
    for window_idx, (train_window, test_window) in enumerate(windows):
        logger.info(f"Processing window {window_idx + 1}/{len(windows)}")
        
        # Prepare data for this window
        train_returns = train_window.values
        test_returns = test_window.values
        
        # Step 3: Create and train diffusion model
        logger.info("Setting up diffusion model")
        diffusion = DiffusionProcess(
            data_dim=train_returns.shape[1],   # default is 100 — must match the data,
            factor_dim=factor_dim if use_synthetic_data else 5,
            T=diffusion_config['T'],
            t0=diffusion_config['t0'],
            eta=diffusion_config['eta'],
            num_steps=diffusion_config['num_steps'],
            device=DEVICE
        )
        
        # Create model
        model = create_score_network(
            network_config,
            asset_dim=train_returns.shape[1],
            factor_dim=factor_dim if use_synthetic_data else 5,  # Use known factor_dim for synthetic
            device=DEVICE
        )
        
        # Train model if no checkpoint is provided
        if checkpoint_path is None:
            logger.info("Training diffusion model")
            
            # Convert to tensor and create data loader
            train_tensor = torch.tensor(train_returns, dtype=torch.float32, device=DEVICE)
            train_dataset = torch.utils.data.TensorDataset(train_tensor)
            train_loader = torch.utils.data.DataLoader(
                train_dataset,
                batch_size=training_config['batch_size'],
                shuffle=True
            )
            
            # Use a subset for validation
            val_size = int(0.2 * len(train_tensor))
            train_idx, val_idx = torch.utils.data.random_split(
                range(len(train_tensor)), 
                [len(train_tensor) - val_size, val_size]
            )
            val_tensor = train_tensor[val_idx.indices]
            val_dataset = torch.utils.data.TensorDataset(val_tensor)
            val_loader = torch.utils.data.DataLoader(
                val_dataset,
                batch_size=training_config['batch_size'],
                shuffle=False
            )
            
            # Train the model
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
            plt.savefig(
                os.path.join(output_dir, f'training_history_window_{window_idx}.png'), 
                dpi=300, 
                bbox_inches='tight'
            )
            plt.close(fig)
            
        else:
            # Load model from checkpoint
            logger.info(f"Loading model from checkpoint: {checkpoint_path}")
            checkpoint = torch.load(checkpoint_path, map_location=DEVICE)
            model.load_state_dict(checkpoint['model_state_dict'])
        
        # Step 4: Generate samples from the trained model
        logger.info("Generating samples from the model")
        sampler = DiffusionSampler(
            model=model,
            diffusion_process=diffusion,
            device=DEVICE
        )
        
        # Generate samples
        diffusion_samples = sampler.sample(
            num_samples=len(train_returns),  # Same size as training data
            data_shape=(train_returns.shape[1],),
            noise_steps=180,  # Following the paper's setting
            batch_size=64
        ).cpu().numpy()
        
        # Step 5: Evaluate portfolio strategies
        logger.info("Evaluating portfolio strategies")
        
        # First, evaluate standard strategies without diffusion
        standard_results = evaluate_portfolios(
            train_returns=train_returns,
            test_returns=test_returns,
            risk_aversion=evaluation_config['risk_aversion'][0],  # Use first value
            max_weight=evaluation_config['max_position'],
            transaction_cost=evaluation_config['transaction_cost'],
            rebalance_frequency=20  # Monthly rebalancing (assuming ~20 trading days)
        )
        
        # Then, evaluate strategies with diffusion
        diffusion_results = evaluate_diffusion_portfolios(
            train_returns=train_returns,
            test_returns=test_returns,
            diffusion_samples=diffusion_samples,
            risk_aversion=evaluation_config['risk_aversion'][0],  # Use first value
            max_weight=evaluation_config['max_position'],
            transaction_cost=evaluation_config['transaction_cost'],
            rebalance_frequency=20  # Monthly rebalancing
        )
        
        # Combine results
        all_results = {**standard_results, **diffusion_results}
        
        # Step 6: Generate and save visualizations
        logger.info("Generating visualizations")
        
        # Plot 1: Distribution comparison
        fig1 = plot_distribution_comparison(
            generated_samples=diffusion_samples,
            reference_samples=train_returns,
            num_assets=min(4, train_returns.shape[1]),
            title=f"Distribution Comparison: Generated vs. Original (Window {window_idx + 1})"
        )
        plt.savefig(
            os.path.join(output_dir, f'distribution_comparison_window_{window_idx}.png'),
            dpi=300, 
            bbox_inches='tight'
        )
        plt.close(fig1)
        
        # Plot 2: Cumulative returns
        fig2 = plot_cumulative_returns(
            results=all_results,
            title=f"Cumulative Returns (Window {window_idx + 1})",
            log_scale=True
        )
        plt.savefig(
            os.path.join(output_dir, f'cumulative_returns_window_{window_idx}.png'),
            dpi=300, 
            bbox_inches='tight'
        )
        plt.close(fig2)
        
        # Plot 3: Portfolio weights comparison
        fig3 = plot_weights_comparison(
            results={k: all_results[k] for k in ['EW', 'Diff+Emp', 'Shr']},
            num_assets=min(20, train_returns.shape[1]),
            figsize=(12, 10)
        )
        plt.savefig(
            os.path.join(output_dir, f'weights_comparison_window_{window_idx}.png'),
            dpi=300, 
            bbox_inches='tight'
        )
        plt.close(fig3)
        
        # Plot 4: Performance metrics table
        metrics_to_show = ['mean', 'std', 'sharpe_ratio', 'max_drawdown', 'cer']
        strategy_names = ['Diff+Emp', 'Diff+Shr', 'E-Diff', 'EW', 'Emp', 'Shr', 'PCA_tangency', 'Diff+PCA_tangency']
        strategy_names = [name for name in strategy_names if name in all_results]
        
        fig4 = plot_metric_comparison_table(
            results={k: all_results[k] for k in strategy_names if k in all_results},
            metrics=metrics_to_show,
            title=f"Strategy Performance (Window {window_idx + 1})"
        )
        plt.savefig(
            os.path.join(output_dir, f'metrics_table_window_{window_idx}.png'),
            dpi=300, 
            bbox_inches='tight'
        )
        plt.close(fig4)
        
        # Step 7: Save window results
        window_results = {
            'window_idx': window_idx,
            'train_period': (train_window.index[0], train_window.index[-1]),
            'test_period': (test_window.index[0], test_window.index[-1]),
            'portfolio_results': all_results,
            'diffusion_samples_mean': np.mean(diffusion_samples, axis=0),
            'diffusion_samples_std': np.std(diffusion_samples, axis=0),
            'train_returns_mean': np.mean(train_returns, axis=0),
            'train_returns_std': np.std(train_returns, axis=0)
        }
        
        # Extract performance metrics for each strategy
        metrics_df = {}
        for strategy, result in all_results.items():
            if 'metrics' in result:
                for metric, value in result['metrics'].items():
                    metrics_df[(strategy, metric)] = value
        
        # Save as CSV
        pd.DataFrame(metrics_df, index=[window_idx]).to_csv(
            os.path.join(output_dir, f'metrics_window_{window_idx}.csv')
        )
        
        # Append to all windows results
        results_all_windows.append(window_results)
    
    # Step 8: Aggregate results across all windows
    logger.info("Aggregating results across all windows")
    
    # Extract performance metrics for each strategy across windows
    all_metrics = {}
    for window_result in results_all_windows:
        for strategy, result in window_result['portfolio_results'].items():
            if 'metrics' in result:
                if strategy not in all_metrics:
                    all_metrics[strategy] = {}
                
                for metric, value in result['metrics'].items():
                    if metric not in all_metrics[strategy]:
                        all_metrics[strategy][metric] = []
                    
                    all_metrics[strategy][metric].append(value)
    
    # Calculate average metrics
    avg_metrics = {}
    for strategy, metrics in all_metrics.items():
        avg_metrics[strategy] = {metric: np.mean(values) for metric, values in metrics.items()}
    
    # Create comparison table
    metrics_to_show = ['mean', 'std', 'sharpe_ratio', 'max_drawdown', 'cer']
    strategy_names = ['Diff+Emp', 'Diff+Shr', 'E-Diff', 'EW', 'Emp', 'Shr', 'PCA_tangency', 'Diff+PCA_tangency']
    strategy_names = [name for name in strategy_names if name in avg_metrics]
    
    fig = plot_metric_comparison_table(
        results={k: {'metrics': avg_metrics[k]} for k in strategy_names if k in avg_metrics},
        metrics=metrics_to_show,
        title="Average Strategy Performance Across All Windows"
    )
    plt.savefig(
        os.path.join(output_dir, 'average_metrics_table.png'),
        dpi=300, 
        bbox_inches='tight'
    )
    plt.close(fig)
    
    # Save aggregated results
    pd.DataFrame(avg_metrics).to_csv(os.path.join(output_dir, 'average_metrics.csv'))
    
    # Create results dictionary
    results = {
        'window_results': results_all_windows,
        'average_metrics': avg_metrics,
        'configuration': {
            'diffusion_config': diffusion_config,
            'network_config': network_config,
            'training_config': training_config,
            'evaluation_config': evaluation_config
        }
    }
    
    logger.info("Portfolio analysis experiment completed successfully")
    
    return results


def main():
    """Main function to run the portfolio analysis with command line arguments."""
    parser = argparse.ArgumentParser(description='Run portfolio analysis for Diffusion Factor Model')
    parser.add_argument('--data_path', type=str, default=None, 
                        help='Path to returns data CSV')
    parser.add_argument('--output_dir', type=str, default='results/portfolio', 
                        help='Directory to save outputs')
    parser.add_argument('--use_synthetic', action='store_true', 
                        help='Use synthetic data instead of real data')
    parser.add_argument('--checkpoint_path', type=str, default=None, 
                        help='Path to model checkpoint')
    parser.add_argument('--risk_aversion', type=float, default=3.0, 
                        help='Risk aversion parameter')
    parser.add_argument('--transaction_cost', type=float, default=0.002, 
                        help='Transaction cost (bps)')
    parser.add_argument('--max_position', type=float, default=0.05, 
                        help='Maximum position size')
    
    args = parser.parse_args()
    
    # Load data if path provided
    if args.data_path and not args.use_synthetic:
        data = pd.read_csv(args.data_path, index_col=0, parse_dates=True)
    else:
        data = None
    
    # Update configuration based on arguments
    evaluation_config = EVALUATION_CONFIG.copy()
    evaluation_config['risk_aversion'] = [args.risk_aversion]
    evaluation_config['transaction_cost'] = args.transaction_cost
    evaluation_config['max_position'] = args.max_position
    
    # Run the experiment
    run_portfolio_analysis(
        data=data,
        config_dict={
            'diffusion_config': DIFFUSION_CONFIG,
            'score_network_config': SCORE_NETWORK_CONFIG,
            'training_config': TRAINING_CONFIG,
            'evaluation_config': evaluation_config
        },
        output_dir=args.output_dir,
        use_synthetic_data=args.use_synthetic or data is None,
        checkpoint_path=args.checkpoint_path
    )


if __name__ == "__main__":
    main()
