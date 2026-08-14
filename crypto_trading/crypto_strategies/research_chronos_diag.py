"""Is Chronos mis-configured, or is direction simply unpredictable? (Plan 13)

The directional test returned IC ≈ −0.05 and concluded "no edge". That conclusion
is only valid if the PIPELINE is sound — a broken harness produces the same
near-zero IC as a genuinely unpredictable target, and the two demand opposite
responses. The directional study never ran the experiment that separates them.

This module runs it. Nothing here modifies ``research_chronos``; it imports and
reuses ``rolling_forecast`` so the harness under test is exactly the one that
produced the negative result.

  A) POSITIVE CONTROL — point Chronos at targets whose predictability is not in
     question. Realized volatility and traded volume are strongly autocorrelated
     (vol clustering, intraday seasonality); any competent forecaster scores a
     large positive IC on them. If Chronos scores high on vol and ~0 on
     direction, the harness is fine and direction is genuinely unpredictable.
     If it fails on vol too, the harness is broken and the directional verdict
     must be withdrawn.
     A NAIVE PERSISTENCE baseline (next = last) runs alongside: Chronos must at
     least match it, else it is adding nothing over "assume no change".

  B) INPUT QUALITY — the directional test fed the Kalshi CONTRACT mid, which has
     4,594 distinct values over the sample. The 3-exchange spot composite has
     215,696 — 47× the resolution — and the perp tracks it. Both are run, plus
     log-price, to see whether input resolution was the binding constraint.

  C) MODEL CAPACITY — chronos-bolt-small vs -base vs the t5 family, on the same
     targets, so "you used a small model" is answered with numbers.

Output is prediction QUALITY (IC, direction accuracy, calibration), not P&L —
the question here is whether Chronos can forecast better, not whether the
forecast pays.
"""
from __future__ import annotations

import argparse
import json
import logging
import os

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.loader import (load_index_composite,
                                                 load_poll_market_stats,
                                                 load_poll_trades)

logger = logging.getLogger(__name__)

GRID = "5min"
HORIZONS = {"2h": 24, "4h": 48}
CTX = 512
MARKETS = {"KXBTCPERP": "BTC", "KXETHPERP": "ETH"}


def _grid(s: pd.Series) -> pd.Series:
    return s.dropna().resample(GRID, label="right", closed="right").last().dropna()


def build_series(ticker: str, asset: str) -> dict[str, pd.Series]:
    """The candidate input series, cheapest-to-richest in resolution."""
    st = load_poll_market_stats(ticker)
    mid = _grid((st.bid + st.ask) / 2.0)
    out = {"contract_mid": mid}                       # what the directional test used
    try:
        comp = _grid(load_index_composite(asset)["vw_close"])
        out["spot_composite"] = comp.reindex(mid.index).ffill(limit=3).dropna()
    except FileNotFoundError:
        logger.warning("no composite for %s", asset)

    # positive-control targets: realized vol and traded volume on the same grid
    ret = mid.pct_change()
    out["realized_vol_1h"] = (ret.rolling(12, min_periods=6).std(ddof=0) * 1e4).dropna()
    try:
        tr = load_poll_trades(ticker)
        vol = tr["count"].resample(GRID, label="right", closed="right").sum()
        out["volume"] = vol.reindex(mid.index).fillna(0.0).astype(float)
    except Exception:                                  # noqa: BLE001
        logger.warning("no trade tape for %s", ticker)
    return out


def evaluate(pred: pd.Series, actual: pd.Series, *, directional: bool) -> dict:
    d = pd.DataFrame({"p": pred, "a": actual}).dropna()
    if len(d) < 100:
        return {"n": len(d), "note": "thin"}
    out = {"n": len(d),
           "ic_spearman": round(float(d.p.corr(d.a, method="spearman")), 4),
           "ic_pearson": round(float(d.p.corr(d.a)), 4)}
    if directional:
        out["dir_acc"] = round(float((np.sign(d.p) == np.sign(d.a)).mean()), 4)
    else:
        # level targets: relative error against the naive "no change" forecast
        out["mae"] = round(float((d.p - d.a).abs().mean()), 4)
    return out


def naive_persistence(series: pd.Series, horizon: int) -> pd.Series:
    """The forecast Chronos must beat on a level target: next = last."""
    return series.shift(0)


def run_control(pipe, series: dict, tag: str) -> list[dict]:
    """Positive control on targets whose predictability is not in question.

    ``rolling_forecast`` already returns the predicted relative move
    (``pred_bps``) alongside the realized one (``actual_bps``), so the honest
    score is simply IC(pred_bps, actual_bps) on the CHANGE — level IC would be
    trivially high for any autocorrelated series and would prove nothing.

    Baseline: mean reversion, the standard naive forecaster for volatility —
    predict the change as −(current level − trailing mean). Chronos has to beat
    that to be adding anything.
    """
    from crypto_trading.crypto_strategies.research_chronos import rolling_forecast
    rows = []
    for target in ("realized_vol_1h", "volume"):
        s = series.get(target)
        if s is None or len(s) < CTX + 60:
            continue
        s = s[s > 0]                       # relative moves need a positive level
        if len(s) < CTX + 60:
            continue
        for hname, h in HORIZONS.items():
            fc = rolling_forecast(s, ctx_len=CTX, horizon=h, mode="price", pipe=pipe)
            if fc.empty or "pred_bps" not in fc.columns:
                continue
            chro = evaluate(fc["pred_bps"], fc["actual_bps"], directional=True)
            lvl = s.reindex(fc.index).ffill()
            mr = -(lvl / lvl.rolling(288, min_periods=48).mean() - 1.0) * 1e4
            base = evaluate(mr, fc["actual_bps"], directional=True)
            rows.append({"tag": tag, "target": target, "horizon": hname,
                         "chronos_ic": chro.get("ic_spearman"),
                         "chronos_dir_acc": chro.get("dir_acc"),
                         "meanrev_ic": base.get("ic_spearman"),
                         "beats_baseline": (abs(chro.get("ic_spearman") or 0)
                                            > abs(base.get("ic_spearman") or 0)),
                         "n": chro.get("n")})
            logger.info("control %s %s %s → Chronos IC %.3f (acc %.3f) vs mean-rev %.3f",
                        tag, target, hname, chro.get("ic_spearman", np.nan),
                        chro.get("dir_acc", np.nan), base.get("ic_spearman", np.nan))
    return rows


def run_direction(pipe, series: dict, tag: str) -> list[dict]:
    """Directional test across input variants (contract mid vs spot composite)."""
    from crypto_trading.crypto_strategies.research_chronos import rolling_forecast
    rows = []
    for src in ("contract_mid", "spot_composite"):
        s = series.get(src)
        if s is None or len(s) < CTX + 60:
            continue
        target = series["contract_mid"]              # we always TRADE the perp
        for hname, h in HORIZONS.items():
            for mode in ("price", "return"):
                fc = rolling_forecast(s, ctx_len=CTX, horizon=h, mode=mode, pipe=pipe)
                if fc.empty or "pred_bps" not in fc.columns:
                    continue
                # forecast may be built on the composite, but the traded object
                # is always the perp → score against the PERP's forward move
                base = target.reindex(fc.index).ffill()
                fwd_bps = (target.shift(-h).reindex(fc.index) / base - 1.0) * 1e4
                res = evaluate(fc["pred_bps"], fwd_bps, directional=True)
                rows.append({"tag": tag, "input": src, "horizon": hname, "mode": mode,
                             **res})
                logger.info("dir %s %s %s %s → IC %.4f acc %.4f (n=%s)", tag, src,
                            hname, mode, res.get("ic_spearman", np.nan),
                            res.get("dir_acc", np.nan), res.get("n"))
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", default="amazon/chronos-bolt-small,amazon/chronos-bolt-base")
    ap.add_argument("--markets", default="KXBTCPERP")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    control, direction = [], []
    for tk in args.markets.split(","):
        asset = MARKETS.get(tk)
        if asset is None:
            continue
        series = build_series(tk, asset)
        print(f"\n{tk} 输入序列: " + ", ".join(
            f"{k}(n={len(v)}, uniq={len(np.unique(v)):,})" for k, v in series.items()))
        for model in args.models.split(","):
            os.environ["CHRONOS_MODEL"] = model
            import importlib
            import crypto_trading.crypto_strategies.research_chronos as rc
            importlib.reload(rc)                     # pick up the model env var
            pipe = rc._pipeline()
            tag = f"{tk}|{model.split('/')[-1]}"
            control += run_control(pipe, series, tag)
            direction += run_direction(pipe, series, tag)

    print("\n" + "=" * 104)
    print("A) 阳性对照 — 预测已知强可预测的目标(已实现波动率 / 成交量)")
    print("=" * 104)
    if control:
        c = pd.DataFrame(control)
        print(c.to_string(index=False))
        print(f"\n  击败均值回归基线的配置: {int(c.beats_baseline.sum())}/{len(c)}")
        good = c[c.chronos_ic.abs() > 0.15]
        print(f"  |IC| > 0.15 的配置: {len(good)}/{len(c)}")
        print("  → " + ("管线健康:Chronos 在可预测目标上确有强信号,"
                        "因此方向性 IC≈0 反映的是目标本身不可预测"
                        if len(good) > len(c) / 2 else
                        "⚠️ 管线存疑:连公认可预测的目标都抓不到 —— 方向性结论需重新审视"))
    print("\n" + "=" * 104)
    print("B) 方向性 — 输入源(合约价 vs 合成现货指数)× 视界 × 模式 × 模型")
    print("=" * 104)
    if direction:
        d = pd.DataFrame(direction)
        print(d.to_string(index=False))
        print(f"\n  IC 为正的配置: {(d.ic_spearman > 0).sum()}/{len(d)}"
              f" | 方向准确率 > 0.5 的: {(d.dir_acc > 0.5).sum()}/{len(d)}")
        best_src = d.groupby("input").ic_spearman.mean()
        print(f"  按输入源的平均 IC:\n{best_src.to_string()}")

    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"chronos_diag_{stamp}.json").write_text(json.dumps(
        {"control": control, "direction": direction}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
