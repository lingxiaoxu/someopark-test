"""Golden boot — nested player-goal simulation (plan 03 §6).

Player goals are simulated ON the SAME tournament paths as the champion sim
(plan 03 §6.2): a player's goal count is correlated with how far his team
advances, because a knocked-out team plays no more matches. We exploit a clean
identity — the sum of ``k`` i.i.d. Poisson(mu) draws is Poisson(k*mu) — so a
player's tournament goals are::

    goals_p ~ Poisson(mu_eff_p * matches_played[team_of_p])

evaluated per simulation, fully vectorised over the N paths.

``mu_eff_p = mu_goals_per_match * start_prob`` (expected starts). The per-match
attacking-context scaling (plan 03 §6.1: team attack strength, opponent
defence) is a documented v1 simplification — folded into the seed rate, not yet
opponent-specific.

Player rates come from a SEED placeholder file (real players, rough rates);
replace with ingested xG-based rates before trading (see seed_players.json).
"""
from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from prediction_market.config import CONFIG
from prediction_market.ingest.prior_ingest import team_id
from prediction_market.model.tournament import TournamentResult

# Position-based base goal rate (per match) prior, for shrinking tiny in-tournament
# samples (plan 03 §6.1). A 2-goals-in-1-game leader must regress toward this.
_POS_PRIOR = {"Attacker": 0.45, "Midfielder": 0.20, "Defender": 0.08}
_SHRINK_ALPHA = 3.0     # pseudo-matches of prior weight


@dataclass(frozen=True)
class Player:
    player_id: str
    name: str
    team_id: str
    mu_goals_per_match: float
    start_prob: float
    pen_taker: bool
    goals_so_far: int = 0       # real goals already scored (head start, plan 03 §6.3)

    @property
    def mu_eff(self) -> float:
        return self.mu_goals_per_match * self.start_prob


def _accent_strip(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _name_key(team_id_: str, name: str) -> tuple[str, str]:
    """(team_id, last-name) key for de-duping store vs seed players (accent-insensitive)."""
    tokens = _accent_strip(name).replace(".", " ").split()
    last = tokens[-1].lower() if tokens else name.lower()
    return (team_id_, last)


@dataclass
class GoldenBootResult:
    player_ids: list[str]
    player_names: dict[str, str]
    n_sims: int
    p_golden_boot: dict[str, float]
    e_goals: dict[str, float]


def load_seed_players(json_path: Path | str | None = None) -> list[Player]:
    """Pre-tournament SEED favourites (real players, prior rates)."""
    path = Path(json_path) if json_path else CONFIG.paths.priors / "seed_players.json"
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        Player(
            player_id=p["player_id"], name=p["name"], team_id=team_id(p["team"]),
            mu_goals_per_match=float(p["mu_goals_per_match"]),
            start_prob=float(p.get("start_prob", 1.0)),
            pen_taker=bool(p.get("pen_taker", False)),
        )
        for p in raw["players"]
    ]


def build_players_from_store(conn=None) -> list[Player]:
    """Real golden-boot candidates from ingested topscorers (plan 03 §6.1/§6.3).

    Rate is shrunk toward a position prior (tiny in-tournament sample) and the
    current goal count is carried as a head start. Players whose team is not yet
    mapped to a canonical id are skipped.
    """
    from prediction_market.ingest import store

    conn = conn or store.init_db()
    # WC season ONLY: golden-boot head-start = tournament goals, never club-season
    # goals. (Club stats live in player_stat under the club season for the
    # strength aggregation; they must not leak in here.)
    rows = conn.execute(
        "SELECT ps.player_api_id, p.name, p.position, ps.team_api_id, ps.appearances, "
        "       ps.goals, ps.penalty_scored, m.canonical_team_id "
        "FROM player_stat ps JOIN player p ON ps.player_api_id = p.api_id "
        "LEFT JOIN team_meta m ON ps.team_api_id = m.api_id "
        "WHERE ps.goals IS NOT NULL AND ps.season = ?",
        (CONFIG.soccer.season,),
    ).fetchall()

    out: list[Player] = []
    for r in rows:
        cid = r["canonical_team_id"]
        if not cid:
            continue
        apps = r["appearances"] or 1
        goals = r["goals"] or 0
        prior = _POS_PRIOR.get(r["position"], 0.15)
        mu = (goals + _SHRINK_ALPHA * prior) / (apps + _SHRINK_ALPHA)   # shrunk rate
        out.append(Player(
            player_id=f"af{r['player_api_id']}", name=r["name"], team_id=cid,
            mu_goals_per_match=mu, start_prob=0.85 if apps else 0.5,
            pen_taker=bool((r["penalty_scored"] or 0) > 0), goals_so_far=int(goals),
        ))
    return out


def load_players(json_path: Path | str | None = None) -> list[Player]:
    """Merged candidate pool: real topscorers (from store) ∪ seed favourites.

    Real players take precedence; a seed favourite is added only if no store
    player shares its (team, last-name) — so stars who haven't scored yet stay
    in the pool without double-counting those who have. Falls back to seed-only
    when the store has no topscorers ingested.
    """
    if json_path is not None:
        return load_seed_players(json_path)
    real = build_players_from_store()
    if not real:
        return load_seed_players()
    seen = {_name_key(p.team_id, p.name) for p in real}
    merged = list(real)
    for sp in load_seed_players():
        if _name_key(sp.team_id, sp.name) not in seen:
            merged.append(sp)
    return merged


def simulate_golden_boot(
    tournament: TournamentResult,
    players: list[Player] | None = None,
    *,
    seed: int | None = None,
) -> GoldenBootResult:
    """Nested player-goal sim on the tournament's matches-played paths."""
    players = players or load_players()
    mp = tournament.matches_played
    if mp is None:
        raise ValueError("TournamentResult has no matches_played; run simulate() first")
    n = mp.shape[0]
    rng = np.random.default_rng(seed if seed is not None else CONFIG.model.random_seed + 1)

    team_col = {tid: i for i, tid in enumerate(tournament.team_ids)}
    usable = [p for p in players if p.team_id in team_col]

    # future goals ~ Poisson(mu_eff * matches_played); plus real goals already
    # scored as a head start (plan 03 §6.3). NOTE (v2 approximation): mu is
    # applied to the full-tournament match count, lightly double-counting games
    # already played — the head start dominates early, refine when the sim is
    # conditioned on played fixtures.
    lam = np.stack(
        [p.mu_eff * mp[:, team_col[p.team_id]].astype(np.float64) for p in usable],
        axis=1,
    )  # (n, P)
    head_start = np.array([p.goals_so_far for p in usable], dtype=np.int64)
    goals = rng.poisson(lam) + head_start[None, :]  # (n, P)

    # Top scorer per sim. Random tie-break (plan 03 §6.2 FIFA tie-break — assists
    # then fewer minutes — is NOT yet modelled; documented v1 simplification).
    jitter = rng.random(goals.shape) * 1e-3
    winner = np.argmax(goals + jitter, axis=1)  # (n,)
    win_counts = np.bincount(winner, minlength=len(usable)).astype(float)

    inv = 1.0 / n
    return GoldenBootResult(
        player_ids=[p.player_id for p in usable],
        player_names={p.player_id: p.name for p in usable},
        n_sims=n,
        p_golden_boot={usable[i].player_id: win_counts[i] * inv for i in range(len(usable))},
        e_goals={usable[i].player_id: float(goals[:, i].mean()) for i in range(len(usable))},
    )


if __name__ == "__main__":
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.strength import build_strength
    from prediction_market.model.tournament import simulate

    prior = load_prior()
    sm = build_strength(prior)
    res = simulate(prior, sm, n_sims=CONFIG.model.n_sims_quicklook, seed=1)
    gb = simulate_golden_boot(res, seed=2)
    print(f"Golden boot sim N={gb.n_sims}")
    print(f"{'player':<22}{'P(boot)':>9}{'E[goals]':>10}")
    for pid in sorted(gb.player_ids, key=lambda p: -gb.p_golden_boot[p])[:12]:
        print(f"{gb.player_names[pid]:<22}{gb.p_golden_boot[pid]:>9.3f}{gb.e_goals[pid]:>10.2f}")
