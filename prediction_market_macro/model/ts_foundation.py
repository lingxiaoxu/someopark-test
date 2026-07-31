"""model/ts_foundation.py — Chronos-2 zero-shot shadow bridge (PLAN §7 ts_foundation,
§5-bis.3, §7-bis).

Copies the call shape of the house example VIXForecast.py (predict_quantiles on a
context strictly ending at asof — zero leakage by construction) WITHOUT touching that
file or its fine-tuned ckpts. Zero-shot base model `amazon/chronos-2` from the
pre-existing shared HF cache (no new state outside the macro tree is created; a
fine-tune, when it ever happens, checkpoints INSIDE prediction_market_macro/data/).

SHADOW ONLY (§7-bis stage 1): predictions land in preds with model_version
"chronos2/zero-shot-0.1.0" and are excluded from decisions by the production-model
guard in ops/decide_all (model_version must match the registry's model binding).
Adoption gate: live-forward shadow window (weekly series >= 8 periods, monthly >= 3)
beating BOTH the stat model and the market baseline on Brier/CRPS — pretraining has
seen historical macro series, so backtest evidence is contaminated and inadmissible.

PIT: context comes exclusively from FeatureStore reads with knowledge_time <= asof;
data_horizon is stamped from the reads like every other model.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from prediction_market_macro.model.common import Empirical, Pred, grid_pmf, pred_to_row
from prediction_market_macro.model.features import FeatureStore

VERSION = "chronos2/zero-shot-0.1.0"
MODEL_ID = "amazon/chronos-2"
_QLEVELS = [round(q, 2) for q in np.arange(0.01, 1.0, 0.01)]

# series → (source kind, source key, cadence of one forecast step)
_SOURCES = {
    "KXWTIW": ("fut", "CL", "bday"),
    "KXNATGASW": ("fut", "NG", "bday"),
    "KXAAAGASW": ("fred", "GASREGW", "week"),
    "KXJOBLESSCLAIMS": ("fred", "ICSA", "week"),
}

_PIPELINE = None


def _pipeline():
    global _PIPELINE
    if _PIPELINE is None:
        import torch
        from chronos import BaseChronosPipeline
        _PIPELINE = BaseChronosPipeline.from_pretrained(
            MODEL_ID, device_map="cpu", dtype=torch.float32)
    return _PIPELINE


def _context_and_h(conn, asof: datetime, period: str, series: str):
    kind, key, step = _SOURCES[series]
    fs = FeatureStore(conn)
    if kind == "fut":
        s, horizon = fs.fut_closes(key, asof, n=260)
    else:
        s, horizon = fs.fred_series(key, asof)
        s = s.tail(260)
    if len(s) < 60 or horizon is None:
        raise RuntimeError(f"{series}: context too short (n={len(s)})")
    target_d = pd.Timestamp(period)
    last_d = pd.Timestamp(s.index[-1])
    if step == "bday":
        h = int(np.busday_count(last_d.date(), target_d.date())) if target_d > last_d else 1
    else:
        h = math.ceil(max((target_d - last_d).days, 1) / 7.0)
    return s, max(h, 1), horizon


def predict(conn, asof: datetime, period: str, series: str) -> Pred:
    s, h, horizon = _context_and_h(conn, asof, period, series)
    ctx = s.values.astype(np.float32)
    q_list, _ = _pipeline().predict_quantiles(
        [ctx], prediction_length=h, quantile_levels=_QLEVELS)
    q = np.asarray(q_list[0])                    # (1, h, len(_QLEVELS))
    terminal = q[0, h - 1, :].astype(float)
    if not np.all(np.isfinite(terminal)) or np.any(np.diff(terminal) < -1e-9):
        # crossing / NaN quantiles = broken inference (§9.6e) — sort to repair, flag
        terminal = np.sort(terminal[np.isfinite(terminal)])
        if len(terminal) < 10:
            raise RuntimeError(f"{series}: chronos quantiles unusable")
    dist = Empirical(tuple(np.round(terminal, 4).tolist()))
    return Pred(series=series, period=period, dist=dist, asof=asof,
                model_version=VERSION,
                inputs={"model_id": MODEL_ID, "ctx_len": len(ctx), "h_steps": h,
                        "last_obs": float(ctx[-1]), "median": round(float(terminal[49]), 4)
                        if len(terminal) > 49 else None, "mode": "shadow"},
                data_horizon=datetime.fromisoformat(horizon))


def shadow_run(conn, settings) -> int:
    """Write shadow preds for every open period of the covered series. Degradable:
    a per-series failure alerts and moves on; decisions never wait on this."""
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.util.periods import kalshi_period_to_key
    now = datetime.now(timezone.utc)
    n = 0
    for series in _SOURCES:
        spec = REGISTRY[series]
        rows = conn.execute(
            "SELECT DISTINCT period FROM contracts WHERE series=? AND status='active'",
            (series,)).fetchall()
        for r in rows:
            key = kalshi_period_to_key(r["period"])
            if not key:
                continue
            try:
                pred = predict(conn, now, key, series)
                pmf = grid_pmf(pred.dist, spec.round_rule)
                ladder = {str(k): round(v, 6) for k, v in pmf.items()}
                conn.execute(
                    "INSERT OR REPLACE INTO preds(series, period, asof, model_version,"
                    " dist_json, ladder_json, inputs_json, data_horizon, created_ts)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", pred_to_row(pred, ladder))
                n += 1
            except Exception as e:                               # noqa: BLE001
                conn.execute(
                    "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                    (now.isoformat(), "warn", "chronos_shadow", f"{series}/{key}: {e}"))
    conn.commit()
    return n
