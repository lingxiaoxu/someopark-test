"""strategy/arb.py — execute detected riskless arbitrage (PLAN_EXTENSION §24-A).

consistency/devig already DETECT the free money every snapshot; this module finally
trades it (paper). Zero model risk — payoffs are locked by construction:

  monotone   ask(lo strike) < bid(hi strike):
             BUY YES(lo)@ask + BUY NO(hi)@(1-bid); cost = 1 - gross,
             payoff ∈ {1 (outside), 2 (between)} ⇒ ≥ 1 always
  partition  bucket book sums wrong:
             buy_all_yes: cost Σask < 1, exactly one bucket pays 1
             buy_all_no:  cost Σ(1-bid) < n-1, exactly n-1 legs pay 1

Discipline: fee-aware (net after taker fees must clear MIN_NET), depth-capped
(铁律 5: ≤20% of the thinnest leg), $ cap per arb, one open arb per (series,
period), ledger kind='arb' (first-class: marks/settle/risk all see it).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_macro.strategy.edge import taker_fee

MAX_ARB_USD = 2.0
MIN_NET = 0.005          # per contract, after fees
DEPTH_FRAC = 0.20


def _has_open_arb(conn, series: str, period: str) -> bool:
    # #149. Was `count(kind='arb') > count(close of ANY kind)` — one stream's opens
    # against every stream's closes. It disagrees with the truth on 0 of the 66 live
    # periods today, but only because no arb has yet been held through an edge or argmax
    # close. The SQL was as wrong as `has_open`'s, which did produce a duplicate position.
    from prediction_market_macro.ops.ledger import open_decisions
    return any(d["kind"] == "arb" for d in open_decisions(conn, series, period))


def _record(conn, series: str, period: str, legs: list[dict], gross: float,
            net: float, count: int, note: str) -> int:
    """legs: [{ticker, side, price, depth}]. Paper fills at price (arb prices are the
    quoted ask/1-bid — no extra slippage model; the fee is the realism)."""
    now = datetime.now(timezone.utc).isoformat()
    cost = sum(l["price"] for l in legs)
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair, ask,"
        " net_edge, size_usd, inputs_json, model_version, gate_snapshot, note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, series, period,
         json.dumps({"kind": "arb", "desc": note,
                     "legs": [{"ticker": l["ticker"], "side": l["side"],
                               "price": l["price"]} for l in legs]}),
         "arb", None, round(cost, 4), round(net, 4), round(cost * count, 4),
         json.dumps({"gross": gross, "net": net, "count": count}),
         "arb/1.0", "{}", note))
    did = cur.lastrowid
    for l in legs:
        conn.execute(
            "INSERT INTO fills(decision_id, ts_utc, ticker, side, price, count, fee_usd,"
            " mode) VALUES(?,?,?,?,?,?,?, 'paper')",
            (did, now, l["ticker"], l["side"], l["price"], count,
             taker_fee(l["price"], count)))
    conn.commit()
    return did


def execute(conn, series: str, period_key: str, legs_meta: list[dict],
            violations: list[dict]) -> int:
    """Called by decide_all with the same legs + violations it already computed.
    Returns arbs opened (0 or 1 — one per (series, period) at a time)."""
    if not violations or _has_open_arb(conn, series, period_key):
        return 0
    by_ticker = {l["ticker"]: l for l in legs_meta}

    best = None                                   # (net, legs, gross, note)
    best_reject = None                            # (net, gross, cost, fees) of best loser
    for v in violations:
        if "buy" in v:                            # monotone: YES(lo) + NO(hi)
            lo, hi = v["buy"], v["sell"]
            lo_m, hi_m = by_ticker.get(lo["ticker"]), by_ticker.get(hi["ticker"])
            if lo_m is None or hi_m is None:
                continue
            legs = [{"ticker": lo["ticker"], "side": "yes",
                     "price": lo["yes_ask"], "depth": lo_m.get("ask_depth") or 0},
                    {"ticker": hi["ticker"], "side": "no",
                     "price": round(1 - hi["yes_bid"], 4),
                     "depth": hi_m.get("bid_depth") or 0}]
            payoff_min = 1.0
        elif v.get("kind") == "buy_all_yes":
            legs = [{"ticker": l["ticker"], "side": "yes", "price": l["yes_ask"],
                     "depth": l.get("ask_depth") or 0}
                    for l in legs_meta if l.get("yes_ask") is not None]
            payoff_min = 1.0
        elif v.get("kind") == "buy_all_no":
            legs = [{"ticker": l["ticker"], "side": "no",
                     "price": round(1 - l["yes_bid"], 4),
                     "depth": l.get("bid_depth") or 0}
                    for l in legs_meta if l.get("yes_bid") is not None]
            payoff_min = float(len(legs) - 1)
        else:
            continue
        if not legs or any(not (0 < l["price"] < 1) for l in legs):
            continue
        cost = sum(l["price"] for l in legs)
        fees = sum(taker_fee(l["price"], 1) for l in legs)
        net = payoff_min - cost - fees            # locked minimum profit / contract
        if best_reject is None or net > best_reject[0]:
            best_reject = (net, v["gross"], cost, fees)
        if net < MIN_NET:
            continue
        if best is None or net > best[0]:
            best = (net, legs, v["gross"], f"ARB {'+'.join(l['ticker'].rsplit('-', 1)[-1] + ':' + l['side'] for l in legs)} net={net:.3f}")
    if best is None:
        # Every detected violation died at the fee gate. Say so, once per
        # (series, period, day) — the 2026-08-10 audit found 9 MONOTONE-ARB alerts
        # against 0 arb decisions and could not tell "fees always bind" from "the
        # detect→execute path is broken"; replaying the books showed gross 1-2c vs a
        # 2-leg fee wedge of 2-4c (ceil to the cent per leg). This alert keeps the
        # two cases distinguishable forever without a replay.
        if best_reject is not None:
            net_r, gross_r, cost_r, fees_r = best_reject
            msg = (f"ARB-REJECTED {series}/{period_key}: best net {net_r:+.3f}/contract"
                   f" < {MIN_NET} (gross {gross_r:.3f}, cost {cost_r:.3f},"
                   f" fees {fees_r:.3f}) — fee gate, path alive")
            day = datetime.now(timezone.utc).date().isoformat()
            dup = conn.execute(
                "SELECT 1 FROM alerts WHERE source='arb' AND message=? AND ts>=?",
                (msg, day)).fetchone()
            if not dup:
                conn.execute(
                    "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(), "info", "arb", msg))
                conn.commit()
        return 0
    net, legs, gross, note = best
    depth_cap = DEPTH_FRAC * min(l["depth"] for l in legs)
    cost1 = sum(l["price"] for l in legs)
    usd = min(MAX_ARB_USD, depth_cap)
    count = int(usd / max(cost1, 0.01))
    if count < 1:
        return 0
    _record(conn, series, period_key, legs, gross, net, count, note)
    conn.execute(
        "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), "info", "arb",
         f"{series}/{period_key}: {note} x{count}"))
    conn.commit()
    return 1
