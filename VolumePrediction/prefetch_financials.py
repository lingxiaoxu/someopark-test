"""
prefetch_financials — fund 组前置: 批量拉取宇宙并集的 Polygon financials
======================================================================
与 pipeline_build 相同的宇宙并集口径(membership 逐日 ∪ strategy_symbols),
线程池并发调用 inhouse_loader.fetch_financials(自带月度新鲜度缓存 → 可续传;
中断重跑自动跳过已缓存票)。网络型任务,可与 MPS 训练并行。

用法: python -m VolumePrediction.prefetch_financials --start 2019-01-02 --end 2023-12-29 [--workers 8]
"""
from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from VolumePrediction.common import get_logger
from VolumePrediction.data import polygon_loader as pl
from VolumePrediction.data import universe as uni
from VolumePrediction.data.inhouse_loader import fetch_financials, FIN_DIR

log = get_logger("prefetch_fin")


def union_tickers(start: str, end: str) -> list:
    days = pl.trading_days(start, end)
    u: set = set().union(*({d: uni.membership(d) for d in days}).values())
    extra = uni.strategy_symbols()
    u |= extra["etf"] | extra["pairs"] | extra["aiss"]
    return sorted(u)


def main(start: str, end: str, workers: int) -> None:
    tickers = union_tickers(start, end)
    cached = {p.stem for p in FIN_DIR.glob("*.json")} if FIN_DIR.exists() else set()
    todo = [t for t in tickers if t not in cached]
    log.info(f"financials prefetch: {len(tickers)} union, "
             f"{len(cached)} cached, {len(todo)} to fetch")
    t0, done, empty = time.time(), 0, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_financials, t): t for t in todo}
        for f in as_completed(futs):
            t = futs[f]
            try:
                recs = f.result()
                if not recs:
                    empty += 1
            except Exception as e:  # noqa: BLE001
                log.warning(f"{t}: {e}")
            done += 1
            if done % 200 == 0:
                rate = done / (time.time() - t0)
                log.info(f"progress {done}/{len(todo)} "
                         f"({rate:.1f}/s, eta {(len(todo)-done)/max(rate,0.1):.0f}s, "
                         f"empty={empty})")
    log.info(f"prefetch done: {done} fetched, {empty} empty, "
             f"{time.time()-t0:.0f}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-02")
    ap.add_argument("--end", default="2023-12-29")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    main(a.start, a.end, a.workers)
