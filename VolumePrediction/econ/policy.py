"""
econ/policy — 论文 §4 经济框架的闭式解与退化策略(Plan §三 G6 / §7.12)
=====================================================================
核心对象:
- λ(v)  : 价格冲击系数,λ = 0.2/V = 0.2·e^{−v}(主形式)或 0.2·√(1/V)(注16 替代形式)
- losscon(v, z; μ) = λ(v)·z² + μ·(1−z)²   —— 归一化经济损失(论文式)
- s(v̄; μ) = argmin_z losscon = μ/(μ+λ) = 1/(1+exp(−v̄+log0.2−logμ)) —— 闭式解
- μ=∞ 退化: 不再权衡,纯"给定必成交求最快完成"排程(风控离场绝不因成本延迟)
- 约束截断: z_cap = 参与率上限×预测量/目标量;z* 超出即截断并显式标注(§7.12-4)

单位约定:
- v / v_bar 均为 **log 美元成交量**(§7.1: V=shares×vw, v=log V)
- z ∈ [0,1] 为归一化交易率(本期完成目标缺口的比例)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

IMPACT_COEF = 0.1          # PriceImpact = 0.1 × 参与率(Kyle 线性,论文 §4.1)
LAMBDA_COEF = 0.2          # λ = 0.2/V(TradingCost 二次型系数,含 impact 的 2 因子)

FORM_MAIN = "0.2/V"
FORM_SQRT = "0.2*sqrt(1/V)"
FORM_CALIBRATED = "calibrated"   # λ = C/V^γ,自家 Amihud 市场代理标定(E11-T1)

# ── 自家标定 λ 的 lazy 加载(E11-T1 接线,2026-08-15)────────────────────────
# 沿用 μ 的降级纪律: 工件缺失/破损绝不抛异常 → 论文先验 (0.2, 1.0),
# calibration_source 如实标注,加载失败只大声一次。mtime 变化自动重读。
_LAMBDA_CACHE: dict = {"mtime": None, "C": LAMBDA_COEF, "gamma": 1.0,
                       "source": "paper_prior", "warned": False}


def _lambda_registry_path():
    from VolumePrediction.common import OUT
    return OUT / "registry" / "lambda_calibration.json"


def lambda_params(form: str = FORM_CALIBRATED) -> dict:
    """→ {C, gamma, calibration_source}(报告/日志用;非 calibrated 形式给论文常数)。"""
    if form == FORM_MAIN:
        return {"C": LAMBDA_COEF, "gamma": 1.0, "calibration_source": "paper_prior"}
    if form == FORM_SQRT:
        return {"C": LAMBDA_COEF, "gamma": 0.5, "calibration_source": "paper_prior"}
    if form != FORM_CALIBRATED:
        raise ValueError(f"unknown lambda form: {form!r}")
    import json
    p = _lambda_registry_path()
    try:
        mt = p.stat().st_mtime
        if _LAMBDA_CACHE["mtime"] != mt:
            d = json.loads(p.read_text())["lambda_amihud"]
            C, g = float(d["C"]), float(d["gamma"])
            if not (math.isfinite(C) and C > 0 and math.isfinite(g) and 0 < g < 3):
                raise ValueError(f"insane lambda params C={C} gamma={g}")
            _LAMBDA_CACHE.update(mtime=mt, C=C, gamma=g,
                                 source=d.get("calibration_source",
                                              "amihud_market_proxy"), warned=False)
    except Exception as e:  # noqa: BLE001 — 降级论文先验,只大声一次
        if not _LAMBDA_CACHE["warned"]:
            print(f"!!!! [econ WARN] lambda_calibration.json unavailable ({e}) "
                  f"— degrade to paper prior 0.2/V")
            _LAMBDA_CACHE.update(mtime=None, C=LAMBDA_COEF, gamma=1.0,
                                 source="paper_prior", warned=True)
    return {"C": _LAMBDA_CACHE["C"], "gamma": _LAMBDA_CACHE["gamma"],
            "calibration_source": _LAMBDA_CACHE["source"]}


def lambda_of_v(v: float, form: str = FORM_MAIN) -> float:
    """λ(v);v=log 美元量。主形式 0.2·e^{−v};替代形式 0.2·e^{−v/2}(=0.2/√V);
    calibrated = C·e^{−γv}(自家 Amihud 标定,C=0.73/γ=1.19,缺工件降级论文)。"""
    if form == FORM_MAIN:
        return LAMBDA_COEF * math.exp(-v)
    if form == FORM_SQRT:
        return LAMBDA_COEF * math.exp(-v / 2.0)
    if form == FORM_CALIBRATED:
        p = lambda_params(form)
        return p["C"] * math.exp(-p["gamma"] * v)
    raise ValueError(f"unknown lambda form: {form!r}")


def losscon(v: float, z: float, mu: float, form: str = FORM_MAIN) -> float:
    """归一化经济损失 λ(v)z² + μ(1−z)²。μ=inf 时按极限语义: z<1 → inf。"""
    if math.isinf(mu):
        return lambda_of_v(v, form) if z >= 1.0 else float("inf")
    return lambda_of_v(v, form) * z * z + mu * (1.0 - z) ** 2


def s_opt(v_bar: float, mu: float, form: str = FORM_MAIN) -> float:
    """最优交易率闭式解 z* = μ/(μ+λ(v̄))。

    推导: d/dz[λz²+μ(1−z)²]=0 → z*=μ/(μ+λ)。主形式下等价于
    1/(1+exp(−v̄+log0.2−logμ))(论文图 2 的 S 形曲线)。
    边界: μ=inf→1;μ=0→0。
    """
    if math.isinf(mu):
        return 1.0
    if mu <= 0:
        return 0.0
    lam = lambda_of_v(v_bar, form)
    return mu / (mu + lam)


def impact_cost_dollars(traded_dollars: float, V_dollars: float,
                        coef: float = IMPACT_COEF) -> float:
    """美元冲击成本 = coef × (traded/V) × traded = coef·traded²/V。"""
    if V_dollars <= 0:
        return float("inf") if traded_dollars > 0 else 0.0
    return coef * traded_dollars * traded_dollars / V_dollars


@dataclass
class ConstrainedRate:
    z: float
    z_unconstrained: float
    capped: bool
    cap: float
    reason: str = ""


def s_opt_constrained(v_bar: float, mu: float,
                      target_dollars: float,
                      pred_dollar_volume: float,
                      participation_cap: float,
                      form: str = FORM_MAIN) -> ConstrainedRate:
    """闭式解 + 参与率约束截断(§7.12-4)。

    z_cap = cap × V̂ / |target|(当日最多能完成目标的比例);
    截断激活时 capped=True 并给出 rationing 原因(策略侧可见)。
    """
    z_star = s_opt(v_bar, mu, form)
    if target_dollars <= 0:
        return ConstrainedRate(z=0.0, z_unconstrained=z_star, capped=False, cap=1.0,
                               reason="target<=0")
    cap = min(1.0, participation_cap * pred_dollar_volume / target_dollars)
    if z_star > cap:
        return ConstrainedRate(
            z=cap, z_unconstrained=z_star, capped=True, cap=cap,
            reason=f"participation cap {participation_cap:.0%} of predicted volume "
                   f"limits fill to {cap:.1%} of target today")
    return ConstrainedRate(z=z_star, z_unconstrained=z_star, capped=False, cap=cap)


def solve_urgent(target_shares: float,
                 adv_forecast_shares: pd.Series,
                 participation_cap: float,
                 max_days: Optional[int] = None) -> pd.DataFrame:
    """μ=∞ 退化排程: 每日成交 min(剩余, cap×当日预测量)。

    最优性(一句话): 目标为字典序"最早完成"(风控语义),任何把某日成交量后移的
    方案都不会更早完成,故逐日打满参与率上限的贪心即最优;在最早完成天数给定后,
    该贪心同时是可行域的唯一前沿排程。

    Parameters
    ----------
    adv_forecast_shares : 按日索引的预测股数量(与 target_shares 同单位)
    """
    if target_shares < 0:
        raise ValueError("target_shares must be >= 0 (direction handled by caller)")
    rows: List[dict] = []
    remaining = float(target_shares)
    days = list(adv_forecast_shares.index)
    if max_days is not None:
        days = days[:max_days]
    for d in days:
        if remaining <= 0:
            break
        day_cap = participation_cap * float(adv_forecast_shares.loc[d])
        qty = min(remaining, day_cap)
        participation = (qty / float(adv_forecast_shares.loc[d])
                         if adv_forecast_shares.loc[d] > 0 else float("nan"))
        remaining -= qty
        rows.append({"date": d, "shares": qty, "participation": participation,
                     "day_cap_shares": day_cap, "remaining_after": remaining})
    df = pd.DataFrame(rows)
    df.attrs["completed"] = bool(remaining <= 1e-9)
    df.attrs["remaining"] = remaining
    df.attrs["mode"] = "urgent"
    return df


def mu_from_aum(aum: float, aum_scenarios: List[float], mu_grid: List[float]) -> float:
    """AUM → μ 的论文先验映射(冷启动用;§7.12-2 calibration_source='paper_prior')。

    论文 μ=γσ²/A → AUM 越大 μ 越小。取 log-log 线性插值,AUM 升序对应 μ 降序。
    """
    a = np.log(np.asarray(sorted(aum_scenarios), dtype=float))
    m = np.log(np.asarray(sorted(mu_grid, reverse=True), dtype=float))
    # 用 mu_grid 两端与 aum 两端对齐(网格长度可不同)
    m_pts = np.interp(np.linspace(0, 1, len(a)), np.linspace(0, 1, len(m)), m)
    la = math.log(max(aum, 1.0))
    return float(math.exp(np.interp(la, a, m_pts)))
