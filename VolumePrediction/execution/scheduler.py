"""
execution/scheduler — 单标的多日排程(Plan §四 execution/ / 论文 §6.1 记账协议)
==============================================================================
两种模式(由 Profile 决定):
- tracking/event(μ 有限): 每日对**剩余缺口**应用闭式解 z*=s(v̄;μ),再按参与率
  截断(§7.12-4)。z*<1 表示"有意不完成"是最优——不视为失败。
- urgent(μ=∞): 委托 policy.solve_urgent(逐日打满 cap,最早完成)。

式13 记账接口: evolve_x0(x0_dollars, r_raw) —— 隔夜 x⁰ 随原始收益演化,
调用侧(回测/实验层)逐日推进;本排程器输出的是"以当日信息生成的建议表",
真正的动态耦合在 evaluation/trading 实验里逐日重排。
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from VolumePrediction.econ.policy import (
    s_opt_constrained, solve_urgent, impact_cost_dollars, IMPACT_COEF,
)
from VolumePrediction.econ.objective import Profile


def evolve_x0(x0_dollars: float, r_raw: float) -> float:
    """式13: x⁰_t = x_{t-1} × R^raw(隔夜持仓市值随原始收益演化)。"""
    return x0_dollars * (1.0 + r_raw)


def schedule_single(ticker: str,
                    target_shares: float,
                    price: float,
                    adv_forecast_shares: pd.Series,
                    profile: Profile,
                    mu: float,
                    max_days: int = 10,
                    impact_coef: float = IMPACT_COEF) -> pd.DataFrame:
    """单标的多日建议表。

    Parameters
    ----------
    target_shares : 需要成交的股数(绝对值;方向由调用侧携带)
    price         : 当前价(股数↔美元换算)
    adv_forecast_shares : 日索引的预测股量序列(前瞻;service 侧准备)
    mu            : 已 resolve 的 μ(inf → urgent 路径)

    Returns: DataFrame[date, shares, participation, z, capped, est_cost_dollars,
    remaining_after];attrs: completed / remaining / mode。
    """
    target_shares = abs(float(target_shares))
    cap = profile.adv_cap
    if profile.is_urgent or math.isinf(mu):
        df = solve_urgent(target_shares, adv_forecast_shares, cap, max_days)
        # 补成本估计
        if not df.empty:
            V_shares = adv_forecast_shares.loc[df["date"]].values
            traded_d = df["shares"].values * price
            V_d = V_shares * price
            df["est_cost_dollars"] = [
                impact_cost_dollars(t, v, impact_coef) for t, v in zip(traded_d, V_d)]
            df["z"] = float("nan")
            df["capped"] = df["participation"] >= cap - 1e-12
        return df

    rows = []
    remaining = target_shares
    days = list(adv_forecast_shares.index)[:max_days]
    for d in days:
        if remaining <= 1e-9:
            break
        v_shares = float(adv_forecast_shares.loc[d])
        V_dollars = v_shares * price
        gap_dollars = remaining * price
        cr = s_opt_constrained(
            v_bar=math.log(max(V_dollars, 1e-12)), mu=mu,
            target_dollars=gap_dollars, pred_dollar_volume=V_dollars,
            participation_cap=cap, form=profile.lambda_form)
        qty = cr.z * remaining
        traded_dollars = qty * price
        participation = traded_dollars / V_dollars if V_dollars > 0 else float("nan")
        remaining -= qty
        rows.append({"date": d, "shares": qty, "participation": participation,
                     "z": cr.z, "z_unconstrained": cr.z_unconstrained,
                     "capped": cr.capped, "cap_reason": cr.reason,
                     "est_cost_dollars": impact_cost_dollars(traded_dollars, V_dollars,
                                                             impact_coef),
                     "remaining_after": remaining})
    df = pd.DataFrame(rows)
    df.attrs["completed"] = bool(remaining <= max(1e-9, 0.005 * target_shares))
    df.attrs["remaining"] = remaining
    df.attrs["mode"] = profile.mode
    df.attrs["note"] = ("tracking 模式 z*<1 为最优部分成交,不视为失败"
                        if not df.attrs["completed"] else "")
    return df
