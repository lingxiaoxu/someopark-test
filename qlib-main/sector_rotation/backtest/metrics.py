"""
Performance Metrics
===================
Comprehensive performance analytics using qlib's risk_analysis where available,
with full custom implementations as fallback.

Metrics computed:
    Core:
        - Annualized return
        - Annualized volatility
        - Sharpe ratio (annualized, risk-free = 0 or configurable)
        - Calmar ratio (annualized return / max drawdown)
        - Information Ratio vs benchmark
        - CAGR (compound annual growth rate)

    Risk:
        - Maximum Drawdown (MDD) and duration
        - 95% Conditional Value-at-Risk (CVaR)
        - 99% CVaR
        - Monthly win rate
        - Skewness, Kurtosis

    Turnover:
        - Annualized turnover (single-side)
        - Average sectors traded per month

    Attribution (Brinson):
        - Allocation effect
        - Selection effect (simplified)
        - Total active return

qlib risk_analysis integration:
    Wraps qlib.backtest.analyze.risk_analysis.RiskAnalysis if available.
    Falls back to manual computation for all metrics.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# qlib risk_analysis import
# ---------------------------------------------------------------------------

import sys
import io as _io

_qlib_metrics_stderr = sys.stderr
sys.stderr = _io.StringIO()

try:
    from qlib.contrib.evaluate import risk_analysis as qlib_risk_analysis
    _QLIB_RISK_AVAILABLE = True
    logger.debug("qlib risk_analysis (qlib.contrib.evaluate) loaded.")
except Exception:
    _QLIB_RISK_AVAILABLE = False
    logger.debug("qlib risk_analysis not available. Using manual computation.")

try:
    from qlib.contrib.evaluate_portfolio import (
        get_max_drawdown_from_series as qlib_max_drawdown,
        get_sharpe_ratio_from_return_series as qlib_sharpe,
        get_annaul_return_from_return_series as qlib_annual_return,
        get_beta as qlib_get_beta,
        get_rank_ic,
        get_normal_ic,
    )
    _QLIB_PORTFOLIO_EVAL_AVAILABLE = True
    logger.debug("qlib evaluate_portfolio functions loaded.")
except Exception:
    _QLIB_PORTFOLIO_EVAL_AVAILABLE = False
    logger.debug("qlib evaluate_portfolio not available. Using manual fallback.")

sys.stderr = _qlib_metrics_stderr


# ---------------------------------------------------------------------------
# Core metric functions
# ---------------------------------------------------------------------------

def annualized_return(returns: pd.Series, periods_per_year: int = 252) -> float:
    """Annualized arithmetic return from daily returns."""
    if len(returns) == 0:
        return float("nan")
    total = (1 + returns).prod()
    years = len(returns) / periods_per_year
    return float(total ** (1 / years) - 1) if years > 0 else float("nan")


def annualized_vol(returns: pd.Series, periods_per_year: int = 252) -> float:
    """Annualized volatility from daily returns."""
    if len(returns) < 2:
        return float("nan")
    return float(returns.std() * np.sqrt(periods_per_year))


def sharpe_ratio(
    returns: pd.Series,
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """
    Annualized Sharpe ratio.

    Primary: qlib ``get_sharpe_ratio_from_return_series`` (compound interest, 250 days).
    Fallback: (annualized_return - rf) / annualized_vol using 252 days.
    """
    if len(returns) < 2:
        return float("nan")

    if _QLIB_PORTFOLIO_EVAL_AVAILABLE:
        try:
            return float(qlib_sharpe(returns, risk_free_rate=risk_free_rate, method="ci"))
        except Exception:
            pass

    ann_ret = annualized_return(returns, periods_per_year)
    ann_vol = annualized_vol(returns, periods_per_year)
    if np.isnan(ann_ret) or np.isnan(ann_vol) or ann_vol == 0:
        return float("nan")
    return float((ann_ret - risk_free_rate) / ann_vol)


def max_drawdown(returns: pd.Series) -> Tuple[float, int]:
    """
    Maximum drawdown and its duration (in days).

    Returns
    -------
    (max_dd, duration_days) : (float, int)
        max_dd is negative (e.g., -0.25 = -25% drawdown).
        duration_days is the number of days from peak to trough.

    Notes
    -----
    Max drawdown value computed via qlib's ``get_max_drawdown_from_series``
    (compounded cumulative curve). Duration computed locally (no qlib equivalent).
    """
    if len(returns) == 0:
        return float("nan"), 0

    # Max drawdown value: qlib primary, manual fallback
    if _QLIB_PORTFOLIO_EVAL_AVAILABLE:
        try:
            mdd = float(qlib_max_drawdown(returns))
        except Exception:
            mdd = None
    else:
        mdd = None

    if mdd is None:
        cumulative = (1 + returns).cumprod()
        peak = cumulative.expanding().max()
        dd = (cumulative / peak) - 1.0
        mdd = float(dd.min())
    else:
        cumulative = (1 + returns).cumprod()
        peak = cumulative.expanding().max()
        dd = (cumulative / peak) - 1.0

    # Duration: time from peak to worst trough (manual — no qlib equivalent)
    trough_idx = dd.idxmin()
    peak_idx = cumulative[:trough_idx].idxmax() if len(cumulative[:trough_idx]) > 0 else trough_idx
    duration = (trough_idx - peak_idx).days if trough_idx > peak_idx else 0

    return mdd, duration


def calmar_ratio(returns: pd.Series, periods_per_year: int = 252) -> float:
    """Calmar = annualized return / |max drawdown|."""
    ann_ret = annualized_return(returns, periods_per_year)
    mdd, _ = max_drawdown(returns)
    if np.isnan(ann_ret) or np.isnan(mdd) or mdd == 0:
        return float("nan")
    return float(ann_ret / abs(mdd))


def information_ratio(
    portfolio_returns: pd.Series,
    benchmark_returns: pd.Series,
    periods_per_year: int = 252,
) -> float:
    """
    Information Ratio = annualized active return / tracking error.

    active_return = portfolio - benchmark (daily)
    """
    aligned = pd.concat([portfolio_returns, benchmark_returns], axis=1).dropna()
    if len(aligned) < 20:
        return float("nan")
    active = aligned.iloc[:, 0] - aligned.iloc[:, 1]
    ann_active = annualized_return(active, periods_per_year)
    te = annualized_vol(active, periods_per_year)
    if np.isnan(te) or te == 0:
        return float("nan")
    return float(ann_active / te)


def cvar(returns: pd.Series, confidence: float = 0.95) -> float:
    """
    Conditional Value-at-Risk (Expected Shortfall) at given confidence level.

    CVaR_alpha = E[loss | loss > VaR_alpha]
    Returns a negative number (loss magnitude).
    """
    if len(returns) < 10:
        return float("nan")
    sorted_r = returns.sort_values()
    cutoff_idx = int(np.floor((1 - confidence) * len(sorted_r)))
    tail = sorted_r.iloc[:cutoff_idx]
    return float(tail.mean()) if len(tail) > 0 else float("nan")


def monthly_win_rate(returns: pd.Series) -> float:
    """
    Fraction of months with positive returns.

    Parameters
    ----------
    returns : pd.Series
        Daily returns (will be resampled to monthly).
    """
    monthly_ret = returns.resample("ME").apply(lambda x: (1 + x).prod() - 1)
    if len(monthly_ret) == 0:
        return float("nan")
    return float((monthly_ret > 0).mean())


def cagr(returns: pd.Series, periods_per_year: int = 252) -> float:
    """Compound Annual Growth Rate."""
    return annualized_return(returns, periods_per_year)


# ---------------------------------------------------------------------------
# Comprehensive metrics summary
# ---------------------------------------------------------------------------

def compute_metrics(
    portfolio_returns: pd.Series,
    benchmark_returns: Optional[pd.Series] = None,
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252,
) -> Dict[str, float]:
    """
    Compute full set of performance metrics for a return series.

    If qlib risk_analysis is available, it's used for core metrics.
    All other metrics (CVaR, win rate, IR, attribution) are computed manually.

    Parameters
    ----------
    portfolio_returns : pd.Series
        Daily portfolio returns (simple, not log).
    benchmark_returns : pd.Series, optional
        Daily benchmark returns for relative metrics.
    risk_free_rate : float
        Annual risk-free rate (default 0).
    periods_per_year : int
        252 for daily.

    Returns
    -------
    dict of metric_name → float
    """
    metrics = {}

    # --- qlib risk_analysis for CAGR and max drawdown (compounded curve) ---
    # qlib.contrib.evaluate.risk_analysis(r, N, mode="product") returns a DataFrame
    # with column "risk" and index: [mean, std, annualized_return, information_ratio, max_drawdown]
    # "information_ratio" in qlib = geometric_mean / log_return_std * sqrt(N) ≈ Sharpe (rf=0)
    _qlib_cagr_used = False
    if _QLIB_RISK_AVAILABLE:
        try:
            ra_df = qlib_risk_analysis(
                portfolio_returns.dropna(),
                N=periods_per_year,
                freq=None,   # suppress "freq will be ignored" warning when N is explicit
                mode="product",
            )
            ra = ra_df["risk"]  # pd.Series indexed by metric name
            metrics["annual_return"] = float(ra["annualized_return"])
            metrics["max_drawdown"] = float(ra["max_drawdown"])
            # Also store qlib Sharpe (geometric approach) as supplementary metric
            metrics["qlib_sharpe"] = float(ra["information_ratio"])
            metrics["qlib_annual_vol"] = float(ra["std"] * np.sqrt(periods_per_year))
            _qlib_cagr_used = True
            logger.debug("qlib risk_analysis succeeded: CAGR and max_drawdown from qlib.")
        except Exception as e:
            logger.debug(f"qlib risk_analysis failed ({e}). Using manual computation.")

    # --- Manual computation for remaining metrics (and fallback for qlib failures) ---
    # Sharpe: standard annualized (arithmetic return / arithmetic vol), risk-free adjustable
    metrics["annual_vol"] = annualized_vol(portfolio_returns, periods_per_year)
    metrics["sharpe"] = sharpe_ratio(portfolio_returns, risk_free_rate, periods_per_year)

    if not _qlib_cagr_used:
        metrics["annual_return"] = annualized_return(portfolio_returns, periods_per_year)
        mdd_val, mdd_dur = max_drawdown(portfolio_returns)
        metrics["max_drawdown"] = mdd_val
        metrics["max_drawdown_days"] = float(mdd_dur)
    else:
        # Still compute duration (not provided by qlib)
        _, mdd_dur = max_drawdown(portfolio_returns)
        metrics["max_drawdown_days"] = float(mdd_dur)

    metrics["calmar"] = (
        metrics["annual_return"] / abs(metrics["max_drawdown"])
        if metrics.get("max_drawdown") and metrics["max_drawdown"] != 0
        else float("nan")
    )
    metrics["cvar_95"] = cvar(portfolio_returns, confidence=0.95)
    metrics["cvar_99"] = cvar(portfolio_returns, confidence=0.99)
    metrics["monthly_win_rate"] = monthly_win_rate(portfolio_returns)
    metrics["skewness"] = float(portfolio_returns.skew())
    metrics["kurtosis"] = float(portfolio_returns.kurtosis())

    # Total return
    metrics["total_return"] = float((1 + portfolio_returns).prod() - 1)

    # Number of years
    metrics["years"] = len(portfolio_returns) / periods_per_year

    # Benchmark relative metrics
    if benchmark_returns is not None:
        metrics["info_ratio"] = information_ratio(
            portfolio_returns, benchmark_returns, periods_per_year
        )
        aligned = pd.concat([portfolio_returns, benchmark_returns], axis=1).dropna()
        if len(aligned) > 0:
            r_aligned = aligned.iloc[:, 0]
            b_aligned = aligned.iloc[:, 1]
            active_ret = r_aligned - b_aligned
            metrics["tracking_error"] = annualized_vol(active_ret, periods_per_year)
            metrics["active_return"] = annualized_return(active_ret, periods_per_year)
            bench_total = float((1 + benchmark_returns).prod() - 1)
            metrics["benchmark_total_return"] = bench_total
            metrics["excess_return"] = metrics["total_return"] - bench_total

            # Beta and Jensen's Alpha via qlib evaluate_portfolio
            # qlib_get_beta returns a 2×2 covariance/variance matrix; beta = element [0,1]
            if _QLIB_PORTFOLIO_EVAL_AVAILABLE and len(aligned) >= 20:
                try:
                    beta_matrix = qlib_get_beta(r_aligned.values, b_aligned.values)
                    metrics["beta"] = float(beta_matrix[0, 1])  # Cov(r,b)/Var(b)
                    # Alpha: Jensen's alpha = ann_r - rf - beta * (ann_b - rf)
                    ann_r = float(qlib_annual_return(r_aligned, method="ci"))
                    ann_b = float(qlib_annual_return(b_aligned, method="ci"))
                    metrics["alpha"] = float(
                        ann_r - risk_free_rate - metrics["beta"] * (ann_b - risk_free_rate)
                    )
                except Exception as e:
                    logger.debug(f"Beta/alpha computation failed: {e}")
                    metrics["beta"] = float("nan")
                    metrics["alpha"] = float("nan")
        else:
            metrics["info_ratio"] = float("nan")
            metrics["tracking_error"] = float("nan")
            metrics["active_return"] = float("nan")

    return metrics


# ---------------------------------------------------------------------------
# Drawdown analysis
# ---------------------------------------------------------------------------

def find_drawdown_episodes(
    returns: pd.Series,
    top_n: int = 5,
) -> pd.DataFrame:
    """
    Find the worst N drawdown episodes with start, trough, recovery dates.

    Returns pd.DataFrame with columns:
        peak_date, trough_date, recovery_date (or NaT),
        drawdown_pct, duration_days, recovery_days
    """
    if len(returns) == 0:
        return pd.DataFrame()

    cumulative = (1 + returns).cumprod()
    peak = cumulative.expanding().max()
    dd = (cumulative / peak) - 1.0

    episodes = []
    in_dd = False
    peak_date = None
    current_peak_val = None

    for i, (dt, dd_val) in enumerate(dd.items()):
        cum_val = cumulative.iloc[i]
        if not in_dd:
            if dd_val < -0.001:
                in_dd = True
                peak_date = dt
                current_peak_val = float(peak.iloc[i])
                min_dd = dd_val
                trough_date = dt
        else:
            if dd_val < min_dd:
                min_dd = dd_val
                trough_date = dt
            elif dd_val >= -0.001:
                # Recovered
                recovery_date = dt
                episodes.append(
                    {
                        "peak_date": peak_date,
                        "trough_date": trough_date,
                        "recovery_date": recovery_date,
                        "drawdown_pct": round(min_dd * 100, 2),
                        "duration_days": (trough_date - peak_date).days,
                        "recovery_days": (recovery_date - trough_date).days,
                    }
                )
                in_dd = False
                peak_date = None

    # If still in drawdown at end
    if in_dd and peak_date is not None:
        episodes.append(
            {
                "peak_date": peak_date,
                "trough_date": trough_date,
                "recovery_date": pd.NaT,
                "drawdown_pct": round(min_dd * 100, 2),
                "duration_days": (trough_date - peak_date).days,
                "recovery_days": None,
            }
        )

    if not episodes:
        return pd.DataFrame()

    df = pd.DataFrame(episodes)
    df = df.sort_values("drawdown_pct").head(top_n)
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Brinson attribution (simplified)
# ---------------------------------------------------------------------------

def brinson_attribution(
    portfolio_weights: pd.DataFrame,
    portfolio_returns_by_sector: pd.DataFrame,
    benchmark_weights: pd.DataFrame,
    benchmark_returns: pd.DataFrame,
) -> pd.DataFrame:
    """
    Simplified Brinson-Hood-Beebower attribution.

    Allocation effect: (w_p - w_b) × (r_b_sector - r_b_total)
    Selection effect:  w_b × (r_p_sector - r_b_sector)
    Interaction:       (w_p - w_b) × (r_p_sector - r_b_sector)

    Parameters
    ----------
    portfolio_weights : pd.DataFrame
        Monthly portfolio weights (index = dates, columns = tickers).
    portfolio_returns_by_sector : pd.DataFrame
        Monthly sector returns for the portfolio period.
    benchmark_weights : pd.DataFrame
        Monthly benchmark sector weights (e.g., S&P 500 sector weights).
    benchmark_returns : pd.DataFrame
        Monthly benchmark sector returns.

    Returns
    -------
    pd.DataFrame
        Monthly attribution by sector with columns:
        allocation, selection, interaction, total_active
    """
    results = []
    dates = portfolio_weights.index.intersection(benchmark_weights.index)

    for dt in dates:
        wp = portfolio_weights.loc[dt]
        wb = benchmark_weights.loc[dt]
        rp = portfolio_returns_by_sector.loc[dt] if dt in portfolio_returns_by_sector.index else pd.Series(0, index=wp.index)
        rb = benchmark_returns.loc[dt] if dt in benchmark_returns.index else pd.Series(0, index=wb.index)

        all_tickers = wp.index.union(wb.index)
        wp = wp.reindex(all_tickers, fill_value=0.0)
        wb = wb.reindex(all_tickers, fill_value=0.0)
        rp = rp.reindex(all_tickers, fill_value=0.0)
        rb = rb.reindex(all_tickers, fill_value=0.0)

        rb_total = (wb * rb).sum()

        allocation = (wp - wb) * (rb - rb_total)
        selection = wb * (rp - rb)
        interaction = (wp - wb) * (rp - rb)
        total_active = rp * wp - rb * wb  # Simplified

        results.append(
            {
                "date": dt,
                "allocation": float(allocation.sum()),
                "selection": float(selection.sum()),
                "interaction": float(interaction.sum()),
                "total_active": float(total_active.sum()),
            }
        )

    return pd.DataFrame(results).set_index("date")


# ---------------------------------------------------------------------------
# Information Coefficient (IC) — signal evaluation
# ---------------------------------------------------------------------------

def compute_ic(
    signals: pd.DataFrame,
    forward_returns: pd.DataFrame,
    method: str = "rank",
) -> Dict:
    """
    Compute Information Coefficient between sector signals and forward returns.

    Uses qlib's ``get_rank_ic`` (Spearman, rank IC) or ``get_normal_ic`` (Pearson).
    Evaluates how well composite z-scores predict next-period sector returns.

    Parameters
    ----------
    signals : pd.DataFrame
        Composite z-scores (rows = dates, columns = tickers).
    forward_returns : pd.DataFrame
        Sector returns in the next period (rows = dates, columns = tickers).
        Caller is responsible for shifting (forward-looking) if needed.
    method : str
        "rank" (Spearman, default) or "normal" (Pearson).

    Returns
    -------
    dict with keys:
        ic_mean : float  — mean IC across dates
        ic_std  : float  — IC standard deviation
        ic_ir   : float  — IC Information Ratio (mean / std)
        ic_series : pd.Series  — IC value by date
    """
    if not _QLIB_PORTFOLIO_EVAL_AVAILABLE:
        logger.warning("qlib evaluate_portfolio not available. IC computation unavailable.")
        return {"ic_mean": float("nan"), "ic_std": float("nan"), "ic_ir": float("nan"),
                "ic_series": pd.Series(dtype=float)}

    ic_fn = get_rank_ic if method == "rank" else get_normal_ic
    common_dates = signals.index.intersection(forward_returns.index)
    ic_records = []

    for dt in common_dates:
        sig = signals.loc[dt].dropna()
        ret = forward_returns.loc[dt].dropna()
        tickers = sig.index.intersection(ret.index)
        if len(tickers) < 3:
            continue
        try:
            ic_val = float(ic_fn(sig[tickers].values, ret[tickers].values))
            ic_records.append((dt, ic_val))
        except Exception:
            pass

    if not ic_records:
        return {"ic_mean": float("nan"), "ic_std": float("nan"), "ic_ir": float("nan"),
                "ic_series": pd.Series(dtype=float)}

    ic_series = pd.Series(
        [v for _, v in ic_records],
        index=[d for d, _ in ic_records],
        name=f"ic_{method}",
    )
    ic_mean = float(ic_series.mean())
    ic_std = float(ic_series.std())
    ic_ir = float(ic_mean / ic_std) if ic_std > 0 else float("nan")

    return {
        "ic_mean": ic_mean,
        "ic_std": ic_std,
        "ic_ir": ic_ir,
        "ic_series": ic_series,
    }


# ---------------------------------------------------------------------------
# Subperiod analysis
# ---------------------------------------------------------------------------

SUBPERIODS = [
    ("Full Sample", "2018-07-01", "2024-12-31"),
    ("COVID Crash + Recovery", "2020-01-01", "2021-12-31"),
    ("Rate Hike Cycle", "2022-01-01", "2023-12-31"),
    ("Post-COVID Boom", "2021-01-01", "2022-06-30"),
    ("Recent", "2023-01-01", "2024-12-31"),
]


def subperiod_analysis(
    portfolio_returns: pd.Series,
    benchmark_returns: Optional[pd.Series] = None,
    subperiods: Optional[list] = None,
) -> pd.DataFrame:
    """
    Compute performance metrics for multiple subperiods.

    Returns a DataFrame with subperiod names as index and metrics as columns.
    """
    if subperiods is None:
        subperiods = SUBPERIODS

    rows = []
    for name, start, end in subperiods:
        mask = (portfolio_returns.index >= start) & (portfolio_returns.index <= end)
        sub_ret = portfolio_returns[mask]
        if len(sub_ret) < 20:
            continue

        bench_sub = None
        if benchmark_returns is not None:
            bench_mask = (benchmark_returns.index >= start) & (benchmark_returns.index <= end)
            bench_sub = benchmark_returns[bench_mask]

        m = compute_metrics(sub_ret, bench_sub)
        m["subperiod"] = name
        m["start"] = start
        m["end"] = end
        m["n_days"] = len(sub_ret)
        rows.append(m)

    return pd.DataFrame(rows).set_index("subperiod")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    # Smoke test with random data
    np.random.seed(42)
    n = 252 * 6  # 6 years
    ret = pd.Series(
        np.random.normal(0.0004, 0.01, n),
        index=pd.date_range("2018-07-01", periods=n, freq="B"),
        name="portfolio",
    )
    bench = pd.Series(
        np.random.normal(0.0003, 0.012, n),
        index=ret.index,
        name="benchmark",
    )

    m = compute_metrics(ret, bench)
    print("\n=== Performance Metrics ===")
    for k, v in m.items():
        if isinstance(v, float):
            print(f"  {k:<25}: {v:.4f}")

    ep = find_drawdown_episodes(ret, top_n=3)
    print("\n=== Top 3 Drawdown Episodes ===")
    print(ep)

    print("\n=== Subperiod Analysis ===")
    sp = subperiod_analysis(ret, bench)
    print(sp[["annual_return", "annual_vol", "sharpe", "max_drawdown"]].round(3))
