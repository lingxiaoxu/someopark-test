"""Historical reference prices, with explicit sampling and request evidence.

``sample_price`` is the business interface: causal, bounded, and never executable
BBO. ``price_at`` retains the old tuple API solely for legacy research callers.
"""
from __future__ import annotations

import hashlib
import json
import math
from statistics import median


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def normalize_series(series, *, key="price"):
    """Sort/deduplicate without hiding malformed or conflicting provider points."""
    points = {}
    for p in series:
        ts, price = p.get("ts", p.get("t")), p.get(key, p.get("p"))
        if not _finite(ts) or not _finite(price) or not 0 <= price <= 1:
            raise ValueError("invalid_history_point")
        if ts in points and points[ts] != price:
            raise ValueError("conflicting_history_timestamp")
        points[ts] = price
    return [{"ts": ts, "price": points[ts]} for ts in sorted(points)]


def series_receipt(series, *, identity, start_ts, end_ts, fidelity, received_at,
                   request_started_at=None, raw=None, raw_hash=None, provider_request_window=None):
    """Keep observed quality separate from a requested one-minute fidelity.

    If a legacy reader supplied only normalized points, raw_hash stays unknown.
    received_at is retrieval time, never retroactive historical availability.
    A valid provider response may append points outside its requested window.
    Retain that evidence, but sample only the declared window; malformed points
    or conflicting prices anywhere in the response still invalidate the series.
    """
    if not (_finite(start_ts) and _finite(end_ts) and start_ts < end_ts):
        raise ValueError("invalid_history_window")
    if not received_at or fidelity < 1:
        raise ValueError("missing_history_receipt_time")
    reason = None
    try:
        points = normalize_series(series)
    except (ValueError, TypeError, AttributeError) as exc:
        points, reason = [], str(exc)
    projection = None
    empty_reason = "empty_series"
    outside = [p for p in points if p["ts"] < start_ts or p["ts"] > end_ts]
    if outside:
        projection = {"rule": "declared_request_window_inclusive", "schema_version": 1,
                      "source_points_hash": _hash(points), "source_count": len(points),
                      "excluded_points": outside, "excluded_count": len(outside)}
        points = [p for p in points if start_ts <= p["ts"] <= end_ts]
        projection["retained_count"] = len(points)
        empty_reason = "no_points_in_requested_window"
    gaps = [b["ts"] - a["ts"] for a, b in zip(points, points[1:])]
    payload = {"schema_version": 1, "identity": identity,
               "request_window": {"start_ts": start_ts, "end_ts": end_ts, "fidelity": fidelity},
               "request_started_at": request_started_at, "received_at": received_at,
               "provider_request_window": provider_request_window, "raw": raw,
               "raw_hash": raw_hash or (_hash(raw) if raw is not None else None),
               "points_hash": _hash(points), "points": points,
               "coverage": {"start_ts": points[0]["ts"] if points else None,
                            "end_ts": points[-1]["ts"] if points else None,
                            "count": len(points), "median_gap_s": median(gaps) if gaps else None,
                            "max_gap_s": max(gaps) if gaps else None},
               "quality": "invalid" if reason else ("available" if points else "empty"),
               "reason": reason or (None if points else empty_reason),
               "price_kind": "historical_reference", "price_unit": "probability",
               "executable": False}
    if projection is not None:
        payload["window_projection"] = projection
    return {**payload, "series_receipt_id": _hash(payload)}


def sample_price(series_or_receipt, target_ts, *, max_gap_s=180):
    """Return the last point <= target, at most 180 seconds old by default."""
    receipt = series_or_receipt if isinstance(series_or_receipt, dict) else None
    out = {"status": "unavailable", "price": None, "target_ts": target_ts,
           "provider_sample_at": None, "sample_ts": None, "age_seconds": None,
           "series_receipt_id": receipt.get("series_receipt_id") if receipt else None,
           "price_kind": "historical_reference", "price_unit": "probability",
           "executable": False, "reason": None}
    if not _finite(target_ts) or not _finite(max_gap_s) or not 0 <= max_gap_s <= 180:
        return {**out, "status": "invalid", "reason": "invalid_sampling_rule"}
    if receipt and receipt.get("quality") == "invalid":
        return {**out, "status": "invalid", "reason": receipt["reason"]}
    try:
        points = normalize_series(receipt["points"] if receipt else series_or_receipt)
    except (ValueError, TypeError, AttributeError) as exc:
        return {**out, "status": "invalid", "reason": str(exc)}
    past = [p for p in points if p["ts"] <= target_ts]
    if not past:
        return {**out, "reason": "no_causal_sample"}
    p = past[-1]
    age = target_ts - p["ts"]
    if age > max_gap_s:
        return {**out, "reason": "sample_too_old", "age_seconds": age}
    return {**out, "status": "ok", "price": p["price"], "sample_ts": p["ts"],
            "provider_sample_at": p["ts"], "age_seconds": age}


def price_at(series: list[dict], when_ts: int, *, key="price", max_gap_s=900, causal=False):
    """Legacy tuple adapter. nearest mode is noncausal research, never a trade input.

    New historical business collectors use sample_price; retaining this old API
    avoids changing archived analyses when they are explicitly replayed.
    """
    if not series:
        return None, None
    points = [p for p in series if not causal or p["ts"] <= when_ts]
    if not points:
        return None, None
    best = max(points, key=lambda p: p["ts"]) if causal else min(points, key=lambda p: abs(p["ts"] - when_ts))
    if max_gap_s is not None and abs(best["ts"] - when_ts) > max_gap_s:
        return None, None
    return best.get(key), best["ts"]


def kalshi_mid_series(candles):
    """Legacy reference midpoint adapter; does not imply two executable book sides."""
    from prediction_market_soccer.util.pricing import mid
    return [{**c, "mid": mid(c.get("ask"), c.get("bid"))} for c in candles]
