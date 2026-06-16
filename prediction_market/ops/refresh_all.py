"""ops/refresh_all.py — one command to refresh every export on the CURRENT sample.

The OOS tools (oos_eval, param_sweep, backtest, walkforward) all select "every
finished fixture" dynamically — there is no fixed 15/16 cap. So as more matches
finish, the sample automatically grows; this orchestrator just (optionally) pulls
the latest results/odds and then regenerates all the JSON the frontend reads, so a
single run reflects the larger sample everywhere.

    python -m prediction_market.ops.refresh_all              # regen exports from stored DB
    python -m prediction_market.ops.refresh_all --ingest     # + pull new results & missing odds
    python -m prediction_market.ops.refresh_all --ingest --with-form   # + re-pull recent NT form
    python -m prediction_market.ops.refresh_all --with-sweep # + re-run the 180-set param sweep (slow)

Then: npm run sync:wc && firebase deploy (or the deploy step) to push it live.
"""
from __future__ import annotations

import argparse
import json

from prediction_market.config import CONFIG


def _write(name: str, doc) -> None:
    (CONFIG.paths.output / name).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Refresh all prediction exports on the current (growing) sample")
    ap.add_argument("--ingest", action="store_true", help="pull latest finished results + missing odds first")
    ap.add_argument("--with-form", action="store_true", help="also re-pull recent NT results (form) — ~48 requests")
    ap.add_argument("--with-sweep", action="store_true", help="also re-run the 180-set param sweep (slow)")
    args = ap.parse_args()

    from prediction_market.ingest import store
    conn = store.init_db()

    if args.ingest:
        from prediction_market.ingest.api_football import ApiFootball
        from prediction_market.ingest import soccer_ingest as si
        api = ApiFootball(conn)
        try:
            si.sync_results(api, conn)            # newly finished matches → bigger OOS sample
            si.sync_odds(api, conn, limit=30, include_settled=True)   # market reference incl. settled
            if args.with_form:
                si.sync_nt_recent(api, conn)      # refresh recent-form inputs
        except Exception as e:
            print(f"[refresh] ingest partial/failed (continuing with stored data): {e}")

    n_settled = conn.execute(
        "SELECT COUNT(*) n FROM fixture WHERE status_short IN ('FT','AET','PEN') AND home_goals IS NOT NULL"
    ).fetchone()["n"]
    print(f"[refresh] OOS sample is now {n_settled} settled matches (dynamic — grows automatically)")

    # Regenerate every export the frontend reads, all on the CURRENT sample.
    from prediction_market.ops import (backtest_export, form_export, frontend_export,
                                       inplay_export, performance_report, risk_report,
                                       squad_export)
    from dataclasses import asdict
    steps = [
        ("backtest.json",          lambda: backtest_export.build(conn)),
        ("squad.json",             lambda: squad_export.build(conn)),
        ("form.json",              lambda: form_export.build(conn)),
        ("upcoming.json",          lambda: _payload_upcoming(conn)),
        ("inplay_live.json",       lambda: inplay_export.build(conn, with_venues=False)),
        ("performance_report.json", lambda: asdict(performance_report.build(conn))),
        ("risk_report.json",       lambda: asdict(risk_report.build(conn))),
    ]
    for name, fn in steps:
        try:
            _write(name, fn())
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")

    if args.with_sweep:
        try:
            from prediction_market.ops import param_sweep
            _write("param_sweep.json", param_sweep.run(conn))
            print("  ✓ param_sweep.json")
        except Exception as e:
            print(f"  ✗ param_sweep.json: {e}")

    # frontend_overview aggregates the above — build last.
    try:
        _write("frontend_overview.json", frontend_export.build(conn))
        print("  ✓ frontend_overview.json")
    except Exception as e:
        print(f"  ✗ frontend_overview.json: {e}")
    print("[refresh] done. Run `npm run sync:wc && firebase deploy --only hosting` to publish.")


def _payload_upcoming(conn):
    from datetime import datetime, timezone
    from prediction_market.ops import upcoming_export
    rows = upcoming_export.build(limit=6, conn=conn)
    return {"as_of": datetime.now(timezone.utc).isoformat(), "n": len(rows),
            "note": "Real Kalshi + Polymarket US single-match quotes; venue=null only when unlisted.",
            "matches": rows}


if __name__ == "__main__":
    main()
