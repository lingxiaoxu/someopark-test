"""ops/reach_round_export.py — the knockout REACH-ROUND product (plan 20 Part B).

Distinct from the per-match 90-min 3-way: Kalshi's KXWCROUND markets are PER-TEAM,
2-way Yes/No "will <team> qualify for the <round>" — the genuine no-draw "advance"
product. We compare each team's MODEL probability of reaching a round (from the
tournament simulation: p_r16 / p_qf / p_sf / p_final, already in worldcup_model.json)
against the live Kalshi Yes price, and surface the per-team edge.

    python -m prediction_market.ops.reach_round_export → data/output/reach_round.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market.config import CONFIG

# round key → (model prob field in the champion array, human label)
_ROUNDS = {
    "r16": ("p_r16", "Round of 16"),
    "qf": ("p_qf", "Quarterfinals"),
    "sf": ("p_sf", "Semifinals"),
    "final": ("p_final", "Final"),
}


def _load_champion_rows() -> list[dict]:
    """The per-team round probabilities from the published tournament sim. Reads the
    canonical worldcup_model.json (output dir, else the synced frontend dir)."""
    for d in (CONFIG.paths.output, CONFIG.paths.frontend_data):
        p = d / "worldcup_model.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8")).get("champion", []) or []
            except Exception:
                continue
    return []


def build(conn=None) -> dict:
    from prediction_market.venues.champion_prices import REACH_ROUND_SERIES, reach_round_cents
    champ = _load_champion_rows()
    cents = reach_round_cents()
    theta = CONFIG.risk.min_net_edge

    rounds = []
    for rk, (fld, label) in _ROUNDS.items():
        venues = cents.get(rk, {})
        kal, pol = venues.get("kalshi", {}), venues.get("poly", {})
        teams = []
        for row in champ:
            tid = row.get("team_id")
            p = row.get(fld)
            if tid is None or p is None:
                continue
            kc, pc = kal.get(tid), pol.get(tid)
            avail = [x for x in (kc, pc) if x is not None]
            best = min(avail) if avail else None        # cheapest executable buy price
            edge = (p - best / 100.0) if best is not None else None
            teams.append({
                "team_id": tid, "name": row.get("name", tid), "zh": row.get("zh", ""),
                "model_pct": round(p, 4), "model_c": round(p * 100, 1),
                "kalshi_c": kc, "poly_c": pc,
                "edge": (round(edge, 4) if edge is not None else None),
                "tradable": bool(edge is not None and edge >= theta),
            })
        teams.sort(key=lambda t: -t["model_pct"])
        rounds.append({"key": rk, "label": label,
                       "series": REACH_ROUND_SERIES.get(rk), "teams": teams})

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "theta": theta,
        "note": ("Per-team REACH-ROUND market (2-way Yes/No — the no-draw 'advance' product, "
                 "distinct from the per-match 90-min 3-way). Model prob of reaching each round "
                 "(tournament sim) vs the live venue Yes price; edge = model − cheapest price. "
                 "BOTH venues list these: Kalshi (KXWCROUND, thin in the group stage) and "
                 "Polymarket Global (Nation-To-Reach-X, liquid for all 48 teams). Real-money "
                 "trading is still gated + $1-capped."),
        "rounds": rounds,
    }


def main() -> None:
    doc = build()
    (CONFIG.paths.output / "reach_round.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in doc["rounds"]:
        n_k = sum(1 for t in r["teams"] if t.get("kalshi_c") is not None)
        n_p = sum(1 for t in r["teams"] if t.get("poly_c") is not None)
        print(f"{r['label']:<16} {len(r['teams'])} teams | kalshi¢ on {n_k}, poly¢ on {n_p}")


if __name__ == "__main__":
    main()
