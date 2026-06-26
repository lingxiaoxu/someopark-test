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

# Kalshi reach-round quote is trusted only when it's close to the liquid Poly reference:
# within this absolute ¢ gap AND at least this fraction of the Poly price (a relative
# floor — a 6¢-vs-25¢ quote is a 19¢ gap but, more tellingly, ¼ of Poly → noise).
_KALSHI_SANITY_CENTS = 15.0
_KALSHI_SANITY_FRAC = 0.60

# round key → (model prob field in the champion array, human label). 'advance' = reach
# the Round of 32 (qualify from the group); Kalshi lists no such market, only Poly.
_ROUNDS = {
    "advance": ("p_advance_model", "Advance (R32)"),
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


def _group_form(conn) -> dict[str, dict]:
    """Per-team group-stage goal difference, matches played, and CURRENT in-group rank (1-4),
    derived LIVE from the fixture table (the same data the model runs on, always current —
    unlike the TTL-cached standing table, which was 10 days stale on 2026-06-26). GD/played
    verified equal to API-Football's official goalsDiff/played, and the in-group rank verified
    equal to the official per-group standings (0/48 mismatch, 2026-06-26). Rank uses the draw's
    authoritative group composition with the points→GD→GF tie-break. Returns
    {canonical_team_id: {"gd": int, "played": int, "rank": int}} — feeds the 净胜球（完赛场次/排名）column."""
    if conn is None:
        return {}
    from collections import defaultdict
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    pts: dict = defaultdict(int)
    gd: dict = defaultdict(int)
    gf: dict = defaultdict(int)
    played: dict = defaultdict(int)
    for r in conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals FROM fixture "
        "WHERE round LIKE '%roup%' AND status_short IN ('FT','AET','PEN') AND home_goals IS NOT NULL"):
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            continue
        hg, ag = r["home_goals"], r["away_goals"]
        played[hi] += 1; played[ai] += 1
        gf[hi] += hg; gf[ai] += ag
        gd[hi] += hg - ag; gd[ai] += ag - hg
        if hg > ag:
            pts[hi] += 3
        elif ag > hg:
            pts[ai] += 3
        else:
            pts[hi] += 1; pts[ai] += 1
    # Current in-group rank (1-4) from the authoritative group draw, ordered points→GD→GF.
    rank: dict = {}
    try:
        from prediction_market.ingest.prior_ingest import load_prior
        for teams in load_prior().draw().values():
            for i, t in enumerate(sorted(teams, key=lambda t: (-pts[t], -gd[t], -gf[t])), 1):
                rank[t] = i
    except Exception:
        rank = {}
    return {tid: {"gd": int(gd[tid]), "played": int(played[tid]), "rank": rank.get(tid)}
            for tid in played}


def build(conn=None) -> dict:
    from prediction_market.venues.champion_prices import REACH_ROUND_SERIES, reach_round_cents
    champ = _load_champion_rows()
    cents = reach_round_cents()
    theta = CONFIG.risk.min_net_edge
    form = _group_form(conn)   # canonical_team_id → {gd, played} (group stage, live)

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
            # Cross-venue sanity gate: Poly Global is liquid (48/48), Kalshi reach-round is
            # thin and its asks are often broken (e.g. France→SF quoted 1¢, Iran→RO16 69¢).
            # If Kalshi disagrees with the liquid Poly price by more than the tolerance,
            # treat the Kalshi quote as noise and drop it ('—') rather than show a fake edge.
            if kc is not None and pc is not None and (
                    abs(kc - pc) > _KALSHI_SANITY_CENTS or kc < _KALSHI_SANITY_FRAC * pc
                    or kc > pc / _KALSHI_SANITY_FRAC):
                kc = None
            avail = [x for x in (kc, pc) if x is not None]
            best = min(avail) if avail else None        # cheapest executable buy price
            edge = (p - best / 100.0) if best is not None else None
            frm = form.get(tid) or {}
            teams.append({
                "team_id": tid, "name": row.get("name", tid), "zh": row.get("zh", ""),
                "model_pct": round(p, 4), "model_c": round(p * 100, 1),
                "kalshi_c": kc, "poly_c": pc,
                "edge": (round(edge, 4) if edge is not None else None),
                "tradable": bool(edge is not None and edge >= theta),
                # Group net goal difference, matches played, current in-group rank — the
                # 净胜球（完赛场次/排名）column: "+1（2=1）（#3）" = GD +1, 2 played / 1 to go, 3rd in group.
                "group_gd": frm.get("gd"), "group_played": frm.get("played"),
                "group_rank": frm.get("rank"),
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
                 "BOTH venues list these: Polymarket Global (Nation-To-Reach-X, liquid for all "
                 "48 teams — the reference) and Kalshi (KXWCROUND, thin; its quotes are shown "
                 "ONLY when within 20¢ of Poly, else dropped as noise — its deeper-round asks "
                 "are often broken). Real-money trading is still gated + $1-capped."),
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
