"""research/attribution.py — the error-attribution feedback loop (PLAN §19-6).

Weekly: collect settled decisions whose |z| > 2 (model-miss per ops/pnl attribution),
cluster by series and by calendar features that plausibly explain the miss (holiday
week / FOMC week / quarter-end), store an 'error_attribution' experiments row and
alert. Discipline (mother template): findings feed STRUCTURAL fixes only — no
parameter tuning off single misses.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


def _period_features(period: str) -> list[str]:
    feats = []
    if len(period) >= 7:
        month = period[5:7]
        if month in ("11", "12", "01"):
            feats.append("holiday_season")
        if month in ("03", "06", "09", "12"):
            feats.append("quarter_end")
    return feats


def weekly_attribution(conn) -> dict:
    rows = conn.execute(
        "SELECT series, period, note, inputs_json FROM decisions WHERE id IN"
        " (SELECT MIN(id) FROM decisions WHERE kind='settle_note'"
        "  GROUP BY series, period) ORDER BY id DESC LIMIT 200").fetchall()
    misses, total = [], 0
    for r in rows:
        try:
            inp = json.loads(r["inputs_json"] or "{}")
        except json.JSONDecodeError:
            continue
        z = inp.get("z")
        if z is None:
            continue
        total += 1
        if abs(z) > 2:
            misses.append({"series": r["series"], "period": r["period"],
                           "z": z, "features": _period_features(r["period"])})
    by_series: dict[str, int] = {}
    by_feature: dict[str, int] = {}
    for m in misses:
        by_series[m["series"]] = by_series.get(m["series"], 0) + 1
        for f in m["features"]:
            by_feature[f] = by_feature.get(f, 0) + 1
    out = {"n_settled_scored": total, "n_misses": len(misses),
           "by_series": by_series, "by_feature": by_feature,
           "misses": misses[:30]}
    now = datetime.now(timezone.utc).isoformat()
    wk = datetime.now(timezone.utc).strftime("%G-W%V")
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES('error_attribution',?,'*',?,?,?)",
        (f"weekly:{wk}", f"n{total}", json.dumps(out, ensure_ascii=False), now))
    for s, c in by_series.items():
        if c >= 3:
            conn.execute(
                "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                (now, "warn", "attribution",
                 f"{s}: {c} model-miss settlements (|z|>2) — structural review due"))
    conn.commit()
    return out
