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
from datetime import datetime, timedelta, timezone

from prediction_market_macro.ops.ledger import open_positions
from prediction_market_macro.strategy.edge import WIDE_SPREAD, two_sided


def _dist_mu_sigma(dist: dict) -> tuple[float, float] | None:
    comps = dist.get("comps")
    if comps:
        w = [c[0] for c in comps]
        mu = sum(ci[0] * ci[1] for ci in comps)
        var = sum(ci[0] * (ci[2] ** 2 + (ci[1] - mu) ** 2) for ci in comps)
        return mu, math.sqrt(max(var, 1e-12))
    vals = dist.get("values") or dist.get("quantiles")   # Empirical emits quantiles
    if vals and len(vals) > 3:
        finite = [v for v in vals if v is not None and math.isfinite(v)]
        if len(finite) < 4:
            return None
        n = len(finite)
        mu = sum(finite) / n
        var = sum((v - mu) ** 2 for v in finite) / (n - 1)
        return mu, math.sqrt(max(var, 1e-12))
    return None


def _first_print_value(conn, sid: str, event_like: str) -> float | None:
    r = conn.execute(
        "SELECT value, MIN(knowledge_time) FROM fred_obs WHERE sid=?"
        " AND event_time LIKE ? GROUP BY event_time LIMIT 1",
        (sid, event_like + "%")).fetchone()
    return float(r["value"]) if r and r["value"] is not None else None


def _same_vintage_pair(conn, sid: str, month: str, prev: str) -> tuple[float, float] | None:
    """(value_month, value_prev) BOTH read from month's first-print vintage — the
    published MoM% divides same-vintage levels (SA factors revise annually, so
    mixing vintages can shift MoM by a grid step)."""
    v = conn.execute(
        "SELECT vintage_date, MIN(knowledge_time) FROM fred_obs WHERE sid=?"
        " AND event_time LIKE ? GROUP BY event_time LIMIT 1",
        (sid, month + "%")).fetchone()
    if v is None:
        return None
    vd = v["vintage_date"]
    a = conn.execute(
        "SELECT value FROM fred_obs WHERE sid=? AND event_time LIKE ?"
        " AND vintage_date=?", (sid, month + "%", vd)).fetchone()
    b = conn.execute(
        "SELECT value FROM fred_obs WHERE sid=? AND event_time LIKE ?"
        " AND vintage_date<=? ORDER BY vintage_date DESC LIMIT 1",
        (sid, prev + "%", vd)).fetchone()
    if a is None or b is None or a["value"] is None or b["value"] is None:
        return None
    return float(a["value"]), float(b["value"])


def _prev_month(period: str, k: int = 1) -> str:
    y, m = int(period[:4]), int(period[5:7])
    m -= k
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


def _realized_print(conn, series: str, period: str) -> float | None:
    """First-print label for the settled period, in the CONTRACT'S unit (the
    registry sid may be an index level — %mom/%yoy/k_jobs need transformation).
    Returns None when no honest label exists in contract units."""
    from prediction_market_macro.config.registry import REGISTRY
    spec = REGISTRY.get(series)
    if spec is None or not spec.fred_first_release:
        return None
    sid = spec.fred_first_release
    if len(period) == 7:                              # monthly 'YYYY-MM'
        if spec.unit == "%mom":
            pair = _same_vintage_pair(conn, sid, period, _prev_month(period))
            if pair is None:
                a = _first_print_value(conn, sid, period)
                b = _first_print_value(conn, sid, _prev_month(period))
                pair = (a, b) if a and b else None
            return round((pair[0] / pair[1] - 1) * 100, 4) if pair else None
        if spec.unit == "%yoy":
            a = _first_print_value(conn, sid, period)
            b = _first_print_value(conn, sid, _prev_month(period, 12))
            return round((a / b - 1) * 100, 4) if a and b else None
        if spec.unit == "k_jobs":                     # PAYEMS level → change in jobs
            a = _first_print_value(conn, sid, period)
            b = _first_print_value(conn, sid, _prev_month(period))
            return round((a - b) * 1000, 1) if a is not None and b is not None else None
        return _first_print_value(conn, sid, period)  # pct levels (U3, rates)
    # claims: period = RELEASE date. Pick the event whose true first print landed
    # that day — filter on the aggregated MIN(kt), never on raw rows (a same-day
    # REVISION of the prior week would otherwise masquerade as the print)
    r = conn.execute(
        "SELECT event_time, value, MIN(knowledge_time) kt FROM fred_obs WHERE sid=?"
        " GROUP BY event_time HAVING DATE(kt)=? LIMIT 1", (sid, period)).fetchone()
    return float(r["value"]) if r and r["value"] is not None else None


def _mid(conn, ticker: str) -> float | None:
    """Midpoint of the newest quote, or None when the book cannot support one.

    Used to return `(bid+ask)/2` for ANY two numbers, and to fall back to whichever side
    existed when one was missing. Both fabricate a price out of a book nobody is making:
    see strategy/edge.py::two_sided for the measurement. Callers must treat None as
    "unmarked", never as zero.
    """
    r = conn.execute(
        "SELECT yes_bid, yes_ask FROM quotes WHERE ticker=? ORDER BY ts DESC LIMIT 1",
        (ticker,)).fetchone()
    if r is None:
        return None
    b, a = r["yes_bid"], r["yes_ask"]
    return (b + a) / 2 if two_sided(b, a) else None


def mark_all(conn) -> int:
    """Mark open paper legs. Legs with no usable book are CARRIED AT COST (mid=NULL).

    Carrying at cost rather than at a fabricated midpoint keeps the headline unrealized
    number free of a number nobody would trade at, in either direction. It is not a claim
    that the position is flat: the fee is still charged, mid=NULL flags the leg as
    unmarked, and the count is written to `alerts` so an illiquid book can never quietly
    disappear from the reported exposure.
    """
    now = datetime.now(timezone.utc).isoformat()
    n = unmarked = 0
    for pos in open_positions(conn):
        for f in pos["fills"]:
            mid = _mid(conn, f["ticker"])
            if mid is None:
                # no reliable two-sided market — carry at entry, charge only the fee
                pnl, unmarked = -(f["fee_usd"] or 0.0), unmarked + 1
            else:
                val = mid if f["side"] == "yes" else 1 - mid
                pnl = (val - f["price"]) * f["count"] - f["fee_usd"]
            conn.execute(
                "INSERT OR REPLACE INTO marks(ts, decision_id, ticker, mid, pnl_usd)"
                " VALUES(?,?,?,?,?)",
                (now, pos["id"], f["ticker"], mid, round(pnl, 4)))
            n += 1
    if unmarked:
        msg = (f"{unmarked}/{n} open legs carried at cost — book wider than"
               f" {WIDE_SPREAD:.2f} or one-sided; unrealized PnL excludes them")
        # mark_all runs from jobs.tick every 900s, so an illiquid book that persists for
        # a day used to write ~96 byte-identical rows and bury the alert feed (which is
        # what made a normal disclosure look like an outage). Dedupe on the message text
        # within 24h: any CHANGE in the counts changes the text and fires immediately,
        # and an unchanged condition still re-asserts itself once a day rather than going
        # silent — the docstring's promise is that this can never quietly disappear, not
        # that it must be repeated every 15 minutes.
        dup = conn.execute(
            "SELECT 1 FROM alerts WHERE source='pnl.mark_all' AND message=? AND ts > ?",
            (msg, (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat())
        ).fetchone()
        if not dup:
            conn.execute(
                "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                (now, "warn", "pnl.mark_all", msg))
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
                         "origin": pos["kind"],
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
    # §24 strategy split: model opens vs arb vs snipe (origin recorded at settle)
    by_origin = conn.execute(
        "SELECT COALESCE(json_extract(inputs_json,'$.origin'),'open') origin,"
        " COUNT(*) n, ROUND(SUM(size_usd),4) realized FROM ("
        "  SELECT inputs_json, size_usd, MIN(id) FROM decisions"
        "  WHERE kind='settle_note' GROUP BY series, period)"
        " GROUP BY origin").fetchall()
    open_kind = conn.execute(
        "SELECT kind, COUNT(*) n, ROUND(SUM(size_usd),4) staked FROM decisions d"
        " WHERE d.kind IN ('open','argmax','arb','snipe') AND NOT EXISTS"
        " (SELECT 1 FROM decisions e WHERE e.series=d.series AND e.period=d.period"
        "  AND e.kind IN ('exit','cancel','settle_note') AND e.id>d.id)"
        " GROUP BY kind").fetchall()
    return {"open_by_series": [dict(r) for r in rows],
            "settled_by_series": [dict(r) for r in settled],
            "settled_by_origin": [dict(r) for r in by_origin],
            "open_by_kind": [dict(r) for r in open_kind]}
