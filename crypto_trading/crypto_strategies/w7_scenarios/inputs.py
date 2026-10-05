"""Inputs for the W7 scenario tables. Every value is known at its decision time:
strike-series features at window open, funding settled at/before window open,
order-flow features from rows received at/before the entry snapshot.

Closed UTC days are parsed once and cached (parquet) under CACHE; the current day
is always recomputed. A cache file records the size of every source file it was
built from and is rebuilt if any of them changed.
"""
from __future__ import annotations

import bisect
import gzip
import json
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .config import (CACHE, CENTRES, COINS, EDGES, FUND_BASE, HL, LABELS, SNAPSHOT,
                     STRIPS, VERSION, W7_STATE)

from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")         # Kalshi tickers are stamped in US Eastern time (DST-aware)


# ── small helpers ───────────────────────────────────────────────────────────
def day_str(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def days_between(start_ts: float, end_ts: float) -> list[str]:
    d0 = datetime.fromtimestamp(start_ts, timezone.utc).date()
    d1 = datetime.fromtimestamp(end_ts, timezone.utc).date()
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((d1 - d0).days + 1)]


def _source(path_base: Path) -> Path | None:
    """The day file, rotated (.jsonl.gz) or live (.jsonl)."""
    for p in (path_base.with_name(path_base.name + ".jsonl.gz"), path_base.with_name(path_base.name + ".jsonl")):
        if p.exists():
            return p
    return None


def _open(p: Path):
    return gzip.open(p, "rt") if p.suffix == ".gz" else open(p)


def _closed(p: Path | None) -> bool:
    return p is not None and p.suffix == ".gz"


_FP: dict[str, str] = {}


def code_fingerprint(kind: str) -> str:
    """Hash of every function whose output a cache of this kind stores, including the
    live-module helpers it reuses — editing any of them invalidates the cache."""
    if kind not in _FP:
        import hashlib
        import inspect
        if kind == "strips":
            from crypto_trading.crypto_strategies.live_watch.w7_noisefade import walk_ladder
            src = inspect.getsource(walk_ladder) + inspect.getsource(_parse_strips)
        else:
            from crypto_trading.crypto_strategies.downside_paper import features
            src = inspect.getsource(features) + inspect.getsource(_flow_for)
        _FP[kind] = hashlib.sha256(src.encode()).hexdigest()[:16]
    return _FP[kind]


def _cached(kind: str, coin: str, day: str, sources: list[Path | None], build):
    """Parquet cache for closed days; source sizes and the code fingerprint are the key."""
    sig = {str(s): (s.stat().st_size if s is not None else None) for s in sources}
    sig["code"] = code_fingerprint(kind)
    closed = all(_closed(s) for s in sources if s is not None) and any(s is not None for s in sources)
    path = CACHE / kind / coin / f"{day}.parquet"
    meta = path.with_suffix(".json")
    if closed and path.exists() and meta.exists():
        try:
            m = json.loads(meta.read_text())
            if m.get("version") == VERSION and m.get("sources") == sig:
                return pd.read_parquet(path)
        except (OSError, ValueError):
            pass
    df = build()
    if closed:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Per-writer temp names (2026-10-05): W7 prod, W11, W13 and the 4-hourly table job
        # can rebuild the same day on the same tick; a shared "<day>.tmp" made one writer's
        # rename fail (FileNotFoundError in W11/W13 decisions at T-9.75). Both files are
        # replaced atomically; the last writer wins.
        tag = f"{os.getpid()}.{uuid.uuid4().hex[:8]}"
        tmp = path.with_name(f"{path.stem}.{tag}.tmp")
        df.to_parquet(tmp, index=False)
        tmp.replace(path)
        mtmp = meta.with_name(f"{meta.stem}.{tag}.meta.tmp")
        mtmp.write_text(json.dumps({"version": VERSION, "sources": sig}))
        mtmp.replace(meta)
    return df


# ── Kalshi 15-minute orderbook snapshots (90 s recorder) ────────────────────
def _parse_strips(coin: str, src: Path | None) -> pd.DataFrame:
    from crypto_trading.crypto_strategies.live_watch.w7_noisefade import walk_ladder
    rows = []
    if src is not None:
        with _open(src) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                ob = (r.get("ob") or {}).get("orderbook_fp") or {}
                ct, strike = r.get("close_time"), r.get("strike")
                if not ct or strike is None or not ob:
                    continue
                close = datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
                rem = (close - r["recv_ts"]) / 60.0
                if not 0.0 < rem <= 15.0:
                    continue
                w = {s: {n: walk_ladder(ob, s, n) for n in (25, 40, 60)} for s in ("yes", "no")}
                if not w["yes"][25] or not w["no"][25]:
                    continue
                ya = w["yes"][25]["top_cost"]; yb = round(1.0 - w["no"][25]["top_cost"], 4)
                if not (0 < yb < 1 and 0 < ya < 1):
                    continue
                mid = (ya + yb) / 2
                fav = "yes" if mid > 0.5 else "no" if mid < 0.5 else None
                if fav is None:
                    continue
                row = dict(coin=coin, ticker=r.get("ticker"), close_ts=int(round(close)), recv_ts=float(r["recv_ts"]),
                           rem_min=rem, strike=float(strike), fav=fav, top_cost=w[fav][25]["top_cost"])
                for n in (25, 40, 60):
                    x = w[fav][n]
                    row[f"fill{n}"] = x.get("fill_cost") if x and not x.get("shortfall") else np.nan
                rows.append(row)
    cols = ["coin", "ticker", "close_ts", "recv_ts", "rem_min", "strike", "fav", "top_cost", "fill25", "fill40", "fill60"]
    return pd.DataFrame(rows, columns=cols)


def strips_day(coin: str, day: str) -> pd.DataFrame:
    src = _source(STRIPS / f"KX{coin}15M" / "orderbook" / day)
    return _cached("strips", coin, day, [src], lambda: _parse_strips(coin, src))


# ── strike-series features (known at window open) ───────────────────────────
def strike_features(snaps: pd.DataFrame) -> pd.DataFrame:
    """Per coin-window: strike, strike_next, trend60, vol2h ratio; market aggregates."""
    w = snaps.groupby(["coin", "close_ts"], as_index=False)["strike"].first()
    lo, hi = int(w.close_ts.min()), int(w.close_ts.max())
    grid = np.arange(lo, hi + 1, 900)
    parts = []
    for coin, g in w.groupby("coin"):
        s = g.set_index("close_ts")["strike"].reindex(grid)          # real clock grid; gaps stay NaN
        f = pd.DataFrame({"close_ts": grid, "strike": s.values})
        f["coin"] = coin
        f["strike_next"] = s.shift(-1).values
        f["trend60"] = (np.log(s / s.shift(4)) * 1e4).values
        r15 = np.log(s / s.shift(1)) * 1e4
        vol2h = r15.rolling(8, min_periods=8).std()
        med = vol2h.shift(1).rolling(672, min_periods=192).median()
        f["vr2"] = (vol2h / med).values
        parts.append(f)
    f = pd.concat(parts, ignore_index=True)
    P = f.pivot(index="close_ts", columns="coin", values="trend60")
    V = f.pivot(index="close_ts", columns="coin", values="vr2")
    agg = pd.DataFrame({
        "mkt_trend60": P.mean(axis=1).where(P.notna().sum(axis=1) >= 4),
        "mkt_vr2": V.mean(axis=1).where(V.notna().sum(axis=1) >= 4),
    }).reset_index()
    f = f.merge(agg, on="close_ts", how="left")
    return f[f.strike.notna()].reset_index(drop=True)


# ── official outcomes ───────────────────────────────────────────────────────
def ticker(coin: str, close_ts: int) -> str:
    t = datetime.fromtimestamp(int(close_ts), timezone.utc).astimezone(ET)
    return f"KX{coin}15M-{t.strftime('%y%b%d%H%M').upper()}-{t.strftime('%M')}"


def official_outcomes() -> dict[str, float]:
    """Venue-settled results we hold: W7 paper ledger win flags and prod settlements."""
    out: dict[str, float] = {}
    try:
        st = json.loads(W7_STATE.read_text())
        for t in st.get("trades", []):
            if t.get("win") is not None:
                out[t["ticker"]] = float((t["side"] == "yes") == bool(t["win"]))
    except (OSError, ValueError):
        pass
    try:
        snap = json.loads(SNAPSHOT.read_text())
        for x in snap["strategies"]["fave"]["settlements"]:
            if x.get("result") in ("yes", "no"):
                out[x["ticker"]] = float(x["result"] == "yes")
    except (OSError, ValueError, KeyError):
        pass
    return out


def resolve_outcomes(f: pd.DataFrame, official: dict[str, float]) -> pd.Series:
    """Official where known; else strike_next >= strike; ties without an official result -> NaN.
    Keyed on the recorder's own ticker when present (unique across the DST fall-back hour)."""
    rec = f["ticker"] if "ticker" in f else pd.Series([None] * len(f), index=f.index)
    tk = [r if isinstance(r, str) and r else ticker(c, t) for r, c, t in zip(rec, f.coin, f.close_ts)]
    off = pd.Series([official.get(k, np.nan) for k in tk], index=f.index, dtype=float)
    rule = (f.strike_next >= f.strike).astype(float).where(f.strike_next.notna())
    rule = rule.where(f.strike_next != f.strike)
    return off.where(off.notna(), rule)


# ── funding (settled at/before window open) ─────────────────────────────────
def funding_at_open(f: pd.DataFrame, funding: pd.DataFrame) -> pd.Series:
    fu = funding.copy()
    fu["t"] = fu.time_ms / 1000.0                                    # keep milliseconds
    fu["dev"] = (fu.funding - FUND_BASE) * 8 * 1e4
    fu = fu.sort_values(["coin", "t"])
    fu["dev8h"] = fu.groupby("coin")["dev"].transform(lambda x: x.rolling(8, min_periods=6).mean())
    out = pd.Series(np.nan, index=f.index)
    for coin, g in f.groupby("coin"):
        h = fu[fu.coin == coin][["t", "dev8h"]]
        if h.empty:
            continue
        q = pd.DataFrame({"open_ts": (g.close_ts - 900).astype(float).values, "idx": g.index}).sort_values("open_ts")
        m = pd.merge_asof(q, h.sort_values("t"), left_on="open_ts", right_on="t", direction="backward")
        out.loc[m["idx"].values] = m["dev8h"].values
    return out


# ── entry snapshots + order-flow features (live W7 flow gate code) ──────────
def select_entries(snaps: pd.DataFrame) -> pd.DataFrame:
    e = snaps[(snaps.rem_min >= EDGES[0]) & (snaps.rem_min < EDGES[-1])].copy()
    e["bin"] = pd.cut(e.rem_min, EDGES, labels=LABELS, right=False).astype(str)
    e["d"] = (e.rem_min - e["bin"].map(CENTRES)).abs()
    e = e.sort_values(["d", "recv_ts"], kind="mergesort").drop_duplicates(["coin", "close_ts", "bin"])
    return e.drop(columns="d").reset_index(drop=True)


def _hl_rows(kind: str, coin: str, day: str, since: float) -> list[dict]:
    out = []
    src = _source(HL / kind / coin / day)
    if src is None:
        return out
    with _open(src) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            rt = r.get("recv_ts")
            if isinstance(rt, (int, float)) and rt >= since:
                out.append(r)
    return out


def _flow_for(coin: str, day: str, entries: pd.DataFrame) -> pd.DataFrame:
    from crypto_trading.crypto_strategies.downside_paper.features import compute_features
    d0 = datetime.fromisoformat(day + "T00:00:00+00:00")
    prev = (d0 - timedelta(days=1)).strftime("%Y-%m-%d")
    start = d0.timestamp() - 400
    data = {}
    for k in ("context", "book", "trades"):
        rows = _hl_rows(k, coin, prev, start) + _hl_rows(k, coin, day, start)
        rows.sort(key=lambda r: r["recv_ts"])
        data[k] = (rows, [r["recv_ts"] for r in rows])
    res = []
    for e in entries.itertuples():
        t = e.recv_ts
        sl = {}
        for k, (rows, ts) in data.items():
            sl[k] = rows[bisect.bisect_left(ts, t - 330): bisect.bisect_right(ts, t)]
        f = compute_features(sl["context"], sl["book"], sl["trades"], t)
        res.append(dict(coin=coin, close_ts=int(e.close_ts), bin=e.bin, recv_ts=t,
                        valid=bool(f.get("valid")), flow_valid=bool(f.get("flow_valid")),
                        flow_1m=f.get("observed_flow_imbalance_1m", np.nan),
                        mom_1m_bp=f.get("momentum_1m_bp", np.nan)))
    cols = ["coin", "close_ts", "bin", "recv_ts", "valid", "flow_valid", "flow_1m", "mom_1m_bp"]
    return pd.DataFrame(res, columns=cols)


def flow_day(coin: str, day: str, entries: pd.DataFrame) -> pd.DataFrame:
    d0 = datetime.fromisoformat(day + "T00:00:00+00:00")
    prev = (d0 - timedelta(days=1)).strftime("%Y-%m-%d")
    srcs = [_source(HL / k / coin / d) for k in ("context", "book", "trades") for d in (prev, day)]
    srcs.append(_source(STRIPS / f"KX{coin}15M" / "orderbook" / day))
    df = _cached("flow", coin, day, srcs, lambda: _flow_for(coin, day, entries))
    want = set(zip(entries.close_ts.astype("int64"), entries.bin, entries.recv_ts))
    have = set(zip(df.close_ts.astype("int64"), df.bin, df.recv_ts))
    if not want <= have:                       # cache built for a different entry set: rebuild it
        stale = CACHE / "flow" / coin / f"{day}.json"
        stale.unlink(missing_ok=True)
        df = _cached("flow", coin, day, srcs, lambda: _flow_for(coin, day, entries))
    return df


def newest_recv(kind_dir: Path) -> float | None:
    """Newest recv_ts at the tail of today's/yesterday's live file (freshness checks)."""
    best = None
    for p in sorted(kind_dir.glob("*.jsonl"))[-1:]:
        with open(p, "rb") as fh:
            fh.seek(max(0, os.path.getsize(p) - 65536))
            for line in fh.read().splitlines()[-50:]:
                try:
                    v = float(json.loads(line).get("recv_ts"))
                    best = v if best is None else max(best, v)
                except (ValueError, TypeError, AttributeError):
                    continue
    return best
