"""Plan 05 risk calibration on the 2-yr OKX proxy panel (honest IS/OOS).

Sweeps the risk levers that the uncalibrated run left unbound (maxDD −51%):
  * vol_target_mode="absolute" × target_vol ∈ {0.20, 0.30, 0.40}
  * DD tiers (halve, flat) ∈ {(−8,−15), (−10,−20), (−12,−25)} %
  * regime cash-out: emergency rvol ∈ {60, 80, none}
  * carry_mode ∈ {percentile, level} (level = cross-sectional funding ranking)

Discipline: calibrate ONLY on the IS window (first ~14 months of the proxy
panel); select by IS Calmar (Sharpe tiebreak); evaluate the FROZEN winner on
the untouched OOS remainder; report OOS with Newey-West t and deflate for the
number of configs swept. Proxy data is labeled — this calibrates RISK MECHANICS,
not Kalshi alpha (plan §8: never ship proxy-tuned params as validated).

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.perp_rotation.calibrate_risk
        [--is-months 14] [--quick] [--out config_calibrated.yaml]
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from crypto_trading.crypto_common.backtest.daily_engine import PerpRotationBacktest
from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.trade_stats import (deflated_sharpe,
                                                      trade_significance_report)
from crypto_trading.crypto_strategies.perp_rotation.data.loader import proxy_panel
from crypto_trading.crypto_strategies.perp_rotation.run_backtest import (build_regime_inputs,
                                                                         load_config)

logger = logging.getLogger(__name__)

OUT_DIR = SIGNALS_DIR / "perp_rotation" / "calibration"


def sweep_grid(quick: bool = False) -> list[dict]:
    """The bounded risk-lever grid (staged: carry_mode only on risk winners)."""
    vols = [0.20, 0.30] if quick else [0.20, 0.30, 0.40]
    dds = [(-0.10, -0.20)] if quick else [(-0.08, -0.15), (-0.10, -0.20), (-0.12, -0.25)]
    rvols = [60.0, None] if quick else [60.0, 80.0, None]
    grid = []
    for tv in vols:
        for dd_h, dd_f in dds:
            for rv in rvols:
                grid.append({"target_vol_annual": tv, "vol_target_mode": "absolute",
                             "dd_halve_threshold": dd_h, "dd_flat_threshold": dd_f,
                             "emergency_derisk_rvol": rv})
    return grid


def apply_config(base: dict, risk_over: dict, carry_mode: str = "percentile") -> dict:
    cfg = copy.deepcopy(base)
    cfg.setdefault("rebalance", {})["frequency"] = "weekly"     # established cadence
    cfg.setdefault("costs", {})["fee_scenario"] = "projected"
    r = cfg.setdefault("risk", {})
    r["target_vol_annual"] = risk_over["target_vol_annual"]
    r["vol_target_mode"] = risk_over["vol_target_mode"]
    r["dd_halve_threshold"] = risk_over["dd_halve_threshold"]
    r["dd_flat_threshold"] = risk_over["dd_flat_threshold"]
    rv = risk_over["emergency_derisk_rvol"]
    cfg.setdefault("rebalance", {})["emergency_derisk_rvol"] = (
        999.0 if rv is None else rv)                            # 999 ≈ disabled
    cfg.setdefault("signals", {})["carry_mode"] = carry_mode
    cfg["universe"] = dict(cfg.get("universe", {}))
    return cfg


def run_window(cfg: dict, prices, volumes, funding, start, end) -> dict:
    cfg = copy.deepcopy(cfg)
    cfg.setdefault("universe", {})["min_perps_to_activate"] = min(
        cfg.get("universe", {}).get("min_perps_to_activate", 6), len(prices.columns))
    regime = build_regime_inputs(prices, funding)
    res = PerpRotationBacktest(cfg).run(prices, funding, regime,
                                        volumes_notional=volumes, oi_notional=None,
                                        start=str(start), end=str(end))
    m = res.metrics
    rets = res.daily_returns.loc[str(start):str(end)]
    return {"sharpe": m.get("sharpe", np.nan), "maxdd": m.get("max_drawdown", np.nan),
            "total_return": m.get("total_return", np.nan),
            "calmar": m.get("calmar", np.nan),
            "n_stops": len(res.stop_loss_events or []),
            "returns": rets}


def calibrate(is_months: int = 14, quick: bool = False) -> dict:
    prices, volumes, funding = proxy_panel()
    split = prices.index[0] + pd.DateOffset(months=is_months)
    is_start, is_end = prices.index[0], split - pd.Timedelta(days=1)
    oos_start, oos_end = split, prices.index[-1]
    logger.info("IS %s→%s | OOS %s→%s", is_start.date(), is_end.date(),
                oos_start.date(), oos_end.date())

    base = load_config()
    grid = sweep_grid(quick)
    rows = []
    for i, g in enumerate(grid):
        cfg = apply_config(base, g)
        try:
            r = run_window(cfg, prices, volumes, funding, is_start.date(), is_end.date())
        except Exception as e:
            logger.warning("config %d failed: %s", i, str(e)[:120])
            continue
        rows.append({**g, **{k: r[k] for k in ("sharpe", "maxdd", "total_return",
                                               "calmar", "n_stops")}})
        logger.info("IS %d/%d: tv=%.2f dd=(%.2f,%.2f) rv=%s → Sharpe %.2f maxDD %.1f%%",
                    i + 1, len(grid), g["target_vol_annual"], g["dd_halve_threshold"],
                    g["dd_flat_threshold"], g["emergency_derisk_rvol"],
                    r["sharpe"], 100 * r["maxdd"])

    is_df = pd.DataFrame(rows)
    if is_df.empty:
        raise RuntimeError("no IS configs ran")
    is_df = is_df.sort_values(["calmar", "sharpe"], ascending=False).reset_index(drop=True)

    # stage 2: carry_mode on the top-3 risk configs (IS only)
    top3 = is_df.head(3).to_dict("records")
    stage2 = []
    for g in top3:
        for cm in ("percentile", "level"):
            cfg = apply_config(base, g, carry_mode=cm)
            r = run_window(cfg, prices, volumes, funding, is_start.date(), is_end.date())
            stage2.append({**{k: g[k] for k in ("target_vol_annual", "dd_halve_threshold",
                                                "dd_flat_threshold", "emergency_derisk_rvol")},
                           "carry_mode": cm,
                           **{k: r[k] for k in ("sharpe", "maxdd", "calmar",
                                                "total_return", "n_stops")}})
    s2 = pd.DataFrame(stage2).sort_values(["calmar", "sharpe"],
                                          ascending=False).reset_index(drop=True)
    best = s2.iloc[0].to_dict()
    n_trials = len(is_df) + len(s2)

    # FROZEN evaluation on OOS
    frozen = {k: best[k] for k in ("target_vol_annual", "dd_halve_threshold",
                                   "dd_flat_threshold", "emergency_derisk_rvol")}
    frozen["vol_target_mode"] = "absolute"
    cfg_best = apply_config(base, frozen, carry_mode=best["carry_mode"])
    oos = run_window(cfg_best, prices, volumes, funding, oos_start.date(), oos_end.date())
    weekly = oos["returns"].resample("1W").sum().dropna()
    sig = trade_significance_report(weekly, k=min(5, max(2, len(weekly) // 8)))
    dsr = deflated_sharpe(oos["returns"], n_trials=n_trials)

    # baseline OOS (old defaults) for comparison
    oos_base = run_window(apply_config(base, {
        "target_vol_annual": 0.40, "vol_target_mode": "spike",
        "dd_halve_threshold": -0.10, "dd_flat_threshold": None,
        "emergency_derisk_rvol": 60.0}), prices, volumes, funding,
        oos_start.date(), oos_end.date())

    report = {
        "is_window": [str(is_start.date()), str(is_end.date())],
        "oos_window": [str(oos_start.date()), str(oos_end.date())],
        "n_trials": n_trials,
        "best_config": {**frozen, "carry_mode": best["carry_mode"]},
        "is_best": {k: best[k] for k in ("sharpe", "maxdd", "calmar", "total_return")},
        "oos_frozen": {k: oos[k] for k in ("sharpe", "maxdd", "calmar",
                                           "total_return", "n_stops")},
        "oos_baseline_olddefaults": {k: oos_base[k] for k in ("sharpe", "maxdd",
                                                              "calmar", "total_return")},
        "oos_weekly_nw": {"t_nw": sig["t_nw"], "n": sig["n"],
                          "frac_positive": sig["purged_cv"]["frac_positive"]},
        "oos_dsr": {"sharpe": dsr["sharpe"], "dsr": dsr["dsr"]},
        "is_table_top10": is_df.head(10).drop(columns=["returns"], errors="ignore")
                                .to_dict("records"),
        "stage2_table": s2.to_dict("records"),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (OUT_DIR / f"calibration_{stamp}.json").write_text(
        json.dumps(report, indent=1, default=str))
    return report


def write_calibrated_yaml(report: dict, path: Path) -> None:
    base = load_config()
    cfg = apply_config(base, {**report["best_config"],
                              "vol_target_mode": "absolute"},
                       carry_mode=report["best_config"]["carry_mode"])
    header = (
        "# RISK-CALIBRATED Plan 05 profile — generated by calibrate_risk.py\n"
        f"# IS {report['is_window']} (proxy, {report['n_trials']} trials) | "
        f"OOS frozen: Sharpe {report['oos_frozen']['sharpe']:.2f}, "
        f"maxDD {report['oos_frozen']['maxdd']:.1%}\n"
        "# PROXY-calibrated risk mechanics — NOT validated Kalshi alpha (plan §8).\n"
        "# Use by passing this file explicitly; live default config.yaml unchanged.\n")
    path.write_text(header + yaml.safe_dump(cfg, sort_keys=False))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--is-months", type=int, default=14)
    ap.add_argument("--quick", action="store_true", help="reduced grid (smoke)")
    ap.add_argument("--out", default=str(Path(__file__).parent / "config_calibrated.yaml"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("crypto_trading").setLevel(logging.WARNING)   # quiet the engine

    report = calibrate(is_months=args.is_months, quick=args.quick)
    write_calibrated_yaml(report, Path(args.out))

    print("=" * 72)
    print("PLAN 05 RISK CALIBRATION (proxy 2yr, IS/OOS frozen)")
    print("=" * 72)
    print(f"IS  {report['is_window']}  best: {report['best_config']}")
    b = report["is_best"]; o = report["oos_frozen"]; ob = report["oos_baseline_olddefaults"]
    print(f"IS  best     : Sharpe {b['sharpe']:+.2f}  maxDD {b['maxdd']:.1%}  "
          f"ret {b['total_return']:+.1%}")
    print(f"OOS frozen   : Sharpe {o['sharpe']:+.2f}  maxDD {o['maxdd']:.1%}  "
          f"ret {o['total_return']:+.1%}  stops {o['n_stops']}")
    print(f"OOS baseline : Sharpe {ob['sharpe']:+.2f}  maxDD {ob['maxdd']:.1%}  "
          f"ret {ob['total_return']:+.1%}   (old spike-mode defaults)")
    nw = report["oos_weekly_nw"]; d = report["oos_dsr"]
    print(f"OOS weekly NW-t {nw['t_nw']:+.2f} (n={nw['n']})  "
          f"DSR {d['dsr']:.3f} (deflated for {report['n_trials']} trials)")
    print(f"wrote {Path(args.out).name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
