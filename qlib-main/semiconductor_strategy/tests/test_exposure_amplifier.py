"""通路③ 敞口放大器(AEUS 镜像) — 纯函数 + 风控函数集成 + 图谱多跳敏感度(零生产写入)。"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from semiconductor_strategy.portfolio.risk import (apply_risk_controls, compute_exposure_amplifier)
from semiconductor_strategy.signals.supply_chain import (NODE_AI_CAPEX, NODE_CONSUMER,
                                                         demand_sensitivity)


def _w(**kw):
    return pd.Series(kw, dtype=float)


def test_amplifier_math_and_clip():
    w = _w(a=0.5, b=0.5)
    e, phi = compute_exposure_amplifier(1.0, w, None, k=0.10, lo=0.85, hi=1.15)
    assert abs(e - 1.10) < 1e-12 and phi == 1.0
    assert compute_exposure_amplifier(3.0, w, None)[0] == 1.15      # clipped hi
    assert compute_exposure_amplifier(-3.0, w, None)[0] == 0.85     # clipped lo
    e, phi = compute_exposure_amplifier(float("nan"), w, None)
    assert e == 1.0 and math.isnan(phi)                             # missing demand → neutral


def test_graph_weighted_phi_depends_on_composition():
    sens = {"ai_gpu": 2.0, "analog_defense": 0.5}
    e_gpu, phi_gpu = compute_exposure_amplifier(1.0, _w(ai_gpu=1.0, analog_defense=0.0), sens)
    e_ana, phi_ana = compute_exposure_amplifier(1.0, _w(ai_gpu=0.0, analog_defense=1.0), sens)
    assert phi_gpu == 2.0 and phi_ana == 0.5
    assert e_gpu > e_ana                                            # same z, AI-levered book moves more
    assert abs(e_gpu - 1.15) < 1e-12 and abs(e_ana - 1.05) < 1e-12


def test_demand_sensitivity_propagates_multi_hop():
    g = {(NODE_AI_CAPEX, "ai_gpu"): (1.0, 0, ""),
         ("ai_gpu", "memory_hbm"): (0.8, 4, ""),
         ("memory_hbm", "equipment"): (0.7, 4, ""),
         (NODE_CONSUMER, "rf_edge"): (0.8, 0, "")}
    s = demand_sensitivity(g, floor=0.5, decay=0.6, max_hops=4)
    assert set(s) == {"ai_gpu", "memory_hbm", "equipment", "rf_edge"}
    # 一跳 > 两跳 ≥ 三跳;链外(consumer 驱动的 rf_edge)与被地板托住的深层成员同值
    assert s["ai_gpu"] > s["memory_hbm"] >= s["equipment"]
    assert abs(s["equipment"] - s["rf_edge"]) < 1e-12          # 地板是所有人的下限
    assert abs(sum(s.values()) / len(s) - 1.0) < 1e-12              # normalized to mean 1
    # 生产图谱:每个子板块都在,均值 1,全正
    p = demand_sensitivity(None)
    assert len(p) >= 7 and all(v > 0 for v in p.values())
    assert abs(np.mean(list(p.values())) - 1.0) < 1e-9
    assert p["ai_gpu"] == max(p.values())                           # AI capex 的直接下游最敏感


def _run_rc(weights, **kw):
    idx = pd.bdate_range("2024-01-01", periods=300)
    rets = pd.Series(np.random.default_rng(0).normal(0, 0.01, len(idx)), index=idx)
    macro = pd.DataFrame({"vix": 15.0}, index=idx)
    return apply_risk_controls(weights=weights, portfolio_returns=rets, macro=macro,
                               vol_scaling_enabled=False, max_weight=0.55, **kw)


def test_risk_controls_neutral_when_E_is_one():
    w = _w(a=0.4, b=0.35, c=0.25)
    base = _run_rc(w)
    same = _run_rc(w, exposure_mult=1.0)
    pd.testing.assert_series_equal(base[0], same[0]); assert base[1] == same[1]
    assert same[2].exposure_mult == 1.0 and not any("Exposure amplifier" in n for n in same[2].notes)


def test_risk_controls_shrinks_gross_when_E_below_one():
    w = _w(a=0.4, b=0.35, c=0.25)
    adj, cash, flags = _run_rc(w, exposure_mult=0.90)
    assert abs(adj.sum() - 0.90) < 1e-9 and abs(cash - 0.10) < 1e-9
    assert flags.exposure_mult == 0.90 and any("Exposure amplifier" in n for n in flags.notes)
    assert abs(adj["a"] / adj["b"] - 0.4 / 0.35) < 1e-9             # selection untouched, gross only


def test_no_leverage_cap_and_defensive_clamp():
    w = _w(a=0.4, b=0.35, c=0.25)                                   # fully invested
    adj, cash, _ = _run_rc(w, exposure_mult=1.15)                   # cannot exceed 100% without leverage
    assert abs(adj.sum() - 1.0) < 1e-9 and cash == 0.0
    adj, cash, _ = _run_rc(w, exposure_mult=1.15, exposure_allow_leverage=True)
    assert adj.sum() < 1.15 + 1e-9 and adj.max() <= 0.55 + 1e-9     # max_weight still binds
    # 防守优先: VIX 应急档已留 45% 现金 → E>1 被钳到 1.0; E<1 仍可继续减
    idx = pd.bdate_range("2024-01-01", periods=300)
    rets = pd.Series(0.0, index=idx); macro = pd.DataFrame({"vix": 40.0}, index=idx)
    kw = dict(portfolio_returns=rets, macro=macro, vol_scaling_enabled=False, max_weight=0.55,
              vix_emergency_threshold=35.0, emergency_cash_pct=0.45)
    adj, cash, flags = apply_risk_controls(weights=w, exposure_mult=1.15, **kw)
    assert flags.vix_emergency_triggered
    assert abs(adj.sum() - 0.55) < 1e-9 and abs(cash - 0.45) < 1e-9
    adj2, cash2, flags2 = apply_risk_controls(weights=w, exposure_mult=0.85, **kw)
    assert abs(adj2.sum() - 0.55 * 0.85) < 1e-9 and abs(cash2 - (1 - 0.55 * 0.85)) < 1e-9
