"""ops/health_export.py — the health surface, and the C-33 orphan-output ledger.

Two jobs, and they are the same job.

**1. Write health.json.** ``ops/monitor`` has always been able to build a health
report; nothing ever called it. It is in no pipeline — not ``refresh_all``, not
``live_refresh``, not any of the three plists — so ``health.json`` was named in the
plan and then never existed. This module is the entry point that produces it, and
it is the ONLY writer of that file.

**2. Keep the orphan list honest.** ``TRANSFORM_PLAN`` C-33 enumerates the backend
outputs no frontend view consumes and rules on each one. A plan row saying "照常生成"
is not a mechanism: four of those outputs were simply never produced, and nothing
noticed, because a file that is missing looks identical to a file nobody reads. So
the rulings live here as ``OUTPUT_REGISTRY`` — executable, not prose — and the
monitor checks them every run. A "live" output that stops appearing raises an alert;
a "struck" output is struck for a written reason, in code, where the next person to
wonder why it is missing will actually look.

Run:
    python -m prediction_market_soccer.ops.health_export                  # health.json
    python -m prediction_market_soccer.ops.health_export --scan-venues    # + R6 alias scan
    python -m prediction_market_soccer.ops.health_export --with-walkforward
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.ops import monitor

# ── C-33: every planned backend output, with its ruling ──────────────────────
# status:
#   live      — produced, and its absence/staleness is a monitored failure
#   deferred  — planned, not due yet; absence is correct until the trigger fires
#   struck    — decided NOT to produce in the club edition; `why` is the ruling


@dataclass(frozen=True)
class PlannedOutput:
    name: str
    producer: str
    status: str
    why: str
    max_age_h: float | None = None      # None ⇒ presence-only (no cadence to be late against)
    on_missing: str = "ALERT"
    trigger: str = ""                   # deferred only: what makes it due
    graded: bool = True                 # False ⇒ listed for the record, never scored


OUTPUT_REGISTRY: tuple[PlannedOutput, ...] = (
    PlannedOutput(
        "latest.json", "model.run_model.refresh_model", "live",
        "Per-competition model archive; model_run_<ts>.json keeps the last 10 as history.",
        max_age_h=24.0),
    PlannedOutput(
        "calibration.json", "ops.calibrate_fit (via refresh_all)", "live",
        "Internal consumer only (risk_report / performance_report / every priced export "
        "read it); the frontend deliberately never fetches it.",
        max_age_h=24.0),
    PlannedOutput(
        "match_signals.json", "exec.executor.build_match_signals (via refresh_all)", "live",
        "Pre-match decision payload. No frontend card reads it yet — wire one when a "
        "signals view lands; the file is the contract that makes that a frontend-only change.",
        max_age_h=24.0),
    PlannedOutput(
        "xv_champion.json", "strategy.xv_monitor.compare_champion (via refresh_all)", "live",
        "Per-competition champion divergence. Kept for research value; the WC's dead "
        "getWCChampionXV fetcher was deliberately not mirrored into soccerApi.ts.",
        max_age_h=24.0),
    PlannedOutput(
        "health.json", "ops.health_export (this module)", "live",
        "Ops health + the R6 unmapped-market alert + the §3.5 gate roll-up. Internal; "
        "not published to the frontend. Ungraded on purpose: it is this module's own "
        "output, written moments after the inventory is taken, so scoring it would only "
        "ever report the state of the previous run.",
        max_age_h=None, graded=False),
    PlannedOutput(
        "walkforward_eval.json", "ops.walkforward_eval", "live",
        "PIT validation of the Elo result-update, and the one C-33 output the club edition "
        "makes MORE valuable than the WC did: with 34-38 rounds a season nearly every match "
        "has prior results (3,923 of 4,050 measured), where the WC sample was mostly first "
        "games and the harness was structurally inert. Kept off the per-minute path — the "
        "harness replays every earlier match for every match, so it is quadratic and the "
        "full grid measures ~57s on the current sample — but that is a daily-cadence run, "
        "not a research-only one. `--with-walkforward`.",
        max_age_h=24.0 * 7, on_missing="WARN"),
    PlannedOutput(
        "bracket.json", "ops.cup_bracket_export", "live",
        "C-18 reverses the WC's dead knockout_bracket loop and makes this the BracketView "
        "data source. The exporter exists and writes the file, but no pipeline calls it, "
        "so the reversal is not closed yet.",
        max_age_h=24.0, on_missing="WARN"),
    PlannedOutput(
        "param_selected.json", "ops.param_sweep", "deferred",
        "The WC copy was archived out on purpose (inheriting WC-tuned parameters would "
        "have silently re-tuned the club model). The club file appears only once the sweep "
        "is enabled; until then config runs on hand-set defaults, which is correct.",
        trigger="param_sweep enabled (~6 weeks after launch, TRANSFORM_PLAN step 21)"),
    PlannedOutput(
        "signals.json", "exec.executor.run", "struck",
        "Superseded and unproducible. (1) xv_champion.json already carries per-competition "
        "model-vs-Kalshi champion divergence, with a Shin de-vig this path never had; "
        "match_signals.json carries the gated per-match decision layer. (2) Its producer is "
        "dead against the club contract: xv_monitor.compare_champion() returns a payload "
        "dict, while generate_champion_signals still walks it as WC row objects (r.p_kalshi), "
        "so calling it raises AttributeError. (3) Its gate, _calibration_ok('champion'), "
        "reads the GLOBAL OOS Brier — §3.5 replaced that with per-competition gates, so "
        "reviving it as-is would emit champion signals under a gate the plan retired."),
    PlannedOutput(
        "inplay_opportunities.json", "jobs.live_poller.poll_once", "struck",
        "Duplicate in-play truth. launchd runs ops/live_refresh.sh → ops.live_refresh → "
        "ops.inplay_export.build(), which calls the SAME strategy.inplay_arb."
        "find_opportunities and writes inplay_live.json with the live model state attached. "
        "live_poller is not in the club pipeline; two writers of the same opportunities on "
        "two cadences is how the two views start disagreeing."),
    PlannedOutput(
        "inplay_signals.json", "jobs.live_poller.poll_once", "struck",
        "Same ruling as inplay_opportunities.json, same producer: the tactics signals "
        "(draw take-profit, convergence take-profit, xG momentum) are already embedded "
        "per-match in inplay_live.json / inplay_live_advance.json."),
)


def output_inventory() -> tuple[list[dict], monitor.Check]:
    """Presence + freshness of every planned output → (rows, one roll-up Check).

    Struck and deferred outputs are reported but never graded: their absence is the
    decision, so grading them would train the reader to ignore this check.
    """
    out_dir = CONFIG.paths.output
    rows: list[dict] = []
    missing: list[str] = []
    stale: list[str] = []
    worst = "OK"
    order = {"OK": 0, "WARN": 1, "ALERT": 2}
    for spec in OUTPUT_REGISTRY:
        p = out_dir / spec.name
        exists = p.exists()
        age_h = ((time.time() - p.stat().st_mtime) / 3600.0) if exists else None
        row = {"name": spec.name, "producer": spec.producer, "status": spec.status,
               "exists": exists, "age_hours": round(age_h, 2) if age_h is not None else None,
               "why": spec.why}
        if spec.trigger:
            row["trigger"] = spec.trigger
        if spec.status == "live" and spec.graded:
            if not exists:
                row["verdict"] = spec.on_missing
                missing.append(spec.name)
            elif spec.max_age_h is not None and age_h is not None and age_h > spec.max_age_h:
                row["verdict"] = "WARN"
                stale.append(f"{spec.name} ({age_h:.0f}h)")
            else:
                row["verdict"] = "OK"
            if order[row["verdict"]] > order[worst]:
                worst = row["verdict"]
        else:
            row["verdict"] = "N/A"
        rows.append(row)

    live = [s for s in OUTPUT_REGISTRY if s.status == "live" and s.graded]
    struck = [s.name for s in OUTPUT_REGISTRY if s.status == "struck"]
    detail = f"{len(live) - len(missing)}/{len(live)} planned outputs present"
    if missing:
        detail += f"; MISSING: {', '.join(missing)}"
    if stale:
        detail += f"; stale: {', '.join(stale)}"
    if struck:
        detail += f"; struck by C-33: {', '.join(struck)}"
    return rows, monitor.Check("planned_outputs", worst, float(len(missing)), detail)


def scan_venues(conn) -> dict:
    """R6 producer: walk each venue's live listings and record what resolved to nothing.

    Opt-in because it hits the venues. Kalshi needs one discovery per competition
    (series tickers are per-league); Polymarket US resolves every competition in a
    single listing pass. Each competition is guarded on its own — a venue outage on
    one must not cost the scan the other eleven.
    """
    from prediction_market_soccer.config.leagues import active

    recorded: dict[str, object] = {}
    try:
        from prediction_market_soccer.venues.kalshi.discovery import KalshiDiscovery
    except Exception as e:                                          # noqa: BLE001
        recorded["kalshi"] = f"unavailable ({e})"
        KalshiDiscovery = None                                      # type: ignore[assignment]
    if KalshiDiscovery is not None:
        for comp in active():
            try:
                d = KalshiDiscovery(comp.key)
                d.match_index()          # per-match 3-way listings
                d.champion_markets()     # season champion listings
                recorded[f"kalshi/{comp.key}"] = monitor.record_unmapped_from(
                    conn, "kalshi", comp.key, d)
            except Exception as e:                                  # noqa: BLE001
                recorded[f"kalshi/{comp.key}"] = f"skipped ({e})"
    try:
        from prediction_market_soccer.venues.polymarket_us.discovery import PolymarketUSDiscovery
        d = PolymarketUSDiscovery()
        d.code_map()                     # one listing pass across every competition
        recorded["poly_us"] = monitor.record_unmapped_from(conn, "poly_us", "", d)
    except Exception as e:                                          # noqa: BLE001
        recorded["poly_us"] = f"skipped ({e})"
    return recorded


@dataclass
class HealthDoc:
    ts: str
    worst: str
    checks: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    gates: list = field(default_factory=list)


def _gate_rows() -> list[dict]:
    """One row per enabled competition: may it trade, and on whose calibrator (§3.5)."""
    from prediction_market_soccer.config.leagues import active
    from prediction_market_soccer.model.probability_calibration import (
        PER_LEAGUE_MIN_N, gate_open_for, load_calibration)

    cal = load_calibration() or {}
    per = cal.get("per_league") or {}
    rows = []
    for c in active():
        f = per.get(c.key) or {}
        rows.append({
            "league": c.key, "name": c.name, "zh": c.zh,
            "gate_open": bool(gate_open_for(cal, c.key)),
            "n": f.get("n"), "min_n": PER_LEAGUE_MIN_N,
            "cold_start": bool(f.get("cold_start")),
            "applies": f.get("applies") or ("pooled" if cal else None),
            "calibrated_brier": f.get("calibrated_brier"),
            "uniform_brier": f.get("uniform_brier") or cal.get("uniform_brier"),
        })
    return rows


def build(conn=None) -> HealthDoc:
    """The health.json payload. Pure read — no venue calls, no writes."""
    from prediction_market_soccer.ingest import store

    conn = conn or store.init_db()
    rep = monitor.health_report(conn)
    out_rows, out_check = output_inventory()
    checks = list(rep.checks) + [out_check]
    order = {"OK": 0, "WARN": 1, "ALERT": 2}
    worst = max((c.level for c in checks), key=lambda l: order[l], default="OK")
    return HealthDoc(ts=rep.ts, worst=worst,
                     checks=[c.__dict__ for c in checks],
                     outputs=out_rows, gates=_gate_rows())


def write(doc: HealthDoc):
    """health.json → data/output only. C-33 keeps it an internal surface: it names
    files and producers, which is ops detail, not something to ship to the browser."""
    CONFIG.paths.ensure()
    out = CONFIG.paths.output / "health.json"
    out.write_text(json.dumps(doc.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Health export (health.json) + C-33 output ledger")
    ap.add_argument("--scan-venues", action="store_true",
                    help="hit Kalshi/Poly US listings first and record unresolvable club "
                         "labels into unmapped_market (feeds the R6 alert)")
    ap.add_argument("--with-walkforward", action="store_true",
                    help="also regenerate walkforward_eval.json (~1min: quadratic in the "
                         "settled sample — daily cadence, not per-refresh)")
    args = ap.parse_args()

    from prediction_market_soccer.ingest import store
    conn = store.init_db()

    if args.scan_venues:
        print("R6 venue alias scan:")
        for k, v in scan_venues(conn).items():
            print(f"  {k:<28} {v}")

    if args.with_walkforward:
        from prediction_market_soccer.ops import walkforward_eval
        try:
            walkforward_eval.main()
        except Exception as e:                                      # noqa: BLE001
            print(f"  ✗ walkforward_eval.json: {e}")

    doc = build(conn)
    out = write(doc)

    icon = {"OK": "✅", "WARN": "⚠️", "ALERT": "🚨", "N/A": "·"}
    print(f"HEALTH: {doc.worst}")
    for c in doc.checks:
        print(f"  {icon[c['level']]} {c['name']:<20} {c['detail']}")
    print("planned outputs (C-33):")
    for r in doc.outputs:
        age = f"{r['age_hours']:.0f}h" if r["age_hours"] is not None else "—"
        print(f"  {icon.get(r['verdict'], '·')} {r['name']:<28} {r['status']:<9} age={age:<6} "
              f"{r['producer']}")
    n_open = sum(1 for g in doc.gates if g["gate_open"])
    print(f"calibration gates (§3.5): {n_open}/{len(doc.gates)} open")
    for g in doc.gates:
        state = "OPEN" if g["gate_open"] else ("cold-start" if g["cold_start"] else "shut")
        print(f"  {g['league']:<14} n={g['n'] if g['n'] is not None else '-':<5} "
              f"applies={str(g['applies']):<7} {state}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
