"""Honest IS/OOS evaluation of Plan 05 LONG-SHORT mode (proxy 2yr panel).

Protocol mirrors calibrate_risk.py exactly: calibrate on IS (first 14 months),
freeze the best-IS config, evaluate untouched OOS; weekly NW-t + DSR deflated
for the sweep size. Risk levers are FIXED at the calibrated frozen profile
(tv=0.20 absolute, DD −12%/−25%, rvol 60) — this evaluates the STRUCTURE
(long-short) and its own knobs, not a risk re-fit.

8 trials: {factor stack: full | carry-core} × {gross: 1.0 | 1.5} × {band: 0 | 2}.
carry_mode="level" for all (a-priori: the structural motivation IS the funding
cross-section). Decomposes OOS P&L into long-leg / short-leg / funding.

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.perp_rotation.evaluate_long_short
        [--is-months 14]
"""
from __future__ import annotations

import argparse
import copy
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.backtest.daily_engine import PerpRotationBacktest
from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.trade_stats import (deflated_sharpe,
                                                      trade_significance_report)
from crypto_trading.crypto_strategies.perp_rotation.data.loader import (load_returns,
                                                                        proxy_panel)
from crypto_trading.crypto_strategies.perp_rotation.run_backtest import (build_regime_inputs,
                                                                         load_config)

logger = logging.getLogger(__name__)
OUT_DIR = SIGNALS_DIR / "research"

FULL_STACK = {"cross_sectional_momentum": 0.40, "ts_momentum": 0.15,
              "carry": 0.20, "regime_adjustment": 0.25}
CARRY_CORE = {"cross_sectional_momentum": 0.0, "ts_momentum": 0.0,
              "carry": 0.75, "regime_adjustment": 0.25}
FROZEN_RISK = {"target_vol_annual": 0.20, "vol_target_mode": "absolute",
               "dd_halve_threshold": -0.12, "dd_flat_threshold": -0.25}


def ls_config(base: dict, *, stack: str, gross: float, band: int) -> dict:
    cfg = copy.deepcopy(base)
    cfg.setdefault("rebalance", {})["frequency"] = "weekly"
    cfg["rebalance"]["emergency_derisk_rvol"] = 60.0
    cfg.setdefault("costs", {})["fee_scenario"] = "projected"
    cfg.setdefault("signals", {})["weights"] = (FULL_STACK if stack == "full"
                                                else CARRY_CORE)
    cfg["signals"]["carry_mode"] = "level"
    cfg.setdefault("portfolio", {})["long_short"] = True
    cfg["portfolio"]["ls"] = {"k_per_side": 3, "gross": gross, "rank_band": band}
    cfg.setdefault("risk", {}).update(FROZEN_RISK)
    cfg.setdefault("stop_loss", {})["enabled"] = False   # LS uses DD tiers
    cfg.setdefault("universe", {})["min_perps_to_activate"] = 2
    return cfg


def run_window(cfg, prices, volumes, funding, start, end):
    regime = build_regime_inputs(prices, funding)
    res = PerpRotationBacktest(cfg).run(prices, funding, regime,
                                        volumes_notional=volumes,
                                        start=str(start), end=str(end))
    m = res.metrics
    return {"sharpe": m.get("sharpe", np.nan), "maxdd": m.get("max_drawdown", np.nan),
            "total_return": m.get("total_return", np.nan),
            "calmar": m.get("calmar", np.nan), "res": res}


def decompose(res, prices: pd.DataFrame, funding: pd.DataFrame,
              start, end) -> dict:
    """OOS P&L split: long leg / short leg / funding (daily, weights ffilled)."""
    w = res.weights_history
    if w.empty:
        return {}
    daily_ret = load_returns(prices[w.columns]).loc[str(start):str(end)]
    w_daily = w.reindex(daily_ret.index.union(w.index)).ffill() \
               .reindex(daily_ret.index).fillna(0.0)
    fund = funding.reindex(columns=w.columns).reindex(daily_ret.index).fillna(0.0)
    long_leg = (w_daily.clip(lower=0.0) * daily_ret).sum(axis=1)
    short_leg = (w_daily.clip(upper=0.0) * daily_ret).sum(axis=1)
    fund_leg = (w_daily * (-fund)).sum(axis=1)
    return {"long_leg_cum": float(long_leg.sum()),
            "short_leg_cum": float(short_leg.sum()),
            "funding_cum": float(fund_leg.sum()),
            "long_leg_sharpe": float(long_leg.mean() / long_leg.std() * np.sqrt(365))
                if long_leg.std() > 0 else np.nan,
            "short_leg_sharpe": float(short_leg.mean() / short_leg.std() * np.sqrt(365))
                if short_leg.std() > 0 else np.nan}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--is-months", type=int, default=14)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("crypto_trading").setLevel(logging.WARNING)

    prices, volumes, funding = proxy_panel()
    split = prices.index[0] + pd.DateOffset(months=args.is_months)
    is_start, is_end = prices.index[0].date(), (split - pd.Timedelta(days=1)).date()
    oos_start, oos_end = split.date(), prices.index[-1].date()
    base = load_config()

    trials = [{"stack": s, "gross": g, "band": b}
              for s in ("full", "carry_core") for g in (1.0, 1.5) for b in (0, 2)]
    rows = []
    for t in trials:
        cfg = ls_config(base, **t)
        try:
            r = run_window(cfg, prices, volumes, funding, is_start, is_end)
        except Exception as e:
            logger.warning("IS trial %s failed: %s", t, str(e)[:120])
            continue
        rows.append({**t, **{k: r[k] for k in ("sharpe", "maxdd", "calmar",
                                               "total_return")}})
        print(f"IS {t}: Sharpe {r['sharpe']:+.2f} maxDD {r['maxdd']:.1%} "
              f"ret {r['total_return']:+.1%}")

    is_df = pd.DataFrame(rows).sort_values(["calmar", "sharpe"],
                                           ascending=False).reset_index(drop=True)
    best = is_df.iloc[0].to_dict()
    frozen = {k: best[k] for k in ("stack", "gross", "band")}
    frozen["band"] = int(frozen["band"])

    cfg_best = ls_config(base, **frozen)
    oos = run_window(cfg_best, prices, volumes, funding, oos_start, oos_end)
    rets = oos["res"].daily_returns
    weekly = rets.resample("1W").sum().dropna()
    sig = trade_significance_report(weekly, k=min(5, max(2, len(weekly) // 8)))
    dsr = deflated_sharpe(rets, n_trials=len(is_df))
    legs = decompose(oos["res"], prices, funding, oos_start, oos_end)

    # Kalshi 52d descriptive
    kalshi_desc = {}
    try:
        from crypto_trading.crypto_strategies.perp_rotation.data.loader import build_perp_panel
        kp, kv, kf = build_perp_panel()
        ck = ls_config(base, **frozen)
        ck.setdefault("universe", {})["listing_history_floor_days"] = 5
        ck["signals"].setdefault("cs_momentum", {})["zscore_window_days"] = 20
        rk = run_window(ck, kp, kv, kf, kp.index[0].date(), kp.index[-1].date())
        kalshi_desc = {k: rk[k] for k in ("sharpe", "maxdd", "total_return")}
    except Exception as e:
        kalshi_desc = {"error": str(e)[:150]}

    report = {
        "is_window": [str(is_start), str(is_end)],
        "oos_window": [str(oos_start), str(oos_end)],
        "n_trials": len(is_df),
        "is_table": is_df.to_dict("records"),
        "frozen": frozen,
        "oos_frozen": {k: oos[k] for k in ("sharpe", "maxdd", "calmar", "total_return")},
        "oos_weekly_nw": {"t_nw": sig["t_nw"], "n": sig["n"],
                          "frac_positive": sig["purged_cv"]["frac_positive"]},
        "oos_dsr": {"sharpe": dsr["sharpe"], "dsr": dsr["dsr"]},
        "oos_decomposition": legs,
        "kalshi_52d_descriptive": kalshi_desc,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (OUT_DIR / f"plan05_longshort_{stamp}.json").write_text(
        json.dumps(report, indent=1, default=str))

    print("=" * 72)
    print("PLAN 05 LONG-SHORT — IS/OOS (proxy 2yr, frozen risk profile)")
    print("=" * 72)
    o = report["oos_frozen"]; nw = report["oos_weekly_nw"]
    print(f"frozen {frozen} | IS best Sharpe {best['sharpe']:+.2f}")
    print(f"OOS: Sharpe {o['sharpe']:+.2f}  maxDD {o['maxdd']:.1%}  "
          f"ret {o['total_return']:+.1%}  NW-t {nw['t_nw']:+.2f} (n={nw['n']})  "
          f"DSR {report['oos_dsr']['dsr']:.3f}")
    print(f"legs: {legs}")
    print(f"Kalshi 52d: {kalshi_desc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
