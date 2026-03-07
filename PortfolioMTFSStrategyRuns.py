"""
PortfolioMTFSStrategyRuns.py — JSON-driven backtest runner for MTFS (Momentum Trend Following).

Reads a run config JSON from run_configs/ and executes each run entry using PortfolioMTFSRun.
Data is pre-loaded per unique symbol set from Parquet cache (no redundant API calls).
Results are collected into a summary CSV.

Usage:
    python PortfolioMTFSStrategyRuns.py                                    # uses latest MTFS JSON
    python PortfolioMTFSStrategyRuns.py run_configs/mtfs_runs_20260307.json
"""

import os
import sys
import json
import glob
import logging
import pandas as pd
from datetime import datetime, timedelta

import PortfolioMTFSRun as PortfolioRun

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# ~~~~~~~~~~~~~~~~~~~~~~ NAMED PARAMETER SETS (MTFS) ~~~~~~~~~~~~~~~~~~~~~~
# Referenced by name from the JSON config ("param_set": "default" etc.)

PARAM_SETS = {

    # ════════════════════════════════════════════════════════════════════════
    # GROUP A — BASELINE & SCORING VARIANTS
    # 核心动量评分方式和窗口权重的变化
    # ════════════════════════════════════════════════════════════════════════

    # A1: 默认基准。VAMS评分, 短期偏重, 快速SMA, 10天换仓, 严格风控
    'default': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 10,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # A2: 原始动量评分（不做波动率调整）。测试VAMS vs raw momentum的差异
    'raw_momentum': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': False,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 10,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # A3: 短期动量偏重。极端短期权重，最快响应
    # 捕捉近期强势股，换手更频繁
    'short_term_tilt': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.30, 0.25, 0.20, 0.10, 0.10, 0.05],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 10, 'sma_long': 30,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.4,
        'reversal_sma_lookback': 2,
        'momentum_decay_short_window': 3,
        'momentum_decay_long_window': 15,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 30,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 7,
        'cooling_off_period': 2,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 5,
        'mean_back': 15, 'std_back': 15, 'v_back': 15,
    },

    # A4: 长期动量偏重。提高120/150天窗口权重
    # 跟踪长期趋势，更少换手，更平滑
    'long_term_tilt': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.10, 0.10, 0.15, 0.20, 0.25, 0.20],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 30, 'sma_long': 80,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 5,
        'momentum_decay_short_window': 10,
        'momentum_decay_long_window': 60,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 50,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 2.0,
        'max_holding_period': 21,
        'cooling_off_period': 5,
        'pair_stop_loss_pct': 0.05,
        'rebalance_frequency': 21,
        'mean_back': 30, 'std_back': 30, 'v_back': 30,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP B — TREND CONFIRMATION VARIANTS
    # 测试趋势确认过滤器的影响
    # ════════════════════════════════════════════════════════════════════════

    # B1: 无趋势确认。纯动量得分驱动，不需SMA对齐
    'no_trend_filter': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': False,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 10,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # B2: 快速SMA确认。SMA 10/30（更快响应趋势变化）
    'fast_trend_filter': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 10, 'sma_long': 30,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 2,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 10,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP C — RISK MANAGEMENT VARIANTS
    # 止损、波动率缩放、冷却期等
    # ════════════════════════════════════════════════════════════════════════

    # C1: 激进风控。宽止损 + 高杠杆 + 快重入
    'aggressive': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': False,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.4,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': False,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.15,
        'vol_scale_window': 30,
        'max_vol_scale_factor': 2.5,
        'crash_vol_percentile': 0.90,
        'crash_scale_factor': 0.30,
        'amplifier': 3,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 2.0,
        'max_holding_period': 15,
        'cooling_off_period': 1,
        'pair_stop_loss_pct': 0.05,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # C2: 保守风控。紧止损 + 低杠杆 + 长冷却期
    'conservative': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.6,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.08,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 1.0,
        'crash_vol_percentile': 0.80,
        'crash_scale_factor': 0.15,
        'amplifier': 1,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.2,
        'max_holding_period': 7,
        'cooling_off_period': 5,
        'pair_stop_loss_pct': 0.02,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # C3: 无杠杆基准。amplifier=1, 测试纯信号质量
    'no_leverage': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 1.0,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 1,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 10,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP D — REBALANCING FREQUENCY VARIANTS
    # ════════════════════════════════════════════════════════════════════════

    # D1: 超高频换仓 (每5天)
    'fast_rebalance': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.25, 0.25, 0.20, 0.15, 0.10, 0.05],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 10, 'sma_long': 30,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 2,
        'momentum_decay_short_window': 3,
        'momentum_decay_long_window': 15,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 30,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 7,
        'cooling_off_period': 2,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 5,
        'mean_back': 15, 'std_back': 15, 'v_back': 15,
    },

    # D2: 中频换仓 (每15天)
    'slow_rebalance': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.15, 0.15, 0.20, 0.20, 0.15, 0.15],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 60,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 5,
        'momentum_decay_short_window': 10,
        'momentum_decay_long_window': 60,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 50,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 2.0,
        'max_holding_period': 15,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.05,
        'rebalance_frequency': 15,
        'mean_back': 25, 'std_back': 25, 'v_back': 25,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP E — HEDGE METHOD VARIANTS
    # ════════════════════════════════════════════════════════════════════════

    # E1: Beta-neutral对冲
    'beta_neutral': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'beta_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 10,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # E2: Kalman filter对冲（和MRPT相同方法）
    'kalman_hedge': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'kalman',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 10,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP F — REVERSAL PROTECTION VARIANTS
    # 系统变化趋势反转检测的敏感度
    # ════════════════════════════════════════════════════════════════════════

    # F1: 无反转保护。不检测动量衰减和SMA交叉
    # 测试反转保护的净价值
    'no_reversal_protection': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': False,
        'exit_on_momentum_decay': False,
        'target_annual_vol': 0.10,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 10,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # F2: 高敏感度反转保护。快速检测 + 低衰减阈值
    'sensitive_reversal': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.6,
        'reversal_sma_lookback': 2,
        'momentum_decay_short_window': 3,
        'momentum_decay_long_window': 15,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 30,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.2,
        'max_holding_period': 7,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.02,
        'rebalance_frequency': 7,
        'mean_back': 15, 'std_back': 15, 'v_back': 15,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP G — COMBINED BEST-GUESS BLENDS
    # ════════════════════════════════════════════════════════════════════════

    # G1: 稳健均衡型。适度短期偏重 + VAMS + 适度杠杆
    'balanced': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.15, 0.20, 0.20, 0.20, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 10,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.04,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # G2: 快速响应 + 严格风控型
    'fast_strict': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.25, 0.25, 0.20, 0.15, 0.10, 0.05],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 10, 'sma_long': 30,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.6,
        'reversal_sma_lookback': 2,
        'momentum_decay_short_window': 3,
        'momentum_decay_long_window': 15,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.08,
        'vol_scale_window': 30,
        'max_vol_scale_factor': 1.2,
        'crash_vol_percentile': 0.80,
        'crash_scale_factor': 0.15,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.2,
        'max_holding_period': 7,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.02,
        'rebalance_frequency': 5,
        'mean_back': 15, 'std_back': 15, 'v_back': 15,
    },

    # G3: 短期趋势 + 高杠杆型
    'trend_leverage': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.15, 0.20, 0.20, 0.20, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.12,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 2.0,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 3,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 10,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.04,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # G4: 无skip-month。测试skip-month规则的影响
    'no_skip_month': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 0,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 10,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP H — ENTRY THRESHOLD VARIANTS
    # 测试正动量阈值（滤掉弱信号）的效果
    # 现有19组 entry_momentum_threshold 全部为0，这是未探索的关键维度
    # ════════════════════════════════════════════════════════════════════════

    # H1: 弱过滤阈值。只允许moderate正动量差再入场
    # 减少低质量交易，但不会过度限制信号
    'entry_threshold_weak': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.02,   # KEY: 过滤掉差值<0.02的弱信号
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 10,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # H2: 强过滤阈值。只允许强动量差入场，交易次数更少但质量更高
    'entry_threshold_strong': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.05,   # KEY: 只做最强的动量分歧信号
        'exit_momentum_decay_threshold': 0.45,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 14,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.04,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP I — VOL-WEIGHTED SIZING
    # 测试波动率加权仓位（现有19组全部 use_vol_weighted_sizing=False）
    # ════════════════════════════════════════════════════════════════════════

    # I1: 波动率加权仓位。低波动品种分配更多资金，动态平衡
    'vol_weighted_sizing': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': True,    # KEY: 启用波动率加权仓位
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 10,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # I2: 波动率加权仓位 + 高杠杆。测试vol-weighted在放大leverage时的风控效果
    # vol-weighted能否有效控制tail risk，让高sharpe对的仓位更大
    'vol_weighted_aggressive': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': False,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.45,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': False,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.15,
        'vol_scale_window': 30,
        'max_vol_scale_factor': 2.0,
        'crash_vol_percentile': 0.90,
        'crash_scale_factor': 0.25,
        'amplifier': 3,                         # KEY: 高杠杆
        'use_vol_weighted_sizing': True,         # KEY: 但用vol-weighted控制单对风险
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.8,
        'max_holding_period': 12,
        'cooling_off_period': 2,
        'pair_stop_loss_pct': 0.04,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP J — CALENDAR-ALIGNED WINDOWS
    # 用更贴近实际交易周期的窗口（月/季度/半年/年）替代原有窗口
    # 原有 [6,12,30,60,120,150] 是任意天数，这里用自然日历周期
    # ════════════════════════════════════════════════════════════════════════

    # J1: 月度对齐窗口。[21,42,63,126,189,252] ≈ 1/2/3/6/9/12个月
    # 贴合机构投资者的月度动量再平衡周期
    'monthly_aligned_windows': {
        'momentum_windows': [21, 42, 63, 126, 189, 252],   # KEY: 月度对齐
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 21, 'sma_long': 63,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 21,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 42,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 21,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 21,
        'mean_back': 21, 'std_back': 21, 'v_back': 21,
    },

    # J2: 短期日历窗口。[5,10,20,40,60,90] ≈ 周/双周/月/季度
    # 捕捉更高频的短周期动量，适合高换手股票对
    'weekly_aligned_windows': {
        'momentum_windows': [5, 10, 20, 40, 60, 90],       # KEY: 周/月对齐
        'momentum_weights': [0.25, 0.25, 0.20, 0.15, 0.10, 0.05],
        'skip_days': 5,     # KEY: skip_days=5（只跳过1周，而非1个月）
        'use_vams': True,
        'sma_short': 10, 'sma_long': 40,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.45,
        'reversal_sma_lookback': 2,
        'momentum_decay_short_window': 3,
        'momentum_decay_long_window': 10,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 20,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 10,
        'cooling_off_period': 2,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 5,
        'mean_back': 10, 'std_back': 10, 'v_back': 10,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP K — CRASH PROTECTION VARIANTS
    # 现有19组的crash参数变化不大，这里测试极端崩溃防护配置
    # ════════════════════════════════════════════════════════════════════════

    # K1: 极强崩溃保护。低percentile触发 + 极小scale，市场压力下几乎空仓
    'crash_defensive': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.55,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.75,   # KEY: 更早触发（低分位数）
        'crash_scale_factor': 0.10,     # KEY: 崩溃时缩减至10%
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 10,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # K2: 放宽崩溃保护。允许更高vol才触发，崩溃时仍保留大部分仓位
    # 适合认为短期波动不影响动量信号的场景
    'crash_tolerant': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.20, 0.20, 0.20, 0.15, 0.15, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 20, 'sma_long': 50,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 3,
        'momentum_decay_short_window': 5,
        'momentum_decay_long_window': 30,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 40,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.95,   # KEY: 只有极端vol才触发
        'crash_scale_factor': 0.40,     # KEY: 崩溃时还保留40%
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 10,
        'cooling_off_period': 3,
        'pair_stop_loss_pct': 0.03,
        'rebalance_frequency': 10,
        'mean_back': 20, 'std_back': 20, 'v_back': 20,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP L — CROSS-DIMENSION COMBINATIONS
    # 跨维度有意义组合，测试参数协同效应
    # ════════════════════════════════════════════════════════════════════════

    # L1: 长期Kalman对冲。长期动量权重 + Kalman hedge（最稳定的对冲比率）
    # 适合长期趋势稳定的股票对，hedge ratio随时间慢慢漂移
    'long_term_kalman': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.05, 0.10, 0.15, 0.25, 0.25, 0.20],   # KEY: 极长期偏重
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 30, 'sma_long': 80,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.5,
        'reversal_sma_lookback': 5,
        'momentum_decay_short_window': 10,
        'momentum_decay_long_window': 60,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 50,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'kalman',           # KEY: Kalman hedge for long-term drift
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 2.0,
        'max_holding_period': 30,
        'cooling_off_period': 5,
        'pair_stop_loss_pct': 0.05,
        'rebalance_frequency': 21,
        'mean_back': 30, 'std_back': 30, 'v_back': 30,
    },

    # L2: 短期Beta-neutral。短期动量权重 + beta-neutral hedge
    # beta-neutral消除市场beta暴露，配合短期信号做纯相对强弱
    'short_term_beta_neutral': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.35, 0.25, 0.20, 0.10, 0.05, 0.05],   # KEY: 极短期偏重
        'skip_days': 10,    # KEY: 较短skip，减少延迟
        'use_vams': True,
        'sma_short': 10, 'sma_long': 30,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.4,
        'reversal_sma_lookback': 2,
        'momentum_decay_short_window': 3,
        'momentum_decay_long_window': 15,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.10,
        'vol_scale_window': 20,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'beta_neutral',     # KEY: beta-neutral for pure relative momentum
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 1.2,
        'max_holding_period': 7,
        'cooling_off_period': 2,
        'pair_stop_loss_pct': 0.025,
        'rebalance_frequency': 5,
        'mean_back': 15, 'std_back': 15, 'v_back': 15,
    },

    # L3: 让利润奔跑型。宽止损 + 长持仓 + 慢反转保护 + 低波动目标
    # 减少过早离场，让强势趋势充分发展
    'let_profits_run': {
        'momentum_windows': [6, 12, 30, 60, 120, 150],
        'momentum_weights': [0.10, 0.15, 0.20, 0.25, 0.20, 0.10],
        'skip_days': 21,
        'use_vams': True,
        'sma_short': 25, 'sma_long': 75,
        'require_trend_confirmation': True,
        'entry_momentum_threshold': 0.0,
        'exit_momentum_decay_threshold': 0.35,  # KEY: 低阈值，不轻易判断衰减
        'reversal_sma_lookback': 5,              # KEY: 慢反转检测
        'momentum_decay_short_window': 10,       # KEY: 更长短窗口
        'momentum_decay_long_window': 60,
        'exit_on_reversal': True,
        'exit_on_momentum_decay': True,
        'target_annual_vol': 0.08,
        'vol_scale_window': 50,
        'max_vol_scale_factor': 1.5,
        'crash_vol_percentile': 0.85,
        'crash_scale_factor': 0.20,
        'amplifier': 2,
        'use_vol_weighted_sizing': False,
        'hedge_method': 'dollar_neutral',
        'hedge_lag': 1,
        'volatility_stop_loss_multiplier': 2.5,  # KEY: 宽波动率止损
        'max_holding_period': 30,                # KEY: 允许持仓30天
        'cooling_off_period': 5,
        'pair_stop_loss_pct': 0.06,              # KEY: 宽百分比止损
        'rebalance_frequency': 15,
        'mean_back': 25, 'std_back': 25, 'v_back': 25,
    },
}


# ~~~~~~~~~~~~~~~~~~~~~~ CONFIG LOADING ~~~~~~~~~~~~~~~~~~~~~~

def find_latest_config(config_dir):
    """Return path to the most recently modified MTFS JSON in config_dir."""
    pattern = os.path.join(config_dir, 'mtfs_*.json')
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not files:
        # Fall back to any JSON
        pattern = os.path.join(config_dir, '*.json')
        files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not files:
        raise FileNotFoundError(f"No JSON config files found in {config_dir}")
    return files[0]


def _resolve_param_set(param_set_ref, label):
    """Resolve a param_set reference (string name or inline dict) to a params dict."""
    if isinstance(param_set_ref, str):
        if param_set_ref not in PARAM_SETS:
            raise ValueError(
                f"Run '{label}': unknown param_set '{param_set_ref}'. "
                f"Available: {list(PARAM_SETS.keys())}"
            )
        return PARAM_SETS[param_set_ref], param_set_ref
    else:
        return param_set_ref, 'custom'


def load_config(config_path):
    """Load and validate a run config JSON. Returns (start_date, end_date, trade_start_date, runs)."""
    with open(config_path, 'r') as f:
        cfg = json.load(f)

    start_date = cfg.get('start_date', '2024-12-01')

    raw_end = cfg.get('end_date', 'auto_minus_30d')
    if raw_end.startswith('auto_minus_') and raw_end[len('auto_minus_'):].rstrip('d').isdigit():
        days = int(raw_end[len('auto_minus_'):].rstrip('d'))
        end_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    elif raw_end == 'auto':
        end_date = datetime.now().strftime('%Y-%m-%d')
    else:
        end_date = raw_end

    raw_tsd = cfg.get('trade_start_date')
    if raw_tsd and raw_tsd.startswith('auto_minus_') and raw_tsd[len('auto_minus_'):].rstrip('d').isdigit():
        days = int(raw_tsd[len('auto_minus_'):].rstrip('d'))
        trade_start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    else:
        trade_start_date = raw_tsd

    runs = cfg.get('runs', [])
    if not runs:
        raise ValueError(f"No runs defined in {config_path}")

    resolved_runs = []
    for i, run in enumerate(runs):
        label = run.get('label', f'run_{i}')
        raw_pairs = run.get('pairs')
        if not raw_pairs:
            raise ValueError(f"Run '{label}' has no pairs defined")

        run_param_set_ref = run.get('param_set', 'default')
        params, param_set_name = _resolve_param_set(run_param_set_ref, label)

        pairs = []
        pair_params = {}
        for entry in raw_pairs:
            s1, s2 = entry[0], entry[1]
            pairs.append([s1, s2])
            if len(entry) >= 3:
                per_pair_ps = entry[2]
                per_pair_dict, _ = _resolve_param_set(per_pair_ps, f"{label}/{s1}/{s2}")
                pair_params[f"{s1}/{s2}"] = per_pair_dict

        resolved_runs.append({
            'label': label,
            'pairs': pairs,
            'params': params,
            'param_set_name': param_set_name,
            'pair_params': pair_params,
        })

    return start_date, end_date, trade_start_date, resolved_runs


# ~~~~~~~~~~~~~~~~~~~~~~ HELPERS ~~~~~~~~~~~~~~~~~~~~~~

def make_pairs_label(pairs):
    return '_'.join(f"{p[0]}-{p[1]}" for p in pairs)


# ~~~~~~~~~~~~~~~~~~~~~~ MAIN ~~~~~~~~~~~~~~~~~~~~~~

def run_from_config(config_path):
    """Execute all runs defined in a JSON config file."""
    log.info(f"MTFS: Loading run config: {config_path}")
    start_date, end_date, trade_start_date, runs = load_config(config_path)
    log.info(f"Date range: {start_date} → {end_date}" +
             (f"  (trading from {trade_start_date})" if trade_start_date else ""))
    log.info(f"Total runs: {len(runs)}")

    base_dir = os.path.dirname(os.path.abspath(__file__))

    # Pre-load data once per unique symbol set
    data_cache = {}
    for run in runs:
        symbols = sorted(set(sym for pair in run['pairs'] for sym in pair))
        sym_key = tuple(symbols)
        if sym_key not in data_cache:
            log.info(f"Pre-loading {len(symbols)} symbols from Parquet: {symbols}")
            data_cache[sym_key] = PortfolioRun.load_historical_data(
                start_date, end_date, list(symbols)
            )

    # Execute each run
    all_results = []
    for run_idx, run in enumerate(runs, start=1):
        label = run['label']
        pairs = run['pairs']
        params = run['params']
        param_set_name = run['param_set_name']
        pair_params = run.get('pair_params', {})
        pairs_label = make_pairs_label(pairs)

        symbols = sorted(set(sym for pair in pairs for sym in pair))
        sym_key = tuple(symbols)
        historical_data = data_cache[sym_key]

        run_label = f"{label}_{param_set_name}"

        log.info(f"\n{'='*60}")
        log.info(f"Run {run_idx}/{len(runs)}: label={label}  param_set={param_set_name}")
        log.info(f"  pairs: {pairs_label}")
        if pair_params:
            log.info(f"  per-pair overrides: {list(pair_params.keys())}")
        log.info(f"{'='*60}")

        try:
            result = PortfolioRun.main(config={
                'pairs': pairs,
                'params': params,
                'pair_params': pair_params,
                'run_label': run_label,
                'output_dir': base_dir,
                'historical_data': historical_data,
                'start_date': start_date,
                'end_date': end_date,
                'trade_start_date': trade_start_date,
            })

            if result:
                row = {
                    'run_idx': run_idx,
                    'run_name': result['run_name'],
                    'label': label,
                    'param_set': param_set_name,
                    'pairs': pairs_label,
                    'final_equity': result['final_equity'],
                    'acc_pnl': result['acc_pnl'],
                    'sharpe_ratio': result['sharpe_ratio'],
                    'max_drawdown_dollar': result['max_drawdown_dollar'],
                    'max_drawdown_pct': result['max_drawdown_pct'],
                    'trading_days_pct': result['trading_days_pct'],
                    'output_file': result['output_file'],
                }
                row.update(params)
                all_results.append(row)
                if result['sharpe_ratio']:
                    log.info(f"  -> Equity={result['final_equity']:.2f}  "
                             f"PnL={result['acc_pnl']:.2f}  "
                             f"Sharpe={result['sharpe_ratio']:.4f}")
                else:
                    log.info(f"  -> Equity={result['final_equity']:.2f}  Sharpe=N/A")
            else:
                log.warning(f"  -> Run returned no results")

        except Exception as e:
            log.error(f"  -> Run failed: {e}")
            import traceback
            log.error(traceback.format_exc())

    # Save summary
    if all_results:
        summary_df = pd.DataFrame(all_results)
        summary_df = summary_df.sort_values('sharpe_ratio', ascending=False, na_position='last')

        os.makedirs(os.path.join(base_dir, 'historical_runs'), exist_ok=True)
        summary_file = os.path.join(
            base_dir, 'historical_runs',
            f'mtfs_strategy_summary_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
        )
        summary_df.to_csv(summary_file, index=False)

        log.info(f"\n{'='*60}")
        log.info(f"Complete: {len(all_results)}/{len(runs)} successful runs")
        log.info(f"Summary saved to: {summary_file}")
        log.info(f"\nTop 5 by Sharpe Ratio:")
        for _, row in summary_df.head(5).iterrows():
            log.info(f"  Sharpe={row['sharpe_ratio']:.4f}  Equity={row['final_equity']:.0f}  "
                     f"PnL={row['acc_pnl']:.0f}  DD={row['max_drawdown_pct']:.2%}  "
                     f"| {row['label']}  param_set={row['param_set']}")
    else:
        log.warning("No successful runs to summarize.")


if __name__ == "__main__":
    config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'run_configs')

    if len(sys.argv) > 1:
        config_path = sys.argv[1]
    else:
        config_path = find_latest_config(config_dir)
        log.info(f"No config specified — using latest: {os.path.basename(config_path)}")

    run_from_config(config_path)
