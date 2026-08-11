"""strategy/calibration.py — isotonic probability calibration before Kelly (PLAN §19-3).

Un-calibrated over-confidence is poison for Kelly sizing: if the model says 0.70 but
events at "0.70" only realize 55% of the time, quarter-Kelly still over-bets every time.
The fix (mother-template probability_calibration, same shape):

  fit    walk-forward replayed per-leg (fair, outcome) pairs (research/eval collects
         them OOS-only) → sklearn IsotonicRegression → stored as a breakpoint map in
         the experiments table (name='calibration_map', per series)
  apply  piecewise-linear interpolation through the stored map; IDENTITY fallback when
         no map exists or the sample is too thin (< MIN_PAIRS) — a bad map is worse
         than no map

decide_all applies this to every enumerated structure's fair before decide(), so the
gates and Kelly consume calibrated probabilities. The map is refit weekly by
research/eval.run_series (OOS pairs only — never in-sample).

⚠ LIVE MAPS ARE PINNED TO IDENTITY since 2026-08-10 (the fit still runs, into the
'calibration_map_shadow' experiments row). What happened: the per-leg (fair, outcome)
pairs are clustered by event — legs of one event share an outcome — so ~480 "pairs"
carried ~43 independent events, and isotonic regression on that produced step maps
with terminal plateaus at exactly 0.0 and 1.0 (KXWTIW: every model prob >= 0.664
became a CERTAINTY) and a base-rate shelf (KXNATGASW: [0.27, 0.55] -> 0.60) that
turned decision #4346's raw-fair-0.334 leg into a fake 0.60-fair entry at cost 0.36.
On a model already graded Brier-behind-market, isotonic "calibration" converges on
the base rate — statistically faithful, and still poison for Kelly, which reads a
flat probability vs a sloped price curve as edge everywhere below base rate.
Re-enabling requires a preregistered forward criterion; do not un-pin ad hoc.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

MIN_PAIRS = 200          # below this the isotonic fit is noise — stay identity
_CACHE: dict[str, tuple[str, list, list]] = {}   # series -> (created_ts, xs, ys)


def fit_map(pairs: list[tuple[float, float]]) -> dict | None:
    """pairs: OOS (fair, outcome01). Returns {'x': [...], 'y': [...]} breakpoints or
    None if too thin. y is the calibrated probability at model-prob x."""
    if len(pairs) < MIN_PAIRS:
        return None
    from sklearn.isotonic import IsotonicRegression
    xs = [max(0.0, min(1.0, f)) for f, _ in pairs]
    ys = [o for _, o in pairs]
    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    iso.fit(xs, ys)
    bx = [round(float(v), 5) for v in iso.X_thresholds_]
    by = [round(float(v), 5) for v in iso.y_thresholds_]
    return {"x": bx, "y": by, "n_pairs": len(pairs)}


def store_map(conn, series: str, cal_map: dict | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES('calibration_map',?,?,?,?,?)",
        (f"iso:{series}", series, f"n{(cal_map or {}).get('n_pairs', 0)}",
         json.dumps(cal_map) if cal_map else json.dumps({"identity": True}),
         datetime.now(timezone.utc).isoformat()))
    conn.commit()
    _CACHE.pop(series, None)


def store_named_map(conn, series: str, name: str, cal_map: dict | None) -> None:
    """Same breakpoint format under any experiments name (e.g. the favorite-longshot
    'market_calibration_map' fit on (market prob, outcome) pairs)."""
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES(?,?,?,?,?,?)",
        (name, f"iso:{series}", series, f"n{(cal_map or {}).get('n_pairs', 0)}",
         json.dumps(cal_map) if cal_map else json.dumps({"identity": True}),
         datetime.now(timezone.utc).isoformat()))
    conn.commit()
    _CACHE.pop(f"{name}:{series}", None)


def _load_named(conn, series: str, name: str):
    key = f"{name}:{series}"
    r = conn.execute(
        "SELECT metrics_json, created_ts FROM experiments WHERE name=?"
        " AND series=? ORDER BY created_ts DESC LIMIT 1", (name, series)).fetchone()
    if r is None:
        return None
    cached = _CACHE.get(key)
    if cached and cached[0] == r["created_ts"]:
        return cached if cached[1] is not None else None
    m = json.loads(r["metrics_json"] or "{}")
    entry = ((r["created_ts"], m["x"], m["y"]) if m.get("x") and m.get("y")
             else (r["created_ts"], None, None))
    _CACHE[key] = entry
    return entry if entry[1] is not None else None


def interp(entry, p: float) -> float:
    """Piecewise-linear interpolation through a loaded breakpoint entry."""
    _, xs, ys = entry
    if p <= xs[0]:
        return float(ys[0])
    if p >= xs[-1]:
        return float(ys[-1])
    import bisect
    i = bisect.bisect_right(xs, p) - 1
    x0, x1, y0, y1 = xs[i], xs[i + 1], ys[i], ys[i + 1]
    if x1 == x0:
        return float(y0)
    return float(y0 + (y1 - y0) * (p - x0) / (x1 - x0))


def _load(conn, series: str):
    r = conn.execute(
        "SELECT metrics_json, created_ts FROM experiments WHERE name='calibration_map'"
        " AND series=? ORDER BY created_ts DESC LIMIT 1", (series,)).fetchone()
    if r is None:
        return None
    cached = _CACHE.get(series)
    if cached and cached[0] == r["created_ts"]:
        return cached
    m = json.loads(r["metrics_json"] or "{}")
    if not m.get("x") or not m.get("y"):
        entry = (r["created_ts"], None, None)
    else:
        entry = (r["created_ts"], m["x"], m["y"])
    _CACHE[series] = entry
    return entry


def apply(conn, series: str, p: float) -> float:
    """Calibrated probability; identity when no usable map."""
    entry = _load(conn, series)
    if entry is None or entry[1] is None:
        return p
    _, xs, ys = entry
    if p <= xs[0]:
        return float(ys[0])
    if p >= xs[-1]:
        return float(ys[-1])
    import bisect
    i = bisect.bisect_right(xs, p) - 1
    x0, x1, y0, y1 = xs[i], xs[i + 1], ys[i], ys[i + 1]
    if x1 == x0:
        return float(y0)
    return float(y0 + (y1 - y0) * (p - x0) / (x1 - x0))


def calibrate_structs(conn, series: str, structs: list) -> list:
    """Rebuild Structs with calibrated fair (frozen dataclass → replace)."""
    from dataclasses import replace
    out = []
    for st in structs:
        cf = apply(conn, series, st.fair)
        out.append(replace(st, fair=round(cf, 6)) if cf != st.fair else st)
    return out
