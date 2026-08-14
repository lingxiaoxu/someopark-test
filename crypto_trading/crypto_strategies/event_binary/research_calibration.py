"""Are the Kalshi "BTC above X" binaries well priced?  (Plan 15 §A)

First direct study of the BINARY contracts themselves — Plan 02 (event_perp)
used these strips only as a SIGNAL for the perp leg. The binary leg has a
different fee model entirely: fee = mult × P × (1−P) per contract per fill
(taker; maker pays 25% of that), so the perp's flat 10bps-of-notional wall does
not apply here. Whether there is anything to eat depends on CALIBRATION: do
implied probabilities match realized frequencies, and if not, where?

Design (all PIT — decisions at snapshot t use only that snapshot; thresholds
and directions fixed on the IS half of DAYS, measured on the OOS half):

  SETTLEMENT   outcome from the last recorded ``spot_est`` ≤ close_time
               (staleness >10min excluded; knife-edge |spot−K|<5bps flagged
               and excluded from headline tables). NOTE this is Kalshi's own
               index estimate captured in the feed, not the official
               settlement print — an approximation, honest error source.
  CALIBRATION  mid-implied probability sampled at fixed TTE checkpoints
               (240/120/60/30/15/5 min) × probability buckets, vs realized
               frequency, Wilson 95% CI. The classic anomaly to test for is
               favorite-longshot bias (longshots overpriced, favorites cheap).
  RULES        (a) extremes: buy YES at ask ≤5c / sell YES at bid ≥95c, hold
                   to settlement;
               (b) IS-calibration-direction: buckets where the IS half shows a
                   ≥3c bias with n≥30 → trade the OOS half in that direction;
               (c) fair-value model on threshold (greater/less) contracts:
                   P_model = Φ(ln(S/K)/σ_TTE) from the trailing 24h realized
                   vol of spot_est (normal — model risk stated); trade when
                   |P_model − mid| exceeds an IS-chosen threshold.
  FEES         taker mult sensitivity {0.07, 0.10} (official PDF is behind a
               bot-check; crypto may be a premium tier), maker = 25% of taker.
               Maker fills are approximated by "a later quote crossed our
               price" — OPTIMISTIC, labelled as such.
  STATS        per-rule net $/contract, hit rate, day-block bootstrap p,
               Bonferroni across every rule variant examined.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
from bisect import bisect_right
from collections import defaultdict

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_strategies.event_perp.backtest import read_snapshots

logger = logging.getLogger(__name__)

SERIES = ["KXBTC", "KXBTCD", "KXETH", "KXETHD"]
TTE_MIN = (240, 120, 60, 30, 15, 5)
TTE_TOL_S = 150.0
STALE_SETTLE_S = 600.0
KNIFE_BPS = 5.0
PROB_BUCKETS = [(0.0, .05), (.05, .15), (.15, .30), (.30, .50), (.50, .70),
                (.70, .85), (.85, .95), (.95, 1.0)]
TAKER_MULTS = (0.07, 0.10)
MAKER_FRAC = 0.25
VOL_WIN = 288                     # 24h of 5-min spot bars
MODEL_TTES = (60, 30, 15)
MODEL_THRESH = (0.03, 0.05, 0.08, 0.12)


def fee(p: float, mult: float) -> float:
    return mult * p * (1.0 - p)


def _f(x) -> float | None:
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return None


def collect_series(series: str, days: list[str]) -> tuple[list[dict], np.ndarray, np.ndarray]:
    """One pass over the tape → per-market records + the spot_est series.

    Per market we keep: contract geometry, TTE-checkpoint quotes, and running
    post-checkpoint best quotes (for the maker-fill approximation) — O(1) per
    market per snapshot, so the whole 24-day tape streams in bounded memory.
    """
    mkts: dict[str, dict] = {}
    ladders: dict[str, set] = defaultdict(set)
    spot_ts, spot_px = [], []
    n_snap = 0
    for rec in read_snapshots(series, days=days):
        ts = _f(rec.get("recv_ts"))
        spot = _f(rec.get("spot_est"))
        if ts is None:
            continue
        n_snap += 1
        if spot and spot > 0:
            spot_ts.append(ts); spot_px.append(spot)
        for m in rec.get("markets") or []:
            if m.get("market_type") != "binary":
                continue
            st = m.get("strike_type")
            k = _f(m.get("floor_strike"))
            if st not in ("greater", "less", "between") or k is None:
                continue
            ct = m.get("close_time")
            tick = m.get("ticker") or m.get("event_ticker")
            if not ct or not tick:
                continue
            close_ts = pd.Timestamp(ct).timestamp()
            tte_s = close_ts - ts
            if st == "between":
                ladders[m.get("event_ticker") or ""].add(k)
            e = mkts.get(tick)
            if e is None:
                e = mkts[tick] = {"series": series, "ticker": tick,
                                  "event": m.get("event_ticker"),
                                  "stype": st, "k": k, "close_ts": close_ts,
                                  "samples": {}, "after_min_ask": {}, "after_max_bid": {}}
            if m.get("status") != "active" or tte_s <= 0:
                continue
            b, a = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
            if b is None or a is None or not (0 <= b <= a <= 1):
                continue
            empty_book = (b <= 0.0 and a >= 1.0)
            for T in TTE_MIN:
                d = abs(tte_s - T * 60.0)
                if d < TTE_TOL_S:
                    prev = e["samples"].get(T)
                    if prev is None or d < prev[0]:
                        e["samples"][T] = (d, b, a, empty_book, ts)
                # running post-checkpoint quote extremes (maker-fill proxy)
                if tte_s < T * 60.0 - TTE_TOL_S and not empty_book:
                    cur = e["after_min_ask"].get(T)
                    e["after_min_ask"][T] = a if cur is None else min(cur, a)
                    cur = e["after_max_bid"].get(T)
                    e["after_max_bid"][T] = b if cur is None else max(cur, b)
    logger.info("%s: %d snapshots, %d markets, %d spot points",
                series, n_snap, len(mkts), len(spot_ts))
    # between-bin upper bound = successor strike in the event ladder
    out = []
    for e in mkts.values():
        if e["stype"] == "between":
            lad = sorted(ladders.get(e["event"] or "", ()))
            i = bisect_right(lad, e["k"])
            if i >= len(lad):
                continue                      # top bin — no successor, drop
            e["k_hi"] = lad[i]
        out.append(e)
    return out, np.asarray(spot_ts), np.asarray(spot_px)


def settle(mkts: list[dict], spot_ts: np.ndarray, spot_px: np.ndarray) -> list[dict]:
    """Attach outcome from the last spot_est before close; flag knife-edges."""
    settled = []
    for e in mkts:
        i = int(np.searchsorted(spot_ts, e["close_ts"], side="right")) - 1
        if i < 0 or (e["close_ts"] - spot_ts[i]) > STALE_SETTLE_S:
            continue
        s = float(spot_px[i])
        if e["stype"] == "greater":
            outcome, dist = s > e["k"], abs(s - e["k"])
        elif e["stype"] == "less":
            outcome, dist = s < e["k"], abs(s - e["k"])
        else:
            outcome = (e["k"] < s <= e["k_hi"])
            dist = min(abs(s - e["k"]), abs(s - e["k_hi"]))
        e["outcome"] = float(outcome)
        e["knife"] = (dist / s * 1e4) < KNIFE_BPS
        e["day"] = pd.Timestamp(e["close_ts"], unit="s", tz="UTC").strftime("%Y-%m-%d")
        settled.append(e)
    return settled


def sample_rows(settled: list[dict]) -> pd.DataFrame:
    rows = []
    for e in settled:
        for T, (_, b, a, empty, ts) in e["samples"].items():
            rows.append({"series": e["series"], "ticker": e["ticker"],
                         "stype": e["stype"], "tte": T, "bid": b, "ask": a,
                         "mid": (b + a) / 2, "spread": a - b, "no_quote": empty,
                         "outcome": e["outcome"], "knife": e["knife"],
                         "day": e["day"], "k": e["k"], "ts": ts,
                         "min_ask_after": e["after_min_ask"].get(T),
                         "max_bid_after": e["after_max_bid"].get(T)})
    return pd.DataFrame(rows)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def calibration_table(df: pd.DataFrame) -> pd.DataFrame:
    q = df[~df.knife & ~df.no_quote]
    rows = []
    for lo, hi in PROB_BUCKETS:
        for T in TTE_MIN:
            s = q[(q.tte == T) & (q.mid >= lo) & (q.mid < hi)]
            if len(s) < 15:
                continue
            k = int(s.outcome.sum())
            w = wilson(k, len(s))
            rows.append({"bucket": f"{lo:.2f}-{hi:.2f}", "tte": T, "n": len(s),
                         "implied": round(float(s.mid.mean()), 4),
                         "realized": round(k / len(s), 4),
                         "bias_c": round((k / len(s) - float(s.mid.mean())) * 100, 2),
                         "wilson_lo": round(w[0], 4), "wilson_hi": round(w[1], 4),
                         "sig": (w[0] > s.mid.mean()) or (w[1] < s.mid.mean()),
                         "spread_c": round(float(s.spread.mean()) * 100, 2)})
    return pd.DataFrame(rows)


def day_boot_p(pnl: pd.Series, days: pd.Series, n_boot: int = 3000) -> float | None:
    grp = pd.DataFrame({"p": pnl.values, "d": days.values}).groupby("d").p.apply(list)
    blocks = list(grp)
    if len(blocks) < 5:
        return None
    rng = np.random.default_rng(41)
    boots = np.array([np.concatenate([blocks[i] for i in
                      rng.integers(0, len(blocks), len(blocks))]).mean()
                      for _ in range(n_boot)])
    return round(float((boots <= 0).mean()), 4)


def rule_pnl(sub: pd.DataFrame, side: str, mult: float, *, maker: bool) -> pd.DataFrame:
    """Settlement P&L per contract for one rule leg.

    taker: cross now (buy at ask / sell at bid), full fee at the fill price.
    maker: post one tick inside (buy at bid+1c / sell at ask−1c); filled only
    if a LATER quote crossed our level — an optimistic approximation (quote
    touching ≠ our order trading), fee = 25% of taker at the fill price.
    """
    out = []
    for _, r in sub.iterrows():
        if side == "buy":
            if maker:
                L = round(r.bid + 0.01, 2)
                if L >= r.ask or r.min_ask_after is None or r.min_ask_after > L:
                    continue
                px = L
            else:
                px = r.ask
            if not (0 < px < 1):
                continue
            f = fee(px, mult) * (MAKER_FRAC if maker else 1.0)
            out.append({"pnl": r.outcome - px - f, "day": r.day})
        else:
            if maker:
                L = round(r.ask - 0.01, 2)
                if L <= r.bid or r.max_bid_after is None or r.max_bid_after < L:
                    continue
                px = L
            else:
                px = r.bid
            if not (0 < px < 1):
                continue
            f = fee(px, mult) * (MAKER_FRAC if maker else 1.0)
            out.append({"pnl": px - r.outcome - f, "day": r.day})
    return pd.DataFrame(out)


def summarize_rule(name: str, tp: pd.DataFrame) -> dict:
    if len(tp) < 10:
        return {"rule": name, "n": len(tp), "note": "thin"}
    pnl = tp.pnl
    return {"rule": name, "n": len(tp),
            "mean_c": round(float(pnl.mean()) * 100, 3),
            "median_c": round(float(pnl.median()) * 100, 3),
            "hit": round(float((pnl > 0).mean()), 3),
            "total_$": round(float(pnl.sum()), 2),
            "boot_p": day_boot_p(pnl, tp.day)}


def model_prob(spot: float, k: float, sig_5m: float, tte_min: float,
               stype: str) -> float | None:
    if not (spot > 0 and k > 0 and sig_5m > 0):
        return None
    z = math.log(spot / k) / (sig_5m * math.sqrt(tte_min / 5.0))
    p_above = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return p_above if stype == "greater" else 1.0 - p_above


def run(days: list[str] | None = None) -> dict:
    all_samples, spot_series = [], {}
    for s in SERIES:
        mkts, sts, spx = collect_series(s, days)
        settled = settle(mkts, sts, spx)
        df = sample_rows(settled)
        if len(df):
            all_samples.append(df)
        und = "BTC" if s.startswith("KXBTC") else "ETH"
        if und not in spot_series and len(sts):
            ser = pd.Series(spx, index=pd.to_datetime(sts, unit="s", utc=True))
            spot_series[und] = ser.resample("5min", label="right", closed="right").last().dropna()
        logger.info("%s: %d settled markets, %d samples", s, len(settled), len(df))
    df = pd.concat(all_samples, ignore_index=True)
    df["und"] = np.where(df.series.str.startswith("KXBTC"), "BTC", "ETH")

    days_sorted = sorted(df.day.unique())
    is_days = set(days_sorted[:len(days_sorted) // 2])
    df["is_half"] = df.day.isin(is_days)

    res: dict = {"n_markets_settled": int(df.ticker.nunique()),
                 "n_samples": len(df),
                 "knife_excluded": int(df[df.knife].ticker.nunique()),
                 "empty_book_frac": round(float(df.no_quote.mean()), 4),
                 "days": [days_sorted[0], days_sorted[-1]],
                 "is_days": len(is_days), "oos_days": len(days_sorted) - len(is_days)}

    # ── calibration (full-sample descriptive + IS-only for rule b) ──
    cal_full = calibration_table(df)
    cal_is = calibration_table(df[df.is_half])
    res["calibration_full"] = cal_full.to_dict("records")

    rules: list[dict] = []
    live = df[~df.knife & ~df.no_quote]
    oos = live[~live.is_half]

    for mult in TAKER_MULTS:
        for maker in (False, True):
            tag = f"m{mult}|{'maker' if maker else 'taker'}"
            # (a) extremes
            cheap = oos[(oos.ask > 0) & (oos.ask <= 0.05) & (oos.tte <= 60)]
            rich = oos[(oos.bid >= 0.95) & (oos.bid < 1) & (oos.tte <= 60)]
            rules.append(summarize_rule(f"a.buy_longshot≤5c|{tag}",
                                        rule_pnl(cheap, "buy", mult, maker=maker)))
            rules.append(summarize_rule(f"a.sell_favorite≥95c|{tag}",
                                        rule_pnl(rich, "sell", mult, maker=maker)))
            # (b) IS-calibration direction
            biased = cal_is[(cal_is.n >= 30) & (cal_is.sig) & (cal_is.bias_c.abs() >= 3)]
            legs = []
            for _, b in biased.iterrows():
                lo, hi = (float(x) for x in b.bucket.split("-"))
                s = oos[(oos.tte == b.tte) & (oos.mid >= lo) & (oos.mid < hi)]
                legs.append(rule_pnl(s, "buy" if b.bias_c > 0 else "sell",
                                     mult, maker=maker))
            if legs:
                allb = pd.concat(legs, ignore_index=True)
                rules.append(summarize_rule(
                    f"b.IS_bias_dir({len(biased)}bkt)|{tag}", allb))
            # (c) fair-value model, threshold contracts only
            thr_mkts = oos[oos.stype.isin(["greater", "less"])
                           & oos.tte.isin(MODEL_TTES)].copy()
            if len(thr_mkts):
                probs = []
                for _, r in thr_mkts.iterrows():
                    ser = spot_series.get(r.und)
                    if ser is None:
                        probs.append(None); continue
                    t = pd.Timestamp(r.ts, unit="s", tz="UTC")
                    hist = ser.loc[:t]
                    if len(hist) < 60:
                        probs.append(None); continue
                    sig = float(np.log(hist).diff().tail(VOL_WIN).std(ddof=0))
                    probs.append(model_prob(float(hist.iloc[-1]), r.k, sig,
                                            r.tte, r.stype))
                thr_mkts["p_model"] = probs
                thr_mkts = thr_mkts.dropna(subset=["p_model"])
                # threshold chosen on the IS half (same construction there)
                best_th, best_is = None, -9e9
                is_thr = live[live.is_half & live.stype.isin(["greater", "less"])
                              & live.tte.isin(MODEL_TTES)].copy()
                probs = []
                for _, r in is_thr.iterrows():
                    ser = spot_series.get(r.und)
                    t = pd.Timestamp(r.ts, unit="s", tz="UTC")
                    hist = ser.loc[:t] if ser is not None else pd.Series(dtype=float)
                    if len(hist) < 60:
                        probs.append(None); continue
                    sig = float(np.log(hist).diff().tail(VOL_WIN).std(ddof=0))
                    probs.append(model_prob(float(hist.iloc[-1]), r.k, sig,
                                            r.tte, r.stype))
                is_thr["p_model"] = probs
                is_thr = is_thr.dropna(subset=["p_model"])
                for th in MODEL_THRESH:
                    sel = is_thr[(is_thr.p_model - is_thr.mid).abs() >= th]
                    buys = rule_pnl(sel[sel.p_model > sel.mid], "buy", mult, maker=maker)
                    sells = rule_pnl(sel[sel.p_model < sel.mid], "sell", mult, maker=maker)
                    both = pd.concat([buys, sells], ignore_index=True)
                    if len(both) >= 10 and both.pnl.mean() > best_is:
                        best_is, best_th = float(both.pnl.mean()), th
                if best_th is not None:
                    sel = thr_mkts[(thr_mkts.p_model - thr_mkts.mid).abs() >= best_th]
                    buys = rule_pnl(sel[sel.p_model > sel.mid], "buy", mult, maker=maker)
                    sells = rule_pnl(sel[sel.p_model < sel.mid], "sell", mult, maker=maker)
                    both = pd.concat([buys, sells], ignore_index=True)
                    rules.append(summarize_rule(
                        f"c.model_gap≥{best_th}(IS-picked)|{tag}", both))

    res["rules"] = rules
    res["n_rule_variants"] = len(rules)
    res["bonferroni_p"] = round(0.05 / max(len(rules), 1), 5)
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", default=None,
                    help="comma list YYYY-MM-DD; default = all except today")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.days:
        days = args.days.split(",")
    else:
        import glob
        from crypto_trading.crypto_common.config import PRICE_DATA
        fs = glob.glob(str(PRICE_DATA / "kalshi" / "event_strips" / "prod"
                           / "KXBTC" / "markets" / "*.jsonl*"))
        today = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
        days = sorted({f.split("/")[-1].split(".")[0] for f in fs} - {today})

    res = run(days)
    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"event_binary_calibration_{stamp}.json").write_text(
        json.dumps(res, indent=1, default=str))

    print("=" * 108)
    print(f"KALSHI BINARY CALIBRATION — {res['n_markets_settled']} settled markets, "
          f"{res['n_samples']} samples, days {res['days'][0]}..{res['days'][1]} "
          f"(IS {res['is_days']}d / OOS {res['oos_days']}d) | knife-edge excluded "
          f"{res['knife_excluded']} | empty-book frac {res['empty_book_frac']:.1%}")
    print("=" * 108)
    cal = pd.DataFrame(res["calibration_full"])
    if len(cal):
        print("\nCALIBRATION (full sample; bias_c = realized − implied, cents; "
              "sig = Wilson CI excludes implied):")
        piv = cal.pivot_table(index="bucket", columns="tte", values="bias_c")
        print(piv.to_string())
        print("\nsample sizes:")
        print(cal.pivot_table(index="bucket", columns="tte", values="n").fillna(0)
              .astype(int).to_string())
        print("\nspread (cents):")
        print(cal.pivot_table(index="bucket", columns="tte", values="spread_c")
              .round(1).to_string())
        nsig = int(cal.sig.sum())
        print(f"\nbuckets with significant mispricing: {nsig}/{len(cal)}")
    print(f"\nRULES (OOS half only; Bonferroni p ≤ {res['bonferroni_p']}):")
    r = pd.DataFrame(res["rules"])
    if len(r):
        cols = [c for c in ("rule", "n", "mean_c", "median_c", "hit", "total_$",
                            "boot_p") if c in r.columns]
        print(r[cols].to_string(index=False))
        surv = r[(r.get("mean_c", pd.Series(dtype=float)) > 0)
                 & (r.get("boot_p", pd.Series(dtype=float)).fillna(1)
                    <= res["bonferroni_p"])]
        print(f"\nSURVIVORS after Bonferroni: {len(surv)}")
        if len(surv):
            print(surv[cols].to_string(index=False))
    print("\nCAVEATS: settlement via recorded spot_est (not the official print); "
          "maker fills = 'later quote crossed our price' (OPTIMISTIC); 24 days, "
          "one regime; taker mult 0.07 vs 0.10 shown because the official "
          "schedule is bot-walled and crypto may be a premium tier.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
