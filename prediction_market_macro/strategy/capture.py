"""strategy/capture.py — per-strike edge-capture memory (PLAN §19-4, 铁律 5 后半).

research/eval.decision_replay accumulates, per (series, cap_key), how much of the
booked edge was actually REALIZED — cap_key = "{side}@{offset}" where offset is the
strike's distance from the model mean in settlement-grid steps. Strikes where we
historically capture <40% of booked edge (with enough sample) are exactly where the
model's booked edge is fictional (adverse selection / stale quotes) — stop pressing
those. Series-level capture already gates real money in research/eval.gate_verdict.
"""
from __future__ import annotations

import json

MIN_N = 8
MIN_CAPTURE = 0.4
_CACHE: dict[str, tuple[str, dict]] = {}


def load_strike_capture(conn, series: str) -> dict:
    r = conn.execute(
        "SELECT metrics_json, created_ts FROM experiments WHERE name='decision_replay'"
        " AND series=? ORDER BY created_ts DESC LIMIT 1", (series,)).fetchone()
    if r is None:
        return {}
    cached = _CACHE.get(series)
    if cached and cached[0] == r["created_ts"]:
        return cached[1]
    caps = (json.loads(r["metrics_json"] or "{}")).get("strike_capture") or {}
    _CACHE[series] = (r["created_ts"], caps)
    return caps


def cap_key(side: str, strike: float | None, model_mean: float | None,
            round_rule: float) -> str | None:
    if strike is None or model_mean is None or not round_rule:
        return None
    off = int(round((strike - model_mean) / round_rule))
    return f"{side}@{max(-5, min(5, off)):+d}"


def filter_structs(structs: list, capture: dict, model_mean: float | None,
                   round_rule: float, strikes_by_ticker: dict[str, float]) -> tuple[list, list[str]]:
    """Drop single structs whose cap_key has n>=MIN_N and realized/expected<MIN_CAPTURE.
    Buckets and unknown keys pass through untouched."""
    if not capture:
        return structs, []
    kept, dropped = [], []
    for st in structs:
        if st.kind != "single":
            kept.append(st)
            continue
        leg = st.legs[0]
        k = cap_key(leg.side, strikes_by_ticker.get(leg.ticker), model_mean, round_rule)
        c = capture.get(k) if k else None
        if c and c.get("n", 0) >= MIN_N and c.get("expected", 0) > 0:
            ratio = c["realized"] / c["expected"]
            if ratio < MIN_CAPTURE:
                dropped.append(f"capture_gate:{k}:{ratio:.2f}")
                continue
        kept.append(st)
    return kept, dropped
