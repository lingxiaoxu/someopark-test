"""Hourly pipeline orchestrator (plan 02 §3.3, 05 §1/§4).

Ties the whole read-side pipeline together, idempotently:

    ingest (results + topscorers, watermark-gated → cheap)
      → team strength + Bayesian update from results
      → tournament / golden-boot / match pricing  → data/output/latest.json
      → cross-venue champion monitor (Global)      → data/output/xv_champion.json
      → OOS calibration on played matches          → data/output/oos_report.json
      → structured run summary + API request accounting

Every step is safe to re-run: ingest skips fresh resources (0 API calls), the
model is deterministic, and all writes are upserts/overwrites. NOTHING here
places an order — execution stays behind the venue trading gates.

Scheduling (plan 05 §4): run @hourly from cron/launchd, or use ``--loop``.

    # crontab — top of every hour, env loaded
    0 * * * * cd /Users/xuling/code/someopark-test && set -a && . prediction_market_soccer/.env && set +a && \
      conda run -n someopark_run python -m prediction_market_soccer.jobs.hourly_job >> prediction_market_soccer/data/logs/hourly.log 2>&1

    # or a self-contained loop (foreground)
    conda run -n someopark_run python -m prediction_market_soccer.jobs.hourly_job --loop 3600
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG

log = logging.getLogger("hourly_job")


def _setup_logging() -> None:
    CONFIG.paths.ensure()
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.FileHandler(CONFIG.paths.logs / "hourly.log")
    fh.setFormatter(fmt)
    log.addHandler(sh)
    log.addHandler(fh)


def run_once(*, dry_run: bool = False, ingest: bool = True, emit_frontend: bool = False,
             n_sims: int = 50_000) -> dict:
    """One pipeline pass. Returns a summary dict (also logged)."""
    _setup_logging()
    t0 = time.monotonic()
    run_ts = datetime.now(timezone.utc).isoformat()
    log.info("hourly_job START (dry_run=%s, ingest=%s)", dry_run, emit_frontend)

    from prediction_market_soccer.ingest import store
    conn = store.init_db()
    reqs_before = store.monthly_request_count(conn)

    # 1) Incremental ingest (watermark-gated; results + topscorers). Skipped on dry-run.
    if ingest and not dry_run:
        from prediction_market_soccer.ingest import soccer_ingest
        try:
            soccer_ingest.run("results")  # fixtures + events + topscorers
        except Exception as e:  # ingest must never crash the whole job
            log.warning("ingest failed (continuing on stored data): %s", e)

    # 2) Model run (strength + result-update → tournament/golden-boot/pricing).
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.run_model import build_payload, write_outputs
    prior = load_prior()
    payload = build_payload(prior, n_sims=n_sims, seed=CONFIG.model.random_seed, update_results=True)
    if not dry_run:
        write_outputs(payload, emit_frontend=emit_frontend)
    champ = payload["champion"][0]
    boot = payload["golden_boot"][0]

    # 3) Cross-venue champion monitor (Global, read-only).
    xv_top = None
    try:
        from prediction_market_soccer.strategy.xv_monitor import compare_champion, write_report
        rows = compare_champion(n_sims=min(n_sims, 30_000))
        if not dry_run:
            write_report(rows)
        flagged = [r for r in rows if (r.rel_value_kalshi or r.rel_value_global) is not None]
        if flagged:
            top = flagged[0]
            xv_top = (top.name, top.rel_value_kalshi if top.rel_value_kalshi is not None
                      else top.rel_value_global)
    except Exception as e:
        log.warning("cross-venue monitor skipped: %s", e)

    # 3b) Single-match cross-venue (model vs sharp bookmaker de-vig; US when live).
    n_match_xv = 0
    try:
        from prediction_market_soccer.strategy.xv_monitor import compare_matches
        mrows = compare_matches(limit=12)
        n_match_xv = len(mrows)
        if not dry_run:
            import json as _json
            (CONFIG.paths.output / "xv_matches.json").write_text(
                _json.dumps(mrows, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("single-match cross-venue skipped: %s", e)

    # 3c) Champion strategy signals (calibration-gated; refuses uncalibrated edge).
    sig_summary = None
    try:
        from prediction_market_soccer.exec.executor import run as run_signals
        sg = run_signals()
        sig_summary = {"tradable": len(sg["tradable"]), "blocked": len(sg["blocked"])}
    except Exception as e:
        log.warning("champion signals skipped: %s", e)

    # 3d) In-play signals if any match is live (live poller handles high-frequency).
    inplay_n = 0
    try:
        from prediction_market_soccer.jobs.live_poller import poll_once
        ip = poll_once(conn=conn, ingest=(not dry_run))
        inplay_n = ip["n_signals"]
        if ip["n_live"]:
            log.info("LIVE: %d match(es), %d in-play signals", ip["n_live"], inplay_n)
    except Exception as e:
        log.warning("in-play check skipped: %s", e)

    # 4) OOS calibration on played matches.
    oos = None
    try:
        from prediction_market_soccer.model.oos_eval import evaluate
        rep = evaluate(conn=conn)
        oos = {"n": rep.n_matches, "brier": rep.brier}
    except Exception as e:
        log.warning("OOS eval skipped: %s", e)

    # 5) Health check (plan 05 §5).
    health = None
    try:
        from prediction_market_soccer.ops.monitor import health_report
        rep = health_report(conn=conn)
        health = rep.worst
        for c in rep.checks:
            if c.level != "OK":
                log.warning("health %s: %s — %s", c.level, c.name, c.detail)
    except Exception as e:
        log.warning("health check skipped: %s", e)

    reqs_after = store.monthly_request_count(conn)
    summary = {
        "health": health,
        "match_xv": n_match_xv,
        "champion_signals": sig_summary,
        "inplay_signals": inplay_n,
        "run_ts": run_ts,
        "elapsed_s": round(time.monotonic() - t0, 1),
        "dry_run": dry_run,
        "top_champion": (champ["name"], champ["p_champion"]),
        "top_golden_boot": (boot["name"], boot["p_golden_boot"]),
        "xv_top_divergence": xv_top,
        "oos": oos,
        "api_requests_used": reqs_after - reqs_before,
        "api_requests_month": reqs_after,
    }
    log.info("hourly_job DONE %.1fs | champ=%s %.1f%% | boot=%s %.1f%% | xv=%s | "
             "match_xv=%d | champ_sig=%s | inplay=%d | oos=%s | reqs=%d (mo %d)",
             summary["elapsed_s"], champ["name"], champ["p_champion"] * 100,
             boot["name"], boot["p_golden_boot"] * 100, xv_top, n_match_xv, sig_summary,
             inplay_n, oos, summary["api_requests_used"], reqs_after)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Club-football hourly pipeline (all enabled competitions)")
    ap.add_argument("--dry-run", action="store_true", help="compute but write nothing, no API calls")
    ap.add_argument("--no-ingest", action="store_true", help="skip API ingest, use stored data")
    ap.add_argument("--emit-frontend", action="store_true", help="mirror model JSON to frontend")
    ap.add_argument("--n-sims", type=int, default=50_000)
    ap.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                    help="run forever every N seconds instead of once")
    args = ap.parse_args()

    def _go():
        return run_once(dry_run=args.dry_run, ingest=not args.no_ingest,
                        emit_frontend=args.emit_frontend, n_sims=args.n_sims)

    if args.loop:
        while True:
            try:
                _go()
            except Exception as e:  # keep the loop alive across failures
                logging.getLogger("hourly_job").exception("run failed: %s", e)
            time.sleep(args.loop)
    else:
        _go()


if __name__ == "__main__":
    main()
