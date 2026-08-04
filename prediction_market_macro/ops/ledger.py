"""ops/ledger.py — the append-only PIT decision ledger (PLAN §12; frozen-ledger discipline).

decisions rows are NEVER updated; every state change is a NEW row (kind: open/exit/pass/
cancel/settle_note). Paper fills are written alongside opens at the depth-aware fill price
(strategy/edge.py::fill_price) with the exact taker fee — the same prices strategy/
decision.py sized the order against.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_macro.strategy.decision import Decision
from prediction_market_macro.strategy.edge import taker_fee



def record(conn, *, series: str, period: str, decision: Decision, pred_inputs: dict,
           model_version: str, note: str = "") -> int:
    now = datetime.now(timezone.utc).isoformat()
    st = decision.struct
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair, ask,"
        " net_edge, size_usd, inputs_json, model_version, gate_snapshot, note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, series, period,
         json.dumps({"kind": st.kind, "desc": st.desc,
                     "legs": [{"ticker": l.ticker, "side": l.side, "price": l.price}
                              for l in st.legs]} if st else {}),
         decision.action, st.fair if st else None, st.cost if st else None,
         st.net_edge() if st else None, decision.size_usd,
         json.dumps(pred_inputs, ensure_ascii=False), model_version,
         json.dumps(decision.gate_snapshot), note or ";".join(decision.reasons)))
    did = cur.lastrowid
    if decision.action == "open" and st is not None:
        # one shared fill model with strategy/decision.py, which sized `count` against
        # these same prices — a flat pad here (the old PAPER_SLIP) was invisible to sizing
        # and blew the $1 cap on cheap legs. See strategy/edge.py::fill_price.
        for leg, px in zip(st.legs, st.fill_prices(decision.count)):
            conn.execute(
                "INSERT INTO fills(decision_id, ts_utc, ticker, side, price, count, fee_usd,"
                " mode) VALUES(?,?,?,?,?,?,?, 'paper')",
                (did, now, leg.ticker, leg.side, px, decision.count,
                 taker_fee(px, decision.count)))
    conn.commit()
    return did


def has_open(conn, series: str, period: str) -> bool:
    r = conn.execute(
        "SELECT SUM(CASE WHEN kind='open' THEN 1 ELSE 0 END) o,"
        " SUM(CASE WHEN kind IN ('exit','cancel') THEN 1 ELSE 0 END) x"
        " FROM decisions WHERE series=? AND period=?", (series, period)).fetchone()
    return (r["o"] or 0) > (r["x"] or 0)


def open_positions(conn) -> list[dict]:
    """Open decisions (no matching exit/cancel/settle) with their paper fills.
    settle_note counts as closed — otherwise settle_pass re-settles (and marks keep
    marking) finished positions forever."""
    rows = conn.execute(
        "SELECT d.* FROM decisions d WHERE d.kind IN ('open','argmax','arb','snipe')"
        " AND NOT EXISTS"
        " (SELECT 1 FROM decisions e WHERE e.series=d.series AND e.period=d.period"
        "  AND e.kind IN ('exit','cancel','settle_note') AND e.id>d.id)").fetchall()
    out = []
    for d in rows:
        fills = conn.execute("SELECT * FROM fills WHERE decision_id=?", (d["id"],)).fetchall()
        out.append({**dict(d), "fills": [dict(f) for f in fills]})
    return out
