# Diffusion Factor Model

This repository contains an implementation of the **Diffusion Factor Model** as described in the paper "Diffusion Factor Model: Utilizing Factor Structure for Generating High-Dimensional Returns" by Chen, Xu, Xu, and Zhang (2025).

## Overview

The Diffusion Factor Model is a novel approach that integrates a factor model structure into the framework of diffusion models, specifically designed for generating high-dimensional asset returns. The method addresses the challenge of "dimension curse" in financial applications by leveraging the inherent low-dimensional factor structure in asset returns.

Key features of this implementation:

- **Factor-aware diffusion model**: Incorporates factor structure directly into the score network architecture
- **Score decomposition**: Separates the score function into subspace and complementary components
- **Efficient training**: Enables training with limited sample sizes even for high-dimensional data
- **Portfolio applications**: Evaluates the model on real portfolio construction tasks

## Installation

First, clone the repository:

```bash
git clone https://github.com/yourusername/diffusion-factor-model.git
cd diffusion-factor-model
```

Install the required dependencies:

```bash
pip install -r requirements.txt
```

## Project Structure

```
diffusion_factor_model/
├── data/                  # Data handling modules
├── model/                 # Diffusion model implementation
├── evaluation/            # Evaluation metrics and methods
├── utils/                 # Utility functions
├── experiments/           # Experiment scripts
├── config.py              # Configuration parameters
├── main.py                # Main entry point
└── requirements.txt       # Project dependencies
```

## Usage

### Running Synthetic Data Experiments

This experiment reproduces the synthetic data results from Section 6 of the paper:

```bash
python main.py --experiment synthetic --asset_dim 2048 --factor_dim 16 --num_samples 8192 --output_dir results
```

### Running Portfolio Analysis

This experiment reproduces the portfolio construction results from Section 7 of the paper:

```bash
python main.py --experiment portfolio --data_path path/to/returns.csv --risk_aversion 3.0 --transaction_cost 0.002 --output_dir results
```

For testing with synthetic data instead of real market data:

```bash
python main.py --experiment portfolio --use_synthetic --output_dir results
```

### Running Both Experiments

```bash
python main.py --experiment both --output_dir results
```

## Implementation Details

### Diffusion Process

The implementation uses an Ornstein-Uhlenbeck (O-U) process for the forward diffusion:

```
dR_t = -0.5 * η(t) * R_t * dt + sqrt(η(t)) * dW_t, R_0 ~ P_data, t ∈ [0, T]
```

with a corresponding reverse process:

```
dR_t^← = (0.5 * R_t^← + ∇log p_{T-t}(R_t^←)) * dt + dW_t^←, R_0^← ~ P_T, t ∈ [0, T]
```

### Score Network Architecture

The score network leverages the factor structure by decomposing the score function into:

1. **Subspace score**: Operates in the low-dimensional factor space
2. **Complementary score**: Handles the remaining noise components

This design allows efficient training even with high-dimensional asset returns (e.g., d=2048) and limited data samples.

### Factor Recovery

The model can recover the underlying factor structure by:
- Generating samples from the trained diffusion model
- Performing eigendecomposition on the generated samples' covariance matrix
- Extracting the principal subspace and comparing it to the true factor space

## Experiments

### Synthetic Data Experiment

This experiment evaluates:
- **Distribution quality**: How well the model captures the true distribution
- **Subspace recovery**: How accurately the model recovers the true factor structure
- **Sample efficiency**: Performance across different training sample sizes

### Portfolio Analysis

This experiment evaluates:
- **Mean-variance optimization**: Using diffusion-generated samples for portfolio optimization
- **Factor portfolios**: Constructing factor portfolios based on diffusion-generated factors
- **Out-of-sample performance**: Testing portfolio performance on future returns

## Results

The implementation reproduces the main findings of the paper:

1. The Diffusion Factor Model successfully captures the low-dimensional factor structure in high-dimensional asset returns.
2. The model significantly outperforms empirical estimates when the sample size is small relative to the dimension.
3. Portfolios constructed using diffusion-generated samples achieve higher Sharpe ratios and better risk-adjusted performance.

## Citation

If you use this code, please cite the original paper:

```
@article{chen2025diffusion,
  title={Diffusion Factor Model: Utilizing Factor Structure for Generating High-Dimensional Returns},
  author={Chen, Minshuo and Xu, Renyuan and Xu, Yumin and Zhang, Ruixun},
  journal={SSRN},
  year={2025},
  month={April}
}
```

## License

[MIT License](LICENSE)
