"""Fill-aware execution of the 4h conviction signal — the decisive gate (§H).

The five-gate adjudication passed, but every number in it assumed execution at
the MID. That assumption is doing all the work: the positive result was carried
by KXNEARPERP (+62.9bps on 12 episodes), the market with the WIDEST spread
(7.4bps median) — precisely where a mid-price fill is most fictional. Harvey-Liu-
Zhu's bar for a factor-zoo survivor is t>3.0; our NW-t was 1.29. And the regime
split came out INVERTED versus Nagel (2012): the edge sat in LOW volatility,
where a liquidity-provision premium should be weakest — the signature of stale
quotes, not of a risk premium.

So this module replaces the mid-price fiction with the recorded tape:

  ENTRY  post_only at the touch on our side; filled only if the tape later
         trades through it (queue-aware, `simulate_maker_fill`). No fill → the
         trade simply does not happen, and that missed opportunity is counted.
  EXIT   at the 4h mark: passive at the touch for 2 minutes, else cross at the
         ADVERSE touch at cross time (never the stale favourable quote).
  FEES   maker 5bps entry; maker or taker exit per what actually happened.

Also runs the literature's priority-6 diagnostic: MARKOUT on our maker fills
(mid drift at +30s/+2m/+5m after the fill, signed by our side). Negative markout
= we are being adversely selected, i.e. we fill exactly when the market is about
to run us over — the mechanism that turns a mid-price edge into a real loss.
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
from crypto_trading.crypto_common.loader import load_poll_market_stats, load_poll_trades
from crypto_trading.crypto_common.trade_stats import newey_west_tstat
from crypto_trading.crypto_strategies.ml_directional.features import (FEATURES, GRID,
                                                                      build_feature_frame)
from crypto_trading.crypto_strategies.research_adjudicate_4h import _tz
from crypto_trading.crypto_strategies.research_horizon_atlas import (ALT_FEATURES,
                                                                    ALT_MARKETS,
                                                                    FULL_MARKETS,
                                                                    HORIZON_STEPS,
                                                                    build_alt_frame)

logger = logging.getLogger(__name__)

HORIZON = "4h"
ENTRY_THRESH = 1.0
CLIP = 3.0
ENTRY_TIMEOUT_MIN = 15
EXIT_PASSIVE_MIN = 2
CONTRACTS = 10
MARKOUT_HORIZONS = {"30s": 30, "2m": 120, "5m": 300}


def signal_series(ticker: str, frame: pd.DataFrame):
    """OOS-half conviction score (IS-fixed directions and scale) + fwd returns."""
    feats = [f for f in (FEATURES if ticker in FULL_MARKETS else ALT_FEATURES)
             if f in frame.columns and not frame[f].dropna().empty]
    if len(feats) < 4:
        return None
    k = HORIZON_STEPS[HORIZON]
    mark = frame["mark_mid"]
    fwd = (mark.shift(-k) / mark - 1.0) * 1e4
    zs = pd.DataFrame({f: _tz(frame[f]) for f in feats})
    base = pd.concat([zs, fwd.rename("fwd")], axis=1).dropna()
    if len(base) < 500:
        return None
    split = base.index[len(base) // 2]
    is_d, oos_d = base[base.index < split], base[base.index >= split]
    if len(is_d) < 200 or len(oos_d) < 200:
        return None
    dirs = {}
    for f in feats:
        ic = is_d[f].corr(is_d.fwd, method="spearman")
        dirs[f] = 0.0 if (pd.isna(ic) or ic == 0) else float(np.sign(ic))
    live = [f for f in feats if dirs[f] != 0]
    if len(live) < 4:
        return None

    def conv(df):
        return pd.DataFrame({f: dirs[f] * df[f].clip(-CLIP, CLIP) / CLIP
                             for f in live}).mean(axis=1)

    sd_is = float(conv(is_d).std(ddof=0)) or 1e-9
    return conv(oos_d) / sd_is, oos_d.index, k


def run_market(ticker: str, frame: pd.DataFrame) -> dict:
    sig = signal_series(ticker, frame)
    if sig is None:
        return {}
    score, idx, k = sig
    stats = load_poll_market_stats(ticker)
    trades = load_poll_trades(ticker).sort_index()
    if stats.empty or trades.empty:
        return {}
    touch = (stats[["bid", "ask"]].dropna()
             .resample(GRID, label="right", closed="right").last().ffill(limit=3))
    mid = ((stats.bid + stats.ask) / 2.0).dropna()

    def touch_at(ts, col):
        try:
            v = touch.loc[:ts, col].iloc[-1]
            return float(v) if pd.notna(v) else None
        except (KeyError, IndexError):
            return None

    def mid_at(ts):
        try:
            return float(mid.loc[:ts].iloc[-1])
        except (KeyError, IndexError):
            return None

    def resting(ts):
        w = trades.loc[ts - pd.Timedelta(minutes=5):ts, "count"]
        return float(w.median()) if len(w) else 1.0

    hold = pd.Timedelta(GRID) * k
    entry_to = pd.Timedelta(minutes=ENTRY_TIMEOUT_MIN)
    rows, attempted, filled, open_until = [], 0, 0, None
    for ts, s in score.dropna().items():
        if abs(s) < ENTRY_THRESH or (open_until is not None and ts < open_until):
            continue
        side_sign = int(np.sign(s))
        attempted += 1
        entry_side = "bid" if side_sign > 0 else "ask"
        limit = touch_at(ts, entry_side)
        if limit is None:
            continue
        fr = simulate_maker_fill(limit, entry_side, ts, trades, timeout=entry_to,
                                 queue_ahead=resting(ts))
        if not fr.filled:
            open_until = ts + entry_to          # we were tied up trying
            continue
        filled += 1
        entry_px, entry_ts = fr.fill_price, fr.fill_ts
        exit_ts = ts + hold
        exit_side = "ask" if side_sign > 0 else "bid"
        exit_limit = touch_at(exit_ts, exit_side)
        if exit_limit is None:
            continue
        efr = simulate_maker_fill(exit_limit, exit_side, exit_ts, trades,
                                  timeout=pd.Timedelta(minutes=EXIT_PASSIVE_MIN),
                                  queue_ahead=resting(exit_ts))
        exit_role = "maker"
        if efr.filled:
            exit_px = efr.fill_price
        else:
            cross_ts = exit_ts + pd.Timedelta(minutes=EXIT_PASSIVE_MIN)
            adverse = touch_at(cross_ts, "bid" if side_sign > 0 else "ask")
            exit_px = adverse if adverse is not None else exit_limit
            exit_role = "taker"
        gross = side_sign * (exit_px - entry_px) * CONTRACTS
        fee = (fee_dollars(entry_px * CONTRACTS, role="maker", scenario="projected",
                           ticker=ticker)
               + fee_dollars(exit_px * CONTRACTS, role=exit_role, scenario="projected",
                             ticker=ticker))
        notional = entry_px * CONTRACTS
        rec = {"ts": ts, "ticker": ticker, "side": side_sign,
               "entry_ts": entry_ts, "entry_px": entry_px, "exit_px": exit_px,
               "gross": gross, "fee": fee, "net": gross - fee,
               "net_bps": 1e4 * (gross - fee) / notional if notional else np.nan,
               "gross_bps": 1e4 * gross / notional if notional else np.nan,
               "exit_role": exit_role}
        # markout on the maker fill (adverse-selection diagnostic)
        m0 = mid_at(entry_ts)
        for lbl, secs in MARKOUT_HORIZONS.items():
            m1 = mid_at(entry_ts + pd.Timedelta(seconds=secs))
            rec[f"markout_{lbl}_bps"] = (1e4 * side_sign * (m1 - m0) / m0
                                         if (m0 and m1) else np.nan)
        rows.append(rec)
        open_until = exit_ts
    if not rows:
        return {"ticker": ticker, "attempted": attempted, "filled": 0}
    tp = pd.DataFrame(rows)
    return {"ticker": ticker, "attempted": attempted, "filled": filled,
            "fill_rate": round(filled / attempted, 3) if attempted else 0.0,
            "trades": tp}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    all_tp, per_mkt = [], []
    for t in FULL_MARKETS + ALT_MARKETS:
        try:
            frame = build_feature_frame(t) if t in FULL_MARKETS else build_alt_frame(t)
            r = run_market(t, frame)
        except Exception as e:                              # noqa: BLE001
            logger.warning("%s: %s", t, str(e)[:70])
            continue
        if not r:
            continue
        tp = r.pop("trades", None)
        if tp is not None and len(tp):
            all_tp.append(tp)
            r.update({"n": len(tp), "mean_net_bps": round(float(tp.net_bps.mean()), 2),
                      "mean_gross_bps": round(float(tp.gross_bps.mean()), 2),
                      "hit": round(float((tp.net > 0).mean()), 3)})
        per_mkt.append(r)
        logger.info("%s done", t)

    print("=" * 104)
    print("FILL-AWARE 4h CONVICTION — real maker fills on the recorded tape")
    print("=" * 104)
    pm = pd.DataFrame(per_mkt)
    print(pm.to_string(index=False))
    if not all_tp:
        print("\nno fills at all — the signal cannot be executed passively")
        return 0
    tp = pd.concat(all_tp).sort_values("ts")
    net = tp.net_bps.reset_index(drop=True)
    nw = newey_west_tstat(net)
    print(f"\nPOOLED: n={len(tp)} fills | fill-rate "
          f"{tp.shape[0] / max(sum(r.get('attempted', 0) for r in per_mkt), 1):.1%}")
    print(f"  GROSS {tp.gross_bps.mean():+.2f} bps | NET {net.mean():+.2f} bps "
          f"| median {net.median():+.2f} | hit {(tp.net > 0).mean():.1%} "
          f"| NW-t {nw['t_nw']:+.2f}")
    print(f"  maker-exit fraction: {(tp.exit_role == 'maker').mean():.1%}")

    print("\nMARKOUT ON OUR MAKER FILLS (adverse selection; negative = we are run over):")
    mk = {lbl: round(float(tp[f"markout_{lbl}_bps"].mean()), 2) for lbl in MARKOUT_HORIZONS}
    for lbl, v in mk.items():
        print(f"  +{lbl:3}  {v:+.2f} bps")
    print("  → " + ("我们的被动成交被逆向选择(填进去就被推着走)"
                    if min(mk.values()) < -0.5 else
                    "markout 未显示严重逆向选择"))

    print("\nPER-MARKET NET (fill-aware):")
    if "mean_net_bps" in pm.columns:
        print(pm[["ticker", "attempted", "filled", "fill_rate", "n",
                  "mean_gross_bps", "mean_net_bps", "hit"]].to_string(index=False))

    verdict = (net.mean() > 0 and nw["t_nw"] >= 2.0)
    print("\n" + "=" * 104)
    print("VERDICT: " + ("SURVIVES fill-aware execution" if verdict else
                         "DIES under fill-aware execution — the mid-price edge is not obtainable"))
    print("=" * 104)

    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"fillaware_4h_{stamp}.json").write_text(json.dumps(
        {"per_market": per_mkt, "pooled": {"n": len(tp),
         "gross_bps": float(tp.gross_bps.mean()), "net_bps": float(net.mean()),
         "nw_t": float(nw["t_nw"]), "markout": mk}}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
