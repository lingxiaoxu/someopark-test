"""Does COMBINING signals beat SELECTING one? (Plan 11 §F — the "强化" test)

Everything so far tested single (market × feature × horizon) cells and then
tried to SELECT the good ones. That failed: IS→OOS rank persistence was +0.065,
so selection is close to random. The standard response in a low-signal-to-noise
environment is the opposite of selection — COMBINE everything and let the noise
average out (DeMiguel-Garlappi-Uppal 1/N; signal averaging). This module tests
that directly, plus two other genuine strengthening levers:

  A) COMBINATION vs SELECTION, at 2h and 4h:
       · equal-weight vote  — mean of sign(z)·direction over all features
       · IS-weighted        — weights ∝ IS mean net (shrunk), no hard top-K cut
       · z-magnitude weight — conviction weighting by |z|
       · single best cell   — the selection baseline that already failed
     Direction per feature is fixed IS-only; combination weights too.

  B) REGIME CONDITIONING: is the gross edge concentrated when volatility /
     liquidation intensity is high? If the edge lives in stressed regimes, then
     trading only there raises edge-per-trade against a constant fee.

  C) CROSS-MARKET DIVERSIFICATION: same combined signal traded across all 10
     markets simultaneously — does pooling independent markets lift the
     portfolio t-stat even when each market alone is insignificant?

All PIT-clean: features/direction/weights from the IS half only, economics on
the OOS half, non-overlapping episodes, net of 10bps maker-maker round trip.
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.trade_stats import newey_west_tstat
from crypto_trading.crypto_strategies.ml_directional.features import (FEATURES, GRID,
                                                                      build_feature_frame)
from crypto_trading.crypto_strategies.research_horizon_atlas import (ALT_FEATURES,
                                                                    ALT_MARKETS,
                                                                    FULL_MARKETS,
                                                                    HORIZON_STEPS,
                                                                    ZWIN,
                                                                    build_alt_frame)

logger = logging.getLogger(__name__)

FEE_MM_BPS = 10.0
HORIZONS = ("2h", "4h")
ENTRY_Q = 1.0          # combined-score threshold in units of its own IS sd
BOOT_N = 3000


def _trailing_z(s: pd.Series) -> pd.Series:
    mu = s.rolling(ZWIN, min_periods=48).mean()
    sd = s.rolling(ZWIN, min_periods=48).std(ddof=0)
    return (s - mu) / sd.replace(0.0, np.nan)


def _episodes_from_score(score: pd.Series, fwd: pd.Series, k: int,
                         thresh: float) -> pd.DataFrame:
    """Non-overlapping trades whenever |score| >= thresh; side = sign(score)."""
    d = pd.DataFrame({"s": score, "fwd": fwd}).dropna()
    hold = pd.Timedelta(GRID) * k
    rows, open_until = [], None
    for ts, r in d.iterrows():
        if abs(r.s) < thresh or (open_until is not None and ts < open_until):
            continue
        side = np.sign(r.s)
        rows.append({"ts": ts, "side": float(side),
                     "net_bps": float(side * r.fwd) - FEE_MM_BPS})
        open_until = ts + hold
    return pd.DataFrame(rows)


def market_study(ticker: str, frame: pd.DataFrame, horizon: str) -> dict:
    """Build every combination scheme for one market/horizon."""
    feats = [f for f in (FEATURES if ticker in FULL_MARKETS else ALT_FEATURES)
             if f in frame.columns and not frame[f].dropna().empty]
    if len(feats) < 4:
        return {}
    k = HORIZON_STEPS[horizon]
    mark = frame["mark_mid"]
    fwd = (mark.shift(-k) / mark - 1.0) * 1e4
    zs = pd.DataFrame({f: _trailing_z(frame[f]) for f in feats})
    base = pd.concat([zs, fwd.rename("fwd")], axis=1).dropna()
    if len(base) < 500:
        return {}
    split = base.index[len(base) // 2]
    is_d, oos_d = base[base.index < split], base[base.index >= split]
    if len(is_d) < 200 or len(oos_d) < 200:
        return {}

    # ── IS-only: per-feature direction and per-feature IS quality ──
    dirs, is_mean = {}, {}
    for f in feats:
        ic = is_d[f].corr(is_d.fwd, method="spearman")
        if pd.isna(ic) or ic == 0:
            dirs[f], is_mean[f] = 0.0, 0.0
            continue
        dirs[f] = float(np.sign(ic))
        r = dirs[f] * np.sign(is_d[f]) * is_d.fwd - FEE_MM_BPS
        is_mean[f] = float(r.mean())
    live = [f for f in feats if dirs[f] != 0]
    if len(live) < 4:
        return {}

    def score_of(df: pd.DataFrame, weights: dict) -> pd.Series:
        w = pd.Series({f: weights.get(f, 0.0) for f in live})
        if w.abs().sum() == 0:
            return pd.Series(0.0, index=df.index)
        w = w / w.abs().sum()
        sig = pd.DataFrame({f: dirs[f] * np.sign(df[f]) for f in live})
        return sig.mul(w, axis=1).sum(axis=1)

    def zscore_of(df: pd.DataFrame, weights: dict) -> pd.Series:
        """Conviction version: weight by clipped |z| instead of just the sign."""
        w = pd.Series({f: weights.get(f, 0.0) for f in live})
        if w.abs().sum() == 0:
            return pd.Series(0.0, index=df.index)
        w = w / w.abs().sum()
        sig = pd.DataFrame({f: dirs[f] * df[f].clip(-3, 3) / 3.0 for f in live})
        return sig.mul(w, axis=1).sum(axis=1)

    # shrunk IS weights: positive part of IS mean, shrunk toward equal weight
    pos = {f: max(is_mean[f], 0.0) for f in live}
    tot = sum(pos.values())
    eq = {f: 1.0 / len(live) for f in live}
    shrunk = ({f: 0.5 * eq[f] + 0.5 * (pos[f] / tot) for f in live} if tot > 0 else eq)

    schemes = {
        "equal_weight_vote": (score_of, eq),
        "is_shrunk_weight": (score_of, shrunk),
        "equal_weight_conviction": (zscore_of, eq),
    }
    out = {"ticker": ticker, "horizon": horizon, "n_features": len(live),
           "directions": {f: dirs[f] for f in live},
           "is_mean_per_feature": {f: round(is_mean[f], 2) for f in live}}

    for name, (fn, w) in schemes.items():
        s_is = fn(is_d, w)
        sd_is = float(s_is.std(ddof=0)) or 1e-9
        s_oos = fn(oos_d, w) / sd_is           # scale fixed IS-only
        ep = _episodes_from_score(s_oos, oos_d.fwd, k, ENTRY_Q)
        if len(ep) < 5:
            out[name] = {"n": len(ep), "note": "thin"}
            continue
        net = ep.net_bps.reset_index(drop=True)
        out[name] = {"n": len(ep), "mean_net_bps": round(float(net.mean()), 2),
                     "hit": round(float((net > 0).mean()), 3),
                     "nw_t": round(float(newey_west_tstat(net)["t_nw"]), 2),
                     "_series": net.tolist(),
                     "_ts": ep.ts.astype(str).tolist()}
    # selection baseline: the single best IS feature, traded alone
    best_f = max(live, key=lambda f: is_mean[f])
    s_oos = (dirs[best_f] * np.sign(oos_d[best_f]))
    ep = _episodes_from_score(s_oos, oos_d.fwd, k, 0.5)
    if len(ep) >= 5:
        net = ep.net_bps.reset_index(drop=True)
        out["single_best_IS"] = {"feature": best_f, "n": len(ep),
                                 "mean_net_bps": round(float(net.mean()), 2),
                                 "nw_t": round(float(newey_west_tstat(net)["t_nw"]), 2),
                                 "_series": net.tolist()}
    # ── regime conditioning on the equal-weight scheme ──
    if "vol_pct_24h" in frame.columns and "equal_weight_vote" in out and \
            "_series" in out.get("equal_weight_vote", {}):
        volp = frame["vol_pct_24h"].reindex(oos_d.index).ffill()
        s_oos = score_of(oos_d, eq) / (float(score_of(is_d, eq).std(ddof=0)) or 1e-9)
        for label, mask in [("high_vol", volp >= 0.75), ("low_vol", volp < 0.75)]:
            ep = _episodes_from_score(s_oos.where(mask), oos_d.fwd, k, ENTRY_Q)
            if len(ep) >= 5:
                net = ep.net_bps.reset_index(drop=True)
                out[f"cond_{label}"] = {
                    "n": len(ep), "mean_net_bps": round(float(net.mean()), 2),
                    "nw_t": round(float(newey_west_tstat(net)["t_nw"]), 2)}
    return out


def run() -> dict:
    frames = {}
    for t in FULL_MARKETS + ALT_MARKETS:
        try:
            frames[t] = (build_feature_frame(t) if t in FULL_MARKETS
                         else build_alt_frame(t))
        except Exception as e:                              # noqa: BLE001
            logger.warning("%s skipped: %s", t, str(e)[:60])
    results = []
    for hz in HORIZONS:
        for t, fr in frames.items():
            try:
                r = market_study(t, fr, hz)
            except Exception as e:                          # noqa: BLE001
                logger.warning("%s %s: %s", t, hz, str(e)[:60])
                continue
            if r:
                results.append(r)
                logger.info("%s %s done", t, hz)

    # ── cross-market portfolio: pool the equal-weight scheme across markets ──
    portfolio = {}
    rng = np.random.default_rng(13)
    for hz in HORIZONS:
        for scheme in ("equal_weight_vote", "is_shrunk_weight",
                       "equal_weight_conviction", "single_best_IS"):
            series, stamps = [], []
            for r in results:
                if r["horizon"] != hz:
                    continue
                cell = r.get(scheme, {})
                if "_series" in cell:
                    series += cell["_series"]
                    stamps += cell.get("_ts", [None] * len(cell["_series"]))
            if len(series) < 20:
                continue
            arr = pd.Series(series)
            nw = newey_west_tstat(arr)
            entry = {"n_episodes": len(arr),
                     "mean_net_bps": round(float(arr.mean()), 2),
                     "median_net_bps": round(float(arr.median()), 2),
                     "hit": round(float((arr > 0).mean()), 3),
                     "nw_t": round(float(nw["t_nw"]), 2)}
            # day-block bootstrap when timestamps are available
            days = [s[:10] if s else "x" for s in stamps]
            grp = pd.DataFrame({"net": series, "day": days}).groupby("day").net.apply(list)
            if len(grp) >= 3:
                blocks = list(grp)
                boots = np.array([np.concatenate([blocks[i] for i in
                                  rng.integers(0, len(blocks), len(blocks))]).mean()
                                  for _ in range(BOOT_N)])
                entry["boot_p_le_zero"] = round(float((boots <= 0).mean()), 4)
                entry["boot_ci95"] = [round(float(np.percentile(boots, 2.5)), 2),
                                      round(float(np.percentile(boots, 97.5)), 2)]
                entry["n_days"] = len(blocks)
            portfolio[f"{hz}_{scheme}"] = entry

    for r in results:                       # strip heavy payloads before saving
        for k in list(r):
            if isinstance(r[k], dict):
                r[k].pop("_series", None)
                r[k].pop("_ts", None)
    return {"per_market": results, "portfolio": portfolio,
            "fee_mm_bps": FEE_MM_BPS, "entry_q": ENTRY_Q}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    res = run()
    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"combination_{stamp}.json").write_text(json.dumps(res, indent=1, default=str))

    print("=" * 108)
    print("A) COMBINE vs SELECT — cross-market portfolio (OOS half, net of 10bps RT)")
    print("=" * 108)
    rows = [{"variant": k, **v} for k, v in res["portfolio"].items()]
    if rows:
        df = pd.DataFrame(rows)
        cols = [c for c in ["variant", "n_episodes", "n_days", "mean_net_bps",
                            "median_net_bps", "hit", "nw_t", "boot_p_le_zero",
                            "boot_ci95"] if c in df.columns]
        print(df[cols].to_string(index=False))
    print("\n" + "=" * 108)
    print("B) REGIME CONDITIONING (equal-weight scheme, per market)")
    print("=" * 108)
    cond = []
    for r in res["per_market"]:
        hi, lo = r.get("cond_high_vol"), r.get("cond_low_vol")
        if hi and lo:
            cond.append({"ticker": r["ticker"], "horizon": r["horizon"],
                         "high_vol_n": hi["n"], "high_vol_net": hi["mean_net_bps"],
                         "low_vol_n": lo["n"], "low_vol_net": lo["mean_net_bps"],
                         "hi_minus_lo": round(hi["mean_net_bps"] - lo["mean_net_bps"], 2)})
    if cond:
        c = pd.DataFrame(cond)
        print(c.to_string(index=False))
        print(f"\n  median(high_vol − low_vol) = {c.hi_minus_lo.median():+.2f} bps "
              f"| markets where high-vol is better: {(c.hi_minus_lo > 0).sum()}/{len(c)}")
    print("\nREAD: if equal-weight combination clears zero with t>=2 and bootstrap "
          "p<=0.05 where single-cell selection could not, combination is the answer; "
          "if all variants sit at the same negative level, the signals themselves "
          "carry no exploitable edge at this fee level.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
