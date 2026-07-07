import pytest
import numpy as np
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from synthetic import SyntheticDataGenerator

@pytest.fixture
def default_generator():
    return SyntheticDataGenerator(
        asset_dim=2048,
        factor_dim=16,
        factor_mean_range=(0, 0.1),
        factor_std_multiplier=1.5,
        noise_std_range=(0.01, 0.1),
        seed=42
    )

def test_factor_orthogonality(default_generator):
    """Test that factor loading matrix has orthogonal columns"""
    beta = default_generator.beta
    orth_test = np.allclose(beta.T @ beta, np.eye(default_generator.factor_dim), atol=1e-6)
    assert orth_test, "Factor loading matrix columns are not orthogonal"

def test_noise_variance_monotonicity(default_generator):
    """Test that noise variances are strictly decreasing"""
    noise_vars = default_generator.noise_stds**2
    assert np.all(np.diff(noise_vars) <= 0), "Noise variances are not monotonically decreasing"

def test_covariance_structure(default_generator):
    """Verify covariance matrix decomposition matches factor model"""
    # Generate large sample for accurate covariance estimation
    returns = default_generator.generate_returns(100000)
    empirical_cov = np.cov(returns.T)
    
    # Theoretical covariance
    reconstructed_cov = (default_generator.beta @ default_generator.factor_cov 
                         @ default_generator.beta.T + default_generator.noise_cov)
    
    # Check reconstruction error
    cov_error = np.linalg.norm(empirical_cov - reconstructed_cov, 'fro')
    # assert cov_error < 0.1
    # With normalized error check:
    relative_error = cov_error / np.linalg.norm(reconstructed_cov, 'fro')
    assert relative_error < 0.05, f"Relative covariance error: {relative_error:.4f}"

def test_eigenvalue_gap(default_generator):
    """Verify significant eigenvalue gap at k-th component"""
    eigvals = default_generator.true_eigenvalues
    k = default_generator.factor_dim
    eig_gap = eigvals[k-1] - eigvals[k]
    assert eig_gap > 0.1, f"Insufficient eigenvalue gap: {eig_gap:.4f}"

def test_r_squared(default_generator):
    """Verify theoretical vs empirical R-squared match"""
    returns = default_generator.generate_returns(100000)
    total_var = np.trace(np.cov(returns.T))
    factor_var = np.trace(default_generator.beta @ default_generator.factor_cov 
                          @ default_generator.beta.T)
    empirical_r2 = factor_var / total_var
    assert np.isclose(default_generator.r_squared, empirical_r2, atol=0.05), \
        f"R² mismatch: Theory={default_generator.r_squared:.4f}, Empirical={empirical_r2:.4f}"
    
def test_return_distribution(default_generator):
    """Test that generated returns follow a multivariate normal distribution"""
    from scipy import stats
    
    returns = default_generator.generate_returns(1000)
    # Use a simplified test for normality (could use more sophisticated tests)
    # Testing marginal distributions
    p_values = []
    for i in range(min(100, default_generator.asset_dim)):  # Test first 100 assets
        _, p = stats.normaltest(returns[:, i])
        p_values.append(p)
    
    # Check if at least 95% of tests pass at 1% significance level
    pass_ratio = np.mean([p > 0.01 for p in p_values])
    assert pass_ratio > 0.95, f"Only {pass_ratio:.2%} of assets pass normality test"

def test_factor_recovery(default_generator):
    """Test that PCA can recover the factor structure"""
    # Generate data
    returns = default_generator.generate_returns(10000)
    
    # Perform PCA
    from sklearn.decomposition import PCA
    pca = PCA(n_components=default_generator.factor_dim)
    pca.fit(returns)
    
    # Get the principal components subspace
    pc_subspace = pca.components_.T
    
    # Compare with true factor subspace
    true_subspace = default_generator.true_factor_subspace
    
    # Calculate subspace angle (note: this is a simplified metric)
    # True metric should account for rotation invariance
    overlap = np.linalg.norm(pc_subspace.T @ true_subspace)
    max_overlap = min(default_generator.factor_dim, default_generator.factor_dim)
    
    # We're looking for high overlap, normalized by maximum possible
    normalized_overlap = overlap / max_overlap
    assert normalized_overlap > 0.8, f"Poor factor recovery: {normalized_overlap:.2f}"

def test_batch_consistency(default_generator):
    """Test consistency across multiple batches"""
    batch1 = default_generator.generate_returns(1000)
    batch2 = default_generator.generate_returns(1000)
    
    # Check mean consistency
    mean1 = np.mean(batch1, axis=0)
    mean2 = np.mean(batch2, axis=0)
    assert np.allclose(mean1, mean2, atol=0.05), "Inconsistent means across batches"
    
    # Check covariance consistency
    cov1 = np.cov(batch1.T)
    cov2 = np.cov(batch2.T)
    rel_error = np.linalg.norm(cov1 - cov2, 'fro') / np.linalg.norm(cov1, 'fro')
    assert rel_error < 0.1, f"Inconsistent covariance across batches: {rel_error:.4f}"

def test_edge_cases():
    """Test edge cases for configurations"""
    # Very small factor dimension
    gen1 = SyntheticDataGenerator(asset_dim=100, factor_dim=1, seed=42)
    assert gen1.r_squared > 0, "Failed with factor_dim=1"
    
    # Equal asset and factor dimensions
    gen2 = SyntheticDataGenerator(asset_dim=10, factor_dim=10, seed=42)
    assert gen2.r_squared > 0.9, "Failed with asset_dim=factor_dim"
    
    # Very large noise to ensure it dominates
    gen3 = SyntheticDataGenerator(
        asset_dim=100, 
        factor_dim=5, 
        noise_std_range=(1.0, 5.0),
        seed=42
    )
    assert gen3.r_squared < 0.5, "Failed with high noise setting"

def test_dataset_shapes(default_generator):
    """Test that dataset generation returns expected shapes"""
    # Test basic dataset
    dataset = default_generator.generate_dataset(
        num_samples=1000, 
        train_ratio=0.7,
        val_ratio=0.15,
        test_ratio=0.15
    )
    
    assert dataset['train'].shape[0] == 700, "Incorrect train set size"
    assert dataset['val'].shape[0] == 150, "Incorrect validation set size"
    assert dataset['test'].shape[0] == 150, "Incorrect test set size"
    
    # Test with reshaping
    reshape_dims = (32, 64)  # 32×64 = 2048
    dataset_reshaped = default_generator.generate_dataset(
        num_samples=1000,
        reshape_2d=reshape_dims
    )
    
    assert dataset_reshaped['train'].shape[1:] == reshape_dims, "Incorrect reshaping"