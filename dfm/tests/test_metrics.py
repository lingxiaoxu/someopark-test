# test_metrics.py
import os
import numpy as np
import torch
import pandas as pd
from metrics import compute_all_metrics
import sys

def test_metrics_with_saved_data(
    output_dir='Project/results/synthetic',
    factor_dim=16
):
    """Test metrics calculation with already generated data"""
    print(f"Loading data from: {output_dir}")
    
    # Load generated samples
    if os.path.exists(os.path.join(output_dir, 'generated_samples.npy')):
        generated_samples = np.load(os.path.join(output_dir, 'generated_samples.npy'))
        print(f"Loaded generated samples with shape: {generated_samples.shape}")
    else:
        print(f"ERROR: Could not find generated_samples.npy in {output_dir}")
        return
    
    # Try to reconstruct the necessary data structures
    generated_data = {}
    true_data = {}
    
    # Add samples to generated_data
    generated_data['samples'] = generated_samples
    
    # Try to load covariance matrix
    if os.path.exists(os.path.join(output_dir, 'covariance_heatmap.png')):
        print("Covariance visualization found. Need to recalculate covariance.")
        # Recalculate covariance from samples
        sample_mean = np.mean(generated_samples, axis=0)
        sample_cov = np.matmul(
            (generated_samples - sample_mean).T,
            (generated_samples - sample_mean)
        ) / (generated_samples.shape[0] - 1)
        
        generated_data['cov'] = sample_cov
        print(f"Recalculated covariance matrix with shape: {sample_cov.shape}")
    
    # Calculate eigendecomposition if needed
    if 'cov' in generated_data:
        eigenvalues, eigenvectors = np.linalg.eigh(generated_data['cov'])
        # Sort in descending order
        idx = np.argsort(eigenvalues)[::-1]
        eigenvalues = eigenvalues[idx]
        eigenvectors = eigenvectors[:, idx]
        
        generated_data['eigenvalues'] = eigenvalues
        generated_data['eigenvectors'] = eigenvectors
        print(f"Calculated eigenvalues and eigenvectors from covariance matrix")
    
    # Create mock reference samples if needed
    if 'samples' not in true_data:
        print("Creating mock reference samples for testing...")
        # Use generated samples with small noise as mock reference
        true_data['samples'] = generated_samples + 0.1 * np.random.randn(*generated_samples.shape)
    
    # Try to load true factor subspace if it exists
    models_dir = 'models/generator'
    if os.path.exists(os.path.join(models_dir, 'synthetic_generator.pkl')):
        import pickle
        try:
            with open(os.path.join(models_dir, 'synthetic_generator.pkl'), 'rb') as f:
                generator = pickle.load(f)
                if hasattr(generator, 'true_factor_subspace'):
                    true_data['true_factor_subspace'] = generator.true_factor_subspace
                if hasattr(generator, 'true_cov'):
                    true_data['true_cov'] = generator.true_cov
                if hasattr(generator, 'true_eigenvalues'):
                    true_data['true_eigenvalues'] = generator.true_eigenvalues
                print("Loaded true data from generator pickle")
        except Exception as e:
            print(f"Error loading generator pickle: {e}")
    
    # Ensure we have minimum required data
    if 'samples' not in generated_data:
        print("ERROR: Missing required samples in generated_data")
        return
    
    # Add missing true data fields with mock data if needed
    if 'true_factor_subspace' not in true_data and 'eigenvectors' in generated_data:
        print("Creating mock true_factor_subspace for testing...")
        true_data['true_factor_subspace'] = generated_data['eigenvectors'][:, :factor_dim] + 0.1 * np.random.randn(
            generated_data['eigenvectors'].shape[0], factor_dim)
    
    if 'true_cov' not in true_data and 'cov' in generated_data:
        print("Creating mock true_cov for testing...")
        true_data['true_cov'] = generated_data['cov'] + 0.1 * np.random.randn(*generated_data['cov'].shape)
    
    if 'true_eigenvalues' not in true_data and 'eigenvalues' in generated_data:
        print("Creating mock true_eigenvalues for testing...")
        true_data['true_eigenvalues'] = generated_data['eigenvalues'] + 0.1 * np.random.randn(
            generated_data['eigenvalues'].shape[0])
    
    # Run metrics calculation with the available data
    print("\nComputing metrics with fixed metrics.py...")
    try:
        all_metrics = compute_all_metrics(generated_data, true_data, k=factor_dim)
        
        # Display metrics
        print("\nMetrics calculation successful!")
        for category, metrics in all_metrics.items():
            print(f"\n{category.upper()} METRICS:")
            for name, value in metrics.items():
                print(f"  {name}: {value}")
        
        # Save metrics as CSV
        metrics_df = {}
        for category, metrics in all_metrics.items():
            for name, value in metrics.items():
                metrics_df[f"{category}_{name}"] = [value]
        
        pd.DataFrame(metrics_df).to_csv(os.path.join(output_dir, 'metrics_test.csv'), index=False)
        print(f"\nMetrics saved to {os.path.join(output_dir, 'metrics_test.csv')}")
        
        return all_metrics
    except Exception as e:
        import traceback
        print(f"ERROR in metrics calculation: {e}")
        traceback.print_exc()
        return None

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Test metrics calculation with saved data')
    parser.add_argument('--output_dir', type=str, default='results/synthetic', 
                        help='Directory with saved outputs')
    parser.add_argument('--factor_dim', type=int, default=16, 
                        help='Number of factors (k)')
    
    args = parser.parse_args()
    test_metrics_with_saved_data(
        output_dir=args.output_dir,
        factor_dim=args.factor_dim
    )