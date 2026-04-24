"""
Tearsheet Generator
===================
Generates a PDF performance tearsheet for the sector rotation strategy.

PDF layout (multi-page):
    Page 1: Title + Key Metrics Summary Table
    Page 2: Equity Curve (strategy vs SPY vs equal weight)
    Page 3: Drawdown + Rolling Sharpe
    Page 4: Sector Weights Heatmap (monthly allocation)
    Page 5: Regime Overlay + Annual Returns
    Page 6: Monthly Returns Calendar Heatmap
    Page 7: Subperiod Analysis Table + Drawdown Episodes
    Page 8: Correlation Matrix

Uses matplotlib + reportlab for PDF generation.
Falls back to matplotlib's PdfPages if reportlab not available.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages


# ---------------------------------------------------------------------------
# PDF utilities
# ---------------------------------------------------------------------------

def _add_title_page(pdf: PdfPages, metrics: dict, config: dict) -> None:
    """Create title page with key metrics summary."""
    fig, ax = plt.subplots(figsize=(14, 8.5))
    ax.axis("off")

    # Title
    start_date = config.get("backtest", {}).get("start_date", "N/A")
    end_date = config.get("backtest", {}).get("end_date", "Today")
    title_text = (
        f"Sector Rotation Strategy\n"
        f"Performance Tearsheet\n\n"
        f"Period: {start_date} → {end_date}"
    )
    ax.text(0.5, 0.88, title_text, ha="center", va="top", transform=ax.transAxes,
            fontsize=16, fontweight="bold", linespacing=2.0)

    # Key metrics table
    m = metrics
    table_data = [
        ["Metric", "Value", "Target"],
        ["Total Return", f"{m.get('total_return', float('nan')):.1%}", "> SPY"],
        ["CAGR", f"{m.get('annual_return', float('nan')):.1%}", "> 10%"],
        ["Annualized Vol", f"{m.get('annual_vol', float('nan')):.1%}", "~12%"],
        ["Sharpe Ratio", f"{m.get('sharpe', float('nan')):.3f}", "> 0.4"],
        ["Calmar Ratio", f"{m.get('calmar', float('nan')):.3f}", "> 0.5"],
        ["Max Drawdown", f"{m.get('max_drawdown', float('nan')):.1%}", "> -20%"],
        ["Max DD Duration", f"{m.get('max_drawdown_days', 0):.0f} days", "< 365d"],
        ["CVaR 95%", f"{m.get('cvar_95', float('nan')):.2%}", ""],
        ["Monthly Win Rate", f"{m.get('monthly_win_rate', float('nan')):.1%}", "> 55%"],
        ["Info Ratio vs SPY", f"{m.get('info_ratio', float('nan')):.3f}", "> 0.3"],
        ["Active Return", f"{m.get('active_return', float('nan')):.1%}", "> 2%"],
        ["Tracking Error", f"{m.get('tracking_error', float('nan')):.1%}", ""],
        ["Skewness", f"{m.get('skewness', float('nan')):.3f}", "> 0 preferred"],
        ["Kurtosis", f"{m.get('kurtosis', float('nan')):.3f}", ""],
    ]

    table = ax.table(
        cellText=table_data[1:],
        colLabels=table_data[0],
        cellLoc="center",
        loc="center",
        bbox=[0.05, 0.05, 0.90, 0.75],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.scale(1, 1.5)

    # Header styling
    for j in range(3):
        table[0, j].set_facecolor("#1f77b4")
        table[0, j].set_text_props(color="white", fontweight="bold")

    # Row coloring
    for i in range(1, len(table_data)):
        for j in range(3):
            if i % 2 == 0:
                table[i, j].set_facecolor("#f0f4f8")

    ax.text(0.5, 0.01,
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | "
            "Sector Rotation Strategy v1.0 | qlib_run env",
            ha="center", va="bottom", transform=ax.transAxes, fontsize=8, color="gray")

    plt.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _add_subperiod_table(pdf: PdfPages, subperiod_df: pd.DataFrame, dd_episodes: pd.DataFrame) -> None:
    """Page with subperiod analysis table and top drawdown episodes."""
    fig, axes = plt.subplots(2, 1, figsize=(14, 10))

    # Subperiod table
    ax1 = axes[0]
    ax1.axis("off")
    ax1.set_title("Subperiod Performance Analysis", fontsize=13, fontweight="bold", pad=15)

    if not subperiod_df.empty:
        cols = ["annual_return", "annual_vol", "sharpe", "max_drawdown",
                "info_ratio", "monthly_win_rate"]
        display_cols = ["CAGR", "Vol", "Sharpe", "MaxDD", "IR", "Win%"]
        sub_display = subperiod_df[[c for c in cols if c in subperiod_df.columns]].copy()
        sub_display.columns = display_cols[:len(sub_display.columns)]

        for col in sub_display.columns:
            if "%" in col or col in ["CAGR", "Vol", "MaxDD", "Win%"]:
                sub_display[col] = sub_display[col].apply(
                    lambda x: f"{x:.1%}" if isinstance(x, float) and not np.isnan(x) else "N/A"
                )
            else:
                sub_display[col] = sub_display[col].apply(
                    lambda x: f"{x:.3f}" if isinstance(x, float) and not np.isnan(x) else "N/A"
                )

        t = ax1.table(
            cellText=sub_display.values,
            rowLabels=sub_display.index,
            colLabels=sub_display.columns,
            cellLoc="center",
            loc="center",
            bbox=[0.0, 0.1, 1.0, 0.85],
        )
        t.auto_set_font_size(False)
        t.set_fontsize(9)
        t.scale(1, 1.4)
        for j in range(len(sub_display.columns)):
            t[0, j].set_facecolor("#1f77b4")
            t[0, j].set_text_props(color="white", fontweight="bold")

    # Drawdown episodes
    ax2 = axes[1]
    ax2.axis("off")
    ax2.set_title("Top 5 Worst Drawdown Episodes", fontsize=13, fontweight="bold", pad=15)

    if not dd_episodes.empty:
        dd_display = dd_episodes.copy()
        for col in ["peak_date", "trough_date", "recovery_date"]:
            if col in dd_display.columns:
                dd_display[col] = dd_display[col].apply(
                    lambda x: str(x.date()) if pd.notna(x) else "Not recovered"
                )
        t2 = ax2.table(
            cellText=dd_display.values,
            colLabels=dd_display.columns,
            cellLoc="center",
            loc="center",
            bbox=[0.0, 0.1, 1.0, 0.85],
        )
        t2.auto_set_font_size(False)
        t2.set_fontsize(9)
        t2.scale(1, 1.4)
        for j in range(len(dd_display.columns)):
            t2[0, j].set_facecolor("#d62728")
            t2[0, j].set_text_props(color="white", fontweight="bold")

    plt.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main tearsheet
# ---------------------------------------------------------------------------

def generate_tearsheet(
    result,
    output_path: Optional[str] = None,
    prices: Optional[pd.DataFrame] = None,
) -> Path:
    """
    Generate a PDF tearsheet from a BacktestResult.

    Parameters
    ----------
    result : BacktestResult
        Output from SectorRotationBacktest.run().
    output_path : str, optional
        Full path for the PDF. Auto-generated if None.
    prices : pd.DataFrame, optional
        Raw prices for correlation matrix. If None, skips correlation page.

    Returns
    -------
    Path
        Path to the saved PDF file.
    """
    from .plots import (
        equity_curve_plot, drawdown_plot, rolling_sharpe_plot,
        sector_weights_heatmap, regime_overlay_plot, annual_returns_bar,
        monthly_returns_heatmap, correlation_matrix_plot,
    )

    # Resolve output path
    if output_path is None:
        report_cfg = result.config.get("report", {})
        out_dir = Path(report_cfg.get("output_dir", "sector_rotation/report/output"))
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = out_dir / f"sector_rotation_tearsheet_{ts}.pdf"
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Generating tearsheet: {output_path}")

    daily_ret = result.daily_returns
    equity = result.equity_curve
    bench_ret = result.benchmark_returns
    bench_eq = result.benchmark_equity
    weights_hist = result.weights_history
    regime_hist = result.regime_history

    with PdfPages(str(output_path)) as pdf:
        # Page 1: Title + metrics table
        _add_title_page(pdf, result.metrics, result.config)

        # Page 2: Equity curve
        fig = equity_curve_plot(equity, bench_eq)
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        # Page 3: Drawdown + Rolling Sharpe
        fig, axes = plt.subplots(2, 1, figsize=(14, 8))
        # Drawdown subplot
        def compute_dd(r):
            cum = (1 + r).cumprod()
            peak = cum.expanding().max()
            return (cum / peak - 1) * 100
        dd = compute_dd(daily_ret)
        axes[0].fill_between(dd.index, dd.values, 0, color="#d62728", alpha=0.6)
        axes[0].plot(dd.index, dd.values, color="#d62728", linewidth=1.0)
        axes[0].set_title("Drawdown (%)", fontweight="bold")
        axes[0].set_ylabel("Drawdown (%)")

        window = 12 * 21
        roll_vol = daily_ret.rolling(window, min_periods=window // 2).std() * np.sqrt(252)
        roll_ret = daily_ret.rolling(window, min_periods=window // 2).mean() * 252
        roll_sr = roll_ret / roll_vol.replace(0, np.nan)
        axes[1].plot(roll_sr.index, roll_sr.values, color="#1f77b4", linewidth=1.5)
        axes[1].axhline(0, color="black", linewidth=0.8, linestyle="--")
        axes[1].axhline(0.5, color="green", linewidth=0.8, linestyle=":", alpha=0.7)
        axes[1].fill_between(roll_sr.index, roll_sr.values, 0,
                              where=(roll_sr > 0), alpha=0.2, color="#2ca02c")
        axes[1].fill_between(roll_sr.index, roll_sr.values, 0,
                              where=(roll_sr <= 0), alpha=0.2, color="#d62728")
        axes[1].set_title("12-Month Rolling Sharpe Ratio", fontweight="bold")
        axes[1].set_ylabel("Sharpe Ratio")
        plt.tight_layout()
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        # Page 4: Sector weights heatmap
        fig = sector_weights_heatmap(weights_hist)
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        # Page 5: Regime overlay + Annual returns
        fig, axes = plt.subplots(2, 1, figsize=(14, 10))
        plt.tight_layout()
        # Regime overlay
        norm = equity / equity.iloc[0] * 100
        axes[0].plot(norm.index, norm.values, color="#1f77b4", linewidth=2.0, label="Strategy")
        if not regime_hist.empty:
            daily_regime = regime_hist.reindex(equity.index, method="ffill").fillna("risk_on")
            for state in ["risk_on", "transition_up", "transition_down", "risk_off"]:
                from .plots import REGIME_COLORS
                mask = (daily_regime == state)
                in_block = False
                block_start = None
                for i, (dt, is_state) in enumerate(mask.items()):
                    if is_state and not in_block:
                        block_start = dt
                        in_block = True
                    elif not is_state and in_block:
                        axes[0].axvspan(block_start, dt, alpha=0.2,
                                        color=REGIME_COLORS.get(state, "#e9ecef"), zorder=1)
                        in_block = False
                if in_block and block_start is not None:
                    axes[0].axvspan(block_start, equity.index[-1], alpha=0.2,
                                    color=REGIME_COLORS.get(state, "#e9ecef"), zorder=1)
        axes[0].set_title("Equity Curve with Regime Overlay", fontweight="bold")
        axes[0].legend()
        # Annual returns
        annual = daily_ret.resample("YE").apply(lambda x: (1 + x).prod() - 1)
        years = [str(y.year) for y in annual.index]
        x = np.arange(len(years))
        axes[1].bar(x, annual.values * 100,
                    color=["#2ca02c" if v > 0 else "#d62728" for v in annual.values],
                    alpha=0.8, label="Strategy")
        if bench_ret is not None:
            ba = bench_ret.resample("YE").apply(lambda x: (1 + x).prod() - 1)
            ba = ba.reindex(annual.index, fill_value=0)
            axes[1].bar(x + 0.3, ba.values * 100, 0.3, color="#ff7f0e", alpha=0.6, label="SPY")
        axes[1].set_xticks(x)
        axes[1].set_xticklabels(years)
        axes[1].axhline(0, color="black", linewidth=0.8)
        axes[1].set_title("Annual Returns", fontweight="bold")
        axes[1].legend()
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        # Page 6: Monthly returns heatmap
        fig = monthly_returns_heatmap(daily_ret)
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        # Page 7: Subperiod + Drawdown episodes
        _add_subperiod_table(pdf, result.subperiod_metrics, result.drawdown_episodes)

        # Page 8: Correlation matrix (if prices available)
        if prices is not None:
            etf_tickers = result.config.get("universe", {}).get("etfs", [])
            etf_p = prices[[t for t in etf_tickers if t in prices.columns]]
            if not etf_p.empty:
                fig = correlation_matrix_plot(etf_p)
                pdf.savefig(fig, bbox_inches="tight")
                plt.close(fig)

        # PDF metadata
        d = pdf.infodict()
        d["Title"] = "Sector Rotation Strategy Tearsheet"
        d["Author"] = "Sector Rotation (qlib)"
        d["Subject"] = "Institutional Sector ETF Rotation Backtest"
        d["CreationDate"] = datetime.now()

    logger.info(f"Tearsheet saved: {output_path}")
    return output_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    print("Tearsheet module loaded. Run backtest engine first to generate BacktestResult.")
