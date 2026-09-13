"""Refresh all soccer data/exports; publish a batch only after required steps succeed."""
from __future__ import annotations

import argparse
from dataclasses import asdict

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.ops.run_status import RunStatus, atomic_json, atomic_bytes, publish_operations


def _write(name: str, doc) -> None:
    atomic_json(CONFIG.paths.output / name, doc)
    atomic_json(CONFIG.paths.frontend_data / name, doc)


def _quality(conn):
    from prediction_market_soccer.ops import clubelo_quality
    report = clubelo_quality.run(conn=conn)
    if report["verdict"] == "FAIL":
        raise RuntimeError("ClubElo quality gate failed: " + ", ".join(
            c["check"] for c in report["checks"] if c["level"] == "FAIL"))
    return report


def _refresh(conn, args, status):
    from prediction_market_soccer.config.leagues import active
    from prediction_market_soccer.ingest import soccer_ingest as si, fc_ingest
    from prediction_market_soccer.ingest.api_football import ApiFootball

    status.step("fixture_roster", lambda: si.reconcile_fixture_clubs(conn))
    if args.ingest:
        api = ApiFootball(conn)
        # Teams/standings are dependencies of priors, not one-time bootstrap data.
        for comp in active():
            status.step(f"teams:{comp.key}", lambda c=comp: si.sync_teams(api, conn, c))
        status.step("results", lambda: si.sync_results(api, conn))
        for comp in active():
            status.step(f"standings:{comp.key}", lambda c=comp: si.sync_standings(api, conn, c))
            status.step(f"standings_previous:{comp.key}", lambda c=comp: si.sync_standings(api, conn, c, season=c.season - 1))
            status.step(f"topscorers:{comp.key}", lambda c=comp: si.sync_topscorers(api, conn, c), required=False)
        status.step("odds", lambda: si.sync_odds(api, conn, limit=30, include_settled=True), required=False)
        status.step("club_recent", lambda: si.project_results_to_club_recent(conn))
        if args.with_form:
            status.step("club_form", lambda: si.sync_club_recent(api, conn), required=False)

    if status.failed:
        raise RuntimeError("Required ingest failed; dependent model and ledger work stopped")

    if args.with_fc_download:
        status.step("fc_download", fc_ingest.download_fc26, required=False)
    status.step("fc_players", lambda: fc_ingest.ingest_fc_players(conn), required=False)

    from prediction_market_soccer.ingest.club_prior import build_all
    from prediction_market_soccer.ops import backfill_price_ticks, param_select_club
    from prediction_market_soccer.ops.daily_collection import run as collect_daily
    status.step("price_ticks", lambda: collect_daily(conn, collector='ticks'), required=False)
    status.step("club_priors", lambda: build_all(conn))
    # Validate the anchor before expensive model work; a failed gate cannot be published.
    status.step("clubelo_quality", lambda: _quality(conn))
    if status.failed:
        raise RuntimeError("Prior/anchor validation failed; dependent model and ledger work stopped")
    # Recommendations are research output. Adoption changes the forward method
    # and requires the explicit no-open-position epoch workflow.
    param_report = status.step("param_select", lambda: param_select_club.run(test_days=14, dry_run=True))
    from prediction_market_soccer.ingest import clubelo_web
    status.step("clubelo_histories", clubelo_web.refresh_histories, required=False)
    # Build today's observed daily model per competition NOW, so a matchday decision
    # never has to build it inline. build_observed_strength freezes the first eligible
    # model per UTC day; when it is built at decision time its available_at lands AFTER
    # that decision's cutoff, costing one ObservedInputsUnavailable round — and on
    # 2026-09-12 the retry arrived after the ≤20-min PRE staging window had closed,
    # losing every pre leg of the 13:00 and 13:30 waves. Paying that round here, in the
    # quiet pre-match refresh, leaves matchday decisions a plain cache read.
    status.step("daily_models", lambda: _prewarm_daily_models(conn), required=False)

    # Forward paper/demo decisions retain their own observed inputs. Historical
    # replay caches are research artifacts and no longer gate daily publication.
    from prediction_market_soccer.ops import calibrate_fit, backfill_milestones, settle_bets
    status.step("calibration.json", lambda: _write("calibration.json", calibrate_fit.fit(conn)))
    status.step("milestone_backfill", lambda: collect_daily(conn), required=False)
    status.step("settled_bet", lambda: settle_bets.freeze_settled_bets(conn))
    if status.failed:
        raise RuntimeError("Paper settlement failed; dependent financial publication stopped")
    from prediction_market_soccer.util.frozen_strategy_store import consume_completed_paper
    status.step("strategy_book_append", lambda: consume_completed_paper(conn))
    if status.failed:
        raise RuntimeError("Paper book append failed; dependent financial publication stopped")

    from prediction_market_soccer.model.run_model import refresh_model
    status.step("soccer_model.json", refresh_model)
    from prediction_market_soccer.ops import (
        backtest_export, form_export, inplay_export, milestone_export, performance_report,
        risk_report, squad_export, schedule_export, season_odds_export, cup_bracket_export)
    from prediction_market_soccer.model import oos_eval
    from prediction_market_soccer.exec import executor
    from prediction_market_soccer.strategy.xv_monitor import compare_champion, compare_matches

    reports = {}
    def report(name, module):
        reports[name] = module.build(conn, freeze=False) if name == "performance" else module.build(conn)
        return asdict(reports[name])

    steps = [
        ("upcoming.json", lambda: _payload_upcoming(conn)),
        ("xv_matches.json", lambda: compare_matches(limit=12)),
        ("season_odds.json", lambda: season_odds_export.build(conn)),
        ("schedule.json", lambda: schedule_export.build(conn)),
        ("squad.json", lambda: squad_export.build(conn)),
        ("form.json", lambda: form_export.build(conn)),
        ("backtest.json", lambda: backtest_export.build(conn)),
        ("inplay_live.json", lambda: inplay_export.build(conn, with_venues=False)),
        ("oos_report.json", lambda: asdict(oos_eval.evaluate(conn=conn))),
        ("performance_report.json", lambda: report("performance", performance_report)),
        ("risk_report.json", lambda: report("risk", risk_report)),
        ("milestone_marks.json", lambda: milestone_export.build(
            conn, freeze=False, ledger=reports["performance"].strategy_ledger)),
        ("match_signals.json", lambda: executor.build_match_signals(conn)),
        ("bracket.json", lambda: cup_bracket_export.build(conn)),
    ]
    for name, fn in steps:
        status.step(name, lambda n=name, f=fn: _write(n, f()))
    if param_report is not None:
        status.step("param_select_club.json", lambda: _write("param_select_club.json", param_report))
    from prediction_market_soccer.ops import team_styles_export, venue_liquidity
    status.step("team_styles.json", team_styles_export.main, required=False)
    status.step("xv_champion.json", compare_champion)
    status.step("venue_liquidity.json", lambda: venue_liquidity.summary(conn), required=False)

    # PDFs use exactly the already-computed report, never a second independent freeze.
    def pdf(key, module):
        if key not in reports:
            raise RuntimeError(f"{key} report unavailable")
        path = CONFIG.paths.output / f"{key}_report.pdf"
        module.build_pdf(reports[key], str(path))
        atomic_bytes(CONFIG.paths.frontend_data / path.name, path.read_bytes())
    status.step("performance_report.pdf", lambda: pdf("performance", performance_report))
    status.step("risk_report.pdf", lambda: pdf("risk", risk_report))
    if args.with_sweep:
        from prediction_market_soccer.ops import param_sweep
        status.step("param_sweep.json", lambda: _write("param_sweep.json", param_sweep.run(conn)), required=False)
    from prediction_market_soccer.ops import frontend_export
    status.step("frontend_overview.json", lambda: _write("frontend_overview.json", frontend_export.build(conn)))


from prediction_market_soccer.ops.maintenance_gate import writer


@writer
def _prewarm_daily_models(conn) -> dict:
    """First-eligible daily model for every competition with a fixture still to play today."""
    from datetime import datetime, timedelta, timezone
    from prediction_market_soccer.config.leagues import active
    from prediction_market_soccer.model.observed_strength import (
        build_observed_strength, ObservedInputsUnavailable)
    now = datetime.now(timezone.utc)
    horizon = (now + timedelta(hours=24)).isoformat()
    due = {r[0] for r in conn.execute(
        "SELECT DISTINCT league_id FROM fixture WHERE status_short='NS' AND kickoff_ts BETWEEN ? AND ?",
        (now.isoformat(), horizon))}
    out = {}
    for comp in active():
        if comp.api_football_id not in due:
            continue
        for attempt in (1, 2):     # the first build of the UTC day always costs one round
            try:
                model = build_observed_strength(conn, datetime.now(timezone.utc).isoformat(), comp.key)
                connection = getattr(model, "observed_connection", None)
                if connection is not None:
                    connection.close()
                out[comp.key] = f"ok(attempt {attempt})"
                break
            except ObservedInputsUnavailable as exc:
                out[comp.key] = f"unavailable: {str(exc)[:60]}"
            except Exception as exc:  # noqa: BLE001 — one competition must not stop the rest
                out[comp.key] = f"{type(exc).__name__}: {str(exc)[:60]}"
                break
    print(f"[daily_models] {out}")
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--with-form", action="store_true")
    ap.add_argument("--with-sweep", action="store_true")
    ap.add_argument("--with-fc-download", action="store_true")
    args = ap.parse_args(argv)
    from prediction_market_soccer.ops.proc_lock import acquire_or_exit
    acquire_or_exit("refresh_all", busy_exit=75)
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ops.export_stage import ExportStage
    conn = store.init_db()
    status = RunStatus("full_refresh")
    settled = conn.execute("SELECT COUNT(*) FROM fixture WHERE status_short IN ('FT','AET','PEN') "
                           "AND home_goals IS NOT NULL").fetchone()[0]
    try:
        with ExportStage() as stage:
            _refresh(conn, args, status)
            if status.failed:
                raise RuntimeError("Required refresh steps failed; retained the last complete export batch")
            stage.promote()
        # This is the conservative input count captured before building this
        # batch, not a fresh count that could include results arriving mid-run.
        # Optional collection gaps keep state=degraded but do not undo a
        # successfully validated and promoted required report batch.
        from prediction_market_soccer.ops.run_status import utc_now
        status.doc["published_input"] = {
            "schema_version": 1, "attempt_at": status.doc["last_attempt_at"],
            "published_at": utc_now(), "settled_results": settled,
        }
        status.finish(input_value=settled)
        # A successful full refresh also satisfies the background report watermark.
        atomic_bytes(CONFIG.paths.output / ".settle_reports_watermark", str(settled).encode())
        print("[refresh] complete; batch ready for sync/build/deploy")
    except BaseException as exc:
        status.finish(error=exc)
        raise
    finally:
        conn.close()
        publish_operations()
        from prediction_market_soccer.ops.proc_lock import release
        release("refresh_all")


def _payload_upcoming(conn):
    from datetime import datetime, timezone
    from prediction_market_soccer.ops import upcoming_export
    rows = upcoming_export.build(limit=16, conn=conn)
    from prediction_market_soccer.ops.export_status import source_as_of, data_status
    return {"as_of": datetime.now(timezone.utc).isoformat(), "n": len(rows),
            "note": "Venue availability and model coverage are reported per match.",
            "source_as_of": source_as_of(rows), "data_status": data_status(rows),
            "matches": rows, "recent_finished": upcoming_export.recent_finished(conn)}


if __name__ == "__main__":
    main()
