"""
multi_horizon_backtest.py — Multi-period backtest for parameter health assessment (P4)
======================================================================================

Runs top N candidates across multiple lookback horizons (6m, 1y, 3y, full period).
Provides multi-period perspective to avoid single-window bias.

Designed to run weekly (not daily — too slow). Results cached in
backtest_results/multi_horizon_results.json for daily smart_select to read.

Usage:
  conda run -n qlib_run --no-capture-output python \\
      qlib-main/sector_rotation/multi_horizon_backtest.py [--top-n 10]
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).parent.resolve()
_QLIB_DIR = _THIS_DIR.parent.resolve()
_PROJECT_DIR = _QLIB_DIR.parent.resolve()

sys.path.insert(0, str(_QLIB_DIR))
sys.path.insert(0, str(_PROJECT_DIR))

from sector_rotation.data.loader import load_config, load_all
from sector_rotation.backtest.engine import SectorRotationBacktest
from sector_rotation.SectorRotationStrategyRuns import PARAM_SETS, apply_param_set

log = logging.getLogger(__name__)

HORIZONS = [
    {"name": "recent_6m",   "lookback_months": 6,   "weight": 0.15},
    {"name": "recent_1y",   "lookback_months": 12,  "weight": 0.25},
    {"name": "recent_3y",   "lookback_months": 36,  "weight": 0.35},
    {"name": "full_period",  "lookback_months": None, "weight": 0.25},
]


def _load_top_candidates(cache_dir: Path) -> List[str]:
    """Load top candidate names from last batch run."""
    p = cache_dir / "top_candidates.json"
    if p.exists():
        data = json.loads(p.read_text())
        return [c.get("name", c.get("param_set", "")) for c in data.get("top", [])]
    return []


def run_multi_horizon(
    top_n: int = 10,
    signal_version: str = None,
    output_dir: Path = None,
) -> dict:
    """
    Run top N candidates across 4 lookback horizons.

    Returns dict of {param_name: {horizon_name: {sharpe, mcps, calmar, max_dd}}}.
    Also writes multi_horizon_results.json to output_dir.
    """
    out_dir = output_dir or (_THIS_DIR / "backtest_results")
    out_dir.mkdir(parents=True, exist_ok=True)

    base_cfg = load_config()
    prices, macro = load_all(config=base_cfg)

    # Get top candidates from last batch
    candidates = _load_top_candidates(out_dir)
    if not candidates:
        # Fallback: use a known good set
        candidates = list(PARAM_SETS.keys())[:top_n]
    candidates = candidates[:top_n]

    # Load MCPS + MacroStateStore
    mcps_available = False
    today_vec = None
    try:
        from MCPS import macro_cond_sharpe
        from MacroStateStore import MacroStateStore, SIMILARITY_FEATURES
        store = MacroStateStore()
        today_vec = store.get(str(date.today()))
        if today_vec and any(v is not None for v in today_vec.values()):
            mcps_available = True
    except Exception as e:
        log.warning(f"MCPS/MacroStateStore unavailable: {e}")

    signal_date = pd.Timestamp(prices.index[-1])

    results: Dict[str, Dict[str, dict]] = {}
    t0 = time.time()

    print(f"\n{'═'*60}")
    print(f"  MULTI-HORIZON BACKTEST (P4)")
    print(f"  Candidates : {len(candidates)}")
    print(f"  Horizons   : {', '.join(h['name'] for h in HORIZONS)}")
    if signal_version:
        print(f"  Signal ver : {signal_version}")
    print(f"{'═'*60}\n")

    for h in HORIZONS:
        if h["lookback_months"]:
            start = signal_date - pd.DateOffset(months=h["lookback_months"])
        else:
            start = pd.Timestamp(base_cfg.get("backtest", {}).get("start_date", "2018-07-01"))

        prices_h = prices[prices.index >= start]
        macro_h = macro[macro.index >= start] if not macro.empty else macro

        if len(prices_h) < 60:
            print(f"  [{h['name']}] Skipped (only {len(prices_h)} days)")
            continue

        print(f"  [{h['name']}] {len(prices_h)} days, {len(candidates)} params...", end="", flush=True)
        h_t0 = time.time()

        for name in candidates:
            if name not in PARAM_SETS:
                continue
            try:
                cfg = apply_param_set(base_cfg, PARAM_SETS[name])
                if signal_version:
                    cfg.setdefault("signals", {})["signal_version"] = signal_version
                # Override start date for this horizon
                cfg.setdefault("backtest", {})["start_date"] = start.strftime("%Y-%m-%d")

                bt = SectorRotationBacktest(cfg)
                result = bt.run(prices=prices_h, macro=macro_h)

                entry = {
                    "sharpe": round(result.metrics.get("sharpe", float("nan")), 4),
                    "calmar": round(result.metrics.get("calmar", float("nan")), 4),
                    "max_dd": round(result.metrics.get("max_drawdown", float("nan")), 4),
                    "ann_ret": round(result.metrics.get("annual_return", float("nan")), 4),
                }

                # MCPS score for this horizon
                if mcps_available and result.equity_curve is not None:
                    try:
                        mcps_score = macro_cond_sharpe(
                            equity=result.equity_curve,
                            macro_df=macro_h,
                            today_vec=today_vec,
                            features=list(SIMILARITY_FEATURES),
                        )
                        entry["mcps"] = round(float(mcps_score), 4) if not np.isnan(mcps_score) else None
                    except Exception:
                        entry["mcps"] = None
                else:
                    entry["mcps"] = None

                results.setdefault(name, {})[h["name"]] = entry

            except Exception as exc:
                log.debug(f"  [{name}@{h['name']}] failed: {exc}")

        print(f" done ({time.time() - h_t0:.1f}s)")

    # Compute weighted composite score
    composite = {}
    for name, horizons in results.items():
        score = 0.0
        total_weight = 0.0
        for h in HORIZONS:
            h_data = horizons.get(h["name"], {})
            mcps = h_data.get("mcps")
            if mcps is not None:
                score += h["weight"] * mcps
                total_weight += h["weight"]
            else:
                # Fallback to Sharpe
                sr = h_data.get("sharpe", 0)
                if sr and not np.isnan(sr):
                    score += h["weight"] * sr
                    total_weight += h["weight"]
        composite[name] = round(score / total_weight, 4) if total_weight > 0 else None

    output = {
        "generated_at": datetime.now().isoformat(),
        "signal_date": str(signal_date.date()),
        "signal_version": signal_version or "v1",
        "n_candidates": len(candidates),
        "horizons": {h["name"]: h for h in HORIZONS},
        "results": results,
        "composite_scores": composite,
        "ranking": sorted(
            [(n, s) for n, s in composite.items() if s is not None],
            key=lambda x: x[1], reverse=True,
        ),
    }

    out_path = out_dir / "multi_horizon_results.json"
    out_path.write_text(json.dumps(output, indent=2, default=str))

    elapsed = time.time() - t0
    print(f"\n  Multi-horizon complete ({elapsed:.0f}s)")
    print(f"  Results → {out_path}")

    # Summary
    if output["ranking"]:
        print(f"\n  {'Param':<30} {'6m':>8} {'1y':>8} {'3y':>8} {'full':>8} {'composite':>10}")
        print(f"  {'─'*72}")
        for name, comp in output["ranking"][:5]:
            r = results.get(name, {})
            vals = []
            for h in HORIZONS:
                v = r.get(h["name"], {}).get("mcps") or r.get(h["name"], {}).get("sharpe", "")
                vals.append(f"{v:>8.3f}" if isinstance(v, (int, float)) else f"{'N/A':>8}")
            print(f"  {name:<30} {vals[0]} {vals[1]} {vals[2]} {vals[3]} {comp:>10.3f}")

    return output


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Multi-horizon backtest (P4)")
    parser.add_argument("--top-n", type=int, default=10, help="Number of top candidates (default: 10)")
    parser.add_argument("--signal-version", default=None, choices=["v1", "v2"],
                        help="Override signal version")
    parser.add_argument("--output-dir", default=None, help="Output directory")
    args = parser.parse_args()

    out_dir = Path(args.output_dir) if args.output_dir else None
    run_multi_horizon(top_n=args.top_n, signal_version=args.signal_version, output_dir=out_dir)
