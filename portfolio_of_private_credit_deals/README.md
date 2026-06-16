# Institutional-Quality Fixed Income Portfolio Analytics

## Overview

Advanced fixed income portfolio analysis system with Credit Risk Modeling, AI-powered PDF processing, and comprehensive stress testing. Features both static assumptions and fundamental-based credit analytics with risk-adjusted pricing.

## Core Python Files

### Main Scripts
- `run_deals.py` - Real deal portfolio analytics with advanced credit risk modeling and PDF memo processing
- `run_synthetic.py` - Synthetic 150-instrument fixed income portfolio modeling

### Credit Risk & Analytics Modules
- `credit_risk_module.py` - NEW: Advanced credit risk integration with toggle between static/fundamental models
- `bond_utilities.py` - Mathematical functions for bonds and loans (IRR, duration, pricing)
- `forward_rate_lookup.py` - Rate lookup system with historical FRED data and forward projections
- `forward_rate_projections.py` - Nelson-Siegel yield curve forward rate generation
- `yield_curve_modeling.py` - Nelson-Siegel yield curve implementation
- `enriched_bond_portfolio.py` - Enhanced bond portfolio analysis with OU modeling
- `cashflow_exporter.py` - Professional cashflow export functions
- `download_fred_data.py` - FRED economic data download and integration

### Configuration
- `config_template.py` - Template for API key configuration (copy to config.py)

## Project Structure
```
├── run_deals.py                 # Main analysis script with credit risk modeling
├── credit_risk_module.py        # Credit risk integration module (NEW)
├── bond_utilities.py           # Core financial calculations
├── forward_rate_*.py           # Rate modeling and projections  
├── config_template.py          # API configuration template
├── requirements.txt            # Dependencies with organized sections
├── deals_data/                 # Portfolio data and CSV files
├── logs/                       # Timestamped analysis logs (71+ entries)
├── cashflows_YYYYMMDD_HHMMSS/  # Generated cashflow schedules
└── forward_rates_YYYYMMDD_HHMMSS/ # Rate projections
```

## Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Setup Configuration
```bash
cp config_template.py config.py
# Edit config.py and add your OpenAI API key
```

### 2.1. Verify Installation
```python
# Test basic imports
from run_deals import PrivateLoanPortfolioAnalyzer
from credit_risk_module import get_cashflow_attributes
print("✅ Installation successful!")
```

### 3. Run Analysis

#### Real Deal Portfolio Analysis (Advanced Credit Mode - Default)
```bash
python run_deals.py
```
- 🔧 Credit Risk Mode: Advanced (uses credit scores and risk-adjusted spreads)
- Processes PDF memos automatically
- Generates comprehensive portfolio analytics with fundamental-based credit modeling
- Creates detailed loan cashflows (base + stressed scenarios)
- Output: `logs/deals_analysis_YYYYMMDD_HHMMSS.log`

#### Alternative: Static Credit Mode
```python
from run_deals import run_portfolio_analysis_with_source
run_portfolio_analysis_with_source(advanced_credit_mode=False)
```
- 🔧 Credit Risk Mode: Static (uses fixed assumptions)
- Uses uniform 40% recovery rates and original default probabilities

#### Synthetic Portfolio Analysis
```bash
python run_synthetic.py  
```
- Analyzes 150 synthetic fixed income instruments
- OU mean-reverting bond models
- Professional portfolio optimization
- Output: `logs/synthetic_analysis_YYYYMMDD_HHMMSS.log`

## Expected Output Examples

### Console Output (Advanced Mode)
```
🔧 Credit Risk Mode: Advanced
   ✅ Using credit scores, risk-adjusted spreads, and fundamental-based default/recovery modeling
Loading CSV data files from source: deals_data/deal_start_unstructured.csv
Loaded deals_data/deal_start_unstructured.csv: 5 records
Combined portfolio: 10 total loans

🏦 Processing DEAL_001 - Mode: Advanced
   📊 Credit Score: 59.4
   📈 Risk-Adj Spread: 0.00%
   🎯 Adj Default Prob: 4.50%
   🛡️ Recovery Rate: 35.00%

✅ Private loan portfolio analysis completed successfully!
✅ Credit Risk Mode: Advanced
```

### Log File Output (71+ entries)
```
2025-08-30 01:29:27,512 - INFO - 🔧 Credit Risk Mode: Advanced
2025-08-30 01:29:27,533 - INFO - Combined portfolio: 10 total loans
2025-08-30 01:29:27,533 - INFO - 🏦 Processing DEAL_001 - Mode: Advanced
2025-08-30 01:29:27,533 - INFO -    📊 Credit Score: 59.4
```

### Generated Files
```
logs/deals_analysis_20250830_012927.log          # Complete audit trail (5KB)
private_loan_portfolio_analytics_20250830.png    # 9-panel dashboard
cashflows_20250830_012927/                       # Individual loan cashflows
forward_rates_stressed_20250830_012927/           # Stressed rate scenarios
credit_risk_analysis_20250830.txt                # Credit mode comparison
```

## Performance Benchmarks
- Portfolio Size: Up to 150+ instruments tested
- Processing Time: ~30 seconds for 10-loan portfolio with full analysis
- Memory Usage: ~200MB for comprehensive analytics
- Log Generation: 71+ detailed log entries per run
- Output Files: 5-8 analysis files generated per run

## Key Deliverables

### Portfolio Metrics
- Weighted average coupon, maturity, and effective duration
- Sector and currency exposures (% of market value)
- Top 5 issuers by exposure
- Advanced Credit Risk Metrics: Credit scores, risk-adjusted spreads, fundamental-based PD/LGD
- Mode Comparison: Static vs Advanced credit modeling impact analysis

### Stress Testing & Credit Analytics
- Credit-Differentiated Stress: Custom parameters based on credit scores (80%-200% multipliers)
- Risk-Adjusted Pricing: Leverage-based spread adjustments (25bp per turn above 3x)
- Recovery Rate Modeling: Credit score-driven recovery rates (35%-75%)
- Expected Loss Calculation: PD × LGD × EAD with fundamental inputs
- Default Probability Adjustment: Credit score-based PD scaling (50%-200% of base)
- Aggregated portfolio impact analysis with credit risk attribution

### Visualization
- 9-panel analytics dashboard
- Sector weights and leverage analysis
- EBITDA distribution with stress thresholds
- Credit rating and maturity profiles

## Data Structure

### Input Data
- `deals_data/` - Real deal data from PDF memos
- `synthetic_data/` - Synthetic bonds and loans for modeling
- `memos_structured/` - Structured PDF investment memos
- `memos_unstructured/` - Unstructured PDF investment memos

### Generated Output
- `cashflows_YYYYMMDD_HHMMSS/` - Detailed loan cashflows (base/stressed)
- `forward_rates_YYYYMMDD_HHMMSS/` - Forward rate projections
- `logs/` - Analysis logs with timestamps
- `private_loan_portfolio_analytics_*.png` - Dashboard visualizations

## Technical Features

### NEW: Advanced Credit Risk Integration
- Dual Mode System: Toggle between Static (fixed assumptions) and Advanced (fundamental modeling)
- Credit Score Calculation: EBITDA, leverage, growth, and sector-based scoring
- Risk-Adjusted Spreads: Instrument-specific spread adjustments using fundamental metrics
- Dynamic Default/Recovery: Credit score-driven PD and LGD calculations
- Stress Testing Calibration: Credit quality-based stress parameter customization

### Advanced Rate Modeling
- No static rates - All rates from real market data or proper error handling
- Temporal logic - Historical FRED data for past, forward curves for future
- Stress scenarios - Proper application to historical vs forward rates
- Credit-Enhanced Cashflows: Default probability haircuts and expected payment calculations

### AI Integration
- PDF processing - Automatic memo data extraction
- Smart caching - Skips expensive processing when data exists  
- Priority logic - AI-extracted unstructured data preferred over structured

### Professional Standards
- Institutional format - Matches existing loan infrastructure
- Comprehensive logging - Every operation logged to timestamped files (71+ log entries per run)
- Error handling - Graceful failures when data unavailable
- Modular design - Clean separation of concerns with dedicated credit risk module

## Requirements

See `requirements.txt` for full dependency list. Key packages:
- pandas>=2.2.2, numpy>=1.26.4 - Data analysis and portfolio modeling
- matplotlib>=3.9.1, seaborn>=0.13.2 - Visualization and analytics dashboards
- openai==0.28.0 - AI-powered PDF memo processing
- pdfplumber>=0.11.7 - PDF text extraction and parsing
- scipy>=1.14.0, scikit-learn>=1.5.1 - Mathematical calculations and modeling
- fredapi>=0.5.2 - Economic data integration
- statsmodels>=0.14.2 - Advanced statistical modeling

## Credit Risk Modeling Usage

### Advanced Mode Examples
```python
from credit_risk_module import get_cashflow_attributes, compare_cashflow_modes

# Get credit-adjusted attributes for single loan
loan_data = {
    'credit_score': 72.6, 'leverage': 4.4, 'ebitda': 95_000_000,
    'risk_adjusted_spread': 0.071, 'instrument': 'Unitranche Term Loan'
}
attrs = get_cashflow_attributes(loan_data, advanced_mode=True)
print(f"Adjusted Default Prob: {attrs.default_prob:.2%}")  # 4.00%
print(f"Recovery Rate: {attrs.recovery_rate:.2%}")         # 50.00%

# Compare portfolio-level impacts
import pandas as pd
portfolio_df = pd.read_csv('deal_start_structured.csv')
comparison = compare_cashflow_modes(portfolio_df)
print(f"Expected loss change: {comparison['impact_analysis']['expected_loss_change']:+.1f}%")
```

### Credit Score Examples by Quality
- DEAL_004 (Score: 117.3): 1.00% default, 75.00% recovery - *Excellent credit quality*
- DEAL_003 (Score: 72.6): 4.00% default, 50.00% recovery - *Average credit quality*  
- DEAL_001 (Score: 59.4): 4.50% default, 35.00% recovery - *Below average credit quality*

## API Reference

### Core Functions
```python
# Main Analysis
from run_deals import run_portfolio_analysis_with_source
result = run_portfolio_analysis_with_source(
    data_source='deals_data/deal_start_unstructured.csv',
    advanced_credit_mode=True  # Toggle credit modeling
)

# Credit Risk Analysis
from credit_risk_module import get_cashflow_attributes, compare_cashflow_modes
attrs = get_cashflow_attributes(loan_data, advanced_mode=True)
comparison = compare_cashflow_modes(portfolio_df)

# Direct Class Usage
from run_deals import PrivateLoanPortfolioAnalyzer
analyzer = PrivateLoanPortfolioAnalyzer(advanced_credit_mode=False)
analyzer.run_analysis()
```

### Key Parameters
- `advanced_credit_mode`: `bool` - Use credit scores and risk-adjusted spreads (default: `True`)
- `data_source`: `str` - Path to CSV data file (default: unstructured PDF data)
- `loan_data`: `dict` - Loan metadata with credit_score, leverage, ebitda, etc.

## Troubleshooting

### Common Issues

"ModuleNotFoundError: No module named 'config'"
```bash
# Solution: Copy and configure API keys
cp config_template.py config.py
# Edit config.py and add your OpenAI API key
```

"FileNotFoundError: deals_data/portfolio.csv not found"
```bash
# Solution: Ensure data files exist
ls deals_data/  # Should show CSV files
# Or run with different data source
```

"Forward rates not available" warnings
- Cause: FRED API issues or missing historical data
- Solution: Script continues with cached data, warnings are non-fatal

Memory issues with large portfolios
- Cause: Processing 200+ instruments with full cashflow generation
- Solution: Reduce portfolio size or increase system memory



---

*Professional fixed income analytics with institutional-quality credit risk modeling and comprehensive stress testing capabilities.*
