"""
multi_horizon_backtest.py — Multi-period backtest for parameter health assessment (P4)
======================================================================================

Runs top N candidates across multiple lookback horizons (6m, 1y, 3y, full period).
Provides multi-period perspective to avoid single-window bias.

Designed to run weekly (not daily — too slow). Results cached in
backtest_results/multi_horizon_results.json for daily smart_select to read.

Usage:
  conda run -n qlib_run --no-capture-output python \\
      qlib-main/electric_utilities_strategy/multi_horizon_backtest.py [--top-n 10]
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

from electric_utilities_strategy.data.loader import load_config, load_all
from electric_utilities_strategy.backtest.engine import AEUSBacktest
from electric_utilities_strategy.AEUSStrategyRuns import PARAM_SETS, apply_param_set

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
        from SimilarityEngine import AUTOENCODER_FEATURES
        store = MacroStateStore()
        today_vec = store.get(str(date.today()), features=list(AUTOENCODER_FEATURES))
        # 2026-07-31: MCPS 打分用 MacroStateStore 完整帧(23维全有)——策略自身
        # loader 的 macro 帧列不全(SSRS 仅7列)会把 AE 打回守卫降级。与 BatchRun
        # 的 _store.load 做法对齐;策略引擎自身回测仍用原 macro 帧,互不影响。
        mcps_macro = store.load("2017-01-01")
        if today_vec and any(v is not None for v in today_vec.values()):
            mcps_available = True
    except Exception as e:
        log.warning(f"MCPS/MacroStateStore unavailable: {e}")

    signal_date = pd.Timestamp(prices.index[-1])

    results: Dict[str, Dict[str, dict]] = {}
    # 窗口过短而未跑的视野 —— 显式留痕,不让它消失在合成分的重归一化里
    skipped: Dict[str, dict] = {}
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

        # 窗口只用来判定"这个视野够不够交易"与打印 —— **不切喂给引擎的历史**。
        # 2026-09-07 修正:原先把 prices/macro 也切到 start 再交给 bt.run,等于对信号
        # 做了第二次截断。合成信号需要 ~30 个月末预热(12-1 动量 + 36 月滚动 z,
        # min_periods 18),6 个月/1 年的窗口切完只剩 7/13 个月末 → cs_mom 全 NaN →
        # composite 0 个有效月 → 引擎的 avail_scores 恒空,一笔不交易(实测:自
        # 2026-05-10 第一份 weekly 起,recent_6m/recent_1y 从未交易过,净值恒为
        # $1,000,000、Sharpe NaN,却被当作数据参与合成评分)。
        # 这一刀恰好抵消了 config.yaml 写明的预热余量:
        #     price_start: "2017-01-01"  # Earlier than backtest start for lookback warmup
        # 引擎本来就在传入的全帧上算信号(engine.py "Computing composite signals for
        # full history"),并用 cfg backtest.start_date(下方第 138 行,早已设好)把净值
        # 曲线限制在窗口内 —— 窗口化是引擎的职责,这里再切一次是冗余且有害的。
        window_days = int((prices.index >= start).sum())
        if window_days < 60:
            # 视野本身太短 → 显式记状态,不能静默 continue:下游合成分会按
            # total_weight 重归一化,消失的视野会变成"没有这个视野"而非"这个视野无效"。
            print(f"  [{h['name']}] Skipped (window only {window_days} days)")
            skipped[h["name"]] = {"status": "insufficient_window",
                                  "window_days": window_days, "min_days": 60}
            continue

        print(f"  [{h['name']}] {window_days} days, {len(candidates)} params...", end="", flush=True)
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

                bt = AEUSBacktest(cfg)
                # 全历史喂进去(信号预热),交易窗口由上面的 start_date 界定。
                result = bt.run(prices=prices, macro=macro)

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
                            macro_df=mcps_macro[mcps_macro.index >= start],
                            today_vec=today_vec,
                            features=list(AUTOENCODER_FEATURES),
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
        "skipped_horizons": skipped,   # 窗口过短未跑的视野(空 dict = 四个视野全跑了)
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
