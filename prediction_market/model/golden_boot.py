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
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from prediction_market.config import CONFIG
from prediction_market.ingest.prior_ingest import team_id
from prediction_market.model.tournament import TournamentResult

# Position-based base goal rate (per match) prior, for shrinking tiny in-tournament
# samples (plan 03 §6.1). A 2-goals-in-1-game leader must regress toward this.
_POS_PRIOR = {"Attacker": 0.45, "Midfielder": 0.20, "Defender": 0.08}
_SHRINK_ALPHA = 3.0     # pseudo-matches of prior weight
_GB_MAX_SIMS = 250_000  # cap the nested golden-boot sim (memory); champion can run larger


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


# Generation suffixes that aren't part of the identifying name (Vinícius "Júnior" = "Jr.").
_GEN_SUFFIX = {"jr", "jr.", "junior", "sr", "snr", "ii", "iii", "filho", "neto"}


def _name_tokens(name: str) -> list[str]:
    """Accent-stripped lowercase name tokens, dropping initials' dots and generation suffixes."""
    return [t for t in _accent_strip(name).replace(".", " ").lower().split() if t and t not in _GEN_SUFFIX]


def _names_match(a: str, b: str) -> bool:
    """Flexible person-name match to reconcile the EA-FC display name with the API scorer name
    (no shared player id; the two sources differ on nicknames/suffixes). Rule: the SURNAME (last
    token) must match — exact ≥3 chars, or a ≥4-char prefix either way for nicknames (EA 'Vini
    Jr.' ↔ API 'Vinícius Júnior': 'junior'/'jr' dropped, 'vini' a prefix of 'vinicius') — AND, when
    BOTH names carry a given name, their first tokens must be consistent (a prefix either way) so
    two same-surname teammates don't merge ('Jonathan David' ≠ 'P. David', J. ≠ P.)."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    la, lb = ta[-1], tb[-1]
    surname_ok = ((la == lb and len(la) >= 3)
                  or (len(la) >= 4 and len(lb) >= 4 and (la.startswith(lb) or lb.startswith(la))))
    if not surname_ok:
        return False
    if len(ta) >= 2 and len(tb) >= 2:                 # both have a given name → initials must agree
        fa, fb = ta[0], tb[0]
        if not (fa == fb or fa.startswith(fb) or fb.startswith(fa)):
            return False
    return True


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


# Within-team attacking rank (EA FC goal-rate order) → expected-start probability.
# The first-choice striker plays nearly every match; squad depth rotates.
_RANK_START_PROB = {1: 0.92, 2: 0.88, 3: 0.76, 4: 0.55, 5: 0.38}


def _wc_to_date_by_team(conn) -> dict[tuple[str, str], tuple[int, int]]:
    """(team_id, last-name) → (wc_goals, wc_apps) — tournament goals scored SO FAR.

    Counted from the per-match GOAL EVENTS (fixture_event), which are the authoritative,
    immediate record of who scored — the players/topscorers endpoint lags and can miss a
    just-finished match's scorers (e.g. Mbappé's goal not yet on the topscorers list).
    Own goals are excluded; penalties count. Appearances are approximated by the scorer's
    team's settled-match count (a goalscorer has clearly played). Falls back to player_stat
    when no goal events are stored yet. WC only — club scoring lives in squad strength.
    """
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    played = games_played_by_team(conn)

    out: dict[tuple[str, str], tuple[int, int]] = {}
    rows = conn.execute(
        "SELECT e.team_api_id, p.name, COUNT(*) g "
        "FROM fixture_event e "
        "JOIN fixture f ON e.fixture_api_id = f.api_id "
        "LEFT JOIN player p ON e.player_api_id = p.api_id "
        # API-Football logs MISSED penalties as type='Goal', detail='Missed Penalty' — those
        # are NOT goals (this over-counted e.g. Messi 6 vs the real 5). Own goals also excluded.
        "WHERE e.type = 'Goal' AND (e.detail IS NULL OR e.detail NOT IN ('Own Goal', 'Missed Penalty')) "
        "  AND f.status_short IN ('FT','AET','PEN') AND p.name IS NOT NULL "
        "GROUP BY e.player_api_id"
    ).fetchall()
    for r in rows:
        cid = cmap.get(r["team_api_id"])
        if not cid:
            continue
        key = _name_key(cid, r["name"])
        g, a = out.get(key, (0, 0))
        out[key] = (g + int(r["g"] or 0), max(a, played.get(cid, 1)))

    if out:
        return out
    # Fallback: no goal events stored yet → use the topscorers endpoint snapshot.
    for r in conn.execute(
        "SELECT p.name, ps.appearances, ps.goals, m.canonical_team_id "
        "FROM player_stat ps JOIN player p ON ps.player_api_id = p.api_id "
        "LEFT JOIN team_meta m ON ps.team_api_id = m.api_id "
        "WHERE ps.goals IS NOT NULL AND ps.season = ?",
        (CONFIG.soccer.season,),
    ):
        cid = r["canonical_team_id"]
        if not cid:
            continue
        key = _name_key(cid, r["name"])
        g, a = out.get(key, (0, 0))
        out[key] = (g + int(r["goals"] or 0), a + int(r["appearances"] or 0))
    return out


def games_played_by_team(conn) -> dict[str, int]:
    """Settled WC matches played so far, per canonical team (for mp_remaining)."""
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    played: dict[str, int] = {}
    for r in conn.execute(
        "SELECT home_api_id, away_api_id FROM fixture "
        "WHERE status_short IN ('FT','AET','PEN') AND home_goals IS NOT NULL"):
        for cid in (cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])):
            if cid:
                played[cid] = played.get(cid, 0) + 1
    return played


def _wc_scorers_by_team(conn) -> dict[str, list[tuple[str, int, int]]]:
    """team_id → [(api_player_name, wc_goals, wc_apps)] from settled-match GOAL EVENTS, kept as
    a per-team list (not a last-name map) so the EA-FC pool can be matched by _names_match —
    the two name sources have no shared id and differ on nicknames/suffixes (Vini Jr. vs
    Vinícius Júnior). Falls back to the topscorers snapshot when no goal events are stored."""
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    played = games_played_by_team(conn)
    out: dict[str, list[tuple[str, int, int]]] = {}
    rows = conn.execute(
        "SELECT e.team_api_id, p.name, COUNT(*) g FROM fixture_event e "
        "JOIN fixture f ON e.fixture_api_id = f.api_id LEFT JOIN player p ON e.player_api_id = p.api_id "
        "WHERE e.type = 'Goal' AND (e.detail IS NULL OR e.detail NOT IN ('Own Goal', 'Missed Penalty')) "
        "  AND f.status_short IN ('FT','AET','PEN') AND p.name IS NOT NULL GROUP BY e.player_api_id"
    ).fetchall()
    for r in rows:
        cid = cmap.get(r["team_api_id"])
        if cid:
            out.setdefault(cid, []).append((r["name"], int(r["g"] or 0), max(1, played.get(cid, 1))))
    if out:
        return out
    for r in conn.execute(
        "SELECT p.name, ps.appearances, ps.goals, m.canonical_team_id FROM player_stat ps "
        "JOIN player p ON ps.player_api_id = p.api_id LEFT JOIN team_meta m ON ps.team_api_id = m.api_id "
        "WHERE ps.goals IS NOT NULL AND ps.season = ?", (CONFIG.soccer.season,)):
        cid = r["canonical_team_id"]
        if cid and int(r["goals"] or 0):
            out.setdefault(cid, []).append((r["name"], int(r["goals"] or 0), int(r["appearances"] or 0)))
    return out


def build_golden_boot_players(conn=None) -> list[Player]:
    """Talent-grounded golden-boot pool (plan 03 §6.1): EA FC 26 + WC-to-date.

    The per-match goal rate is a Bayesian posterior — the EA FC talent rate is the
    prior (``gb_fc_prior_alpha`` pseudo-matches), updated by this tournament's goals
    and appearances::

        mu = (fc_rate * alpha + wc_goals) / (alpha + wc_apps)

    so a one-game burst from a weak-team forward (Balogun: 2 goals in 1 game) regresses
    toward his true ~0.33 talent rate instead of a naive 0.84, while a star who has not
    scored yet keeps his high prior. Real WC goals are carried as a head start, and the
    final boot is dominated downstream by talent x knockout depth (matches played).

    Falls back to the legacy topscorer builder / seed if FC ratings are not ingested.
    """
    from prediction_market.ingest import store

    conn = conn or store.init_db()
    alpha = CONFIG.model.gb_fc_prior_alpha
    pool_n = CONFIG.model.gb_pool_per_team

    rows = conn.execute(
        "SELECT fc_id, name, canonical_team_id, position_type, overall, "
        "       goal_rate, pen_taker, team_attack_rank "
        "FROM fc_player WHERE team_attack_rank <= ? ORDER BY canonical_team_id, team_attack_rank",
        (pool_n,),
    ).fetchall()
    if not rows:
        return build_players_from_store(conn)

    scorers = _wc_scorers_by_team(conn)
    out: list[Player] = []
    for r in rows:
        cid = r["canonical_team_id"]
        fc_rate = float(r["goal_rate"])
        # Match this EA-FC pool player to a WC scorer by flexible name (handles Vini Jr. ↔
        # Vinícius Júnior, nicknames, Jr/Júnior suffixes) — no shared id between the sources.
        wc_goals, wc_apps = 0, 0
        for api_name, g, a in scorers.get(cid, []):
            if _names_match(r["name"], api_name):
                wc_goals, wc_apps = g, a
                break
        mu = (fc_rate * alpha + wc_goals) / (alpha + wc_apps)   # Bayesian posterior rate
        start = _RANK_START_PROB.get(int(r["team_attack_rank"]), 0.20)
        # A player who already started + scored is clearly first-choice: floor his start.
        if wc_apps:
            start = max(start, 0.85)
        out.append(Player(
            player_id=f"fc{r['fc_id']}", name=r["name"], team_id=cid,
            mu_goals_per_match=mu, start_prob=start,
            pen_taker=bool(r["pen_taker"]), goals_so_far=int(wc_goals),
        ))
    return apply_teammate_competition(out, CONFIG.model.gb_teammate_competition)


def apply_teammate_competition(players: list[Player], kappa: float) -> list[Player]:
    """Discount a forward's rate when elite teammates share the team's finite goals.

    A lone spearhead (Haaland for Norway, Kane for England) concentrates his team's
    goals; a team with several competitive forwards (France: Mbappé + Dembélé + Olise)
    splits them, so no single player runs away with the boot — the 2002-Brazil "three
    R's" effect. We model this with each player's SHARE of his team's expected starting
    goal output (mu_eff = rate x start prob)::

        share_p = mu_eff_p / sum(mu_eff over the team's pool)
        factor_p = 1 - kappa * (1 - share_p)        # share→1 ⇒ no discount

    and scale his rate by factor_p. ``kappa`` is deliberately small (bounded by kappa
    as share→0) so this stays a SECONDARY nudge, never decisive: a deep run still
    dominates, it just gets divided among co-stars. kappa=0 disables it.
    """
    if kappa <= 0 or not players:
        return players
    team_eff: dict[str, float] = {}
    for p in players:
        team_eff[p.team_id] = team_eff.get(p.team_id, 0.0) + p.mu_eff
    out: list[Player] = []
    for p in players:
        total = team_eff.get(p.team_id, 0.0)
        share = (p.mu_eff / total) if total > 0 else 1.0
        factor = 1.0 - kappa * (1.0 - share)
        out.append(replace(p, mu_goals_per_match=p.mu_goals_per_match * factor))
    return out


def build_players_from_store(conn=None) -> list[Player]:
    """Legacy fallback: candidates from ingested WC-to-date topscorers (plan 03 §6.1/§6.3).

    Rate is shrunk toward a position prior (tiny in-tournament sample) and the
    current goal count is carried as a head start. Used only when EA FC ratings
    are not ingested (see build_golden_boot_players). Players whose team is not yet
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
    """Candidate pool: EA FC talent-grounded candidates (top attackers per team).

    The FC pool gives every contender a talent-grounded rate updated by WC-to-date
    goals, so it supersedes the hand-tuned seed list entirely — mixing the two would
    duplicate stars under spelling variants (e.g. "Vinicius Junior" vs "Vini Jr.") and
    re-introduce the stale rates this rewrite replaces. Falls back to the legacy
    topscorer builder, then seed-only, when FC ratings are not ingested.
    """
    if json_path is not None:
        return load_seed_players(json_path)
    real = build_golden_boot_players()
    return real or load_seed_players()


def simulate_golden_boot(
    tournament: TournamentResult,
    players: list[Player] | None = None,
    *,
    seed: int | None = None,
    games_played: dict[str, int] | None = None,
    eliminated: set[str] | None = None,
) -> GoldenBootResult:
    """Nested player-goal sim on the tournament's matches-played paths.

    ``games_played`` (canonical team_id → settled WC matches) corrects the
    double-count: future goals are projected over REMAINING matches only
    (``matches_played - games_played``), and the goals already scored enter
    separately as the head start. Without it, matches already played would be
    counted both in the head start and the forward Poisson.

    ``eliminated`` (canonical team_ids out of the tournament) freezes those teams'
    players at their goals-so-far — they play no more matches, so they can only win
    the boot if their current tally already leads. A player on an eliminated team who
    is not (near) the top scorer therefore lands at ~0%, which is correct.
    """
    players = players or load_players()
    mp = tournament.matches_played
    if mp is None:
        raise ValueError("TournamentResult has no matches_played; run simulate() first")
    # The nested player-goal array is (n_sims, n_players) — at 1M champion paths that
    # would be GBs of RAM. The champion sim can run at 1M while the golden boot stays
    # accurate on a sub-sample of the (i.i.d.) paths, so cap the rows used here.
    if mp.shape[0] > _GB_MAX_SIMS:
        mp = mp[:_GB_MAX_SIMS]
    n = mp.shape[0]
    rng = np.random.default_rng(seed if seed is not None else CONFIG.model.random_seed + 1)
    elim = eliminated or set()

    team_col = {tid: i for i, tid in enumerate(tournament.team_ids)}
    usable = [p for p in players if p.team_id in team_col]
    gp = games_played or {}

    # future goals ~ Poisson(mu_eff * matches_REMAINING); plus real goals already
    # scored as a head start (plan 03 §6.3). matches_remaining = matches_played in
    # the sim path minus games already settled (clipped at 0), so a game already
    # played is never counted both in the head start and the forward projection.
    # An eliminated team plays no more matches → its players' future lambda is 0
    # (only their head-start goals remain). Otherwise project over remaining matches.
    # OPPONENT-AWARE projection: future goals ~ Poisson(mu_eff * future_opp), where future_opp
    # is the sum over the team's FUTURE matches of each opponent's defence-weakness (per the
    # per-sim official-bracket draw) — a soft knockout route scores >1 goal-share/match, a hard
    # one <1, vs the old flat remaining-match count. Falls back to the flat count if a caller
    # ran an older sim without future_opp.
    fo = getattr(tournament, "future_opp", None)
    if fo is not None and fo.shape[0] > _GB_MAX_SIMS:
        fo = fo[:_GB_MAX_SIMS]

    def _future_lambda(p):
        if p.team_id in elim:
            return np.zeros(n)
        col = team_col[p.team_id]
        if fo is not None:
            return p.mu_eff * fo[:, col]
        return p.mu_eff * np.clip(mp[:, col].astype(np.float64) - gp.get(p.team_id, 0), 0.0, None)

    lam = np.stack([_future_lambda(p) for p in usable], axis=1)  # (n, P)
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

    from prediction_market.ingest import store

    conn = store.init_db()
    prior = load_prior()
    sm = build_strength(prior)
    res = simulate(prior, sm, n_sims=CONFIG.model.n_sims_quicklook, seed=1)
    players = load_players()
    gb = simulate_golden_boot(res, players, seed=2, games_played=games_played_by_team(conn))
    team_of = {p.player_id: p.team_id for p in players}
    print(f"Golden boot sim N={gb.n_sims}  (pool={len(gb.player_ids)} players)")
    print(f"{'player':<22}{'team':<16}{'P(boot)':>9}{'E[goals]':>10}")
    for pid in sorted(gb.player_ids, key=lambda p: -gb.p_golden_boot[p])[:14]:
        print(f"{gb.player_names[pid][:21]:<22}{team_of.get(pid,''):<16}"
              f"{gb.p_golden_boot[pid]:>9.3f}{gb.e_goals[pid]:>10.2f}")
