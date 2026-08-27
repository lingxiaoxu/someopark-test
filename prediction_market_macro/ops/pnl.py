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


# Series whose settlement quantity is NOT the registry's FRED series. Measured against
# the ladders that actually paid out (2026-08-27, /tmp/dfm_verify/label_fix_probe.py);
# the agreement rate for each rule is quoted where it is used.
_FUT_SETTLE = {"KXWTIW": "CL"}          # NYMEX front-month settle, not EIA spot
_DAILY_MONTH_END = {"KXFED"}            # DFEDTARU is daily; the month key needs its END
_DAILY_SID = {"KXAAAGASW": "AAA_DAILY"}  # AAA national average, not the EIA weekly


def _daily_value_on(conn, sid: str, day: str) -> float | None:
    r = conn.execute(
        "SELECT value FROM fred_obs WHERE sid=? AND event_time LIKE ?"
        " ORDER BY knowledge_time LIMIT 1", (sid, day + "%")).fetchone()
    return None if r is None or r["value"] is None else float(r["value"])


def _realized_print(conn, series: str, period: str) -> float | None:
    """First-print label for the settled period, in the CONTRACT'S unit (the
    registry sid may be an index level — %mom/%yoy/k_jobs need transformation).
    Returns None when no honest label exists in contract units.

    Every branch below is scored against the settled ladder — the thing that actually
    paid out — in tests/test_settlement_labels.py. That test exists because five of
    these branches were silently wrong until 2026-08-27, and a wrong label is worse
    than no label: strategy/snipe.py buys the "certain" side of a ladder from this
    number, and research/health.py fuses the global breaker on it.
    """
    from prediction_market_macro.config.registry import REGISTRY
    spec = REGISTRY.get(series)
    if spec is None:
        return None
    if len(period) == 10 and series in _FUT_SETTLE:
        # KXWTIW settles on the NYMEX front-month CL settle for the contract date.
        # DCOILWTICO (EIA Cushing SPOT) disagreed with the ladder on 61 of 141 periods,
        # mean +31.6 grid steps; CL agrees on 139 of 143.
        r = conn.execute(
            "SELECT close FROM fut_daily WHERE root=? AND event_time LIKE ? LIMIT 1",
            (_FUT_SETTLE[series], period + "%")).fetchone()
        return None if r is None or r["close"] is None else round(float(r["close"]), 4)
    if len(period) == 10 and series in _DAILY_SID:
        # AAA daily national average. Only ingested from 2026-07-31 (ingest/aaa_daily.py:
        # "No history is available"), so older periods have NO honest label and get None
        # rather than the EIA weekly proxy, which sat a mean 3.1 grid steps LOW and
        # disagreed with the ladder on 27 of 73 periods.
        return _daily_value_on(conn, _DAILY_SID[series], period)
    if not spec.fred_first_release:
        return None
    sid = spec.fred_first_release
    if len(period) == 7:                              # monthly 'YYYY-MM'
        if series in _DAILY_MONTH_END:
            # DFEDTARU is a DAILY series keyed here by month. Taking its first row in
            # the month returns the PRE-meeting rate: 6 of 28 FOMC periods came out
            # exactly one 25bp grid step high. The settlement is the range upper bound
            # AFTER the meeting, which is the month's last daily value (28/28).
            r = conn.execute(
                "SELECT value FROM fred_obs WHERE sid=? AND event_time LIKE ?"
                " ORDER BY event_time DESC LIMIT 1", (sid, period + "%")).fetchone()
            return None if r is None or r["value"] is None else float(r["value"])
        if spec.unit == "%mom":
            pair = _same_vintage_pair(conn, sid, period, _prev_month(period))
            if pair is None:
                a = _first_print_value(conn, sid, period)
                b = _first_print_value(conn, sid, _prev_month(period))
                pair = (a, b) if a and b else None
            return round((pair[0] / pair[1] - 1) * 100, 4) if pair else None
        if spec.unit == "%yoy":
            # Same-vintage, for the same reason %mom is: differencing two independent
            # first prints lets every intervening revision into the label. 33/41 -> 36/41
            # (headline) and 36/43 -> 40/43 (core). The residual gap is NOT noise — the
            # published CPI YoY is computed on the NSA index and CPIAUCSL/CPILFESL are
            # seasonally adjusted. Closing it needs CPIAUCNS/CPILFENS ingested; until
            # then this label is right most of the time and test_settlement_labels.py
            # holds it to the measured rate rather than to 100%.
            pair = _same_vintage_pair(conn, sid, period, _prev_month(period, 12))
            if pair is None:
                a = _first_print_value(conn, sid, period)
                b = _first_print_value(conn, sid, _prev_month(period, 12))
                pair = (a, b) if a and b else None
            return round((pair[0] / pair[1] - 1) * 100, 4) if pair else None
        if spec.unit == "k_jobs":                     # PAYEMS level → change in jobs
            # Same-vintage is not a refinement here, it is the whole label: differencing
            # two independent first prints put every prior-month revision — and January's
            # annual benchmark revision — into the number. 14/39 agreement became 39/39.
            # The worst case was 2026-01: -899,000 against a ladder that settled above
            # +125,000, which strategy/snipe.py would have read as a certainty.
            pair = _same_vintage_pair(conn, sid, period, _prev_month(period))
            return None if pair is None else round((pair[0] - pair[1]) * 1000, 1)
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
            " net_edge, size_usd, inputs_json, model_version, gate_snapshot, note,"
            " closes_decision_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, pos["series"], pos["period"], pos["structure_json"], "settle_note",
             pos["fair"], pos["ask"], pos["net_edge"], round(realized, 4),
             json.dumps({"realized_usd": round(realized, 4), "results": results,
                         "origin": pos["kind"],
                         "z": round(z, 3) if z is not None else None,
                         "attribution": attribution}), pos["model_version"], "{}",
             note, pos["id"]))                       # #149: which position settled
        n += 1
    conn.commit()
    return n


def report(conn) -> dict:
    # #150. One entry per SETTLED POSITION, from `ledger.closures`.
    #
    # This was `GROUP BY series, period`, keeping the first settle_note per period. That
    # deduped the historical duplicates correctly — and all four multi-settle periods on
    # the live book really are re-settles of a single open, so it does not misfire today.
    # But the unit that closes is a POSITION, not a period: KXWTIW 2026-08-07 currently
    # holds three, and one settle_note per period would have discarded two thirds of that
    # settlement's realized PnL from the report. #149 gave closes an id to be deduped by;
    # the dedup here was still the period.
    from prediction_market_macro.ops import ledger as _ledger
    settled_pos = _ledger.closures(conn, ("settle_note",))
    by_series: dict = {}
    by_origin_d: dict = {}
    for p in settled_pos:
        rz = _ledger.realized_usd(p["close"])
        s = by_series.setdefault(p["series"], {"series": p["series"], "n": 0,
                                               "realized": 0.0})
        s["n"] += 1
        # a settle_note that never recorded its figure must not read as a $0.00 result
        if rz is not None:
            s["realized"] += rz
        origin = (json.loads(p["close"]["inputs_json"] or "{}").get("origin") or "open")
        o = by_origin_d.setdefault(origin, {"origin": origin, "n": 0, "realized": 0.0})
        o["n"] += 1
        if rz is not None:
            o["realized"] += rz
    settled = [{**v, "realized": round(v["realized"], 4)}
               for v in sorted(by_series.values(), key=lambda d: d["series"])]
    by_origin = [{**v, "realized": round(v["realized"], 4)}
                 for v in sorted(by_origin_d.values(), key=lambda d: d["origin"])]
    # #151/F9. `open_by_series` was `SELECT ... WHERE kind='open' GROUP BY series`, and
    # `open_by_kind` 5 lines below — in the SAME returned dict — already read the ledger.
    # Two defects stacked in that one SELECT:
    #   1. no close accounting at all. Nothing joined `closes_decision_id`, so every
    #      position the book has ever opened still counted as open. Live: 107 positions /
    #      $105.36 against a ledger truth of 8 / $6.39. This is the dominant term.
    #   2. `kind='open'` alone, so argmax/arb/snipe holdings were invisible — the same
    #      one-question-two-kind-sets split as #149, #150 and F7.
    # #149 and #150 both edited this function (the closures and open_positions blocks) and
    # walked past this line twice, which is the argument for deriving both breakdowns from
    # ONE pass over ONE source: they can no longer disagree, because there is nothing left
    # to disagree with. Mitigating, and why this sat here: `report` is an operator/CLI and
    # test surface — neither `refresh.py` nor `jobs/tick.py` calls it, so no published
    # figure was ever computed from it.
    open_kind_d: dict = {}
    open_series_d: dict = {}
    for d in _ledger.open_positions(conn):     # #150: was the last NOT EXISTS copy
        k = open_kind_d.setdefault(d["kind"], {"kind": d["kind"], "n": 0, "staked": 0.0})
        k["n"] += 1
        k["staked"] += d["size_usd"] or 0.0
        s = open_series_d.setdefault(d["series"], {"series": d["series"], "n": 0,
                                                   "staked": 0.0})
        s["n"] += 1
        s["staked"] += d["size_usd"] or 0.0
    open_kind = [{**v, "staked": round(v["staked"], 4)}
                 for v in sorted(open_kind_d.values(), key=lambda d: d["kind"])]
    open_series = [{**v, "staked": round(v["staked"], 4)}
                   for v in sorted(open_series_d.values(), key=lambda d: d["series"])]
    return {"open_by_series": open_series,
            "settled_by_series": settled,
            "settled_by_origin": by_origin,
            "open_by_kind": open_kind}
