"""
AEUS Data Loader
================
Loads the AI Infra & Electric Utilities Strategy's data and constructs the **subsector
basket price series** that are the strategy's tradeable assets.

Three layers
------------
1. Single-stock prices  — read from the isolated AEUS price store
   (``price_data/elec_strategy/prices/`` via ``aeus_fetch_prices``).
2. Subsector baskets    — 8 synthetic 80/15/5 total-return indices built from the
   stock prices (``build_subsector_prices``).  These 8 series + the 3 benchmarks
   (XLU/GRID/SPY) are what the signals / optimizer / engine consume — exactly the
   shape SSRS's engine expects (N "assets" + benchmark columns).
3. Macro (FRED)         — VIX / yield curve / HY spread / etc., reused from the
   SSRS loader (self-contained, drives the regime model).

Basket construction (PIT-correct)
---------------------------------
A subsector basket holds primary 80% / backup1 15% / backup2 5%.  The **primary
anchors the basket from its first available date**; each **backup tier only
enters after ``min_history_months`` of its own history** (so late IPOs — ARM,
ALAB, CRDO, GFS — phase in without look-ahead), with surviving weights
renormalised.  The basket return on day t is the weighted sum of the constituent
stock returns under the weights effective on t; the basket price is the
cumulative total-return index.  This is the exact economic equivalent of holding
the 80/15/5 stock basket, so the engine's PnL/cost accounting at the subsector
level is correct.

SSRS-compatible function names (load_config / load_all / load_returns /
load_monthly_returns / load_macro_data / data_quality_report) are preserved so
the copied engine/signal modules keep importing them unchanged.
"""

from __future__ import annotations

import logging
import os
import pickle
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

_THIS_DIR = Path(__file__).resolve().parent
_PKG_DIR = _THIS_DIR.parent
_QLIB_DIR = _PKG_DIR.parent
_PROJECT_DIR = _QLIB_DIR.parent
for _p in (str(_QLIB_DIR), str(_PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from electric_utilities_strategy.data import universe as U
    from electric_utilities_strategy.data import aeus_fetch_prices as fp
except Exception:  # pragma: no cover
    import universe as U          # type: ignore
    import aeus_fetch_prices as fp  # type: ignore

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = _PKG_DIR / "config.yaml"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: Optional[Path] = None) -> dict:
    p = Path(path) if path else _DEFAULT_CONFIG_PATH
    with open(p, "r") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Cache helpers (macro only — prices are already a parquet store)
# ---------------------------------------------------------------------------

def _cache_path(cache_dir: Path, key: str) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{key}.pkl"


def _is_cache_fresh(path: Path, max_age_hours: float = 24.0) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) / 3600.0 < max_age_hours


def _load_cache(path: Path):
    with open(path, "rb") as f:
        return pickle.load(f)


def _save_cache(path: Path, obj) -> None:
    with open(path, "wb") as f:
        pickle.dump(obj, f)


# ---------------------------------------------------------------------------
# Single-stock prices (from the isolated AEUS store)
# ---------------------------------------------------------------------------

def load_stock_prices(
    tickers: List[str],
    start: Optional[str] = None,
    end: Optional[str] = None,
    field: str = "AdjClose",
    auto_fetch: bool = True,
) -> pd.DataFrame:
    """Wide (Date x ticker) adjusted-close DataFrame from the AEUS price store."""
    return fp.load_prices_wide(tickers, start=start, end=end, field=field, auto_fetch=auto_fetch)


# ---------------------------------------------------------------------------
# Subsector basket construction (the strategy's tradeable assets)
# ---------------------------------------------------------------------------

def build_subsector_prices(
    stock_prices: pd.DataFrame,
    min_history_months: int = 24,
    base_value: float = 100.0,
    stale_days: int = 10,
    graph_scores: "Optional[pd.DataFrame]" = None,
    kappa: float = 0.0,
    tilt_clip: float = 0.40,
) -> pd.DataFrame:
    """Build 8 synthetic 80/15/5 subsector total-return price indices.

    Parameters
    ----------
    stock_prices : DataFrame (Date x stock ticker), adjusted close.
    min_history_months : int
        Minimum own-history before a *backup* tier is allowed into its basket.
        The primary tier is never gated (it anchors the basket).
    base_value : float
        Starting index level for every basket.
    stale_days : int
        A weighted tier (primary/backup1/backup2) that was active but has had NO
        valid price for the trailing ``stale_days`` trading days is treated as
        unusable (halt / delisting / data outage); its weight is handed to the
        subsector's 0%-reserve stock (mechanism B) when the reserve is itself live.
        Short 1–2 day gaps never trigger.  When nothing goes stale, the reserve
        stays at 0% and basket returns are identical to the base construction.
    graph_scores / kappa / tilt_clip :
        AEUS_PLAN §2.5 purity tilt(回测篮子层)。graph_scores = (月末 × 板块)
        supply_chain z 分;逐日权重乘 1+clamp(κ·purity·g_上月末, ±clip) 再归一,
        与 stock_decompose.apply_purity_tilt(执行层)同公式。κ=0 → 本段整体
        跳过,逐 bit 等于基础构造(回归锚)。

    Returns
    -------
    DataFrame (Date x subsector), total-return index per subsector.
    """
    if stock_prices.empty:
        return pd.DataFrame()

    idx = stock_prices.index
    rets = stock_prices.pct_change()
    first_avail = {t: stock_prices[t].first_valid_index() for t in stock_prices.columns}
    # "live" = at least one valid price within the trailing stale_days window.
    live_mask = (stock_prices.notna().rolling(stale_days, min_periods=1).sum() > 0)

    def _active_from(t: str, primary: str) -> pd.Timestamp:
        fa = pd.Timestamp(first_avail[t])
        return fa if t == primary else fa + pd.DateOffset(months=min_history_months)

    basket_returns: Dict[str, pd.Series] = {}
    for s in U.subsector_names():
        base_w = U.subsector_weights(s)                  # {ticker: 0.80/0.15/0.05}
        primary = U.subsector_tickers(s)[0]
        reserve = U.subsector_reserve(s)
        cols = [t for t in base_w if t in stock_prices.columns and first_avail.get(t) is not None]
        if primary not in cols:
            logger.warning("Subsector %s: primary %s has no price data; skipping", s, primary)
            continue
        has_reserve = (reserve is not None and reserve in stock_prices.columns
                       and first_avail.get(reserve) is not None)
        all_cols = cols + ([reserve] if has_reserve else [])

        W = pd.DataFrame(0.0, index=idx, columns=all_cols)
        accident = pd.Series(0.0, index=idx)              # weight freed by stale tiers
        for t in cols:
            active = (idx >= _active_from(t, primary))    # boolean array over idx
            liveT = live_mask[t].reindex(idx).fillna(False).to_numpy()
            on = active & liveT
            W.loc[on, t] = base_w[t]
            # active but not live = accident → its weight cascades to the reserve
            accident.loc[active & ~liveT] += base_w[t]

        if has_reserve:
            live_r = live_mask[reserve].reindex(idx).fillna(False).to_numpy()
            res_on = (idx >= _active_from(reserve, primary)) & live_r & (accident.to_numpy() > 0)
            W[reserve] = np.where(res_on, accident.to_numpy(), 0.0)

        row_sum = W.sum(axis=1)
        # Renormalise rows (covers IPO phase-in AND any accident weight the reserve
        # couldn't absorb → falls back to the surviving live tiers).
        Wn = W.div(row_sum.where(row_sum > 0), axis=0).fillna(0.0)

        # §2.5 purity tilt(篮子层;κ=0 整段跳过 = 逐 bit 基础构造)
        if graph_scores is not None and kappa:
            g = graph_scores.get(s) if hasattr(graph_scores, "get") else None
            if g is not None and len(g):
                g_daily = g.shift(1).reindex(idx, method="ffill").fillna(0.0)  # 上月末分,PIT
                mult = pd.DataFrame(1.0, index=idx, columns=all_cols)
                for t_ in all_cols:
                    adj = (kappa * U.get_purity(t_, s)) * g_daily
                    mult[t_] = 1.0 + adj.clip(-tilt_clip, tilt_clip)
                Wt = Wn * mult
                rs_ = Wt.sum(axis=1)
                Wn = Wt.div(rs_.where(rs_ > 0), axis=0).fillna(0.0)
        br = (Wn * rets[all_cols]).sum(axis=1)
        br = br.where(row_sum > 0)                         # undefined before primary exists
        basket_returns[s] = br

    if not basket_returns:
        return pd.DataFrame()

    out = {}
    for s, br in basket_returns.items():
        brv = br.dropna()
        if brv.empty:
            continue
        out[s] = (1.0 + brv).cumprod() * base_value
    prices = pd.DataFrame(out).sort_index()
    return prices


def load_benchmark_prices(
    start: Optional[str] = None, end: Optional[str] = None,
    tickers: Optional[List[str]] = None,
) -> pd.DataFrame:
    """XLU / GRID / SPY adjusted-close prices for benchmark comparison."""
    bt = tickers or U.benchmark_tickers()
    return load_stock_prices(bt, start=start, end=end, field="AdjClose")


def load_subsector_prices(
    config: Optional[dict] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    include_benchmark: bool = True,
    graph_scores: "Optional[pd.DataFrame]" = None,
    kappa: float = 0.0,
    tilt_clip: float = 0.40,
) -> pd.DataFrame:
    """Build subsector baskets (+ benchmarks) ready for signals/engine."""
    cfg = config or load_config()
    data_cfg = cfg.get("data", {})
    uni_cfg = cfg.get("universe", {})
    start = start or data_cfg.get("price_start", "2016-01-01")
    end = end or data_cfg.get("price_end")
    min_hist = int(uni_cfg.get("min_history_months", 24))
    base_value = float(uni_cfg.get("basket_base_value", 100.0))

    stocks = load_stock_prices(U.all_tickers(), start=start, end=end)
    baskets = build_subsector_prices(stocks, min_history_months=min_hist, base_value=base_value,
                                     graph_scores=graph_scores, kappa=kappa, tilt_clip=tilt_clip)

    if include_benchmark:
        bench = load_benchmark_prices(start=start, end=end)
        if not bench.empty:
            baskets = baskets.join(bench, how="left")
    return _validate_and_clean_prices(baskets)


def _validate_and_clean_prices(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise index, drop dup dates, ffill gaps, drop all-NaN rows."""
    if df.empty:
        return df
    df = df.copy()
    df.index = pd.to_datetime(df.index).normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.ffill().dropna(how="all")
    return df


def load_prices(
    tickers: List[str],
    start: str,
    end: Optional[str] = None,
    source: str = "store",
    cache_dir: Optional[Path] = None,
    force_refresh: bool = False,
    cache_max_age_hours: float = 8.0,
    mongodb_cfg: Optional[dict] = None,
) -> pd.DataFrame:
    """SSRS-compatible price loader.

    If ``tickers`` are subsector names, returns the 8 subsector baskets (filtered
    to those names); otherwise returns single-stock adjusted closes.  Benchmarks
    requested in ``tickers`` are served from the stock store.
    """
    subs = [t for t in tickers if t in U.SUBSECTORS]
    others = [t for t in tickers if t not in U.SUBSECTORS]
    frames = []
    if subs:
        baskets = load_subsector_prices(start=start, end=end, include_benchmark=False)
        frames.append(baskets[[c for c in subs if c in baskets.columns]])
    if others:
        frames.append(load_stock_prices(others, start=start, end=end))
    if not frames:
        return pd.DataFrame()
    out = frames[0] if len(frames) == 1 else frames[0].join(frames[1:], how="outer")
    out = _validate_and_clean_prices(out)
    # restore requested order where possible
    cols = [t for t in tickers if t in out.columns]
    return out[cols] if cols else out


# ---------------------------------------------------------------------------
# Returns helpers (reused from SSRS, unchanged behaviour)
# ---------------------------------------------------------------------------

def load_returns(prices: pd.DataFrame, method: str = "log") -> pd.DataFrame:
    if method == "log":
        return np.log(prices / prices.shift(1)).iloc[1:]
    if method == "pct":
        return prices.pct_change().iloc[1:]
    raise ValueError(f"Unknown return method: {method}")


def load_monthly_returns(prices: pd.DataFrame) -> pd.DataFrame:
    monthly = prices.resample("ME").last()
    return monthly.pct_change().iloc[1:]


# ---------------------------------------------------------------------------
# Macro data (FRED) — reused verbatim from the SSRS loader (self-contained)
# ---------------------------------------------------------------------------

FRED_SERIES: Dict[str, str] = {
    "vix":           "VIXCLS",
    "yield_curve":   "T10Y2Y",
    "hy_spread":     "BAMLH0A0HYM2",
    "ig_spread":     "BAMLC0A0CM",
    "breakeven_10y": "T10YIE",
    "breakeven_5y":  "T5YIE",
    "effr":          "EFFR",
    "dgs10":         "DGS10",
    "dgs2":          "DGS2",
    "fin_stress":    "STLFSI4",
    "nfci":          "NFCI",
    "icsa":          "ICSA",
    "consumer_sent": "UMCSENT",
    "recession_flag": "USREC",
    "unrate":        "UNRATE",
}
WEEKLY_SERIES: set = {"fin_stress", "nfci", "icsa"}
MONTHLY_SERIES: set = {"consumer_sent", "recession_flag", "unrate"}


def _load_fred_series(field_name, series_id, start, end, api_key) -> pd.Series:
    from fredapi import Fred
    fred = Fred(api_key=api_key)
    end_dt = end or datetime.today().strftime("%Y-%m-%d")
    data = fred.get_series(series_id, observation_start=start, observation_end=end_dt)
    data.index = pd.to_datetime(data.index).normalize()
    data.name = field_name
    return data


def load_macro_data(
    start: str,
    end: Optional[str] = None,
    api_key: Optional[str] = None,
    cache_dir: Optional[Path] = None,
    force_refresh: bool = False,
    cache_max_age_hours: float = 24.0,
) -> pd.DataFrame:
    """Daily-indexed FRED macro frame (HY/IG spreads converted to bps)."""
    cache_key = f"macro_{start}_{end or 'latest'}"
    if cache_dir:
        cp = _cache_path(cache_dir, cache_key)
        if not force_refresh and _is_cache_fresh(cp, cache_max_age_hours):
            logger.info("Loading macro data from cache: %s", cp)
            return _load_cache(cp)

    resolved_key = api_key or os.environ.get("FRED_API_KEY")
    if not resolved_key:
        raise ValueError("FRED_API_KEY not set and no api_key passed to load_macro_data().")

    series_dict, failed = {}, []
    for field_name, series_id in FRED_SERIES.items():
        try:
            series_dict[field_name] = _load_fred_series(field_name, series_id, start, end, resolved_key)
        except Exception as e:  # noqa: BLE001
            logger.warning("FRED %s (%s) failed: %s", series_id, field_name, e)
            failed.append(field_name)
    if not series_dict:
        raise RuntimeError("All FRED series downloads failed.")

    end_dt = pd.Timestamp(end) if end else pd.Timestamp.now().normalize()
    start_dt = pd.Timestamp(start)
    idx = pd.date_range(start=start_dt, end=end_dt, freq="B")
    cal_idx = pd.date_range(start=start_dt, end=end_dt + pd.Timedelta(days=8), freq="D")

    result = pd.DataFrame(index=idx)
    for field_name, s in series_dict.items():
        s_cal = s.reindex(cal_idx)
        if field_name in MONTHLY_SERIES:
            s_cal = s_cal.ffill(limit=31)
        elif field_name in WEEKLY_SERIES:
            s_cal = s_cal.ffill(limit=7)
        else:
            s_cal = s_cal.ffill(limit=5)
        result[field_name] = s_cal.reindex(idx)

    for field_name in FRED_SERIES:
        if field_name not in result.columns:
            result[field_name] = np.nan
    for _spread in ("hy_spread", "ig_spread"):
        if _spread in result.columns:
            result[_spread] = result[_spread] * 100  # pct -> bps

    # ── PIT FREEZE (append-only): FRED revises recent data, which flips the
    #    regime classification on refresh → non-reproducible backtest.  Keep an
    #    immutable frozen macro store: historical rows never change; only dates
    #    newer than the frozen tail are appended.  Seeded from the first run.
    if cache_dir:
        try:
            frozen_path = Path(cache_dir) / "macro_frozen.parquet"
            if frozen_path.exists():
                frozen = pd.read_parquet(frozen_path)
                frozen.index = pd.to_datetime(frozen.index)
                frozen = frozen.reindex(columns=result.columns)
                last_frozen = frozen.index.max()
                new_rows = result[result.index > last_frozen]
                result = pd.concat([frozen, new_rows])
                result = result[~result.index.duplicated(keep="first")].sort_index()
            result.to_parquet(frozen_path)
        except Exception as _fe:  # noqa: BLE001
            logger.warning("macro PIT-freeze skipped (%s); using fresh FRED frame", _fe)

    if cache_dir:
        _save_cache(_cache_path(cache_dir, cache_key), result)
    return result


# ---------------------------------------------------------------------------
# Industry / company signal wrappers
# ---------------------------------------------------------------------------

def load_industry_data(config: Optional[dict] = None,
                       start: Optional[str] = None, end: Optional[str] = None) -> dict:
    """Return {tsmc_yoy, asml_orders, asml_guidance, dram_proxy} PIT daily Series."""
    try:
        from electric_utilities_strategy.data import industry_signals as ind
    except Exception:  # pragma: no cover
        import industry_signals as ind  # type: ignore
    return {
        "tsmc_yoy": ind.load_tsmc_monthly(start=start, end=end),
        # asml_orders 到 2026-01-28 为止(上游停更),asml_guidance 是它的接续序列
        "asml_orders": ind.load_asml_orders(start=start, end=end),
        "asml_guidance": ind.load_asml_guidance(start=start, end=end),
        "dram_proxy": ind.load_dram_proxy(start=start, end=end),
    }


def load_company_signals(config: Optional[dict] = None,
                         start: Optional[str] = None, end: Optional[str] = None) -> dict:
    """Return {capex_pulse, mu_dio} PIT daily Series."""
    try:
        from electric_utilities_strategy.data import company_signals as comp
    except Exception:  # pragma: no cover
        import company_signals as comp  # type: ignore
    return {
        "capex_pulse": comp.load_capex_pulse(start=start, end=end),
        "mu_dio": comp.load_mu_dio(start=start, end=end),
    }


# ---------------------------------------------------------------------------
# load_all
# ---------------------------------------------------------------------------

def load_all(
    config: Optional[dict] = None,
    config_path: Optional[Path] = None,
    force_refresh: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Load (subsector_prices + benchmarks, macro) per config.yaml.

    The returned price frame has 8 subsector columns followed by the benchmark
    columns (XLU/GRID/SPY).  The macro frame is FRED daily data.
    """
    cfg = config or load_config(config_path)
    data_cfg = cfg["data"]

    start = data_cfg.get("price_start", "2016-01-01")
    end = data_cfg.get("price_end")
    _raw_cache = data_cfg.get("cache_dir", "../../price_data/elec_strategy/cache")
    cache_dir = Path(_raw_cache)
    if not cache_dir.is_absolute():
        cache_dir = (_DEFAULT_CONFIG_PATH.parent / cache_dir).resolve()
    fred_key = os.environ.get(data_cfg.get("fred_api_key_env", "FRED_API_KEY"))

    prices = load_subsector_prices(config=cfg, start=start, end=end, include_benchmark=True)
    macro = load_macro_data(
        start=start, end=end, api_key=fred_key,
        cache_dir=cache_dir, force_refresh=force_refresh,
    )
    return prices, macro


# ---------------------------------------------------------------------------
# Data quality report
# ---------------------------------------------------------------------------

def data_quality_report(prices: pd.DataFrame, macro: pd.DataFrame) -> str:
    lines = ["=" * 64, "AEUS DATA QUALITY REPORT", "=" * 64]
    lines.append("\n--- Subsector / benchmark prices ---")
    if not prices.empty:
        lines.append(f"Date range : {prices.index[0].date()} → {prices.index[-1].date()}")
        lines.append(f"# Trading days: {len(prices)} | # columns: {len(prices.columns)}")
        rets = prices.pct_change().iloc[1:]
        for col in prices.columns:
            fv = prices[col].first_valid_index()
            r = rets[col].dropna()
            ann = r.mean() * 252 if len(r) else float("nan")
            vol = r.std() * np.sqrt(252) if len(r) else float("nan")
            lines.append(f"  {col:16}: from {fv.date() if fv is not None else 'NA'} "
                         f"ann={ann:6.1%} vol={vol:6.1%}")
    lines.append("\n--- Macro (FRED) ---")
    if not macro.empty:
        lines.append(f"Date range : {macro.index[0].date()} → {macro.index[-1].date()}")
        for col in macro.columns:
            nan_pct = macro[col].isna().mean() * 100
            last = macro[col].dropna().iloc[-1] if macro[col].notna().any() else float("nan")
            lines.append(f"  {col:14}: NaN={nan_pct:5.1f}%  last={last:.4g}")
    lines.append("=" * 64)
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    import argparse
    ap = argparse.ArgumentParser(description="AEUS data loader")
    ap.add_argument("--config", default=None)
    ap.add_argument("--force-refresh", action="store_true")
    ap.add_argument("--no-macro", action="store_true", help="Skip FRED (offline)")
    args = ap.parse_args()
    cfg = load_config(Path(args.config) if args.config else None)
    px = load_subsector_prices(config=cfg, include_benchmark=True)
    if args.no_macro:
        print(data_quality_report(px, pd.DataFrame()))
    else:
        prices, macro = load_all(config=cfg, force_refresh=args.force_refresh)
        print(data_quality_report(prices, macro))
