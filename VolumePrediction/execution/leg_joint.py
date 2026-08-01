"""
execution/leg_joint — pairs 双腿联合排程(Plan §5.7 pairs 剖面约束)
==================================================================
对冲保持原则: 两腿每日按**相同比例** f_t 推进(任一时点两腿已完成比例相等
→ 对冲比例全程不破)。每日 f_t 受两腿参与率上限的较紧者约束:
  f_t = min(剩余比例, min_leg( cap × ADV̂_leg / target_leg ))
瓶颈=较不流动腿;完成天数 = 两腿独立完成天数的最大值(联合下自然实现)。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from VolumePrediction.econ.policy import impact_cost_dollars, IMPACT_COEF


def schedule_pair(s1: str, s2: str,
                  target_shares_1: float, target_shares_2: float,
                  price_1: float, price_2: float,
                  adv_forecast_1: pd.Series, adv_forecast_2: pd.Series,
                  participation_cap: float = 0.20,
                  max_days: int = 10,
                  impact_coef: float = IMPACT_COEF) -> pd.DataFrame:
    """双腿同步排程。

    Returns: DataFrame[date, frac, s1_shares, s2_shares, s1_participation,
    s2_participation, est_cost_dollars, cum_frac];attrs: completed/bottleneck_leg。
    对冲同步不变式: 逐日 s1_shares/target_1 == s2_shares/target_2 == frac。
    """
    t1, t2 = abs(float(target_shares_1)), abs(float(target_shares_2))
    if t1 <= 0 or t2 <= 0:
        raise ValueError("both legs must have nonzero targets")
    idx = adv_forecast_1.index.intersection(adv_forecast_2.index)[:max_days]
    rows = []
    cum = 0.0
    for d in idx:
        if cum >= 1.0 - 1e-12:
            break
        cap1 = participation_cap * float(adv_forecast_1.loc[d]) / t1
        cap2 = participation_cap * float(adv_forecast_2.loc[d]) / t2
        f = min(1.0 - cum, cap1, cap2)
        if f <= 0:
            rows.append({"date": d, "frac": 0.0, "s1_shares": 0.0, "s2_shares": 0.0,
                         "s1_participation": 0.0, "s2_participation": 0.0,
                         "est_cost_dollars": 0.0, "cum_frac": cum})
            continue
        q1, q2 = f * t1, f * t2
        V1 = float(adv_forecast_1.loc[d]) * price_1
        V2 = float(adv_forecast_2.loc[d]) * price_2
        cost = (impact_cost_dollars(q1 * price_1, V1, impact_coef)
                + impact_cost_dollars(q2 * price_2, V2, impact_coef))
        cum += f
        rows.append({"date": d, "frac": f, "s1_shares": q1, "s2_shares": q2,
                     "s1_participation": q1 * price_1 / V1 if V1 > 0 else float("nan"),
                     "s2_participation": q2 * price_2 / V2 if V2 > 0 else float("nan"),
                     "est_cost_dollars": cost, "cum_frac": cum})
    df = pd.DataFrame(rows)
    df.attrs["completed"] = bool(cum >= 1.0 - 1e-9)
    df.attrs["remaining_frac"] = max(0.0, 1.0 - cum)
    # 瓶颈腿 = 单腿独立完成所需天数更多者
    need1 = t1 / (participation_cap * adv_forecast_1.reindex(idx).mean() + 1e-12)
    need2 = t2 / (participation_cap * adv_forecast_2.reindex(idx).mean() + 1e-12)
    df.attrs["bottleneck_leg"] = s1 if need1 >= need2 else s2
    df.attrs["hedge_preserved"] = True
    return df
