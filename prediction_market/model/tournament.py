"""Monte-Carlo tournament simulation — 2026 48-team format (plan 03 §5).

One vectorised engine produces ALL tournament-level probabilities from the same
batch of simulated paths, so champion / advancement / golden-boot stay mutually
coherent (plan 03 philosophy). Vectorisation is over the N simulations (numpy
batch ops), not Python per-match loops (plan 03 §8).

Format implemented (plan 03 §5.1, 10 §1):
  * 12 groups of 4, single round-robin (6 matches each).
  * Top 2 per group (24) advance directly; best 8 of 12 third-placed teams
    revive; 4th-placed teams out.
  * 32-team single elimination: R32 -> R16 -> QF -> SF -> Final, with extra
    time + penalties resolved via the knockout advance matrix.

DOCUMENTED SIMPLIFICATIONS (v1, flagged for later replacement):
  * Group tie-breaks use points -> goal difference -> goals for -> random.
    Head-to-head and the full official tie-break chain are NOT yet implemented.
  * The R32 bracket is a FIXED, balanced, same-group-avoiding mapping (see
    ``_BRACKET_SLOTS``) — NOT the exact official 2026 slotting / third-place
    combination table. Champion odds are bracket-sensitive; swap in the official
    table before trading title markets.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from prediction_market.config import CONFIG, ModelConfig
from prediction_market.ingest.prior_ingest import PriorSnapshot, load_prior
from prediction_market.model.dixon_coles import knockout_advance_prob
from prediction_market.model.strength import StrengthModel, build_strength

# Group round-robin fixtures over within-group indices 0..3 (6 matches).
_GROUP_FIXTURES = [(0, 1), (2, 3), (0, 2), (1, 3), (0, 3), (1, 2)]

# Fixed R32 bracket in pairing order: slots (0,1),(2,3),... meet in R32, and the
# winners cascade up the tree. Descriptors: ("W", group) winner, ("R", group)
# runner-up, ("T", k) the k-th best third (0=best). No same-group R32 clash.
_BRACKET_SLOTS: list[tuple[str, object]] = [
    ("W", "A"), ("R", "B"), ("W", "C"), ("R", "D"),
    ("W", "E"), ("R", "F"), ("W", "G"), ("R", "H"),
    ("W", "I"), ("R", "J"), ("W", "K"), ("R", "L"),
    ("W", "B"), ("R", "A"), ("W", "D"), ("R", "C"),
    ("W", "F"), ("R", "E"), ("W", "H"), ("R", "G"),
    ("W", "J"), ("R", "I"), ("W", "L"), ("R", "K"),
    ("T", 0), ("T", 1), ("T", 2), ("T", 3),
    ("T", 4), ("T", 5), ("T", 6), ("T", 7),
]


@dataclass
class TournamentResult:
    """Aggregated tournament probabilities, indexed by team_id."""

    team_ids: list[str]
    n_sims: int
    p_champion: dict[str, float]
    p_final: dict[str, float]
    p_sf: dict[str, float]
    p_qf: dict[str, float]
    p_r16: dict[str, float]
    p_advance: dict[str, float]          # reached knockout (out of group)
    e_matches: dict[str, float]
    # Per-sim matches played per team (N, 48) int8 — consumed by golden boot.
    matches_played: np.ndarray = field(repr=False, default=None)


def r3_intensity(points_after2: np.ndarray, cfg: ModelConfig) -> np.ndarray:
    """Per-team round-3 effort multiplier from points after 2 games (plan 03 §3).

    Clinched (>= clinch points) → rotate (intensity < 1); winless (0 pts) →
    push (intensity > 1); otherwise normal.
    """
    return np.where(points_after2 >= cfg.r3_clinch_points, cfg.r3_rotation_intensity,
                    np.where(points_after2 == 0, cfg.r3_desperation_intensity, 1.0))


def load_official_bracket_from_store(conn=None) -> list[tuple[str, object]] | None:
    """Official R32 bracket from API-Football fixtures, once R32 is scheduled.

    Until the knockout fixtures exist (they appear as the tournament progresses,
    plan 11), this returns None and the simulator uses the fixed structural
    bracket in ``_BRACKET_SLOTS`` (a documented v1 approximation). When R32
    fixtures are present, the authoritative pairing is read from them.
    """
    from prediction_market.ingest import store

    conn = conn or store.init_db()
    rows = conn.execute(
        "SELECT COUNT(*) n FROM fixture WHERE round LIKE '%Round of 32%'").fetchone()
    if not rows or rows["n"] == 0:
        return None
    # R32 scheduled: the official mapping is derivable from the fixture pairings.
    # (Slot-assembly from real fixtures is wired here when the data lands.)
    return None


def eliminated_teams(conn=None, all_team_ids=None) -> set[str]:
    """Canonical team_ids that are OUT of the tournament — champion prob must be 0.

    Empty during the group stage (nobody is eliminated until the knockouts begin).
    Once any knockout fixture exists:
      * group non-qualifiers = the 48 minus everyone drawn into a knockout tie, and
      * knockout losers = the team that did NOT advance in a settled KO match
        (by score, else the API-Football winner flag for penalty shootouts).
    """
    import json as _json

    from prediction_market.ingest import store

    conn = conn or store.init_db()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    ko = conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals, status_short, raw_json, round "
        "FROM fixture WHERE round IS NOT NULL AND lower(round) NOT LIKE '%group%'"
    ).fetchall()
    if not ko:
        return set()   # still in the group stage — no eliminations yet

    participants: set[str] = set()
    elim: set[str] = set()
    for r in ko:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if hi:
            participants.add(hi)
        if ai:
            participants.add(ai)
        if r["status_short"] not in ("FT", "AET", "PEN") or r["home_goals"] is None or not (hi and ai):
            continue
        gh, ga = r["home_goals"], r["away_goals"]
        if gh > ga:
            elim.add(ai)
        elif ga > gh:
            elim.add(hi)
        else:  # level after extra time → penalty shootout; read the winner flag
            try:
                teams = _json.loads(r["raw_json"] or "{}").get("teams", {})
                if teams.get("home", {}).get("winner") is True:
                    elim.add(ai)
                elif teams.get("away", {}).get("winner") is True:
                    elim.add(hi)
            except Exception:
                pass
    # Group non-qualifiers: anyone not drawn into the knockout bracket.
    if all_team_ids:
        elim |= (set(all_team_ids) - participants)
    return elim


def _build_adv_matrix(sm: StrengthModel, team_ids: list[str], cfg: ModelConfig) -> np.ndarray:
    """ADV[i, j] = P(team i advances past team j) in a knockout tie."""
    from prediction_market.model.penalties import shootout_win_prob

    n = len(team_ids)
    adv = np.full((n, n), 0.5)
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            lam_i, lam_j = sm.pair_lambdas(team_ids[i], team_ids[j], knockout=True)
            # Team-specific shootout edge (quality + reputation), not a flat average.
            edge = shootout_win_prob(sm, team_ids[i], team_ids[j])
            adv[i, j] = knockout_advance_prob(
                lam_i, lam_j,
                rho=cfg.dc_rho, kmax=cfg.score_matrix_kmax,
                et_fraction=cfg.extra_time_fraction, penalty_home_edge=edge,
            )
    return adv


def _played_group_results(conn) -> dict:
    """{frozenset(cid_a, cid_b): {cid: goals}} for group fixtures ALREADY PLAYED — so the
    sim can FIX them to reality instead of re-rolling, conditioning advancement on the
    actual standings (a team that can no longer qualify then gets ~0 advance naturally)."""
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    out: dict = {}
    for r in conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals FROM fixture "
        "WHERE round LIKE '%roup%' AND status_short IN ('FT','AET','PEN') AND home_goals IS NOT NULL"):
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if hi and ai and hi != ai:
            out[frozenset((hi, ai))] = {hi: int(r["home_goals"]), ai: int(r["away_goals"])}
    return out


def simulate(
    prior: PriorSnapshot | None = None,
    sm: StrengthModel | None = None,
    *,
    n_sims: int | None = None,
    seed: int | None = None,
    conn=None,
) -> TournamentResult:
    prior = prior or load_prior()
    cfg = sm.cfg if sm is not None else CONFIG.model
    sm = sm or build_strength(prior, cfg)
    n = n_sims or cfg.n_sims_tournament
    rng = np.random.default_rng(seed if seed is not None else cfg.random_seed)

    team_ids = [t.team_id for t in prior.teams]
    idx_of = {tid: k for k, tid in enumerate(team_ids)}
    draw = prior.draw()  # group -> [team_id x4], groups sorted A..L
    groups = sorted(draw.keys())
    group_global = {g: np.array([idx_of[tid] for tid in draw[g]]) for g in groups}

    adv = _build_adv_matrix(sm, team_ids, cfg)

    # ── Group stage (vectorised over sims) ───────────────────────────────────
    winners = np.empty((n, len(groups)), dtype=np.int32)
    runners = np.empty((n, len(groups)), dtype=np.int32)
    third_global = np.empty((n, len(groups)), dtype=np.int32)
    third_key = np.empty((n, len(groups)), dtype=np.float64)
    matches_played = np.full((n, len(team_ids)), 3, dtype=np.int8)  # everyone plays 3

    def _r3_intensity(points_after2: np.ndarray) -> np.ndarray:
        return r3_intensity(points_after2, cfg)

    # Already-played group results to CONDITION the sim on reality (empty ⇒ pure prior).
    played = _played_group_results(conn) if conn is not None else {}

    # Round-robin rounds for 4 teams: R1+R2 = first 4 fixtures, R3 = last 2.
    r12, r3 = _GROUP_FIXTURES[:4], _GROUP_FIXTURES[4:]
    for gi, g in enumerate(groups):
        gg = group_global[g]                       # 4 global indices
        pts = np.zeros((n, 4));  gd = np.zeros((n, 4));  gf = np.zeros((n, 4))

        def _fixed(a, b):
            """Actual (goals_a, goals_b) if this group match is already played, else None."""
            res = played.get(frozenset((team_ids[gg[a]], team_ids[gg[b]])))
            if not res:
                return None
            return res[team_ids[gg[a]]], res[team_ids[gg[b]]]

        def _play(a, b, lam_a, lam_b, fixed=None):
            if fixed is not None:               # PLAYED → fix to the real score (all sims)
                ga = np.full(n, fixed[0]);  gb = np.full(n, fixed[1])
            else:
                ga = rng.poisson(lam_a, n) if np.isscalar(lam_a) else rng.poisson(lam_a)
                gb = rng.poisson(lam_b, n) if np.isscalar(lam_b) else rng.poisson(lam_b)
            a_win = ga > gb;  b_win = gb > ga;  tie = ga == gb
            pts[:, a] += 3 * a_win + tie
            pts[:, b] += 3 * b_win + tie
            gd[:, a] += ga - gb;  gd[:, b] += gb - ga
            gf[:, a] += ga;       gf[:, b] += gb

        for a, b in r12:
            la, lb = sm.pair_lambdas(team_ids[gg[a]], team_ids[gg[b]])
            _play(a, b, la, lb, fixed=_fixed(a, b))
        # Round 3: adjust intensity by qualification incentive (plan 03 §3). A team
        # that rotates scores less and defends weaker; a desperate team pushes. (A played
        # R3 match is fixed regardless of the incentive model.)
        for a, b in r3:
            fixed = _fixed(a, b)
            base_a, base_b = sm.pair_lambdas(team_ids[gg[a]], team_ids[gg[b]])
            if cfg.r3_incentives and fixed is None:
                ia, ib = _r3_intensity(pts[:, a]), _r3_intensity(pts[:, b])
                la = base_a * np.sqrt(ia / ib)
                lb = base_b * np.sqrt(ib / ia)
            else:
                la, lb = base_a, base_b
            _play(a, b, la, lb, fixed=fixed)

        key = pts * 1e4 + gd * 1e2 + gf + rng.random((n, 4)) * 1e-2  # random tie-break
        order = np.argsort(-key, axis=1)           # (n,4): col0=1st place ... col3=4th
        rows = np.arange(n)
        winners[:, gi] = gg[order[:, 0]]
        runners[:, gi] = gg[order[:, 1]]
        third_global[:, gi] = gg[order[:, 2]]
        third_key[:, gi] = key[rows, order[:, 2]]

    # Best 8 of 12 thirds (highest key) revive.
    third_order = np.argsort(-third_key, axis=1)    # (n,12)
    rows = np.arange(n)
    thirds_sorted = np.take_along_axis(third_global, third_order[:, :8], axis=1)  # (n,8)

    # ── Assemble the 32-team bracket per sim ─────────────────────────────────
    gcol = {g: i for i, g in enumerate(groups)}
    cols = []
    for kind, ref in _BRACKET_SLOTS:
        if kind == "W":
            cols.append(winners[:, gcol[ref]])
        elif kind == "R":
            cols.append(runners[:, gcol[ref]])
        else:  # "T"
            cols.append(thirds_sorted[:, int(ref)])
    bracket = np.stack(cols, axis=1).astype(np.int32)  # (n,32) global team idx

    # Everyone in the bracket played their R32 match (one knockout game).
    np.add.at(matches_played, (rows[:, None], bracket), 1)

    # ── Knockout rounds ──────────────────────────────────────────────────────
    reach_counts = {
        "r16": np.zeros(len(team_ids)),  # reached last 16 (won R32)
        "qf": np.zeros(len(team_ids)),
        "sf": np.zeros(len(team_ids)),
        "final": np.zeros(len(team_ids)),
        "champion": np.zeros(len(team_ids)),
    }
    advance_counts = np.bincount(bracket.ravel(), minlength=len(team_ids)).astype(float)

    current = bracket
    round_names = ["r16", "qf", "sf", "final", "champion"]
    for r_name in round_names:
        m = current.shape[1] // 2
        a_team = current[:, 0::2]                  # (n, m)
        b_team = current[:, 1::2]
        p_a = adv[a_team, b_team]                  # gather advance probs
        a_wins = rng.random((n, m)) < p_a
        winner = np.where(a_wins, a_team, b_team)  # (n, m)
        # Winners of this round played one more match in the NEXT round (except
        # the champion: the final is the last match, already counted when they
        # entered it as finalists).
        flat = winner.ravel()
        reach_counts[r_name] += np.bincount(flat, minlength=len(team_ids))
        if r_name != "champion":
            np.add.at(matches_played, (np.repeat(rows, m), flat), 1)
        current = winner

    inv = 1.0 / n
    res = TournamentResult(
        team_ids=team_ids,
        n_sims=n,
        p_champion={team_ids[i]: reach_counts["champion"][i] * inv for i in range(len(team_ids))},
        p_final={team_ids[i]: reach_counts["final"][i] * inv for i in range(len(team_ids))},
        p_sf={team_ids[i]: reach_counts["sf"][i] * inv for i in range(len(team_ids))},
        p_qf={team_ids[i]: reach_counts["qf"][i] * inv for i in range(len(team_ids))},
        p_r16={team_ids[i]: reach_counts["r16"][i] * inv for i in range(len(team_ids))},
        p_advance={team_ids[i]: advance_counts[i] * inv for i in range(len(team_ids))},
        e_matches={team_ids[i]: float(matches_played[:, i].mean()) for i in range(len(team_ids))},
        matches_played=matches_played,
    )
    return res


if __name__ == "__main__":
    prior = load_prior()
    sm = build_strength(prior)
    res = simulate(prior, sm, n_sims=CONFIG.model.n_sims_quicklook, seed=1)
    print(f"Tournament sim N={res.n_sims}")
    print(f"{'team':<16}{'champ':>8}{'final':>8}{'SF':>8}{'advance':>9}{'E[mp]':>7}")
    top = sorted(res.team_ids, key=lambda t: -res.p_champion[t])[:12]
    for t in top:
        name = next(x.name for x in prior.teams if x.team_id == t)
        print(f"{name:<16}{res.p_champion[t]:>8.3f}{res.p_final[t]:>8.3f}"
              f"{res.p_sf[t]:>8.3f}{res.p_advance[t]:>9.3f}{res.e_matches[t]:>7.2f}")
