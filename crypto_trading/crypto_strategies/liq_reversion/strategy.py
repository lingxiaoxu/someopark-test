"""Plan 04 — liquidation-cascade reversion backtest (event-sampled, Kalshi-native).

Detects cascades (signals/liquidation.py) then, per §7/§8, event-samples each
cascade window through crypto_common.backtest.intraday_sim: enter counter-trend
toward the index ONLY when the burst is FADING (never accelerating), scale-in a
tiny ladder, exit on partial reversion / time-stop / hard-abort. Reports P&L net
of BOTH taker and maker fees (passive liquidity provision is the plan's framing,
so the maker line is the relevant one; taker is the pessimistic bound).

Honesty (Plan 04 §8): cascades are rare and the Kalshi native tape is only ~6
days → the tradeable (fading, above-threshold) sample is a handful of events.
This is PRELIMINARY — a detector characterization + tiny-sample P&L, NOT a
validation gate. Do not read the aggregate P&L as an established edge.

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.liq_reversion.strategy
        [--tickers KXBTCPERP,KXETHPERP,...] [--overshoot 5] [--fees projected]
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.backtest.intraday_sim import (IntradaySim, SimConfig,
                                                                SimOrder)
from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.loader import (load_index_live, load_poll_market_stats,
                                                 load_poll_trades)
from crypto_trading.crypto_strategies.liq_reversion.signals.liquidation import (
    DetectorParams, build_features, detect_cascades)

logger = logging.getLogger(__name__)

ASSET_OF = {"KXBTCPERP": "BTC", "KXETHPERP": "ETH", "KXSOLPERP": "SOL",
            "KXXRPPERP": "XRP"}


@dataclass(frozen=True)
class EntryParams:
    scale_in_levels: int = 2         # tiny ladder, never the whole knife (§7)
    contracts_per_level: int = 1
    tp_fraction_of_overshoot: float = 0.6   # take this fraction of the reversion
    time_stop_bars: int = 30         # ~5 min at 10s grid
    hard_abort_mult: float = 1.6     # abort if |overshoot| extends past entry×this
    require_fading: bool = True


def _cascade_window_tape(feat: pd.DataFrame, start_i: int, bars: int,
                         synth_depth: int = 20) -> list[dict]:
    """Book+index tape for one cascade window from the feature grid (contract px)."""
    seg = feat.iloc[start_i:start_i + bars]
    tape = []
    for dt, row in seg.iterrows():
        ts = dt.timestamp()
        mid_c = row["mid_contract"]
        if not np.isfinite(mid_c) or mid_c <= 0:
            continue
        half = max(0.0001, (row["spread_bps"] or 1.0) * 1e-4 * mid_c / 2)
        tape.append({"type": "book", "ts": ts,
                     "bids": [[round(mid_c - half, 4), synth_depth]],
                     "asks": [[round(mid_c + half, 4), synth_depth]],
                     "overshoot_bps": row["overshoot_bps"], "index": row["index"]})
    return tape


def backtest_event(feat: pd.DataFrame, ev_ts, ev_row, p: EntryParams,
                   *, fee_scenario: str, fee_role: str | None,
                   ticker: str) -> dict | None:
    """Run one cascade event through the sim. Returns per-event summary."""
    if p.require_fading and not bool(ev_row["fading"]):
        return None
    try:
        start_i = feat.index.get_loc(ev_ts)
    except KeyError:
        return None
    tape = _cascade_window_tape(feat, start_i, p.time_stop_bars + 2)
    if len(tape) < 3:
        return None

    direction = int(ev_row["direction"])           # +1 buy (fade down), −1 sell
    entry_overshoot = abs(ev_row["overshoot_bps"])
    filled = {"n": 0}
    state = {"entered": 0, "peak_adverse": entry_overshoot}

    def strat(evb, sim: IntradaySim):
        if evb["type"] != "book":
            return []
        os_now = evb["overshoot_bps"]
        pos = sim.state.position
        # hard-abort: overshoot extended past abort in the SAME direction → flatten
        if abs(os_now) > entry_overshoot * p.hard_abort_mult and \
                np.sign(os_now) == np.sign(ev_row["overshoot_bps"]) and pos != 0:
            return [SimOrder("sell" if pos > 0 else "buy", abs(pos), tag="abort")]
        # scale-in toward index while still stretched and not yet full
        if state["entered"] < p.scale_in_levels and \
                abs(os_now) >= entry_overshoot * 0.6 and \
                np.sign(os_now) == np.sign(ev_row["overshoot_bps"]):
            state["entered"] += 1
            side = "buy" if direction > 0 else "sell"
            return [SimOrder(side, p.contracts_per_level, tag=f"scale{state['entered']}")]
        # take profit: overshoot reverted by tp_fraction toward 0
        if pos != 0 and abs(os_now) <= entry_overshoot * (1 - p.tp_fraction_of_overshoot):
            return [SimOrder("sell" if pos > 0 else "buy", abs(pos), tag="tp")]
        return []

    res = IntradaySim(SimConfig(fee_scenario=fee_scenario, ticker=ticker,
                                initial_cash=1000.0, fee_role_override=fee_role,
                                force_flat_at_end=True)).run(tape, strat)
    s = res.summary()
    return {"ts": str(ev_ts), "direction": direction,
            "entry_overshoot_bps": float(entry_overshoot),
            "net_pnl": s["net_pnl"], "fees": s["fees_paid"], "n_fills": s["n_fills"],
            "confidence": float(ev_row["confidence"])}


def run_ticker(ticker: str, *, overshoot_bps: float, oi_drop: float,
               fee_scenario: str, fee_role: str | None,
               p: EntryParams = EntryParams()) -> dict:
    asset = ASSET_OF.get(ticker)
    if asset is None:
        return {"ticker": ticker, "error": "no asset mapping"}
    tr = load_poll_trades(ticker)
    ms = load_poll_market_stats(ticker)
    # anchor: composite FIRST (load-bearing — the widened sweep showed the live
    # feed's tracking noise hides real cascades); live feed only as fallback
    ix = None
    try:
        from crypto_trading.crypto_common.loader import load_index_composite
        comp = load_index_composite(asset)
        if len(comp):
            ix = comp["vw_close"]
    except FileNotFoundError:
        pass
    if ix is None:
        try:
            ix = load_index_live(asset)["index"]
        except (FileNotFoundError, KeyError):
            return {"ticker": ticker, "error": "no index"}
    dp = DetectorParams(overshoot_entry_bps=overshoot_bps, oi_drop_min=oi_drop)
    feat = build_features(tr, ms, ix, dp)
    if not len(feat):
        return {"ticker": ticker, "error": "insufficient features"}
    ev = detect_cascades(feat, dp)
    n_all, n_fading = len(ev), int(ev["fading"].sum()) if len(ev) else 0
    events = []
    for ts, row in ev.iterrows():
        r = backtest_event(feat, ts, row, p, fee_scenario=fee_scenario,
                           fee_role=fee_role, ticker=ticker)
        if r:
            events.append(r)
    if not events:
        return {"ticker": ticker, "cascades": n_all, "fading": n_fading,
                "traded": 0, "note": "no tradeable (fading) events"}
    pnl = pd.Series([e["net_pnl"] for e in events])
    return {"ticker": ticker, "cascades": n_all, "fading": n_fading,
            "traded": len(events),
            "total_net_pnl": float(pnl.sum()), "mean_pnl": float(pnl.mean()),
            "hit_rate": float((pnl > 0).mean()), "worst": float(pnl.min()),
            "best": float(pnl.max()), "events": events}


# ── walk-forward wiring (Plan 04 §8) ─────────────────────────────────────────
WF_TICKERS = ("KXBTCPERP", "KXETHPERP")     # = frozen config universe (composite anchor)


def _frozen_cfg() -> dict:
    """The production config (config.yaml) — WF defaults come from HERE so the
    validate chain reflects the deployed cell, not code-level fallbacks."""
    try:
        import pathlib

        import yaml
        return yaml.safe_load(
            (pathlib.Path(__file__).with_name("config.yaml")).read_text()) or {}
    except Exception:
        return {}


def _frozen_params() -> dict:
    c = _frozen_cfg()
    d, e = (c.get("detector") or {}), (c.get("exits") or {})
    return {"overshoot_entry_bps": float(d.get("overshoot_entry_bps", 15.0)),
            "oi_drop_min": float(d.get("oi_drop_min", 0.002)),
            "tp_fraction": float(e.get("tp_fraction", 0.5)),
            "time_stop_bars": 30}


# Sweep AROUND the frozen winning cell (os15/oi0.002/tp0.5 — DSR 0.96) for future
# re-validation. maker fills. DSR trials = this full sweep (8 sets).
_F = _frozen_params()
WF_PARAM_SETS: dict[str, dict] = {
    "frozen_os15_oi002_tp05": dict(_F),
    "os12_oi002_tp05": {**_F, "overshoot_entry_bps": 12.0},
    "os20_oi002_tp05": {**_F, "overshoot_entry_bps": 20.0},
    "os15_oi002_tp07": {**_F, "tp_fraction": 0.7},
    "os12_oi002_tp07": {**_F, "overshoot_entry_bps": 12.0, "tp_fraction": 0.7},
    "os15_oi004_tp05": {**_F, "oi_drop_min": 0.004},
    "os15_oi002_tp05_ts60": {**_F, "time_stop_bars": 60},
    "os20_oi002_tp07": {**_F, "overshoot_entry_bps": 20.0, "tp_fraction": 0.7},
}   # 8 sets centered on the production cell


def wf_run_backtest(params: dict, start, end) -> dict:
    """WF engine: event P&L (maker, projected fees) → daily equity (base 1.0).

    Defaults = the FROZEN production config (config.yaml), so an empty params
    dict runs exactly the deployed cell. Detection runs once over the full
    loaded tape per param set (the analyzer slices IS/OOS); events are filtered
    to [start,end] and their net P&L accumulated per UTC day into equity.
    """
    from collections import defaultdict
    frozen = _frozen_params()
    ep = EntryParams(
        tp_fraction_of_overshoot=params.get("tp_fraction", frozen["tp_fraction"]),
        time_stop_bars=int(params.get("time_stop_bars", frozen["time_stop_bars"])))
    daily_pnl: dict[pd.Timestamp, float] = defaultdict(float)
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    for ticker in WF_TICKERS:
        r = run_ticker(ticker,
                       overshoot_bps=params.get("overshoot_entry_bps",
                                                frozen["overshoot_entry_bps"]),
                       oi_drop=params.get("oi_drop_min", frozen["oi_drop_min"]),
                       fee_scenario="projected", fee_role="maker", p=ep)
        for e in r.get("events", []):
            ts = pd.Timestamp(e["ts"])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            if start <= ts <= end:
                daily_pnl[ts.normalize()] += float(e["net_pnl"])
    if not daily_pnl:
        return {"equity_curve": pd.Series(dtype=float)}
    s = pd.Series(daily_pnl).sort_index()
    equity = 1.0 + s.cumsum() / 1000.0        # base-1 equity on the $1000 sleeve
    return {"equity_curve": equity}


def wf_prices() -> pd.DataFrame:
    """Daily reference frame defining the fold grid (KXBTCPERP mid candles)."""
    from crypto_trading.crypto_common.loader import load_perp_candles
    c = load_perp_candles("KXBTCPERP", "1d")
    px = ((c["bid_close"] + c["ask_close"]) / 2).dropna()
    return px.to_frame("price")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tickers", default="KXBTCPERP,KXETHPERP,KXSOLPERP")
    ap.add_argument("--overshoot", type=float, default=5.0, help="overshoot_entry_bps")
    ap.add_argument("--oi-drop", type=float, default=0.001)
    ap.add_argument("--fees", default="projected", choices=["zero", "projected"])
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]

    print("=" * 70)
    print(f"PLAN 04 cascade-reversion backtest — PRELIMINARY (~6d Kalshi native)")
    print(f"overshoot>={args.overshoot}bps + OI-drop>={args.oi_drop} + one-sided burst")
    print("=" * 70)
    out = {}
    for role_label, role in [("taker", "taker"), ("maker", "maker")]:
        print(f"\n[fees={args.fees} / {role_label} fills]")
        for t in tickers:
            r = run_ticker(t, overshoot_bps=args.overshoot, oi_drop=args.oi_drop,
                           fee_scenario=args.fees, fee_role=role)
            out[f"{t}_{role_label}"] = r
            if r.get("error"):
                print(f"  {t}: {r['error']}"); continue
            if r.get("traded", 0) == 0:
                print(f"  {t}: {r['cascades']} cascades / {r['fading']} fading / "
                      f"0 traded — {r.get('note','')}"); continue
            print(f"  {t}: {r['cascades']} casc / {r['fading']} fading / {r['traded']} traded "
                  f"| net ${r['total_net_pnl']:+.4f} | mean ${r['mean_pnl']:+.4f} "
                  f"| hit {100*r['hit_rate']:.0f}% | worst ${r['worst']:+.4f}")

    outdir = SIGNALS_DIR / "liq_reversion" / "backtests"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"liq_backtest_{stamp}.json").write_text(json.dumps(out, indent=1, default=str))
    print("\n" + "=" * 70)
    print("HONEST READ: cascades on Kalshi are RARE + SMALL in this calm ~6d window;")
    print("tradeable (fading) sample is a handful of events → tiny-sample anecdote,")
    print("NOT a validation gate. Detector works; needs a volatile period + more data.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
