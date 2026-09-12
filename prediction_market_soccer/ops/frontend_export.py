"""Frontend data contract — one consolidated JSON for the soccer overview card.

Club edition v1: a compact, honest snapshot (the WC version bundled a large
static Chinese catalog from system_overview.py — that copy carries WC wording
and is [闲置] until its full rewrite; the overview card renders model_notes +
this summary instead). Read-only; writes nothing but its return value.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import active

SCHEMA_VERSION = "1.0-soccer"


def _read_json(name: str):
    p = CONFIG.paths.output / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def build(conn=None, *, as_of: str = "") -> dict:
    cal = _read_json("calibration.json") or {}
    model = _read_json("soccer_model.json") or {}
    up = _read_json("upcoming.json") or {}
    leagues = []
    for lg in model.get("leagues", []):
        priced = [row for row in lg.get("season_odds", []) if row.get("p_champion") is not None]
        champion_state = (lg.get("odds_family_states") or {}).get("champion", lg.get("odds_state", "ok"))
        top = max(priced, key=lambda row: row["p_champion"]) if priced and champion_state == "ok" else {}
        leagues.append({
            "league": lg["league"], "name": lg["name"], "zh": lg["zh"], "kind": lg["kind"],
            "n_teams": lg.get("n_teams"), "n_remaining": lg.get("n_remaining"),
            "leader": top.get("name"), "leader_p": top.get("p_champion"),
            **{k: lg.get(k) for k in ("odds_state", "odds_family_states", "availability_reason", "coverage")},
        })
    comps = {c.key: {"kalshi_game": c.kalshi.get("game"),
                     "kalshi_champion": c.kalshi.get("champion")} for c in active()}
    gate_open = bool(cal.get("trade_grade"))
    headline = ("交易闸门开启(校准 Brier {:.3f} ≤ 均匀 {:.3f},n={})".format(
        cal.get("calibrated_brier", float("nan")), cal.get("uniform_brier", 2 / 3), cal.get("n", 0))
        if gate_open else
        "冷启动观察期:校准闸门关闭(paper-only;每联赛攒 30 场结算后逐联赛放行,plan §3.5)")
    # {key, args} beside every hand-written line so the five-language frontend
    # renders them in the reader's own language — the strings below stay as the
    # fallback (and as what a JSON reader sees), but they are Chinese, and the
    # overview card was showing them to English/Japanese/Spanish/French readers.
    # Single source of truth for this sentence: ops/system_overview owns both the
    # Chinese fallback and the {key,args} form. Two inline copies had already
    # drifted — the gate-closed branch used different keys in the two files, so the
    # overview card fell back to raw text whenever the gate was shut.
    from prediction_market_soccer.ops.system_overview import headline_i18n as _hl
    headline_i18n = _hl(gate_open, cal.get("calibrated_brier"),
                        cal.get("uniform_brier") or 2 / 3, cal.get("n", 0))
    from prediction_market_soccer.ops.run_status import safe_summary
    sources = {}
    documents = {"model": model, "upcoming": up,
                 "inplay": _read_json("inplay_live.json"),
                 "performance": _read_json("performance_report.json"),
                 "risk": _read_json("risk_report.json")}
    issues = []
    for key, document in documents.items():
        doc = document or {}
        # Empty live polls legitimately run less often. Daily exports get a six-hour margin.
        max_age = (900 if not doc.get("n_live") else 300) if key == "inplay" else 30 * 3600
        generated_at = doc.get("as_of") or doc.get("ts") or (doc.get("meta") or {}).get("run_ts")
        source_at = doc.get("source_as_of") or generated_at
        sources[key] = {"as_of": source_at, "generated_at": generated_at, "max_age_seconds": max_age}
        if not document:
            issues.append({"code": "source_unavailable", "source": key})
        if (doc.get("data_status") or {}).get("state") in ("degraded", "unavailable"):
            issues.extend((doc.get("data_status") or {}).get("issues", []))
    return {
        "source_as_of": model.get("source_as_of") or (model.get("meta") or {}).get("run_ts"),
        "sources": sources, "operations": safe_summary(),
        "data_status": {"state": "degraded" if issues else "ok", "issues": issues},
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of or datetime.now(timezone.utc).isoformat(),
        "headline": headline,
        "headline_i18n": headline_i18n,
        "mode_key": "overview.paperOnly",
        "gate_open": gate_open,
        "calibration": {k: cal.get(k) for k in ("method", "param", "raw_brier",
                                                "calibrated_brier", "uniform_brier",
                                                "trade_grade", "n")},
        "n_upcoming": up.get("n"),
        "leagues": leagues,
        "series": comps,
        "model_notes": (model.get("meta") or {}).get("model_notes", []),
        "mode": "paper-only（$1 硬上限;双下单开关硬默认 false)",
    }


def main() -> None:
    doc = build()
    CONFIG.paths.ensure()
    from prediction_market_soccer.ops.run_status import atomic_json
    atomic_json(CONFIG.paths.output / "frontend_overview.json", doc)
    print(f"frontend_overview.json: {len(doc['leagues'])} leagues, gate_open={doc['gate_open']}")


if __name__ == "__main__":
    main()
