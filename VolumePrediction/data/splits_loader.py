"""
splits_loader — 拆合股(复用 CorporateActions,§6.4/§7.10)
========================================================
集中复用裁定: 只 import 根目录 CorporateActions 的 fetch_all_splits/adjust_price_df,
一行不改;**共享 price_data/splits_cache.json 不读不写**(use_cache=False + 自有缓存
price_data/volume_prediction/splits/splits_cache.json;adjust 一律显式传 splits)。
"""
from __future__ import annotations

import json
import logging
import math
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date

import pandas as pd

from VolumePrediction.common import REPO, DATA_ROOT, get_logger

log = get_logger("splits")
CACHE = DATA_ROOT / "splits" / "splits_cache.json"

sys.path.insert(0, str(REPO))
from CorporateActions import fetch_all_splits, adjust_price_df  # noqa: E402  只读复用


_RETRY_SECONDS = 300
_failed_refresh_at: dict[tuple[str, str], float] = {}


def _valid_results(results) -> bool:
    """全市场长历史应非空; 空响应也可能是共享 fetch 吞掉异常后的失败值。"""
    if not isinstance(results, list) or not results:
        return False
    try:
        for row in results:
            if not isinstance(row, dict) or not isinstance(row.get("ticker"), str):
                return False
            if not row["ticker"]:
                return False
            date.fromisoformat(row["execution_date"])
            for field in ("split_from", "split_to"):
                value = float(row[field])
                if not math.isfinite(value) or value <= 0:
                    return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return True


def _read_usable_cache(since: str, today: str) -> dict | None:
    try:
        cached = json.loads(CACHE.read_text())
        if not isinstance(cached, dict):
            return None
        fetched_at = date.fromisoformat(cached["fetched_at"])
        covered_since = date.fromisoformat(cached["since"])
        if fetched_at > date.fromisoformat(today) or covered_since > date.fromisoformat(since):
            return None
        return cached if _valid_results(cached.get("results")) else None
    except (OSError, KeyError, TypeError, ValueError):
        return None


@contextmanager
def _quiet_fetch_logs():
    """共享 fetch 的异常文本可能含 apiKey; VP 调用线程只输出自己的安全告警。

    不改共享实现/handler，也不抑制其他线程; finally 恢复原 logger filters。
    """
    caller = threading.get_ident()

    class FetchFilter(logging.Filter):
        def filter(self, record):
            return record.thread != caller

    upstream = logging.getLogger("corporate_actions")
    guard = FetchFilter()
    upstream.addFilter(guard)
    try:
        yield
    finally:
        upstream.removeFilter(guard)


def refresh(since: str = "2019-01-01") -> list:
    """全市场 splits → 自有缓存; 只有有效非空结果能刷新 fetched_at。

    上游失败或空响应时保留覆盖请求范围的旧缓存和原日期并告警; 无有效
    缓存则明确失败。空响应保守视为无效: 共享 fetch 的全市场查询至少从
    2025-05-01 起，按多年历史非空约束保守拒绝空响应。逐 ticker 失败重试在进程
    内冷却 5 分钟，避免网络故障时重复数千次请求; 不伪造缓存新鲜度。
    """
    since = date.fromisoformat(since).isoformat()
    today = str(date.today())
    cached = _read_usable_cache(since, today)
    if cached is not None and cached["fetched_at"] == today:
        return cached["results"]
    retry_key = (str(CACHE), since)
    last_failure = _failed_refresh_at.get(retry_key)
    if last_failure is not None and time.monotonic() - last_failure < _RETRY_SECONDS:
        if cached is not None:
            return cached["results"]
        raise RuntimeError("VP splits refresh unavailable: no valid covering cache; retry cooling down")

    reason = "empty or invalid market-wide response"
    try:
        with _quiet_fetch_logs():
            results = fetch_all_splits(since, use_cache=False)   # 不碰共享缓存
    except Exception:  # noqa: BLE001 — 不输出上游异常文本/URL 中的 key
        results = None
        reason = "upstream fetch failed"
    if not _valid_results(results):
        _failed_refresh_at[retry_key] = time.monotonic()
        if cached is not None:
            log.warning("splits refresh degraded: %s; retaining %d records fetched_at=%s; "
                        "cache freshness unchanged", reason, len(cached["results"]), cached["fetched_at"])
            return cached["results"]
        log.error("splits refresh failed: %s; no valid covering VP cache", reason)
        raise RuntimeError("VP splits refresh failed: no valid covering cache") from None

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched_at": today, "since": since,
                               "results": results}))
    tmp.rename(CACHE)
    _failed_refresh_at.pop(retry_key, None)
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
