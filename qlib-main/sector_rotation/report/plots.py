"""
Visualization Functions
=======================
All plotting functions for the sector rotation tearsheet.

Functions return matplotlib Figure objects (not saved to disk here).
Saving is handled by tearsheet.py.

Plot inventory:
    1. equity_curve_plot     - Cumulative returns: strategy vs SPY vs equal-weight
    2. drawdown_plot         - Drawdown over time
    3. rolling_sharpe_plot   - 12-month rolling Sharpe ratio
    4. sector_weights_heatmap - Monthly sector allocation heatmap
    5. regime_overlay_plot   - Equity curve with regime color bands
    6. signal_ic_plot        - Monthly signal IC (information coefficient) analysis
    7. correlation_matrix    - Sector return correlation matrix
    8. brinson_attribution   - Allocation vs selection effect bar chart
    9. annual_returns_bar    - Annual returns grouped bar (strategy vs SPY)
    10. monthly_returns_heatmap - Calendar heatmap of monthly returns
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Matplotlib setup (non-interactive backend for PDF generation)
# ---------------------------------------------------------------------------

import matplotlib
matplotlib.use("Agg")  # Must be before pyplot import
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.colors import LinearSegmentedColormap
import matplotlib.ticker as mticker

# Default style
plt.rcParams.update({
    "figure.facecolor": "white",
    "axes.facecolor": "#f8f9fa",
    "axes.grid": True,
    "grid.alpha": 0.4,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "font.size": 10,
    "axes.titlesize": 12,
    "axes.labelsize": 10,
})

# Regime colors
REGIME_COLORS = {
    "risk_on": "#d4edda",
    "transition_up": "#fff3cd",
    "transition_down": "#fde8e0",
    "risk_off": "#f8d7da",
}


# ---------------------------------------------------------------------------
# 1. Equity Curve
# ---------------------------------------------------------------------------

def equity_curve_plot(
    equity: pd.Series,
    benchmark: Optional[pd.Series] = None,
    equal_weight: Optional[pd.Series] = None,
    strategy_label: str = "Strategy",
    benchmark_label: str = "SPY",
    ew_label: str = "Equal Weight",
    figsize: Tuple = (14, 5),
) -> plt.Figure:
    """
    Cumulative return chart (normalized to 100 at start).

    Shows: strategy, benchmark (SPY), and equal-weight sector basket.
    """
    fig, ax = plt.subplots(figsize=figsize)

    # Normalize to 100
    norm = equity / equity.iloc[0] * 100
    ax.plot(norm.index, norm.values, linewidth=2.0, color="#1f77b4", label=strategy_label, zorder=3)

    if benchmark is not None:
        b_norm = benchmark / benchmark.iloc[0] * 100
        ax.plot(b_norm.index, b_norm.values, linewidth=1.5, color="#ff7f0e",
                linestyle="--", label=benchmark_label, zorder=2)

    if equal_weight is not None:
        ew_norm = equal_weight / equal_weight.iloc[0] * 100
        ax.plot(ew_norm.index, ew_norm.values, linewidth=1.5, color="#2ca02c",
                linestyle=":", label=ew_label, zorder=2)

    ax.set_title(f"Cumulative Return (base=100)", fontsize=13, fontweight="bold")
    ax.set_ylabel("Portfolio Value (base=100)")
    ax.legend(loc="upper left")
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.0f"))
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# 2. Drawdown
# ---------------------------------------------------------------------------

def drawdown_plot(
    returns: pd.Series,
    benchmark_returns: Optional[pd.Series] = None,
    figsize: Tuple = (14, 4),
) -> plt.Figure:
    """Underwater plot showing portfolio drawdown over time."""
    fig, ax = plt.subplots(figsize=figsize)

    def compute_dd(r):
        cum = (1 + r).cumprod()
        peak = cum.expanding().max()
        return (cum / peak - 1) * 100

    dd = compute_dd(returns)
    ax.fill_between(dd.index, dd.values, 0, color="#d62728", alpha=0.6, label="Strategy DD")
    ax.plot(dd.index, dd.values, color="#d62728", linewidth=1.0)

    if benchmark_returns is not None:
        bench_dd = compute_dd(benchmark_returns)
        ax.plot(bench_dd.index, bench_dd.values, color="#ff7f0e",
                linewidth=1.0, linestyle="--", alpha=0.7, label="SPY DD")

    ax.set_title("Drawdown (%)", fontsize=13, fontweight="bold")
    ax.set_ylabel("Drawdown (%)")
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.0f%%"))
    if benchmark_returns is not None:
        ax.legend()
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# 3. Rolling Sharpe
# ---------------------------------------------------------------------------

def rolling_sharpe_plot(
    returns: pd.Series,
    window_months: int = 12,
    figsize: Tuple = (14, 4),
) -> plt.Figure:
    """12-month rolling Sharpe ratio."""
    fig, ax = plt.subplots(figsize=figsize)

    window = window_months * 21  # Trading days
    rolling_vol = returns.rolling(window=window, min_periods=window // 2).std() * np.sqrt(252)
    rolling_ret = returns.rolling(window=window, min_periods=window // 2).mean() * 252
    rolling_sharpe = rolling_ret / rolling_vol.replace(0, np.nan)

    ax.plot(rolling_sharpe.index, rolling_sharpe.values, color="#1f77b4", linewidth=1.5)
    ax.axhline(0, color="black", linewidth=0.8, linestyle="--", alpha=0.5)
    ax.axhline(0.5, color="green", linewidth=0.8, linestyle=":", alpha=0.7, label="Target=0.5")
    ax.fill_between(rolling_sharpe.index, rolling_sharpe.values, 0,
                    where=(rolling_sharpe > 0), alpha=0.2, color="#2ca02c")
    ax.fill_between(rolling_sharpe.index, rolling_sharpe.values, 0,
                    where=(rolling_sharpe <= 0), alpha=0.2, color="#d62728")

    ax.set_title(f"{window_months}-Month Rolling Sharpe Ratio", fontsize=13, fontweight="bold")
    ax.set_ylabel("Sharpe Ratio")
    ax.legend()
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# 4. Sector Weights Heatmap
# ---------------------------------------------------------------------------

def sector_weights_heatmap(
    weights_history: pd.DataFrame,
    figsize: Optional[Tuple] = None,
) -> plt.Figure:
    """
    Monthly sector allocation heatmap.

    Rows = sectors, columns = months.
    Color intensity = weight (0% = white, 40%+ = dark blue).
    """
    if weights_history.empty:
        fig, ax = plt.subplots(figsize=(12, 5))
        ax.text(0.5, 0.5, "No weights data", ha="center", va="center")
        return fig

    # Transpose so sectors are rows, dates are columns
    data = weights_history.T * 100  # Convert to %
    n_sectors, n_months = data.shape
    if figsize is None:
        figsize = (max(12, n_months * 0.4), max(5, n_sectors * 0.5))

    fig, ax = plt.subplots(figsize=figsize)

    # Custom colormap: white → blue
    cmap = LinearSegmentedColormap.from_list("weight_heat", ["#f8f9fa", "#1f77b4"], N=256)
    im = ax.imshow(data.values, aspect="auto", cmap=cmap, vmin=0, vmax=40, interpolation="nearest")

    # Axis labels
    ax.set_yticks(range(n_sectors))
    ax.set_yticklabels(data.index)
    date_labels = [d.strftime("%Y-%m") for d in data.columns]
    step = max(1, n_months // 20)
    ax.set_xticks(range(0, n_months, step))
    ax.set_xticklabels(date_labels[::step], rotation=45, ha="right", fontsize=8)

    # Value annotations
    for i in range(n_sectors):
        for j in range(n_months):
            val = data.values[i, j]
            if val > 1.0:
                ax.text(j, i, f"{val:.0f}%", ha="center", va="center",
                        fontsize=6, color="white" if val > 25 else "black")

    plt.colorbar(im, ax=ax, label="Weight (%)", fraction=0.02, pad=0.02)
    ax.set_title("Monthly Sector Allocation (%)", fontsize=13, fontweight="bold")
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# 5. Regime Overlay
# ---------------------------------------------------------------------------

def regime_overlay_plot(
    equity: pd.Series,
    regime: pd.Series,
    figsize: Tuple = (14, 5),
) -> plt.Figure:
    """
    Equity curve with regime color bands in the background.

    Regime bands: green=risk_on, yellow=transition_up,
                  orange=transition_down, red=risk_off.
    """
    fig, ax = plt.subplots(figsize=figsize)

    # Normalize equity
    norm = equity / equity.iloc[0] * 100
    ax.plot(norm.index, norm.values, linewidth=2.0, color="#1f77b4", zorder=3, label="Strategy")

    # Add regime bands
    if not regime.empty:
        # Align regime to daily equity index
        daily_regime = regime.reindex(equity.index, method="ffill").fillna("risk_on")
        unique_regimes = daily_regime.unique()

        for state in unique_regimes:
            color = REGIME_COLORS.get(state, "#e9ecef")
            mask = (daily_regime == state)
            # Find contiguous blocks
            in_block = False
            block_start = None
            for i, (dt, is_state) in enumerate(mask.items()):
                if is_state and not in_block:
                    block_start = dt
                    in_block = True
                elif not is_state and in_block:
                    ax.axvspan(block_start, dt, alpha=0.25, color=color, zorder=1)
                    in_block = False
            if in_block and block_start is not None:
                ax.axvspan(block_start, equity.index[-1], alpha=0.25, color=color, zorder=1)

        # Legend for regimes
        patches = [
            mpatches.Patch(color=REGIME_COLORS.get(s, "#e9ecef"), alpha=0.5, label=s.replace("_", " ").title())
            for s in ["risk_on", "transition_up", "transition_down", "risk_off"]
        ]
        legend1 = ax.legend(handles=patches, loc="lower right", fontsize=8, title="Regime")
        ax.add_artist(legend1)

    ax.set_title("Equity Curve with Regime Overlay", fontsize=13, fontweight="bold")
    ax.set_ylabel("Portfolio Value (base=100)")
    ax.legend(loc="upper left")
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# 6. Annual Returns Bar Chart
# ---------------------------------------------------------------------------

def annual_returns_bar(
    returns: pd.Series,
    benchmark_returns: Optional[pd.Series] = None,
    figsize: Tuple = (14, 5),
) -> plt.Figure:
    """Annual return comparison bar chart."""
    fig, ax = plt.subplots(figsize=figsize)

    annual = returns.resample("YE").apply(lambda x: (1 + x).prod() - 1)
    years = [str(y.year) for y in annual.index]
    x = np.arange(len(years))
    width = 0.35

    bars1 = ax.bar(
        x - width / 2, annual.values * 100, width,
        color=["#2ca02c" if v > 0 else "#d62728" for v in annual.values],
        label="Strategy", alpha=0.8, edgecolor="white",
    )

    if benchmark_returns is not None:
        bench_annual = benchmark_returns.resample("YE").apply(lambda x: (1 + x).prod() - 1)
        bench_annual = bench_annual.reindex(annual.index, fill_value=0)
        ax.bar(
            x + width / 2, bench_annual.values * 100, width,
            color="#ff7f0e", label="SPY", alpha=0.6, edgecolor="white",
        )

    ax.set_xticks(x)
    ax.set_xticklabels(years)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_ylabel("Annual Return (%)")
    ax.set_title("Annual Returns: Strategy vs Benchmark", fontsize=13, fontweight="bold")
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.0f%%"))
    ax.legend()
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# 7. Monthly Returns Heatmap (calendar)
# ---------------------------------------------------------------------------

def monthly_returns_heatmap(
    returns: pd.Series,
    figsize: Tuple = (14, 6),
) -> plt.Figure:
    """
    Calendar heatmap of monthly returns (years × months grid).
    """
    monthly = returns.resample("ME").apply(lambda x: (1 + x).prod() - 1)
    df = pd.DataFrame({
        "year": monthly.index.year,
        "month": monthly.index.month,
        "return": monthly.values * 100,
    })
    pivot = df.pivot(index="year", columns="month", values="return")
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    pivot.columns = month_names[:len(pivot.columns)]

    fig, ax = plt.subplots(figsize=figsize)
    cmap = LinearSegmentedColormap.from_list(
        "ret_heatmap", ["#d62728", "#f8f9fa", "#2ca02c"], N=256
    )
    max_abs = max(abs(pivot.values[~np.isnan(pivot.values)].min()),
                  abs(pivot.values[~np.isnan(pivot.values)].max()), 1.0)

    im = ax.imshow(pivot.values, cmap=cmap, vmin=-max_abs, vmax=max_abs,
                   aspect="auto", interpolation="nearest")

    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(pivot.columns)
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index)

    # Annotate cells
    for i in range(len(pivot.index)):
        for j in range(len(pivot.columns)):
            val = pivot.values[i, j]
            if not np.isnan(val):
                color = "white" if abs(val) > max_abs * 0.6 else "black"
                ax.text(j, i, f"{val:.1f}%", ha="center", va="center",
                        fontsize=7, color=color)

    plt.colorbar(im, ax=ax, label="Monthly Return (%)", fraction=0.02, pad=0.02)
    ax.set_title("Monthly Returns Heatmap (%)", fontsize=13, fontweight="bold")
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# 8. Sector Correlation Matrix
# ---------------------------------------------------------------------------

def correlation_matrix_plot(
    prices: pd.DataFrame,
    figsize: Tuple = (10, 8),
) -> plt.Figure:
    """Sector return correlation heatmap."""
    returns = prices.pct_change().iloc[1:]
    corr = returns.corr()

    fig, ax = plt.subplots(figsize=figsize)
    cmap = LinearSegmentedColormap.from_list(
        "corr", ["#d62728", "#f8f9fa", "#1f77b4"], N=256
    )
    im = ax.imshow(corr.values, cmap=cmap, vmin=-1, vmax=1, interpolation="nearest")

    n = len(corr.columns)
    ax.set_xticks(range(n))
    ax.set_xticklabels(corr.columns, rotation=45, ha="right")
    ax.set_yticks(range(n))
    ax.set_yticklabels(corr.index)

    for i in range(n):
        for j in range(n):
            color = "white" if abs(corr.values[i, j]) > 0.6 else "black"
            ax.text(j, i, f"{corr.values[i, j]:.2f}", ha="center", va="center",
                    fontsize=8, color=color)

    plt.colorbar(im, ax=ax, label="Correlation", fraction=0.04, pad=0.04)
    ax.set_title("Sector Return Correlation Matrix", fontsize=13, fontweight="bold")
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# 9. Brinson Attribution
# ---------------------------------------------------------------------------

def brinson_attribution_plot(
    attribution: pd.DataFrame,
    figsize: Tuple = (14, 5),
) -> plt.Figure:
    """Stacked bar chart of Brinson attribution effects over time."""
    if attribution.empty:
        fig, ax = plt.subplots(figsize=figsize)
        ax.text(0.5, 0.5, "No attribution data", ha="center", va="center")
        return fig

    fig, ax = plt.subplots(figsize=figsize)

    dates = attribution.index
    alloc = attribution["allocation"].values * 100
    select = attribution["selection"].values * 100
    interact = attribution.get("interaction", pd.Series(0, index=attribution.index)).values * 100

    x = np.arange(len(dates))
    ax.bar(x, alloc, label="Allocation", color="#1f77b4", alpha=0.8)
    ax.bar(x, select, bottom=alloc, label="Selection", color="#2ca02c", alpha=0.8)
    ax.bar(x, interact, bottom=alloc + select, label="Interaction", color="#ff7f0e", alpha=0.6)

    ax.axhline(0, color="black", linewidth=0.8)
    step = max(1, len(dates) // 20)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([d.strftime("%Y-%m") for d in dates[::step]], rotation=45, ha="right")
    ax.set_ylabel("Active Return (bps)")
    ax.set_title("Brinson Attribution: Allocation vs Selection", fontsize=13, fontweight="bold")
    ax.legend()
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Convenience: generate all plots
# ---------------------------------------------------------------------------

def generate_all_plots(result) -> Dict[str, plt.Figure]:
    """
    Generate all standard plots from a BacktestResult.

    Returns dict of {plot_name: Figure}.
    """
    from ..backtest.engine import BacktestResult  # type: ignore

    daily_ret = result.daily_returns
    equity = result.equity_curve
    bench_ret = result.benchmark_returns
    bench_eq = result.benchmark_equity
    weights_hist = result.weights_history
    regime_hist = result.regime_history

    figs = {}
    figs["equity_curve"] = equity_curve_plot(equity, bench_eq)
    figs["drawdown"] = drawdown_plot(daily_ret, bench_ret)
    figs["rolling_sharpe"] = rolling_sharpe_plot(daily_ret)
    figs["sector_weights"] = sector_weights_heatmap(weights_hist)
    figs["regime_overlay"] = regime_overlay_plot(equity, regime_hist)
    figs["annual_returns"] = annual_returns_bar(daily_ret, bench_ret)
    figs["monthly_returns"] = monthly_returns_heatmap(daily_ret)

    return figs
