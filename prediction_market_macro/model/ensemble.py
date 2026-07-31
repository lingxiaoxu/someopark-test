"""model/ensemble.py — adaptive log-pool of model + market + bridge (PLAN §19-2,
PLAN_EXTENSION §23.2-1: Bates-Granger inverse-MSPE weights + trimmed-mean guard).

Sources per (series, period), all read from what refresh already stores:
  model   the registry-bound production model's ladder pmf (preds)
  market  the devigged ladder implied from the latest quotes (strategy/devig)
  bridge  model/bridge.py shadow pred where supported

Weights: w_i ∝ 1/MSPE_i from the trailing TRAIL_N per-event Brier rows in
source_scores (research/eval writes them weekly), floored/ceilinged so no source ever
dies or dominates; uniform-prior fallback when history is thin. Trimmed guard: a source
whose trailing Brier is catastrophically worse than the best (×3) is dropped from the
pool for that series rather than averaged in.

§7-bis: SHADOW ONLY — shadow_run() writes preds with model_version='ensemble/0.1.0';
the production guard in decide_all keeps them out of live decisions until the adoption
gate (research/eval) promotes the member. 铁律 13.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from prediction_market_macro.model.common import Empirical, Pred

VERSION = "ensemble/0.1.0"
TRAIL_N = 60
W_FLOOR, W_CEIL = 0.10, 0.70
TRIM_RATIO = 3.0
PRIOR = {"model": 0.35, "market": 0.50, "bridge": 0.15}


def learn_weights(conn, series: str, offset: str = "-1h") -> dict[str, float]:
    """Inverse-MSPE weights from trailing source_scores; PRIOR fallback."""
    rows = conn.execute(
        "SELECT source, brier FROM source_scores WHERE series=? AND offset=?"
        " ORDER BY period DESC LIMIT ?", (series, offset, TRAIL_N * 3)).fetchall()
    per: dict[str, list[float]] = {}
    for r in rows:
        if r["brier"] is not None:
            per.setdefault(r["source"], []).append(float(r["brier"]))
    per = {k: v[:TRAIL_N] for k, v in per.items() if len(v) >= 6}
    if len(per) < 2:
        return dict(PRIOR)
    mspe = {k: max(sum(v) / len(v), 1e-6) for k, v in per.items()}
    best = min(mspe.values())
    kept = {k: m for k, m in mspe.items() if m <= best * TRIM_RATIO}   # trimmed guard
    raw = {k: 1.0 / m for k, m in kept.items()}
    tot = sum(raw.values())
    w = {k: v / tot for k, v in raw.items()}
    # floor/ceiling then renormalize
    w = {k: min(max(v, W_FLOOR), W_CEIL) for k, v in w.items()}
    tot = sum(w.values())
    return {k: round(v / tot, 4) for k, v in w.items()}


def log_pool(pmfs: dict[str, dict[float, float]], weights: dict[str, float],
             eps: float = 1e-6) -> dict[float, float]:
    """Weighted geometric pool over the UNION grid, renormalized."""
    keys = sorted({k for p in pmfs.values() for k in p})
    used = {s: w for s, w in weights.items() if s in pmfs and w > 0}
    tot_w = sum(used.values())
    out = {}
    for k in keys:
        lp = 0.0
        for s, w in used.items():
            lp += (w / tot_w) * math.log(max(pmfs[s].get(k, 0.0), eps))
        out[k] = math.exp(lp)
    z = sum(out.values())
    return {k: v / z for k, v in out.items()}


def _model_pmf(conn, series: str, key: str, spec) -> tuple[dict, str] | None:
    r = conn.execute(
        "SELECT ladder_json, data_horizon FROM preds WHERE series=? AND period=?"
        " AND model_version LIKE ? ORDER BY asof DESC LIMIT 1",
        (series, key, spec.model + "/%")).fetchone()
    if r is None or not r["ladder_json"]:
        return None
    return ({float(k): v for k, v in json.loads(r["ladder_json"]).items()},
            r["data_horizon"])


def _bridge_pmf(conn, series: str, key: str) -> dict | None:
    r = conn.execute(
        "SELECT ladder_json FROM preds WHERE series=? AND period=?"
        " AND model_version LIKE 'bridge/%' ORDER BY asof DESC LIMIT 1",
        (series, key)).fetchone()
    if r is None or not r["ladder_json"]:
        return None
    return {float(k): v for k, v in json.loads(r["ladder_json"]).items()}


def _market_pmf(conn, series: str, tok: str) -> dict | None:
    from prediction_market_macro.ops.decide_all import _legs_meta
    from prediction_market_macro.strategy import devig
    legs = _legs_meta(conn, series, tok)
    if not legs:
        return None
    is_bucket = any(l.get("strike_type") == "between" for l in legs)
    impl = devig.bucket_implied(legs) if is_bucket else devig.ladder_implied(legs)
    pmf = impl.get("pmf")
    if not pmf:
        return None
    return {float(k): v for k, v in pmf.items()}


def shadow_run(conn, settings) -> int:
    """Blend and store shadow ensemble preds for every open ladder (series, period)."""
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.model.common import pred_to_row
    from prediction_market_macro.ops.predict_all import _open_periods
    now = datetime.now(timezone.utc)
    n = 0
    for spec in REGISTRY.values():
        if spec.structure == "categorical":
            continue                     # fed already log-pools rule×market internally
        for tok, key in _open_periods(conn, spec.ticker):
            try:
                m = _model_pmf(conn, spec.ticker, key, spec)
                if m is None:
                    continue
                model_pmf, horizon = m
                pmfs = {"model": model_pmf}
                mk = _market_pmf(conn, spec.ticker, tok)
                if mk:
                    pmfs["market"] = mk
                br = _bridge_pmf(conn, spec.ticker, key)
                if br:
                    pmfs["bridge"] = br
                if len(pmfs) < 2:
                    continue             # nothing to pool
                w = learn_weights(conn, spec.ticker)
                pooled = log_pool(pmfs, w)
                # encode the pooled pmf; grid values ARE the support (already on the
                # settlement grid) — store as ladder + an Empirical stub dist
                vals = []
                for k, v in sorted(pooled.items()):
                    vals.extend([k] * max(1, int(round(v * 2000))))
                pred = Pred(series=spec.ticker, period=key,
                            dist=Empirical(tuple(vals[:4000])), asof=now,
                            model_version=VERSION,
                            inputs={"weights": w, "sources": sorted(pmfs)},
                            data_horizon=datetime.fromisoformat(horizon))
                ladder = {str(k): round(v, 6) for k, v in pooled.items()}
                conn.execute(
                    "INSERT OR REPLACE INTO preds(series, period, asof, model_version,"
                    " dist_json, ladder_json, inputs_json, data_horizon, created_ts)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", pred_to_row(pred, ladder))
                n += 1
            except Exception:                                # noqa: BLE001
                continue                                      # shadow: silent skip
    conn.commit()
    return n
