"""Buy-and-HOLD hedged funding carry — the structurally correct answer to a
per-trade fee wall (Plan 12).

Everything that failed so far tried to PREDICT PRICE and paid the fee on every
trade. Two facts change the arithmetic completely:

  1. Fees are charged per ROUND TRIP, not per unit time. A position held 90 days
     pays the same 10bps as one held 5 minutes. So an edge that accrues with
     TIME rather than with trades makes the fee wall irrelevant: BCH funding is
     −7.59%/yr, i.e. 1.87% over 90 days against a 0.10% fee — fees are 5% of the
     edge instead of 1000% of it.
  2. Kalshi funding signs are extraordinarily persistent: BCH/SUI/ZEC/LTC/NEAR
     all have sign-consistency 1.000 over 140 cycles (47 days — EVERY cycle
     negative), while BTC is +4.10% at 0.978. Long the negative-funding alts and
     short BTC and BOTH legs collect. The hedge pays for itself.

Why the earlier cross-sectional carry died (diagnosed, not guessed): it
rebalanced EVERY cycle — turnover 0.56/cycle × 140 cycles burned 7.9% in fees to
collect 1.2% of funding, while unhedged price P&L lost another 7.6%. The fee wall
was never the problem; TURNOVER was. This module fixes both:

  * hold the book (rebalance never / monthly / weekly — swept, not assumed),
  * beta-hedge the alt basket against BTC using an IS-estimated beta, so the
    price leg is neutralised instead of dominating,
  * diversify across ALL qualifying alts, so idiosyncratic basis vol falls ~1/√n
    (the single-name version was Sharpe 0.31; that was one name, not a basket).

PIT: universe qualification and betas use the IS half only; the OOS half is
traded with the committed book. Funding is credited only for cycles the position
is actually open (Kalshi settles at 04:00/12:00/20:00 UTC).
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.costs import load_fee_rates
from crypto_trading.crypto_common.loader import (load_funding, load_perp_candles,
                                                 load_poll_market_stats)
from crypto_trading.crypto_common.trade_stats import newey_west_tstat

logger = logging.getLogger(__name__)

HEDGE = "KXBTCPERP"
CANDIDATES = ["KXBCHPERP", "KXSUIPERP", "KXZECPERP", "KXLTCPERP", "KXNEARPERP",
              "KXETHPERP", "KXSOLPERP", "KXDOGEPERP", "KXLINKPERP", "KXXRPPERP"]
MIN_SIGN_CONSISTENCY = 0.95      # agreement among the weeks that DID pay
MIN_WEEKS_PAYING = 0.6           # ≥60% of calendar weeks must actually pay
MIN_ABS_ANN_PCT = 1.5
REBALANCE_DAYS = (0, 7, 30)          # 0 = never (pure buy-and-hold)
GRID = "1h"


def _price_panel(tickers: list[str], *, source: str = "candles") -> pd.DataFrame:
    """Hourly price panel.

    ``candles`` (default): Kalshi 1h candles, end_period_ts labelled — these are
    backfilled to early June, giving ~7 weeks for beta estimation and the hold
    simulation. The poller's own quote record only starts 2026-07-07, and a beta
    fitted on 12 days of hourly data is so noisy that the "hedge" raised
    portfolio vol from 18% to 24% — the hedge has to be estimated on real
    history, not on a fortnight.
    ``poll``: recorded tradeable mid (shorter, but a genuine executable price).
    """
    cols = {}
    for t in tickers:
        if source == "candles":
            try:
                c = load_perp_candles(t, "1h")
            except FileNotFoundError:
                continue
            if c.empty:
                continue
            px = c["price_close"] if "price_close" in c.columns else None
            if px is None and {"bid_close", "ask_close"} <= set(c.columns):
                px = (c.bid_close + c.ask_close) / 2.0
            if px is None:
                continue
            cols[t] = px.dropna().resample(GRID, label="right", closed="right").last()
        else:
            st = load_poll_market_stats(t)
            if st.empty:
                continue
            mid = ((st.bid + st.ask) / 2.0).dropna()
            if mid.empty:
                continue
            cols[t] = mid.resample(GRID, label="right", closed="right").last()
    px = pd.DataFrame(cols).sort_index()
    return px.ffill(limit=6).dropna(how="all")


def _funding_panel(tickers: list[str], index: pd.DatetimeIndex | None) -> pd.DataFrame:
    """Per-hour funding accrual: each settlement's rate lands on its own hour.

    ``index=None`` returns the full available history (used by the screen);
    passing an index reindexes onto the trading grid (used by the simulation).
    """
    cols = {}
    for t in tickers:
        try:
            f = load_funding(t)["funding_rate"].sort_index()
        except FileNotFoundError:
            continue
        h = f.resample(GRID, label="right", closed="right").sum()
        cols[t] = h if index is None else h.reindex(index).fillna(0.0)
    df = pd.DataFrame(cols).sort_index()
    return df.fillna(0.0) if index is None else df.reindex(index).fillna(0.0)


def qualify(fund_is: pd.DataFrame) -> dict:
    """IS-only universe screen — WEEK-LEVEL persistence, not sign-consistency.

    The naive screen (sign of nonzero cycles) is a trap: KXBCHPERP scored
    consistency 1.000 and −7.59%/yr while 74% of its cycles paid nothing and its
    entire carry came from two weeks in mid-June, followed by five consecutive
    weeks of EXACTLY zero. Conditioning on nonzero cycles hides a dead regime.
    A name qualifies only if MOST CALENDAR WEEKS pay, and pay the same sign.
    """
    out = {}
    for t in fund_is.columns:
        f = fund_is[t]
        if f.abs().sum() == 0:
            continue
        wk = f.groupby(pd.Grouper(freq="7D")).mean() * 24 * 365 * 100   # weekly ann %
        wk = wk[wk.notna()]
        if len(wk) < 2:
            continue
        paying = wk[wk.abs() >= 0.5]                    # weeks that actually paid
        frac_paying = len(paying) / len(wk)
        if len(paying) == 0:
            continue
        sign = float(np.sign(paying.mean()))
        same_sign = float((np.sign(paying) == sign).mean())
        ann = float(f.mean() * 24 * 365 * 100)
        if (frac_paying >= MIN_WEEKS_PAYING and same_sign >= MIN_SIGN_CONSISTENCY
                and abs(ann) >= MIN_ABS_ANN_PCT):
            out[t] = {"sign": sign, "ann_pct": round(ann, 2),
                      "weeks": len(wk), "frac_weeks_paying": round(frac_paying, 2),
                      "week_sign_agreement": round(same_sign, 2),
                      "worst_week_ann": round(float(wk.min()), 2),
                      "best_week_ann": round(float(wk.max()), 2)}
    return out


def build_book(qual: dict, ret_is: pd.DataFrame) -> pd.Series:
    """Long negative-funding names (collect), short positive-funding names,
    then neutralise net beta with the BTC hedge (which itself collects)."""
    legs = {t: (-1.0 if v["sign"] > 0 else 1.0) for t, v in qual.items() if t != HEDGE}
    if not legs:
        return pd.Series(dtype=float)
    w = pd.Series({t: s / len(legs) for t, s in legs.items()})   # equal gross per name

    if HEDGE in ret_is.columns:
        betas = {}
        for t in w.index:
            if t not in ret_is.columns:
                continue
            d = pd.concat([ret_is[t], ret_is[HEDGE]], axis=1).dropna()
            if len(d) < 50 or d.iloc[:, 1].var() == 0:
                betas[t] = 1.0
            else:
                betas[t] = float(np.cov(d.iloc[:, 0], d.iloc[:, 1])[0, 1] / d.iloc[:, 1].var())
        net_beta = float(sum(w[t] * betas.get(t, 1.0) for t in w.index))
        w[HEDGE] = w.get(HEDGE, 0.0) - net_beta          # hedge to zero net beta
        logger.info("net beta before hedge %.3f → hedge weight %.3f", net_beta, w[HEDGE])
    return w


def simulate(w0: pd.Series, ret: pd.DataFrame, fund: pd.DataFrame,
             rebalance_days: int, fee_rt_bps: float) -> dict:
    """Hold the book; optionally rebalance back to w0 every N days."""
    idx = ret.index
    cols = [c for c in w0.index if c in ret.columns]
    w0 = w0[cols]
    gross = float(w0.abs().sum())
    if gross == 0:
        return {}
    w = w0.copy()
    entry_cost = gross * fee_rt_bps / 2 / 1e4          # one side at entry
    equity, rows = 1.0 - entry_cost, []
    last_reb = idx[0]
    turnover_total = gross
    for ts in idx:
        r = ret.loc[ts, cols].fillna(0.0)
        f = fund.loc[ts, cols].fillna(0.0) if ts in fund.index else pd.Series(0.0, index=cols)
        price_pnl = float((w * r).sum())
        funding_pnl = float((w * (-f)).sum())           # long pays positive rate
        equity *= (1.0 + price_pnl + funding_pnl)
        # weights drift with price unless rebalanced
        w = w * (1.0 + r)
        cost = 0.0
        if rebalance_days and (ts - last_reb).days >= rebalance_days:
            scale = float(w.abs().sum()) or 1.0
            target = w0 * (scale / gross)
            turn = float((target - w).abs().sum())
            cost = turn * fee_rt_bps / 2 / 1e4
            equity *= (1.0 - cost)
            turnover_total += turn
            w, last_reb = target, ts
        rows.append({"ts": ts, "price": price_pnl, "funding": funding_pnl,
                     "cost": cost, "equity": equity})
    df = pd.DataFrame(rows).set_index("ts")
    exit_cost = float(w.abs().sum()) * fee_rt_bps / 2 / 1e4
    df.loc[df.index[-1], "equity"] *= (1.0 - exit_cost)
    turnover_total += float(w.abs().sum())

    days = max((idx[-1] - idx[0]).days, 1)
    total = float(df.equity.iloc[-1]) - 1.0
    daily = df.equity.pct_change().dropna()
    dd = float((df.equity / df.equity.cummax() - 1.0).min())
    nw = newey_west_tstat(daily.resample("1D").sum().dropna())
    return {
        "rebalance_days": rebalance_days, "days": days,
        "gross_exposure": round(gross, 3),
        "total_return_pct": round(total * 100, 3),
        "ann_return_pct": round(((1 + total) ** (365 / days) - 1) * 100, 2),
        "funding_cum_pct": round(float(df.funding.sum()) * 100, 3),
        "price_cum_pct": round(float(df.price.sum()) * 100, 3),
        "fees_cum_pct": round((entry_cost + float(df.cost.sum()) + exit_cost) * 100, 3),
        "turnover_total": round(turnover_total, 2),
        "max_drawdown_pct": round(dd * 100, 2),
        "ann_vol_pct": round(float(daily.std() * np.sqrt(24 * 365)) * 100, 2),
        "sharpe": round(float(daily.mean() / daily.std() * np.sqrt(24 * 365)), 3)
        if daily.std() > 0 else None,
        "daily_nw_t": round(float(nw["t_nw"]), 2),
        "_equity": df.equity,
    }


def run(*, hedged: bool = True, source: str = "candles") -> dict:
    px = _price_panel(CANDIDATES + [HEDGE], source=source)
    if px.empty:
        raise RuntimeError("no price panel")
    fund = _funding_panel(list(px.columns), px.index)
    ret = px.pct_change().fillna(0.0)
    split = px.index[len(px) // 2]
    ret_is = ret[ret.index < split]
    # The screen needs FUNDING history only, and funding is backfilled well before
    # the poller's price record begins — so screen on every settlement strictly
    # before the OOS start (still PIT), not just the price panel's IS half. That
    # is ~7 weeks of week-level evidence instead of ~2.
    fund_hist = _funding_panel(list(px.columns), None)
    qual = qualify(fund_hist[fund_hist.index < split])
    logger.info("qualified universe (IS-only): %s", json.dumps(qual, indent=1))
    if not qual:
        return {"error": "no market passed the IS screen"}

    w = build_book(qual, ret_is) if hedged else pd.Series(
        {t: (-1.0 if v["sign"] > 0 else 1.0) / len(qual) for t, v in qual.items()})
    ret_oos, fund_oos = ret[ret.index >= split], fund[fund.index >= split]
    maker_rate, _ = load_fee_rates(HEDGE)
    fee_rt = maker_rate * 1e4 * 2                      # maker both sides, in bps

    runs = []
    for rb in REBALANCE_DAYS:
        r = simulate(w, ret_oos, fund_oos, rb, fee_rt)
        if r:
            r.pop("_equity", None)
            runs.append(r)
    return {"universe": qual, "weights": {k: round(float(v), 4) for k, v in w.items()},
            "hedged": hedged, "fee_rt_bps": fee_rt,
            "oos_start": str(split), "runs": runs}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--unhedged", action="store_true", help="skip the BTC beta hedge")
    ap.add_argument("--source", default="candles", choices=["candles", "poll"],
                    help="price panel source (candles = longer history)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    out = {}
    for hedged in ([False] if args.unhedged else [True, False]):
        res = run(hedged=hedged, source=args.source)
        out["hedged" if hedged else "unhedged"] = res
        label = "HEDGED (alts long + BTC beta short)" if hedged else "UNHEDGED (alts only)"
        print("=" * 100)
        print(f"{label}   fee round-trip = {res.get('fee_rt_bps')} bps")
        print("=" * 100)
        if "error" in res:
            print(" ", res["error"]); continue
        print("universe (qualified on pre-OOS funding history only):")
        for t, v in res["universe"].items():
            print(f"   {t:12} funding {v['ann_pct']:+7.2f}%/yr over {v['weeks']}w | "
                  f"paying weeks {v['frac_weeks_paying']:.0%} | sign agree "
                  f"{v['week_sign_agreement']:.0%} | worst/best week "
                  f"{v['worst_week_ann']:+.1f}/{v['best_week_ann']:+.1f} "
                  f"→ {'LONG' if v['sign'] < 0 else 'SHORT'}")
        print(f"weights: {res['weights']}")
        print(f"\nOOS from {res['oos_start']}:")
        df = pd.DataFrame(res["runs"])
        cols = ["rebalance_days", "days", "total_return_pct", "ann_return_pct",
                "funding_cum_pct", "price_cum_pct", "fees_cum_pct", "turnover_total",
                "ann_vol_pct", "max_drawdown_pct", "sharpe", "daily_nw_t"]
        print(df[[c for c in cols if c in df.columns]].to_string(index=False))
        print()

    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"carry_hold_{stamp}.json").write_text(json.dumps(out, indent=1, default=str))
    print("READ: rebalance_days=0 is pure buy-and-hold — fees are paid ONCE. "
          "funding_cum should dominate fees_cum; price_cum is what the hedge is "
          "meant to neutralise. A real carry strategy shows funding_cum > 0, "
          "|price_cum| small, fees_cum tiny.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
