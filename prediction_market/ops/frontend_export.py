"""Frontend data contract — one consolidated JSON the someo-park UI can render.

Bundles the STATIC system catalog (interfaces / modes / schedule / I-O / value,
from system_overview.py) together with LIVE snapshots (performance + risk reports
and the headline prediction artifacts) into a single versioned document:

    data/output/frontend_overview.json

This is read-only and side-effect free beyond writing that one file. The frontend
consumes it as the source of truth for the "what is this system / how do I run it
/ is it any good / what's the risk" pages — no need to re-implement any logic
client-side. Re-run after refresh/report jobs to update the snapshot.
"""
from __future__ import annotations

import json
from dataclasses import asdict

from prediction_market.config import CONFIG
from prediction_market.ops import system_overview as ov

SCHEMA_VERSION = "1.0"


def _read_json(name: str):
    p = CONFIG.paths.output / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def build(conn=None, *, as_of: str = "") -> dict:
    """Assemble the consolidated frontend document (static catalog + live snapshots)."""
    from prediction_market.ops import performance_report, risk_report

    conn = conn or __import__("prediction_market.ingest.store", fromlist=["store"]).init_db()
    perf = performance_report.build(conn=conn)
    risk = risk_report.build(conn=conn)

    doc = {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of,
        "headline": ov.HONEST_HEADLINE,

        # ── Static catalog (the "what / how / when" narrative) ──
        "interfaces": [
            {"category": a, "command": f"python -m prediction_market.{b}", "purpose": c}
            for a, b, c in ov.INTERFACES
        ],
        "modes": [{"mode": m, "detail": d} for m, d in ov.MODES],
        "schedule": [{"when": a, "runs": b, "frequency": c} for a, b, c in ov.SCHEDULE],
        "inputs": [{"item": a, "location": b} for a, b in ov.INPUTS],
        "outputs": [{"file": a, "description": b} for a, b in ov.OUTPUTS],
        "output_dir": ov.OUTPUT_DIR,
        "value": ov.VALUE,

        # ── Live snapshots (the "is it any good / what's the risk" data) ──
        "performance": asdict(perf),
        "risk": asdict(risk),

        # ── Headline prediction artifacts (rendered as-is by the UI) ──
        "predictions": {
            "champion": _read_json("xv_champion.json"),
            "match_divergences": _read_json("xv_matches.json"),
            "upcoming": _read_json("upcoming.json"),          # per-match real venue quotes
            "inplay_signals": _read_json("inplay_signals.json"),
            "oos_calibration": _read_json("oos_report.json"),
        },
        "report_pdfs": {
            "performance": "performance_report.pdf",
            "risk": "risk_report.pdf",
        },
    }
    return doc


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Export consolidated frontend overview JSON")
    ap.add_argument("--as-of", default="", help="as-of date label")
    args = ap.parse_args()

    doc = build(as_of=args.as_of)
    CONFIG.paths.ensure()
    out = CONFIG.paths.output / "frontend_overview.json"
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"frontend_overview.json written → {out}")
    print(f"  interfaces : {len(doc['interfaces'])}")
    print(f"  modes      : {len(doc['modes'])}")
    print(f"  outputs    : {len(doc['outputs'])}")
    print(f"  performance: {doc['performance']['n_settled']} settled, Brier {doc['performance']['brier']}")
    print(f"  risk gates : env={doc['risk']['gates']['kalshi_env']}, "
          f"blocked={len(doc['risk']['blocked_summary'])} rails")


if __name__ == "__main__":
    main()
