"""
validate.py — AEUS win-criterion validation
============================================
THE go/no-go gate for the AI Infra & Electric Utilities Strategy.

The strategy exists only to be better than simply holding XLU or GRID, so this
module backtests AEUS (a chosen param set, default = production default) over the
same window as XLU / GRID / SPY buy-and-hold and reports CAGR / Vol / Sharpe /
Calmar / MaxDD for all four, plus the explicit verdict:

    PASS  iff  AEUS beats BOTH XLU and GRID on Sharpe AND CAGR.

Usage:
    python -m electric_utilities_strategy.validate                 # default param set
    python -m electric_utilities_strategy.validate --param-set momentum_heavy
    python -m electric_utilities_strategy.validate --json out.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_QLIB_DIR = Path(__file__).resolve().parents[1]
if str(_QLIB_DIR) not in sys.path:
    sys.path.insert(0, str(_QLIB_DIR))

from electric_utilities_strategy.data.loader import load_config, load_all
from electric_utilities_strategy.backtest.engine import AEUSBacktest
from electric_utilities_strategy.AEUSStrategyRuns import PARAM_SETS, apply_param_set

log = logging.getLogger("aeus.validate")

PERIODS = 252


def _metrics(returns: pd.Series) -> dict:
    r = returns.dropna()
    if len(r) < 30:
        return {"cagr": float("nan"), "vol": float("nan"), "sharpe": float("nan"),
                "calmar": float("nan"), "max_dd": float("nan"), "total": float("nan")}
    total = (1 + r).prod()
    cagr = total ** (PERIODS / len(r)) - 1
    vol = r.std() * np.sqrt(PERIODS)
    sharpe = (r.mean() / r.std() * np.sqrt(PERIODS)) if r.std() > 0 else float("nan")
    eq = (1 + r).cumprod()
    max_dd = (eq / eq.cummax() - 1).min()
    calmar = (cagr / abs(max_dd)) if max_dd < 0 else float("nan")
    return {"cagr": cagr, "vol": vol, "sharpe": sharpe, "calmar": calmar,
            "max_dd": max_dd, "total": total - 1}


def validate(param_set: str = "default", config: dict | None = None,
             signal_version: str | None = None) -> dict:
    cfg = config or load_config()
    prices, macro = load_all(config=cfg)
    run_cfg = apply_param_set(cfg, PARAM_SETS.get(param_set, {}))
    if signal_version:
        run_cfg.setdefault("signals", {})["signal_version"] = signal_version
    result = AEUSBacktest(run_cfg).run(prices, macro)

    sret = result.daily_returns.dropna()
    start, end = sret.index[0], sret.index[-1]
    out = {
        "param_set": param_set,
        "period": [str(start.date()), str(end.date())],
        "AEUS": _metrics(sret),
    }
    for b in ("XLU", "GRID", "SPY"):
        if b in prices.columns:
            out[b] = _metrics(prices[b].pct_change().loc[start:end])

    a = out["AEUS"]
    hurdles = [b for b in ("XLU", "GRID") if b in out]
    beats = {
        b: {"sharpe": bool(a["sharpe"] > out[b]["sharpe"]),
            "cagr": bool(a["cagr"] > out[b]["cagr"]),
            "max_dd": bool(a["max_dd"] > out[b]["max_dd"])}
        for b in hurdles
    }
    out["beats"] = beats
    out["PASS"] = all(beats[b]["sharpe"] and beats[b]["cagr"] for b in hurdles)
    return out


def _print(report: dict) -> None:
    print("=" * 78)
    print(f"AEUS WIN-CRITERION VALIDATION — param_set={report['param_set']}  "
          f"({report['period'][0]} → {report['period'][1]})")
    print("=" * 78)
    print(f"{'':14}{'CAGR':>9}{'Vol':>9}{'Sharpe':>9}{'Calmar':>9}{'MaxDD':>9}")
    for name in ("AEUS", "XLU", "GRID", "SPY"):
        if name not in report:
            continue
        m = report[name]
        print(f"{name:14}{m['cagr']:9.1%}{m['vol']:9.1%}{m['sharpe']:9.2f}"
              f"{m['calmar']:9.2f}{m['max_dd']:9.1%}")
    print("-" * 78)
    for b, v in report["beats"].items():
        print(f"  vs {b}: Sharpe {'PASS' if v['sharpe'] else 'fail'} | "
              f"CAGR {'PASS' if v['cagr'] else 'fail'} | "
              f"MaxDD {'better' if v['max_dd'] else 'worse'}")
    print("=" * 78)
    print(f"WIN CRITERION (beat XLU & GRID on Sharpe AND CAGR): "
          f"{'PASS ✅' if report['PASS'] else 'FAIL ❌'}")
    print("=" * 78)


def main() -> None:
    logging.basicConfig(level=logging.ERROR)
    ap = argparse.ArgumentParser(description="AEUS win-criterion validation")
    ap.add_argument("--param-set", default="default")
    ap.add_argument("--signal-version", default=None, choices=["v1", "v2"],
                    help="v1=monthly rebalance, v2=semi-monthly (1st + mid-month)")
    ap.add_argument("--json", default=None, help="Write full report JSON here")
    args = ap.parse_args()
    report = validate(param_set=args.param_set, signal_version=args.signal_version)
    _print(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=float))
        print(f"\nWrote {args.json}")
    sys.exit(0 if report["PASS"] else 1)


if __name__ == "__main__":
    main()
