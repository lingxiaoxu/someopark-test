"""Knockdown — buy the freshly-knocked near-ATM binary side (Plan 18, W5).

Reverse-engineered from a profitable Polymarket account (5,235 trades,
+$58.9K/16d on 5-min BTC binaries): when a SMALL spot move knocks one side of
a near-ATM binary into the 0.15-0.45 zone, buy it (taker) and hold to
settlement — the counterparty is retail momentum paying 0.55-0.85 for the side
that just "won" the last few minutes. Kalshi translation uses the hourly
KXBTC ladders (no 5-min series exists here) and REAL fees 0.07·P·(1−P).

CANONICAL BACKTEST = the adversarially-hardened version (2026-08-11). The
naive first pass (+17.2c, t 14.3) survived four attacks, each of which killed
earlier candidates elsewhere in this project:
  1. pre-registered primary config (zone imported from HIS fills, not fitted);
  2. ASK PERSISTENCE — a trigger only fills if the ask still exists (≤ entry
     +2c) at the NEXT snapshot, and fills at THAT price;
  3. independent settlement — outcomes re-derived from our own 5s composite
     agreed 99.5% with the venue's spot_est;
  4. L2 DEPTH GATE — 62% of triggers had NO real resting counterparty size
     (phantom derived quotes); requiring ≥50 contracts at the level keeps the edge.
CANONICAL NUMBERS (hardened run 2026-08-11: one-sided depth window +
pre-close-only settlement): n=1,797, mean +24.5c/contract, hit 49.6% @ 0.24,
NW-t 18.4, boot_p 0.0; all 9 sweep configs OOS-positive (t 5.9-15.8);
depth-capped gross ≈ $83.8K/35d; fee-mult 0.10 sensitivity −0.5c.
ETH failed (−1.4c) → BTC only. Economic scale on tape: min(depth,200)
contracts/signal ≈ $2.6K/day gross. The ONE thing tape cannot exclude: both
recorded streams share one recorder; live capture rate vs competing bots is
unknown until the W5 dry-run probe measures it. NOT ARMED until then.
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import PRICE_DATA, SIGNALS_DIR
from crypto_trading.crypto_common.trade_stats import newey_west_tstat

logger = logging.getLogger(__name__)

STRIPS = PRICE_DATA / "kalshi" / "event_strips" / "prod"

# ── frozen parameters (registered 2026-08-11; ZONE imported from his fills) ──
ZONE = (0.15, 0.45)
MONEYNESS_BPS = 30.0
KNIFE_BPS = 5.0
LOOKBACK_SNAPS = 5
MIN_DEPTH = 50                   # real resting contracts at the matched level
PRIMARY = {"tte_lo": 5.0, "tte_hi": 45.0, "dip_c": 0.05}
SWEEP = [{"tte_lo": a, "tte_hi": b, "dip_c": d}
         for (a, b) in ((5.0, 45.0), (5.0, 20.0), (20.0, 60.0))
         for d in (0.03, 0.05, 0.08)]
FEE_MULTS = (0.07, 0.10)         # standard / crypto-premium sensitivity


def fee(p: float, mult: float = 0.07) -> float:
    return mult * p * (1 - p)


def knockdown_trigger(hist: list[tuple[float, float]], ya: float, na: float,
                      dip_c: float, zone: tuple[float, float] = ZONE
                      ) -> str | None:
    """Pure trigger: which side (if any) was freshly knocked into the zone.

    ``hist`` = the last LOOKBACK_SNAPS (ya, na) tuples EXCLUDING the current
    snapshot. Fires when the current ask sits in the zone and is ≥ dip_c below
    that side's max over the lookback. Shared verbatim by backtest and W5 live.
    """
    if len(hist) < LOOKBACK_SNAPS:
        return None
    for side, ask, col in (("yes", ya, 0), ("no", na, 1)):
        if zone[0] <= ask <= zone[1] and \
                max(x[col] for x in hist) - ask >= dip_c:
            return side
    return None


def settle_outcome(side: str, strike: float, spot: float) -> bool:
    return (spot > strike) if side == "yes" else (spot <= strike)


def build_ob_index(series: str) -> dict:
    """ticker → list[(ts, {no_px:sz}, {yes_px:sz})] from the L2 stream.

    Memory: keeps ONLY near-money records (|strike − spot_est| ≤ 60bps — twice
    the strategy's 30bps gate, so every possible query is covered). The naive
    full index (every record, 35 days) is several GB of Python objects and got
    the first canonical run OOM-killed; the filter cuts it ~50×.
    """
    idx: dict[str, list] = {}
    for f in sorted((STRIPS / series / "orderbook").glob("2026-*")):
        op = gzip.open if f.suffix == ".gz" else open
        try:
            fh = op(f, "rt")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    k = float(d.get("strike") or 0)
                    sp = float(d.get("spot_est") or 0)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if k <= 0 or sp <= 0 or abs(k - sp) / sp * 1e4 > 2 * MONEYNESS_BPS:
                    continue
                ob = (d.get("ob") or {}).get("orderbook_fp") or {}
                no = {round(float(p), 2): float(s)
                      for p, s in (ob.get("no_dollars") or [])}
                ye = {round(float(p), 2): float(s)
                      for p, s in (ob.get("yes_dollars") or [])}
                idx.setdefault(d.get("ticker"), []).append((d["recv_ts"], no, ye))
    return idx


def depth_at(ob_idx: dict, ticker: str, ts: float, side: str,
             ask: float, tol_s: float = 95.0) -> float:
    """Max real resting size at the counterparty level (1−ask ± 1.5c).

    PIT: only book records AT OR BEFORE the fill instant count (window
    [ts − tol_s, ts + 5s]; the +5s absorbs recorder clock skew between the two
    streams). The first audit draft used a symmetric ±95s window — depth that
    appeared only AFTER the fill would have validated a fill we could not have
    made. Buying YES at the ask consumes a resting NO bid at 1−ask (mirror for
    NO)."""
    best = 0.0
    for rts, no, ye in ob_idx.get(ticker, ()):
        if -tol_s <= rts - ts <= 5.0:
            opp = no if side == "yes" else ye
            tgt = round(1 - ask, 2)
            best = max(best, sum(s for p, s in opp.items()
                                 if abs(p - tgt) <= 0.015))
    return best


def stream_trades(series: str, cfg: dict, ob_idx: dict | None,
                  *, strict: bool = True) -> pd.DataFrame:
    """Replay the snapshot stream. strict=True (canonical) = ask-persistence
    fill at the NEXT snapshot + L2 depth gate; strict=False = naive first-pass
    (kept for comparison so the artifact magnitude stays visible)."""
    hist: dict[str, list] = {}
    pend: dict[str, dict] = {}
    open_pos: dict[str, dict] = {}
    last_spot: dict[str, tuple] = {}
    for f in sorted((STRIPS / series / "markets").glob("2026-*")):
        op = gzip.open if f.suffix == ".gz" else open
        try:
            fh = op(f, "rt")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                spot = d.get("spot_est")
                if not spot:
                    continue
                rts = d["recv_ts"]
                rts_t = pd.Timestamp(rts, unit="s", tz="UTC")
                for m in d.get("markets", []):
                    ct = m.get("close_time")
                    try:
                        k = float(m.get("floor_strike") or 0)
                        tkr = m.get("ticker") or ""
                        tte = (pd.Timestamp(ct).timestamp() - rts) / 60
                        ya = float(m.get("yes_ask_dollars") or 0)
                        na = float(m.get("no_ask_dollars") or 0)
                    except Exception:
                        continue
                    # settlement reference: only PRE-close snapshots may set it
                    # (post-close lines would overwrite it with later spot —
                    # an outcome-misclassification vector caught in audit)
                    if ct and tte > -0.5:
                        last_spot[ct] = (spot, rts_t)
                    if k <= 0 or tte < -1 or tte > 90:
                        continue
                    key = tkr or f"{k}|{ct}"
                    if strict and key in pend:
                        p = pend.pop(key)
                        ask_now = ya if p["side"] == "yes" else na
                        if 0 < ask_now <= p["px"] + 0.02:
                            dep = (depth_at(ob_idx, tkr, rts, p["side"], ask_now)
                                   if ob_idx is not None else MIN_DEPTH)
                            if dep >= MIN_DEPTH:
                                open_pos[key] = {**p, "px": ask_now, "depth": dep}
                    if abs(k - spot) / spot * 1e4 > MONEYNESS_BPS:
                        hist.pop(key, None)
                        continue
                    h = hist.setdefault(key, [])
                    h.append((ya, na))
                    if len(h) > LOOKBACK_SNAPS + 1:
                        h.pop(0)
                    if key in open_pos or key in pend or \
                            not (cfg["tte_lo"] <= tte <= cfg["tte_hi"]):
                        continue
                    side = knockdown_trigger(h[:-1], ya, na, cfg["dip_c"])
                    if side is None:
                        continue
                    rec = {"ts": rts_t, "side": side, "k": k, "close": ct,
                           "px": ya if side == "yes" else na,
                           "day": str(rts_t.date()), "tte": round(tte, 1)}
                    if strict:
                        pend[key] = rec
                    else:
                        open_pos[key] = {**rec, "depth": np.nan}
    rows = []
    for key, p in open_pos.items():
        s = last_spot.get(p["close"])
        if s is None:
            continue
        spot, sts = s
        ct_ts = pd.Timestamp(p["close"])
        if (ct_ts - sts).total_seconds() > 600:
            continue
        if abs(spot - p["k"]) / spot * 1e4 < KNIFE_BPS:
            continue
        win = settle_outcome(p["side"], p["k"], spot)
        rows.append({**p, "series": series, "win": win,
                     "pnl_c": ((1.0 if win else 0.0) - p["px"]
                               - fee(p["px"])) * 100})
    return pd.DataFrame(rows)


def boot_p(net: pd.Series, days: pd.Series, n: int = 3000) -> float | None:
    grp = pd.DataFrame({"n": net.values, "d": days.values}).groupby("d").n.apply(list)
    blocks = list(grp)
    if len(blocks) < 5:
        return None
    rng = np.random.default_rng(23)
    boots = np.array([np.concatenate([blocks[i] for i in
                      rng.integers(0, len(blocks), len(blocks))]).mean()
                      for _ in range(n)])
    return round(float((boots <= 0).mean()), 4)


def summarize(tp: pd.DataFrame, label: str) -> dict:
    if len(tp) < 10:
        return {"label": label, "n": len(tp), "note": "thin"}
    net = tp.pnl_c.reset_index(drop=True)
    out = {"label": label, "n": len(net),
           "mean_c": round(float(net.mean()), 2),
           "median_c": round(float(net.median()), 2),
           "hit": round(float(tp.win.mean()), 3),
           "avg_entry": round(float(tp.px.mean()), 3),
           "nw_t": round(float(newey_west_tstat(net)["t_nw"]), 2),
           "boot_p": boot_p(net, tp.day)}
    if "depth" in tp.columns and tp.depth.notna().any():
        cap = tp.depth.clip(upper=200)
        out["gross_usd_at_min_depth_200"] = round(float((tp.pnl_c / 100 * cap).sum()), 0)
        # fee sensitivity at the crypto-premium multiplier
        alt = tp.pnl_c / 100 + tp.px.map(lambda p: fee(p, 0.07) - fee(p, 0.10))
        out["mean_c_at_mult_0.10"] = round(float(alt.mean() * 100), 2)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--series", default="KXBTC")
    ap.add_argument("--naive", action="store_true",
                    help="first-pass mode without persistence/depth (comparison)")
    ap.add_argument("--skip-sweep", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    ob = None if args.naive else build_ob_index(args.series)
    logger.info("orderbook index: %s tickers", len(ob) if ob else 0)
    tp = stream_trades(args.series, PRIMARY, ob, strict=not args.naive)
    out = {"mode": "naive" if args.naive else "strict(persistence+depth)",
           "series": args.series, "min_depth": MIN_DEPTH,
           "primary": summarize(tp, "PRIMARY tte5-45 dip5c"), "sweep": []}
    if not args.skip_sweep:
        days_sorted = sorted(tp.day.unique()) if len(tp) else []
        is_days = set(days_sorted[:len(days_sorted) // 2])
        for cfg in SWEEP:
            pp = stream_trades(args.series, cfg, ob, strict=not args.naive)
            if len(pp):
                out["sweep"].append({"cfg": cfg,
                                     "full": summarize(pp, "full"),
                                     "oos": summarize(pp[~pp.day.isin(is_days)], "oos")})
    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"knockdown_{stamp}.json").write_text(json.dumps(out, indent=1, default=str))

    p = out["primary"]
    print("=" * 96)
    print(f"KNOCKDOWN [{out['mode']}] {args.series} — PRIMARY: n={p.get('n')} "
          f"mean {p.get('mean_c')}c median {p.get('median_c')} hit {p.get('hit')} "
          f"entry {p.get('avg_entry')} NW-t {p.get('nw_t')} boot_p {p.get('boot_p')}")
    if "gross_usd_at_min_depth_200" in p:
        print(f"  depth-capped gross ${p['gross_usd_at_min_depth_200']:,.0f} | "
              f"mult=0.10 sensitivity {p.get('mean_c_at_mult_0.10')}c")
    for rec in out["sweep"]:
        f_, o_ = rec["full"], rec["oos"]
        print(f"  {rec['cfg']} → full {f_.get('mean_c')}c (t {f_.get('nw_t')}) | "
              f"OOS {o_.get('mean_c')}c (t {o_.get('nw_t')}, p {o_.get('boot_p')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
