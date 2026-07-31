"""ops/exits.py — position exit policy (PLAN §11 exit.py; mother smart_exit's macro twin).

Default: hold to settlement. Early exit only when:
  1. edge reversal: holding edge (fair - current mid) < -0.06 with exit-side depth
  2. red-light: series health-degraded (health table lands M4; hook ready via alerts)
Freeze window (10 min pre-release) blocks exits too. Every exit = new ledger rows
(kind='exit') + paper fills at mid - $0.01 slippage; append-only always.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.ops.ledger import open_positions
from prediction_market_macro.strategy.edge import taker_fee

EXIT_EDGE = -0.06
SLIP = 0.01


def _quote(conn, ticker):
    return conn.execute(
        "SELECT yes_bid, yes_ask, bid_depth, ask_depth FROM quotes WHERE ticker=?"
        " ORDER BY ts DESC LIMIT 1", (ticker,)).fetchone()


def run(conn, settings) -> int:
    now = datetime.now(timezone.utc)
    n = 0
    for pos in open_positions(conn):
        spec = REGISTRY.get(pos["series"])
        if spec is None:
            continue
        rel = conn.execute("SELECT scheduled_ts FROM releases WHERE cal=? AND period=?",
                           (spec.calendar, pos["period"])).fetchone()
        if rel:
            dt_min = (datetime.fromisoformat(rel["scheduled_ts"]) - now).total_seconds() / 60
            if 0 <= dt_min <= 10:
                continue                                    # freeze window: no exits
        pr = conn.execute(
            "SELECT ladder_json FROM preds WHERE series=? AND period=?"
            " ORDER BY asof DESC LIMIT 1", (pos["series"], pos["period"])).fetchone()
        if pr is None or not pr["ladder_json"]:
            continue
        pmf = {float(k): v for k, v in json.loads(pr["ladder_json"]).items()}
        from prediction_market_macro.model.common import survival
        hold_edges, legs_exit = [], []
        for f in pos["fills"]:
            c = conn.execute("SELECT floor_strike, strike_type FROM contracts WHERE ticker=?",
                             (f["ticker"],)).fetchone()
            q = _quote(conn, f["ticker"])
            if c is None or q is None or c["floor_strike"] is None:
                hold_edges = []
                break
            strict = (c["strike_type"] == "greater")
            fair_yes = survival(pmf, float(c["floor_strike"]), strict=strict)
            fair = fair_yes if f["side"] == "yes" else 1 - fair_yes
            if q["yes_bid"] is None or q["yes_ask"] is None:
                hold_edges = []
                break
            mid = (q["yes_bid"] + q["yes_ask"]) / 2
            mid_side = mid if f["side"] == "yes" else 1 - mid
            hold_edges.append(fair - mid_side)
            exit_px = (q["yes_bid"] if f["side"] == "yes" else 1 - q["yes_ask"])
            depth = q["bid_depth"] if f["side"] == "yes" else q["ask_depth"]
            legs_exit.append((f, max(exit_px - SLIP, 0.01), depth))
        if not hold_edges:
            continue
        worst = min(hold_edges)
        if worst >= EXIT_EDGE or any(d < 20 for _, _, d in legs_exit):
            continue
        ts = now.isoformat()
        cur = conn.execute(
            "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair, ask,"
            " net_edge, size_usd, inputs_json, model_version, gate_snapshot, note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, pos["series"], pos["period"], pos["structure_json"], "exit",
             None, None, worst, pos["size_usd"], json.dumps({"hold_edges": hold_edges}),
             pos["model_version"], "{}", f"edge_reversal worst={worst:.4f}"))
        did = cur.lastrowid
        for f, px, _ in legs_exit:
            conn.execute(
                "INSERT INTO fills(decision_id, ts_utc, ticker, side, price, count, fee_usd,"
                " mode) VALUES(?,?,?,?,?,?,?, 'paper')",
                (did, ts, f["ticker"], f"close_{f['side']}", px, f["count"],
                 taker_fee(px, f["count"])))
        n += 1
    conn.commit()
    return n
