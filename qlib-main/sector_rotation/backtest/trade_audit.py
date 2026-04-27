"""
Trade Audit Script
==================
逐笔复盘每次调仓：日期 / 触发原因 / Regime / 信号分 / 调仓前后权重 /
换手率 / 风控触发 / 交易成本

用法（在 someopark-test/ 目录下）：
    set -a && source .env && set +a && \
    conda run -n qlib_run --no-capture-output \
    python qlib-main/sector_rotation/backtest/trade_audit.py
"""

from __future__ import annotations

import os
import sys
import logging

import numpy as np
import pandas as pd
from pathlib import Path

# ── path setup ────────────────────────────────────────────────────────────────
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SR_ROOT   = os.path.join(REPO_ROOT, "qlib-main", "sector_rotation")
sys.path.insert(0, os.path.join(REPO_ROOT, "qlib-main"))
sys.path.insert(0, REPO_ROOT)
os.chdir(SR_ROOT)

logging.basicConfig(level=logging.WARNING)   # suppress signal-computation noise

from sector_rotation.backtest.engine    import SectorRotationBacktest
from sector_rotation.portfolio.rebalance import (
    compute_turnover, should_emergency_rebalance, get_monthly_rebalance_dates,
    apply_zscore_threshold_filter, cap_turnover,
)
from sector_rotation.portfolio.risk      import apply_risk_controls
from sector_rotation.portfolio.optimizer import optimize_weights
from sector_rotation.backtest.costs      import compute_transaction_costs
from sector_rotation.data.loader         import load_prices, load_macro_data as load_macro
from sector_rotation.signals.composite   import compute_composite_signals

# ── load config ───────────────────────────────────────────────────────────────
import yaml
with open(os.path.join(SR_ROOT, "config.yaml")) as f:
    CFG = yaml.safe_load(f)

from sector_rotation.data.universe import get_tickers
ETF_TICKERS = CFG["universe"]["etfs"]
BENCH       = CFG["universe"].get("benchmark", "SPY")

# ── load data (reuse engine's load path) ─────────────────────────────────────
print("Loading prices + macro …")
prices = load_prices(
    tickers = ETF_TICKERS + [BENCH],
    start   = CFG["data"].get("price_start", "2017-01-01"),
    end     = CFG["data"].get("price_end"),
    source  = CFG["data"].get("price_source", "yfinance"),
    cache_dir = Path(CFG["data"]["cache_dir"]) if CFG["data"].get("cache_dir") else None,
)
macro = load_macro(
    start   = CFG["data"].get("price_start", "2017-01-01"),
    api_key = os.environ.get("FRED_API_KEY"),
    cache_dir = Path(CFG["data"]["cache_dir"]) if CFG["data"].get("cache_dir") else None,
)

# ── compute signals ───────────────────────────────────────────────────────────
print("Computing signals …")
etf_prices = prices[[t for t in ETF_TICKERS if t in prices.columns]]
sig_cfg    = CFG.get("signals", {})

composite, regime_monthly, _ = compute_composite_signals(
    etf_prices,
    macro,
    weights        = sig_cfg.get("weights"),
    regime_method  = sig_cfg.get("regime", {}).get("method", "rules"),
    value_source   = sig_cfg.get("value_source", "constituents"),
    value_cache_dir= CFG["data"].get("cache_dir"),
    regime_kwargs  = {k:v for k,v in sig_cfg.get("regime",{}).items()
                     if k not in ("method","regime_weights","defensive_sectors","defensive_bonus_risk_off")},
)

from sector_rotation.data.loader import load_returns
daily_ret     = load_returns(prices)
etf_daily_ret = daily_ret[[t for t in ETF_TICKERS if t in daily_ret.columns]]

# ── backtest parameters ───────────────────────────────────────────────────────
bt_cfg   = CFG.get("backtest", {})
port_cfg = CFG.get("portfolio", {})
reb_cfg  = CFG.get("rebalance", {})
risk_cfg = CFG.get("risk", {})

BT_START = bt_cfg.get("start_date", "2018-07-01")
BT_END   = prices.index[-1].strftime("%Y-%m-%d")
CAPITAL  = bt_cfg.get("initial_capital", 1_000_000.0)

rebalance_dates    = get_monthly_rebalance_dates(BT_START, BT_END)
rebalance_date_set = set(rebalance_dates)

# ── replay loop ───────────────────────────────────────────────────────────────
print("Replaying trades …\n")

records = []
current_weights   = pd.Series(dtype=float)
prev_scores       = pd.Series(dtype=float)
portfolio_value   = CAPITAL
portfolio_daily_returns = pd.Series(dtype=float)

all_dates = prices.loc[BT_START:BT_END].index

for dt in all_dates:
    is_scheduled  = dt in rebalance_date_set
    is_emergency  = should_emergency_rebalance(
        macro.loc[:dt] if dt in macro.index else macro,
        current_weights,
        vix_threshold=reb_cfg.get("emergency_derisk_vix", 35.0),
    )

    if is_scheduled or is_emergency:
        avail_scores = composite.loc[:dt].dropna(how="all")
        if avail_scores.empty:
            continue
        latest_scores = avail_scores.iloc[-1]

        # ── regime at this date ───────────────────────────────────────────────
        regime_vals = regime_monthly.loc[:dt]
        regime_now  = str(regime_vals.iloc[-1]) if not regime_vals.empty else "unknown"

        # ── VIX at this date ──────────────────────────────────────────────────
        vix_now = float("nan")
        if "vix" in macro.columns:
            vix_slice = macro.loc[:dt, "vix"].dropna()
            if not vix_slice.empty:
                vix_now = float(vix_slice.iloc[-1])

        # ── Step 1: optimizer → proposed weights ──────────────────────────────
        hist_ret = etf_daily_ret.loc[:dt].iloc[
            -port_cfg.get("cov", {}).get("lookback_days", 252):
        ]
        proposed = optimize_weights(
            scores    = latest_scores,
            returns   = hist_ret,
            method    = port_cfg.get("optimizer", "inv_vol"),
            cov_method= port_cfg.get("cov", {}).get("method", "ledoit_wolf"),
            max_weight= port_cfg.get("constraints", {}).get("max_weight", 0.40),
            min_weight= port_cfg.get("constraints", {}).get("min_weight", 0.00),
            top_n     = port_cfg.get("top_n_sectors", 4),
            min_score = port_cfg.get("min_zscore", -0.5),
        )

        # ── Step 2: z-score threshold filter ─────────────────────────────────
        thresh = reb_cfg.get("zscore_change_threshold", 0.5)
        filtered, rebalanced_sectors, held_sectors = apply_zscore_threshold_filter(
            new_scores  = latest_scores,
            prev_scores = prev_scores,
            new_weights = proposed,
            prev_weights= current_weights,
            threshold   = thresh,
        )

        # ── Step 3: turnover cap ─────────────────────────────────────────────
        max_to = reb_cfg.get("max_monthly_turnover", 0.80)
        filtered = cap_turnover(filtered, current_weights, max_to)

        # ── Step 4: risk controls ─────────────────────────────────────────────
        macro_slice = macro.loc[:dt] if dt in macro.index else macro
        adj_weights, cash_pct, flags = apply_risk_controls(
            weights            = filtered,
            portfolio_returns  = portfolio_daily_returns.iloc[-252:] if len(portfolio_daily_returns) > 0 else pd.Series(dtype=float),
            macro              = macro_slice,
            equity_curve       = None,
            vol_target         = risk_cfg.get("vol_scaling", {}).get("target_vol_annual", 0.12),
            vol_scaling_enabled= risk_cfg.get("vol_scaling", {}).get("enabled", True),
            vix_emergency_threshold = reb_cfg.get("emergency_derisk_vix", 35.0),
            emergency_cash_pct = reb_cfg.get("emergency_cash_pct", 0.50),
            dd_halve_threshold = risk_cfg.get("drawdown", {}).get("cumulative_dd_halve", -0.15),
            max_weight         = port_cfg.get("constraints", {}).get("max_weight", 0.40),
        )

        # ── Step 5: costs ─────────────────────────────────────────────────────
        cost_result = compute_transaction_costs(current_weights, adj_weights, portfolio_value)
        portfolio_value -= cost_result["total_cost_usd"]

        # ── compute turnover ─────────────────────────────────────────────────
        turnover = compute_turnover(adj_weights, current_weights)

        # ── build record ──────────────────────────────────────────────────────
        row = {
            "date"           : dt.date(),
            "trigger"        : "EMERGENCY_VIX" if is_emergency and not is_scheduled
                               else ("SCHEDULED+EMERG" if is_emergency and is_scheduled
                               else "SCHEDULED"),
            "regime"         : regime_now,
            "vix"            : round(vix_now, 1),
            "vol_scaling"    : flags.vol_scaling_triggered,
            "vix_emergency"  : flags.vix_emergency_triggered,
            "dd_circuit"     : flags.dd_circuit_triggered,
            "cash_pct"       : round(cash_pct, 3),
            "realized_vol"   : round(flags.realized_vol_annual, 3) if not np.isnan(flags.realized_vol_annual) else None,
            "turnover"       : round(turnover, 3),
            "cost_usd"       : round(cost_result["total_cost_usd"], 2),
            "held_sectors"   : ",".join(held_sectors) if held_sectors else "—",
        }

        # per-sector scores and weights before/after
        for t in ETF_TICKERS:
            row[f"score_{t}"]      = round(float(latest_scores.get(t, float("nan"))), 3)
            row[f"prev_w_{t}"]     = round(float(current_weights.get(t, 0.0)), 3)
            row[f"new_w_{t}"]      = round(float(adj_weights.get(t, 0.0)), 3)
            row[f"delta_w_{t}"]    = round(float(adj_weights.get(t, 0.0)) - float(current_weights.get(t, 0.0)), 3)

        records.append(row)
        current_weights = adj_weights.copy()
        prev_scores     = latest_scores.copy()

    # ── daily P&L ──────────────────────────────────────────────────────────────
    if dt in etf_daily_ret.index:
        port_ret = float(
            (current_weights * etf_daily_ret.loc[dt].reindex(current_weights.index, fill_value=0.0)).sum()
        )
    else:
        port_ret = 0.0

    portfolio_value *= (1 + port_ret)
    _new = pd.Series([port_ret], index=[dt])
    portfolio_daily_returns = pd.concat(
        [s for s in [portfolio_daily_returns, _new] if not s.empty]
    )

# ── output ────────────────────────────────────────────────────────────────────
df = pd.DataFrame(records)

# ── 1. Human-readable summary ─────────────────────────────────────────────────
SECTORS = ETF_TICKERS
SECTOR_LABEL = {
    "XLE":"Energy","XLB":"Matl","XLI":"Indus","XLY":"ConsDisc","XLP":"ConsStp",
    "XLV":"Health","XLF":"Fin","XLK":"Tech","XLC":"Comm","XLU":"Util","XLRE":"RE",
}
SCORE_COLS  = [f"score_{t}"  for t in SECTORS]
NEW_W_COLS  = [f"new_w_{t}"  for t in SECTORS]
DELTA_COLS  = [f"delta_w_{t}" for t in SECTORS]

print("=" * 110)
print(f"{'DATE':<12} {'TRIGGER':<18} {'REGIME':<16} {'VIX':>5}  "
      f"{'TO':>5} {'CASH':>5} {'VOL':>4} {'EMRG':>4} {'DD':>4}  "
      f"{'COST$':>7}  TOP-4 SCORES → WEIGHTS")
print("=" * 110)

for _, r in df.iterrows():
    # top-4 by score
    scores = {t: r[f"score_{t}"] for t in SECTORS if not np.isnan(r[f"score_{t}"])}
    top4   = sorted(scores, key=scores.get, reverse=True)[:4]
    score_str = "  ".join(
        f"{SECTOR_LABEL[t]}:{scores[t]:+.2f}→{r[f'new_w_{t}']:.0%}" for t in top4
    )

    flags_str = (
        ("V" if r["vol_scaling"] else " ") +
        ("E" if r["vix_emergency"] else " ") +
        ("D" if r["dd_circuit"]   else " ")
    )

    print(f"{str(r['date']):<12} {r['trigger']:<18} {r['regime']:<16} {r['vix']:>5.1f}  "
          f"{r['turnover']:>5.1%} {r['cash_pct']:>5.1%} "
          f"{flags_str}  "
          f"{r['cost_usd']:>7,.0f}  {score_str}")
    if r["held_sectors"] != "—":
        print(f"             ↳ z-score threshold held: {r['held_sectors']}")

print("=" * 110)
print(f"Total rebalances: {len(df)} "
      f"(scheduled={len(df[df.trigger=='SCHEDULED'])}, "
      f"emergency={len(df[df.trigger.str.contains('EMERG')])}, "
      f"sched+emerg={len(df[df.trigger=='SCHEDULED+EMERG'])})")
print(f"Total turnover  : {df['turnover'].sum():.1%}")
print(f"Total costs     : ${df['cost_usd'].sum():,.0f}")
print(f"Vol scaling     : {df['vol_scaling'].sum()} rebalances")
print(f"VIX emergency   : {df['vix_emergency'].sum()} rebalances")
print(f"DD circuit      : {df['dd_circuit'].sum()} rebalances")

# ── 2. Save to Excel ──────────────────────────────────────────────────────────
out_path = os.path.join(REPO_ROOT, "historical_runs", "trade_audit.xlsx")
os.makedirs(os.path.dirname(out_path), exist_ok=True)

# Summary sheet: compact
summary_cols = (
    ["date","trigger","regime","vix","turnover","cash_pct",
     "vol_scaling","vix_emergency","dd_circuit","realized_vol","cost_usd","held_sectors"]
    + SCORE_COLS + NEW_W_COLS + DELTA_COLS
)
csv_path = out_path.replace(".xlsx", ".csv")
df[summary_cols].to_csv(csv_path, index=False)
print(f"\nSaved → {csv_path}")
