"""
Robustness & Validation Module  (Phase 6)
==========================================
Statistical validation of backtest results beyond point-estimate metrics.

Contents
--------
1. Sub-period analysis      — 2018-2020 / 2020-2022 / 2022-2024 breakdown
2. Stress test scenarios    — COVID crash (2020-02 to 2020-04), 2022 rate hike cycle
3. Bootstrap confidence intervals — Stationary block bootstrap for Sharpe ratio
4. someopark correlation    — Correlation of sector rotation returns with pairs PnL series

References
----------
Politis, D. N., & Romano, J. P. (1994). The Stationary Bootstrap.
    JASA, 89(428), 1303-1313.
Ledoit, O., & Wolf, M. (2008). Robust performance hypothesis testing with the Sharpe ratio.
    Journal of Empirical Finance, 15(5), 850-859.
Cederburg, S., et al. (2023). Beyond the Status Quo: A Critical Assessment of Lifecycle
    Investment Advice. Journal of Finance.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. Sub-period analysis
# ---------------------------------------------------------------------------

# Named stress and cycle sub-periods
ANALYSIS_SUBPERIODS = [
    # (name, start, end, description)
    ("Full Sample",       "2018-07-01", "2024-12-31", "Full backtest window"),
    ("Pre-COVID Bull",    "2018-07-01", "2020-02-14", "Bull market, QE tailwind"),
    ("COVID Crash",       "2020-02-14", "2020-04-30", "Fastest bear market in history (-34% SPY in 33d)"),
    ("COVID Recovery",    "2020-04-30", "2021-12-31", "Post-crash recovery & growth surge"),
    ("Rate Hike Cycle",   "2022-01-01", "2023-10-31", "Fed funds 0→5.25%, worst bond year since 1788"),
    ("Post-Hike Easing",  "2023-11-01", "2024-12-31", "Soft landing, rate cut expectations"),
    ("2018-2020",         "2018-07-01", "2020-06-30", "Full first two years"),
    ("2020-2022",         "2020-07-01", "2022-06-30", "Pandemic recovery + early tightening"),
    ("2022-2024",         "2022-07-01", "2024-12-31", "Rate hike cycle + normalization"),
]


def subperiod_analysis(
    portfolio_returns: pd.Series,
    benchmark_returns: Optional[pd.Series] = None,
    subperiods: Optional[List[Tuple]] = None,
    periods_per_year: int = 252,
) -> pd.DataFrame:
    """
    Compute performance metrics for multiple sub-periods.

    Parameters
    ----------
    portfolio_returns : pd.Series
        Daily portfolio returns.
    benchmark_returns : pd.Series, optional
        Daily benchmark (SPY) returns.
    subperiods : list of (name, start, end, description) tuples, optional
        Defaults to ANALYSIS_SUBPERIODS.
    periods_per_year : int
        Trading days per year.

    Returns
    -------
    pd.DataFrame
        Index = subperiod names, columns = metrics.
    """
    from .metrics import compute_metrics

    subperiods = subperiods or ANALYSIS_SUBPERIODS
    rows = []

    for item in subperiods:
        name, start, end = item[0], item[1], item[2]
        description = item[3] if len(item) > 3 else ""

        mask = (portfolio_returns.index >= start) & (portfolio_returns.index <= end)
        sub_ret = portfolio_returns[mask]
        if len(sub_ret) < 20:
            logger.debug(f"Skipping subperiod {name}: insufficient data ({len(sub_ret)} days)")
            continue

        bench_sub = None
        if benchmark_returns is not None:
            bm = (benchmark_returns.index >= start) & (benchmark_returns.index <= end)
            bench_sub = benchmark_returns[bm]

        m = compute_metrics(sub_ret, bench_sub, periods_per_year=periods_per_year)
        rows.append({
            "subperiod": name,
            "description": description,
            "start": start,
            "end": end,
            "n_days": len(sub_ret),
            "cagr": m.get("annual_return", float("nan")),
            "vol": m.get("annual_vol", float("nan")),
            "sharpe": m.get("sharpe", float("nan")),
            "max_dd": m.get("max_drawdown", float("nan")),
            "calmar": m.get("calmar", float("nan")),
            "cvar_95": m.get("cvar_95", float("nan")),
            "win_rate": m.get("monthly_win_rate", float("nan")),
            "info_ratio": m.get("info_ratio", float("nan")),
            "active_return": m.get("active_return", float("nan")),
            "total_return": m.get("total_return", float("nan")),
        })

    df = pd.DataFrame(rows).set_index("subperiod")
    return df


# ---------------------------------------------------------------------------
# 2. Stress test scenarios
# ---------------------------------------------------------------------------

STRESS_SCENARIOS = {
    "COVID_crash_2020": {
        "start": "2020-02-14",
        "end": "2020-03-23",
        "description": "COVID crash: SPY -34% in 33 trading days",
        "benchmark_dd": -0.34,
    },
    "COVID_full_drawdown": {
        "start": "2020-02-14",
        "end": "2020-04-30",
        "description": "Full COVID drawdown + initial recovery",
        "benchmark_dd": -0.20,
    },
    "rate_hike_2022": {
        "start": "2022-01-01",
        "end": "2022-12-31",
        "description": "2022 rate hike cycle: SPY -19%, Nasdaq -33%",
        "benchmark_dd": -0.19,
    },
    "inflation_shock_2022H1": {
        "start": "2022-01-01",
        "end": "2022-06-30",
        "description": "H1 2022 inflation shock (worst first half since 1970)",
        "benchmark_dd": -0.20,
    },
    "tech_selloff_2022": {
        "start": "2021-11-22",
        "end": "2022-10-13",
        "description": "Tech bubble deflation: QQQ -36% peak-to-trough",
        "benchmark_dd": -0.28,
    },
}


def stress_test(
    portfolio_returns: pd.Series,
    benchmark_returns: Optional[pd.Series] = None,
    scenarios: Optional[Dict] = None,
) -> pd.DataFrame:
    """
    Evaluate strategy performance during identified stress scenarios.

    Returns pd.DataFrame comparing strategy vs benchmark during each scenario.
    """
    scenarios = scenarios or STRESS_SCENARIOS
    rows = []

    for scenario_name, sc in scenarios.items():
        start, end = sc["start"], sc["end"]
        mask = (portfolio_returns.index >= start) & (portfolio_returns.index <= end)
        sub = portfolio_returns[mask]

        if len(sub) < 5:
            logger.debug(f"Stress test {scenario_name}: insufficient data")
            continue

        total_ret = float((1 + sub).prod() - 1)
        max_dd = float(((1 + sub).cumprod() / (1 + sub).cumprod().expanding().max() - 1).min())
        vol = float(sub.std() * np.sqrt(252))

        row = {
            "scenario": scenario_name,
            "description": sc.get("description", ""),
            "start": start,
            "end": end,
            "n_days": len(sub),
            "strategy_return": total_ret,
            "strategy_max_dd": max_dd,
            "strategy_vol": vol,
            "benchmark_expected_dd": sc.get("benchmark_dd", float("nan")),
        }

        if benchmark_returns is not None:
            bm_mask = (benchmark_returns.index >= start) & (benchmark_returns.index <= end)
            bm_sub = benchmark_returns[bm_mask]
            if len(bm_sub) > 0:
                bm_ret = float((1 + bm_sub).prod() - 1)
                row["benchmark_actual_return"] = bm_ret
                row["active_return"] = total_ret - bm_ret
                row["relative_dd_protection"] = max_dd - float(
                    ((1 + bm_sub).cumprod() / (1 + bm_sub).cumprod().expanding().max() - 1).min()
                )

        rows.append(row)

    return pd.DataFrame(rows).set_index("scenario")


# ---------------------------------------------------------------------------
# 3. Bootstrap confidence intervals
# ---------------------------------------------------------------------------

def stationary_block_bootstrap(
    returns: pd.Series,
    statistic_fn,
    n_bootstrap: int = 1000,
    block_length: Optional[int] = None,
    confidence: float = 0.95,
    random_state: int = 42,
) -> Tuple[float, float, float, np.ndarray]:
    """
    Stationary block bootstrap for time-series statistics.

    Preserves temporal dependence structure by resampling overlapping blocks
    of random length drawn from a geometric distribution.

    Parameters
    ----------
    returns : pd.Series
        Daily returns time series.
    statistic_fn : callable
        Function(pd.Series) → float. E.g., lambda r: sharpe_ratio(r).
    n_bootstrap : int
        Number of bootstrap resamples (default 1000).
    block_length : int, optional
        Expected block length. If None, uses sqrt(T) heuristic.
    confidence : float
        Confidence level for the interval (default 0.95).
    random_state : int
        RNG seed for reproducibility.

    Returns
    -------
    (point_estimate, ci_lower, ci_upper, bootstrap_distribution)
        point_estimate : statistic on original data
        ci_lower, ci_upper : (1-confidence)/2 percentiles of bootstrap dist
        bootstrap_distribution : full array of bootstrap statistics
    """
    rng = np.random.default_rng(random_state)
    n = len(returns)

    if block_length is None:
        block_length = max(1, int(np.sqrt(n)))  # Politis & Romano heuristic

    point_estimate = statistic_fn(returns)
    bootstrap_stats = np.full(n_bootstrap, float("nan"))

    for b in range(n_bootstrap):
        # Geometric block lengths (stationary bootstrap)
        resampled = []
        while len(resampled) < n:
            start_idx = rng.integers(0, n)
            bl = rng.geometric(1.0 / block_length)
            bl = min(bl, n - len(resampled))
            end_idx = start_idx + bl
            if end_idx <= n:
                resampled.extend(returns.iloc[start_idx:end_idx].tolist())
            else:
                # Wrap around
                resampled.extend(returns.iloc[start_idx:].tolist())
                resampled.extend(returns.iloc[:end_idx - n].tolist())

        bootstrap_sample = pd.Series(resampled[:n], index=returns.index)
        try:
            bootstrap_stats[b] = statistic_fn(bootstrap_sample)
        except Exception:
            bootstrap_stats[b] = float("nan")

    # Remove NaN
    valid_stats = bootstrap_stats[~np.isnan(bootstrap_stats)]
    alpha = (1 - confidence) / 2
    ci_lower = float(np.percentile(valid_stats, alpha * 100))
    ci_upper = float(np.percentile(valid_stats, (1 - alpha) * 100))

    logger.info(
        f"Bootstrap ({n_bootstrap} resamples, block_length={block_length}): "
        f"point={point_estimate:.3f}, CI=[{ci_lower:.3f}, {ci_upper:.3f}]"
    )
    return point_estimate, ci_lower, ci_upper, valid_stats


def bootstrap_sharpe(
    returns: pd.Series,
    n_bootstrap: int = 1000,
    confidence: float = 0.95,
    risk_free: float = 0.0,
    random_state: int = 42,
) -> Dict:
    """
    Bootstrap confidence interval for the Sharpe ratio.

    Returns dict with:
        point_estimate, ci_lower, ci_upper, p_value_positive,
        bootstrap_mean, bootstrap_std, n_valid_samples
    """
    from .metrics import annualized_return, annualized_vol

    def sharpe_fn(r: pd.Series) -> float:
        ann_ret = annualized_return(r)
        ann_vol = annualized_vol(r)
        if np.isnan(ann_vol) or ann_vol == 0:
            return float("nan")
        return (ann_ret - risk_free) / ann_vol

    pt, ci_lo, ci_hi, dist = stationary_block_bootstrap(
        returns, sharpe_fn,
        n_bootstrap=n_bootstrap,
        confidence=confidence,
        random_state=random_state,
    )

    # p-value: fraction of bootstrap samples with Sharpe ≤ 0
    p_value_positive = float((dist > 0).mean())

    return {
        "point_estimate": round(pt, 4),
        "ci_lower": round(ci_lo, 4),
        "ci_upper": round(ci_hi, 4),
        "confidence_level": confidence,
        "p_value_positive": round(p_value_positive, 4),
        "bootstrap_mean": round(float(np.mean(dist)), 4),
        "bootstrap_std": round(float(np.std(dist)), 4),
        "n_valid_samples": int((~np.isnan(dist)).sum()),
        "n_bootstrap": n_bootstrap,
        "statistically_significant": ci_lo > 0,
    }


def bootstrap_calmar(
    returns: pd.Series,
    n_bootstrap: int = 500,
    confidence: float = 0.95,
    random_state: int = 42,
) -> Dict:
    """Bootstrap CI for Calmar ratio."""
    from .metrics import annualized_return, max_drawdown

    def calmar_fn(r: pd.Series) -> float:
        ann_ret = annualized_return(r)
        mdd, _ = max_drawdown(r)
        if np.isnan(mdd) or mdd == 0:
            return float("nan")
        return ann_ret / abs(mdd)

    pt, ci_lo, ci_hi, dist = stationary_block_bootstrap(
        returns, calmar_fn,
        n_bootstrap=n_bootstrap,
        confidence=confidence,
        random_state=random_state,
    )
    return {
        "point_estimate": round(pt, 4),
        "ci_lower": round(ci_lo, 4),
        "ci_upper": round(ci_hi, 4),
        "statistically_significant": ci_lo > 0,
    }


def full_bootstrap_report(
    portfolio_returns: pd.Series,
    benchmark_returns: Optional[pd.Series] = None,
    n_bootstrap: int = 1000,
    confidence: float = 0.95,
) -> pd.DataFrame:
    """
    Run bootstrap CI for Sharpe, Calmar, and Information Ratio.

    Returns DataFrame with one row per statistic.
    """
    rows = []

    # Sharpe
    sr = bootstrap_sharpe(portfolio_returns, n_bootstrap=n_bootstrap, confidence=confidence)
    rows.append({"statistic": "Sharpe Ratio", **sr})

    # Calmar
    cal = bootstrap_calmar(portfolio_returns, n_bootstrap=n_bootstrap // 2, confidence=confidence)
    rows.append({"statistic": "Calmar Ratio", **{k: v for k, v in cal.items()}})

    # Information Ratio (if benchmark available)
    if benchmark_returns is not None:
        from .metrics import annualized_return, annualized_vol

        aligned = pd.concat([portfolio_returns, benchmark_returns], axis=1).dropna()
        active = aligned.iloc[:, 0] - aligned.iloc[:, 1]

        def ir_fn(r: pd.Series) -> float:
            ann_act = annualized_return(r)
            te = annualized_vol(r)
            return ann_act / te if te > 0 else float("nan")

        pt, ci_lo, ci_hi, dist = stationary_block_bootstrap(
            active, ir_fn, n_bootstrap=n_bootstrap, confidence=confidence
        )
        rows.append({
            "statistic": "Information Ratio",
            "point_estimate": round(pt, 4),
            "ci_lower": round(ci_lo, 4),
            "ci_upper": round(ci_hi, 4),
            "statistically_significant": ci_lo > 0,
        })

    return pd.DataFrame(rows).set_index("statistic")


# ---------------------------------------------------------------------------
# 4. someopark correlation analysis
# ---------------------------------------------------------------------------

def load_someopark_pnl(
    strategy_performance_path: Optional[str] = None,
) -> Optional[pd.Series]:
    """
    Load someopark daily P&L series from strategy_performance.json.

    This is used to compute correlation between sector rotation and
    someopark pairs trading — the two strategies should be complementary
    (low correlation → better diversification).

    Parameters
    ----------
    strategy_performance_path : str, optional
        Path to strategy_performance.json.
        Default: someo-park-investment-management/public/data/strategy_performance.json

    Returns
    -------
    pd.Series or None
        Daily P&L returns (simple). None if file not found.
    """
    from pathlib import Path as P

    if strategy_performance_path is None:
        # Try default path relative to project root
        candidates = [
            P("/Users/xuling/code/someopark-test/someo-park-investment-management/public/data/strategy_performance.json"),
            P("someo-park-investment-management/public/data/strategy_performance.json"),
        ]
        path = next((c for c in candidates if c.exists()), None)
        if path is None:
            logger.warning("strategy_performance.json not found. Skipping someopark correlation.")
            return None
    else:
        path = P(strategy_performance_path)

    try:
        import json
        with open(path) as f:
            data = json.load(f)

        # Expected structure: {"equity_curve": [{"date": "YYYY-MM-DD", "value": float}, ...]}
        # or {"daily_pnl": [{"date": ..., "pnl": float}, ...]}
        if "equity_curve" in data:
            records = data["equity_curve"]
            df = pd.DataFrame(records)
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            value_col = [c for c in df.columns if "value" in c.lower() or "equity" in c.lower()]
            if value_col:
                equity = df[value_col[0]].dropna()
                returns = equity.pct_change().dropna()
                returns.name = "someopark_pairs"
                return returns
        logger.warning(f"Unexpected structure in strategy_performance.json at {path}")
        return None
    except Exception as e:
        logger.warning(f"Failed to load someopark P&L: {e}")
        return None


def someopark_correlation_analysis(
    sector_rotation_returns: pd.Series,
    someopark_returns: Optional[pd.Series] = None,
    strategy_performance_path: Optional[str] = None,
) -> Dict:
    """
    Compute correlation between sector rotation and someopark pairs trading.

    Goal: verify complementarity (expected correlation < 0.2).

    Parameters
    ----------
    sector_rotation_returns : pd.Series
        Daily sector rotation portfolio returns.
    someopark_returns : pd.Series, optional
        Pre-loaded someopark daily returns.
    strategy_performance_path : str, optional
        Path to strategy_performance.json if someopark_returns not provided.

    Returns
    -------
    dict with correlation metrics:
        full_sample_corr, rolling_corr (pd.Series), diversification_ratio
    """
    if someopark_returns is None:
        someopark_returns = load_someopark_pnl(strategy_performance_path)

    if someopark_returns is None:
        return {"error": "someopark P&L data not available"}

    aligned = pd.concat([
        sector_rotation_returns.rename("sector_rotation"),
        someopark_returns.rename("someopark"),
    ], axis=1).dropna()

    if len(aligned) < 20:
        return {"error": f"Insufficient overlapping data ({len(aligned)} days)"}

    full_corr = float(aligned.corr().loc["sector_rotation", "someopark"])

    # Rolling 63-day correlation
    rolling_corr = (
        aligned["sector_rotation"]
        .rolling(63, min_periods=21)
        .corr(aligned["someopark"])
    )

    # Diversification ratio: combined portfolio vol / weighted avg vol
    w = 0.5  # Equal weight
    combined = w * aligned["sector_rotation"] + (1 - w) * aligned["someopark"]
    vol_combined = float(combined.std() * np.sqrt(252))
    vol_sr = float(aligned["sector_rotation"].std() * np.sqrt(252))
    vol_sp = float(aligned["someopark"].std() * np.sqrt(252))
    weighted_avg_vol = w * vol_sr + (1 - w) * vol_sp
    diversification_ratio = weighted_avg_vol / vol_combined if vol_combined > 0 else float("nan")

    # Combined portfolio Sharpe
    from .metrics import annualized_return, sharpe_ratio
    combined_sharpe = sharpe_ratio(combined)
    sr_sharpe = sharpe_ratio(aligned["sector_rotation"])
    sp_sharpe = sharpe_ratio(aligned["someopark"])

    return {
        "n_overlapping_days": len(aligned),
        "overlap_start": str(aligned.index[0].date()),
        "overlap_end": str(aligned.index[-1].date()),
        "full_sample_correlation": round(full_corr, 4),
        "rolling_corr_mean": round(float(rolling_corr.mean()), 4),
        "rolling_corr_std": round(float(rolling_corr.std()), 4),
        "is_complementary": abs(full_corr) < 0.3,
        "diversification_ratio": round(diversification_ratio, 4),
        "sector_rotation_sharpe": round(sr_sharpe, 4),
        "someopark_sharpe": round(sp_sharpe, 4),
        "combined_50_50_sharpe": round(combined_sharpe, 4),
        "rolling_corr": rolling_corr,
    }


# ---------------------------------------------------------------------------
# Full Phase 6 report
# ---------------------------------------------------------------------------

def run_phase6_validation(
    result,
    n_bootstrap: int = 1000,
    someopark_path: Optional[str] = None,
) -> Dict:
    """
    Run all Phase 6 validation checks on a BacktestResult.

    Parameters
    ----------
    result : BacktestResult
        Output from SectorRotationBacktest.run().
    n_bootstrap : int
        Bootstrap samples for CI estimation.
    someopark_path : str, optional
        Path to strategy_performance.json.

    Returns
    -------
    dict with keys:
        subperiods, stress_tests, bootstrap, someopark_corr
    """
    logger.info("=== Phase 6: Robustness Validation ===")

    ret = result.daily_returns
    bench = result.benchmark_returns

    # 1. Sub-period analysis
    logger.info("Running sub-period analysis...")
    subperiods_df = subperiod_analysis(ret, bench)

    # 2. Stress tests
    logger.info("Running stress tests...")
    stress_df = stress_test(ret, bench)

    # 3. Bootstrap CIs
    logger.info(f"Running bootstrap CI ({n_bootstrap} samples)...")
    bootstrap_df = full_bootstrap_report(ret, bench, n_bootstrap=n_bootstrap)

    # 4. someopark correlation
    logger.info("Computing someopark correlation...")
    corr_result = someopark_correlation_analysis(ret, strategy_performance_path=someopark_path)

    # Print summary
    logger.info("\n--- Sub-period Sharpe ---")
    if not subperiods_df.empty:
        logger.info(subperiods_df[["cagr", "sharpe", "max_dd"]].round(3).to_string())

    logger.info("\n--- Stress Tests ---")
    if not stress_df.empty:
        cols = [c for c in ["strategy_return", "benchmark_actual_return", "active_return"] if c in stress_df.columns]
        logger.info(stress_df[cols].round(3).to_string())

    logger.info("\n--- Bootstrap Confidence Intervals ---")
    logger.info(bootstrap_df.round(4).to_string())

    if "error" not in corr_result:
        logger.info(f"\n--- someopark Correlation: {corr_result['full_sample_correlation']:.4f} ---")
        logger.info(f"Complementary: {corr_result['is_complementary']}")
        logger.info(f"Diversification ratio: {corr_result['diversification_ratio']:.4f}")

    return {
        "subperiods": subperiods_df,
        "stress_tests": stress_df,
        "bootstrap": bootstrap_df,
        "someopark_corr": corr_result,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    # Smoke test with synthetic returns
    np.random.seed(42)
    n = 252 * 6
    ret = pd.Series(
        np.random.normal(0.0005, 0.01, n),
        index=pd.date_range("2018-07-01", periods=n, freq="B"),
    )
    bench = pd.Series(
        np.random.normal(0.0003, 0.012, n),
        index=ret.index,
    )

    # Bootstrap test
    result = bootstrap_sharpe(ret, n_bootstrap=200)
    print("\n=== Bootstrap Sharpe ===")
    for k, v in result.items():
        print(f"  {k:<30}: {v}")

    # Sub-period test
    sp = subperiod_analysis(ret, bench)
    print("\n=== Sub-period Analysis ===")
    print(sp[["cagr", "sharpe", "max_dd"]].round(3))

    # Stress test
    st = stress_test(ret, bench)
    print("\n=== Stress Tests ===")
    print(st[["strategy_return", "strategy_max_dd"]].round(3))
