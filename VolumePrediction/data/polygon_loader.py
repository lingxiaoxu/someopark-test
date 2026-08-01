"""
polygon_loader — 行情唯一来源(Plan B §四/§7.1/§7.10)
====================================================
铁律: 只用 Polygon;显式禁用 yfinance(本模块及全包不 import yfinance)。

设计要点(全部来自计划,不简化):
- grouped-daily 端点 /v2/aggs/grouped/locale/us/market/stocks/{date},
  **adjusted=false 原始 bar**(原始成交事实不可变 → 缓存永久有效,拆股防未来)
- 缓存: price_data/volume_prediction/raw/grouped_{date}.parquet,一日一文件
- 复权不在此层——由 splits 表(splits_loader,复用 CorporateActions 函数)在面板构建时现算
- 限速: 温和节流 + 429 指数退避;网络 IO 为主,nice 启动不打扰共存管道
- 密钥: 只经 os.environ;任何日志/异常文本不得含 key(单测断言)
- 美元量定义(§7.1): V := v_shares × vw —— 在 load_day/load_range 输出中同时给出

CLI:
  python -m VolumePrediction.data.polygon_loader --backfill 2019-01-01 2026-07-23
  python -m VolumePrediction.data.polygon_loader --daily
  python -m VolumePrediction.data.polygon_loader --verify
"""
from __future__ import annotations

import os
import sys
import time
import json
import logging
from pathlib import Path
from typing import Optional, List

import requests
import pandas as pd

_PKG_DIR = Path(__file__).resolve().parent.parent          # VolumePrediction/
_REPO = _PKG_DIR.parent                                     # someopark-test/
RAW_DIR = _REPO / "price_data" / "volume_prediction" / "raw"
LOG_DIR = _PKG_DIR / "logs"

log = logging.getLogger("VolumePrediction.polygon")

_BASE = "https://api.polygon.io"
_GROUPED = _BASE + "/v2/aggs/grouped/locale/us/market/stocks/{date}"

THROTTLE_S = 0.15
MAX_RETRIES = 5
BACKOFF_BASE = 2.0


class PolygonKeyMissing(RuntimeError):
    pass


def _api_key() -> str:
    k = os.environ.get("POLYGON_API_KEY")
    if not k:
        # 尝试 .env(dotenv 方式,绕过 shell source 的 & 问题)
        try:
            from dotenv import load_dotenv
            load_dotenv(_REPO / ".env")
            k = os.environ.get("POLYGON_API_KEY")
        except Exception:
            pass
    if not k:
        raise PolygonKeyMissing("POLYGON_API_KEY not set (load .env first)")
    return k


def _sanitize(msg: str) -> str:
    """确保任何往外冒的文本不含 key。"""
    k = os.environ.get("POLYGON_API_KEY", "")
    return msg.replace(k, "<KEY>") if k else msg


def trading_days(start: str, end: str) -> List[str]:
    """NYSE 交易日(YYYY-MM-DD)。"""
    import pandas_market_calendars as mcal
    sched = mcal.get_calendar("NYSE").schedule(start_date=start, end_date=end)
    return [d.strftime("%Y-%m-%d") for d in sched.index]


def raw_path(date: str) -> Path:
    return RAW_DIR / f"grouped_{date}.parquet"


def _fetch_grouped_raw(date: str, session: Optional[requests.Session] = None) -> pd.DataFrame:
    """单日全市场原始 bar(adjusted=false)。空市场日返回空 DataFrame。"""
    sess = session or requests
    params = {"adjusted": "false", "apiKey": _api_key()}
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            r = sess.get(_GROUPED.format(date=date), params=params, timeout=30)
            if r.status_code == 429:
                wait = BACKOFF_BASE ** attempt
                log.warning(f"429 on {date}, backoff {wait:.0f}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            body = r.json()
            results = body.get("results") or []
            df = pd.DataFrame(results)
            if not df.empty:
                # Polygon 字段: T=ticker v=share volume vw o h l c t(ms) n
                df = df.rename(columns={"T": "ticker"})
                keep = [c for c in ("ticker", "v", "vw", "o", "h", "l", "c", "t", "n") if c in df.columns]
                df = df[keep]
                df["date"] = date
            return df
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(BACKOFF_BASE ** attempt)
    raise RuntimeError(_sanitize(f"grouped fetch failed for {date}: {last_err}"))


def ensure_day(date: str, session: Optional[requests.Session] = None,
               force: bool = False) -> Path:
    """确保某交易日原始 bar 已缓存;原始数据不可变 → 已存在即跳过。"""
    p = raw_path(date)
    if p.exists() and not force:
        return p
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    df = _fetch_grouped_raw(date, session)
    tmp = p.with_suffix(".tmp")
    df.to_parquet(tmp, index=False)
    tmp.rename(p)                                   # 原子落盘
    return p


def backfill(start: str, end: str, throttle: float = THROTTLE_S) -> dict:
    """历史回填;幂等(已缓存日跳过)。返回统计。"""
    days = trading_days(start, end)
    sess = requests.Session()
    stats = {"total": len(days), "fetched": 0, "cached": 0, "empty": 0, "t0": time.time()}
    for i, d in enumerate(days):
        p = raw_path(d)
        if p.exists():
            stats["cached"] += 1
            continue
        df_path = ensure_day(d, sess)
        n = len(pd.read_parquet(df_path))
        if n == 0:
            stats["empty"] += 1
        stats["fetched"] += 1
        if stats["fetched"] % 50 == 0:
            el = time.time() - stats["t0"]
            log.info(f"backfill {i+1}/{len(days)} fetched={stats['fetched']} "
                     f"pace={stats['fetched']/max(el,1):.1f} req/s")
        time.sleep(throttle)
    stats["elapsed_s"] = round(time.time() - stats["t0"], 1)
    return stats


def load_day(date: str) -> pd.DataFrame:
    """读单日原始 bar;附 dollar_volume=v×vw(§7.1 量纲)。"""
    p = raw_path(date)
    if not p.exists():
        raise FileNotFoundError(f"raw cache missing for {date}; run backfill")
    df = pd.read_parquet(p)
    if not df.empty and "v" in df.columns and "vw" in df.columns:
        df["dollar_volume"] = df["v"].astype(float) * df["vw"].astype(float)
    return df


def load_range(start: str, end: str, tickers: Optional[set] = None) -> pd.DataFrame:
    """按日拼接 [start,end] 的原始 bar 长表(可选 ticker 过滤)。"""
    frames = []
    for d in trading_days(start, end):
        p = raw_path(d)
        if not p.exists():
            continue
        df = pd.read_parquet(p)
        if df.empty:
            continue
        if tickers is not None:
            df = df[df["ticker"].isin(tickers)]
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["dollar_volume"] = out["v"].astype(float) * out["vw"].astype(float)
    return out


def coverage(start: str, end: str) -> dict:
    days = trading_days(start, end)
    missing = [d for d in days if not raw_path(d).exists()]
    return {"days": len(days), "cached": len(days) - len(missing),
            "missing": missing[:20], "n_missing": len(missing)}


def main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="VolumePrediction Polygon grouped-daily loader")
    ap.add_argument("--backfill", nargs=2, metavar=("START", "END"))
    ap.add_argument("--daily", action="store_true", help="fetch latest trading day")
    ap.add_argument("--verify", nargs=2, metavar=("START", "END"))
    ap.add_argument("--throttle", type=float, default=THROTTLE_S)
    args = ap.parse_args()

    if args.backfill:
        stats = backfill(args.backfill[0], args.backfill[1], throttle=args.throttle)
        print(json.dumps(stats, ensure_ascii=False))
    elif args.daily:
        today = pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d")
        days = trading_days("2026-01-01", today)
        d = days[-1]
        ensure_day(d)
        print(f"ensured {d}: {len(load_day(d))} tickers")
    elif args.verify:
        print(json.dumps(coverage(args.verify[0], args.verify[1]), ensure_ascii=False))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
