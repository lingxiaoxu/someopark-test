"""ops/frontend_export.py — single writer of public/data/macro_*.json (PLAN §16.3).

The ONLY sanctioned write target outside the macro tree (0-bis whitelist (a)).
Small JSONs only; the frontend macro views (M7) read these at runtime via /data.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.ingest import calendars as cal
from prediction_market_macro.util.periods import kalshi_period_to_key


def _sanitize(o):
    """Browsers' JSON.parse rejects bare NaN/Infinity (Python emits them by
    default) — every export goes through here: non-finite floats → null."""
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, dict):
        return {k: _sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_sanitize(v) for v in o]
    return o


def _write(path, obj) -> None:
    path.write_text(json.dumps(_sanitize(obj), ensure_ascii=False, indent=1,
                               allow_nan=False))


def run(conn, settings) -> str:
    now = datetime.now(timezone.utc)
    out_dir = settings.frontend_data
    written = []

    # ── macro_board.json: next releases + latest pred + decision per (series, period) ──
    board = {"generated_at": now.isoformat(), "next_releases": [], "series": {}}
    for spec in REGISTRY.values():
        nr = cal.next_release(spec.calendar, now)
        if nr:
            board["next_releases"].append({
                "series": spec.ticker, "family": spec.family, "cadence": spec.cadence,
                "period": nr.period, "scheduled_ts": nr.scheduled_ts.isoformat(),
                "note": nr.note})
        entries = []
        for r in conn.execute(
                "SELECT DISTINCT period FROM contracts WHERE series=? AND status='active'",
                (spec.ticker,)).fetchall():
            key = kalshi_period_to_key(r["period"])
            if not key:
                continue
            # PRODUCTION model preds only — shadow members (chronos2/bridge/
            # ensemble) must never drive the board display
            pr = conn.execute(
                "SELECT asof, model_version, dist_json, ladder_json FROM preds"
                " WHERE series=? AND period=? AND model_version LIKE ?"
                " ORDER BY asof DESC LIMIT 1",
                (spec.ticker, key, spec.model + "/%")).fetchone()
            de = conn.execute(
                "SELECT ts_utc, kind, fair, ask, net_edge, size_usd, note, structure_json"
                " FROM decisions WHERE series=? AND period=? AND kind IN ('open','pass','exit')"
                " ORDER BY id DESC LIMIT 1", (spec.ticker, key)).fetchone()
            entries.append({
                "period": key,
                "pred": {"asof": pr["asof"], "model": pr["model_version"],
                         "dist": json.loads(pr["dist_json"])} if pr else None,
                "decision": dict(de) if de else None})
        board["series"][spec.ticker] = {"family": spec.family, "cadence": spec.cadence,
                                        "structure": spec.structure, "entries": entries}
    board["next_releases"].sort(key=lambda x: x["scheduled_ts"])
    _write(out_dir / "macro_board.json", board)
    written.append("macro_board.json")

    # ── macro_decisions.json: full ledger tail + open positions + marks ──
    dec = [dict(r) for r in conn.execute(
        "SELECT * FROM decisions ORDER BY id DESC LIMIT 200").fetchall()]
    marks = [dict(r) for r in conn.execute(
        "SELECT * FROM marks WHERE ts=(SELECT MAX(ts) FROM marks)").fetchall()]
    _write(out_dir / "macro_decisions.json",
           {"generated_at": now.isoformat(), "decisions": dec, "latest_marks": marks})
    written.append("macro_decisions.json")

    # ── macro_coverage.json: lifecycle matrix + MISSED + alerts tail ──
    cov = [dict(r) for r in conn.execute(
        "SELECT * FROM coverage ORDER BY series, period").fetchall()]
    missed = [dict(r) for r in conn.execute(
        "SELECT * FROM runs WHERE status='MISSED' ORDER BY due_ts DESC LIMIT 50").fetchall()]
    alerts = [dict(r) for r in conn.execute(
        "SELECT * FROM alerts WHERE acked=0 ORDER BY id DESC LIMIT 50").fetchall()]
    _write(out_dir / "macro_coverage.json",
           {"generated_at": now.isoformat(), "coverage": cov, "missed": missed,
            "alerts": alerts})
    written.append("macro_coverage.json")

    # ── macro_health.json: re-serialize through the sanitizer (never raw-copy) ──
    hp = settings.output_dir / "macro_health.json"
    if hp.exists():
        _write(out_dir / "macro_health.json", json.loads(hp.read_text()))
        written.append("macro_health.json")

    written.append(run_extended(conn, settings))
    return ",".join(written)


def run_extended(conn, settings) -> str:
    """Additional exports: divergence / performance / oos / risk / fed detail."""
    now = datetime.now(timezone.utc)
    out_dir = settings.frontend_data
    written = []

    # ── macro_divergence.json: model vs market gap ranking per (series, period) ──
    from prediction_market_macro.strategy.devig import ladder_implied
    rows = []
    for spec in REGISTRY.values():
        for r in conn.execute(
                "SELECT DISTINCT period FROM contracts WHERE series=? AND status='active'",
                (spec.ticker,)).fetchall():
            key = kalshi_period_to_key(r["period"])
            if not key:
                continue
            pr = conn.execute(
                "SELECT asof, dist_json, ladder_json FROM preds WHERE series=?"
                " AND period=? AND model_version LIKE ? ORDER BY asof DESC LIMIT 1",
                (spec.ticker, key, spec.model + "/%")).fetchone()
            legs = [dict(x) for x in conn.execute(
                "SELECT c.ticker, c.floor_strike strike, q.yes_bid, q.yes_ask FROM contracts c"
                " LEFT JOIN quotes q ON q.ticker=c.ticker AND q.ts="
                " (SELECT MAX(ts) FROM quotes WHERE ticker=c.ticker)"
                " WHERE c.series=? AND c.period=? AND c.status='active'",
                (spec.ticker, r["period"])).fetchall()]
            if pr is None or not legs:
                continue
            gap = None
            if pr["ladder_json"]:
                import math as _m
                pmf = {float(k): v for k, v in json.loads(pr["ladder_json"]).items()}
                mean_model = sum(k * v for k, v in pmf.items())
                impl = ladder_implied(legs)
                if impl["strikes"]:
                    xs = impl["strikes"]
                    mass = impl["pmf"]
                    finite = {k: v for k, v in mass.items() if k != float("inf")}
                    if sum(finite.values()) > 0.3:
                        mean_mkt = sum(k * v for k, v in finite.items()) / sum(finite.values())
                        rng = max(xs) - min(xs) or 1.0
                        gap = abs(mean_model - mean_mkt) / rng
                        if not _m.isfinite(gap):
                            gap = None
            rows.append({"series": spec.ticker, "period": key, "asof": pr["asof"],
                         "gap_norm": round(gap, 4) if gap is not None else None,
                         "n_legs": len(legs)})
    rows.sort(key=lambda x: -(x["gap_norm"] or -1))
    _write(out_dir / "macro_divergence.json", {"generated_at": now.isoformat(), "rows": rows})
    written.append("macro_divergence.json")

    # ── macro_performance.json ──
    from prediction_market_macro.ops import pnl as _pnl, risk as _risk
    perf = _pnl.report(conn)
    marks_total = conn.execute(
        "SELECT ROUND(SUM(pnl_usd),2) s FROM marks WHERE ts=(SELECT MAX(ts) FROM marks)"
    ).fetchone()["s"]
    from prediction_market_macro.venues.kalshi.account import current_bankroll
    perf.update({"generated_at": now.isoformat(), "unrealized_usd": marks_total,
                 "bankroll_usd": current_bankroll(conn), "bankroll_source": "kalshi_demo",
                 "mode": "paper"})
    _write(out_dir / "macro_performance.json", perf)
    written.append("macro_performance.json")

    # ── macro_oos.json — calibration replays ONLY (experiments also holds non-OOS
    # rows: 'bankroll' balance cache, 'dfm_gate' — those are not calibration cards) ──
    # scoring replays only (Brier cards) — decision_replay rows carry ROI metrics
    # and get their own section; latest row per (name, series)
    exps = [dict(r) for r in conn.execute(
        "SELECT * FROM experiments e WHERE name LIKE '%replay%'"
        " AND name != 'decision_replay'"
        " AND created_ts=(SELECT MAX(created_ts) FROM experiments e2"
        "  WHERE e2.name=e.name AND COALESCE(e2.series,'')=COALESCE(e.series,''))"
        " ORDER BY e.series").fetchall()]
    dec_replays = []
    for r in conn.execute(
            "SELECT * FROM experiments e WHERE name='decision_replay'"
            " AND created_ts=(SELECT MAX(created_ts) FROM experiments e2"
            "  WHERE e2.name='decision_replay'"
            "  AND COALESCE(e2.series,'')=COALESCE(e.series,''))"
            " ORDER BY e.series").fetchall():
        try:
            m = json.loads(r["metrics_json"] or "{}")
        except json.JSONDecodeError:
            m = {}
        dec_replays.append({"series": r["series"], "n_trades": m.get("n_trades"),
                            "staked": m.get("staked"), "realized": m.get("realized"),
                            "roi": m.get("roi"), "edge_capture": m.get("edge_capture"),
                            "n_events": m.get("n_events")})
    gates = [dict(r) for r in conn.execute(
        "SELECT * FROM experiments WHERE name IN ('dfm_gate','series_gate')"
        " ORDER BY created_ts DESC LIMIT 40").fetchall()]
    wf_row = conn.execute(
        "SELECT metrics_json FROM experiments WHERE name='daily_walkforward'"
        " ORDER BY created_ts DESC LIMIT 1").fetchone()
    walkforward = None
    if wf_row:
        try:
            w = json.loads(wf_row["metrics_json"])
            walkforward = {k: w.get(k) for k in
                           ("days", "n_trades", "win_rate", "staked", "realized",
                            "roi", "by_series", "lead_buckets", "curve", "note")}
        except json.JSONDecodeError:
            pass
    _write(out_dir / "macro_oos.json",
           {"generated_at": now.isoformat(), "experiments": exps,
            "decision_replays": dec_replays, "component_gates": gates,
            "walkforward": walkforward,
            "gate_note": "brier_model must beat brier_market before real money"
                         " (paper until then)"})
    written.append("macro_oos.json")

    # ── macro_risk.json ──
    riskdoc = {"generated_at": now.isoformat(), "limits": _risk.LIMITS,
               "scenario": _risk.scenario_var(conn),
               "open_exposure": _risk._open_exposure(conn)}
    _write(out_dir / "macro_risk.json", riskdoc)
    written.append("macro_risk.json")

    # ── macro_pricetrack.json: intraday mark history (mother price-track port) ──
    track = [dict(r) for r in conn.execute(
        "SELECT ts, ROUND(SUM(pnl_usd),4) pnl_usd, COUNT(*) n_legs FROM marks"
        " GROUP BY ts ORDER BY ts DESC LIMIT 500").fetchall()]
    track.reverse()
    per_series = [dict(r) for r in conn.execute(
        "SELECT d.series, ROUND(SUM(m.pnl_usd),4) pnl_usd FROM marks m"
        " JOIN decisions d ON d.id=m.decision_id"
        " WHERE m.ts=(SELECT MAX(ts) FROM marks) GROUP BY d.series").fetchall()]
    _write(out_dir / "macro_pricetrack.json",
           {"generated_at": now.isoformat(), "track": track,
            "latest_by_series": per_series})
    written.append("macro_pricetrack.json")

    # ── PDF reports → public/data/macro_reports/ + index (0-bis whitelist (a):
    # "macro_*.json 与 macro PDF") — makes the Reports tile servable via /data ──
    import shutil
    rep_src = settings.output_dir / "reports"
    rep_dst = out_dir / "macro_reports"
    reports = []
    if rep_src.exists():
        rep_dst.mkdir(exist_ok=True)
        pdfs = sorted(rep_src.glob("*.pdf"), key=lambda p: p.stat().st_mtime,
                      reverse=True)[:14]
        for p in pdfs:
            dst = rep_dst / p.name
            if not dst.exists() or dst.stat().st_mtime < p.stat().st_mtime:
                shutil.copy2(p, dst)
            reports.append({"name": p.name, "url": f"/data/macro_reports/{p.name}",
                            "mtime": datetime.fromtimestamp(
                                p.stat().st_mtime, tz=timezone.utc).isoformat(),
                            "kind": "weekly" if "weekly" in p.name else "daily"})
    _write(out_dir / "macro_reports.json",
           {"generated_at": now.isoformat(), "reports": reports})
    written.append("macro_reports.json")

    # ── macro_walkforward.json: the WF lab artifact (sweep + latest daily run) ──
    wf_doc = {"generated_at": now.isoformat()}
    for name, key in (("walkforward_sweep", "sweep"), ("daily_walkforward", "daily")):
        r = conn.execute(
            "SELECT metrics_json, created_ts FROM experiments WHERE name=?"
            " ORDER BY created_ts DESC LIMIT 1", (name,)).fetchone()
        if r:
            try:
                wf_doc[key] = json.loads(r["metrics_json"])
                wf_doc[key + "_ts"] = r["created_ts"]
            except json.JSONDecodeError:
                pass
    _write(out_dir / "macro_walkforward.json", wf_doc)
    written.append("macro_walkforward.json")

    # ── macro_fed.json: meeting-level detail with evidence chain ──
    meetings = []
    for r in conn.execute(
            "SELECT period, asof, dist_json, inputs_json FROM preds"
            " WHERE series='KXFEDDECISION' AND model_version LIKE 'fed/%'"
            " AND asof=(SELECT MAX(asof) FROM preds p2 WHERE p2.series='KXFEDDECISION'"
            " AND p2.model_version LIKE 'fed/%'"
            " AND p2.period=preds.period) ORDER BY period").fetchall():
        meetings.append({"period": r["period"], "asof": r["asof"],
                         "probs": json.loads(r["dist_json"]).get("probs"),
                         "inputs": json.loads(r["inputs_json"] or "{}")})
    _write(out_dir / "macro_fed.json",
           {"generated_at": now.isoformat(), "meetings": meetings})
    written.append("macro_fed.json")
    return ",".join(written)
