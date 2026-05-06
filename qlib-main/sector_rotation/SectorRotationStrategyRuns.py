"""
SectorRotationStrategyRuns.py
=============================================================
Named parameter sets for sector rotation strategy backtesting.

Mirrors the PARAM_SETS pattern from PortfolioMRPTStrategyRuns.py /
PortfolioMTFSStrategyRuns.py.  Each entry is a dict of dotted-path
config overrides applied on top of the base config.yaml.

Usage
-----
    from SectorRotationStrategyRuns import PARAM_SETS, apply_param_set

    # Load base config
    from sector_rotation.data.loader import load_config
    base_cfg = load_config()

    # Apply a named set and run a backtest
    cfg = apply_param_set(base_cfg, PARAM_SETS['momentum_heavy'])
    from sector_rotation.backtest.engine import SectorRotationBacktest
    result = SectorRotationBacktest(cfg).run(prices=prices, macro=macro)

    # Batch sweep
    for name, ps in PARAM_SETS.items():
        cfg = apply_param_set(base_cfg, ps)
        result = SectorRotationBacktest(cfg).run(prices=prices, macro=macro)
        print(name, result.metrics['sharpe'])

Override key format
-------------------
Keys are dotted paths into config.yaml, e.g.
    "signals.weights.cross_sectional_momentum"  → cfg["signals"]["weights"]["cross_sectional_momentum"]
    "risk.vix_progressive_derisk.tiers"         → list[dict] replaces the tiers list in full

IMPORTANT — Signal weight constraint:
    signals.weights.cross_sectional_momentum
  + signals.weights.ts_momentum
  + signals.weights.relative_value
  + signals.weights.regime_adjustment
  MUST sum to 1.0 in every parameter set that overrides them.

Academic references embedded in set descriptions (abbreviated):
  JT1993   = Jegadeesh-Titman (1993) — 12-1 month momentum optimal window
  JT2001   = Jegadeesh-Titman (2001) — shorter windows in high-dispersion markets
  MOP2012  = Moskowitz-Ooi-Pedersen (2012) — TSMOM explains CS momentum
  BSC2015  = Barroso-Santa-Clara (2015) — vol-scaling > binary crash filter
  DM2016   = Daniel-Moskowitz (2016) — momentum crashes; two-layer defense
  MM2017   = Moreira-Muir (2017) — vol-managed portfolios; optimal target 7-8%
  AMP2013  = Asness-Moskowitz-Pedersen (2013) — value and momentum everywhere
  FF1992   = Fama-French (1992) — value premium (HML)
  AB2007   = Ang-Bekaert (2007) — regime switching adds alpha
  MRT2010  = Maillard-Roncalli-Teïletche (2010) — Equal Risk Contribution
  CD2006   = Clarke-DeMiguel (2006) — Global Minimum Variance
  DGU2009  = DeMiguel-Garlappi-Uppal (2009) — 1/N hard to beat
  LW2004   = Ledoit-Wolf (2004) — analytical shrinkage estimator
  FP2014   = Frazzini-Pedersen (2014) — Betting Against Beta
  GZ1993   = Grossman-Zhou (1993) — optimal drawdown management
  GP2013   = Garleanu-Pedersen (2013) — dynamic trading under transaction costs
  GK2000   = Grinold-Kahn (2000) — Fundamental Law of Active Management
  W2009    = Whaley (2009) — VIX fear gauge interpretation

Parameters intentionally excluded (infrastructure-level, not strategy):
    data.*        price source, cache dirs, MongoDB settings
    universe.*    ETF list and benchmark are fixed
    costs.*       liquidity tiers and cost bps reflect real market structure
    backtest.*    IS/OOS dates, capital, walk-forward windows
    report.*      tearsheet formatting
    logging.*     log level
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# Config override helper
# ---------------------------------------------------------------------------

def apply_param_set(base_config: dict, param_set: dict) -> dict:
    """
    Apply a flat dotted-path param set onto a deep copy of base_config.

    Nested path segments are created on demand.  List-valued overrides
    (e.g. "risk.vix_progressive_derisk.tiers") replace the target node
    in its entirety.
    """
    cfg = deepcopy(base_config)
    for key, value in param_set.items():
        parts = key.split(".")
        d = cfg
        for part in parts[:-1]:
            d = d.setdefault(part, {})
        d[parts[-1]] = value
    return cfg


def get_param_set(name: str) -> dict:
    """Return a named param set (raises KeyError if unknown)."""
    if name not in PARAM_SETS:
        raise KeyError(
            f"Unknown param set: '{name}'.  Available: {sorted(PARAM_SETS)}"
        )
    return PARAM_SETS[name]


# ===========================================================================
# PARAM_SETS  — 59 named parameter sets in 13 thematic groups
#
# Group A  (6 sets) — Signal Factor Architecture          (10-18 params)
# Group B  (5 sets) — Momentum Microstructure             (10-15 params)
# Group C  (4 sets) — Momentum Crash Protection           (11-17 params)
# Group D  (5 sets) — Portfolio Construction Theory       (11-15 params)
# Group E  (5 sets) — Position Concentration              (10-14 params)
# Group F  (5 sets) — Regime Detection Architecture       (18-19 params)
# Group G  (4 sets) — Rebalance & Transaction Cost        (10-13 params)
# Group H  (4 sets) — VIX De-risk Architecture            (11-13 params)
# Group I  (4 sets) — Volatility Scaling Science          (10-12 params)
# Group J  (4 sets) — Beta & Market Exposure              (11-14 params)
# Group K  (3 sets) — Drawdown Circuit Breaker            (11-12 params)
# Group L  (6 sets) — Market Regime Archetypes            (20-23 params)
# Group M  (4 sets) — Isolated Factor Tests               (10-11 params)
# ===========================================================================

PARAM_SETS: Dict[str, Dict[str, Any]] = {

    # ════════════════════════════════════════════════════════════════════════
    # GROUP A — SIGNAL FACTOR ARCHITECTURE
    # 系统变化四因子权重分配，并配套设置各子信号的关键超参数。
    # 测试不同因子架构对信息比率、最大回撤和换手率的边际贡献。
    # 所有变体信号权重严格 = 1.0。
    # ════════════════════════════════════════════════════════════════════════

    # A1: 生产基准。
    # JT1993: 12-1 月 CS 动量是学术文献最充分支撑的参数组合。
    # REG 从 0.35 降至 0.25 防止机制信号压制 XLK 等强动量板块（2023教训）。
    # acceleration 3 月窗口捕捉短期动量加速，weight_boost=0.05 提供温和奖励。
    # beta 约束 0.70-1.10 给优化器适度空间而不偏离市场暴露太远。
    'default': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'signals.cs_momentum.lookback_months':         12,
        'signals.cs_momentum.skip_months':             1,
        'signals.cs_momentum.zscore_window':           36,
        'signals.ts_momentum.lookback_months':         12,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.acceleration.enabled':                True,
        'signals.acceleration.lookback_months':        3,
        'signals.acceleration.weight_boost':           0.05,
        'portfolio.top_n_sectors':                     5,
    },

    # A2: 动量超配。
    # AMP2013: "Value and Momentum Everywhere" — 在板块级别动量与价值均有效，
    # 但动量因子在趋势清晰的牛市中 IR 更高（XLK+57% in 2023）。
    # skip=0 保留近月强信号；9 月窗口比 12 月对季度级轮换反应更快。
    # risk_on / transition_up 的 cs_mom 乘数提至 1.2-1.3，机制确认后放大追势。
    # zscore_window=30 与 lookback_months=9 匹配，避免归一化窗口过长。
    'momentum_heavy': {
        'signals.weights.cross_sectional_momentum':    0.50,
        'signals.weights.ts_momentum':                 0.20,
        'signals.weights.relative_value':              0.10,
        'signals.weights.regime_adjustment':           0.20,
        'signals.cs_momentum.lookback_months':         9,
        'signals.cs_momentum.skip_months':             0,
        'signals.cs_momentum.zscore_window':           30,
        'signals.ts_momentum.lookback_months':         9,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.acceleration.enabled':                True,
        'signals.acceleration.lookback_months':        2,
        'signals.acceleration.weight_boost':           0.08,
        'signals.regime.regime_weights.risk_on.cross_sectional_momentum':        1.2,
        'signals.regime.regime_weights.transition_up.cross_sectional_momentum':  1.3,
        'signals.regime.regime_weights.risk_off.cross_sectional_momentum':       0.5,
        'signals.regime.regime_weights.transition_down.cross_sectional_momentum': 0.6,
        'portfolio.constraints.beta_max':              1.40,
        'portfolio.constraints.max_weight':            0.45,
        'portfolio.top_n_sectors':                     5,
        # STM: 动量超配策略用6月STM补充9月回看的中期盲区
        'signals.short_term_momentum.enabled':         True,
        'signals.short_term_momentum.weight_bonus':    0.06,
    },

    # A3: 价值倾斜。
    # FF1992: Fama-French 三因子模型证明 HML 价值溢价跨周期持续存在。
    # 板块 P/E 分位数在高估值分化环境（2022 泡沫破裂后）信号质量最高。
    # crash_filter=0.3 保留一定弱板块暴露，避免价值陷阱完全排除有支撑板块。
    # pe_lookback_years=10 覆盖完整经济周期，减少单周期 P/E 均值偏差。
    # risk_off 时 RV 乘数提至 1.4，应激期价值因子更稳健（AMP2013）。
    # acceleration 关闭，避免短期加速信号干扰长周期价值判断。
    'value_tilt': {
        'signals.weights.cross_sectional_momentum':    0.25,
        'signals.weights.ts_momentum':                 0.10,
        'signals.weights.relative_value':              0.35,
        'signals.weights.regime_adjustment':           0.30,
        'signals.cs_momentum.lookback_months':         15,
        'signals.cs_momentum.skip_months':             1,
        'signals.cs_momentum.zscore_window':           48,
        'signals.ts_momentum.lookback_months':         12,
        'signals.ts_momentum.crash_filter_multiplier': 0.3,
        'signals.value.pe_lookback_years':             10,
        'signals.acceleration.enabled':                False,
        'signals.regime.regime_weights.risk_off.relative_value':  1.4,
        'signals.regime.regime_weights.risk_on.relative_value':   0.8,
        'signals.regime.defensive_bonus_risk_off':     0.25,
        # ERM: value-heavy策略补充动态盈利趋势(FF1992: earnings momentum ⊥ value)
        'signals.earnings_revision.enabled':           True,
        'signals.earnings_revision.weight_bonus':      0.05,
    },

    # A4: 机制驱动。
    # AB2007: 机制转换模型可识别不同状态下最优信号组合；REG=0.40 主导决策。
    # vix_high_threshold=22 比默认更早触发机制切换，匹配高 REG 权重的保护需求。
    # risk_off 时 cs_mom 乘数降至 0.4：DM2016 发现动量在恐慌中发生崩溃
    # 是因为拥挤多头被迫平仓，急剧降低动量因子权重是主动防御措施。
    # defensive_bonus=0.45 在 risk_off 时对 XLU/XLP/XLV 的 z-score 加 0.45σ。
    'regime_driven': {
        'signals.weights.cross_sectional_momentum':    0.20,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.25,
        'signals.weights.regime_adjustment':           0.40,
        'signals.cs_momentum.lookback_months':         12,
        'signals.cs_momentum.skip_months':             1,
        'signals.cs_momentum.zscore_window':           36,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.regime.vix_high_threshold':           22.0,
        'signals.regime.hy_spread_high_bps':           420,
        'signals.regime.regime_weights.risk_on.cross_sectional_momentum':        1.1,
        'signals.regime.regime_weights.risk_on.ts_momentum':                     1.1,
        'signals.regime.regime_weights.risk_off.cross_sectional_momentum':       0.4,
        'signals.regime.regime_weights.risk_off.relative_value':                 1.5,
        'signals.regime.regime_weights.transition_down.cross_sectional_momentum': 0.5,
        'signals.regime.defensive_bonus_risk_off':     0.45,
        'risk.vix_progressive_derisk.enabled':         True,
        'risk.vix_progressive_derisk.tiers':           [{'vix_above': 22, 'cash_pct': 0.20},
                                                        {'vix_above': 26, 'cash_pct': 0.38}],
    },

    # A5: 时序动量主导。
    # MOP2012: "Time-Series Momentum" — TS 动量在期货和 ETF 宇宙均有独立解释力，
    # 非 CS 动量的噪音副产品；TS=0.30 允许绝对动量信号独立影响权重分配。
    # 高 TS 权重时 crash_filter=0.0 的影响被充分放大：负绝对动量板块完全排除。
    # beta_max=0.95 防止高 TS 偏好的强动量板块（XLK）拉高整体 beta。
    # vol_scaling target=0.10 + threshold=1.2 保护 TS 动量在高波动期的仓位。
    'ts_dominant': {
        'signals.weights.cross_sectional_momentum':    0.30,
        'signals.weights.ts_momentum':                 0.30,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.20,
        'signals.cs_momentum.lookback_months':         12,
        'signals.cs_momentum.skip_months':             1,
        'signals.cs_momentum.zscore_window':           36,
        'signals.ts_momentum.lookback_months':         12,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.acceleration.enabled':                True,
        'signals.acceleration.lookback_months':        3,
        'signals.acceleration.weight_boost':           0.06,
        'portfolio.constraints.beta_min':              0.60,
        'portfolio.constraints.beta_max':              0.95,
        'risk.vol_scaling.target_vol_annual':          0.10,
        'risk.vol_scaling.scale_threshold':            1.2,
    },

    # A6: 四因子均等。
    # DGU2009: "Optimal Versus Naive Diversification" — 等权重 1/N 策略难以被主观
    # 权重设计系统性超越；此集作为无偏基准，量化优化权重分配的实际增量 alpha。
    # zscore_softmax 代替 rank 权重方案，允许 z-score 大小连续影响配置比例。
    # weight_boost=0.04 温和奖励加速板块，不影响四因子等权的基准性质。
    'balanced_four': {
        'signals.weights.cross_sectional_momentum':    0.25,
        'signals.weights.ts_momentum':                 0.25,
        'signals.weights.relative_value':              0.25,
        'signals.weights.regime_adjustment':           0.25,
        'signals.cs_momentum.lookback_months':         12,
        'signals.cs_momentum.skip_months':             1,
        'signals.cs_momentum.zscore_window':           36,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.value.pe_lookback_years':             10,
        'signals.acceleration.enabled':                True,
        'signals.acceleration.weight_boost':           0.04,
        'portfolio.weight_scheme':                     'zscore_softmax',
        'portfolio.top_n_sectors':                     5,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP B — MOMENTUM MICROSTRUCTURE
    # 系统变化 CS 动量回望窗口、短期反转跳过月数、z-score 归一化窗口。
    # JT1993/2001 确立了 12-1 月为最优窗口，但高频噪音环境（JT2001）和
    # 结构性趋势环境（Asness1997）分别支持更短和更长的窗口设置。
    # ════════════════════════════════════════════════════════════════════════

    # B1: 快速信号。
    # JT2001: 在板块轮换频率加快的环境下（如政策驱动行情），6 月窗口
    # 反应灵敏度更高；skip=0 保留近月信息，ETF 宇宙中短期反转效应弱。
    # zscore_window=24 与 lookback=6 匹配，归一化窗口不超过 4× 回望长度。
    # zscore_change_threshold=0.3 降低换手门槛，及时跟踪快速信号变化。
    # estimation_window=10 天的短期已实现波动率与快速信号周期一致。
    'fast_momentum': {
        'signals.weights.cross_sectional_momentum':    0.45,
        'signals.weights.ts_momentum':                 0.20,
        'signals.weights.relative_value':              0.15,
        'signals.weights.regime_adjustment':           0.20,
        'signals.cs_momentum.lookback_months':         6,
        'signals.cs_momentum.skip_months':             0,
        'signals.cs_momentum.zscore_window':           24,
        'signals.ts_momentum.lookback_months':         6,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.acceleration.enabled':                True,
        'signals.acceleration.lookback_months':        1,
        'signals.acceleration.weight_boost':           0.08,
        'rebalance.zscore_change_threshold':           0.3,
        'risk.vol_scaling.estimation_window':          10,
        'portfolio.top_n_sectors':                     4,
        'portfolio.constraints.beta_max':              1.40,
        'portfolio.constraints.max_weight':            0.45,
        # STM: 快速动量(6m回看)已经很短，再加6m STM作为确认信号
        'signals.short_term_momentum.enabled':         True,
        'signals.short_term_momentum.weight_bonus':    0.06,
    },

    # B2: 中速信号。
    # 9-1 月是 JT1993 最优 12-1 窗口与快速 6-0 之间的折中点；
    # skip=1 规避最近 1 月的短期反转（反转在 ETF 中虽弱但仍存在）。
    # zscore_window=30 提供约 2.5 年的归一化基准，平衡稳定性与适应性。
    # acceleration lookback=2 月与 9 月主信号匹配，捕捉近期动量加速。
    'medium_momentum': {
        'signals.weights.cross_sectional_momentum':    0.42,
        'signals.weights.ts_momentum':                 0.18,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.20,
        'signals.cs_momentum.lookback_months':         9,
        'signals.cs_momentum.skip_months':             1,
        'signals.cs_momentum.zscore_window':           30,
        'signals.ts_momentum.lookback_months':         9,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.acceleration.enabled':                True,
        'signals.acceleration.lookback_months':        2,
        'signals.acceleration.weight_boost':           0.06,
        'rebalance.zscore_change_threshold':           0.4,
    },

    # B3: 无跳过中速信号（孤立 skip 效应）。
    # 孤立测试 JT1993 skip=1 假设的必要性：与 B2（9-1）唯一区别是 skip=0 vs skip=1。
    # JT2001: ETF 宇宙中短期反转效应弱，skip=0 可能不劣于 skip=1；
    # 若 B3 Sharpe ≥ B2，说明 skip=1 对板块 ETF 宇宙是多余的过滤步骤。
    # ts_lookback=9 与 cs_lookback=9 一致，保持信号时间尺度内部一致性。
    # zscore_window=30 与 lookback=9 匹配（约 3.3× 回望长度的经验规则）。
    # zscore_change_threshold=0.4 配合 9 月快速信号，适中的换手控制。
    'no_skip_medium': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.18,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.22,
        'signals.cs_momentum.lookback_months':         9,
        'signals.cs_momentum.skip_months':             0,
        'signals.cs_momentum.zscore_window':           30,
        'signals.ts_momentum.lookback_months':         9,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.acceleration.enabled':                True,
        'signals.acceleration.lookback_months':        2,
        'signals.acceleration.weight_boost':           0.05,
        'rebalance.zscore_change_threshold':           0.4,
    },

    # B4: 慢速信号。
    # Asness(1997) 研究发现 15-2 月窗口（避开长期均值回归）在多资产中
    # 表现稳定；skip=2 是比默认 skip=1 更强的短期反转过滤。
    # zscore_window=48 跨越多个经济周期，提升 z-score 基准的长期稳定性。
    # acceleration 关闭，避免短期加速噪音干扰长周期趋势判断。
    # cov.lookback_days=504 与信号周期一致，减少协方差估计的短期噪音。
    'slow_momentum': {
        'signals.weights.cross_sectional_momentum':    0.35,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.25,
        'signals.weights.regime_adjustment':           0.25,
        'signals.cs_momentum.lookback_months':         15,
        'signals.cs_momentum.skip_months':             2,
        'signals.cs_momentum.zscore_window':           48,
        'signals.ts_momentum.lookback_months':         15,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.acceleration.enabled':                False,
        'rebalance.zscore_change_threshold':           0.7,
        'portfolio.cov.lookback_days':                 504,
        'portfolio.constraints.beta_min':              0.65,
        # STM: 15月慢动量的盲区由6月STM补充(Asness1997: intermediate momentum)
        'signals.short_term_momentum.enabled':         True,
        'signals.short_term_momentum.weight_bonus':    0.06,
    },

    # B5: 强反转过滤。
    # JT2001 发现部分市场（包括高流动性 ETF 宇宙）中近 2 月有显著反转。
    # skip=2 比 skip=1 更强地规避近期追高信号，降低信号噪音。
    # crash_filter=0.5 配合 skip=2：双重过滤减少假信号触发。
    # value_source='proxy' 使用价格内嵌的相对价值代理，
    # 无需额外数据获取，与强过滤的低换手定位一致。
    'skip_heavy': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.25,
        'signals.weights.regime_adjustment':           0.20,
        'signals.cs_momentum.lookback_months':         12,
        'signals.cs_momentum.skip_months':             2,
        'signals.cs_momentum.zscore_window':           36,
        'signals.ts_momentum.lookback_months':         12,
        'signals.ts_momentum.crash_filter_multiplier': 0.5,
        'signals.acceleration.enabled':                False,
        'signals.value_source':                        'proxy',
        'portfolio.weight_scheme':                     'rank',
        # STM: acceleration关闭的替代，6月动量捕捉中期趋势(JT2001补充)
        'signals.short_term_momentum.enabled':         True,
        'signals.short_term_momentum.weight_bonus':    0.05,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP C — MOMENTUM CRASH PROTECTION
    # 测试 crash_filter 与 vol_scaling 的不同组合对尾部保护的有效性。
    # DM2016: 动量崩溃发生在熊市底部反弹时，两层防御（crash_filter + vol_scaling）
    # 比单一机制更稳健。BSC2015: vol-scaling 是比二元 crash filter 更优的解决方案。
    # ════════════════════════════════════════════════════════════════════════

    # C1: 完全崩溃过滤 + 紧波动率管理。
    # DM2016 两层防御：crash_filter=0.0 完全排除负 TS 板块，
    # 同时 vol_scaling target=0.10 + threshold=1.2 主动缩减高波动暴露。
    # VIX 渐进降风险门槛提前（vix_above: 25/30）增加第三层保护。
    # beta_max=1.00 防止超配高 beta 板块在恐慌抛售中放大损失。
    'full_crash_filter_tight_vol': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.20,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.20,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.ts_momentum.lookback_months':         12,
        'risk.vol_scaling.enabled':                    True,
        'risk.vol_scaling.target_vol_annual':          0.10,
        'risk.vol_scaling.estimation_window':          20,
        'risk.vol_scaling.scale_threshold':            1.2,
        'risk.vol_scaling.historical_window':          252,
        'risk.vix_progressive_derisk.enabled':         True,
        'risk.vix_progressive_derisk.tiers':           [{'vix_above': 25, 'cash_pct': 0.15},
                                                        {'vix_above': 30, 'cash_pct': 0.35}],
        'portfolio.constraints.beta_min':              0.60,
        'portfolio.constraints.beta_max':              1.00,
    },

    # C2: 部分过滤 + BSC2015 vol-scaling 优化。
    # BSC2015 发现 vol-scaling 单独使用就能消除大部分动量崩溃，
    # 二元 crash_filter 仅提供边际增量；crash_filter=0.5 折中保留一定
    # 弱板块暴露，由 vol_scaling target=0.09 承担主要风险管理职责。
    # cumulative_dd_halve=-0.18 比默认 -0.20 略早触发回撤保护。
    'partial_filter_scaled': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.18,
        'signals.weights.relative_value':              0.22,
        'signals.weights.regime_adjustment':           0.20,
        'signals.ts_momentum.crash_filter_multiplier': 0.5,
        'signals.ts_momentum.lookback_months':         12,
        'risk.vol_scaling.enabled':                    True,
        'risk.vol_scaling.target_vol_annual':          0.09,
        'risk.vol_scaling.estimation_window':          15,
        'risk.vol_scaling.scale_threshold':            1.2,
        'risk.drawdown.cumulative_dd_halve':           -0.18,
        'risk.drawdown.cumulative_dd_recovery':        -0.09,
        'portfolio.constraints.beta_min':              0.60,
        'portfolio.constraints.beta_max':              1.05,
    },

    # C3: 超紧波动率管理盾牌。
    # MM2017 "Volatility-Managed Portfolios"：将目标年化波动率设为 7%
    # 可最大化动量策略的 Sharpe 比率，大幅降低尾部风险。
    # estimation_window=10 天使缩放反应更快；historical_window=126 天
    # 提高历史均值对近期市场结构变化的适应性。
    # cumulative_dd_halve=-0.15 提前触发仓位减半，保护低波目标账户。
    'vol_crash_shield': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.20,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.20,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.ts_momentum.lookback_months':         12,
        'risk.vol_scaling.enabled':                    True,
        'risk.vol_scaling.target_vol_annual':          0.07,
        'risk.vol_scaling.estimation_window':          10,
        'risk.vol_scaling.scale_threshold':            1.1,
        'risk.vol_scaling.historical_window':          126,
        'risk.vix_progressive_derisk.enabled':         True,
        'risk.vix_progressive_derisk.tiers':           [{'vix_above': 24, 'cash_pct': 0.20},
                                                        {'vix_above': 28, 'cash_pct': 0.40}],
        'rebalance.emergency_derisk_vix':              32.0,
        'risk.drawdown.cumulative_dd_halve':           -0.15,
        'risk.drawdown.cumulative_dd_recovery':        -0.07,
        'portfolio.constraints.beta_max':              1.00,
    },

    # C4: 纯波动率管理（无崩溃过滤）。
    # 测试 MM2017 核心命题：vol_scaling 单独是否足以替代 crash_filter？
    # crash_filter=1.0 关闭 TS 动量排除机制，仅依赖 vol_scaling target=0.09
    # 和渐进式 VIX 降风险实现尾部保护。
    # 与 C1/C3 对比可量化 crash_filter 的独立贡献（max_drawdown 差异）。
    'no_filter_vol_only': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'signals.ts_momentum.crash_filter_multiplier': 1.0,
        'signals.ts_momentum.lookback_months':         12,
        'risk.vol_scaling.enabled':                    True,
        'risk.vol_scaling.target_vol_annual':          0.09,
        'risk.vol_scaling.estimation_window':          15,
        'risk.vol_scaling.scale_threshold':            1.2,
        'portfolio.constraints.beta_min':              0.65,
        'portfolio.constraints.beta_max':              1.05,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP D — PORTFOLIO CONSTRUCTION THEORY
    # 系统测试不同权重优化器（inv_vol / risk_parity / gmv / equal_weight）
    # 与协方差估计方法（ledoit_wolf / oas / sample）的组合。
    # 11 板块 ETF 宇宙中样本量小，协方差估计质量至关重要（LW2004）。
    # ════════════════════════════════════════════════════════════════════════

    # D1: 反波动率 + LW 收缩（生产默认）。
    # LW2004 解析收缩估计量在 N 小/T 中等场景中优于样本协方差；
    # inv_vol 简单直观，无需求解数值优化问题，对估计误差不敏感。
    # lookback_days=252 / min_periods=63 是文献中广泛采用的实证参数。
    # beta 约束 0.70-1.10 为优化器保留足够自由度。
    'inv_vol_lw': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'portfolio.optimizer':                         'inv_vol',
        'portfolio.cov.method':                        'ledoit_wolf',
        'portfolio.cov.lookback_days':                 252,
        'portfolio.cov.min_periods':                   63,
        'portfolio.constraints.max_weight':            0.40,
        'portfolio.constraints.beta_min':              0.70,
        'portfolio.constraints.beta_max':              1.10,
        'portfolio.weight_scheme':                     'rank',
    },

    # D2: 等风险贡献 + LW 收缩。
    # MRT2010 "The Properties of Equally Weighted Risk Contributions Portfolios"：
    # ERC 比 inv_vol 更稳健地处理板块间相关性变化（XLE/XLF 相关性波动大）。
    # 对协方差估计质量更敏感，因此 LW + lookback=252 + min_periods=126 必要。
    # top_n=5 / min_zscore=-0.3 允许更多板块参与 ERC 优化，提升多样化效益。
    # vol_target=0.11 略低于默认，体现 ERC 的低波动倾向。
    'risk_parity_lw': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'portfolio.optimizer':                         'risk_parity',
        'portfolio.cov.method':                        'ledoit_wolf',
        'portfolio.cov.lookback_days':                 252,
        'portfolio.cov.min_periods':                   126,
        'portfolio.constraints.max_weight':            0.40,
        'portfolio.constraints.beta_min':              0.65,
        'portfolio.constraints.beta_max':              1.05,
        'portfolio.top_n_sectors':                     5,
        'portfolio.min_zscore':                        -0.3,
        'portfolio.weight_scheme':                     'rank',
        'risk.vol_scaling.target_vol_annual':          0.11,
    },

    # D3: 全局最小方差 + LW 收缩。
    # CD2006 "Minimum-Variance Portfolios in the US Equity Market"：GMV
    # 在低 beta 约束下能系统性降低波动率，适合绝对收益目标账户。
    # lookback_days=504 提供更长协方差估计基准，GMV 对短期噪音高度敏感。
    # beta_max=0.95 防止 GMV 过度集中于低 beta 防御板块后意外高 beta。
    # vol_target=0.11 配合 GMV 天然低波倾向，避免在低波牛市中过度缩仓。
    'gmv_lw': {
        'signals.weights.cross_sectional_momentum':    0.35,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.25,
        'signals.weights.regime_adjustment':           0.25,
        'portfolio.optimizer':                         'gmv',
        'portfolio.cov.method':                        'ledoit_wolf',
        'portfolio.cov.lookback_days':                 504,
        'portfolio.cov.min_periods':                   126,
        'portfolio.constraints.max_weight':            0.45,
        'portfolio.constraints.beta_min':              0.45,
        'portfolio.constraints.beta_max':              0.95,
        'portfolio.top_n_sectors':                     4,
        'portfolio.weight_scheme':                     'rank',
        'risk.vol_scaling.target_vol_annual':          0.11,
    },

    # D4: 等权重优化器。
    # DGU2009 "Optimal Versus Naive Diversification"：在估计误差存在的情况下，
    # 1/N 等权重策略历史上难以被复杂优化器系统性超越（样本外）。
    # 此集去除优化器影响，测试纯信号质量；若与 D1 差距小，说明优化层贡献有限。
    # top_n=5 增加等权覆盖范围，max_weight=0.30 防止等权下单板块过重。
    # min_zscore=-0.3 允许弱信号板块参与，体现 1/N 的宽松准入。
    'equal_weight_optimizer': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'portfolio.optimizer':                         'equal_weight',
        'portfolio.constraints.max_weight':            0.25,
        'portfolio.constraints.beta_min':              0.60,
        'portfolio.constraints.beta_max':              1.4,
        'portfolio.top_n_sectors':                     5,
        'portfolio.min_zscore':                        -0.3,
        'portfolio.weight_scheme':                     'rank',
        'risk.vol_scaling.target_vol_annual':          0.12,
    },

    # D5: 反波动率 + OAS 收缩。
    # Chen-Wiesel-Eldar-Goldsmith(2010) Oracle Approximating Shrinkage：
    # 在小样本（N=11 板块，T=126 天）时 OAS 的均方误差低于 LW。
    # lookback_days=126 是与 OAS 样本需求匹配的最短可行窗口。
    # zscore_softmax 代替 rank，使 z-score 大小连续驱动权重分配，
    # 增强与 OAS 精确协方差估计的信号利用效率。
    'inv_vol_oas': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'portfolio.optimizer':                         'inv_vol',
        'portfolio.cov.method':                        'oas',
        'portfolio.cov.lookback_days':                 126,
        'portfolio.cov.min_periods':                   42,
        'portfolio.constraints.max_weight':            0.40,
        'portfolio.constraints.beta_min':              0.70,
        'portfolio.constraints.beta_max':              1.10,
        'portfolio.weight_scheme':                     'zscore_softmax',
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP E — POSITION CONCENTRATION
    # 变化持仓板块数（top_n_sectors）、信号质量门槛（min_zscore）、
    # 单板块权重上限（max_weight），测试集中度对信息比率的影响。
    # GK2000 基本定律：IR = IC × sqrt(BR)，集中度权衡 IC 与广度 BR。
    # ════════════════════════════════════════════════════════════════════════

    # E1: 高集中持仓（3 板块）。
    # GK2000 基本定律在高 IC 环境下支持集中配置以最大化 IR；
    # min_zscore=0.0 确保每个持仓都有正向 z-score 支撑，
    # zscore_softmax 使最强信号板块获得更大权重。
    # cumulative_dd_halve=-0.18 比标准更早保护，对冲集中风险。
    'concentrated_3': {
        'signals.weights.cross_sectional_momentum':    0.45,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.20,
        'signals.cs_momentum.lookback_months':         12,
        'signals.cs_momentum.skip_months':             1,
        'portfolio.top_n_sectors':                     3,
        'portfolio.min_zscore':                        0.0,
        'portfolio.constraints.max_weight':            0.45,
        'portfolio.constraints.beta_min':              0.70,
        'portfolio.constraints.beta_max':              1.40,
        'portfolio.weight_scheme':                     'zscore_softmax',
        'risk.vol_scaling.target_vol_annual':          0.12,
        'risk.drawdown.cumulative_dd_halve':           -0.18,
        # RSB: 集中3板块时，RSB识别正在breakout的sector(GK2000: 高IC+集中)
        'signals.relative_strength_breakout.enabled':  True,
        'signals.relative_strength_breakout.weight_bonus': 0.06,
    },

    # E2: 标准四板块（默认）。
    # 均衡的集中度与分散度；4 板块是实证文献中截面动量策略常用的
    # 持仓数量，权衡 alpha 浓度与特质风险分散。
    'standard_4': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'portfolio.top_n_sectors':                     4,
        'portfolio.min_zscore':                        -0.5,
        'portfolio.constraints.max_weight':            0.40,
        'portfolio.constraints.beta_min':              0.70,
        'portfolio.constraints.beta_max':              1.10,
        'portfolio.weight_scheme':                     'rank',
    },

    # E3: 分散五板块 + 等风险贡献。
    # Markowitz(1952) 现代组合理论：相关性不为 1 时分散化降低风险；
    # risk_parity 确保每个板块贡献相等风险，避免高波板块（XLK）主导组合。
    # max_weight=0.35 防止 Tech 等单板块超配；zscore_softmax 平滑权重分配。
    # vol_target=0.11 配合 ERC 的天然低波特性。
    'diversified_5_rp': {
        'signals.weights.cross_sectional_momentum':    0.38,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.22,
        'signals.weights.regime_adjustment':           0.25,
        'portfolio.optimizer':                         'risk_parity',
        'portfolio.cov.method':                        'ledoit_wolf',
        'portfolio.top_n_sectors':                     5,
        'portfolio.min_zscore':                        -0.5,
        'portfolio.constraints.max_weight':            0.35,
        'portfolio.constraints.beta_min':              0.65,
        'portfolio.constraints.beta_max':              1.10,
        'portfolio.weight_scheme':                     'zscore_softmax',
        'risk.vol_scaling.target_vol_annual':          0.11,
    },

    # E4: 宽覆盖六板块 + softmax 权重。
    # Britten-Jones(1999) "The Sampling Error in Estimates of Mean-Variance
    # Efficient Portfolio Weights"：持仓数量增加可降低协方差估计误差的影响。
    # 6 板块约 55% 的 ETF 宇宙覆盖率；min_zscore=-0.8 允许弱信号进入。
    # beta_max=1.05 防止宽覆盖下高 beta 板块累积超权。
    'broad_6_softmax': {
        'signals.weights.cross_sectional_momentum':    0.35,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.25,
        'signals.weights.regime_adjustment':           0.25,
        'portfolio.optimizer':                         'inv_vol',
        'portfolio.cov.method':                        'ledoit_wolf',
        'portfolio.cov.lookback_days':                 252,
        'portfolio.top_n_sectors':                     6,
        'portfolio.min_zscore':                        -0.8,
        'portfolio.constraints.max_weight':            0.28,
        'portfolio.constraints.beta_min':              0.60,
        'portfolio.constraints.beta_max':              1.05,
        'portfolio.weight_scheme':                     'zscore_softmax',
    },

    # E5: 信号质量门控动态持仓。
    # GK2000 主动管理基本定律：提高 min_zscore 门槛是提升投资组合 IC 的关键；
    # min_zscore=0.25 约过滤掉信号分布底部 40% 的板块，保留强信号板块。
    # top_n=5 允许宽松覆盖，但 min_zscore 保证每个持仓的信号质量。
    # beta_min=0.68 确保组合在防御化过程中仍有足够市场暴露。
    'score_gated_dynamic': {
        'signals.weights.cross_sectional_momentum':    0.42,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.23,
        'signals.cs_momentum.lookback_months':         12,
        'signals.cs_momentum.skip_months':             1,
        'portfolio.top_n_sectors':                     5,
        'portfolio.min_zscore':                        0.25,
        'portfolio.constraints.max_weight':            0.40,
        'portfolio.constraints.beta_min':              0.68,
        'portfolio.constraints.beta_max':              1.45,
        'portfolio.weight_scheme':                     'rank',
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP F — REGIME DETECTION ARCHITECTURE
    # 系统变化 5 项宏观机制检测参数（VIX阈值、HY利差、收益率曲线、ISM）
    # 以及 4 种机制状态下的 3 个信号乘数（12 个乘数参数），
    # 加上防御板块奖励，总计 18 个参数/组。
    # AB2007: 机制识别精度对条件信号权重的有效性至关重要。
    # ════════════════════════════════════════════════════════════════════════

    # F1: 鹰派宏观（Hawkish）——早触发 + 强信号分化。
    # AB2007 早期预警框架：vix_high=20 比默认 25 更早切换机制；
    # HY=380bps 在信用压力初露苗头时即触发；yield_curve=-0.05 对倒挂零容忍。
    # transition_up 时大幅放大 cs_mom 乘数（1.3）捕捉反弹领先板块；
    # risk_off 时激进降低 cs_mom（0.4）并提升 RV（1.4）+ 防御奖励（0.45σ）。
    'hawkish_macro': {
        'signals.regime.vix_high_threshold':                                     20.0,
        'signals.regime.vix_extreme_threshold':                                  28.0,
        'signals.regime.hy_spread_high_bps':                                     380,
        'signals.regime.yield_curve_inversion':                                  -0.05,
        'signals.regime.ism_expansion':                                          51.0,
        'signals.regime.regime_weights.risk_on.cross_sectional_momentum':        1.1,
        'signals.regime.regime_weights.risk_on.ts_momentum':                     1.1,
        'signals.regime.regime_weights.risk_on.relative_value':                  0.9,
        'signals.regime.regime_weights.risk_off.cross_sectional_momentum':       0.4,
        'signals.regime.regime_weights.risk_off.ts_momentum':                    0.6,
        'signals.regime.regime_weights.risk_off.relative_value':                 1.4,
        'signals.regime.regime_weights.transition_up.cross_sectional_momentum':  1.3,
        'signals.regime.regime_weights.transition_up.ts_momentum':               1.1,
        'signals.regime.regime_weights.transition_up.relative_value':            0.7,
        'signals.regime.regime_weights.transition_down.cross_sectional_momentum': 0.5,
        'signals.regime.regime_weights.transition_down.ts_momentum':             0.7,
        'signals.regime.regime_weights.transition_down.relative_value':          1.3,
        'signals.regime.defensive_bonus_risk_off':                               0.45,
    },

    # F2: 标准机制（生产基准）——完整文档化的默认设置。
    # VIX=25/35 是学术文献和实务中广泛使用的恐慌-极端分隔点（W2009）。
    # HY=450bps 对应历史信用压力的典型阈值（Bloomberg 2007-2023 数据）。
    # 机制乘数参照当前生产配置完整记录：所有 12 个乘数完整显式化。
    'standard_regime': {
        'signals.regime.vix_high_threshold':                                     25.0,
        'signals.regime.vix_extreme_threshold':                                  35.0,
        'signals.regime.hy_spread_high_bps':                                     450,
        'signals.regime.yield_curve_inversion':                                  -0.10,
        'signals.regime.ism_expansion':                                          50.0,
        'signals.regime.regime_weights.risk_on.cross_sectional_momentum':        1.0,
        'signals.regime.regime_weights.risk_on.ts_momentum':                     1.0,
        'signals.regime.regime_weights.risk_on.relative_value':                  1.0,
        'signals.regime.regime_weights.risk_off.cross_sectional_momentum':       0.6,
        'signals.regime.regime_weights.risk_off.ts_momentum':                    0.8,
        'signals.regime.regime_weights.risk_off.relative_value':                 1.2,
        'signals.regime.regime_weights.transition_up.cross_sectional_momentum':  1.2,
        'signals.regime.regime_weights.transition_up.ts_momentum':               1.0,
        'signals.regime.regime_weights.transition_up.relative_value':            0.8,
        'signals.regime.regime_weights.transition_down.cross_sectional_momentum': 0.7,
        'signals.regime.regime_weights.transition_down.ts_momentum':             0.9,
        'signals.regime.regime_weights.transition_down.relative_value':          1.1,
        'signals.regime.defensive_bonus_risk_off':                               0.30,
    },

    # F3: 鸽派宏观（Dovish）——宽松阈值避免误判。
    # AB2007 状态空间模型发现机制识别噪音（regime misidentification）会系统性
    # 降低条件信号的有效性；2023年 VIX 多次触碰 25-26 但并非真 risk_off。
    # vix_high=28 / HY=500 减少假触发；risk_off 乘数分化小（0.8/0.9）
    # 以及防御奖励降至 0.15，整体偏向在模糊环境中保持较强动量暴露。
    'dovish_macro': {
        'signals.regime.vix_high_threshold':                                     28.0,
        'signals.regime.vix_extreme_threshold':                                  40.0,
        'signals.regime.hy_spread_high_bps':                                     500,
        'signals.regime.yield_curve_inversion':                                  -0.15,
        'signals.regime.ism_expansion':                                          49.0,
        'signals.regime.regime_weights.risk_on.cross_sectional_momentum':        1.0,
        'signals.regime.regime_weights.risk_on.ts_momentum':                     1.0,
        'signals.regime.regime_weights.risk_on.relative_value':                  1.0,
        'signals.regime.regime_weights.risk_off.cross_sectional_momentum':       0.8,
        'signals.regime.regime_weights.risk_off.ts_momentum':                    0.9,
        'signals.regime.regime_weights.risk_off.relative_value':                 1.1,
        'signals.regime.regime_weights.transition_up.cross_sectional_momentum':  1.1,
        'signals.regime.regime_weights.transition_up.ts_momentum':               1.0,
        'signals.regime.regime_weights.transition_up.relative_value':            0.95,
        'signals.regime.regime_weights.transition_down.cross_sectional_momentum': 0.85,
        'signals.regime.regime_weights.transition_down.ts_momentum':             0.9,
        'signals.regime.regime_weights.transition_down.relative_value':          1.05,
        'signals.regime.defensive_bonus_risk_off':                               0.15,
    },

    # F4: 动量偏置机制——在扩张期最大化动量 alpha。
    # AMP2013 跨资产实证：动量因子在经济扩张期（risk_on/transition_up）
    # alpha 最显著，恐慌期（risk_off）最脆弱（DM2016 动量崩溃）。
    # transition_up 时 cs_mom 乘数提至 1.4 是本组最高值，
    # 结合 vix_extreme=37 减少过早应急降杠杆，最大化上行捕获。
    'momentum_biased_regime': {
        'signals.regime.vix_high_threshold':                                     26.0,
        'signals.regime.vix_extreme_threshold':                                  37.0,
        'signals.regime.hy_spread_high_bps':                                     460,
        'signals.regime.yield_curve_inversion':                                  -0.12,
        'signals.regime.ism_expansion':                                          50.0,
        'signals.regime.regime_weights.risk_on.cross_sectional_momentum':        1.2,
        'signals.regime.regime_weights.risk_on.ts_momentum':                     1.1,
        'signals.regime.regime_weights.risk_on.relative_value':                  0.8,
        'signals.regime.regime_weights.risk_off.cross_sectional_momentum':       0.7,
        'signals.regime.regime_weights.risk_off.ts_momentum':                    0.8,
        'signals.regime.regime_weights.risk_off.relative_value':                 1.1,
        'signals.regime.regime_weights.transition_up.cross_sectional_momentum':  1.4,
        'signals.regime.regime_weights.transition_up.ts_momentum':               1.2,
        'signals.regime.regime_weights.transition_up.relative_value':            0.7,
        'signals.regime.regime_weights.transition_down.cross_sectional_momentum': 0.75,
        'signals.regime.regime_weights.transition_down.ts_momentum':             0.85,
        'signals.regime.regime_weights.transition_down.relative_value':          1.1,
        'signals.regime.defensive_bonus_risk_off':                               0.20,
        'rebalance.emergency_derisk_vix':                                        37.0,
    },

    # F5: 防御轮换——最大化应激期防御板块配置效率。
    # 2022 年经验：当 XLU/XLP/XLV 在利率上行+信用压力下同步走强时，
    # 防御板块奖励 0.50σ 能大幅提升这三板块的 z-score，加速防御轮换；
    # vix_high=22 / hy_spread=420 早触发确保在真正下跌前完成防御切换。
    # risk_off 时 cs_mom 乘数降至 0.3（本组最低），防止追强信号在崩溃期
    # 持有非防御性拥挤多头。
    'defensive_rotation': {
        'signals.regime.vix_high_threshold':                                     22.0,
        'signals.regime.vix_extreme_threshold':                                  32.0,
        'signals.regime.hy_spread_high_bps':                                     420,
        'signals.regime.yield_curve_inversion':                                  -0.08,
        'signals.regime.ism_expansion':                                          50.5,
        'signals.regime.regime_weights.risk_on.cross_sectional_momentum':        1.0,
        'signals.regime.regime_weights.risk_on.ts_momentum':                     1.0,
        'signals.regime.regime_weights.risk_on.relative_value':                  1.0,
        'signals.regime.regime_weights.risk_off.cross_sectional_momentum':       0.3,
        'signals.regime.regime_weights.risk_off.ts_momentum':                    0.5,
        'signals.regime.regime_weights.risk_off.relative_value':                 1.5,
        'signals.regime.regime_weights.transition_up.cross_sectional_momentum':  1.1,
        'signals.regime.regime_weights.transition_up.ts_momentum':               1.0,
        'signals.regime.regime_weights.transition_up.relative_value':            0.9,
        'signals.regime.regime_weights.transition_down.cross_sectional_momentum': 0.4,
        'signals.regime.regime_weights.transition_down.ts_momentum':             0.6,
        'signals.regime.regime_weights.transition_down.relative_value':          1.4,
        'signals.regime.defensive_bonus_risk_off':                               0.50,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP G — REBALANCE & TRANSACTION COST
    # 变化调仓频率（monthly/biweekly）、z-score 变化门槛（触发调仓的最低
    # 信号变动）和月度最大换手率上限。
    # GP2013: 最优交易速度取决于信号半衰期与交易成本的权衡。
    # GK2000: 最小化信号捕捉滞后是提升 IR 的关键运营决策。
    # ════════════════════════════════════════════════════════════════════════

    # G1: 低换手（Garleanu-Pedersen 成本最优）。
    # GP2013 最优交易公式：信号半衰期长时，高门槛策略净成本更低；
    # 板块轮换信号半衰期通常 3-6 个月，threshold=0.8σ 与之匹配。
    # max_monthly_turnover=0.45 限制单月单侧换手，减少 ETF 买卖价差成本。
    # last_trading_day 调仓减少月底流动性溢价（机构季末操作）。
    'low_turnover': {
        'signals.weights.cross_sectional_momentum':    0.38,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.25,
        'signals.weights.regime_adjustment':           0.22,
        'signals.cs_momentum.lookback_months':         15,
        'rebalance.frequency':                         'monthly',
        'rebalance.rebalance_day':                     'last_trading_day',
        'rebalance.zscore_change_threshold':           0.8,
        'rebalance.max_monthly_turnover':              0.45,
        'portfolio.top_n_sectors':                     4,
        'risk.vol_scaling.target_vol_annual':          0.12,
    },

    # G2: 高响应（GK2000 信号滞后最小化）。
    # GK2000 基本定律：信号利用率下降直接降低 IR；threshold=0.2σ 使策略
    # 对小幅信号变化立即响应，最大化信号捕捉率。
    # skip=0 + lookback=9 月信号在高响应框架下能捕捉季度级轮换节奏。
    # max_turnover=1.0 放开限制，让信号自由驱动换手。
    'responsive': {
        'signals.weights.cross_sectional_momentum':    0.45,
        'signals.weights.ts_momentum':                 0.20,
        'signals.weights.relative_value':              0.15,
        'signals.weights.regime_adjustment':           0.20,
        'signals.cs_momentum.lookback_months':         9,
        'signals.cs_momentum.skip_months':             0,
        'rebalance.frequency':                         'monthly',
        'rebalance.zscore_change_threshold':           0.2,
        'rebalance.max_monthly_turnover':              1.0,
        'portfolio.top_n_sectors':                     4,
        'portfolio.min_zscore':                        -0.5,
        'risk.vol_scaling.target_vol_annual':          0.13,
    },

    # G3: 双周调仓 + 受控换手。
    # GP2013 发现信号半衰期短时更频繁调仓可提升 IR；
    # 9 月 skip=0 信号半衰期约 2-3 个月，双周调仓减少平均信号延迟约 7 天。
    # max_monthly_turnover=0.60 防止双周频率引发过度换手；
    # estimation_window=10 天与双周调仓周期匹配，使波动率估计更及时。
    'biweekly_controlled': {
        'signals.weights.cross_sectional_momentum':    0.43,
        'signals.weights.ts_momentum':                 0.20,
        'signals.weights.relative_value':              0.17,
        'signals.weights.regime_adjustment':           0.20,
        'signals.cs_momentum.lookback_months':         9,
        'signals.cs_momentum.skip_months':             0,
        'signals.cs_momentum.zscore_window':           30,
        'rebalance.frequency':                         'biweekly',
        'rebalance.zscore_change_threshold':           0.5,
        'rebalance.max_monthly_turnover':              0.60,
        'portfolio.constraints.beta_min':              0.70,
        'portfolio.constraints.beta_max':              1.10,
        'risk.vol_scaling.estimation_window':          10,
    },

    # G4: 超选择性（Garleanu-Pedersen 极高信号门槛）。
    # GP2013 极端成本约束场景：只有 z-score 变化超过 1.0σ 才执行调仓，
    # 确保每次换手都有充分的信号支撑，年均调仓次数约 2-4 次。
    # cov.lookback_days=504 与长信号窗口匹配，提供稳定的协方差估计。
    # max_monthly_turnover=0.40 与高门槛协同，确保整体换手率极低。
    'ultra_selective': {
        'signals.weights.cross_sectional_momentum':    0.30,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.30,
        'signals.weights.regime_adjustment':           0.25,
        'signals.cs_momentum.lookback_months':         15,
        'signals.cs_momentum.skip_months':             1,
        'signals.cs_momentum.zscore_window':           48,
        'rebalance.frequency':                         'monthly',
        'rebalance.zscore_change_threshold':           1.0,
        'rebalance.max_monthly_turnover':              0.40,
        'portfolio.cov.lookback_days':                 504,
        'portfolio.weight_scheme':                     'rank',
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP H — VIX DE-RISK ARCHITECTURE
    # 测试二元切换 vs 渐进式降风险，以及不同 VIX 触发门槛对尾部保护
    # 与正常市场表现的权衡。W2009: VIX>35 是恐慌极端值；>25 为压力区间。
    # ════════════════════════════════════════════════════════════════════════

    # H1: 经典二元切换，VIX=35。
    # W2009 VIX 恐惧指数理论：VIX>35 对应市场极端恐慌状态，是切换现金的
    # 传统触发点；缺点是 VIX 30-35 区间完全无梯度缓冲，一次性切换。
    # scale_threshold=1.5 与默认一致，不额外叠加波动率缩放激进化。
    'binary_derisk_35': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'risk.vix_progressive_derisk.enabled':         False,
        'rebalance.emergency_derisk_vix':              35.0,
        'rebalance.emergency_cash_pct':                0.50,
        'risk.vol_scaling.target_vol_annual':          0.12,
        'risk.vol_scaling.scale_threshold':            1.5,
        'portfolio.constraints.beta_min':              0.70,
        'portfolio.constraints.beta_max':              1.10,
    },

    # H2: 激进二元切换，VIX=28。
    # 早于默认 6-7 个 VIX 点触发，显著减少"被追到止损"风险；
    # 假触发率较高（VIX=28 在普通市场波动中也会出现），适合风险极度厌恶型投资者。
    # emergency_cash_pct=0.60 比 H1 的 0.50 更大，切换后保护力度更强。
    # cumulative_dd_halve=-0.18 额外的回撤保护层，与激进 VIX 设置协同。
    'binary_derisk_28': {
        'signals.weights.cross_sectional_momentum':    0.35,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.25,
        'signals.weights.regime_adjustment':           0.25,
        'risk.vix_progressive_derisk.enabled':         False,
        'rebalance.emergency_derisk_vix':              28.0,
        'rebalance.emergency_cash_pct':                0.60,
        'risk.vol_scaling.target_vol_annual':          0.11,
        'risk.vol_scaling.scale_threshold':            1.3,
        'portfolio.constraints.beta_min':              0.60,
        'portfolio.constraints.beta_max':              1.00,
        'risk.drawdown.cumulative_dd_halve':           -0.18,
    },

    # H3: 渐进式（当前生产配置）。
    # 梯度降风险避免大幅切换带来的市场冲击：VIX>28→15%现金，
    # VIX>32→35%现金，VIX≥35→应急50%现金（三层梯度）。
    # 第一档提高至 VIX>28 修复了旧 VIX>25 的高误判率问题。
    'progressive_current': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'risk.vix_progressive_derisk.enabled':         True,
        'risk.vix_progressive_derisk.tiers':           [{'vix_above': 28, 'cash_pct': 0.15},
                                                        {'vix_above': 32, 'cash_pct': 0.35}],
        'rebalance.emergency_derisk_vix':              35.0,
        'rebalance.emergency_cash_pct':                0.50,
        'risk.vol_scaling.target_vol_annual':          0.12,
        'risk.vol_scaling.scale_threshold':            1.5,
        'portfolio.constraints.beta_min':              0.70,
        'portfolio.constraints.beta_max':              1.10,
    },

    # H4: 保守渐进式。
    # W2009 VIX 均值回归：VIX<30 的高波通常在 2-3 周内回归，过早降险
    # 会错过快速反弹行情；VIX>30 才开始第一档（10%现金）是保守倾向。
    # 第二档 VIX>36 仅 25% 现金，应急门槛提高至 40，整体杠杆更稳定。
    # scale_threshold=1.8 / target=0.14 给动量更多运行空间。
    'progressive_conservative': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'risk.vix_progressive_derisk.enabled':         True,
        'risk.vix_progressive_derisk.tiers':           [{'vix_above': 30, 'cash_pct': 0.10},
                                                        {'vix_above': 36, 'cash_pct': 0.25}],
        'rebalance.emergency_derisk_vix':              40.0,
        'rebalance.emergency_cash_pct':                0.50,
        'risk.vol_scaling.target_vol_annual':          0.14,
        'risk.vol_scaling.scale_threshold':            1.8,
        'portfolio.constraints.beta_min':              0.65,
        'portfolio.constraints.beta_max':              1.15,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP I — VOLATILITY SCALING SCIENCE
    # 系统变化波动率目标（target_vol_annual）、缩放触发门槛（scale_threshold）、
    # 估计窗口（estimation_window）和历史均值窗口（historical_window）。
    # MM2017: 动量策略的最优目标波动率约 7-8%；BSC2015: 短窗口估计更有效。
    # ════════════════════════════════════════════════════════════════════════

    # I1: 关闭波动率缩放（对照组）。
    # 测试 MM2017/BSC2015 的核心命题：vol_scaling 对 Sharpe 是否有正贡献？
    # 保留渐进 VIX 降风险和回撤保护作为替代风险管理机制；
    # 若关闭后 Sharpe 下降 → vol_scaling 有价值；若不降 → 可能是多余摩擦。
    'no_vol_scaling': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'risk.vol_scaling.enabled':                    False,
        'risk.vix_progressive_derisk.enabled':         True,
        'risk.vix_progressive_derisk.tiers':           [{'vix_above': 26, 'cash_pct': 0.15},
                                                        {'vix_above': 30, 'cash_pct': 0.35}],
        'risk.drawdown.cumulative_dd_halve':           -0.18,
        'risk.drawdown.cumulative_dd_recovery':        -0.09,
        'portfolio.constraints.beta_min':              0.65,
        'portfolio.constraints.beta_max':              1.05,
    },

    # I2: 严格波动率目标 8%。
    # MM2017: 动量策略波动率管理的最优目标约 7-8%，实证上能最大化 Sharpe；
    # BSC2015: estimation_window=15 天的短窗口提供更准确的前向波动率预测。
    # scale_threshold=1.3 × 降低至 1.3 使缩放更敏感，配合 8% 低目标。
    # beta_min=0.55 允许在低目标下组合自然低 beta 化而不被强制约束。
    'tight_vol_8pct': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'risk.vol_scaling.enabled':                    True,
        'risk.vol_scaling.target_vol_annual':          0.08,
        'risk.vol_scaling.estimation_window':          15,
        'risk.vol_scaling.scale_threshold':            1.3,
        'risk.vol_scaling.historical_window':          252,
        'portfolio.constraints.beta_min':              0.55,
        'portfolio.constraints.beta_max':              1.05,
        'risk.drawdown.cumulative_dd_halve':           -0.20,
    },

    # I3: 标准波动率目标 12%（生产默认）。
    # 12% 年化目标与 S&P500 历史平均波动率接近，保持市场中性波动暴露；
    # scale_threshold=1.5 意味着只有波动率超过历史均值 50% 才缩仓，
    # 减少低波牛市中的误缩仓（estimation_window=20 天标准设置）。
    'standard_vol_target': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'risk.vol_scaling.enabled':                    True,
        'risk.vol_scaling.target_vol_annual':          0.12,
        'risk.vol_scaling.estimation_window':          20,
        'risk.vol_scaling.scale_threshold':            1.5,
        'risk.vol_scaling.historical_window':          252,
        'portfolio.constraints.beta_min':              0.70,
        'portfolio.constraints.beta_max':              1.10,
        'risk.drawdown.cumulative_dd_halve':           -0.20,
    },

    # I4: 宽松波动率目标 16%。
    # 给动量策略更多运行空间，允许在低波牛市中保持全额仓位；
    # scale_threshold=2.0 意味着只有波动率翻倍才触发缩仓，几乎不干预；
    # historical_window=504 提供更长历史均值基准，减少短期高波的误触发。
    # beta_max=1.25 允许超配高 beta 板块在扩张期充分受益。
    'relaxed_vol_target': {
        'signals.weights.cross_sectional_momentum':    0.45,
        'signals.weights.ts_momentum':                 0.20,
        'signals.weights.relative_value':              0.15,
        'signals.weights.regime_adjustment':           0.20,
        'risk.vol_scaling.enabled':                    True,
        'risk.vol_scaling.target_vol_annual':          0.16,
        'risk.vol_scaling.estimation_window':          30,
        'risk.vol_scaling.scale_threshold':            2.0,
        'risk.vol_scaling.historical_window':          504,
        'portfolio.constraints.beta_min':              0.75,
        'portfolio.constraints.beta_max':              1.4,
        'risk.drawdown.cumulative_dd_halve':           -0.22,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP J — BETA & MARKET EXPOSURE
    # 变化组合相对 SPY 的 beta 上下限约束，测试不同市场暴露定位
    # 对 alpha 生成和风险管理效率的影响。
    # FP2014: BAB 因子证明低 beta 策略具有独立的风险调整超额收益。
    # ════════════════════════════════════════════════════════════════════════

    # J1: 紧 beta 约束（类增强指数）。
    # 机构增强指数场景：beta_min=0.80 使策略与基准高度相关，
    # 跟踪误差低；GMV 优化器自然倾向低 beta 板块，配合紧约束避免漂移。
    # LW+252 天协方差估计为 GMV 提供稳定输入。
    # vol_target=0.11 略低于市场，匹配增强指数的低主动风险定位。
    'tight_beta_tracker': {
        'signals.weights.cross_sectional_momentum':    0.38,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.22,
        'signals.weights.regime_adjustment':           0.25,
        'portfolio.optimizer':                         'gmv',
        'portfolio.cov.method':                        'ledoit_wolf',
        'portfolio.cov.lookback_days':                 252,
        'portfolio.constraints.beta_min':              0.80,
        'portfolio.constraints.beta_max':              1.05,
        'portfolio.constraints.max_weight':            0.40,
        'risk.vol_scaling.target_vol_annual':          0.11,
    },

    # J2: 标准 beta 约束（生产默认）。
    # 0.70-1.10 的约束范围给优化器足够自由度执行板块轮换，同时防止
    # 策略系统性偏离市场暴露；inv_vol + LW 是最稳健的默认配置。
    'standard_beta': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'portfolio.optimizer':                         'inv_vol',
        'portfolio.cov.method':                        'ledoit_wolf',
        'portfolio.constraints.beta_min':              0.70,
        'portfolio.constraints.beta_max':              1.10,
        'portfolio.constraints.max_weight':            0.40,
        'portfolio.top_n_sectors':                     4,
        'risk.vol_scaling.target_vol_annual':          0.12,
    },

    # J3: 低 beta 策略（Betting Against Beta）。
    # FP2014 "Betting Against Beta"：低 beta 资产相对于 CAPM 预测具有正 alpha；
    # 在杠杆约束存在时，高风险溢价流向低 beta 板块（XLU/XLP/XLV）。
    # GMV + lookback=504 倾向自然低 beta 板块；RV 权重提至 0.32，
    # 因低 beta 板块（防御）历史上估值更保守，价值信号与 beta 选择协同。
    # vol_target=0.08 与低 beta 低波目标一致，scale_threshold=1.3 敏感。
    'low_beta_bab': {
        'signals.weights.cross_sectional_momentum':    0.28,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.32,
        'signals.weights.regime_adjustment':           0.25,
        'portfolio.optimizer':                         'gmv',
        'portfolio.cov.method':                        'ledoit_wolf',
        'portfolio.cov.lookback_days':                 504,
        'portfolio.constraints.beta_min':              0.40,
        'portfolio.constraints.beta_max':              0.82,
        'portfolio.constraints.max_weight':            0.40,
        'portfolio.top_n_sectors':                     5,
        'risk.vol_scaling.target_vol_annual':          0.08,
        'risk.vol_scaling.scale_threshold':            1.3,
        'signals.regime.defensive_bonus_risk_off':     0.35,
    },

    # J4: 高 beta 成长策略。
    # FP2014 对应面：资金充裕的无约束账户可在扩张期选择性超配高 beta 板块
    # 获取额外风险溢价（XLK/XLC/XLY 经济扩张期 beta 约 1.2-1.4）。
    # crash_filter=0.5 保留部分 TS 负动量板块，防止高 beta 配置中过激排除。
    # scale_threshold=2.0 给高 beta 仓位充分运行空间，只在极端波动时缩仓。
    'high_beta_growth': {
        'signals.weights.cross_sectional_momentum':    0.48,
        'signals.weights.ts_momentum':                 0.20,
        'signals.weights.relative_value':              0.12,
        'signals.weights.regime_adjustment':           0.20,
        'portfolio.optimizer':                         'inv_vol',
        'portfolio.cov.method':                        'ledoit_wolf',
        'portfolio.constraints.beta_min':              0.90,
        'portfolio.constraints.beta_max':              1.52,
        'portfolio.constraints.max_weight':            0.45,
        'signals.ts_momentum.crash_filter_multiplier': 0.5,
        'risk.vol_scaling.target_vol_annual':          0.16,
        'risk.vol_scaling.scale_threshold':            2.0,
        'portfolio.top_n_sectors':                     4,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP K — DRAWDOWN CIRCUIT BREAKER
    # 变化累计回撤触发阈值（cumulative_dd_halve）和恢复门槛
    # （cumulative_dd_recovery）。
    # GZ1993: 最优 floor 约束取决于投资者的风险厌恶系数和投资期限。
    # ════════════════════════════════════════════════════════════════════════

    # K1: 敏感回撤保护。
    # GZ1993 最优 floor 约束：短期投资者（1-2 年期）最优 floor 在 -10% 到 -15%；
    # -12% 触发对应正常熊市调整，适合风险厌恶型短期账户；
    # vol_target=0.10 + scale_threshold=1.3 配合早触发，多层下行保护。
    # beta_max=1.00 防止超配高 beta 板块在回撤期放大损失。
    'sensitive_dd': {
        'signals.weights.cross_sectional_momentum':    0.35,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.25,
        'signals.weights.regime_adjustment':           0.25,
        'risk.drawdown.monthly_dd_alert':              -0.05,
        'risk.drawdown.cumulative_dd_halve':           -0.12,
        'risk.drawdown.cumulative_dd_recovery':        -0.06,
        'risk.vol_scaling.target_vol_annual':          0.10,
        'risk.vol_scaling.scale_threshold':            1.3,
        'portfolio.constraints.beta_min':              0.60,
        'portfolio.constraints.beta_max':              1.00,
        'portfolio.constraints.max_weight':            0.38,
    },

    # K2: 标准回撤保护（生产默认）。
    # Cvitanic-Karatzas(1995) 最优消费问题：-20% 是历史大熊市（非 COVID 量级）
    # 的常见最大回撤水平；-10% 恢复门槛防止过早重建仓位延长亏损期。
    'standard_dd': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'risk.drawdown.monthly_dd_alert':              -0.08,
        'risk.drawdown.cumulative_dd_halve':           -0.20,
        'risk.drawdown.cumulative_dd_recovery':        -0.10,
        'risk.vol_scaling.target_vol_annual':          0.12,
        'portfolio.constraints.beta_min':              0.70,
        'portfolio.constraints.beta_max':              1.10,
        'portfolio.top_n_sectors':                     4,
    },

    # K3: 耐心型回撤保护（长期投资者）。
    # GZ1993 长期投资者（5-10 年）最优 floor 更低（-25% 到 -30%），
    # 因为时间多样化（time diversification）允许承受更大临时回撤。
    # -28% 仅在类 COVID 级别事件时触发；scale_threshold=1.7 宽松；
    # beta_max=1.18 允许长期持有高 beta 成长板块。
    'patient_dd': {
        'signals.weights.cross_sectional_momentum':    0.42,
        'signals.weights.ts_momentum':                 0.18,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.20,
        'risk.drawdown.monthly_dd_alert':              -0.10,
        'risk.drawdown.cumulative_dd_halve':           -0.28,
        'risk.drawdown.cumulative_dd_recovery':        -0.14,
        'risk.vol_scaling.target_vol_annual':          0.13,
        'risk.vol_scaling.scale_threshold':            1.7,
        'portfolio.constraints.beta_min':              0.68,
        'portfolio.constraints.beta_max':              1.18,
        'portfolio.top_n_sectors':                     4,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP L — MARKET REGIME ARCHETYPES
    # 面向特定宏观市场环境的完整参数组合，多子模块协同设计。
    # 每组均经过内在一致性检查：信号权重 =1.0，VIX 与降风险设置协调，
    # beta / vol 约束与市场环境逻辑相符。参数数量 20-24 个/组。
    # ════════════════════════════════════════════════════════════════════════

    # L1: 科技牛市档案（2023 型，XLK +57%）。
    # 2023 AI 浪潮驱动 XLK 暴涨：需最大动量敏感度才能充分受益。
    # crash_filter=1.0 防止 XLK 因短期震荡被 TS 过滤排除；
    # skip=0 保留近月极强信号；lookback=9 月捕捉 AI 叙事驱动的季度级轮换。
    # beta_max=1.30 允许超配高 beta 成长板块；vol_target=0.16 给动量充分运行空间。
    # transition_up 时 cs_mom×1.4 + ts_mom×1.2 放大反弹捕捉力度。
    # scale_threshold=2.0 防止低波牛市中误缩仓错过上涨。
    'tech_bull_2023': {
        'signals.weights.cross_sectional_momentum':    0.52,
        'signals.weights.ts_momentum':                 0.20,
        'signals.weights.relative_value':              0.08,
        'signals.weights.regime_adjustment':           0.20,
        'signals.cs_momentum.lookback_months':         9,
        'signals.cs_momentum.skip_months':             0,
        'signals.cs_momentum.zscore_window':           30,
        'signals.ts_momentum.lookback_months':         9,
        'signals.ts_momentum.crash_filter_multiplier': 1.0,
        'signals.acceleration.enabled':                True,
        'signals.acceleration.lookback_months':        2,
        'signals.acceleration.weight_boost':           0.10,
        'signals.regime.vix_high_threshold':           28.0,
        'signals.regime.regime_weights.transition_up.cross_sectional_momentum': 1.4,
        'signals.regime.regime_weights.transition_up.ts_momentum':              1.2,
        'portfolio.constraints.beta_min':              0.85,
        'portfolio.constraints.beta_max':              1.50,
        'portfolio.constraints.max_weight':            0.45,
        'portfolio.top_n_sectors':                     4,
        'risk.vol_scaling.target_vol_annual':          0.16,
        'risk.vol_scaling.scale_threshold':            2.0,
        'rebalance.emergency_derisk_vix':              40.0,
        'risk.vix_progressive_derisk.enabled':         True,
        'risk.vix_progressive_derisk.tiers':           [{'vix_above': 35, 'cash_pct': 0.10},
                                                        {'vix_above': 40, 'cash_pct': 0.25}],
        # STM: 6月中期动量补充9月回看的盲区，提前捕捉板块轮换
        'signals.short_term_momentum.enabled':         True,
        'signals.short_term_momentum.weight_bonus':    0.06,
    },

    # L2: 危机避险档案（2020 / 2022 型系统性风险）。
    # REG=0.42 主导决策；宁可错过复苏初期行情也要在下行时充分保护。
    # vix_high=20 极早触发；HY=380bps 在信用市场初现压力时即响应。
    # crash_filter=0.0 完全排除负 TS 板块；defensive_bonus=0.50σ 最大化防御轮换。
    # risk_off 时 cs_mom=0.35（极低）防止追强拥挤动量；RV=1.45 主导配置。
    # VIX 渐进降风险提前至 24/28，应急门槛 30；vol_target=0.08 超低；
    # beta_max=0.85 系统性低市场暴露；cumulative_dd_halve=-0.12 早保护。
    'crisis_defense': {
        'signals.weights.cross_sectional_momentum':    0.22,
        'signals.weights.ts_momentum':                 0.16,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.42,
        'signals.cs_momentum.lookback_months':         12,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.regime.vix_high_threshold':           20.0,
        'signals.regime.vix_extreme_threshold':        30.0,
        'signals.regime.hy_spread_high_bps':           380,
        'signals.regime.yield_curve_inversion':        -0.05,
        'signals.regime.defensive_bonus_risk_off':     0.50,
        'signals.regime.regime_weights.risk_off.cross_sectional_momentum':        0.35,
        'signals.regime.regime_weights.risk_off.ts_momentum':                     0.55,
        'signals.regime.regime_weights.risk_off.relative_value':                  1.45,
        'signals.regime.regime_weights.transition_down.cross_sectional_momentum': 0.45,
        'signals.regime.regime_weights.transition_down.relative_value':           1.35,
        'risk.vix_progressive_derisk.enabled':         True,
        'risk.vix_progressive_derisk.tiers':           [{'vix_above': 24, 'cash_pct': 0.20},
                                                        {'vix_above': 28, 'cash_pct': 0.40}],
        'rebalance.emergency_derisk_vix':              30.0,
        'portfolio.constraints.beta_min':              0.45,
        'portfolio.constraints.beta_max':              0.85,
        'risk.vol_scaling.target_vol_annual':          0.08,
        'risk.vol_scaling.scale_threshold':            1.2,
        'risk.drawdown.cumulative_dd_halve':           -0.12,
    },

    # L3: 滞胀档案（2022 型：高通胀 + 低增长）。
    # 滞胀期能源/材料/防御板块兼具相对价值和绝对强度；RV=0.35 重要性提升。
    # pe_lookback_years=7 减少十年均值中历史低通胀时期的权重偏差。
    # 慢速信号（lookback=15, skip=1, zscore_window=48）减少高波动率下噪音触发。
    # crash_filter=0.3 保留一定弱板块暴露，避免价值陷阱板块被过滤。
    # vix_high=28 + HY=480 避免把滞胀期的常态高波动误判为纯风险事件。
    # beta_max=0.92 体现滞胀期的整体防御倾向；top_n=5 提升多样化。
    'stagflation': {
        'signals.weights.cross_sectional_momentum':    0.28,
        'signals.weights.ts_momentum':                 0.12,
        'signals.weights.relative_value':              0.35,
        'signals.weights.regime_adjustment':           0.25,
        'signals.cs_momentum.lookback_months':         15,
        'signals.cs_momentum.skip_months':             1,
        'signals.cs_momentum.zscore_window':           48,
        'signals.ts_momentum.lookback_months':         15,
        'signals.ts_momentum.crash_filter_multiplier': 0.3,
        'signals.value.pe_lookback_years':             7,
        'signals.regime.vix_high_threshold':           28.0,
        'signals.regime.hy_spread_high_bps':           480,
        'signals.regime.yield_curve_inversion':        -0.12,
        'signals.regime.regime_weights.risk_off.cross_sectional_momentum': 0.7,
        'signals.regime.regime_weights.risk_off.relative_value':           1.3,
        'signals.regime.defensive_bonus_risk_off':     0.30,
        'portfolio.optimizer':                         'inv_vol',
        'portfolio.cov.lookback_days':                 252,
        'portfolio.constraints.beta_min':              0.50,
        'portfolio.constraints.beta_max':              0.92,
        'portfolio.top_n_sectors':                     5,
        # ERM: 滞胀期盈利分化大，ERM识别抗滞胀板块(能源/材料EPS逆势增长)
        'signals.earnings_revision.enabled':           True,
        'signals.earnings_revision.weight_bonus':      0.05,
    },

    # L4: 加息周期档案（2022 型：史上最快 Fed 加息）。
    # 利率上行期：XLRE/XLU 受久期拖累，XLE/XLV/XLF 相对强；
    # HY 利差在加息期容易走宽（企业信用压力），HY=420 低门槛早触发。
    # yield_curve_inversion=-0.08 对倒挂的早期信号敏感。
    # defensive_bonus=0.40 在 risk_off 时提前加速防御轮换；
    # transition_down 时 cs_mom=0.60 + RV=1.25 保护性倾向。
    # vol_target=0.09 + scale_threshold=1.2 主动控制加息期高不确定性。
    # beta_max=0.92 + top_n=5 增加板块多样化，对冲利率风险集中性。
    'rate_hike_cycle': {
        'signals.weights.cross_sectional_momentum':    0.35,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.25,
        'signals.weights.regime_adjustment':           0.25,
        'signals.cs_momentum.lookback_months':         12,
        'signals.cs_momentum.skip_months':             1,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.value.pe_lookback_years':             10,
        'signals.regime.vix_high_threshold':           22.0,
        'signals.regime.hy_spread_high_bps':           420,
        'signals.regime.yield_curve_inversion':        -0.08,
        'signals.regime.defensive_bonus_risk_off':     0.40,
        'signals.regime.regime_weights.risk_off.cross_sectional_momentum':        0.55,
        'signals.regime.regime_weights.risk_off.relative_value':                  1.40,
        'signals.regime.regime_weights.transition_down.cross_sectional_momentum': 0.60,
        'signals.regime.regime_weights.transition_down.relative_value':           1.25,
        'portfolio.constraints.beta_min':              0.55,
        'portfolio.constraints.beta_max':              0.92,
        'risk.vol_scaling.target_vol_annual':          0.09,
        'risk.vol_scaling.scale_threshold':            1.2,
        'portfolio.top_n_sectors':                     5,
    },

    # L5: 复苏前期档案（2020 Q4 / 2023 H1 型：XLY/XLF/XLI 率先反弹）。
    # 复苏初期：周期性板块领先，需要快速捕获轮动领先板块。
    # skip=0 + lookback=9 月最大化响应速度；双周调仓减少平均信号延迟。
    # crash_filter=1.0 不排除 TS 负板块，因为复苏初期很多板块尚负 TS。
    # acceleration weight_boost=0.09 重奖近期加速板块；2 月加速窗口匹配。
    # transition_up 时 cs_mom=1.4 + ts_mom=1.2 + RV=0.65：大幅放大动量，
    # 压低价值（复苏期价值因子相对表现弱）。
    # risk_on cs_mom=1.1 确保确认扩张后动量持续主导；zscore_threshold=0.25。
    # beta_max=1.30 允许高 beta 复苏板块充分受益；vol_target=0.15 宽松。
    'early_recovery': {
        'signals.weights.cross_sectional_momentum':    0.47,
        'signals.weights.ts_momentum':                 0.20,
        'signals.weights.relative_value':              0.13,
        'signals.weights.regime_adjustment':           0.20,
        'signals.cs_momentum.lookback_months':         9,
        'signals.cs_momentum.skip_months':             0,
        'signals.cs_momentum.zscore_window':           30,
        'signals.ts_momentum.lookback_months':         9,
        'signals.ts_momentum.crash_filter_multiplier': 1.0,
        'signals.acceleration.enabled':                True,
        'signals.acceleration.lookback_months':        2,
        'signals.acceleration.weight_boost':           0.09,
        'signals.regime.regime_weights.transition_up.cross_sectional_momentum': 1.4,
        'signals.regime.regime_weights.transition_up.ts_momentum':              1.2,
        'signals.regime.regime_weights.transition_up.relative_value':           0.65,
        'signals.regime.regime_weights.risk_on.cross_sectional_momentum':       1.1,
        'rebalance.frequency':                         'biweekly',
        'rebalance.zscore_change_threshold':           0.25,
        'portfolio.constraints.beta_min':              0.75,
        'portfolio.constraints.beta_max':              1.40,
        'portfolio.top_n_sectors':                     5,
        'risk.vol_scaling.target_vol_annual':          0.15,
        # STM: 复苏期快速追涨，6月中期动量识别板块轮换
        'signals.short_term_momentum.enabled':         True,
        'signals.short_term_momentum.weight_bonus':    0.06,
    },

    # L6: 低波动慢牛档案（2017 / 2019 型：VIX<15 长期低波）。
    # 低波动率扩张期：板块轮换缓慢，控制成本是 alpha 的重要来源。
    # vol_target=0.08 在低波期几乎不触发缩仓，保持全额投资；
    # zscore_threshold=0.75 + max_monthly_turnover=0.45 大幅降低换手成本。
    # last_trading_day 调仓减少月底流动性溢价；top_n=5 适度分散。
    # lookback=12, skip=1, zscore_window=36 保留稳健的标准信号设置。
    # acceleration weight_boost=0.04 温和奖励，不增加额外换手。
    # beta_max=1.08 防止低波牛市末期集中超配成长板块积累尾部风险。
    'low_vol_grind': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'signals.cs_momentum.lookback_months':         12,
        'signals.cs_momentum.skip_months':             1,
        'signals.cs_momentum.zscore_window':           36,
        'signals.ts_momentum.lookback_months':         12,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.acceleration.enabled':                True,
        'signals.acceleration.lookback_months':        3,
        'signals.acceleration.weight_boost':           0.04,
        'rebalance.frequency':                         'monthly',
        'rebalance.rebalance_day':                     'last_trading_day',
        'rebalance.zscore_change_threshold':           0.75,
        'rebalance.max_monthly_turnover':              0.45,
        'portfolio.top_n_sectors':                     5,
        'portfolio.constraints.beta_min':              0.65,
        'portfolio.constraints.beta_max':              1.08,
        'risk.vol_scaling.target_vol_annual':          0.08,
    },

    # ════════════════════════════════════════════════════════════════════════
    # GROUP M — ISOLATED FACTOR TESTS
    # 孤立测试单一参数维度的边际贡献，其余参数尽量保持与 A1 default 一致。
    # 目的：提供纯净的单变量对照实验，量化 value_source 和 acceleration
    # 的独立 alpha 贡献（两个在其他组中从未被孤立测试的关键参数）。
    # ════════════════════════════════════════════════════════════════════════

    # M1: 相对价值 — constituents TTM P/E（精确数据，对照组）。
    # 孤立测试 value_source='constituents'：与 M2 唯一区别是数据源。
    # constituents 方法通过 yfinance 季报 EPS 重建各板块成分股 TTM P/E，
    # 准确反映当前估值水平（2023年 XLK 平均 P/E 约 30×）；
    # 首次运行需下载历史 EPS，后续缓存。对比 M2 可量化数据质量贡献。
    'value_constituents': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'signals.value_source':                        'constituents',
        'signals.value.pe_lookback_years':             10,
        'signals.cs_momentum.lookback_months':         12,
        'signals.cs_momentum.skip_months':             1,
        'signals.cs_momentum.zscore_window':           36,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.acceleration.enabled':                True,
    },

    # M2: 相对价值 — proxy 价格代理（快速，无需额外数据）。
    # 与 M1 唯一区别是 value_source='proxy'。
    # proxy = (当前价格 / 5年均价 - 1)，不依赖 EPS 数据；
    # 在生产日报中可降低 API 调用量并加速信号生成；
    # 若 M2 vs M1 Sharpe 差异小，说明 proxy 是低成本的有效替代；
    # 若 M1 显著更优，则精确 P/E 数据的额外成本是值得的。
    'value_proxy': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'signals.value_source':                        'proxy',
        'signals.value.pe_lookback_years':             10,
        'signals.cs_momentum.lookback_months':         12,
        'signals.cs_momentum.skip_months':             1,
        'signals.cs_momentum.zscore_window':           36,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.acceleration.enabled':                True,
    },

    # M3: 加速因子开启（对照组）。
    # 孤立测试 acceleration 因子：与 M4 唯一区别是 enabled=True vs False。
    # 3 月加速窗口捕捉近期动量加速，weight_boost=0.05 对加速板块奖励约 0.05σ。
    # 理论依据：短期动量加速（Grinblatt-Han 2005）可作为中期动量的领先指标。
    # 若 M3 Sharpe > M4：加速因子有正向 alpha，值得保留并可考虑提高 weight_boost。
    'acceleration_on': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'signals.cs_momentum.lookback_months':         12,
        'signals.cs_momentum.skip_months':             1,
        'signals.cs_momentum.zscore_window':           36,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.acceleration.enabled':                True,
        'signals.acceleration.lookback_months':        3,
        'signals.acceleration.weight_boost':           0.05,
    },

    # M4: 加速因子关闭（对照组）。
    # 与 M3 唯一区别是 acceleration.enabled=False，消除加速信号的全部影响。
    # 若 M3 vs M4 Sharpe 无显著差异 → 加速因子贡献有限，可简化策略；
    # 若 M4 换手率更低但收益相近 → 加速因子带来不必要的换手摩擦；
    # Grinblatt-Han(2005): 短期加速与长期动量的交互效应因宇宙大小而异。
    'acceleration_off': {
        'signals.weights.cross_sectional_momentum':    0.40,
        'signals.weights.ts_momentum':                 0.15,
        'signals.weights.relative_value':              0.20,
        'signals.weights.regime_adjustment':           0.25,
        'signals.cs_momentum.lookback_months':         12,
        'signals.cs_momentum.skip_months':             1,
        'signals.cs_momentum.zscore_window':           36,
        'signals.ts_momentum.crash_filter_multiplier': 0.0,
        'signals.acceleration.enabled':                False,
        'portfolio.top_n_sectors':                     4,
        # STM: acceleration关闭时，STM作为更强的中期动量替代(Grinblatt-Han2005)
        'signals.short_term_momentum.enabled':         True,
        'signals.short_term_momentum.weight_bonus':    0.05,
    },

}


# ---------------------------------------------------------------------------
# Convenience: list all sets with brief description
# ---------------------------------------------------------------------------

_PARAM_SET_DESCRIPTIONS: Dict[str, str] = {
    # Group A — Signal Factor Architecture
    'default':                    'A1 — 生产基准：JT1993 12-1月，CS=0.40 TS=0.15 RV=0.20 REG=0.25 (12 params)',
    'momentum_heavy':             'A2 — 动量超配：AMP2013，CS+TS=0.70，9月，risk_off/td乘数补全 (16 params)',
    'value_tilt':                 'A3 — 价值倾斜：FF1992，RV=0.35，15月慢速，crash_filter=0.3 (14 params)',
    'regime_driven':              'A4 — 机制驱动：AB2007，REG=0.40，vix=22，VIX渐进22/26 (18 params)',
    'ts_dominant':                'A5 — 时序主导：MOP2012，TS=0.30，vol=10%，beta_max=0.95 (16 params)',
    'balanced_four':              'A6 — 四因子均等：DGU2009，各0.25，zscore_softmax (12 params)',
    # Group B — Momentum Microstructure
    'fast_momentum':              'B1 — 快速信号：JT2001，lookback=6，skip=0，zscore_win=24 (15 params)',
    'medium_momentum':            'B2 — 中速信号：9-1月折中，zscore_win=30，threshold=0.4 (12 params)',
    'no_skip_medium':             'B3 — 无跳过中速：9-0月，孤立skip效应（vs B2 9-1） (12 params)',
    'slow_momentum':              'B4 — 慢速信号：Asness1997，15-2月，cov=504天 (13 params)',
    'skip_heavy':                 'B5 — 强反转过滤：JT2001，skip=2，crash_filter=0.5 (11 params)',
    # Group C — Momentum Crash Protection
    'full_crash_filter_tight_vol':'C1 — DM2016两层防御：crash=0.0，vol=10%，VIX梯度25/30 (15 params)',
    'partial_filter_scaled':      'C2 — BSC2015 vol主导：crash=0.5，vol=9%，dd=-0.18 (13 params)',
    'vol_crash_shield':           'C3 — MM2017超紧盾牌：vol=7%，win=10天，VIX24/28梯度 (15 params)',
    'no_filter_vol_only':         'C4 — 纯vol测试：crash=1.0，vol=9%（量化crash_filter贡献）(11 params)',
    # Group D — Portfolio Construction Theory
    'inv_vol_lw':                 'D1 — inv_vol+LW收缩：LW2004，lookback=252（生产默认）(12 params)',
    'risk_parity_lw':             'D2 — ERC+LW：MRT2010，top_n=5，min_zscore=-0.3 (15 params)',
    'gmv_lw':                     'D3 — GMV+LW：CD2006，lookback=504，beta_max=0.95 (13 params)',
    'equal_weight_optimizer':     'D4 — 等权重：DGU2009，top_n=5，max_w=0.30（去优化基准）(12 params)',
    'inv_vol_oas':                'D5 — inv_vol+OAS：Chen2010，lookback=126，zscore_softmax (11 params)',
    # (D4 max_weight=0.25 修复：5板块等权=20%，0.25是合理软上限)
    # Group E — Position Concentration
    'concentrated_3':             'E1 — 高集中3板块：GK2000高IC，max_w=0.45，dd=-0.18 (14 params)',
    'standard_4':                 'E2 — 标准4板块：max_w=0.40，rank权重（默认）(10 params)',
    'diversified_5_rp':           'E3 — 分散5板块+ERC：Markowitz，max_w=0.35，softmax (12 params)',
    'broad_6_softmax':            'E4 — 宽覆盖6板块：Britten-Jones，max_w=0.28，softmax (11 params)',
    'score_gated_dynamic':        'E5 — 信号门控5板块：GK2000，min_zscore=0.25 (11 params)',
    # Group F — Regime Detection Architecture
    'hawkish_macro':              'F1 — 鹰派早触发：AB2007，vix=20/28，HY=380，18个完整机制参数',
    'standard_regime':            'F2 — 标准机制：vix=25/35，HY=450，所有12乘数完整文档化 (18 params)',
    'dovish_macro':               'F3 — 鸽派宽松：vix=28/40，HY=500，减少误判 (18 params)',
    'momentum_biased_regime':     'F4 — 动量偏置：transition_up cs_mom×1.4，emergency_vix=37 (19 params)',
    'defensive_rotation':         'F5 — 防御轮换：risk_off cs=0.3，defensive_bonus=0.50σ (18 params)',
    # Group G — Rebalance & Transaction Cost
    'low_turnover':               'G1 — 低换手：GP2013，threshold=0.8σ，max_turn=45% (11 params)',
    'responsive':                 'G2 — 高响应：GK2000，threshold=0.2σ，skip=0 (12 params)',
    'biweekly_controlled':        'G3 — 双周受控：biweekly，max_turn=60%，est_win=10天 (13 params)',
    'ultra_selective':            'G4 — 超选择性：GP2013极端成本约束，threshold=1.0σ (12 params)',
    # Group H — VIX De-risk Architecture
    'binary_derisk_35':           'H1 — 经典二元切换：W2009，VIX=35→50%现金 (11 params)',
    'binary_derisk_28':           'H2 — 激进二元切换：VIX=28→60%现金，dd=-0.18 (12 params)',
    'progressive_current':        'H3 — 渐进式（生产）：VIX 28→15%, 32→35%, 35→50% (11 params)',
    'progressive_conservative':   'H4 — 保守渐进式：W2009均值回归，VIX 30/36/40梯度 (12 params)',
    # Group I — Volatility Scaling Science
    'no_vol_scaling':             'I1 — 关闭vol_scaling：对照组测试MM2017贡献 (10 params)',
    'tight_vol_8pct':             'I2 — 严格vol=8%：MM2017最优目标，threshold=1.3 (12 params)',
    'standard_vol_target':        'I3 — 标准vol=12%：生产默认，threshold=1.5 (12 params)',
    'relaxed_vol_target':         'I4 — 宽松vol=16%：scale=2.0，beta_max=1.25 (12 params)',
    # Group J — Beta & Market Exposure
    'tight_beta_tracker':         'J1 — 紧beta(0.80-1.05)：FP2014，GMV+LW，vol=11% (11 params)',
    'standard_beta':              'J2 — 标准beta(0.70-1.10)：inv_vol+LW（默认）(11 params)',
    'low_beta_bab':               'J3 — 低beta(0.40-0.82)：FP2014 BAB，GMV，RV=0.32 (13 params)',
    'high_beta_growth':           'J4 — 高beta(0.90-1.32)：FP2014无约束，scale=2.0 (13 params)',
    # Group K — Drawdown Circuit Breaker
    'sensitive_dd':               'K1 — 敏感回撤：GZ1993短期，-12%触发，vol=10% (12 params)',
    'standard_dd':                'K2 — 标准回撤：Cvitanic-Karatzas，-20%触发（默认）(11 params)',
    'patient_dd':                 'K3 — 耐心型：GZ1993长期，-28%触发，scale=1.7 (12 params)',
    # Group L — Market Regime Archetypes
    'tech_bull_2023':             'L1 — 科技牛市2023：crash=1.0，skip=0，VIX梯度35/40延迟 (23 params)',
    'crisis_defense':             'L2 — 危机避险：REG=0.42，vix=20，HY=380，VIX24/28梯度 (22 params)',
    'stagflation':                'L3 — 滞胀：RV=0.35，15月慢速，pe_lookback=7，beta_max=0.92 (19 params)',
    'rate_hike_cycle':            'L4 — 加息周期：vix=22，HY=420，vol=9%，beta_max=0.92 (21 params)',
    'early_recovery':             'L5 — 复苏前期：biweekly，skip=0，transition_up cs×1.4 (21 params)',
    'low_vol_grind':              'L6 — 低波慢牛：vol=8%，threshold=0.75，last_trading_day (21 params)',
    # Group M — Isolated Factor Tests
    'value_constituents':         'M1 — 孤立测试：value_source=constituents TTM P/E（精确） (11 params)',
    'value_proxy':                'M2 — 孤立测试：value_source=proxy 价格代理（快速） (11 params)',
    'acceleration_on':            'M3 — 孤立测试：acceleration=True，weight_boost=0.05 (11 params)',
    'acceleration_off':           'M4 — 孤立测试：acceleration=False（对照组，量化贡献） (10 params)',
}


def list_param_sets() -> None:
    """Print all 59 parameter sets with descriptions."""
    groups = {
        'A': 'Signal Factor Architecture',
        'B': 'Momentum Microstructure',
        'C': 'Momentum Crash Protection',
        'D': 'Portfolio Construction Theory',
        'E': 'Position Concentration',
        'F': 'Regime Detection Architecture',
        'G': 'Rebalance & Transaction Cost',
        'H': 'VIX De-risk Architecture',
        'I': 'Volatility Scaling Science',
        'J': 'Beta & Market Exposure',
        'K': 'Drawdown Circuit Breaker',
        'L': 'Market Regime Archetypes',
        'M': 'Isolated Factor Tests',
    }
    current_group = None
    for name, desc in _PARAM_SET_DESCRIPTIONS.items():
        grp = desc[0]
        if grp != current_group:
            current_group = grp
            print(f"\n{'─'*72}")
            print(f"  GROUP {grp} — {groups.get(grp, '')}")
            print(f"{'─'*72}")
        n_params = len(PARAM_SETS.get(name, {}))
        print(f"  {name:<32} [{n_params:2d} params]  {desc}")
    print(f"\nTotal: {len(PARAM_SETS)} parameter sets  (A6+B5+C4+D5+E5+F5+G4+H4+I4+J4+K3+L6+M4)")


if __name__ == '__main__':
    list_param_sets()
