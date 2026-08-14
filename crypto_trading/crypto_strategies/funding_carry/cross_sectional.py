"""Plan 03 REDESIGN — cross-sectional funding carry, dollar-neutral long/short.

Why the redesign: the single-leg carry died to directional noise (1798 cycles,
NW-t −0.98 — funding +0.04 swamped by price ±0.17). The measured cross-section
is the real prize: BTC funding +5.4%/yr vs BCH −12.8%/yr ⇒ an ~18%/yr SPREAD.
Going LONG the funding-collecting side (negative-funding perps) and SHORT the
funding-paying side (positive-funding perps), dollar-neutral, collects BOTH
legs' funding while the (highly correlated) crypto price moves largely cancel.

Per 8h funding cycle:
  forecast per perp  = recency-weighted mean of PAST realized funding (PIT)
  long basket        = k most-negative forecasts (long collects −rate)
  short basket       = k most-positive forecasts (short collects +rate)
  weights            = 1/k per name per side ($1 long / $1 short book)
  cycle P&L per $1/side:
      long leg :  Σ w·(price_ret − rate_next)
      short leg:  Σ w·(−price_ret + rate_next)
      fees     :  turnover × fee_rate  (charged on basket changes only)
Significance via trade_stats (NW-t, purged CV) on the per-cycle net series.

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.funding_carry.cross_sectional
        [--k 3] [--fees projected] [--role taker|maker] [--forecast-window 6]
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import ACTIVE_PERPS_SNAPSHOT, SIGNALS_DIR
from crypto_trading.crypto_common.costs import load_fee_rates
from crypto_trading.crypto_common.loader import load_funding, load_perp_candles

logger = logging.getLogger(__name__)
CYCLES_PER_YEAR = 365 * 3


def build_cycle_panels(tickers=ACTIVE_PERPS_SNAPSHOT) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(funding_rate panel, price panel) indexed by funding settlement time."""
    rates, prices = {}, {}
    for t in tickers:
        try:
            f = load_funding(t)
            p = load_perp_candles(t, "1h")["price_close"].dropna()
        except FileNotFoundError:
            continue
        if len(f) < 10 or len(p) < 24:
            continue
        rates[t] = f["funding_rate"]
        # price as-of each funding time (backward)
        idx = p.index.searchsorted(f.index, side="right") - 1
        ok = idx >= 0
        prices[t] = pd.Series(p.iloc[idx[ok]].to_numpy(), index=f.index[ok])
    rate_panel = pd.DataFrame(rates).sort_index()
    price_panel = pd.DataFrame(prices).sort_index().reindex(rate_panel.index)
    return rate_panel, price_panel


def forecast_panel(rate_panel: pd.DataFrame, window: int) -> pd.DataFrame:
    """PIT funding forecast: recency-weighted mean of the trailing `window`
    REALIZED cycles (row t uses rates up to and including t — the position is
    taken after settlement t and realizes rate t+1, so no lookahead)."""
    w = np.linspace(1.0, 2.0, window)

    def _f(x):
        v = x[~np.isnan(x)]
        if len(v) == 0:
            return np.nan
        return float(np.average(v, weights=w[-len(v):]))

    return rate_panel.rolling(window, min_periods=max(2, window // 2)).apply(_f, raw=True)


def backtest_xs(*, k: int = 3, forecast_window: int = 6,
                fee_scenario: str = "projected", fee_role: str = "taker",
                min_names: int = 5, rebalance_every: int = 1) -> dict:
    """``rebalance_every``: re-pick baskets every N funding cycles (1 = every 8h,
    3 = daily). Between rebalances the held book still accrues funding + price
    P&L each cycle but pays NO fees — turnover is the measured killer."""
    rate_panel, price_panel = build_cycle_panels()
    fc = forecast_panel(rate_panel, forecast_window)
    _, taker = load_fee_rates("KXBTCPERP")
    maker, _ = load_fee_rates("KXBTCPERP")
    fee_rate = 0.0 if fee_scenario == "zero" else (maker if fee_role == "maker" else taker)

    times = list(rate_panel.index)
    prev_w = pd.Series(dtype=float)
    recs = []
    for i in range(len(times) - 1):
        t, t1 = times[i], times[i + 1]
        px_t = price_panel.loc[t].dropna()
        px_t1 = price_panel.loc[t1].dropna()
        rebalance_now = (i % rebalance_every == 0) or prev_w.empty
        if rebalance_now:
            f = fc.loc[t].dropna()
            # PIT: eligibility uses ONLY time-t information — requiring a t+1
            # price here was survivorship (quietly dropping names that die
            # during the cycle); missing t+1 prices are handled at P&L time.
            avail = f.index.intersection(px_t.index)
            if len(avail) < min_names:
                continue
            f = f[avail].sort_values()
            kk = min(k, len(avail) // 2)
            if kk < 1:
                continue
            longs = f.index[:kk]      # most negative forecast → long collects
            shorts = f.index[-kk:]    # most positive forecast → short collects
            w = pd.Series(0.0, index=avail)
            w[longs] += 1.0 / kk      # +$1 long book
            w[shorts] -= 1.0 / kk     # −$1 short book
        else:
            # hold the book; restrict to names priced at t (PIT — no t+1 peek)
            avail = prev_w.index.intersection(px_t.index)
            if len(avail) < 2:
                continue
            w = prev_w.reindex(avail).fillna(0.0)
            longs = w[w > 0].index
            shorts = w[w < 0].index

        # names with no t+1 price mark flat at their last price (delist-safe)
        ret = (px_t1.reindex(avail).fillna(px_t[avail]) / px_t[avail] - 1.0)
        rate_next = rate_panel.loc[t1, avail].fillna(0.0)
        price_pnl = float((w * ret).sum())
        funding_pnl = float((-w * rate_next).sum())   # holder funding = −rate·pos
        turnover = float((w.reindex(w.index.union(prev_w.index)).fillna(0)
                          - prev_w.reindex(w.index.union(prev_w.index)).fillna(0)).abs().sum())
        fee = turnover * fee_rate
        recs.append({"dt": t1, "price_pnl": price_pnl, "funding_pnl": funding_pnl,
                     "fee": fee, "net": price_pnl + funding_pnl - fee,
                     "turnover": turnover, "n_names": len(avail),
                     "long": ",".join(longs), "short": ",".join(shorts)})
        prev_w = w

    df = pd.DataFrame(recs).set_index("dt") if recs else pd.DataFrame()
    if df.empty:
        return {"summary": {"error": "no cycles"}, "series": df}
    net = df["net"]
    ann = float(net.mean() * CYCLES_PER_YEAR)
    sharpe = float(net.mean() / net.std() * np.sqrt(CYCLES_PER_YEAR)) if net.std() > 0 else 0.0
    summary = {
        "cycles": len(df), "k_per_side": k, "fee_scenario": fee_scenario,
        "fee_role": fee_role,
        "ann_return_per_$1_side": ann,
        "sharpe_ann": sharpe,
        "funding_collected_total": float(df.funding_pnl.sum()),
        "price_pnl_total": float(df.price_pnl.sum()),
        "fees_total": float(df.fee.sum()),
        "avg_turnover_per_cycle": float(df.turnover.mean()),
        "hit_rate": float((net > 0).mean()),
        "span": [str(df.index.min()), str(df.index.max())],
    }
    return {"summary": summary, "series": df}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--forecast-window", type=int, default=6)
    ap.add_argument("--fees", default="projected", choices=["zero", "projected"])
    ap.add_argument("--role", default="taker", choices=["taker", "maker"])
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    r = backtest_xs(k=args.k, forecast_window=args.forecast_window,
                    fee_scenario=args.fees, fee_role=args.role)
    print(json.dumps(r["summary"], indent=2, default=str))
    out = SIGNALS_DIR / "funding_carry" / "cross_sectional"
    out.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    if len(r["series"]):
        r["series"].to_csv(out / f"xs_{stamp}.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
