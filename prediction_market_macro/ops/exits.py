"""ops/exits.py — position exit policy (PLAN §11 exit.py; mother smart_exit's macro twin).

Default: hold to settlement. Early exit only when:
  1. edge reversal: holding edge (fair - current mid) < -0.06 with exit-side depth
  2. red-light: series circuit breaker tripped (health red / ledger mismatch /
     rolling-20 drawdown) → FORCED exit at market regardless of edge (铁律 10)
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


def _write_exit(conn, pos, ts: str, worst, legs_exit, note: str) -> None:
    # realized PnL of the early close: exit proceeds − entry cost − both fees.
    # Stored in inputs_json so the rolling-20 drawdown breaker and reports see
    # exit losses, not just settlement losses.
    realized = 0.0
    for f, px, _ in legs_exit:
        realized += (px - f["price"]) * f["count"] \
            - taker_fee(px, f["count"]) - (f["fee_usd"] or 0.0)
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair, ask,"
        " net_edge, size_usd, inputs_json, model_version, gate_snapshot, note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, pos["series"], pos["period"], pos["structure_json"], "exit",
         None, None, worst, pos["size_usd"],
         json.dumps({"exit_note": note, "realized_usd": round(realized, 4)}),
         pos["model_version"], "{}",
         f"{note} realized={realized:+.4f}"))
    did = cur.lastrowid
    for f, px, _ in legs_exit:
        conn.execute(
            "INSERT INTO fills(decision_id, ts_utc, ticker, side, price, count, fee_usd,"
            " mode) VALUES(?,?,?,?,?,?,?, 'paper')",
            (did, ts, f["ticker"], f"close_{f['side']}", px, f["count"],
             taker_fee(px, f["count"])))


def run(conn, settings) -> int:
    from prediction_market_macro.ops import risk
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

        # rule 2 — red light: breaker tripped ⇒ forced market exit, no edge check
        trip = risk.breaker_tripped(conn, pos["series"])
        if trip:
            legs_exit, ok = [], True
            for f in pos["fills"]:
                q = _quote(conn, f["ticker"])
                if q is None or q["yes_bid"] is None or q["yes_ask"] is None:
                    ok = False
                    break
                exit_px = (q["yes_bid"] if f["side"] == "yes" else 1 - q["yes_ask"])
                depth = q["bid_depth"] if f["side"] == "yes" else q["ask_depth"]
                legs_exit.append((f, max(exit_px - SLIP, 0.01), depth))
            if ok and legs_exit:
                _write_exit(conn, pos, now.isoformat(), None, legs_exit,
                            f"health_red_forced_exit [{trip[:120]}]")
                n += 1
            continue

        # rule 1 — edge reversal against the CURRENT model. Ladder series price
        # via the latest ladder pmf; CATEGORICAL series (Fed) via the latest
        # probs — they were previously never re-evaluated at all (blind spot:
        # the deep-OTM Fed lottery legs from the pre-gate era just sat there).
        pr = conn.execute(
            "SELECT ladder_json, dist_json FROM preds WHERE series=? AND period=?"
            " ORDER BY asof DESC LIMIT 1", (pos["series"], pos["period"])).fetchone()
        if pr is None:
            continue
        pmf = ({float(k): v for k, v in json.loads(pr["ladder_json"]).items()}
               if pr["ladder_json"] else None)
        probs = None
        if pmf is None:
            d0 = json.loads(pr["dist_json"] or "{}")
            probs = d0.get("probs") if isinstance(d0.get("probs"), dict) else None
            if probs is None:
                continue
        from prediction_market_macro.model.common import leg_fair
        hold_edges, legs_exit = [], []
        for f in pos["fills"]:
            c = conn.execute(
                "SELECT floor_strike, cap_strike, strike_type FROM contracts"
                " WHERE ticker=?", (f["ticker"],)).fetchone()
            q = _quote(conn, f["ticker"])
            if q is None:
                hold_edges = []
                break
            base_side = f["side"].replace("close_", "")
            if probs is not None:                        # categorical leg
                cat = f["ticker"].rsplit("-", 1)[-1]
                fair_yes = float(probs.get(cat, 0.0))
            else:
                if c is None or (c["floor_strike"] is None
                                 and c["cap_strike"] is None):
                    hold_edges = []
                    break
                # the leg's OWN strike metadata — between buckets and less-type
                # legs priced correctly, not as bare survival(floor)
                try:
                    fair_yes = leg_fair(pmf, c["strike_type"] or "greater",
                                        c["floor_strike"], c["cap_strike"])
                except Exception:                         # noqa: BLE001
                    hold_edges = []
                    break
            fair = fair_yes if base_side == "yes" else 1 - fair_yes
            if q["yes_bid"] is None or q["yes_ask"] is None:
                hold_edges = []
                break
            mid = (q["yes_bid"] + q["yes_ask"]) / 2
            mid_side = mid if base_side == "yes" else 1 - mid
            hold_edges.append(fair - mid_side)
            exit_px = (q["yes_bid"] if base_side == "yes" else 1 - q["yes_ask"])
            depth = q["bid_depth"] if base_side == "yes" else q["ask_depth"]
            legs_exit.append((f, max(exit_px - SLIP, 0.01), depth))
        if not hold_edges:
            continue
        worst = min(hold_edges)
        # rule 3 — regime review: a position that today's structural gates would
        # REFUSE to open (penny-lottery entry) and whose CURRENT model sees no
        # positive holding edge is dead capital — release it while any meaningful
        # value remains (recoverable ≥ 2c/contract; below that fees eat the exit)
        from prediction_market_macro.strategy.decision import GATES as _G
        regime_bad = (any(f["price"] < _G.get("min_leg_price", 0.10)
                          for f in pos["fills"])
                      and worst < 0.0
                      and all(px >= 0.02 for _, px, _ in legs_exit))
        if regime_bad:
            _write_exit(conn, pos, now.isoformat(), worst, legs_exit,
                        f"regime_review_exit worst={worst:.4f} (penny-entry,"
                        f" no current edge)")
            n += 1
            continue
        if worst >= EXIT_EDGE or any(d < 20 for _, _, d in legs_exit):
            continue
        _write_exit(conn, pos, now.isoformat(), worst, legs_exit,
                    f"edge_reversal worst={worst:.4f}")
        n += 1
    conn.commit()
    return n
