"""Stable Hyperliquid hourly funding cache for the 5 coins.

Primary source: the public Hyperliquid info API `fundingHistory` (settled hourly
rates, stamped at the settlement time, typically HH:00:00.000-.150). Each run
fetches only what is newer than the cache. If the API is unreachable, the hours
are filled from our own recorder (price_data/hyperliquid/context, field
`funding`, last value seen before the hour) and tagged source='recorder'; the
next successful API run replaces them with the settled values.

    python -m crypto_trading.crypto_strategies.w7_scenarios.funding refresh
    python -m crypto_trading.crypto_strategies.w7_scenarios.funding status
"""
from __future__ import annotations

import argparse
import gzip
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .config import COINS, FUNDING_CACHE, HL

HOUR_MS = 3_600_000
KEEP_DAYS = 60
COLS = ["coin", "time_ms", "funding", "premium", "source"]


def _now_ms() -> int:
    return int(time.time() * 1000)


def load(path: Path = FUNDING_CACHE) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=COLS)
    df = pd.read_csv(path)
    df["time_ms"] = df["time_ms"].astype("int64")
    return df[COLS]


def _save(df: pd.DataFrame, path: Path = FUNDING_CACHE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.to_csv(tmp, index=False)
    tmp.replace(path)


def _api_history(coin: str, start_ms: int, end_ms: int, retries: int = 3) -> list[dict] | None:
    from crypto_trading.crypto_common.refdata.hyperliquid import Info
    cli = Info()
    rows, start = [], start_ms
    while start < end_ms:
        page = None
        for attempt in range(retries):
            page = cli.post({"type": "fundingHistory", "coin": coin, "startTime": start, "endTime": end_ms}, timeout=15.0)
            if page is not None:
                break
            time.sleep(2 * (attempt + 1))
        if page is None:
            return None                       # API failed for this coin
        if not page:
            break
        rows += [dict(coin=coin, time_ms=int(x["time"]), funding=float(x["fundingRate"]),
                      premium=float(x.get("premium") or 0.0), source="api") for x in page]
        last = int(page[-1]["time"])
        if last < start:
            break
        start = last + 1
        time.sleep(0.25)
    return rows


def _recorder_hours(coin: str, after_ms: int, until_ms: int) -> list[dict]:
    """Fallback: the recorder's live `funding` field just before each hour."""
    out = []
    first_hour = (after_ms // HOUR_MS + 1) * HOUR_MS
    hours = list(range(first_hour, until_ms + 1, HOUR_MS))
    if not hours:
        return out
    days = sorted({datetime.fromtimestamp(h / 1000, timezone.utc).strftime("%Y-%m-%d") for h in hours})
    pts = []
    for day in days:
        for p in (HL / "context" / coin / f"{day}.jsonl.gz", HL / "context" / coin / f"{day}.jsonl"):
            if not p.exists():
                continue
            with (gzip.open(p, "rt") if p.suffix == ".gz" else open(p)) as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                        pts.append((float(r["recv_ts"]), float(r["funding"])))
                    except (ValueError, KeyError, TypeError):
                        continue
            break
    pts.sort()
    j = 0
    for h in hours:
        hs = h / 1000
        while j + 1 < len(pts) and pts[j + 1][0] < hs:
            j += 1
        if pts and pts[j][0] < hs and hs - pts[j][0] <= 120:
            out.append(dict(coin=coin, time_ms=h, funding=pts[j][1], premium=0.0, source="recorder"))
    return out


def refresh(path: Path = FUNDING_CACHE, default_days: int = 40) -> dict:
    df = load(path)
    now = _now_ms()
    report = {}
    new = []
    for coin in COINS:
        have = df[(df.coin == coin) & (df.source == "api")]
        start = int(have.time_ms.max()) + 1 if len(have) else now - default_days * 86_400_000
        rows = _api_history(coin, start, now)
        if rows is None:
            last_any = df[df.coin == coin].time_ms.max() if (df.coin == coin).any() else start
            fb = _recorder_hours(coin, int(last_any), now)
            new += fb
            report[coin] = f"api_failed, recorder_fill={len(fb)}"
        else:
            new += rows
            report[coin] = f"api_rows={len(rows)}"
    if new:
        add = pd.DataFrame(new, columns=COLS)
        df = add if df.empty else pd.concat([df, add], ignore_index=True)
    # one row per coin-hour; settled API values replace recorder fills
    df["hour"] = df.time_ms // HOUR_MS
    df["rank"] = (df.source != "api").astype(int)
    df = df.sort_values(["coin", "hour", "rank", "time_ms"]).drop_duplicates(["coin", "hour"], keep="first")
    df = df[df.time_ms >= now - KEEP_DAYS * 86_400_000].drop(columns=["hour", "rank"])
    df = df.sort_values(["coin", "time_ms"]).reset_index(drop=True)
    _save(df, path)
    return report


def health(df: pd.DataFrame, now_ms: int | None = None, max_age_h: float = 2.0) -> list[str]:
    """Problems that make the cache unusable: stale coins or missing hours."""
    now_ms = now_ms or _now_ms()
    problems = []
    for coin in COINS:
        g = df[df.coin == coin].sort_values("time_ms")
        if g.empty:
            problems.append(f"funding {coin}: no data")
            continue
        age_h = (now_ms - g.time_ms.iloc[-1]) / HOUR_MS
        if age_h > max_age_h:
            problems.append(f"funding {coin}: latest settlement {age_h:.1f}h old")
        hours = (g.time_ms // HOUR_MS).to_numpy()
        gaps = int(((hours[1:] - hours[:-1]) > 1).sum())
        if gaps:
            problems.append(f"funding {coin}: {gaps} missing-hour gap(s)")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["refresh", "status"])
    a = ap.parse_args(argv)
    if a.cmd == "refresh":
        print(json.dumps(refresh(), ensure_ascii=False))
    df = load()
    probs = health(df)
    print(f"rows={len(df)} recorder_rows={(df.source == 'recorder').sum()} problems={probs or 'none'}")
    return 1 if probs else 0


if __name__ == "__main__":
    raise SystemExit(main())
