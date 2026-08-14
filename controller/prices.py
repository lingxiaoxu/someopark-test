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
from zoneinfo import ZoneInfo

import requests

from controller.registry import Registry, RegistryError

_API = "https://api.polygon.io"
_BATCH = 100                       # snapshot tickers 每批上限(保守)
_ET = ZoneInfo("America/New_York")
_BACKFILL_CAP = 40                 # 备通道单 tick 上限(逐票调用,防限速;≥全书 31)


def et_today() -> str:
    return datetime.now(_ET).strftime("%Y-%m-%d")


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
        backfilled = []
        if missing:                                   # 备通道:分钟聚合补缺口
            got = self.minute_backfill(missing[:_BACKFILL_CAP])
            for isin, (price, ts) in got.items():
                prices[isin] = price
                if ts:
                    tss[isin] = ts
                backfilled.append(isin)
            missing = [i for i in isins if i not in prices]
        delay = None
        if tss:
            newest = max(tss.values())
            delay = round((time.time() - newest) / 60.0, 1)
        return {"prices": prices, "ts": tss, "missing": missing,
                "backfilled": backfilled, "feed_delay_min": delay,
                "asof": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    # ── 备通道:分钟聚合(同 Polygon,不算换源;plan §三)──────────────────────
    def minute_backfill(self, isins: list[str]) -> dict:
        """快照缺口的票逐票补价 → {isin: (close, epoch_s)}。
        当日最新分钟 bar,当日无 bar(如 snapshot 3:30am ET 重置窗、盘前无成交)
        退到 /prev 前收(仍是 Polygon,闭市估值的正确价)。
        单票失败跳过(留给 missing → stale 语义),绝不注入假价。"""
        out = {}
        day = et_today()
        for isin in isins:
            rec = self.reg.master.get(isin)
            if rec is None:
                continue
            t = rec["polygon_ticker"]
            try:
                d = self._get(f"/v2/aggs/ticker/{t}/range/1/minute/{day}/{day}",
                              {"sort": "desc", "limit": 1}, retries=1)
                bars = d.get("results") or []
                if not bars:
                    d = self._get(f"/v2/aggs/ticker/{t}/prev", retries=1)
                    bars = d.get("results") or []
            except Exception:  # noqa: BLE001 — 单票缺口保持 missing
                continue
            if bars and bars[0].get("c"):
                out[isin] = (float(bars[0]["c"]), _to_epoch_s(bars[0].get("t")))
        return out

    # ── 官方日收盘(对账独立价源路径;/v2/aggs 日 bar = Polygon 官方 close)────
    def daily_close(self, isins: list[str], date_iso: str) -> dict:
        """→ {isin: official_close} 指定交易日的日 bar 收盘价(与盘中 snapshot
        的 lastTrade 路径独立,供持仓级对账重算)。缺 bar 的票跳过(调用方上报)。"""
        out = {}
        for isin in isins:
            rec = self.reg.master.get(isin)
            if rec is None:
                continue
            t = rec["polygon_ticker"]
            try:
                d = self._get(f"/v2/aggs/ticker/{t}/range/1/day/{date_iso}/{date_iso}",
                              {"limit": 1}, retries=2)
            except Exception:  # noqa: BLE001
                continue
            bars = d.get("results") or []
            if bars and bars[0].get("c"):
                out[isin] = float(bars[0]["c"])
        return out

    # ── 指定日全部分钟 bar(对账 A:时点同步完整性检查用)────────────────────
    def minute_closes(self, isins: list[str], date_iso: str) -> dict:
        """→ {isin: [(epoch_s, close), …]} 该日分钟 bar(升序)。
        单票失败/无 bar → 空列表(调用方按 skipped 上报,不注入假价)。"""
        out: dict[str, list] = {}
        for isin in isins:
            rec = self.reg.master.get(isin)
            if rec is None:
                out[isin] = []
                continue
            t = rec["polygon_ticker"]
            try:
                d = self._get(f"/v2/aggs/ticker/{t}/range/1/minute/{date_iso}/{date_iso}",
                              {"sort": "asc", "limit": 50000}, retries=2)
                out[isin] = [(_to_epoch_s(b["t"]), float(b["c"]))
                             for b in (d.get("results") or []) if b.get("c")]
            except Exception:  # noqa: BLE001
                out[isin] = []
        return out

    # ── 当日 split 日历(plan §九-7:标注不改 shares,持仓文件是 golden)──────
    def splits_today(self, isins: list[str]) -> dict:
        """→ {isin: "from:to"} 当日 execution_date 落在全书内的 split。"""
        t2i = {self.reg.master[i]["polygon_ticker"]: i
               for i in isins if i in self.reg.master}
        d = self._get("/v3/reference/splits",
                      {"execution_date": et_today(), "limit": 1000})
        out = {}
        for row in d.get("results", []):
            isin = t2i.get(row.get("ticker"))
            if isin:
                out[isin] = f'{row.get("split_from")}:{row.get("split_to")}'
        return out


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
