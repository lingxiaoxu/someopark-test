"""Book imbalance from the NEVER-USED prod L2 snapshots (Plan 15).

`price_data/kalshi/perps/poll/prod/orderbook/` — 13 perps × 10s × 35 days —
was recorded from day one and consumed by nothing: every fill model priced
queues off the trade tape and the TOUCH only. Depth imbalance is the most
replicated short-horizon predictor in the microstructure literature (Cont-
Kukanov-Stoikov), so this is the "did we really try everything" gap to close.

Two tests, in priority order:

  T1 EXECUTION FILTER — every maker strategy here died of adverse selection
     (markout −2.6..−5.6bps). Hypothesis: a resting BUY is run over precisely
     when the bid side is thin, so posting ONLY when the book leans our way
     (imb ≥ τ for buys, ≤ −τ for sells) should move markout toward ≥ 0.
     Harness: hypothetical posts every 30min per side at the touch, queue
     behind the DISPLAYED size at our level (the book finally gives us the
     real queue, not the traded-size proxy), 15min timeout, fills against the
     trade tape. τ chosen on the IS day-half by 2m markout, OOS measured once.
     Salvageable = OOS markout ≥ 0 AND fill rate still > 30%.

  T2 SIGNAL — imbalance z-scores at 30m/1h/2h/4h under the standard protocol
     (direction fixed IS-only, non-overlapping episodes, 10bps maker-maker RT,
     NW-t, day-block bootstrap, cross-market pooling). The literature puts
     imbalance's power at seconds-to-minutes — the fee-blocked zone — so this
     is expected to fail, but it gets numbers, not a citation.

Hygiene (the SOL 4.6e14-sentinel lesson applies to books too): a snapshot is
dropped when a side is empty, the book is crossed, the mid is a factor-5 from
the ticker's sample median, or the spread exceeds 500bps. Counts reported.

PIT: recv_ts is event time; features at t use only snapshot t; thresholds and
directions come from the IS half; bootstrap blocks are days.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import logging
import os

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.backtest.fill_model import simulate_maker_fill
from crypto_trading.crypto_common.config import PRICE_DATA, SIGNALS_DIR
from crypto_trading.crypto_common.loader import load_poll_trades
from crypto_trading.crypto_common.trade_stats import newey_west_tstat

logger = logging.getLogger(__name__)

BOOK_DIR = PRICE_DATA / "kalshi" / "perps" / "poll" / "prod" / "orderbook"
MARKETS = ["KXBTCPERP", "KXETHPERP", "KXSOLPERP", "KXXRPPERP",
           "KXDOGEPERP", "KXLTCPERP", "KXBCHPERP", "KXSUIPERP"]

POST_EVERY_MIN = 30
ENTRY_TIMEOUT_MIN = 15
MARKOUT_S = {"30s": 30, "2m": 120, "5m": 300}
TAUS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5)          # imb3 filter grid (0.0 = no filter)
MIN_IS_FILLS = 50

FEE_MM_BPS = 10.0
GRID = "5min"
ZWIN = 288                                      # 24h of 5-min bars
HORIZON_STEPS = {"30m": 6, "60m": 12, "2h": 24, "4h": 48}
ENTRY_Q = 1.0
FEATURES = ("imb1", "imb3", "imb5", "slope", "thick")
BOOT_N = 3000


# ── parsing ─────────────────────────────────────────────────────────────────

def _levels(side: list, best_last: bool) -> list[tuple[float, float]]:
    """[(price, size)] with the TOUCH first. Raw arrays keep best at the END."""
    out = []
    for p, s in (reversed(side) if best_last else side):
        try:
            fp, fs = float(p), float(s)
        except (TypeError, ValueError):
            continue
        if fs > 0 and np.isfinite(fp) and fp > 0:
            out.append((fp, fs))
    return out


def parse_ticker(ticker: str, cache_dir: str) -> tuple[pd.DataFrame, dict]:
    """10s book frame for one ticker (cached parquet) + hygiene counters."""
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, f"{ticker}.parquet")
    if os.path.exists(cache):
        return pd.read_parquet(cache), {"cached": True}

    rows = []
    hyg = {"lines": 0, "bad_json": 0, "empty_side": 0, "crossed": 0}
    for path in sorted(glob.glob(str(BOOK_DIR / ticker / "*"))):
        op = gzip.open if path.endswith(".gz") else open
        try:
            with op(path, "rt") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    hyg["lines"] += 1
                    try:
                        d = json.loads(line)
                        ob = d["ob"]
                        asks = _levels(ob.get("asks") or [], best_last=True)
                        bids = _levels(ob.get("bids") or [], best_last=True)
                    except Exception:            # noqa: BLE001 - malformed line
                        hyg["bad_json"] += 1
                        continue
                    if not asks or not bids:
                        hyg["empty_side"] += 1
                        continue
                    ba, bb = asks[0][0], bids[0][0]
                    if bb >= ba:
                        hyg["crossed"] += 1
                        continue
                    rows.append({
                        "ts": float(d["recv_ts"]), "bb": bb, "ba": ba,
                        "bd1": bids[0][1],
                        "bd3": sum(s for _, s in bids[:3]),
                        "bd5": sum(s for _, s in bids[:5]),
                        "ad1": asks[0][1],
                        "ad3": sum(s for _, s in asks[:3]),
                        "ad5": sum(s for _, s in asks[:5]),
                    })
        except OSError:
            continue
    df = pd.DataFrame(rows)
    if df.empty:
        return df, hyg
    df.index = pd.to_datetime(df.pop("ts"), unit="s", utc=True)
    df = df.sort_index()
    df["mid"] = (df.bb + df.ba) / 2.0
    df["spread_bps"] = 1e4 * (df.ba - df.bb) / df.mid

    # factor-5 sanity vs the sample median (the sentinel lesson) + spread cap
    med = float(df.mid.median())
    bad = (df.mid > med * 5) | (df.mid < med / 5) | (df.spread_bps > 500)
    hyg["insane_mid_or_spread"] = int(bad.sum())
    df = df[~bad]

    for k in (1, 3, 5):
        tot = (df[f"bd{k}"] + df[f"ad{k}"]).replace(0.0, np.nan)
        df[f"imb{k}"] = (df[f"bd{k}"] - df[f"ad{k}"]) / tot
    tot5 = (df.bd5 + df.ad5).replace(0.0, np.nan)
    df["slope"] = ((df.bd5 - df.bd1) - (df.ad5 - df.ad1)) / tot5
    df["thick"] = np.log(df.bd1 / df.ad1.replace(0.0, np.nan)).clip(-3, 3)

    df.to_parquet(cache)
    return df, hyg


# ── T1: execution filter ────────────────────────────────────────────────────

def hypothetical_posts(frame: pd.DataFrame, trades: pd.DataFrame,
                       ticker: str) -> pd.DataFrame:
    """Post at the touch every POST_EVERY_MIN per side; queue = displayed size."""
    mid = frame["mid"]
    bucket = (frame.index.asi8 // (POST_EVERY_MIN * 60 * 10**9))
    first_of_bucket = frame.index[np.r_[True, bucket[1:] != bucket[:-1]]]
    timeout = pd.Timedelta(minutes=ENTRY_TIMEOUT_MIN)

    def mid_at(ts):
        i = mid.index.searchsorted(ts, side="right") - 1
        return float(mid.iloc[i]) if i >= 0 else None

    out = []
    for ts in first_of_bucket:
        r = frame.loc[ts]
        for side_name, side, limit, queue in (
                ("buy", "bid", r.bb, r.bd1), ("sell", "ask", r.ba, r.ad1)):
            fr = simulate_maker_fill(limit, side, ts, trades,
                                     timeout=timeout, queue_ahead=queue)
            rec = {"ticker": ticker, "post_ts": ts, "side": side_name,
                   "sgn": 1.0 if side_name == "buy" else -1.0,
                   "imb3": float(r.imb3) if np.isfinite(r.imb3) else 0.0,
                   "half_spread_bps": float(r.spread_bps) / 2.0,
                   "day": str(ts.date()), "filled": fr.filled}
            if fr.filled:
                m0 = mid_at(fr.fill_ts)
                for lbl, secs in MARKOUT_S.items():
                    m1 = mid_at(fr.fill_ts + pd.Timedelta(seconds=secs))
                    rec[f"mk_{lbl}"] = (1e4 * rec["sgn"] * (m1 - m0) / m0
                                        if (m0 and m1) else np.nan)
            out.append(rec)
    return pd.DataFrame(out)


def _day_boot(vals: pd.Series, days: pd.Series, seed: int = 7) -> dict:
    grp = pd.DataFrame({"v": vals.values, "d": days.values}).groupby("d").v.apply(list)
    blocks = list(grp)
    if len(blocks) < 3:
        return {}
    rng = np.random.default_rng(seed)
    boots = np.array([np.concatenate([blocks[i] for i in
                      rng.integers(0, len(blocks), len(blocks))]).mean()
                      for _ in range(BOOT_N)])
    return {"p_le_zero": round(float((boots <= 0).mean()), 4),
            "ci95": [round(float(np.percentile(boots, 2.5)), 2),
                     round(float(np.percentile(boots, 97.5)), 2)]}


def _arm(sub: pd.DataFrame) -> dict:
    fills = sub[sub.filled & sub.mk_2m.notna()]
    out = {"posts": len(sub), "fills": len(fills),
           "fill_rate": round(len(fills) / len(sub), 3) if len(sub) else None}
    if len(fills) < 10:
        return out
    for lbl in MARKOUT_S:
        out[f"mk_{lbl}"] = round(float(fills[f"mk_{lbl}"].mean()), 2)
    out["half_spread_bps"] = round(float(fills.half_spread_bps.mean()), 2)
    out.update(_day_boot(fills.mk_2m, fills.day))
    return out


def run_t1(posts: pd.DataFrame) -> dict:
    days = sorted(posts.day.unique())
    is_days = set(days[:len(days) // 2])
    posts = posts.assign(is_half=posts.day.isin(is_days))

    def passes(df, tau):
        if tau == 0.0:
            return df
        keep = np.where(df.sgn > 0, df.imb3 >= tau, df.imb3 <= -tau)
        return df[keep]

    # τ from the IS half only (2m markout, sample floor)
    grid = []
    for tau in TAUS:
        f = passes(posts[posts.is_half], tau)
        fills = f[f.filled & f.mk_2m.notna()]
        grid.append({"tau": tau, "is_fills": len(fills),
                     "is_mk_2m": round(float(fills.mk_2m.mean()), 2)
                     if len(fills) >= MIN_IS_FILLS else None})
    valid = [g for g in grid if g["is_mk_2m"] is not None]
    best_tau = max(valid, key=lambda g: g["is_mk_2m"])["tau"] if valid else 0.0

    oos = posts[~posts.is_half]
    res = {"tau_grid_IS": grid, "chosen_tau": best_tau,
           "oos_unfiltered": _arm(oos),
           "oos_filtered": _arm(passes(oos, best_tau))}

    # bootstrap of the DIFFERENCE (same day draws feed both arms)
    fu = oos[oos.filled & oos.mk_2m.notna()]
    ff = passes(oos, best_tau)
    ff = ff[ff.filled & ff.mk_2m.notna()]
    days_o = sorted(oos.day.unique())
    if len(days_o) >= 3 and len(ff) >= 10:
        rng = np.random.default_rng(11)
        diffs = []
        for _ in range(BOOT_N):
            pick = [days_o[i] for i in rng.integers(0, len(days_o), len(days_o))]
            a = pd.concat([fu[fu.day == d] for d in pick])
            b = pd.concat([ff[ff.day == d] for d in pick])
            if len(a) and len(b):
                diffs.append(b.mk_2m.mean() - a.mk_2m.mean())
        if diffs:
            diffs = np.array(diffs)
            res["filter_improvement_2m"] = {
                "mean": round(float(diffs.mean()), 2),
                "p_le_zero": round(float((diffs <= 0).mean()), 4)}
    return res


# ── T2: signal ──────────────────────────────────────────────────────────────

def _tz(s: pd.Series) -> pd.Series:
    mu = s.rolling(ZWIN, min_periods=48).mean()
    sd = s.rolling(ZWIN, min_periods=48).std(ddof=0)
    return (s - mu) / sd.replace(0.0, np.nan)


def run_t2(frames: dict[str, pd.DataFrame]) -> list[dict]:
    pooled: dict[tuple, list] = {}
    for ticker, f in frames.items():
        g = f.resample(GRID, label="right", closed="right").last().dropna(subset=["mid"])
        mark = g["mid"]
        half = g.index[len(g) // 2]
        for feat in FEATURES:
            z = _tz(g[feat])
            for hname, k in HORIZON_STEPS.items():
                fwd = (mark.shift(-k) / mark - 1.0) * 1e4
                d = pd.DataFrame({"z": z, "fwd": fwd}).dropna()
                is_d, oos_d = d[d.index < half], d[d.index >= half]
                if len(is_d) < 200 or len(oos_d) < 200:
                    continue
                ic = is_d.z.corr(is_d.fwd, method="spearman")
                if pd.isna(ic) or ic == 0:
                    continue
                sgn = float(np.sign(ic))
                hold = pd.Timedelta(GRID) * k
                rows, until = [], None
                for ts, r in oos_d.iterrows():
                    if abs(r.z) < ENTRY_Q or (until is not None and ts < until):
                        continue
                    rows.append({"ts": ts, "day": str(ts.date()),
                                 "net": float(sgn * np.sign(r.z) * r.fwd) - FEE_MM_BPS})
                    until = ts + hold
                if len(rows) >= 5:
                    pooled.setdefault((feat, hname), []).append(pd.DataFrame(rows))
    out = []
    for (feat, hname), parts in pooled.items():
        ep = pd.concat(parts).sort_values("ts")
        net = ep.net.reset_index(drop=True)
        nw = newey_west_tstat(net)
        rec = {"feature": feat, "horizon": hname, "n": len(ep),
               "n_markets": len(parts),
               "mean_net_bps": round(float(net.mean()), 2),
               "hit": round(float((net > 0).mean()), 3),
               "nw_t": round(float(nw["t_nw"]), 2)}
        rec.update(_day_boot(ep.net, ep.day, seed=13))
        out.append(rec)
    return sorted(out, key=lambda r: -(r["nw_t"] or -9))


# ── entry point ─────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--markets", default=",".join(MARKETS))
    ap.add_argument("--cache", default="/tmp/bookimb_test/cache")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    frames, hygiene, post_parts = {}, {}, []
    for tk in args.markets.split(","):
        f, hyg = parse_ticker(tk, args.cache)
        hygiene[tk] = hyg
        if f.empty:
            logger.warning("%s: no usable book rows", tk)
            continue
        frames[tk] = f
        logger.info("%s: %d snapshots (%s)", tk, len(f), hyg)
        trades = load_poll_trades(tk).sort_index()
        if len(trades):
            post_parts.append(hypothetical_posts(f, trades, tk))
            logger.info("%s: posts done", tk)

    print("=" * 100)
    print("HYGIENE (per ticker):")
    for tk, h in hygiene.items():
        print(f"  {tk}: {h}")

    res: dict = {"hygiene": hygiene}
    if post_parts:
        posts = pd.concat(post_parts, ignore_index=True)
        res["t1"] = run_t1(posts)
        print("\n" + "=" * 100)
        print(f"T1 EXECUTION FILTER — posts every {POST_EVERY_MIN}min at the touch, "
              f"queue behind displayed size, {ENTRY_TIMEOUT_MIN}min timeout")
        print("=" * 100)
        print(f"IS τ grid: {res['t1']['tau_grid_IS']}")
        print(f"chosen τ = {res['t1']['chosen_tau']}")
        for arm in ("oos_unfiltered", "oos_filtered"):
            print(f"  {arm}: {res['t1'][arm]}")
        if "filter_improvement_2m" in res["t1"]:
            print(f"  improvement(2m markout, filtered−unfiltered): "
                  f"{res['t1']['filter_improvement_2m']}")

    res["t2"] = run_t2(frames)
    print("\n" + "=" * 100)
    print("T2 SIGNAL — imbalance z at 30m..4h (pooled, OOS, net of 10bps RT)")
    print("=" * 100)
    if res["t2"]:
        print(pd.DataFrame(res["t2"]).to_string(index=False))

    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"book_imbalance_{stamp}.json").write_text(
        json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
