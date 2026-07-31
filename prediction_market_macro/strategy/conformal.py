"""strategy/conformal.py — Adaptive Conformal Inference sizing throttle
(PLAN_EXTENSION §23.2-4; Gibbs & Candès 2021 ACI update rule).

Isotonic calibration fixes marginal probabilities; it says nothing about whether the
model is CURRENTLY inside its own historical error envelope. This module runs ACI on
the per-event OOS model Brier sequence (source_scores, chronological):

  nonconformity score  s_t = event Brier of the model
  envelope             q_t = empirical (1-α_t) quantile of the trailing scores
  ACI update           α_{t+1} = α_t + γ(target − 1{s_t > q_t})

When the LATEST event breached the envelope (the model is suddenly worse than its own
history says it should be), the sizing factor for that series drops to 0.5 until the
next in-envelope event. This is a throttle, not a forecaster — no §7-bis gate needed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

GAMMA = 0.05
TARGET = 0.10            # aim: 10% of events outside the envelope
ALPHA0 = 0.10
TRAIL = 40
BREACH_FACTOR = 0.5


def aci_update(alpha: float, breached: bool, gamma: float = GAMMA,
               target: float = TARGET) -> float:
    """One ACI step; α clamped to (0.01, 0.5)."""
    a = alpha + gamma * (target - (1.0 if breached else 0.0))
    return min(0.5, max(0.01, a))


def _quantile(xs: list[float], q: float) -> float:
    ss = sorted(xs)
    i = min(len(ss) - 1, max(0, int(round(q * (len(ss) - 1)))))
    return ss[i]


def evaluate_sequence(scores: list[float]) -> dict:
    """Walk the chronological score sequence with ACI. Returns the terminal state."""
    alpha = ALPHA0
    breached = False
    width = None
    for i, s in enumerate(scores):
        hist = scores[max(0, i - TRAIL):i]
        if len(hist) < 8:
            continue
        width = _quantile(hist, 1.0 - alpha)
        breached = bool(s > width)
        alpha = aci_update(alpha, breached)
    return {"alpha": round(alpha, 4),
            "width": round(width, 6) if width is not None else None,
            "latest_breached": breached,
            "factor": BREACH_FACTOR if breached else 1.0,
            "n": len(scores)}


def evaluate(conn, series: str) -> dict:
    """ACI over the stored per-event model Briers; persists 'conformal_state'."""
    rows = conn.execute(
        "SELECT brier FROM source_scores WHERE series=? AND source='model'"
        " AND offset='-1h' ORDER BY period", (series,)).fetchall()
    scores = [float(r["brier"]) for r in rows if r["brier"] is not None]
    st = evaluate_sequence(scores)
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES('conformal_state',?,?,?,?,?)",
        (f"aci:{series}", series, f"n{st['n']}", json.dumps(st),
         datetime.now(timezone.utc).isoformat()))
    conn.commit()
    return st


def sizing_factor(conn, series: str) -> float:
    """Latest stored throttle factor (1.0 when never evaluated)."""
    r = conn.execute(
        "SELECT metrics_json FROM experiments WHERE name='conformal_state' AND series=?"
        " ORDER BY created_ts DESC LIMIT 1", (series,)).fetchone()
    if r is None:
        return 1.0
    return float(json.loads(r["metrics_json"] or "{}").get("factor", 1.0))
