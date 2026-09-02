"""
supply_chain.py — AI-power supply-chain propagation signal (AEUS core alpha)
============================================================================
The defining innovation inherited from AISS, retargeted at the electricity
value chain (AEUS_PLAN §3).

Idea
----
Power subsectors lead/lag each other along the AI-power chain (AI capex → PPA
signings → transformer orders → grid construction → rate-base growth).  We
encode that as a directed knowledge graph of edges ``(source → target, weight,
lag_months)`` and score each subsector by the *lagged* strength of its
upstream drivers:

    propagation_score(S, t) = Σ_{X→S}  edge_weight(X→S) · signal(X, t − lag(X→S))

A "signal" for a subsector source is its own 12-1 month basket momentum; the
five macro trigger nodes are:
    * ai_capex_proxy         → hyperscaler CapEx pulse z (shared with AISS)
    * power_demand_proxy     → weather-adjusted US demand YoY (EIA A1×A5)
    * power_price_proxy      → Henry Hub / hub power price z
    * rate_env_proxy         → 10Y yield NEGATED (utilities bond-proxy channel)
    * industrial_demand_proxy→ FRED IPUTIL YoY

Price proxies cover every edge across the full backtest; PIT external datasets
(EIA generation/capacity, XBRL backlog/capex, transformer PPI, ERCOT prices…)
ADD capped confirmation tilts via ``external_tilts`` (≤2 per subsector × 0.30
— the AISS memory_hbm dual-tilt precedent is the ceiling).  Missing data
contributes 0 → graceful fallback to pure price proxies.

Output: (month-end × subsector) cross-sectionally z-scored DataFrame,
consumed by ``composite.py`` as the 35%-weight core factor.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

try:
    from electric_utilities_strategy.data import universe as U
except Exception:  # pragma: no cover
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from electric_utilities_strategy.data import universe as U  # type: ignore

logger = logging.getLogger(__name__)

# Special (non-subsector) source nodes (AEUS_PLAN §3.1)
NODE_AI_CAPEX = "ai_capex_proxy"           # shared upstream driver with AISS
NODE_DEMAND = "power_demand_proxy"         # weather-adjusted US demand (EIA A1×A5)
NODE_POWER_PRICE = "power_price_proxy"     # Henry Hub / hub power price
NODE_RATE_ENV = "rate_env_proxy"           # 10Y yield, NEGATED (bond-proxy channel)
NODE_PMI = "industrial_demand_proxy"       # FRED IPUTIL YoY
SPECIAL_NODES = {NODE_AI_CAPEX, NODE_DEMAND, NODE_POWER_PRICE, NODE_RATE_ENV, NODE_PMI}

# Directed propagation graph — (source, target): (weight, lag_months, proxy_desc)
# The AEUS economic PRIOR (AEUS_PLAN §3.2), mirrored 1:1 in config graph_config
# (V2); lags get IC-calibrated by graph_calibration on factor-residual returns.
# Negative weight = valuation/substitution channel.
SUPPLY_CHAIN_GRAPH: Dict[Tuple[str, str], Tuple[float, int, str]] = {
    # AI capex propagates down the power chain (fast → slow)
    (NODE_AI_CAPEX, "ipp_wholesale"):      (1.00, 0,  "PPA signings monetize fastest"),
    (NODE_AI_CAPEX, "dc_power_cooling"):   (0.90, 0,  "VRT power/cooling orders track DC builds"),
    (NODE_AI_CAPEX, "grid_equipment"):     (0.80, 3,  "transformer/switchgear orders lag"),
    (NODE_AI_CAPEX, "grid_epc"):           (0.70, 5,  "interconnect/substation construction"),
    (NODE_AI_CAPEX, "regulated_mega"):     (0.60, 9,  "load growth -> rate base (slowest)"),
    (NODE_AI_CAPEX, "nuclear_fuel"):       (0.50, 6,  "nuclear PPA / SMR orders"),
    (NODE_AI_CAPEX, "renewables_storage"): (0.50, 3,  "green-compute pledges -> storage/solar"),
    (NODE_AI_CAPEX, "gas_midstream"):      (0.50, 4,  "DC-driven pipeline expansions"),
    (NODE_AI_CAPEX, "water_cooling"):      (0.30, 6,  "DC cooling-water demand (slow)"),
    # intra-chain propagation
    ("ipp_wholesale",  "nuclear_fuel"):    (0.50, 3,  "nuclear premium -> fuel chain"),
    ("grid_equipment", "grid_epc"):        (0.60, 2,  "equipment delivery precedes construction"),
    ("regulated_mega", "grid_equipment"):  (0.50, 4,  "utility capex cycle -> equipment orders"),
    ("gas_midstream",  "ipp_wholesale"):   (0.40, 1,  "fuel availability -> gas-fleet output"),
    # macro nodes
    (NODE_DEMAND, "ipp_wholesale"):        (0.80, 0,  "structural demand -> merchant output"),
    (NODE_DEMAND, "regulated_mega"):       (0.50, 1,  "demand growth -> regulated volumes"),
    (NODE_DEMAND, "regional_utility"):     (0.50, 1,  "demand growth -> regional volumes"),
    (NODE_POWER_PRICE, "ipp_wholesale"):   (0.70, 0,  "power/gas price -> merchant margins"),
    (NODE_POWER_PRICE, "gas_midstream"):   (0.70, 0,  "gas price -> spreads & volumes"),
    (NODE_RATE_ENV, "regulated_mega"):     (0.60, 0,  "rates (negated) -> bond proxies"),
    (NODE_RATE_ENV, "regional_utility"):   (0.70, 0,  "rates (negated), higher small-cap beta"),
    (NODE_RATE_ENV, "water_cooling"):      (0.70, 0,  "rates (negated), most rate-sensitive"),
    (NODE_RATE_ENV, "renewables_storage"): (0.50, 2,  "financing cost -> project IRRs"),
    (NODE_PMI, "regional_utility"):        (0.50, 2,  "industrial load -> retail volumes"),
}

# Sub-weight on external-data confirmation tilt (added before cross-sectional z).
_EXTERNAL_TILT_WEIGHT = 0.30
# §4.2 纪律:单板块确认 tilt 封顶(AISS memory_hbm 双确认先例 = 上限)
_MAX_TILTS_PER_SUBSECTOR = 2
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


def _z_splice(primary: Optional[pd.Series], fallback: Optional[pd.Series],
              monthly_index: pd.DatetimeIndex) -> Optional[pd.Series]:
    """z 空间序列拼接 —— 承自 AISS 的 _asml_tilt 机制,泛化保留。

    两个口径不同的前瞻序列(如 A 停更后由 B 接续)先**各自**做滚动 z 再按优先级
    取,而不是先拼后 z —— 后者会把口径切换本身当成一次几个标准差的"信号跳变",
    纯属人造。primary 的 z 为 NaN(未攒够 _TS_Z_MIN 个月)时回落到 fallback,
    历史段与切换前逐位相同。两条都没有 → None,调用方跳过该 tilt(graceful)。

    AEUS 当前用途:序列换代时的通用工具(如未来某 backlog 成员停 tag、由新序列
    接续);机制在此保活,不因 ASML 场景消失而丢弃。
    """
    z = []
    for s in (fallback, primary):
        z.append(_ts_zscore(_to_monthly(s, monthly_index))
                 if s is not None and not s.empty else None)
    z_fb, z_pri = z
    if z_pri is None and z_fb is None:
        return None
    if z_pri is None:
        return z_fb.fillna(0.0)
    if z_fb is None:
        return z_pri.fillna(0.0)
    return z_pri.combine_first(z_fb).fillna(0.0)


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
    power_demand: Optional[pd.Series] = None,
    power_price: Optional[pd.Series] = None,
    rate_env: Optional[pd.Series] = None,
    pmi_series: Optional[pd.Series] = None,
    external_tilts: Optional[dict] = None,
    use_external_macro: bool = True,
    graph: Optional[dict] = None,
    lag_decay: float = 0.0,
) -> pd.DataFrame:
    """Compute the (month × subsector) supply-chain propagation z-scores.

    Parameters
    ----------
    subsector_prices : DataFrame (daily, Date × 10 subsectors)
    capex_pulse : daily AI-CapEx pulse z-score (company_signals; shared w/ AISS)
    vix : daily VIX (unused in propagation; regime tilt handled in composite)
    monthly_index : month-end index to produce scores on (== cs_mom.index)
    power_demand / power_price / rate_env / pmi_series : optional daily PIT
        series driving the four macro source nodes (AEUS_PLAN §3.1).
        ``rate_env`` must arrive ALREADY NEGATED (higher = easier financing).
        None/empty → that node contributes 0 (graceful, AISS convention).
    external_tilts : optional {subsector: [daily PIT series, ...]} confirmation
        tilts (AEUS_PLAN §4.2 通路②) — the generalisation of the AISS
        TSMC/ASML/DRAM/MU-DIO hardcoded tilts.  Each series is monthly-resampled,
        z-scored unless it already is one, and added at _EXTERNAL_TILT_WEIGHT.
        DISCIPLINE: at most _MAX_TILTS_PER_SUBSECTOR series per subsector are
        applied (excess logged + dropped — the memory_hbm dual-tilt precedent
        is the ceiling, never more).
    use_external_macro : blend external confirmation tilts when available
    graph : optional override of SUPPLY_CHAIN_GRAPH (V2 passes a config-built dict)
    lag_decay : λ ≥ 0.  0 = hard lag (byte-identical prior).  >0 applies e^{-λk}
        geometric decay over a forward window (Hong-Stein gradual diffusion).

    Returns
    -------
    DataFrame (month_end × subsector) cross-sectionally z-scored.
    """
    graph = graph or SUPPLY_CHAIN_GRAPH
    subs = U.subsector_names()

    # --- per-subsector momentum (time-series z-scored so edges combine on a comparable scale)
    mom = _monthly_momentum(subsector_prices)[subs].reindex(monthly_index)
    mom_z = _ts_zscore_df(mom).fillna(0.0)

    # --- macro source-node monthly series (time-series z-scored) ---
    def _node_m(s: Optional[pd.Series]) -> pd.Series:
        if s is None or s.empty:
            return pd.Series(0.0, index=monthly_index)
        return _ts_zscore(_to_monthly(s, monthly_index)).fillna(0.0)

    capex_m = _node_m(capex_pulse)
    demand_m = _node_m(power_demand)
    price_m = _node_m(power_price)
    rate_m = _node_m(rate_env)
    pmi_m = _node_m(pmi_series)

    _node_map = {NODE_AI_CAPEX: capex_m, NODE_DEMAND: demand_m,
                 NODE_POWER_PRICE: price_m, NODE_RATE_ENV: rate_m,
                 NODE_PMI: pmi_m}

    def _source_series(node: str) -> pd.Series:
        if node in _node_map:
            return _node_map[node]
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
    if use_external_macro and external_tilts:
        for sub, series_list in external_tilts.items():
            if sub not in score.columns or not series_list:
                continue
            applied = 0
            for s in series_list:
                if s is None or getattr(s, "empty", True):
                    continue
                if applied >= _MAX_TILTS_PER_SUBSECTOR:
                    logger.warning("supply_chain: %s has >%d tilts — extras dropped "
                                   "(§4.2 cap discipline)", sub, _MAX_TILTS_PER_SUBSECTOR)
                    break
                m = _to_monthly(s, monthly_index)
                # 已是 z/量纲有界的序列(|值| 天然小)直接用;原始 YoY% 先 z 化
                t = m.fillna(0.0) if float(m.abs().max() or 0) <= 6.0 \
                    else _ts_zscore(m).fillna(0.0)
                score[sub] = score[sub] + _EXTERNAL_TILT_WEIGHT * t
                applied += 1

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


def shortage_sensitivity(graph: Optional[dict] = None, floor: float = 0.5) -> Dict[str, float]:
    """通路③ 的图谱敏感度:每个板块对"缺电"的暴露 = 来自 power_demand_proxy 与
    power_price_proxy 的入边权重之和,归一到均值 1;没有这两类入边的板块给地板值
    (缺电时整条链都受益,只是幅度不同)。graph=None → 用硬编码 V1 图。"""
    g = graph or SUPPLY_CHAIN_GRAPH
    subs = sorted({tgt for (_, tgt) in g.keys()})
    raw = {s: 0.0 for s in subs}
    for (src, tgt), (w, _lag, _d) in g.items():
        if src in (NODE_DEMAND, NODE_POWER_PRICE) and tgt in raw:
            raw[tgt] += abs(float(w))
    pos = [v for v in raw.values() if v > 0]
    if not pos:
        return {s: 1.0 for s in subs}
    mean_pos = sum(pos) / len(pos)
    sens = {s: (v / mean_pos if v > 0 else floor) for s, v in raw.items()}
    m = sum(sens.values()) / len(sens)
    return {s: v / m for s, v in sens.items()}


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
    from electric_utilities_strategy.data import loader as L
    from electric_utilities_strategy.data import company_signals as comp
    from electric_utilities_strategy.data import industry_signals as ind

    px = L.load_subsector_prices(include_benchmark=False)
    monthly_idx = px.resample("ME").last().index
    capex = comp.load_capex_pulse()
    sc = compute_supply_chain_scores(
        px, capex, None, monthly_idx,
        tsmc_yoy=ind.load_tsmc_monthly(), asml_orders=ind.load_asml_orders(),
        asml_guidance=ind.load_asml_guidance(),
        dram_proxy=ind.load_dram_proxy(), mu_dio=comp.load_mu_dio(),
    )
    print("Propagation matrix (source × target):")
    print(build_propagation_matrix().round(2))
    print("\nLatest supply-chain z-scores:")
    print(sc.dropna(how="all").iloc[-1].round(3).sort_values(ascending=False))
