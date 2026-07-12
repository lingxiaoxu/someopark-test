"""Plan 03 funding-carry backtest (milestone 2 proxy + Kalshi).

The honest test the plan demands (§2, §8): holding the funding-collecting side is
net-directional, so does the harvested funding actually BEAT the adverse price
drift you take to earn it? This backtest nets the two, per cycle, per perp.

Two panels (plan §8 history caveat — prototype on offshore proxy, re-fit on Kalshi):
  * Kalshi-native funding + perp price (33 days, 13 perps) — the real venue.
  * OKX offshore funding + price (deeper, labeled proxy) — mechanism check.

Two strategy variants, both reported:
  * naive     : always hold the collecting side (shows the raw directional bleed)
  * gated     : the plan §6 carry-vs-drift gate (only hold when funding beats drift+cost)

P&L per cycle for a $1 notional position on the collecting side:
    funding_pnl = |rate| · 1           (you receive it — you're on the paid side)
    price_pnl   = position_sign · (P[t+1]/P[t] − 1)
    fee         = round-trip taker fee amortized when the position flips
    net         = funding_pnl + price_pnl − fee
Annualization: 365 × 3 cycles/day (8h funding).

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.funding_carry.backtest
        [--panel kalshi|proxy|both] [--fees projected|zero] [--gate|--no-gate]
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import ACTIVE_PERPS_SNAPSHOT, SIGNALS_DIR
from crypto_trading.crypto_common.costs import load_fee_rates
from crypto_trading.crypto_common.loader import load_funding, load_offshore, load_perp_candles
from crypto_trading.crypto_strategies.funding_carry.signals.funding import (CarryParams,
                                                                            carry_signal)

logger = logging.getLogger(__name__)
CYCLES_PER_YEAR = 365 * 3          # 8h funding

# Kalshi perp ↔ OKX proxy symbol
PROXY_MAP = {"KXBTCPERP": "BTCUSDT", "KXETHPERP": "ETHUSDT", "KXSOLPERP": "SOLUSDT",
             "KXXRPPERP": "XRPUSDT", "KXDOGEPERP": "DOGEUSDT"}


def _align_price_to_funding(funding: pd.DataFrame, price: pd.Series) -> pd.DataFrame:
    """Attach the perp price at each funding settlement (as-of backward)."""
    p = price.sort_index()
    f = funding.sort_index().copy()
    # price at or before each funding_time
    idx = p.index.searchsorted(f.index, side="right") - 1
    valid = idx >= 0
    f = f[valid]
    f["price"] = p.iloc[idx[valid]].to_numpy()
    return f.dropna(subset=["price"])


def backtest_perp(rates: pd.DataFrame, price: pd.Series, *, gated: bool,
                  fee_scenario: str, ticker: str,
                  params: CarryParams = CarryParams()) -> dict | None:
    """Cycle-by-cycle carry P&L for one perp. Returns per-cycle net-return series."""
    df = _align_price_to_funding(rates, price)
    if len(df) < params.funding_forecast_window + 3:
        return None
    df = df.sort_index()
    rate_hist: list[float] = []
    price_hist: list[float] = []
    maker, taker = load_fee_rates(ticker)
    fee_rt = 0.0 if fee_scenario == "zero" else 2 * taker   # round-trip taker on flip

    prev_sign = 0
    recs = []
    times = list(df.index)
    rate_arr = df["funding_rate"].to_numpy()
    price_arr = df["price"].to_numpy()
    for i in range(len(df) - 1):
        rate_hist.append(rate_arr[i])
        price_hist.append(price_arr[i])
        rs = pd.Series(rate_hist)
        ps = pd.Series(price_hist)
        pct = float((rs.abs().rank(pct=True)).iloc[-1]) if len(rs) > 1 else 1.0
        if gated:
            sig = carry_signal(rs, ps, params, rate_percentile=pct)
            sign = sig["position_sign"]
        else:
            sign = int(-np.sign(rate_arr[i])) if rate_arr[i] != 0 else 0

        # realize over the NEXT cycle. Funding P&L of the holder = −rate·position
        # (costs.funding_payment convention): collecting side (sign = −sign(rate))
        # earns −rate·sign > 0. Price P&L = position · forward return.
        fwd_ret = price_arr[i + 1] / price_arr[i] - 1.0
        funding_pnl = -rate_arr[i + 1] * sign
        price_pnl = sign * fwd_ret
        fee = fee_rt if sign != prev_sign and sign != 0 else 0.0
        net = funding_pnl + price_pnl - fee
        recs.append({"dt": times[i + 1], "sign": sign, "funding_pnl": funding_pnl,
                     "price_pnl": price_pnl, "fee": fee, "net": net,
                     "rate": rate_arr[i + 1]})
        prev_sign = sign

    r = pd.DataFrame(recs).set_index("dt")
    active = r[r["sign"] != 0]
    if r.empty:
        return None
    net = r["net"]
    ann_factor = np.sqrt(CYCLES_PER_YEAR)
    sharpe = (net.mean() / net.std() * ann_factor) if net.std() > 0 else 0.0
    return {
        "ticker": ticker, "cycles": len(r), "active_cycles": int((r["sign"] != 0).sum()),
        "total_net": float(net.sum()),
        "funding_collected": float(r["funding_pnl"].sum()),
        "price_pnl": float(r["price_pnl"].sum()),
        "fees": float(r["fee"].sum()),
        "sharpe_ann": float(sharpe),
        "hit_rate": float((active["net"] > 0).mean()) if len(active) else 0.0,
        "series": net,
    }


def run_panel(panel: str, *, gated: bool, fee_scenario: str,
              params: CarryParams = CarryParams()) -> dict:
    results = {}
    tickers = list(ACTIVE_PERPS_SNAPSHOT) if panel == "kalshi" else list(PROXY_MAP)
    for t in tickers:
        try:
            if panel == "kalshi":
                rates = load_funding(t)
                price = load_perp_candles(t, "1h")["price_close"].dropna()
            else:
                sym = PROXY_MAP[t]
                off_f = load_offshore("funding", sym)
                rates = pd.DataFrame({"funding_rate": off_f["funding_rate"]})
                price = load_offshore("klines_1h", sym)["close"].dropna()
        except (FileNotFoundError, KeyError):
            continue
        res = backtest_perp(rates, price, gated=gated, fee_scenario=fee_scenario,
                            ticker=t, params=params)
        if res:
            results[t] = res

    if not results:
        return {"panel": panel, "error": "no data"}

    # equal-weight portfolio of per-perp net series
    series = pd.concat({t: r["series"] for t, r in results.items()}, axis=1).fillna(0.0)
    port = series.mean(axis=1)
    port_sharpe = (port.mean() / port.std() * np.sqrt(CYCLES_PER_YEAR)) if port.std() > 0 else 0.0
    return {
        "panel": panel, "gated": gated, "fee_scenario": fee_scenario,
        "n_perps": len(results),
        "portfolio": {
            "total_net_per_unit": float(port.sum()),
            "ann_return_pct": float(port.mean() * CYCLES_PER_YEAR * 100),
            "sharpe_ann": float(port_sharpe),
            "funding_collected": float(sum(r["funding_collected"] for r in results.values())),
            "price_pnl": float(sum(r["price_pnl"] for r in results.values())),
            "fees": float(sum(r["fees"] for r in results.values())),
        },
        "per_perp": {t: {k: v for k, v in r.items() if k != "series"}
                     for t, r in results.items()},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--panel", default="both", choices=["kalshi", "proxy", "both"])
    ap.add_argument("--fees", default="projected", choices=["zero", "projected"])
    ap.add_argument("--no-gate", action="store_true", help="naive always-on carry")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    panels = ["kalshi", "proxy"] if args.panel == "both" else [args.panel]
    out = {}
    for p in panels:
        for gated in ([False] if args.no_gate else [False, True]):
            key = f"{p}_{'gated' if gated else 'naive'}"
            out[key] = run_panel(p, gated=gated, fee_scenario=args.fees)

    outdir = SIGNALS_DIR / "funding_carry" / "backtests"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"carry_backtest_{stamp}.json").write_text(json.dumps(out, indent=2, default=str))

    # human summary
    print(f"\n{'='*66}\nPlan 03 funding-carry backtest (fees={args.fees})\n{'='*66}")
    for key, r in out.items():
        if r.get("error"):
            print(f"{key:16} — {r['error']}"); continue
        p = r["portfolio"]
        print(f"{key:16} | {r['n_perps']} perp | ann {p['ann_return_pct']:+6.1f}% | "
              f"Sharpe {p['sharpe_ann']:+.2f} | funding {p['funding_collected']:+.4f} "
              f"price {p['price_pnl']:+.4f} fees {p['fees']:.4f}")
    print("="*66)
    print("读法: funding(收) + price(方向性盈亏) − fees = net。price 负=方向性亏损吃掉carry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
