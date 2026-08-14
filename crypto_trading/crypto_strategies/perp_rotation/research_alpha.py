"""Plan 05 alpha research: WHY did the factor stack flip negative OOS, and do
theory-motivated fixes (regime-gated momentum, Kalshi carry cross-section)
rescue it? Rerunnable research CLI — writes artifacts, changes no live config.

Design discipline:
  * Risk profile HELD CONSTANT at the calibrated frozen config for every run
    (config_calibrated.yaml) so differences are ALPHA, not risk mechanics.
  * IS/OOS windows identical to calibrate_risk.py (IS = first 14 months of the
    proxy panel, OOS = untouched remainder).
  * ONE a-priori momentum-conditioning rule (from the plan's own regime
    machinery: momentum OFF in TRANSITION_DOWN/RISK_OFF, carry takes over) —
    no rule sweeping (that would be refitting).
  * Kalshi carry-only run is DESCRIPTIVE (n≈7 weekly rebalances — no
    significance theater).

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.perp_rotation.research_alpha
        [--is-months 14] [--quick]
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
from crypto_trading.crypto_common.regime import (RISK_OFF, RISK_ON, TRANSITION_DOWN,
                                                 TRANSITION_UP)
from crypto_trading.crypto_common.trade_stats import (deflated_sharpe,
                                                      trade_significance_report)
from crypto_trading.crypto_strategies.perp_rotation.data.loader import (build_funding_panel,
                                                                        build_perp_panel,
                                                                        proxy_panel)
from crypto_trading.crypto_strategies.perp_rotation.run_backtest import (_relax_short_history,
                                                                         build_regime_inputs)

logger = logging.getLogger(__name__)

CALIBRATED_YAML = Path(__file__).parent / "config_calibrated.yaml"
OUT_DIR = SIGNALS_DIR / "research"

# Factor-isolation weight sets (bonuses explicitly zeroed — they default ON).
_ZERO_BONUS = {"acceleration_bonus": 0.0, "low_vol_bonus": 0.0}
FACTOR_WEIGHTS = {
    "full_stack": None,                                  # config default (incl. bonuses)
    "cs_momentum_only": {"cross_sectional_momentum": 1.0, "ts_momentum": 0.0,
                         "carry": 0.0, **_ZERO_BONUS},
    "ts_momentum_only": {"cross_sectional_momentum": 0.0, "ts_momentum": 1.0,
                         "carry": 0.0, **_ZERO_BONUS},
    "carry_only": {"cross_sectional_momentum": 0.0, "ts_momentum": 0.0,
                   "carry": 1.0, **_ZERO_BONUS},
}

# THE a-priori conditioning rule (plan §6 taken to its limit): momentum OFF in
# down/stress states; carry takes over. Stated once, not swept.
APRIORI_MULTIPLIERS = {
    RISK_ON: {"cross_sectional_momentum": 1.0, "ts_momentum": 1.0, "carry": 1.0},
    TRANSITION_UP: {"cross_sectional_momentum": 1.0, "ts_momentum": 1.0, "carry": 1.0},
    TRANSITION_DOWN: {"cross_sectional_momentum": 0.0, "ts_momentum": 0.5, "carry": 1.5},
    RISK_OFF: {"cross_sectional_momentum": 0.0, "ts_momentum": 0.0, "carry": 2.0},
}


def load_calibrated() -> dict:
    return yaml.safe_load(CALIBRATED_YAML.read_text())


def run_window(cfg: dict, prices, volumes, funding, start, end,
               oi=None) -> dict:
    cfg = copy.deepcopy(cfg)
    cfg.setdefault("universe", {})["min_perps_to_activate"] = min(
        cfg.get("universe", {}).get("min_perps_to_activate", 6), len(prices.columns))
    regime = build_regime_inputs(prices, funding)
    res = PerpRotationBacktest(cfg).run(prices, funding, regime,
                                        volumes_notional=volumes, oi_notional=oi,
                                        start=str(start), end=str(end))
    rets = res.daily_returns.loc[str(start):str(end)]
    weekly = rets.resample("1W").sum().dropna()
    nw = (trade_significance_report(weekly, k=min(5, max(2, len(weekly) // 8)))
          if len(weekly) >= 8 else None)
    return {"sharpe": res.metrics.get("sharpe", np.nan),
            "maxdd": res.metrics.get("max_drawdown", np.nan),
            "total_return": res.metrics.get("total_return", np.nan),
            "nw_t": nw["t_nw"] if nw else np.nan,
            "n_weeks": len(weekly),
            "returns": rets,
            "regime_counts": None}


def _variant_cfg(base: dict, weights: dict | None = None,
                 multipliers: dict | None = None, fee: str | None = None,
                 carry_mode: str | None = None) -> dict:
    cfg = copy.deepcopy(base)
    sig = cfg.setdefault("signals", {})
    if weights is not None:
        sig["weights"] = dict(weights)
    if multipliers is not None:
        sig["regime_multipliers"] = multipliers
    if carry_mode is not None:
        sig["carry_mode"] = carry_mode
    if fee is not None:
        cfg.setdefault("costs", {})["fee_scenario"] = fee
    return cfg


def research(is_months: int = 14, quick: bool = False) -> dict:
    base = load_calibrated()
    prices, volumes, funding = proxy_panel()
    split = prices.index[0] + pd.DateOffset(months=is_months)
    windows = {"IS": (prices.index[0].date(), (split - pd.Timedelta(days=1)).date()),
               "OOS": (split.date(), prices.index[-1].date())}
    report: dict = {"windows": {k: [str(a), str(b)] for k, (a, b) in windows.items()}}

    # ── 1. factor attribution IS vs OOS ────────────────────────────────────
    attrib = {}
    names = list(FACTOR_WEIGHTS) if not quick else ["full_stack", "cs_momentum_only",
                                                    "carry_only"]
    for name in names:
        w = FACTOR_WEIGHTS[name]
        row = {}
        for wname, (a, b) in windows.items():
            r = run_window(_variant_cfg(base, weights=w), prices, volumes, funding, a, b)
            row[wname] = {k: r[k] for k in ("sharpe", "maxdd", "total_return", "nw_t",
                                            "n_weeks")}
        attrib[name] = row
        logger.info("attrib %s: IS %.2f / OOS %.2f (NW-t %.2f)", name,
                    row["IS"]["sharpe"], row["OOS"]["sharpe"], row["OOS"]["nw_t"])
    report["attribution"] = attrib

    # ── 2. a-priori momentum regime-conditioning (full stack) ──────────────
    cond = {}
    for wname, (a, b) in windows.items():
        r = run_window(_variant_cfg(base, multipliers=APRIORI_MULTIPLIERS),
                       prices, volumes, funding, a, b)
        cond[wname] = {k: r[k] for k in ("sharpe", "maxdd", "total_return", "nw_t")}
    report["conditioned_full_stack"] = cond

    # regime-state distribution over the panel (is the conditioner even active?)
    regime_inputs = build_regime_inputs(prices, funding)
    from crypto_trading.crypto_common.regime import compute_regime
    reg = compute_regime(regime_inputs, method="rules")
    report["regime_distribution"] = reg.value_counts(normalize=True).round(3).to_dict()

    # ── 3. Kalshi carry-only (descriptive) ────────────────────────────────
    try:
        kp, kv, koi = build_perp_panel()
        kf = build_funding_panel()
        kcfg = _variant_cfg(base, weights=FACTOR_WEIGHTS["carry_only"],
                            carry_mode="level")
        _relax_short_history(kcfg, len(kp))
        kcfg.setdefault("rebalance", {})["frequency"] = "weekly"
        r = run_window(kcfg, kp, kv, kf, kp.index[0].date(), kp.index[-1].date(), oi=koi)
        wk = r["returns"].resample("1W").sum().dropna()
        report["kalshi_carry_only"] = {
            "n_days": len(kp), "total_return": r["total_return"],
            "weekly_mean_pct": float(wk.mean() * 100) if len(wk) else None,
            "weekly_returns_pct": [round(float(x) * 100, 3) for x in wk],
            "note": "DESCRIPTIVE ONLY — ~7 weekly rebalances, no significance claimed",
        }
    except Exception as e:
        report["kalshi_carry_only"] = {"error": str(e)[:200]}

    # ── 4. OOS fee attribution (full stack) ───────────────────────────────
    a, b = windows["OOS"]
    fee_rows = {}
    for fee in ("projected", "zero"):
        r = run_window(_variant_cfg(base, fee=fee), prices, volumes, funding, a, b)
        fee_rows[fee] = {k: r[k] for k in ("sharpe", "total_return", "maxdd")}
    fee_rows["fee_drag_return"] = round(
        fee_rows["zero"]["total_return"] - fee_rows["projected"]["total_return"], 4)
    report["oos_fee_attribution"] = fee_rows

    # ── 5. synthesis: best defensible variant OOS, deflated ───────────────
    # candidates examined (a priori set, not swept): default stack, conditioned
    # stack — 2 candidates; deflate OOS sharpe for n_trials=2.
    cand = {"default": attrib["full_stack"]["OOS"]["sharpe"],
            "conditioned": cond["OOS"]["sharpe"]}
    best_name = max(cand, key=lambda k: (np.nan_to_num(cand[k], nan=-9)))
    best_cfg = (_variant_cfg(base, multipliers=APRIORI_MULTIPLIERS)
                if best_name == "conditioned" else base)
    r = run_window(best_cfg, prices, volumes, funding, *windows["OOS"])
    d = deflated_sharpe(r["returns"], n_trials=2)
    report["synthesis"] = {"best_variant": best_name, "oos_sharpe": r["sharpe"],
                           "oos_nw_t": r["nw_t"], "dsr_2trials": d["dsr"]}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (OUT_DIR / f"plan05_alpha_{stamp}.json").write_text(
        json.dumps(report, indent=1, default=str))
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--is-months", type=int, default=14)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("crypto_trading").setLevel(logging.WARNING)

    rep = research(is_months=args.is_months, quick=args.quick)
    print("=" * 76)
    print("PLAN 05 ALPHA RESEARCH (risk held at calibrated profile)")
    print("=" * 76)
    print(f"windows: IS {rep['windows']['IS']}  OOS {rep['windows']['OOS']}")
    print("\n[1] factor attribution (Sharpe IS / OOS, OOS NW-t):")
    for name, row in rep["attribution"].items():
        print(f"  {name:18} IS {row['IS']['sharpe']:+6.2f}  "
              f"OOS {row['OOS']['sharpe']:+6.2f}  NW-t {row['OOS']['nw_t']:+5.2f}  "
              f"OOSret {row['OOS']['total_return']:+7.1%}")
    c = rep["conditioned_full_stack"]
    print(f"\n[2] a-priori conditioned stack: IS {c['IS']['sharpe']:+.2f} → "
          f"OOS {c['OOS']['sharpe']:+.2f} (NW-t {c['OOS']['nw_t']:+.2f}, "
          f"ret {c['OOS']['total_return']:+.1%})")
    print(f"    regime distribution: {rep['regime_distribution']}")
    k = rep["kalshi_carry_only"]
    if "error" not in k:
        print(f"\n[3] Kalshi carry-only (level, weekly, {k['n_days']}d): "
              f"ret {k['total_return']:+.2%}, weekly mean {k['weekly_mean_pct']:+.3f}% "
              f"({k['note']})")
    else:
        print(f"\n[3] Kalshi carry-only: ERROR {k['error']}")
    f = rep["oos_fee_attribution"]
    print(f"\n[4] OOS fee attribution: projected ret {f['projected']['total_return']:+.1%} "
          f"vs zero-fee {f['zero']['total_return']:+.1%} → fee drag "
          f"{f['fee_drag_return']:+.1%}")
    s = rep["synthesis"]
    print(f"\n[5] best defensible variant: {s['best_variant']} — OOS Sharpe "
          f"{s['oos_sharpe']:+.2f}, NW-t {s['oos_nw_t']:+.2f}, DSR(2) {s['dsr_2trials']:.3f}")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
