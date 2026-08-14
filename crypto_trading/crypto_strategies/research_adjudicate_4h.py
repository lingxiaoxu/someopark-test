"""Adjudicate the one live lead: 4h conviction-weighted combination (Plan 11 §G).

The combination study produced a single positive variant — 4h equal-weight
CONVICTION combination, +9.10bps net/episode, day-block bootstrap p=0.0123,
CI95 [0.96, 18.73] — but with NW-t only 0.90 and a median of just +1.42bps
against a mean of +9.10. Mean ≫ median means a right tail is carrying it. Two
tests disagreeing is exactly when a result must be attacked, not adopted.

Five gates, all pre-registered here:

  G1 MULTIPLE TESTING — 8 variants were examined (2 horizons × 4 schemes).
     The Bonferroni-corrected bar is p ≤ 0.05/8 = 0.00625.
  G2 TAIL DEPENDENCE — drop the best episode, the best day, and winsorize at
     the 5/95 percentiles. A real edge survives; a tail artifact does not.
  G3 PARAMETER SENSITIVITY — the entry threshold (1.0 sd) and the |z| clip (3)
     were arbitrary. Sweep them; a real edge is a plateau, not a spike.
  G4 BREADTH — how many of the 10 markets contribute positively? An edge
     carried by one market is that market's story, not a strategy.
  G5 SIGN STABILITY — split the OOS half again: does the first sub-half agree
     with the second? (A crude but honest persistence probe.)

Also re-runs regime conditioning at the MEDIAN vol split, since the 75th-pctile
split left <5 episodes — reported with its own power caveat.
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
HORIZON = "4h"
N_VARIANTS_EXAMINED = 8          # for the Bonferroni bar
BOOT_N = 4000


def _tz(s: pd.Series) -> pd.Series:
    mu = s.rolling(ZWIN, min_periods=48).mean()
    sd = s.rolling(ZWIN, min_periods=48).std(ddof=0)
    return (s - mu) / sd.replace(0.0, np.nan)


def _episodes(score: pd.Series, fwd: pd.Series, k: int, thresh: float) -> pd.DataFrame:
    d = pd.DataFrame({"s": score, "fwd": fwd}).dropna()
    hold = pd.Timedelta(GRID) * k
    rows, until = [], None
    for ts, r in d.iterrows():
        if abs(r.s) < thresh or (until is not None and ts < until):
            continue
        side = np.sign(r.s)
        rows.append({"ts": ts, "net_bps": float(side * r.fwd) - FEE_MM_BPS})
        until = ts + hold
    return pd.DataFrame(rows)


def conviction_episodes(ticker: str, frame: pd.DataFrame, *, thresh: float = 1.0,
                        clip: float = 3.0, vol_mask: str | None = None) -> pd.DataFrame:
    """The 4h conviction-combination episodes for one market (OOS half)."""
    feats = [f for f in (FEATURES if ticker in FULL_MARKETS else ALT_FEATURES)
             if f in frame.columns and not frame[f].dropna().empty]
    if len(feats) < 4:
        return pd.DataFrame()
    k = HORIZON_STEPS[HORIZON]
    mark = frame["mark_mid"]
    fwd = (mark.shift(-k) / mark - 1.0) * 1e4
    zs = pd.DataFrame({f: _tz(frame[f]) for f in feats})
    base = pd.concat([zs, fwd.rename("fwd")], axis=1).dropna()
    if len(base) < 500:
        return pd.DataFrame()
    split = base.index[len(base) // 2]
    is_d, oos_d = base[base.index < split], base[base.index >= split]
    if len(is_d) < 200 or len(oos_d) < 200:
        return pd.DataFrame()
    dirs = {}
    for f in feats:
        ic = is_d[f].corr(is_d.fwd, method="spearman")
        dirs[f] = 0.0 if (pd.isna(ic) or ic == 0) else float(np.sign(ic))
    live = [f for f in feats if dirs[f] != 0]
    if len(live) < 4:
        return pd.DataFrame()

    def conv(df):
        sig = pd.DataFrame({f: dirs[f] * df[f].clip(-clip, clip) / clip for f in live})
        return sig.mean(axis=1)

    sd_is = float(conv(is_d).std(ddof=0)) or 1e-9
    s_oos = conv(oos_d) / sd_is
    if vol_mask and "vol_pct_24h" in frame.columns:
        volp = frame["vol_pct_24h"].reindex(oos_d.index).ffill()
        s_oos = s_oos.where(volp >= 0.5) if vol_mask == "high" else s_oos.where(volp < 0.5)
    ep = _episodes(s_oos, oos_d.fwd, k, thresh)
    if len(ep):
        ep["ticker"] = ticker
    return ep


def boot_p(net: pd.Series, days: pd.Series, seed: int = 17) -> tuple:
    grp = pd.DataFrame({"n": net.values, "d": days.values}).groupby("d").n.apply(list)
    blocks = list(grp)
    if len(blocks) < 3:
        return None, None, len(blocks)
    rng = np.random.default_rng(seed)
    boots = np.array([np.concatenate([blocks[i] for i in
                      rng.integers(0, len(blocks), len(blocks))]).mean()
                      for _ in range(BOOT_N)])
    return (round(float((boots <= 0).mean()), 4),
            [round(float(np.percentile(boots, 2.5)), 2),
             round(float(np.percentile(boots, 97.5)), 2)], len(blocks))


def summarize(ep: pd.DataFrame, label: str) -> dict:
    if len(ep) < 5:
        return {"label": label, "n": len(ep), "note": "thin"}
    net = ep.net_bps.reset_index(drop=True)
    days = ep.ts.dt.date.astype(str).reset_index(drop=True)
    p, ci, nb = boot_p(net, days)
    return {"label": label, "n": len(net),
            "mean": round(float(net.mean()), 2),
            "median": round(float(net.median()), 2),
            "hit": round(float((net > 0).mean()), 3),
            "nw_t": round(float(newey_west_tstat(net)["t_nw"]), 2),
            "boot_p": p, "ci95": ci, "n_day_blocks": nb}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    frames = {}
    for t in FULL_MARKETS + ALT_MARKETS:
        try:
            frames[t] = (build_feature_frame(t) if t in FULL_MARKETS
                         else build_alt_frame(t))
        except Exception as e:                              # noqa: BLE001
            logger.warning("%s skipped: %s", t, str(e)[:50])

    base = pd.concat([conviction_episodes(t, f) for t, f in frames.items()
                      if not conviction_episodes(t, f).empty] or [pd.DataFrame()])
    out: dict = {"n_variants_examined": N_VARIANTS_EXAMINED,
                 "bonferroni_p_bar": round(0.05 / N_VARIANTS_EXAMINED, 5)}
    if base.empty:
        print("no episodes"); return 0
    base = base.sort_values("ts").reset_index(drop=True)
    headline = summarize(base, "headline (thresh=1.0, clip=3)")
    out["headline"] = headline

    print("=" * 104)
    print("ADJUDICATION — 4h conviction-weighted combination")
    print("=" * 104)
    print(f"\nHEADLINE: n={headline['n']} mean={headline['mean']}bps "
          f"median={headline['median']} hit={headline['hit']} "
          f"NW-t={headline['nw_t']} boot_p={headline['boot_p']} "
          f"CI95={headline['ci95']} (day blocks={headline['n_day_blocks']})")

    # ── G1 multiple testing ──
    bar = 0.05 / N_VARIANTS_EXAMINED
    g1 = headline["boot_p"] is not None and headline["boot_p"] <= bar
    print(f"\nG1 MULTIPLE TESTING: {N_VARIANTS_EXAMINED} variants examined → bar p<={bar:.5f}; "
          f"observed p={headline['boot_p']} → {'PASS' if g1 else 'FAIL'}")

    # ── G2 tail dependence ──
    net = base.net_bps
    by_day = net.groupby(base.ts.dt.date).sum()
    ex_best_ep = base.drop(net.idxmax())
    ex_best_day = base[base.ts.dt.date != by_day.idxmax()]
    wins = base.copy()
    wins["net_bps"] = net.clip(net.quantile(0.05), net.quantile(0.95))
    tails = [summarize(ex_best_ep, "drop best episode"),
             summarize(ex_best_day, "drop best day"),
             summarize(wins, "winsorized 5/95")]
    out["tail_tests"] = tails
    print("\nG2 TAIL DEPENDENCE:")
    print(pd.DataFrame(tails)[["label", "n", "mean", "median", "nw_t", "boot_p"]]
          .to_string(index=False))
    g2 = all(t.get("mean", -1) > 0 for t in tails)
    print(f"   → {'PASS' if g2 else 'FAIL'} (all three must stay positive)")

    # ── G3 parameter sensitivity ──
    sweep = []
    for th in (0.75, 1.0, 1.25, 1.5):
        for cl in (2.0, 3.0, 4.0):
            eps = [conviction_episodes(t, f, thresh=th, clip=cl) for t, f in frames.items()]
            eps = [e for e in eps if not e.empty]
            if not eps:
                continue
            e = pd.concat(eps).sort_values("ts")
            if len(e) < 10:
                continue
            n = e.net_bps.reset_index(drop=True)
            sweep.append({"thresh": th, "clip": cl, "n": len(n),
                          "mean": round(float(n.mean()), 2),
                          "nw_t": round(float(newey_west_tstat(n)["t_nw"]), 2)})
    out["sensitivity"] = sweep
    print("\nG3 PARAMETER SENSITIVITY (entry threshold × |z| clip):")
    sw = pd.DataFrame(sweep)
    print(sw.pivot(index="thresh", columns="clip", values="mean").to_string())
    frac_pos = float((sw["mean"] > 0).mean()) if len(sw) else 0.0
    g3 = frac_pos >= 0.75
    print(f"   fraction of parameter cells positive: {frac_pos:.0%} → "
          f"{'PASS' if g3 else 'FAIL'} (need >=75% — a plateau, not a spike)")

    # ── G4 breadth ──
    per_mkt = (base.groupby("ticker").net_bps
               .agg(n="size", mean="mean").round(2).sort_values("mean", ascending=False))
    out["per_market"] = per_mkt.reset_index().to_dict("records")
    print("\nG4 BREADTH (per-market contribution):")
    print(per_mkt.to_string())
    n_pos = int((per_mkt["mean"] > 0).sum())
    g4 = n_pos >= max(3, len(per_mkt) // 2)
    print(f"   markets positive: {n_pos}/{len(per_mkt)} → {'PASS' if g4 else 'FAIL'}")

    # ── G5 sign stability across OOS sub-halves ──
    mid = base.ts.iloc[len(base) // 2]
    a, b = base[base.ts < mid], base[base.ts >= mid]
    sub = [summarize(a, "OOS first sub-half"), summarize(b, "OOS second sub-half")]
    out["sub_halves"] = sub
    print("\nG5 SIGN STABILITY:")
    print(pd.DataFrame(sub)[["label", "n", "mean", "median", "nw_t"]].to_string(index=False))
    g5 = all(s.get("mean", -1) > 0 for s in sub)
    print(f"   → {'PASS' if g5 else 'FAIL'} (both sub-halves must be positive)")

    # ── regime conditioning at the MEDIAN split (power caveat) ──
    cond = {}
    for lbl in ("high", "low"):
        eps = [conviction_episodes(t, f, vol_mask=lbl) for t, f in frames.items()]
        eps = [e for e in eps if not e.empty]
        if eps:
            cond[lbl] = summarize(pd.concat(eps).sort_values("ts"), f"vol {lbl} (median split)")
    out["regime"] = cond
    print("\nREGIME CONDITIONING (median vol split — 75th pctile left <5 episodes):")
    if cond:
        print(pd.DataFrame(list(cond.values()))[["label", "n", "mean", "median", "nw_t"]]
              .to_string(index=False))

    gates = {"G1_multiple_testing": g1, "G2_tail": g2, "G3_sensitivity": g3,
             "G4_breadth": g4, "G5_sign_stability": g5}
    out["gates"] = gates
    print("\n" + "=" * 104)
    print("VERDICT: " + " | ".join(f"{k}={'PASS' if v else 'FAIL'}" for k, v in gates.items()))
    print("TRADEABLE" if all(gates.values()) else
          "NOT TRADEABLE — the positive headline does not survive adjudication")
    print("=" * 104)

    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"adjudicate_4h_{stamp}.json").write_text(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
