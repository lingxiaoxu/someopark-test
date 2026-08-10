"""ops/retire_stale_book.py — close out positions a superseded rule set opened (#121).

Between 2026-07-28 and the 2026-07-31 cutover the live paper book opened 98 positions
under a gate set that had **no `max_days_to_close`** — every one of those decisions carries
`gate_snapshot.max_days_to_close = null`. Today that gate is 7 days, so all 43 of the
positions still open would be rejected at entry now; the longest sits 546 days from close
(KXFEDDECISION 2028-01).

Because `decide()` refuses to average down, one open position locks its whole
(series, period). Those 43 positions were holding 50 periods, which is 77% of every `pass`
recorded since the cutover (842 of 1090) and the reason the live segment had not settled a
single trade. Waiting them out means waiting until 2028-01.

So they are retired here. The justification is that **the current strategy would not hold
them** — not that they are inconvenient. That distinction is the whole point: this module
retires positions by a rule (`gate_snapshot` lacks the gate that would now reject them),
never by their PnL, and it is deliberately incapable of selecting on outcome.

Append-only is preserved. Nothing is UPDATEd and nothing is DELETEd — each retirement is a
new `kind='cancel'` row, which is what the ledger's own vocabulary provides and what
`has_open` / `open_positions` already read. The original opens and their fills stay exactly
where they were, so the disavowed book remains fully auditable.

These positions are excluded from the displayed track either way (the pre-cutover book was
disavowed for display in #123), so retiring them cannot flatter the record.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

CUTOVER = "2026-07-31T16:14:00+00:00"
NOTE = ("retired by ops/retire_stale_book (#121): opened under a rule set with no "
        "max_days_to_close; today's gate rejects it at entry")


def candidates(conn, cutover: str = CUTOVER) -> list[dict]:
    """Open positions opened before `cutover` whose entry today's gate would refuse.

    The test is on the decision's OWN `gate_snapshot`, not on a date range: a pre-cutover
    position that the current rules would still allow is left alone.
    """
    from prediction_market_macro.ops import ledger
    from prediction_market_macro.strategy.decision import GATES

    max_dtc = GATES["max_days_to_close"]
    out = []
    for d in ledger.open_positions(conn):
        if d["kind"] != "open" or d["ts_utc"] >= cutover:
            continue
        legs = json.loads(d["structure_json"] or "{}").get("legs", [])
        tks = [l["ticker"] for l in legs]
        if not tks:
            continue
        q = ",".join("?" * len(tks))
        row = conn.execute(
            f"SELECT MAX(close_time) c FROM contracts WHERE ticker IN ({q})", tks).fetchone()
        if not row or not row["c"]:
            continue
        dtc = (datetime.fromisoformat(row["c"])
               - datetime.fromisoformat(d["ts_utc"])).total_seconds() / 86400.0
        if dtc > max_dtc:
            out.append({"id": d["id"], "series": d["series"], "period": d["period"],
                        "ts_utc": d["ts_utc"], "size_usd": d["size_usd"],
                        "days_to_close_at_entry": round(dtc, 1),
                        "close_time": row["c"]})
    return sorted(out, key=lambda x: -x["days_to_close_at_entry"])


def retire(conn, cutover: str = CUTOVER) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    rows = candidates(conn, cutover)
    for r in rows:
        conn.execute(
            "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair, ask,"
            " net_edge, size_usd, inputs_json, model_version, gate_snapshot, note,"
            " closes_decision_id)"
            " VALUES(?,?,?,?, 'cancel', NULL, NULL, NULL, ?, ?, 'retire/1.0', '{}', ?, ?)",
            (now, r["series"], r["period"], "{}", r["size_usd"],
             json.dumps({"retires_decision_id": r["id"],
                         "days_to_close_at_entry": r["days_to_close_at_entry"]}),
             # #149. This writer ALREADY knew which position it retired — it just kept the
             # answer in free-form JSON where no counting query could reach it. Same value,
             # promoted to the column; the json key stays for readers that expect it.
             NOTE, r["id"]))
    conn.commit()
    return {"retired": len(rows), "capital_released": round(
        sum(r["size_usd"] or 0 for r in rows), 2),
        "periods": sorted({(r["series"], r["period"]) for r in rows})}


def main():
    import argparse
    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cutover", default=CUTOVER)
    a = ap.parse_args()
    conn = init_db(load_settings().db_path)
    if a.dry_run:
        rows = candidates(conn, a.cutover)
        for r in rows:
            print(f"  {r['days_to_close_at_entry']:6.1f}d  {r['series']:16} {r['period']:12}"
                  f"  opened {r['ts_utc'][:10]}  closes {r['close_time'][:10]}  "
                  f"${r['size_usd']}")
        print(f"\n{len(rows)} positions, ${sum(r['size_usd'] or 0 for r in rows):.2f} "
              f"paper capital — DRY RUN, nothing written")
        return
    print(json.dumps(retire(conn, a.cutover), indent=1))


if __name__ == "__main__":
    main()
