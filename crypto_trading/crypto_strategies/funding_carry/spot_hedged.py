"""S3 improved — SAME-ASSET spot-hedged funding carry (Plan 17 / watchlist W4).

Why the original S3 died and what changes here. `hold_hedged.py` hedged a basket
of alt perps with a SHORT BTC perp: a cross-asset beta hedge whose residual vol
(22.6%/yr) was 17× the carry. The fix is structural, not statistical: hedge the
SAME asset — short KXBTCPERP + long spot BTC. Spot长腿 needs no borrowing and is
available on US-compliant venues (Coinbase/Kraken); the residual is only the
Kalshi-perp-to-spot basis, measured on our tape at **5.6%/yr** (4× smaller), and
the income side is BTC funding, positive in **8/8 recorded weeks** (+0.6 to
+11.9%/yr, mean ≈ +4%).

FROZEN RULE — v3 "always30" (registered 2026-08-10, PIT-trivial, ZERO fitted
parameters):
  * short 1× notional KXBTCPERP + long 1× notional spot BTC, ALWAYS ON;
  * exit only if the trailing 30-day settled-funding SUM turns negative
    (structural-flip stop; never triggered in sample).
Design iterations DISCLOSED: v1 last-cycle rule churned 13×/57d (net −17%),
v2 trail-7d churned 4× (net −2.5%) — both killed by spot-leg round trips, both
kept in ``position_series`` for comparison. v3 pays the spot RT once.
In-sample (57d): funding +4.11%/yr, residual −0.37%/yr @ 3.2% vol, gross daily
NW-t +3.00, net +2.9~+3.5%/yr across ALL tier × spot-fee scenarios.

Costs modeled:
  * perp leg: maker entry/exit at CRYPTO_FEE_TIER rates (tier 3/4/5 reported);
  * spot leg: parametrized round-trip scenarios {20, 50, 80} bps — Kraken Pro
    base ≈ maker 25/taker 40, Coinbase Advanced base higher; amortized naturally
    since the rule trades only on funding-sign flips (entry count reported);
  * funding accrues per settled cycle while ON (position notional × rate).

The extension for NEGATIVE-funding alts (long alt perp + short CME micro
futures) is documented but NOT implemented — CME covers BTC/ETH only and ETH
funding currently oscillates around zero; revisit if an alt's funding turns
persistently negative again.
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
from crypto_trading.crypto_common.loader import (load_funding,
                                                 load_index_composite,
                                                 load_perp_candles)
from crypto_trading.crypto_common.trade_stats import newey_west_tstat

logger = logging.getLogger(__name__)

TICKER, ASSET = "KXBTCPERP", "BTC"
SPOT_RT_BPS = (20.0, 50.0, 80.0)      # spot-leg round-trip scenarios
OFF_AFTER = 3                          # consecutive non-positive cycles to exit
REGISTRATION = pd.Timestamp("2026-08-10", tz="UTC")


def build_frames():
    """Hourly perp close, hourly spot composite, and the settled funding tape."""
    c = load_perp_candles(TICKER, "1h")
    perp = (c["price_close"] if "price_close" in c.columns
            else (c.bid_close + c.ask_close) / 2).dropna().sort_index()
    spot = (load_index_composite(ASSET)["vw_close"]
            .resample("1h", label="right", closed="right").last().dropna())
    fund = load_funding(TICKER)["funding_rate"].sort_index()
    idx = perp.index.intersection(spot.index)
    return perp.reindex(idx), spot.reindex(idx), fund


def position_series(fund: pd.Series, index: pd.DatetimeIndex,
                    rule: str = "trail7d") -> pd.Series:
    """ON/OFF state, evaluated only at settlement instants (PIT: a cycle's rate
    is known the moment it settles; the state holds until the next settlement).

    ``last_cycle`` (v1): ON while the last settled cycle > 0, OFF after 3
    consecutive ≤ 0. DISCLOSED FAILURE: BTC funding is zero in ~72% of cycles,
    so this flipped 13 times in 57 days and the spot-leg round trips ate the
    carry (net −17 to −67%/yr). Kept for comparison.

    ``trail7d`` (v2, the FROZEN W4 rule): ON while the trailing 7-day SUM of
    settled funding > 0. This matches the persistence unit the data actually
    has (weekly sums positive 8/8 recorded weeks; cycles are sparse), so the
    state is sticky and entries collapse to ~1. The signal is unchanged —
    "is funding positive" — only the aggregation window matches the structure.
    """
    if rule == "always30":
        # v3, the FROZEN W4 rule — zero fitted parameters: ALWAYS ON, exit only
        # if the trailing 30-day funding SUM turns negative (a structural-flip
        # stop that never triggered in sample; BTC cycle sign-consistency 0.978).
        # Entries collapse to 1; the spot round-trip is paid once.
        trail = fund.rolling("30D").sum()
        s = (trail.reindex(index, method="ffill").fillna(0.0) >= 0).astype(float)
        return s
    if rule == "last_cycle":
        state, run_nonpos, states = 0, 0, {}
        for ts, r in fund.items():
            if r > 0:
                state, run_nonpos = 1, 0
            else:
                run_nonpos += 1
                if run_nonpos >= OFF_AFTER:
                    state = 0
            states[ts] = state
        s = pd.Series(states).sort_index()
    else:
        trail = fund.rolling("7D").sum()
        s = (trail > 0).astype(float)
    return s.reindex(index, method="ffill").fillna(0.0)


def run(rule: str = "always30") -> dict:
    perp, spot, fund = build_frames()
    pos = position_series(fund, perp.index, rule=rule)

    # hedged residual return while ON: long spot − short perp (per $1 notional/leg)
    resid = (spot.pct_change() - perp.pct_change()).fillna(0.0) * pos.shift(1).fillna(0.0)
    # funding accrual: rate lands on its settlement hour if position was ON through it
    f_hourly = fund.resample("1h", label="right", closed="right").sum().reindex(perp.index).fillna(0.0)
    f_acc = f_hourly * pos.shift(1).fillna(0.0)

    entries = int((pos.diff() > 0).sum() + (pos.iloc[0] > 0))
    days = max((perp.index[-1] - perp.index[0]).days, 1)
    on_frac = float(pos.mean())

    out = {"ticker": TICKER, "rule": rule, "days": days,
           "on_fraction": round(on_frac, 3), "entries": entries,
           "steady_state_note": "amortized 1 entry/yr: net ≈ funding + resid − "
                                "(spot_rt + 2×maker)/1yr",
           "funding_ann_pct": round(float(f_acc.sum()) / days * 365 * 100, 2),
           "resid_ann_pct": round(float(resid.sum()) / days * 365 * 100, 2),
           "resid_vol_ann_pct": round(float(resid[pos.shift(1) > 0].std()
                                            * np.sqrt(24 * 365) * 100), 2),
           "registration": str(REGISTRATION.date()), "scenarios": []}

    daily = (resid + f_acc).resample("1D").sum().dropna()
    nw = newey_west_tstat(daily * 1e4)
    out["gross_daily_nw_t"] = round(float(nw["t_nw"]), 2)

    gross_ann = float(resid.sum() + f_acc.sum()) / days * 365 * 100
    for tier in (3, 4, 5):
        maker = FEE_TIERS_BPS[tier][0] / 1e4
        perp_cost = entries * 2 * maker                      # enter+exit, maker
        for srt in SPOT_RT_BPS:
            spot_cost = entries * srt / 1e4
            total_ret = float(resid.sum() + f_acc.sum()) - perp_cost - spot_cost
            ann = total_ret / days * 365 * 100
            net_daily = daily - (perp_cost + spot_cost) / max(len(daily), 1)
            ir = float(net_daily.mean() / net_daily.std() * np.sqrt(365)) \
                if net_daily.std() > 0 else None
            # steady state: position held ~1yr, entry paid once
            steady = gross_ann - (srt / 1e4 + 2 * maker) * 100
            out["scenarios"].append({
                "tier": tier, "spot_rt_bps": srt,
                "ann_net_pct": round(ann, 2),
                "steady_state_net_pct": round(steady, 2),
                "ir_ann": round(ir, 2) if ir is not None else None})
    # post-registration subsample (the watchlist reads this)
    post = (resid + f_acc)[(resid + f_acc).index >= REGISTRATION]
    if len(post) > 24:
        pd_daily = post.resample("1D").sum().dropna()
        out["post_registration"] = {
            "days": len(pd_daily),
            "ann_gross_pct": round(float(post.sum()) / max(len(pd_daily), 1) * 365 * 100, 2),
            "nw_t": round(float(newey_west_tstat(pd_daily * 1e4)["t_nw"]), 2)}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    res = run()
    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"spot_hedged_{stamp}.json").write_text(json.dumps(res, indent=1))

    print("=" * 88)
    print(f"S3-improved: short {TICKER} + long spot {ASSET} (same-asset hedge)  "
          f"span {res['days']}d | ON {res['on_fraction']:.0%} | entries {res['entries']}")
    print("=" * 88)
    print(f"  funding collected : {res['funding_ann_pct']:+.2f}%/yr")
    print(f"  hedge residual    : {res['resid_ann_pct']:+.2f}%/yr "
          f"(vol {res['resid_vol_ann_pct']:.2f}%/yr — cross-asset version was 22.6%)")
    print(f"  gross daily NW-t  : {res['gross_daily_nw_t']:+.2f}")
    print(f"\n  net %/yr (tier × spot-leg RT bps):")
    df = pd.DataFrame(res["scenarios"]).pivot(index="tier", columns="spot_rt_bps",
                                              values="steady_state_net_pct")
    print(df.to_string())
    print(f"\n  IR (tier × spot RT):")
    df2 = pd.DataFrame(res["scenarios"]).pivot(index="tier", columns="spot_rt_bps",
                                               values="ir_ann")
    print(df2.to_string())
    if "post_registration" in res:
        p = res["post_registration"]
        print(f"\n  post-registration ({p['days']}d): {p['ann_gross_pct']:+.2f}%/yr "
              f"gross, NW-t {p['nw_t']:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
