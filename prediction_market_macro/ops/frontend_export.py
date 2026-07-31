"""ops/frontend_export.py — single writer of public/data/macro_*.json (PLAN §16.3).

The ONLY sanctioned write target outside the macro tree (0-bis whitelist (a)).
Small JSONs only; the frontend macro views (M7) read these at runtime via /data.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.ingest import calendars as cal
from prediction_market_macro.util.periods import kalshi_period_to_key


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
            pr = conn.execute(
                "SELECT asof, model_version, dist_json, ladder_json FROM preds WHERE series=?"
                " AND period=? ORDER BY asof DESC LIMIT 1", (spec.ticker, key)).fetchone()
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
    (out_dir / "macro_board.json").write_text(json.dumps(board, ensure_ascii=False, indent=1))
    written.append("macro_board.json")

    # ── macro_decisions.json: full ledger tail + open positions + marks ──
    dec = [dict(r) for r in conn.execute(
        "SELECT * FROM decisions ORDER BY id DESC LIMIT 200").fetchall()]
    marks = [dict(r) for r in conn.execute(
        "SELECT * FROM marks WHERE ts=(SELECT MAX(ts) FROM marks)").fetchall()]
    (out_dir / "macro_decisions.json").write_text(json.dumps(
        {"generated_at": now.isoformat(), "decisions": dec, "latest_marks": marks},
        ensure_ascii=False, indent=1))
    written.append("macro_decisions.json")

    # ── macro_coverage.json: lifecycle matrix + MISSED + alerts tail ──
    cov = [dict(r) for r in conn.execute(
        "SELECT * FROM coverage ORDER BY series, period").fetchall()]
    missed = [dict(r) for r in conn.execute(
        "SELECT * FROM runs WHERE status='MISSED' ORDER BY due_ts DESC LIMIT 50").fetchall()]
    alerts = [dict(r) for r in conn.execute(
        "SELECT * FROM alerts WHERE acked=0 ORDER BY id DESC LIMIT 50").fetchall()]
    (out_dir / "macro_coverage.json").write_text(json.dumps(
        {"generated_at": now.isoformat(), "coverage": cov, "missed": missed,
         "alerts": alerts}, ensure_ascii=False, indent=1))
    written.append("macro_coverage.json")

    # ── macro_health.json: copy from output dir if the health step produced it ──
    hp = settings.output_dir / "macro_health.json"
    if hp.exists():
        (out_dir / "macro_health.json").write_text(hp.read_text())
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
                "SELECT asof, dist_json, ladder_json FROM preds WHERE series=? AND period=?"
                " ORDER BY asof DESC LIMIT 1", (spec.ticker, key)).fetchone()
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
            rows.append({"series": spec.ticker, "period": key, "asof": pr["asof"],
                         "gap_norm": round(gap, 4) if gap is not None else None,
                         "n_legs": len(legs)})
    rows.sort(key=lambda x: -(x["gap_norm"] or -1))
    (out_dir / "macro_divergence.json").write_text(json.dumps(
        {"generated_at": now.isoformat(), "rows": rows}, ensure_ascii=False, indent=1))
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
    (out_dir / "macro_performance.json").write_text(json.dumps(perf, ensure_ascii=False, indent=1))
    written.append("macro_performance.json")

    # ── macro_oos.json — calibration replays ONLY (experiments also holds non-OOS
    # rows: 'bankroll' balance cache, 'dfm_gate' — those are not calibration cards) ──
    exps = [dict(r) for r in conn.execute(
        "SELECT * FROM experiments WHERE name LIKE '%replay%'"
        " ORDER BY created_ts DESC LIMIT 20").fetchall()]
    gates = [dict(r) for r in conn.execute(
        "SELECT * FROM experiments WHERE name IN ('dfm_gate','series_gate')"
        " ORDER BY created_ts DESC LIMIT 10").fetchall()]
    (out_dir / "macro_oos.json").write_text(json.dumps(
        {"generated_at": now.isoformat(), "experiments": exps, "component_gates": gates,
         "gate_note": "brier_model must beat brier_market before real money (paper until then)"},
        ensure_ascii=False, indent=1))
    written.append("macro_oos.json")

    # ── macro_risk.json ──
    riskdoc = {"generated_at": now.isoformat(), "limits": _risk.LIMITS,
               "scenario": _risk.scenario_var(conn),
               "open_exposure": _risk._open_exposure(conn)}
    (out_dir / "macro_risk.json").write_text(json.dumps(riskdoc, ensure_ascii=False, indent=1))
    written.append("macro_risk.json")

    # ── macro_fed.json: meeting-level detail with evidence chain ──
    meetings = []
    for r in conn.execute(
            "SELECT period, asof, dist_json, inputs_json FROM preds WHERE series='KXFEDDECISION'"
            " AND asof=(SELECT MAX(asof) FROM preds p2 WHERE p2.series='KXFEDDECISION'"
            " AND p2.period=preds.period) ORDER BY period").fetchall():
        meetings.append({"period": r["period"], "asof": r["asof"],
                         "probs": json.loads(r["dist_json"]).get("probs"),
                         "inputs": json.loads(r["inputs_json"] or "{}")})
    (out_dir / "macro_fed.json").write_text(json.dumps(
        {"generated_at": now.isoformat(), "meetings": meetings}, ensure_ascii=False, indent=1))
    written.append("macro_fed.json")
    return ",".join(written)
