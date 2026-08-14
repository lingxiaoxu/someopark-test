"""Plan 04 WIDENED — full-universe cascade fade on the recorded tape.

The single-ticker fill-aware run found the OI-drop+fading fade went 6/6 on ~20
days — right sign, data-starved. This multiplies event count WITHOUT diluting
the liquidation signature (OI-drop stays mandatory):

  * ALL 13 active perps (each has its own 20d poll tape).
  * Anchor: BTC/ETH keep the clean 1-min spot composite. The other 11 have no
    composite → anchor = the perp's own PIT rolling-median mark (30-min median
    of the 10s-grid mid, shifted one bar). That redefines "overshoot" as a
    deviation from the perp's own recent level rather than true spot basis —
    weaker as an absolute anchor, but combined with the mandatory OI-drop +
    one-sided burst it preserves the cascade signature. Documented, not hidden.
  * Threshold ladder: overshoot ∈ {10, 15, 20} bps (fading-only always).
  * Entry style: maker (post at touch) AND taker (cross now) per cell.
  * Cross-venue lever: flag trades where the SAME asset printed an OKX
    liquidation within ±2 min of entry; report conditional edge.
  * Event cooldown 15 min per ticker so one cascade's clustered grid-bars
    can't double-count.

Multiple-testing discipline: the pooled verdict is deflated with
n_trials = (#ladder cells × #entry styles) via trade_stats (per-ticker rows are
descriptive only, not separately selected).

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.liq_reversion.widened
        [--queue-frac 1.0] [--fees projected]
"""
from __future__ import annotations

import argparse
import glob
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import PRICE_DATA, SIGNALS_DIR
from crypto_trading.crypto_common.loader import (load_index_composite,
                                                 load_poll_market_stats,
                                                 load_poll_trades)
from crypto_trading.crypto_common.trade_stats import trade_significance_report
from crypto_trading.crypto_strategies.liq_reversion.fill_aware import run_fill_aware
from crypto_trading.crypto_strategies.liq_reversion.signals.liquidation import DetectorParams

logger = logging.getLogger(__name__)
STRATEGY = "liq_reversion"

ALL_PERPS = ("KXBTCPERP", "KXETHPERP", "KXSOLPERP", "KXXRPPERP", "KXDOGEPERP",
             "KXKSHIBPERP", "KXBCHPERP", "KXLTCPERP", "KXLINKPERP", "KXNEARPERP",
             "KXSUIPERP", "KXHYPEPERP", "KXZECPERP")
# ticker → composite asset. anchor_for() tries load_index_composite(asset) and
# falls back to self_median when the parquet doesn't exist yet, so listing a
# ticker here is safe before its composite is backfilled. (True spot composites
# for the alts landed 2026-07-26 — 60d Coinbase+Kraken+Bitstamp VWAP.)
COMPOSITE_ASSET = {"KXBTCPERP": "BTC", "KXETHPERP": "ETH", "KXSOLPERP": "SOL",
                   "KXXRPPERP": "XRP", "KXDOGEPERP": "DOGE", "KXLTCPERP": "LTC",
                   "KXBCHPERP": "BCH", "KXLINKPERP": "LINK", "KXNEARPERP": "NEAR",
                   "KXSUIPERP": "SUI"}   # KSHIB/HYPE/ZEC: no spot venue coverage
OKX_SYMBOL = {"KXBTCPERP": "BTCUSDT", "KXETHPERP": "ETHUSDT", "KXSOLPERP": "SOLUSDT",
              "KXXRPPERP": "XRPUSDT", "KXDOGEPERP": "DOGEUSDT", "KXKSHIBPERP": "SHIBUSDT",
              "KXBCHPERP": "BCHUSDT", "KXLTCPERP": "LTCUSDT", "KXLINKPERP": "LINKUSDT",
              "KXNEARPERP": "NEARUSDT", "KXSUIPERP": "SUIUSDT", "KXHYPEPERP": "HYPEUSDT",
              "KXZECPERP": "ZECUSDT"}

MEDIAN_BARS = 180          # 30 min of 10s grid — the self-anchor window
COOLDOWN_MIN = 15.0        # ≥ exit timeout: one position per cascade


def self_median_anchor(stats: pd.DataFrame, grid_sec: int = 10) -> pd.Series:
    """PIT rolling-median-of-mark anchor (underlying scale) for perps without a
    spot composite. shift(1) so the current bar never sees itself."""
    st = stats.copy()
    st["mid_contract"] = (st["bid"] + st["ask"]) / 2.0
    st["mid_contract"] = st["mid_contract"].where(st["mid_contract"] > 0, st["price"])
    csize = st["contract_size"].replace(0, np.nan).ffill()   # PIT: no future backfill
    mid = (st["mid_contract"] / csize).resample(f"{grid_sec}s").last()
    return mid.rolling(MEDIAN_BARS, min_periods=30).median().shift(1).dropna()


def anchor_for(ticker: str, stats: pd.DataFrame,
               mode: str = "auto") -> tuple[pd.Series, str]:
    """Anchor series for a ticker. ``mode``: "auto" prefers the true spot
    composite when its parquet exists (falls back to self-median);
    "self_median" forces the fallback (used for the anchor-diagnosis A/B);
    "composite" requires the composite (raises if absent)."""
    if mode not in ("auto", "composite", "self_median"):
        raise ValueError(f"unknown anchor mode {mode!r}")
    if mode != "self_median":
        asset = COMPOSITE_ASSET.get(ticker)
        if asset:
            try:
                comp = load_index_composite(asset)
                if len(comp):
                    return comp["vw_close"], "composite"
            except FileNotFoundError:
                pass
        if mode == "composite":
            raise FileNotFoundError(f"no composite anchor for {ticker}")
    return self_median_anchor(stats), "self_median"


def load_okx_liq_times(okx_symbol: str) -> pd.DatetimeIndex:
    """Timestamps of recorded OKX liquidation prints for one symbol."""
    ts = []
    for f in sorted(glob.glob(str(PRICE_DATA / "offshore" / "okx" / "liquidations"
                                  / okx_symbol / "*.jsonl*"))):
        opener = __import__("gzip").open if f.endswith(".gz") else open
        try:
            with opener(f, "rt") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        t = d.get("ts_ms") or d.get("recv_ts", 0) * 1000
                        if t:
                            ts.append(float(t) / 1000.0)
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass
    return pd.DatetimeIndex(pd.to_datetime(sorted(ts), unit="s", utc=True))


def okx_confirmed(entry_ts: pd.Timestamp, liq_times: pd.DatetimeIndex,
                  window_min: float = 2.0) -> bool:
    """True iff an OKX liquidation printed in the PAST window (entry−w, entry].

    PIT: the old ±window also matched prints AFTER entry — future information a
    live decision cannot have. The earlier "confirmed → 91% hit" dose-response
    was measured with that leak; only the past half-window is armable.
    """
    if len(liq_times) == 0:
        return False
    w = pd.Timedelta(minutes=window_min)
    i = liq_times.searchsorted(entry_ts, side="right") - 1
    return 0 <= i < len(liq_times) and (entry_ts - liq_times[i]) <= w


def run_widened(*, tickers=ALL_PERPS, ladder=(10.0, 15.0, 20.0),
                styles=("maker", "taker"), queue_frac: float = 1.0,
                fee_scenario: str = "projected", anchor_mode: str = "auto",
                preloaded: dict | None = None) -> dict:
    """``anchor_mode`` per anchor_for(). ``preloaded``: optional
    {ticker: (stats, tape)} cache so A/B runs don't re-read the jsonl tape."""
    cells: list[dict] = []
    all_trades: dict[tuple, pd.DataFrame] = {}
    span_days = None

    for ticker in tickers:
        try:
            if preloaded and ticker in preloaded:
                stats, tape = preloaded[ticker]
            else:
                stats = load_poll_market_stats(ticker)
                tape = load_poll_trades(ticker).sort_index()
        except Exception as e:
            logger.warning("%s: tape load failed %s", ticker, str(e)[:80])
            continue
        if stats.empty or tape.empty:
            logger.warning("%s: empty tape — skipped", ticker)
            continue
        try:
            anchor, anchor_kind = anchor_for(ticker, stats, mode=anchor_mode)
        except FileNotFoundError:
            logger.warning("%s: no composite anchor — skipped (mode=%s)",
                           ticker, anchor_mode)
            continue
        if span_days is None and len(stats):
            span_days = (stats.index.max() - stats.index.min()).days
        liq_times = load_okx_liq_times(OKX_SYMBOL.get(ticker, ""))

        for os_bps in ladder:
            det = DetectorParams(overshoot_entry_bps=os_bps)
            for style in styles:
                try:
                    r = run_fill_aware(ticker=ticker, det=det, queue_frac=queue_frac,
                                       fee_scenario=fee_scenario, stats=stats,
                                       trades=tape, index_series=anchor,
                                       entry_style=style,
                                       event_cooldown_min=COOLDOWN_MIN)
                except Exception as e:
                    logger.warning("%s %s %s: run failed %s", ticker, os_bps, style,
                                   str(e)[:100])
                    continue
                s = r["summary"]
                tp = r["trade_pnl"]
                if len(tp):
                    tp = tp.assign(ticker=ticker, os_bps=os_bps, style=style,
                                   okx_confirmed=[okx_confirmed(t, liq_times)
                                                  for t in tp["entry_ts"]])
                all_trades[(ticker, os_bps, style)] = tp
                cells.append({"ticker": ticker, "anchor": anchor_kind,
                              "os_bps": os_bps, "style": style,
                              "detected": s["cascades_detected"],
                              "fading": s["cascades_fading"],
                              "trades": s["round_trips"],
                              "net": round(s["net_pnl_per_10c"], 4),
                              "hit": round(s["hit_rate"], 2)})

    table = pd.DataFrame(cells)
    n_trials = len(ladder) * len(styles)     # pooled selection dimensions

    pooled = {}
    for os_bps in ladder:
        for style in styles:
            parts = [t for (tk, ob, st), t in all_trades.items()
                     if ob == os_bps and st == style and len(t)]
            if not parts:
                continue
            allt = pd.concat(parts).sort_values("entry_ts")
            rep = (trade_significance_report(allt["net"], k=min(5, max(2, len(allt) // 3)),
                                             n_trials=n_trials)
                   if len(allt) >= 5 else None)
            conf = allt[allt.okx_confirmed]
            unconf = allt[~allt.okx_confirmed]
            pooled[f"{os_bps}bps_{style}"] = {
                "n": len(allt), "net": float(allt.net.sum()),
                "mean": float(allt.net.mean()), "hit": float((allt.net > 0).mean()),
                "nw_t": rep["t_nw"] if rep else None,
                "dsr": rep["dsr"] if rep else None,
                "frac_positive_folds": rep["purged_cv"]["frac_positive"] if rep else None,
                "significant_deflated": rep["significant"] and rep["dsr"] >= 0.9 if rep else None,
                "okx_confirmed": {"n": len(conf), "mean": float(conf.net.mean()) if len(conf) else None,
                                  "hit": float((conf.net > 0).mean()) if len(conf) else None},
                "okx_unconfirmed": {"n": len(unconf), "mean": float(unconf.net.mean()) if len(unconf) else None,
                                    "hit": float((unconf.net > 0).mean()) if len(unconf) else None},
            }

    return {"cells": table, "pooled": pooled, "n_trials": n_trials,
            "span_days": span_days,
            "all_trades": pd.concat([t for t in all_trades.values() if len(t)])
            if any(len(t) for t in all_trades.values()) else pd.DataFrame()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queue-frac", type=float, default=1.0)
    ap.add_argument("--fees", default="projected", choices=["zero", "projected"])
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    r = run_widened(queue_frac=args.queue_frac, fee_scenario=args.fees)

    print("=" * 78)
    print(f"PLAN 04 WIDENED — 13 perps × ladder × style | span ~{r['span_days']}d | "
          f"n_trials={r['n_trials']}")
    print("=" * 78)
    t = r["cells"]
    if len(t):
        agg = (t.groupby(["os_bps", "style"])
               .agg(tickers=("ticker", "nunique"), detected=("detected", "sum"),
                    fading=("fading", "sum"), trades=("trades", "sum"),
                    net=("net", "sum")).reset_index())
        print(agg.to_string(index=False))
        print("-" * 78)
        active = t[t.trades > 0].sort_values("net", ascending=False)
        print("per-ticker cells with trades (top 12 by net):")
        print(active.head(12).to_string(index=False))
    print("-" * 78)
    for k, v in r["pooled"].items():
        line = (f"POOLED {k:14} n={v['n']:>3} net={v['net']:+8.3f} mean={v['mean']:+.4f} "
                f"hit={v['hit']:.0%}")
        if v["nw_t"] is not None:
            line += (f" NW-t={v['nw_t']:+.2f} DSR={v['dsr']:.2f} "
                     f"folds+={v['frac_positive_folds']:.0%} sig(defl)={v['significant_deflated']}")
        print(line)
        oc, ou = v["okx_confirmed"], v["okx_unconfirmed"]
        if oc["n"] or ou["n"]:
            print(f"       └ OKX±2min: confirmed n={oc['n']} mean={oc['mean']} hit={oc['hit']}"
                  f" | unconfirmed n={ou['n']} mean={ou['mean']} hit={ou['hit']}")
    print("=" * 78)

    out = SIGNALS_DIR / STRATEGY / "widened"
    out.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    r["cells"].to_csv(out / f"cells_{stamp}.csv", index=False)
    if len(r["all_trades"]):
        r["all_trades"].to_csv(out / f"trades_{stamp}.csv", index=False)
    pooled_clean = json.loads(json.dumps(r["pooled"], default=float))
    (out / f"pooled_{stamp}.json").write_text(json.dumps(pooled_clean, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
