"""Fill-aware Plan 04 backtest on the RECORDED tape (mirrors basis_meanrev/fill_aware).

Plan 04 provides PASSIVE liquidity into liquidation cascades — the same
adverse-selection trap as Plan 01: you post to fade the overshoot and get filled
precisely while the cascade is STILL running against you. The candle/optimistic
backtest can't see that. This checks it against the dense recorded trade tape.

Flow:
  1. detect cascades on the recorded Kalshi tape (liquidation.py: trade-burst +
     OI-drop + overshoot vs index), keep FADING + above-threshold events (Plan 04 §6).
  2. PASSIVE entry: post_only at the touch on the reversion side; fill checked
     against the real trade tape (queue-aware) → captures fill-rate + adverse
     selection. Unfilled within entry_timeout → cascade missed.
  3. EXIT: when the overshoot retraces tp_fraction toward the index / time-stop /
     hard-abort (overshoot extends). Passive-then-cross (taker) to guarantee exit.
  4. Net P&L per cascade (maker fee on passive, taker on crossed) → trade_stats.

Honest: ~20d recorded Kalshi tape, few clean cascades (calm venue; 07-24→26 more
volatile). If the sample is thin the report says so — no manufactured significance.

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.liq_reversion.fill_aware
        [--ticker KXBTCPERP] [--asset BTC] [--queue-frac 1.0] [--overshoot-bps 15]
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.backtest.fill_model import (simulate_maker_fill,
                                                              simulate_taker_fill)
from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.costs import fee_dollars
from crypto_trading.crypto_common.loader import (load_index_composite, load_index_live,
                                                 load_poll_market_stats, load_poll_trades)
from crypto_trading.crypto_common.trade_stats import trade_significance_report
from crypto_trading.crypto_strategies.liq_reversion.signals.liquidation import (
    DetectorParams, build_features, detect_cascades)

logger = logging.getLogger(__name__)
STRATEGY = "liq_reversion"


def run_fill_aware(*, ticker: str = "KXBTCPERP", asset: str = "BTC",
                   det: DetectorParams = DetectorParams(), queue_frac: float = 1.0,
                   entry_timeout_min: int = 5, exit_timeout_min: int = 15,
                   tp_fraction: float = 0.5, hard_abort_mult: float = 2.0,
                   fee_scenario: str = "projected", contracts: int = 10,
                   stats: pd.DataFrame | None = None, trades: pd.DataFrame | None = None,
                   index_series: pd.Series | None = None,
                   entry_style: str = "maker", event_cooldown_min: float = 0.0) -> dict:
    """``stats``/``trades``/``index_series`` are injectable so the widened
    full-universe sweep loads each tape once and supplies non-composite anchors
    (rolling-median-of-mark for alts). Defaults preserve the original behavior:
    load per ticker, composite anchor, maker entry, no cooldown.

    ``entry_style``: "maker" (post_only at touch, queue-aware fill vs tape) or
    "taker" (cross the spread immediately at the opposite touch, taker fee).
    ``event_cooldown_min``: skip events within this many minutes of the previous
    ENTERED event (dedups clustered grid-bars of the same cascade)."""
    if stats is None:
        stats = load_poll_market_stats(ticker)
    if stats.empty:
        raise RuntimeError("no recorded market-stats — run the poller first")
    if trades is None:
        trades = load_poll_trades(ticker).sort_index()
    if trades.empty:
        raise RuntimeError("no recorded trades — run the poller first")
    if index_series is None:
        # index anchor = the CLEAN 1-min spot composite (vw_close), not the noisier
        # 5s live feed: the live index's tracking noise vs the recorded mark hid
        # real cascades (3 vs 78 candidate bars). Composite = mechanism-test anchor.
        comp = load_index_composite(asset)
        if comp.empty:
            raise RuntimeError("no spot composite — run refdata.index backfill first")
        index_series = comp["vw_close"]

    feat = build_features(trades, stats, index_series, det)
    events = detect_cascades(feat, det)
    n_all = len(events)
    events = events[events["fading"]] if n_all else events        # §6: only fading
    n_fading = len(events)

    entry_to = pd.Timedelta(minutes=entry_timeout_min)
    exit_to = pd.Timedelta(minutes=exit_timeout_min)
    # touch (real bid/ask) resampled to the detector grid.
    # PIT: right/right labels — bar T holds (T-grid, T], knowable at label time.
    touch = (stats[["bid", "ask"]].dropna()
             .resample(f"{det.grid_sec}s", label="right", closed="right")
             .last().ffill(limit=6))
    grid_mid = feat["mid"]          # underlying mark on the grid (for reversion tracking)
    grid_index = feat["index"]
    grid_ts = list(feat.index)

    def touch_at(ts, col):
        try:
            v = touch.loc[:ts, col].iloc[-1]
            return float(v) if pd.notna(v) else None
        except (KeyError, IndexError):
            return None

    def resting_size(ts):
        w = trades.loc[ts - pd.Timedelta(minutes=5):ts, "count"]
        return float(w.median()) if len(w) else 1.0

    rows = []
    fills_attempted = fills_made = 0
    last_entry_ts = None
    cooldown = pd.Timedelta(minutes=event_cooldown_min)
    for ev_ts, ev in events.iterrows():
        if (last_entry_ts is not None and event_cooldown_min > 0
                and ev_ts - last_entry_ts < cooldown):
            continue                                  # same cascade cluster — dedup
        fills_attempted += 1
        direction = int(ev["direction"])              # +1 fade UP (buy), −1 fade DOWN (sell)
        entry_side = "bid" if direction > 0 else "ask"
        if entry_style == "taker":
            # cross the spread now: buy at the ask / sell at the bid
            cross_px = touch_at(ev_ts, "ask" if direction > 0 else "bid")
            if cross_px is None:
                continue
            fr = simulate_taker_fill(cross_px, entry_side, ev_ts)
        else:
            limit = touch_at(ev_ts, "bid" if direction > 0 else "ask")
            if limit is None:
                continue
            q = queue_frac * resting_size(ev_ts)
            fr = simulate_maker_fill(limit, entry_side, ev_ts, trades,
                                     timeout=entry_to, queue_ahead=q)
        if not fr.filled:
            continue                                  # missed — honest fill-rate cost
        fills_made += 1
        entry_px, entry_ts = fr.fill_price, fr.fill_ts
        last_entry_ts = ev_ts
        entry_overshoot = float(ev["overshoot_bps"])
        abort_bps = abs(entry_overshoot) * hard_abort_mult

        # walk the grid forward from the fill to find the exit trigger
        fwd = [(t, grid_mid[t], grid_index[t]) for t in grid_ts
               if entry_ts < t <= entry_ts + exit_to]
        exit_ts = None
        for t, mid, ix in fwd:
            os_now = 1e4 * (mid - ix) / ix
            # reverted: overshoot retraced tp_fraction toward 0
            reverted = abs(os_now) <= abs(entry_overshoot) * (1 - tp_fraction)
            aborted = abs(os_now) >= abort_bps
            if reverted or aborted:
                exit_ts = t
                break
        if exit_ts is None:
            exit_ts = fwd[-1][0] if fwd else entry_ts + exit_to      # time-stop

        # EXIT passive-then-cross on the opposite touch
        exit_side = "ask" if direction > 0 else "bid"
        exit_limit = touch_at(exit_ts, "ask" if direction > 0 else "bid")
        if exit_limit is None:
            continue
        efr = simulate_maker_fill(exit_limit, exit_side, exit_ts, trades,
                                  timeout=pd.Timedelta(minutes=2),
                                  queue_ahead=queue_frac * resting_size(exit_ts))
        exit_role = "maker"
        if not efr.filled:
            # cross happens AFTER the passive window, at the ADVERSE touch: a long
            # exits by hitting the bid (old code priced the decision-time ask —
            # stale AND wrong side, doubly optimistic on the stop-out branch).
            cross_ts = exit_ts + pd.Timedelta(minutes=2)
            cross_px = touch_at(cross_ts, "bid" if direction > 0 else "ask")
            if cross_px is None:
                cross_px = exit_limit                     # last resort: stale quote
            efr = simulate_taker_fill(cross_px, exit_side, cross_ts)
            exit_role = "taker"
        exit_px = efr.fill_price

        gross = direction * (exit_px - entry_px) * contracts
        entry_role = "taker" if entry_style == "taker" else "maker"
        fee = (fee_dollars(entry_px * contracts, role=entry_role, scenario=fee_scenario, ticker=ticker)
               + fee_dollars(exit_px * contracts, role=exit_role, scenario=fee_scenario, ticker=ticker))
        rows.append({"entry_ts": entry_ts, "exit_ts": exit_ts, "direction": direction,
                     "entry_overshoot_bps": entry_overshoot, "gross": gross,
                     "fee": fee, "net": gross - fee, "exit_role": exit_role})

    tp = pd.DataFrame(rows)
    fill_rate = fills_made / fills_attempted if fills_attempted else 0.0
    summary = {
        "ticker": ticker, "span": [str(feat.index.min()), str(feat.index.max())] if len(feat) else [],
        "cascades_detected": n_all, "cascades_fading": n_fading,
        "entries_filled": fills_made, "fill_rate": fill_rate, "round_trips": len(tp),
        "net_pnl_per_10c": float(tp.net.sum()) if len(tp) else 0.0,
        "mean_net_per_trade": float(tp.net.mean()) if len(tp) else 0.0,
        "hit_rate": float((tp.net > 0).mean()) if len(tp) else 0.0,
        "maker_exit_frac": float((tp.exit_role == "maker").mean()) if len(tp) else 0.0,
        "queue_frac": queue_frac, "overshoot_bps": det.overshoot_entry_bps,
        "fee_scenario": fee_scenario,
        "note": "fill-aware on recorded tape — passive fade into cascades, real fills",
    }
    sig = None
    if len(tp) >= 5:
        sig = trade_significance_report(tp["net"], k=min(5, len(tp)))
        summary.update({"nw_t": sig["t_nw"], "purged_frac_positive": sig["purged_cv"]["frac_positive"],
                        "significant": sig["significant"]})
    else:
        summary["significant"] = None                 # too few cascades to test
        summary["note"] += " — INSUFFICIENT sample for significance (<5 trades)"
    return {"summary": summary, "trade_pnl": tp, "significance": sig}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", default="KXBTCPERP")
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--queue-frac", type=float, default=1.0)
    ap.add_argument("--overshoot-bps", type=float, default=15.0)
    ap.add_argument("--fees", default="projected", choices=["zero", "projected"])
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    det = DetectorParams(overshoot_entry_bps=args.overshoot_bps)
    r = run_fill_aware(ticker=args.ticker, asset=args.asset, det=det,
                       queue_frac=args.queue_frac, fee_scenario=args.fees)
    print(json.dumps(r["summary"], indent=2, default=str))
    out = SIGNALS_DIR / STRATEGY / "fill_aware"
    out.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    if len(r["trade_pnl"]):
        r["trade_pnl"].to_csv(out / f"trades_{stamp}.csv", index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
