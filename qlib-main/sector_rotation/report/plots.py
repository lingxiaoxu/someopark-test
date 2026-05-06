"""
Visualization Functions — Professional Edition
===============================================
All plotting functions for the sector rotation tearsheet.

All functions return matplotlib Figure objects (caller handles PDF saving).
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, Rectangle
from matplotlib.colors import LinearSegmentedColormap
import matplotlib.ticker as mticker
import matplotlib.patheffects as pe

# ---------------------------------------------------------------------------
# Design System
# ---------------------------------------------------------------------------

PALETTE = {
    "bg":            "#FFFFFF",
    "surface":       "#F8FAFC",
    "header_dark":   "#0D1B2A",
    "header_mid":    "#1A3A5C",
    "accent":        "#1E5F74",
    "gold":          "#C8A951",
    "strategy":      "#1A3A5C",
    "benchmark":     "#D97B2A",
    "ew":            "#2E7D32",
    "positive":      "#1B5E20",
    "negative":      "#B71C1C",
    "pos_light":     "#E8F5E9",
    "neg_light":     "#FFEBEE",
    "neutral":       "#37474F",
    "muted":         "#78909C",
    "light_row":     "#F0F4F8",
    "border":        "#CFD8DC",
    "grid":          "#ECEFF1",
    "tbl_header":    "#1A3A5C",
    "tbl_header_txt":"#FFFFFF",
    "tbl_alt":       "#F4F7FB",
    "tbl_total":     "#E8F0FE",
}

REGIME_COLORS = {
    "risk_on":        "#A5D6A7",   # green 200
    "transition_up":  "#FFE082",   # amber 200
    "transition_down":"#FFAB91",   # deep orange 200
    "risk_off":       "#EF9A9A",   # red 200
}

# Darker legend swatches so patches are visible against white legend background
REGIME_LEGEND_COLORS = {
    "risk_on":        "#4CAF50",   # green
    "transition_up":  "#FFC107",   # amber
    "transition_down":"#FF7043",   # deep orange
    "risk_off":       "#E53935",   # red
}

REGIME_LABELS = {
    "risk_on":        "Risk On",
    "transition_up":  "Transition Up",
    "transition_down":"Transition Down",
    "risk_off":       "Risk Off",
}

# Matplotlib global defaults
plt.rcParams.update({
    "figure.facecolor":     PALETTE["bg"],
    "axes.facecolor":       PALETTE["surface"],
    "axes.grid":            True,
    "grid.color":           PALETTE["grid"],
    "grid.alpha":           0.8,
    "grid.linewidth":       0.5,
    "axes.spines.top":      False,
    "axes.spines.right":    False,
    "axes.spines.left":     True,
    "axes.spines.bottom":   True,
    "axes.edgecolor":       PALETTE["border"],
    "axes.linewidth":       0.8,
    "font.family":          "DejaVu Sans",
    "font.size":            10,
    "axes.titlesize":       12,
    "axes.titleweight":     "bold",
    "axes.titlepad":        10,
    "axes.labelsize":       9,
    "axes.labelcolor":      PALETTE["neutral"],
    "xtick.color":          PALETTE["neutral"],
    "ytick.color":          PALETTE["neutral"],
    "xtick.labelsize":      8,
    "ytick.labelsize":      8,
    "legend.fontsize":      9,
    "legend.framealpha":    0.92,
    "legend.edgecolor":     PALETTE["border"],
    "legend.fancybox":      True,
})


def _style_axis(ax, title: str = "", xlabel: str = "", ylabel: str = ""):
    """Apply consistent styling to an axis."""
    if title:
        ax.set_title(title, fontsize=12, fontweight="bold",
                     color=PALETTE["header_mid"], pad=10)
    if xlabel:
        ax.set_xlabel(xlabel, fontsize=9, color=PALETTE["muted"])
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=9, color=PALETTE["muted"])
    ax.tick_params(colors=PALETTE["neutral"], labelsize=8)
    for spine in ax.spines.values():
        spine.set_edgecolor(PALETTE["border"])
        spine.set_linewidth(0.8)


def _section_header(ax, x, y, w, h, text, bg=None):
    """Draw a section header rectangle with text on ax (data coords)."""
    bg = bg or PALETTE["tbl_header"]
    rect = Rectangle((x, y), w, h, facecolor=bg, edgecolor="none",
                      transform=ax.transAxes, zorder=3, clip_on=False)
    ax.add_patch(rect)
    ax.text(x + 0.015, y + h / 2, text, transform=ax.transAxes,
            ha="left", va="center", fontsize=10, fontweight="bold",
            color=PALETTE["tbl_header_txt"], zorder=4)


# ---------------------------------------------------------------------------
# Table drawing helper
# ---------------------------------------------------------------------------

def _draw_styled_table(ax, headers, rows, col_x, col_w, row_h=0.048,
                       y_start=0.96, header_bg=None, alt_bg=None,
                       col_aligns=None, row_colors=None):
    """
    Draw a professional table manually using rectangles and text.

    Parameters
    ----------
    ax           : axes with xlim/ylim [0,1]×[0,1] and axis('off')
    headers      : list of str column headers
    rows         : list of list[str] cell values
    col_x        : list of float — left edge of each column (0..1)
    col_w        : list of float — width of each column
    row_h        : float — height of each row (axes fraction)
    y_start      : float — top of header row (axes fraction)
    header_bg    : str — header background color
    alt_bg       : str — alternating row background
    col_aligns   : list of str — 'left'|'center'|'right' per column
    row_colors   : list of list of str — per-cell text color overrides
    """
    hdr_bg  = header_bg or PALETTE["tbl_header"]
    alt_bg  = alt_bg    or PALETTE["tbl_alt"]
    aligns  = col_aligns or ["center"] * len(headers)

    def _text_x(cx, cw, align):
        if align == "left":   return cx + 0.012
        if align == "right":  return cx + cw - 0.012
        return cx + cw / 2

    # Header row
    for c, (hdr, cx, cw) in enumerate(zip(headers, col_x, col_w)):
        rect = Rectangle((cx, y_start - row_h), cw, row_h,
                         facecolor=hdr_bg, edgecolor=PALETTE["border"],
                         linewidth=0.4, transform=ax.transAxes,
                         clip_on=False, zorder=3)
        ax.add_patch(rect)
        ax.text(_text_x(cx, cw, aligns[c]), y_start - row_h / 2,
                hdr, transform=ax.transAxes,
                ha=aligns[c], va="center",
                fontsize=8.5, fontweight="bold",
                color=PALETTE["tbl_header_txt"], zorder=4)

    # Data rows
    for r, row_vals in enumerate(rows):
        y_bot = y_start - (r + 2) * row_h
        bg = alt_bg if r % 2 == 1 else PALETTE["bg"]
        for c, (val, cx, cw) in enumerate(zip(row_vals, col_x, col_w)):
            rect = Rectangle((cx, y_bot), cw, row_h,
                             facecolor=bg, edgecolor=PALETTE["border"],
                             linewidth=0.3, transform=ax.transAxes,
                             clip_on=False, zorder=3)
            ax.add_patch(rect)
            # Cell text color
            txt_color = PALETTE["neutral"]
            if row_colors and r < len(row_colors) and c < len(row_colors[r]):
                txt_color = row_colors[r][c]
            ax.text(_text_x(cx, cw, aligns[c]), y_bot + row_h / 2,
                    str(val), transform=ax.transAxes,
                    ha=aligns[c], va="center",
                    fontsize=8.5, color=txt_color, zorder=4)


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
    """Cumulative return chart (normalized to 100 at first active trade).

    Detects the warmup period (flat equity) and rebases both strategy
    and benchmark from the first date with non-zero returns, so the
    comparison is fair (no 12-month warmup flat vs SPY gaining 10%).
    """
    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_facecolor(PALETTE["bg"])

    # Find first active date (skip warmup flat period)
    rets = equity.pct_change()
    active_mask = rets.abs() > 1e-6
    if active_mask.any():
        first_active = active_mask.idxmax()
        # Start 1 day before first trade for base=100
        active_start_idx = max(0, equity.index.get_loc(first_active) - 1)
        equity = equity.iloc[active_start_idx:]
        if benchmark is not None:
            benchmark = benchmark.reindex(equity.index, method="ffill")
        if equal_weight is not None:
            equal_weight = equal_weight.reindex(equity.index, method="ffill")

    norm = equity / equity.iloc[0] * 100

    # Gradient fill under strategy line
    ax.fill_between(norm.index, norm.values, 100,
                    where=(norm.values >= 100),
                    alpha=0.08, color=PALETTE["strategy"], interpolate=True)
    ax.fill_between(norm.index, norm.values, 100,
                    where=(norm.values < 100),
                    alpha=0.12, color=PALETTE["negative"], interpolate=True)
    ax.plot(norm.index, norm.values, linewidth=2.2,
            color=PALETTE["strategy"], label=strategy_label, zorder=4)

    if benchmark is not None:
        b_norm = benchmark / benchmark.iloc[0] * 100
        ax.plot(b_norm.index, b_norm.values, linewidth=1.5,
                color=PALETTE["benchmark"], linestyle="--",
                label=benchmark_label, alpha=0.85, zorder=3)

    if equal_weight is not None:
        ew_norm = equal_weight / equal_weight.iloc[0] * 100
        ax.plot(ew_norm.index, ew_norm.values, linewidth=1.2,
                color=PALETTE["ew"], linestyle=":",
                label=ew_label, alpha=0.75, zorder=2)

    ax.axhline(100, color=PALETTE["border"], linewidth=0.8, linestyle="-", alpha=0.6)
    _style_axis(ax, title="Cumulative Return (Base = 100)",
                ylabel="Portfolio Value (Base = 100)")
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
    """Underwater drawdown plot."""
    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_facecolor(PALETTE["bg"])

    def compute_dd(r):
        cum = (1 + r).cumprod()
        peak = cum.expanding().max()
        return (cum / peak - 1) * 100

    dd = compute_dd(returns)
    ax.fill_between(dd.index, dd.values, 0,
                    color=PALETTE["negative"], alpha=0.35, interpolate=True)
    ax.plot(dd.index, dd.values, color=PALETTE["negative"], linewidth=1.2)

    if benchmark_returns is not None:
        bench_dd = compute_dd(benchmark_returns)
        ax.plot(bench_dd.index, bench_dd.values,
                color=PALETTE["benchmark"], linewidth=1.0,
                linestyle="--", alpha=0.7, label="SPY DD")
        ax.legend()

    ax.axhline(0, color=PALETTE["border"], linewidth=0.8)
    _style_axis(ax, title="Drawdown (%)", ylabel="Drawdown (%)")
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.0f%%"))
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
    fig.patch.set_facecolor(PALETTE["bg"])

    window = window_months * 21
    rv = returns.rolling(window=window, min_periods=window // 2).std() * np.sqrt(252)
    rr = returns.rolling(window=window, min_periods=window // 2).mean() * 252
    rs = rr / rv.replace(0, np.nan)

    ax.fill_between(rs.index, rs.values, 0,
                    where=(rs >= 0), alpha=0.15,
                    color=PALETTE["ew"], interpolate=True)
    ax.fill_between(rs.index, rs.values, 0,
                    where=(rs < 0), alpha=0.15,
                    color=PALETTE["negative"], interpolate=True)
    ax.plot(rs.index, rs.values, color=PALETTE["strategy"], linewidth=1.6)

    ax.axhline(0,   color=PALETTE["neutral"],  linewidth=0.9, linestyle="--", alpha=0.6)
    ax.axhline(0.5, color=PALETTE["positive"], linewidth=0.8, linestyle=":",  alpha=0.6,
               label="Target Sharpe = 0.5")
    ax.axhline(1.0, color=PALETTE["positive"], linewidth=0.8, linestyle=":",  alpha=0.4)

    _style_axis(ax, title=f"{window_months}-Month Rolling Sharpe Ratio",
                ylabel="Sharpe Ratio")
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
    """Monthly sector allocation heatmap — pivoted: sectors on X, months on Y.

    Each cell's height ≈ 1/3 of its width so rows are compact.
    """
    if weights_history.empty:
        fig, ax = plt.subplots(figsize=(12, 5))
        ax.text(0.5, 0.5, "No weights data", ha="center", va="center",
                color=PALETTE["muted"])
        return fig

    # rows = months, cols = sectors
    data = weights_history * 100          # (n_months × n_sectors)
    n_months, n_sectors = data.shape

    if figsize is None:
        fig_w   = 14.0
        cell_w  = fig_w / n_sectors
        cell_h  = cell_w / 3.0            # height ≈ 1/3 of width
        fig_h   = n_months * cell_h + 2.0  # +2 for title / labels / colorbar
        figsize = (fig_w, max(fig_h, 5.0))

    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_facecolor(PALETTE["bg"])

    cmap = LinearSegmentedColormap.from_list(
        "wt_heat",
        ["#FAFBFC", PALETTE["header_mid"]],
        N=256
    )
    # imshow rows = months (y), cols = sectors (x)
    im = ax.imshow(data.values, aspect="auto", cmap=cmap,
                   vmin=0, vmax=40, interpolation="nearest")

    # ── X-axis: sector tickers (top) ──────────────────────────────────────────
    ax.set_xticks(range(n_sectors))
    ax.set_xticklabels(data.columns.tolist(), fontsize=9.5, fontweight="bold",
                       color=PALETTE["header_mid"])
    ax.xaxis.set_ticks_position("top")
    ax.xaxis.set_label_position("top")
    ax.tick_params(axis="x", which="both", length=0, pad=4)

    # ── Y-axis: month labels (left, sparse ~30 ticks) ─────────────────────────
    step = max(1, n_months // 30)
    ytick_pos = list(range(0, n_months, step))
    ax.set_yticks(ytick_pos)
    ax.set_yticklabels(
        [data.index[i].strftime("%Y-%m") for i in ytick_pos],
        fontsize=7.0, color=PALETTE["muted"]
    )
    ax.tick_params(axis="y", which="both", length=0)

    # ── Cell value annotations ────────────────────────────────────────────────
    for i in range(n_months):
        for j in range(n_sectors):
            val = data.values[i, j]
            if val > 1.5:
                ax.text(j, i, f"{val:.0f}%", ha="center", va="center",
                        fontsize=10.0,
                        color="white" if val > 22 else PALETTE["neutral"])

    # ── Colorbar (right side, horizontal fraction) ────────────────────────────
    cbar = plt.colorbar(im, ax=ax, label="Weight (%)", fraction=0.015, pad=0.01)
    cbar.ax.tick_params(labelsize=8)
    cbar.set_label("Weight (%)", fontsize=8, color=PALETTE["muted"])

    ax.set_title("Monthly Sector Allocation (%)", fontsize=12, fontweight="bold",
                 color=PALETTE["header_mid"], pad=14)
    for spine in ax.spines.values():
        spine.set_visible(False)
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
    """Equity curve with colored regime background bands."""
    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_facecolor(PALETTE["bg"])

    norm = equity / equity.iloc[0] * 100

    regime_patches = []
    if not regime.empty:
        daily_regime = regime.reindex(equity.index, method="ffill").fillna("risk_on")
        for state in ["risk_on", "transition_up", "transition_down", "risk_off"]:
            color = REGIME_COLORS.get(state, "#F5F5F5")
            mask = daily_regime == state
            in_block, block_start = False, None
            for dt, is_state in mask.items():
                if is_state and not in_block:
                    block_start = dt
                    in_block = True
                elif not is_state and in_block:
                    ax.axvspan(block_start, dt, alpha=0.50,
                               color=color, zorder=1, linewidth=0)
                    in_block = False
            if in_block and block_start is not None:
                ax.axvspan(block_start, equity.index[-1],
                           alpha=0.50, color=color, zorder=1, linewidth=0)

        regime_patches = [
            mpatches.Patch(facecolor=REGIME_LEGEND_COLORS[s], edgecolor="none",
                           alpha=0.85, label=REGIME_LABELS[s])
            for s in ["risk_on", "transition_up", "transition_down", "risk_off"]
        ]

    ax.fill_between(norm.index, norm.values, 100,
                    where=(norm.values >= 100), alpha=0.06,
                    color=PALETTE["strategy"], interpolate=True)
    strat_line, = ax.plot(norm.index, norm.values, linewidth=2.2,
                          color=PALETTE["strategy"], label="Strategy", zorder=3)
    ax.axhline(100, color=PALETTE["border"], linewidth=0.7)

    _style_axis(ax, title="Equity Curve with Regime Overlay",
                ylabel="Portfolio Value (Base = 100)")

    # Single combined legend: Strategy line first, then regime color patches
    all_handles = [strat_line] + regime_patches
    ax.legend(handles=all_handles, loc="upper left", fontsize=8,
              framealpha=0.88, edgecolor=PALETTE["border"],
              title="Regime" if regime_patches else None, title_fontsize=8)
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# 6. Annual Returns Bar
# ---------------------------------------------------------------------------

def annual_returns_bar(
    returns: pd.Series,
    benchmark_returns: Optional[pd.Series] = None,
    figsize: Tuple = (14, 4.5),
) -> plt.Figure:
    """Annual return grouped bar chart (strategy vs SPY)."""
    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_facecolor(PALETTE["bg"])

    annual = returns.resample("YE").apply(lambda x: (1 + x).prod() - 1)
    years = [str(y.year) for y in annual.index]
    x = np.arange(len(years))
    w = 0.35

    strat_colors = [PALETTE["strategy"] if v > 0 else PALETTE["negative"]
                    for v in annual.values]
    ax.bar(x - w / 2, annual.values * 100, w,
           color=strat_colors, label="Strategy", alpha=0.90, edgecolor="white",
           linewidth=0.5)

    if benchmark_returns is not None:
        bench = benchmark_returns.resample("YE").apply(lambda x: (1 + x).prod() - 1)
        bench = bench.reindex(annual.index, fill_value=0)
        bm_colors = [PALETTE["benchmark"] if v > 0 else "#FF8A65"
                     for v in bench.values]
        ax.bar(x + w / 2, bench.values * 100, w,
               color=bm_colors, label="SPY", alpha=0.75, edgecolor="white",
               linewidth=0.5)

    ax.set_xticks(x)
    ax.set_xticklabels(years, fontsize=9)
    ax.axhline(0, color=PALETTE["neutral"], linewidth=0.9)
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.0f%%"))
    _style_axis(ax, title="Annual Returns — Strategy vs Benchmark",
                ylabel="Return (%)")
    ax.legend()
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# 7. Monthly Returns Heatmap
# ---------------------------------------------------------------------------

def monthly_returns_heatmap(
    returns: pd.Series,
    figsize: Tuple = (14, 6),
) -> plt.Figure:
    """Calendar heatmap of monthly returns."""
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
    fig.patch.set_facecolor(PALETTE["bg"])

    vals = pivot.values[~np.isnan(pivot.values)]
    max_abs = max(abs(vals.min()), abs(vals.max()), 1.0) if len(vals) else 10.0

    cmap = LinearSegmentedColormap.from_list(
        "ret_cal",
        [PALETTE["negative"], "#F8F9FA", PALETTE["positive"]],
        N=512
    )
    im = ax.imshow(pivot.values, cmap=cmap, vmin=-max_abs, vmax=max_abs,
                   aspect="auto", interpolation="nearest")

    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(pivot.columns, fontsize=9)
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index, fontsize=9)

    for i in range(len(pivot.index)):
        for j in range(len(pivot.columns)):
            val = pivot.values[i, j]
            if not np.isnan(val):
                txt_color = "white" if abs(val) > max_abs * 0.55 else PALETTE["neutral"]
                ax.text(j, i, f"{val:.1f}%", ha="center", va="center",
                        fontsize=7, color=txt_color, fontweight="bold" if abs(val) > 5 else "normal")

    cbar = plt.colorbar(im, ax=ax, label="Monthly Return (%)", fraction=0.018, pad=0.02)
    cbar.ax.tick_params(labelsize=8)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_title("Monthly Returns Heatmap (%)", fontsize=12, fontweight="bold",
                 color=PALETTE["header_mid"], pad=10)
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# 8. Sector Correlation Matrix
# ---------------------------------------------------------------------------

def correlation_matrix_plot(
    prices: pd.DataFrame,
    figsize: Tuple = (10, 8.5),
) -> plt.Figure:
    """Sector return correlation heatmap."""
    rets = prices.pct_change().iloc[1:]
    corr = rets.corr()

    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_facecolor(PALETTE["bg"])

    cmap = LinearSegmentedColormap.from_list(
        "corr_map",
        [PALETTE["negative"], "#FAFAFA", PALETTE["header_mid"]],
        N=256
    )
    im = ax.imshow(corr.values, cmap=cmap, vmin=-1, vmax=1,
                   interpolation="nearest")

    n = len(corr.columns)
    ax.set_xticks(range(n))
    ax.set_xticklabels(corr.columns, rotation=45, ha="right", fontsize=9)
    ax.set_yticks(range(n))
    ax.set_yticklabels(corr.index, fontsize=9)

    for i in range(n):
        for j in range(n):
            v = corr.values[i, j]
            c = "white" if abs(v) > 0.65 else PALETTE["neutral"]
            ax.text(j, i, f"{v:.2f}", ha="center", va="center",
                    fontsize=7.5, color=c, fontweight="bold" if i == j else "normal")

    cbar = plt.colorbar(im, ax=ax, label="Correlation",
                        fraction=0.04, pad=0.04)
    cbar.ax.tick_params(labelsize=8)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_title("Sector Return Correlation Matrix", fontsize=12,
                 fontweight="bold", color=PALETTE["header_mid"], pad=10)
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
        ax.text(0.5, 0.5, "No attribution data", ha="center", va="center",
                color=PALETTE["muted"])
        return fig

    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_facecolor(PALETTE["bg"])

    dates = attribution.index
    alloc  = attribution["allocation"].values * 100
    select = attribution["selection"].values  * 100
    interact = attribution.get("interaction",
                               pd.Series(0, index=attribution.index)).values * 100
    x = np.arange(len(dates))

    ax.bar(x, alloc,   label="Allocation",  color=PALETTE["strategy"], alpha=0.8)
    ax.bar(x, select,  bottom=alloc,         label="Selection",  color=PALETTE["ew"], alpha=0.8)
    ax.bar(x, interact,bottom=alloc + select,label="Interaction",color=PALETTE["benchmark"], alpha=0.6)

    ax.axhline(0, color=PALETTE["neutral"], linewidth=0.8)
    step = max(1, len(dates) // 20)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([d.strftime("%Y-%m") for d in dates[::step]],
                       rotation=45, ha="right")
    _style_axis(ax, title="Brinson Attribution: Allocation vs Selection",
                ylabel="Active Return (bps)")
    ax.legend()
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def generate_all_plots(result) -> Dict[str, plt.Figure]:
    """Generate all standard plots from a BacktestResult."""
    daily_ret    = result.daily_returns
    equity       = result.equity_curve
    bench_ret    = result.benchmark_returns
    bench_eq     = result.benchmark_equity
    weights_hist = result.weights_history
    regime_hist  = result.regime_history

    figs = {}
    figs["equity_curve"]  = equity_curve_plot(equity, bench_eq)
    figs["drawdown"]      = drawdown_plot(daily_ret, bench_ret)
    figs["rolling_sharpe"]= rolling_sharpe_plot(daily_ret)
    figs["sector_weights"]= sector_weights_heatmap(weights_hist)
    figs["regime_overlay"]= regime_overlay_plot(equity, regime_hist)
    figs["annual_returns"]= annual_returns_bar(daily_ret, bench_ret)
    figs["monthly_returns"]= monthly_returns_heatmap(daily_ret)
    return figs
