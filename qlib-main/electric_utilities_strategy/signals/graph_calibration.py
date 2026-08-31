"""
graph_calibration.py — empirical lead-lag calibration of the AEUS supply-chain graph
=====================================================================================
The V1 supply-chain graph (``supply_chain.SUPPLY_CHAIN_GRAPH``) carried hand-set,
round-number transmission lags (0/2/4/12 months).  A raw monthly-return lead-lag
test showed every edge peaking at lag 0 — but that is contaminated by the common
electric utilities beta (all subsectors co-move).  This module calibrates each edge's
lag on **factor-residual returns** (cross-sectionally demeaned, removing the
common semi factor), which is the correct way to isolate genuine cross-subsector
lead-lag (Cohen-Frazzini 2008; Menzly-Ozbas 2010; Shahrur 2010 use factor-adjusted
returns).

For each directed edge (source → target):
    IC(L) = corr( source_signal.shift(L) , target_residual_return ),  L = 0..12
    best_lag = argmax_L IC   (positive edges)   /   argmin_L IC   (negative edges)

Source signal:
    * subsector node            → residual monthly return of that subsector
    * ai_capex_proxy            → hyperscaler CapEx pulse (monthly)
    * consumer_demand_proxy     → residual monthly return of rf_edge
    * pmi_proxy                 → IPMAN YoY (monthly)

Outputs (CLI):
    backtest_results/graph_calibration_report.json   — per-edge assumed vs empirical
        lag, IC@best, IC@assumed, full IC profile, n_obs, kept-or-dropped flag, plus
        a ready-to-paste ``v2_graph_edges`` list (calibrated lags) and Katz-centrality
        reference.  A human-readable table + YAML block are also printed.

Per the user's decision, the empirically-best lag is adopted directly (no theory
band constraint, no OOS gate); the report still prints the literature bands so any
edge whose calibrated lag looks unreasonable can be vetoed by hand.

Usage:
    conda run -n qlib_run python -m electric_utilities_strategy.signals.graph_calibration
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

_QLIB_DIR = Path(__file__).resolve().parents[2]
if str(_QLIB_DIR) not in sys.path:
    sys.path.insert(0, str(_QLIB_DIR))

from electric_utilities_strategy.signals.supply_chain import (  # noqa: E402
    SUPPLY_CHAIN_GRAPH, NODE_AI_CAPEX, NODE_DEMAND, NODE_POWER_PRICE, NODE_RATE_ENV, NODE_PMI, SPECIAL_NODES,
)

logger = logging.getLogger(__name__)

MAX_LAG = 12
MIN_OBS = 24            # need ≥24 overlapping months for a meaningful IC
KEEP_IC_THRESHOLD = 0.05  # candidate edge retained only if |IC@best| ≥ this

# Candidate NEW edges probed each calibration run (AEUS_PLAN §3.3) — kept only
# when |IC@best_lag| >= KEEP_IC_THRESHOLD on factor-residual returns.
CANDIDATE_EDGES: List[Tuple[str, str, float, str]] = [
    (NODE_POWER_PRICE, "regional_utility", 0.40, "power price -> regional retail margins"),
    ("grid_epc",       "regulated_mega",   0.40, "construction completion -> rate base"),
    (NODE_PMI,         "ipp_wholesale",    0.40, "industrial load -> merchant demand"),
    (NODE_DEMAND,      "gas_midstream",    0.40, "demand -> gas burn -> throughput"),
    (NODE_AI_CAPEX,    "regional_utility", 0.30, "secondary-market DC siting spillover"),
]

# Literature lag bands (months) for the report's sanity column (not enforced).
LIT_BANDS = {
    "customer_momentum": (1, 1),     # Cohen-Frazzini / Pinchuk
    "cross_industry_supply": (6, 12),  # Menzly-Ozbas / Shahrur
}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _load_inputs() -> dict:
    """Load monthly residual returns + macro source series (qlib_run)."""
    from electric_utilities_strategy.data.loader import load_config, load_all
    from electric_utilities_strategy.data import company_signals as comp
    from electric_utilities_strategy.data import industry_signals as ind

    cfg = load_config()
    prices, macro = load_all(config=cfg)
    etfs = [t for t in cfg["universe"]["etfs"] if t in prices.columns]
    ep = prices[etfs]

    mret = ep.resample("ME").last().pct_change()
    # residualize: remove the common electric utilities factor (cross-sectional mean)
    resid = mret.sub(mret.mean(axis=1), axis=0)
    idx = resid.index

    try:
        capex = comp.load_capex_pulse()
        capex_m = capex.resample("ME").last().reindex(idx, method="ffill") \
            if capex is not None and not capex.empty else pd.Series(np.nan, index=idx)
    except Exception:  # noqa: BLE001
        capex_m = pd.Series(np.nan, index=idx)
    try:
        pmi = ind.load_pmi_series()
        pmi_m = pmi.resample("ME").last().reindex(idx, method="ffill") \
            if pmi is not None and not pmi.empty else pd.Series(np.nan, index=idx)
    except Exception:  # noqa: BLE001
        pmi_m = pd.Series(np.nan, index=idx)

    def _try_monthly(fn):
        try:
            s = fn()
            return (s.resample("ME").last().reindex(idx, method="ffill")
                    if s is not None and not s.empty else pd.Series(np.nan, index=idx))
        except Exception:  # noqa: BLE001
            return pd.Series(np.nan, index=idx)

    from electric_utilities_strategy.data import altdata_signals as alt
    demand_m = _try_monthly(alt.load_power_demand_structural)
    price_m = _try_monthly(ind.load_gas_price_proxy)
    # rate_env:macro store 的 DGS10 取负 YoY(与 composite 同源同变换)
    def _rate():
        if macro is not None and "dgs10" in getattr(macro, "columns", []):
            r = macro["dgs10"].dropna()
            return -(r - r.shift(252))
        return pd.Series(dtype="float64")
    rate_m = _try_monthly(_rate)

    return {"resid": resid, "etfs": etfs, "idx": idx, "capex_m": capex_m,
            "pmi_m": pmi_m, "demand_m": demand_m, "price_m": price_m, "rate_m": rate_m}


def _source_series(node: str, data: dict) -> Optional[pd.Series]:
    resid = data["resid"]
    node_map = {NODE_AI_CAPEX: "capex_m", NODE_DEMAND: "demand_m",
                NODE_POWER_PRICE: "price_m", NODE_RATE_ENV: "rate_m",
                NODE_PMI: "pmi_m"}
    if node in node_map:
        return data.get(node_map[node])
    return resid[node] if node in resid.columns else None


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def _ic_at(src: pd.Series, tgt: pd.Series, lag: int) -> float:
    df = pd.concat([src.shift(lag), tgt], axis=1).dropna()
    if len(df) < MIN_OBS:
        return float("nan")
    return float(df.iloc[:, 0].corr(df.iloc[:, 1]))


def calibrate_edge_lag(src: pd.Series, tgt: pd.Series, weight: float) -> dict:
    """Return {best_lag, ic_best, ic_profile, n_obs} over lags 0..MAX_LAG.

    Positive edges maximise IC; negative (competition) edges minimise it (keep sign).
    """
    profile = {L: _ic_at(src, tgt, L) for L in range(MAX_LAG + 1)}
    valid = {L: c for L, c in profile.items() if not np.isnan(c)}
    if not valid:
        return {"best_lag": None, "ic_best": float("nan"), "ic_profile": profile, "n_obs": 0}
    best_lag = (min if weight < 0 else max)(valid, key=lambda L: valid[L])
    overlap = pd.concat([src.shift(best_lag), tgt], axis=1).dropna()
    return {"best_lag": int(best_lag), "ic_best": float(valid[best_lag]),
            "ic_profile": {int(k): (None if np.isnan(v) else round(float(v), 4))
                           for k, v in profile.items()},
            "n_obs": int(len(overlap))}


def _katz_centrality(graph: dict, alpha: float = 0.1) -> Dict[str, float]:
    """Katz centrality of subsector nodes from |edge weight| adjacency (numpy)."""
    nodes = sorted({n for e in graph for n in e} - SPECIAL_NODES)
    if not nodes:
        return {}
    ix = {n: i for i, n in enumerate(nodes)}
    A = np.zeros((len(nodes), len(nodes)))
    for (s, t), (w, _lag, _d) in graph.items():
        if s in ix and t in ix:
            A[ix[s], ix[t]] = abs(w)
    try:
        c = np.linalg.solve(np.eye(len(nodes)) - alpha * A.T, np.ones(len(nodes)))
    except np.linalg.LinAlgError:
        return {}
    c = c / (np.linalg.norm(c) or 1.0)
    return {n: round(float(c[ix[n]]), 4) for n in nodes}


def calibrate_graph() -> dict:
    """Calibrate all V1 edges + candidate logic_cpu inbound edges. Returns report dict."""
    data = _load_inputs()
    resid = data["resid"]
    report = {"meta": {"span": f"{data['idx'][0].date()}→{data['idx'][-1].date()}",
                       "n_months": int(len(data["idx"])), "max_lag": MAX_LAG,
                       "min_obs": MIN_OBS, "residualized": "cross-sectional demean (common-factor removed)"},
              "edges": [], "candidate_edges": [], "v2_graph_edges": [],
              "katz_centrality": _katz_centrality(SUPPLY_CHAIN_GRAPH)}

    # --- existing V1 edges: keep weight, recalibrate lag ---
    for (src, tgt), (w, assumed_lag, desc) in SUPPLY_CHAIN_GRAPH.items():
        s = _source_series(src, data)
        if s is None or tgt not in resid.columns:
            row = {"source": src, "target": tgt, "weight": w, "assumed_lag": assumed_lag,
                   "best_lag": assumed_lag, "ic_best": None, "n_obs": 0,
                   "note": "no source/target series — keep assumed lag", "desc": desc}
            report["edges"].append(row)
            report["v2_graph_edges"].append(
                {"source": src, "target": tgt, "weight": w, "lag_months": assumed_lag, "desc": desc})
            continue
        cal = calibrate_edge_lag(s, resid[tgt], w)
        best_lag = cal["best_lag"] if cal["best_lag"] is not None else assumed_lag
        ic_assumed = cal["ic_profile"].get(assumed_lag)
        report["edges"].append({
            "source": src, "target": tgt, "weight": w, "assumed_lag": assumed_lag,
            "best_lag": best_lag, "ic_best": cal["ic_best"], "ic_assumed": ic_assumed,
            "ic_profile": cal["ic_profile"], "n_obs": cal["n_obs"], "desc": desc})
        report["v2_graph_edges"].append(
            {"source": src, "target": tgt, "weight": w, "lag_months": best_lag, "desc": desc})

    # --- candidate logic_cpu inbound edges: calibrate, keep if |IC| ≥ threshold ---
    for (src, tgt, w, desc) in CANDIDATE_EDGES:
        s = _source_series(src, data)
        if s is None or tgt not in resid.columns:
            continue
        cal = calibrate_edge_lag(s, resid[tgt], w)
        keep = cal["ic_best"] is not None and not np.isnan(cal["ic_best"]) \
            and abs(cal["ic_best"]) >= KEEP_IC_THRESHOLD
        row = {"source": src, "target": tgt, "weight": w, "best_lag": cal["best_lag"],
               "ic_best": cal["ic_best"], "ic_profile": cal["ic_profile"],
               "n_obs": cal["n_obs"], "kept": bool(keep), "desc": desc}
        report["candidate_edges"].append(row)
        if keep:
            report["v2_graph_edges"].append(
                {"source": src, "target": tgt, "weight": w,
                 "lag_months": int(cal["best_lag"]), "desc": desc + f" (IC={cal['ic_best']:+.3f})"})

    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _print_report(report: dict) -> None:
    print("=" * 96)
    print(f"AEUS SUPPLY-CHAIN GRAPH — empirical lag calibration (residual returns)")
    print(f"  span {report['meta']['span']}  ({report['meta']['n_months']} months)  "
          f"residualized: {report['meta']['residualized']}")
    print("=" * 96)
    print(f"{'edge':34}{'assumed':>8}{'empirical':>10}{'IC@assumed':>12}{'IC@best':>9}{'n':>5}")
    print("-" * 96)
    for r in report["edges"]:
        ica = r.get("ic_assumed")
        icb = r.get("ic_best")
        print(f"{(r['source']+'→'+r['target']):34}{r['assumed_lag']:>8}{r['best_lag']:>10}"
              f"{(f'{ica:+.3f}' if isinstance(ica,(int,float)) else '   n/a'):>12}"
              f"{(f'{icb:+.3f}' if isinstance(icb,(int,float)) else '  n/a'):>9}{r['n_obs']:>5}")
    print("\n-- candidate logic_cpu inbound edges (keep if |IC| ≥ %.2f) --" % KEEP_IC_THRESHOLD)
    for r in report["candidate_edges"]:
        icb = r.get("ic_best")
        print(f"{(r['source']+'→'+r['target']):34}{'':>8}{str(r['best_lag']):>10}"
              f"{'':>12}{(f'{icb:+.3f}' if isinstance(icb,(int,float)) else '  n/a'):>9}"
              f"{r['n_obs']:>5}   {'KEEP' if r['kept'] else 'drop'}")
    print("\n-- Katz centrality (subsector nodes, |weight| adjacency; Ahern reference) --")
    for n, c in sorted(report["katz_centrality"].items(), key=lambda x: -x[1]):
        print(f"   {n:16} {c:.3f}")
    print("\n-- V2 graph_config.edges (paste into config.yaml supply_chain.graph_config) --")
    print(_edges_to_yaml(report["v2_graph_edges"]))


def _edges_to_yaml(edges: List[dict]) -> str:
    lines = ["    edges:"]
    for e in edges:
        lines.append(
            f"      - {{source: {e['source']}, target: {e['target']}, "
            f"weight: {e['weight']}, lag_months: {e['lag_months']}, desc: \"{e['desc']}\"}}")
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    report = calibrate_graph()
    out_dir = _QLIB_DIR / "electric_utilities_strategy" / "backtest_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "graph_calibration_report.json"
    out_path.write_text(json.dumps(report, indent=2))
    _print_report(report)
    print(f"\nReport written → {out_path}")


if __name__ == "__main__":
    main()
