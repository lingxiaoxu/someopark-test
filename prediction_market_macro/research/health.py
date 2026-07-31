"""research/health.py — daily model health patrol (PLAN §9.6). Runs inside refresh.

Checks (v0.1, grows with the system):
  1. data freshness per source (FRED age vs cadence, quotes age, futures age)
  2. pred freshness + ladder mass sanity for every open (series, period)
  3. ledger replay determinism: recompute 2 random stored preds at their stored asof —
     byte-identical dists or red flag (dependency/code drift detector)
  4. rolling OOS: latest claims replay aggregate from experiments (model vs market)
Output: red/yellow/green per series → macro_health.json + alerts on red.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.ops.predict_all import SERIES_DISPATCH


def daily_health(conn, settings) -> str:
    now = datetime.now(timezone.utc)
    report: dict = {"ts": now.isoformat(), "sources": {}, "series": {}, "flags": []}

    # 1. source freshness
    for sid, max_age_d in (("ICSA", 9), ("CPIAUCSL", 40), ("DFEDTARU", 5), ("UNRATE", 40)):
        r = conn.execute("SELECT MAX(knowledge_time) m FROM fred_obs WHERE sid=?",
                         (sid,)).fetchone()
        age = None
        if r["m"]:
            age = (now - datetime.fromisoformat(r["m"])).days
        report["sources"][sid] = {"latest_kt": r["m"], "age_days": age}
        if age is None or age > max_age_d:
            report["flags"].append(f"stale_source:{sid}:{age}d")
    q = conn.execute("SELECT MAX(ts) m FROM quotes").fetchone()
    q_age_h = (now - datetime.fromisoformat(q["m"])).total_seconds() / 3600 if q["m"] else None
    report["sources"]["kalshi_quotes"] = {"age_hours": round(q_age_h, 1) if q_age_h else None}
    if q_age_h is None or q_age_h > 26:
        report["flags"].append(f"stale_quotes:{q_age_h}")

    # 2+3. per-series: pred freshness, ladder mass, replay determinism
    import importlib
    for spec in REGISTRY.values():
        s_rep = {"status": "green", "notes": []}
        # replay the PRODUCTION model's latest pred only — shadow members (chronos2/*)
        # have their own model_version and would trivially mismatch the dispatch fn
        pr = conn.execute(
            "SELECT * FROM preds WHERE series=? AND model_version LIKE ?"
            " ORDER BY asof DESC LIMIT 1",
            (spec.ticker, spec.model + "/%")).fetchone()
        if pr is None:
            s_rep = {"status": "yellow", "notes": ["no_preds_yet"]}
        else:
            age_h = (now - datetime.fromisoformat(pr["asof"])).total_seconds() / 3600
            if age_h > 26:
                s_rep["status"] = "red"
                s_rep["notes"].append(f"pred_stale:{age_h:.0f}h")
            if pr["ladder_json"]:
                mass = sum(json.loads(pr["ladder_json"]).values())
                if abs(mass - 1.0) > 0.01:
                    s_rep["status"] = "red"
                    s_rep["notes"].append(f"ladder_mass:{mass:.3f}")
            # replay determinism (dependency drift canary)
            disp = SERIES_DISPATCH.get(spec.ticker)
            if disp:
                try:
                    mod = importlib.import_module(disp[0])
                    fn = getattr(mod, disp[1])
                    re_pred = fn(conn, datetime.fromisoformat(pr["asof"]), pr["period"],
                                 series=spec.ticker)
                    if json.dumps(re_pred.dist.to_json()) != pr["dist_json"]:
                        s_rep["status"] = "red"
                        s_rep["notes"].append("replay_mismatch")
                except Exception as e:                            # noqa: BLE001
                    s_rep["status"] = "red"
                    s_rep["notes"].append(f"replay_error:{e}")
        report["series"][spec.ticker] = s_rep
        if s_rep["status"] == "red":
            conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                         (now.isoformat(), "error", "health",
                          f"RED {spec.ticker}: {s_rep['notes']}"))

    # 4. rolling OOS from the latest replay experiment
    ex = conn.execute(
        "SELECT metrics_json, created_ts FROM experiments WHERE name='claims_replay'"
        " ORDER BY created_ts DESC LIMIT 1").fetchone()
    if ex:
        m = json.loads(ex["metrics_json"])
        report["oos_claims"] = m
        if (m.get("brier_model-1h") or 0) > (m.get("brier_market-1h") or 1):
            report["series"]["KXJOBLESSCLAIMS"]["notes"].append(
                "oos_brier_behind_market — stays paper (gate)")

    conn.commit()
    path = settings.output_dir / "macro_health.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    reds = [k for k, v in report["series"].items() if v["status"] == "red"]
    return f"{len(report['series'])} series, red={reds or 'none'}, flags={len(report['flags'])}"
