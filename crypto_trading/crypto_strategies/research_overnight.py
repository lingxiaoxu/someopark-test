"""The last unexplored horizons: 8h / 12h / 24h on the candle history (Plan 14).

The horizon atlas stopped at 4h because the poll tape starts 2026-07-07. But the
1h CANDLE backfill reaches back to early June — ~8 weeks. At 24h that is ~55
non-overlapping episodes per market × 13 markets ≈ 700 pooled episodes: enough
for a first honest read on the one region of the horizon axis never tested.

Why these horizons could differ in kind, not just degree:
  * fee arithmetic — 10bps RT against a typical 24h move of 200-400bps means a
    signal only needs to capture ~3-5% of the move (at 4h it needed ~30%);
  * the adverse-selection tax is per-trade and constant, so it too shrinks
    relative to the edge;
  * crypto daily momentum/reversal is the one horizon family with a real
    academic literature behind it.

Signals (all trailing, PIT by construction on end-labelled 1h candles):
  mom_24h / mom_72h  — sign continuation vs reversal (direction fixed IS-only)
  vol-scaled mom     — mom_24h / trailing σ (literature-standard normalisation)
  funding_tilt       — as-of last settled funding sign (collect-side tilt)

Protocol identical to the atlas: direction on the IS half, economics on the OOS
half, non-overlapping episodes (cooldown = horizon), 10bps maker-maker fees,
NW-t + pooled cross-market read + day-block bootstrap. Execution realism note:
candle closes are not tradeable quotes; anything that passes here goes to the
poll-tape fill-aware harness on the overlapping window before it is believed.
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.loader import load_funding, load_perp_candles
from crypto_trading.crypto_common.trade_stats import newey_west_tstat

logger = logging.getLogger(__name__)

FEE_MM_BPS = 10.0
HORIZONS = {"8h": 8, "12h": 12, "24h": 24}
MARKETS = ["KXBTCPERP", "KXETHPERP", "KXSOLPERP", "KXXRPPERP", "KXDOGEPERP",
           "KXBCHPERP", "KXLTCPERP", "KXLINKPERP", "KXSUIPERP", "KXNEARPERP",
           "KXZECPERP", "KXDOTPERP", "KXAVAXPERP"]
Q = 1.0                       # |z| entry threshold on the trailing-z signal
ZWIN = 168                    # 7d of hourly bars


def hourly_close(ticker: str) -> pd.Series:
    c = load_perp_candles(ticker, "1h")
    if c.empty:
        raise FileNotFoundError(ticker)
    px = c["price_close"] if "price_close" in c.columns else (c.bid_close + c.ask_close) / 2
    return px.dropna().sort_index()


def build_signals(px: pd.Series, ticker: str) -> pd.DataFrame:
    f = pd.DataFrame(index=px.index)
    f["mom_24h"] = px.pct_change(24)
    f["mom_72h"] = px.pct_change(72)
    sigma = px.pct_change().rolling(ZWIN, min_periods=48).std(ddof=0)
    f["mom_24h_volscaled"] = f["mom_24h"] / (sigma * np.sqrt(24)).replace(0, np.nan)
    try:
        fund = load_funding(ticker)["funding_rate"].sort_index()
        state = fund.resample("1h", label="right", closed="right").last().ffill()
        f["funding_tilt"] = state.reindex(px.index).ffill()
    except FileNotFoundError:
        f["funding_tilt"] = np.nan
    return f


def _tz(s: pd.Series) -> pd.Series:
    mu = s.rolling(ZWIN, min_periods=48).mean()
    sd = s.rolling(ZWIN, min_periods=48).std(ddof=0)
    return (s - mu) / sd.replace(0.0, np.nan)


def episodes(z: pd.Series, fwd: pd.Series, direction: float, k: int,
             q: float = Q) -> pd.DataFrame:
    d = pd.DataFrame({"z": z, "fwd": fwd}).dropna()
    hold = pd.Timedelta(hours=k)
    rows, until = [], None
    for ts, r in d.iterrows():
        if abs(r.z) < q or (until is not None and ts < until):
            continue
        side = direction * np.sign(r.z)
        rows.append({"ts": ts, "net_bps": float(side * r.fwd) - FEE_MM_BPS,
                     "gross_bps": float(side * r.fwd)})
        until = ts + hold
    return pd.DataFrame(rows)


def run() -> dict:
    per_cell, pooled = [], {}
    for tk in MARKETS:
        try:
            px = hourly_close(tk)
        except FileNotFoundError:
            continue
        sig = build_signals(px, tk)
        half = px.index[len(px) // 2]
        for feat in sig.columns:
            s = sig[feat]
            if s.dropna().empty:
                continue
            z = _tz(s)
            for hname, k in HORIZONS.items():
                fwd = (px.shift(-k) / px - 1.0) * 1e4
                d = pd.DataFrame({"z": z, "fwd": fwd}).dropna()
                is_d, oos_d = d[d.index < half], d[d.index >= half]
                if len(is_d) < 200 or len(oos_d) < 200:
                    continue
                ic_is = is_d.z.corr(is_d.fwd, method="spearman")
                if pd.isna(ic_is) or ic_is == 0:
                    continue
                direction = float(np.sign(ic_is))
                ep = episodes(oos_d.z, oos_d.fwd, direction, k)
                if len(ep) < 5:
                    continue
                net = ep.net_bps.reset_index(drop=True)
                nw = newey_west_tstat(net)
                per_cell.append({"ticker": tk, "feature": feat, "horizon": hname,
                                 "dir": direction, "ic_is": round(float(ic_is), 3),
                                 "n": len(ep),
                                 "gross": round(float(ep.gross_bps.mean()), 2),
                                 "net": round(float(net.mean()), 2),
                                 "hit": round(float((net > 0).mean()), 3),
                                 "nw_t": round(float(nw["t_nw"]), 2)})
                pooled.setdefault((feat, hname), []).append(ep.assign(ticker=tk))
        logger.info("%s done (%d cells)", tk, len(per_cell))

    fam = []
    rng = np.random.default_rng(29)
    for (feat, hname), parts in pooled.items():
        allep = pd.concat(parts).sort_values("ts")
        if len(allep) < 30:
            continue
        net = allep.net_bps.reset_index(drop=True)
        nw = newey_west_tstat(net)
        groups = [g.net_bps.to_numpy() for _, g in allep.groupby(allep.ts.dt.date)]
        bp = None
        if len(groups) >= 5:
            boots = np.array([np.concatenate([groups[i] for i in
                              rng.integers(0, len(groups), len(groups))]).mean()
                              for _ in range(3000)])
            bp = round(float((boots <= 0).mean()), 4)
        n_mkt = allep.ticker.nunique()
        pos_mkt = int((allep.groupby("ticker").net_bps.mean() > 0).sum())
        fam.append({"feature": feat, "horizon": hname, "n": len(allep),
                    "n_markets": n_mkt, "pos_markets": pos_mkt,
                    "gross": round(float(allep.gross_bps.mean()), 2),
                    "net": round(float(net.mean()), 2),
                    "median_net": round(float(net.median()), 2),
                    "hit": round(float((net > 0).mean()), 3),
                    "nw_t": round(float(nw["t_nw"]), 2), "boot_p": bp})
    return {"per_cell": per_cell, "pooled": fam, "fee_mm_bps": FEE_MM_BPS}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    res = run()
    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"overnight_{stamp}.json").write_text(json.dumps(res, indent=1, default=str))

    print("=" * 108)
    print("8h/12h/24h — the last untested horizons (candle history, ~8 weeks, "
          f"13 markets, fees {FEE_MM_BPS}bps RT)")
    print("=" * 108)
    fam = pd.DataFrame(res["pooled"])
    if fam.empty:
        print("no pooled families")
        return 0
    fam = fam.sort_values("nw_t", ascending=False)
    print("\nPOOLED cross-market families (direction fixed IS-only, OOS economics):")
    print(fam.to_string(index=False))
    surv = fam[(fam.net > 0) & (fam.nw_t >= 2.0) & (fam.boot_p.fillna(1) <= 0.05)
               & (fam.pos_markets >= fam.n_markets / 2)]
    print(f"\nSURVIVORS (net>0, t>=2, boot_p<=0.05, breadth>=half): {len(surv)}")
    if len(surv):
        print(surv.to_string(index=False))
        print("\nNEXT: anything here must pass the poll-tape fill-aware harness "
              "on the overlapping window before it is believed.")
    cells = pd.DataFrame(res["per_cell"])
    if not cells.empty:
        print(f"\nper-cell: {len(cells)} cells | net>0: {(cells.net > 0).mean():.0%} "
              f"| |t|>=2: {(cells.nw_t.abs() >= 2).sum()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
