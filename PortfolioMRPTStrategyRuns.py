"""
PortfolioStrategyRuns.py — JSON-driven backtest runner for PortfolioRun.

Reads a run config JSON from run_configs/ and executes each run entry.
Data is pre-loaded per unique symbol set from Parquet cache (no redundant API calls).
Results are collected into a summary CSV.

Usage:
    python PortfolioStrategyRuns.py                        # uses latest JSON in run_configs/
    python PortfolioStrategyRuns.py run_configs/runs_20260304_000000.json
"""

import os
import sys
import json
import glob
import logging
import pandas as pd
from datetime import datetime, timedelta

import PortfolioMRPTRun as PortfolioRun

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# ~~~~~~~~~~~~~~~~~~~~~~ NAMED PARAMETER SETS ~~~~~~~~~~~~~~~~~~~~~~
# Referenced by name from the JSON config ("param_set": "default" etc.)

PARAM_SETS = {

    # ════════════════════════════════════════════════════════════════════════
    # GROUP A — BASELINE & LEVERAGE VARIANTS
    # 固定中等窗口 (z=36, v=32)，系统变化杠杆和止损宽度，测试杠杆的边际贡献
    # ════════════════════════════════════════════════════════════════════════

    # A1: 系统默认基准。均衡的回望/入场/杠杆/止损组合，其余参数集的参照点
    'default': {
        'z_back': 36, 'v_back': 32,
        'base_entry_z': 0.75, 'base_exit_z': 0.0,
        'entry_volatility_factor': 2.25, 'exit_volatility_factor': 0.75,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 12, 'cooling_off_period': 2,
    },

    # A2: 无杠杆基准。amplifier=1，测试去掉杠杆后纯信号质量
    # 同等止损宽度下，杠杆对 Sharpe 的净贡献是正是负？
    'no_leverage': {
        'z_back': 36, 'v_back': 32,
        'base_entry_z': 0.75, 'base_exit_z': 0.0,
        'entry_volatility_factor': 2.25, 'exit_volatility_factor': 0.75,
        'amplifier': 1, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 12, 'cooling_off_period': 2,
    },

    # A3: 高杠杆。amplifier=3，宽止损 (×2.5) 防止被过早震荡出局
    # 高杠杆需要宽止损，否则频繁止损会侵蚀收益
    'high_leverage': {
        'z_back': 36, 'v_back': 32,
        'base_entry_z': 0.75, 'base_exit_z': 0.0,
        'entry_volatility_factor': 2.25, 'exit_volatility_factor': 0.75,
        'amplifier': 3, 'volatility_stop_loss_multiplier': 2.5,
        'max_holding_period': 15, 'cooling_off_period': 2,
    },

    # A4: 紧止损基准。stop multiplier=1.5，快速切损，测试严格风控的代价与收益
    # 保持默认其他参数，单独验证 stop multiplier 对 max drawdown 的影响
    'tight_stop': {
        'z_back': 36, 'v_back': 32,
        'base_entry_z': 0.75, 'base_exit_z': 0.0,
        'entry_volatility_factor': 2.25, 'exit_volatility_factor': 0.75,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 12, 'cooling_off_period': 2,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP B — SIGNAL SPEED VARIANTS
    # 系统变化 z_back / v_back，测试信号响应速度的影响
    # ════════════════════════════════════════════════════════════════════════

    # B1: 快速信号。短窗口 (z=20, v=20)，迅速响应价差回归
    # 短持仓 (5天) 匹配快信号，避免信号腐烂后仍持仓
    'fast_signal': {
        'z_back': 20, 'v_back': 20,
        'base_entry_z': 0.75, 'base_exit_z': 0.0,
        'entry_volatility_factor': 2.0, 'exit_volatility_factor': 0.75,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 5, 'cooling_off_period': 1,
    },

    # B2: 慢速信号。长窗口 (z=50, v=50)，只对充分确立的偏离入场
    # 长持仓 (20天) 等待完全回归，冷静期 2 天避免立即反向开仓
    'slow_signal': {
        'z_back': 50, 'v_back': 50,
        'base_entry_z': 1.0, 'base_exit_z': 0.25,
        'entry_volatility_factor': 2.5, 'exit_volatility_factor': 0.75,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 20, 'cooling_off_period': 2,
    },

    # B3: 错配窗口 A — 长 z / 短 v
    # z_back=50 (稳定z-score信号) + v_back=20 (快速波动率响应)
    # 入场信号滞后但稳健，动态阈值对波动率变化反应快
    'long_z_short_v': {
        'z_back': 50, 'v_back': 20,
        'base_entry_z': 0.75, 'base_exit_z': 0.0,
        'entry_volatility_factor': 2.5, 'exit_volatility_factor': 0.75,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 15, 'cooling_off_period': 2,
    },

    # B4: 错配窗口 B — 短 z / 长 v
    # z_back=20 (敏感信号) + v_back=50 (稳定波动率基准)
    # 快速入场信号被稳定的波动率门槛过滤，减少噪声信号
    'short_z_long_v': {
        'z_back': 20, 'v_back': 50,
        'base_entry_z': 0.75, 'base_exit_z': 0.0,
        'entry_volatility_factor': 2.5, 'exit_volatility_factor': 0.75,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 15, 'cooling_off_period': 2,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP C — ENTRY THRESHOLD VARIANTS
    # 系统变化 base_entry_z 和 entry_volatility_factor，测试入场门槛设计
    # ════════════════════════════════════════════════════════════════════════

    # C1: 低门槛高频入场。base_entry_z=0.5，entry_factor=1.5
    # 入场容易，交易次数多，平均每笔利润小；测试高频次是否带来更高总收益
    'low_entry': {
        'z_back': 36, 'v_back': 32,
        'base_entry_z': 0.5, 'base_exit_z': 0.0,
        'entry_volatility_factor': 1.5, 'exit_volatility_factor': 0.5,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 10, 'cooling_off_period': 1,
    },

    # C2: 高门槛低频入场。base_entry_z=1.25，entry_factor=3.0
    # 只在极端偏离时入场，每笔利润大但次数少；测试择时精度的价值
    'high_entry': {
        'z_back': 36, 'v_back': 32,
        'base_entry_z': 1.25, 'base_exit_z': 0.25,
        'entry_volatility_factor': 3.0, 'exit_volatility_factor': 1.0,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 15, 'cooling_off_period': 2,
    },

    # C3: 静态入场阈值。entry/exit_factor=0，入场阈值不随波动率变化
    # 剥离动态阈值的贡献，测试固定阈值 vs 动态阈值的优劣
    'static_threshold': {
        'z_back': 36, 'v_back': 32,
        'base_entry_z': 1.0, 'base_exit_z': 0.25,
        'entry_volatility_factor': 0.0, 'exit_volatility_factor': 0.0,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 12, 'cooling_off_period': 2,
    },

    # C4: 极度动态阈值。entry_factor=3.5，低波动时疯狂入场，高波动时几乎不入场
    # 最大化波动率敏感性：只在市场平静时抄底，高波动时完全不参与
    'vol_gated': {
        'z_back': 36, 'v_back': 28,
        'base_entry_z': 0.5, 'base_exit_z': 0.0,
        'entry_volatility_factor': 3.5, 'exit_volatility_factor': 1.0,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 15, 'cooling_off_period': 2,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP D — EXIT & HOLDING PERIOD VARIANTS
    # 系统变化 base_exit_z、exit_factor、max_holding_period，测试出场策略设计
    # ════════════════════════════════════════════════════════════════════════

    # D1: 即时离场。base_exit_z=0.0，exit_factor=0.25，一旦价差开始回归就离场
    # 锁定利润快，避免回吐；但可能错过价差继续回归的更大利润
    'quick_exit': {
        'z_back': 36, 'v_back': 32,
        'base_entry_z': 0.75, 'base_exit_z': 0.0,
        'entry_volatility_factor': 2.25, 'exit_volatility_factor': 0.25,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 5, 'cooling_off_period': 2,
    },

    # D2: 耐心持仓等待完全回归。base_exit_z=0.25，宽止损，长持仓
    # 等待价差完全穿越均值才平仓，最大化单笔收益；适合协整强的配对
    'patient_hold': {
        'z_back': 40, 'v_back': 36,
        'base_entry_z': 1.0, 'base_exit_z': 0.25,
        'entry_volatility_factor': 2.5, 'exit_volatility_factor': 1.0,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2.5,
        'max_holding_period': 20, 'cooling_off_period': 3,
    },

    # D3: 超短持仓闪电战。max_holding_period=3，强制 3 天内离场
    # 测试均值回归是否主要发生在开仓后 1-3 天内，极短持仓能否降低尾部风险
    'flash_hold': {
        'z_back': 25, 'v_back': 20,
        'base_entry_z': 0.5, 'base_exit_z': 0.0,
        'entry_volatility_factor': 1.0, 'exit_volatility_factor': 0.5,
        'amplifier': 1, 'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 3, 'cooling_off_period': 1,
    },

    # D4: 对称入出场。exit_z = entry_z / 2，exit_factor = entry_factor / 2
    # 出场门槛始终与入场门槛保持比例关系，避免过早或过晚离场
    'symmetric_exit': {
        'z_back': 36, 'v_back': 32,
        'base_entry_z': 1.0, 'base_exit_z': 0.5,
        'entry_volatility_factor': 2.0, 'exit_volatility_factor': 1.0,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 15, 'cooling_off_period': 2,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP E — RISK PROFILE ARCHETYPES
    # 整体风险档位：激进、保守、极端深度等，参数协同设计
    # ════════════════════════════════════════════════════════════════════════

    # E1: 激进全面型。低门槛 + 高杠杆 + 宽止损 + 长持仓 + 快冷静期
    # 最大化交易频次和仓位规模，承担更多尾部风险
    'aggressive': {
        'z_back': 30, 'v_back': 28,
        'base_entry_z': 0.5, 'base_exit_z': 0.0,
        'entry_volatility_factor': 1.5, 'exit_volatility_factor': 0.5,
        'amplifier': 3, 'volatility_stop_loss_multiplier': 2.5,
        'max_holding_period': 20, 'cooling_off_period': 1,
    },

    # E2: 保守防守型。高门槛 + 低杠杆 + 紧止损 + 长冷静期
    # 最小化交易频次，每笔交易都要有充分的统计支撑
    'conservative': {
        'z_back': 40, 'v_back': 36,
        'base_entry_z': 1.25, 'base_exit_z': 0.25,
        'entry_volatility_factor': 3.0, 'exit_volatility_factor': 1.0,
        'amplifier': 1, 'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 12, 'cooling_off_period': 3,
    },

    # E3: 极端深度偏离猎手。只等最极端的偏离机会，超高杠杆放大回归收益
    # base_entry_z=1.25 + factor=3.0 → 普通波动时 entry_z ≈ 2.5 (上限)
    # 只有价差偏离超过 2.5σ 才入场，每年交易次数极少但单笔盈利大
    'deep_dislocation': {
        'z_back': 40, 'v_back': 32,
        'base_entry_z': 1.25, 'base_exit_z': 0.0,
        'entry_volatility_factor': 3.0, 'exit_volatility_factor': 0.5,
        'amplifier': 3, 'volatility_stop_loss_multiplier': 2.5,
        'max_holding_period': 20, 'cooling_off_period': 1,
    },

    # E4: 高频紧风控型。低门槛快速入场，但止损极紧、持仓超短、冷静期长
    # 高换手率 + 严格损失控制；每笔亏损小，靠胜率和次数取胜
    'high_turnover': {
        'z_back': 25, 'v_back': 25,
        'base_entry_z': 0.5, 'base_exit_z': 0.0,
        'entry_volatility_factor': 1.5, 'exit_volatility_factor': 0.5,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 5, 'cooling_off_period': 3,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP F — VOLATILITY REGIME SPECIALISTS
    # 专门为高/低波动率市场环境设计，vol_back 影响止损 level 的计算基础
    # ════════════════════════════════════════════════════════════════════════

    # F1: 低波动率市场专用。短 v_back 捕捉当前低波动，tight stop 适合低波动价差
    # 低波动时 normalized_vol 趋近 0，entry_z ≈ base_entry_z，频繁触发
    'low_vol_specialist': {
        'z_back': 30, 'v_back': 15,
        'base_entry_z': 0.5, 'base_exit_z': 0.0,
        'entry_volatility_factor': 1.5, 'exit_volatility_factor': 0.5,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 8, 'cooling_off_period': 1,
    },

    # F2: 高波动率市场专用。长 v_back 稳定波动基准，宽止损应对高波动价差
    # 高波动时 normalized_vol→1，entry_z 被推高，只在极端偏离入场
    'high_vol_specialist': {
        'z_back': 40, 'v_back': 50,
        'base_entry_z': 1.0, 'base_exit_z': 0.0,
        'entry_volatility_factor': 2.0, 'exit_volatility_factor': 0.75,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 3.0,
        'max_holding_period': 20, 'cooling_off_period': 2,
    },

    # F3: 波动自适应全范围。极大 entry_factor 使阈值随波动率线性拉伸
    # 低波动：entry_z≈0.5（低门槛）；高波动：entry_z→2.5（高门槛）
    # 全自动适应市场状态，无需人工切换
    'vol_adaptive': {
        'z_back': 36, 'v_back': 28,
        'base_entry_z': 0.5, 'base_exit_z': 0.0,
        'entry_volatility_factor': 3.0, 'exit_volatility_factor': 1.0,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 15, 'cooling_off_period': 2,
    },

    # F4: 波动率反向操作。entry_factor 低 (入场不受波动率限制) + stop_mult 高 (宽止损)
    # 不管波动率高低都积极入场，但给每笔交易足够的呼吸空间
    # 测试去掉波动率门控的影响
    'vol_agnostic': {
        'z_back': 36, 'v_back': 32,
        'base_entry_z': 0.75, 'base_exit_z': 0.0,
        'entry_volatility_factor': 0.5, 'exit_volatility_factor': 0.25,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 3.0,
        'max_holding_period': 15, 'cooling_off_period': 2,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP G — COOLING-OFF / RE-ENTRY VARIANTS
    # 系统变化 cooling_off_period，测试止损后重新开仓的时机选择
    # ════════════════════════════════════════════════════════════════════════

    # G1: 即时重入。cooling_off=1，止损后次日就允许重新开仓
    # 假设价差均值回归，止损后很快可能再次触发入场信号
    'fast_reentry': {
        'z_back': 36, 'v_back': 32,
        'base_entry_z': 0.75, 'base_exit_z': 0.0,
        'entry_volatility_factor': 2.25, 'exit_volatility_factor': 0.75,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 12, 'cooling_off_period': 1,
    },

    # G2: 长冷静期。cooling_off=5，止损后等待 5 天才重新评估
    # 假设止损意味着协整关系暂时破裂，需要更长时间恢复
    'slow_reentry': {
        'z_back': 36, 'v_back': 32,
        'base_entry_z': 0.75, 'base_exit_z': 0.0,
        'entry_volatility_factor': 2.25, 'exit_volatility_factor': 0.75,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 12, 'cooling_off_period': 5,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP H — COMBINED BEST-GUESS BLENDS
    # 基于对策略机理的理解，设计几个预期表现最优的混合方案
    # ════════════════════════════════════════════════════════════════════════

    # H1: 信号稳定 + 快速离场。长 z_back (稳定信号) + 短持仓 + tight exit
    # 长窗口过滤噪声信号，快速锁定回归收益，减少持仓时间暴露
    'stable_signal_quick_exit': {
        'z_back': 45, 'v_back': 30,
        'base_entry_z': 0.75, 'base_exit_z': 0.0,
        'entry_volatility_factor': 2.5, 'exit_volatility_factor': 0.5,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 8, 'cooling_off_period': 2,
    },

    # H2: 快信号 + 严格止损。短窗口快速响应，但止损极紧防止噪声开仓扩大亏损
    # 快信号天然噪声多，tight stop 补偿
    'fast_signal_tight_stop': {
        'z_back': 20, 'v_back': 20,
        'base_entry_z': 0.75, 'base_exit_z': 0.0,
        'entry_volatility_factor': 2.0, 'exit_volatility_factor': 0.5,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 1.5,
        'max_holding_period': 5, 'cooling_off_period': 2,
    },

    # H3: 中等信号 + 高杠杆 + 宽止损。均衡窗口，提高杠杆放大每笔回归收益
    # 协整好的配对最适合：信号稳定、仓位大、止损给空间
    'medium_signal_high_leverage': {
        'z_back': 36, 'v_back': 28,
        'base_entry_z': 0.75, 'base_exit_z': 0.0,
        'entry_volatility_factor': 2.0, 'exit_volatility_factor': 0.75,
        'amplifier': 3, 'volatility_stop_loss_multiplier': 2.5,
        'max_holding_period': 15, 'cooling_off_period': 2,
    },

    # H4: 深度偏离 + 快出场。只等极端偏离入场，但一旦开始回归立即锁利
    # 极少开仓，每笔入场置信度高，快速获利了结避免回吐
    'deep_entry_quick_exit': {
        'z_back': 40, 'v_back': 32,
        'base_entry_z': 1.25, 'base_exit_z': 0.0,
        'entry_volatility_factor': 2.5, 'exit_volatility_factor': 0.25,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 8, 'cooling_off_period': 2,
    },

    # H5: 保守信号 + 无杠杆 + 超宽止损。最低频率入场，完全依赖信号质量而非杠杆
    # 适合协整关系较弱、波动大的配对：减少开仓、给每笔足够空间自然回归
    'conservative_no_leverage': {
        'z_back': 45, 'v_back': 40,
        'base_entry_z': 1.0, 'base_exit_z': 0.25,
        'entry_volatility_factor': 3.0, 'exit_volatility_factor': 1.0,
        'amplifier': 1, 'volatility_stop_loss_multiplier': 3.0,
        'max_holding_period': 20, 'cooling_off_period': 3,
    },

    # H6: 全面均衡中等偏激进。在 default 基础上，窗口略短、杠杆微升、持仓略长
    # 对于协整稳定的配对，这是比 default 更激进但仍保持纪律的档位
    'balanced_plus': {
        'z_back': 30, 'v_back': 28,
        'base_entry_z': 0.75, 'base_exit_z': 0.0,
        'entry_volatility_factor': 2.0, 'exit_volatility_factor': 0.75,
        'amplifier': 2, 'volatility_stop_loss_multiplier': 2,
        'max_holding_period': 15, 'cooling_off_period': 2,
    },
}


# ~~~~~~~~~~~~~~~~~~~~~~ CONFIG LOADING ~~~~~~~~~~~~~~~~~~~~~~

def find_latest_config(config_dir):
    """Return path to the most recently modified JSON in config_dir."""
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
        # Inline param dict — use as-is
        return param_set_ref, 'custom'


def load_config(config_path):
    """Load and validate a run config JSON. Returns (start_date, end_date, runs).

    Pair entries in JSON can be:
      - [s1, s2]              — uses run-level param_set
      - [s1, s2, "param_set"] — per-pair param_set override
    """
    with open(config_path, 'r') as f:
        cfg = json.load(f)

    start_date = cfg.get('start_date', '2024-12-01')

    raw_end = cfg.get('end_date', 'auto_minus_30d')
    if raw_end == 'auto_minus_30d':
        end_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    elif raw_end == 'auto':
        end_date = datetime.now().strftime('%Y-%m-%d')
    else:
        end_date = raw_end

    trade_start_date = cfg.get('trade_start_date')  # optional — None means trade from day 1

    runs = cfg.get('runs', [])
    if not runs:
        raise ValueError(f"No runs defined in {config_path}")

    # Resolve param_set references and validate
    resolved_runs = []
    for i, run in enumerate(runs):
        label = run.get('label', f'run_{i}')
        raw_pairs = run.get('pairs')
        if not raw_pairs:
            raise ValueError(f"Run '{label}' has no pairs defined")

        # Run-level param_set (used as default for all pairs in this run)
        run_param_set_ref = run.get('param_set', 'default')
        params, param_set_name = _resolve_param_set(run_param_set_ref, label)

        # Build clean pairs list (only [s1, s2]) and per-pair overrides dict
        pairs = []
        pair_params = {}
        for entry in raw_pairs:
            s1, s2 = entry[0], entry[1]
            pairs.append([s1, s2])
            # If a 3rd element is present, it's a per-pair param_set name
            if len(entry) >= 3:
                per_pair_ps = entry[2]
                per_pair_dict, _ = _resolve_param_set(per_pair_ps, f"{label}/{s1}/{s2}")
                pair_params[f"{s1}/{s2}"] = per_pair_dict

        resolved_runs.append({
            'label': label,
            'pairs': pairs,
            'params': params,
            'param_set_name': param_set_name,
            'pair_params': pair_params,  # empty dict if no per-pair overrides
        })

    return start_date, end_date, trade_start_date, resolved_runs


# ~~~~~~~~~~~~~~~~~~~~~~ HELPERS ~~~~~~~~~~~~~~~~~~~~~~

def make_pairs_label(pairs):
    return '_'.join(f"{p[0]}-{p[1]}" for p in pairs)


# ~~~~~~~~~~~~~~~~~~~~~~ MAIN ~~~~~~~~~~~~~~~~~~~~~~

def run_from_config(config_path):
    """Execute all runs defined in a JSON config file."""
    log.info(f"Loading run config: {config_path}")
    start_date, end_date, trade_start_date, runs = load_config(config_path)
    log.info(f"Date range: {start_date} → {end_date}" +
             (f"  (trading from {trade_start_date})" if trade_start_date else ""))
    log.info(f"Total runs: {len(runs)}")

    base_dir = os.path.dirname(os.path.abspath(__file__))

    # ---- Pre-load data once per unique symbol set ----
    # Collect all unique symbol sets across all runs first
    data_cache = {}
    for run in runs:
        symbols = sorted(set(sym for pair in run['pairs'] for sym in pair))
        sym_key = tuple(symbols)
        if sym_key not in data_cache:
            log.info(f"Pre-loading {len(symbols)} symbols from Parquet: {symbols}")
            data_cache[sym_key] = PortfolioRun.load_historical_data(
                start_date, end_date, list(symbols)
            )

    # ---- Execute each run ----
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

    # ---- Save summary ----
    if all_results:
        summary_df = pd.DataFrame(all_results)
        summary_df = summary_df.sort_values('sharpe_ratio', ascending=False, na_position='last')

        os.makedirs(os.path.join(base_dir, 'historical_runs'), exist_ok=True)
        summary_file = os.path.join(
            base_dir, 'historical_runs',
            f'strategy_summary_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
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
