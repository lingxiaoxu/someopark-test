"""Plan 04 live armable DRY-RUN signal — the frozen production cell, live.

Mirrors basis_meanrev's `signal` pattern: pull the recent recorded tape (the
poller/index daemons keep it fresh), run the FROZEN detector config, and if a
fading cascade ≥15bps with the OI-drop signature is ACTIVE right now:
size → risk-gate → route a DRY-RUN maker order through the gated
ExecutionRouter → arm an overshoot-relative TP/SL bracket → log to the tracker.
Everything stays inert until the operator opens the live gate (demo-first).

Anchor note: backtests use the 1-min spot composite. Live, the composite parquet
is only as fresh as the last backfill, so when it is stale we fall back to the
recorded 5s live-composite feed RESAMPLED TO 1-MIN (same three-venue VWAP; the
resample removes most of the tracking noise that hid cascades at the 10s grid).

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.liq_reversion.live_signal
        [--ticker KXBTCPERP] [--allow-stale]
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib

import pandas as pd

from crypto_trading.crypto_common.bracket import Bracket, BracketMonitor
from crypto_trading.crypto_common.bracket_watcher import state_path
from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.execution import ExecutionRouter, Order
from crypto_trading.crypto_common.io_jsonl import DailyJsonlWriter
from crypto_trading.crypto_common.loader import (load_index_composite, load_index_live,
                                                 load_poll_market_stats, load_poll_trades)
from crypto_trading.crypto_common.risk_kill import GuardConfig, RiskKill, RiskState
from crypto_trading.crypto_common.sizing import SizingConfig, size_position
from crypto_trading.crypto_strategies.liq_reversion.signals.liquidation import (
    DetectorParams, build_features, detect_cascades)
from crypto_trading.crypto_strategies.liq_reversion.widened import (COMPOSITE_ASSET,
                                                                    OKX_SYMBOL,
                                                                    load_okx_liq_times,
                                                                    okx_confirmed)

logger = logging.getLogger(__name__)
STRATEGY = "liq_reversion"
CONFIG_PATH = pathlib.Path(__file__).with_name("config.yaml")


def load_config() -> dict:
    try:
        import yaml
        return yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except Exception:
        logger.warning("could not load %s — using code defaults", CONFIG_PATH)
        return {}


def detector_from_config(cfg: dict) -> DetectorParams:
    d = (cfg.get("detector") or {})
    return DetectorParams(
        grid_sec=int(d.get("grid_sec", 10)),
        burst_window_bars=int(d.get("burst_window_bars", 3)),
        baseline_bars=int(d.get("baseline_bars", 180)),
        intensity_threshold=float(d.get("intensity_threshold", 3.0)),
        one_sided_min=float(d.get("one_sided_min", 0.65)),
        oi_drop_min=float(d.get("oi_drop_min", 0.002)),
        overshoot_entry_bps=float(d.get("overshoot_entry_bps", 15.0)),
        fade_lookback_bars=int(d.get("fade_lookback_bars", 2)))


def _recent_days() -> list[str]:
    now = pd.Timestamp.now(tz="UTC")
    return [(now - pd.Timedelta(days=1)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")]


def _index_series(asset: str, *, freshness_min: float = 30.0) -> tuple[pd.Series, str]:
    """Composite if fresh enough, else the live feed resampled to 1-min."""
    try:
        comp = load_index_composite(asset)
        if len(comp):
            age_min = (pd.Timestamp.now(tz="UTC") - comp.index.max()).total_seconds() / 60
            if age_min <= freshness_min:
                return comp["vw_close"], "composite"
    except FileNotFoundError:
        pass
    live = load_index_live(asset, days=_recent_days())
    if live.empty:
        raise RuntimeError(f"no index feed for {asset} — is idxrecord running?")
    # right/right for convention consistency with the end-labeled composite —
    # live has no lookahead either way; this keeps live == backtest semantics
    return (live["index"].resample("1min", label="right", closed="right")
            .last().dropna(), "live_resampled_1min")


def run_live_signal(*, ticker: str = "KXBTCPERP", allow_stale: bool = False,
                    cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else load_config()
    det = detector_from_config(cfg)
    live_cfg = cfg.get("live") or {}
    lookback = pd.Timedelta(minutes=float(live_cfg.get("lookback_min", 240)))
    active_win = pd.Timedelta(minutes=float(live_cfg.get("active_window_min", 5)))
    staleness_max = pd.Timedelta(minutes=float((cfg.get("risk") or {})
                                               .get("staleness_max_min", 10)))
    subaccount = int(cfg.get("subaccount", 0))
    asset = COMPOSITE_ASSET.get(ticker)
    result: dict = {"ts": str(pd.Timestamp.now(tz="UTC")), "ticker": ticker,
                    "strategy": STRATEGY}
    if asset is None:
        result["status"] = "unsupported_ticker"        # config universe is BTC+ETH only
        return result

    days = _recent_days()
    stats = load_poll_market_stats(ticker, days=days)
    trades = load_poll_trades(ticker, days=days)
    if stats.empty or trades.empty:
        result["status"] = "no_tape"
        return result
    cut = stats.index.max() - lookback
    stats = stats[stats.index >= cut]
    trades = trades[trades.index >= cut].sort_index()
    index_series, anchor_kind = _index_series(asset)
    result["anchor"] = anchor_kind

    feat = build_features(trades, stats, index_series, det)
    if feat.empty:
        result["status"] = "insufficient_features"
        return result
    now_bar = feat.index[-1]
    tape_age = pd.Timestamp.now(tz="UTC") - now_bar
    result["tape_age_min"] = round(tape_age.total_seconds() / 60, 1)
    if tape_age > staleness_max and not allow_stale:
        result["status"] = "stale_tape"
        return result

    events = detect_cascades(feat, det)
    events = events[events["fading"]] if len(events) else events    # fading-only
    active = events[events.index >= now_bar - active_win] if len(events) else events
    result["cascades_recent"] = int(len(events))
    if active.empty:
        result["status"] = "no_cascade"
        _track(result)
        return result

    ev_ts, ev = active.index[-1], active.iloc[-1]
    direction = int(ev["direction"])                    # +1 fade down-overshoot (long)
    okx_cfg = cfg.get("okx_confirm") or {}
    confirmed = False
    if okx_cfg.get("enabled", False):
        liq_times = load_okx_liq_times(OKX_SYMBOL.get(ticker, ""))
        confirmed = okx_confirmed(ev_ts, liq_times,
                                  window_min=float(okx_cfg.get("window_min", 2.0)))
    result.update({"status": "cascade_signal", "event_ts": str(ev_ts),
                   "direction": direction,
                   "overshoot_bps": float(ev["overshoot_bps"]),
                   "oi_delta": float(ev["oi_delta"]),
                   "intensity": float(ev["intensity"]),
                   "confidence": float(ev["confidence"]),
                   "okx_confirmed": bool(confirmed)})

    # touch prices from the freshest stats row
    last = stats.dropna(subset=["bid", "ask"]).iloc[-1]
    bid, ask = float(last["bid"]), float(last["ask"])
    csize = float(last["contract_size"]) if last.get("contract_size") else 1e-4
    entry_px = bid if direction > 0 else ask            # maker at the touch
    side = "bid" if direction > 0 else "ask"

    sz_cfg = cfg.get("sizing") or {}
    mid_1m = feat["mid"].resample("1min").last().dropna()
    rvol = float(mid_1m.pct_change().std() * (365 * 24 * 60) ** 0.5) if len(mid_1m) > 5 else 0.5
    dec = size_position(equity=float(sz_cfg.get("equity", 1000.0)),
                        contract_price=(bid + ask) / 2, realized_vol_annual=rvol,
                        cfg=SizingConfig(
                            target_vol_annual=float(sz_cfg.get("target_vol_annual", 0.40)),
                            min_contracts=int(sz_cfg.get("min_contracts", 1))))
    contracts = min(dec.contracts, int(sz_cfg.get("contracts", 10)))
    r_cfg = cfg.get("risk") or {}
    kill = RiskKill(STRATEGY, GuardConfig(
        max_daily_loss_pct=float(r_cfg.get("max_daily_loss_pct", 0.05)),
        max_events_per_hour=int(r_cfg.get("max_events_per_hour", 4))))
    now_s = pd.Timestamp.now(tz="UTC").timestamp()
    ok, why = kill.pre_trade_ok(
        RiskState(equity_sod=float(sz_cfg.get("equity", 1000.0)),
                  equity_now=float(sz_cfg.get("equity", 1000.0)),
                  last_index_ts=now_s, last_book_ts=now_s),
        order_contracts=contracts)
    result.update({"size": contracts, "size_binding": dec.binding,
                   "risk_ok": ok, "risk_why": why})
    if not ok or contracts < 1:
        _track(result)
        return result

    router = ExecutionRouter(STRATEGY)
    rec = router.submit(Order(ticker, side, contracts, entry_px, post_only=True,
                              subaccount=subaccount))
    router.close()
    result["order"] = rec["status"]

    # overshoot-relative TP/SL bracket (armed for the watcher daemon)
    ex_cfg = cfg.get("exits") or {}
    tp_frac = float(ex_cfg.get("tp_fraction", 0.5))
    abort_mult = float(ex_cfg.get("hard_abort_mult", 2.0))
    index_now = float(ev["index"])
    entry_under = entry_px / csize
    os_frac = (entry_under - index_now) / index_now     # signed overshoot at entry
    tp_under = index_now * (1 + os_frac * (1 - tp_frac))
    sl_under = index_now * (1 + os_frac * abort_mult)
    bracket = Bracket(ticker, side, contracts, entry_price=entry_px,
                      take_profit=tp_under * csize, stop_loss=sl_under * csize,
                      subaccount=subaccount)
    BracketMonitor(ExecutionRouter(STRATEGY),
                   state_path=state_path(STRATEGY)).arm(bracket)
    result["bracket"] = {"armed": True, "take_profit": bracket.take_profit,
                         "stop_loss": bracket.stop_loss,
                         "tp_underlying": tp_under, "sl_underlying": sl_under}
    _track(result)
    return result


def _track(result: dict) -> None:
    w = DailyJsonlWriter(SIGNALS_DIR / STRATEGY / "signal_tracker")
    w.write(".", result)
    w.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", default="KXBTCPERP")
    ap.add_argument("--allow-stale", action="store_true",
                    help="evaluate even if the recorded tape is stale (diagnostics)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(json.dumps(run_live_signal(ticker=args.ticker, allow_stale=args.allow_stale),
                     indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
