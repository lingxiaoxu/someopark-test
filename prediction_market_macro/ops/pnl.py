"""ops/pnl.py — marks + settlement reconciliation (PLAN §12).

mark_all: open paper positions marked to latest orderbook mid.
settle_pass: settled contracts → realized PnL rows + z-score attribution: z of the
realized first print under the OPEN decision's own predictive dist —
|z|<1 ⇒ luck-zone (outcome was in the model's meat, PnL is variance),
|z|>2 ⇒ model-miss (the model was simply wrong — feeds the error-attribution loop).
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from prediction_market_macro.ops.ledger import open_positions


def _dist_mu_sigma(dist: dict) -> tuple[float, float] | None:
    comps = dist.get("comps")
    if comps:
        w = [c[0] for c in comps]
        mu = sum(ci[0] * ci[1] for ci in comps)
        var = sum(ci[0] * (ci[2] ** 2 + (ci[1] - mu) ** 2) for ci in comps)
        return mu, math.sqrt(max(var, 1e-12))
    vals = dist.get("values")
    if vals and len(vals) > 3:
        n = len(vals)
        mu = sum(vals) / n
        var = sum((v - mu) ** 2 for v in vals) / (n - 1)
        return mu, math.sqrt(max(var, 1e-12))
    return None


def _realized_print(conn, series: str, period: str) -> float | None:
    """First-print label for the settled period via the registry's ALFRED sid."""
    from prediction_market_macro.config.registry import REGISTRY
    spec = REGISTRY.get(series)
    if spec is None or not spec.fred_first_release:
        return None
    sid = spec.fred_first_release
    if len(period) == 7:                              # monthly 'YYYY-MM'
        r = conn.execute(
            "SELECT value, MIN(knowledge_time) kt FROM fred_obs WHERE sid=?"
            " AND event_time LIKE ? GROUP BY event_time LIMIT 1",
            (sid, period + "%")).fetchone()
    else:                                             # claims: release-date period
        r = conn.execute(
            "SELECT value, MIN(knowledge_time) kt FROM fred_obs WHERE sid=?"
            " AND DATE(knowledge_time)=? GROUP BY event_time LIMIT 1",
            (sid, period)).fetchone()
    return float(r["value"]) if r and r["value"] is not None else None


def _mid(conn, ticker: str) -> float | None:
    r = conn.execute(
        "SELECT yes_bid, yes_ask FROM quotes WHERE ticker=? ORDER BY ts DESC LIMIT 1",
        (ticker,)).fetchone()
    if r is None:
        return None
    b, a = r["yes_bid"], r["yes_ask"]
    if b is not None and a is not None:
        return (b + a) / 2
    return a if a is not None else b


def mark_all(conn) -> int:
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for pos in open_positions(conn):
        for f in pos["fills"]:
            mid = _mid(conn, f["ticker"])
            if mid is None:
                continue
            val = mid if f["side"] == "yes" else 1 - mid
            pnl = (val - f["price"]) * f["count"] - f["fee_usd"]
            conn.execute(
                "INSERT OR REPLACE INTO marks(ts, decision_id, ticker, mid, pnl_usd)"
                " VALUES(?,?,?,?,?)",
                (now, pos["id"], f["ticker"], mid, round(pnl, 4)))
            n += 1
    conn.commit()
    return n


def settle_pass(conn) -> int:
    """For open positions whose every leg is settled: write a settle_note decision row
    with realized PnL (yes→result=='yes' pays 1)."""
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for pos in open_positions(conn):
        results = {}
        for f in pos["fills"]:
            r = conn.execute("SELECT result FROM settlements WHERE ticker=?",
                             (f["ticker"],)).fetchone()
            if r is None or r["result"] not in ("yes", "no"):
                results = None
                break
            results[f["ticker"]] = r["result"]
        if not results:
            continue
        realized = 0.0
        for f in pos["fills"]:
            won = (results[f["ticker"]] == f["side"])
            realized += ((1.0 if won else 0.0) - f["price"]) * f["count"] - f["fee_usd"]
        # z attribution: realized print under the open decision's own dist
        z, attribution = None, None
        try:
            ms = _dist_mu_sigma(json.loads(pos["inputs_json"] or "{}"))
            y = _realized_print(conn, pos["series"], pos["period"])
            if ms is not None and y is not None:
                z = (y - ms[0]) / ms[1]
                attribution = ("model_miss" if abs(z) > 2
                               else "luck_zone" if abs(z) < 1 else "gray_zone")
        except Exception:                             # noqa: BLE001
            pass
        note = f"settled realized={realized:+.4f}"
        if z is not None:
            note += f" z={z:+.2f} ({attribution})"
        conn.execute(
            "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair, ask,"
            " net_edge, size_usd, inputs_json, model_version, gate_snapshot, note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, pos["series"], pos["period"], pos["structure_json"], "settle_note",
             pos["fair"], pos["ask"], pos["net_edge"], round(realized, 4),
             json.dumps({"realized_usd": round(realized, 4), "results": results,
                         "z": round(z, 3) if z is not None else None,
                         "attribution": attribution}), pos["model_version"], "{}",
             note))
        n += 1
    conn.commit()
    return n


def report(conn) -> dict:
    rows = conn.execute(
        "SELECT series, COUNT(*) n, SUM(size_usd) staked FROM decisions WHERE kind='open'"
        " GROUP BY series").fetchall()
    # first settle_note per (series, period) only — historical duplicate rows from
    # the pre-fix open_positions bug stay in the append-only ledger but never count
    settled = conn.execute(
        "SELECT series, COUNT(*) n, SUM(size_usd) realized FROM ("
        "  SELECT series, period, size_usd, MIN(id) FROM decisions"
        "  WHERE kind='settle_note' GROUP BY series, period)"
        " GROUP BY series").fetchall()
    return {"open_by_series": [dict(r) for r in rows],
            "settled_by_series": [dict(r) for r in settled]}
