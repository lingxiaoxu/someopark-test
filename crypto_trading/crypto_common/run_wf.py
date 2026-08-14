"""Unified walk-forward runner — the WF→validate BRIDGE (Plan 00 §7 wf/select).

`WalkForwardAnalyzer` computes folds; `validate.py` reads
`trading_signals/walk_forward/<strategy>_oos_equity.csv` + `<strategy>_detail.json`.
Nothing wrote those. This does: it runs the analyzer for a strategy and persists
exactly the artifacts validate consumes.

Registry — each equity strategy module exposes:
    WF_PARAM_SETS: dict[str, dict]
    wf_run_backtest(params, start, end) -> {"equity_curve": pd.Series}
    wf_prices() -> pd.DataFrame        (daily reference; defines the fold grid)
    wf_macro() -> pd.DataFrame         (optional regime features; else empty)
event_perp is SIGNAL-IC (no tradeable P&L yet) → its own run_signal_ic_wf writes
`event_perp_oos_ic.csv` instead.

FAIL-SAFE: if the analyzer can't form folds (too little data), NO artifacts are
written and validate correctly reports "insufficient data" — never a garbage PASS.

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_common.run_wf --strategy X
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging

import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.walk_forward import WalkForwardAnalyzer

logger = logging.getLogger(__name__)

WF_DIR = SIGNALS_DIR / "walk_forward"

_EQUITY_STRATEGIES = {
    "basis_meanrev": "crypto_trading.crypto_strategies.basis_meanrev.strategy",
    "liq_reversion": "crypto_trading.crypto_strategies.liq_reversion.strategy",
    "perp_rotation": "crypto_trading.crypto_strategies.perp_rotation.run_backtest",
}
_SIGNAL_IC_STRATEGIES = {"event_perp"}
ALL_STRATEGIES = sorted(set(_EQUITY_STRATEGIES) | _SIGNAL_IC_STRATEGIES)


def _run_equity(strategy: str, **wf_kwargs) -> dict:
    mod = importlib.import_module(_EQUITY_STRATEGIES[strategy])
    prices = mod.wf_prices()
    macro = mod.wf_macro() if hasattr(mod, "wf_macro") else pd.DataFrame()
    if prices is None or len(prices) == 0:
        return {"ok": False, "reason": "no reference prices (data not yet available)"}

    analyzer = WalkForwardAnalyzer(mod.wf_run_backtest, mod.WF_PARAM_SETS,
                                   prices, macro, **wf_kwargs)
    result = analyzer.run()
    if not result.folds or result.synthetic_equity.empty:
        return {"ok": False, "n_folds": len(result.folds),
                "reason": "insufficient data: analyzer formed no complete folds "
                          f"(have {len(prices)} daily rows; needs is_days_min + oos_days)"}

    WF_DIR.mkdir(parents=True, exist_ok=True)
    eq = result.synthetic_equity.rename("equity")
    eq.index.name = "date"
    eq.to_frame().to_csv(WF_DIR / f"{strategy}_oos_equity.csv")
    (WF_DIR / f"{strategy}_detail.json").write_text(
        json.dumps(result.to_detail_dict(), indent=1, default=float))
    return {"ok": True, "n_folds": len(result.folds),
            "oos_sharpe": result.synthetic_metrics.get("sharpe"),
            "dsr": result.dsr_aggregate,
            "artifacts": [f"{strategy}_oos_equity.csv", f"{strategy}_detail.json"]}


def _run_signal_ic(strategy: str) -> dict:
    from crypto_trading.crypto_strategies.event_perp.backtest import run_signal_ic_wf
    res = run_signal_ic_wf("KXBTC")
    if res["n_folds"] == 0:
        return {"ok": False, "reason": "insufficient data: not enough strip days for "
                f"IS/OOS folds (have {res['n_days']} days)"}
    WF_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(res["folds"])
    df.to_csv(WF_DIR / "event_perp_oos_ic.csv", index=False)
    valid = df["oos_ic"].dropna()
    return {"ok": True, "n_folds": res["n_folds"],
            "mean_oos_ic": float(valid.mean()) if len(valid) else None,
            "artifacts": ["event_perp_oos_ic.csv"]}


def run_wf(strategy: str, **wf_kwargs) -> dict:
    if strategy in _SIGNAL_IC_STRATEGIES:
        return _run_signal_ic(strategy)
    return _run_equity(strategy, **wf_kwargs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", required=True, choices=ALL_STRATEGIES)
    ap.add_argument("--is-days", type=int, default=None, help="min IS window (days)")
    ap.add_argument("--oos-days", type=int, default=None)
    ap.add_argument("--step-days", type=int, default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    kw = {}
    if args.is_days is not None:
        kw["is_days_min"] = args.is_days
    if args.oos_days is not None:
        kw["oos_days"] = args.oos_days
    if args.step_days is not None:
        kw["step_days"] = args.step_days

    res = run_wf(args.strategy, **kw)
    print("=" * 70)
    print(f"WALK-FORWARD RUN — {args.strategy}")
    print("=" * 70)
    if res["ok"]:
        print(f"  folds: {res['n_folds']}")
        for k in ("oos_sharpe", "dsr", "mean_oos_ic"):
            if res.get(k) is not None:
                print(f"  {k}: {res[k]:.3f}")
        print(f"  wrote: {', '.join(res['artifacts'])}")
        print("  → now run:  pipeline.sh validate --strategy " + args.strategy)
    else:
        print(f"  NO ARTIFACTS WRITTEN — {res['reason']}")
        print("  (validate will correctly report insufficient data — FAIL-safe)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
