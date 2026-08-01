"""
splits_loader — 拆合股(复用 CorporateActions,§6.4/§7.10)
========================================================
集中复用裁定: 只 import 根目录 CorporateActions 的 fetch_all_splits/adjust_price_df,
一行不改;**共享 price_data/splits_cache.json 不读不写**(use_cache=False + 自有缓存
price_data/volume_prediction/splits/splits_cache.json;adjust 一律显式传 splits)。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

from VolumePrediction.common import REPO, DATA_ROOT, get_logger

log = get_logger("splits")
CACHE = DATA_ROOT / "splits" / "splits_cache.json"

sys.path.insert(0, str(REPO))
from CorporateActions import fetch_all_splits, adjust_price_df  # noqa: E402  只读复用


def refresh(since: str = "2019-01-01") -> list:
    """全市场 splits(含未来预告)→ 自有缓存。当天已刷新则直接读缓存。"""
    today = str(date.today())
    if CACHE.exists():
        try:
            c = json.loads(CACHE.read_text())
            if c.get("fetched_at") == today and c.get("since", "9999") <= since:
                return c["results"]
        except Exception:  # noqa: BLE001
            pass
    results = fetch_all_splits(since, use_cache=False)   # 不碰共享缓存
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched_at": today, "since": since,
                               "results": results}))
    tmp.rename(CACHE)
    log.info(f"splits refreshed: {len(results)} records since {since}")
    return results


def splits_for(ticker: str, since: str = "2019-01-01") -> list:
    return [s for s in refresh(since) if s.get("ticker") == ticker]


def adjust(df: pd.DataFrame, ticker: str,
           price_cols=("o", "h", "l", "c", "vw"),
           volume_col: str = "v",
           since: str = "2019-01-01") -> tuple[pd.DataFrame, int]:
    """原始 bar → 当前口径(价÷factor、量×factor);显式传 splits,不读共享缓存。

    §7.1: dollar_volume=v×vw 拆股不变——调整后应重算以保数值一致性
    (÷f 与 ×f 相消,重算仅消浮点误差)。df 需 DatetimeIndex。
    """
    sp = splits_for(ticker, since)
    df2, n = adjust_price_df(df, ticker, splits=sp,
                             price_cols=[c for c in price_cols if c in df.columns],
                             volume_col=volume_col)
    if "v" in df2.columns and "vw" in df2.columns:
        df2["dollar_volume"] = df2["v"].astype(float) * df2["vw"].astype(float)
    return df2, n
