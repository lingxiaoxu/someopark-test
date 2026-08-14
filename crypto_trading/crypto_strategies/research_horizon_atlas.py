"""Horizon atlas — is there ANY (feature × horizon × market) cell that clears
the fee wall at 5min–4h?  (Plan 11 §A, post-PIT-audit)

Protocol (pre-registered, leak-free):
  * features: ml_directional.build_feature_frame (PIT-audited clean) for
    BTC/ETH; a lighter same-discipline frame (mom/flow/oi/funding/vol) for alts.
  * every feature is trailing-z-scored (288 bars = 24h, PIT) for comparability.
  * horizons: 1/3/6/12/24/48 grid steps (5m grid) = 5m..4h forward returns.
  * DIRECTION of each cell is fixed on the FIRST HALF of the sample only
    (sign of first-half IC); per-signal economics are measured on the SECOND
    HALF only. Full-sample IC is reported as descriptive.
  * significance: NW-t with lags = horizon steps + 2; Bonferroni across the
    whole atlas (all cells actually computed).
  * economics per cell (OOS half, |z| >= Q): signals/day, mean gross bps per
    signal (direction-conditioned forward return), net = gross − 10bps
    (maker-maker RT; taker-taker would be 20bps).

Honest caveats printed with the output: ~22d of tape; 4h horizon has heavy
overlap (NW handles autocorr, not regime concentration); nothing here is a
tradeable claim until it survives a fill-aware backtest.
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.loader import (load_funding,
                                                 load_poll_market_stats,
                                                 load_poll_trades)
from crypto_trading.crypto_common.trade_stats import newey_west_tstat
from crypto_trading.crypto_strategies.ml_directional.features import (
    FEATURES, GRID, build_feature_frame, _signed_flow)

logger = logging.getLogger(__name__)

HORIZON_STEPS = {"5m": 1, "15m": 3, "30m": 6, "60m": 12, "2h": 24, "4h": 48}
ZWIN = 288                       # 24h trailing z window on the 5-min grid
Q_THRESH = (1.0, 1.5, 2.0)       # |z| entry thresholds for per-signal economics
FEE_MM_BPS = 10.0                # maker-maker round trip (tier-0)

FULL_MARKETS = ["KXBTCPERP", "KXETHPERP"]
ALT_MARKETS = ["KXSOLPERP", "KXXRPPERP", "KXDOGEPERP", "KXBCHPERP",
               "KXLTCPERP", "KXLINKPERP", "KXSUIPERP", "KXNEARPERP"]
ALT_FEATURES = ["flow_imb_5m", "flow_imb_15m", "flow_imb_60m",
                "oi_delta_15m", "oi_delta_60m", "funding_streak_signed",
                "funding_mag", "vol_pct_24h", "mom_15m", "mom_1h", "mom_4h"]


def build_alt_frame(ticker: str) -> pd.DataFrame:
    """Alt feature frame — the composite/strips-free subset, same PIT patterns
    (right/right resample, trailing windows, merge_asof backward)."""
    stats = load_poll_market_stats(ticker)
    trades = load_poll_trades(ticker).sort_index()
    if stats.empty or trades.empty:
        raise RuntimeError(f"no recorded tape for {ticker}")

    mid = ((stats.bid + stats.ask) / 2.0).dropna()
    grid_mid = mid.resample(GRID, label="right", closed="right").last().dropna()
    grid = grid_mid.index
    f = pd.DataFrame(index=grid)
    f["mark_mid"] = grid_mid

    for w, name in [("5min", "flow_imb_5m"), ("15min", "flow_imb_15m"),
                    ("60min", "flow_imb_60m")]:
        f[name] = _signed_flow(trades, grid, w)

    oi = stats.oi.dropna().resample(GRID, label="right", closed="right").last()
    oi = oi.reindex(grid).ffill(limit=3)
    for k, name in [(3, "oi_delta_15m"), (12, "oi_delta_60m")]:
        f[name] = oi.pct_change(k)

    try:
        from crypto_trading.crypto_strategies.ml_directional.features import _streak_series
        fund = load_funding(ticker).sort_index()
        fund = fund.assign(streak_signed=_streak_series(fund))
        fstate = fund[["funding_rate", "streak_signed"]].reset_index(names="dt")
        merged = pd.merge_asof(pd.DataFrame(index=grid).reset_index(names="dt"),
                               fstate, on="dt", direction="backward").set_index("dt")
        f["funding_streak_signed"] = merged["streak_signed"]
        f["funding_mag"] = merged["funding_rate"]
    except FileNotFoundError:
        f["funding_streak_signed"] = f["funding_mag"] = np.nan

    ret5 = f["mark_mid"].pct_change()
    rv = ret5.rolling(288, min_periods=48).std(ddof=0)
    f["vol_pct_24h"] = rv.expanding(min_periods=96).apply(
        lambda x: (x[:-1] <= x[-1]).mean() if len(x) > 1 else np.nan, raw=True)

    f["mom_15m"] = f["mark_mid"].pct_change(3)
    f["mom_1h"] = f["mark_mid"].pct_change(12)
    f["mom_4h"] = f["mark_mid"].pct_change(48)
    return f


def _trailing_z(s: pd.Series) -> pd.Series:
    mu = s.rolling(ZWIN, min_periods=48).mean()
    sd = s.rolling(ZWIN, min_periods=48).std(ddof=0)
    return (s - mu) / sd.replace(0.0, np.nan)


def atlas_for_market(ticker: str, frame: pd.DataFrame,
                     features: list[str]) -> list[dict]:
    rows = []
    mark = frame["mark_mid"]
    half = len(frame) // 2
    for feat in features:
        if feat not in frame.columns or frame[feat].dropna().empty:
            continue
        z = _trailing_z(frame[feat])
        for hname, k in HORIZON_STEPS.items():
            fwd = (mark.shift(-k) / mark - 1.0) * 1e4          # bps
            d = pd.DataFrame({"z": z, "fwd": fwd}).dropna()
            if len(d) < 300:
                continue
            ic_full = float(d.z.corr(d.fwd, method="spearman"))
            # ── pre-registered direction on the FIRST half only ──
            d_is, d_oos = d.iloc[:half], d.iloc[half:]
            if len(d_is) < 150 or len(d_oos) < 150:
                continue
            ic_is = d_is.z.corr(d_is.fwd, method="spearman")
            if pd.isna(ic_is) or ic_is == 0:
                continue
            direction = float(np.sign(ic_is))
            row = {"ticker": ticker, "feature": feat, "horizon": hname,
                   "n": len(d), "ic_full": round(ic_full, 4),
                   "ic_is": round(float(ic_is), 4), "direction": direction}
            # per-bar direction returns on the OOS half (for NW-t of the cell)
            r_oos = direction * np.sign(d_oos.z) * d_oos.fwd
            nw = newey_west_tstat(pd.Series(r_oos.values), lags=k + 2)
            row["oos_nw_t"] = round(float(nw["t_nw"]), 2)
            row["oos_mean_bps_allbars"] = round(float(r_oos.mean()), 2)
            # thresholded per-signal economics on the OOS half
            days_oos = max((d_oos.index.max() - d_oos.index.min()).days, 1)
            for q in Q_THRESH:
                sig = d_oos[abs(d_oos.z) >= q]
                if len(sig) < 20:
                    row[f"q{q}"] = None
                    continue
                r = direction * np.sign(sig.z) * sig.fwd
                nwq = newey_west_tstat(pd.Series(r.values), lags=k + 2)
                row[f"q{q}"] = {"n": len(sig),
                                "signals_per_day": round(len(sig) / days_oos, 1),
                                "gross_bps": round(float(r.mean()), 2),
                                "net_mm_bps": round(float(r.mean()) - FEE_MM_BPS, 2),
                                "nw_t": round(float(nwq["t_nw"]), 2)}
            rows.append(row)
    return rows


def run_atlas(*, refresh: bool = True) -> dict:
    all_rows: list[dict] = []
    for tk in FULL_MARKETS:
        frame = build_feature_frame(tk) if refresh else None
        if frame is None:
            from crypto_trading.crypto_strategies.ml_directional.features import cached_feature_frame
            frame = cached_feature_frame(tk)
        all_rows += atlas_for_market(tk, frame, FEATURES)
        logger.info("atlas %s done (%d cells)", tk, len(all_rows))
    for tk in ALT_MARKETS:
        try:
            frame = build_alt_frame(tk)
        except (RuntimeError, FileNotFoundError) as e:
            logger.warning("alt %s skipped: %s", tk, e)
            continue
        all_rows += atlas_for_market(tk, frame, ALT_FEATURES)
        logger.info("atlas %s done (%d cells total)", tk, len(all_rows))

    n_cells = len(all_rows)
    # Bonferroni on the OOS all-bars NW-t across every cell actually computed
    from scipy.stats import norm
    bonf_t = float(norm.ppf(1 - 0.025 / max(n_cells, 1)))
    for r in all_rows:
        r["oos_sig_raw"] = abs(r["oos_nw_t"]) >= 2.0
        r["oos_sig_bonf"] = abs(r["oos_nw_t"]) >= bonf_t
    return {"cells": all_rows, "n_cells": n_cells, "bonferroni_t": round(bonf_t, 2),
            "fee_mm_bps": FEE_MM_BPS, "grid": GRID,
            "protocol": "direction fixed on first half; economics measured on second half"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-refresh", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    res = run_atlas(refresh=not args.no_refresh)
    df = pd.DataFrame(res["cells"])
    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"horizon_atlas_{stamp}.json").write_text(json.dumps(res, indent=1, default=str))

    print("=" * 100)
    print(f"HORIZON ATLAS — {res['n_cells']} cells | Bonferroni |t|>= {res['bonferroni_t']} "
          f"| OOS-half protocol | fee wall {FEE_MM_BPS}bps maker-maker RT")
    print("=" * 100)
    sig = df[df.oos_sig_bonf].sort_values("oos_nw_t", key=abs, ascending=False)
    print(f"\n── Bonferroni survivors (OOS half): {len(sig)} ──")
    if len(sig):
        print(sig[["ticker", "feature", "horizon", "n", "ic_full", "oos_nw_t",
                   "oos_mean_bps_allbars"]].to_string(index=False))
    raw = df[df.oos_sig_raw].sort_values("oos_nw_t", key=abs, ascending=False)
    print(f"\n── raw |t|>=2 (OOS half): {len(raw)} — top 25 ──")
    print(raw[["ticker", "feature", "horizon", "n", "ic_full", "oos_nw_t",
               "oos_mean_bps_allbars"]].head(25).to_string(index=False))

    # the question that matters: does ANY thresholded cell clear the fee wall OOS?
    print("\n── cells with q-threshold NET (gross − 10bps) > 0 on the OOS half ──")
    hits = []
    for r in res["cells"]:
        for q in Q_THRESH:
            cell = r.get(f"q{q}")
            if cell and cell["net_mm_bps"] > 0 and cell["nw_t"] >= 2.0:
                hits.append({**{k: r[k] for k in ("ticker", "feature", "horizon")},
                             "q": q, **cell})
    if hits:
        print(pd.DataFrame(hits).sort_values("nw_t", ascending=False).to_string(index=False))
    else:
        print("NONE — no (feature, horizon, threshold) cell clears maker-maker fees "
              "with t>=2 on the held-out half.")
    print("\ncaveats: ~22d tape; 2h/4h cells have heavy overlap + regime concentration; "
          "any hit here still needs a fill-aware backtest before it is a tradeable claim.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
