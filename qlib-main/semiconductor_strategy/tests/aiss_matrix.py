"""
aiss_matrix.py — full 33×V1/V2 backtest + validate matrix (in-process).
=======================================================================
Runs EVERY param set under BOTH signal versions, twice over:

  PART 1  single backtest (IS)  — 33 × {v1,v2} = 66 runs; asserts no error,
          records rebalance cadence (V1 monthly ~96, V2 semi-monthly ~185,
          ratio ~1.9–2.0) and the engine metrics.
  PART 2  win-criterion validate — 33 × {v1,v2} = 66 runs; asserts no error,
          tallies how many beat SOXX & SMH (PASS).

Fast (data is loaded once and cached). Exit 0 iff zero errors.

Usage:
    conda run -n qlib_run python -m semiconductor_strategy.tests.aiss_matrix
"""
from __future__ import annotations

import sys, os, copy, math, traceback, warnings
warnings.filterwarnings("ignore")
import logging
logging.getLogger().setLevel(logging.ERROR)

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..")))  # qlib-main/

from semiconductor_strategy.data.loader import load_config, load_all
from semiconductor_strategy.backtest.engine import AISSBacktest
from semiconductor_strategy.AISSStrategyRuns import PARAM_SETS, apply_param_set
from semiconductor_strategy.validate import validate as run_validate


def main():
    base = load_config()
    prices, macro = load_all(config=base)
    names = list(PARAM_SETS.keys())
    print(f"PARAM_SETS={len(names)} | prices{prices.shape} macro{macro.shape}")

    # ---- PART 1: single backtest (IS) ----
    print("\n== PART 1: single backtest (IS) — 33 × V1/V2 ==")
    bt_err = 0; bt_ok = 0; v1r = []; v2r = []; rows = []
    for ps in names:
        for ver in ("v1", "v2"):
            try:
                cfg = apply_param_set(copy.deepcopy(base), PARAM_SETS.get(ps, {}))
                cfg.setdefault("signals", {})["signal_version"] = ver
                r = AISSBacktest(cfg).run(prices, macro)
                m = r.metrics
                wh = getattr(r, "weights_history", None)
                nreb = len(wh) if wh is not None else 0
                ann = m.get("annual_return", float("nan"))
                shp = m.get("sharpe", m.get("sharpe_ratio"))
                mdd = m.get("max_drawdown", float("nan"))
                (v1r if ver == "v1" else v2r).append(nreb)
                rows.append((ps, ver, ann, shp, mdd, nreb)); bt_ok += 1
            except Exception:
                bt_err += 1
                print(f"  !! BT {ps}/{ver}: {traceback.format_exc().splitlines()[-1][:110]}")
    print(f"backtests ok={bt_ok} err={bt_err} (expect {2*len(names)})")
    if v1r and v2r:
        ratio = (sum(v2r)/len(v2r)) / max(1e-9, sum(v1r)/len(v1r))
        print(f"  V1 reb min={min(v1r)} max={max(v1r)} | V2 reb min={min(v2r)} max={max(v2r)} | V2/V1={ratio:.2f}")

    # ---- PART 2: validate (win-criterion) ----
    print("\n== PART 2: validate (win-criterion vs SOXX/SMH) — 33 × V1/V2 ==")
    v_err = 0; v_ok = 0; npass = 0
    for ps in names:
        for ver in ("v1", "v2"):
            try:
                rep = run_validate(param_set=ps, config=copy.deepcopy(base), signal_version=ver)
                v_ok += 1
                if rep.get("PASS"): npass += 1
            except Exception:
                v_err += 1
                print(f"  !! VAL {ps}/{ver}: {traceback.format_exc().splitlines()[-1][:110]}")
    print(f"validate ok={v_ok} err={v_err} (expect {2*len(names)}) | win-criterion PASS={npass}/{v_ok}")

    # ---- samples ----
    print("\n== samples ==")
    for ps, ver, ann, shp, mdd, nreb in rows:
        if ps in ("default", "opt_equal_weight", "derisk_tight", "balanced_four"):
            s = f"{shp:.2f}" if isinstance(shp, (int, float)) and shp == shp else str(shp)
            print(f"  {ps+'/'+ver:24} ann={ann:6.1%} sharpe={s:>6} mdd={mdd:6.1%} reb={nreb}")

    ok = (bt_err == 0 and v_err == 0)
    print(f"\nRESULT: {'ALL PASS' if ok else 'ERRORS'} (bt_err={bt_err} val_err={v_err})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
