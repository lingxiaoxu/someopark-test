"""
composite.py — AEUS 4-factor composite signal
=============================================
Blends the four AEUS factors into a single cross-sectional score per subsector
per month, conditioned on the macro regime:

    cs_momentum   0.30   12-1 month basket momentum (cross-sectional z)
    supply_chain  0.35   knowledge-graph propagation score  (core AEUS alpha)
    capex_pulse   0.25   hyperscaler AI-CapEx pulse tilt
    cycle_regime  0.10   VIX/CapEx regime tilt (defensive vs AI-cycle)

The function signature mirrors the AEUS ``compute_composite_signals`` so the
copied engine calls it unchanged; the legacy value/new-signal parameters are
accepted but ignored (AEUS V1 has no P/E value factor).  Returns the same
``(composite_df, regime_monthly, components)`` contract.

Regime conditioning reuses the AEUS 4-state machine (risk_on / transition_up /
transition_down / risk_off) with AEUS-tightened VIX thresholds (22/28) and
AEUS regime-weight multipliers; a defensive bonus is applied to the
``analog_defense`` subsector in risk-off.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from .momentum import compute_all_momentum
from .regime import compute_regime, regime_to_monthly, RISK_ON, RISK_OFF, TRANSITION_UP, TRANSITION_DOWN
from .supply_chain import compute_supply_chain_scores, load_graph_from_config

try:
    from ..data import universe as U
except Exception:  # pragma: no cover
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from electric_utilities_strategy.data import universe as U  # type: ignore

logger = logging.getLogger(__name__)

# --- AEUS 4-factor weights (must sum to 1.0) -------------------------------
DEFAULT_WEIGHTS = {
    "cs_momentum":  0.30,
    "supply_chain": 0.35,
    "capex_pulse":  0.25,
    "cycle_regime": 0.10,
}

# --- AEUS regime-conditional weight multipliers (4-state machine) ----------
DEFAULT_REGIME_WEIGHT_MULTIPLIERS = {
    RISK_ON:        {"cs_momentum": 1.1, "supply_chain": 1.1, "capex_pulse": 1.2, "cycle_regime": 0.8},
    TRANSITION_UP:  {"cs_momentum": 1.1, "supply_chain": 1.1, "capex_pulse": 1.1, "cycle_regime": 0.9},
    TRANSITION_DOWN: {"cs_momentum": 0.8, "supply_chain": 0.9, "capex_pulse": 0.7, "cycle_regime": 1.3},
    RISK_OFF:       {"cs_momentum": 0.5, "supply_chain": 0.5, "capex_pulse": 0.3, "cycle_regime": 1.8},
}

# 单一真源 = data/universe.py(AEUS_PLAN §3.4;STOCK_TIER 教训)
DEFENSIVE_TICKERS = list(U.DEFENSIVE_SUBSECTORS)
DEFENSIVE_BONUS_RISK_OFF = 0.40

# Per-subsector sensitivity to the AI-CapEx pulse (capex factor beta).
CAPEX_BETA = dict(U.CAPEX_BETA)

# 高波动组(regime 防御逻辑里被减配的那组;AEUS = SMR/储能/商户电厂最烈)
HIGH_BETA_SUBSECTORS = {"nuclear_fuel", "renewables_storage", "ipp_wholesale"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_weights(weights: Dict[str, float]) -> None:
    core = {k: weights.get(k, 0.0) for k in DEFAULT_WEIGHTS}
    total = sum(core.values())
    if abs(total - 1.0) > 1e-6:
        logger.warning("AEUS signal weights sum to %.4f (expected 1.0): %s", total, core)


def _cs_zscore(df: pd.DataFrame) -> pd.DataFrame:
    mu = df.mean(axis=1)
    sd = df.std(axis=1).replace(0, np.nan)
    return df.sub(mu, axis=0).div(sd, axis=0).fillna(0.0)


def _load_aux_signals(prices: pd.DataFrame, signal_kwargs: dict) -> dict:
    """Load CapEx pulse + AEUS external PIT series bounded to the price window.

    Returns node series + the ``external_tilts`` dict consumed by
    ``compute_supply_chain_scores`` (AEUS_PLAN §4.2 挂接表;每板块 ≤2 tilt)。
    Any individual source failing → that entry None/empty → graceful-0.
    """
    end = prices.index[-1].strftime("%Y-%m-%d") if len(prices) else None
    out = {"capex": None, "demand": None, "gas_price": None, "pmi": None,
           "tilts": {}}
    try:
        from ..data import company_signals as comp
        from ..data import industry_signals as ind
        from ..data import altdata_signals as alt
        from ..data import ercot_signals as erc
        from ..data import pjm_signals as pjm
    except Exception:  # pragma: no cover
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from electric_utilities_strategy.data import company_signals as comp  # type: ignore
        from electric_utilities_strategy.data import industry_signals as ind  # type: ignore
        from electric_utilities_strategy.data import altdata_signals as alt  # type: ignore
        from electric_utilities_strategy.data import ercot_signals as erc  # type: ignore
        from electric_utilities_strategy.data import pjm_signals as pjm  # type: ignore

    def _safe(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            logger.warning("aux %s failed: %s", getattr(fn, "__name__", fn), e)
            return None

    # ── 节点序列(通路①)──
    out["capex"] = _safe(comp.load_capex_pulse, end=end)
    out["demand"] = _safe(alt.load_power_demand_structural, end=end)
    out["gas_price"] = _safe(ind.load_gas_price_proxy, end=end)
    out["pmi"] = _safe(ind.load_pmi_series, end=end)

    # ── 确认 tilt(通路②;每板块 ≤2,supply_chain 内再硬性封顶)──
    def _mean_z(series_list):
        parts = [s for s in series_list if s is not None and len(s)]
        if not parts:
            return None
        df = pd.concat(parts, axis=1).sort_index().ffill()
        return df.mean(axis=1).dropna()

    # 电价脉冲:零售电价 YoY z + CPI 电力 YoY z + ERCOT/PJM 枢纽 z 的 z 均值
    elec_price = _safe(ind.load_elec_price, end=end)
    price_yoy_z = None
    if elec_price is not None and len(elec_price) > 300:
        yoy = elec_price.pct_change(252) * 100.0
        m = yoy.rolling(756, min_periods=252)
        price_yoy_z = ((yoy - m.mean()) / m.std().replace(0, np.nan)).dropna()
    price_pulse = _mean_z([
        price_yoy_z,
        _safe(alt.load_cpi_electricity_yoy, end=end),
        _safe(erc.load_hub_power_price, end=end),
        _safe(pjm.load_pjm_hub_price, end=end),      # empty until PJM wired
    ])

    green_gen = _mean_z([_safe(ind.load_fuel_yoy, "solar", end=end),
                         _safe(ind.load_fuel_yoy, "wind", end=end)])

    out["tilts"] = {
        "ipp_wholesale":      [price_pulse, _safe(erc.load_as_scarcity, end=end)],
        "gas_midstream":      [_safe(ind.load_fuel_yoy, "gas", end=end)],
        "nuclear_fuel":       [_safe(ind.load_fuel_yoy, "nuclear", end=end)],
        "renewables_storage": [_safe(alt.load_renewables_adds_yoy, end=end), green_gen],
        "grid_equipment":     [_safe(ind.load_backlog_yoy, end=end),
                               _safe(alt.load_transformer_ppi_yoy, end=end)],
        "grid_epc":           [_safe(alt.load_construction_hiring, end=end)],
        "regulated_mega":     [_safe(comp.load_utility_capex_yoy, end=end)],
        "regional_utility":   [_safe(alt.load_dc_price_premium, end=end)],
        "water_cooling":      [_safe(comp.load_water_capex_yoy, end=end)],
    }
    return out


def _capex_monthly(capex: Optional[pd.Series], monthly_index: pd.DatetimeIndex) -> pd.Series:
    if capex is None or capex.empty:
        return pd.Series(0.0, index=monthly_index)
    return capex.resample("ME").last().reindex(monthly_index, method="ffill").fillna(0.0)


def _build_capex_tilt(capex_m: pd.Series, monthly_index: pd.DatetimeIndex) -> pd.DataFrame:
    """Per-subsector CapEx tilt = capex_z * subsector beta, cross-sectional z."""
    subs = U.subsector_names()
    tilt = pd.DataFrame(index=monthly_index, columns=subs, dtype=float)
    for s in subs:
        tilt[s] = capex_m * CAPEX_BETA.get(s, 0.0)
    return _cs_zscore(tilt)


def _build_cycle_regime_tilt(
    regime_monthly: pd.Series, capex_m: pd.Series,
    capex_strong: float, capex_weak: float, monthly_index: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Per-subsector cycle tilt from regime state + CapEx regime."""
    subs = U.subsector_names()
    ai_cycle = set(U.AI_CYCLE_SUBSECTORS)
    high_beta = set(HIGH_BETA_SUBSECTORS)
    defensive = set(U.DEFENSIVE_SUBSECTORS)
    tilt = pd.DataFrame(0.0, index=monthly_index, columns=subs)
    for dt in monthly_index:
        reg = regime_monthly.get(dt, RISK_ON)
        cz = float(capex_m.get(dt, 0.0))
        row = pd.Series(0.0, index=subs)
        if reg in (RISK_OFF, TRANSITION_DOWN):
            for s in defensive:
                row[s] += 1.0
            for s in high_beta:
                row[s] -= 0.5
        elif reg in (RISK_ON, TRANSITION_UP):
            for s in ai_cycle:
                row[s] += 0.5
        if cz >= capex_strong:
            for s in ai_cycle:
                row[s] += 0.3
        elif cz <= capex_weak:
            for s in defensive:
                row[s] += 0.3
        tilt.loc[dt] = row.values
    return _cs_zscore(tilt)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def compute_composite_signals(
    prices: pd.DataFrame,
    macro: pd.DataFrame,
    weights: Optional[Dict[str, float]] = None,
    regime_multipliers: Optional[Dict[str, Dict[str, float]]] = None,
    defensive_tickers: Optional[list] = None,
    defensive_bonus: float = DEFENSIVE_BONUS_RISK_OFF,
    regime_method: str = "rules",
    value_source: Optional[str] = None,       # accepted & ignored (AEUS V1 has no value factor)
    value_cache_dir=None,                      # ignored
    polygon_api_key: Optional[str] = None,     # ignored
    regime_kwargs: Optional[dict] = None,
    signal_kwargs: Optional[dict] = None,
    benchmark_prices: Optional[pd.Series] = None,  # ignored (AEUS uses dedicated benchmarks)
) -> Tuple[pd.DataFrame, pd.Series, Dict[str, pd.DataFrame]]:
    """Compute regime-conditioned AEUS composite z-scores per subsector.

    ``prices`` columns are the 8 subsector baskets (benchmarks already excluded
    by the engine).  Returns (composite [month × subsector], regime_monthly,
    components).
    """
    weights = weights or DEFAULT_WEIGHTS
    _validate_weights(weights)
    regime_multipliers = regime_multipliers or DEFAULT_REGIME_WEIGHT_MULTIPLIERS
    defensive_tickers = defensive_tickers or DEFENSIVE_TICKERS
    signal_kwargs = signal_kwargs or {}
    regime_kwargs = regime_kwargs or {}

    subs = list(prices.columns)

    # --- Factor 1: cross-sectional momentum (monthly z) ---
    mom = compute_all_momentum(
        prices,
        cs_lookback=signal_kwargs.get("cs_lookback", 12),
        cs_skip=signal_kwargs.get("cs_skip", 1),
        cs_zscore_window=signal_kwargs.get("cs_zscore_window", 36),
        ts_lookback=signal_kwargs.get("ts_lookback", 12),
        ts_skip=signal_kwargs.get("ts_skip", 1),
        ts_crash_mult=signal_kwargs.get("ts_crash_mult", 0.0),
        accel_enabled=False,
    )
    cs_mom = mom["cs_mom"]
    monthly_index = cs_mom.index

    # --- Regime (daily → monthly) ---
    regime_daily = compute_regime(macro, method=regime_method, **regime_kwargs)
    regime_monthly = regime_to_monthly(regime_daily).reindex(monthly_index, method="ffill").fillna(RISK_ON)

    # --- Aux signals (CapEx + external PIT) ---
    aux = _load_aux_signals(prices, signal_kwargs)
    capex_m = _capex_monthly(aux["capex"], monthly_index)

    # --- Factor 2: supply-chain propagation (CS z) ---
    # V2: config-built graph (calibrated lags + logic_cpu inbound + real PMI) and
    # optional exponential lag-decay.  Absent/v1 config → hardcoded V1 graph, λ=0,
    # PMI source neutral (0) → byte-identical to the pre-V2 behaviour.
    sc_cfg = signal_kwargs.get("supply_chain", {}) or {}
    sc_graph = load_graph_from_config(sc_cfg)
    sc_lag_decay = float(sc_cfg.get("lag_decay", 0.0) or 0.0)
    sc_pmi = aux["pmi"] if str(sc_cfg.get("graph_version", "v1")).lower() == "v2" else None
    # rate_env 节点:10Y 收益率取负 YoY(利率涨 = 债券代理承压;§3.1)
    rate_env = None
    if macro is not None and "dgs10" in getattr(macro, "columns", []):
        r10 = macro["dgs10"].dropna()
        if len(r10) > 300:
            rate_env = -(r10 - r10.shift(252)).dropna()
    supply_chain = compute_supply_chain_scores(
        prices, aux["capex"], macro.get("vix") if macro is not None else None,
        monthly_index,
        power_demand=aux["demand"],
        power_price=aux["gas_price"],
        rate_env=rate_env,
        pmi_series=sc_pmi,
        external_tilts=aux["tilts"],
        use_external_macro=signal_kwargs.get("use_external_macro", True),
        graph=sc_graph, lag_decay=sc_lag_decay,
    ).reindex(index=monthly_index, columns=subs).fillna(0.0)

    # --- Factor 3: CapEx pulse tilt (CS z) ---
    capex_tilt = _build_capex_tilt(capex_m, monthly_index)[subs]

    # --- Factor 4: cycle/regime tilt (CS z) ---
    capex_strong = float(regime_kwargs.get("capex_strong_zscore", 1.0))
    capex_weak = float(regime_kwargs.get("capex_weak_zscore", -1.0))
    cycle_tilt = _build_cycle_regime_tilt(regime_monthly, capex_m, capex_strong, capex_weak, monthly_index)[subs]

    # --- Blend per month with regime-adjusted weights ---
    w0 = {k: weights.get(k, DEFAULT_WEIGHTS[k]) for k in DEFAULT_WEIGHTS}
    composite = pd.DataFrame(index=monthly_index, columns=subs, dtype=float)
    for dt in monthly_index:
        reg = regime_monthly.get(dt, RISK_ON)
        rm = regime_multipliers.get(reg, regime_multipliers.get(RISK_ON, {}))
        wadj = {k: w0[k] * rm.get(k, 1.0) for k in w0}
        tot_raw, tot_adj = sum(w0.values()), sum(wadj.values())
        if tot_adj > 0:
            scale = tot_raw / tot_adj
            wadj = {k: v * scale for k, v in wadj.items()}

        score = pd.Series(0.0, index=subs)
        score += cs_mom.loc[dt].reindex(subs).fillna(0.0) * wadj["cs_momentum"]
        score += supply_chain.loc[dt].reindex(subs).fillna(0.0) * wadj["supply_chain"]
        score += capex_tilt.loc[dt].reindex(subs).fillna(0.0) * wadj["capex_pulse"]
        score += cycle_tilt.loc[dt].reindex(subs).fillna(0.0) * wadj["cycle_regime"]

        if reg == RISK_OFF:
            for d in defensive_tickers:
                if d in score.index:
                    score[d] += defensive_bonus

        valid = score.dropna()
        if len(valid) >= 2 and valid.std() > 0:
            score = (score - valid.mean()) / valid.std()
        composite.loc[dt] = score.values

    composite = composite.astype(float)
    components = {
        "cs_momentum": cs_mom,
        "cs_mom": cs_mom,                      # alias for any AEUS-era reader
        "supply_chain": supply_chain,
        "capex_pulse": capex_tilt,
        "cycle_regime": cycle_tilt,
        "capex_monthly": capex_m,
        "regime_daily": regime_daily,
    }
    logger.info("AEUS composite: %d months × %d subsectors (last regime=%s)",
                composite.dropna(how="all").shape[0], composite.shape[1],
                regime_monthly.iloc[-1] if not regime_monthly.empty else "?")
    return composite, regime_monthly, components


# ---------------------------------------------------------------------------
# Live snapshot
# ---------------------------------------------------------------------------

def get_current_signals(config: Optional[dict] = None, as_of: Optional[str] = None) -> dict:
    """Return the latest-month AEUS composite + per-factor breakdown (for live use)."""
    try:
        from ..data.loader import load_all, load_config
    except Exception:  # pragma: no cover
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from electric_utilities_strategy.data.loader import load_all, load_config  # type: ignore

    cfg = config or load_config()
    prices, macro = load_all(config=cfg)
    uni = cfg.get("universe", {})
    etfs = uni.get("etfs", U.subsector_names())
    etf_prices = prices[[c for c in etfs if c in prices.columns]]
    sig = cfg.get("signals", {})
    regime_kwargs = {k: v for k, v in sig.get("regime", {}).items()
                     if k not in ("method", "regime_weights", "defensive_sectors", "defensive_bonus_risk_off")}
    composite, regime_monthly, components = compute_composite_signals(
        etf_prices, macro,
        weights=sig.get("weights"),
        regime_multipliers=sig.get("regime", {}).get("regime_weights"),
        defensive_tickers=sig.get("regime", {}).get("defensive_sectors"),
        defensive_bonus=sig.get("regime", {}).get("defensive_bonus_risk_off", DEFENSIVE_BONUS_RISK_OFF),
        regime_method=sig.get("regime", {}).get("method", "rules"),
        regime_kwargs=regime_kwargs,
        signal_kwargs={"signal_version": sig.get("signal_version", "v1"),
                       "use_external_macro": sig.get("supply_chain", {}).get("use_external_macro", True),
                       "supply_chain": sig.get("supply_chain", {})},
    )
    last = composite.dropna(how="all").index[-1]
    return {
        "as_of": str(last.date()),
        "regime": regime_monthly.get(last, RISK_ON),
        "composite": composite.loc[last].sort_values(ascending=False).to_dict(),
        "supply_chain": components["supply_chain"].loc[last].to_dict(),
        "cs_momentum": components["cs_momentum"].loc[last].to_dict(),
        "capex_pulse": components["capex_pulse"].loc[last].to_dict(),
        "cycle_regime": components["cycle_regime"].loc[last].to_dict(),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import json
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    snap = get_current_signals()
    print(json.dumps(snap, indent=2, default=lambda x: round(float(x), 3)))
