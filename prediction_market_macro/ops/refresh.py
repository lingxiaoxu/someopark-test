"""ops/refresh.py — the daily full-reforecast entry (PLAN §8.0, §12).

Steps-table pattern (mother template): each step runs independently; one failure never
kills the rest; every step prints ✓/✗ and the failures land in alerts.

    conda run -n someopark_run python -m prediction_market_macro.ops.refresh [--weekly]
"""
from __future__ import annotations

import argparse
import json
import time
import traceback
from datetime import datetime, timezone

from prediction_market_macro.config.registry import REGISTRY, p0
from prediction_market_macro.config.settings import load_settings
from prediction_market_macro.ingest import calendars, market_data
from prediction_market_macro.ingest.fred import FredPIT
from prediction_market_macro.ingest.kalshi_md import KalshiMD
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.jobs import scheduler


def _alert(conn, level: str, source: str, msg: str) -> None:
    conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                 (datetime.now(timezone.utc).isoformat(), level, source, msg))
    conn.commit()


def run(weekly: bool = False) -> dict:
    s = load_settings()
    conn = init_db(s.db_path)
    fred = FredPIT(s.fred_api_key, conn)
    md = KalshiMD(conn)
    results: dict[str, str] = {}

    def step(name, fn):
        t0 = time.time()
        try:
            out = fn()
            results[name] = f"ok ({time.time()-t0:.1f}s)" + (f" {out}" if out is not None else "")
            print(f"  ✓ {name}: {results[name]}")
        except Exception as e:                                   # noqa: BLE001
            results[name] = f"FAIL {e}"
            print(f"  ✗ {name}: {e}")
            _alert(conn, "error", "refresh", f"{name}: {e}\n{traceback.format_exc()[-800:]}")

    print(f"[refresh] {datetime.now(timezone.utc).isoformat()} weekly={weekly}")
    # ── §8.0 step 1: ingest (incl. new-event auto-discovery) ─────────────
    step("calendars", lambda: calendars.sync_to_db(conn))
    step("calendar_actuals", lambda: calendars.reconcile_actuals(conn))
    if weekly:
        step("calendar_web_check", lambda: calendars.refresh_from_web(conn))
    from prediction_market_macro.venues.kalshi import account
    step("bankroll", lambda: account.refresh_bankroll(conn))
    step("fred_core", lambda: sum(fred.pull_core().values()))
    from prediction_market_macro.ingest import nowcast
    step("gdpnow", lambda: nowcast.pull_gdpnow(fred, conn))
    step("futures", lambda: market_data.pull_futures(conn))
    step("fx", lambda: market_data.pull_fx(conn, s.polygon_api_key))
    step("news", lambda: market_data.pull_news(conn, s.polygon_api_key))
    from prediction_market_macro.ingest import fed_text
    step("fed_statements", lambda: fed_text.fetch_statements(conn))
    for spec in REGISTRY.values():
        step(f"kalshi:{spec.ticker}", lambda t=spec.ticker: md.snapshot_series(t))
        step(f"settle:{spec.ticker}", lambda t=spec.ticker: md.sync_settlements(t))
    # ── scheduler upkeep ─────────────────────────────────────────────────
    step("materialize", lambda: scheduler.materialize(conn))
    # ── model registry (§9.2) — idempotent card seeding ──────────────────
    from prediction_market_macro.model import registry as model_registry
    step("models_registry", lambda: model_registry.ensure_registered(conn))
    # ── §8.0 step 2/3: predict + decide (registered per-model as they land, M1+) ──
    try:
        from prediction_market_macro.ops import predict_all
        step("predict_all", lambda: predict_all.run(conn, s))
        from prediction_market_macro.ops import decide_all
        step("decide_all", lambda: decide_all.run(conn, s))
        from prediction_market_macro.ops import exits
        step("exits", lambda: exits.run(conn, s))
    except ImportError:
        print("  - predict/decide layers not installed yet (pre-M1)")
    # ── §8.0 step 4/5: marks, settle, health, exports (land at M4-M6) ────
    try:
        from prediction_market_macro.ops import pnl
        step("marks", lambda: pnl.mark_all(conn))
        step("settle_pass", lambda: pnl.settle_pass(conn))
    except ImportError:
        print("  - pnl layer not installed yet (pre-M1)")
    # ── shadow members (§7-bis stage 1: preds only, never decisions) ─────
    from prediction_market_macro.model import ts_foundation
    step("chronos_shadow", lambda: ts_foundation.shadow_run(conn, s))
    from prediction_market_macro.model import bridge as bridge_model
    step("bridge_shadow", lambda: bridge_model.shadow_run(conn, s))
    from prediction_market_macro.model import ensemble as ensemble_model
    step("ensemble_shadow", lambda: ensemble_model.shadow_run(conn, s))
    # ── model-free cross-market consistency (§11 四件套) ─────────────────
    from prediction_market_macro.strategy import consistency
    step("consistency", lambda: consistency.run(conn))
    # ── LLM annotation layer (§10/§19-8, serial + degradable, never blocking) ──
    from prediction_market_macro.analysis import llm as llm_mod
    step("news_flags", lambda: llm_mod.apply_news_flags(conn, s))
    step("statement_risk", lambda: (llm_mod.statement_risk_pass(conn, s) or {}
                                    ).get("hawk_score"))
    try:
        from prediction_market_macro.research import health
        step("health", lambda: health.daily_health(conn, s))
    except ImportError:
        print("  - health layer not installed yet (pre-M4)")
    try:
        from prediction_market_macro.ops import frontend_export
        step("frontend_export", lambda: frontend_export.run(conn, s))
    except ImportError:
        print("  - frontend export not installed yet (pre-M6)")
    # ── reports (§12): daily always; weekly adds narrative+calibration+gates ──
    from prediction_market_macro.ops import report
    step("report_daily", lambda: report.daily_pdf(conn, s))
    if weekly:
        from prediction_market_macro.research import backtest
        step("weekly_backtest_all",
             lambda: json.dumps({k: {kk: v[kk] for kk in ("n", "brier_model-1h",
                                                          "brier_market-1h")}
                                 for k, v in backtest.replay_all(conn, md).items()})[:400])
        from prediction_market_macro.model import dfm_bridge
        step("weekly_dfm_gate",
             lambda: {k: v for k, v in dfm_bridge.gate_check(conn, s).items()
                      if k.startswith(("pass", "cov_"))})
        from prediction_market_macro.research import eval as eval_mod
        step("weekly_eval_gates",
             lambda: json.dumps({k: {"real": v.get("real"), "roi": v.get("roi"),
                                     "dm_p": v.get("dm_p")}
                                 for k, v in eval_mod.run_all(conn).items()})[:400])
        step("report_weekly", lambda: report.weekly_pdf(conn, s))
    step("watchdog_inline", lambda: len(scheduler.watchdog(conn)))

    out = {"ts": datetime.now(timezone.utc).isoformat(), "weekly": weekly, "steps": results}
    (s.output_dir / "refresh_last.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    fails = [k for k, v in results.items() if v.startswith("FAIL")]
    print(f"[refresh] done — {len(results)-len(fails)}/{len(results)} steps ok"
          + (f", FAILED: {fails}" if fails else ""))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weekly", action="store_true")
    args = ap.parse_args()
    run(weekly=args.weekly)


if __name__ == "__main__":
    main()
