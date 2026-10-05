"""Decision-time features. `feats(X, M, k)` is the single definition used by training
(from the recordings) and by the live observer (from the recorder tails): the same code
on the same channel layout, so train and live cannot drift apart.

Channel layout of X[window, step, :] (5-second steps from window open):
  0 index | 1 mid 2 mark 3 oracle 4 premium 5 funding 6 oi | 7 bb 8 ba 9 dbid5 10 dask5 11 dbid1 12 dask1
  | 13 signed flow 14 volume 15 prints 16 max print | 17 kalshi yes price | 18 dvol
"""
from __future__ import annotations
import gzip, json, os, bisect
from datetime import datetime, timezone, timedelta
import numpy as np, pandas as pd
from scipy.stats import norm
from .config import COINS, STEP, K, BINS, STRIPS, HL, INDEX, DERIBIT

NCH = 19
CHANNELS = ["index", "mid", "mark", "oracle", "premium", "funding", "oi", "bb", "ba", "dbid5", "dask5", "dbid1", "dask1",
            "flow", "vol", "cnt", "maxp", "kyes", "dvol"]
NOPRICE = ["dist", "z", "rv_sofar", "rv15_prior", "rv60_prior", "dvol", "mom1", "mom3", "mom_all", "accel", "f1", "f3", "fall", "vol_rate", "maxp_rel", "cnt",
           "spread", "imb5", "imb1", "imb5_m", "prem", "dprem", "fund", "doi", "basis", "hour", "dow", "dev8h", "x_dist", "x_f3", "x_mom1", "k"]
BLEND1 = ["kyes", "p_fair_prior"]                          # logistic: market + fair value
BLEND2 = ["kyes", "p_fair_prior", "dist", "mom1", "mom3"]  # logistic: market + fair + distance/momentum
PRICE = ["kyes", "dk", "p_fair_prior", "p_fair_in", "p_fair_dv"]
ALL = NOPRICE + PRICE


def nanlast(a):
    m = ~np.isnan(a); idx = np.where(m.any(1), m.shape[1] - 1 - np.argmax(m[:, ::-1], axis=1), 0)
    out = a[np.arange(len(a)), idx]; out[~m.any(1)] = np.nan; return out


def nanfirst(a):
    m = ~np.isnan(a); idx = np.argmax(m, axis=1); out = a[np.arange(len(a)), idx]; out[~m.any(1)] = np.nan; return out


def feats(X, M, k) -> pd.DataFrame:
    """Causal features at step k (uses X[:, :k] only). M needs: coin, close_ts, strike,
    rv60_prior, rv15_prior, hour, dow, dev8h; optional strike_next for the targets."""
    n = len(X); W = X[:, :k]
    idx = W[:, :, 0]; strike = M.strike.values.astype(float); idx_e = nanlast(idx); idx_0 = nanfirst(idx)
    with np.errstate(all="ignore"):
        r = np.diff(np.log(idx), axis=1) * 1e4
        rv_sofar = np.sqrt(np.nansum(r ** 2, axis=1))
        nanv = np.full(n, np.nan)
        mom1 = (np.log(idx_e) - np.log(nanlast(idx[:, :-12]))) * 1e4 if k > 12 else nanv
        mom3 = (np.log(idx_e) - np.log(nanlast(idx[:, :-36]))) * 1e4 if k > 36 else nanv
        mom_all = (np.log(idx_e) - np.log(idx_0)) * 1e4
        mom1_prev = (np.log(nanlast(idx[:, :-12])) - np.log(nanlast(idx[:, :-24]))) * 1e4 if k > 24 else nanv
        dist = (np.log(idx_e) - np.log(strike)) * 1e4
        rem = 900 - STEP * k
        sig_prior = M.rv60_prior.values * np.sqrt(rem / 3600); sig_in = rv_sofar * np.sqrt(rem / (STEP * k))
        sig_dv = W[:, -1, 18] / 100 * np.sqrt(rem / (365 * 86400)) * 1e4
        p_fair_prior = norm.cdf(dist / sig_prior); p_fair_in = norm.cdf(dist / sig_in); p_fair_dv = norm.cdf(dist / sig_dv)
        z = dist / (M.rv15_prior.values * np.sqrt(rem / 900))                      # distance in units of the remaining-time vol (15-min prior RV)
        flow, vol, cnt, maxp = W[:, :, 13], W[:, :, 14], W[:, :, 15], W[:, :, 16]
        def fl(a):
            f, v = np.nansum(flow[:, -a:], 1), np.nansum(vol[:, -a:], 1); return np.where(v > 0, f / np.where(v > 0, v, 1), np.nan)
        f1, f3, fall = fl(12), fl(36), fl(k)
        vsum = np.nansum(vol, 1); vol_rate = vsum / (STEP * k); maxp_rel = np.where(vsum > 0, np.nanmax(maxp, 1) / (vsum / k + 1e-9), np.nan)
        bb, ba, db5, da5, db1, da1 = (nanlast(W[:, :, j]) for j in (7, 8, 9, 10, 11, 12))
        spread = (ba - bb) / ((ba + bb) / 2) * 1e4; imb5 = (db5 - da5) / (db5 + da5); imb1 = (db1 - da1) / (db1 + da1)
        imb5_m = np.nanmean((W[:, -12:, 9] - W[:, -12:, 10]) / (W[:, -12:, 9] + W[:, -12:, 10]), axis=1)
        prem = nanlast(W[:, :, 4]) * 1e4; dprem = (nanlast(W[:, :, 4]) - nanfirst(W[:, :, 4])) * 1e4
        fund = nanlast(W[:, :, 5]) * 1e4 * 8; doi = (np.log(nanlast(W[:, :, 6])) - np.log(nanfirst(W[:, :, 6]))) * 1e4
        basis = (np.log(nanlast(W[:, :, 1])) - np.log(idx_e)) * 1e4
        kyes = nanlast(W[:, :, 17]); kyes0 = nanfirst(W[:, :, 17]); dk = kyes - kyes0
        dvol = nanlast(W[:, :, 18])
    F = pd.DataFrame(dict(coin=M.coin.values, close_ts=M.close_ts.values, k=k, rem=rem, strike=strike, idx_e=idx_e, dist=dist, z=z,
                          rv_sofar=rv_sofar, rv15_prior=M.rv15_prior.values, rv60_prior=M.rv60_prior.values, dvol=dvol,
                          p_fair_prior=p_fair_prior, p_fair_in=p_fair_in, p_fair_dv=p_fair_dv,
                          mom1=mom1, mom3=mom3, mom_all=mom_all, accel=mom1 - mom1_prev, f1=f1, f3=f3, fall=fall, vol_rate=vol_rate, maxp_rel=maxp_rel, cnt=np.nansum(cnt, 1),
                          spread=spread, imb5=imb5, imb1=imb1, imb5_m=imb5_m, prem=prem, dprem=dprem, fund=fund, doi=doi, basis=basis,
                          kyes=kyes, dk=dk, hour=M.hour.values, dow=M.dow.values, dev8h=M.dev8h.values))
    sn = M.strike_next.values.astype(float) if "strike_next" in M else np.full(n, np.nan)
    F["y_yes"] = np.where(np.isnan(sn), np.nan, np.where(sn > strike, 1.0, np.where(sn < strike, 0.0, np.nan)))
    F["y_rem"] = (np.log(sn) - np.log(idx_e)) * 1e4
    F["y_dir"] = np.where(F.y_rem > 0, 1.0, np.where(F.y_rem < 0, 0.0, np.nan))
    g = F.groupby("close_ts"); nn = g.dist.transform("size")
    F["x_dist"] = (g.dist.transform("sum") - F.dist) / (nn - 1).replace(0, np.nan)
    F["x_f3"] = (g.f3.transform("sum") - F.f3.fillna(0)) / (nn - 1).replace(0, np.nan)
    F["x_mom1"] = (g.mom1.transform("sum") - F.mom1.fillna(0)) / (nn - 1).replace(0, np.nan)
    F["fav_yes"] = (F.kyes >= 0.5).astype(float); F["p_fav"] = np.where(F.fav_yes == 1, F.kyes, 1 - F.kyes)
    F["y_fav_won"] = np.where(F.y_yes.isna(), np.nan, (F.y_yes == F.fav_yes).astype(float))
    F["day"] = pd.to_datetime(F.close_ts, unit="s", utc=True).dt.date
    return F


def channels(X, M, vol_median: float | None = None):
    """GRU per-step channels (scale-free): d log index bp, index vs strike bp, flow/vol, log volume,
    book imbalance, spread bp, premium bp, kalshi yes − 0.5 (ffilled)."""
    idx = X[:, :, 0]; strike = M.strike.values.astype(float)[:, None]
    with np.errstate(all="ignore"):
        li = np.log(idx); dl = np.concatenate([np.zeros((len(X), 1)), np.diff(li, axis=1)], axis=1) * 1e4
        dist = (li - np.log(strike)) * 1e4
        flow = X[:, :, 13]; vol = X[:, :, 14]; fr = np.where(vol > 0, flow / np.where(vol > 0, vol, 1), 0.0)
        if vol_median is None: vol_median = float(np.nanmedian(vol[vol > 0])) if (vol > 0).any() else 1.0
        lv = np.log1p(vol / vol_median)
        imb = (X[:, :, 9] - X[:, :, 10]) / (X[:, :, 9] + X[:, :, 10]); sp = (X[:, :, 8] - X[:, :, 7]) / ((X[:, :, 8] + X[:, :, 7]) / 2) * 1e4
        prem = X[:, :, 4] * 1e4; ky = pd.DataFrame(X[:, :, 17]).ffill(axis=1).fillna(0.5).values
    C = np.stack([dl, dist, fr, lv, imb, sp, prem, ky - 0.5], axis=2).astype(np.float32)
    return np.nan_to_num(C, nan=0.0, posinf=0.0, neginf=0.0), vol_median


def static(df: pd.DataFrame):
    s = np.column_stack([df.rem / 900, df.kyes.fillna(0.5) - 0.5, df.dist / 100, df.rv60_prior.fillna(30) / 100,
                         np.sin(2 * np.pi * df.hour / 24), np.cos(2 * np.pi * df.hour / 24)] + [(df.coin == c).astype(float) for c in COINS])
    return np.nan_to_num(np.asarray(s, dtype=np.float32))


# ───────────────────────── recorder readers (shared by training and live) ─────────────────────────
def _open(p):
    if os.path.exists(p): return open(p)
    if os.path.exists(p + ".gz"): return gzip.open(p + ".gz", "rt")
    return None


def load_jsonl(path, fn, tail_bytes: int | None = None) -> list:
    """Parse one recorder file (or its rotated .gz); `tail_bytes` reads only the end of a live file."""
    out = []
    if tail_bytes and os.path.exists(path) and not str(path).endswith(".gz"):
        with open(path, "rb") as fh:
            size = os.path.getsize(path); fh.seek(max(0, size - tail_bytes)); lines = fh.read().splitlines()
        if size > tail_bytes: lines = lines[1:]
        for line in lines:
            try: v = fn(json.loads(line))
            except (ValueError, KeyError, TypeError): continue
            if v is not None: out.append(v)
        return out
    fh = _open(str(path))
    if fh is None: return out
    with fh:
        for line in fh:
            try: v = fn(json.loads(line))
            except (ValueError, KeyError, TypeError): continue
            if v is not None: out.append(v)
    return out


IDX_FN = lambda r: (float(r["ts"]), float(r["index"])) if not r.get("stale") else None
CTX_FN = lambda r: (float(r["recv_ts"]), float(r["mid_px"]), float(r["mark_px"]), float(r["oracle_px"]), float(r["premium"]), float(r["funding"]), float(r["open_interest"]))
TRD_FN = lambda r: (float(r["recv_ts"]), float(r["px"]) * float(r["sz"]) * (1.0 if r["side"] == "B" else -1.0), float(r["px"]) * float(r["sz"]))
DV_FN = lambda r: (float(r["recv_ts"]), float(r["dvol"]))


def BOOK_FN(r):
    bids, asks = r["bids"][:5], r["asks"][:5]; bb, ba = float(bids[0][0]), float(asks[0][0])
    return (float(r["recv_ts"]), bb, ba, sum(float(p) * float(q) for p, q, *_ in bids), sum(float(p) * float(q) for p, q, *_ in asks), float(bids[0][1]) * bb, float(asks[0][1]) * ba)


def STRIP_FN(r):
    ob = (r.get("ob") or {}).get("orderbook_fp") or {}
    yb = max((float(p) for p, _ in (ob.get("yes_dollars") or []) if 0 < float(p) < 1), default=np.nan)
    nb = max((float(p) for p, _ in (ob.get("no_dollars") or []) if 0 < float(p) < 1), default=np.nan)
    # favourite's cost at the touch, mapped to the yes axis (what the tables price from)
    yes_px = np.nan
    if yb == yb and nb == nb: yes_px = yb if yb >= nb else 1 - nb
    elif yb == yb: yes_px = yb
    elif nb == nb: yes_px = 1 - nb
    ct = datetime.fromisoformat(r["close_time"].replace("Z", "+00:00")).timestamp()
    return (float(r["recv_ts"]), yes_px, int(round(ct)), float(r["strike"]))


def arr(rows, ncol):
    a = np.array(rows, dtype=np.float64) if rows else np.zeros((0, ncol))
    return a[np.argsort(a[:, 0])] if len(a) else a


def last_le(ts, vals, grid, maxage):
    if len(ts) == 0: return np.full((len(grid),) + (vals.shape[1:] if vals.ndim > 1 else ()), np.nan, dtype=np.float32)
    i = np.searchsorted(ts, grid, side="right") - 1
    ok = (i >= 0) & (grid - ts[np.clip(i, 0, len(ts) - 1)] <= maxage)
    v = np.full((len(grid),) + vals.shape[1:], np.nan, dtype=np.float32); v[ok] = vals[i[ok]]
    return v


def window_array(close_ts: int, I, C, T, B, Sx, D):
    """One window's [K, 19] array from sorted raw arrays (I index, C context, T trades, B book, Sx strips rows of this window, D dvol)."""
    o = close_ts - 900; grid = o + STEP * np.arange(1, K + 1, dtype=np.float64)
    ix = last_le(I[:, 0], I[:, 1], grid, 20) if len(I) else np.full(K, np.nan, np.float32)
    cx = last_le(C[:, 0], C[:, 1:], grid, 15) if len(C) else np.full((K, 6), np.nan, np.float32)
    bx = last_le(B[:, 0], B[:, 1:], grid, 15) if len(B) else np.full((K, 6), np.nan, np.float32)
    flow = np.zeros(K, np.float32); vol = np.zeros(K, np.float32); cnt = np.zeros(K, np.float32); maxp = np.zeros(K, np.float32)
    if len(T):
        a, b = np.searchsorted(T[:, 0], o, side="right"), np.searchsorted(T[:, 0], close_ts, side="right"); tt = T[a:b]
        if len(tt):
            bucket = np.minimum(((tt[:, 0] - o - 1e-9) // STEP).astype(int), K - 1)
            flow = np.bincount(bucket, weights=tt[:, 1], minlength=K).astype(np.float32); vol = np.bincount(bucket, weights=tt[:, 2], minlength=K).astype(np.float32)
            cnt = np.bincount(bucket, minlength=K).astype(np.float32); np.maximum.at(maxp, bucket, tt[:, 2].astype(np.float32))
    kx = last_le(Sx[:, 0], Sx[:, 1], grid, 100) if len(Sx) else np.full(K, np.nan, np.float32)
    dv = last_le(D[:, 0], D[:, 1], grid, 120) if len(D) else np.full(K, np.nan, np.float32)
    return np.column_stack([ix, cx, bx, flow, vol, cnt, maxp, kx, dv]).astype(np.float32)


def prior_vols(I, o):
    p60 = I[(I[:, 0] > o - 3600) & (I[:, 0] <= o), 1] if len(I) else np.zeros(0); p15 = I[(I[:, 0] > o - 900) & (I[:, 0] <= o), 1] if len(I) else np.zeros(0)
    rv60 = float(np.sqrt(np.sum(np.diff(np.log(p60)) ** 2)) * 1e4) if len(p60) > 10 else np.nan
    rv15 = float(np.sqrt(np.sum(np.diff(np.log(p15)) ** 2)) * 1e4) if len(p15) > 10 else np.nan
    return rv60, rv15


def live_arrays(close_ts: int, now: float, coins=COINS, dev8h: dict | None = None):
    """[5, K, 19] + meta for the CURRENT window from the recorder tails (rows after `now` are
    masked so the array is exactly what was knowable at `now`)."""
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"); prev = (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    day_start = (now // 86400) * 86400
    with_prev = (close_ts - 900) < day_start or (now - day_start) < 1500       # the window (or its 60-min history) started on the previous UTC day
    def tails(sub, fn, tail_today, tail_prev):
        rows = load_jsonl(sub(prev), fn, tail_prev) if with_prev else []
        return rows + load_jsonl(sub(day), fn, tail_today)
    Xs, meta = [], []
    for c in coins:
        I = arr(load_jsonl(INDEX / c / f"{prev}.jsonl", IDX_FN, 400_000) + load_jsonl(INDEX / c / f"{day}.jsonl", IDX_FN, 1_500_000), 2)
        C = arr(tails(lambda d: HL / "context" / c / f"{d}.jsonl", CTX_FN, 600_000, 300_000), 7)
        T = arr(tails(lambda d: HL / "trades" / c / f"{d}.jsonl", TRD_FN, 4_000_000, 1_500_000), 3)
        B = arr(tails(lambda d: HL / "book" / c / f"{d}.jsonl", BOOK_FN, 3_000_000, 1_200_000), 7)
        S_ = tails(lambda d: STRIPS / f"KX{c}15M" / "orderbook" / f"{d}.jsonl", STRIP_FN, 1_500_000, 400_000)
        D = arr(tails(lambda d: DERIBIT / "dvol" / c / f"{d}.jsonl", DV_FN, 200_000, 100_000), 2) if c in ("BTC", "ETH") else np.zeros((0, 2))
        for A in (I, C, T, B, D):
            if len(A): A[A[:, 0] > now, 1:] = np.nan                      # knowable at `now` only
        Sx = arr([(t, px, ct, k) for t, px, ct, k in S_ if ct == close_ts and t <= now], 4)
        strike = float(Sx[-1, 3]) if len(Sx) else np.nan
        X = window_array(close_ts, I[I[:, 0] <= now] if len(I) else I, C, T[T[:, 0] <= now] if len(T) else T, B, Sx, D)
        rv60, rv15 = prior_vols(I, close_ts - 900)
        meta.append(dict(coin=c, close_ts=close_ts, strike=strike, strike_next=np.nan, rv60_prior=rv60, rv15_prior=rv15,
                         hour=datetime.fromtimestamp(close_ts, timezone.utc).hour, dow=datetime.fromtimestamp(close_ts, timezone.utc).weekday(),
                         dev8h=(dev8h or {}).get(c, np.nan)))
        Xs.append(X)
    return np.stack(Xs), pd.DataFrame(meta)


def index_series_10s(coin: str, t0: float, t1: float, now: float):
    """Index path at 10 s from t0 to t1 (<= now), ffilled — the Chronos context."""
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"); prev = (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    I = arr(load_jsonl(INDEX / coin / f"{prev}.jsonl", IDX_FN, 400_000) + load_jsonl(INDEX / coin / f"{day}.jsonl", IDX_FN, 1_500_000), 2)
    I = I[I[:, 0] <= now] if len(I) else I
    grid = np.arange(t0, t1 + 1e-6, 10)
    if not len(I): return np.full(len(grid), np.nan)
    i = np.searchsorted(I[:, 0], grid, side="right") - 1; ok = (i >= 0) & (grid - I[np.clip(i, 0, len(I) - 1), 0] <= 30)
    v = np.where(ok, I[np.clip(i, 0, len(I) - 1), 1], np.nan)
    return pd.Series(v).ffill().bfill().values


def logit_cols(df: pd.DataFrame, cols) -> np.ndarray:
    """Design matrix for the logistic blends: probabilities enter as logits, the rest as is."""
    out = []
    for c in cols:
        v = df[c].values.astype(float)
        if c in ("kyes", "p_fair_prior", "p_fair_in"):
            v = np.clip(v, 1e-3, 1 - 1e-3); v = np.log(v / (1 - v))
        out.append(v)
    return np.column_stack(out)
