"""
Parameter Sensitivity Analysis
================================
Systematic study of how backtest performance changes across parameter variations.

Parameters swept:
    1. Momentum lookback window : [6, 9, 12, 15, 18] months
    2. Top-N sectors held       : [3, 4, 5, 6]
    3. Rebalance frequency      : monthly (fixed; biweekly as extension)
    4. Volatility scaling threshold: [1.2, 1.5, 2.0, disabled]
    5. Rebalance z-score threshold : [0.0, 0.3, 0.5, 0.8, 1.0]
    6. Optimizer method         : ["inv_vol", "risk_parity", "gmv", "equal_weight"]

For each parameter combination the engine runs a full backtest and records:
    Sharpe, CAGR, MaxDD, Calmar, IR, turnover

Results are returned as a DataFrame suitable for heatmap or line plots.

Design note:
    Grid search can be expensive (~minutes). Results are cached to disk.
    Use `force_rerun=True` to invalidate cache.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import pickle
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parameter grid definitions
# ---------------------------------------------------------------------------

SENSITIVITY_GRIDS: Dict[str, dict] = {
    "momentum_lookback": {
        "param_path": ["signals", "cs_momentum", "lookback_months"],
        "values": [6, 9, 12, 15, 18],
        "label": "CS Momentum Lookback (months)",
        "default": 12,
    },
    "top_n_sectors": {
        "param_path": ["portfolio", "top_n_sectors"],
        "values": [3, 4, 5, 6],
        "label": "Top-N Sectors Held",
        "default": 4,
    },
    "vol_scale_threshold": {
        "param_path": ["risk", "vol_scaling", "scale_threshold"],
        "values": [1.2, 1.5, 2.0, None],  # None = disable vol scaling
        "label": "Vol Scaling Threshold (×avg)",
        "default": 1.5,
    },
    "zscore_threshold": {
        "param_path": ["rebalance", "zscore_change_threshold"],
        "values": [0.0, 0.3, 0.5, 0.8, 1.0],
        "label": "Z-score Change Threshold (σ)",
        "default": 0.5,
    },
    "optimizer": {
        "param_path": ["portfolio", "optimizer"],
        "values": ["inv_vol", "risk_parity", "gmv", "equal_weight"],
        "label": "Portfolio Optimizer",
        "default": "inv_vol",
    },
    "max_weight": {
        "param_path": ["portfolio", "constraints", "max_weight"],
        "values": [0.25, 0.30, 0.40, 0.50],
        "label": "Max Single-Sector Weight",
        "default": 0.40,
    },
}


# ---------------------------------------------------------------------------
# Config mutation helpers
# ---------------------------------------------------------------------------

def _set_nested(cfg: dict, path: List[str], value) -> dict:
    """
    Set a nested config value by dot-path list.
    Returns a deep copy of cfg with the value set.
    """
    cfg = deepcopy(cfg)
    d = cfg
    for key in path[:-1]:
        d = d.setdefault(key, {})
    if value is None:
        # Special case: disable feature
        if path[-1] == "scale_threshold":
            d["enabled"] = False
        else:
            d[path[-1]] = value
    else:
        d[path[-1]] = value
        # Re-enable if it was disabled
        if path[-1] == "scale_threshold":
            cfg.setdefault("risk", {}).setdefault("vol_scaling", {})["enabled"] = True
    return cfg


def _get_nested(cfg: dict, path: List[str]):
    """Get a nested config value by dot-path list."""
    d = cfg
    for key in path:
        if not isinstance(d, dict) or key not in d:
            return None
        d = d[key]
    return d


# ---------------------------------------------------------------------------
# Single run wrapper
# ---------------------------------------------------------------------------

def _run_single(
    cfg: dict,
    prices: pd.DataFrame,
    macro: pd.DataFrame,
) -> Dict[str, float]:
    """
    Run one backtest with given config and return key metrics.
    Returns dict: sharpe, cagr, max_dd, calmar, info_ratio, annual_turnover.
    """
    import sys
    from pathlib import Path as P
    sys.path.insert(0, str(P(__file__).parent.parent.parent))

    from sector_rotation.backtest.engine import SectorRotationBacktest
    from sector_rotation.backtest.costs import estimate_annual_costs

    try:
        engine = SectorRotationBacktest(cfg)
        result = engine.run(prices=prices, macro=macro)
        m = result.metrics

        # Turnover
        ann_to = float("nan")
        if not result.costs_history.empty and "turnover_pct" in result.costs_history.columns:
            monthly_to = result.costs_history["turnover_pct"] / 100.0
            ann_to = float(monthly_to.mean() * 12) if len(monthly_to) > 0 else float("nan")

        return {
            "sharpe": m.get("sharpe", float("nan")),
            "cagr": m.get("annual_return", float("nan")),
            "max_dd": m.get("max_drawdown", float("nan")),
            "calmar": m.get("calmar", float("nan")),
            "info_ratio": m.get("info_ratio", float("nan")),
            "annual_turnover": ann_to,
        }
    except Exception as e:
        logger.warning(f"Run failed: {e}")
        return {k: float("nan") for k in
                ["sharpe", "cagr", "max_dd", "calmar", "info_ratio", "annual_turnover"]}


# ---------------------------------------------------------------------------
# Single-parameter sweep
# ---------------------------------------------------------------------------

def sweep_parameter(
    param_name: str,
    base_config: dict,
    prices: pd.DataFrame,
    macro: pd.DataFrame,
    cache_dir: Optional[Path] = None,
    force_rerun: bool = False,
) -> pd.DataFrame:
    """
    Sweep one parameter across its predefined value grid.

    Parameters
    ----------
    param_name : str
        Key in SENSITIVITY_GRIDS (e.g. "momentum_lookback").
    base_config : dict
        Base config.yaml dict. The swept parameter overrides this.
    prices, macro : pd.DataFrame
        Pre-loaded data.
    cache_dir : Path, optional
        Cache directory for results.
    force_rerun : bool
        Ignore cache and rerun.

    Returns
    -------
    pd.DataFrame
        Rows = parameter values, columns = metrics.
        Index = parameter value, columns = [sharpe, cagr, max_dd, calmar, info_ratio, annual_turnover].
    """
    if param_name not in SENSITIVITY_GRIDS:
        raise ValueError(f"Unknown parameter: {param_name}. Available: {list(SENSITIVITY_GRIDS)}")

    grid = SENSITIVITY_GRIDS[param_name]
    cache_key = f"sensitivity_{param_name}_{hashlib.md5(json.dumps(base_config, sort_keys=True, default=str).encode()).hexdigest()[:8]}"

    if cache_dir:
        cache_path = Path(cache_dir) / f"{cache_key}.pkl"
        if not force_rerun and cache_path.exists():
            logger.info(f"Loading sensitivity results from cache: {cache_path}")
            with open(cache_path, "rb") as f:
                return pickle.load(f)

    results = []
    for val in grid["values"]:
        label = "disabled" if val is None else str(val)
        logger.info(f"Sweeping {param_name} = {label}")

        cfg = _set_nested(base_config, grid["param_path"], val)
        metrics = _run_single(cfg, prices, macro)
        metrics["param_value"] = label
        results.append(metrics)

    df = pd.DataFrame(results).set_index("param_value")

    if cache_dir:
        cache_path = Path(cache_dir) / f"{cache_key}.pkl"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "wb") as f:
            pickle.dump(df, f)

    return df


# ---------------------------------------------------------------------------
# Multi-parameter grid search (2D)
# ---------------------------------------------------------------------------

def grid_search_2d(
    param_x: str,
    param_y: str,
    base_config: dict,
    prices: pd.DataFrame,
    macro: pd.DataFrame,
    metric: str = "sharpe",
    cache_dir: Optional[Path] = None,
    force_rerun: bool = False,
) -> pd.DataFrame:
    """
    2D parameter grid search (param_x × param_y).

    Returns a pivot DataFrame where rows=param_x values, cols=param_y values,
    values=metric. Suitable for heatmap visualization.

    Parameters
    ----------
    param_x, param_y : str
        Parameter names from SENSITIVITY_GRIDS.
    metric : str
        Metric to display: "sharpe" | "cagr" | "max_dd" | "calmar".
    """
    grid_x = SENSITIVITY_GRIDS[param_x]
    grid_y = SENSITIVITY_GRIDS[param_y]

    cache_key = f"grid2d_{param_x}_{param_y}_{metric}_{hashlib.md5(json.dumps(base_config, sort_keys=True, default=str).encode()).hexdigest()[:8]}"

    if cache_dir:
        cache_path = Path(cache_dir) / f"{cache_key}.pkl"
        if not force_rerun and cache_path.exists():
            with open(cache_path, "rb") as f:
                return pickle.load(f)

    records = []
    combos = list(itertools.product(grid_x["values"], grid_y["values"]))
    logger.info(f"2D grid search: {param_x} × {param_y}, {len(combos)} combinations")

    for val_x, val_y in combos:
        lx = "disabled" if val_x is None else str(val_x)
        ly = "disabled" if val_y is None else str(val_y)
        logger.info(f"  {param_x}={lx}, {param_y}={ly}")

        cfg = _set_nested(base_config, grid_x["param_path"], val_x)
        cfg = _set_nested(cfg, grid_y["param_path"], val_y)
        m = _run_single(cfg, prices, macro)
        records.append({param_x: lx, param_y: ly, metric: m.get(metric, float("nan"))})

    df = pd.DataFrame(records)
    pivot = df.pivot(index=param_x, columns=param_y, values=metric)

    if cache_dir:
        with open(cache_path, "wb") as f:
            pickle.dump(pivot, f)

    return pivot


# ---------------------------------------------------------------------------
# Full sensitivity report
# ---------------------------------------------------------------------------

def run_full_sensitivity(
    base_config: dict,
    prices: pd.DataFrame,
    macro: pd.DataFrame,
    params_to_sweep: Optional[List[str]] = None,
    cache_dir: Optional[Path] = None,
    force_rerun: bool = False,
) -> Dict[str, pd.DataFrame]:
    """
    Run sensitivity analysis for all (or selected) parameters.

    Returns
    -------
    dict: {param_name: pd.DataFrame of metrics per value}
    """
    if params_to_sweep is None:
        params_to_sweep = list(SENSITIVITY_GRIDS.keys())

    results = {}
    for param in params_to_sweep:
        logger.info(f"\n{'='*50}\nSweeping: {param}\n{'='*50}")
        df = sweep_parameter(param, base_config, prices, macro,
                             cache_dir=cache_dir, force_rerun=force_rerun)
        results[param] = df
        logger.info(f"\n{df.round(3)}")

    return results


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def sensitivity_summary_table(results: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    Summarize sensitivity results: for each parameter, show the best and worst
    Sharpe across its value range, and the range (max - min).

    Returns pd.DataFrame with columns:
        param, best_value, best_sharpe, worst_value, worst_sharpe, sharpe_range,
        default_sharpe
    """
    rows = []
    for param_name, df in results.items():
        if "sharpe" not in df.columns or df["sharpe"].isna().all():
            continue
        grid = SENSITIVITY_GRIDS.get(param_name, {})
        default = str(grid.get("default", ""))
        sharpe = df["sharpe"].dropna()
        rows.append({
            "parameter": param_name,
            "label": grid.get("label", param_name),
            "n_values": len(df),
            "best_value": sharpe.idxmax(),
            "best_sharpe": round(sharpe.max(), 3),
            "worst_value": sharpe.idxmin(),
            "worst_sharpe": round(sharpe.min(), 3),
            "sharpe_range": round(sharpe.max() - sharpe.min(), 3),
            "default_sharpe": round(sharpe.get(default, float("nan")), 3) if default in sharpe.index else float("nan"),
        })

    return pd.DataFrame(rows).set_index("parameter")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from sector_rotation.data.loader import load_all, load_config

    cfg = load_config()
    prices, macro = load_all(config=cfg)
    cache_dir = Path(cfg.get("data", {}).get("cache_dir", "sector_rotation/data/cache"))

    # Quick single-param sweep
    df = sweep_parameter("top_n_sectors", cfg, prices, macro, cache_dir=cache_dir)
    print("\n=== Top-N Sectors Sensitivity ===")
    print(df.round(3))
