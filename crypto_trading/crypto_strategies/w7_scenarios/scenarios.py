"""The 12 mutually exclusive scenarios (decision list) and W7 per-contract PnL."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import BAND, DEFAULT_SIZE_COL, FEE_MULT, FLOW_TH, FUND_TH, SIZE_COL, TREND_TH, VOL_TH


def label(m: pd.DataFrame) -> pd.Series:
    """Exactly one label per row, first matching rule wins:
    1 大单反向 (live gate skip rule: aligned flow <= -0.5 AND aligned 1-min move < 0) x 波动
    2 大单同向 (aligned flow >= +0.5 AND aligned move > 0) x 波动
    3 funding热门方拥挤   4 funding对手方拥挤   5 方向 x 波动 (6)
    Invalid flow data never counts as 大单 (same as the live gate, which fails open)."""
    sg = np.where(m.fav == "yes", 1.0, -1.0)
    af, am = m.flow_1m * sg, m.mom_1m_bp * sg
    ok = m.flow_valid.astype(bool)
    against = ok & (af <= -FLOW_TH) & (am < 0)
    withfav = ok & (af >= FLOW_TH) & (am > 0)
    hv = m.mkt_vr2 >= VOL_TH
    vol = pd.Series(np.where(hv, "高波动", "低波动"), index=m.index)
    hot = ((m.fav == "yes") & (m.dev8h >= FUND_TH)) | ((m.fav == "no") & (m.dev8h <= -FUND_TH))
    opp = ((m.fav == "yes") & (m.dev8h <= -FUND_TH)) | ((m.fav == "no") & (m.dev8h >= FUND_TH))
    dirn = pd.Series(np.where(m.mkt_trend60 >= TREND_TH, "上涨", np.where(m.mkt_trend60 <= -TREND_TH, "下跌", "横盘")),
                     index=m.index)
    out = dirn + "·" + vol
    out[opp] = "funding对手方拥挤"
    out[hot] = "funding热门方拥挤"
    out[withfav] = "大单同向·" + vol[withfav]
    out[against] = "大单反向·" + vol[against]
    return out


def cost_and_pnl(m: pd.DataFrame) -> pd.DataFrame:
    col = m.coin.map(lambda c: SIZE_COL.get(c, DEFAULT_SIZE_COL))
    cost = pd.Series([m.at[i, c] for i, c in zip(m.index, col)], index=m.index, dtype=float)
    cost = cost.fillna(m.top_cost)
    won = ((m.fav == "yes") == (m.outcome_yes == 1)).astype(float)
    return pd.DataFrame({
        "cost": cost,
        "inband": (cost >= BAND[0]) & (cost <= BAND[1]),
        "fav_won": won,
        "pnl": 100 * (won - cost - FEE_MULT * cost * (1 - cost)),
    }, index=m.index)
