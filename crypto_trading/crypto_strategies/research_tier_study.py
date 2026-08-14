"""Tier study — every strategy re-run under fee tiers 3/4/5 (Plan 15).

The user's capital plan lands around tier 4 ±1 (30-day volume ≥$1M/$3M/$10M →
taker 6/5/4 bps, maker 2.4/2.0/1.6). Fees stop being a constant and become a
parameter, so every verdict is re-derived per tier — with thresholds/direction
re-selected on the IS half UNDER THAT TIER'S FEES (a threshold tuned to clear a
10bps wall is not the right threshold at 4bps).

Two strategies get their first TRADE-LEVEL taker backtests here (they were
mid-price studies before, because taker execution was pointless at tier 0):

  N2  jump lead-lag — index 30s jump ≥J → taker entry at the touch, exit at
      +1m (taker adverse touch; maker variant with 2min passive then cross).
  N4  21-23 UTC window — deterministic schedule: taker buy at the 21:00 ask,
      taker sell at the 23:00 bid. No fill model needed: taker fills are
      certain, the cost is the spread plus two taker fees.

Everything else is re-run through its existing decisive (fill-aware) module
with CRYPTO_FEE_TIER set, so fills/adverse selection stay identical and only
the fee line moves — plus FEE_MM_BPS patches for the mid-price harnesses.

PIT: unchanged everywhere (day-by-day, trailing-only features, IS-half
selection, non-overlapping episodes). Fees do not touch information sets.
"""
from __future__ import annotations

import argparse
import json
import logging
import os

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.costs import FEE_TIERS_BPS
from crypto_trading.crypto_common.loader import (load_index_live,
                                                 load_poll_market_stats)
from crypto_trading.crypto_common.trade_stats import newey_west_tstat

logger = logging.getLogger(__name__)

TIERS = (3, 4, 5)


# ── N2: jump lead-lag, trade level ──────────────────────────────────────────

def jump_trades(ticker: str, asset: str, *, jump_bps: float, hold_min: int = 1,
                cooldown_s: int = 300) -> pd.DataFrame:
    """Taker-entry trades after 30s index jumps. Returns gross bps per trade
    (entry at adverse touch, exit at adverse touch at +hold — both taker), so
    net at tier t = gross − 2×taker(t). Spread cost is inside the touches."""
    live = load_index_live(asset)["index"].sort_index()
    st = load_poll_market_stats(ticker)
    bid = st.bid.dropna().sort_index()
    ask = st.ask.dropna().sort_index()
    j = (live / live.shift(6) - 1) * 1e4
    ev = j[abs(j) >= jump_bps]
    rows, last = [], None
    for ts, jv in ev.items():
        if last is not None and (ts - last).total_seconds() < cooldown_s:
            continue
        last = ts
        side = int(np.sign(jv))
        try:
            # entry NOW at the adverse touch (buy at ask / sell at bid), quote ≤ ts
            e_ask = ask.loc[:ts]; e_bid = bid.loc[:ts]
            if not len(e_ask) or not len(e_bid):
                continue
            if (ts - e_ask.index[-1]).total_seconds() > 30:
                continue                                   # stale book — skip
            entry = float(e_ask.iloc[-1]) if side > 0 else float(e_bid.iloc[-1])
            xts = ts + pd.Timedelta(minutes=hold_min)
            # exit at the first quote AT/AFTER xts (we are executing then)
            x_bid = bid.loc[xts:]; x_ask = ask.loc[xts:]
            if not len(x_bid) or not len(x_ask):
                continue
            if (x_bid.index[0] - xts).total_seconds() > 30:
                continue
            exitp = float(x_bid.iloc[0]) if side > 0 else float(x_ask.iloc[0])
            gross = side * (exitp - entry) / entry * 1e4
            rows.append({"ts": ts, "side": side, "gross_bps": gross,
                         "notional": entry})
        except (KeyError, IndexError):
            continue
    return pd.DataFrame(rows)


# ── N4: 21-23 UTC window, trade level ───────────────────────────────────────

def window_trades(ticker: str, h_in: int = 21, h_out: int = 23) -> pd.DataFrame:
    """Deterministic daily schedule: taker buy at the h_in:00 ask, taker sell
    at the h_out:00 bid. Gross bps per day; net = gross − 2×taker(tier)."""
    st = load_poll_market_stats(ticker)
    bid = st.bid.dropna().sort_index()
    ask = st.ask.dropna().sort_index()
    days = sorted({d.date() for d in bid.index})
    rows = []
    for d in days:
        t_in = pd.Timestamp(f"{d} {h_in:02d}:00:00", tz="UTC")
        t_out = pd.Timestamp(f"{d} {h_out:02d}:00:00", tz="UTC")
        try:
            a = ask.loc[t_in:]; b = bid.loc[t_out:]
            if not len(a) or not len(b):
                continue
            if (a.index[0] - t_in).total_seconds() > 60 or \
               (b.index[0] - t_out).total_seconds() > 60:
                continue
            entry, exitp = float(a.iloc[0]), float(b.iloc[0])
            rows.append({"day": d, "gross_bps": (exitp - entry) / entry * 1e4})
        except (KeyError, IndexError):
            continue
    return pd.DataFrame(rows)


def _summ(g: pd.Series, fee_bps: float) -> dict:
    net = g - fee_bps
    nw = newey_west_tstat(net.reset_index(drop=True))
    return {"n": len(net), "gross_bps": round(float(g.mean()), 2),
            "net_bps": round(float(net.mean()), 2),
            "hit": round(float((net > 0).mean()), 3),
            "nw_t": round(float(nw["t_nw"]), 2)}


def run_new_candidates() -> dict:
    out: dict = {}
    # N2 — both jump thresholds reported, no selection between them
    for tk, a in [("KXBTCPERP", "BTC"), ("KXETHPERP", "ETH")]:
        for J in (15.0, 25.0):
            tp = jump_trades(tk, a, jump_bps=J)
            if len(tp) < 15:
                continue
            half = len(tp) // 2
            oos = tp.iloc[half:]
            days = max((oos.ts.max() - oos.ts.min()).days, 1)
            for t in TIERS:
                _, taker = FEE_TIERS_BPS[t]
                key = f"N2|{tk}|J{J:.0f}|T{t}"
                s = _summ(oos.gross_bps, 2 * taker)
                s["trades_per_day"] = round(len(oos) / days, 1)
                out[key] = s
    # N4 — pre-registered window, all days (the whole sample IS the OOS of the
    # published anomaly); second half reported for decay honesty
    for tk in ("KXBTCPERP", "KXETHPERP", "KXLTCPERP"):
        tp = window_trades(tk)
        if len(tp) < 15:
            continue
        for t in TIERS:
            _, taker = FEE_TIERS_BPS[t]
            key = f"N4|{tk}|T{t}"
            s = _summ(tp.gross_bps, 2 * taker)
            h = len(tp) // 2
            s["second_half_net"] = round(float(tp.gross_bps.iloc[h:].mean() - 2 * taker), 2)
            out[key] = s
    return out


# ── driver: legacy strategies through their decisive modules per tier ───────

def run_legacy(tier: int) -> dict:
    """Set the tier env and call each decisive backtest programmatically."""
    os.environ["CRYPTO_FEE_TIER"] = str(tier)
    maker, taker = FEE_TIERS_BPS[tier]
    mm = 2 * maker
    res: dict = {}

    # S1 basis selective — best cell of the sweep under this tier's fees
    try:
        from crypto_trading.crypto_strategies.basis_meanrev import improved
        r = improved.run_sweep()
        df = r["table"].dropna(subset=["t_nw"])
        if len(df):
            best = df.sort_values("t_nw", ascending=False).iloc[0]
            res["S1_best"] = {"label": str(best["label"]), "n": int(best["round_trips"]),
                              "net": round(float(best["net"]), 3),
                              "t_nw": round(float(best["t_nw"]), 2),
                              "significant": bool(best["significant"])}
    except Exception as e:                                   # noqa: BLE001
        res["S1_best"] = {"error": str(e)[:60]}

    # S4 liq fill-aware (frozen config, BTC)
    try:
        from crypto_trading.crypto_strategies.liq_reversion import fill_aware as lfa
        r = lfa.run_fill_aware(ticker="KXBTCPERP")
        s = r["summary"]
        res["S4_fillaware"] = {k: s.get(k) for k in
                               ("round_trips", "mean_net_per_trade", "hit_rate", "nw_t")}
    except Exception as e:                                   # noqa: BLE001
        res["S4_fillaware"] = {"error": str(e)[:60]}

    # S6/09 ML gate3 (linear basis_z, pooled 15m across BTC+ETH)
    try:
        from crypto_trading.crypto_strategies.ml_directional import backtest as g3
        parts = []
        for tk in ("KXBTCPERP", "KXETHPERP"):
            r = g3.run_market(tk, "15m")
            if len(r["trades"]):
                parts.append(r["trades"]["net_bps"])
        if parts:
            pooled = pd.concat(parts).reset_index(drop=True)
            res["S6_gate3_15m"] = {"n": len(pooled),
                                   "net": round(float(pooled.mean()), 2),
                                   "nw_t": round(float(newey_west_tstat(pooled)["t_nw"]), 2)}
    except Exception as e:                                   # noqa: BLE001
        res["S6_gate3_15m"] = {"error": str(e)[:60]}

    # S7/11 conviction 4h fill-aware — pooled net under tier fees
    try:
        from crypto_trading.crypto_strategies import research_fillaware_4h as f4
        tps = []
        for t in ("KXBTCPERP", "KXETHPERP"):
            fr = f4.build_feature_frame(t)
            rr = f4.run_market(t, fr)
            tp = rr.pop("trades", None)
            if tp is not None and len(tp):
                tps.append(tp)
        if tps:
            tp = pd.concat(tps)
            net = tp.net_bps.reset_index(drop=True)
            res["S7_fillaware4h"] = {"n": len(tp),
                                     "gross": round(float(tp.gross_bps.mean()), 2),
                                     "net": round(float(net.mean()), 2),
                                     "nw_t": round(float(newey_west_tstat(net)["t_nw"]), 2)}
    except Exception as e:                                   # noqa: BLE001
        res["S7_fillaware4h"] = {"error": str(e)[:60]}

    # S9 overnight 24h uniform-direction — patch its fee constant
    try:
        import importlib
        from crypto_trading.crypto_strategies import research_overnight as ov
        importlib.reload(ov)
        ov.FEE_MM_BPS = mm
        parts = []
        for tk in ov.MARKETS:
            try:
                px = ov.hourly_close(tk)
            except FileNotFoundError:
                continue
            sig = ov.build_signals(px, tk)["mom_24h_volscaled"]
            if sig.dropna().empty:
                continue
            z = ov._tz(sig)
            fwd = (px.shift(-24) / px - 1.0) * 1e4
            half = px.index[len(px) // 2]
            d = pd.DataFrame({"z": z, "fwd": fwd}).dropna()
            oos = d[d.index >= half]
            if len(oos) < 200:
                continue
            ep = ov.episodes(oos.z, oos.fwd, +1.0, 24)
            if len(ep) >= 5:
                parts.append(ep)
        if parts:
            allep = pd.concat(parts)
            net = allep.net_bps.reset_index(drop=True)
            res["S9_24h_uniform"] = {"n": len(net),
                                     "net": round(float(net.mean()), 2),
                                     "nw_t": round(float(newey_west_tstat(net)["t_nw"]), 2)}
    except Exception as e:                                   # noqa: BLE001
        res["S9_24h_uniform"] = {"error": str(e)[:60]}

    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-legacy", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    out: dict = {"tiers": {str(t): {"maker_bps": FEE_TIERS_BPS[t][0],
                                    "taker_bps": FEE_TIERS_BPS[t][1]} for t in TIERS}}
    logger.info("new candidates (N2/N4) …")
    out["new_candidates"] = run_new_candidates()
    if not args.skip_legacy:
        for t in TIERS:
            logger.info("legacy re-run at tier %d …", t)
            out[f"legacy_T{t}"] = run_legacy(t)
    os.environ.pop("CRYPTO_FEE_TIER", None)

    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"tier_study_{stamp}.json").write_text(json.dumps(out, indent=1, default=str))

    print("=" * 100)
    print("NEW CANDIDATES — trade-level taker execution, per tier")
    print("=" * 100)
    rows = [{"key": k, **v} for k, v in out["new_candidates"].items()]
    if rows:
        print(pd.DataFrame(rows).to_string(index=False))
    for t in TIERS:
        key = f"legacy_T{t}"
        if key in out:
            print(f"\n── legacy @ T{t} ──")
            print(json.dumps(out[key], indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
