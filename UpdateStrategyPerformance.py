#!/usr/bin/env python3
"""
Update strategy_performance.json with REAL equity data for a given date range.

Usage:
    # Fixed weight mode (default): use latest available regime weight for all days
    python UpdateStrategyPerformance.py --start 2026-03-19 --end 2026-03-27

    # Daily weight mode: use each day's actual regime weight (shows rebalancing effects)
    python UpdateStrategyPerformance.py --start 2026-03-19 --end 2026-03-27 --daily-weights

    # Other options
    python UpdateStrategyPerformance.py --start 2026-03-19 --end 2026-03-27 --dry-run
    python UpdateStrategyPerformance.py --start 2026-03-19 --end 2026-03-27 --quiet

Weight modes:
    --fixed-weights (default):
        Use the most recent available regime weight (from --end date or nearest prior).
        All days in the range use the same weight. This shows pure strategy performance
        without regime rebalancing noise.

    --daily-weights:
        Use each day's actual regime weight from combined_signals.
        Shows true portfolio equity including regime rebalancing effects.
        Can cause large day-to-day jumps when weights shift significantly.

How it works:
    1. Reads inventory_history/ snapshots to get EOD positions for each trading day
    2. Fetches real close prices from MongoDB (stock_data collection)
    3. Computes equity = 500k + cumulative_realized + current_unrealized per strategy
    4. Applies regime weights to get real equity
    5. Replaces matching dates in strategy_performance.json (or appends new dates)

File convention in inventory_history/:
    Each DailySignal run produces 2 files per strategy:
        File 1 (as_of = prev trading day): yesterday's final holdings backup (pre-monitor)
        File 2 (as_of = today):            after monitor closes + new entries = EOD state
    There may be re-runs → we always take the LAST file for each as_of date.

Equity calculation:
    - Compare consecutive EOD snapshots to detect closes (realized PnL)
    - For open positions: unrealized PnL = (current_close - open_price) * shares
    - Same-day entries (open_date == date): PnL = $0 (system convention)
    - equity = sim_capital + cumulative_realized + current_unrealized
    - real_equity = regime_capital * (sim_equity / sim_capital)
"""

import argparse
import json
import glob
import os
from datetime import datetime, timedelta, date


SIM_CAPITAL = 500_000
PERF_JSON = "someo-park-investment-management/public/data/strategy_performance.json"


# ─── MongoDB price loading ───────────────────────────────────────────────────

def load_prices_mongo(tickers, start_date, end_date):
    """Fetch close prices from MongoDB stock_data collection.
    Returns dict: { 'TICKER': { 'YYYY-MM-DD': close_price, ... }, ... }
    """
    from db.connection import get_main_db
    import pandas as pd
    db = get_main_db()
    col = db["stock_data"]
    start_ms = int(pd.Timestamp(start_date).timestamp() * 1000)
    end_ms = int((pd.Timestamp(end_date) + pd.Timedelta(days=1)).timestamp() * 1000)

    prices = {}
    for sym in sorted(tickers):
        docs = list(col.find(
            {"symbol": sym, "t": {"$gte": start_ms, "$lte": end_ms}},
            {"c": 1, "t": 1, "_id": 0}
        ).sort("t", 1))
        if docs:
            dates = [pd.Timestamp(d["t"], unit="ms").normalize().strftime("%Y-%m-%d") for d in docs]
            closes = [d["c"] for d in docs]
            prices[sym] = dict(zip(dates, closes))
    return prices


# ─── Inventory snapshot loading ──────────────────────────────────────────────

def get_eod_snapshots(strategy):
    """For each as_of date, return the LAST snapshot (= true EOD state).
    Files are sorted by filename (= file timestamp), last one wins per as_of.
    Returns dict: { 'YYYY-MM-DD': inventory_data }
    """
    pattern = f"inventory_history/inventory_{strategy}_*.json"
    all_files = sorted(glob.glob(pattern))
    by_as_of = {}
    for f in all_files:
        with open(f) as fh:
            data = json.load(fh)
        as_of = data.get("as_of", "")
        if as_of:
            by_as_of[as_of] = data  # last file overwrites earlier ones
    return by_as_of


def get_open_positions(inventory_data):
    """Extract positions with direction != null."""
    return {pair: info for pair, info in inventory_data.get("pairs", {}).items()
            if info.get("direction") is not None}


def is_same_position(pos1, pos2):
    """Check if two entries represent the same trade (same open_date + shares + price)."""
    return (pos1.get("open_date") == pos2.get("open_date") and
            pos1.get("s1_shares") == pos2.get("s1_shares") and
            pos1.get("open_s1_price") == pos2.get("open_s1_price"))


# ─── PnL computation ────────────────────────────────────────────────────────

def compute_pnl_mongo(pair, pos, prices, date_str):
    """Compute PnL for a position using MongoDB close prices.
    Returns (pnl, source_str) or (None, None) if prices unavailable.
    """
    s1, s2 = pair.split("/")
    if (s1 in prices and date_str in prices[s1] and
            s2 in prices and date_str in prices[s2]):
        pnl = (pos["s1_shares"] * (prices[s1][date_str] - pos["open_s1_price"]) +
               pos["s2_shares"] * (prices[s2][date_str] - pos["open_s2_price"]))
        return pnl, "MongoDB"
    return None, None


def compute_pnl_monitor(pos, date_str):
    """Fallback: get PnL from monitor_log."""
    for entry in reversed(pos.get("monitor_log", [])):
        if entry.get("date") == date_str and entry.get("unrealized_pnl") is not None:
            return entry["unrealized_pnl"], "monitor_log"
    return None, None


def get_position_pnl(pair, pos, prices, date_str):
    """Get PnL for a position, with same-day entry handling.
    Returns (pnl, source_str).
    """
    # Same-day entries: system convention is $0 PnL
    if pos.get("open_date") == date_str:
        return 0.0, "same-day"
    # Try MongoDB first, then monitor_log fallback
    pnl, src = compute_pnl_mongo(pair, pos, prices, date_str)
    if pnl is not None:
        return pnl, src
    pnl, src = compute_pnl_monitor(pos, date_str)
    if pnl is not None:
        return pnl, src
    return 0.0, "NO DATA"


# ─── Equity reconstruction ──────────────────────────────────────────────────

def reconstruct_equity(strategy, eod_snapshots, prices, trading_dates, verbose=True):
    """Reconstruct daily sim equity by comparing consecutive EOD snapshots.

    Returns dict: { 'YYYY-MM-DD': sim_equity }
    """
    cumulative_realized = 0.0
    prev_positions = {}
    equity_series = {}

    if verbose:
        print(f"\n{'='*70}")
        print(f"Strategy: {strategy.upper()}")
        print(f"{'='*70}")

    for date_str in trading_dates:
        if verbose:
            print(f"\n--- {date_str} ---")

        # Get current EOD positions
        if date_str in eod_snapshots:
            current_positions = get_open_positions(eod_snapshots[date_str])
        else:
            current_positions = dict(prev_positions)
            if verbose:
                print("  (no snapshot, carry forward)")

        # Detect closes: in prev but gone/replaced in current
        for pair, old_pos in prev_positions.items():
            closed = (pair not in current_positions or
                      not is_same_position(old_pos, current_positions[pair]))
            if closed:
                rpnl, src = get_position_pnl(pair, old_pos, prices, date_str)
                cumulative_realized += rpnl
                if verbose:
                    print(f"  CLOSE  {pair}: ${rpnl:+,.2f} ({src}, opened {old_pos['open_date']})")

        # Compute unrealized for current open positions
        total_unrealized = 0.0
        for pair, pos in current_positions.items():
            pnl, src = get_position_pnl(pair, pos, prices, date_str)
            total_unrealized += pnl
            if verbose:
                print(f"  HOLD   {pair}: ${pnl:+,.2f} ({src})")

        equity = SIM_CAPITAL + cumulative_realized + total_unrealized
        equity_series[date_str] = equity
        if verbose:
            print(f"  >>> cum_realized=${cumulative_realized:+,.2f}  "
                  f"unrealized=${total_unrealized:+,.2f}  EQUITY=${equity:,.2f}")

        prev_positions = dict(current_positions)

    return equity_series


# ─── Regime weights ──────────────────────────────────────────────────────────

def load_regime_weights():
    """Load regime weights from all combined_signals files.
    Returns dict: { 'YYYY-MM-DD': { mrpt_capital, mtfs_capital, ... } }
    """
    weights = {}
    for f in sorted(glob.glob("trading_signals/combined_signals_*.json")):
        with open(f) as fh:
            cs = json.load(fh)
        sd = cs.get("signal_date")
        if sd:
            weights[sd] = {
                "mrpt_weight": cs["regime"]["mrpt_weight"],
                "mtfs_weight": cs["regime"]["mtfs_weight"],
                "mrpt_capital": cs["mrpt"]["capital"],
                "mtfs_capital": cs["mtfs"]["capital"],
            }
    return weights


def get_regime_for_date(regime_weights, date_str):
    """Get regime weights for a date, falling back to nearest prior date."""
    if date_str in regime_weights:
        return regime_weights[date_str]
    prev = [d for d in sorted(regime_weights.keys()) if d <= date_str]
    if prev:
        return regime_weights[prev[-1]]
    return {"mrpt_weight": 0.5, "mtfs_weight": 0.5,
            "mrpt_capital": 500000, "mtfs_capital": 500000}


# ─── Trading days ────────────────────────────────────────────────────────────

def get_trading_days(start_str, end_str):
    """Generate weekday dates between start and end (inclusive)."""
    start = datetime.strptime(start_str, "%Y-%m-%d").date()
    end = datetime.strptime(end_str, "%Y-%m-%d").date()
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return days


def get_prev_trading_day(date_str):
    """Get the previous weekday."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Update strategy_performance.json with real equity data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Weight mode examples:
  # Fixed weights (default) — smooth chart, pure strategy performance
  python UpdateStrategyPerformance.py --start 2026-03-19 --end 2026-03-27

  # Daily weights — shows regime rebalancing effects
  python UpdateStrategyPerformance.py --start 2026-03-19 --end 2026-03-27 --daily-weights
        """)
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--daily-weights", action="store_true",
                        help="Use each day's regime weight (default: fixed weight from --end date)")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing")
    parser.add_argument("--quiet", action="store_true", help="Suppress detailed output")
    args = parser.parse_args()

    verbose = not args.quiet
    target_dates = get_trading_days(args.start, args.end)
    print(f"Target dates ({len(target_dates)}): {target_dates[0]} to {target_dates[-1]}")

    weight_mode = "daily" if args.daily_weights else "fixed"
    print(f"Weight mode: {weight_mode}")

    # We need the day BEFORE start to establish baseline positions
    baseline_date = get_prev_trading_day(args.start)
    all_dates = [baseline_date] + target_dates
    print(f"Baseline date: {baseline_date}")

    # Load EOD snapshots
    mrpt_eod = get_eod_snapshots("mrpt")
    mtfs_eod = get_eod_snapshots("mtfs")

    if verbose:
        for strat, eod in [("MRPT", mrpt_eod), ("MTFS", mtfs_eod)]:
            relevant = {d: get_open_positions(eod[d]) for d in sorted(eod) if d in all_dates}
            print(f"\n{strat} EOD positions:")
            for d in sorted(relevant):
                print(f"  {d}: {list(relevant[d].keys())}")

    # Collect all tickers
    all_tickers = set()
    for eod in [mrpt_eod, mtfs_eod]:
        for d, data in eod.items():
            if d not in all_dates:
                continue
            for pair, info in data.get("pairs", {}).items():
                if info.get("direction") is not None:
                    t1, t2 = pair.split("/")
                    all_tickers.add(t1)
                    all_tickers.add(t2)

    # Load prices from MongoDB (wider range for positions opened before start)
    price_start = (datetime.strptime(baseline_date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    price_end = args.end
    print(f"\nLoading {len(all_tickers)} tickers from MongoDB ({price_start} to {price_end})...")
    prices = load_prices_mongo(all_tickers, price_start, price_end)
    print(f"Loaded prices for {len(prices)} tickers")

    # Reconstruct sim equity
    mrpt_equity = reconstruct_equity("mrpt", mrpt_eod, prices, all_dates, verbose)
    mtfs_equity = reconstruct_equity("mtfs", mtfs_eod, prices, all_dates, verbose)

    # Remove baseline
    mrpt_equity.pop(baseline_date, None)
    mtfs_equity.pop(baseline_date, None)

    # Load regime weights and determine fixed weight if needed
    regime_weights = load_regime_weights()

    if weight_mode == "fixed":
        # Use the latest available weight (from --end or nearest prior)
        fixed_rw = get_regime_for_date(regime_weights, args.end)
        print(f"\nFixed weight: MRPT={fixed_rw['mrpt_weight']:.3f} ({fixed_rw['mrpt_capital']:,.0f}), "
              f"MTFS={fixed_rw['mtfs_weight']:.3f} ({fixed_rw['mtfs_capital']:,.0f})")

    # Build real equity records
    real_records = []
    for date_str in target_dates:
        mrpt_sim = mrpt_equity.get(date_str, SIM_CAPITAL)
        mtfs_sim = mtfs_equity.get(date_str, SIM_CAPITAL)

        if weight_mode == "fixed":
            rw = fixed_rw
        else:
            rw = get_regime_for_date(regime_weights, date_str)

        mrpt_real = rw["mrpt_capital"] * (mrpt_sim / SIM_CAPITAL)
        mtfs_real = rw["mtfs_capital"] * (mtfs_sim / SIM_CAPITAL)

        real_records.append({
            "date": date_str,
            "mrpt_sim": round(mrpt_sim, 2),
            "mtfs_sim": round(mtfs_sim, 2),
            "mrpt_real": round(mrpt_real, 2),
            "mtfs_real": round(mtfs_real, 2),
            "combined": round(mrpt_real + mtfs_real, 2),
            "mrpt_weight": rw["mrpt_weight"],
            "mtfs_weight": rw["mtfs_weight"],
        })

    # Print summary
    print(f"\n{'='*90}")
    print(f"{'Date':<12} {'MRPT Sim':>12} {'MTFS Sim':>12} {'MRPT Real':>12} {'MTFS Real':>12} {'Combined':>12}")
    print("-" * 90)
    for r in real_records:
        print(f"{r['date']:<12} {r['mrpt_sim']:>12,.2f} {r['mtfs_sim']:>12,.2f} "
              f"{r['mrpt_real']:>12,.2f} {r['mtfs_real']:>12,.2f} {r['combined']:>12,.2f}")

    # Update strategy_performance.json
    if not os.path.exists(PERF_JSON):
        print(f"\nERROR: {PERF_JSON} not found")
        return

    with open(PERF_JSON) as f:
        perf_data = json.load(f)

    # Replace/insert real data
    existing_dates = {r["date"]: i for i, r in enumerate(perf_data)}
    replaced = 0
    appended = 0

    for rec in real_records:
        d = rec["date"]
        entry = {
            "date": d,
            "mrpt_equity": rec["mrpt_real"],
            "mtfs_equity": rec["mtfs_real"],
            "combined_equity": rec["combined"],
            "mrpt_pnl": 0.0, "mtfs_pnl": 0.0, "combined_pnl": 0.0,
            "mrpt_dd": 0.0, "mtfs_dd": 0.0, "combined_dd": 0.0,
        }

        if d in existing_dates:
            perf_data[existing_dates[d]] = entry
            replaced += 1
        else:
            perf_data.append(entry)
            appended += 1

    # Sort by date
    perf_data.sort(key=lambda r: r["date"])

    # Recalculate PnL and drawdown for the ENTIRE series
    mrpt_peak = perf_data[0]["mrpt_equity"]
    mtfs_peak = perf_data[0]["mtfs_equity"]
    combined_peak = perf_data[0]["combined_equity"]

    for i, row in enumerate(perf_data):
        if i > 0:
            row["mrpt_pnl"] = round(row["mrpt_equity"] - perf_data[i-1]["mrpt_equity"], 2)
            row["mtfs_pnl"] = round(row["mtfs_equity"] - perf_data[i-1]["mtfs_equity"], 2)
            row["combined_pnl"] = round(row["combined_equity"] - perf_data[i-1]["combined_equity"], 2)
        else:
            row["mrpt_pnl"] = 0.0
            row["mtfs_pnl"] = 0.0
            row["combined_pnl"] = 0.0

        if row["mrpt_equity"] > mrpt_peak:
            mrpt_peak = row["mrpt_equity"]
        if row["mtfs_equity"] > mtfs_peak:
            mtfs_peak = row["mtfs_equity"]
        if row["combined_equity"] > combined_peak:
            combined_peak = row["combined_equity"]

        row["mrpt_dd"] = round((row["mrpt_equity"] - mrpt_peak) / mrpt_peak * 100, 2)
        row["mtfs_dd"] = round((row["mtfs_equity"] - mtfs_peak) / mtfs_peak * 100, 2)
        row["combined_dd"] = round((row["combined_equity"] - combined_peak) / combined_peak * 100, 2)

    if args.dry_run:
        print(f"\n[DRY RUN] Would replace {replaced}, append {appended} records in {PERF_JSON}")
    else:
        with open(PERF_JSON, "w") as f:
            json.dump(perf_data, f, indent=2)
        print(f"\nUpdated {PERF_JSON}: replaced {replaced}, appended {appended} records")
        print(f"Total records: {len(perf_data)}")


if __name__ == "__main__":
    main()
