"""Plan 09 GATE 3 — fill-aware economics of the gate-2 winner (linear basis_z).

Signal (fixed by gates 1–2, no fitting here): on the 5-min grid, when
|basis_z| ≥ 1.0 go −sign(basis_z) (fade the mark-vs-composite dislocation).
One position per market (non-overlapping); hold = the label horizon.

Execution model (mirrors liq_reversion/fill_aware conventions):
  * ENTRY — post_only at the touch on the passive side (long → join the bid,
    short → join the ask); fill checked queue-aware vs the REAL trade tape
    (queue_ahead = queue_frac × median trade size, 5-min lookback); unfilled
    within 5min → signal missed (honest fill-rate cost).
  * EXIT — at entry + horizon: passive at the opposite touch for 2min, else
    cross the spread (taker). Maker/taker fees per fill role ("projected"
    scenario); funding applied when the hold crosses a settlement stamp.

Trial accounting: gate 1 = 60 cells, gate 2 = 12 configs, gate 3 = 4 configs
(2 markets × 2 horizons) → n_trials = 76 for the deflated-Sharpe gate.

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.ml_directional.backtest
        [--queue-frac 1.0] [--fees projected]
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
from crypto_trading.crypto_common.costs import fee_dollars, funding_payment
from crypto_trading.crypto_common.loader import (load_funding, load_poll_market_stats,
                                                 load_poll_trades)
from crypto_trading.crypto_common.trade_stats import trade_significance_report
from crypto_trading.crypto_strategies.ml_directional.features import (HORIZONS,
                                                                      cached_feature_frame)

logger = logging.getLogger(__name__)

BASELINE_Z = 1.0                       # fixed by gate 2 — not swept here
N_TRIALS = 60 + 12 + 4                 # honest count across all three gates


def run_market(ticker: str, horizon: str, *, queue_frac: float = 1.0,
               fee_scenario: str = "projected", contracts: int = 10) -> dict:
    f = cached_feature_frame(ticker)
    stats = load_poll_market_stats(ticker)
    trades = load_poll_trades(ticker).sort_index()
    fund = load_funding(ticker).sort_index()
    touch = stats[["bid", "ask"]].dropna()
    hold = pd.Timedelta(minutes=5 * HORIZONS[horizon])

    def touch_at(ts, col):
        try:
            seg = touch.loc[:ts, col]
            if seg.empty or ts - seg.index[-1] > pd.Timedelta("10min"):
                return None
            v = seg.iloc[-1]
            return float(v) if pd.notna(v) else None
        except KeyError:
            return None

    def resting_size(ts):
        w = trades.loc[ts - pd.Timedelta(minutes=5):ts, "count"]
        return float(w.median()) if len(w) else 1.0

    bz = f["basis_z"]
    sig_times = bz.index[bz.abs() >= BASELINE_Z]
    rows = []
    attempted = 0
    busy_until = pd.Timestamp.min.tz_localize("UTC")
    for ts in sig_times:
        if ts < busy_until:
            continue                                   # one position at a time
        direction = int(-np.sign(bz[ts]))
        entry_limit = touch_at(ts, "bid" if direction > 0 else "ask")
        if entry_limit is None:
            continue
        attempted += 1
        fr = simulate_maker_fill(entry_limit, "bid" if direction > 0 else "ask", ts,
                                 trades, timeout=pd.Timedelta("5min"),
                                 queue_ahead=queue_frac * resting_size(ts))
        if not fr.filled:
            continue
        entry_px, entry_ts = fr.fill_price, fr.fill_ts
        exit_at = entry_ts + hold

        exit_limit = touch_at(exit_at, "ask" if direction > 0 else "bid")
        if exit_limit is None:
            continue
        efr = simulate_maker_fill(exit_limit, "ask" if direction > 0 else "bid",
                                  exit_at, trades, timeout=pd.Timedelta("2min"),
                                  queue_ahead=queue_frac * resting_size(exit_at))
        exit_role = "maker"
        if not efr.filled:
            efr = simulate_taker_fill(exit_limit, "ask" if direction > 0 else "bid", exit_at)
            exit_role = "taker"
        exit_px, exit_ts = efr.fill_price, efr.fill_ts
        busy_until = exit_ts + pd.Timedelta("5min")    # cooldown = one grid step

        gross = direction * (exit_px - entry_px) * contracts
        fee = (fee_dollars(entry_px * contracts, role="maker", scenario=fee_scenario, ticker=ticker)
               + fee_dollars(exit_px * contracts, role=exit_role, scenario=fee_scenario, ticker=ticker))
        f_seg = fund.loc[entry_ts:exit_ts]
        funding = sum(funding_payment(direction * contracts, entry_px, r)
                      for r in f_seg["funding_rate"]) if len(f_seg) else 0.0
        net = gross - fee + funding
        rows.append({"entry_ts": entry_ts, "exit_ts": exit_ts, "direction": direction,
                     "basis_z": float(bz[ts]), "gross": gross, "fee": fee,
                     "funding": funding, "net": net, "exit_role": exit_role,
                     "net_bps": 1e4 * net / (entry_px * contracts)})

    tp = pd.DataFrame(rows)
    summary = {"ticker": ticker, "horizon": horizon, "signals": int(len(sig_times)),
               "attempted": attempted, "round_trips": len(tp),
               "fill_rate": len(tp) / attempted if attempted else 0.0,
               "net_pnl": float(tp.net.sum()) if len(tp) else 0.0,
               "mean_net_bps": float(tp.net_bps.mean()) if len(tp) else 0.0,
               "hit_rate": float((tp.net > 0).mean()) if len(tp) else 0.0,
               "maker_exit_frac": float((tp.exit_role == "maker").mean()) if len(tp) else 0.0,
               "queue_frac": queue_frac, "fee_scenario": fee_scenario,
               "contracts": contracts}
    sig = None
    if len(tp) >= 20:
        sig = trade_significance_report(tp["net_bps"], k=5, embargo=3, n_trials=N_TRIALS)
        summary.update({"nw_t": sig["t_nw"], "dsr": sig["dsr"],
                        "purged_frac_positive": sig["purged_cv"]["frac_positive"],
                        "significant": sig["significant"]})
    else:
        summary["significant"] = None
    return {"summary": summary, "trades": tp, "significance": sig}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queue-frac", type=float, default=1.0)
    ap.add_argument("--fees", default="projected", choices=["zero", "projected"])
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    out, all_bps = {"configs": [], "n_trials": N_TRIALS}, []
    art = SIGNALS_DIR / "research"
    art.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    for ticker in ("KXBTCPERP", "KXETHPERP"):
        for horizon in HORIZONS:
            r = run_market(ticker, horizon, queue_frac=args.queue_frac,
                           fee_scenario=args.fees)
            out["configs"].append(r["summary"])
            if len(r["trades"]):
                r["trades"].to_csv(art / f"ml_gate3_trades_{ticker}_{horizon}_{stamp}.csv",
                                   index=False)
                if horizon == "15m":
                    all_bps.append(r["trades"][["entry_ts", "net_bps"]])
            logger.info("gate3 %s %s done", ticker, horizon)

    # pooled 15m verdict (primary horizon per gates 1–2)
    if all_bps:
        pooled = (pd.concat(all_bps).sort_values("entry_ts")["net_bps"]
                  .reset_index(drop=True))
        out["pooled_15m"] = trade_significance_report(pooled, k=5, embargo=3,
                                                      n_trials=N_TRIALS)
    (art / f"ml_gate3_{stamp}.json").write_text(json.dumps(out, indent=1, default=str))

    print("=" * 78)
    print(f"PLAN 09 GATE 3 — fill-aware economics, linear basis_z signal "
          f"(n_trials={N_TRIALS}, fees={args.fees}, queue_frac={args.queue_frac})")
    print("=" * 78)
    print(pd.DataFrame(out["configs"]).to_string(index=False))
    if "pooled_15m" in out:
        p = out["pooled_15m"]
        print(f"\nPOOLED 15m: n={p['n']} mean={p['mean']:.2f}bps NW-t={p['t_nw']:.2f} "
              f"DSR={p['dsr']:.3f} frac_pos={p['purged_cv']['frac_positive']:.2f} "
              f"significant={p['significant']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
