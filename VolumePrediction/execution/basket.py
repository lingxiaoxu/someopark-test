"""
execution/basket — AISS 篮子排程(Plan §5.7 aiss_rebalance 剖面)
================================================================
输入 = AISS 日报 stock_trades 条目(附录 D schema: ticker/shares/…;只取
ticker+shares,其余字段透传)。两种对齐模式:
- independent(默认): 各股独立按 profile 排程(各自最优;完成日可不同)
- aligned: 全篮子按公共比例推进(leg_joint 的 N 腿推广;整篮对冲/风格保持,
  瓶颈=约束最紧的股票)
"""
from __future__ import annotations

from typing import Iterable, Optional

import pandas as pd

from VolumePrediction.econ.objective import Profile
from VolumePrediction.execution.scheduler import schedule_single
from VolumePrediction.econ.policy import impact_cost_dollars, IMPACT_COEF


def schedule_basket(stock_trades: Iterable[dict],
                    prices: dict,
                    adv_forecasts: dict,
                    profile: Profile,
                    mu: float,
                    max_days: int = 10,
                    align: str = "independent",
                    impact_coef: float = IMPACT_COEF) -> dict:
    """→ {"per_ticker": {ticker: DataFrame}, "summary": DataFrame, "align": str}

    stock_trades: [{"ticker": str, "shares": float(±), ...}, ...]
    prices/adv_forecasts: ticker → price / 日索引预测股量 Series
    """
    trades = [t for t in stock_trades if abs(float(t.get("shares", 0))) > 0]
    if align not in ("independent", "aligned"):
        raise ValueError("align must be independent|aligned")

    per: dict[str, pd.DataFrame] = {}
    if align == "independent":
        for t in trades:
            tk = t["ticker"]
            per[tk] = schedule_single(
                ticker=tk, target_shares=abs(float(t["shares"])),
                price=float(prices[tk]),
                adv_forecast_shares=adv_forecasts[tk],
                profile=profile, mu=mu, max_days=max_days,
                impact_coef=impact_coef)
    else:
        # aligned: 公共比例 f_t = min(剩余, min_i cap×ADV̂_i/target_i)
        cap = profile.adv_cap
        targets = {t["ticker"]: abs(float(t["shares"])) for t in trades}
        idx = None
        for tk in targets:
            s = adv_forecasts[tk].index
            idx = s if idx is None else idx.intersection(s)
        idx = idx[:max_days]
        cum = 0.0
        rows_per = {tk: [] for tk in targets}
        for d in idx:
            if cum >= 1.0 - 1e-12:
                break
            f = min([1.0 - cum] + [
                cap * float(adv_forecasts[tk].loc[d]) / targets[tk]
                for tk in targets])
            f = max(f, 0.0)
            for tk in targets:
                q = f * targets[tk]
                V = float(adv_forecasts[tk].loc[d]) * float(prices[tk])
                rows_per[tk].append({
                    "date": d, "shares": q,
                    "participation": q * float(prices[tk]) / V if V > 0 else float("nan"),
                    "frac": f,
                    "est_cost_dollars": impact_cost_dollars(q * float(prices[tk]), V,
                                                            impact_coef)})
            cum += f
        for tk in targets:
            df = pd.DataFrame(rows_per[tk])
            df.attrs["completed"] = bool(cum >= 1.0 - 1e-9)
            df.attrs["cum_frac"] = cum
            per[tk] = df

    summary_rows = []
    for t in trades:
        tk = t["ticker"]
        df = per[tk]
        summary_rows.append({
            "ticker": tk,
            "target_shares": abs(float(t["shares"])),
            "direction": "BUY" if float(t["shares"]) > 0 else "SELL",
            "days": len(df),
            "completed": bool(df.attrs.get("completed", False)),
            "est_cost_dollars": float(df["est_cost_dollars"].sum()) if len(df) else 0.0,
            "max_participation": float(df["participation"].max()) if len(df) else 0.0,
        })
    return {"per_ticker": per, "summary": pd.DataFrame(summary_rows), "align": align}
