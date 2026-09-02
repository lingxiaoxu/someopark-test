"""ops/refresh.py — the daily full-reforecast entry (PLAN §8.0, §12).

Steps-table pattern (mother template): each step runs independently; one failure never
kills the rest; every step prints ✓/✗ and the failures land in alerts.

    conda run -n someopark_run python -m prediction_market_macro.ops.refresh [--weekly]
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


class RefreshBusy(RuntimeError):
    """Another holder already owns the single-instance lock."""


@contextmanager
def _single_instance(output_dir: Path, lock_name: str = "refresh.lock"):
    """Refuse to start a second refresh while one is in flight.

    `lock_name` exists so other long, DB-heavy jobs can reuse the mechanism without
    sharing refresh's lock — `research/live_replay.py` runs a ~10-minute simulation from
    its own launchd job and needs the same "refuse, don't queue" guarantee against
    ITSELF, but must not be blocked by (or block) a refresh. Same file, different name,
    same semantics.

    `refresh_last.json` is written by the LAST line of `_run`, so any caller that judges
    "did today's refresh happen" from its `ts` is blind for the whole ~17 min the run
    takes. jobs/tick.py does exactly that, and its `daily_refresh` run is materialised at
    09:00:00Z — the same instant the launchd refresh fires — so the first tick after 09:00
    always lands inside that blind window and starts a concurrent full pass.

    That is not merely wasted CPU: `_run` calls decide_all AND exits, in that order, so a
    single pass can never re-enter a position it closed in the same cycle. On 2026-08-20
    the second pass ran decide_all 13 s after the first pass's exits had flattened
    KXNATGASW, saw the `already_open_no_averaging_down` gate lifted, and opened a
    same-day reversal leg (decision 7389, YES T2.799) that no single pass would have taken.

    flock is released by the kernel when the holding fd closes, including on SIGKILL, so
    this cannot leave a stale lock that wedges the daily job.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(output_dir / lock_name, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = os.pread(fd, 256, 0).decode(errors="replace").strip()
            raise RefreshBusy(holder or "held by an unidentified process") from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"pid={os.getpid()} started="
                      f"{datetime.now(timezone.utc).isoformat()}".encode(), 0)
        yield
    finally:
        os.close(fd)                                     # releases the flock


def run(weekly: bool = False) -> dict:
    """Single-instance entry point. Raises RefreshBusy if one is already running."""
    with _single_instance(load_settings().output_dir):
        return _run(weekly)


def _run(weekly: bool = False) -> dict:
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
            # short reason in the alert (dashboards); full traceback to stdout/log
            print(traceback.format_exc())
            _alert(conn, "error", "refresh", f"{name}: {str(e)[:200]}")

    print(f"[refresh] {datetime.now(timezone.utc).isoformat()} weekly={weekly}")
    # ── §8.0 step 1: ingest (incl. new-event auto-discovery) ─────────────
    step("calendars", lambda: calendars.sync_to_db(conn))
    if weekly:
        step("calendar_web_check", lambda: calendars.refresh_from_web(conn))
    from prediction_market_macro.venues.kalshi import account
    step("bankroll", lambda: account.refresh_bankroll(conn))
    step("fred_core", lambda: sum(fred.pull_core().values()))
    # AFTER fred_core (2026-08-13): the reconciler used to run before the day's
    # pull, so a print that lands in the same morning's fetch (GASREGW arrives on
    # FRED ~2 days after each Monday, at exactly this refresh's fred_core minute)
    # drew one final false "POSTPONED?" the instant before it appeared. Checking
    # actuals against the freshest data is also just the right order.
    step("calendar_actuals", lambda: calendars.reconcile_actuals(conn))
    from prediction_market_macro.ingest import nowcast
    step("gdpnow", lambda: nowcast.pull_gdpnow(fred, conn))
    from prediction_market_macro.ingest import aaa_daily, eia
    step("aaa_daily", lambda: aaa_daily.fetch_daily(conn))
    step("eia_storage", lambda: eia.pull_storage(conn))
    # ERCOT grid fundamentals (2026-08-30, SHADOW per §7-bis — no model reads the
    # table until a preregistered gate clears). Texas gas burn is the demand side of
    # the EIA storage prints that move NG; accrues daily from the public dashboards.
    from prediction_market_macro.ingest import ercot
    step("ercot", lambda: ercot.refresh(conn))
    step("ercot_mirror", lambda: ercot.mirror_weekly_burn(conn))
    # PJM grid fundamentals (2026-09-02, SHADOW per §7-bis — the deliberate twin of the
    # ERCOT lane; PJM is the larger market, so its gas burn carries more national weight
    # in the storage prints that move NG). One Data Miner request per refresh: the
    # non-member rate limit is ~6/min and the AEUS strategy owns the bulk pulls.
    from prediction_market_macro.ingest import pjm
    step("pjm", lambda: pjm.refresh(conn))
    step("pjm_mirror", lambda: pjm.mirror_weekly_burn(conn))
    # Cleveland Fed daily inflation nowcasts (2026-08-15, shadow per §7-bis —
    # no model reads it until a preregistered gate clears). The fetched files
    # carry full history, so this one step is backfill + daily tail in one.
    from prediction_market_macro.ingest import cleveland_nowcast
    step("cleveland_nowcast", lambda: cleveland_nowcast.refresh(conn))
    from prediction_market_macro.ingest import weather as wx
    # trailing 45d only: the whole history is a `ops.backfill --weather` job, and this
    # window also re-writes the tail where ERA5T later settles into ERA5.
    step("weather", lambda: wx.pull(
        conn, start=(datetime.now(timezone.utc).date()
                     - timedelta(days=45)).isoformat())["days"])
    step("futures", lambda: market_data.pull_futures(conn))
    step("fx", lambda: market_data.pull_fx(conn, s.polygon_api_key))
    step("news", lambda: market_data.pull_news(conn, s.polygon_api_key))
    from prediction_market_macro.ingest import fed_text
    step("fed_statements", lambda: fed_text.fetch_statements(conn))
    for spec in REGISTRY.values():
        step(f"kalshi:{spec.ticker}", lambda t=spec.ticker: md.snapshot_series(t))
        step(f"settle:{spec.ticker}", lambda t=spec.ticker: md.sync_settlements(t))
    # #120 — AFTER the settle pass, which is what puts a newly-closed market into
    # `settlements` in the first place; ordering the other way would always archive a day
    # late. Kalshi drops candlesticks at ~75 days and the loss is permanent, so this is
    # the one step in the daily lane with an external deadline.
    from prediction_market_macro.ops import archive_candles
    step("archive_candles", lambda: archive_candles.run(conn, md))
    # ── scheduler upkeep ─────────────────────────────────────────────────
    step("materialize", lambda: scheduler.materialize(conn))
    # ── model registry (§9.2) — idempotent card seeding ──────────────────
    from prediction_market_macro.model import registry as model_registry
    step("models_registry", lambda: model_registry.ensure_registered(conn))
    # ── §8.0 step 2/3: predict + decide (registered per-model as they land, M1+) ──
    try:
        # #119 — must run BEFORE predict_all, which reads the row it writes. It is
        # fingerprint-cached, so on a day with no newly-scoreable event this is a handful
        # of SELECTs; on the day a weekly series settles it rescores that one series.
        from prediction_market_macro.research import param_select
        step("param_select", lambda: len(param_select.refresh(conn, log=None)))
        # user policy 2026-08-11: daily raw-argmin re-selection writes manual_params
        # rows that OVERRIDE the DSR-gated selector above (select_for checks manual
        # first). Fingerprint-cached — most days most markets cost one SELECT. The
        # DSR selector keeps running for its report; the objection stands in README §E.
        from prediction_market_macro.research import param_argmin
        step("param_argmin", lambda: json.dumps(param_argmin.daily(conn, log=None))[:400])
        from prediction_market_macro.ops import predict_all
        step("predict_all", lambda: predict_all.run(conn, s))
        from prediction_market_macro.ops import decide_all
        step("decide_all", lambda: decide_all.run(conn, s))
        from prediction_market_macro.ops import exits
        # PR-7 step 1 (#143) BEFORE the live exits: a position the live rules close this
        # same cycle is gone from open_positions by the time exits.run returns, and S2 —
        # which triggers at a looser threshold — must be seen on that last day too.
        step("s2_shadow", lambda: exits.shadow_run(conn, s))
        step("exits", lambda: exits.run(conn, s))
        # §30 mirror: sweep backstop + order poll + balance-sheet snapshot, then the
        # daily position/balance reconciliation (armed only; dark mode snapshots too)
        from prediction_market_macro.ops import trading_kalshi
        step("trading_kalshi_sync", lambda: json.dumps(trading_kalshi.sync(conn)))
        step("trading_kalshi_reconcile",
             lambda: json.dumps(trading_kalshi.reconcile(conn)))
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
        # DFM synthetic sample for the argmin lane (§S7). Weekly, not daily: this is the
        # only step that imports torch and it costs minutes, while `param_argmin` reads
        # the *scores* it leaves in macro.db and never touches a world again. Regenerating
        # weekly keeps every sample inside `param_argmin.SYNTH_MAX_AGE_DAYS`.
        from prediction_market_macro.research.synth import regen as synth_regen
        # The ten-series joint pass (PR-26): one coupled weekly draw, weekly worlds from
        # its slices, monthly labor/inflation worlds pinned to its month-means. Replaces
        # the former weekly_synth_regen + weekly_synth_regen_coupled pair.
        step("weekly_synth_regen_joint",
             lambda: json.dumps(synth_regen.run_joint(conn, s, log=None))[:600])
        # The switch position, stated out loud (§7c): "no lambda row" and "a lambda row
        # that is zero" refuse with the same line in the daily log, which is how a missing
        # writer went unnoticed for a full build cycle. The weekly log therefore records
        # the effective lambda per monthly target, its basis (measured vs pre-registered)
        # and the row's age, every week, whether or not anything changed.
        # PR-27's follow-through: the weekly series' lambda is re-MEASURED weekly under
        # the standing §6 rule (per-series rows only; '*' stays owned by the original
        # measurement + S5-WF). It turns on the day the accruing real sample says so.
        step("weekly_synth_lambda_remeasure",
             lambda: json.dumps(synth_regen.remeasure_weekly_lambda(conn, s, log=None))[:600])
        step("weekly_synth_lambda",
             lambda: json.dumps(synth_regen.lambda_board(conn))[:600])
        # PR-30: refresh the joint-law correlation matrix the corr-cluster cap reads.
        # n_paths=512 is the judged precision; failure leaves the old file (or none),
        # and an absent/stale matrix degrades the cap to inert, never to wrong.
        step("weekly_portfolio_corr",
             lambda: {"pairs": len(synth_regen.portfolio_corr(conn, s, n_paths=512,
                                                              log=None)["corr"])})
        # S5-WF accrual: each monthly release that settled since last week gets scored
        # point-in-time and stored (~10 min once a month per series, no-op other weeks);
        # then every series' pooled matrices are re-aggregated, and a series that has
        # reached identification persists its OWN measured lambda, superseding '*'.
        from prediction_market_macro.research.synth import calibrate as synth_cal
        step("weekly_synth_wf_accrue",
             lambda: json.dumps({k: v for k, v in
                                 synth_cal.wf_accrue(conn, s, log=None).items()})[:600])
        step("weekly_synth_wf_aggregate",
             lambda: json.dumps({name: synth_cal.wf_aggregate(conn, name).get("status")
                                 for name in synth_cal.wf_targets()})[:600])
        from prediction_market_macro.research import eval as eval_mod
        step("weekly_eval_gates",
             # `enabled` is §25.4's per-series switch, refreshed by this very step —
             # it is the thing that decides whether decide_all bets the series at all,
             # so it belongs in the weekly line rather than only in the experiments row.
             lambda: json.dumps({k: {"real": v.get("real"), "roi": v.get("roi"),
                                     "dm_p": v.get("dm_p"), "on": v.get("enabled")}
                                 for k, v in eval_mod.run_all(conn).items()})[:600])
        from prediction_market_macro.research import prereg
        # the graders judge themselves at their registered thresholds; this caller only
        # surfaces maturation (edge-triggered alert) — see research/prereg.py docstring
        step("weekly_prereg_shadows", lambda: json.dumps(prereg.run_all(conn)))
        from prediction_market_macro.research import attribution
        step("weekly_attribution",
             lambda: {k: v for k, v in attribution.weekly_attribution(conn).items()
                      if k in ("n_settled_scored", "n_misses", "by_series")})
        from prediction_market_macro.research import walkforward
        step("weekly_walkforward_sweep",
             lambda: json.dumps({k: {"roi": v.get("roi"), "n": v.get("n_trades")}
                                 for k, v in walkforward.sweep(
                                     conn, days=30)["leads"].items()}))
        # 60d run feeds the comparison table's last60 baseline column
        step("weekly_walkforward_60d",
             lambda: json.dumps({k: {"roi": v.get("roi"), "n": v.get("n_trades")}
                                 for k, v in walkforward.run(
                                     conn, days=60)["streams"].items()}))
        # canonical as-if-live 30d run LAST (sweep's per-lead runs overwrite the
        # daily_walkforward row; this one is what the frontend headlines)
        step("weekly_walkforward_30d",
             lambda: json.dumps({k: {"roi": v.get("roi"), "n": v.get("n_trades")}
                                 for k, v in walkforward.run(
                                     conn, days=30)["streams"].items()}))
        from prediction_market_macro.research import selector
        step("weekly_ml_selector",
             lambda: json.dumps({k: {"roi": (v or {}).get("roi"),
                                     "n": (v or {}).get("n_trades")}
                                 for k, v in selector.walkforward_eval(conn).items()
                                 if isinstance(v, dict) and "n_trades" in v}))
        step("report_weekly", lambda: report.weekly_pdf(conn, s))
        # re-export AFTER the weekly WF/ML evals — the main export step ran
        # before this block, so without this the fresh numbers would sit in
        # experiments until the next daily refresh
        from prediction_market_macro.ops import frontend_export as fe_post
        step("frontend_export_postweekly", lambda: fe_post.run(conn, s))
    step("watchdog_inline", lambda: len(scheduler.watchdog(conn)))
    # LAST, so the snapshot contains everything this run wrote. `candles` older than
    # Kalshi's ~75-day window exists only in this file and macro.db is gitignored, so
    # without this step the whole archive lives on one un-backed-up disk.
    from prediction_market_macro.ops import backup_db
    step("backup_db", lambda: backup_db.run(s.db_path))

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
