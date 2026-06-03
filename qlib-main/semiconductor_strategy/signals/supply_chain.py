"""
supply_chain.py — Semiconductor supply-chain propagation signal (AISS core alpha)
================================================================================
This is the defining innovation of AISS and has **no equivalent in AISS**.

Idea
----
Semiconductor subsectors lead/lag each other along the supply chain (equipment →
foundry → AI/GPU → custom-ASIC / memory, etc.).  We encode that as a directed
knowledge graph of edges ``(source → target, weight, lag_months)`` and score each
subsector by the *lagged* strength of its upstream drivers:

    propagation_score(S, t) = Σ_{X→S}  edge_weight(X→S) · signal(X, t − lag(X→S))

A "signal" for a subsector source is its own 12-1 month basket momentum; for the
external trigger nodes it is a price/macro proxy:
    * ai_capex_proxy        → hyperscaler CapEx pulse z-score (MSFT/GOOGL/META/AMZN)
    * consumer_demand_proxy → RF/edge basket momentum (consumer-electronics geared)
    * pmi_proxy             → (V1: neutral; PMI not in free FRED — contributes 0)

V1 uses **stock-price proxies for every edge** (always available across the full
backtest).  When the optional PIT external datasets are present and visible as of
``t`` they ADD a small confirmation tilt (never look-ahead — they are already
PIT-shifted by the data layer):
    * foundry      += z(TSMC monthly-revenue YoY)
    * equipment    += z(ASML net bookings)            (equipment feedback edge)
    * memory_hbm   += z(DRAM proxy) + MU-DIO signal
Missing external data simply contributes 0 → graceful fallback to the price proxy.

Output: a (month-end × subsector) DataFrame of cross-sectionally z-scored
propagation scores, consumed by ``composite.py`` as the 35%-weight core factor.

V2 (future): YAML-configurable graph, exponential lag decay, GNN-learned weights.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

try:
    from semiconductor_strategy.data import universe as U
except Exception:  # pragma: no cover
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from semiconductor_strategy.data import universe as U  # type: ignore

logger = logging.getLogger(__name__)

# Special (non-subsector) source nodes
NODE_AI_CAPEX = "ai_capex_proxy"
NODE_CONSUMER = "consumer_demand_proxy"
NODE_PMI = "pmi_proxy"
SPECIAL_NODES = {NODE_AI_CAPEX, NODE_CONSUMER, NODE_PMI}

# Directed propagation graph — (source, target): (weight, lag_months, proxy_desc)
# Mirrors the plan's edge table (§2.2).  Negative weight = competitive/substitution.
SUPPLY_CHAIN_GRAPH: Dict[Tuple[str, str], Tuple[float, int, str]] = {
    (NODE_AI_CAPEX, "ai_gpu"):        (1.00, 0,  "4 hyperscalers 3M momentum"),
    ("ai_gpu",      "custom_asic"):   (0.60, 2,  "NVDA relative strength"),
    ("ai_gpu",      "memory_hbm"):    (0.80, 4,  "NVDA shipments -> MU"),
    ("ai_gpu",      "foundry"):       (0.80, 4,  "TSMC monthly revenue YoY"),
    ("foundry",     "equipment"):     (0.90, 4,  "KLAC/LRCX strength (V1 proxy)"),
    ("memory_hbm",  "equipment"):     (0.70, 4,  "MU inventory days change"),
    ("equipment",   "foundry"):       (0.50, 12, "ASML net bookings (feedback)"),
    ("foundry",     "custom_asic"):   (0.60, 0,  "CoWoS tightness"),
    ("logic_cpu",   "ai_gpu"):        (-0.20, 0, "AMD share (negative competition)"),
    (NODE_CONSUMER, "rf_edge"):       (0.80, 0,  "Apple/IDC PC demand"),
    (NODE_PMI,      "analog_defense"): (0.50, 4, "Manufacturing PMI trend"),
}

# Sub-weight on external-data confirmation tilt (added before cross-sectional z).
_EXTERNAL_TILT_WEIGHT = 0.30
# Rolling window (months) for z-scoring momentum / external series across time.
_TS_Z_WINDOW = 36
_TS_Z_MIN = 12
# Forward months summed under exponential lag-decay (λ>0); 6 ≈ negligible tail.
_DECAY_WINDOW = 6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _monthly_momentum(subsector_prices: pd.DataFrame,
                      lookback: int = 12, skip: int = 1) -> pd.DataFrame:
    """Per-subsector 12-1 month basket momentum on a month-end index."""
    monthly = subsector_prices.resample("ME").last()
    # return from t-(lookback) to t-(skip): monthly.shift(skip)/monthly.shift(lookback) - 1
    mom = monthly.shift(skip) / monthly.shift(lookback) - 1.0
    return mom


def _to_monthly(series: pd.Series, monthly_index: pd.DatetimeIndex) -> pd.Series:
    """Resample a daily PIT series to month-end and align to ``monthly_index``."""
    if series is None or series.empty:
        return pd.Series(np.nan, index=monthly_index)
    m = series.resample("ME").last()
    return m.reindex(monthly_index, method="ffill")


def _ts_zscore(s: pd.Series, window: int = _TS_Z_WINDOW, min_p: int = _TS_Z_MIN) -> pd.Series:
    mu = s.rolling(window, min_periods=min_p).mean()
    sd = s.rolling(window, min_periods=min_p).std().replace(0, np.nan)
    return (s - mu) / sd


def _ts_zscore_df(df: pd.DataFrame) -> pd.DataFrame:
    return df.apply(_ts_zscore, axis=0)


def _lagged_with_decay(s: pd.Series, lag: int, lam: float,
                       window: int = _DECAY_WINDOW) -> pd.Series:
    """Geometric-decay weighted lag (Hong-Stein gradual information diffusion).

    Returns Σ_{k=0..window} d^k · s.shift(lag+k) / Σ_{k} d^k  with d = e^{-λ}.
    At λ→0 the weights collapse to k=0 (≡ a hard ``s.shift(lag)``), so callers
    must only invoke this when ``lam > 0`` (the caller keeps the exact V1 path
    for λ=0 to guarantee byte-identical regression).
    """
    d = float(np.exp(-lam))
    num = None
    denom = 0.0
    for k in range(window + 1):
        wk = d ** k
        term = s.shift(lag + k) * wk
        num = term if num is None else num.add(term, fill_value=0.0)
        denom += wk
    return num / denom if denom > 0 else s.shift(lag)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def compute_supply_chain_scores(
    subsector_prices: pd.DataFrame,
    capex_pulse: Optional[pd.Series],
    vix: Optional[pd.Series],
    monthly_index: pd.DatetimeIndex,
    tsmc_yoy: Optional[pd.Series] = None,
    asml_orders: Optional[pd.Series] = None,
    dram_proxy: Optional[pd.Series] = None,
    mu_dio: Optional[pd.Series] = None,
    pmi_series: Optional[pd.Series] = None,
    use_external_macro: bool = True,
    graph: Optional[dict] = None,
    lag_decay: float = 0.0,
) -> pd.DataFrame:
    """Compute the (month × subsector) supply-chain propagation z-scores.

    Parameters
    ----------
    subsector_prices : DataFrame (daily, Date × 8 subsectors)
    capex_pulse : daily AI-CapEx pulse z-score (company_signals)
    vix : daily VIX (unused in V1 propagation; regime tilt handled in composite)
    monthly_index : month-end index to produce scores on (== cs_mom.index)
    tsmc_yoy / asml_orders / dram_proxy / mu_dio : optional daily PIT series
    pmi_series : optional daily PIT manufacturing-trend proxy (IPMAN YoY); drives
        the ``pmi_proxy`` source node.  None/empty → that source contributes 0
        (V1 behaviour).  V2 feeds the real FRED IPMAN series.
    use_external_macro : blend external confirmation tilts when available
    graph : optional override of SUPPLY_CHAIN_GRAPH (V2 passes a config-built dict)
    lag_decay : λ ≥ 0.  0 = hard lag (V1, byte-identical).  >0 applies a geometric
        decay e^{-λk} over a forward window from each edge's lag (Hong-Stein gradual
        information diffusion); half-life ≈ ln2/λ months.

    Returns
    -------
    DataFrame (month_end × subsector) cross-sectionally z-scored.
    """
    graph = graph or SUPPLY_CHAIN_GRAPH
    subs = U.subsector_names()

    # --- per-subsector momentum (time-series z-scored so edges combine on a comparable scale)
    mom = _monthly_momentum(subsector_prices)[subs].reindex(monthly_index)
    mom_z = _ts_zscore_df(mom).fillna(0.0)

    # --- special source-node monthly series (time-series z-scored) ---
    capex_m = _ts_zscore(_to_monthly(capex_pulse, monthly_index)).fillna(0.0) \
        if capex_pulse is not None else pd.Series(0.0, index=monthly_index)
    # CapEx pulse is already a z-score; re-z over months keeps it comparable, harmless.
    consumer_m = mom_z["rf_edge"] if "rf_edge" in mom_z else pd.Series(0.0, index=monthly_index)
    # PMI proxy: real FRED IPMAN-YoY when supplied (V2), else neutral 0 (V1).
    pmi_m = _ts_zscore(_to_monthly(pmi_series, monthly_index)).fillna(0.0) \
        if pmi_series is not None and not pmi_series.empty \
        else pd.Series(0.0, index=monthly_index)

    def _source_series(node: str) -> pd.Series:
        if node == NODE_AI_CAPEX:
            return capex_m
        if node == NODE_CONSUMER:
            return consumer_m
        if node == NODE_PMI:
            return pmi_m
        # subsector source
        return mom_z[node] if node in mom_z else pd.Series(0.0, index=monthly_index)

    # --- propagate edges ---
    score = pd.DataFrame(0.0, index=monthly_index, columns=subs)
    for (src, tgt), (w, lag, _desc) in graph.items():
        if tgt not in score.columns:
            continue
        base = _source_series(src)
        if lag_decay and lag_decay > 0:
            src_series = _lagged_with_decay(base, lag, lag_decay).reindex(monthly_index).fillna(0.0)
        else:
            src_series = base.shift(lag).reindex(monthly_index).fillna(0.0)
        score[tgt] = score[tgt] + w * src_series

    # --- external confirmation tilts (PIT; already lagged by the data layer) ---
    if use_external_macro:
        if tsmc_yoy is not None and not tsmc_yoy.empty:
            t = _ts_zscore(_to_monthly(tsmc_yoy, monthly_index)).fillna(0.0)
            score["foundry"] = score["foundry"] + _EXTERNAL_TILT_WEIGHT * t
        if asml_orders is not None and not asml_orders.empty:
            a = _ts_zscore(_to_monthly(asml_orders, monthly_index)).fillna(0.0)
            score["equipment"] = score["equipment"] + _EXTERNAL_TILT_WEIGHT * a
        if dram_proxy is not None and not dram_proxy.empty:
            d = _to_monthly(dram_proxy, monthly_index).fillna(0.0)  # already z-scored daily
            score["memory_hbm"] = score["memory_hbm"] + _EXTERNAL_TILT_WEIGHT * d
        if mu_dio is not None and not mu_dio.empty:
            md = _to_monthly(mu_dio, monthly_index).fillna(0.0)     # +1/0/-1 PIT signal
            score["memory_hbm"] = score["memory_hbm"] + _EXTERNAL_TILT_WEIGHT * md

    # --- cross-sectional z-score per month ---
    out = _cross_sectional_zscore(score)
    logger.info("Supply-chain scores: %d months × %d subsectors",
                out.dropna(how="all").shape[0], out.shape[1])
    return out


def _cross_sectional_zscore(df: pd.DataFrame) -> pd.DataFrame:
    mu = df.mean(axis=1)
    sd = df.std(axis=1).replace(0, np.nan)
    z = df.sub(mu, axis=0).div(sd, axis=0)
    return z.fillna(0.0)


def load_graph_from_config(sc_cfg: Optional[dict]) -> dict:
    """Build the propagation graph dict from a ``signals.supply_chain`` config block.

    Returns the hardcoded V1 ``SUPPLY_CHAIN_GRAPH`` unless ``graph_version == "v2"``
    AND a non-empty ``graph_config.edges`` list is present — so any caller that
    doesn't pass a V2 config (or sets v1) gets byte-identical V1 behaviour.

    V2 edge schema (config.yaml):
        graph_config:
          edges:
            - {source: ai_capex_proxy, target: ai_gpu, weight: 1.0, lag_months: 0, desc: "..."}
    """
    if not sc_cfg or str(sc_cfg.get("graph_version", "v1")).lower() != "v2":
        return SUPPLY_CHAIN_GRAPH
    gc = sc_cfg.get("graph_config") or {}
    edges = gc.get("edges") or []
    if not edges:
        logger.warning("supply_chain.graph_version=v2 but no graph_config.edges; using V1 graph")
        return SUPPLY_CHAIN_GRAPH
    out: Dict[Tuple[str, str], Tuple[float, int, str]] = {}
    for e in edges:
        try:
            src, tgt = e["source"], e["target"]
        except (KeyError, TypeError):
            logger.warning("skipping malformed supply-chain edge: %r", e)
            continue
        out[(src, tgt)] = (float(e.get("weight", 0.0)),
                           int(e.get("lag_months", 0)),
                           str(e.get("desc", "")))
    return out or SUPPLY_CHAIN_GRAPH


def build_propagation_matrix(graph: Optional[dict] = None) -> pd.DataFrame:
    """Static (source × target) edge-weight matrix for inspection / debugging."""
    graph = graph or SUPPLY_CHAIN_GRAPH
    sources = sorted({s for (s, _t) in graph})
    targets = sorted({t for (_s, t) in graph})
    m = pd.DataFrame(0.0, index=sources, columns=targets)
    for (s, t), (w, _lag, _d) in graph.items():
        m.loc[s, t] = w
    return m


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from semiconductor_strategy.data import loader as L
    from semiconductor_strategy.data import company_signals as comp
    from semiconductor_strategy.data import industry_signals as ind

    px = L.load_subsector_prices(include_benchmark=False)
    monthly_idx = px.resample("ME").last().index
    capex = comp.load_capex_pulse()
    sc = compute_supply_chain_scores(
        px, capex, None, monthly_idx,
        tsmc_yoy=ind.load_tsmc_monthly(), asml_orders=ind.load_asml_orders(),
        dram_proxy=ind.load_dram_proxy(), mu_dio=comp.load_mu_dio(),
    )
    print("Propagation matrix (source × target):")
    print(build_propagation_matrix().round(2))
    print("\nLatest supply-chain z-scores:")
    print(sc.dropna(how="all").iloc[-1].round(3).sort_values(ascending=False))
