"""Fill-aware Plan 01 backtest on the RECORDED tape (not synthetic candle depth).

Answers the one real question the candle-tape backtest can't: do the passive
(maker) entries the strategy relies on ACTUALLY FILL against real order flow, and
does the edge survive the resulting fill-rate + adverse selection?

Data (all self-recorded, ~20 days):
  * mark touch  ← load_poll_market_stats (real tight bid/ask per ~10s)
  * index anchor← load_index_live (5s spot composite)
  * fills       ← load_poll_trades (dense ~100k trades/day) via fill_model

Flow per signal (same b_t / z / OU logic as the candle backtest):
  ENTER passive: post_only at the near touch on the stretched side; fill checked
    against the trade tape (queue-aware). If it doesn't fill within entry_timeout
    → the trade is MISSED (this is the honest fill-rate cost).
  EXIT: when z reverts (or time-stop), post_only exit at the touch; if it hasn't
    filled by exit_timeout, CROSS (taker) to guarantee the exit (pay taker fee).
Net P&L per round-trip, maker fee on passive fills, taker on crossed exits.

Outputs a per-trade P&L series (fed to trade_stats for significance) + fill-rate.
CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.basis_meanrev.fill_aware
        [--ticker KXBTCPERP] [--asset BTC] [--queue-frac 1.0] [--entry-timeout-min 10]
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
from crypto_trading.crypto_strategies.basis_meanrev.signals.basis import (BasisParams,
                                                                          compute_signal_frame)

logger = logging.getLogger(__name__)
STRATEGY = "basis_meanrev"


def build_recorded_frame(ticker: str, asset: str,
                         index_source: str = "composite") -> tuple[pd.DataFrame, float]:
    """1-min basis frame from the RECORDED tape (mark touch + index anchor).

    ``index_source``: "composite" (clean 1-min VWAP spot composite — DEFAULT, the
    right anchor) or "live" (the 5s recorded feed, noisier — its tracking noise
    deflates/distorts the basis and can fabricate signal; kept for comparison).
    """
    stats = load_poll_market_stats(ticker)
    if stats.empty:
        raise RuntimeError("no recorded market-stats — run the poller first")
    csize = float(stats.contract_size.dropna().median())

    # PIT: right/right labels everywhere — bar T = (T-1min, T], knowable at T.
    # (Default label-left stamps [T, T+1min) at T: the mark side would carry up
    # to 59s of lookahead, and after the loader's composite end-label fix the two
    # sides would sit 1 minute apart in opposite directions.)
    m = stats[["bid", "ask"]].dropna().resample("1min", label="right", closed="right").last().dropna()
    m["mark_mid_contract"] = (m.bid + m.ask) / 2
    m["mark_mid_underlying"] = m.mark_mid_contract / csize
    if index_source == "composite":
        ix = (load_index_composite(asset)["vw_close"]
              .resample("1min", label="right", closed="right").last().rename("index_proxy"))
    else:
        idx = load_index_live(asset)
        if idx.empty:
            raise RuntimeError("no recorded live index — run idxrecord first")
        ix = idx["index"].resample("1min", label="right", closed="right").last().rename("index_proxy")
    frame = m.join(ix, how="inner").dropna()
    frame["index_venues"] = 3
    frame["b_t"] = (frame.mark_mid_underlying - frame.index_proxy) / frame.index_proxy
    frame["b_t_bps"] = 1e4 * frame.b_t
    return frame, csize


def run_fill_aware(*, ticker: str = "KXBTCPERP", asset: str = "BTC",
                   params: BasisParams = BasisParams(), queue_frac: float = 1.0,
                   entry_timeout_min: int = 10, exit_timeout_min: int = 30,
                   fee_scenario: str = "projected", contracts: int = 10,
                   index_source: str = "composite") -> dict:
    frame, csize = build_recorded_frame(ticker, asset, index_source=index_source)
    sig = compute_signal_frame(frame, params)
    stats = load_poll_market_stats(ticker)
    trades = load_poll_trades(ticker).sort_index()
    if trades.empty:
        raise RuntimeError("no recorded trades — run the poller first")

    entry_to = pd.Timedelta(minutes=entry_timeout_min)
    exit_to = pd.Timedelta(minutes=exit_timeout_min)
    # touch series (real bid/ask) aligned to signal minutes (PIT: right/right)
    touch = (stats[["bid", "ask"]].dropna()
             .resample("1min", label="right", closed="right").last().ffill(limit=3))

    def touch_at(ts, col):
        try:
            v = touch.loc[:ts, col].iloc[-1]
            return float(v) if pd.notna(v) else None
        except (KeyError, IndexError):
            return None

    def resting_size(ts):
        # queue-ahead proxy: median recent traded size as a stand-in for depth
        w = trades.loc[ts - pd.Timedelta(minutes=5):ts, "count"]
        return float(w.median()) if len(w) else 1.0

    trade_pnls = []
    fills_attempted = fills_made = 0
    in_pos = False
    entry_px = entry_ts = pos_sign = None
    idx_list = list(sig.index)
    desired = sig.desired.to_numpy()

    for i, ts in enumerate(idx_list):
        want = int(desired[i])
        if not in_pos and want != 0:
            fills_attempted += 1
            side = "bid" if want > 0 else "ask"          # long buys at bid, short sells at ask
            limit = touch_at(ts, "bid" if want > 0 else "ask")
            if limit is None:
                continue
            q = queue_frac * resting_size(ts)
            fr = simulate_maker_fill(limit, side, ts, trades, timeout=entry_to, queue_ahead=q)
            if fr.filled:
                fills_made += 1
                in_pos, entry_px, entry_ts, pos_sign = True, fr.fill_price, fr.fill_ts, want
            # not filled → trade missed (honest fill-rate cost)
        elif in_pos:
            timed_out = (ts - entry_ts) >= exit_to
            reverted = want == 0 or np.sign(want) != np.sign(pos_sign)
            if reverted or timed_out:
                # try passive exit at the touch; if it wouldn't fill, cross (taker)
                exit_side = "ask" if pos_sign > 0 else "bid"   # close long by selling at ask
                exit_limit = touch_at(ts, "ask" if pos_sign > 0 else "bid")
                if exit_limit is None:
                    continue
                fr = simulate_maker_fill(exit_limit, exit_side, ts, trades,
                                         timeout=pd.Timedelta(minutes=2),
                                         queue_ahead=queue_frac * resting_size(ts))
                exit_role = "maker"
                if not fr.filled:
                    # guaranteed exit — cross AFTER the passive window at the
                    # ADVERSE touch (long exits hit the bid), not the stale
                    # decision-time favorable quote.
                    cross_ts = ts + pd.Timedelta(minutes=2)
                    cross_px = touch_at(cross_ts, "bid" if pos_sign > 0 else "ask")
                    if cross_px is None:
                        cross_px = exit_limit
                    fr = simulate_taker_fill(cross_px, exit_side, cross_ts)
                    exit_role = "taker"
                exit_px = fr.fill_price
                gross = pos_sign * (exit_px - entry_px) * contracts
                notional_in = entry_px * contracts
                notional_out = exit_px * contracts
                fee = (fee_dollars(notional_in, role="maker", scenario=fee_scenario, ticker=ticker)
                       + fee_dollars(notional_out, role=exit_role, scenario=fee_scenario, ticker=ticker))
                trade_pnls.append({"entry_ts": entry_ts, "exit_ts": ts, "sign": pos_sign,
                                   "gross": gross, "fee": fee, "net": gross - fee,
                                   "exit_role": exit_role})
                in_pos = False

    tp = pd.DataFrame(trade_pnls)
    fill_rate = fills_made / fills_attempted if fills_attempted else 0.0
    summary = {
        "ticker": ticker, "span": [str(sig.index.min()), str(sig.index.max())],
        "signal_minutes": len(sig), "entries_attempted": fills_attempted,
        "entries_filled": fills_made, "fill_rate": fill_rate,
        "round_trips": len(tp),
        "net_pnl_per_10c": float(tp.net.sum()) if len(tp) else 0.0,
        "mean_net_per_trade": float(tp.net.mean()) if len(tp) else 0.0,
        "hit_rate": float((tp.net > 0).mean()) if len(tp) else 0.0,
        "maker_exit_frac": float((tp.exit_role == "maker").mean()) if len(tp) else 0.0,
        "queue_frac": queue_frac, "fee_scenario": fee_scenario,
        "note": "fill-aware on recorded tape — realistic maker fills + adverse selection",
    }
    return {"summary": summary, "trade_pnl": tp}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", default="KXBTCPERP")
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--queue-frac", type=float, default=1.0,
                    help="0=optimistic (fill on first touch); 1=join back of queue")
    ap.add_argument("--entry-timeout-min", type=int, default=10)
    ap.add_argument("--fees", default="projected", choices=["zero", "projected"])
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    r = run_fill_aware(ticker=args.ticker, asset=args.asset, queue_frac=args.queue_frac,
                       entry_timeout_min=args.entry_timeout_min, fee_scenario=args.fees)
    print(json.dumps(r["summary"], indent=2, default=str))
    out = SIGNALS_DIR / STRATEGY / "fill_aware"
    out.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    if len(r["trade_pnl"]):
        r["trade_pnl"].to_csv(out / f"trades_{stamp}.csv", index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
