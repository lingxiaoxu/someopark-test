"""
controller/prices.py — 价格层(plan §三;M3)。Polygon 唯一数据源。

主通道:批量全市场 snapshot(一次调用覆盖全书叶子;220 票远离限速)。
备通道:分钟聚合(同 Polygon,补缺口/回填,不算换源)。
边界:引擎只见 ISIN——出站 ISIN→polygon_ticker(security master),回站即换回。
失败语义:指数退避重试;连续失败该 tick 标 stale(沿用 last_price,不注入假价,
          绝不换源);市场状态用 Polygon /v1/marketstatus/now(同源)。
新鲜度:每次 snapshot 返回逐票时间戳,计算 feed 延迟(实时 vs delayed 订阅),
        如实标注 feed_delay_min,不隐瞒(plan §三)。
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import requests

from controller.registry import Registry, RegistryError

_API = "https://api.polygon.io"
_BATCH = 100                       # snapshot tickers 每批上限(保守)


def _to_epoch_s(ts) -> float | None:
    """Polygon 时间戳单位不一(lastTrade=纳秒, min/day=毫秒)→ 统一秒。"""
    if not ts:
        return None
    ts = float(ts)
    if ts > 1e17:                  # ns
        return ts / 1e9
    if ts > 1e14:                  # µs
        return ts / 1e6
    if ts > 1e11:                  # ms
        return ts / 1e3
    return ts                      # s


class PriceFeed:
    def __init__(self, registry: Registry | None = None):
        self.reg = registry or Registry()
        self.key = os.environ.get("POLYGON_API_KEY")
        if not self.key:
            raise RegistryError("POLYGON_API_KEY not visible — source .env first")
        self.consecutive_failures = 0

    # ── HTTP(退避重试,不换源)────────────────────────────────────────────────
    def _get(self, path: str, params: dict | None = None, retries: int = 3) -> dict:
        params = dict(params or {})
        params["apiKey"] = self.key
        last = None
        for attempt in range(retries):
            try:
                r = requests.get(_API + path, params=params, timeout=20)
                if r.status_code == 429:                  # 限速:必须退避
                    raise RuntimeError("429 rate limited")
                r.raise_for_status()
                self.consecutive_failures = 0
                return r.json()
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2 ** attempt)
        self.consecutive_failures += 1
        raise RuntimeError(f"polygon GET {path} failed after {retries}: {last}")

    # ── 市场状态(同源日历)───────────────────────────────────────────────────
    def market_status(self) -> dict:
        d = self._get("/v1/marketstatus/now")
        return {"market": d.get("market"),               # open|closed|extended-hours
                "server_time": d.get("serverTime")}

    # ── 批量快照:ISIN 进、ISIN 出 ───────────────────────────────────────────
    def snapshot(self, isins: list[str]) -> dict:
        """→ {"prices": {isin: price}, "ts": {isin: epoch_ms}, "missing": [isin],
             "feed_delay_min": float|None, "asof": iso}
        价格取优先级:最新成交 lastTrade.p → 当日分钟 bar min.c → 日 bar day.c
        (盘后 lastTrade 即收盘附近成交;三者全缺 → missing,由调用方 stale 处理)。"""
        t2i = {}
        for isin in isins:
            rec = self.reg.master.get(isin)
            if rec is None:
                raise RegistryError(f"ISIN {isin} not in security master")
            t2i[rec["polygon_ticker"]] = isin
        tickers = sorted(t2i)
        prices, tss = {}, {}
        for i in range(0, len(tickers), _BATCH):
            chunk = tickers[i:i + _BATCH]
            d = self._get("/v2/snapshot/locale/us/markets/stocks/tickers",
                          {"tickers": ",".join(chunk)})
            for row in d.get("tickers", []):
                isin = t2i.get(row.get("ticker"))
                if isin is None:
                    continue
                lt = row.get("lastTrade") or {}
                mn = row.get("min") or {}
                day = row.get("day") or {}
                price, ts = None, None
                if lt.get("p"):
                    price, ts = float(lt["p"]), _to_epoch_s(lt.get("t"))
                elif mn.get("c"):
                    price, ts = float(mn["c"]), _to_epoch_s(mn.get("t"))
                elif day.get("c"):
                    price = float(day["c"])
                if price is not None and price > 0:
                    prices[isin] = price
                    if ts:
                        tss[isin] = ts
        missing = [i for i in isins if i not in prices]
        delay = None
        if tss:
            newest = max(tss.values())
            delay = round((time.time() - newest) / 60.0, 1)
        return {"prices": prices, "ts": tss, "missing": missing,
                "feed_delay_min": delay,
                "asof": datetime.now(timezone.utc).isoformat(timespec="seconds")}


if __name__ == "__main__":
    import argparse
    import json as _json
    from controller.model import assemble
    ap = argparse.ArgumentParser(description="polygon price feed (M3)")
    ap.add_argument("--test", action="store_true", help="拉一次全书快照并报告新鲜度")
    a = ap.parse_args()
    if a.test:
        reg = Registry()
        S = assemble(reg)
        feed = PriceFeed(reg)
        st = feed.market_status()
        snap = feed.snapshot(sorted(S.leaves()))
        miss = [reg.render(m) for m in snap["missing"]]
        print(f"market={st['market']}  got {len(snap['prices'])}/{len(S.leaves())} "
              f"prices  missing={miss}")
        print(f"feed_delay_min={snap['feed_delay_min']}  (实时订阅应 <1;delayed ≈15)")
        sample = sorted(snap["prices"])[:5]
        print("样例:", {reg.render(i): snap["prices"][i] for i in sample})
