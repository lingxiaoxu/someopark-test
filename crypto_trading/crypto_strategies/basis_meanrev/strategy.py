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
CONFIG_PATH = __import__("pathlib").Path(__file__).with_name("config.yaml")
BRACKET_STATE_PATH = SIGNALS_DIR / STRATEGY / "brackets.json"


def load_config() -> dict:
    """Read config.yaml (so the yaml is actually honored, not decorative)."""
    try:
        import yaml
        return yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except Exception:
        logger.warning("could not load %s — using code defaults", CONFIG_PATH)
        return {}


def _bracket_pcts_from_config(cfg: dict) -> tuple[float, float] | None:
    """(tp_pct, sl_pct) from config.bracket, or None if disabled/absent."""
    br = (cfg or {}).get("bracket") or {}
    if not br.get("enabled", False):
        return None
    return (float(br.get("take_profit_pct", 0.010)), float(br.get("stop_loss_pct", 0.020)))


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
                 fee_role: str | None = None,
                 start=None, end=None, contracts_per_trade: int = 10,
                 initial_cash: float = 1000.0) -> dict:
    """Plan 01 v1 backtest. Returns summary + per-day returns (WF contract).

    ``fee_role``: None = fills' natural role (candle-tape IOC ⇒ taker); "maker"
    forces the maker rate. NOTE this is the OPTIMISTIC BOUND — it charges the
    maker fee but does NOT model maker fill probability or adverse selection
    (it assumes every passive order fills). Reality sits BETWEEN the taker line
    (pessimistic) and the maker line (optimistic); a true maker backtest needs
    the recorded book tape + a queue/fill model. Passive-first is still the
    point — just don't read the maker line as achieved P&L.
    """
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
                                initial_cash=initial_cash,
                                fee_role_override=fee_role)).run(tape, strat)
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


def _daily_equity(equity: pd.Series) -> pd.Series:
    """Per-event equity Series → daily equity (last value per UTC day)."""
    if equity is None or len(equity) == 0:
        return pd.Series(dtype=float)
    return equity.resample("1D").last().dropna()


def wf_run_backtest(params: dict, start, end) -> dict:
    """walk_forward-compatible engine (injected callable): returns
    {"equity_curve": <daily equity Series>} per the WalkForwardAnalyzer contract.
    """
    p = replace(BasisParams(), **{k: v for k, v in params.items()
                                  if k in BasisParams.__dataclass_fields__})
    r = run_backtest(params=p, start=start, end=end,
                     fee_scenario=params.get("fee_scenario", "projected"),
                     fee_role=params.get("fee_role"))
    return {"equity_curve": _daily_equity(r["equity"])}


# WF param sweep (Plan 01 §6): passive/maker execution is the intended mode, so
# every set forces fee_role=maker (see run_backtest docstring). DSR trials = this
# full sweep. Calibrate on recorded book tape as it accumulates.
WF_PARAM_SETS: dict[str, dict] = {
    f"k{ek}_x{xk}_w{w}_t{ts}": {"entry_k": ek, "exit_k": xk, "zscore_window_min": w,
                                "time_stop_min": ts, "fee_role": "maker",
                                "fee_scenario": "projected"}
    for ek in (2.0, 2.5, 3.0)
    for xk in (0.5,)
    for w in (30, 60)
    for ts in (60, 120)
}   # 12 sets


def wf_prices() -> pd.DataFrame:
    """Daily reference frame defining the WF fold grid (KXBTCPERP mid)."""
    from crypto_trading.crypto_common.loader import load_perp_candles
    c = load_perp_candles("KXBTCPERP", "1d")
    px = ((c["bid_close"] + c["ask_close"]) / 2).dropna()
    return px.to_frame("price")


# ── live dry-run signal (Plan 01 milestone 1: log live b_t + decay tracker) ─

def live_signal(*, ticker: str = "KXBTCPERP", asset: str = "BTC",
                params: BasisParams = BasisParams(), equity: float = 1000.0,
                bracket_pcts: tuple[float, float] | None = "config") -> dict:
    from crypto_trading.crypto_common.bracket import Bracket, BracketMonitor
    from crypto_trading.crypto_common.execution import ExecutionRouter, Order
    from crypto_trading.crypto_common.kalshi.rest_margin import KalshiMarginClient
    from crypto_trading.crypto_common.refdata.index import LiveComposite

    if bracket_pcts == "config":               # honor config.yaml bracket: block
        bracket_pcts = _bracket_pcts_from_config(load_config())

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
        subaccount = int(load_config().get("subaccount", 0))   # where perps live
        router = ExecutionRouter(STRATEGY)
        side = "bid" if desired > 0 else "ask"   # long=bid, short=ask (Kalshi enum)
        px = ba if desired > 0 else bb
        rec = router.submit(Order(ticker, side, dec.contracts, px, post_only=True,
                                  subaccount=subaccount))
        router.close()
        result["order"] = rec["status"]
        # ARM a TP/SL bracket (config-driven) into the PERSISTED monitor so the
        # bracket_watcher daemon actually enforces it — not just reported. Kalshi
        # has no native stop; the client-side watcher fires the reduce_only close.
        # The bracket carries the SAME subaccount so the close hits the position.
        if bracket_pcts is not None:
            tp_pct, sl_pct = bracket_pcts
            long = desired > 0
            bracket = Bracket(
                ticker, side, dec.contracts, entry_price=px, subaccount=subaccount,
                take_profit=px * (1 + tp_pct) if long else px * (1 - tp_pct),
                stop_loss=px * (1 - sl_pct) if long else px * (1 + sl_pct))
            BracketMonitor(ExecutionRouter(STRATEGY),
                           state_path=BRACKET_STATE_PATH).arm(bracket)
            result["bracket"] = {"armed": True,
                                 "take_profit": bracket.take_profit,
                                 "stop_loss": bracket.stop_loss,
                                 "tp_underlying": bracket.take_profit / csize,
                                 "sl_underlying": bracket.stop_loss / csize}
        else:
            result["bracket"] = {"armed": False, "reason": "disabled in config"}
    result["status"] = "signal"
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["backtest", "signal"])
    ap.add_argument("--ticker", default="KXBTCPERP")
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--fees", default="projected", choices=["zero", "projected"])
    ap.add_argument("--role", default=None, choices=["maker", "taker"],
                    help="force fee role; maker models passive/post-only execution")
    ap.add_argument("--contracts", type=int, default=10)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.mode == "backtest":
        r = run_backtest(ticker=args.ticker, asset=args.asset,
                         fee_scenario=args.fees, fee_role=args.role,
                         contracts_per_trade=args.contracts)
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
