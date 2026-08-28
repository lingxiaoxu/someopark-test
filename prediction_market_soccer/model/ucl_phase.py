"""European/CONMEBOL cup phases — league-phase wrapper + KO-tree champion sim
(TRANSFORM_PLAN §3.4).

Three regimes by competition state:
  1. swiss league phase (UCL/UEL/UECL post-draw): handled by league_season with
     rank cuts (p_qual_direct/p_qual_playoff) — this module adds nothing.
  2. pre-draw swiss (now, Aug 2026): champion odds deliberately N/A (returning
     None) — pricing a 36-team field before the opponents exist would be noise.
  3. KO state (Libertadores/Sudamericana now; UCL KO from Feb 2027):
     ``ko_champion`` — Monte-Carlo over the remaining knockout tree.

KO-tree v1 honesty notes (mirrors the WC module's v1 disclosure discipline):
  * current-round tie winners use the REAL tie state (two_leg/tie_advance with
    the live aggregate carried in, per-comp ET rule);
  * later hypothetical rounds approximate each tie as one neutral-venue match
    (knockout_advance_prob) and pair survivors in bracket order as stored —
    the exact seeded path lands when the venue publishes it (R-noted).
"""
from __future__ import annotations

import numpy as np

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import Stage, get, stage_of
from prediction_market_soccer.model.dixon_coles import (
    knockout_advance_prob,
    tie_advance_prob,
    two_leg_advance_prob,
)
from prediction_market_soccer.model.strength import StrengthModel

_FINISHED = ("FT", "AET", "PEN")


def _alive_ties(conn, comp) -> list[dict]:
    """Undecided ties of the CURRENT knockout round, in stored (bracket) order."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM tie WHERE comp=? AND decided=0 ORDER BY leg1_fixture_id",
        (comp.key,))]
    return rows


def _tie_win_prob(conn, comp, sm: StrengthModel, tie: dict,
                  cmap: dict[int, str]) -> tuple[str, str, float] | None:
    """(club_a, club_b, P(a advances)) for one undecided tie, using its live state."""
    a = cmap.get(tie["team_a_api_id"]); b = cmap.get(tie["team_b_api_id"])
    if not (a in sm.ratings and b in sm.ratings):
        return None
    cfg = sm.cfg
    leg1 = conn.execute("SELECT * FROM fixture WHERE api_id=?", (tie["leg1_fixture_id"],)).fetchone()
    leg2 = conn.execute("SELECT * FROM fixture WHERE api_id=?", (tie["leg2_fixture_id"],)).fetchone()
    et = comp.et_in_ties
    if leg1 and leg1["status_short"] in _FINISHED and leg2:
        # leg 2 pending: leg-2 home side hosts. agg from tie row (a = leg-1 home).
        h2 = cmap.get(leg2["home_api_id"]); a2 = cmap.get(leg2["away_api_id"])
        if not (h2 and a2):
            return None
        lam_h, lam_a = sm.pair_lambdas(h2, a2, knockout=True)
        agg_h = tie["agg_a"] if h2 == a else tie["agg_b"]
        agg_aw = tie["agg_b"] if h2 == a else tie["agg_a"]
        p_h2 = two_leg_advance_prob(lam_h, lam_a, int(agg_h or 0), int(agg_aw or 0),
                                    rho=cfg.dc_rho, kmax=cfg.score_matrix_kmax,
                                    et_fraction=cfg.extra_time_fraction,
                                    penalty_home_edge=0.5, et_then_pens=et)
        p_a = p_h2 if h2 == a else 1.0 - p_h2
    else:
        # neither leg played: full tie expectation (a hosts leg 1, b hosts leg 2)
        l1h, l1a = sm.pair_lambdas(a, b, knockout=True)
        l2h, l2a = sm.pair_lambdas(b, a, knockout=True)
        p_a = tie_advance_prob(l1h, l1a, l2h, l2a,
                               rho=cfg.dc_rho, kmax=cfg.score_matrix_kmax,
                               et_fraction=cfg.extra_time_fraction,
                               penalty_leg2_home_edge=0.5, et_then_pens=et)
    return a, b, float(p_a)


# Alive-count → the season family that count IS. "Reaching the round of 16" means
# being one of the 16 clubs still standing when that round is played, which is exactly
# a stage of the simulation the tree already walks through — it was simply not recorded.
_LADDER_BY_ALIVE = {16: "ro16", 8: "ro8", 4: "ro4", 2: "finalist"}


def ko_ladder(conn, comp_key: str, sm: StrengthModel, *,
              n_sims: int = 20_000, seed: int | None = None) -> dict[str, dict] | None:
    """{family: {club_id: p}} for champion AND every reach-round rung the tree passes.

    The same Monte-Carlo as ``ko_champion``, recording membership at each stage rather
    than only the winner: Kalshi lists RO16 / RO8 / RO4 / FINALIST as separate season
    markets (the registry has carried those tickers all along) and the board could not
    price them because nothing produced the probabilities.

    Returns None when the comp is not in a simulable KO state (pre-draw swiss)."""
    comp = get(comp_key)
    if comp.kind == "swiss_ucl":
        # pre-draw: no field to price. Post-draw league phase → league_season path.
        return None
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    ties = _alive_ties(conn, comp)
    entries = []
    for t in ties:
        e = _tie_win_prob(conn, comp, sm, t, cmap)
        if e:
            entries.append(e)
    if not entries:
        return None

    rng = np.random.default_rng(seed if seed is not None else CONFIG.model.random_seed)
    cfg = sm.cfg
    clubs = sorted({c for e in entries for c in e[:2]})
    champ_count = {c: 0 for c in clubs}
    reach: dict[int, dict[str, int]] = {}

    # neutral single-match advance matrix for hypothetical later rounds
    def p_beat(x: str, y: str) -> float:
        lam_h, lam_a = sm.pair_lambdas(x, y, knockout=True, neutral=True)
        return knockout_advance_prob(lam_h, lam_a, rho=cfg.dc_rho, kmax=cfg.score_matrix_kmax,
                                     et_fraction=cfg.extra_time_fraction, penalty_home_edge=0.5)

    # vectorized MC: first round from tie probs; later rounds pair survivors in
    # bracket order with cached neutral-match win probs (v1 disclosed approx).
    import numpy as _np
    pcache: dict[tuple, float] = {}
    ent_p = _np.array([e[2] for e in entries])
    names = []
    for a, b, _pp in entries:
        names.extend([a, b])
    def _record(cols) -> None:
        """Count each club still alive at this stage (once per simulation)."""
        k = int(cols.shape[1])
        if k not in _LADDER_BY_ALIVE:
            return
        d = reach.setdefault(k, {c: 0 for c in clubs})
        for idx, cnt in zip(*_np.unique(cols, return_counts=True)):
            d[names[int(idx)]] += int(cnt)

    # Everyone in a live tie has ALREADY reached the round those ties constitute.
    _entrants = _np.tile(_np.arange(2 * len(entries)), (n_sims, 1))
    _record(_entrants)
    cur = _np.where(rng.random((n_sims, len(entries))) < ent_p[None, :],
                    _np.arange(len(entries)) * 2, _np.arange(len(entries)) * 2 + 1)
    _record(cur)
    while cur.shape[1] > 1:
        n_pairs = cur.shape[1] // 2
        nxt_cols = []
        for i2 in range(n_pairs):
            x_idx, y_idx = cur[:, 2 * i2], cur[:, 2 * i2 + 1]
            pw = _np.empty(n_sims)
            for pair in set(zip(x_idx.tolist(), y_idx.tolist())):
                kk = (names[pair[0]], names[pair[1]])
                if kk not in pcache:
                    pcache[kk] = p_beat(*kk)
                mask = (x_idx == pair[0]) & (y_idx == pair[1])
                pw[mask] = pcache[kk]
            nxt_cols.append(_np.where(rng.random(n_sims) < pw, x_idx, y_idx))
        if cur.shape[1] % 2:
            nxt_cols.append(cur[:, -1])
        cur = _np.stack(nxt_cols, axis=1)
        _record(cur)
    winners = cur[:, 0]
    for w_idx, cnt in zip(*_np.unique(winners, return_counts=True)):
        champ_count[names[int(w_idx)]] = int(cnt)
    out: dict[str, dict] = {
        "champion": {c: round(nn / n_sims, 5) for c, nn in champ_count.items() if nn > 0}}
    for alive, fam in _LADDER_BY_ALIVE.items():
        d = reach.get(alive)
        if d:
            out[fam] = {c: round(nn / n_sims, 5) for c, nn in d.items() if nn > 0}
    return out


def ko_champion(conn, comp_key: str, sm: StrengthModel, *,
                n_sims: int = 20_000, seed: int | None = None) -> dict[str, float] | None:
    """P(champion) over the remaining knockout tree of a cup-state competition.

    Returns None when the comp is not in a simulable KO state (pre-draw swiss)."""
    lad = ko_ladder(conn, comp_key, sm, n_sims=n_sims, seed=seed)
    return lad["champion"] if lad else None


if __name__ == "__main__":
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.strength import build_strength
    conn = store.init_db()
    for lg in ("libertadores", "sudamericana"):
        sm = build_strength(load_prior(lg), league=lg)
        pc = ko_champion(conn, lg, sm, n_sims=20_000)
        if pc:
            top = sorted(pc.items(), key=lambda kv: -kv[1])[:6]
            print(f"— {lg} KO champion: " + ", ".join(f"{c} {p:.1%}" for c, p in top)
                  + f"  (Σ={sum(pc.values()):.3f})")
        else:
            print(f"— {lg}: not in a simulable KO state")