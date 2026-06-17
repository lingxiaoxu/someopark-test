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
    ap.add_argument("--with-fc-download", action="store_true",
                    help="re-download the latest EA FC 26 ratings from Kaggle (needs KAGGLE_API_TOKEN)")
    args = ap.parse_args()

    from prediction_market.ingest import store
    conn = store.init_db()

    if args.ingest:
        from prediction_market.ingest.api_football import ApiFootball
        from prediction_market.ingest import soccer_ingest as si
        api = ApiFootball(conn)
        try:
            si.sync_results(api, conn)            # newly finished matches → bigger OOS sample
            si.sync_topscorers(api, conn)         # updated WC goal tallies → golden boot
            si.sync_odds(api, conn, limit=30, include_settled=True)   # market reference incl. settled
            # Keep recent-form current with WC results EVERY run (0 API calls — the
            # fixtures are already synced); the weekly --with-form pull adds friendlies.
            si.project_wc_results_to_nt_recent(conn)
            if args.with_form:
                si.sync_nt_recent(api, conn)      # refresh recent-form inputs (friendlies/qualifiers)
        except Exception as e:
            print(f"[refresh] ingest partial/failed (continuing with stored data): {e}")

    # EA FC 26 talent ratings → golden-boot / squad prior. Re-ingest from the local
    # CSV every run (cheap, idempotent); optionally re-download the latest first.
    from prediction_market.ingest import fc_ingest
    if args.with_fc_download:
        try:
            fc_ingest.download_fc26()
            print("  ✓ EA FC 26 ratings re-downloaded from Kaggle")
        except Exception as e:
            print(f"  ✗ FC download (continuing with stored CSV): {e}")
    try:
        n_fc = fc_ingest.ingest_fc_players(conn)
        print(f"  ✓ fc_player ({n_fc} players mapped to WC teams)")
    except Exception as e:
        print(f"  ✗ fc_player ingest (golden boot falls back to seed): {e}")

    n_settled = conn.execute(
        "SELECT COUNT(*) n FROM fixture WHERE status_short IN ('FT','AET','PEN') AND home_goals IS NOT NULL"
    ).fetchone()["n"]
    print(f"[refresh] OOS sample is now {n_settled} settled matches (dynamic — grows automatically)")

    # Refit the probability calibration FIRST (on the current sample) so the gate
    # and the calibrated predictions below all use the fresh map.
    from prediction_market.ops import calibrate_fit
    try:
        _write("calibration.json", calibrate_fit.fit(conn))
        print("  ✓ calibration.json (re-fit)")
    except Exception as e:
        print(f"  ✗ calibration.json: {e}")

    # Regenerate every export the frontend reads, all on the CURRENT sample.
    from prediction_market.ops import (backtest_export, form_export, frontend_export,
                                       inplay_export, milestone_export, performance_report,
                                       risk_report, squad_export, backfill_milestones, schedule_export,
                                       reach_round_export)
    from prediction_market.strategy.xv_monitor import compare_matches
    from prediction_market.model import oos_eval
    from prediction_market.exec import executor
    from dataclasses import asdict

    def _milestones():
        # Backfill any newly-finished matches' price tracks from venue history, then export.
        try:
            backfill_milestones.backfill(conn)
        except Exception as e:
            print(f"    (milestone backfill skipped: {e})")
        return milestone_export.build(conn)

    steps = [
        ("backtest.json",          lambda: backtest_export.build(conn)),
        ("squad.json",             lambda: squad_export.build(conn)),
        ("form.json",              lambda: form_export.build(conn)),
        ("upcoming.json",          lambda: _payload_upcoming(conn)),
        ("inplay_live.json",       lambda: inplay_export.build(conn, with_venues=False)),
        ("xv_matches.json",        lambda: compare_matches(limit=12)),  # model vs market (NS only)
        ("oos_report.json",        lambda: asdict(oos_eval.evaluate(conn=conn))),  # calibration view
        ("performance_report.json", lambda: asdict(performance_report.build(conn))),
        ("risk_report.json",       lambda: asdict(risk_report.build(conn))),
        ("milestone_marks.json",   _milestones),   # PriceTrack / mark-to-market view
        ("schedule.json",          lambda: schedule_export.build(conn)),  # full group schedule
        ("match_signals.json",     lambda: executor.build_match_signals(conn)),  # daily decision-model bets ($1-capped)
        ("reach_round.json",       lambda: reach_round_export.build(conn)),  # knockout reach-round (advance) product
    ]
    for name, fn in steps:
        try:
            _write(name, fn())
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")

    # Re-simulate champion + golden boot on the LATEST results (strength nudged by
    # finished matches; eliminated teams forced to 0% once the knockouts begin).
    # refresh_champion writes worldcup_model.json to output + frontend dirs itself.
    try:
        from prediction_market.model.run_model import refresh_champion
        pl = refresh_champion()
        top = pl["champion"][0]
        print(f"  ✓ worldcup_model.json (champion refreshed — leader {top['name']} {top['p_champion']:.1%})")
    except Exception as e:
        print(f"  ✗ worldcup_model.json: {e}")

    # Champion model-vs-market divergence (xv_champion.json) — the Champion-Divergence
    # view; compare_champion writes the file itself.
    try:
        from prediction_market.strategy.xv_monitor import compare_champion
        compare_champion()
        print("  ✓ xv_champion.json")
    except Exception as e:
        print(f"  ✗ xv_champion.json: {e}")

    # Render the PDF reports (the "下载报告" view) from the SAME just-built reports, to
    # BOTH dirs, so the full pipeline keeps the PDFs current — not just live_refresh.
    try:
        import shutil
        from prediction_market.ops import performance_report as _pr, risk_report as _rr
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
            "matches": rows,
            "recent_finished": upcoming_export.recent_finished(conn)}


if __name__ == "__main__":
    main()
