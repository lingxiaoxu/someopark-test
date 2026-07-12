"""
weekly_review.py — Weekly parameter health & market review (Plan 05 §5)
=======================================================================
COPIED from qlib-main/sector_rotation/weekly_review.py (read-only template,
338 lines). Same six steps, same output contract (JSON + `latest` symlink):

  1. Multi-horizon scoring        4. Cache health (freshness/staleness)
  2. Parameter drift analysis     5. Stop-loss proximity on current holdings
  3. Regime trend analysis        6. Version preference trend

ADAPTATIONS (only these):
  * Step 1: the template invoked sector_rotation.multi_horizon_backtest; here
    the multi-horizon composite is computed from the cached WF equity curves
    (select_cache/batch_equity_cache.parquet) over trailing 30/60/90/180-day
    windows (Sharpe, 365-ann., recency-weighted 40/30/20/10) and PERSISTED to
    select_cache/multi_horizon_results.json — the same file smart_select's
    composite consumes. No cache → "insufficient data", never a crash.
  * Step 3: VIX/HY/curve → btc_rvol / funding / basis_dispersion / dominance
    (via crypto_common.smart_select._build_regime_frame).
  * Step 5: SPY 3d circuit → KXBTCPERP 3d (−15% crypto circuit threshold);
    trailing-stop proximity uses the crypto −25% peak threshold; holdings
    from trading_signals/inventory/inventory_perp_rotation.json.
  * Paths → trading_signals/perp_rotation/ + select_cache/; ann. 252 → 365.
  * Graceful short-history behavior throughout (34-day Kalshi panel today).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from crypto_trading.crypto_common import config as _config
from crypto_trading.crypto_common.smart_select import (_build_regime_frame,
                                                       _cache_dir)

log = logging.getLogger(__name__)

TRADING_DAYS = 365
MH_WINDOWS = [30, 60, 90, 180]
MH_WEIGHTS = [0.40, 0.30, 0.20, 0.10]        # recency-weighted (template P4 intent)


def _load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _load_selected_state(cache_dir: Path) -> dict:
    p = cache_dir / "selected_param_set.json"
    if p.exists():
        return json.loads(p.read_text())
    return {}


def _out_dir() -> Path:
    return _config.SIGNALS_DIR / "perp_rotation"


def run_weekly_review(top_n: int = 10, output_dir: Path = None) -> dict:
    """Generate the weekly review (template flow; crypto inputs)."""
    out_dir = output_dir or _out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = _cache_dir()

    t0 = time.time()
    print(f"\n{'═' * 60}")
    print("  WEEKLY REVIEW (perp_rotation)")
    print(f"{'═' * 60}\n")

    review = {"generated_at": datetime.now().isoformat(),
              "signal_date": str(date.today())}

    state = _load_selected_state(cache_dir)
    current_param = state.get("param_set", "")
    current_version = state.get("signal_version", "v1")

    # ── 1. Multi-horizon scoring (ADAPTED: from cached WF equity curves) ──
    print(f"  [1/6] Multi-horizon scoring (version={current_version})...")
    mh_report: dict = {"signal_version": current_version}
    eq_path = cache_dir / f"batch_equity_cache_{current_version}.parquet"
    if not eq_path.exists():
        eq_path = cache_dir / "batch_equity_cache.parquet"
    if eq_path.exists():
        try:
            eq_cache = pd.read_parquet(eq_path)
            composite: dict[str, float] = {}
            for name in eq_cache.columns:
                eq = eq_cache[name].dropna()
                rets = eq.pct_change().dropna()
                parts, wts = [], []
                for win, wt in zip(MH_WINDOWS, MH_WEIGHTS):
                    tail = rets.tail(win)
                    if len(tail) >= max(15, win // 3) and tail.std() > 0:
                        parts.append(float(tail.mean() / tail.std() * np.sqrt(TRADING_DAYS)))
                        wts.append(wt)
                if parts:
                    composite[name] = round(float(np.average(parts, weights=wts)), 4)
            ranking = sorted(composite, key=composite.get, reverse=True)
            mh_report["top_5"] = ranking[:5]
            mh_report["composite_scores"] = composite
            # persist for smart_select's component 3 (template chain)
            (cache_dir / "multi_horizon_results.json").write_text(json.dumps({
                "generated_at": datetime.now().isoformat(),
                "windows_days": MH_WINDOWS, "weights": MH_WEIGHTS,
                "composite_scores": composite}, indent=2))
            print(f"    scored {len(composite)} param sets; top: {ranking[:3]}")
        except Exception as e:
            mh_report["error"] = str(e)
    else:
        mh_report["error"] = "insufficient data: no batch_equity_cache (run WF first)"
        print(f"    {mh_report['error']}")
    review["multi_horizon"] = mh_report

    # ── 2. Parameter drift analysis (template verbatim; √365) ──
    print(f"\n  [2/6] Parameter drift analysis ({current_param}, {current_version})...")
    drift_report = {"current_param": current_param, "signal_version": current_version}
    if eq_path.exists() and current_param:
        try:
            eq_cache = pd.read_parquet(eq_path)
            if current_param in eq_cache.columns:
                eq = eq_cache[current_param].dropna()
                rets = eq.pct_change().dropna()
                for window in [30, 60, 90]:
                    tail = rets.tail(window)
                    if len(tail) >= 20:
                        sr = float(tail.mean() / tail.std() * np.sqrt(TRADING_DAYS))
                        drift_report[f"rolling_sharpe_{window}d"] = round(sr, 3)
                sr_30 = drift_report.get("rolling_sharpe_30d")
                sr_90 = drift_report.get("rolling_sharpe_90d")
                if sr_30 is not None and sr_90 is not None:
                    drift_report["trend"] = ("declining" if sr_30 < sr_90 - 0.3 else
                                             "improving" if sr_30 > sr_90 + 0.3 else "stable")
                print(f"    {current_param}: 30d SR={drift_report.get('rolling_sharpe_30d', '?')} "
                      f"90d={drift_report.get('rolling_sharpe_90d', '?')} "
                      f"trend={drift_report.get('trend', '?')}")
        except Exception as e:
            drift_report["error"] = str(e)
    review["param_drift"] = drift_report

    # ── shared data for steps 3 + 5 ──
    macro = pd.DataFrame()
    prices = pd.DataFrame()
    try:
        macro = _build_regime_frame()
    except Exception as e:
        log.warning(f"regime frame failed: {e}")
    try:
        from crypto_trading.crypto_strategies.perp_rotation.data.loader import \
            build_perp_panel
        prices, _, _ = build_perp_panel()
    except Exception as e:
        log.warning(f"perp panel failed: {e}")

    # ── 3. Regime trend analysis (crypto features) ──
    print("\n  [3/6] Regime trend analysis...")
    regime_report: dict = {}
    try:
        if not macro.empty and "btc_rvol" in macro.columns:
            rv = macro["btc_rvol"].dropna()
            rv_5d = rv.tail(5)
            rv_prev = rv.tail(10).head(5) if len(rv) >= 10 else pd.Series(dtype=float)
            regime_report["btc_rvol_current"] = round(float(rv.iloc[-1]), 2) if len(rv) else None
            regime_report["btc_rvol_5d_avg"] = round(float(rv_5d.mean()), 2) if len(rv_5d) else None
            regime_report["btc_rvol_prev_5d_avg"] = (round(float(rv_prev.mean()), 2)
                                                     if len(rv_prev) else None)
            if regime_report["btc_rvol_5d_avg"] and regime_report["btc_rvol_prev_5d_avg"]:
                delta = regime_report["btc_rvol_5d_avg"] - regime_report["btc_rvol_prev_5d_avg"]
                regime_report["rvol_weekly_change"] = round(delta, 2)
                regime_report["rvol_direction"] = ("rising" if delta > 2 else
                                                   "falling" if delta < -2 else "flat")
            if len(rv_5d) >= 3:
                regime_report["days_above_rvol45_this_week"] = int((rv_5d > 45).sum())
        for col in ["funding", "basis_dispersion", "btc_dominance"]:
            if col in macro.columns:
                vals = macro[col].dropna().tail(5)
                if len(vals):
                    regime_report[f"{col}_current"] = round(float(vals.iloc[-1]), 6)
        print(f"    btc_rvol: {regime_report.get('btc_rvol_current', '?')} "
              f"(dir={regime_report.get('rvol_direction', '?')})")
    except Exception as e:
        regime_report["error"] = str(e)
    review["regime_trend"] = regime_report

    # ── 4. Cache health (template pattern; select_cache files) ──
    print(f"\n  [4/6] Cache health ({current_version})...")
    import os
    cache_files = {
        "batch_equity_cache": eq_path,
        "param_oos_by_regime": cache_dir / "param_oos_by_regime.json",
        "param_oos_by_macro_cluster": cache_dir / "param_oos_by_macro_cluster.json",
        "macro_latent_centroids": cache_dir / "macro_latent_centroids.npy",
        "top_candidates": cache_dir / "top_candidates.json",
        "multi_horizon_results": cache_dir / "multi_horizon_results.json",
    }
    cache_health: dict = {}
    for name, p in cache_files.items():
        if p.exists():
            stat = os.stat(p)
            age_days = (time.time() - stat.st_mtime) / 86400
            cache_health[name] = {"exists": True, "age_days": round(age_days, 1),
                                  "stale": age_days > 7,
                                  "size_kb": round(stat.st_size / 1024, 1)}
        else:
            cache_health[name] = {"exists": False, "stale": True}
    n_stale = sum(1 for v in cache_health.values() if v.get("stale"))
    n_missing = sum(1 for v in cache_health.values() if not v.get("exists"))
    cache_health["summary"] = {"n_stale": n_stale, "n_missing": n_missing,
                               "healthy": n_stale == 0 and n_missing == 0}
    print(f"    {len(cache_files)} files: {n_missing} missing, {n_stale} stale (>7d)")
    review["cache_health"] = cache_health

    # ── 5. Stop-loss proximity (BTC circuit + per-perp trailing) ──
    print("\n  [5/6] Stop-loss proximity...")
    sl_report: dict = {}
    try:
        inv = _load_json(_config.SIGNALS_DIR / "inventory" / "inventory_perp_rotation.json")
        positions = {p["ticker"]: p for p in inv.get("positions", [])}
        if not prices.empty and "KXBTCPERP" in prices.columns:
            btc = prices["KXBTCPERP"].dropna()
            if len(btc) >= 4:
                btc_3d = float(btc.iloc[-1] / btc.iloc[-4] - 1)
                sl_report["btc_3d_return"] = round(btc_3d * 100, 2)
                sl_report["circuit_breaker_threshold"] = -15.0    # crypto stop_loss.py
                sl_report["circuit_breaker_risk"] = ("HIGH" if btc_3d < -0.10 else
                                                     "MEDIUM" if btc_3d < -0.06 else "LOW")
        perp_risk = {}
        for ticker in (positions or {}):
            if ticker in prices.columns:
                px = prices[ticker].dropna()
                if len(px) >= 20:
                    peak = float(px.tail(60).max())
                    current = float(px.iloc[-1])
                    dd = (current - peak) / peak
                    perp_risk[ticker] = {
                        "current": round(current, 4), "peak_60d": round(peak, 4),
                        "dd_from_peak_pct": round(dd * 100, 1),
                        "trailing_stop_threshold": -25.0,          # crypto stop_loss.py
                        "risk": ("HIGH" if dd < -0.18 else
                                 "MEDIUM" if dd < -0.10 else "LOW")}
        sl_report["perps"] = perp_risk
        print(f"    BTC 3d: {sl_report.get('btc_3d_return', '?')}%, "
              f"positions checked: {len(perp_risk)}")
    except Exception as e:
        sl_report["error"] = str(e)
    review["stop_loss_proximity"] = sl_report

    # ── 6. Version preference (template verbatim) ──
    print("\n  [6/6] Version preference...")
    version_report = {"current_version": current_version,
                      "switch_history": state.get("switch_history", []),
                      "n_switches_this_month": 0}
    this_month = str(date.today())[:7]
    for h in state.get("switch_history", []):
        if h.get("date", "")[:7] == this_month:
            version_report["n_switches_this_month"] += 1
    vs = state.get("version_selector", {})
    if vs:
        version_report["last_v1_confidence"] = vs.get("v1_confidence")
        version_report["last_recommendation"] = vs.get("recommended")
    print(f"    Current: {version_report['current_version']} "
          f"(switches this month={version_report['n_switches_this_month']})")
    review["version_preference"] = version_report

    # ── output (template contract: timestamped JSON + latest symlink) ──
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    review["signal_version"] = current_version
    review["param_set"] = current_param
    review_path = out_dir / f"weekly_review_{ts}.json"
    review_path.write_text(json.dumps(review, indent=2, default=str))
    latest_link = out_dir / "weekly_review_latest.json"
    latest_link.unlink(missing_ok=True)
    try:
        latest_link.symlink_to(review_path.name)
    except OSError:
        latest_link.write_text(review_path.read_text())

    print(f"\n{'═' * 60}")
    print(f"  WEEKLY REVIEW COMPLETE ({time.time() - t0:.0f}s)")
    print(f"  Output → {review_path}")
    print(f"{'═' * 60}\n")
    return review


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Weekly review (perp_rotation)")
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()
    out_dir = Path(args.output_dir) if args.output_dir else None
    run_weekly_review(top_n=args.top_n, output_dir=out_dir)
