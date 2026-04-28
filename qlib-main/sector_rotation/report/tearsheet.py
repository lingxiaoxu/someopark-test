"""
Tearsheet Generator — Professional Edition
==========================================
Generates a multi-page institutional-quality PDF tearsheet.

PDF Layout
----------
Page 1  : Cover — headline KPIs + full performance metrics table
Page 2  : Cumulative Return (strategy vs SPY, gradient fill)
Page 3  : Drawdown (%) + 12-month Rolling Sharpe
Page 4  : Monthly Sector Allocation heatmap
Page 5  : Equity with Regime Overlay + Annual Returns bar chart
Page 6  : Monthly Returns calendar heatmap
Page 7  : Subperiod Analysis + Top 5 Drawdown Episodes
Page 8  : Sector Correlation Matrix
Page 9  : Latest Signal — current portfolio, sector rankings, risk flags
Page 10 : Rebalance History log (all trades)
"""

from __future__ import annotations

import json
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
import matplotlib.ticker as mticker
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyBboxPatch, Rectangle

from .plots import (
    PALETTE, REGIME_LABELS,
    equity_curve_plot, drawdown_plot, rolling_sharpe_plot,
    sector_weights_heatmap, regime_overlay_plot, annual_returns_bar,
    monthly_returns_heatmap, correlation_matrix_plot,
    _draw_styled_table,
)


# ---------------------------------------------------------------------------
# Cover page
# ---------------------------------------------------------------------------

def _add_title_page(pdf: PdfPages, metrics: dict, config: dict,
                    equity: Optional[pd.Series] = None) -> None:
    """Page 1 — Premium cover with KPI cards + full metrics table."""

    fig = plt.figure(figsize=(14, 9))
    fig.patch.set_facecolor(PALETTE["bg"])

    # ── Canvas axes (full figure, data coords 0..1 × 0..1) ──────────────────
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_facecolor(PALETTE["bg"])

    # ── HEADER BAND ──────────────────────────────────────────────────────────
    header = Rectangle((0, 0.832), 1, 0.168,
                        facecolor=PALETTE["header_dark"],
                        edgecolor="none", transform=ax.transAxes,
                        clip_on=False, zorder=2)
    ax.add_patch(header)

    # Gold accent line below header
    accent = Rectangle((0, 0.828), 1, 0.004,
                        facecolor=PALETTE["gold"],
                        edgecolor="none", transform=ax.transAxes,
                        clip_on=False, zorder=3)
    ax.add_patch(accent)

    # Header text
    ax.text(0.040, 0.975, "Someo Park — 策略 PnL 分析报告",
            transform=ax.transAxes, ha="left", va="center",
            fontsize=8.5, fontweight="bold", color=PALETTE["gold"],
            zorder=4, fontfamily="Hiragino Sans GB")

    ax.text(0.040, 0.938, "Sector Rotation Strategy  —  Performance Tearsheet",
            transform=ax.transAxes, ha="left", va="center",
            fontsize=16, fontweight="bold", color="white", zorder=4)

    # Period + generated date (right-aligned)
    start_date = config.get("backtest", {}).get("start_date", "N/A")
    if equity is not None and not equity.empty:
        end_str = str(equity.index[-1].date())
    else:
        end_str = str(config.get("backtest", {}).get("end_date") or
                      datetime.now().strftime("%Y-%m-%d"))

    ax.text(0.960, 0.975,
            f"Period:  {start_date}  →  {end_str}",
            transform=ax.transAxes, ha="right", va="center",
            fontsize=9.5, color="#90A8C0", zorder=4)
    ax.text(0.960, 0.938,
            f"Generated:  {datetime.now().strftime('%Y-%m-%d  %H:%M')}   |   Universe: 11 SPDR Sector ETFs",
            transform=ax.transAxes, ha="right", va="center",
            fontsize=8, color="#70889A", zorder=4)

    # ── STAT CARDS (4 across) ────────────────────────────────────────────────
    m = metrics
    cagr    = m.get("annual_return", float("nan"))
    sharpe  = m.get("sharpe",        float("nan"))
    maxdd   = m.get("max_drawdown",  float("nan"))
    winrate = m.get("monthly_win_rate", float("nan"))
    total_r = m.get("total_return",  float("nan"))
    capital = 1_000_000 * (1 + total_r) if not np.isnan(total_r) else float("nan")

    # Capital summary — third line of header (left side, bottom of band)
    if not np.isnan(total_r):
        ax.text(0.040, 0.847,
                f"\\$1,000,000  →  \\${capital:,.0f}   (Total Return  {total_r:.1%})",
                transform=ax.transAxes, ha="left", va="center",
                fontsize=9, fontweight="bold", color=PALETTE["gold"],
                zorder=5, alpha=0.90)

    def _pct(v, d=1): return f"{v:.{d}%}" if not np.isnan(v) else "N/A"
    def _val(v, d=3): return f"{v:.{d}f}" if not np.isnan(v) else "N/A"

    def _is_good(v, threshold, direction="above"):
        if np.isnan(v): return None
        return (v > threshold) if direction == "above" else (v > threshold)

    card_data = [
        ("CAGR",            _pct(cagr),    "Target  > 10%",   cagr  > 0.10  if not np.isnan(cagr)  else False),
        ("Sharpe Ratio",    _val(sharpe),  "Target  > 0.4",   sharpe > 0.4  if not np.isnan(sharpe) else False),
        ("Max Drawdown",    _pct(maxdd),   "Target  > −20%",  maxdd > -0.20 if not np.isnan(maxdd)  else False),
        ("Monthly Win Rate",_pct(winrate), "Target  > 55%",   winrate > 0.55 if not np.isnan(winrate) else False),
    ]

    card_x0, card_y0 = 0.020, 0.625
    card_w,  card_h  = 0.233, 0.185
    card_gap = 0.008

    for i, (title, value, target, is_pass) in enumerate(card_data):
        x = card_x0 + i * (card_w + card_gap)
        # Shadow
        shadow = FancyBboxPatch((x + 0.003, card_y0 - 0.004), card_w, card_h,
                                boxstyle="round,pad=0.012",
                                facecolor="#C8D0DA", edgecolor="none",
                                transform=ax.transAxes, zorder=2, clip_on=False)
        ax.add_patch(shadow)
        # Card
        card = FancyBboxPatch((x, card_y0), card_w, card_h,
                               boxstyle="round,pad=0.012",
                               facecolor="white",
                               edgecolor=PALETTE["border"], linewidth=1.2,
                               transform=ax.transAxes, zorder=3, clip_on=False)
        ax.add_patch(card)

        cx = x + card_w / 2
        # Metric name — close to card top edge
        ax.text(cx, card_y0 + card_h * 0.88, title,
                transform=ax.transAxes, ha="center", va="center",
                fontsize=8.5, color=PALETTE["muted"], zorder=4)
        # Value (big) — 0.17 gap below title
        val_color = PALETTE["strategy"] if is_pass else PALETTE["negative"]
        ax.text(cx, card_y0 + card_h * 0.71, value,
                transform=ax.transAxes, ha="center", va="center",
                fontsize=22, fontweight="bold", color=val_color, zorder=4)
        # Target label (bottom of card, with enough clearance from card edge)
        ax.text(cx, card_y0 + card_h * 0.09, target,
                transform=ax.transAxes, ha="center", va="center",
                fontsize=7, color=PALETTE["muted"], zorder=4)
        # Pass/fail badge (above target label)
        badge_color = "#E8F5E9" if is_pass else "#FFEBEE"
        badge_txt_c = PALETTE["positive"] if is_pass else PALETTE["negative"]
        badge_sym   = "✔  PASS" if is_pass else "✖  BELOW"
        badge_y0 = card_y0 + card_h * 0.21
        badge = FancyBboxPatch((cx - 0.048, badge_y0),
                               0.096, 0.038,
                               boxstyle="round,pad=0.005",
                               facecolor=badge_color,
                               edgecolor="none",
                               transform=ax.transAxes, zorder=4, clip_on=False)
        ax.add_patch(badge)
        ax.text(cx, badge_y0 + 0.019, badge_sym,
                transform=ax.transAxes, ha="center", va="center",
                fontsize=7, color=badge_txt_c, fontweight="bold", zorder=5)

    # ── SECTION DIVIDER ──────────────────────────────────────────────────────
    sep = Rectangle((0.020, 0.614), 0.960, 0.0015,
                    facecolor=PALETTE["border"],
                    edgecolor="none", transform=ax.transAxes,
                    clip_on=False, zorder=3)
    ax.add_patch(sep)

    # ── FULL METRICS TABLE ───────────────────────────────────────────────────
    ax2 = fig.add_axes([0.020, 0.030, 0.960, 0.575])
    ax2.set_xlim(0, 1)
    ax2.set_ylim(0, 1)
    ax2.axis("off")

    table_rows_raw = [
        ("Total Return",       _pct(m.get("total_return")),         "> SPY",      m.get("total_return", 0) > 0),
        ("CAGR",               _pct(m.get("annual_return")),        "> 10%",      m.get("annual_return", 0) > 0.10),
        ("Annualized Vol",     _pct(m.get("annual_vol")),           "~ 12%",      abs(m.get("annual_vol", 0) - 0.12) < 0.04),
        ("Sharpe Ratio",       _val(m.get("sharpe")),               "> 0.4",      m.get("sharpe", 0) > 0.4),
        ("Calmar Ratio",       _val(m.get("calmar")),               "> 0.5",      m.get("calmar", 0) > 0.5),
        ("Max Drawdown",       _pct(m.get("max_drawdown")),         "> −20%",     m.get("max_drawdown", -1) > -0.20),
        ("Max DD Duration",    f"{m.get('max_drawdown_days', 0):.0f} days",   "< 365 d",  m.get("max_drawdown_days", 999) < 365),
        ("CVaR 95%",           _pct(m.get("cvar_95")),              "—",          None),
        ("Monthly Win Rate",   _pct(m.get("monthly_win_rate")),     "> 55%",      m.get("monthly_win_rate", 0) > 0.55),
        ("Info Ratio vs SPY",  _val(m.get("info_ratio")),           "> 0.3",      m.get("info_ratio", -99) > 0.3),
        ("Active Return",      _pct(m.get("active_return")),        "> 2%",       m.get("active_return", -99) > 0.02),
        ("Tracking Error",     _pct(m.get("tracking_error")),       "—",          None),
        ("Skewness",           _val(m.get("skewness")),             "> 0",        m.get("skewness", -1) > 0),
        ("Kurtosis",           _val(m.get("kurtosis")),             "—",          None),
    ]

    col_x  = [0.002, 0.420, 0.660, 0.840]
    col_w  = [0.418, 0.240, 0.180, 0.158]
    aligns = ["left", "center", "center", "center"]

    def _status(ok):
        if ok is None: return "—"
        return "✔ PASS" if ok else "✖ MISS"

    def _status_color(ok):
        if ok is None: return PALETTE["muted"]
        return PALETTE["positive"] if ok else PALETTE["negative"]

    rows_display = [[name, val, tgt, _status(ok)]
                    for name, val, tgt, ok in table_rows_raw]

    # Value + status colors per cell
    row_colors = []
    for _, val_str, _, ok in table_rows_raw:
        # Value cell color: green if positive numeric, red if negative, else neutral
        try:
            num = float(val_str.strip("%").replace(",", ""))
            v_color = PALETTE["positive"] if num > 0 else PALETTE["negative"] if num < 0 else PALETTE["neutral"]
        except Exception:
            v_color = PALETTE["neutral"]
        s_color = _status_color(ok)
        row_colors.append([PALETTE["neutral"], v_color, PALETTE["muted"], s_color])

    _draw_styled_table(
        ax2,
        headers=["PERFORMANCE METRIC", "VALUE", "TARGET", "STATUS"],
        rows=rows_display,
        col_x=col_x, col_w=col_w,
        row_h=0.058,
        y_start=0.980,
        col_aligns=aligns,
        row_colors=row_colors,
    )

    # ── FOOTER ────────────────────────────────────────────────────────────────
    ax.text(0.5, 0.005,
            "Someo Park  ·  Sector Rotation Strategy v1.0  ·  "
            f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}  ·  qlib_run env",
            transform=ax.transAxes, ha="center", va="bottom",
            fontsize=7, color=PALETTE["muted"])

    plt.tight_layout(pad=0)
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Subperiod + Drawdown episodes
# ---------------------------------------------------------------------------

def _add_subperiod_table(pdf: PdfPages,
                          subperiod_df: pd.DataFrame,
                          dd_episodes: pd.DataFrame) -> None:
    """Page 7 — Styled subperiod analysis + drawdown episodes."""
    fig = plt.figure(figsize=(14, 10))
    fig.patch.set_facecolor(PALETTE["bg"])

    # ── Top half: Subperiod ──────────────────────────────────────────────────
    ax1 = fig.add_axes([0.020, 0.530, 0.960, 0.440])
    ax1.set_xlim(0, 1)
    ax1.set_ylim(0, 1)
    ax1.axis("off")

    # Section header
    hdr = Rectangle((0, 0.920), 1, 0.080, facecolor=PALETTE["header_mid"],
                     edgecolor="none", transform=ax1.transAxes,
                     clip_on=False, zorder=2)
    ax1.add_patch(hdr)
    ax1.text(0.015, 0.960, "SUBPERIOD PERFORMANCE ANALYSIS",
             transform=ax1.transAxes, ha="left", va="center",
             fontsize=10.5, fontweight="bold",
             color=PALETTE["tbl_header_txt"], zorder=3)

    if not subperiod_df.empty:
        cols_map = {
            "annual_return": "CAGR",
            "annual_vol":    "Vol",
            "sharpe":        "Sharpe",
            "max_drawdown":  "Max DD",
            "info_ratio":    "Info Ratio",
            "monthly_win_rate": "Win %",
        }
        avail_cols = [c for c in cols_map if c in subperiod_df.columns]
        sub = subperiod_df[avail_cols].copy()
        sub.columns = [cols_map[c] for c in avail_cols]

        def _fmt(v, col):
            if isinstance(v, float) and np.isnan(v): return "—"
            pct_cols = {"CAGR", "Vol", "Max DD", "Win %"}
            return f"{v:.1%}" if col in pct_cols else f"{v:.3f}"

        for col in sub.columns:
            sub[col] = sub[col].apply(lambda v: _fmt(v, col))

        n_cols = len(sub.columns) + 1  # +1 for row label
        cw = 1.0 / n_cols
        col_x  = [i * cw for i in range(n_cols)]
        col_w  = [cw]     * n_cols
        aligns = ["left"] + ["center"] * (n_cols - 1)

        rows_display = [[idx] + list(row) for idx, row in zip(sub.index, sub.values)]

        # Color CAGR and Max DD
        row_colors = []
        for idx in subperiod_df.index:
            r = subperiod_df.loc[idx]
            c_cagr = PALETTE["positive"] if r.get("annual_return", 0) > 0 else PALETTE["negative"]
            c_dd   = PALETTE["positive"] if r.get("max_drawdown",  0) > -0.15 else PALETTE["negative"]
            base   = [PALETTE["neutral"]] * n_cols
            if "CAGR"   in sub.columns: base[1] = c_cagr
            if "Max DD" in sub.columns:
                idx_dd = list(sub.columns).index("Max DD") + 1
                base[idx_dd] = c_dd
            row_colors.append(base)

        _draw_styled_table(ax1,
                           headers=["PERIOD"] + list(sub.columns),
                           rows=rows_display,
                           col_x=col_x, col_w=col_w,
                           row_h=0.115, y_start=0.890,
                           col_aligns=aligns,
                           row_colors=row_colors)

    # ── Bottom half: DD Episodes ──────────────────────────────────────────────
    ax2 = fig.add_axes([0.020, 0.020, 0.960, 0.465])
    ax2.set_xlim(0, 1)
    ax2.set_ylim(0, 1)
    ax2.axis("off")

    # Red section header
    hdr2 = Rectangle((0, 0.920), 1, 0.080,
                      facecolor="#8B1A1A",
                      edgecolor="none", transform=ax2.transAxes,
                      clip_on=False, zorder=2)
    ax2.add_patch(hdr2)
    ax2.text(0.015, 0.960, "TOP 5 WORST DRAWDOWN EPISODES",
             transform=ax2.transAxes, ha="left", va="center",
             fontsize=10.5, fontweight="bold",
             color=PALETTE["tbl_header_txt"], zorder=3)

    if not dd_episodes.empty:
        dd_disp = dd_episodes.copy()
        for col in ["peak_date", "trough_date", "recovery_date"]:
            if col in dd_disp.columns:
                dd_disp[col] = dd_disp[col].apply(
                    lambda x: str(x.date()) if pd.notna(x) else "Not recovered"
                )
        if "drawdown_pct" in dd_disp.columns:
            dd_disp["drawdown_pct"] = dd_disp["drawdown_pct"].apply(
                lambda x: f"{x:.2f}%" if isinstance(x, float) else str(x)
            )

        col_names = [c.replace("_", " ").title() for c in dd_disp.columns]
        n_cols = len(dd_disp.columns)
        col_w  = [1.0 / n_cols] * n_cols
        col_x  = [i * col_w[0] for i in range(n_cols)]

        row_colors = [[PALETTE["negative"]] * n_cols] * len(dd_disp)

        _draw_styled_table(ax2,
                           headers=col_names,
                           rows=dd_disp.values.tolist(),
                           col_x=col_x, col_w=col_w,
                           row_h=0.115, y_start=0.890,
                           header_bg="#8B1A1A",
                           row_colors=row_colors)

    plt.tight_layout(pad=0.3)
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Latest signal / current portfolio page
# ---------------------------------------------------------------------------

def _add_latest_signal_page(pdf: PdfPages,
                              result,
                              daily_report_path: Optional[Path] = None) -> None:
    """
    Page 9 — Current portfolio signal with sector rankings and risk flags.

    Data source: last row of BacktestResult.weights_history + signals_history +
                 risk_flags, optionally augmented by the latest daily report JSON.
    """
    # ── Gather data ──────────────────────────────────────────────────────────
    report_data = None
    if daily_report_path and daily_report_path.exists():
        try:
            with open(daily_report_path) as f:
                report_data = json.load(f)
        except Exception:
            pass

    # Prefer daily report JSON (most current)
    if report_data:
        signal_date  = report_data.get("signal_date", "N/A")
        regime_label = report_data.get("regime", {}).get("label", "N/A").upper().replace("_", " ")
        vix_val      = report_data.get("regime", {}).get("vix", float("nan"))
        hy_spread    = report_data.get("regime", {}).get("hy_spread_bps", float("nan"))
        yc           = report_data.get("regime", {}).get("yield_curve_pct", float("nan"))
        cash_pct     = report_data.get("cash_weight", 0.0)
        risk_notes   = report_data.get("risk_flags", "")
        signals_list = report_data.get("signals", [])
        trades_list  = [s for s in signals_list if s.get("action") in ("ENTER", "REBALANCE", "EXIT", "HOLD")]
        active_list  = [s for s in signals_list if s.get("target_weight", 0) > 0.001]
        tx           = report_data.get("transaction_costs", {})
        cost_usd     = tx.get("total_cost_usd", 0)
        cost_bps     = tx.get("total_cost_bps", 0)
        turnover     = tx.get("turnover_pct", 0)
        n_pos        = report_data.get("holdings_summary", {}).get("n_positions", len(active_list))
        rebal_reason = report_data.get("rebalance_decision", {}).get("reason", "N/A")
        rebal_flag   = report_data.get("rebalance_decision", {}).get("rebalance", False)
    elif not result.weights_history.empty:
        last_date  = result.weights_history.index[-1]
        signal_date = str(last_date.date())
        last_w = result.weights_history.iloc[-1]
        last_s = result.signals_history.iloc[-1] if not result.signals_history.empty else pd.Series(dtype=float)
        last_reg = result.regime_history.iloc[-1] if not result.regime_history.empty else "N/A"
        regime_label = str(last_reg).upper().replace("_", " ")
        vix_val = hy_spread = yc = float("nan")
        cash_pct = 0.0
        active_list = [{"ticker": t, "target_weight": w, "composite_score": last_s.get(t, float("nan")), "action": "HOLD"}
                       for t, w in last_w.items() if w > 0.001]
        trades_list  = active_list
        risk_notes   = ""
        cost_usd = cost_bps = turnover = 0.0
        n_pos = len(active_list)
        rebal_reason = "last rebalance"
        rebal_flag = True
    else:
        return  # No data to show

    # ── Figure ───────────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(14, 10))
    fig.patch.set_facecolor(PALETTE["bg"])

    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_facecolor(PALETTE["bg"])

    # ── HEADER BAND ──────────────────────────────────────────────────────────
    hdr = Rectangle((0, 0.895), 1, 0.105,
                     facecolor=PALETTE["header_mid"],
                     edgecolor="none", transform=ax.transAxes,
                     clip_on=False, zorder=2)
    ax.add_patch(hdr)
    gld = Rectangle((0, 0.890), 1, 0.005,
                     facecolor=PALETTE["gold"],
                     edgecolor="none", transform=ax.transAxes,
                     clip_on=False, zorder=3)
    ax.add_patch(gld)

    ax.text(0.040, 0.955, "LATEST PORTFOLIO SIGNAL",
            transform=ax.transAxes, ha="left", va="center",
            fontsize=16, fontweight="bold", color="white", zorder=4)
    ax.text(0.040, 0.910, f"Signal Date:  {signal_date}",
            transform=ax.transAxes, ha="left", va="center",
            fontsize=9, color="#90A8C0", zorder=4)

    # Regime badge
    regime_bg = {"RISK ON": "#2E7D32", "TRANSITION UP": "#F57F17",
                 "TRANSITION DOWN": "#E65100", "RISK OFF": "#B71C1C"}.get(regime_label, PALETTE["accent"])
    badge_w = 0.13
    badge_h = 0.042
    badge_x = 0.855
    badge_y = 0.916
    badge = FancyBboxPatch((badge_x, badge_y), badge_w, badge_h,
                           boxstyle="round,pad=0.008",
                           facecolor=regime_bg, edgecolor="none",
                           transform=ax.transAxes, zorder=4, clip_on=False)
    ax.add_patch(badge)
    ax.text(badge_x + badge_w / 2, badge_y + badge_h / 2, regime_label,
            transform=ax.transAxes, ha="center", va="center",
            fontsize=9, fontweight="bold", color="white", zorder=5)

    # ── MACRO STRIP ──────────────────────────────────────────────────────────
    macro_items = []
    if not np.isnan(vix_val):    macro_items.append(f"VIX  {vix_val:.1f}")
    if not np.isnan(hy_spread):  macro_items.append(f"HY Spread  {hy_spread:.0f} bps")
    if not np.isnan(yc):         macro_items.append(f"Yield Curve  {yc:+.2f}%")
    macro_items.append(f"Cash  {cash_pct:.0%}")
    macro_items.append(f"Positions  {n_pos}")

    macro_strip = Rectangle((0, 0.855), 1, 0.035,
                             facecolor="#EEF2F7", edgecolor="none",
                             transform=ax.transAxes, clip_on=False, zorder=2)
    ax.add_patch(macro_strip)
    macro_txt = "    ·    ".join(macro_items)
    ax.text(0.040, 0.872, macro_txt,
            transform=ax.transAxes, ha="left", va="center",
            fontsize=9, color=PALETTE["neutral"], zorder=3)

    if risk_notes:
        # Parse RiskFlags repr string → extract triggered flags + notes
        rf_str = str(risk_notes)
        triggered = []
        for flag, label in [("vol_scaling_triggered=True", "Vol"),
                             ("vix_emergency_triggered=True", "VIX"),
                             ("dd_circuit_triggered=True", "DD"),
                             ("beta_adjusted=True", "Beta")]:
            if flag in rf_str:
                triggered.append(label)
        # Extract notes list content
        import re as _re
        notes_match = _re.search(r"notes=\[([^\]]*)\]", rf_str)
        notes_str = ""
        if notes_match:
            raw = notes_match.group(1).strip()
            if raw:
                # strip quotes around each item and join
                items = [s.strip().strip("'\"") for s in raw.split(",") if s.strip().strip("'\"")]
                notes_str = "; ".join(items[:2])  # max 2 notes to keep line short
        if triggered:
            risk_line = "Risk Flags: " + ", ".join(triggered)
            if notes_str:
                risk_line += f"  ({notes_str[:60]})"
        elif notes_str:
            risk_line = f"Risk: {notes_str[:80]}"
        else:
            risk_line = "Risk: None triggered"
        ax.text(0.960, 0.872, risk_line[:100],
                transform=ax.transAxes, ha="right", va="center",
                fontsize=7.5, color=PALETTE["muted"], zorder=3)

    # ── CURRENT POSITIONS TABLE ───────────────────────────────────────────────
    ax_pos = fig.add_axes([0.020, 0.540, 0.960, 0.295])
    ax_pos.set_xlim(0, 1); ax_pos.set_ylim(0, 1)
    ax_pos.axis("off")

    pos_hdr = Rectangle((0, 0.920), 1, 0.080,
                         facecolor=PALETTE["header_dark"],
                         edgecolor="none", transform=ax_pos.transAxes,
                         clip_on=False, zorder=2)
    ax_pos.add_patch(pos_hdr)
    rebal_txt = f"REBALANCE: {rebal_reason.upper()}" if rebal_flag else "NO REBALANCE"
    ax_pos.text(0.015, 0.960, f"CURRENT PORTFOLIO  ·  {rebal_txt}",
                transform=ax_pos.transAxes, ha="left", va="center",
                fontsize=10.5, fontweight="bold",
                color=PALETTE["tbl_header_txt"], zorder=3)

    if active_list:
        active_sorted = sorted(active_list, key=lambda s: s.get("target_weight", 0), reverse=True)
        tbl_rows = []
        tbl_colors = []
        for s in active_sorted:
            ticker = s.get("ticker", "")
            wgt    = s.get("target_weight", 0)
            prev   = s.get("current_weight", 0)
            delta  = wgt - prev
            score  = s.get("composite_score", float("nan"))
            action = s.get("action", "HOLD")
            sh     = s.get("target_shares", 0)
            price  = s.get("price",  float("nan"))
            value  = sh * price if sh and not np.isnan(price) else float("nan")

            delta_str = f"{delta:+.1%}" if not np.isnan(delta) else "—"
            score_str = f"{score:.3f}" if not np.isnan(score) else "—"
            val_str   = f"${value:,.0f}" if not np.isnan(value) else "—"

            action_color = {
                "ENTER":     PALETTE["positive"],
                "REBALANCE": PALETTE["strategy"],
                "EXIT":      PALETTE["negative"],
                "HOLD":      PALETTE["muted"],
            }.get(action, PALETTE["neutral"])

            delta_color = PALETTE["positive"] if delta > 0.001 else (
                PALETTE["negative"] if delta < -0.001 else PALETTE["muted"])

            tbl_rows.append([ticker, f"{wgt:.1%}", f"{prev:.1%}",
                             delta_str, score_str, action,
                             str(sh) if sh else "—", val_str])
            tbl_colors.append([
                PALETTE["header_mid"],  # ticker (bold navy)
                PALETTE["neutral"],     # target
                PALETTE["muted"],       # prev
                delta_color,            # delta
                PALETTE["strategy"],    # score
                action_color,           # action
                PALETTE["neutral"],     # shares
                PALETTE["neutral"],     # value
            ])

        _draw_styled_table(
            ax_pos,
            headers=["SECTOR", "TARGET %", "PREV %", "DELTA", "SIGNAL", "ACTION", "SHARES", "EST. VALUE"],
            rows=tbl_rows,
            col_x=[0.002, 0.105, 0.205, 0.305, 0.405, 0.510, 0.635, 0.788],
            col_w=[0.103, 0.100, 0.100, 0.100, 0.105, 0.125, 0.153, 0.210],
            row_h=0.135,
            y_start=0.880,
            col_aligns=["left", "center", "center", "center", "center", "center", "right", "right"],
            row_colors=tbl_colors,
        )

        # Transaction cost strip
        if cost_usd > 0:
            ax_pos.text(0.985, 0.005,
                        f"Est. Transaction Cost:  ${cost_usd:,.0f}  ({cost_bps:.1f} bps)   |   "
                        f"Turnover:  {turnover:.1f}%",
                        transform=ax_pos.transAxes, ha="right", va="bottom",
                        fontsize=8, color=PALETTE["muted"])

    # ── SECTOR SIGNAL RANKING (horizontal bar chart) ─────────────────────────
    ax_sig = fig.add_axes([0.020, 0.040, 0.960, 0.460])
    ax_sig.set_facecolor(PALETTE["surface"])

    # Collect all sector signals
    if report_data and signals_list:
        sig_sorted = sorted(signals_list, key=lambda s: s.get("composite_score", 0), reverse=True)
    elif not result.signals_history.empty:
        last_s = result.signals_history.iloc[-1].sort_values(ascending=False)
        sig_sorted = [{"ticker": t, "composite_score": v,
                       "target_weight": result.weights_history.iloc[-1].get(t, 0)}
                      for t, v in last_s.items()]
    else:
        sig_sorted = []

    if sig_sorted:
        tickers = [s.get("ticker", "") for s in sig_sorted]
        scores  = [float(s.get("composite_score", 0)) for s in sig_sorted]
        weights = [float(s.get("target_weight", 0))   for s in sig_sorted]
        n = len(tickers)
        y_pos = np.arange(n)

        bar_colors = []
        for i, (t, sc, wt) in enumerate(zip(tickers, scores, weights)):
            if wt > 0.001:
                bar_colors.append(PALETTE["strategy"])
            elif sc > 0:
                bar_colors.append("#B0BEC5")
            else:
                bar_colors.append("#FFCDD2")

        bars = ax_sig.barh(y_pos, scores, color=bar_colors,
                           edgecolor="white", linewidth=0.5,
                           height=0.65, zorder=3)

        ax_sig.axvline(0, color=PALETTE["neutral"], linewidth=1.0, zorder=4)
        ax_sig.set_yticks(y_pos)
        ax_sig.set_yticklabels(tickers, fontsize=10, fontweight="bold",
                               color=PALETTE["neutral"])
        ax_sig.invert_yaxis()

        # Value labels
        for bar, sc, wt in zip(bars, scores, weights):
            label_x = sc + 0.04 * (1 if sc >= 0 else -1)
            align = "left" if sc >= 0 else "right"
            wt_str = f"  {wt:.1%}" if wt > 0.001 else ""
            ax_sig.text(label_x, bar.get_y() + bar.get_height() / 2,
                        f"{sc:+.3f}{wt_str}",
                        va="center", ha=align, fontsize=8.5,
                        color=PALETTE["neutral"], zorder=5)

        ax_sig.set_title("Sector Signal Rankings  (Composite Z-Score)",
                         fontsize=11, fontweight="bold",
                         color=PALETTE["header_mid"], pad=8)
        ax_sig.set_xlabel("Composite Z-Score", fontsize=9, color=PALETTE["muted"])
        ax_sig.grid(axis="x", alpha=0.5, color=PALETTE["grid"])
        ax_sig.grid(axis="y", visible=False)
        for spine in ["top", "right"]:
            ax_sig.spines[spine].set_visible(False)
        for spine in ["left", "bottom"]:
            ax_sig.spines[spine].set_edgecolor(PALETTE["border"])
            ax_sig.spines[spine].set_linewidth(0.7)

        # Legend patches
        from matplotlib.patches import Patch
        legend_els = [
            Patch(facecolor=PALETTE["strategy"],  label="Active (Allocated)"),
            Patch(facecolor="#B0BEC5",             label="Positive / Not Allocated"),
            Patch(facecolor="#FFCDD2",             label="Negative Score"),
        ]
        ax_sig.legend(handles=legend_els, loc="lower right", fontsize=8,
                      framealpha=0.85)

    plt.tight_layout(pad=0.3)
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Rebalance history page
# ---------------------------------------------------------------------------

def _add_rebalance_history_page(pdf: PdfPages, result) -> None:
    """
    Page 10 — Rebalance history log showing all trades across the backtest.
    """
    if result.weights_history.empty:
        return

    wh  = result.weights_history
    sh  = result.signals_history if not result.signals_history.empty else pd.DataFrame()
    rh  = result.regime_history
    rf  = result.risk_flags       # list of dicts
    ch  = result.costs_history

    # Build rebalance log
    rows = []
    prev_w = pd.Series(dtype=float)

    # Build a daily-indexed regime series for easy lookup
    if not rh.empty:
        all_dates = pd.date_range(rh.index.min(), wh.index.max() + pd.offsets.MonthEnd(1), freq="D")
        regime_daily = rh.reindex(all_dates, method="ffill").fillna("N/A")
    else:
        regime_daily = pd.Series(dtype=str)

    for i, (dt, cur_w) in enumerate(wh.iterrows()):
        cur_w = cur_w[cur_w > 0.001]
        if not regime_daily.empty and dt in regime_daily.index:
            regime = regime_daily[dt]
        else:
            regime = "N/A"
        regime_str = str(regime).replace("_", " ").title()

        # Entered / exited
        entered = sorted(set(cur_w.index) - set(prev_w.index))
        exited  = sorted(set(prev_w.index) - set(cur_w.index))
        entered_str = ", ".join(entered) if entered else "—"
        exited_str  = ", ".join(exited)  if exited  else "—"

        # Risk flags
        rf_dict = {}
        for r in rf:
            try:
                r_dt = pd.Timestamp(r.get("date", ""))
                if abs((r_dt - dt).days) <= 3:
                    rf_dict = r
                    break
            except Exception:
                pass

        cash_pct = rf_dict.get("cash_pct", 0.0)
        flags_triggered = []
        if rf_dict.get("vix_emergency_triggered"): flags_triggered.append("VIX")
        if rf_dict.get("vol_scaling_triggered"):   flags_triggered.append("Vol")
        if rf_dict.get("dd_circuit_triggered"):    flags_triggered.append("DD")
        if rf_dict.get("beta_adjusted"):           flags_triggered.append("β")
        flags_str = ", ".join(flags_triggered) if flags_triggered else "—"

        # Cost
        cost_row = ch.loc[dt] if (not ch.empty and dt in ch.index) else None
        cost_bps = cost_row.get("cost_bps", float("nan")) if cost_row is not None else float("nan")
        cost_str = f"{cost_bps:.1f} bps" if not np.isnan(cost_bps) else "—"

        rows.append({
            "date":    dt.strftime("%Y-%m-%d"),
            "regime":  regime_str,
            "entered": entered_str,
            "exited":  exited_str,
            "n_pos":   str(len(cur_w)),
            "cash":    f"{cash_pct:.0%}" if cash_pct > 0.001 else "0%",
            "flags":   flags_str,
            "cost":    cost_str,
        })
        prev_w = cur_w

    if not rows:
        return

    # Split into pages of ~30 rows each
    page_size = 32
    pages = [rows[i:i + page_size] for i in range(0, len(rows), page_size)]

    for p_idx, page_rows in enumerate(pages):
        fig = plt.figure(figsize=(14, 10))
        fig.patch.set_facecolor(PALETTE["bg"])

        ax = fig.add_axes([0.010, 0.010, 0.980, 0.980])
        ax.set_xlim(0, 1); ax.set_ylim(0, 1)
        ax.axis("off")

        # Header
        hdr = Rectangle((0, 0.950), 1, 0.050,
                         facecolor=PALETTE["header_mid"],
                         edgecolor="none", transform=ax.transAxes,
                         clip_on=False, zorder=2)
        ax.add_patch(hdr)
        suffix = f"  (page {p_idx + 1} / {len(pages)})" if len(pages) > 1 else ""
        ax.text(0.015, 0.975,
                f"REBALANCE HISTORY LOG{suffix}  ·  {len(rows)} total rebalance events",
                transform=ax.transAxes, ha="left", va="center",
                fontsize=10, fontweight="bold",
                color=PALETTE["tbl_header_txt"], zorder=3)

        tbl_rows = [[r["date"], r["regime"], r["entered"],
                     r["exited"], r["n_pos"], r["cash"],
                     r["flags"], r["cost"]]
                    for r in page_rows]

        # Color flags column red when triggered
        row_colors_hist = []
        for r in page_rows:
            f_color = PALETTE["negative"] if r["flags"] != "—" else PALETTE["muted"]
            c_color = PALETTE["negative"] if r["cash"] != "0%"  else PALETTE["muted"]
            row_colors_hist.append([
                PALETTE["neutral"], PALETTE["neutral"],
                PALETTE["strategy"], PALETTE["negative"] if r["exited"] != "—" else PALETTE["muted"],
                PALETTE["neutral"], c_color, f_color, PALETTE["muted"],
            ])

        _draw_styled_table(
            ax,
            headers=["DATE", "REGIME", "ENTERED", "EXITED",
                     "POS", "CASH", "RISK FLAGS", "COST"],
            rows=tbl_rows,
            col_x=[0.002, 0.100, 0.200, 0.380, 0.535, 0.590, 0.655, 0.810],
            col_w=[0.098, 0.100, 0.180, 0.155, 0.055, 0.065, 0.155, 0.188],
            row_h=0.026,
            y_start=0.938,
            col_aligns=["left", "left", "left", "left", "center", "center", "left", "right"],
            row_colors=row_colors_hist,
        )

        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)


# ---------------------------------------------------------------------------
# Main tearsheet
# ---------------------------------------------------------------------------

def generate_tearsheet(
    result,
    output_path: Optional[str] = None,
    prices: Optional[pd.DataFrame] = None,
    daily_report_dir: Optional[str] = None,
) -> Path:
    """
    Generate a professional PDF tearsheet from a BacktestResult.

    Parameters
    ----------
    result            : BacktestResult from SectorRotationBacktest.run()
    output_path       : Full PDF path (auto-generated if None)
    prices            : Raw prices DataFrame (for correlation page)
    daily_report_dir  : Directory containing sr_daily_report_*.json files
                        (defaults to <repo>/trading_signals/)
    """
    from .plots import (
        equity_curve_plot, drawdown_plot, rolling_sharpe_plot,
        sector_weights_heatmap, regime_overlay_plot, annual_returns_bar,
        monthly_returns_heatmap, correlation_matrix_plot,
    )

    # ── Resolve output path ──────────────────────────────────────────────────
    if output_path is None:
        report_cfg = result.config.get("report", {})
        raw_dir = report_cfg.get("output_dir")
        if raw_dir:
            out_dir = Path(raw_dir)
            if not out_dir.is_absolute():
                out_dir = Path(__file__).parent.parent / out_dir
        else:
            out_dir = Path(__file__).parent / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = out_dir / f"sector_rotation_tearsheet_{ts}.pdf"
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Locate latest daily report JSON ─────────────────────────────────────
    daily_report_path = None
    search_dirs = []
    if daily_report_dir:
        search_dirs.append(Path(daily_report_dir))
    # Also check <repo_root>/trading_signals/ relative to this file
    repo_root = Path(__file__).parent.parent.parent
    search_dirs.append(repo_root / "trading_signals")
    search_dirs.append(Path(__file__).parent.parent / "trading_signals")

    for d in search_dirs:
        if d.exists():
            candidates = sorted(d.glob("sr_daily_report_*.json"), key=os.path.getmtime)
            if candidates:
                daily_report_path = candidates[-1]
                logger.info(f"Using daily report: {daily_report_path}")
                break

    logger.info(f"Generating tearsheet → {output_path}")

    daily_ret    = result.daily_returns
    equity       = result.equity_curve
    bench_ret    = result.benchmark_returns
    bench_eq     = result.benchmark_equity
    weights_hist = result.weights_history
    regime_hist  = result.regime_history

    with PdfPages(str(output_path)) as pdf:

        # ── P1: Cover ────────────────────────────────────────────────────────
        _add_title_page(pdf, result.metrics, result.config, equity=equity)

        # ── P2: Equity Curve ─────────────────────────────────────────────────
        fig = equity_curve_plot(equity, bench_eq)
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        # ── P3: Drawdown + Rolling Sharpe ─────────────────────────────────────
        fig, axes = plt.subplots(2, 1, figsize=(14, 9))
        fig.patch.set_facecolor(PALETTE["bg"])
        fig.suptitle("Risk Analysis", fontsize=13, fontweight="bold",
                     color=PALETTE["header_mid"], y=0.995)

        def _compute_dd_pct(r):
            cum = (1 + r).cumprod()
            return (cum / cum.expanding().max() - 1) * 100

        dd = _compute_dd_pct(daily_ret)
        axes[0].fill_between(dd.index, dd.values, 0,
                             color=PALETTE["negative"], alpha=0.35, interpolate=True)
        axes[0].plot(dd.index, dd.values, color=PALETTE["negative"], linewidth=1.2)
        if bench_ret is not None:
            bench_dd = _compute_dd_pct(bench_ret)
            axes[0].plot(bench_dd.index, bench_dd.values, color=PALETTE["benchmark"],
                         linewidth=1.0, linestyle="--", alpha=0.7, label="SPY DD")
            axes[0].legend(fontsize=8)
        axes[0].axhline(0, color=PALETTE["border"], linewidth=0.8)
        axes[0].set_title("Drawdown (%)", fontsize=11, fontweight="bold",
                          color=PALETTE["header_mid"])
        axes[0].set_ylabel("Drawdown (%)", fontsize=9)
        axes[0].yaxis.set_major_formatter(mticker.FormatStrFormatter("%.0f%%"))

        window = 12 * 21
        rv = daily_ret.rolling(window, min_periods=window // 2).std() * np.sqrt(252)
        rr = daily_ret.rolling(window, min_periods=window // 2).mean() * 252
        rs = rr / rv.replace(0, np.nan)
        axes[1].fill_between(rs.index, rs.values, 0,
                             where=(rs >= 0), alpha=0.15, color=PALETTE["ew"], interpolate=True)
        axes[1].fill_between(rs.index, rs.values, 0,
                             where=(rs < 0),  alpha=0.15, color=PALETTE["negative"], interpolate=True)
        axes[1].plot(rs.index, rs.values, color=PALETTE["strategy"], linewidth=1.6)
        axes[1].axhline(0,   color=PALETTE["neutral"],  linewidth=0.9, linestyle="--", alpha=0.5)
        axes[1].axhline(0.5, color=PALETTE["positive"], linewidth=0.8, linestyle=":", alpha=0.6,
                        label="Target = 0.5")
        axes[1].axhline(1.0, color=PALETTE["positive"], linewidth=0.6, linestyle=":", alpha=0.35)
        axes[1].set_title("12-Month Rolling Sharpe Ratio", fontsize=11, fontweight="bold",
                          color=PALETTE["header_mid"])
        axes[1].set_ylabel("Sharpe Ratio", fontsize=9)
        axes[1].legend(fontsize=8)

        for ax_ in axes:
            ax_.set_facecolor(PALETTE["surface"])
            for sp in ax_.spines.values():
                sp.set_edgecolor(PALETTE["border"]); sp.set_linewidth(0.7)
            ax_.spines["top"].set_visible(False)
            ax_.spines["right"].set_visible(False)
            ax_.grid(color=PALETTE["grid"], alpha=0.8, linewidth=0.5)
            ax_.tick_params(labelsize=8)

        fig.tight_layout(rect=[0, 0, 1, 0.99])
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        # ── P4: Sector Weights Heatmap ────────────────────────────────────────
        fig = sector_weights_heatmap(weights_hist)
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        # ── P5: Regime Overlay + Annual Returns ───────────────────────────────
        fig, axes = plt.subplots(2, 1, figsize=(14, 10))
        fig.patch.set_facecolor(PALETTE["bg"])

        norm = equity / equity.iloc[0] * 100
        if not regime_hist.empty:
            daily_reg = regime_hist.reindex(equity.index, method="ffill").fillna("risk_on")
            for state in ["risk_on", "transition_up", "transition_down", "risk_off"]:
                from .plots import REGIME_COLORS
                color = REGIME_COLORS.get(state, "#F5F5F5")
                mask = daily_reg == state
                in_block, bs = False, None
                for dt, is_s in mask.items():
                    if is_s and not in_block:   bs = dt; in_block = True
                    elif not is_s and in_block:
                        axes[0].axvspan(bs, dt, alpha=0.45, color=color,
                                        zorder=1, linewidth=0)
                        in_block = False
                if in_block and bs:
                    axes[0].axvspan(bs, equity.index[-1], alpha=0.45,
                                    color=color, zorder=1, linewidth=0)

        axes[0].fill_between(norm.index, norm.values, 100, alpha=0.07,
                             color=PALETTE["strategy"], interpolate=True)
        strat_line, = axes[0].plot(norm.index, norm.values, color=PALETTE["strategy"],
                                   linewidth=2.2, label="Strategy", zorder=3)
        axes[0].axhline(100, color=PALETTE["border"], linewidth=0.7)
        axes[0].set_title("Equity Curve with Regime Overlay", fontsize=11,
                          fontweight="bold", color=PALETTE["header_mid"])
        axes[0].set_ylabel("Portfolio Value (Base = 100)", fontsize=9)

        from matplotlib.patches import Patch as _Patch
        from .plots import REGIME_LEGEND_COLORS, REGIME_LABELS as _RL
        _regime_legend_colors = {"risk_on": "#4CAF50", "transition_up": "#FFC107",
                                 "transition_down": "#FF7043", "risk_off": "#E53935"}
        regime_leg_patches = [
            _Patch(facecolor=_regime_legend_colors[s], edgecolor="none",
                   alpha=0.85, label=_RL[s])
            for s in ["risk_on", "transition_up", "transition_down", "risk_off"]
        ]
        axes[0].legend(handles=[strat_line] + regime_leg_patches,
                       loc="upper left", fontsize=8, framealpha=0.88)

        annual = daily_ret.resample("YE").apply(lambda x: (1 + x).prod() - 1)
        years  = [str(y.year) for y in annual.index]
        xpos   = np.arange(len(years))
        w_bar  = 0.35
        strat_colors = [PALETTE["strategy"] if v >= 0 else PALETTE["negative"]
                        for v in annual.values]
        axes[1].bar(xpos - w_bar / 2, annual.values * 100, w_bar,
                    color=strat_colors, label="Strategy",
                    alpha=0.90, edgecolor="white", linewidth=0.5)
        if bench_ret is not None:
            ba = bench_ret.resample("YE").apply(lambda x: (1 + x).prod() - 1)
            ba = ba.reindex(annual.index, fill_value=0)
            axes[1].bar(xpos + w_bar / 2, ba.values * 100, w_bar,
                        color=PALETTE["benchmark"], label="SPY",
                        alpha=0.72, edgecolor="white", linewidth=0.5)
        axes[1].set_xticks(xpos)
        axes[1].set_xticklabels(years, fontsize=9)
        axes[1].axhline(0, color=PALETTE["neutral"], linewidth=0.9)
        axes[1].yaxis.set_major_formatter(mticker.FormatStrFormatter("%.0f%%"))
        axes[1].set_title("Annual Returns — Strategy vs Benchmark", fontsize=11,
                          fontweight="bold", color=PALETTE["header_mid"])
        axes[1].legend(fontsize=9)

        for ax_ in axes:
            ax_.set_facecolor(PALETTE["surface"])
            for sp in ax_.spines.values():
                sp.set_edgecolor(PALETTE["border"]); sp.set_linewidth(0.7)
            ax_.spines["top"].set_visible(False)
            ax_.spines["right"].set_visible(False)
            ax_.grid(color=PALETTE["grid"], alpha=0.7, linewidth=0.5)
            ax_.tick_params(labelsize=8)

        fig.tight_layout()
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        # ── P6: Monthly Returns Heatmap ───────────────────────────────────────
        fig = monthly_returns_heatmap(daily_ret)
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        # ── P7: Subperiod + DD Episodes ───────────────────────────────────────
        _add_subperiod_table(pdf, result.subperiod_metrics, result.drawdown_episodes)

        # ── P8: Correlation Matrix ────────────────────────────────────────────
        if prices is not None:
            etf_tickers = result.config.get("universe", {}).get("etfs", [])
            etf_p = prices[[t for t in etf_tickers if t in prices.columns]]
            if not etf_p.empty:
                fig = correlation_matrix_plot(etf_p)
                pdf.savefig(fig, bbox_inches="tight")
                plt.close(fig)

        # ── P9: Latest Signal ─────────────────────────────────────────────────
        _add_latest_signal_page(pdf, result,
                                 daily_report_path=daily_report_path)

        # ── P10: Rebalance History ────────────────────────────────────────────
        _add_rebalance_history_page(pdf, result)

        # PDF metadata
        d = pdf.infodict()
        d["Title"]        = "Sector Rotation Strategy — Performance Tearsheet"
        d["Author"]       = "Someo Park"
        d["Subject"]      = "Institutional Sector ETF Rotation Backtest"
        d["Keywords"]     = "sector rotation, ETF, risk parity, momentum, backtest"
        d["CreationDate"] = datetime.now()

    logger.info(f"Tearsheet saved → {output_path}")
    return output_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    print("Tearsheet module loaded. Run backtest engine first to generate BacktestResult.")
