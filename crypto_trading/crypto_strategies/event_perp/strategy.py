"""Plan 02 TRADEABLE expression — perp-leg convergence on the validated
fair-value-gap signal, with a fill-aware P&L backtest + live dry-run mode.

WHAT'S VALIDATED (and what this monetizes): gap = (implied_mean − perp_spot)/
perp_spot from the event-strip implied distribution predicts the PERP's forward
convergence (16-fold OOS IC +0.215, 100% folds positive, hit 52-56%). This
module trades the PERP leg in the convergence direction:

    gap_z > +entry_k  (implied mean ABOVE perp)  → LONG perp
    gap_z < −entry_k                             → SHORT perp
    exit when the SAME horizon's gap_z decays inside ±exit_k, or max_hold.

WHY THE EVENT-LEG HEDGE (dual_sleeve) STAYS DEFERRED: the strips' bins are
quoted WIDE (we measured tile Σask far above 1 — huge per-bin spreads), so
hedging in the event book eats multiples of the perp edge. The perp-leg
expression monetizes the validated IC directly; its honest cost is UNHEDGED
BTC/ETH delta for the holding period — size is capped accordingly and the
default bracket protects the tail.

Execution honesty (mirrors basis_meanrev/fill_aware.py — the proven pattern):
maker entry at the touch via simulate_maker_fill against the real recorded
trade tape (queue-aware, adverse-selection-exposed); if not filled within the
entry timeout the trade is MISSED. Exit passive-then-cross; the crossed exit
fills at the UNFAVORABLE touch (opposite side — not the optimistic same-side
price). Projected fees. Per-trade P&L → trade_stats significance.

CLI:
    … -m crypto_trading.crypto_strategies.event_perp.strategy backtest
        [--series KXBTC] [--entry-k 1.5] [--max-hold 60] [--sweep]
    … -m crypto_trading.crypto_strategies.event_perp.strategy signal
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict, dataclass, replace

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.backtest.fill_model import (simulate_maker_fill,
                                                              simulate_taker_fill)
from crypto_trading.crypto_common.config import PRICE_DATA, SIGNALS_DIR
from crypto_trading.crypto_common.costs import fee_dollars
from crypto_trading.crypto_common.io_jsonl import DailyJsonlWriter
from crypto_trading.crypto_common.loader import load_poll_market_stats, load_poll_trades
from crypto_trading.crypto_strategies.event_perp.backtest import (SERIES_TO_PERP,
                                                                  _implied_mean,
                                                                  _perp_spot_from_record,
                                                                  read_snapshots)
from crypto_trading.crypto_strategies.event_perp.signals.dislocation import rolling_z

logger = logging.getLogger(__name__)

STRATEGY = "event_perp"
CONFIG_PATH = __import__("pathlib").Path(__file__).with_name("config.yaml")
BRACKET_STATE_PATH = SIGNALS_DIR / STRATEGY / "brackets.json"


def load_config() -> dict:
    try:
        import yaml
        return yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except Exception:
        logger.warning("could not load %s — using code defaults", CONFIG_PATH)
        return {}


@dataclass(frozen=True)
class EventPerpParams:
    """Sweep surface. entry_k is in gap-z units (the validated signal's scale)."""
    entry_k: float = 1.5
    exit_k: float = 0.5
    zwin: int = 60                    # z window in snapshots, WITHIN horizon (PIT)
    max_hold_min: int = 60
    entry_timeout_min: int = 5        # snapshots are ~90s apart
    exit_timeout_min: int = 2
    contracts: int = 10
    queue_frac: float = 1.0


# ── signal builder ───────────────────────────────────────────────────────────

def build_gap_frame(series: str = "KXBTC", *, days: list[str] | None = None,
                    zwin: int = 60) -> pd.DataFrame:
    """Per-snapshot gap + WITHIN-HORIZON gap_z from the recorded strips.

    Columns: dt(index, UTC), close_time, implied_mean, perp_spot, gap, gap_z.
    The z is computed per close_time horizon (interleaved horizons otherwise
    fabricate mean-reversion — the 2026-07-10 bug; never regress this).
    """
    perp = SERIES_TO_PERP.get(series)
    rows = []
    for rec in read_snapshots(series, days=days):
        ts = rec.get("recv_ts")
        im = _implied_mean(rec)
        if ts is None or im is None:
            continue
        rows.append({"recv_ts": float(ts), "implied_mean": im,
                     "perp_spot": _perp_spot_from_record(rec),
                     "close_time": rec.get("close_time")})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("recv_ts").reset_index(drop=True)

    # poll-stats fallback for missing contemporaneous spot (rare)
    if df["perp_spot"].isna().any() and perp:
        try:
            stats = load_poll_market_stats(perp)
        except Exception:
            stats = pd.DataFrame()
        if len(stats) and "price" in stats:
            csize = float(stats["contract_size"].dropna().iloc[-1])
            st_ts = stats.index.view("int64") / 1e9
            st_px = stats["price"].to_numpy()
            for i in df.index[df["perp_spot"].isna()]:
                j = int(np.searchsorted(st_ts, df.at[i, "recv_ts"], side="right")) - 1
                if j >= 0 and csize > 0 and (df.at[i, "recv_ts"] - st_ts[j]) < 120:
                    df.at[i, "perp_spot"] = st_px[j] / csize
    df = df.dropna(subset=["perp_spot"]).reset_index(drop=True)
    if df.empty:
        return df

    df["gap"] = (df["implied_mean"] - df["perp_spot"]) / df["perp_spot"]
    df["gap_z"] = df.groupby("close_time", sort=False)["gap"].transform(
        lambda s: rolling_z(s, zwin))
    df.index = pd.to_datetime(df["recv_ts"], unit="s", utc=True)
    df.index.name = "dt"
    return df


# ── fill-aware backtest core (injectable → unit-testable) ────────────────────

def _backtest_loop(gap_frame: pd.DataFrame, touch: pd.DataFrame,
                   trades: pd.DataFrame, params: EventPerpParams, *,
                   ticker: str, fee_scenario: str = "projected",
                   entry_mask: np.ndarray | None = None) -> dict:
    """State machine over gap snapshots with fill-aware execution.

    ``touch``: 1-min bid/ask frame (real recorded touch). ``trades``: the tape.
    Entry maker-only at the touch (missed if unfilled); exit passive-then-cross,
    crossed exits fill at the UNFAVORABLE touch (honest).
    ``entry_mask``: optional bool array (len == gap_frame) — rows where entry is
    permitted; exits/decay logic unaffected (research conditioning hook).
    """
    entry_to = pd.Timedelta(minutes=params.entry_timeout_min)
    exit_passive_to = pd.Timedelta(minutes=params.exit_timeout_min)
    max_hold = pd.Timedelta(minutes=params.max_hold_min)

    def touch_at(ts, col):
        try:
            v = touch.loc[:ts, col].iloc[-1]
            return float(v) if pd.notna(v) else None
        except (KeyError, IndexError):
            return None

    def resting_size(ts):
        w = trades.loc[ts - pd.Timedelta(minutes=5):ts, "count"]
        return float(w.median()) if len(w) else 1.0

    trade_pnls = []
    attempted = filled_n = 0
    in_pos = False
    entry_px = entry_ts = pos_sign = entry_horizon = None
    needs_reset = False    # after a max-hold (non-decay) exit: block re-entry until
                           # the signal first decays inside ±exit_k — otherwise a
                           # stuck-elevated z churns entries and bleeds fees

    gz = gap_frame["gap_z"].to_numpy()
    horizons = gap_frame["close_time"].to_numpy()
    idx_list = list(gap_frame.index)

    def close_position(ts):
        nonlocal in_pos
        # passive exit posts at the favorable touch; cross pays the spread
        if pos_sign > 0:                       # close long: sell
            passive_px = touch_at(ts, "ask")
            cross_px = touch_at(ts, "bid")
            exit_side = "ask"
        else:                                  # close short: buy
            passive_px = touch_at(ts, "bid")
            cross_px = touch_at(ts, "ask")
            exit_side = "bid"
        if passive_px is None or cross_px is None:
            return False
        fr = simulate_maker_fill(passive_px, exit_side, ts, trades,
                                 timeout=exit_passive_to,
                                 queue_ahead=params.queue_frac * resting_size(ts))
        if fr.filled:
            exit_px, exit_role = fr.fill_price, "maker"
        else:
            # cross happens AFTER the failed passive window — price it at the
            # adverse touch AT that time, not the stale decision-time quote
            cross_ts = ts + exit_passive_to
            late_cross = touch_at(cross_ts, "bid" if pos_sign > 0 else "ask")
            fr = simulate_taker_fill(late_cross if late_cross is not None else cross_px,
                                     exit_side, cross_ts)
            exit_px, exit_role = fr.fill_price, "taker"
        gross = pos_sign * (exit_px - entry_px) * params.contracts
        fee = (fee_dollars(entry_px * params.contracts, role="maker",
                           scenario=fee_scenario, ticker=ticker)
               + fee_dollars(exit_px * params.contracts, role=exit_role,
                             scenario=fee_scenario, ticker=ticker))
        trade_pnls.append({"entry_ts": entry_ts, "exit_ts": ts, "sign": pos_sign,
                           "entry_px": entry_px, "exit_px": exit_px,
                           "gross": gross, "fee": fee, "net": gross - fee,
                           "exit_role": exit_role})
        in_pos = False
        return True

    for i, ts in enumerate(idx_list):
        z = gz[i]
        if not in_pos:
            if needs_reset:
                if np.isfinite(z) and abs(z) <= params.exit_k:
                    needs_reset = False        # signal reset — re-entry re-armed
                continue
            if entry_mask is not None and not entry_mask[i]:
                continue
            if not np.isfinite(z) or abs(z) < params.entry_k:
                continue
            want = 1 if z > 0 else -1          # +gap ⇒ perp cheap ⇒ long
            attempted += 1
            limit = touch_at(ts, "bid" if want > 0 else "ask")
            if limit is None:
                continue
            fr = simulate_maker_fill(limit, "bid" if want > 0 else "ask", ts, trades,
                                     timeout=entry_to,
                                     queue_ahead=params.queue_frac * resting_size(ts))
            if fr.filled:
                filled_n += 1
                in_pos = True
                entry_px, entry_ts, pos_sign = fr.fill_price, fr.fill_ts, want
                entry_horizon = horizons[i]
        else:
            same_horizon = horizons[i] == entry_horizon
            decayed = same_horizon and np.isfinite(z) and abs(z) <= params.exit_k
            timed_out = (ts - entry_ts) >= max_hold
            if decayed or timed_out:
                if close_position(ts) and timed_out and not decayed:
                    needs_reset = True         # stuck signal — wait for decay

    # force-close a dangling position at the last snapshot (mark discipline)
    if in_pos and idx_list:
        close_position(idx_list[-1])

    tp = pd.DataFrame(trade_pnls)
    return {
        "summary": {
            "ticker": ticker, "params": asdict(params),
            "snapshots": len(gap_frame),
            "entries_attempted": attempted, "entries_filled": filled_n,
            "fill_rate": filled_n / attempted if attempted else 0.0,
            "round_trips": len(tp),
            "net_pnl": float(tp.net.sum()) if len(tp) else 0.0,
            "mean_net_per_trade": float(tp.net.mean()) if len(tp) else 0.0,
            "hit_rate": float((tp.net > 0).mean()) if len(tp) else 0.0,
            "maker_exit_frac": float((tp.exit_role == "maker").mean()) if len(tp) else 0.0,
            "fee_scenario": fee_scenario,
        },
        "trade_pnl": tp,
    }


def run_fill_aware(series: str = "KXBTC", *, params: EventPerpParams = EventPerpParams(),
                   fee_scenario: str = "projected",
                   days: list[str] | None = None) -> dict:
    """Fill-aware P&L backtest of the perp-leg expression on recorded data."""
    perp = SERIES_TO_PERP.get(series)
    if perp is None:
        raise ValueError(f"no perp mapping for series {series!r}")
    gap = build_gap_frame(series, days=days, zwin=params.zwin)
    if gap.empty:
        return {"summary": {"error": f"no gap frame for {series}"}, "trade_pnl": pd.DataFrame()}
    stats = load_poll_market_stats(perp)
    # PIT: right/right — bar T = (T-1min, T], knowable at label time
    touch = (stats[["bid", "ask"]].dropna()
             .resample("1min", label="right", closed="right").last().ffill(limit=3))
    trades = load_poll_trades(perp).sort_index()
    if trades.empty:
        return {"summary": {"error": f"no trade tape for {perp}"}, "trade_pnl": pd.DataFrame()}
    out = _backtest_loop(gap, touch, trades, params, ticker=perp,
                         fee_scenario=fee_scenario)
    out["summary"]["series"] = series
    out["summary"]["span"] = [str(gap.index.min()), str(gap.index.max())]
    return out


# ── sweep ────────────────────────────────────────────────────────────────────

SWEEP_ENTRY_K = (1.0, 1.5, 2.0)
SWEEP_MAX_HOLD = (30, 60, 120)


def run_sweep(series_list=("KXBTC", "KXETH"), *, fee_scenario: str = "projected") -> dict:
    from crypto_trading.crypto_common.trade_stats import trade_significance_report
    n_trials = len(SWEEP_ENTRY_K) * len(SWEEP_MAX_HOLD) * len(series_list)
    rows = []
    for series in series_list:
        # build the expensive inputs ONCE per series; the sweep only varies the
        # state machine's thresholds (zwin fixed → gap frame reusable)
        perp = SERIES_TO_PERP[series]
        gap = build_gap_frame(series, zwin=EventPerpParams().zwin)
        if gap.empty:
            logger.warning("no gap frame for %s — skipping", series)
            continue
        stats = load_poll_market_stats(perp)
        touch = (stats[["bid", "ask"]].dropna()
                 .resample("1min", label="right", closed="right").last().ffill(limit=3))
        trades = load_poll_trades(perp).sort_index()
        for ek in SWEEP_ENTRY_K:
            for mh in SWEEP_MAX_HOLD:
                p = replace(EventPerpParams(), entry_k=ek, max_hold_min=mh)
                r = _backtest_loop(gap, touch, trades, p, ticker=perp,
                                   fee_scenario=fee_scenario)
                s = r["summary"]
                tp = r["trade_pnl"]
                row = {"series": series, "entry_k": ek, "max_hold_min": mh,
                       "n": s.get("round_trips", 0),
                       "fill_rate": round(s.get("fill_rate", 0.0), 3),
                       "net": round(s.get("net_pnl", 0.0), 4),
                       "hit": round(s.get("hit_rate", 0.0), 3),
                       "maker_exit": round(s.get("maker_exit_frac", 0.0), 3)}
                if len(tp) >= 5:
                    rep = trade_significance_report(tp["net"], k=min(5, len(tp)),
                                                    n_trials=n_trials)
                    row.update({"t_nw": round(rep["t_nw"], 2),
                                "frac_pos_folds": round(rep["purged_cv"]["frac_positive"], 2),
                                "dsr": round(rep["dsr"], 3),
                                "significant": rep["significant"]})
                else:
                    row.update({"t_nw": None, "frac_pos_folds": None, "dsr": None,
                                "significant": False})
                rows.append(row)
    return {"n_trials": n_trials, "fee_scenario": fee_scenario,
            "table": pd.DataFrame(rows)}


# ── live dry-run signal (mirrors basis strategy.py; all inert until gate) ────

def _latest_strip_record(series: str) -> dict | None:
    """Last parseable snapshot from today's (or the newest) markets jsonl."""
    import glob
    import gzip
    files = sorted(glob.glob(str(
        PRICE_DATA / "kalshi" / "event_strips" / "prod" / series / "markets" / "*")))
    for f in reversed(files):
        opener = gzip.open if f.endswith(".gz") else open
        last = None
        try:
            with opener(f, "rt") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        last = line
            if last:
                return json.loads(last)
        except Exception:
            continue
    return None


def live_signal(*, series: str = "KXBTC",
                params: EventPerpParams | None = None,
                equity: float = 1000.0) -> dict:
    from crypto_trading.crypto_common.bracket import Bracket, BracketMonitor
    from crypto_trading.crypto_common.costs import decimal_book_to_levels
    from crypto_trading.crypto_common.execution import ExecutionRouter, Order
    from crypto_trading.crypto_common.kalshi.rest_margin import KalshiMarginClient
    from crypto_trading.crypto_common.risk_kill import GuardConfig, RiskKill, RiskState
    from crypto_trading.crypto_common.sizing import SizingConfig, size_position

    cfg = load_config()
    if params is None:
        params = EventPerpParams(
            entry_k=float(cfg.get("signal", {}).get("entry_k", 1.5)),
            exit_k=float(cfg.get("signal", {}).get("exit_k", 0.5)),
            max_hold_min=int(cfg.get("signal", {}).get("max_hold_min", 60)),
            contracts=int(cfg.get("signal", {}).get("contracts", 10)))
    perp = SERIES_TO_PERP.get(series)
    now = time.time()
    result: dict = {"ts": now, "series": series, "perp": perp}

    rec = _latest_strip_record(series)
    im = _implied_mean(rec) if rec else None
    margin = KalshiMarginClient(env="prod")
    m = margin.market(perp)
    book = margin.orderbook(perp)
    bids, asks = decimal_book_to_levels(book)
    csize = float(m.get("contract_size") or 0)
    bb = max((p for p, s in bids), default=None)
    ba = min((p for p, s in asks), default=None)
    if im is None or bb is None or ba is None or not csize:
        result["status"] = "no-data"
        return result

    perp_spot = (bb + ba) / 2 / csize
    gap_now = (im - perp_spot) / perp_spot
    # z vs the entry horizon's recorded history + current point (PIT)
    hist = build_gap_frame(series, zwin=params.zwin)
    horizon = rec.get("close_time")
    h = hist[hist["close_time"] == horizon]["gap"] if len(hist) else pd.Series(dtype=float)
    g_all = pd.concat([h, pd.Series([gap_now])], ignore_index=True)
    z_now = float(rolling_z(g_all, params.zwin).iloc[-1])

    # strip staleness: the snapshot must be recent for the gap to mean anything
    strip_age_s = now - float(rec.get("recv_ts") or 0)
    result.update({"implied_mean": im, "perp_spot": perp_spot,
                   "gap_bps": 1e4 * gap_now, "gap_z": z_now,
                   "horizon": horizon, "strip_age_s": round(strip_age_s, 1),
                   "spread_bps": 1e4 * (ba - bb) / ((ba + bb) / 2)})
    DailyJsonlWriter(SIGNALS_DIR / STRATEGY / "decay_tracker").write(".", result)

    desired = 0
    if np.isfinite(z_now) and strip_age_s < 300:
        if z_now >= params.entry_k:
            desired = +1                      # perp cheap vs implied → long
        elif z_now <= -params.entry_k:
            desired = -1
    result["desired"] = desired
    if desired == 0:
        result["status"] = "flat"
        return result

    rv = float(h.std() * np.sqrt(365 * 24 * 3600 / 90)) if len(h) > 10 else 0.5
    dec = size_position(equity=equity, contract_price=(ba + bb) / 2,
                        realized_vol_annual=rv, cfg=SizingConfig())
    kill = RiskKill(STRATEGY, GuardConfig())
    ok, why = kill.pre_trade_ok(RiskState(equity_sod=equity, equity_now=equity,
                                          last_index_ts=now, last_book_ts=now),
                                order_contracts=dec.contracts)
    result.update({"size": dec.contracts, "size_binding": dec.binding,
                   "risk_ok": ok, "risk_why": why})
    if ok and dec.contracts > 0:
        subaccount = int(cfg.get("subaccount", 0))
        side = "bid" if desired > 0 else "ask"
        px = bb if desired > 0 else ba        # post at the touch (maker)
        router = ExecutionRouter(STRATEGY)
        rec_o = router.submit(Order(perp, side, dec.contracts, px, post_only=True,
                                    subaccount=subaccount))
        router.close()
        result["order"] = rec_o["status"]
        br = cfg.get("bracket") or {}
        if br.get("enabled", True):
            tp_pct = float(br.get("take_profit_pct", 0.01))
            sl_pct = float(br.get("stop_loss_pct", 0.02))
            long = desired > 0
            bracket = Bracket(perp, side, dec.contracts, entry_price=px,
                              subaccount=subaccount,
                              take_profit=px * (1 + tp_pct) if long else px * (1 - tp_pct),
                              stop_loss=px * (1 - sl_pct) if long else px * (1 + sl_pct))
            BracketMonitor(ExecutionRouter(STRATEGY),
                           state_path=BRACKET_STATE_PATH).arm(bracket)
            result["bracket"] = {"armed": True, "take_profit": bracket.take_profit,
                                 "stop_loss": bracket.stop_loss}
    result["status"] = "signal"
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["backtest", "signal"])
    ap.add_argument("--series", default="KXBTC")
    ap.add_argument("--entry-k", type=float, default=1.5)
    ap.add_argument("--max-hold", type=int, default=60)
    ap.add_argument("--fees", default="projected", choices=["zero", "projected"])
    ap.add_argument("--sweep", action="store_true", help="run the full sweep grid")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.mode == "signal":
        print(json.dumps(live_signal(series=args.series), indent=2, default=str))
        return 0

    out_dir = SIGNALS_DIR / STRATEGY / "fill_aware"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    if args.sweep:
        res = run_sweep(fee_scenario=args.fees)
        print(f"n_trials={res['n_trials']} fees={res['fee_scenario']}")
        print(res["table"].to_string(index=False))
        res["table"].to_csv(out_dir / f"sweep_{stamp}.csv", index=False)
    else:
        p = replace(EventPerpParams(), entry_k=args.entry_k, max_hold_min=args.max_hold)
        r = run_fill_aware(args.series, params=p, fee_scenario=args.fees)
        print(json.dumps(r["summary"], indent=2, default=str))
        if len(r["trade_pnl"]):
            r["trade_pnl"].to_csv(out_dir / f"trades_{stamp}.csv", index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
