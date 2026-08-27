"""model/top_scorer.py — per-competition top-scorer race (TRANSFORM_PLAN 附录 C).

The club counterpart of the World Cup golden boot (model/golden_boot.py). The
shrinkage idea carries over unchanged — a player who scored twice in one match
must regress toward his talent, not lead the board — but the tournament
machinery does not: in a knockout a player's remaining matches depend on his
team surviving, while in a league every club plays out its fixture list. So the
nested per-path simulation collapses to a clean closed form per player:

    goals_final = goals_so_far + Poisson(mu_eff * matches_remaining)

with ``mu_eff`` the shrunk goals-per-appearance rate and ``matches_remaining``
his club's unplayed fixtures in that competition. Ranking is done by simulation
rather than by expectation because the market pays the WINNER: two players with
the same expected total have very different chances if one is 5 goals ahead with
few rounds left.

Rate estimation (mirrors golden_boot §6.1):
  * observed  = goals / appearances this season;
  * prior     = the FC26 talent goal rate (ingest/fc_ingest.fc_goal_rate), or a
                position default when the player is outside the FC26 licence;
  * shrunk    = (goals + alpha * prior) / (appearances + alpha).

Cup competitions (two-legged CONMEBOL, UEFA qualifying) are skipped: "remaining
matches" there is a function of surviving the bracket, which is the knockout
problem the WC module solves — a league season is not that shape, and pretending
otherwise would publish a number built on the wrong assumption.
"""
from __future__ import annotations

import numpy as np

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import get

# Position fallback rates (goals per appearance) for players outside FC26.
_POS_PRIOR = {"Attacker": 0.42, "Midfielder": 0.18, "Defender": 0.06, "Goalkeeper": 0.0}
_DEFAULT_PRIOR = 0.20
# Pseudo-appearances of prior weight. Deliberately heavy: one round into a season
# every scorer has "1 goal in 1 appearance", and a light prior turns that into a
# 38-goal pace. At 12, a player needs roughly a third of a season before his own
# record outweighs his talent estimate.
_SHRINK_ALPHA = 12.0
_MAX_SIMS = 200_000


def _remaining_by_club(conn, comp) -> dict[int, int]:
    """team_api_id → unplayed fixtures left in this competition."""
    rows = conn.execute(
        "SELECT home_api_id h, away_api_id a FROM fixture "
        "WHERE league_id=? AND season=? AND status_short NOT IN "
        "('FT','AET','PEN','AWD','WO','CANC','ABD')",
        (comp.api_football_id, comp.season)).fetchall()
    out: dict[int, int] = {}
    for r in rows:
        for t in (r["h"], r["a"]):
            if t is not None:
                out[t] = out.get(t, 0) + 1
    return out


def _candidates(conn, comp) -> list[dict]:
    """Scorers on record this season, with a talent prior attached."""
    rows = conn.execute(
        "SELECT ps.player_api_id pid, p.name, ps.team_api_id tid, ps.goals, "
        "       ps.appearances, ps.minutes, tm.canonical_team_id cid "
        "FROM player_stat ps "
        "JOIN player p ON p.api_id = ps.player_api_id "
        "LEFT JOIN team_meta tm ON tm.api_id = ps.team_api_id "
        "WHERE ps.league_id=? AND ps.season=?",
        (comp.api_football_id, comp.season)).fetchall()
    if not rows:
        return []
    # FC26 talent rate by (club, surname) — the same matching discipline as fc_ingest
    fc: dict[tuple, tuple] = {}
    for r in conn.execute(
            "SELECT canonical_team_id cid, name, goal_rate, position_type FROM fc_player"):
        key = (r["cid"], (r["name"] or "").split()[-1].lower())
        fc[key] = (r["goal_rate"], r["position_type"])
    out = []
    for r in rows:
        nm = r["name"] or ""
        surname = nm.split()[-1].lower().strip(".") if nm else ""
        hit = fc.get((r["cid"], surname))
        prior = hit[0] if hit else _POS_PRIOR.get((hit or (None, None))[1], _DEFAULT_PRIOR)
        apps = int(r["appearances"] or 0)
        goals = int(r["goals"] or 0)
        mu = (goals + _SHRINK_ALPHA * prior) / (apps + _SHRINK_ALPHA) if (apps + _SHRINK_ALPHA) else prior
        mu = min(mu, 1.10)   # no player sustains more than ~1.1 goals a game over a season
        out.append({"player_id": r["pid"], "name": nm, "team_api_id": r["tid"],
                    "club_id": r["cid"], "goals": goals, "appearances": apps,
                    "prior_rate": round(float(prior), 4), "mu": float(mu),
                    "from_fc26": bool(hit)})
    return out


def top_scorer_board(conn, comp_key: str, *, n_sims: int = 50_000,
                     seed: int | None = None, top_n: int = 15) -> list[dict]:
    """[{player, club, goals, e_goals, p_top_scorer, ...}] for a LEAGUE competition.

    Returns [] for cup competitions (see the module docstring) and for a
    competition whose scorer feed is empty."""
    comp = get(comp_key)
    if comp.kind not in ("league", "league_playoffs"):
        return []
    cands = _candidates(conn, comp)
    if not cands:
        return []
    rem = _remaining_by_club(conn, comp)
    rng = np.random.default_rng(seed if seed is not None else CONFIG.model.random_seed)
    n_sims = min(n_sims, _MAX_SIMS)

    mu = np.array([c["mu"] for c in cands])
    left = np.array([rem.get(c["team_api_id"], 0) for c in cands], dtype=float)
    have = np.array([c["goals"] for c in cands], dtype=np.int64)
    lam = mu * left                                   # expected goals still to come
    draws = rng.poisson(lam[None, :], size=(n_sims, len(cands)))
    totals = have[None, :] + draws

    best = totals.max(axis=1, keepdims=True)
    winners = totals == best                          # ties share the title
    share = winners / winners.sum(axis=1, keepdims=True)
    p_top = share.sum(axis=0) / n_sims
    e_goals = totals.mean(axis=0)

    board = []
    for i, c in enumerate(cands):
        board.append({
            "player_id": c["player_id"], "name": c["name"], "club_id": c["club_id"],
            "goals": c["goals"], "appearances": c["appearances"],
            "matches_left": int(left[i]),
            "rate": round(c["mu"], 4), "prior_rate": c["prior_rate"],
            "talent_source": "fc26" if c["from_fc26"] else "position_default",
            "e_goals": round(float(e_goals[i]), 2),
            "p_top_scorer": round(float(p_top[i]), 5),
        })
    board.sort(key=lambda r: -r["p_top_scorer"])
    return board[:top_n]


if __name__ == "__main__":
    from prediction_market_soccer.ingest import store
    conn = store.init_db()
    for lg in ("epl", "brasileirao", "argentina"):
        b = top_scorer_board(conn, lg, n_sims=20_000)
        print(f"— {lg}: {len(b)} candidates")
        for r in b[:5]:
            print(f"   {r['name'][:22]:24s} {r['goals']}g +{r['matches_left']} left  "
                  f"E={r['e_goals']:.1f}  P(top)={r['p_top_scorer']:.1%}  [{r['talent_source']}]")
