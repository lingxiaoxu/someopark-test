"""ops/refresh_all.py — one command to refresh every export on the CURRENT sample.

The OOS tools (oos_eval, param_sweep, backtest, walkforward) all select "every
finished fixture" dynamically — there is no fixed 15/16 cap. So as more matches
finish, the sample automatically grows; this orchestrator just (optionally) pulls
the latest results/odds and then regenerates all the JSON the frontend reads, so a
single run reflects the larger sample everywhere.

    python -m prediction_market_soccer.ops.refresh_all              # regen exports from stored DB
    python -m prediction_market_soccer.ops.refresh_all --ingest     # + pull new results & missing odds
    python -m prediction_market_soccer.ops.refresh_all --ingest --with-form   # + re-pull recent NT form
    python -m prediction_market_soccer.ops.refresh_all --with-sweep # + re-run the 180-set param sweep (slow)

Then: npm run sync:wc && firebase deploy (or the deploy step) to push it live.
"""
from __future__ import annotations

import argparse
import json

from prediction_market_soccer.config import CONFIG


def _write(name: str, doc) -> None:
    (CONFIG.paths.output / name).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Refresh all prediction exports on the current (growing) sample")
    ap.add_argument("--ingest", action="store_true", help="pull latest finished results + missing odds first")
    ap.add_argument("--with-form", action="store_true", help="also re-pull recent NT results (form) — ~48 requests")
    ap.add_argument("--with-sweep", action="store_true", help="also re-run the 180-set param sweep (slow)")
    ap.add_argument("--with-fc-download", action="store_true",
                    help="re-download the latest EA FC 26 ratings from Kaggle (needs KAGGLE_API_TOKEN)")
    args = ap.parse_args()

    from prediction_market_soccer.ops.proc_lock import acquire_or_exit
    acquire_or_exit("refresh_all")   # single-instance guard (R10)

    from prediction_market_soccer.ingest import store
    conn = store.init_db()

    if args.ingest:
        from prediction_market_soccer.ingest.api_football import ApiFootball
        from prediction_market_soccer.ingest import soccer_ingest as si
        api = ApiFootball(conn)
        from prediction_market_soccer.config.leagues import active as _active

        # Each step in its own guard: a budget-cap stop inside one step must not
        # starve the later steps (results detail once ate the whole cap and left
        # topscorers/odds empty for the day).
        def _step(label, fn):
            try:
                fn()
            except Exception as e:
                print(f"[refresh] ingest step {label} partial/failed (continuing): {e}")

        # NOT wired here on purpose: si.sync_squads. It costs 1 request per club (506
        # clubs ≈ 8% of the 6,500/day budget) and nothing downstream reads what it
        # writes — the only reader of the `squad` table is
        # model.club_aggregation.squad_attack_quality, whose join is
        # COALESCE(squad.team_api_id, player_stat.team_api_id) and therefore a no-op on
        # club data (player_stat.team_api_id already IS the club), and its only caller
        # ops/knockout_export is not in this pipeline. The squad card's positions come
        # from fc_player (ops/squad_export), not from this table. Wire it when a
        # consumer actually lands (injury/rotation signals), and fix two defects in the
        # function first: its default limit=400 cannot cover 506 clubs, and it stamps
        # the watermark even after a BudgetExceededError break, so one partial pull
        # would mark the table fresh for the full 7-day ttl_static.
        _step("results", lambda: si.sync_results(api, conn))
        for _comp in _active():
            _step(f"topscorers:{_comp.key}",
                  lambda c=_comp: si.sync_topscorers(api, conn, c))
        _step("odds", lambda: si.sync_odds(api, conn, limit=30, include_settled=True))
        # Keep club recent-form current EVERY run (0 API calls — league fixtures
        # already synced); the weekly --with-form pull adds cups/Europe per club.
        _step("club_recent", lambda: si.project_results_to_club_recent(conn))
        if args.with_form:
            _step("club_form", lambda: si.sync_club_recent(api, conn))

    # EA FC 26 talent ratings → golden-boot / squad prior. Re-ingest from the local
    # CSV every run (cheap, idempotent); optionally re-download the latest first.
    from prediction_market_soccer.ingest import fc_ingest
    if args.with_fc_download:
        try:
            fc_ingest.download_fc26()
            print("  ✓ EA FC 26 ratings re-downloaded from Kaggle")
        except Exception as e:
            print(f"  ✗ FC download (continuing with stored CSV): {e}")
    try:
        n_fc = fc_ingest.ingest_fc_players(conn)
        print(f"  ✓ fc_player ({n_fc} players mapped to registry clubs)")
    except Exception as e:
        print(f"  ✗ fc_player ingest (golden boot falls back to seed): {e}")

    n_settled = conn.execute(
        "SELECT COUNT(*) n FROM fixture WHERE status_short IN ('FT','AET','PEN') AND home_goals IS NOT NULL"
    ).fetchone()["n"]
    print(f"[refresh] OOS sample is now {n_settled} settled matches (dynamic — grows automatically)")

    # Backfill per-minute price ticks (Poly Global) for newly-settled matches FIRST — the
    # smart-exit cash-out in performance_report / milestone_export reads price_tick, so without
    # this every recent bet silently degrades to hold-to-FT (this job was never pipeline-wired
    # and had stalled, freezing the realised P&L). Network + only_missing → cheap catch-up.
    from prediction_market_soccer.ops import backfill_price_ticks
    try:
        bp = backfill_price_ticks.backfill(conn, only_missing=True)
        print(f"  ✓ price_tick backfill ({bp.get('fixtures', 0)} fixtures, {bp.get('ticks', 0)} ticks)")
    except Exception as e:
        print(f"  ✗ price_tick backfill: {e}")

    # Parameter selection FIRST — a real run, not a dry one, on the day's fresh sample.
    # It used to sit in the artifact list below with dry_run=True, which had two problems:
    # a winning candidate could never actually be adopted (the dry run withheld
    # param_selected.json, the file config.py auto-loads), and it ran AFTER the model
    # build, so even a real adoption would only have taken effect the NEXT day. Running
    # it here means the calibration, the model and every export downstream price with
    # the parameters the new day's data just chose. Discipline is inside run(): adoption
    # only when a candidate beats the incumbent's TEST Brier by >= 0.005 on the
    # per-league time split — most days that means "no change", which is correct.
    from prediction_market_soccer.ops import param_select_club
    _param_report = None
    try:
        _param_report = param_select_club.run(test_days=14, dry_run=False)
        print(f"  ✓ param_select (winner={_param_report.get('winner')}, "
              f"adopted={_param_report.get('adopted')})")
    except Exception as e:
        print(f"  ✗ param_select: {e}")

    # PIT records cache for the Kalshi demo mirror (exec/kalshi_mirror) — daily warm-up;
    # settle_reports refreshes it again after each settle wave.
    try:
        from prediction_market_soccer.exec.kalshi_mirror import build_pit_cache
        print(f"  ✓ pit_records cache: {build_pit_cache(conn)}")
    except Exception as e:
        print(f"  ✗ pit_records cache: {e}")

    # Refit the probability calibration next (on the current sample) so the gate
    # and the calibrated predictions below all use the fresh map.
    from prediction_market_soccer.ops import calibrate_fit
    try:
        _write("calibration.json", calibrate_fit.fit(conn))
        print("  ✓ calibration.json (re-fit)")
    except Exception as e:
        print(f"  ✗ calibration.json: {e}")

    # FREEZE newly-settled bets (append-only) so the Accuracy/PnL, PriceTrack and PnL-report
    # views never rewrite history: each match's decision is computed ONCE with point-in-time
    # strength + point-in-time calibration and persisted. Backfill PRE milestones first so the
    # freeze sees the real pre-match entry quotes (also fixes performance_report reading
    # milestones before the marks-view backfill below).
    from prediction_market_soccer.ops import backfill_milestones as _bm, settle_bets as _sb
    try:
        _bm.backfill(conn)
    except Exception as e:
        print(f"  ✗ milestone backfill (pre-freeze): {e}")
    try:
        _n_froze = _sb.freeze_settled_bets(conn)
        _n_total = conn.execute("SELECT COUNT(*) FROM settled_bet").fetchone()[0]
        print(f"  ✓ settled_bet ledger (+{_n_froze} frozen, {_n_total} total)")
    except Exception as e:
        print(f"  ✗ settled_bet freeze: {e}")

    # Re-simulate every league FIRST (season MC + fixture pricing + ratings cache) —
    # refresh_model writes soccer_model.json to output + frontend itself, and the
    # exports below (season_odds) read it.
    try:
        from prediction_market_soccer.model.run_model import refresh_model
        pl = refresh_model()
        print(f"  ✓ soccer_model.json ({len(pl['leagues'])} leagues refreshed)")
    except Exception as e:
        print(f"  ✗ soccer_model.json: {e}")

    # Regenerate every export the frontend reads, all on the CURRENT sample.
    # Staged bring-up (plan Phases 4-5): steps whose deps aren't rewired yet fail
    # soft with a visible ✗ and come alive as their modules land.
    from prediction_market_soccer.ops import cup_bracket_export, param_select_club
    from prediction_market_soccer.ops import (backtest_export, form_export,
                                       inplay_export, milestone_export, performance_report,
                                       risk_report, squad_export, backfill_milestones,
                                       schedule_export, season_odds_export)
    from prediction_market_soccer.model import oos_eval
    from prediction_market_soccer.exec import executor
    from dataclasses import asdict

    def _milestones():
        try:
            backfill_milestones.backfill(conn)
        except Exception as e:
            print(f"    (milestone backfill skipped: {e})")
        return milestone_export.build(conn)

    from prediction_market_soccer.strategy.xv_monitor import compare_champion, compare_matches
    steps = [
        ("upcoming.json",          lambda: _payload_upcoming(conn)),
        ("xv_matches.json",        lambda: compare_matches(limit=12)),
        ("season_odds.json",       lambda: season_odds_export.build(conn)),
        ("schedule.json",          lambda: schedule_export.build(conn)),
        ("squad.json",             lambda: squad_export.build(conn)),
        ("form.json",              lambda: form_export.build(conn)),
        ("backtest.json",          lambda: backtest_export.build(conn)),
        ("inplay_live.json",       lambda: inplay_export.build(conn, with_venues=False)),
        ("oos_report.json",        lambda: asdict(oos_eval.evaluate(conn=conn))),
        ("performance_report.json", lambda: asdict(performance_report.build(conn))),
        ("risk_report.json",       lambda: asdict(risk_report.build(conn))),
        ("milestone_marks.json",   _milestones),
        ("match_signals.json",     lambda: executor.build_match_signals(conn)),
        ("bracket.json",           lambda: cup_bracket_export.build(conn)),
        # Parameter selection runs with the rest: it trains on past seasons and
        # scores the most recent matches out-of-sample, so it has something to say
        # from day one — there is no sample-size gate to wait out.
        # Written from the REAL run at the top of the pipeline (see above) — running the
        # evaluation twice per day would double the cost and could disagree with itself.
        ("param_select_club.json", lambda: _param_report or param_select_club.run(test_days=14, dry_run=False)),
    ]
    for name, fn in steps:
        try:
            _write(name, fn())
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")

    # Team styles — FC26 playStyles auto-prior (§3.8-d), weekly self-throttled.
    try:
        from prediction_market_soccer.ops import team_styles_export
        team_styles_export.main()
    except Exception as e:
        print(f"  ✗ team_styles.json: {e}")

    # Champion divergence board (writes xv_champion.json itself).
    try:
        compare_champion()
        print("  ✓ xv_champion.json")
    except Exception as e:
        print(f"  ✗ xv_champion.json: {e}")

    # Render the PDF reports (the "下载报告" view) from the SAME just-built reports, to
    # BOTH dirs, so the full pipeline keeps the PDFs current — not just live_refresh.
    try:
        import shutil
        from prediction_market_soccer.ops import performance_report as _pr, risk_report as _rr
        for mod, name in ((_pr, "performance_report.pdf"), (_rr, "risk_report.pdf")):
            rep = mod.build(conn)
            out = CONFIG.paths.output / name
            mod.build_pdf(rep, str(out))
            shutil.copyfile(out, CONFIG.paths.frontend_data / name)
        print("  ✓ performance_report.pdf + risk_report.pdf")
    except Exception as e:
        print(f"  ✗ report PDFs: {e}")

    if args.with_sweep:
        try:
            from prediction_market_soccer.ops import param_sweep
            _write("param_sweep.json", param_sweep.run(conn))
            print("  ✓ param_sweep.json")
        except Exception as e:
            print(f"  ✗ param_sweep.json: {e}")

    # frontend_overview aggregates the above — build last.
    try:
        from prediction_market_soccer.ops import frontend_export
        _write("frontend_overview.json", frontend_export.build(conn))
        print("  ✓ frontend_overview.json")
    except Exception as e:
        print(f"  ✗ frontend_overview.json: {e}")
    print("[refresh] done. Run `npm run sync:soccer && firebase deploy --only hosting` to publish.")


def _payload_upcoming(conn):
    from datetime import datetime, timezone
    from prediction_market_soccer.ops import upcoming_export
    rows = upcoming_export.build(limit=16, conn=conn)
    return {"as_of": datetime.now(timezone.utc).isoformat(), "n": len(rows),
            "note": "Real Kalshi + Polymarket US single-match quotes; venue=null only when unlisted.",
            "matches": rows,
            "recent_finished": upcoming_export.recent_finished(conn)}


if __name__ == "__main__":
    main()
