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
from crypto_trading.crypto_common.loader import (_read_jsonl_days, load_funding,
                                                 load_perp_candles,
                                                 load_poll_market_stats)
from crypto_trading.crypto_strategies.event_perp.signals.dislocation import (
    DislocationParams, rolling_z, snapshot_factors)
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


def _dist_and_quotes(rec: dict):
    """(ImpliedDist|None, surv_quotes, bins, tail_lo, tail_hi) for one record."""
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
    return dist, surv, bins, tail_lo, tail_hi


def _implied_mean(rec: dict) -> float | None:
    dist, *_ = _dist_and_quotes(rec)
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


# ── sub-backtest 3: per-FACTOR IC (dislocation.py factors) ───────────────────

def _realized_vol_lookup(perp: str) -> "tuple[np.ndarray, np.ndarray] | None":
    """(ts, realized_vol) from 1h candles — rolling std of log returns (24h)."""
    try:
        c = load_perp_candles(perp, "1h")
    except Exception:
        return None
    px = c["price_close"].dropna()
    if len(px) < 30:
        return None
    rv = np.log(px).diff().rolling(24, min_periods=8).std()
    ts = px.index.view("int64") / 1e9
    return ts, rv.to_numpy()


def _funding_lookup(perp: str) -> "tuple[np.ndarray, np.ndarray] | None":
    try:
        f = load_funding(perp)
    except Exception:
        return None
    if not len(f):
        return None
    return f.index.view("int64") / 1e9, f["funding_rate"].to_numpy()


def _asof(ts_arr, val_arr, t, max_gap=None):
    """Most recent val at or before t (as-of backward)."""
    if ts_arr is None:
        return None
    j = int(np.searchsorted(ts_arr, t, side="right")) - 1
    if j < 0:
        return None
    if max_gap is not None and (t - ts_arr[j]) > max_gap:
        return None
    return float(val_arr[j])


def run_factor_ic(series: str, *, fwd: int = 3, zwin: int = 60,
                  days: list[str] | None = None,
                  params: DislocationParams | None = None) -> dict:
    """Build all four dislocation factors per snapshot, z-score within horizon,
    and report the IC of each factor + the composite against forward perp
    convergence. fair_value is the directional one; vol/skew are reported but are
    not price-directional (they drive vol/skew trades, not perp convergence)."""
    params = params or DislocationParams(zwin=zwin)
    perp = SERIES_TO_PERP.get(series)
    rv = _realized_vol_lookup(perp) if perp else None
    fund = _funding_lookup(perp) if perp else None

    rows = []
    for rec in read_snapshots(series, days=days):
        ts = rec.get("recv_ts")
        ps = _perp_spot_from_record(rec)
        if ts is None or ps is None:
            continue
        dist, surv, bins, tlo, thi = _dist_and_quotes(rec)
        if dist is None:
            continue
        realized = _asof(*rv, float(ts), max_gap=7200) if rv else None
        funding = _asof(*fund, float(ts), max_gap=86400) if fund else None
        fac = snapshot_factors(dist, perp_spot=ps, realized_vol=realized,
                               funding_rate=funding, surv_quotes=surv, bins=bins,
                               tail_lo=tlo, tail_hi=thi, params=params)
        rows.append({"recv_ts": float(ts), "perp_spot": ps,
                     "implied_mean": dist.mean, "close_time": rec.get("close_time"),
                     **fac})
    if len(rows) < fwd + 20:
        return {"series": series, "n": len(rows),
                "note": f"insufficient snapshots ({len(rows)}) for factor IC"}
    df = pd.DataFrame(rows).sort_values("recv_ts").reset_index(drop=True)

    # forward perp convergence toward implied (same target as run_dislocation_ic)
    per_factor = {}
    for factor in ("fair_value_gap", "vol_gap", "skew_gap", "arb_violation"):
        gz_all, fd_all = [], []
        for _, g in df.groupby("close_time", sort=False):
            g = g.sort_values("recv_ts")
            if len(g) < max(fwd + 20, zwin // 2):
                continue
            z = (g[factor] if factor == "arb_violation"          # magnitude, not z
                 else rolling_z(g[factor], zwin))
            d = g["perp_spot"] - g["implied_mean"]
            fwd_dd = d.shift(-fwd) - d
            pair = pd.DataFrame({"z": z, "fd": fwd_dd}).dropna()
            gz_all.append(pair["z"].to_numpy())
            fd_all.append(pair["fd"].to_numpy())
        gz = np.concatenate(gz_all) if gz_all else np.array([])
        fd = np.concatenate(fd_all) if fd_all else np.array([])
        ic = _spearman(gz, fd) if len(gz) >= 20 else float("nan")
        # a constant factor (e.g. arb_violation all-0 = no free money) has no
        # variance → undefined correlation; report None, not nan
        per_factor[factor] = {
            "n": int(len(gz)),
            "IC": round(ic, 4) if np.isfinite(ic) else None,
            "hit_rate": (round(float(np.mean(np.sign(gz) * fd > 0)), 4)
                         if len(gz) >= 20 and np.isfinite(ic) else None),
            "note": ("no variance (all ~0 — no fee-positive arb)"
                     if factor == "arb_violation" and not np.isfinite(ic) else None),
        }
    return {
        "series": series, "perp": perp, "n_snapshots": int(len(df)),
        "realized_vol_available": rv is not None, "funding_available": fund is not None,
        "fwd_snaps": fwd, "z_window": zwin,
        "per_factor_IC": per_factor,
        "note": ("fair_value_gap is the directional factor (should carry the IC); "
                 "vol_gap/skew_gap are NOT price-directional — they drive vol/skew "
                 "trades, so ~0 IC vs perp convergence is EXPECTED, not a failure. "
                 "arb_violation ~0 (no free money). ~7-day sample: preliminary."),
    }


# ── orchestration ────────────────────────────────────────────────────────────

def _n_strip_days(series_list: list[str]) -> int:
    import glob
    days = set()
    for s in series_list:
        for f in glob.glob(str(STRIPS_DIR / "prod" / s / "markets" / "*")):
            days.add(f.split("/")[-1][:10])
    return len(days)


def _series_days(series: str) -> list[str]:
    import glob
    days = {f.split("/")[-1][:10]
            for f in glob.glob(str(STRIPS_DIR / "prod" / series / "markets" / "*"))}
    return sorted(days)


def run_signal_ic_wf(series: str = "KXBTC", *, is_days: int = 3, oos_days: int = 2,
                     step: int = 1, fwd: int = 3, zwin: int = 60) -> dict:
    """SIGNAL-IC walk-forward (NOT P&L — the two-leg hedge is deferred, Plan 02 §10).

    Rolls the recorded strip days into IS/OOS windows and measures the
    fair-value-gap dislocation IC on each OOS window only (causal). Writes
    trading_signals/walk_forward/event_perp_oos_ic.csv (fold,oos_start,oos_end,
    oos_ic,n). validate_event_perp() consumes it. FAIL-safe: too few days →
    fewer/zero folds, and the gate then reports insufficient data.
    """
    all_days = _series_days(series)
    folds = []
    i = is_days
    fold_id = 0
    while i + oos_days <= len(all_days):
        oos = all_days[i:i + oos_days]
        r = run_dislocation_ic(series, fwd=fwd, zwin=zwin, days=oos)
        ic = r.get("IC_spearman_gapz_vs_fwd_convergence")
        folds.append({"fold": fold_id, "oos_start": oos[0], "oos_end": oos[-1],
                      "oos_ic": ic, "n": r.get("n", 0)})
        fold_id += 1
        i += step
    return {"series": series, "n_days": len(all_days), "n_folds": len(folds),
            "folds": folds, "is_days": is_days, "oos_days": oos_days}


def run(series_list: list[str], *, fee_rate: float = 0.07, fwd: int = 3,
        zwin: int = 60, days: list[str] | None = None) -> dict:
    ndays = _n_strip_days(series_list)
    out = {"PRELIMINARY": True,
           "caveat": f"~{ndays}-day self-recorded sample; mechanism check only, NOT "
                     "a validation gate (crypto-dev/02 §8/§10.2).",
           "fee_rate": fee_rate, "series": {}}
    for s in series_list:
        arb = run_static_arb(s, fee_rate=fee_rate, days=days)
        disloc = run_dislocation_ic(s, fwd=fwd, zwin=zwin, days=days)
        factors = run_factor_ic(s, fwd=fwd, zwin=zwin, days=days)
        out["series"][s] = {"static_arb": arb.summary(), "dislocation": disloc,
                            "factor_ic": factors}
    return out


def _print_report(res: dict) -> None:
    print("=" * 66)
    print("Plan 02 event×perp — PRELIMINARY backtest (self-recorded strips)")
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
        f = r.get("factor_ic", {})
        if f.get("per_factor_IC"):
            print("  [per-factor IC vs perp convergence] rvol=%s funding=%s"
                  % (f["realized_vol_available"], f["funding_available"]))
            for name, fi in f["per_factor_IC"].items():
                ic = fi["IC"]
                print("     %-14s IC=%s  hit=%s  (n=%d)"
                      % (name, f"{ic:+.3f}" if ic is not None else "n/a",
                         f"{100*fi['hit_rate']:.0f}%" if fi["hit_rate"] is not None else "n/a",
                         fi["n"]))
        elif f.get("note"):
            print("  [per-factor IC] " + f["note"])
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
