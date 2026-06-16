"""ops/form_export.py — recent-form view for the frontend.

Ranks all teams by the recent-form index (model/form_strength.py: time-weighted,
friendly-discounted goal difference from recent national-team results) and lists
each team's last few results. Read-only, point-in-time.

Unlike squad strength, the form blend IMPROVED the live OOS Brier (0.7339 → 0.7175
on the settled matches), so it IS used by the live model (cfg.form_blend_weight).

    python -m prediction_market.ops.form_export  →  data/output/form.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market.config import CONFIG


def build(conn=None) -> dict:
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.form_strength import form_index

    conn = conn or store.init_db()
    prior = load_prior()
    name = {t.team_id: t.name for t in prior.teams}
    zh = {t.team_id: t.zh for t in prior.teams}
    idx = form_index(conn)
    ranked = sorted(idx.values(), key=lambda s: -s.form_z)
    teams = []
    for i, s in enumerate(ranked, 1):
        teams.append({
            "rank": i, "team_id": s.team_id, "name": name.get(s.team_id, s.team_id), "zh": zh.get(s.team_id, ""),
            "form_z": s.form_z, "weighted_gd": s.weighted_gd, "n": s.n, "n_friendly": s.n_friendly,
            "recent": [f"{r['gf']}-{r['ga']}{'F' if r['friendly'] else ''}" for r in s.recent],
        })
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "n_teams": len(teams),
        "teams": teams,
        "note": ("Recent national-team form: time-weighted, friendly-discounted goal difference "
                 "from recent results (F = friendly). Used by the live model — it lowered the OOS Brier."),
    }


def main() -> None:
    doc = build()
    CONFIG.paths.ensure()
    (CONFIG.paths.output / "form.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"form.json: {doc['n_teams']} teams")
    for t in doc["teams"][:8]:
        print(f"  #{t['rank']:<2} {t['name']:<14} form_z={t['form_z']:+.2f}  wGD={t['weighted_gd']:+.2f}  recent={t['recent']}")


if __name__ == "__main__":
    main()
