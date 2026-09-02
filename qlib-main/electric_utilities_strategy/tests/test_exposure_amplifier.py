"""通路③ 敞口放大器 — 纯函数 + 风控函数集成 + 图谱敏感度(零生产写入)。"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from electric_utilities_strategy.portfolio.risk import (apply_risk_controls, compute_exposure_amplifier)
from electric_utilities_strategy.signals.supply_chain import (NODE_DEMAND, NODE_POWER_PRICE, NODE_AI_CAPEX,
                                                                shortage_sensitivity)


def _w(**kw):
    return pd.Series(kw, dtype=float)


def test_amplifier_math_and_clip():
    w = _w(a=0.5, b=0.5)
    e, phi = compute_exposure_amplifier(1.0, w, None, k=0.10, lo=0.85, hi=1.15)
    assert abs(e - 1.10) < 1e-12 and phi == 1.0
    e, _ = compute_exposure_amplifier(3.0, w, None)          # 1.30 → clipped to hi
    assert e == 1.15
    e, _ = compute_exposure_amplifier(-3.0, w, None)         # 0.70 → clipped to lo
    assert e == 0.85
    e, phi = compute_exposure_amplifier(float("nan"), w, None)
    assert e == 1.0 and math.isnan(phi)                      # missing shortage → neutral


def test_graph_weighted_phi_depends_on_composition():
    sens = {"ipp": 2.0, "water": 0.5}
    e_ipp, phi_ipp = compute_exposure_amplifier(1.0, _w(ipp=1.0, water=0.0), sens)
    e_wat, phi_wat = compute_exposure_amplifier(1.0, _w(ipp=0.0, water=1.0), sens)
    assert phi_ipp == 2.0 and phi_wat == 0.5
    assert e_ipp > e_wat                                     # same z, IPP-heavy book amplified more
    assert abs(e_ipp - 1.15) < 1e-12 and abs(e_wat - 1.05) < 1e-12


def test_shortage_sensitivity_from_graph():
    g = {(NODE_DEMAND, "ipp"): (0.8, 0, ""), (NODE_POWER_PRICE, "ipp"): (0.7, 0, ""),
         (NODE_DEMAND, "reg"): (0.9, 1, ""), (NODE_AI_CAPEX, "water"): (0.3, 6, "")}
    s = shortage_sensitivity(g, floor=0.5)
    assert set(s) == {"ipp", "reg", "water"}
    assert s["ipp"] > s["reg"] > s["water"]                  # water has no demand/price inbound → floor
    assert abs(sum(s.values()) / 3 - 1.0) < 1e-12            # normalized to mean 1
    # production graph: every subsector present, mean 1, all positive
    p = shortage_sensitivity(None)
    assert len(p) >= 10 and all(v > 0 for v in p.values()) and abs(np.mean(list(p.values())) - 1) < 1e-9


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
    assert abs(adj["a"] / adj["b"] - 0.4 / 0.35) < 1e-9       # selection untouched, gross only


def test_no_leverage_cap_and_max_weight_cap():
    w = _w(a=0.4, b=0.35, c=0.25)                            # fully invested
    adj, cash, _ = _run_rc(w, exposure_mult=1.15)             # cannot exceed 100% without leverage
    assert abs(adj.sum() - 1.0) < 1e-9 and cash == 0.0
    adj, cash, _ = _run_rc(w, exposure_mult=1.15, exposure_allow_leverage=True)
    assert adj.sum() < 1.15 + 1e-9 and adj.max() <= 0.55 + 1e-9   # max_weight still binds (a: 0.46 → capped)
    # apply_risk_controls always renormalizes to (1 − cash_pct), so a "cash-holding base" only
    # exists when a defensive tier raised cash. 防守优先(2026-09-02): 这时 E>1 被钳到 1.0 ——
    # 缺电度尖峰不得把 VIX/DD/vol 档留的防守现金买回去; E<1 仍然可以继续减。
    idx = pd.bdate_range("2024-01-01", periods=300)
    rets = pd.Series(0.0, index=idx); macro = pd.DataFrame({"vix": 40.0}, index=idx)
    adj, cash, flags = apply_risk_controls(weights=w, portfolio_returns=rets, macro=macro,
                                           vol_scaling_enabled=False, max_weight=0.55,
                                           vix_emergency_threshold=35.0, emergency_cash_pct=0.45,
                                           exposure_mult=1.15)
    assert flags.vix_emergency_triggered
    assert abs(adj.sum() - 0.55) < 1e-9 and abs(cash - 0.45) < 1e-9         # E>1 被防守钳制
    adj2, cash2, flags2 = apply_risk_controls(weights=w, portfolio_returns=rets, macro=macro,
                                              vol_scaling_enabled=False, max_weight=0.55,
                                              vix_emergency_threshold=35.0, emergency_cash_pct=0.45,
                                              exposure_mult=0.85)
    assert flags2.vix_emergency_triggered                                   # E<1 仍生效(继续减)
    assert abs(adj2.sum() - 0.55 * 0.85) < 1e-9 and abs(cash2 - (1 - 0.55 * 0.85)) < 1e-9
