# Sector Rotation Strategy

**Institutional-grade GICS sector ETF rotation strategy built on qlib.**

> Environment: `qlib_run` conda | Language: Python 3.11
> Benchmark: SPY | Universe: 11 SPDR Select Sector ETFs | Rebalance: Monthly

---

## Strategy Summary

Multi-factor sector rotation across 11 GICS SPDR ETFs (XLE, XLB, XLI, XLY, XLP, XLV, XLF, XLK, XLC, XLU, XLRE).

| Dimension       | Design |
|---|---|
| Universe        | 11 SPDR Select Sector ETFs + SPY benchmark |
| Backtest start  | 2018-07-01 (XLC inception; full 11-sector universe) |
| Rebalance       | First trading day of each month |
| Direction       | Long-only (no shorting, no leverage in base config) |
| Target Sharpe   | 0.4–0.6 net of costs |
| Target MaxDD    | < 20% |

### Signal Architecture

| Signal | Weight | Method |
|---|---|---|
| Cross-Sectional Momentum | 40% | 12-1 month relative return, z-scored cross-sectionally |
| Time-Series Momentum | 15% | 12-month self-return crash filter (multiplier) |
| Relative Value | 20% | TTM P/E percentile vs 10-year history (constituent earnings from yfinance; proxy fallback available) |
| Regime Adjustment | 25% | 4-state VIX/spread/ISM rule-based regime → signal weight modifiers |

### Academic Basis

- **Momentum**: Moskowitz, Ooi, Pedersen (2012), *Time Series Momentum*, JFE.
- **Sector dynamics**: Gupta, Kelly (2019, AQR), *Factor Momentum Everywhere*.
- **Regime**: Guidolin, Timmermann (2007), *Asset Allocation under Multivariate Regime Switching*.
- **Value-momentum**: Asness, Moskowitz, Pedersen (2013), *Value and Momentum Everywhere*, JF.

---

## Quick Start

```bash
# All commands must use qlib_run environment
# NEVER use someopark_run for this strategy

# 1. Load data and run a quick smoke test
conda run -n qlib_run --no-capture-output python -c "
from sector_rotation.data.loader import load_all
prices, macro = load_all()
print(prices.tail(3))
"

# 2. Run backtest
conda run -n qlib_run --no-capture-output \
    python qlib-main/sector_rotation/backtest/engine.py

# 3. Generate tearsheet
conda run -n qlib_run --no-capture-output python -c "
from sector_rotation.data.loader import load_all, load_config
from sector_rotation.backtest.engine import SectorRotationBacktest
from sector_rotation.report.tearsheet import generate_tearsheet

cfg = load_config()
prices, macro = load_all(config=cfg)
bt = SectorRotationBacktest(cfg)
result = bt.run(prices, macro)
print(result.summary())
generate_tearsheet(result, prices=prices)
"

# 4. Run unit tests
conda run -n qlib_run --no-capture-output \
    python -m pytest qlib-main/sector_rotation/tests/ -v

# 5. Get current signals
conda run -n qlib_run --no-capture-output python -c "
from sector_rotation.signals.composite import get_current_signals
import json
print(json.dumps(get_current_signals(), indent=2, default=str))
"
```

---

## File Structure

```
sector_rotation/
├── README.md             This file
├── config.yaml           All tunable parameters
│
├── data/
│   ├── universe.py       ETF universe + GICS metadata + liquidity tiers
│   ├── loader.py         Price (MongoDB→yfinance fallback) + FRED macro loader
│   └── cache/            Local disk cache (gitignored)
│
├── signals/
│   ├── momentum.py       CS momentum (12-1m) + TS momentum crash filter + acceleration
│   ├── value.py          P/E percentile relative value signal
│   ├── regime.py         4-state regime detection (rules-based + HMM option)
│   └── composite.py      Multi-factor aggregation with regime conditioning
│
├── portfolio/
│   ├── optimizer.py      Inv-vol / risk-parity / GMV + Ledoit-Wolf cov (qlib)
│   ├── risk.py           Vol scaling + VIX emergency + DD circuit breaker + beta
│   └── rebalance.py      Monthly schedule + threshold filter + turnover cap
│
├── backtest/
│   ├── engine.py         Event-driven monthly backtest + walk-forward support
│   ├── costs.py          Spread + impact cost model by ETF liquidity tier
│   └── metrics.py        Sharpe/Calmar/IR/CVaR/Brinson attribution (qlib + manual)
│
├── report/
│   ├── plots.py          All matplotlib visualization functions
│   └── tearsheet.py      Multi-page PDF tearsheet generator
│
├── tests/
│   ├── test_signals.py      Signal unit tests (no network)
│   ├── test_optimizer.py    Portfolio/risk unit tests
│   ├── test_backtest.py     Cost + metrics unit tests
│   └── test_engine_smoke.py End-to-end smoke tests (synthetic data, no network)
│
└── notebooks/
    ├── 01_data_exploration.ipynb   Sector correlations, rolling stats
    ├── 02_signal_research.ipynb    IC analysis, signal decay curves
    └── 03_backtest_analysis.ipynb  Full backtest deep dive
```

---

## Configuration Reference

All parameters are in `config.yaml`. Key settings:

| Section | Key | Default | Description |
|---|---|---|---|
| `data.price_source` | `"yfinance"` | `"mongodb"` | Primary data source |
| `signals.weights.cross_sectional_momentum` | `0.40` | | CS momentum weight |
| `signals.regime.method` | `"rules"` | `"hmm"` | Regime detection method |
| `portfolio.optimizer` | `"inv_vol"` | `"risk_parity"` | Weight method |
| `portfolio.top_n_sectors` | `4` | | Active sector count |
| `portfolio.constraints.max_weight` | `0.40` | | Single sector cap |
| `rebalance.zscore_change_threshold` | `0.5` | | Signal change before rebalance |
| `risk.vol_scaling.enabled` | `true` | | Volatility-based position scaling |
| `backtest.start_date` | `"2018-07-01"` | | XLC inception date |
| `backtest.initial_capital` | `1_000_000` | | Starting capital USD |

---

## GICS Structural Break Warning

**Backtest must start on or after 2018-07-01.**

XLC (Communication Services ETF) was created 2018-06-18 from the GICS
restructuring of Telecom Services:
- Meta (FB) and Alphabet (GOOGL) moved from XLK → XLC
- Disney (DIS) and Comcast (CMCSA) moved from XLY → XLC

Any backtest starting before 2018-07-01 uses an apples-and-oranges sector
composition. This invalidates cross-sector momentum comparisons (XLK's
momentum signal before 2018-09 includes today's XLC companies).

---

## qlib Integration

Uses qlib components where available, with fallbacks:

| Component | qlib module | Fallback |
|---|---|---|
| Covariance | `qlib.model.riskmodel.ShrinkCovEstimator` | sklearn LedoitWolf |
| Risk metrics | `qlib.backtest.analyze.risk_analysis` | manual computation |
| Portfolio opt | Custom (qlib's PortfolioOptimizer API changed) | scipy.optimize |
| Brinson attribution | `qlib.backtest.profit_attribution.decompose_portofolio` | skipped |
| Trade indicators | `qlib.contrib.evaluate.indicator_analysis` | skipped |
| Experiment tracking | `qlib.workflow.QlibRecorder` + `MLflowExpManager` | skipped |

---

## Expected Performance (Literature-Based)

| Metric | Optimistic | Base | Conservative |
|---|---|---|---|
| Excess return vs SPY | +4% | +2% | +0.5% |
| Sharpe (net) | 0.6 | 0.45 | 0.3 |
| MaxDD | -15% | -20% | -30% |
| Annual turnover | 300% | 400% | 600% |
| Annual transaction cost | 30 bps | 50 bps | 80 bps |

*OOS Sharpe is typically 40% below IS (Cederburg et al. 2023).*

---

## Relationship to someopark System

This strategy is **completely independent** of the someopark pairs trading system:

| | someopark | sector_rotation |
|---|---|---|
| Environment | `someopark_run` | `qlib_run` |
| Framework | Custom | qlib |
| Universe | Individual stocks | Sector ETFs |
| Frequency | Daily | Monthly |
| Direction | Market-neutral | Long-only |
| Expected Sharpe | 2–3 (alpha) | 0.4–0.6 (beta timing) |

**Future integration paths:**
1. Regime signal sharing: sector rotation regime → someopark capital allocation
2. Portfolio-level risk budgeting across both strategies

---

*All code in `qlib_run` conda environment. Never touch `someopark_run`.*
