"""Plan 02 event×perp — PRELIMINARY backtest on captured strip snapshots.

⚠️ PRELIMINARY — this runs on the ~4 days of self-recorded event-strip data we
have so far. It is a MILESTONE-2 mechanism check (crypto-dev/02 §10.2: "validate
arb-bound violations on captured data — no perp API needed"), NOT a validation
gate. The sample is far too small and too short for OOS Sharpe / DSR. Treat every
number here as directional evidence about whether the two sub-signals have any
predictive/expectancy content at all.

Two sub-backtests, both reusing signals/implied_dist.py:

 1. STATIC ARB (near-riskless, Plan 02 §2c / §6):
    - full-tile sum arb: a complete outcome partition must price to 1; buy the
      whole tile if Σask < 1 − fees, sell it if Σbid > 1 + fees. Captured credit
      settles risk-free at expiry (the partition pays exactly 1).
    - pairwise monotonicity violations across the threshold survival curve.
    We already saw (single snapshot) that nothing clears fees; here we CONFIRM
    across every captured snapshot and report the near-miss distribution — how
    close the tile sums get to the 1±fees no-arb band.

 2. DISLOCATION mean-reversion (the softer, tradeable edge):
    signal_t = (implied_mean_t − perp_spot_t) / perp_spot_t, z-scored over the
    available history. perp_spot is the perp mark / contract_size captured
    CONTEMPORANEOUSLY in each strip record (`spot_est`); when absent we fall
    back to nearest-timestamp poll market stats. We cannot trade the full
    two-leg strategy yet (no perp order path), so we measure the INFORMATION
    COEFFICIENT: corr( gap_z_t , Δ(perp_spot − implied_mean) over next K snaps ).
    Mean-reversion of the gap ⇒ POSITIVE IC (perp catches up to the implied
    distribution). Reported with sign + sample size + a caveat.

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.event_perp.backtest \
        [--series KXBTC,KXETH] [--fee-rate 0.07] [--fwd 3] [--zwin 60]
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import PRICE_DATA, SIGNALS_DIR
from crypto_trading.crypto_common.loader import _read_jsonl_days, load_poll_market_stats
from crypto_trading.crypto_strategies.event_perp.signals.implied_dist import (
    _nearest_survival, event_fee, find_violations, implied_distribution,
    implied_distribution_from_bins, parse_bins, parse_strip, tile_arb)

logger = logging.getLogger(__name__)

STRIPS_DIR = PRICE_DATA / "kalshi" / "event_strips"
OUT_DIR = SIGNALS_DIR / "event_perp" / "backtests"
SERIES_TO_PERP = {"KXBTC": "KXBTCPERP", "KXBTCD": "KXBTCPERP",
                  "KXETH": "KXETHPERP", "KXETHD": "KXETHPERP"}

# Size cap so a hypothetical arb doesn't claim an implausible book (contracts).
MAX_ARB_SIZE = 50.0


# ── snapshot reading ─────────────────────────────────────────────────────────

def read_snapshots(series: str, *, env: str = "prod", days: list[str] | None = None):
    """Yield captured strip 'markets' records for one series (gz-aware)."""
    yield from _read_jsonl_days(STRIPS_DIR / env / series / "markets", days=days)


# ── sub-backtest 1: static arb ───────────────────────────────────────────────

@dataclass
class ArbResult:
    n_snapshots: int = 0
    n_tile_evaluated: int = 0
    n_tile_complete: int = 0
    n_tile_buy_arb: int = 0          # fee-positive buy-the-tile opportunities
    n_tile_sell_arb: int = 0
    n_pair_violations: int = 0       # fee-positive pairwise monotonicity arbs
    captured_credit: float = 0.0     # hypothetical $ credit locked to settlement
    # near-miss diagnostics on the tile sum vs the 1±fee no-arb band — COMPLETE
    # partitions only (an incomplete tile's Σ is not comparable to 1)
    tile_sum_ask_complete: list[float] = field(default_factory=list)
    tile_sum_bid_complete: list[float] = field(default_factory=list)
    best_buy_credit_net: float = -9.99
    best_sell_credit_net: float = -9.99

    def summary(self) -> dict:
        # Σask/Σbid near-miss stats are only meaningful on COMPLETE partitions
        # (an incomplete tile has Σ<1 simply because outcomes are missing, not
        # because of an arb). Restrict the band diagnostics accordingly.
        has = bool(self.tile_sum_ask_complete)
        ask = np.array(self.tile_sum_ask_complete) if has else None
        bid = np.array(self.tile_sum_bid_complete) if has else None
        return {
            "n_snapshots": self.n_snapshots,
            "n_tile_evaluated": self.n_tile_evaluated,
            "n_tile_complete_partition": self.n_tile_complete,
            "n_tile_buy_arb_fee_positive": self.n_tile_buy_arb,
            "n_tile_sell_arb_fee_positive": self.n_tile_sell_arb,
            "n_pairwise_violations_fee_positive": self.n_pair_violations,
            "hypothetical_credit_captured": round(self.captured_credit, 4),
            "best_buy_credit_net_complete_only": (round(self.best_buy_credit_net, 4)
                                                  if self.n_tile_complete else None),
            "best_sell_credit_net_complete_only": (round(self.best_sell_credit_net, 4)
                                                   if self.n_tile_complete else None),
            "complete_tile_sum_ask_median": round(float(np.median(ask)), 4) if has else None,
            "complete_tile_sum_ask_min": round(float(np.min(ask)), 4) if has else None,
            "complete_tile_sum_bid_median": round(float(np.median(bid)), 4) if has else None,
            "complete_tile_sum_bid_max": round(float(np.max(bid)), 4) if has else None,
            "note": ("NO complete sum-to-1 partition captured — the greater/less "
                     "threshold tails never closed the range of bins, so the tile "
                     "arb could not be verified as a true partition."
                     if self.n_tile_complete == 0 else None),
        }


def run_static_arb(series: str, *, fee_rate: float = 0.07,
                   days: list[str] | None = None) -> ArbResult:
    r = ArbResult()
    for rec in read_snapshots(series, days=days):
        r.n_snapshots += 1
        markets = rec.get("markets") or []
        surv = parse_strip(markets)
        bins = parse_bins(markets)

        # pairwise monotonicity violations on the survival (threshold) curve
        for v in find_violations(surv, fee_rate=fee_rate, min_net_credit=0.0):
            r.n_pair_violations += 1
            r.captured_credit += v.net_credit * min(v.size, MAX_ARB_SIZE)

        # full-tile sum arb
        if bins:
            gap = float(np.median([b.k_hi - b.k_lo for b in bins]))
            tail_lo = _nearest_survival(surv, bins[0].k_lo, gap)
            tail_hi = _nearest_survival(surv, bins[-1].k_hi, gap)
            ta = tile_arb(bins, tail_lo=tail_lo, tail_hi=tail_hi, fee_rate=fee_rate)
            if ta is not None:
                r.n_tile_evaluated += 1
                if ta.coverage_complete:
                    r.n_tile_complete += 1
                    # band + best-credit diagnostics are meaningful only here
                    r.tile_sum_ask_complete.append(ta.sum_ask)
                    r.tile_sum_bid_complete.append(ta.sum_bid)
                    r.best_buy_credit_net = max(r.best_buy_credit_net, ta.buy_credit_net)
                    r.best_sell_credit_net = max(r.best_sell_credit_net, ta.sell_credit_net)
                    # only a COMPLETE partition is a genuine sum-to-1 arb
                    if ta.buy_credit_net > 0:
                        r.n_tile_buy_arb += 1
                        r.captured_credit += ta.buy_credit_net * min(ta.min_leg_size, MAX_ARB_SIZE)
                    if ta.sell_credit_net > 0:
                        r.n_tile_sell_arb += 1
                        r.captured_credit += ta.sell_credit_net * min(ta.min_leg_size, MAX_ARB_SIZE)
    return r


# ── sub-backtest 2: dislocation IC ───────────────────────────────────────────

def _perp_spot_from_record(rec: dict) -> float | None:
    """Contemporaneous perp-implied spot captured in the strip record."""
    s = rec.get("spot_est")
    try:
        s = float(s)
        return s if s > 0 else None
    except (TypeError, ValueError):
        return None


def _implied_mean(rec: dict) -> float | None:
    markets = rec.get("markets") or []
    surv = parse_strip(markets)
    bins = parse_bins(markets)
    tail_lo = tail_hi = None
    if bins:
        gap = float(np.median([b.k_hi - b.k_lo for b in bins]))
        tail_lo = _nearest_survival(surv, bins[0].k_lo, gap)
        tail_hi = _nearest_survival(surv, bins[-1].k_hi, gap)
    dist = (implied_distribution_from_bins(bins, tail_lo=tail_lo, tail_hi=tail_hi)
            or implied_distribution(surv))
    return dist.mean if dist is not None else None


def _rolling_z(s: pd.Series, win: int) -> pd.Series:
    mp = max(10, win // 3)
    mu = s.rolling(win, min_periods=mp).mean()
    sd = s.rolling(win, min_periods=mp).std(ddof=0)
    return (s - mu) / sd.replace(0.0, np.nan)


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Rank correlation without scipy (rank → Pearson)."""
    if len(a) < 3:
        return float("nan")
    ar = pd.Series(a).rank().to_numpy()
    br = pd.Series(b).rank().to_numpy()
    ar -= ar.mean(); br -= br.mean()
    denom = np.sqrt((ar @ ar) * (br @ br))
    return float(ar @ br / denom) if denom > 0 else float("nan")


def run_dislocation_ic(series: str, *, fwd: int = 3, zwin: int = 60,
                       days: list[str] | None = None,
                       use_poll_fallback: bool = True) -> dict:
    perp = SERIES_TO_PERP.get(series)
    rows = []
    for rec in read_snapshots(series, days=days):
        ts = rec.get("recv_ts")
        im = _implied_mean(rec)
        ps = _perp_spot_from_record(rec)
        if ts is None or im is None:
            continue
        # close_time = the event HORIZON; strips interleave 2 horizons per series,
        # so the gap time series MUST be built within each horizon separately —
        # otherwise the alternation between two horizon means fabricates
        # mean-reversion and inflates the IC (a bug found on 2026-07-10).
        rows.append({"recv_ts": float(ts), "implied_mean": im, "perp_spot": ps,
                     "close_time": rec.get("close_time")})
    if not rows:
        return {"series": series, "n": 0, "note": "no usable snapshots"}
    df = pd.DataFrame(rows).sort_values("recv_ts").reset_index(drop=True)

    # fill missing perp_spot from nearest poll market stat (rare; spot_est is
    # captured contemporaneously so this is a fallback only)
    n_from_poll = 0
    if use_poll_fallback and df["perp_spot"].isna().any() and perp:
        try:
            stats = load_poll_market_stats(perp)
        except Exception:
            stats = pd.DataFrame()
        if len(stats) and stats.get("price") is not None:
            csize = float(stats["contract_size"].dropna().iloc[-1]) if "contract_size" in stats else None
            st_ts = stats.index.view("int64") / 1e9
            st_px = stats["price"].to_numpy()
            for i in df.index[df["perp_spot"].isna()]:
                j = int(np.argmin(np.abs(st_ts - df.at[i, "recv_ts"])))
                if csize and csize > 0 and abs(st_ts[j] - df.at[i, "recv_ts"]) < 120:
                    df.at[i, "perp_spot"] = st_px[j] / csize
                    n_from_poll += 1
    df = df.dropna(subset=["perp_spot"])
    if len(df) < fwd + 20:
        return {"series": series, "n": int(len(df)),
                "note": f"insufficient usable rows ({len(df)}) for IC at fwd={fwd}"}

    df["gap"] = (df["implied_mean"] - df["perp_spot"]) / df["perp_spot"]

    # per-horizon gap_z + forward convergence, then POOL the pairs for one IC
    gz_all, fd_all = [], []
    n_horizons = 0
    for _, g in df.groupby("close_time", sort=False):
        g = g.sort_values("recv_ts")
        if len(g) < max(fwd + 20, zwin // 2):
            continue
        n_horizons += 1
        gz = _rolling_z(g["gap"], zwin)
        d = g["perp_spot"] - g["implied_mean"]              # = −gap·perp_spot
        fwd_dd = d.shift(-fwd) - d                          # forward convergence move
        pair = pd.DataFrame({"gz": gz, "fd": fwd_dd}).dropna()
        gz_all.append(pair["gz"].to_numpy())
        fd_all.append(pair["fd"].to_numpy())

    gz = np.concatenate(gz_all) if gz_all else np.array([])
    fd = np.concatenate(fd_all) if fd_all else np.array([])
    if len(gz) < 20:
        return {"series": series, "n": int(len(gz)), "n_horizons": n_horizons,
                "note": "insufficient within-horizon (gap_z, fwd_dd) pairs"}

    ic = _spearman(gz, fd)
    trade_ret = np.sign(gz) * fd                           # mean-reversion trade
    hit = float(np.mean(trade_ret > 0))
    return {
        "series": series, "perp": perp, "n_snapshots_used": int(len(gz)),
        "n_horizons_pooled": n_horizons,
        "fwd_snaps": fwd, "z_window": zwin, "perp_spot_from_poll_fallback": n_from_poll,
        "gap_bps_mean": round(1e4 * float(df["gap"].mean()), 3),
        "gap_bps_std": round(1e4 * float(df["gap"].std()), 3),
        "IC_spearman_gapz_vs_fwd_convergence": round(ic, 4),
        "IC_sign": "positive→mean-reversion predictive" if ic > 0 else
                   ("negative→gap widens (momentum)" if ic < 0 else "flat"),
        "reversion_trade_hit_rate": round(hit, 4),
        "caveat": "within-horizon pooled; overlapping fwd windows overstate "
                  "significance; stale event quotes can inflate reversion.",
    }


# ── orchestration ────────────────────────────────────────────────────────────

def run(series_list: list[str], *, fee_rate: float = 0.07, fwd: int = 3,
        zwin: int = 60, days: list[str] | None = None) -> dict:
    out = {"PRELIMINARY": True,
           "caveat": "~4-day self-recorded sample; mechanism check only, NOT a "
                     "validation gate (crypto-dev/02 §8/§10.2).",
           "fee_rate": fee_rate, "series": {}}
    for s in series_list:
        arb = run_static_arb(s, fee_rate=fee_rate, days=days)
        disloc = run_dislocation_ic(s, fwd=fwd, zwin=zwin, days=days)
        out["series"][s] = {"static_arb": arb.summary(), "dislocation": disloc}
    return out


def _print_report(res: dict) -> None:
    print("=" * 66)
    print("Plan 02 event×perp — PRELIMINARY backtest (~4-day captured strips)")
    print("  " + res["caveat"])
    print("=" * 66)
    for s, r in res["series"].items():
        a, d = r["static_arb"], r["dislocation"]
        print(f"\n### {s}")
        print("  [static arb]  snapshots %d | tile evaluated %d | complete-partition %d"
              % (a["n_snapshots"], a["n_tile_evaluated"], a["n_tile_complete_partition"]))
        print("     fee-positive arbs:  buy-tile %d | sell-tile %d | pairwise %d"
              % (a["n_tile_buy_arb_fee_positive"], a["n_tile_sell_arb_fee_positive"],
                 a["n_pairwise_violations_fee_positive"]))
        print("     hypothetical credit captured: $%.4f" % a["hypothetical_credit_captured"])
        if a["n_tile_complete_partition"]:
            print("     complete-tile Σask (need <1−fee): median %.3f  min %.3f"
                  % (a["complete_tile_sum_ask_median"], a["complete_tile_sum_ask_min"]))
            print("     complete-tile Σbid (need >1+fee): median %.3f  max %.3f"
                  % (a["complete_tile_sum_bid_median"], a["complete_tile_sum_bid_max"]))
        else:
            print("     " + (a.get("note") or "no complete partition"))
        if d.get("n_snapshots_used"):
            print("  [dislocation] n=%d (%d horizons) | gap %.1f±%.1f bps | IC(gap_z→conv)=%.3f (%s)"
                  % (d["n_snapshots_used"], d.get("n_horizons_pooled", 0),
                     d["gap_bps_mean"], d["gap_bps_std"],
                     d["IC_spearman_gapz_vs_fwd_convergence"], d["IC_sign"]))
            print("     reversion-trade hit rate %.1f%%  | fwd=%d snaps, z-win=%d"
                  % (100 * d["reversion_trade_hit_rate"], d["fwd_snaps"], d["z_window"]))
        else:
            print("  [dislocation] " + d.get("note", "n/a"))
    print("=" * 66)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--series", default="KXBTC,KXETH")
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--fwd", type=int, default=3, help="forward snapshots for IC")
    ap.add_argument("--zwin", type=int, default=60, help="z-score window (snapshots)")
    ap.add_argument("--days", default="", help="comma-separated YYYY-MM-DD filter")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    series_list = [s.strip() for s in args.series.split(",") if s.strip()]
    days = [d.strip() for d in args.days.split(",") if d.strip()] or None
    res = run(series_list, fee_rate=args.fee_rate, fwd=args.fwd, zwin=args.zwin, days=days)
    _print_report(res)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"event_backtest_{stamp}.json"
    out_path.write_text(json.dumps(res, indent=2, default=str))
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
