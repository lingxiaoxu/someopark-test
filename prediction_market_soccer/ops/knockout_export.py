"""ops/knockout_export.py — knockout BRACKET & OPPONENT prediction (plan C).

Enumerates every still-possible Round-of-32 bracket (each Monte-Carlo path IS one possible
bracket, conditioned on the played group games + standings, and weighted by its sampling
probability — the mathematical equivalent of "list all possibility tables and weight them"),
simulates each to the final, and reports the PROBABILITY-WEIGHTED average across them:

  * official_bracket — the 16 fixed R32 pairings in group-position terms (FIFA matches 73–88),
  * matches          — per R32 match, the likely occupant of each slot (with probability),
  * teams            — per team, the distribution of its R32 OPPONENT + reach-round odds.

The match-result SIMULATION ENGINE is the `adv` advance-probability matrix inside
model.tournament (Dixon-Coles maths today); it is the single swap point for a future
agent-based simulator — replace how `adv` is produced and the whole weighted-average
machinery here is unchanged.

    python -m prediction_market_soccer.ops.knockout_export → data/output/knockout_bracket.json
"""
from __future__ import annotations

import json

import numpy as np

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.model import knockout_bracket as KB


def _slot_label(slot) -> str:
    kind, ref = slot
    if kind == "W":
        return f"W-{ref}"
    if kind == "R":
        return f"RU-{ref}"
    return "3rd[" + "/".join(sorted(ref)) + "]"


def _strength(conn, prior):
    """Production champion strength: rank-anchored ratings + played-result form + roster."""
    from prediction_market_soccer.model.strength import build_strength, update_strength_from_store
    sm = update_strength_from_store(build_strength(prior))
    try:
        from prediction_market_soccer.model.club_aggregation import blend_into_strength, squad_attack_quality
        sm = blend_into_strength(sm, squad_attack_quality(season=CONFIG.soccer.season))
    except Exception as e:
        print(f"[knockout_export] roster blend skipped: {e}")
    return sm


def build(conn=None, *, n_sims: int | None = None, top: int = 4) -> dict:
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.tournament import simulate

    conn = conn or store.init_db()
    prior = load_prior()
    name = {t.team_id: t.name for t in prior.teams}
    zh = {t.team_id: t.zh for t in prior.teams}
    sm = _strength(conn, prior)
    n = n_sims or CONFIG.model.n_sims_tournament
    res = simulate(prior, sm, n_sims=n, seed=CONFIG.model.random_seed, conn=conn, bracket_report=True)
    rep = res.bracket_report
    tids = res.team_ids
    occ = rep["slot_counts"]          # (32, N) per-leaf occupancy
    opp = rep["opp_counts"]           # (N, N) R32 opponent co-occurrence
    nn = rep["n"]

    def team_row(ti: int):
        return {"team_id": tids[ti], "name": name.get(tids[ti], tids[ti]), "zh": zh.get(tids[ti], "")}

    def topk(counts: np.ndarray, k: int):
        order = np.argsort(-counts)
        out = []
        for ti in order[:k]:
            if counts[ti] <= 0:
                break
            out.append({**team_row(int(ti)), "p": round(float(counts[ti]) / nn, 4)})
        return out

    # 32 bracket leaves are assembled in R32_LEAF_ORDER, two slots per match.
    leaves = []
    for mi in KB.R32_LEAF_ORDER:
        for si in range(2):
            leaves.append((mi, si))

    matches = []
    for k in range(16):
        (mi_a, sa), (mi_b, sb) = leaves[2 * k], leaves[2 * k + 1]
        mi = mi_a                                  # both leaves belong to the same R32 match
        slot_a, slot_b = KB.R32_MATCHES[mi]
        matches.append({
            "match": mi + 73,
            "slot_a": {"label": _slot_label(slot_a), "likely": topk(occ[2 * k], top)},
            "slot_b": {"label": _slot_label(slot_b), "likely": topk(occ[2 * k + 1], top)},
        })

    teams = []
    for ti, tid in enumerate(tids):
        reach_r32 = float(opp[ti].sum()) / nn      # = P(team reaches R32)
        if reach_r32 <= 0:
            continue
        teams.append({
            **team_row(ti),
            "p_reach_r32": round(reach_r32, 4),
            "r32_opponents": topk(opp[ti], top),   # the knockout OPPONENT distribution
            "reach": {"r16": round(res.p_r16[tid], 4), "qf": round(res.p_qf[tid], 4),
                      "sf": round(res.p_sf[tid], 4), "final": round(res.p_final[tid], 4),
                      "champion": round(res.p_champion[tid], 4)},
        })
    teams.sort(key=lambda t: -t["reach"]["champion"])

    return {
        "n_sims": n,
        "note": ("Probability-weighted average over every sampled R32 bracket (conditioned on "
                 "played group results + 2026 tie-breaks); official FIFA group-position slotting "
                 "and Annex-C third-place assignment. Sim engine = Dixon-Coles advance matrix "
                 "(swap point for a future agent simulator)."),
        "official_bracket": [
            {"match": mi + 73, "slot_a": _slot_label(a), "slot_b": _slot_label(b)}
            for mi, (a, b) in enumerate(KB.R32_MATCHES)
        ],
        "matches": matches,
        "teams": teams,
    }


def main() -> None:
    payload = build()
    (CONFIG.paths.output / "knockout_bracket.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"knockout_bracket.json → {len(payload['matches'])} R32 matches, {len(payload['teams'])} teams")
    # Spotlight a couple of marquee teams' likely R32 opponents.
    for t in payload["teams"][:3]:
        opps = ", ".join(f"{o['name']} {o['p']*100:.0f}%" for o in t["r32_opponents"][:3])
        print(f"  {t['name']}: champion {t['reach']['champion']*100:.1f}% | R32 opp ~ {opps}")


if __name__ == "__main__":
    main()
