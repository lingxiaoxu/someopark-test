"""jobs/tick.py — the 15-min executor of materialised runs (PLAN §8.2-2).

    conda run -n someopark_run python -m prediction_market_macro.jobs.tick
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.config.settings import load_settings
from prediction_market_macro.ingest.kalshi_md import KalshiMD
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.jobs import scheduler


def _exec_task(conn, s, md, r) -> str:
    task, series = r["task"], r["series"]
    from prediction_market_macro.ops import decide_all, exits, pnl, predict_all
    if task in ("arm", "snapshot", "reassess", "decide"):
        if series in REGISTRY:
            md.snapshot_series(series)
    if task in ("arm", "decide", "reassess"):
        predict_all.run(conn, s)
        decide_all.run(conn, s)
        exits.run(conn, s)
    if task == "freeze":
        scheduler.set_coverage(conn, series, r["period"], "frozen")
    if task == "reconcile":
        if series in REGISTRY:
            md.sync_settlements(series)
        pnl.settle_pass(conn)
        scheduler.set_coverage(conn, series, r["period"], "reconciled")
    if task in ("daily_refresh", "health", "pred_freshness"):
        last = s.output_dir / "refresh_last.json"
        if last.exists():
            ts = json.loads(last.read_text()).get("ts")
            if ts and datetime.now(timezone.utc) - datetime.fromisoformat(ts) \
                    < timedelta(hours=20):
                return "covered_by_daily_refresh"
        from prediction_market_macro.ops import refresh
        refresh.run()
        return "ran_full_refresh"
    return "ok"


def main():
    s = load_settings()
    conn = init_db(s.db_path)
    md = KalshiMD(conn)
    due = scheduler.claim_due(conn)
    print(f"[tick] {datetime.now(timezone.utc).isoformat()} due={len(due)}")
    for r in due:
        try:
            note = _exec_task(conn, s, md, r)
            scheduler.mark_done(conn, r["id"], note)
            print(f"  ✓ {r['lane']}/{r['series']}/{r['period']}/{r['task']}: {note}")
        except Exception as e:                                   # noqa: BLE001
            scheduler.mark_late(conn, r["id"], str(e)[:200])
            print(f"  ✗ {r['task']}: {e}")


if __name__ == "__main__":
    main()
