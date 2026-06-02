"""
ssrs_matrix.py — full 59-param × V1/V2 backtest matrix (in-process).
====================================================================
Mirror of semiconductor_strategy/tests/aiss_matrix.py.  SSRS has no win-criterion
`validate` module (unlike AISS), so this runs the single-backtest matrix only:

  PART 1  single backtest (IS) — every param set × {v1,v2}; asserts no error,
          records rebalance cadence (V1 monthly ~94, V2 semi-monthly ~176,
          ratio ~1.8-2.0) and the engine metrics.

Fast (data loaded once and cached).  Exit 0 iff zero errors.

Usage:
    conda run -n qlib_run python -m sector_rotation.tests.ssrs_matrix
"""
from __future__ import annotations

import sys, os, copy, traceback, warnings
warnings.filterwarnings("ignore")
import logging
logging.getLogger().setLevel(logging.ERROR)

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..")))  # qlib-main/

from sector_rotation.data.loader import load_config, load_all
from sector_rotation.backtest.engine import SectorRotationBacktest
from sector_rotation.SectorRotationStrategyRuns import PARAM_SETS, apply_param_set


def main():
    base = load_config()
    prices, macro = load_all(config=base)
    names = list(PARAM_SETS.keys())
    print(f"PARAM_SETS={len(names)} | prices{prices.shape} macro{macro.shape}")

    print("\n== PART 1: single backtest (IS) — all params × V1/V2 ==")
    bt_err = 0; bt_ok = 0; v1r = []; v2r = []; rows = []
    for ps in names:
        for ver in ("v1", "v2"):
            try:
                cfg = apply_param_set(copy.deepcopy(base), PARAM_SETS.get(ps, {}))
                cfg.setdefault("signals", {})["signal_version"] = ver
                r = SectorRotationBacktest(cfg).run(prices=prices, macro=macro)
                m = r.metrics
                wh = getattr(r, "weights_history", None)
                nreb = len(wh) if wh is not None else 0
                ann = m.get("annual_return", m.get("ann_ret", float("nan")))
                shp = m.get("sharpe", m.get("sharpe_ratio"))
                mdd = m.get("max_drawdown", m.get("maxdd", float("nan")))
                (v1r if ver == "v1" else v2r).append(nreb)
                rows.append((ps, ver, ann, shp, mdd, nreb)); bt_ok += 1
            except Exception:
                bt_err += 1
                print(f"  !! BT {ps}/{ver}: {traceback.format_exc().splitlines()[-1][:110]}")
    print(f"backtests ok={bt_ok} err={bt_err} (expect {2*len(names)})")
    if v1r and v2r:
        ratio = (sum(v2r)/len(v2r)) / max(1e-9, sum(v1r)/len(v1r))
        print(f"  V1 reb min={min(v1r)} max={max(v1r)} | V2 reb min={min(v2r)} max={max(v2r)} | V2/V1={ratio:.2f}")

    print("\n== samples ==")
    shown = 0
    for ps, ver, ann, shp, mdd, nreb in rows:
        if shown < 8:
            s = f"{shp:.2f}" if isinstance(shp, (int, float)) and shp == shp else str(shp)
            try:
                print(f"  {ps+'/'+ver:28} ann={ann:6.1%} sharpe={s:>6} mdd={mdd:6.1%} reb={nreb}")
            except (TypeError, ValueError):
                print(f"  {ps+'/'+ver:28} ann={ann} sharpe={s} mdd={mdd} reb={nreb}")
            shown += 1

    ok = (bt_err == 0)
    print(f"\nRESULT: {'ALL PASS' if ok else 'ERRORS'} (bt_err={bt_err})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
