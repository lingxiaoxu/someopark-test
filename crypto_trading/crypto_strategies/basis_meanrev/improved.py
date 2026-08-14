"""Plan 01 SELECTIVE passive variant — the redesign after the naive fade died.

Diagnosis (fill-aware validation, 2026-07-26): naive "post at touch on |z|≥2.5,
wait for reversion" is significantly NEGATIVE on the recorded tape (~200 trades,
NW-t −5.8, hit 20%) — adverse selection: the touch fills exactly when aggressive
flow sweeps through and the basis then continues. The Plan 04 contrast showed
SELECTIVITY fixes the same failure (OI-drop + fading-filtered cascade fades won).

Levers implemented here (all sweepable):
  1. fading-flow entry filter — only post when the aggressive flow that
     stretched the basis is DECELERATING (recent adverse-taker volume < prior
     window, and there WAS a sweep). Never catch an accelerating sweep.
  2. post BEHIND the touch — limit offset N ticks beyond the touch on the
     stretched side; only deep sweeps reach us (more overshoot per fill + extra
     spread earned). KXBTCPERP tick=0.0001, median spread ≈ 11 ticks.
  3. bigger stretch only — entry_k / min_abs_bps raised vs naive.
  4. fast asymmetric abort — if the basis EXTENDS ≥ abort_bps beyond the entry
     basis while held, cross out immediately (taker). Caps the toxic-fill loss
     the naive version held to the bitter end.
  5. optional OI-drop confirmation — only fade moves carrying a liquidation
     signature (converges toward the validated Plan 04 detector).

Execution realism mirrors fill_aware.py (recorded tape, queue-aware maker fills,
composite index anchor) with one deliberate change: taker exits cross the spread
to the OPPOSITE touch (the original credited the same-side touch — optimistic).
Results here are therefore slightly more pessimistic than the naive baseline's
published numbers, which is the honest direction.

CLI (runs the bounded sweep + multiple-testing-deflated verdict):
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.basis_meanrev.improved
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.backtest.fill_model import simulate_maker_fill
from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.costs import fee_dollars
from crypto_trading.crypto_common.loader import load_poll_market_stats, load_poll_trades
from crypto_trading.crypto_common.trade_stats import trade_significance_report
from crypto_trading.crypto_strategies.basis_meanrev.fill_aware import build_recorded_frame
from crypto_trading.crypto_strategies.basis_meanrev.signals.basis import (ou_half_life,
                                                                          rolling_zscore)

logger = logging.getLogger(__name__)
STRATEGY = "basis_meanrev"
TICK = 0.0001                       # KXBTCPERP tick size (probe-verified)


@dataclass(frozen=True)
class ImprovedParams:
    # signal (lever 3)
    entry_k: float = 3.5
    exit_k: float = 0.5
    min_abs_bps: float = 10.0
    zscore_window_min: int = 30
    half_life_max_min: float = 60.0
    half_life_window_min: int = 240
    signal_time_stop_min: int = 90          # hysteresis time-stop (as naive)
    # execution
    offset_ticks: int = 4                   # lever 2: post behind the touch
    entry_timeout_min: int = 10
    exit_timeout_min: int = 30              # position time-stop (as fill_aware)
    queue_frac: float = 1.0
    fee_scenario: str = "projected"
    contracts: int = 10
    # lever 1: fading-flow filter
    flow_filter: bool = True
    flow_recent_min: int = 2                # adverse-taker vol in [t-2, t)
    flow_prior_min: int = 4                 # vs [t-6, t-2)
    # lever 4: fast abort
    abort_bps: float | None = 15.0          # None = no abort (naive behaviour)
    # lever 5: OI-drop confirmation
    oi_confirm: bool = False
    oi_lookback_min: int = 5
    oi_drop_frac: float = 0.001             # ≥0.1% OI drop = liquidation signature


@dataclass
class Prep:
    """Loaded-once recorded-tape context shared across the sweep."""
    frame: pd.DataFrame          # 1min: bid, ask, b_t_bps, z, hl
    trades: pd.DataFrame         # dense tape (dt-indexed)
    minute_vol: pd.DataFrame     # per-minute taker volume, cols ['bid','ask']
    oi: pd.Series                # per-minute open interest (ffilled)
    csize: float


def prepare(ticker: str = "KXBTCPERP", asset: str = "BTC") -> Prep:
    frame, csize = build_recorded_frame(ticker, asset, index_source="composite")
    trades = load_poll_trades(ticker).sort_index()
    if trades.empty:
        raise RuntimeError("no recorded trades — run the poller first")
    stats = load_poll_market_stats(ticker)

    p = ImprovedParams()
    frame = frame.copy()
    frame["z"] = rolling_zscore(frame.b_t, p.zscore_window_min)
    frame["hl"] = (frame.b_t.rolling(p.half_life_window_min,
                                     min_periods=max(30, p.half_life_window_min // 4))
                   .apply(lambda w: ou_half_life(pd.Series(w)) or np.nan, raw=False))

    # PIT: right/right to match the (right-labeled) frame — a left-labeled bar
    # reindexed onto right labels would hand each row its own future minute.
    mv = (trades.groupby([pd.Grouper(freq="1min", label="right", closed="right"),
                          "taker_side"])["count"].sum()
          .unstack(fill_value=0.0))
    for c in ("bid", "ask"):
        if c not in mv.columns:
            mv[c] = 0.0
    minute_vol = mv[["bid", "ask"]].reindex(frame.index, fill_value=0.0)

    oi = (stats["oi"].dropna().resample("1min", label="right", closed="right")
          .last().reindex(frame.index).ffill())
    return Prep(frame=frame, trades=trades, minute_vol=minute_vol, oi=oi, csize=csize)


def desired_path(prep: Prep, p: ImprovedParams) -> np.ndarray:
    """Hysteresis state machine (verbatim semantics of signals.basis) on the
    precomputed z/hl — recomputed cheaply per (entry_k, min_abs) combo."""
    z = prep.frame.z.to_numpy()
    hl = prep.frame.hl.to_numpy()
    bps = prep.frame.b_t_bps.to_numpy()
    n = len(z)
    desired = np.zeros(n)
    state, entry_i = 0, None
    for i in range(n):
        zi, hli = z[i], hl[i]
        if state == 0:
            tradeable = (not np.isnan(zi) and not np.isnan(hli)
                         and hli <= p.half_life_max_min and abs(bps[i]) >= p.min_abs_bps)
            if tradeable and zi >= p.entry_k:
                state, entry_i = -1, i
            elif tradeable and zi <= -p.entry_k:
                state, entry_i = +1, i
        else:
            timed_out = entry_i is not None and (i - entry_i) >= p.signal_time_stop_min
            hl_blown = np.isnan(hli) or hli > p.half_life_max_min
            reverted = not np.isnan(zi) and abs(zi) <= p.exit_k
            if reverted or timed_out or hl_blown:
                state, entry_i = 0, None
        desired[i] = state
    return desired


def flow_is_fading(minute_vol: pd.DataFrame, i: int, adverse_col: str,
                   recent_min: int, prior_min: int) -> bool:
    """Lever 1: adverse-taker volume decelerating (and a sweep existed)."""
    if i < recent_min + prior_min:
        return False
    v = minute_vol[adverse_col].to_numpy()
    recent = float(v[i - recent_min:i].sum())
    prior = float(v[i - recent_min - prior_min:i - recent_min].sum())
    return prior > 0 and recent < prior


def oi_dropped(oi: pd.Series, i: int, lookback: int, drop_frac: float) -> bool:
    """Lever 5: OI fell ≥ drop_frac from its recent max (liquidation signature)."""
    if i < lookback:
        return False
    window = oi.iloc[i - lookback:i]
    now = oi.iloc[i]
    if window.isna().all() or pd.isna(now):
        return False
    peak = float(window.max())
    return peak > 0 and (peak - float(now)) / peak >= drop_frac


def run_config(prep: Prep, p: ImprovedParams, *, ticker: str = "KXBTCPERP") -> dict:
    frame = prep.frame
    trades = prep.trades
    desired = desired_path(prep, p)
    bid = frame.bid.to_numpy()
    ask = frame.ask.to_numpy()
    bps = frame.b_t_bps.to_numpy()
    idx = frame.index
    entry_to = pd.Timedelta(minutes=p.entry_timeout_min)

    def resting_size(ts):
        w = trades.loc[ts - pd.Timedelta(minutes=5):ts, "count"]
        return float(w.median()) if len(w) else 1.0

    rows = []
    attempts = fills = flow_blocked = oi_blocked = 0
    in_pos = False
    entry_px = entry_ts = entry_b = pos_sign = None

    for i, ts in enumerate(idx):
        want = int(desired[i])
        if not in_pos and want != 0:
            adverse_col = "bid" if want < 0 else "ask"   # buyers stretched it rich / sellers cheap
            if p.flow_filter and not flow_is_fading(prep.minute_vol, i, adverse_col,
                                                    p.flow_recent_min, p.flow_prior_min):
                flow_blocked += 1
                continue
            if p.oi_confirm and not oi_dropped(prep.oi, i, p.oi_lookback_min, p.oi_drop_frac):
                oi_blocked += 1
                continue
            if np.isnan(bid[i]) or np.isnan(ask[i]):
                continue
            attempts += 1
            off = p.offset_ticks * TICK
            if want > 0:            # long: bid below the touch
                side, limit = "bid", round(bid[i] - off, 6)
            else:                   # short: ask above the touch
                side, limit = "ask", round(ask[i] + off, 6)
            q = p.queue_frac * resting_size(ts)
            fr = simulate_maker_fill(limit, side, ts, trades, timeout=entry_to, queue_ahead=q)
            if fr.filled:
                fills += 1
                in_pos, entry_px, entry_ts, pos_sign = True, fr.fill_price, fr.fill_ts, want
                j = idx.searchsorted(fr.fill_ts, side="right") - 1
                entry_b = bps[j] if j >= 0 else bps[i]
        elif in_pos:
            # lever 4: fast abort on adverse basis extension (taker, cross the spread)
            adverse_ext = (bps[i] - entry_b) if pos_sign < 0 else (entry_b - bps[i])
            aborted = p.abort_bps is not None and adverse_ext >= p.abort_bps
            timed_out = (ts - entry_ts) >= pd.Timedelta(minutes=p.exit_timeout_min)
            reverted = want == 0 or np.sign(want) != np.sign(pos_sign)
            if not (aborted or reverted or timed_out):
                continue
            if np.isnan(bid[i]) or np.isnan(ask[i]):
                continue
            if aborted:
                exit_px = ask[i] if pos_sign < 0 else bid[i]   # cross: cover at ask / sell at bid
                exit_role = "taker"
            else:
                exit_side = "ask" if pos_sign > 0 else "bid"   # passive: sell at ask / cover at bid
                exit_limit = ask[i] if pos_sign > 0 else bid[i]
                fr = simulate_maker_fill(exit_limit, exit_side, ts, trades,
                                         timeout=pd.Timedelta(minutes=2),
                                         queue_ahead=p.queue_frac * resting_size(ts))
                if fr.filled:
                    exit_px, exit_role = fr.fill_price, "maker"
                else:                                          # cross the spread (conservative)
                    exit_px = bid[i] if pos_sign > 0 else ask[i]
                    exit_role = "taker"
            gross = pos_sign * (exit_px - entry_px) * p.contracts
            fee = (fee_dollars(entry_px * p.contracts, role="maker",
                               scenario=p.fee_scenario, ticker=ticker)
                   + fee_dollars(exit_px * p.contracts, role=exit_role,
                                 scenario=p.fee_scenario, ticker=ticker))
            rows.append({"entry_ts": entry_ts, "exit_ts": ts, "sign": pos_sign,
                         "gross": gross, "fee": fee, "net": gross - fee,
                         "exit_role": exit_role, "aborted": aborted})
            in_pos = False

    tp = pd.DataFrame(rows)
    return {
        "params": p, "trade_pnl": tp,
        "summary": {
            "round_trips": len(tp),
            "attempts": attempts, "fills": fills,
            "fill_rate": fills / attempts if attempts else 0.0,
            "flow_blocked": flow_blocked, "oi_blocked": oi_blocked,
            "net": float(tp.net.sum()) if len(tp) else 0.0,
            "mean_net": float(tp.net.mean()) if len(tp) else 0.0,
            "hit_rate": float((tp.net > 0).mean()) if len(tp) else 0.0,
            "abort_frac": float(tp.aborted.mean()) if len(tp) else 0.0,
        },
    }


def sweep_grid() -> list[ImprovedParams]:
    """Bounded grid (45 trials incl. the naive baseline) — n_trials for deflation."""
    grid: list[ImprovedParams] = []
    # naive baseline for contrast (the known-negative config, abort off)
    grid.append(ImprovedParams(entry_k=2.5, min_abs_bps=5.0, offset_ticks=0,
                               flow_filter=False, oi_confirm=False, abort_bps=None))
    # main selective grid: flow filter always on
    for entry_k in (2.5, 3.5, 5.0):
        for offset in (0, 4, 10):
            for abort in (10.0, 20.0):
                for oi in (False, True):
                    grid.append(ImprovedParams(entry_k=entry_k, min_abs_bps=10.0,
                                               offset_ticks=offset, abort_bps=abort,
                                               flow_filter=True, oi_confirm=oi))
    # deeper-stretch variants
    for entry_k in (3.5, 5.0):
        for offset in (0, 10):
            for oi in (False, True):
                grid.append(ImprovedParams(entry_k=entry_k, min_abs_bps=20.0,
                                           offset_ticks=offset, abort_bps=20.0,
                                           flow_filter=True, oi_confirm=oi))
    return grid


def config_label(p: ImprovedParams) -> str:
    return (f"k{p.entry_k}/abs{p.min_abs_bps:.0f}/off{p.offset_ticks}"
            f"/abort{p.abort_bps if p.abort_bps is not None else '-'}"
            f"/flow{'Y' if p.flow_filter else 'N'}/oi{'Y' if p.oi_confirm else 'N'}")


def run_sweep(prep: Prep | None = None, *, ticker: str = "KXBTCPERP",
              asset: str = "BTC", min_trades: int = 5) -> dict:
    prep = prep or prepare(ticker, asset)
    grid = sweep_grid()
    n_trials = len(grid)
    results = []
    for p in grid:
        r = run_config(prep, p, ticker=ticker)
        s = r["summary"]
        row = {"label": config_label(p), **{k: s[k] for k in
               ("round_trips", "fill_rate", "net", "mean_net", "hit_rate", "abort_frac")}}
        if s["round_trips"] >= min_trades:
            rep = trade_significance_report(r["trade_pnl"]["net"],
                                            k=min(5, max(2, s["round_trips"] // 3)),
                                            n_trials=n_trials)
            row.update({"t_nw": rep["t_nw"], "frac_pos": rep["purged_cv"]["frac_positive"],
                        "dsr": rep["dsr"], "significant": rep["significant"]})
        else:
            row.update({"t_nw": np.nan, "frac_pos": np.nan, "dsr": np.nan,
                        "significant": False, "note": "insufficient trades"})
        results.append(row)
        logger.info("%s → n=%d net=%+.2f t_nw=%s", row["label"], row["round_trips"],
                    row["net"], f"{row['t_nw']:+.2f}" if not pd.isna(row["t_nw"]) else "n/a")
    df = pd.DataFrame(results)
    return {"n_trials": n_trials, "table": df}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", default="KXBTCPERP")
    ap.add_argument("--asset", default="BTC")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    out = run_sweep(ticker=args.ticker, asset=args.asset)
    df = out["table"]
    ranked = df.dropna(subset=["t_nw"]).sort_values("t_nw", ascending=False)
    print("=" * 100)
    print(f"Plan 01 SELECTIVE sweep — {out['n_trials']} configs (multiple-testing "
          f"deflation uses n_trials={out['n_trials']})")
    print("=" * 100)
    cols = ["label", "round_trips", "fill_rate", "net", "hit_rate", "abort_frac",
            "t_nw", "frac_pos", "dsr", "significant"]
    with pd.option_context("display.width", 200):
        print("TOP 5 by NW-t:")
        print(ranked.head(5)[cols].to_string(index=False))
        print("\nWORST:")
        print(ranked.tail(1)[cols].to_string(index=False))
        thin = df[df.t_nw.isna()]
        print(f"\n(insufficient-trade configs: {len(thin)})")
    any_sig = bool(ranked.significant.any()) if len(ranked) else False
    print("\nVERDICT:", "at least one config significant after deflation"
          if any_sig else "NOTHING significantly positive after deflation — "
          "Plan 01 not salvageable with selective passive execution on current data")
    outdir = SIGNALS_DIR / STRATEGY / "improved"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    df.to_csv(outdir / f"sweep_{stamp}.csv", index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
