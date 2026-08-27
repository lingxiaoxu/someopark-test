"""Season-odds export — the club mirror of the WC reach-round board (plan C-7).

Per enabled competition, one board per season family:
  league kinds   → champion / top_n / relegation (+last)
  swiss_ucl      → champion / top8 / (ro16… once KO exists)
  cup_two_leg    → champion (KO-tree sim)

Each cell: model % + model ¢ + Kalshi ¢ (settlement-aware thin-book marks from
venues/champion_prices) + edge vs Kalshi. Poly ¢ lands with the Phase-3b Global
season-slug resolution (column present, null until then).

→ data/output/season_odds.json (+ frontend copy via _write refresh paths)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import active

_FAMS_LEAGUE = (("champion", "冠军"), ("top_n", "欧战区"), ("relegation", "降级"))
_FAMS_SWISS = (("champion", "冠军"), ("qual_direct", "Top 8"), ("qual_playoff", "9-24 播降"))


def build(conn=None) -> dict:
    from prediction_market_soccer.ingest import store
    conn = conn or store.init_db()

    model_doc = None
    p = CONFIG.paths.output / "soccer_model.json"
    if p.exists():
        try:
            model_doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            model_doc = None
    by_league = {lg["league"]: lg for lg in (model_doc or {}).get("leagues", [])}

    try:
        from prediction_market_soccer.venues.champion_prices import season_odds_cents
    except Exception:
        season_odds_cents = None

    leagues_out = []
    for comp in active():
        lg = by_league.get(comp.key)
        if not lg:
            continue
        cents = {}
        if season_odds_cents is not None:
            try:
                cents = season_odds_cents(comp.key)
            except Exception as e:  # noqa: BLE001
                print(f"[season_odds:{comp.key}] venue cents skipped ({e})")
        fam_defs = _FAMS_SWISS if comp.kind == "swiss_ucl" else _FAMS_LEAGUE
        fam_to_kalshi = {"champion": "champion", "top_n": "top4", "relegation": "relegation",
                         "qual_direct": "top8", "qual_playoff": None}
        boards = []
        for fam, label in fam_defs:
            rows = []
            for so in lg.get("season_odds", []):
                pmod = so.get({"champion": "p_champion", "top_n": "p_top_n",
                               "relegation": "p_relegation", "qual_direct": "p_qual_direct",
                               "qual_playoff": "p_qual_playoff"}[fam])
                if pmod is None:
                    continue
                kfam = fam_to_kalshi.get(fam)
                kc = (cents.get(kfam) or {}).get(so["club_id"]) if kfam else None
                rows.append({
                    "club_id": so["club_id"], "name": so["name"], "zh": so.get("zh", ""),
                    "logo": so.get("logo"),
                    "model_pct": round(pmod, 5), "model_c": round(pmod * 100, 1),
                    "kalshi_c": kc, "poly_c": None,
                    "edge_vs_kalshi": round(pmod * 100 - kc, 1) if kc is not None else None,
                })
            rows.sort(key=lambda r: -r["model_pct"])
            if any(r["model_pct"] > 0 for r in rows):
                boards.append({"family": fam, "label": label,
                               "kalshi_series": comp.kalshi.get(fam_to_kalshi.get(fam) or "", ""),
                               "rows": rows})
        # A competition with no priceable board still gets an entry carrying WHY
        # (待抽签 / 待签表): dropping it made the league silently vanish from the
        # card, which reads as "missing data" rather than "not computable yet".
        leagues_out.append({"league": comp.key, "name": comp.name, "zh": comp.zh,
                            "kind": comp.kind, "boards": boards,
                            "state": lg.get("odds_state", "ok")})

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "note_key": "notes.seasonOdds",
        "note": "model = season MC (league_season/ucl_phase); kalshi_c = settlement-aware "
                "thin-book marks (champion: last-traded preference); edge = model¢ − kalshi¢. "
                "poly_c pending Phase-3b Global season slugs.",
        "leagues": leagues_out,
    }


def main() -> None:
    doc = build()
    CONFIG.paths.ensure()
    for d in (CONFIG.paths.output, CONFIG.paths.frontend_data):
        (d / "season_odds.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    n = sum(len(l["boards"]) for l in doc["leagues"])
    print(f"season_odds.json written ({len(doc['leagues'])} leagues, {n} boards)")


if __name__ == "__main__":
    main()
