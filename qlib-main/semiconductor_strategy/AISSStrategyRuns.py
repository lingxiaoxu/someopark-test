"""
AISSStrategyRuns — AISS named parameter sets
============================================
Purpose-built parameter library for the AI Infra & Semiconductor Strategy.
(Replaces the inherited AISS sets, which referenced config paths — e.g.
``signals.value.*`` — that do not exist in AISS.)

Each set is a flat **dotted-path override** applied onto ``config.yaml`` via
``apply_param_set`` (list values replace the whole node, e.g. vix tiers).  The
interface (``PARAM_SETS``, ``apply_param_set``, ``get_param_set``,
``_PARAM_SET_DESCRIPTIONS``, ``list_param_sets``) is drop-in compatible with the
batch / walk-forward / smart-select / daily callers.

Group convention: each description starts with its group letter so
``AISSBatchRun._group_of`` can bucket it.

Groups
------
A  Signal-weight architecture (cs / supply_chain / capex / cycle)
B  Concentration (top_n, max_weight)
C  Volatility-scaling / sizing aggressiveness
D  VIX de-risk aggressiveness
E  Momentum window
F  Supply-chain external-data usage
G  Portfolio optimizer
H  Market archetypes (aggressive / defensive / capex blends)
M  Isolated single-factor tests
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Apply / access helpers (same semantics as AISS)
# ---------------------------------------------------------------------------

def apply_param_set(base_config: dict, param_set: dict) -> dict:
    """Apply a flat dotted-path override set onto a deep copy of base_config.

    Nested segments are created on demand; list-valued overrides replace the
    target node entirely (e.g. ``risk.vix_progressive_derisk.tiers``).
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
    if name not in PARAM_SETS:
        raise KeyError(f"Unknown AISS param set: {name!r}. Available: {sorted(PARAM_SETS)}")
    return PARAM_SETS[name]


# ---------------------------------------------------------------------------
# Weight helper (4 AISS factors must sum to 1.0)
# ---------------------------------------------------------------------------

def _w(cs: float, sc: float, cx: float, cy: float) -> dict:
    s = cs + sc + cx + cy
    assert abs(s - 1.0) < 1e-9, f"weights must sum to 1.0, got {s}"
    return {
        "signals.weights.cs_momentum": cs,
        "signals.weights.supply_chain": sc,
        "signals.weights.capex_pulse": cx,
        "signals.weights.cycle_regime": cy,
    }


# ---------------------------------------------------------------------------
# PARAM_SETS
# ---------------------------------------------------------------------------

PARAM_SETS: Dict[str, Dict[str, Any]] = {
    # ===== Group A — signal-weight architecture =========================
    "default": {},  # uses config.yaml as-is (the validated win-criterion config)
    "supply_chain_heavy": _w(0.20, 0.50, 0.20, 0.10),
    "momentum_heavy":     _w(0.45, 0.20, 0.25, 0.10),
    "capex_heavy":        _w(0.25, 0.25, 0.40, 0.10),
    "balanced_four":      _w(0.25, 0.25, 0.25, 0.25),
    "momentum_capex":     _w(0.40, 0.15, 0.40, 0.05),

    # ===== Group B — concentration ======================================
    "concentrated_2": {"portfolio.top_n_sectors": 2, "portfolio.constraints.max_weight": 0.65},
    "standard_3":     {"portfolio.top_n_sectors": 3, "portfolio.constraints.max_weight": 0.55},
    "diversified_4":  {"portfolio.top_n_sectors": 4, "portfolio.constraints.max_weight": 0.45},
    "broad_5":        {"portfolio.top_n_sectors": 5, "portfolio.constraints.max_weight": 0.35},

    # ===== Group C — volatility scaling / sizing ========================
    "vol_target_24":   {"risk.vol_scaling.target_vol_annual": 0.24},
    "vol_target_30":   {"risk.vol_scaling.target_vol_annual": 0.30},
    "vol_target_40":   {"risk.vol_scaling.target_vol_annual": 0.40},
    "no_vol_scaling":  {"risk.vol_scaling.enabled": False},
    # semivol(I-1, 2026-07-21): 下行半波动缩放——反弹期的向上波动不再压制仓位。
    # 对称市况下 semivol≈总波动/√2,target 相应折算: 0.21≈0.30/√2 为等效基线,
    # 0.18/0.24 为紧/松标定探针。经 Batch→WF→MCPS 竞争采纳,不手动切换。
    "semivol_18": {"risk.vol_scaling.downside_only": True,
                   "risk.vol_scaling.target_vol_annual": 0.18},
    "semivol_21": {"risk.vol_scaling.downside_only": True,
                   "risk.vol_scaling.target_vol_annual": 0.21},
    "semivol_24": {"risk.vol_scaling.downside_only": True,
                   "risk.vol_scaling.target_vol_annual": 0.24},
    # I-3(2026-07-21): DD 断路器曾是死代码(qlib 路径 equity_curve=None 硬编码);
    # 当日晚已修复 strategy.py 接线(自维护日收益累乘重建净值)→ 断路器复活,
    # 离底反弹释放探针随之生效: DD<-25% 但净值离本轮谷底反弹≥X% → 跳过逐日砍半
    # (崩塌下行段 rebound≈0,保护不变)。daily 实盘路径仍不传 equity_curve(选参域生效)。
    "dd_release_08": {"risk.drawdown.recovery_release_rebound": 0.08},
    "dd_release_12": {"risk.drawdown.recovery_release_rebound": 0.12},

    # ===== Group D — VIX de-risk aggressiveness =========================
    "derisk_tight": {
        "rebalance.emergency_derisk_vix": 32.0, "rebalance.emergency_cash_pct": 0.50,
        "risk.vix_progressive_derisk.tiers": [
            {"vix_above": 25, "cash_pct": 0.15}, {"vix_above": 30, "cash_pct": 0.35}],
    },
    "derisk_loose": {
        "rebalance.emergency_derisk_vix": 40.0, "rebalance.emergency_cash_pct": 0.40,
        "risk.vix_progressive_derisk.tiers": [
            {"vix_above": 32, "cash_pct": 0.10}, {"vix_above": 36, "cash_pct": 0.25}],
    },
    "no_vix_derisk": {"risk.vix_progressive_derisk.enabled": False,
                      "rebalance.emergency_derisk_vix": 99.0},

    # ===== Group E — momentum window ====================================
    "fast_momentum":   {"signals.cs_momentum.lookback_months": 6,  "signals.cs_momentum.skip_months": 0,
                        "signals.cs_momentum.zscore_window": 24},
    "standard_momentum": {"signals.cs_momentum.lookback_months": 12, "signals.cs_momentum.skip_months": 1},
    "slow_momentum":   {"signals.cs_momentum.lookback_months": 15, "signals.cs_momentum.skip_months": 1,
                        "signals.cs_momentum.zscore_window": 48},

    # ===== Group F — supply-chain external data =========================
    "external_on":  {"signals.supply_chain.use_external_macro": True},
    "external_off": {"signals.supply_chain.use_external_macro": False},

    # ===== Group G — optimizer ==========================================
    "opt_inv_vol":      {"portfolio.optimizer": "inv_vol"},
    "opt_risk_parity":  {"portfolio.optimizer": "risk_parity"},
    "opt_gmv":          {"portfolio.optimizer": "gmv"},
    "opt_equal_weight": {"portfolio.optimizer": "equal_weight"},

    # ===== Group H — market archetypes ==================================
    "max_aggression": {
        **_w(0.45, 0.20, 0.30, 0.05),
        "portfolio.top_n_sectors": 2, "portfolio.constraints.max_weight": 0.65,
        "risk.vol_scaling.target_vol_annual": 0.40,
        "risk.drawdown.cumulative_dd_halve": -0.30,
        "risk.vix_progressive_derisk.tiers": [
            {"vix_above": 32, "cash_pct": 0.10}, {"vix_above": 36, "cash_pct": 0.25}],
    },
    "quality_defensive": {
        **_w(0.25, 0.45, 0.15, 0.15),
        "portfolio.top_n_sectors": 4, "portfolio.constraints.max_weight": 0.45,
        "risk.vol_scaling.target_vol_annual": 0.22,
        "risk.vix_progressive_derisk.tiers": [
            {"vix_above": 24, "cash_pct": 0.15}, {"vix_above": 28, "cash_pct": 0.35}],
    },
    "ai_capex_tilt": {
        **_w(0.30, 0.25, 0.40, 0.05),
        "portfolio.top_n_sectors": 3, "portfolio.constraints.max_weight": 0.55,
        "risk.vol_scaling.target_vol_annual": 0.32,
    },
    "supply_chain_core": {
        **_w(0.20, 0.55, 0.15, 0.10),
        "portfolio.top_n_sectors": 3, "signals.supply_chain.use_external_macro": True,
    },
    # recovery_tiers_30(I-3): 中间档——默认(28/32,紧急36)与 derisk_loose(32/36,紧急40)
    # 之间的新频谱点。注: 曾试 recovery_deploy(=loose tiers+休眠 release 键),W4 证明与
    # derisk_loose 逐位相同 → 撤销以免重复候选双倍加权;release 探针待 equity 修复后再加。
    "recovery_tiers_30": {
        "rebalance.emergency_derisk_vix": 38.0, "rebalance.emergency_cash_pct": 0.40,
        "risk.vix_progressive_derisk.tiers": [
            {"vix_above": 30, "cash_pct": 0.10}, {"vix_above": 34, "cash_pct": 0.25}],
    },

    # ===== Group M — isolated single-factor tests =======================
    "pure_momentum":     _w(1.0, 0.0, 0.0, 0.0),
    "pure_supply_chain": _w(0.0, 1.0, 0.0, 0.0),
    "pure_capex":        _w(0.0, 0.0, 1.0, 0.0),
}


_PARAM_SET_DESCRIPTIONS: Dict[str, str] = {
    "default":           "A1 default — validated win-criterion config (cs.30/sc.35/cx.25/cy.10)",
    "supply_chain_heavy": "A2 supply-chain-heavy (sc=0.50) — lean on propagation alpha",
    "momentum_heavy":    "A3 momentum-heavy (cs=0.45) — ride recent winners",
    "capex_heavy":       "A4 capex-heavy (cx=0.40) — hyperscaler AI-capex driven",
    "balanced_four":     "A5 equal 0.25 across all four factors",
    "momentum_capex":    "A6 momentum+capex blend (cs=.40, cx=.40)",
    "concentrated_2":    "B1 top-2, max_weight 0.65 — highest conviction",
    "standard_3":        "B2 top-3, max_weight 0.55 (production default concentration)",
    "diversified_4":     "B3 top-4, max_weight 0.45",
    "broad_5":           "B4 top-5, max_weight 0.35 — diversified",
    "semivol_18":        "C5 semivol: downside-only vol scaling, target 18% (tight probe)",
    "semivol_21":        "C6 semivol: downside-only vol scaling, target 21% (≈30%/√2 baseline)",
    "semivol_24":        "C7 semivol: downside-only vol scaling, target 24% (loose probe)",
    "recovery_tiers_30": "D4 intermediate VIX tiers (30/34, emergency 38) — between default and loose",
    "dd_release_08":     "C8 DD off-bottom release: rebound ≥8% off trough skips daily halve",
    "dd_release_12":     "C9 DD off-bottom release: rebound ≥12% (conservative probe)",
    "vol_target_24":     "C1 vol target 24% — modest sizing",
    "vol_target_30":     "C2 vol target 30% (default)",
    "vol_target_40":     "C3 vol target 40% — run hot",
    "no_vol_scaling":    "C4 vol scaling OFF — full exposure",
    "derisk_tight":      "D1 tight VIX de-risk (25/30 tiers, emergency 32)",
    "derisk_loose":      "D2 loose VIX de-risk (32/36 tiers, emergency 40)",
    "no_vix_derisk":     "D3 no VIX de-risk — never cut for vol",
    "fast_momentum":     "E1 6-0 fast momentum (zwin 24)",
    "standard_momentum": "E2 12-1 standard momentum",
    "slow_momentum":     "E3 15-1 slow momentum (zwin 48)",
    "external_on":       "F1 supply-chain external data ON (TSMC/ASML/DRAM/MU-DIO)",
    "external_off":      "F2 supply-chain price-proxy only",
    "opt_inv_vol":       "G1 inverse-vol optimizer (default)",
    "opt_risk_parity":   "G2 risk-parity optimizer",
    "opt_gmv":           "G3 global-minimum-variance optimizer",
    "opt_equal_weight":  "G4 equal-weight optimizer",
    "max_aggression":    "H1 max aggression — top-2, vol 40%, loose de-risk",
    "quality_defensive": "H2 quality-defensive — top-4, supply-chain-heavy, vol 22%",
    "ai_capex_tilt":     "H3 AI-capex tilt — capex-heavy, top-3, vol 32%",
    "supply_chain_core": "H4 supply-chain core — sc 0.55, top-3, external on",
    "pure_momentum":     "M1 isolated: cross-sectional momentum only",
    "pure_supply_chain": "M2 isolated: supply-chain propagation only",
    "pure_capex":        "M3 isolated: AI-capex pulse only",
}


def list_param_sets() -> None:
    """Print all AISS param sets grouped by letter."""
    by_group: Dict[str, List[str]] = {}
    for name in PARAM_SETS:
        g = _PARAM_SET_DESCRIPTIONS.get(name, "?")[0].upper()
        by_group.setdefault(g, []).append(name)
    print(f"AISS PARAM SETS ({len(PARAM_SETS)} total)")
    for g in sorted(by_group):
        print(f"\n[Group {g}]")
        for name in by_group[g]:
            print(f"  {name:22} {_PARAM_SET_DESCRIPTIONS.get(name, '')}")


if __name__ == "__main__":
    list_param_sets()
