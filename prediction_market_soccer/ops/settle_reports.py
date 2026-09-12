"""Publish completed paper finances promptly; keep research off this critical path."""
from __future__ import annotations

from dataclasses import asdict
from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.ops.run_status import RunStatus, atomic_bytes, atomic_json, publish_operations


from prediction_market_soccer.ops.maintenance_gate import writer


@writer
def main() -> None:
    from prediction_market_soccer.ops.proc_lock import acquire, release
    # Shared with full refresh: two writers must not replace each other's report batch.
    if not acquire("refresh_all"):
        print("[settle_reports] a full/report refresh is active — pending results will retry")
        return
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ops.export_stage import ExportStage
    conn = store.init_db()
    status = RunStatus("settle_reports")
    settled = conn.execute("SELECT COUNT(*) FROM fixture WHERE status_short IN ('FT','AET','PEN') "
                           "AND home_goals IS NOT NULL").fetchone()[0]
    try:
        with ExportStage() as stage:
            from prediction_market_soccer.ops import (
                settle_bets, performance_report, milestone_export, frontend_export)
            from prediction_market_soccer.util.frozen_strategy_store import consume_completed_paper
            status.step("settled_bet", lambda: settle_bets.freeze_settled_bets(conn))
            if status.failed:
                raise RuntimeError("Paper settlement failed; preserved the published financial group")
            status.step("strategy_book_append", lambda: consume_completed_paper(conn))
            if status.failed:
                raise RuntimeError("Paper book append failed; preserved the published financial group")
            reports = {}
            def write(name, fn):
                doc = fn()
                atomic_json(CONFIG.paths.output / name, doc)
                atomic_json(CONFIG.paths.frontend_data / name, doc)
            def performance():
                reports["performance"] = performance_report.build(conn, freeze=False)
                return asdict(reports["performance"])
            status.step("performance_report.json", lambda: write("performance_report.json", performance))
            status.step("milestone_marks.json", lambda: write("milestone_marks.json", lambda: milestone_export.build(
                conn, freeze=False, ledger=reports["performance"].strategy_ledger)))
            def pdf():
                report = reports["performance"]
                path = CONFIG.paths.output / "performance_report.pdf"
                performance_report.build_pdf(report, str(path))
                atomic_bytes(CONFIG.paths.frontend_data / path.name, path.read_bytes())
            status.step("performance_report.pdf", pdf)
            status.step("frontend_overview.json", lambda: write("frontend_overview.json", lambda: frontend_export.build(conn)))
            if status.failed:
                raise RuntimeError("Report refresh failed; previous complete reports retained")
            stage.promote()
        status.finish(input_value=settled)
        atomic_bytes(CONFIG.paths.output / ".settle_reports_watermark", str(settled).encode())
        print(f"[settle_reports] complete on {settled} settled fixtures")
        # Quote-history collection is bounded and independently observable. Its
        # failure cannot undo the already-published financial group or watermark.
        # Expensive backtest/OOS diagnostics belong to the full-refresh pipeline.
        try:
            from prediction_market_soccer.ops.daily_collection import run as collect_daily
            collection = RunStatus("settle_reports_collection")
            collection.step("milestone_backfill", lambda: collect_daily(conn, limit=12), required=False)
            collection.finish(input_value=settled)
        except Exception as exc:
            print(f"[settle_reports] optional history collection failed after financial publication: {exc}")
    except BaseException as exc:
        status.finish(error=exc)
        raise
    finally:
        conn.close()
        publish_operations()
        release("refresh_all")


if __name__ == "__main__":
    main()
