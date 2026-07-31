"""strategy/snipe.py — post-print sniper (PLAN_EXTENSION §24-B; §19-9 realized).

Macro's unfair advantage over event markets: at T+minutes the print is a PUBLIC
CERTAINTY (BLS/BEA/DOL publish at 08:30 sharp) while Kalshi quotes converge over
minutes. Any leg whose settlement is already determined by the published first
print but still trades away from $1/$0 is near-riskless.

Safety rails:
  * only series whose label is directly in contract units AND whose first print
    is already ingested (knowledge_time <= now) — no guessing, no scraping races
  * boundary guard: legs within half a settlement grid step of the print are
    SKIPPED (rounding-convention ambiguity is exactly where 铁律 2 bites)
  * max price 0.95 for the certain side (below that the fee eats the edge)
  * $ cap per (series, period), depth-capped, kind='snipe' first-class ledger
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_macro.strategy.edge import taker_fee

MAX_SNIPE_USD = 2.0
MAX_PRICE = 0.95          # certain side must cost less than this
DEPTH_FRAC = 0.20
BOUNDARY_FRAC = 0.5       # skip legs within 0.5 grid steps of the print

# labels usable as settlement certainties (same set as health's settle fuse +
# the unit-transformed monthlies — pnl._realized_print returns contract units)
SNIPE_SERIES = ("KXJOBLESSCLAIMS", "KXU3", "KXCPI", "KXCPICORE", "KXPCECORE",
                "KXPAYROLLS")


def _has_open_snipe(conn, series: str, period: str) -> bool:
    r = conn.execute(
        "SELECT SUM(CASE WHEN kind='snipe' THEN 1 ELSE 0 END) s,"
        " SUM(CASE WHEN kind IN ('exit','cancel','settle_note') THEN 1 ELSE 0 END) x"
        " FROM decisions WHERE series=? AND period=?", (series, period)).fetchone()
    return (r["s"] or 0) > (r["x"] or 0)


def run_for(conn, series: str, period_key: str) -> int:
    """Called from the tick reassess task (T+3m, quotes just densified).
    Returns snipes opened."""
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.ops.decide_all import _legs_meta
    from prediction_market_macro.ops.pnl import _realized_print
    from prediction_market_macro.research.health import _leg_expected
    from prediction_market_macro.util.periods import kalshi_period_to_key
    spec = REGISTRY.get(series)
    if spec is None or series not in SNIPE_SERIES:
        return 0
    if _has_open_snipe(conn, series, period_key):
        return 0
    kalshi_tok = next(
        (r["period"] for r in conn.execute(
            "SELECT DISTINCT period FROM contracts WHERE series=? AND status='active'",
            (series,)).fetchall()
         if kalshi_period_to_key(r["period"]) == period_key), None)
    if kalshi_tok is None:
        return 0
    y = _realized_print(conn, series, period_key)
    if y is None:
        return 0                                  # print not ingested yet — no guessing
    legs = _legs_meta(conn, series, kalshi_tok)
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    total_usd = 0.0
    for l in legs:
        strike = l.get("strike") if l.get("strike") is not None else l.get("cap_strike")
        if strike is None:
            continue
        if abs(y - float(strike)) < BOUNDARY_FRAC * spec.round_rule:
            continue                              # rounding boundary — 铁律 2 territory
        expected = _leg_expected(y, l.get("strike_type"), l.get("strike"),
                                 l.get("cap_strike"), spec.strict_gt)
        if expected is None:
            continue
        if expected == "yes" and l.get("yes_ask") is not None \
                and 0 < l["yes_ask"] <= MAX_PRICE:
            side, price, depth = "yes", l["yes_ask"], l.get("ask_depth") or 0
        elif expected == "no" and l.get("yes_bid") is not None \
                and 0 < (1 - l["yes_bid"]) <= MAX_PRICE:
            side, price, depth = "no", round(1 - l["yes_bid"], 4), l.get("bid_depth") or 0
        else:
            continue
        net = 1.0 - price - taker_fee(price, 1)
        if net < 0.01:
            continue
        usd = min(MAX_SNIPE_USD - total_usd, DEPTH_FRAC * depth)
        count = int(usd / max(price, 0.01))
        if count < 1:
            continue
        note = f"SNIPE {l['ticker'].rsplit('-', 1)[-1]}:{side} print={y} net={net:.3f}"
        cur = conn.execute(
            "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair,"
            " ask, net_edge, size_usd, inputs_json, model_version, gate_snapshot, note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, series, period_key,
             json.dumps({"kind": "snipe", "desc": note,
                         "legs": [{"ticker": l["ticker"], "side": side,
                                   "price": price}]}),
             "snipe", 1.0, price, round(net, 4), round(price * count, 4),
             json.dumps({"print": y, "net": net, "count": count}),
             "snipe/1.0", "{}", note))
        conn.execute(
            "INSERT INTO fills(decision_id, ts_utc, ticker, side, price, count, fee_usd,"
            " mode) VALUES(?,?,?,?,?,?,?, 'paper')",
            (cur.lastrowid, now, l["ticker"], side, price, count,
             taker_fee(price, count)))
        conn.execute(
            "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
            (now, "info", "snipe", f"{series}/{period_key}: {note} x{count}"))
        total_usd += price * count
        n += 1
        if total_usd >= MAX_SNIPE_USD:
            break
    conn.commit()
    return n
