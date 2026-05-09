"""
weekly_review.py — Weekly parameter health and market review (P7)
================================================================

Runs every Sunday as part of weekly pipeline. Generates a comprehensive
review of parameter health, regime trends, and smart-select diagnostics.

Steps:
  1. Multi-horizon backtest (P4): top 10 × 4 periods (uses current signal_version)
  2. Parameter drift analysis: rolling Sharpe from versioned equity cache
  3. Regime trend analysis: this week vs last week
  4. P0 cache health: verify MCPS data freshness (centroids, cluster OOS, fold detail)
  5. Stop-loss proximity: check current holdings for trailing/sector/circuit breaker risk
  6. Version preference trend

Output: backtest_results/weekly_review.json

Usage:
  conda run -n qlib_run --no-capture-output python \\
      qlib-main/sector_rotation/weekly_review.py [--top-n 10]
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).parent.resolve()
_QLIB_DIR = _THIS_DIR.parent.resolve()
_PROJECT_DIR = _QLIB_DIR.parent.resolve()

sys.path.insert(0, str(_QLIB_DIR))
sys.path.insert(0, str(_PROJECT_DIR))

from sector_rotation.data.loader import load_config, load_all

log = logging.getLogger(__name__)


def _load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _load_selected_state(cache_dir: Path) -> dict:
    for p in (_THIS_DIR / "selected_param_set.json", cache_dir / "selected_param_set.json"):
        if p.exists():
            return json.loads(p.read_text())
    return {}


def run_weekly_review(
    top_n: int = 10,
    output_dir: Path = None,
) -> dict:
    """
    Generate weekly review report.

    Returns review dict, also written to weekly_review.json.
    """
    out_dir = output_dir or (_THIS_DIR / "backtest_results")
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    print(f"\n{'═'*60}")
    print(f"  WEEKLY REVIEW (P7)")
    print(f"{'═'*60}\n")

    review = {
        "generated_at": datetime.now().isoformat(),
        "signal_date": str(date.today()),
    }

    # ── Load state ─────────────────────────────────────────────────
    state = _load_selected_state(out_dir)
    current_param = state.get("param_set", "")
    current_version = state.get("signal_version", "v1")

    # ── 1. Multi-horizon backtest (P4) ─────────────────────────────
    print(f"  [1/6] Multi-horizon backtest (version={current_version})...")
    try:
        from sector_rotation.multi_horizon_backtest import run_multi_horizon
        mh_results = run_multi_horizon(
            top_n=top_n, signal_version=current_version, output_dir=out_dir,
        )
        review["multi_horizon"] = {
            "signal_version": current_version,
            "top_5": mh_results.get("ranking", [])[:5],
            "composite_scores": mh_results.get("composite_scores", {}),
        }
    except Exception as e:
        log.warning(f"Multi-horizon backtest failed: {e}")
        review["multi_horizon"] = {"error": str(e)}

    # ── 2. Parameter drift analysis ────────────────────────────────
    print(f"\n  [2/6] Parameter drift analysis ({current_param}, {current_version})...")

    # Prefer versioned equity cache, fall back to unversioned
    eq_path = out_dir / f"batch_equity_cache_{current_version}.parquet"
    if not eq_path.exists():
        eq_path = out_dir / "batch_equity_cache.parquet"

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
                        sr = float(tail.mean() / tail.std() * np.sqrt(252))
                        drift_report[f"rolling_sharpe_{window}d"] = round(sr, 3)

                # Trend: is Sharpe declining?
                sr_30 = drift_report.get("rolling_sharpe_30d")
                sr_90 = drift_report.get("rolling_sharpe_90d")
                if sr_30 is not None and sr_90 is not None:
                    drift_report["trend"] = "declining" if sr_30 < sr_90 - 0.3 else \
                                            "improving" if sr_30 > sr_90 + 0.3 else "stable"
                print(f"    {current_param}: 30d SR={drift_report.get('rolling_sharpe_30d', '?')} "
                      f"60d={drift_report.get('rolling_sharpe_60d', '?')} "
                      f"90d={drift_report.get('rolling_sharpe_90d', '?')} "
                      f"trend={drift_report.get('trend', '?')}")
        except Exception as e:
            drift_report["error"] = str(e)

    review["param_drift"] = drift_report

    # ── Load prices & macro (shared by steps 3 and 5) ───────────
    prices, macro = pd.DataFrame(), pd.DataFrame()
    try:
        base_cfg = load_config()
        prices, macro = load_all(config=base_cfg)
    except Exception as e:
        log.warning(f"Failed to load prices/macro: {e}")

    # ── 3. Regime trend analysis ───────────────────────────────────
    print("\n  [3/6] Regime trend analysis...")
    regime_report = {}
    try:

        if not macro.empty and "vix" in macro.columns:
            vix = macro["vix"].dropna()

            # This week vs last week
            vix_5d = vix.tail(5)
            vix_prev_5d = vix.tail(10).head(5) if len(vix) >= 10 else pd.Series()

            regime_report["vix_current"] = round(float(vix.iloc[-1]), 2) if len(vix) > 0 else None
            regime_report["vix_5d_avg"] = round(float(vix_5d.mean()), 2) if len(vix_5d) > 0 else None
            regime_report["vix_prev_5d_avg"] = round(float(vix_prev_5d.mean()), 2) if len(vix_prev_5d) > 0 else None

            # VIX direction
            if regime_report["vix_5d_avg"] and regime_report["vix_prev_5d_avg"]:
                delta = regime_report["vix_5d_avg"] - regime_report["vix_prev_5d_avg"]
                regime_report["vix_weekly_change"] = round(delta, 2)
                regime_report["vix_direction"] = "rising" if delta > 1 else "falling" if delta < -1 else "flat"

            # Regime transitions this week
            if len(vix_5d) >= 3:
                above_25 = (vix_5d > 25).sum()
                regime_report["days_above_vix25_this_week"] = int(above_25)

            # Spread trends
            for col in ["hy_spread", "yield_curve", "fin_stress"]:
                if col in macro.columns:
                    vals = macro[col].dropna().tail(5)
                    if len(vals) > 0:
                        regime_report[f"{col}_current"] = round(float(vals.iloc[-1]), 4)

            print(f"    VIX: {regime_report.get('vix_current', '?')} "
                  f"(5d avg={regime_report.get('vix_5d_avg', '?')}, "
                  f"prev 5d={regime_report.get('vix_prev_5d_avg', '?')}, "
                  f"dir={regime_report.get('vix_direction', '?')})")

    except Exception as e:
        regime_report["error"] = str(e)

    review["regime_trend"] = regime_report

    # ── 4. P0 cache health check ─────────────────────────────────
    print(f"\n  [4/6] P0 cache health ({current_version})...")
    p0_health = {}
    p0_files = {
        "batch_equity_cache": out_dir / f"batch_equity_cache_{current_version}.parquet",
        "wf_fold_detail": out_dir / f"wf_fold_detail_{current_version}.json",
        "param_oos_by_regime": out_dir / f"param_oos_by_regime_{current_version}.json",
        "macro_latent_centroids": out_dir / "macro_latent_centroids.npy",
        "top_candidates": out_dir / "top_candidates.json",
    }
    for name, p in p0_files.items():
        if p.exists():
            import os
            stat = os.stat(p)
            age_days = (time.time() - stat.st_mtime) / 86400
            p0_health[name] = {
                "exists": True,
                "age_days": round(age_days, 1),
                "stale": age_days > 7,
                "size_kb": round(stat.st_size / 1024, 1),
            }
        else:
            # Try unversioned fallback
            fallback = out_dir / p.name.replace(f"_{current_version}", "")
            if fallback.exists() and fallback != p:
                stat = os.stat(fallback)
                age_days = (time.time() - stat.st_mtime) / 86400
                p0_health[name] = {"exists": True, "age_days": round(age_days, 1),
                                   "stale": age_days > 7, "note": "unversioned fallback"}
            else:
                p0_health[name] = {"exists": False, "stale": True}

    n_stale = sum(1 for v in p0_health.values() if v.get("stale"))
    n_missing = sum(1 for v in p0_health.values() if not v.get("exists"))
    p0_health["summary"] = {
        "n_stale": n_stale, "n_missing": n_missing,
        "healthy": n_stale == 0 and n_missing == 0,
    }
    print(f"    {len(p0_files)} files checked: {n_missing} missing, {n_stale} stale (>7d)")
    review["p0_cache_health"] = p0_health

    # ── 5. Stop-loss proximity check ──────────────────────────────
    print("\n  [5/6] Stop-loss proximity...")
    sl_report = {}
    try:
        inv_path = _THIS_DIR / "inventory_sector_rotation.json"
        inv = _load_json(inv_path) if inv_path.exists() else {}
        holdings = inv.get("holdings", {})

        if holdings and not macro.empty:
            # SPY circuit breaker check: 3-day SPY return
            if "SPY" in prices.columns:
                spy = prices["SPY"].dropna()
                if len(spy) >= 4:
                    spy_3d_ret = float((spy.iloc[-1] / spy.iloc[-4]) - 1)
                    sl_report["spy_3d_return"] = round(spy_3d_ret * 100, 2)
                    sl_report["spy_circuit_breaker_threshold"] = -7.0
                    sl_report["spy_circuit_breaker_risk"] = "HIGH" if spy_3d_ret < -0.05 else \
                                                             "MEDIUM" if spy_3d_ret < -0.03 else "LOW"

            # Per-sector trailing stop proximity
            sector_risk = {}
            for ticker, h in holdings.items():
                if h.get("weight", 0) < 0.01:
                    continue
                if ticker in prices.columns:
                    px = prices[ticker].dropna()
                    if len(px) >= 20:
                        peak = float(px.tail(60).max())
                        current = float(px.iloc[-1])
                        drawdown_from_peak = (current - peak) / peak
                        sector_risk[ticker] = {
                            "current": round(current, 2),
                            "peak_60d": round(peak, 2),
                            "dd_from_peak_pct": round(drawdown_from_peak * 100, 1),
                            "trailing_stop_threshold": -15.0,
                            "risk": "HIGH" if drawdown_from_peak < -0.10 else \
                                    "MEDIUM" if drawdown_from_peak < -0.05 else "LOW",
                        }
            sl_report["sectors"] = sector_risk
            print(f"    SPY 3d: {sl_report.get('spy_3d_return', '?')}%, "
                  f"sectors checked: {len(sector_risk)}")
    except Exception as e:
        sl_report["error"] = str(e)
    review["stop_loss_proximity"] = sl_report

    # ── 6. Version preference trend ────────────────────────────────
    print("\n  [6/6] Version preference...")
    version_report = {
        "current_version": state.get("signal_version", "v1"),
        "switch_history": state.get("switch_history", []),
        "n_switches_this_month": 0,
    }

    # Count switches this month
    this_month = str(date.today())[:7]
    for h in state.get("switch_history", []):
        if h.get("date", "")[:7] == this_month:
            version_report["n_switches_this_month"] += 1

    # V1 confidence from last smart_select run
    vs = state.get("version_selector", {})
    if vs:
        version_report["last_v1_confidence"] = vs.get("v1_confidence")
        version_report["last_recommendation"] = vs.get("recommended")

    print(f"    Current: {version_report['current_version']} "
          f"(v1_conf={version_report.get('last_v1_confidence', '?')}, "
          f"switches this month={version_report['n_switches_this_month']})")

    review["version_preference"] = version_report

    # ── Summary & output ───────────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    review["signal_version"] = current_version
    review["param_set"] = current_param

    review_path = out_dir / f"weekly_review_{ts}.json"
    review_path.write_text(json.dumps(review, indent=2, default=str))

    # Symlink latest for stable reads (tools, frontend)
    latest_link = out_dir / "weekly_review_latest.json"
    latest_link.unlink(missing_ok=True)
    latest_link.symlink_to(review_path.name)

    elapsed = time.time() - t0
    print(f"\n{'═'*60}")
    print(f"  WEEKLY REVIEW COMPLETE ({elapsed:.0f}s)")
    print(f"  Output → {review_path}")
    print(f"  Latest → {latest_link}")
    print(f"{'═'*60}\n")

    return review


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Weekly review (P7)")
    parser.add_argument("--top-n", type=int, default=10, help="Top N candidates for multi-horizon")
    parser.add_argument("--output-dir", default=None, help="Output directory")
    args = parser.parse_args()

    out_dir = Path(args.output_dir) if args.output_dir else None
    run_weekly_review(top_n=args.top_n, output_dir=out_dir)
