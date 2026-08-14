"""Chronos (time-series foundation model) as a directional predictor — §I.

Everything hand-crafted has failed: the 1816-cell feature grid had PBO 0.332 and
the IS-top decile beat the field by only +0.61bps, while the fee wall is 10bps
and measured adverse selection is another ~3.9bps. The open question the user
raised is whether a PRETRAINED foundation model — which has seen billions of
series and is not fitted by us at all — extracts structure our features cannot.

That hypothesis deserves a real test, not a dismissal, because Chronos is
zero-shot: there is no in-sample fitting for it to overfit with. If it fails, the
failure is informative in a way ours was not.

Design (PIT-clean by construction):

  * ROLLING CONTEXTS. At each grid point t the context is mark_mid over
    (t-L, t] — a trailing window, nothing after t. Contexts are batched for
    speed, but each is independently a trailing window, so batching cannot leak.
  * SIGNAL. Chronos returns quantiles of the terminal price at t+h. The signal
    is the median predicted move in bps; the q90-q10 width is its confidence.
  * NO-TRADE BAND (Gârleanu-Pedersen). Never trade on a bare sign: trade only
    when |predicted move| clears a threshold. Absolute bands (10/20/30/40bps,
    the economically meaningful form) AND conviction quantiles (top 20/10/5% of
    |pred|, needed because Chronos forecasts are conservative — its |pred|
    median is ~12bps against a ~32bps median actual move, so absolute bands
    alone would starve it of trades and understate it).
  * IS-FIXED POLARITY. Chronos is zero-shot, so nothing forbids it from being
    systematically anti-predictive on this venue; the first pass measured
    IC −0.07. The sign is therefore fixed on the IS half and applied to OOS —
    the same discipline the feature-grid studies use, and the only fair test.
  * EPISODES. Non-overlapping (cooldown = the horizon), so returns are
    independent draws.
  * THREE LAYERS. (a) mid-price economics, (b) FILL-AWARE on the recorded tape
    (queue-aware maker fills, adverse-touch crossing), (c) markout diagnostic.
  * BASELINES. Identical trading rule driven by random / momentum / reversal
    signals. Chronos only counts if it beats all three.

The report separates two very different failure modes: predictions that are
WRONG, versus predictions that are RIGHT but smaller than the cost of acting.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time

import numpy as np
import pandas as pd
import torch

from crypto_trading.crypto_common.backtest.fill_model import simulate_maker_fill
from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.costs import fee_dollars
from crypto_trading.crypto_common.loader import load_poll_market_stats, load_poll_trades
from crypto_trading.crypto_common.trade_stats import newey_west_tstat
from crypto_trading.crypto_strategies.ml_directional.features import (GRID,
                                                                      build_feature_frame)
from crypto_trading.crypto_strategies.research_horizon_atlas import (ALT_MARKETS,
                                                                    FULL_MARKETS,
                                                                    build_alt_frame)

logger = logging.getLogger(__name__)

MODEL = os.environ.get("CHRONOS_MODEL", "amazon/chronos-bolt-small")
QUANTILES = [0.1, 0.25, 0.5, 0.75, 0.9]
BATCH = 256
CONTEXT_LENS = (256, 512)
HORIZON_STEPS = {"2h": 24, "4h": 48}
THRESHOLDS_BPS = (10.0, 20.0, 30.0, 40.0)
FEE_MM_BPS = 10.0
ENTRY_TIMEOUT_MIN = 15
EXIT_PASSIVE_MIN = 2
CONTRACTS = 10
MARKOUT_HORIZONS = {"30s": 30, "2m": 120, "5m": 300}


# ── Chronos rolling forecast ────────────────────────────────────────────────

def _pipeline():
    from chronos import BaseChronosPipeline
    return BaseChronosPipeline.from_pretrained(MODEL, device_map="cpu",
                                               dtype=torch.float32)


def rolling_forecast(prices: pd.Series, *, ctx_len: int, horizon: int,
                     mode: str = "price", pipe=None) -> pd.DataFrame:
    """Predicted terminal move at each grid point, using ONLY data ≤ t.

    ``mode='price'``  — context is the price level; the signal is the median
    predicted terminal price versus the last observed price.
    ``mode='return'`` — context is the 5-min return series in bps; the signal is
    the summed mean forecast (the expectation of the cumulative move, which is
    exact under summation in a way a sum of medians would not be).
    """
    pipe = pipe or _pipeline()
    px = prices.dropna()
    if len(px) < ctx_len + horizon + 10:
        return pd.DataFrame()
    vals = px.to_numpy(dtype=np.float32)
    idx = px.index

    if mode == "return":
        series = np.diff(np.log(vals)) * 1e4          # bps log-returns
        base_off = 1                                   # series[i] ends at idx[i+1]
    else:
        series = vals
        base_off = 0

    # decision points: every grid point with a full context and a full label
    starts = range(ctx_len, len(series) - horizon)
    contexts, dec_idx, last_px = [], [], []
    for i in starts:
        contexts.append(torch.tensor(series[i - ctx_len:i], dtype=torch.float32))
        t_pos = i + base_off - 1 if mode == "return" else i - 1
        dec_idx.append(idx[t_pos])
        last_px.append(vals[t_pos])
    if not contexts:
        return pd.DataFrame()

    med, lo, hi = [], [], []
    for s in range(0, len(contexts), BATCH):
        chunk = contexts[s:s + BATCH]
        q, mean = pipe.predict_quantiles(chunk, prediction_length=horizon,
                                         quantile_levels=QUANTILES)
        q = q.detach().cpu().numpy()                   # (B, horizon, n_quantiles)
        mean = mean.detach().cpu().numpy()             # (B, horizon)
        if mode == "return":
            med.append(mean.sum(axis=1))                       # cumulative bps
            lo.append(q[:, :, 0].sum(axis=1))
            hi.append(q[:, :, -1].sum(axis=1))
        else:
            med.append(q[:, -1, 2])                            # terminal median
            lo.append(q[:, -1, 0])
            hi.append(q[:, -1, -1])
    med = np.concatenate(med); lo = np.concatenate(lo); hi = np.concatenate(hi)
    last_px = np.asarray(last_px, dtype=np.float64)

    if mode == "return":
        pred_bps = med
        width_bps = hi - lo
    else:
        pred_bps = (med / last_px - 1.0) * 1e4
        width_bps = (hi - lo) / last_px * 1e4

    out = pd.DataFrame({"pred_bps": pred_bps, "width_bps": width_bps,
                        "last_px": last_px}, index=pd.DatetimeIndex(dec_idx))
    fwd = (px.shift(-horizon) / px - 1.0) * 1e4
    out["actual_bps"] = fwd.reindex(out.index)
    return out.dropna(subset=["actual_bps"])


# ── trading rule ────────────────────────────────────────────────────────────

def episodes(signal: pd.Series, actual: pd.Series, horizon: int,
             thresh: float) -> pd.DataFrame:
    """Non-overlapping mid-price trades whenever |signal| clears the band."""
    d = pd.DataFrame({"s": signal, "a": actual}).dropna()
    hold = pd.Timedelta(GRID) * horizon
    rows, until = [], None
    for ts, r in d.iterrows():
        if abs(r.s) < thresh or (until is not None and ts < until):
            continue
        side = float(np.sign(r.s))
        rows.append({"ts": ts, "side": side, "gross_bps": side * r.a,
                     "net_bps": side * r.a - FEE_MM_BPS})
        until = ts + hold
    return pd.DataFrame(rows)


def summarize(ep: pd.DataFrame, label: str) -> dict:
    if len(ep) < 5:
        return {"label": label, "n": len(ep), "note": "thin"}
    net = ep.net_bps.reset_index(drop=True)
    return {"label": label, "n": len(net),
            "gross_bps": round(float(ep.gross_bps.mean()), 2),
            "net_bps": round(float(net.mean()), 2),
            "median_net": round(float(net.median()), 2),
            "hit": round(float((ep.gross_bps > 0).mean()), 3),
            "nw_t": round(float(newey_west_tstat(net)["t_nw"]), 2)}


# ── fill-aware execution (same mechanics as research_fillaware_4h) ──────────

def fill_aware(ticker: str, signal: pd.Series, horizon: int,
               thresh: float) -> pd.DataFrame:
    stats = load_poll_market_stats(ticker)
    trades = load_poll_trades(ticker).sort_index()
    if stats.empty or trades.empty:
        return pd.DataFrame()
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

    hold = pd.Timedelta(GRID) * horizon
    entry_to = pd.Timedelta(minutes=ENTRY_TIMEOUT_MIN)
    rows, attempted, filled, until = [], 0, 0, None
    for ts, s in signal.dropna().items():
        if abs(s) < thresh or (until is not None and ts < until):
            continue
        side = int(np.sign(s))
        attempted += 1
        entry_side = "bid" if side > 0 else "ask"
        limit = touch_at(ts, entry_side)
        if limit is None:
            continue
        fr = simulate_maker_fill(limit, entry_side, ts, trades, timeout=entry_to,
                                 queue_ahead=resting(ts))
        if not fr.filled:
            until = ts + entry_to
            continue
        filled += 1
        entry_px, entry_ts = fr.fill_price, fr.fill_ts
        exit_ts = ts + hold
        exit_side = "ask" if side > 0 else "bid"
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
            adverse = touch_at(cross_ts, "bid" if side > 0 else "ask")
            exit_px = adverse if adverse is not None else exit_limit
            exit_role = "taker"
        gross = side * (exit_px - entry_px) * CONTRACTS
        fee = (fee_dollars(entry_px * CONTRACTS, role="maker", scenario="projected",
                           ticker=ticker)
               + fee_dollars(exit_px * CONTRACTS, role=exit_role,
                             scenario="projected", ticker=ticker))
        notional = entry_px * CONTRACTS
        rec = {"ts": ts, "ticker": ticker, "side": side,
               "gross_bps": 1e4 * gross / notional if notional else np.nan,
               "net_bps": 1e4 * (gross - fee) / notional if notional else np.nan,
               "exit_role": exit_role}
        m0 = mid_at(entry_ts)
        for lbl, secs in MARKOUT_HORIZONS.items():
            m1 = mid_at(entry_ts + pd.Timedelta(seconds=secs))
            rec[f"markout_{lbl}"] = (1e4 * side * (m1 - m0) / m0
                                     if (m0 and m1) else np.nan)
        rows.append(rec)
        until = exit_ts
    tp = pd.DataFrame(rows)
    if len(tp):
        tp.attrs["attempted"] = attempted
        tp.attrs["filled"] = filled
    return tp


# ── per-market study ────────────────────────────────────────────────────────

def study(ticker: str, frame: pd.DataFrame, pipe) -> dict:
    px = frame["mark_mid"].dropna()
    out = {"ticker": ticker, "variants": [], "chosen": None}
    best = None
    for hz_name, h in HORIZON_STEPS.items():
        for ctx_len in CONTEXT_LENS:
            for mode in ("price", "return"):
                t0 = time.time()
                fc = rolling_forecast(px, ctx_len=ctx_len, horizon=h,
                                      mode=mode, pipe=pipe)
                if fc.empty or len(fc) < 400:
                    continue
                split = fc.index[len(fc) // 2]
                is_d, oos_d = fc[fc.index < split], fc[fc.index >= split]
                if len(is_d) < 150 or len(oos_d) < 150:
                    continue
                # prediction quality, no trading involved
                ic_oos = float(oos_d.pred_bps.corr(oos_d.actual_bps, method="spearman"))
                dir_acc = float((np.sign(oos_d.pred_bps) ==
                                 np.sign(oos_d.actual_bps)).mean())
                # quantile calibration on OOS (share of actuals below each band)
                calib = {"below_q10": float((oos_d.actual_bps <
                                             oos_d.pred_bps - oos_d.width_bps / 2).mean()),
                         "above_q90": float((oos_d.actual_bps >
                                             oos_d.pred_bps + oos_d.width_bps / 2).mean())}
                # polarity fixed on IS (Chronos may be anti-predictive here)
                ic_is = is_d.pred_bps.corr(is_d.actual_bps, method="spearman")
                polarity = -1.0 if (pd.notna(ic_is) and ic_is < 0) else 1.0
                sig_is, sig_oos = polarity * is_d.pred_bps, polarity * oos_d.pred_bps

                # bands: absolute bps, plus conviction quantiles of |pred|
                bands = [("abs", th, th) for th in THRESHOLDS_BPS]
                for qq in (0.80, 0.90, 0.95):
                    bands.append(("q", qq, float(is_d.pred_bps.abs().quantile(qq))))
                is_scores = []
                for kind, param, th in bands:
                    s = summarize(episodes(sig_is, is_d.actual_bps, h, th), "IS")
                    if s.get("n", 0) >= 5:
                        is_scores.append((s["net_bps"], kind, param, th, s["n"]))
                if not is_scores:
                    continue
                best_is_net, band_kind, band_param, best_th, best_is_n = max(is_scores)
                ep_oos = episodes(sig_oos, oos_d.actual_bps, h, best_th)
                s_oos = summarize(ep_oos, f"{ticker} {hz_name} ctx{ctx_len} {mode}")
                rec = {"horizon": hz_name, "h": h, "ctx_len": ctx_len, "mode": mode,
                       "n_forecasts": len(fc), "secs": round(time.time() - t0, 1),
                       "oos_ic": round(ic_oos, 4), "oos_dir_acc": round(dir_acc, 4),
                       "calib": {k: round(v, 3) for k, v in calib.items()},
                       "pred_abs_median_bps": round(float(fc.pred_bps.abs().median()), 2),
                       "actual_abs_median_bps": round(float(fc.actual_bps.abs().median()), 2),
                       "polarity": polarity, "band_kind": band_kind,
                       "band_param": band_param,
                       "is_best_thresh": round(best_th, 2),
                       "is_best_net": round(best_is_net, 2),
                       "is_n": best_is_n, "oos_mid": s_oos}
                out["variants"].append(rec)
                score = s_oos.get("net_bps", -999) if s_oos.get("n", 0) >= 8 else -999
                if best is None or score > best[0]:
                    best = (score, rec, oos_d, best_th, h, polarity)
                logger.info("%s %s ctx%d %s: IC %.3f acc %.3f | OOS mid net %s (n=%s)",
                            ticker, hz_name, ctx_len, mode, ic_oos, dir_acc,
                            s_oos.get("net_bps"), s_oos.get("n"))
    if best is None:
        return out
    _, rec, oos_d, th, h, polarity = best
    out["chosen"] = {k: rec[k] for k in ("horizon", "ctx_len", "mode", "polarity",
                                         "band_kind", "is_best_thresh", "oos_mid")}
    sig_oos = polarity * oos_d.pred_bps

    # ── baselines under the identical rule, on the identical OOS window ──
    rng = np.random.default_rng(101)
    scale = float(oos_d.pred_bps.abs().median()) or 1.0
    ret_1h = (oos_d.last_px / oos_d.last_px.shift(12) - 1.0) * 1e4
    baselines = {
        "random": pd.Series(rng.normal(0, scale * 1.5, len(oos_d)), index=oos_d.index),
        "momentum_1h": ret_1h,
        "reversal_1h": -ret_1h,
    }
    bl = {}
    for name, sig in baselines.items():
        # each baseline gets its own IS-free but matched-count threshold: use the
        # quantile of |signal| that reproduces Chronos's OOS trade count
        n_target = out["chosen"]["oos_mid"].get("n", 0)
        if n_target < 5:
            continue
        q = 1.0 - min(0.99, (n_target * h) / max(len(sig.dropna()), 1))
        th_b = float(sig.abs().dropna().quantile(max(0.0, min(0.99, q))))
        bl[name] = summarize(episodes(sig, oos_d.actual_bps, h, th_b), name)
    out["baselines"] = bl

    # ── fill-aware on the chosen variant ──
    tp = fill_aware(ticker, sig_oos, h, th)
    if len(tp):
        net = tp.net_bps.dropna().reset_index(drop=True)
        out["fill_aware"] = {
            "n": len(tp), "attempted": tp.attrs.get("attempted"),
            "filled": tp.attrs.get("filled"),
            "fill_rate": round(tp.attrs.get("filled", 0) /
                               max(tp.attrs.get("attempted", 1), 1), 3),
            "gross_bps": round(float(tp.gross_bps.mean()), 2),
            "net_bps": round(float(net.mean()), 2),
            "hit": round(float((tp.gross_bps > 0).mean()), 3),
            "nw_t": round(float(newey_west_tstat(net)["t_nw"]), 2),
            "maker_exit_frac": round(float((tp.exit_role == "maker").mean()), 3),
            "markout": {lbl: round(float(tp[f"markout_{lbl}"].mean()), 2)
                        for lbl in MARKOUT_HORIZONS
                        if tp[f"markout_{lbl}"].notna().any()},
        }
    else:
        out["fill_aware"] = {"n": 0, "note": "no fills"}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--markets", default="KXBTCPERP,KXETHPERP")
    ap.add_argument("--all-alts", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    tickers = (FULL_MARKETS + ALT_MARKETS if args.all_alts
               else [t.strip() for t in args.markets.split(",") if t.strip()])
    t0 = time.time()
    pipe = _pipeline()
    logger.info("loaded %s in %.1fs", MODEL, time.time() - t0)

    results = []
    for t in tickers:
        try:
            frame = build_feature_frame(t) if t in FULL_MARKETS else build_alt_frame(t)
            results.append(study(t, frame, pipe))
        except Exception as e:                                   # noqa: BLE001
            logger.warning("%s failed: %s", t, str(e)[:100])

    print("=" * 112)
    print(f"CHRONOS DIRECTIONAL TEST — {MODEL} | fee {FEE_MM_BPS}bps RT | "
          "IS-half picks the band, OOS-half measures")
    print("=" * 112)

    print("\nA) PREDICTION QUALITY (OOS half, no trading — is the forecast even right?)")
    rows = []
    for r in results:
        for v in r["variants"]:
            rows.append({"ticker": r["ticker"], "hz": v["horizon"], "ctx": v["ctx_len"],
                         "mode": v["mode"], "n_fc": v["n_forecasts"],
                         "IC": v["oos_ic"], "dir_acc": v["oos_dir_acc"],
                         "|pred|med": v["pred_abs_median_bps"],
                         "|act|med": v["actual_abs_median_bps"],
                         "secs": v["secs"]})
    if rows:
        print(pd.DataFrame(rows).to_string(index=False))

    print("\nB) MID-PRICE ECONOMICS at the IS-chosen band (OOS half)")
    rows = []
    for r in results:
        for v in r["variants"]:
            m = v["oos_mid"]
            rows.append({"ticker": r["ticker"], "hz": v["horizon"], "ctx": v["ctx_len"],
                         "mode": v["mode"], "pol": int(v["polarity"]),
                         "band": f'{v["band_kind"]}{v["band_param"]}',
                         "band_bps": v["is_best_thresh"],
                         "n": m.get("n"), "gross": m.get("gross_bps"),
                         "net": m.get("net_bps"), "hit": m.get("hit"),
                         "nw_t": m.get("nw_t")})
    if rows:
        print(pd.DataFrame(rows).to_string(index=False))

    print("\nC) BASELINES vs CHRONOS (same rule, matched trade count, OOS half)")
    for r in results:
        if not r.get("chosen"):
            continue
        ch = r["chosen"]["oos_mid"]
        print(f"  {r['ticker']} [chosen {r['chosen']['horizon']} ctx{r['chosen']['ctx_len']} "
              f"{r['chosen']['mode']} band={r['chosen']['is_best_thresh']}bps]")
        print(f"     chronos      n={ch.get('n')} net={ch.get('net_bps')} "
              f"hit={ch.get('hit')} t={ch.get('nw_t')}")
        for name, b in (r.get("baselines") or {}).items():
            print(f"     {name:12} n={b.get('n')} net={b.get('net_bps')} "
                  f"hit={b.get('hit')} t={b.get('nw_t')}")

    print("\nD) FILL-AWARE — the decisive layer (real tape, queue-aware maker fills)")
    for r in results:
        fa = r.get("fill_aware")
        if not fa:
            continue
        if fa.get("n", 0) == 0:
            print(f"  {r['ticker']}: no fills")
            continue
        print(f"  {r['ticker']}: n={fa['n']} fill_rate={fa['fill_rate']} "
              f"GROSS {fa['gross_bps']:+.2f} NET {fa['net_bps']:+.2f} "
              f"hit={fa['hit']} NW-t={fa['nw_t']:+.2f} maker_exit={fa['maker_exit_frac']}")
        print(f"     markout: " + " ".join(f"{k}={v:+.2f}"
                                           for k, v in fa.get("markout", {}).items()))

    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"chronos_{stamp}.json").write_text(json.dumps(results, indent=1, default=str))
    print(f"\nsaved: {outdir}/chronos_{stamp}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
