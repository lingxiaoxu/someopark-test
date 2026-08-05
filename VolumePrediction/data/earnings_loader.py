"""
earnings_loader — 财报日历(复用 MRPTFetchEarnings,§6.4)
=======================================================
主源=根目录 MRPTFetchEarnings 的 Polygon financials 链路(fetch_earnings_for_symbol/
run_fetch),import 复用一行不改;**自有缓存**(不写共享 price_data/earnings_cache.json)。
前瞻日历(未来财报日,earn_ 组分桶需要)=Mongo fmp_historical_earning_calendar(→2027)。
Mongo 日期字段为 BSON datetime——查询必须用 datetime 对象(§6.5 工程要点)。
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from VolumePrediction.common import REPO, DATA_ROOT, get_logger

log = get_logger("earnings")
CACHE = DATA_ROOT / "earnings" / "earnings_cache_vp.json"

sys.path.insert(0, str(REPO))
# MRPTFetchEarnings 在 import 期读 os.environ['POLYGON_API_KEY'](其模块级行为,
# 我们不改它一行)→ 先加载 .env 再 import
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO / ".env")
# MFE 内部 HTTP 调用未必带 timeout——死连接会永久阻塞(2026-07-23 实测两次挂 4h)。
# socket.setdefaulttimeout 无效: urllib3 会显式 settimeout(None) 覆盖全局默认。
# 唯一可靠兜底 = 在 requests 会话层注入默认 timeout(只在调用方未给时生效,
# 不改 MFE 一行,也不影响显式传 timeout 的调用)。
import requests as _rq  # noqa: E402
_orig_request = _rq.sessions.Session.request

def _request_with_default_timeout(self, method, url, **kw):
    kw.setdefault("timeout", 60)
    return _orig_request(self, method, url, **kw)

_rq.sessions.Session.request = _request_with_default_timeout
import MRPTFetchEarnings as MFE  # noqa: E402  只读复用


def _load_cache() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {"fetched_at": None, "symbols": {}}


def _save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.rename(CACHE)


def fetch_symbols(symbols: List[str], refresh_days: int = 3) -> dict:
    """批量确保 symbols 的历史财报日在自有缓存(增量;复用 MFE.run_fetch 不落共享盘)。

    MFE.run_fetch(symbols, cache) 返回更新后的 cache dict(其 save 由我方执行)。
    """
    cache = _load_cache()
    today = str(date.today())
    # meta 迁移: 早期批次记录无 _meta(进程中断场景)——记录存在即视为今日新鲜,
    # 避免整批重拉;后续 refresh_days 逻辑照常
    cache.setdefault("_meta", {})
    for s_ in list(cache.get("symbols", {})):
        cache["_meta"].setdefault(s_, {"fetched_at": today})
    need = []
    for s in symbols:
        rec = cache["symbols"].get(s)
        if rec is None:
            need.append(s)
            continue
        meta = cache.get("_meta", {}).get(s, {})
        if meta.get("fetched_at", "1970") < str(date.today() - timedelta(days=refresh_days)):
            need.append(s)
    if need:
        # 分块 + 每块落盘(2026-08-04): 逐票节流 0.3-0.8s,3869 票要 35-60 分钟。
        # 原先只在整批结束后 _save_cache 一次 —— 任何中断(进程被杀/重启/
        # jetsam)都让这段工作全部作废,下次从零开始,实测导致永远跑不完。
        # 现在每 CHUNK 票存一次盘并打进度,中断只损失当前块。
        log.warning(f"earnings fetch for {len(need)} symbols — 三天一次的全量刷新, "
                    f"分块落盘,预计 {len(need) * 2.5 / 3600:.1f} 小时。"
                    f"看到本行说明日更会跑很久,**不要杀进程**,看下方进度行。")
        CHUNK = 250
        done = 0
        for i in range(0, len(need), CHUNK):
            batch = need[i:i + CHUNK]
            updated = MFE.run_fetch(batch, cache=cache, quiet=True)
            cache = updated if isinstance(updated, dict) else cache
            cache.setdefault("_meta", {})
            cache.setdefault("symbols", {})
            for s in batch:
                # 空结果也落缓存([]),否则无财报票(退市/未覆盖)每次构建全量重拉
                cache["symbols"].setdefault(s, [])
                cache["_meta"][s] = {"fetched_at": today}
            done += len(batch)
            cache["fetched_at"] = today
            _save_cache(cache)                       # 检查点
            log.warning(f"  earnings 进度 {done}/{len(need)} 已落盘")
    return cache


def historical_dates(symbols: List[str]) -> Dict[str, List[date]]:
    """symbol → 历史 earnings_date 列表(升序)。"""
    cache = fetch_symbols(symbols)
    out: Dict[str, List[date]] = {}
    for s in symbols:
        recs = cache["symbols"].get(s, []) or []
        ds = sorted({r["earnings_date"] for r in recs if r.get("earnings_date")})
        out[s] = [date.fromisoformat(d) for d in ds]
    return out


def release_timing(symbols: List[str]) -> Dict[str, Dict[str, str]]:
    """symbol → {earnings_date: BMO/AMC/INTRADAY/UNKNOWN}(earnings_zero 精化,§6.8)。"""
    cache = fetch_symbols(symbols)
    out: Dict[str, Dict[str, str]] = {}
    for s in symbols:
        out[s] = {r["earnings_date"]: r.get("release_timing", "UNKNOWN")
                  for r in cache["symbols"].get(s, []) or [] if r.get("earnings_date")}
    return out


def future_dates(symbols: List[str], horizon_days: int = 400) -> Dict[str, List[date]]:
    """前瞻日历: Mongo fmp_historical_earning_calendar 未来窗(→2027);datetime 查询。"""
    from dotenv import load_dotenv
    import os
    load_dotenv(REPO / ".env")
    from pymongo import MongoClient
    cli = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=10000,
                      socketTimeoutMS=60000, connectTimeoutMS=20000)
    col = cli["someopark"]["fmp_historical_earning_calendar"]
    t0 = datetime.combine(date.today(), datetime.min.time())
    t1 = t0 + timedelta(days=horizon_days)
    out: Dict[str, List[date]] = {s: [] for s in symbols}
    cur = col.find({"symbol": {"$in": list(symbols)},
                    "date": {"$gte": t0, "$lte": t1}},
                   {"symbol": 1, "date": 1})
    for doc in cur:
        out[doc["symbol"]].append(doc["date"].date())
    return {s: sorted(set(v)) for s, v in out.items()}


def all_dates(symbols: List[str]) -> Dict[str, List[date]]:
    """历史(Polygon/SEC,含退市)∪ 未来(FMP 前瞻);earn_ 组分桶的完整输入。"""
    hist = historical_dates(symbols)
    fut = future_dates(symbols)
    return {s: sorted(set(hist.get(s, [])) | set(fut.get(s, []))) for s in symbols}
