"""ops/settle_reports.py — the heavy on-settle reports, run OUT of the live loop's way.

When a match settles, the bet ledger (performance_report), the OOS evaluation and the
PnL PDF should refresh. They used to run INLINE in the live loop's settle branch, which
was fine while they scored with the cached live model — but the honest point-in-time
rework made them expensive (per-day walk-forward: ~140 strength fits each), and the
loop holds a single-instance lock, so every settle wave froze the in-play card for the
whole rebuild: 8-15 minute "cycles" in the log, 2,440 prior rebuilds in one day.

The date-stamped prior cache (model/pit_strength.pit_prior) removed the rebuild grind;
this module removes the BLOCKING. The live loop now spawns it detached and moves on —
the reports land a few minutes later, the in-play card never stops. A lock file keeps
it single-flight; a stale lock (>30 min) is treated as a crashed run and taken over.

    python -m prediction_market_soccer.ops.settle_reports
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict

from prediction_market_soccer.config import CONFIG

_LOCK = CONFIG.paths.output / ".settle_reports.lock"
_STALE_S = 30 * 60


def _acquire() -> bool:
    try:
        if _LOCK.exists() and (time.time() - _LOCK.stat().st_mtime) < _STALE_S:
            return False
        _LOCK.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return False


def _write_both(name: str, doc) -> None:
    payload = json.dumps(doc, ensure_ascii=False, indent=2)
    for d in (CONFIG.paths.output, CONFIG.paths.frontend_data):
        (d / name).write_text(payload, encoding="utf-8")


def main() -> None:
    if not _acquire():
        print("[settle_reports] another run is active — exiting")
        return
    t0 = time.time()
    try:
        from prediction_market_soccer.ingest import store
        conn = store.init_db()

        from prediction_market_soccer.ops import performance_report
        rep = performance_report.build(conn)
        _write_both("performance_report.json", asdict(rep))
        print(f"[settle_reports] performance_report.json ({time.time() - t0:.0f}s)")

        try:
            from prediction_market_soccer.model import oos_eval
            _write_both("oos_report.json", asdict(oos_eval.evaluate(conn=conn)))
            print(f"[settle_reports] oos_report.json ({time.time() - t0:.0f}s)")
        except Exception as e:  # noqa: BLE001
            print(f"[settle_reports] oos skipped: {e}")

        try:
            import shutil
            pdf = CONFIG.paths.output / "performance_report.pdf"
            performance_report.build_pdf(rep, str(pdf))
            shutil.copy(pdf, CONFIG.paths.frontend_data / "performance_report.pdf")
            print(f"[settle_reports] performance_report.pdf ({time.time() - t0:.0f}s)")
        except Exception as e:  # noqa: BLE001
            print(f"[settle_reports] pdf skipped: {e}")
    finally:
        try:
            _LOCK.unlink()
        except OSError:
            pass
    print(f"[settle_reports] done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
