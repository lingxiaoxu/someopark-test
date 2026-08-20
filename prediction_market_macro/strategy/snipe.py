"""strategy/snipe.py — post-print sniper (PLAN_EXTENSION §24-B; §19-9 realized).

Macro's unfair advantage over event markets: at T+minutes the print is a PUBLIC
CERTAINTY (BLS/BEA/DOL publish at 08:30 sharp) while Kalshi quotes converge over
minutes. Any leg whose settlement is already determined by the published first
print but still trades away from $1/$0 is near-riskless.

**That premise does not hold on Kalshi, for any series in SNIPE_SERIES.** All six
close their books 1-5 minutes BEFORE the 08:30 ET print (see
`_book_shut_before_print` for the measured table) — Kalshi settles on a book that is
already shut when the number lands, so the post-print window this module trades does
not exist. The stream has opened zero positions since it was wired in c3d33e0
(2026-07-31) and that is the reason; the FRED-ingestion lag found alongside it is a
real second blocker but a redundant one. Left wired and instrumented rather than
deleted: the close-time check is per-book, so the day Kalshi lists a macro series
that closes after its release, this fires as designed.

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


def _alert_once(conn, msg: str, level: str = "info") -> None:
    """'snipe'-source alert, at most once per UTC day per exact message.
    Mirrors `arb._alert_once`; same reason — this module's blocking branches were
    silent, so "the sniper has never fired" had no answer outside a code read."""
    now = datetime.now(timezone.utc)
    dup = conn.execute(
        "SELECT 1 FROM alerts WHERE source='snipe' AND message=? AND ts>=?",
        (msg, now.date().isoformat())).fetchone()
    if dup:
        return
    conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                 (now.isoformat(), level, "snipe", msg))
    conn.commit()


def _book_shut_before_print(conn, series: str, kalshi_tok: str) -> str | None:
    """Return an explanatory message when this book closes at/before the release.

    §24-B rests on one premise: "the print is a PUBLIC CERTAINTY at T+minutes while
    Kalshi quotes converge over minutes". For these six series that premise is FALSE,
    and it is false by contract metadata, not by luck of timing. Measured 2026-08-20
    over `contracts.close_time` against the 12:30Z (08:30 ET) print:

        KXJOBLESSCLAIMS  close 12:25Z   print 12:30Z    -5 min
        KXCPI            close 12:25Z   print 12:30Z    -5 min
        KXCPICORE        close 12:25Z   print 12:30Z    -5 min
        KXPCECORE        close 12:25Z   print 12:30Z    -5 min
        KXU3             close 12:29Z   print 12:30Z    -1 min
        KXPAYROLLS       close 12:29Z   print 12:30Z    -1 min

    Kalshi settles these on a book it shuts BEFORE the number lands — there is no
    post-print window to snipe, for any of them. The reassess task fires at T+3m
    (12:33Z), which is 4 to 8 minutes after the last possible trade. So the sniper has
    never fired because it CANNOT fire, and no amount of faster ingestion changes that.

    Kept wired rather than deleted: the check is per-book and cheap, the module is
    correct for any series Kalshi ever lists with a post-print close, and a rule that
    explains its own silence every morning is worth more than a deleted file plus a
    note. If a close_time ever lands after the release this returns None and §24-B
    runs as designed."""
    r = conn.execute(
        "SELECT MIN(close_time) ct FROM contracts WHERE series=? AND period=?",
        (series, kalshi_tok)).fetchone()
    if r is None or not r["ct"]:
        return None                               # unknown close — do not invent a wall
    close = datetime.fromisoformat(r["ct"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    if now < close:
        return None                               # still open; §24-B premise holds
    return (f"SNIPE-BOOK-CLOSED {series}/{kalshi_tok}: book closed"
            f" {close.isoformat()} and the sniper looked {now.isoformat()}"
            f" ({(now - close).total_seconds() / 60:.0f} min late). These series close"
            f" 1-5 min BEFORE the 08:30 ET print, so §24-B's post-print window does not"
            f" exist — this stream is structurally unfireable, not merely unlucky.")


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
        # No ACTIVE contract for the period the reassess task just handed us. Given the
        # close-time arithmetic below this is the expected outcome, not an oddity, so it
        # gets a line rather than a silent return.
        _alert_once(conn, f"SNIPE-NO-ACTIVE-BOOK {series}/{period_key}: reassess reached"
                          f" the sniper but no contract for this period is still active"
                          f" — the book shut before the print (see SNIPE-BOOK-CLOSED)")
        return 0
    shut = _book_shut_before_print(conn, series, kalshi_tok)
    if shut is not None:
        _alert_once(conn, shut, "warn")
        return 0
    y = _realized_print(conn, series, period_key)
    if y is None:
        # Not a quiet "no guessing" — this is the second wall, and it is the one that
        # was mistaken for the whole story. `_realized_print` reads `fred_obs`, and the
        # only writer of first-release rows is `fred.pull_core()` from ops/refresh.py at
        # 09:00Z. `fred_obs.first_seen_ts` puts every one of the live window's prints in
        # the DB the NEXT MORNING: ICSA's 2026-08-13T12:30Z print was first seen at
        # 2026-08-14T09:00:40Z, CPIAUCSL's 2026-08-12 print at 2026-08-13T09:00:07Z —
        # ~20.5h after the sniper looked. FRED itself serves the number the same day
        # (checked live 2026-08-20: the 08-15 claims week was already on the wire at
        # 19:14Z), so this half IS fixable with an inline pull. Not doing it, because the
        # wall above makes it moot — but alerting so the alert log, not a code read,
        # says which of the two binds on any given morning.
        _alert_once(conn, f"SNIPE-NO-PRINT {series}/{period_key}: book still open but the"
                          f" first print is not ingested — fred_obs is written by the"
                          f" 09:00Z refresh, ~20.5h after the T+3m look")
        return 0
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
        # Two caps in two different units — same fix as strategy/arb.py, same bug. This
        # was `usd = min(MAX_SNIPE_USD - total_usd, DEPTH_FRAC * depth)` followed by
        # `int(usd / price)`, which divided a CONTRACT COUNT by $/contract and so
        # inflated the 铁律 5 depth cap by 1/price: on a 0.20 leg it would take five
        # times 20% of the book. The remaining-budget half is a genuine dollar amount
        # and stays one; only the depth half moves to a contract count.
        count = min(int((MAX_SNIPE_USD - total_usd) / max(price, 0.01)),
                    int(DEPTH_FRAC * depth))
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
