"""Plan 01 strategy glue: backtest + live dry-run signal (`basis_meanrev`).

Backtest (candle tape, v1): replays the 1m basis frame through IntradaySim.
Book events are synthesized from candle bid/ask closes with configured
synthetic depth — honest limitation, stated loudly: candle-level fills are
OPTIMISTIC about depth/queue. The recorded poll/WS book tape replaces this
as it accumulates (loader.load_poll_books) — same Sim, richer tape.

Live signal (dry-run): samples the LIVE book (public REST) + LIVE composite,
computes z against recent recorded history, sizes via crypto_common.sizing,
gates via RiskKill, and routes a DRY-RUN order through ExecutionRouter (the
demo-first gate keeps everything inert). Also appends the b_t decay-tracker
row (Plan 01 §8).

CLI:
    … -m crypto_trading.crypto_strategies.basis_meanrev.strategy backtest
        [--ticker KXBTCPERP] [--asset BTC] [--fees projected] [--params k=v …]
    … -m crypto_trading.crypto_strategies.basis_meanrev.strategy signal
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict, replace

import pandas as pd

from crypto_trading.crypto_common.backtest.intraday_sim import (IntradaySim, SimConfig,
                                                                SimOrder)
from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.costs import decimal_book_to_levels, walk_book
from crypto_trading.crypto_common.io_jsonl import DailyJsonlWriter
from crypto_trading.crypto_common.loader import build_basis_frame, load_funding
from crypto_trading.crypto_common.risk_kill import GuardConfig, RiskKill, RiskState
from crypto_trading.crypto_common.sizing import SizingConfig, size_position
from crypto_trading.crypto_strategies.basis_meanrev.signals.basis import (BasisParams,
                                                                          compute_signal_frame,
                                                                          ou_half_life,
                                                                          rolling_zscore)

logger = logging.getLogger(__name__)

STRATEGY = "basis_meanrev"
SYNTH_DEPTH = 50           # contracts per side in candle-tape book events (v1 limitation)


# ── backtest ────────────────────────────────────────────────────────────────

def frame_to_tape(frame: pd.DataFrame, funding: pd.DataFrame | None,
                  contract_size: float) -> list[dict]:
    """1m basis frame → sim tape: book events (+funding settlements, PIT)."""
    tape = []
    for dt, row in frame.iterrows():
        ts = dt.timestamp()
        mid_c = row.mark_mid_contract
        half_spread = max(0.0001, mid_c * 1e-4 / 2)      # ≥1 tick, ~1bp default
        tape.append({"type": "book", "ts": ts,
                     "bids": [[round(mid_c - half_spread, 4), SYNTH_DEPTH]],
                     "asks": [[round(mid_c + half_spread, 4), SYNTH_DEPTH]],
                     "signal_row": row})
    if funding is not None and len(funding):
        f = funding[(funding.index >= frame.index.min())
                    & (funding.index <= frame.index.max())]
        for dt, row in f.iterrows():
            tape.append({"type": "funding", "ts": dt.timestamp(),
                         "rate": float(row.funding_rate)})
    tape.sort(key=lambda e: (e["ts"], 0 if e["type"] == "funding" else 1))
    return tape


def run_backtest(*, ticker: str = "KXBTCPERP", asset: str = "BTC",
                 params: BasisParams = BasisParams(), fee_scenario: str = "projected",
                 start=None, end=None, contracts_per_trade: int = 10,
                 initial_cash: float = 1000.0) -> dict:
    """Plan 01 v1 backtest. Returns summary + per-day returns (WF contract)."""
    frame = build_basis_frame(ticker, asset, start=start, end=end)
    sig = compute_signal_frame(frame, params)
    try:
        funding = load_funding(ticker)
    except FileNotFoundError:
        funding = None
    # contract mid → underlying scale factor implied by the frame itself
    csize = float(frame.mark_mid_contract.iloc[-1] / frame.mark_mid_underlying.iloc[-1])
    tape = frame_to_tape(sig, funding, csize)

    desired_by_ts = {dt.timestamp(): d for dt, d in sig.desired.items()}

    def strat(ev, sim: IntradaySim):
        if ev["type"] != "book":
            return []
        want = desired_by_ts.get(ev["ts"])
        if want is None:
            return []
        target = want * contracts_per_trade
        diff = target - sim.state.position
        if diff == 0:
            return []
        return [SimOrder("buy" if diff > 0 else "sell", abs(diff), tag=f"target={target}")]

    res = IntradaySim(SimConfig(fee_scenario=fee_scenario, ticker=ticker,
                                initial_cash=initial_cash)).run(tape, strat)
    summary = res.summary()
    n_trades = int((sig.desired.diff().fillna(0) != 0).sum())
    summary.update({
        "ticker": ticker, "fee_scenario": fee_scenario, "params": asdict(params),
        "bars": len(sig), "signal_changes": n_trades,
        "pct_time_in_market": float((sig.desired != 0).mean()),
        "span": [str(sig.index.min()), str(sig.index.max())],
        "note": "v1 candle tape: synthetic depth, optimistic fills — "
                "re-run on recorded book tape as it accumulates",
    })
    return {"summary": summary, "daily_returns": res.daily_returns,
            "equity": res.equity}


def wf_run_backtest(params: dict, start, end) -> dict:
    """walk_forward-compatible wrapper (injected-callable contract)."""
    p = replace(BasisParams(), **{k: v for k, v in params.items()
                                  if k in BasisParams.__dataclass_fields__})
    r = run_backtest(params=p, start=start, end=end,
                     fee_scenario=params.get("fee_scenario", "projected"))
    return {"returns": r["daily_returns"], **r["summary"]}


# ── live dry-run signal (Plan 01 milestone 1: log live b_t + decay tracker) ─

def live_signal(*, ticker: str = "KXBTCPERP", asset: str = "BTC",
                params: BasisParams = BasisParams(),
                equity: float = 1000.0) -> dict:
    from crypto_trading.crypto_common.execution import ExecutionRouter, Order
    from crypto_trading.crypto_common.kalshi.rest_margin import KalshiMarginClient
    from crypto_trading.crypto_common.refdata.index import LiveComposite

    margin = KalshiMarginClient(env="prod")           # public reads
    m = margin.market(ticker)
    book = margin.orderbook(ticker)
    bids, asks = decimal_book_to_levels(book)
    live_idx = LiveComposite([asset]).sample(asset)

    csize = float(m.get("contract_size") or 0)
    bb = max((p for p, s in bids), default=None)
    ba = min((p for p, s in asks), default=None)
    now = time.time()
    result: dict = {"ts": now, "ticker": ticker, "asset": asset,
                    "index": live_idx.get("index"), "stale_index": live_idx.get("stale")}
    if bb is None or ba is None or not csize or not live_idx.get("index"):
        result["status"] = "no-data"
        return result

    mid_underlying = (bb + ba) / 2 / csize
    b_now = (mid_underlying - live_idx["index"]) / live_idx["index"]
    hist = build_basis_frame(ticker, asset).b_t
    b_all = pd.concat([hist, pd.Series([b_now], index=[pd.Timestamp.now(tz="UTC")])])
    z_now = float(rolling_zscore(b_all, params.zscore_window_min).iloc[-1])
    hl = ou_half_life(b_all.tail(params.half_life_window_min))

    result.update({"b_t_bps": 1e4 * b_now, "z": z_now,
                   "half_life_min": hl, "spread_bps": 1e4 * (ba - bb) / ((ba + bb) / 2)})

    # decay tracker (Plan 01 §8) — one row per invocation
    DailyJsonlWriter(SIGNALS_DIR / STRATEGY / "decay_tracker").write(".", result)

    desired = 0
    if hl is not None and hl <= params.half_life_max_min:
        if z_now >= params.entry_k:
            desired = -1
        elif z_now <= -params.entry_k:
            desired = +1
    result["desired"] = desired
    if desired == 0:
        result["status"] = "flat"
        return result

    # sizing + Layer-1 gate + dry-run routing (all inert until operator opens gate)
    side_levels = asks if desired > 0 else bids
    w = walk_book(side_levels, 10, side="buy" if desired > 0 else "sell")
    dec = size_position(equity=equity, contract_price=(ba + bb) / 2,
                        realized_vol_annual=float(hist.std() * (365 * 1440) ** 0.5),
                        cfg=SizingConfig())
    kill = RiskKill(STRATEGY, GuardConfig())
    ok, why = kill.pre_trade_ok(RiskState(equity_sod=equity, equity_now=equity,
                                          last_index_ts=now, last_book_ts=now),
                                order_contracts=dec.contracts)
    result.update({"size": dec.contracts, "size_binding": dec.binding,
                   "risk_ok": ok, "risk_why": why,
                   "book_walk_avg": w.avg_price})
    if ok and dec.contracts > 0:
        router = ExecutionRouter(STRATEGY)
        px = ba if desired > 0 else bb
        rec = router.submit(Order(ticker, "buy" if desired > 0 else "sell",
                                  dec.contracts, px, post_only=True))
        router.close()
        result["order"] = rec["status"]
    result["status"] = "signal"
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["backtest", "signal"])
    ap.add_argument("--ticker", default="KXBTCPERP")
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--fees", default="projected", choices=["zero", "projected"])
    ap.add_argument("--contracts", type=int, default=10)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.mode == "backtest":
        r = run_backtest(ticker=args.ticker, asset=args.asset,
                         fee_scenario=args.fees, contracts_per_trade=args.contracts)
        print(json.dumps(r["summary"], indent=2, default=str))
        out = SIGNALS_DIR / STRATEGY / "backtests"
        out.mkdir(parents=True, exist_ok=True)
        stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
        (out / f"backtest_{stamp}.json").write_text(
            json.dumps(r["summary"], indent=2, default=str))
    else:
        print(json.dumps(live_signal(ticker=args.ticker, asset=args.asset),
                         indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
