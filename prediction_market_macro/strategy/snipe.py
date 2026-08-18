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
    # #149 — same one-stream-numerator / all-stream-denominator bug as `_has_open_arb`.
    from prediction_market_macro.ops.ledger import open_decisions
    return any(d["kind"] == "snipe" for d in open_decisions(conn, series, period))


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
        stake = round(price * count, 4)
        # #151/F8. The edge and argmax paths both clear `risk.check` before they open;
        # this one wrote straight to the ledger, so a snipe was subject to no cap but its
        # own MAX_SNIPE_USD. That is reachable, not hypothetical: this path is bounded at
        # $2 per (series, period) by `_has_open_snipe`, and the edge stream can already be
        # holding up to the $5 per-event limit on the same period — the sum clears it.
        # A snipe is directional (it buys the leg the realised print implies), so its max
        # loss IS its stake and the caps apply to it unmodified. Deliberately NOT extended
        # to `arb.execute`: an arb's payoff floor is >= its cost, so its max loss is not
        # its stake, and capping a locked-profit structure by a directional loss limit is
        # a different question — recorded in PLAN_EXTENSION §25.22, not decided here.
        # Uncommitted inserts are visible on this connection, so a second leg in the same
        # loop is checked against the first.
        from prediction_market_macro.ops import risk as _risk
        if _risk.check(conn, series, period_key, stake) is not None:
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
             "snipe", 1.0, price, round(net, 4), stake,
             json.dumps({"print": y, "net": net, "count": count}),
             "snipe/1.0", "{}", note))
        from prediction_market_macro.ops import trading_kalshi
        curf = conn.execute(
            "INSERT INTO fills(decision_id, ts_utc, ticker, side, price, count, fee_usd,"
            " mode) VALUES(?,?,?,?,?,?,?, 'paper')",
            (cur.lastrowid, now, l["ticker"], side, price, count,
             taker_fee(price, count)))
        trading_kalshi.on_fill(conn, curf.lastrowid)   # §30.3 inline mirror
        conn.execute(
            "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
            (now, "info", "snipe", f"{series}/{period_key}: {note} x{count}"))
        total_usd += price * count
        n += 1
        if total_usd >= MAX_SNIPE_USD:
            break
    conn.commit()
    return n
