"""Edge-conditioning research: WHERE do Plan 01-selective and Plan 04-frozen-cell
wins live, and can the samples grow without diluting?

Four questions (rerunnable, artifacts under trading_signals/research/):
  1. Condition each strategy's trades on observable entry state — UTC session,
     funding proximity, BTC vol regime, weekend, OKX-confirmation strength
     (Plan 04). Small buckets → report n/mean/hit only, no significance theater.
  2. Plan 01 selective on KXETHPERP (config PRE-REGISTERED from the BTC sweep —
     ETH is genuine out-of-market data). Pooled BTC+ETH significance.
  3. Plan 04 exit surface on the frozen cell's events: tp_fraction × time_stop ×
     hard_abort (27 cells). Full surface reported; flat ⇒ default fine.
  4. Overlap: are Plan 01 selected trades and Plan 04 cascades the same episodes?

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.research_conditioning
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.loader import (load_index_composite,
                                                 load_poll_market_stats,
                                                 load_poll_trades)
from crypto_trading.crypto_common.timeutils import FUNDING_HOURS_UTC
from crypto_trading.crypto_common.trade_stats import trade_significance_report
from crypto_trading.crypto_strategies.basis_meanrev.improved import (ImprovedParams,
                                                                     prepare, run_config)
from crypto_trading.crypto_strategies.liq_reversion.fill_aware import run_fill_aware
from crypto_trading.crypto_strategies.liq_reversion.signals.liquidation import DetectorParams
from crypto_trading.crypto_strategies.liq_reversion.widened import (COOLDOWN_MIN,
                                                                    OKX_SYMBOL,
                                                                    load_okx_liq_times)

logger = logging.getLogger(__name__)

RESEARCH_DIR = SIGNALS_DIR / "research"

# Plan 01 pre-registered config = the BTC sweep's top cell (k3.5/abs10/off10/abort20/flowY)
P01_BEST = ImprovedParams(entry_k=3.5, min_abs_bps=10.0, offset_ticks=10,
                          abort_bps=20.0, flow_filter=True, oi_confirm=False)
# Plan 04 frozen cell (config.yaml): composite anchor, maker, 15bps, cooldown 15m
P04_DET = DetectorParams(overshoot_entry_bps=15.0)


# ── bucketing helpers (pure — unit-tested) ──────────────────────────────────

def session_bucket(ts: pd.Timestamp) -> str:
    """UTC 3-session split: Asia 00-08, EU 08-16, US 16-24."""
    h = ts.tz_convert("UTC").hour if ts.tzinfo else ts.hour
    return "asia_00_08" if h < 8 else ("eu_08_16" if h < 16 else "us_16_24")


def near_funding(ts: pd.Timestamp, window_min: float = 60.0) -> bool:
    """Within ±window of a funding settlement (04/12/20 UTC)."""
    t = ts.tz_convert("UTC") if ts.tzinfo else ts
    for h in FUNDING_HOURS_UTC:
        anchor = t.replace(hour=h, minute=0, second=0, microsecond=0)
        for day_off in (-1, 0, 1):
            a = anchor + pd.Timedelta(days=day_off)
            if abs((t - a).total_seconds()) <= window_min * 60:
                return True
    return False


def is_weekend(ts: pd.Timestamp) -> bool:
    t = ts.tz_convert("UTC") if ts.tzinfo else ts
    return t.dayofweek >= 5


def okx_count(entry_ts: pd.Timestamp, liq_times: pd.DatetimeIndex,
              window_min: float = 2.0) -> int:
    """# OKX liquidation prints in the PAST window (entry−w, entry].

    PIT: the old ±window also counted prints AFTER entry — future information;
    the dose-response measured with it was not live-replicable.
    """
    if len(liq_times) == 0:
        return 0
    w = pd.Timedelta(minutes=window_min)
    lo = liq_times.searchsorted(entry_ts - w)
    hi = liq_times.searchsorted(entry_ts, side="right")
    return int(hi - lo)


def okx_bucket(n: int) -> str:
    return "0" if n == 0 else ("1" if n == 1 else "2+")


def btc_vol_regime() -> pd.Series:
    """Rolling-24h annualized vol of BTC composite 1-min returns; True = above
    the sample median (descriptive split, in-sample by construction)."""
    comp = load_index_composite("BTC")["vw_close"]
    r = comp.pct_change()
    vol = r.rolling(1440, min_periods=300).std() * np.sqrt(365 * 1440)
    return vol > vol.median()


def vol_bucket_at(ts: pd.Timestamp, regime: pd.Series) -> str:
    try:
        pos = regime.index.searchsorted(ts) - 1
        if pos < 0:
            return "unknown"
        v = regime.iloc[pos]
        return "high_vol" if bool(v) else "low_vol"
    except Exception:
        return "unknown"


def bucket_table(trades: pd.DataFrame, col: str) -> pd.DataFrame:
    """n / mean net / hit per bucket value."""
    if trades.empty:
        return pd.DataFrame()
    g = trades.groupby(col)["net"]
    return pd.DataFrame({"n": g.size(), "mean_net": g.mean().round(4),
                         "hit": g.apply(lambda s: float((s > 0).mean())).round(2)})


# ── strategy trade extraction ───────────────────────────────────────────────

def plan01_trades(ticker: str, asset: str) -> pd.DataFrame:
    prep = prepare(ticker, asset)
    r = run_config(prep, P01_BEST, ticker=ticker)
    tp = r["trade_pnl"]
    if len(tp):
        tp = tp.assign(ticker=ticker)
    logger.info("plan01 %s: %d trades (attempts %d)", ticker,
                len(tp), r["summary"]["attempts"])
    return tp


def plan04_trades(ticker: str, asset: str) -> pd.DataFrame:
    stats = load_poll_market_stats(ticker)
    tape = load_poll_trades(ticker).sort_index()
    comp = load_index_composite(asset)["vw_close"]
    r = run_fill_aware(ticker=ticker, det=P04_DET, stats=stats, trades=tape,
                       index_series=comp, entry_style="maker",
                       event_cooldown_min=COOLDOWN_MIN)
    tp = r["trade_pnl"]
    if len(tp):
        liq = load_okx_liq_times(OKX_SYMBOL[ticker])
        tp = tp.assign(ticker=ticker,
                       okx_n=[okx_count(t, liq) for t in tp["entry_ts"]])
    logger.info("plan04 %s: %d trades", ticker, len(tp))
    return tp


def annotate(trades: pd.DataFrame, regime: pd.Series) -> pd.DataFrame:
    if trades.empty:
        return trades
    t = trades.copy()
    ts = pd.DatetimeIndex(t["entry_ts"])
    t["session"] = [session_bucket(x) for x in ts]
    t["near_funding"] = [near_funding(x) for x in ts]
    t["weekend"] = [is_weekend(x) for x in ts]
    t["vol_regime"] = [vol_bucket_at(x, regime) for x in ts]
    if "okx_n" in t.columns:
        t["okx_bucket"] = [okx_bucket(int(n)) for n in t["okx_n"]]
    return t


# ── task 3: Plan 04 exit surface ────────────────────────────────────────────

def exit_surface(tickers_assets=(("KXBTCPERP", "BTC"), ("KXETHPERP", "ETH"))) -> pd.DataFrame:
    loaded = []
    for ticker, asset in tickers_assets:
        loaded.append((ticker, load_poll_market_stats(ticker),
                       load_poll_trades(ticker).sort_index(),
                       load_index_composite(asset)["vw_close"]))
    rows = []
    for tp_frac in (0.3, 0.5, 0.7):
        for tstop in (10, 15, 30):
            for abort in (1.5, 2.0, 3.0):
                nets, hits, ns = [], [], 0
                for ticker, stats, tape, comp in loaded:
                    r = run_fill_aware(ticker=ticker, det=P04_DET, stats=stats,
                                       trades=tape, index_series=comp,
                                       entry_style="maker",
                                       event_cooldown_min=COOLDOWN_MIN,
                                       tp_fraction=tp_frac, exit_timeout_min=tstop,
                                       hard_abort_mult=abort)
                    t = r["trade_pnl"]
                    if len(t):
                        nets.append(t["net"])
                        ns += len(t)
                allnet = pd.concat(nets) if nets else pd.Series(dtype=float)
                rows.append({"tp_fraction": tp_frac, "time_stop_min": tstop,
                             "hard_abort": abort, "n": ns,
                             "net": round(float(allnet.sum()), 4) if ns else 0.0,
                             "mean": round(float(allnet.mean()), 4) if ns else np.nan,
                             "hit": round(float((allnet > 0).mean()), 2) if ns else np.nan})
                logger.info("exit cell tp%.1f/ts%d/ab%.1f → n=%d net=%+.3f",
                            tp_frac, tstop, abort, ns,
                            float(allnet.sum()) if ns else 0.0)
    return pd.DataFrame(rows)


# ── task 4: overlap ─────────────────────────────────────────────────────────

def overlap_fraction(a_ts: pd.Series, b_ts: pd.Series, window_min: float = 30.0) -> float:
    """Fraction of a-trades with a b-event within ±window."""
    if len(a_ts) == 0 or len(b_ts) == 0:
        return 0.0
    b = pd.DatetimeIndex(b_ts).sort_values()
    w = pd.Timedelta(minutes=window_min)
    hits = 0
    for t in pd.DatetimeIndex(a_ts):
        pos = b.searchsorted(t)
        for i in (pos - 1, pos):
            if 0 <= i < len(b) and abs(b[i] - t) <= w:
                hits += 1
                break
    return hits / len(a_ts)


# ── main ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    report: dict = {"generated": str(pd.Timestamp.now(tz="UTC")),
                    "p01_config": "k3.5/abs10/off10/abort20/flowY (pre-registered from BTC sweep)",
                    "p04_config": "frozen cell: composite/maker/15bps/cooldown15"}
    md: list[str] = ["# Edge conditioning research", report["generated"], ""]

    regime = btc_vol_regime()

    # Plan 01: BTC (in-sample-selected config) + ETH (out-of-market)
    p01_btc = plan01_trades("KXBTCPERP", "BTC")
    p01_eth = plan01_trades("KXETHPERP", "ETH")
    p01_all = pd.concat([x for x in (p01_btc, p01_eth) if len(x)], ignore_index=True) \
        if (len(p01_btc) or len(p01_eth)) else pd.DataFrame()
    p01_all = annotate(p01_all, regime)

    # Plan 04 frozen cell BTC+ETH
    p04_all = pd.concat([x for x in (plan04_trades("KXBTCPERP", "BTC"),
                                     plan04_trades("KXETHPERP", "ETH")) if len(x)],
                        ignore_index=True)
    p04_all = annotate(p04_all, regime)

    # ── task 1: conditioning tables ─────────────────────────────────────────
    cond: dict = {}
    for name, df, cols in (("plan01_selective", p01_all,
                            ["session", "near_funding", "vol_regime", "weekend", "ticker"]),
                           ("plan04_frozen", p04_all,
                            ["session", "near_funding", "vol_regime", "weekend",
                             "okx_bucket", "ticker"])):
        cond[name] = {}
        md.append(f"\n## Conditioning — {name} (n={len(df)})")
        for c in cols:
            if df.empty or c not in df.columns:
                continue
            t = bucket_table(df, c)
            cond[name][c] = t.to_dict()
            md.append(f"\n### by {c}\n{t.to_markdown()}")

    # ── task 2: ETH expansion + pooled significance ─────────────────────────
    exp: dict = {}
    if len(p01_eth) >= 3:
        exp["eth"] = {"n": len(p01_eth), "net": float(p01_eth.net.sum()),
                      "hit": float((p01_eth.net > 0).mean())}
        if len(p01_eth) >= 5:
            rep = trade_significance_report(p01_eth["net"], k=min(5, len(p01_eth) // 2),
                                            n_trials=1)   # pre-registered config
            exp["eth"].update({"nw_t": rep["t_nw"],
                               "frac_pos": rep["purged_cv"]["frac_positive"]})
    else:
        exp["eth"] = {"n": len(p01_eth), "note": "too few ETH trades"}
    if len(p01_all) >= 5:
        rep = trade_significance_report(p01_all["net"], k=min(5, len(p01_all) // 3),
                                        n_trials=2)       # 2-market trial per directive
        exp["pooled"] = {"n": len(p01_all), "net": float(p01_all.net.sum()),
                         "hit": float((p01_all.net > 0).mean()), "nw_t": rep["t_nw"],
                         "dsr": rep["dsr"],
                         "frac_pos": rep["purged_cv"]["frac_positive"],
                         "significant": rep["significant"],
                         "caveat": "BTC leg config was selected on BTC (45-trial sweep); "
                                   "ETH leg is genuinely out-of-market"}
    report["plan01_expansion"] = exp
    md.append(f"\n## Plan 01 ETH expansion\n```json\n{json.dumps(exp, indent=1, default=float)}\n```")

    # ── task 3: exit surface ────────────────────────────────────────────────
    surf = exit_surface()
    default_cell = surf[(surf.tp_fraction == 0.5) & (surf.time_stop_min == 15)
                        & (surf.hard_abort == 2.0)]
    surf_stats = {"net_min": float(surf.net.min()), "net_max": float(surf.net.max()),
                  "net_std": float(surf.net.std()),
                  "default_net": float(default_cell.net.iloc[0]) if len(default_cell) else None,
                  "best": surf.sort_values("net", ascending=False).head(1).to_dict("records")[0],
                  "flat": bool(surf.net.std() <= max(0.05, 0.35 * abs(surf.net.mean())))}
    report["plan04_exit_surface"] = {"cells": surf.to_dict("records"), "assessment": surf_stats}
    md.append(f"\n## Plan 04 exit surface (27 cells, BTC+ETH pooled)\n{surf.to_markdown(index=False)}")
    md.append(f"\nassessment: {json.dumps(surf_stats, default=float)}")

    # ── task 4: overlap ─────────────────────────────────────────────────────
    ov = {}
    for tkr in ("KXBTCPERP", "KXETHPERP"):
        a = p01_all[p01_all.ticker == tkr]["entry_ts"] if len(p01_all) else pd.Series(dtype=object)
        b = p04_all[p04_all.ticker == tkr]["entry_ts"] if len(p04_all) else pd.Series(dtype=object)
        ov[tkr] = {"p01_n": len(a), "p04_n": len(b),
                   "p01_near_p04": round(overlap_fraction(a, b), 2),
                   "p04_near_p01": round(overlap_fraction(b, a), 2)}
    report["overlap"] = ov
    md.append(f"\n## Overlap (±30min)\n```json\n{json.dumps(ov, indent=1)}\n```")

    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    report["conditioning"] = cond
    (RESEARCH_DIR / f"conditioning_{stamp}.json").write_text(
        json.dumps(report, indent=1, default=str))
    (RESEARCH_DIR / f"conditioning_{stamp}.md").write_text("\n".join(md))
    print("\n".join(md))
    print(f"\nartifacts: research/conditioning_{stamp}.{{json,md}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
