"""ops/pnl.py — marks + settlement reconciliation (PLAN §12).

mark_all: open paper positions marked to latest orderbook mid.
settle_pass: settled contracts → realized PnL rows. z-score attribution
(model vs luck) is planned (PLAN_EXTENSION §22 #29) and lands with the
error-attribution loop — not implemented here yet.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_macro.ops.ledger import open_positions


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
        conn.execute(
            "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair, ask,"
            " net_edge, size_usd, inputs_json, model_version, gate_snapshot, note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, pos["series"], pos["period"], pos["structure_json"], "settle_note",
             pos["fair"], pos["ask"], pos["net_edge"], round(realized, 4),
             json.dumps({"realized_usd": round(realized, 4),
                         "results": results}), pos["model_version"], "{}",
             f"settled realized={realized:+.4f}"))
        n += 1
    conn.commit()
    return n


def report(conn) -> dict:
    rows = conn.execute(
        "SELECT series, COUNT(*) n, SUM(size_usd) staked FROM decisions WHERE kind='open'"
        " GROUP BY series").fetchall()
    settled = conn.execute(
        "SELECT series, COUNT(*) n, SUM(size_usd) realized FROM decisions"
        " WHERE kind='settle_note' GROUP BY series").fetchall()
    return {"open_by_series": [dict(r) for r in rows],
            "settled_by_series": [dict(r) for r in settled]}
